"""Deterministic alert-fixture startup checks; no Docker or network required."""
import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/test-alert-originate.py'
SPEC = importlib.util.spec_from_file_location('alert_fixture_readiness', SCRIPT)
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


class AlertFixtureTest(unittest.TestCase):
    def setUp(self):
        self.outputs = {
            'core show application WaitForSignal': 'WaitForSignal(signalname,[signaltimeout])',
            'dialplan show 201@cisco-alert':
                '  4. Dial(PJSIP/201,,iU(cisco-alert-answered)) [extensions.conf:101]',
            'pjsip show endpoints': ' Endpoint: 201/201 Unavailable 0 of inf',
        }

    def probe(self, outputs=None, failed=None):
        outputs = self.outputs if outputs is None else outputs

        def run(command, **kwargs):
            self.assertEqual(command[:7], ['docker', '--context', 'fixture', 'exec',
                                          'created-fixture', 'asterisk', '-rx'])
            self.assertEqual(kwargs['timeout'], 5)
            return subprocess.CompletedProcess(command, int(command[-1] == failed),
                                               outputs.get(command[-1], ''), '')

        with mock.patch.object(fixture.subprocess, 'run', side_effect=run):
            return fixture.fixture_ready(['docker', '--context', 'fixture'], 'created-fixture')

    def test_application_loaded_before_dialplan_is_not_ready(self):
        self.assertFalse(self.probe({'core show application WaitForSignal':
                                     self.outputs['core show application WaitForSignal']}))

    def test_dialplan_loaded_before_endpoint_is_not_ready(self):
        outputs = dict(self.outputs, **{'pjsip show endpoints': 'No objects found.'})
        self.assertFalse(self.probe(outputs))

    def test_wrong_endpoint_or_changed_alert_route_is_not_ready(self):
        for key, value in (
                ('pjsip show endpoints', ' Endpoint: 2010/2010 Not in use 0 of inf'),
                ('dialplan show 201@cisco-alert', 'Dial(PJSIP/201,20,iU(cisco-alert-answered))'),
                ('core show application WaitForSignal', 'No application found.')):
            with self.subTest(key=key):
                self.assertFalse(self.probe(dict(self.outputs, **{key: value})))

    def test_loaded_prerequisites_are_required_even_if_cli_returns_output_on_failure(self):
        self.assertTrue(self.probe())
        for command in self.outputs:
            with self.subTest(command=command):
                self.assertFalse(self.probe(failed=command))

    def test_post_bind_wait_requires_exact_available_endpoint(self):
        ami = mock.Mock()
        phone = SimpleNamespace(error=None)
        ami.action.side_effect = [
            [{'Event': 'EndpointList', 'ObjectName': '201', 'DeviceState': 'Unavailable'}],
            [{'Event': 'EndpointList', 'ObjectName': '201', 'DeviceState': 'Not in use'}],
        ]
        with mock.patch.object(fixture.time, 'sleep'):
            self.assertTrue(fixture.await_condition(lambda: fixture.endpoint_ready(ami, phone),
                                                    8, 'synthetic alert endpoint ready'))
        self.assertEqual(ami.action.call_args_list, [
            mock.call('PJSIPShowEndpoints', complete='EndpointListComplete'),
            mock.call('PJSIPShowEndpoints', complete='EndpointListComplete')])

    def test_wrong_endpoint_and_busy_endpoint_do_not_pass_post_bind_wait(self):
        for row in (
                {'Event': 'EndpointList', 'ObjectName': '2010', 'DeviceState': 'Not in use'},
                {'Event': 'EndpointList', 'ObjectName': '201', 'DeviceState': 'Ringing'},
                {'Event': 'OtherEvent', 'ObjectName': '201', 'DeviceState': 'Not in use'}):
            with self.subTest(row=row):
                ami = mock.Mock()
                ami.action.return_value = [row]
                self.assertFalse(fixture.endpoint_ready(ami, SimpleNamespace(error=None)))

    def test_worker_failure_is_not_silently_polled(self):
        ami = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, 'SIP fixture worker failed'):
            fixture.endpoint_ready(ami, SimpleNamespace(error='socket failure'))
        ami.action.assert_not_called()

    def test_no_originate_before_post_bind_readiness(self):
        ami = mock.Mock()
        ami.action.return_value = [{'AsteriskVersion': '22.11.0'}]
        ami.channels.return_value = []
        phone = mock.Mock()
        phone.error = None
        with mock.patch.object(fixture.socket, 'if_nameindex', return_value=[(1, 'lo')]), \
                mock.patch.object(fixture.Path, 'read_text', return_value='{}'), \
                mock.patch.object(fixture, 'AMI', return_value=ami), \
                mock.patch.object(fixture, 'Phone', return_value=phone), \
                mock.patch.object(fixture, 'await_condition', side_effect=RuntimeError('not ready')):
            with self.assertRaisesRegex(RuntimeError, 'not ready'):
                fixture.test_inside()
        self.assertEqual(ami.action.call_args_list, [mock.call('CoreSettings')])
        ami.close.assert_called_once()
        phone.close.assert_called_once()

    def test_fixture_transform_refuses_unexpected_qualification_shape(self):
        for value in ('[201]\ntype=aor\n',
                      '[201]\ntype=aor\nqualify_frequency=60\nqualify_frequency=60\n',
                      '[202]\ntype=aor\nqualify_frequency=60\n'):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                fixture.fixture_pjsip(value)

    def test_generated_fixture_and_failure_diagnostics_use_only_created_container(self):
        calls = []

        def run(command, **kwargs):
            self.assertEqual(command[0], 'docker')
            calls.append(command)
            action = command[1]
            output, code = '', 0
            if action == 'create':
                self.assertIn('none', command)
                self.assertIn('never', command)
                output = 'created-fixture\n'
            elif action == 'inspect':
                output = 'none\n'
            elif action == 'cp':
                folder = Path(command[2])
                config = (folder / 'pjsip.conf').read_text()
                self.assertIn('contact=sip:201@127.0.0.2:5090\n', config)
                self.assertEqual(config.count('qualify_frequency=1\n'), 1)
                self.assertNotIn('qualify_frequency=60\n', config)
                self.assertEqual(command[3], 'created-fixture:/etc/asterisk/')
            elif action == 'exec' and command[3] == 'asterisk':
                output = self.outputs[command[-1]]
            elif action == 'exec':
                code, output = 1, '{"status":"FAIL","error":"synthetic injected failure"}'
            elif action == 'logs':
                self.assertEqual(command, ['docker', 'logs', '--tail', '80', 'created-fixture'])
                output = 'synthetic fixture diagnostic'
            else:
                self.assertIn(action, ('start', 'rm'))
                self.assertEqual(command[-1], 'created-fixture')
            return subprocess.CompletedProcess(command, code, output, '')

        stderr = io.StringIO()
        with mock.patch.object(sys, 'argv', [str(SCRIPT), '--image', 'synthetic-image']), \
                mock.patch.object(fixture.subprocess, 'run', side_effect=run), \
                contextlib.redirect_stderr(stderr):
            with self.assertRaisesRegex(RuntimeError, 'synthetic injected failure'):
                fixture.main()
        self.assertIn('synthetic fixture diagnostic', stderr.getvalue())
        self.assertEqual([call[1] for call in calls][-2:], ['logs', 'rm'])

    def test_log_failure_does_not_replace_original_error_and_tail_is_bounded(self):
        for outcome in (subprocess.TimeoutExpired('docker', 5), OSError('unavailable')):
            with mock.patch.object(fixture.subprocess, 'run', side_effect=outcome), \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                fixture.failure_logs(['docker'], 'created-fixture')
                self.assertIn('logs unavailable', stderr.getvalue())
        result = subprocess.CompletedProcess([], 0, 'x' * 20000, 'last-line')
        with mock.patch.object(fixture.subprocess, 'run', return_value=result), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            fixture.failure_logs(['docker'], 'created-fixture')
        self.assertLess(len(stderr.getvalue()), 12100)
        self.assertTrue(stderr.getvalue().endswith('last-line\n'))


if __name__ == '__main__':
    unittest.main()
