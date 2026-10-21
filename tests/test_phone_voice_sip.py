"""No network, microphone, PBX process, model, or Home Assistant access."""
import asyncio
from dataclasses import replace
from pathlib import Path
import struct
import sys
import time
from types import SimpleNamespace
import unittest
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'containers/asterisk'))
from phone_voice_sip import BoundaryError, Channel, Grant, Session, Snapshot, Wire, focus

# Immutable local checkout carrying the merged wake-readiness implementation.
SOURCE = ROOT
sys.path.insert(0, str(SOURCE))
from voice_assistant.service import AssistantService, ServiceSettings

TOKEN = UUID('22222222-2222-4222-8222-222222222222')
CALL = Channel('PJSIP/demo-00000001', '1.1', 'demo-line')
FRAME = b'\x01\x00' * 320


def packet(kind, data=b''):
    return bytes((kind,)) + len(data).to_bytes(2, 'big') + data


class Writer:
    def __init__(self):
        self.data, self.times = [], []
        self.closed = False

    def write(self, data):
        self.data.append(data)
        self.times.append(time.monotonic())

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return


class PBX:
    def __init__(self, grant, clock=time.monotonic):
        self.grant, self.clock = grant, clock
        self.channels = (grant.channel,)
        self.occupied = frozenset((grant.channel.line,))
        self.admitted, self.confirmed = True, True
        self.hangups = []
        self.age = 0

    async def validate_admission(self, grant):
        return grant == self.grant and self.admitted

    def snapshot(self):
        return Snapshot(self.grant.phone, self.clock() - self.age, True,
                        self.channels, self.occupied)

    async def hangup_and_confirm(self, grant):
        self.hangups.append(grant.channel)
        return self.confirmed


class Assistant:
    def __init__(self):
        self.frames = []
        self.listened = False
        self.stopped = False

    def ensure_allowed(self, phone):
        return

    async def listen(self, phone):
        self.listened = True
        return 'stream'

    def status(self, phone):
        return SimpleNamespace(state='armed')

    def feed_pcm(self, phone, stream, pcm):
        self.frames.append(pcm)

    async def stop(self, phone, reason):
        self.stopped = True


