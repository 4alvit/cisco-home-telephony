"""Synthetic PCM scheduling tests; no sockets, PBX, models or microphone."""
import asyncio
import time
import unittest

from tests.test_phone_voice_sip import (
    Assistant, CALL, FRAME, Grant, PBX, Session, TOKEN, Wire, Writer, packet,
)

SILENCE = packet(0x12, bytes(640))
OTHER_FRAME = b'\x02\x00' * 320


class ObservedWriter(Writer):
    def __init__(self):
        super().__init__()
        self.changed = asyncio.Event()
        self.drain_entered = asyncio.Event()
        self.drain_release = asyncio.Event()
        self.drain_release.set()
        self.draining = False
        self.closed_while_draining = False
        self.failure = False

    def write(self, data):
        if self.failure:
            raise OSError('synthetic write failure')
        super().write(data)
        self.changed.set()

    async def drain(self):
        self.draining = True
        self.drain_entered.set()
        try:
            await self.drain_release.wait()
            await asyncio.sleep(0)
        finally:
            self.draining = False

    def close(self):
        self.closed_while_draining = self.draining
        super().close()

    async def wait_frames(self, count):
        async with asyncio.timeout(1):
            while len(self.data) < count:
                await self.changed.wait()
                self.changed.clear()


class IdleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tasks = []
        self.sessions = []
        self.releases = []

    async def asyncTearDown(self):
        for release in self.releases:
            release.set()
        for session in self.sessions:
            session.request_stop()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.wait_for(asyncio.gather(*self.tasks, return_exceptions=True), 2)

    def setup_session(self, duration=10, clock=time.monotonic):
        reader, writer = asyncio.StreamReader(), ObservedWriter()
        now = clock()
        grant = Grant(TOKEN, 'demo', CALL, now, now + duration)
        pbx = PBX(grant, clock)
        wire = Wire(reader, writer, TOKEN)
        session = Session(wire, grant, pbx, clock)
        self.sessions.append(session)
        self.releases.append(writer.drain_release)
        return reader, writer, pbx, wire, session

    def launch(self, session, assistant=None):
        task = asyncio.create_task(session.run(assistant or Assistant()))
        self.tasks.append(task)
        return task

    async def finish(self, task):
        done, _ = await asyncio.wait({task}, timeout=1)
        self.assertIn(task, done, 'session or idle writer did not stop')
        return task.result()

    async def test_wrong_uuid_and_unverified_grant_never_write_silence(self):
        for fault in ('uuid', 'grant'):
            with self.subTest(fault=fault):
                reader, writer, pbx, wire, session = self.setup_session()
                pbx.admitted = fault != 'grant'
                reader.feed_data(packet(1, bytes(16) if fault == 'uuid' else TOKEN.bytes))
                result = await self.finish(self.launch(session))
                self.assertEqual(result.reason,
                    'stream_identity_mismatch' if fault == 'uuid' else 'admission_unverified')
                self.assertEqual(writer.data, [])
                self.assertEqual(wire.frames_sent, 0)
                self.assertEqual(session.sink.silence_frames, 0)

    async def test_no_idle_output_until_uuid_admission(self):
        reader, writer, pbx, wire, session = self.setup_session()
        task = self.launch(session)
        await asyncio.sleep(.03)
        self.assertFalse(wire.admitted)
        self.assertEqual(writer.data, [])
        reader.feed_data(packet(1, TOKEN.bytes))
        await writer.wait_frames(2)
        session.request_stop()
        await self.finish(task)
        self.assertEqual(writer.data, [SILENCE] * 2)
        self.assertEqual(wire.frames_sent, 2)
        self.assertEqual(session.sink.silence_frames, 2)

    async def test_no_idle_output_while_native_grant_verification_is_pending(self):
        reader, writer, pbx, wire, session = self.setup_session()
        entered, release = asyncio.Event(), asyncio.Event()
        self.releases.append(release)
        original_admission = pbx.validate_admission

        async def admit(grant):
            entered.set()
            await release.wait()
            return await original_admission(grant)

        pbx.validate_admission = admit
        reader.feed_data(packet(1, TOKEN.bytes))
        task = self.launch(session)
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(.03)
        self.assertEqual(writer.data, [])
        self.assertFalse(wire.admitted)
        release.set()
        await writer.wait_frames(1)
        session.request_stop()
        await self.finish(task)
        self.assertEqual(writer.data, [SILENCE])

    async def test_idle_frames_are_paced_and_never_count_as_reply(self):
        reader, writer, pbx, wire, session = self.setup_session()
        reader.feed_data(packet(1, TOKEN.bytes))
        task = self.launch(session)
        await writer.wait_frames(4)
        session.request_stop()
        result = await self.finish(task)
        self.assertEqual(writer.data, [SILENCE] * 4)
        self.assertTrue(all(b - a >= .018 for a, b in zip(writer.times, writer.times[1:])))
        self.assertEqual(session.sink.silence_frames, 4)
        self.assertEqual(wire.frames_sent, 4)
        self.assertEqual(result.input_frames, 0)
        self.assertEqual(result.reason, 'explicit_stop')

    async def test_missed_time_does_not_create_catchup_burst(self):
        offset = [0.0]
        reader, writer, pbx, wire, session = self.setup_session(
            duration=30, clock=lambda: time.monotonic() + offset[0])
        reader.feed_data(packet(1, TOKEN.bytes))
        task = self.launch(session)
        await writer.wait_frames(2)
        offset[0] += .4
        await writer.wait_frames(5)
        session.request_stop()
        await self.finish(task)
        self.assertEqual(writer.data, [SILENCE] * 5)
        self.assertTrue(all(b - a >= .018 for a, b in zip(writer.times[2:], writer.times[3:])))
        self.assertEqual(session.sink.silence_frames, 5)

    async def test_pending_tts_has_priority_and_idle_resumes_after_exact_frames(self):
        reader, writer, pbx, wire, session = self.setup_session()
        await session.sink.lock.acquire()
        reader.feed_data(packet(1, TOKEN.bytes))
        task = self.launch(session)
        try:
            # Put the idle pump ahead of TTS in lock acquisition order. It must
            # still yield the next wire slots once actual TTS is pending.
            await asyncio.sleep(.03)
            tts = [asyncio.create_task(session.sink.write_pcm('demo', pcm, 16000))
                   for pcm in (FRAME, OTHER_FRAME)]
            self.tasks.extend(tts)
            await asyncio.sleep(0)
        finally:
            session.sink.lock.release()
        await asyncio.wait_for(asyncio.gather(*tts), 1)
        await writer.wait_frames(3)
        session.request_stop()
        await self.finish(task)
        self.assertEqual(writer.data, [packet(0x12, FRAME), packet(0x12, OTHER_FRAME), SILENCE])
        self.assertEqual(wire.frames_sent, 3)
        self.assertEqual(session.sink.silence_frames, 1)
        self.assertTrue(all(b - a >= .018 for a, b in zip(writer.times, writer.times[1:])))

    async def test_zero_valued_tts_counts_as_tts_origin_not_idle(self):
        reader, writer, pbx, wire, session = self.setup_session()
        reader.feed_data(packet(1, TOKEN.bytes))
        await wire.admit()
        await session.sink.write_pcm('demo', bytes(640), 16000)
        self.assertEqual(writer.data, [SILENCE])
        self.assertEqual(wire.frames_sent, 1)
        self.assertEqual(session.sink.silence_frames, 0)
        await wire.close()

    async def test_sequential_tts_frames_are_contiguous_and_then_idle_resumes(self):
        reader, writer, pbx, wire, session = self.setup_session()
        reader.feed_data(packet(1, TOKEN.bytes))
        task = self.launch(session)
        await writer.wait_frames(1)
        frames = [bytes((value, 0)) * 320 for value in range(1, 7)]
        # WavePlayback awaits each write before supplying its next block.
        for frame in frames:
            await session.sink.write_pcm('demo', frame, 16000)
        await writer.wait_frames(8)
        session.request_stop()
        await self.finish(task)
        self.assertEqual(writer.data,
                         [SILENCE] + [packet(0x12, frame) for frame in frames] + [SILENCE])
        self.assertEqual(wire.frames_sent, 8)
        self.assertEqual(session.sink.silence_frames, 2)

    async def test_cancelled_tts_drain_still_reserves_twenty_ms_before_idle(self):
        now = [100.0]
        reader, writer, pbx, wire, session = self.setup_session(clock=lambda: now[0])
        dispatch_times = []
        original_write = writer.write

        def write(data):
            dispatch_times.append(now[0])
            original_write(data)

        writer.write = write
        pump_acquired = asyncio.Event()
        tts_task = [None]

        class ObservedLock(asyncio.Lock):
            async def acquire(self):
                acquired = await super().acquire()
                if asyncio.current_task() is not tts_task[0]:
                    pump_acquired.set()
                return acquired

        session.sink.lock = ObservedLock()
        reader.feed_data(packet(1, TOKEN.bytes))
        task = self.launch(session)
        await writer.wait_frames(1)
        async with asyncio.timeout(1):
            while session.sink.lock.locked():
                await asyncio.sleep(0)

        # Dispatch TTS at the next slot, then cancel while its drain is still
        # pending. The session and its idle pump remain running throughout.
        now[0] += .02
        writer.drain_entered.clear()
        writer.drain_release.clear()
        tts_task[0] = asyncio.create_task(session.sink.write_pcm('demo', FRAME, 16000))
        self.tasks.append(tts_task[0])
        await asyncio.wait_for(writer.drain_entered.wait(), 1)
        self.assertEqual(writer.data, [SILENCE, packet(0x12, FRAME)])
        tts_at = dispatch_times[-1]
        now[0] = tts_at + .001
        pump_acquired.clear()
        tts_task[0].cancel()
        with self.assertRaises(asyncio.CancelledError):
            await tts_task[0]
        writer.drain_release.set()

        # Observe an actual pump pass at +1ms, rather than infer that it ran
        # from elapsed wall time. It must decline this too-early wire slot.
        await asyncio.wait_for(pump_acquired.wait(), 1)
        self.assertEqual(writer.data, [SILENCE, packet(0x12, FRAME)])
        self.assertFalse(task.done())
        self.assertFalse(session.stop_requested.is_set())
        self.assertEqual(session.sink.pending_tts, 0)
        now[0] = tts_at + .02000001
        await writer.wait_frames(3)
        self.assertEqual(writer.data, [SILENCE, packet(0x12, FRAME), SILENCE])
        self.assertGreaterEqual(dispatch_times[2] - dispatch_times[1], .02)
        self.assertFalse(task.done())
        session.request_stop()
        result = await self.finish(task)
        self.assertEqual(result.reason, 'explicit_stop')
        self.assertEqual(wire.frames_sent, 2)
        self.assertEqual(session.sink.silence_frames, 2)
        self.assertEqual(pbx.hangups, [CALL])

    async def test_stop_preemption_staleness_and_expiry_prevent_next_idle_write(self):
        for change, expected in (('stop', 'explicit_stop'), ('preempt', 'call_changed'),
                                 ('stale', 'unknown'), ('expiry', 'lease_expired')):
            with self.subTest(change=change):
                offset = [0.0]
                reader, writer, pbx, wire, session = self.setup_session(
                    clock=lambda: time.monotonic() + offset[0])
                reader.feed_data(packet(1, TOKEN.bytes))
                task = self.launch(session)
                await writer.wait_frames(1)
                if change == 'stop':
                    session.request_stop()
                elif change == 'preempt':
                    pbx.occupied |= frozenset(('other-line',))
                elif change == 'stale':
                    pbx.age = 1.01
                else:
                    offset[0] += 11
                result = await self.finish(task)
                self.assertEqual(result.reason, expected)
                self.assertEqual(writer.data, [SILENCE])
                await asyncio.sleep(.03)
                self.assertEqual(writer.data, [SILENCE])
                self.assertEqual(pbx.hangups, [CALL])
                self.assertTrue(writer.closed)

    async def test_outgoing_silence_does_not_reset_input_idle_timeout(self):
        offset = [0.0]
        reader, writer, pbx, wire, session = self.setup_session(
            clock=lambda: time.monotonic() + offset[0])
        reader.feed_data(packet(1, TOKEN.bytes))
        task = self.launch(session)
        await writer.wait_frames(1)
        offset[0] += 3.01
        result = await self.finish(task)
        self.assertEqual(result.reason, 'receive_idle_timeout')
        self.assertEqual(result.input_frames, 0)
        self.assertEqual(wire.frames_sent, session.sink.silence_frames)
        self.assertGreater(session.sink.silence_frames, 0)
        self.assertTrue(result.stop_confirmed)

    async def test_idle_write_failure_is_observed_and_cleanup_still_runs(self):
        reader, writer, pbx, wire, session = self.setup_session()
        writer.failure = True
        reader.feed_data(packet(1, TOKEN.bytes))
        assistant = Assistant()
        result = await self.finish(self.launch(session, assistant))
        self.assertEqual(result.reason, 'session_failed')
        self.assertEqual(writer.data, [])
        self.assertEqual(session.sink.silence_frames, 0)
        self.assertEqual(wire.frames_sent, 0)
        self.assertTrue(writer.closed)
        self.assertTrue(assistant.stopped)
        self.assertEqual(pbx.hangups, [CALL])
        self.assertTrue(result.stop_confirmed)

    async def test_idle_drain_timeout_fails_and_still_closes_exact_call(self):
        reader, writer, pbx, wire, session = self.setup_session()
        writer.drain_release.clear()
        reader.feed_data(packet(1, TOKEN.bytes))
        assistant = Assistant()
        result = await self.finish(self.launch(session, assistant))
        self.assertEqual(result.reason, 'session_failed')
        self.assertEqual(writer.data, [SILENCE])
        self.assertEqual(wire.frames_sent, 0)
        self.assertEqual(session.sink.silence_frames, 0)
        self.assertFalse(writer.closed_while_draining)
        self.assertTrue(writer.closed)
        self.assertTrue(assistant.stopped)
        self.assertEqual(pbx.hangups, [CALL])
        self.assertTrue(result.stop_confirmed)

    async def test_repeated_cancellation_drains_idle_write_before_wire_and_pbx_cleanup(self):
        reader, writer, pbx, wire, session = self.setup_session()
        writer.drain_release.clear()
        entered, release = asyncio.Event(), asyncio.Event()
        self.releases.append(release)
        original_hangup = pbx.hangup_and_confirm

        async def hangup(grant):
            entered.set()
            await release.wait()
            return await original_hangup(grant)

        pbx.hangup_and_confirm = hangup
        reader.feed_data(packet(1, TOKEN.bytes))
        assistant = Assistant()
        task = self.launch(session, assistant)
        await asyncio.wait_for(writer.drain_entered.wait(), 1)
        task.cancel()
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await self.finish(task)
        self.assertFalse(writer.closed_while_draining)
        self.assertFalse(writer.draining)
        self.assertTrue(writer.closed)
        self.assertTrue(assistant.stopped)
        self.assertEqual(pbx.hangups, [CALL])
        self.assertEqual(len(writer.data), 1)
        self.assertEqual(wire.frames_sent, 0)


if __name__ == '__main__':
    unittest.main()
