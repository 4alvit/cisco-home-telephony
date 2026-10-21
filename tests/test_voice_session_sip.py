"""Runner wiring tests with every socket, PBX action and provider replaced."""
import asyncio
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


if __name__ == '__main__':
    unittest.main()
