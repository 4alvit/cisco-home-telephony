"""Runner wiring tests with every socket, PBX action and provider replaced."""
import asyncio
from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from unittest import mock
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'containers/asterisk'))
from phone_voice_sip import Channel, Grant, Snapshot
from tests.test_phone_voice_sip import Writer, packet, FRAME
from tests.test_phone_voice_control import ControlFixture

SPEC = importlib.util.spec_from_file_location('sip_runner_test', ROOT / 'scripts/voice_session_sip.py')
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
TOKEN = UUID('22222222-2222-4222-8222-222222222222')


class Port:
    latest = None
    def __init__(self, *args):
        import threading
        self.stop_event = threading.Event()
        self.originate_attempted = self.deadline_armed = self.absence_confirmed = False
        self.speaker_attempted = False
        self.grant = None
        Port.latest = self
    async def start(self):
        now = runner.time.monotonic()
        self.originate_attempted = self.deadline_armed = True
        self.grant = Grant(TOKEN, '201', Channel('PJSIP/201-0001', '9.1', '201'), now, now + 3)
        return self.grant
    async def validate_admission(self, grant):
        return grant == self.grant
    def snapshot(self):
        return Snapshot('201', runner.time.monotonic(), True, (self.grant.channel,), frozenset(('201',)))
    async def hangup_and_confirm(self, grant=None):
        self.absence_confirmed = True
        return True
    def seal_no_originate(self):
        return not self.originate_attempted and not self.speaker_attempted


class Assistant:
    def __init__(self):
        self.playback = SimpleNamespace()
        self.frames = []
    def ensure_allowed(self, phone):
        pass
    async def listen(self, phone):
        return 'stream'
    def status(self, phone):
        return SimpleNamespace(state='armed')
    def feed_pcm(self, phone, stream, pcm):
        self.frames.append(pcm)
    async def stop(self, phone, reason):
        pass
    async def close(self):
        pass


class Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.args = SimpleNamespace(session_id=TOKEN.hex, lease_fd=9, state_directory='/unused',
            workspace='/unused', status_file=None, lease_expires_unix=runner.time.time() + 3, ffmpeg='unused')
        self.config = {'line': '201', 'bind_port': 24576}
        self.writer = Writer()
        self.writer.get_extra_info = lambda *args: ('127.0.0.1', 12345)
        self.assistant = Assistant()
        self.progress = SimpleNamespace(turns_completed=0, publish=lambda *args: None)
        self.reader = asyncio.StreamReader()
        self.reader.feed_data(packet(1, TOKEN.bytes) + packet(0x12, FRAME) + packet(3, b'#'))

    async def serve(self, callback, host, port, **kwargs):
        self.assertEqual(host, '127.0.0.1')
        self.assertEqual(kwargs['backlog'], 1)
        callback(self.reader, self.writer)
        async def closed():
            return
        return SimpleNamespace(close=lambda: None, wait_closed=closed)

    async def test_complete_runtime_routes_frame_and_hangup_with_no_xml_media_commands(self):
        with patch.object(runner, 'LeaseWitness', return_value=SimpleNamespace(verify=lambda: None)), \
             patch.object(runner, 'SessionProgress', return_value=self.progress), \
             patch.object(runner, 'AmiPort', Port), \
             patch.object(runner, 'build_service', return_value=self.assistant), \
             patch.object(runner.asyncio, 'start_server', side_effect=self.serve), \
             patch.object(runner.transport, '_gateway_module', return_value=None):
            result = await runner.run(self.args, self.config, {}, {'id': '201'}, {})
        self.assertEqual(self.assistant.frames, [FRAME])
        self.assertEqual(result['result']['reason'], 'dtmf_stop')
        self.assertTrue(result['result']['stop_confirmed'])
        self.assertTrue(result['pbx_call_absent'])
        self.assertEqual(result['session_id'], TOKEN.hex)
        self.assertFalse(result['safe_no_media_started'])
        self.assertTrue(self.writer.closed)

    async def test_model_failure_precedes_listener_and_originate(self):
        with patch.object(runner, 'LeaseWitness', return_value=SimpleNamespace(verify=lambda: None)), \
             patch.object(runner, 'SessionProgress', return_value=self.progress), \
             patch.object(runner, 'AmiPort', Port), \
             patch.object(runner, 'build_service', side_effect=RuntimeError('synthetic')), \
             patch.object(runner.asyncio, 'start_server') as listen, \
             patch.object(runner.transport, '_gateway_module', return_value=None):
            result = await runner.run(self.args, self.config, {}, {'id': '201'}, {})
        listen.assert_not_called()
        self.assertFalse(Port.latest.originate_attempted)
        self.assertTrue(result['safe_no_media_started'])
        self.assertEqual(result['error'], 'voice_session_failed')
        self.assertNotIn('synthetic', str(result))

    async def failure_report(self, *, env=None, build_failure=True, close_failure=False,
                             port_type=Port, gateway=None, bind_failure=False):
        build = mock.Mock(side_effect=RuntimeError('synthetic')) if build_failure else mock.Mock(return_value=self.assistant)
        self.assistant.close = mock.AsyncMock(side_effect=RuntimeError('synthetic close') if close_failure else None)
        listener = mock.AsyncMock(side_effect=OSError('synthetic bind') if bind_failure else self.serve)
        with patch.object(runner, 'LeaseWitness', return_value=SimpleNamespace(verify=lambda: None)), \
             patch.object(runner, 'SessionProgress', return_value=self.progress), \
             patch.object(runner, 'AmiPort', port_type), \
             patch.object(runner, 'build_service', build), \
             patch.object(runner.asyncio, 'start_server', listener), \
             patch.object(runner.transport, '_gateway_module', return_value=gateway), \
             patch.dict(runner.os.environ, env or {}, clear=True):
            return await runner.run(self.args, self.config, {'ami': {}},
                                    {'id': '201', 'lines': ['201', '202'], 'device': 'SEP020000000001'}, {})

    def identity(self):
        return {'CISCO_VOICE_SESSION_ID': TOKEN.hex, 'CISCO_VOICE_LAUNCH_CHALLENGE': 'a' * 64}

    async def test_model_failure_emits_bound_proof_with_real_port_after_clean_reconciliation(self):
        from tests.test_phone_voice_sip_ami import Gateway, no_sip_report
        actual_port = runner.AmiPort
        gateway = Gateway()
        result = await self.failure_report(env=self.identity(), port_type=actual_port, gateway=gateway)
        self.assertEqual(result['cleanup_proof'], {
            'version': 1, 'kind': 'safe_no_sip_originate', 'transport': 'sip_audiosocket',
            'session_id': TOKEN.hex, 'challenge': 'a' * 64,
            'originate_attempted': False, 'cleanup_complete': True})
        self.assertEqual(gateway.actions, [])
        self.assertTrue(result['pbx_call_absent'])
        self.assertEqual(result['cleanup_errors'], [])
        self.assertIsNone(result['result'])
        self.assertEqual(result, no_sip_report(TOKEN.hex, 'a' * 64))

    async def test_missing_malformed_or_other_session_identity_never_emits_proof(self):
        cases = [{}, dict(self.identity(), CISCO_VOICE_SESSION_ID='0' * 32),
                 {'CISCO_VOICE_SESSION_ID': TOKEN.hex},
                 dict(self.identity(), CISCO_VOICE_LAUNCH_CHALLENGE='invalid'),
                 dict(self.identity(), CISCO_VOICE_LAUNCH_CHALLENGE='A' * 64)]
        for env in cases:
            with self.subTest(env_keys=sorted(env)):
                result = await self.failure_report(env=env)
                self.assertNotIn('cleanup_proof', result)

    async def test_bind_failure_is_provable_but_assistant_close_failure_is_not(self):
        result = await self.failure_report(env=self.identity(), build_failure=False, bind_failure=True)
        self.assertIn('cleanup_proof', result)
        result = await self.failure_report(env=self.identity(), build_failure=False,
                                           bind_failure=True, close_failure=True)
        self.assertNotIn('cleanup_proof', result)
        self.assertIn('assistant_cleanup_failed', result['cleanup_errors'])

    async def test_attempted_originate_or_nonzero_turns_excludes_proof(self):
        class Attempted(Port):
            async def start(self):
                self.originate_attempted = True
                raise TimeoutError('synthetic ambiguous originate')
        result = await self.failure_report(env=self.identity(), build_failure=False, port_type=Attempted)
        self.assertNotIn('cleanup_proof', result)
        self.progress.turns_completed = 1
        result = await self.failure_report(env=self.identity())
        self.assertNotIn('cleanup_proof', result)


