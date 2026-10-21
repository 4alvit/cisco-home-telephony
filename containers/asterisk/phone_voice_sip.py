"""Bounded AudioSocket PCM and coordinator integration primitives.

The supplied PbxPort must prove its admission against the existing
physical-phone reservation, exact channel identity, and PBX absolute timeout.
An AudioSocket UUID correlates a stream; it is NOT authentication. Deployment
must additionally fence the TCP peer/topology. This file deliberately provides
neither a listener nor an auto-answer implementation. The opted-in runner and
phone_voice_sip_ami module supply those operations under the existing owner.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import struct
import time
from typing import Protocol
from uuid import UUID

FRAME_BYTES = 640
SILENCE_PCM = bytes(FRAME_BYTES)
MAX_SECONDS = 300
MAX_PAYLOAD = FRAME_BYTES * 10


class BoundaryError(RuntimeError):
    """Fixed category only; never include network/provider data."""


@dataclass(frozen=True)
class Channel:
    name: str
    uniqueid: str
    line: str


@dataclass(frozen=True)
class Grant:
    session: UUID
    phone: str
    channel: Channel
    created_at: float
    deadline: float

    def __post_init__(self):
        if (type(self.session) is not UUID or not self.phone or
                not all((self.channel.name, self.channel.uniqueid, self.channel.line)) or
                not math.isfinite(self.created_at) or not math.isfinite(self.deadline) or
                not 0 < self.deadline - self.created_at <= MAX_SECONDS):
            raise ValueError('invalid_grant')


@dataclass(frozen=True)
class Snapshot:
    phone: str
    request_started_at: float
    registered: bool
    # Every active/held/ringing channel on BOTH lines, not only the chosen line.
    channels: tuple[Channel, ...]
    # All endpoint lines reporting a non-idle state. Unknown states must produce
    # registered=False or an unknown snapshot in the eventual observer adapter.
    occupied_lines: frozenset[str]
    # Known busy/ringing/held endpoint states conflict even if the channel list
    # still contains only our call. This is separate from endpoint availability.
    conflicting_call_state: bool = False


def focus(grant: Grant, snapshot: Snapshot | None, now: float) -> str:
    if now < grant.created_at:
        return 'unknown'
    if now >= grant.deadline:
        return 'lease_expired'
    if (snapshot is None or snapshot.phone != grant.phone or
            not 0 <= now - snapshot.request_started_at <= 1):
        return 'unknown'
    if not snapshot.registered:
        return 'disconnected'
    if snapshot.conflicting_call_state:
        return 'call_changed'
    if snapshot.channels != (grant.channel,):
        # Do not exempt other calls merely because they share the line/linkedid.
        return 'call_changed'
    if snapshot.occupied_lines != frozenset((grant.channel.line,)):
        return 'call_changed'
    return 'owned_call'


class PbxPort(Protocol):
    async def validate_admission(self, grant: Grant) -> bool:
        """Verify lock + exact active call + PBX timeout <= remaining grant.

        The timeout must have been armed on the actual PHONE channel before
        answering/bridging it. A child-process timer or AudioSocket-leg timeout
        alone does not satisfy this contract. Admission is single use.
        """
        ...

    def snapshot(self) -> Snapshot | None:
        """Nonblocking complete AMI snapshot, aged from request start."""
        ...

    async def hangup_and_confirm(self, grant: Grant) -> bool:
        """Match channel AND Uniqueid before Hangup; prove it absent afterward.

        Never hang up a replacement channel reusing the name. Return False on
        incomplete observations. Do not release the physical-phone reservation.
        """
        ...


class Wire:
    """Exactly one externally accepted AudioSocket stream, bounded in memory."""
    def __init__(self, reader, writer, expected: UUID):
        self.reader, self.writer, self.expected = reader, writer, expected
        self.pending = bytearray()
        self.admitted = self.closed = False
        self.frames_received = self.frames_sent = 0

    async def _packet(self):
        try:
            header = await self.reader.readexactly(3)
            kind, size = header[0], int.from_bytes(header[1:], 'big')
            # Refuse excessive declared length before allocating/reading it.
            if size > MAX_PAYLOAD:
                raise BoundaryError('oversized_packet')
            data = await self.reader.readexactly(size)
        except asyncio.IncompleteReadError as exc:
            raise BoundaryError('peer_closed') from exc
        return kind, data

    async def admit(self):
        if self.closed or self.admitted:
            raise BoundaryError('single_use_wire')
        kind, data = await self._packet()
        if kind != 1 or data != self.expected.bytes:
            raise BoundaryError('stream_identity_mismatch')
        self.admitted = True

    async def read_pcm(self):
        if not self.admitted or self.closed:
            raise BoundaryError('wire_not_admitted')
        while len(self.pending) < FRAME_BYTES:
            kind, data = await self._packet()
            if kind == 0 and not data:
                raise BoundaryError('peer_ended')
            if kind == 3 and len(data) == 1 and data in b'0123456789ABCD*#':
                if data == b'#':
                    raise BoundaryError('dtmf_stop')
                continue
            if kind == 0xff:
                raise BoundaryError('peer_error')
            if kind != 0x12 or not data or len(data) % 2:
                raise BoundaryError('unsupported_audio_packet')
            self.pending.extend(data)
        result = bytes(self.pending[:FRAME_BYTES])
        del self.pending[:FRAME_BYTES]
        self.frames_received += 1
        return result

    async def write_pcm(self, pcm):
        if not self.admitted or self.closed or len(pcm) != FRAME_BYTES:
            raise BoundaryError('wire_not_ready')
        self.writer.write(b'\x12\x02\x80' + pcm)
        await asyncio.wait_for(self.writer.drain(), .25)
        self.frames_sent += 1

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.pending.clear()
        # Closing TCP is a protocol termination. No potentially blocking final
        # write is needed; it never substitutes for exact PBX Hangup proof.
        self.writer.close()
        await asyncio.wait_for(self.writer.wait_closed(), .5)


class Sink:
    """WavePlayback sink: 16kHz wire PCM, <=20ms writes, no playback queue."""
    def __init__(self, wire: Wire, grant: Grant, guard, clock=time.monotonic):
        self.wire, self.grant, self.guard, self.clock = wire, grant, guard, clock
        self.generation = 0
        self.next_frame = 0.0
        self.lock = asyncio.Lock()
        self.pending_tts = 0
        self.silence_frames = 0

    async def write_pcm(self, phone, data, sample_rate):
        if (phone != self.grant.phone or type(data) is not bytes or len(data) % 2 or
                type(sample_rate) is not int or
                sample_rate not in (8000, 16000, 22050, 24000, 44100, 48000) or
                len(data) > (sample_rate // 50) * 2):
            raise ValueError('invalid_pcm_block')
        if not data:
            return
        generation = self.generation
        self.pending_tts += 1
        try:
            async with self.lock:
                if generation != self.generation:
                    raise asyncio.CancelledError
                samples = struct.unpack('<' + 'h' * (len(data) // 2), data)
                count = min(320, math.ceil(len(samples) * 16000 / sample_rate))
                resampled = []
                for index in range(count):
                    pos = index * sample_rate / 16000
                    low = min(int(pos), len(samples) - 1)
                    high = min(low + 1, len(samples) - 1)
                    resampled.append(round(samples[low] + (samples[high] - samples[low]) * (pos - low)))
                pcm = struct.pack('<' + 'h' * count, *resampled).ljust(FRAME_BYTES, b'\0')
                self.guard()
                await asyncio.sleep(max(0, self.next_frame - self.clock()))
                if generation != self.generation:
                    raise asyncio.CancelledError
                self.guard()
                # Reserve the wire slot before drain can be cancelled after
                # dispatch; the next idle frame must still respect pacing.
                self.next_frame = self.clock() + .02
                await self.wire.write_pcm(pcm)
        finally:
            self.pending_tts -= 1

    async def pump_silence(self):
        """Send one paced idle frame only when no TTS write is waiting."""
        while True:
            await asyncio.sleep(max(.02 if self.pending_tts else 0,
                                    self.next_frame - self.clock()))
            async with self.lock:
                # Recheck after acquiring the shared writer. A sequential TTS
                # caller registers its next frame before yielding this task.
                if self.pending_tts or self.clock() < self.next_frame:
                    continue
                self.guard()
                self.next_frame = self.clock() + .02
                await self.wire.write_pcm(SILENCE_PCM)
                self.silence_frames += 1

    async def stop(self, phone):
        if phone != self.grant.phone:
            raise ValueError('wrong_phone')
        self.generation += 1


@dataclass(frozen=True)
class Result:
    reason: str
    input_frames: int
    stop_confirmed: bool
    cleanup_errors: tuple[str, ...]

    @property
    def reservation_may_release(self):
        return self.stop_confirmed and not self.cleanup_errors


class Session:
    """Finite coordinator adapter. The caller supplies a verified owned call.

    This is not a replacement for manager locking or independent PBX expiry.
    A process killed here cannot perform cleanup; the native timeout must still
    hang up the exact phone call and startup reconciliation must retain the lock
    until that absence is freshly observed.
    """
    def __init__(self, wire: Wire, grant: Grant, pbx: PbxPort, clock=time.monotonic):
        if wire.expected != grant.session:
            raise ValueError('grant_wire_mismatch')
        self.wire, self.grant, self.pbx, self.clock = wire, grant, pbx, clock
        self.sink = Sink(wire, grant, self.guard, clock)
        self.stop_requested = asyncio.Event()
        self.started = self.closed = False

    def guard(self):
        if self.stop_requested.is_set() or self.closed:
            raise BoundaryError('explicit_stop')
        snapshot = self.pbx.snapshot()
        state = focus(self.grant, snapshot, self.clock())
        # A disappeared/replaced assistant call can be a manual hangup. Only
        # simultaneous, distinct call identities prove competition suitable
        # for later resumption; endpoint occupancy alone does not prove it.
        owned = self.grant.channel
        if (state == 'call_changed' and owned in snapshot.channels and
                any(channel.name and channel.uniqueid and channel.line
                    and channel.name != owned.name and channel.uniqueid != owned.uniqueid
                    for channel in snapshot.channels)):
            state = 'call_preempted'
        if state != 'owned_call':
            raise BoundaryError(state)

    def request_stop(self):
        self.stop_requested.set()

    async def run(self, assistant):
        if self.started:
            raise ValueError('single_use_session')
        self.started = True
        task = pump = None
        frames, reason, confirmed = 0, 'start_failed', False
        cleanup_errors = []
        cancelled = False

        def guard_input():
            # A completed receiver may already have observed a manual stop or
            # disconnected peer. Do not overwrite it with a simultaneous call
            # preemption that a persistent supervisor could later resume.
            if task is not None and task.done() and not task.cancelled():
                error = task.exception()
                if (isinstance(error, BoundaryError) and
                        str(error) in ('dtmf_stop', 'peer_ended', 'peer_closed')):
                    raise error
            self.guard()

        try:
            assistant.ensure_allowed(self.grant.phone)
            self.guard()
            budget = min(1, self.grant.deadline - self.clock())
            if not await asyncio.wait_for(self.pbx.validate_admission(self.grant), budget):
                raise BoundaryError('admission_unverified')
            self.guard()
            await asyncio.wait_for(self.wire.admit(), min(3, self.grant.deadline - self.clock()))
            self.guard()
            stream = await assistant.listen(self.grant.phone)
            pump = asyncio.create_task(self.sink.pump_silence())
            last_audio = self.clock()
            while True:
                guard_input()
                state = assistant.status(self.grant.phone).state
                if state in ('stopped', 'error'):
                    raise BoundaryError('assistant_stopped')
                if task is None:
                    task = asyncio.create_task(self.wire.read_pcm())
                done, _ = await asyncio.wait((task, pump), timeout=.05,
                                             return_when=asyncio.FIRST_COMPLETED)
                guard_input()
                if pump.done():
                    if not pump.cancelled():
                        pump.result()
                    raise BoundaryError('silence_sender_stopped')
                if task in done:
                    pcm, task = task.result(), None
                    last_audio = self.clock()
                    assistant.feed_pcm(self.grant.phone, stream, pcm)
                    frames += 1
                elif self.clock() - last_audio >= 3:
                    raise BoundaryError('receive_idle_timeout')
        except BoundaryError as exc:
            reason = str(exc)
        except asyncio.CancelledError:
            cancelled = True
            reason = 'shutdown'
        except Exception:
            reason = 'session_failed'
        finally:
            self.closed = True
            self.stop_requested.set()

            async def cleanup():
                nonlocal confirmed
                workers = [worker for worker in (task, pump) if worker is not None]
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                for label, action in (
                    ('wire_close_failed', self.wire.close),
                    ('assistant_stop_failed', lambda: assistant.stop(self.grant.phone, reason=reason))):
                    try:
                        await asyncio.wait_for(action(), 2)
                    except (Exception, asyncio.CancelledError):
                        cleanup_errors.append(label)
                try:
                    confirmed = (await asyncio.wait_for(self.pbx.hangup_and_confirm(self.grant), 5)) is True
                except (Exception, asyncio.CancelledError):
                    cleanup_errors.append('pbx_cleanup_failed')

            cleanup_task = asyncio.create_task(cleanup())
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup_task.result()
        if cancelled:
            raise asyncio.CancelledError
        return Result(reason, frames, confirmed, tuple(cleanup_errors))
