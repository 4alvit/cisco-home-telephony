"""Startup diagnostics retain fixed failure categories, never private messages."""
from contextlib import redirect_stdout
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests import test_phone_gateway as gateway_tests
from tests import test_phone_voice_sip_ami as ami_tests
from tests import test_voice_session_sip as sip_tests

runner = sip_tests.runner
SECRET = 'private-password-and-session-path-not-for-reports'


class DiagnosticCategories(unittest.TestCase):
    def test_known_boundary_reason_is_not_replaced_by_fallback_reason(self):
        diagnostic = runner.startup_diagnostic(
            runner.BoundaryError('pbx_deadline_unverified'), 'sip_start',
            SimpleNamespace(startup_stage='verify_deadline'), reason='accept_timeout')
        self.assertEqual(diagnostic, {
            'version': 1, 'reason': 'pbx_deadline_unverified',
            'error_type': 'BoundaryError', 'stage': 'sip_start',
            'ami_stage': 'verify_deadline', 'ami_action': 'other', 'ami_error': 'other'})
        self.assertTrue(runner.control.valid_startup_diagnostic(diagnostic))

    def test_unknown_reason_and_exception_details_are_never_serialized(self):
        class PrivateError(Exception):
            def __str__(self):
                raise AssertionError('diagnostics must not format exceptions')

        for error, expected_type in (
                (runner.BoundaryError(SECRET), 'BoundaryError'),
                (runner.BoundaryError('explicit_stop', SECRET), 'BoundaryError'),
                (RuntimeError(SECRET), 'RuntimeError'),
                (OSError(SECRET), 'OSError'),
                (TimeoutError(SECRET), 'TimeoutError'),
                (PrivateError(SECRET), 'other')):
            with self.subTest(error_type=expected_type, arguments=len(error.args)):
                error.ami_action = SECRET
                error.ami_error = {'secret': SECRET}
                diagnostic = runner.startup_diagnostic(
                    error, SECRET, SimpleNamespace(startup_stage=[SECRET]), reason=SECRET)
                self.assertEqual(diagnostic, {
                    'version': 1, 'reason': 'other', 'error_type': expected_type,
                    'stage': 'other', 'ami_stage': 'other',
                    'ami_action': 'other', 'ami_error': 'other'})
                self.assertNotIn(SECRET, json.dumps(diagnostic))
                self.assertTrue(runner.control.valid_startup_diagnostic(diagnostic))

    def test_timeout_uses_fixed_runner_reason_without_exception_message(self):
        diagnostic = runner.startup_diagnostic(
            TimeoutError(SECRET), 'peer_accept', reason='accept_timeout')
        self.assertEqual(diagnostic['reason'], 'accept_timeout')
        self.assertEqual(diagnostic['error_type'], 'TimeoutError')
        self.assertEqual(diagnostic['ami_stage'], 'not_started')
        self.assertNotIn(SECRET, json.dumps(diagnostic))

    def test_gateway_login_refusal_retains_only_categorized_wire_error(self):
        gateway = gateway_tests.gateway
        for message, category in (
                ('Login Already In Use', 'login_in_use'),
                ('Authentication failed', 'authentication_failed'),
                (SECRET, 'other')):
            with self.subTest(category=category):
                ami = gateway.AMI({})
                ami.sock = Mock()
                ami.file = io.BytesIO(
                    ('Response: Error\r\nActionID: diagnostic-test\r\nMessage: ' +
                     message + '\r\n\r\n').encode())
                with patch.object(gateway.secrets, 'token_hex', return_value='diagnostic-test'):
                    with self.assertRaises(gateway.Refused) as caught:
                        ami.action('Login', Secret=SECRET)
                diagnostic = runner.startup_diagnostic(
                    caught.exception, 'sip_start',
                    SimpleNamespace(gateway=gateway, startup_stage='login'))
                self.assertEqual(diagnostic, {
                    'version': 1, 'reason': 'ami_action_rejected', 'error_type': 'Refused',
                    'stage': 'sip_start', 'ami_stage': 'login',
                    'ami_action': 'Login', 'ami_error': category})
                self.assertNotIn(SECRET, json.dumps(diagnostic))
                self.assertTrue(runner.control.valid_startup_diagnostic(diagnostic))

    def test_unknown_gateway_reason_and_attributes_do_not_escape_allowlists(self):
        gateway = gateway_tests.gateway
        error = gateway.Refused(SECRET)
        error.ami_action, error.ami_error = SECRET, SECRET
        diagnostic = runner.startup_diagnostic(
            error, 'sip_start', SimpleNamespace(gateway=gateway, startup_stage='login'))
        self.assertEqual(diagnostic['error_type'], 'Refused')
        for field in ('reason', 'ami_action', 'ami_error'):
            self.assertEqual(diagnostic[field], 'other')
        self.assertNotIn(SECRET, json.dumps(diagnostic))

    def test_main_input_failure_preserves_valid_session_without_cleanup_proof(self):
        for session_id, expected_session in (
                (str(sip_tests.TOKEN).upper(), sip_tests.TOKEN.hex),
                (SECRET, None)):
            with self.subTest(valid_session=expected_session is not None):
                argv = ['voice_session_sip.py',
                        '--transport-config', '/unused/transport.json',
                        '--voice-config', '/unused/voice.json', '--workspace', '/unused',
                        '--state-directory', '/unused/state', '--session-id', session_id,
                        '--lease-fd', '9', '--lease-expires-unix', '1299',
                        '--status-file', '/unused/status.json', '--run', '--attended',
                        '--exclusive-media', '--enable-microphone']
                output = io.StringIO()
                with patch.object(runner.sys, 'argv', argv), \
                     patch.object(runner.control, '_inputs', side_effect=RuntimeError(SECRET)), \
                     patch.object(runner, 'run') as run, redirect_stdout(output):
                    code = runner.main()
                run.assert_not_called()
                self.assertEqual(code, 1)
                report = json.loads(output.getvalue())
                self.assertEqual(report['error'], 'voice_session_failed')
                self.assertEqual(report['transport'], 'sip_audiosocket')
                self.assertEqual(report.get('session_id'), expected_session)
                self.assertEqual(report['startup_diagnostic'], {
                    'version': 1, 'reason': 'other', 'error_type': 'RuntimeError',
                    'stage': 'inputs_or_initialization', 'ami_stage': 'not_started',
                    'ami_action': 'other', 'ami_error': 'other'})
                self.assertNotIn('cleanup_proof', report)
                self.assertNotIn('safe_no_media_started', report)
                self.assertNotIn(SECRET, output.getvalue())