class PreemptionTests(ControlFixture):
    """Real snapshot, session, runner progress and manager; synthetic external I/O."""
    def setUp(self):
        super().setUp()
        self.transport['media_transport'] = 'sip_audiosocket'
        self.voice['wake']['mode'] = 'onnx'
        self.write_inputs()

    async def run_child(self, state, endpoint_state, *, unavailable=False, stale=False):
        from tests.test_phone_voice_sip_ami import Gateway
        argv, _, env, lease_fd = self.launches[-1]
        argument = lambda name: argv[argv.index('--' + name) + 1]
        args = SimpleNamespace(session_id=state['session_id'], lease_fd=lease_fd,
            state_directory=argument('state-directory'), workspace=argument('workspace'),
            status_file=argument('status-file'),
            lease_expires_unix=float(argument('lease-expires-unix')), ffmpeg=argument('ffmpeg'))
        token = UUID(args.session_id)
        gateway = Gateway()
        gateway.root = 'cisco-voice-' + token.hex
        factory = gateway.AMI
        changed = False
        ports = []

        def ami(config):
            connection = factory(config)
            original_action, original_snapshot = connection.action, connection.snapshot
            def action(name, **headers):
                result = original_action(name, **headers)
                if name == 'Getvar':
                    values = {'VOICE_SIP_SESSION': token.hex,
                              'VOICE_SIP_END': str(int(args.lease_expires_unix)),
                              'TIMEOUT(absolute)': '1'}
                    return [{'Value': values[headers['Variable']]}]
                return result
            def snapshot():
                endpoints, channels = original_snapshot()
                if changed:
                    endpoints['202'] = endpoint_state
                    if unavailable:
                        endpoints['201'] = 'Unavailable'
                return endpoints, channels
            connection.action, connection.snapshot = action, snapshot
            return connection
        gateway.AMI = ami
        actual_port = runner.AmiPort
        def port_factory(*args, **kwargs):
            port = actual_port(*args, **kwargs)
            ports.append(port)
            return port

        assistant = Assistant()
        def feed(phone, stream, pcm):
            nonlocal changed
            assistant.frames.append(pcm)
            changed = gateway.foreign = True
            with gateway.AMI({}) as connection:
                ports[0]._snapshot(connection)
            if stale:
                ports[0].observation = replace(ports[0].observation,
                    request_started_at=runner.time.monotonic() - 2)
        assistant.feed_pcm = feed
        reader, writer = asyncio.StreamReader(), Writer()
        writer.get_extra_info = lambda *args: ('127.0.0.1', 12345)
        reader.feed_data(packet(1, token.bytes) + packet(0x12, FRAME))
        async def serve(callback, *args, **kwargs):
            callback(reader, writer)
            return SimpleNamespace(close=lambda: None, wait_closed=mock.AsyncMock())

        # Drive snapshots at exact frame boundaries instead of a background
        # schedule. Every observation still passes through the real AMI adapter.
        with patch.object(actual_port, '_observe'), \
             patch.object(runner, 'AmiPort', side_effect=port_factory), \
             patch.object(runner, 'build_service', return_value=assistant), \
             patch.object(runner.asyncio, 'start_server', side_effect=serve), \
             patch.object(runner.transport, '_gateway_module', return_value=gateway), \
             patch.dict(runner.os.environ, env, clear=True):
            report = await runner.run(args, self.transport, {'ami': {}}, self.gateway['phones'][0], {})
        ports[0].thread.join(timeout=1)
        self.assertFalse(ports[0].thread.is_alive())
        self.assertEqual(assistant.frames, [FRAME])
        self.assertTrue(writer.closed)
        self.assertTrue(report['result']['stop_confirmed'])
        self.assertTrue(report['pbx_call_absent'])
        self.assertEqual(report['cleanup_errors'], [])
        self.assertEqual([value['Channel'] for name, value in gateway.actions if name == 'Hangup'], ['99.1'])
        with gateway.AMI({}) as connection:
            _, remaining = connection.snapshot()
        self.assertEqual([item['Uniqueid'] for item in remaining], ['99.2'])
        return report

    def test_known_preemption_reaches_public_call_and_preserves_foreign_channel(self):
        manager = self.make_control()
        for index, endpoint in enumerate(('Ringing', 'Ring+Inuse', 'Busy', 'On Hold')):
            with self.subTest(endpoint=endpoint):
                state = self.start(manager, request_id=f'known-preemption-{index}')
                report = asyncio.run(self.run_child(state, endpoint))
                self.assertEqual(report['result']['reason'], 'call_changed')
                self.assertIsNone(report['error'])
                self.runners[-1].finish(report)
                manager.tick()
                status = self.status(manager)
                self.assertEqual((status['state'], status['phase_reason'], status['error']),
                                 ('stopped', 'call', None))
                self.assertTrue(status['stop_confirmed'])
                self.assertTrue(manager._sessions[state['session_id']].cleanup_verified)

    def test_unavailable_or_stale_preemption_remains_failed_after_clean_owned_hangup(self):
        manager = self.make_control()
        for index, options in enumerate(({'unavailable': True}, {'stale': True})):
            with self.subTest(options=options):
                state = self.start(manager, request_id=f'unknown-preemption-{index}')
                report = asyncio.run(self.run_child(state, 'Ringing', **options))
                self.assertEqual(report['result']['reason'], 'disconnected' if index == 0 else 'unknown')
                self.assertEqual(report['error'], 'voice_session_failed')
                self.runners[-1].finish(report, code=1)
                manager.tick()
                status = self.status(manager)
                self.assertEqual((status['state'], status['error']), ('failed', 'voice_session_failed'))
                self.assertNotEqual(status['phase_reason'], 'call')
                self.assertTrue(status['stop_confirmed'])


if __name__ == '__main__':
    unittest.main()