class Tests(unittest.IsolatedAsyncioTestCase):
    def setup_session(self, duration=1, clock=time.monotonic):
        reader, writer = asyncio.StreamReader(), Writer()
        now = clock()
        grant = Grant(TOKEN, 'demo', CALL, now, now + duration)
        pbx = PBX(grant, clock)
        wire = Wire(reader, writer, TOKEN)
        session = Session(wire, grant, pbx, clock)
        return reader, writer, grant, pbx, wire, session

    async def test_fragmented_tcp_and_variable_pcm_packet_boundaries(self):
        reader, writer, grant, pbx, wire, session = self.setup_session()
        source = packet(1, TOKEN.bytes) + packet(0x12, FRAME[:180]) + packet(0x12, FRAME[180:] + FRAME)
        async def producer():
            for byte in source:
                reader.feed_data(bytes((byte,)))
                await asyncio.sleep(0)
        task = asyncio.create_task(producer())
        await wire.admit()
        self.assertEqual(await wire.read_pcm(), FRAME)
        self.assertEqual(await wire.read_pcm(), FRAME)
        await task
        await wire.close()
        self.assertTrue(writer.closed)

    async def test_wrong_uuid_never_reaches_assistant(self):
        reader, writer, grant, pbx, wire, session = self.setup_session()
        reader.feed_data(packet(1, bytes(16)))
        assistant = Assistant()
        result = await session.run(assistant)
        self.assertEqual(result.reason, 'stream_identity_mismatch')
        self.assertFalse(assistant.listened)
        self.assertTrue(result.reservation_may_release)
        self.assertEqual(pbx.hangups, [CALL])

    async def test_wrong_codec_and_odd_pcm_rejected(self):
        for kind, data in ((0x10, FRAME), (0x12, b'x'), (1, TOKEN.bytes)):
            reader, writer, grant, pbx, wire, session = self.setup_session()
            reader.feed_data(packet(1, TOKEN.bytes) + packet(kind, data))
            result = await session.run(Assistant())
            self.assertEqual(result.reason, 'unsupported_audio_packet')

    async def test_oversized_declared_payload_rejected_before_body(self):
        reader, writer, grant, pbx, wire, session = self.setup_session()
        reader.feed_data(packet(1, TOKEN.bytes) + b'\x12\xff\xff')
        result = await session.run(Assistant())
        self.assertEqual(result.reason, 'oversized_packet')

    async def test_hash_ends_call_without_retaining_audio(self):
        reader, writer, grant, pbx, wire, session = self.setup_session()
        reader.feed_data(packet(1, TOKEN.bytes) + packet(0x12, FRAME) + packet(3, b'#'))
        assistant = Assistant()
        result = await session.run(assistant)
        self.assertEqual(result.reason, 'dtmf_stop')
        self.assertEqual(assistant.frames, [FRAME])
        self.assertTrue(assistant.stopped)
        self.assertEqual(wire.pending, bytearray())

    async def test_preemption_on_other_line_does_not_hang_up_other_call(self):
        reader, writer, grant, pbx, wire, session = self.setup_session()
        reader.feed_data(packet(1, TOKEN.bytes))
        assistant = Assistant()
        task = asyncio.create_task(session.run(assistant))
        await asyncio.sleep(.01)
        other = Channel('PJSIP/other-00000002', '2.2', 'second-line')
        pbx.channels += (other,)
        pbx.occupied |= frozenset(('second-line',))
        result = await task
        self.assertEqual(result.reason, 'call_changed')
        self.assertEqual(pbx.hangups, [CALL])
        self.assertNotIn(other, pbx.hangups)

    async def test_reused_channel_name_different_uniqueid_not_exempt(self):
        reader, writer, grant, pbx, wire, session = self.setup_session()
        pbx.channels = (replace(CALL, uniqueid='replacement'),)
        self.assertEqual(focus(grant, pbx.snapshot(), time.monotonic()), 'call_changed')

    async def test_stale_snapshot_and_unknown_occupancy_fail_closed(self):
        reader, writer, grant, pbx, wire, session = self.setup_session()
        pbx.age = 1.01
        self.assertEqual(focus(grant, pbx.snapshot(), time.monotonic()), 'unknown')
        pbx.age = 0
        pbx.occupied |= frozenset(('unknown-line',))
        self.assertEqual(focus(grant, pbx.snapshot(), time.monotonic()), 'call_changed')

    async def test_absolute_lease_includes_setup_and_cannot_exceed_300(self):
        now = [100.0]
        reader, writer, grant, pbx, wire, session = self.setup_session(clock=lambda: now[0])
        reader.feed_data(packet(1, TOKEN.bytes))
        assistant = Assistant()
        task = asyncio.create_task(session.run(assistant))
        await asyncio.sleep(.01)
        now[0] = grant.deadline
        result = await task
        self.assertEqual(result.reason, 'lease_expired')
        self.assertTrue(result.stop_confirmed)
        with self.assertRaises(ValueError):
            replace(grant, deadline=grant.created_at + 300.001)

    async def test_unverified_native_pbx_lease_never_opens_assistant(self):
        reader, writer, grant, pbx, wire, session = self.setup_session()
        pbx.admitted = False
        assistant = Assistant()
        result = await session.run(assistant)
        self.assertEqual(result.reason, 'admission_unverified')
        self.assertFalse(assistant.listened)
        self.assertFalse(wire.admitted)

    async def test_unconfirmed_hangup_retains_reservation(self):
        reader, writer, grant, pbx, wire, session = self.setup_session()
        pbx.confirmed = False
        session.request_stop()
        result = await session.run(Assistant())
        self.assertFalse(result.stop_confirmed)
        self.assertFalse(result.reservation_may_release)

    async def test_cancel_closes_wire_and_confirms_only_exact_call(self):
        reader, writer, grant, pbx, wire, session = self.setup_session()
        reader.feed_data(packet(1, TOKEN.bytes))
        assistant = Assistant()
        task = asyncio.create_task(session.run(assistant))
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(writer.closed)
        self.assertTrue(assistant.stopped)
        self.assertEqual(pbx.hangups, [CALL])

    async def test_self_cancelled_assistant_cleanup_still_attempts_exact_hangup(self):
        reader, writer, grant, pbx, wire, session = self.setup_session()
        class CancellingAssistant(Assistant):
            async def stop(self, phone, reason):
                raise asyncio.CancelledError
        session.request_stop()
        result = await session.run(CancellingAssistant())
        self.assertEqual(pbx.hangups, [CALL])
        self.assertTrue(result.stop_confirmed)
        self.assertEqual(result.cleanup_errors, ('assistant_stop_failed',))
        self.assertFalse(result.reservation_may_release)

    async def test_repeated_caller_cancel_does_not_interrupt_hangup_proof(self):
        reader, writer, grant, pbx, wire, session = self.setup_session()
        entered, released = asyncio.Event(), asyncio.Event()
        original = pbx.hangup_and_confirm
        async def delayed(grant):
            entered.set()
            await released.wait()
            return await original(grant)
        pbx.hangup_and_confirm = delayed
        reader.feed_data(packet(1, TOKEN.bytes))
        task = asyncio.create_task(session.run(Assistant()))
        await asyncio.sleep(.01)
        task.cancel()
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        released.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(pbx.hangups, [CALL])

    async def test_sink_wideband_pacing_and_stop_generation(self):
        reader, writer, grant, pbx, wire, session = self.setup_session()
        reader.feed_data(packet(1, TOKEN.bytes))
        await wire.admit()
        await session.sink.write_pcm('demo', FRAME, 16000)
        await session.sink.write_pcm('demo', FRAME, 16000)
        self.assertGreaterEqual(writer.times[1] - writer.times[0], .018)
        self.assertEqual(writer.data, [packet(0x12, FRAME)] * 2)
        queued = asyncio.create_task(session.sink.write_pcm('demo', FRAME, 16000))
        await asyncio.sleep(0)
        await session.sink.stop('demo')
        with self.assertRaises(asyncio.CancelledError):
            await queued
        self.assertEqual(len(writer.data), 2)
        await wire.close()

    async def test_same_coordinator_two_wake_turns_and_rearm(self):
        reader, writer, grant, pbx, wire, session = self.setup_session(duration=3)
        reader.feed_data(packet(1, TOKEN.bytes))
        class Detector:
            ready = True
            def feed(self, pcm):
                return pcm == FRAME
            def reset(self):
                pass
        class Assist:
            turns = 0
            async def run(self, audio, *, conversation_id=None, on_event=None):
                async for pcm in audio:
                    if pcm == FRAME:
                        break
                await on_event({'type': 'stt-vad-end'})
                self.turns += 1
                return SimpleNamespace(conversation_id='conversation', tts_url=None)
        class Playback:
            plays = 0
            async def play(self, phone, result):
                await session.sink.write_pcm(phone, FRAME, 16000)
                self.plays += 1
            async def stop(self, phone):
                await session.sink.stop(phone)
        assist, playback = Assist(), Playback()
        service = AssistantService(ServiceSettings(enabled=True, rearm_after_turn=True,
                                   duplicate_window_seconds=0), {'demo': True},
                                   lambda _: Detector(), assist, playback)
        task = asyncio.create_task(session.run(service))
        for turn in (1, 2):
            reader.feed_data(packet(0x12, FRAME))
            async with asyncio.timeout(1):
                while playback.plays < turn or service.status('demo').state != 'armed':
                    await asyncio.sleep(.005)
        session.request_stop()
        result = await task
        self.assertEqual(assist.turns, 2)
        self.assertEqual(playback.plays, 2)
        self.assertEqual(result.reason, 'explicit_stop')
        self.assertTrue(result.reservation_may_release)
        self.assertEqual(service.status('demo').state, 'stopped')
        self.assertIsNone(service._owner)
        self.assertEqual(writer.data, [packet(0x12, FRAME)] * 2)
        await service.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
