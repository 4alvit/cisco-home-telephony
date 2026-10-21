"""Ordinary Cisco CLI voice adapters with synthetic transport responses only."""
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

PRIVATE = 'synthetic-private-token-and-transcript'
SESSION = 'a' * 32


def status_value():
    return {'schema_version': 1, 'phone': '201', 'line': '202', 'session_id': SESSION,
            'state': 'stopped', 'active': False, 'remaining_seconds': 0,
            'stop_requested': True, 'stop_confirmed': True, 'transcript': PRIVATE,
            'error': PRIVATE}


def dry_value():
    return {'schema_version': 1, 'phone': '201', 'line': '202', 'valid': True,
            'network_actions': 0, 'microphone_started': False, 'duration_seconds': 10}


class VoiceCLITests(unittest.TestCase):
    def invoke(self, args, *, value=None, envelope=None, failure=None, prefix=None,
               line='202', human=False, response_values=None):
        calls = []
        def request(path, body=None):
            calls.append((path, copy.deepcopy(body)))
            if failure:
                raise failure
            result = {'status': 'ok' if body is None else 'accepted',
                      'voice': (response_values[len(calls) - 1] if response_values is not None
                                else status_value() if value is None else value)}
            if body is not None:
                result['action_id'] = body['idempotency_key']
            return result if envelope is None else envelope
        output = io.StringIO()
        with mock.patch.object(cisco, 'load_config', return_value={}), \
             mock.patch.object(cisco.Gateway, 'request', side_effect=request), \
             contextlib.redirect_stdout(output):
            options = prefix if prefix is not None else ([] if human else ['--json'])
            code = cisco.main([*options, line, 'voice', *args])
        return code, output.getvalue() if human else json.loads(output.getvalue()), calls

    def start_args(self, *, dry=False):
        return ['start', '--idempotency-key', 'synthetic-request', '--attended',
                '--exclusive-media', '--enable-microphone', *(['--dry-run'] if dry else [])]

    def test_status_uses_alias_without_inventory_and_filters_extra_fields(self):
        code, result, calls = self.invoke(['status'])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [('/v1/phones/202/voice_status', None)])
        self.assertEqual(result['voice']['phone'], '201')
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertNotIn('error', result['voice'])

    def test_playback_metadata_is_optional_bounded_and_printed_without_private_data(self):
        counters = dict(playback_requests=4, tts_offers=3, empty_playbacks=1,
                        incomplete_playbacks=1, completed_pcm_bytes=1280)
        value = dict(status_value(), playback=counters)
        code, result, _ = self.invoke(['status'], value=value)
        self.assertEqual(code, 0)
        self.assertEqual(result['voice']['playback'], counters)
        code, output, _ = self.invoke(['status'], value=value, human=True)
        self.assertEqual(code, 0)
        self.assertIn('PCM bytes in completed playbacks: 1280', output)
        self.assertIn('incomplete playback may be partial', output)
        self.assertNotIn(PRIVATE, output)
        code, result, calls = self.invoke(['status', '--playback'], value=value)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [('/v1/phones/202/voice_status?include_playback=1', None)])
        for key in counters:
            for invalid in (True, -1, 1.5, 10 ** 1000, PRIVATE):
                with self.subTest(key=key, invalid=type(invalid).__name__):
                    code, result, _ = self.invoke(['status'], value=dict(
                        status_value(), playback=dict(counters, **{key: invalid})))
                    self.assertNotEqual(code, 0)
                    self.assertNotIn(PRIVATE, json.dumps(result))
        for invalid in (None, {}, dict(counters, transcript=PRIVATE)):
            code, result, _ = self.invoke(['status'], value=dict(status_value(), playback=invalid))
            self.assertNotEqual(code, 0)
            self.assertNotIn(PRIVATE, json.dumps(result))

    def test_playback_on_old_server_is_unavailable_without_retry_or_invented_zeros(self):
        for failure in (None, cisco.Refused('gateway_http_error', http_status=404)):
            code, result, calls = self.invoke(['status', '--playback'], failure=failure)
            self.assertEqual(code, 1)
            self.assertEqual(result['reason'], 'playback_metadata_unavailable')
            self.assertNotIn('playback', result)
            self.assertEqual(calls, [('/v1/phones/202/voice_status?include_playback=1', None)])

    def test_http_worker_accepts_only_exact_readonly_playback_query(self):
        payload = {'url': 'http://192.0.2.1', 'token': 'x' * 40,
                   'timeout': 2, 'body': None,
                   'path': '/v1/phones/202/voice_status?include_playback=1'}
        # Documentation-only ranges are not accepted as gateway destinations.
        payload['url'] = 'http://192.168.50.1'
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b'{"status":"ok"}'
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(cisco_http.request, 'build_opener', return_value=opener):
            self.assertTrue(cisco_http.perform(payload)['ok'])
            self.assertEqual(opener.open.call_args.args[0].get_method(), 'GET')
            opener.reset_mock()
            for path, body in ((payload['path'], {}),
                    (payload['path'] + '&include_playback=1', None),
                    (payload['path'].replace('=1', '=0'), None),
                    (payload['path'].replace('voice_status', 'voice_start'), None),
                    (payload['path'].replace('voice_status', 'voice_stop'), None)):
                self.assertFalse(cisco_http.perform(dict(payload, path=path, body=body))['ok'])
            opener.open.assert_not_called()

    def test_start_requires_all_explicit_flags_and_request_key_before_http(self):
        full = self.start_args()
        for omitted in ('--attended', '--exclusive-media', '--enable-microphone', '--idempotency-key'):
            args = full.copy()
            index = args.index(omitted)
            del args[index:index + (2 if omitted == '--idempotency-key' else 1)]
            with self.subTest(omitted=omitted):
                code, _result, calls = self.invoke(args)
                self.assertEqual(code, 1)
                self.assertEqual(calls, [])

    def test_start_posts_exact_flags_and_caller_request_key(self):
        code, result, calls = self.invoke(self.start_args())
        self.assertEqual(code, 0)
        self.assertEqual(calls, [('/v1/phones/202/voice_start', {
            'idempotency_key': 'synthetic-request', 'attended': True,
            'exclusive_media': True, 'enable_microphone': True, 'dry_run': False})])
        self.assertEqual(result['action_id'], 'synthetic-request')

    def test_known_public_voice_errors_are_shown_without_transcripts(self):
        for reason in cisco.VOICE_PUBLIC_ERRORS:
            with self.subTest(reason=reason):
                code, result, _calls = self.invoke(
                    ['status'], value=dict(status_value(), state='failed', error=reason))
                self.assertEqual(code, 1)
                self.assertEqual(result['voice']['error'], reason)
                self.assertNotIn(PRIVATE, json.dumps(result))
        for reason in (PRIVATE, {'secret': PRIVATE}):
            code, result, _calls = self.invoke(
                ['status'], value=dict(status_value(), error=reason))
            self.assertEqual(code, 0)
            self.assertNotIn('error', result['voice'])
            self.assertNotIn(PRIVATE, json.dumps(result))

    def test_stop_uses_exact_session_and_stable_default_key_without_inventory(self):
        code, result, calls = self.invoke(['stop', '--session-id', SESSION])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [('/v1/phones/202/voice_stop', {
            'idempotency_key': 'voice-stop-' + SESSION, 'session_id': SESSION})])
        self.assertEqual(result['voice']['session_id'], SESSION)

    def test_stop_dry_run_only_reads_and_checks_exact_session(self):
        args = ['stop', '--session-id', SESSION, '--dry-run']
        code, result, calls = self.invoke(args)
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'dry_run')
        self.assertEqual(calls, [('/v1/phones/202/voice_status', None)])
        code, _result, calls = self.invoke(args, value=dict(status_value(), session_id='b' * 32))
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)

    def test_foreign_owner_status_and_stop_dry_run_explain_unavailable_gateway_stop(self):
        for line in ('201', '202'):
            for state, reason in (('external_owner', PRIVATE),
                                  ('recovery_required', 'voice_owner_unavailable'),
                                  ('stopped', 'voice_owner_unavailable')):
                for operation in ('status', 'stop'):
                    with self.subTest(line=line, state=state, operation=operation):
                        value = dict(status_value(), state=state, error=reason,
                                     stop_confirmed=False, note=PRIVATE,
                                     gateway_stop_available=True, job_name=PRIVATE)
                        args = (['status'] if operation == 'status' else
                                ['stop', '--session-id', SESSION, '--dry-run'])
                        code, result, calls = self.invoke(args, line=line, value=value)
                        self.assertEqual(code, 1)
                        self.assertEqual(calls, [('/v1/phones/' + line + '/voice_status', None)])
                        self.assertEqual(result['status'], 'ok' if operation == 'status' else 'unavailable')
                        self.assertFalse(result['gateway_stop_available'])
                        self.assertEqual(result['reason'], 'voice_owner_unavailable')
                        self.assertIn('gateway cannot stop', result['note'])
                        self.assertIn('live wake screen', result['note'])
                        self.assertIn('exact owning Job with its normal grace period', result['note'])
                        self.assertIn('Confirm cleanup', result['note'])
                        self.assertEqual(result['voice']['session_id'], SESSION)
                        self.assertEqual(result['voice']['phone'], '201')
                        self.assertEqual(result['voice']['line'], '202')
                        self.assertNotIn(PRIVATE, json.dumps(result))
                        self.assertNotIn('action_id', result)
                        if operation == 'stop':
                            self.assertEqual(result['action'], 'voice_stop')
                            self.assertEqual(result['selected_line'], line)
                            self.assertEqual(result['session_id'], SESSION)

    def test_human_output_preserves_foreign_owner_guidance(self):
        for line in ('201', '202'):
            for args in (['status'], ['stop', '--session-id', SESSION, '--dry-run']):
                with self.subTest(line=line, operation=args[0]):
                    value = dict(status_value(), state='recovery_required',
                                 error='voice_owner_unavailable', stop_confirmed=False)
                    code, output, calls = self.invoke(args, line=line, human=True, value=value)
                    self.assertEqual(code, 1)
                    self.assertEqual(calls, [('/v1/phones/' + line + '/voice_status', None)])
                    self.assertIn('Gateway Stop available: False', output)
                    self.assertIn('gateway cannot stop', output)
                    self.assertIn('live wake screen', output)
                    self.assertIn('Confirm cleanup', output)
                    self.assertNotIn(PRIVATE, output)

    def test_foreign_stop_dry_run_rejects_wrong_exact_session_before_guidance(self):
        value = dict(status_value(), state='external_owner', stop_confirmed=False)
        code, result, calls = self.invoke(
            ['stop', '--session-id', 'b' * 32, '--dry-run'], value=value)
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(result['reason'], 'voice_session_changed')
        self.assertNotIn('gateway_stop_available', result)
        self.assertEqual(calls, [('/v1/phones/202/voice_status', None)])

    def test_local_recovery_still_allows_stop_dry_run_and_direct_stop(self):
        for line in ('201', '202'):
            with self.subTest(line=line):
                value = dict(status_value(), state='recovery_required',
                             error='voice_stop_unconfirmed', stop_confirmed=False)
                args = ['stop', '--session-id', SESSION]
                code, result, calls = self.invoke([*args, '--dry-run'], line=line, value=value)
                self.assertEqual(code, 0)
                self.assertEqual(result['status'], 'dry_run')
                self.assertNotIn('gateway_stop_available', result)
                self.assertEqual(calls, [('/v1/phones/' + line + '/voice_status', None)])
                code, result, calls = self.invoke(args, line=line, value=value)
                self.assertEqual(code, 1)  # Unconfirmed cleanup still needs attention.
                self.assertEqual(result['status'], 'accepted')
                self.assertNotIn('gateway_stop_available', result)
                self.assertEqual(calls, [('/v1/phones/' + line + '/voice_stop', {
                    'idempotency_key': 'voice-stop-' + SESSION, 'session_id': SESSION})])

    def test_untrusted_error_and_extra_fields_cannot_invent_foreign_ownership(self):
        value = dict(status_value(), error={'reason': 'voice_owner_unavailable', 'secret': PRIVATE},
                     gateway_stop_available=False, note=PRIVATE, state_hint='external_owner')
        for args in (['status'], ['stop', '--session-id', SESSION, '--dry-run']):
            code, result, calls = self.invoke(args, value=value)
            self.assertEqual(code, 0)
            self.assertNotIn('gateway_stop_available', result)
            self.assertNotIn('note', result)
            self.assertNotIn(PRIVATE, json.dumps(result))
            self.assertEqual(calls, [('/v1/phones/202/voice_status', None)])

    def test_start_dry_run_requires_strict_bounded_validation_response(self):
        code, result, calls = self.invoke(self.start_args(dry=True), value=dry_value())
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'dry_run')
        self.assertTrue(calls[0][1]['dry_run'])
        bad = ({'schema_version': True}, {'valid': 1}, {'network_actions': False},
               {'network_actions': 0.0}, {'microphone_started': 0}, {'duration_seconds': 0},
               {'duration_seconds': 301}, {'duration_seconds': 10 ** 1000},
               {'duration_seconds': float('nan')}, {'line': '201'})
        for changes in bad:
            with self.subTest(field=next(iter(changes))):
                code, _result, calls = self.invoke(self.start_args(dry=True), value=dict(dry_value(), **changes))
                self.assertEqual(code, 1)
                self.assertEqual(len(calls), 1)

    def test_dry_validation_is_rejected_for_status_real_start_and_stop(self):
        for args in (['status'], self.start_args(), ['stop', '--session-id', SESSION]):
            with self.subTest(action=args[0]):
                self.assertEqual(self.invoke(args, value=dry_value())[0], 1)

    def test_mutation_acknowledgement_and_stop_session_must_match(self):
        for args in (self.start_args(), ['stop', '--session-id', SESSION]):
            for envelope in ({'status': 'ok', 'action_id': 'synthetic-request', 'voice': status_value()},
                             {'status': 'accepted', 'action_id': 'wrong-request', 'voice': status_value()}):
                with self.subTest(action=args[0], status=envelope['status']):
                    code, _result, calls = self.invoke(args, envelope=envelope)
                    self.assertEqual(code, 1)
                    self.assertEqual(len(calls), 1)
        self.assertEqual(self.invoke(['stop', '--session-id', SESSION],
                         value=dict(status_value(), session_id='b' * 32))[0], 1)

    def test_status_schema_rejects_coercion_and_never_echoes_private_response(self):
        for changes in ({'schema_version': True}, {'active': 1}, {'line': PRIVATE},
                        {'session_id': PRIVATE}, {'remaining_seconds': float('inf')},
                        {'remaining_seconds': 10 ** 1000}, {'state': []},
                        {'active': True}, {'session_id': None}, {'state': 'idle'}):
            with self.subTest(field=next(iter(changes))):
                code, result, _calls = self.invoke(['status'], value=dict(status_value(), **changes))
                self.assertEqual(code, 1)
                self.assertNotIn(PRIVATE, json.dumps(result))

    def test_idle_status_requires_no_session_and_running_status_requires_active(self):
        idle = dict(status_value(), state='idle', session_id=None, stop_requested=False,
                    stop_confirmed=False)
        self.assertEqual(self.invoke(['status'], value=idle)[0], 0)
        running = dict(status_value(), state='running', active=True, stop_confirmed=False)
        self.assertEqual(self.invoke(['status'], value=running)[0], 0)
        self.assertEqual(self.invoke(['status'], value=dict(running, active=False))[0], 1)
        # Status may be requested through either assigned line; the session
        # remains owned by the same physical phone and may name its sibling.
        sibling = dict(status_value(), line='201')
        self.assertEqual(self.invoke(['status'], value=sibling)[0], 0)

    def test_recovery_state_returns_failure_without_retry(self):
        for state in ('failed', 'recovery_required', 'external_owner'):
            code, _result, calls = self.invoke(['status'], value=dict(status_value(), state=state))
            self.assertEqual(code, 1)
            self.assertEqual(len(calls), 1)
        code, result, calls = self.invoke(self.start_args(), failure=cisco.Refused('network_outcome_unknown'))
        self.assertEqual(code, 1)
        self.assertEqual(result['reason'], 'network_outcome_unknown')
        self.assertEqual(len(calls), 1)

    def test_global_options_are_accepted_on_nested_voice_parser(self):
        code, _result, calls = self.invoke(['status', '--json', '--config', 'synthetic', '--timeout', '3'], prefix=[])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)

    def test_dynamic_commands_only_advertise_configured_voice_capability(self):
        self.assertNotIn('voice status', cisco.commands({'capabilities': []}))
        self.assertIn('voice status', cisco.commands({'capabilities': ['voice']}))

    def test_optional_phase_fields_and_five_minute_bounds_preserve_old_status(self):
        for phase in (*sorted(cisco.VOICE_PHASES), None):
            value = dict(status_value(), state='running', active=True, stop_requested=False,
                         stop_confirmed=False, remaining_seconds=300, phase=phase,
                         phase_reason='wake_ready', turns_completed=10000,
                         mode='wake', session_seconds=300)
            code, result, calls = self.invoke(['status'], value=value)
            self.assertEqual(code, 0)
            for field in ('phase', 'phase_reason', 'turns_completed', 'mode', 'session_seconds'):
                self.assertEqual(result['voice'][field], value[field])
            self.assertEqual(len(calls), 1)
        code, result, _ = self.invoke(self.start_args(dry=True),
                                      value=dict(dry_value(), duration_seconds=300))
        self.assertEqual((code, result['status']), (0, 'dry_run'))

    def test_optional_phase_fields_reject_malformed_values_and_filter_reason(self):
        for field, values in {
            'phase': (PRIVATE, [], False), 'mode': (None, 'onnx', False),
            'turns_completed': (None, False, -1, 1.0, 10001, 10 ** 1000),
            'session_seconds': (None, False, 0, 301, float('nan'), 10 ** 1000),
            'remaining_seconds': (301,),
        }.items():
            for value in values:
                with self.subTest(field=field, kind=type(value).__name__):
                    code, result, _ = self.invoke(['status'], value=dict(status_value(), **{field: value}))
                    self.assertEqual((code, result['reason']), (1, 'invalid_voice_response'))
                    self.assertNotIn(PRIVATE, json.dumps(result))
        for reason in (PRIVATE, {'private': PRIVATE}, [], False, None):
            code, result, _ = self.invoke(['status'], value=dict(status_value(), phase_reason=reason))
            self.assertEqual(code, 0)
            self.assertIsNone(result['voice']['phase_reason'])
            self.assertNotIn(PRIVATE, json.dumps(result))
        for reason in ('audio_not_received', 'turn_cancelled', 'duplicate_activation',
                       'call', 'start_failed', 'assistant_error'):
            code, result, _ = self.invoke(['status'], value=dict(status_value(), phase_reason=reason))
            self.assertEqual(code, 0)
            self.assertEqual(result['voice']['phase_reason'], reason)

    def test_human_phase_guidance_and_exact_stop_command_preserve_config(self):
        value = dict(status_value(), state='running', active=True, stop_requested=False,
                     stop_confirmed=False, phase='listening', phase_reason='speech_start',
                     turns_completed=2, mode='wake', session_seconds=180, remaining_seconds=90)
        prefix = ['--config', 'private config/voice.json', '--timeout', '7']
        code, output, calls = self.invoke(['status'], prefix=prefix, human=True, value=value)
        self.assertEqual(code, 0)
        self.assertIn('Listening; speak now.', output)
        self.assertIn('Turns completed: 2', output)
        self.assertIn('Phone: 201; session line: 202', output)
        stop = next(line.removeprefix('Stop this session: ') for line in output.splitlines()
                    if line.startswith('Stop this session: '))
        self.assertEqual(shlex.split(stop), ['cisco', *prefix, '202', 'voice', 'stop', '--session-id', SESSION])
        self.assertEqual(len(calls), 1)
        for update in ({'stop_requested': True}, {'state': 'stopped', 'active': False, 'stop_confirmed': True}):
            _, output, _ = self.invoke(['status'], human=True, value=dict(value, **update))
            self.assertNotIn('speak now', output)
        _, output, _ = self.invoke(['status'], human=True,
                                  value=dict(value, phase=None, phase_reason=None))
        self.assertIn('Readiness not reported', output)

    def test_stop_without_id_reads_once_then_posts_exact_captured_session(self):
        for dry in (False, True):
            with self.subTest(dry_run=dry):
                code, result, calls = self.invoke(['stop', *(['--dry-run'] if dry else [])])
                self.assertEqual(code, 0)
                self.assertEqual(calls[0], ('/v1/phones/202/voice_status', None))
                self.assertEqual(len(calls), 1 if dry else 2)
                if not dry:
                    self.assertEqual(calls[1], ('/v1/phones/202/voice_stop', {
                        'session_id': SESSION, 'idempotency_key': 'voice-stop-' + SESSION}))
                else:
                    self.assertEqual(result['session_id'], SESSION)

    def test_stop_without_id_never_mutates_idle_or_external_owner(self):
        for state, session in (('idle', None), ('external_owner', SESSION)):
            value = dict(status_value(), state=state, session_id=session, stop_confirmed=False)
            code, result, calls = self.invoke(['stop'], value=value)
            self.assertEqual(code, 1)
            self.assertEqual(calls, [('/v1/phones/202/voice_status', None)])
            self.assertNotIn('action_id', result)
        self.assertFalse(result['gateway_stop_available'])

    def test_stop_without_id_never_rebinds_after_session_race(self):
        code, result, calls = self.invoke(['stop'], response_values=[
            status_value(), dict(status_value(), session_id='b' * 32)])
        self.assertEqual((code, result['reason']), (1, 'voice_session_changed'))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1]['session_id'], SESSION)
        self.assertEqual(calls[1][1]['idempotency_key'], 'voice-stop-' + SESSION)


