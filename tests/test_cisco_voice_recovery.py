"""Administrative CLI recovery uses exact identity, private transport and strict receipts."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cisco

SID, SHA = 'a' * 32, 'b' * 64


def status():
    return dict(schema_version=1, phone='201', line='202', session_id=SID,
                journal_sha256=SHA, eligible=True, reason=None,
                quiescence_verified=False, minimum_age_remaining_seconds=0.0)


def receipt():
    return dict(schema_version=1, phone='201', line='202', session_id=SID,
                journal_sha256='c' * 64, prior_journal_sha256=SHA,
                status='ORPHAN_RECOVERY_COMPLETE', state='recovered', enabled=False,
                historical_stop_confirmed=False, stop_confirmed=False,
                proof_kind='phone_reported_transport_quiescence')


class RecoveryCLITests(unittest.TestCase):
    def invoke(self, arguments, value, *, failure=None):
        calls = []
        def request(client, path, body=None):
            calls.append((path, copy.deepcopy(body), client.timeout))
            if body is not None and client.on_dispatch is not None:
                client.on_dispatch()
            if failure:
                raise failure
            return dict(status='accepted' if body else 'ok', recovery=value,
                        **({'action_id': body['idempotency_key']} if body else {}))
        output = io.StringIO()
        with mock.patch.object(cisco, 'load_config', return_value={}), \
             mock.patch.object(cisco.Gateway, 'request', request), contextlib.redirect_stdout(output):
            code = cisco.main(['--json', '202', 'voice', *arguments])
        return code, json.loads(output.getvalue()), calls

    def arguments(self):
        return ['recover', '--session-id', SID, '--journal-sha256', SHA,
                '--idempotency-key', 'synthetic-recovery']

    def test_status_keeps_quiet_unknown_and_filters_private_extensions(self):
        code, result, calls = self.invoke(['recovery-status'], dict(status(), transcript='PRIVATE'))
        self.assertEqual(code, 0)
        self.assertEqual(calls, [('/v1/phones/202/voice_recovery_status', None, 20)])
        self.assertIs(result['recovery']['quiescence_verified'], False)
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_exact_recover_preserves_failure_history_and_extends_timeout(self):
        code, result, calls = self.invoke(self.arguments(), dict(receipt(), archive='/PRIVATE/PATH'))
        self.assertEqual(code, 0)
        self.assertEqual(calls, [('/v1/phones/202/voice_recover', dict(
            idempotency_key='synthetic-recovery', session_id=SID, journal_sha256=SHA), 45)])
        self.assertIs(result['recovery']['stop_confirmed'], False)
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_dry_run_never_submits_recovery(self):
        code, result, calls = self.invoke([*self.arguments(), '--dry-run'], receipt())
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertEqual(result['reason'], 'voice_recover_dry_run_unsupported')

    def test_lost_receipt_points_to_read_only_recovery_status(self):
        code, result, calls = self.invoke(self.arguments(), receipt(), failure=OSError('PRIVATE'))
        self.assertEqual(code, 1)
        self.assertEqual(result['outcome'], 'unknown')
        self.assertIn('202 voice recovery-status', result['recovery_command'])
        self.assertEqual(len(calls), 1)
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_mismatched_or_fabricated_success_receipt_is_rejected(self):
        for field, value in (('session_id', 'd' * 32), ('prior_journal_sha256', 'd' * 64),
                             ('stop_confirmed', True), ('enabled', True),
                             ('proof_kind', 'microphone_powered_off'), ('line', '201')):
            with self.subTest(field=field):
                code, result, _ = self.invoke(self.arguments(), dict(receipt(), **{field: value}))
                self.assertEqual(code, 1)
                self.assertEqual(result['reason'], 'invalid_voice_recovery_response')

    def test_status_rejects_unbounded_or_unknown_values(self):
        for field, value in (('minimum_age_remaining_seconds', float('inf')),
                             ('minimum_age_remaining_seconds', True),
                             ('quiescence_verified', True), ('reason', 'PRIVATE')):
            with self.subTest(field=field):
                code, result, _ = self.invoke(['recovery-status'], dict(status(), **{field: value}))
                self.assertEqual(code, 1)
                self.assertNotIn('PRIVATE', json.dumps(result))
