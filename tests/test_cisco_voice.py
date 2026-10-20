"""Ordinary Cisco CLI voice adapters with synthetic transport responses only."""
import contextlib
import copy
import io
import json
from pathlib import Path
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
    def invoke(self, args, *, value=None, envelope=None, failure=None, prefix=None):
        calls = []
        def request(path, body=None):
            calls.append((path, copy.deepcopy(body)))
            if failure:
                raise failure
            result = {'status': 'ok' if body is None else 'accepted',
                      'voice': status_value() if value is None else value}
            if body is not None:
                result['action_id'] = body['idempotency_key']
            return result if envelope is None else envelope
        output = io.StringIO()
        with mock.patch.object(cisco, 'load_config', return_value={}), \
             mock.patch.object(cisco.Gateway, 'request', side_effect=request), \
             contextlib.redirect_stdout(output):
            code = cisco.main([*(prefix or ['--json']), '202', 'voice', *args])
        return code, json.loads(output.getvalue()), calls

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

    def test_start_dry_run_requires_strict_bounded_validation_response(self):
        code, result, calls = self.invoke(self.start_args(dry=True), value=dry_value())
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'dry_run')
        self.assertTrue(calls[0][1]['dry_run'])
        bad = ({'schema_version': True}, {'valid': 1}, {'network_actions': False},
               {'network_actions': 0.0}, {'microphone_started': 0}, {'duration_seconds': 0},
               {'duration_seconds': 31}, {'duration_seconds': float('nan')}, {'line': '201'})
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
                        {'session_id': PRIVATE}, {'remaining_seconds': float('inf')}):
            with self.subTest(field=next(iter(changes))):
                code, result, _calls = self.invoke(['status'], value=dict(status_value(), **changes))
                self.assertEqual(code, 1)
                self.assertNotIn(PRIVATE, json.dumps(result))

    def test_recovery_state_returns_failure_without_retry(self):
        code, _result, calls = self.invoke(['status'], value=dict(status_value(), state='recovery_required'))
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


if __name__ == '__main__':
    unittest.main()