class DiagnosticRuntime(unittest.IsolatedAsyncioTestCase):
    # Reuse only the fixture helpers, without inheriting or rediscovering its tests.
    setUp = sip_tests.Tests.setUp
    serve = sip_tests.Tests.serve
    failure_report = sip_tests.Tests.failure_report
    identity = sip_tests.Tests.identity

    async def test_first_startup_failure_is_captured_before_cleanup_mutates_port(self):
        class FailedPort(sip_tests.Port):
            async def start(self):
                self.originate_attempted = True
                self.startup_stage = 'verify_deadline'
                raise runner.BoundaryError('pbx_deadline_unverified')

            async def hangup_and_confirm(self, grant=None):
                self.startup_stage = 'complete'
                raise RuntimeError(SECRET)

        with patch.object(runner, 'Session') as session, patch.object(runner, 'Wire') as wire:
            report = await self.failure_report(
                env=self.identity(), build_failure=False, port_type=FailedPort)
        session.assert_not_called()
        wire.assert_not_called()
        self.assertEqual(sip_tests.Port.latest.startup_stage, 'complete')
        self.assertEqual(report['startup_diagnostic'], {
            'version': 1, 'reason': 'pbx_deadline_unverified', 'error_type': 'BoundaryError',
            'stage': 'sip_start', 'ami_stage': 'verify_deadline',
            'ami_action': 'other', 'ami_error': 'other'})
        self.assertEqual(report['cleanup_errors'], ['pbx_cleanup_failed'])
        self.assertIsNone(report['result'])
        self.assertTrue(report['originate_attempted'])
        self.assertFalse(report['pbx_call_absent'])
        self.assertFalse(report['safe_no_media_started'])
        self.assertEqual([report[key] for key in (
            'turns_completed', 'input_frames', 'output_frames')], [0, 0, 0])
        self.assertNotIn('cleanup_proof', report)
        self.assertNotIn(SECRET, json.dumps(report))

    async def test_setup_projection_is_copied_before_cleanup(self):
        class FailedPort(sip_tests.Port):
            async def start(self):
                self.originate_attempted = True
                self.startup_stage = 'setup_snapshot'
                root = 'cisco-voice-' + sip_tests.TOKEN.hex
                rows = [{'Channel': 'PJSIP/201-a', 'Uniqueid': '12.1',
                         'Linkedid': root, 'ChannelStateDesc': 'Ringing'}] * 2
                self.rejected_setup = runner.control.project_setup_rejection(root, rows, rows)
                raise runner.BoundaryError('call_changed')

            async def hangup_and_confirm(self, grant=None):
                self.startup_stage = 'complete'
                self.rejected_setup['rows'].clear()
                return False

        report = await self.failure_report(
            env=self.identity(), build_failure=False, port_type=FailedPort)
        detail = report['startup_diagnostic']
        self.assertTrue(runner.control.valid_startup_diagnostic(detail))
        self.assertEqual(detail['version'], 2)
        self.assertEqual(len(detail['rejected_setup']['rows']), 2)
        self.assertFalse(report['pbx_call_absent'])
        self.assertFalse(report['safe_no_media_started'])
        self.assertNotIn('cleanup_proof', report)

    async def test_real_ami_admission_failure_stage_survives_scoped_hangup(self):
        for option, reason, stage in (
                ('wrong_token', 'call_admission_mismatch', 'verify_session'),
                ('missing_deadline', 'pbx_deadline_unverified', 'verify_deadline')):
            with self.subTest(option=option):
                gateway = ami_tests.Gateway()
                setattr(gateway, option, True)
                port = runner.AmiPort(
                    gateway, {'ami': {}}, ami_tests.PHONE, '201', ami_tests.SESSION,
                    1299, 24576, ami_tests.Witness(),
                    clock=lambda: 100, wall_clock=lambda: 1000)
                self.addCleanup(port.stop_event.set)
                with self.assertRaises(runner.BoundaryError) as caught:
                    await port.start()
                self.assertEqual(caught.exception.args, (reason,))
                self.assertEqual(port.startup_stage, stage)
                before = runner.startup_diagnostic(caught.exception, 'sip_start', port)
                self.assertEqual(before['reason'], reason)
                self.assertEqual(before['ami_stage'], stage)
                self.assertTrue(port.originate_attempted)
                self.assertFalse(port.deadline_armed)
                self.assertIsNone(port.grant)
                self.assertEqual(gateway.keys, [])
                self.assertTrue(await port.hangup_and_confirm())
                self.assertTrue(port.absence_confirmed)
                self.assertEqual([args['Channel'] for name, args in gateway.actions
                                  if name == 'Hangup'], ['99.1'])
                self.assertEqual(port.startup_stage, stage)
                self.assertEqual(
                    runner.startup_diagnostic(caught.exception, 'sip_start', port), before)


if __name__ == '__main__':
    unittest.main()