class VoiceTransportReasonTests(unittest.TestCase):
    def test_known_voice_http_refusals_are_preserved_and_unknown_text_is_suppressed(self):
        for reason in ('voice_recovery_required', 'invalid_voice_ffmpeg', PRIVATE):
            body = io.BytesIO(json.dumps({'reason': reason}).encode())
            failure = error.HTTPError('http://192.168.50.10', 409, 'synthetic', {}, body)
            with mock.patch.object(cisco_http.request, 'build_opener') as opener:
                opener.return_value.open.side_effect = failure
                result = cisco_http.perform({'url': 'http://192.168.50.10:18084',
                    'token': 'a' * 48, 'path': '/v1/phones/202/voice_stop',
                    'body': {'session_id': SESSION}, 'timeout': 3})
            self.assertEqual(result['reason'], reason if reason != PRIVATE else 'gateway_http_error')
            self.assertNotIn(PRIVATE, json.dumps(result))


class VoiceOutcomeTests(unittest.TestCase):
    """Exercise the real dispatch boundary without contacting a phone."""

    def invoke(self, args, *, transport='direct', worker=None, process=None, config_error=None):
        config = {'transport': transport, 'gateway_url': 'http://192.168.50.10:18084',
                  'token': 'a' * 48, 'kubectl': {'context': 'example', 'namespace': 'example',
                  'selector': 'app=example', 'container': 'example'}}
        output = io.StringIO()
        with mock.patch.object(cisco, 'load_config', return_value=config,
                               side_effect=config_error), \
             mock.patch.object(cisco, 'perform', side_effect=worker) as http, \
             mock.patch.object(cisco.subprocess, 'run', side_effect=process) as child, \
             contextlib.redirect_stdout(output):
            code = cisco.main(['--json', '--config', 'private config/example.json',
                               '--timeout', '3', '202', 'voice', *args])
        return code, json.loads(output.getvalue()), http, child

    def mutations(self):
        return [
            (['start', '--idempotency-key', 'synthetic-request', '--attended',
              '--exclusive-media', '--enable-microphone'], 'synthetic-request'),
            (['stop', '--session-id', SESSION], 'voice-stop-' + SESSION),
        ]

    def pod_result(self):
        value = {'items': [{'metadata': {'name': 'example-0'}, 'status': {
            'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}],
            'containerStatuses': [{'name': 'example', 'ready': True}]}}]}
        return subprocess.CompletedProcess([], 0, json.dumps(value).encode(), b'')

    def assert_uncertain(self, result, key):
        self.assertEqual(result['status'], 'not_confirmed')
        self.assertEqual(result['action_id'], key)
        self.assertEqual(result['outcome'], 'unknown')
        self.assertIn('Do not retry', result['note'])
        self.assertEqual(shlex.split(result['recovery_command']),
                         ['cisco', '--config', 'private config/example.json',
                          '--timeout', '3', '--json', '202', 'voice', 'status'])
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertNotIn('a' * 48, json.dumps(result))

    def test_direct_lost_reply_preserves_start_and_stop_keys_without_retry(self):
        for args, key in self.mutations():
            with self.subTest(action=args[0]):
                code, result, http, child = self.invoke(
                    args, worker=[{'ok': False, 'reason': 'network_outcome_unknown'}])
                self.assertEqual(code, 1)
                self.assert_uncertain(result, key)
                self.assertEqual(http.call_count, 1)
                self.assertEqual(child.call_count, 0)
                self.assertEqual(http.call_args.args[0]['body']['idempotency_key'], key)

    def test_untrusted_ack_and_local_response_errors_remain_uncertain(self):
        replies = [
            {'ok': True, 'data': {'status': 'accepted', 'action_id': 'wrong-request',
                                 'voice': status_value()}},
            {'ok': True, 'data': {'status': 'accepted', 'action_id': 'synthetic-request',
                                 'voice': {'transcript': PRIVATE}}},
            OSError(PRIVATE),
        ]
        args, key = self.mutations()[0]
        for reply in replies:
            with self.subTest(reply_type=type(reply).__name__):
                code, result, http, _child = self.invoke(args, worker=[reply])
                self.assertEqual(code, 1)
                self.assert_uncertain(result, key)
                self.assertEqual(http.call_count, 1)

    def test_kubectl_exec_timeout_keeps_key_but_discovery_timeout_is_refused(self):
        for args, key in self.mutations():
            with self.subTest(action=args[0], stage='exec'):
                code, result, http, child = self.invoke(args, transport='kubectl', process=[
                    self.pod_result(), subprocess.TimeoutExpired('synthetic', 13)])
                self.assertEqual(code, 1)
                self.assert_uncertain(result, key)
                self.assertEqual(http.call_count, 0)
                self.assertEqual(child.call_count, 2)
                call = child.call_args
                self.assertIn('exec', call.args[0])
                self.assertNotIn('a' * 48, ' '.join(call.args[0]))
                self.assertEqual(json.loads(call.kwargs['input'])['body']['idempotency_key'], key)
            with self.subTest(action=args[0], stage='discovery'):
                code, result, _http, child = self.invoke(args, transport='kubectl',
                    process=[subprocess.TimeoutExpired('synthetic', 13)])
                self.assertEqual(code, 1)
                self.assertEqual(result['status'], 'refused')
                self.assertNotIn('action_id', result)
                self.assertNotIn('recovery_command', result)
                self.assertEqual(child.call_count, 1)
                self.assertNotIn('exec', child.call_args.args[0])

    def test_read_only_and_dry_run_errors_are_not_reported_as_physical_dispatch(self):
        start, _key = self.mutations()[0]
        for args in (['status'], [*start, '--dry-run'],
                     ['stop', '--session-id', SESSION, '--dry-run'], ['stop'], ['stop', '--dry-run']):
            with self.subTest(args=args):
                code, result, http, _child = self.invoke(
                    args, worker=[{'ok': False, 'reason': 'network_outcome_unknown'}])
                self.assertEqual(code, 1)
                self.assertEqual(result['status'], 'refused')
                self.assertNotIn('action_id', result)
                self.assertNotIn('outcome', result)
                self.assertEqual(http.call_count, 1)

    def test_validation_and_config_errors_do_not_dispatch(self):
        start, _key = self.mutations()[0]
        for args, error in ((start[:-1], None),
                            (start, cisco.Refused('invalid_private_config'))):
            code, result, http, child = self.invoke(args, config_error=error)
            self.assertEqual(code, 1)
            self.assertEqual(result['status'], 'refused')
            self.assertNotIn('action_id', result)
            self.assertEqual(http.call_count, 0)
            self.assertEqual(child.call_count, 0)

    def test_current_session_stop_lost_reply_keeps_captured_key_without_retry(self):
        code, result, http, child = self.invoke(['stop'], worker=[
            {'ok': True, 'data': {'status': 'ok', 'voice': status_value()}},
            {'ok': False, 'reason': 'network_outcome_unknown'}])
        self.assertEqual(code, 1)
        self.assert_uncertain(result, 'voice-stop-' + SESSION)
        self.assertEqual((http.call_count, child.call_count), (2, 0))
        self.assertEqual(http.call_args_list[0].args[0]['body'], None)
        self.assertEqual(http.call_args_list[1].args[0]['body']['session_id'], SESSION)


if __name__ == '__main__':
    unittest.main()
