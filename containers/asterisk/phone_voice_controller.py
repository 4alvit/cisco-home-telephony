"""Bounded bridge between stock-phone RTP and the local Assist coordinator.

No import or constructor starts audio. The runtime supplies a lease-owning
VoiceSession, an AssistantService and the reviewed TTS decoder. The independent
transport watchdog remains mandatory for a real phone.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import struct
import time

from phone_voice_transport import MAX_SESSION_SECONDS


def upsample_8k_frame(pcm: bytes) -> bytes:
    """One 20-ms PCM frame, 8 -> 16 kHz; no frame history or hidden buffering.

    Linear interpolation cannot recover frequencies absent in G.711. It only
    adapts the narrowband signal to the existing 16-kHz wake/STT frontend.
    """
    if type(pcm) is not bytes or len(pcm) != 320:
        raise ValueError('expected_20ms_pcm8k')
    samples = struct.unpack('<160h', pcm)
    output = []
    for i, sample in enumerate(samples):
        output.extend((sample, (sample + samples[min(i + 1, 159)]) // 2))
    return struct.pack('<320h', *output)


def playback_frames(pcm: bytes, sample_rate: int):
    """Bounded mono s16le speech frames; pair-average when decoding at 16 kHz."""
    if (type(pcm) is not bytes or len(pcm) % 2 or sample_rate not in (8000, 16000)
            or type(sample_rate) is not int or len(pcm) > sample_rate * 2 * 30):
        raise ValueError('invalid_bounded_tts_pcm')
    if sample_rate == 16000:
        samples = struct.unpack('<' + 'h' * (len(pcm) // 2), pcm)
        pcm = struct.pack('<' + 'h' * ((len(samples) + 1) // 2),
                          *((samples[i] + samples[min(i + 1, len(samples) - 1)]) // 2
                            for i in range(0, len(samples), 2)))
    for offset in range(0, len(pcm), 320):
        yield pcm[offset:offset + 320].ljust(320, b'\0')


def _playback_transport_stopped(session):
    # The controller closes transport before waiting for assistant cleanup.
    # A queued frame can observe that orderly close before its task is canceled;
    # it is canceled output, not a provider failure or a completed reply.
    if getattr(session, 'reason', None) in (
            'explicit_stop', 'display_stop', 'shutdown', 'lease_expired', 'duration_expired'):
        raise asyncio.CancelledError
    raise RuntimeError('playback_transport_stopped')


class RtpPCMSink:
    """The WavePlayback sink contract, with a single physical-phone target.

    Each input block is at most 20 ms. Linear resampling is speech-oriented;
    this does not imply wideband output from a narrowband G.711 phone path.
    """
    def __init__(self, session, phone_id):
        self.session, self.phone_id = session, phone_id
        self.generation = 0
        self.frames_sent = 0

    async def write_pcm(self, phone_id, data, sample_rate):
        if (phone_id != self.phone_id or type(data) is not bytes or len(data) % 2
                or type(sample_rate) is not int or sample_rate not in (8000, 16000, 22050, 24000, 44100, 48000)
                or len(data) > (sample_rate // 50) * 2):
            raise ValueError('invalid_phone_pcm_block')
        if not data:
            return
        values = struct.unpack('<' + 'h' * (len(data) // 2), data)
        count = min(160, math.ceil(len(values) * 8000 / sample_rate))
        output = []
        for index in range(count):
            position = index * sample_rate / 8000
            low = min(int(position), len(values) - 1)
            high = min(low + 1, len(values) - 1)
            output.append(round(values[low] + (values[high] - values[low]) * (position - low)))
        frame = struct.pack('<' + 'h' * count, *output).ljust(320, b'\0')
        generation = self.generation
        while generation == self.generation:
            if await asyncio.to_thread(self.session.play, frame,
                                       allowed=lambda: generation == self.generation):
                self.frames_sent += 1
                return
            if self.session.state != 'active':
                _playback_transport_stopped(self.session)
            await asyncio.sleep(.005)

    async def stop(self, phone_id):
        if phone_id != self.phone_id:
            raise ValueError('wrong_phone')
        self.generation += 1


class PhonePCMPlayback:
    """Decode a result off-phone, then use the session's guarded, paced sender.

    `decode(result)` is async and returns `(mono_s16le_bytes, sample_rate)`.
    Cancellation invalidates queued frames; stop does not open or restart RTP.
    The outer controller closes the owned RTP session in every exit path.
    """
    def __init__(self, session, decode):
        self.session, self.decode = session, decode
        self.generation = 0
        self.frames_sent = 0

    async def play(self, phone_id, result):
        generation = self.generation
        pcm, rate = await self.decode(result)
        for frame in playback_frames(pcm, rate):
            if generation != self.generation:
                return
            while generation == self.generation:
                if await asyncio.to_thread(self.session.play, frame,
                                           allowed=lambda: generation == self.generation):
                    self.frames_sent += 1
                    break
                if self.session.state != 'active':
                    _playback_transport_stopped(self.session)
                await asyncio.sleep(.005)

    async def stop(self, phone_id):
        self.generation += 1


@dataclass(frozen=True)
class ControllerResult:
    reason: str
    input_frames: int
    stop_confirmed: bool
    assistant_state: str


class PhoneVoiceController:
    """One finite foreground lease; wake turns may rearm within its deadline."""
    def __init__(self, session, assistant, phone_id, *, duration=30,
                 receive_idle_seconds=3, clock=time.monotonic):
        if (type(duration) not in (int, float) or not 0 < duration <= MAX_SESSION_SECONDS
                or not math.isfinite(duration)):
            raise ValueError('duration_must_be_at_most_300_seconds')
        self.session, self.assistant, self.phone_id = session, assistant, phone_id
        if (type(receive_idle_seconds) not in (int, float) or not math.isfinite(receive_idle_seconds)
                or not 0 < receive_idle_seconds <= 10):
            raise ValueError('invalid_receive_idle_timeout')
        self.receive_idle_seconds = receive_idle_seconds
        self.duration, self.clock = duration, clock
        self._stop = asyncio.Event()
        self._started = False
        self._reason = 'explicit_stop'

    def request_stop(self, reason='explicit_stop'):
        # Only fixed categories can be emitted in public summaries.
        self._reason = reason if reason in ('explicit_stop', 'display_stop', 'shutdown') else 'explicit_stop'
        self._stop.set()

    async def run(self, *, mode='manual'):
        if self._started or mode not in ('manual', 'wake'):
            raise ValueError('single_use_controller_or_invalid_mode')
        self._started = True
        frames, reason = 0, 'start_failed'
        deadline = self.clock() + self.duration
        try:
            self.assistant.ensure_allowed(self.phone_id)
            if self._stop.is_set():
                reason = self._reason
            else:
                await asyncio.to_thread(self.session.start)
                stream_id = None
                if not self._stop.is_set():
                    stream_id = await (self.assistant.activate(self.phone_id) if mode == 'manual'
                                       else self.assistant.listen(self.phone_id))
                last_audio = self.clock()
                was_processing = False
                reason = 'lease_expired'
                while self.clock() < deadline and not self._stop.is_set():
                    batch = await asyncio.to_thread(self.session.poll)
                    if self.session.state != 'active':
                        reason = ('lease_expired' if getattr(self.session, 'reason', None) == 'duration_expired'
                                  else 'transport_stopped')
                        break
                    if self.clock() >= deadline:
                        break
                    state = self.assistant.status(self.phone_id).state
                    if state in ('stopped', 'error') or mode == 'manual' and state == 'armed':
                        reason = 'assistant_error' if state == 'error' else 'turn_complete'
                        break
                    now = self.clock()
                    processing = state in ('thinking', 'speaking')
                    # RTP polling still checks focus, socket failures and the
                    # lease during a reply. Missing input is only meaningful
                    # while capturing; resume with a fresh, bounded grace.
                    if batch or processing or was_processing:
                        last_audio = now
                    was_processing = processing
                    if not processing and now - last_audio >= self.receive_idle_seconds:
                        reason = 'receive_idle_timeout'
                        break
                    for pcm in batch:
                        if self._stop.is_set():
                            break
                        self.assistant.feed_pcm(self.phone_id, stream_id, upsample_8k_frame(pcm))
                        frames += 1
                    state = self.assistant.status(self.phone_id).state
                    if state in ('stopped', 'error'):
                        reason = 'turn_complete' if state == 'stopped' else 'assistant_error'
                        break
                    await asyncio.sleep(0)
                if self._stop.is_set():
                    reason = self._reason
        except asyncio.CancelledError:
            reason = 'shutdown'
            raise
        finally:
            # Stop remote capture before waiting for provider tasks to cancel.
            # The watchdog handles process death or a blocked network cleanup.
            try:
                await asyncio.to_thread(self.session.stop, reason)
            finally:
                await self.assistant.stop(self.phone_id, reason=reason)
        return ControllerResult(reason, frames, self.session.stop_confirmed,
                                self.assistant.status(self.phone_id).state)
