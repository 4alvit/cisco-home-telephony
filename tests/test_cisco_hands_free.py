"""Hands-free CLI contracts, with all gateway/process/network I/O replaced."""
import contextlib
import copy
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import unittest
from unittest import mock
from urllib import error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cisco
import cisco_http

SESSION = 'a' * 32
PRIVATE = 'synthetic-private-token-and-transcript'
CONFIG = {'transport': 'direct', 'gateway_url': 'http://192.168.50.10:18084',
          'token': 'a' * 48, 'kubectl': {'context': 'example', 'namespace': 'example',
          'selector': 'app=example', 'container': 'example'}}


def finite(**changes):
    return dict({'schema_version': 1, 'phone': '201', 'line': '201', 'session_id': SESSION,
                 'state': 'running', 'active': True, 'remaining_seconds': 100,
                 'stop_requested': False, 'stop_confirmed': False, 'phase': 'armed',
                 'phase_reason': 'wake_ready', 'error': None}, **changes)


def intent(**changes):
    return dict({'schema_version': 1, 'phone': '201', 'line': '201', 'session_id': SESSION,
                 'enabled': True, 'state': 'running', 'session_seconds': 300,
                 'consecutive_failures': 0, 'retry_after_seconds': 0, 'error': None}, **changes)


class HandsFreeCLITests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket', 'socket.create_connection', 'subprocess.Popen'):
            patch = mock.patch(target, side_effect=AssertionError('unexpected external operation'))
            patch.start()
            self.addCleanup(patch.stop)

    def invoke(self, command, *, voice=None, hands_free=None, failure=None, human=False,
               line='202', response_transform=None):
        calls = []
        def request(path, body=None):
            calls.append((path, copy.deepcopy(body)))
            if 'hands_free' in path:
                if failure is not None:
                    raise failure
                value = intent() if hands_free is None else hands_free
                response = {'status': 'ok' if body is None else 'accepted', 'hands_free': value}
                if body is not None:
                    response['action_id'] = body['idempotency_key']
            else:
                response = {'status': 'ok', 'voice': finite() if voice is None else voice}
            return response_transform(response) if response_transform else response
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cisco, 'load_config', return_value=CONFIG), \
             mock.patch.object(cisco.Gateway, 'request', side_effect=request), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cisco.main([*([] if human else ['--json']), '--config', 'private config/example.json',
                               '--timeout', '3', line, 'voice', *command])
        return code, (out.getvalue() + err.getvalue()) if human else json.loads(out.getvalue()), calls

    def test_enable_generates_one_key_and_explicit_api_consent(self):
        with mock.patch.object(cisco.secrets, 'token_hex', return_value='b' * 32) as random:
            code, result, calls = self.invoke(['enable'], hands_free=intent(state='waiting_idle', session_id=None))
        self.assertEqual(code, 0)
        random.assert_called_once_with(16)
        self.assertEqual(calls, [('/v1/phones/202/voice_hands_free_enable', {
            'idempotency_key': 'cli-' + 'b' * 32, 'enable_microphone': True,
            'exclusive_media': True, 'dry_run': False})])
        self.assertEqual(result['action_id'], 'cli-' + 'b' * 32)
        self.assertEqual(result['selected_line'], '202')

    def test_disable_between_sessions_is_one_atomic_post_without_status(self):
        code, result, calls = self.invoke(['disable', '--idempotency-key', 'disable-example'],
            hands_free=intent(enabled=False, state='disabled', session_id=None))
        self.assertEqual(code, 0)
        self.assertEqual(calls, [('/v1/phones/202/voice_hands_free_disable', {
            'idempotency_key': 'disable-example'})])
        self.assertFalse(result['hands_free']['enabled'])
        self.assertIsNone(result['hands_free']['session_id'])

    def test_replayed_enable_reports_current_disabled_intent_without_another_attempt(self):
        code, result, calls = self.invoke(['enable', '--idempotency-key', 'old-enable-key'],
            hands_free=intent(enabled=False, state='disabled', session_id=None))
        self.assertEqual(code, 0)
        self.assertFalse(result['hands_free']['enabled'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]['idempotency_key'], 'old-enable-key')

    def test_enable_dry_run_does_not_claim_enabled_or_active(self):
        dry = {'schema_version': 1, 'phone': '201', 'line': '202', 'valid': True,
               'network_actions': 0, 'microphone_started': False, 'duration_seconds': 300}
        code, result, calls = self.invoke(['enable', '--dry-run', '--idempotency-key', 'dry-example'],
                                          hands_free=dry)
        self.assertEqual((code, result['status']), (0, 'dry_run'))
        self.assertFalse(result['microphone_started'])
        self.assertNotIn('hands_free', result)
        self.assertTrue(calls[0][1]['dry_run'])
        for changes in ({'valid': False}, {'microphone_started': True}, {'network_actions': True},
                        {'duration_seconds': 10 ** 1000}, {'duration_seconds': 29}, {'line': '203'}):
            code, result, _ = self.invoke(['enable', '--dry-run'], hands_free=dict(dry, **changes))
            self.assertEqual((code, result['reason']), (1, 'invalid_hands_free_response'))

    def test_disable_dry_run_and_invalid_key_never_send(self):
        for args in (['disable', '--dry-run'], ['enable', '--idempotency-key', 'bad'],
                     ['disable', '--idempotency-key', 'bad']):
            code, _, calls = self.invoke(args)
            self.assertEqual(code, 1)
            self.assertEqual(calls, [])

    def test_status_reads_both_endpoints_and_correlates_physical_phone_session(self):
        for line in ('201', '202'):
            code, result, calls = self.invoke(['status'], line=line)
            self.assertEqual(code, 0)
            self.assertEqual(calls, [(f'/v1/phones/{line}/voice_status', None),
                                    (f'/v1/phones/{line}/voice_hands_free_status', None)])
            self.assertTrue(result['hands_free_listening'])
            self.assertEqual(result['voice']['line'], '201')
            self.assertEqual(result['hands_free']['line'], '201')
        code, result, _ = self.invoke(['status'], hands_free=intent(phone='203'))
        self.assertEqual((code, result['reason']), (1, 'invalid_hands_free_response'))

    def test_false_readiness_never_says_ready_or_listening(self):
        changes = ({'phase_reason': 'model_warmup'}, {'phase_reason': None},
                   {'phase': 'thinking'}, {'session_id': 'b' * 32}, {'error': PRIVATE},
                   {'error': 'voice_session_failed'}, {'remaining_seconds': 0},
                   {'stop_requested': True}, {'stop_confirmed': True},
                   {'state': 'stopped', 'active': False})
        for update in changes:
            with self.subTest(update=update):
                _, result, _ = self.invoke(['status'], voice=finite(**update))
                self.assertFalse(result['hands_free_listening'])
                _, output, _ = self.invoke(['status'], voice=finite(**update), human=True)
                self.assertNotIn('Wake listener ready', output)
                self.assertNotIn('say the wake phrase', output)
                self.assertNotIn(PRIVATE, output)
        for value in (intent(enabled=False), intent(state='waiting_idle', session_id=None),
                      intent(error='hands_free_start_failed')):
            _, result, _ = self.invoke(['status'], hands_free=value)
            self.assertFalse(result['hands_free_listening'])
        missing_error = finite(); del missing_error['error']
        _, result, _ = self.invoke(['status'], voice=missing_error)
        self.assertFalse(result['hands_free_listening'])

    def test_ineligible_short_profile_remains_readable_and_retry_bound_is_valid(self):
        for value in (intent(enabled=False, state='disabled', session_id=None, session_seconds=10),
                      intent(state='backoff', consecutive_failures=2, retry_after_seconds=10,
                             error='hands_free_start_failed')):
            _, result, _ = self.invoke(['status'], hands_free=value)
            self.assertEqual(result['hands_free'], value)

    def test_state_display_distinguishes_intent_waiting_backoff_and_fault(self):
        idle = finite(state='idle', active=False, session_id=None, remaining_seconds=0)
        for state, enabled, error, expected_code in (
                ('waiting_idle', True, None, 0), ('backoff', True, 'hands_free_start_failed', 1),
                ('faulted', True, 'hands_free_recovery_required', 1), ('disabled', False, None, 0)):
            code, output, _ = self.invoke(['status'], human=True, voice=idle,
                hands_free=intent(state=state, enabled=enabled, error=error, session_id=None))
            self.assertEqual(code, expected_code)
            self.assertIn('Hands-free desired: ' + ('enabled' if enabled else 'disabled'), output)
            self.assertIn('Hands-free state: ' + state, output)
            self.assertNotIn('paused for call', output.lower())
            self.assertNotIn('Wake listener ready', output)

    def test_disable_ack_does_not_claim_cleanup_and_help_preserves_config(self):
        _, output, calls = self.invoke(['disable'], human=True,
            hands_free=intent(enabled=False, state='stopping'))
        self.assertIn('not confirmed stopped yet', output)
        self.assertEqual(len(calls), 1)
        _, output, _ = self.invoke(['status'], human=True)
        help_line = next(line.split(': ', 1)[1] for line in output.splitlines()
                         if line.startswith('Disable automatic listening:'))
        self.assertEqual(shlex.split(help_line), ['cisco', '--config', 'private config/example.json',
                                                '--timeout', '3', '202', 'voice', 'disable'])

    def test_old_gateway_404_is_unavailable_not_disabled_and_no_fallback(self):
        code, result, calls = self.invoke(['status'], failure=cisco.Refused('gateway_http_error', http_status=404))
        self.assertEqual(code, 0)
        self.assertIsNone(result['hands_free'])
        self.assertFalse(result['hands_free_available'])
        self.assertEqual(result['hands_free_error'], 'hands_free_unavailable')
        self.assertEqual(len(calls), 2)
        _, output, _ = self.invoke(['status'], human=True,
                                  failure=cisco.Refused('unknown_action', http_status=404))
        self.assertIn('desired state is unknown', output)
        self.assertNotIn('Hands-free desired: disabled', output)

    def test_secondary_status_failure_preserves_finite_status_for_recovery(self):
        for failure in (cisco.Refused('hands_free_unavailable', http_status=503),
                        cisco.Refused('network_outcome_unknown')):
            code, result, calls = self.invoke(['status'], failure=failure)
            self.assertEqual(code, 1)
            self.assertEqual(result['voice']['session_id'], SESSION)
            self.assertTrue(result['hands_free_status_failed'])
            self.assertFalse(result['hands_free_available'])
            self.assertIsNone(result['hands_free'])
            self.assertEqual(result['hands_free_error'], failure.reason)
            self.assertEqual(len(calls), 2)
            _, output, _ = self.invoke(['status'], failure=failure, human=True)
            self.assertIn('Stop this session:', output)
            self.assertIn(SESSION, output)

    def test_private_raw_error_cannot_become_ready_after_finite_sanitizing(self):
        for failure in (None, cisco.Refused('unknown_action', http_status=404),
                        cisco.Refused('hands_free_unavailable', http_status=503)):
            _, result, _ = self.invoke(['status'], voice=finite(error=PRIVATE), failure=failure,
                hands_free=intent(enabled=False, state='disabled', session_id=None))
            self.assertFalse(result['voice_wake_ready'])
            self.assertNotIn(PRIVATE, json.dumps(result))
            _, output, _ = self.invoke(['status'], voice=finite(error=PRIVATE), failure=failure,
                hands_free=intent(enabled=False, state='disabled', session_id=None), human=True)
            self.assertNotIn('Wake listener ready', output)
            self.assertNotIn('say the wake phrase', output)
            self.assertNotIn(PRIVATE, output)

    def test_malformed_status_and_unknown_errors_are_not_rendered(self):
        cases = {'schema_version': (True, 2), 'enabled': (1, None), 'state': (PRIVATE, []),
                 'phone': ('bad', None), 'line': ('1234', None), 'session_id': ('bad', 123),
                 'session_seconds': (False, 0, 301, 10 ** 1000, float('nan')),
                 'consecutive_failures': (True, -1, 4, 10 ** 1000),
                 'retry_after_seconds': (True, -1, 1, 11, float('inf')),
                 'error': (PRIVATE, {}, False)}
        for key, values in cases.items():
            for invalid in values:
                with self.subTest(field=key, kind=type(invalid).__name__):
                    code, result, _ = self.invoke(['status'], hands_free=intent(**{key: invalid}))
                    self.assertEqual((code, result['reason']), (1, 'invalid_hands_free_response'))
                    self.assertNotIn(PRIVATE, json.dumps(result))
        for key in intent():
            value = intent(); del value[key]
            code, _, _ = self.invoke(['status'], hands_free=value)
            self.assertEqual(code, 1)
        _, result, _ = self.invoke(['status'], hands_free=dict(intent(), transcript=PRIVATE))
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_status_playback_query_stays_only_on_finite_endpoint(self):
        counters = dict(playback_requests=0, tts_offers=0, empty_playbacks=0,
                        incomplete_playbacks=0, completed_pcm_bytes=0)
        code, _, calls = self.invoke(['status', '--playback'], voice=finite(playback=counters))
        self.assertEqual(code, 0)
        self.assertEqual(calls, [('/v1/phones/202/voice_status?include_playback=1', None),
                                ('/v1/phones/202/voice_hands_free_status', None)])


