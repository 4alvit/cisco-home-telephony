"""Single-event-loop coordinator; transport and physical media remain external."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import math
import secrets
import time
from typing import Any, AsyncIterator, Callable, Protocol

from .audio import UtteranceEndpoint


FRAME_BYTES = 640  # 20 ms, signed little-endian int16, mono, 16 kHz
REPLY_TEXT_LIMIT = 1000
REPLY_SCAN_LIMIT = 4096


def _reply_text(value: Any) -> str:
    """Bound and sanitize ephemeral display text without coercing provider data."""
    if type(value) is not str:
        return ""
    clean = []
    for char in value[:REPLY_SCAN_LIMIT]:
        code = ord(char)
        if (code in (9, 10, 13) or 0x20 <= code <= 0xD7FF or
                0xE000 <= code <= 0xFFFD or 0x10000 <= code <= 0x10FFFF):
            clean.append(char)
            if len(clean) == REPLY_TEXT_LIMIT:
                break
    return "".join(clean)


class Detector(Protocol):
    def feed(self, pcm: bytes) -> bool: ...
    def reset(self) -> None: ...


class Assist(Protocol):
    async def run(self, audio: AsyncIterator[bytes], *,
                  conversation_id: str | None = None, on_event: Any = None) -> Any: ...


class Playback(Protocol):
    async def play(self, phone_id: str, result: Any) -> None: ...
    async def stop(self, phone_id: str) -> None: ...


@dataclass(frozen=True)
class ServiceSettings:
    enabled: bool = False
    rearm_after_turn: bool = False
    preroll_ms: int = 320
    queue_frames: int = 100
    max_utterance_seconds: float = 12.0
    turn_timeout_seconds: float = 35.0
    audio_idle_timeout_seconds: float = 2.0
    duplicate_window_seconds: float = 2.0
    local_endpointing: bool = False
    # Starting point only: tune against the telephone's microphone/noise floor.
    speech_rms_threshold: float = 0.01

    def __post_init__(self) -> None:
        for name in ("enabled", "rearm_after_turn", "local_endpointing"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        if type(self.preroll_ms) is not int or not 0 <= self.preroll_ms <= 2000 or self.preroll_ms % 20:
            raise ValueError("preroll_ms must be a multiple of 20 in 0..2000")
        if type(self.queue_frames) is not int or not self.preroll_ms // 20 + 2 <= self.queue_frames <= 1000:
            raise ValueError("queue_frames must hold pre-roll plus two frames and be <= 1000")
        for name, low, high in (("max_utterance_seconds", .1, 60),
                                ("turn_timeout_seconds", .1, 180),
                                ("audio_idle_timeout_seconds", .05, 10),
                                ("duplicate_window_seconds", 0, 30),
                                ("speech_rms_threshold", .000001, 1)):
            value = getattr(self, name)
            if type(value) not in (float, int) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{name} outside allowed range")
        if self.preroll_ms > self.max_utterance_seconds * 1000:
            raise ValueError("pre-roll must not exceed the utterance limit")


@dataclass(frozen=True)
class VoiceEvent:
    phone_id: str
    state: str
    reason: str | None = None


@dataclass
class _Session:
    detector: Detector
    permitted: bool
    state: str = "stopped"
    stream_id: str | None = None
    conversation_id: str | None = None
    preroll: deque[bytes] = field(default_factory=deque)
    queue: asyncio.Queue[bytes | None] | None = None
    task: asyncio.Task[None] | None = None
    captured_frames: int = 0
    input_closed: bool = False
    error: str | None = None
    reason: str | None = None
    revision: int = 0
    wake_detection: bool = False
    endpoint: UtteranceEndpoint | None = None
    lifecycle_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class AssistantService:
    """Embed behind an authenticated RTP controller, never expose this directly.

    No constructor starts listening. `listen` must follow a deliberate controller
    action and its returned stream ID must accompany each input frame. Stop,
    preemption and reconnect invalidate that ID. Calls must share one event loop.
    """

    def __init__(self, settings: ServiceSettings, phones: dict[str, bool],
                 detector_factory: Callable[[str], Detector], assist: Assist,
                 playback: Playback, on_event: Callable[[VoiceEvent], None] | None = None,
                 clock: Callable[[], float] = time.monotonic, *,
                 on_reply: Callable[[str, str, str], None] | None = None):
        if not phones or len(phones) > 100 or any(
                not isinstance(key, str) or not key or len(key) > 128 or type(value) is not bool
                for key, value in phones.items()):
            raise ValueError("phones must map bounded opaque IDs to boolean permissions")
        self.settings, self.assist, self.playback = settings, assist, playback
        self._clock, self._on_event = clock, on_event
        self._on_reply = on_reply
        self._sessions = {key: _Session(detector_factory(key), value,
                                       preroll=deque(maxlen=settings.preroll_ms // 20))
                          for key, value in phones.items()}
        if len({id(item.detector) for item in self._sessions.values()}) != len(phones):
            raise ValueError("each phone must have an independent detector")
        self._owner: str | None = None
        self._quiet_until = 0.0

    def _emit(self, phone_id: str, state: str, reason: str | None = None) -> None:
        session = self._sessions[phone_id]
        session.state = state
        session.reason = reason
        if self._on_event:
            # UI/metrics must not prevent cancellation or audio cleanup.
            try:
                self._on_event(VoiceEvent(phone_id, state, reason))
            except Exception:
                pass

    def status(self, phone_id: str) -> VoiceEvent:
        item = self._sessions[phone_id]
        return VoiceEvent(phone_id, item.state, item.reason)

    def owns_reply(self, phone_id: str, stream_id: str) -> bool:
        """Whether a synchronous display update still belongs to this speaking turn."""
        if type(phone_id) is not str or type(stream_id) is not str:
            return False
        item = self._sessions.get(phone_id)
        return item is not None and item.stream_id == stream_id and item.state == "speaking"

    def _emit_armed(self, phone_id: str) -> None:
        """Report optional wake-model readiness only when the status changes."""
        item = self._sessions[phone_id]
        ready = getattr(item.detector, "ready", None)
        reason = None if ready is None else "wake_ready" if ready else "model_warmup"
        if item.state != "armed" or item.reason != reason:
            self._emit(phone_id, "armed", reason)

    def ensure_allowed(self, phone_id: str) -> None:
        """Check permission before the controller starts any physical media.

        This synchronous check has no side effects and does not arm a stream.
        Unknown phones are unpermitted. Listening rechecks the same gate.
        """
        item = self._sessions.get(phone_id) if isinstance(phone_id, str) else None
        if not self.settings.enabled or item is None or not item.permitted:
            raise PermissionError("voice listening is disabled or phone is not permitted")

    async def listen(self, phone_id: str) -> str:
        """Explicit opt-in for a new transport session; never called on reconnect."""
        return await self._begin_stream(phone_id, wake_detection=True)

    async def _begin_stream(self, phone_id: str, *, wake_detection: bool) -> str:
        """Create a stream; manual activation does not wait for wake readiness."""
        self.ensure_allowed(phone_id)
        item = self._sessions[phone_id]
        item.revision += 1
        revision = item.revision
        item.stream_id = None
        async with item.lifecycle_lock:
            if revision != item.revision:
                raise RuntimeError("activation superseded by a later control action")
            await self._stop_locked(phone_id, reason="new_session", clear_conversation=False)
            if revision != item.revision:
                raise RuntimeError("activation superseded by a later control action")
            item.detector.reset()
            item.error = None
            item.stream_id = secrets.token_urlsafe(18)
            item.wake_detection = wake_detection
            if wake_detection:
                self._emit_armed(phone_id)
            else:
                self._emit(phone_id, "armed")
            return item.stream_id

    async def stop(self, phone_id: str, reason: str = "user_stop", *,
                   clear_conversation: bool = True) -> None:
        item = self._sessions[phone_id]
        item.revision += 1
        # Invalidate synchronously before waiting for an earlier sink operation.
        item.stream_id = None
        async with item.lifecycle_lock:
            await self._stop_locked(phone_id, reason, clear_conversation=clear_conversation)

    async def _stop_locked(self, phone_id: str, reason: str, *, clear_conversation: bool) -> None:
        item = self._sessions[phone_id]
        item.stream_id = None
        task = item.task
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            await self.playback.stop(phone_id)
        finally:
            item.detector.reset()
            item.preroll.clear()
            item.queue = None
            item.task = None
            item.endpoint = None
            if self._owner == phone_id:
                self._owner = None
                self._quiet_until = self._clock() + self.settings.duplicate_window_seconds
            if clear_conversation:
                item.conversation_id = None
            self._emit(phone_id, "stopped", reason)

    async def preempt(self, phone_id: str, reason: str = "call") -> None:
        """Call, ringing alert, radio, mute, receiver failure or lease expiry."""
        await self.stop(phone_id, reason=reason)

    async def activate(self, phone_id: str) -> str:
        """Manual push-to-talk start; bypasses wake inference, not enable gates."""
        if self._owner is not None or self._clock() < self._quiet_until:
            raise RuntimeError("another turn is active or cooling down")
        stream_id = await self._begin_stream(phone_id, wake_detection=False)
        # Stream cleanup can suspend while another phone completes a turn and
        # starts its duplicate window. Recheck both gates after that await.
        if self._owner is not None or self._clock() < self._quiet_until:
            await self.stop(phone_id, reason="duplicate_activation")
            raise RuntimeError("another turn is active or cooling down")
        self._start_turn(phone_id, stream_id)
        return stream_id

    def feed_pcm(self, phone_id: str, stream_id: str, pcm: bytes) -> bool:
        """Accept one already decoded/resampled 20 ms frame, or reject stale media."""
        item = self._sessions[phone_id]
        if item.stream_id != stream_id or item.state not in ("armed", "listening"):
            return False
        if type(pcm) is not bytes or len(pcm) != FRAME_BYTES:
            raise ValueError("input must be exactly 640 bytes of 16 kHz mono s16le PCM")
        if item.state == "listening":
            if item.input_closed:
                return False
            if item.queue is None:
                return False
            if item.queue.qsize() >= self.settings.queue_frames - 1:
                item.error = "audio_backpressure"
                item.stream_id = None
                if item.task:
                    item.task.cancel()
                return False
            item.queue.put_nowait(pcm)
            item.captured_frames += 1
            if item.endpoint is not None:
                item.endpoint.observe(pcm, self._clock())
                self._check_endpoint(phone_id, stream_id)
            if item.captured_frames * .02 >= self.settings.max_utterance_seconds:
                self.end_utterance(phone_id, stream_id)
            return True
        item.preroll.append(pcm)
        try:
            detected = item.detector.feed(pcm)
            self._emit_armed(phone_id)
        except Exception:
            item.stream_id = None
            item.preroll.clear()
            item.error = "wake_inference_failed"
            self._emit(phone_id, "error", item.error)
            return False
        if not detected:
            return True
        item.detector.reset()
        if self._owner is not None or self._clock() < self._quiet_until:
            item.preroll.clear()
            self._emit_armed(phone_id)
            return True
        self._start_turn(phone_id, stream_id, pcm)
        return True

    def _start_turn(self, phone_id: str, stream_id: str, trigger_frame: bytes | None = None) -> None:
        item = self._sessions[phone_id]
        self._owner = phone_id
        item.queue = asyncio.Queue(maxsize=self.settings.queue_frames)
        for frame in item.preroll:
            item.queue.put_nowait(frame)
        if not item.preroll and trigger_frame is not None:
            item.queue.put_nowait(trigger_frame)
        item.captured_frames = item.queue.qsize()
        item.preroll.clear()
        item.input_closed = False
        item.error = None
        item.endpoint = (UtteranceEndpoint(self._clock(), self.settings.speech_rms_threshold)
                         if self.settings.local_endpointing else None)
        if item.captured_frames * .02 >= self.settings.max_utterance_seconds:
            self.end_utterance(phone_id, stream_id)
        self._emit(phone_id, "listening")
        item.task = asyncio.create_task(self._run_turn(phone_id, stream_id))
        item.task.add_done_callback(lambda task: self._cancelled_before_start(phone_id, task))

    def _cancelled_before_start(self, phone_id: str, task: asyncio.Task[None]) -> None:
        item = self._sessions[phone_id]
        if not task.cancelled() or self._owner != phone_id or item.task is not task:
            return
        # A task cancelled before its first instruction does not run `finally`.
        self._owner = None
        self._quiet_until = self._clock() + self.settings.duplicate_window_seconds
        item.queue = None
        item.endpoint = None
        item.preroll.clear()
        item.detector.reset()
        item.stream_id = None
        self._emit(phone_id, "error" if item.error else "stopped", item.error)

    def end_utterance(self, phone_id: str, stream_id: str) -> bool:
        """Optional upstream VAD/end button; HA's own STT VAD may finish first."""
        item = self._sessions[phone_id]
        if item.stream_id != stream_id or item.queue is None or item.input_closed:
            return False
        item.input_closed = True
        item.queue.put_nowait(None)  # one slot is reserved by feed_pcm
        return True

    def _check_endpoint(self, phone_id: str, stream_id: str) -> None:
        item = self._sessions[phone_id]
        if (item.stream_id != stream_id or item.state != "listening"
                or item.input_closed or item.endpoint is None):
            return
        decision = item.endpoint.decision(self._clock())
        if decision == "end_of_speech":
            self.end_utterance(phone_id, stream_id)
            self._emit(phone_id, "thinking")
        elif decision is not None:
            # No speech is not an invitation to execute an intent or rearm.
            item.error = decision
            item.stream_id = None
            if item.task is not None:
                item.task.cancel()

    async def _watch_capture(self, phone_id: str, stream_id: str) -> None:
        item = self._sessions[phone_id]
        while item.stream_id == stream_id and item.state == "listening" and not item.input_closed:
            self._check_endpoint(phone_id, stream_id)
            await asyncio.sleep(.05)

    async def _audio(self, item: _Session) -> AsyncIterator[bytes]:
        assert item.queue is not None
        while True:
            pending = asyncio.create_task(item.queue.get())
            try:
                # The local clocks distinguish absent first audio, a silent
                # user, and a short command. Do not let the old idle timer
                # cut those grace periods short before HA VAD has engaged.
                local_wait = item.endpoint is not None and not item.endpoint.ha_vad_started
                timeout = .1 if local_wait else self.settings.audio_idle_timeout_seconds
                # Unlike wait_for, wait does not absorb cancellation when a
                # queued frame becomes ready at the same moment as Stop.
                done, _ = await asyncio.wait((pending,), timeout=timeout)
                if not done:
                    if local_wait:
                        continue
                    item.input_closed = True
                    return
                frame = pending.result()
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            if frame is None:
                return
            yield frame

    async def _run_turn(self, phone_id: str, stream_id: str) -> None:
        item = self._sessions[phone_id]

        async def event(message: dict[str, Any]) -> None:
            if item.stream_id != stream_id:
                return
            if message.get("type") == "stt-vad-start" and item.endpoint is not None:
                item.endpoint.ha_vad_started = True
            if message.get("type") in ("stt-vad-end", "stt-end", "intent-start", "tts-start"):
                self.end_utterance(phone_id, stream_id)
                self._emit(phone_id, "thinking")

        async def turn() -> None:
            result = await self.assist.run(self._audio(item), conversation_id=item.conversation_id,
                                           on_event=event)
            if item.stream_id != stream_id:
                return
            item.conversation_id = result.conversation_id or item.conversation_id
            self._emit(phone_id, "speaking")
            if self._on_reply is not None:
                # Deliver cancellation requested reentrantly by the state hook
                # before publishing text; this also works on Python 3.10.
                await asyncio.sleep(0)
            if not self.owns_reply(phone_id, stream_id):
                return
            if self._on_reply is not None:
                # This synchronous, non-blocking UI hook is deliberately separate
                # from VoiceEvent: reply text must not enter status or journals.
                try:
                    text = _reply_text(getattr(result, "text", None))
                    if text and self.owns_reply(phone_id, stream_id):
                        self._on_reply(phone_id, stream_id, text)
                except Exception:
                    # Never expose provider text through callback exception output.
                    pass
                # A reply hook may itself request cancellation. Do not start
                # playback until that cancellation has had a chance to arrive.
                await asyncio.sleep(0)
            if not self.owns_reply(phone_id, stream_id):
                return
            await self.playback.play(phone_id, result)

        watcher = (asyncio.create_task(self._watch_capture(phone_id, stream_id))
                   if item.endpoint is not None else None)
        completed = False
        try:
            await asyncio.wait_for(turn(), self.settings.turn_timeout_seconds)
            completed = True
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            item.error = "turn_timeout"
        except Exception:
            # Provider exceptions can contain URLs, credentials or transcript text.
            item.error = "turn_failed"
        finally:
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            try:
                await self.playback.stop(phone_id)
            except Exception:
                item.error = item.error or "playback_stop_failed"
            item.queue = None
            item.endpoint = None
            item.preroll.clear()
            item.detector.reset()
            self._owner = None
            self._quiet_until = self._clock() + self.settings.duplicate_window_seconds
            for other_id, other in self._sessions.items():
                other.preroll.clear()
                other.detector.reset()
                if other.stream_id is not None and other.state == "armed":
                    self._emit_armed(other_id)
            if item.error:
                item.stream_id = None
                self._emit(phone_id, "error", item.error)
            elif (completed and item.stream_id == stream_id and item.wake_detection
                  and self.settings.rearm_after_turn):
                self._emit_armed(phone_id)
            else:
                item.stream_id = None
                self._emit(phone_id, "stopped", "turn_complete" if completed else "turn_cancelled")

    async def wait_idle(self, phone_id: str) -> None:
        task = self._sessions[phone_id].task
        if task:
            await asyncio.gather(asyncio.shield(task), return_exceptions=True)

    async def close(self) -> None:
        results = await asyncio.gather(
            *(self.stop(phone_id, reason="shutdown") for phone_id in self._sessions),
            return_exceptions=True)
        if any(isinstance(result, BaseException) for result in results):
            raise RuntimeError("one or more playback stops failed during shutdown")
