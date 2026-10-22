"""Real loopback TCP lifecycle tests; PBX, provider and lease I/O are replaced."""
import asyncio
from contextlib import ExitStack
from dataclasses import replace
import errno
from pathlib import Path
import signal
import sys
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from tests.test_voice_session_sip import Assistant, Port, TOKEN, runner
from tests.test_phone_voice_sip import FRAME, packet
from phone_voice_sip import BoundaryError
from phone_voice_sip_ami import AmiPort


class LifecyclePort(Port):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.returned = asyncio.Event()
        self.failure = None
        self.duration = 3
        self.hangups = 0

    async def start(self):
        self.originate_attempted = True
        self.entered.set()
        await self.release.wait()
        if self.failure:
            raise self.failure
        grant = await super().start()
        self.grant = replace(grant, deadline=grant.created_at + self.duration)
        self.returned.set()
        return self.grant

    async def hangup_and_confirm(self, grant=None):
        self.hangups += 1
        return await super().hangup_and_confirm(grant)


class LifecycleAssistant(Assistant):
    def __init__(self):
        super().__init__()
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_release.set()
        self.close_completed = False
        self.frame_received = asyncio.Event()

    def feed_pcm(self, phone, stream, pcm):
        super().feed_pcm(phone, stream, pcm)
        self.frame_received.set()

    async def close(self):
        self.close_started.set()
        await self.close_release.wait()
        self.close_completed = True


class StartupStopPort(LifecyclePort):
    """Pause after the one Speaker, using the real startup sealing predicate."""
    seal_startup_stop = AmiPort.seal_startup_stop

    def __init__(self):
        super().__init__()
        self.release.clear()
        self._lifecycle_lock = threading.RLock()
        self._start_active = self._sealed = self.used = False
        self.thread = None
        self.start_attempted = self.seen_phone = False
        self.startup_stage = 'guard'

    async def start(self):
        self.start_attempted = self._start_active = True
        self.originate_attempted = self.deadline_armed = True
        self.speaker_attempted = self.seen_phone = True
        self.entered.set()
        try:
            await self.release.wait()
            raise self.failure or BoundaryError('explicit_stop')
        finally:
            self._start_active = False


class LoopbackLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Keep real Server/StreamReader/StreamWriter behavior, including on 3.13."""

    async def asyncSetUp(self):
        self.args = SimpleNamespace(session_id=TOKEN.hex, lease_fd=9,
            state_directory='/unused', workspace='/unused', status_file=None,
            lease_expires_unix=runner.time.time() + 3, ffmpeg='unused')
        self.port = LifecyclePort()
        self.assistant = LifecycleAssistant()
        self.servers, self.writers, self.tasks, self.sessions = [], [], [], []
        self.connections = asyncio.Queue()
        self.listening = asyncio.Event()
        self.bind_port = 0
        self.signals = {}
        self.real_start_server = asyncio.start_server
        self.real_session = runner.Session
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(runner, 'LeaseWitness',
            return_value=SimpleNamespace(verify=lambda: None)))
        self.stack.enter_context(patch.object(runner, 'SessionProgress',
            return_value=SimpleNamespace(turns_completed=0, publish=lambda *args: None)))
        self.stack.enter_context(patch.object(runner, 'AmiPort', side_effect=lambda *args: self.port))
        self.stack.enter_context(patch.object(runner, 'build_service',
            side_effect=lambda *args, **kwargs: self.assistant))
        self.stack.enter_context(patch.object(runner.transport, '_gateway_module', return_value=None))
        self.stack.enter_context(patch.object(runner.asyncio, 'start_server', side_effect=self.serve))
        self.stack.enter_context(patch.object(runner, 'Session', side_effect=self.make_session))
        loop = asyncio.get_running_loop()
        self.stack.enter_context(patch.object(loop, 'add_signal_handler',
            side_effect=lambda number, callback: self.signals.__setitem__(number, callback)))
        self.stack.enter_context(patch.object(loop, 'remove_signal_handler', return_value=True))

    async def asyncTearDown(self):
        # Force teardown even when testing the original deadlock. In particular,
        # cancellation alone cannot free a paused reader held by wait_closed().
        self.port.release.set()
        self.assistant.close_release.set()
        for server in self.servers:
            server.close()
        for writer in self.writers:
            writer.transport.abort()
            writer.close()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            _, pending = await asyncio.wait(self.tasks, timeout=3)
            for task in pending:
                task.cancel()
            if pending:
                _, pending = await asyncio.wait(pending, timeout=1)
            self.assertFalse(pending, 'runner did not terminate after forced transport teardown')
            for task in self.tasks:
                if not task.cancelled():
                    task.exception()
        for server in self.servers:
            await asyncio.wait_for(server.wait_closed(), 1)
        for writer in self.writers:
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (ConnectionError, OSError):
                pass
        self.stack.close()

    async def serve(self, callback, host, port, **kwargs):
        self.assertEqual((host, port), ('127.0.0.1', self.bind_port))
        self.assertEqual(kwargs['limit'], 8192)
        self.assertEqual(kwargs['backlog'], 1)

        def connected(reader, writer):
            self.writers.append(writer)
            self.connections.put_nowait((reader, writer))
            callback(reader, writer)

        server = await self.real_start_server(connected, host, port, **kwargs)
        self.servers.append(server)
        self.address = server.sockets[0].getsockname()[:2]
        self.listening.set()
        return server

    def make_session(self, *args):
        session = self.real_session(*args)
        self.sessions.append(session)
        return session

    def launch(self):
        task = asyncio.create_task(runner.run(self.args,
            {'line': '201', 'bind_port': self.bind_port}, {}, {'id': '201'}, {}))
        self.tasks.append(task)
        return task

    async def connect(self):
        await asyncio.wait_for(self.listening.wait(), 1)
        reader, writer = await asyncio.wait_for(asyncio.open_connection(*self.address), 1)
        self.writers.append(writer)
        server_reader, server_writer = await asyncio.wait_for(self.connections.get(), 1)
        return reader, writer, server_reader, server_writer

    async def finished(self, task):
        # Do not cancel the runner to make a deadline assertion pass.
        done, _ = await asyncio.wait({task}, timeout=1.5)
        self.assertIn(task, done, 'runner cleanup/startup blocked with an owned TCP peer')
        return task.result()

    def assert_startup_cleaned(self, report):
        self.assertEqual(report['error'], 'voice_session_failed')
        self.assertIsNone(report['result'])
        self.assertEqual(report['cleanup_errors'], [])
        self.assertEqual(self.sessions, [])
        self.assertTrue(self.assistant.close_completed)
        self.assertEqual(self.port.hangups, 1)
        self.assertTrue(report['pbx_call_absent'])
        self.assertNotIn('cleanup_proof', report)

    async def test_held_open_peer_delivers_pcm_before_dtmf_and_peer_close(self):
        task = self.launch()
        reader, writer, _, _ = await self.connect()
        writer.write(packet(1, TOKEN.bytes) + packet(0x12, FRAME))
        await writer.drain()
        await asyncio.wait_for(self.assistant.frame_received.wait(), 1)
        self.assertFalse(writer.is_closing())
        self.assertEqual(self.assistant.frames, [FRAME])
        writer.write(packet(3, b'#'))
        await writer.drain()
        report = await self.finished(task)
        self.assertEqual(report['result']['reason'], 'dtmf_stop')
        self.assertEqual(report['input_frames'], 1)
        self.assertEqual(report['cleanup_errors'], [])
        self.assertTrue(report['pbx_call_absent'])
        self.assertTrue(self.assistant.close_completed)
        # Read through EOF, accounting only for the admitted idle PCM sender.
        self.assertEqual(await asyncio.wait_for(reader.read(), 1),
                         packet(0x12, bytes(640)) * report['silence_frames'])
        self.assertEqual(report['output_frames'], report['silence_frames'])
        self.assertEqual(report['turns_completed'], 0)

    @unittest.skipUnless(sys.platform == 'linux', 'Linux TCP TIME_WAIT regression')
    async def test_completed_session_rebinds_same_port_in_time_wait(self):
        for attempt in range(2):
            self.listening.clear()
            if attempt:
                self.port, self.assistant = LifecyclePort(), LifecycleAssistant()
            task = self.launch()
            reader, writer, _, _ = await self.connect()
            self.bind_port = self.address[1]
            peer_port = writer.get_extra_info('sockname')[1]
            writer.write(packet(1, TOKEN.bytes) + packet(0x12, FRAME) + packet(3, b'#'))
            await writer.drain()
            report = await self.finished(task)
            self.assertEqual(report['result']['reason'], 'dtmf_stop')
            self.assertIsNone(report['error'])
            self.assertEqual(report['cleanup_errors'], [])
            self.assertTrue(report['pbx_call_absent'])
            self.assertTrue(self.assistant.close_completed)
            self.assertEqual(self.assistant.frames, [FRAME])
            # The runner closes first; only then close the peer so the server
            # endpoint, rather than the client endpoint, enters TIME_WAIT.
            await asyncio.wait_for(reader.read(), 1)
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), 1)
            if attempt == 0:
                local = f'0100007F:{self.bind_port:04X}'
                remote = f'0100007F:{peer_port:04X}'
                for _ in range(100):
                    rows = [line.split() for line in Path('/proc/net/tcp').read_text().splitlines()[1:]]
                    if any(row[1:4] == [local, remote, '06'] for row in rows):
                        break
                    await asyncio.sleep(.01)
                else:
                    self.fail('first runner connection did not reach server-side TIME_WAIT')

    async def test_active_listener_cannot_be_replaced_on_same_port(self):
        task = self.launch()
        await asyncio.wait_for(self.listening.wait(), 1)
        try:
            replacement = await self.real_start_server(
                lambda reader, writer: writer.close(), *self.address,
                reuse_address=True, reuse_port=False)
        except OSError as error:
            self.assertEqual(error.errno, errno.EADDRINUSE)
        else:
            replacement.close()
            await asyncio.wait_for(replacement.wait_closed(), 1)
            self.fail('active runner listener was replaced')
        self.assertFalse(task.done())
        self.signals[signal.SIGTERM]()
        self.assert_startup_cleaned(await self.finished(task))

    async def test_paused_reader_with_106496_bytes_and_peer_eof_cleans_start_failure(self):
        self.port.release.clear()
        task = self.launch()
        _, writer, server_reader, server_writer = await self.connect()
        data = packet(1, TOKEN.bytes)
        writer.write(data + b'x' * (106496 - len(data)))
        await writer.drain()
        for _ in range(100):
            if server_reader._paused:
                break
            await asyncio.sleep(.005)
        self.assertTrue(server_reader._paused, 'fixture must exercise actual read backpressure')
        self.assertGreater(len(server_reader._buffer), 2 * 8192)
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), 1)
        self.port.failure = RuntimeError('synthetic startup failure')
        self.port.release.set()
        self.assert_startup_cleaned(await self.finished(task))
        self.assertTrue(server_writer.is_closing())

    async def test_repeated_cancellation_during_finally_still_closes_service_and_pbx(self):
        self.assistant.close_release.clear()
        task = self.launch()
        await asyncio.wait_for(self.port.returned.wait(), 1)
        task.cancel()
        await asyncio.wait_for(self.assistant.close_started.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assistant.close_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await self.finished(task)
        self.assertTrue(self.assistant.close_completed)
        self.assertEqual(self.port.hangups, 1)
        self.assertTrue(self.port.absence_confirmed)

    async def test_stop_while_waiting_for_first_peer_cleans_without_accept_timeout(self):
        task = self.launch()
        await asyncio.wait_for(self.port.returned.wait(), 1)
        await asyncio.sleep(0)
        self.signals[signal.SIGTERM]()
        self.assert_startup_cleaned(await self.finished(task))
        self.assertTrue(self.port.stop_event.is_set())

    async def test_stop_during_start_after_accept_prevents_session_construction(self):
        self.port.release.clear()
        task = self.launch()
        reader, _, _, server_writer = await self.connect()
        self.signals[signal.SIGTERM]()
        self.assertTrue(self.port.stop_event.is_set())
        self.port.release.set()
        self.assert_startup_cleaned(await self.finished(task))
        self.assertTrue(server_writer.is_closing())
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')

    async def test_post_originate_stop_proves_cleanup_without_session_or_audio(self):
        self.port = StartupStopPort()
        with patch.dict(runner.os.environ, {
                'CISCO_VOICE_SESSION_ID': TOKEN.hex,
                'CISCO_VOICE_LAUNCH_CHALLENGE': 'a' * 64}):
            task = self.launch()
            await asyncio.wait_for(self.port.entered.wait(), 1)
            reader, _, _, server_writer = await self.connect()
            self.signals[signal.SIGTERM]()
            self.port.release.set()
            report = await self.finished(task)
        self.assertEqual(self.sessions, [])
        self.assertIsNone(report['result'])
        self.assertEqual(report['cleanup_errors'], [])
        self.assertEqual(report['startup_diagnostic']['reason'], 'explicit_stop')
        self.assertEqual(report['startup_diagnostic']['stage'], 'sip_start')
        self.assertEqual(report['cleanup_proof'], {
            'version': 1, 'kind': 'safe_sip_startup_stop', 'transport': 'sip_audiosocket',
            'session_id': TOKEN.hex, 'challenge': 'a' * 64, 'originate_attempted': True,
            'startup_drained': True, 'startup_sealed': True, 'phone_seen': True,
            'cleanup_complete': True})
        self.assertTrue(self.port._sealed)
        self.assertTrue(self.assistant.close_completed)
        self.assertEqual(self.port.hangups, 1)
        self.assertEqual(self.assistant.frames, [])
        self.assertEqual([report[key] for key in
                          ('turns_completed', 'input_frames', 'output_frames')], [0, 0, 0])
        self.assertTrue(server_writer.is_closing())
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')

    async def test_later_stop_cannot_convert_different_startup_failure_into_stop_proof(self):
        self.port = StartupStopPort()
        self.port.failure = BoundaryError('call_setup_timeout')
        with patch.dict(runner.os.environ, {
                'CISCO_VOICE_SESSION_ID': TOKEN.hex,
                'CISCO_VOICE_LAUNCH_CHALLENGE': 'a' * 64}):
            task = self.launch()
            await asyncio.wait_for(self.port.entered.wait(), 1)
            self.signals[signal.SIGTERM]()
            self.port.release.set()
            report = await self.finished(task)
        self.assertNotIn('cleanup_proof', report)
        self.assertIsNone(report['result'])
        self.assertTrue(report['pbx_call_absent'])
        self.assertEqual(report['startup_diagnostic']['reason'], 'call_setup_timeout')

    async def test_cleanup_stop_flag_without_an_actual_signal_cannot_issue_proof(self):
        self.port = StartupStopPort()
        with patch.dict(runner.os.environ, {
                'CISCO_VOICE_SESSION_ID': TOKEN.hex,
                'CISCO_VOICE_LAUNCH_CHALLENGE': 'a' * 64}):
            task = self.launch()
            await asyncio.wait_for(self.port.entered.wait(), 1)
            self.port.release.set()
            report = await self.finished(task)
        self.assertNotIn('cleanup_proof', report)
        self.assertTrue(self.port.stop_event.is_set())
        self.assertTrue(report['pbx_call_absent'])
        self.assertEqual(report['startup_diagnostic']['reason'], 'explicit_stop')

    async def test_cancel_during_start_after_accept_still_closes_peer_service_and_pbx(self):
        self.port.release.clear()
        task = self.launch()
        reader, _, _, server_writer = await self.connect()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.finished(task)
        self.assertEqual(self.sessions, [])
        self.assertTrue(server_writer.is_closing())
        self.assertTrue(self.assistant.close_completed)
        self.assertEqual(self.port.hangups, 1)
        self.assertTrue(self.port.absence_confirmed)
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')

    async def test_start_failure_with_held_open_peer_cleans_before_peer_close(self):
        self.port.release.clear()
        task = self.launch()
        reader, writer, _, _ = await self.connect()
        self.port.failure = RuntimeError('synthetic start failure')
        self.port.release.set()
        self.assert_startup_cleaned(await self.finished(task))
        self.assertFalse(writer.is_closing())
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')

    async def test_no_peer_accept_timeout_still_closes_service_and_reconciles_pbx(self):
        self.port.duration = .05
        task = self.launch()
        self.assert_startup_cleaned(await self.finished(task))

    async def test_invalid_uuid_fails_without_delivering_pcm(self):
        task = self.launch()
        _, writer, _, _ = await self.connect()
        writer.write(packet(1, b'\x00' * 16) + packet(0x12, FRAME))
        await writer.drain()
        report = await self.finished(task)
        self.assertEqual(report['error'], 'voice_session_failed')
        self.assertEqual(report['input_frames'], 0)
        self.assertEqual(self.assistant.frames, [])
        self.assertEqual(report['cleanup_errors'], [])
        self.assertTrue(report['pbx_call_absent'])
        self.assertTrue(self.assistant.close_completed)
        self.assertEqual(self.port.hangups, 1)

    async def test_second_peer_cannot_replace_first_or_hold_cleanup(self):
        self.port.release.clear()
        task = self.launch()
        _, first, _, first_server = await self.connect()
        second_reader, second, _, second_server = await self.connect()
        # Both peers remain client-owned until teardown. Only the first one's
        # distinct frame may enter the assistant; the other transport is closed.
        first.write(packet(1, TOKEN.bytes) + packet(0x12, FRAME) + packet(3, b'#'))
        await first.drain()
        try:
            second.write(packet(1, TOKEN.bytes) + packet(0x12, b'\x02\x00' * 320))
            await second.drain()
        except ConnectionError:
            pass
        self.port.release.set()
        report = await self.finished(task)
        self.assertEqual(report['result']['reason'], 'dtmf_stop')
        self.assertEqual(self.assistant.frames, [FRAME])
        self.assertEqual(report['input_frames'], 1)
        self.assertEqual(report['cleanup_errors'], [])
        self.assertTrue(first_server.is_closing())
        self.assertTrue(second_server.is_closing())
        try:
            self.assertEqual(await asyncio.wait_for(second_reader.read(), 1), b'')
        except ConnectionError:
            pass
        self.assertEqual(self.port.hangups, 1)

    async def test_writer_close_error_is_reported_and_does_not_skip_service_or_pbx(self):
        self.port.release.clear()
        task = self.launch()
        _, _, _, server_writer = await self.connect()
        with patch.object(server_writer, 'wait_closed', side_effect=OSError('synthetic close error')):
            self.port.failure = RuntimeError('synthetic startup failure')
            self.port.release.set()
            report = await self.finished(task)
        self.assertEqual(report['error'], 'voice_session_failed')
        self.assertEqual(report['cleanup_errors'], ['connection_cleanup_failed'])
        self.assertIsNone(report['result'])
        self.assertNotIn('cleanup_proof', report)
        self.assertTrue(self.assistant.close_completed)
        self.assertEqual(self.port.hangups, 1)
        self.assertTrue(report['pbx_call_absent'])

    async def test_repeated_cancellation_during_writer_cleanup_still_reconciles_pbx(self):
        self.port.release.clear()
        task = self.launch()
        _, _, _, server_writer = await self.connect()
        entered, release = asyncio.Event(), asyncio.Event()
        original_wait_closed = server_writer.wait_closed

        async def held_wait_closed():
            entered.set()
            await release.wait()
            await original_wait_closed()

        try:
            with patch.object(server_writer, 'wait_closed', side_effect=held_wait_closed):
                self.port.failure = RuntimeError('synthetic startup failure')
                self.port.release.set()
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await self.finished(task)
        finally:
            release.set()
        self.assertTrue(self.assistant.close_completed)
        self.assertEqual(self.port.hangups, 1)
        self.assertTrue(self.port.absence_confirmed)


if __name__ == '__main__':
    unittest.main()