class HandsFreeOutcomeTests(unittest.TestCase):
    def invoke(self, action, *, worker=None, process=None, transport='direct', extra=()):
        out = io.StringIO()
        with mock.patch.object(cisco, 'load_config', return_value=dict(CONFIG, transport=transport)), \
             mock.patch.object(cisco, 'perform', side_effect=worker) as http, \
             mock.patch.object(cisco.subprocess, 'run', side_effect=process) as child, \
             contextlib.redirect_stdout(out):
            code = cisco.main(['--json', '--config', 'private config/example.json', '--timeout', '3',
                               '202', 'voice', action, '--idempotency-key', 'handsfree-example', *extra])
        return code, json.loads(out.getvalue()), http, child

    def assert_unknown(self, result):
        self.assertEqual(result['status'], 'not_confirmed')
        self.assertEqual(result['action_id'], 'handsfree-example')
        self.assertEqual(result['outcome'], 'unknown')
        self.assertEqual(shlex.split(result['recovery_command']),
            ['cisco', '--config', 'private config/example.json', '--timeout', '3', '--json',
             '202', 'voice', 'status'])
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_direct_lost_or_untrusted_response_preserves_key_and_never_retries(self):
        responses = [
            {'ok': False, 'reason': 'network_outcome_unknown'},
            {'ok': True, 'data': {'status': 'accepted', 'action_id': 'wrong', 'hands_free': intent()}},
            {'ok': True, 'data': {'status': 'accepted', 'action_id': 'handsfree-example', 'hands_free': {}}},
            OSError(PRIVATE),
        ]
        for action in ('enable', 'disable'):
            for response in responses:
                with self.subTest(action=action, response=type(response).__name__):
                    code, result, http, child = self.invoke(action, worker=[response])
                    self.assertEqual(code, 1)
                    self.assert_unknown(result)
                    self.assertEqual(http.call_count, 1)
                    self.assertEqual(child.call_count, 0)
                    self.assertEqual(http.call_args.args[0]['body']['idempotency_key'], 'handsfree-example')

    def test_kubectl_discovery_is_definite_but_exec_is_unknown(self):
        pod = {'items': [{'metadata': {'name': 'example-0'}, 'status': {'phase': 'Running',
            'conditions': [{'type': 'Ready', 'status': 'True'}],
            'containerStatuses': [{'name': 'example', 'ready': True}]}}]}
        for action in ('enable', 'disable'):
            for stage in ('discovery', 'exec'):
                process = ([subprocess.CompletedProcess([], 0, json.dumps(pod).encode(), b'')]
                           if stage == 'exec' else [])
                process.append(subprocess.TimeoutExpired('synthetic', 13))
                code, result, http, child = self.invoke(action, transport='kubectl', process=process)
                self.assertEqual(code, 1)
                self.assertEqual(http.call_count, 0)
                if stage == 'exec':
                    self.assert_unknown(result)
                    self.assertEqual(child.call_count, 2)
                else:
                    self.assertEqual(result['status'], 'refused')
                    self.assertNotIn('action_id', result)
                    self.assertEqual(child.call_count, 1)

    def test_dry_run_lost_response_is_not_reported_as_microphone_mutation(self):
        code, result, http, child = self.invoke('enable', extra=('--dry-run',),
            worker=[{'ok': False, 'reason': 'network_outcome_unknown'}])
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'refused')
        self.assertNotIn('action_id', result)
        self.assertNotIn('outcome', result)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(child.call_count, 0)

    def test_old_gateway_enable_has_no_finite_start_fallback(self):
        code, result, http, _ = self.invoke('enable', worker=[{
            'ok': False, 'reason': 'gateway_http_error', 'http_status': 404}])
        self.assertEqual(code, 1)
        self.assert_unknown(result)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(http.call_args.args[0]['path'], '/v1/phones/202/voice_hands_free_enable')

    def test_worker_preserves_fixed_hands_free_errors_closes_once(self):
        for reason in (*cisco.HANDS_FREE_ERRORS, 'hands_free_not_supported', 'hands_free_enabled',
                       'hands_free_start_cancelled', 'hands_free_unavailable', PRIVATE):
            body = io.BytesIO(json.dumps({'reason': reason}).encode())
            failure = error.HTTPError('http://192.168.50.10', 409, 'synthetic', {}, body)
            with mock.patch.object(cisco_http.request, 'build_opener') as opener:
                opener.return_value.open.side_effect = failure
                result = cisco_http.perform({'url': CONFIG['gateway_url'], 'token': CONFIG['token'],
                    'timeout': 3, 'path': '/v1/phones/202/voice_hands_free_enable',
                    'body': {'idempotency_key': 'handsfree-example', 'enable_microphone': True,
                             'exclusive_media': True}})
            self.assertEqual(opener.return_value.open.call_count, 1)
            self.assertTrue(body.closed)
            self.assertEqual(result['http_status'], 409)
            self.assertEqual(result['reason'], reason if reason != PRIVATE else 'gateway_http_error')


if __name__ == '__main__':
    unittest.main()
