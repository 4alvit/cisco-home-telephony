"""Private terminal diagnostics never substitute for exact cleanup evidence."""
import errno
import json
import os
import stat
import unittest
from unittest import mock

from tests.test_phone_voice_control import ControlFixture, PRIVATE_MARKER, control


def diagnostic():
    return {'version': 1, 'reason': 'ami_action_rejected', 'error_type': 'Refused',
            'stage': 'sip_start', 'ami_stage': 'originate', 'ami_action': 'Originate',
            'ami_error': 'permission_denied'}


def no_originate(session):
    return {
        'transport': 'sip_audiosocket', 'session_id': session.session_id,
        'recorded_audio': False, 'error': 'voice_session_failed', 'result': None,
        'cleanup_errors': [], 'turns_completed': 0, 'originate_attempted': False,
        'speaker_attempted': False, 'pbx_deadline_armed': False, 'pbx_call_absent': True,
        'safe_no_media_started': True, 'input_frames': 0, 'output_frames': 0,
        'cleanup_proof': {'version': 1, 'kind': 'safe_no_sip_originate',
                          'transport': 'sip_audiosocket', 'session_id': session.session_id,
                          'challenge': session.launch_challenge,
                          'originate_attempted': False, 'cleanup_complete': True},
    }


class StartupDiagnosticSchemaTests(unittest.TestCase):
    def test_only_exact_typed_fixed_vocabulary_is_accepted(self):
        self.assertTrue(control.valid_startup_diagnostic(diagnostic()))
        for key in diagnostic():
            for value in (None, True, [], {}, PRIVATE_MARKER):
                with self.subTest(key=key, value=value):
                    changed = diagnostic()
                    changed[key] = value
                    self.assertFalse(control.valid_startup_diagnostic(changed))
            missing = diagnostic()
            del missing[key]
            self.assertFalse(control.valid_startup_diagnostic(missing))
        self.assertFalse(control.valid_startup_diagnostic(dict(diagnostic(), extra='other')))
        self.assertFalse(control.valid_startup_diagnostic(None))
        changed = diagnostic()
        changed.update(stage='runner_unhandled', ami_stage='other')
        self.assertTrue(control.valid_startup_diagnostic(changed))


class StartupReceiptTests(ControlFixture):
    def setUp(self):
        super().setUp()
        self.transport['media_transport'] = 'sip_audiosocket'
        self.voice['wake']['mode'] = 'onnx'
        self.write_inputs()

    def prepared(self, index=0):
        self.configuration['state_directory'] = '.local/diagnostics-' + str(index)
        self.write_inputs()
        manager = self.make_control()
        state = self.start(manager)
        session = manager._sessions[state['session_id']]
        return manager, session, self.runners[-1]

    def path(self, manager, session):
        return manager._paths(session.profile)[1].with_suffix('.last-failure.json')

    def receipt(self, manager, session):
        path = self.path(manager, session)
        data = path.read_bytes()
        self.assertLessEqual(len(data), control.MAX_FAILURE_RECEIPT)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(path.stat().st_uid, os.getuid())
        self.assertNotIn(PRIVATE_MARKER, data.decode())
        self.assertNotIn(session.launch_challenge, data.decode())
        self.assertNotIn(str(self.workspace), data.decode())
        return json.loads(data)

    def test_early_failure_retains_safe_report_after_handle_is_discarded(self):
        manager, session, child = self.prepared()
        report = no_originate(session)
        report.pop('cleanup_proof')
        report.update(originate_attempted=True, safe_no_media_started=False,
                      pbx_call_absent=False, startup_diagnostic=diagnostic(),
                      token=PRIVATE_MARKER, transcript=PRIVATE_MARKER,
                      cleanup_errors=['pbx_cleanup_failed', PRIVATE_MARKER])
        child.finish(report, code=1)
        manager.tick()
        receipt = self.receipt(manager, session)
        self.assertEqual(receipt['report_status'], 'matched')
        self.assertEqual(receipt['returncode'], 1)
        self.assertFalse(receipt['cleanup_evidence_verified'])
        self.assertEqual(receipt['child_claims']['startup_diagnostic'], diagnostic())
        self.assertEqual(receipt['child_claims']['cleanup_errors'], ['other', 'pbx_cleanup_failed'])
        self.assertTrue(receipt['child_claims']['originate_attempted'])
        self.assertIsNone(session.handle)
        self.assertFalse(session.cleanup_verified)
        self.assertTrue(manager._journal(session.profile)['reserved'])
        self.assertEqual(session.state, 'recovery_required')
        self.assertNotIn('child_claims', self.status(manager))

    def test_valid_optional_diagnostic_preserves_no_originate_cleanup(self):
        for index, include in enumerate((False, True)):
            with self.subTest(include=include):
                manager, session, child = self.prepared(index)
                report = no_originate(session)
                if include:
                    report['startup_diagnostic'] = diagnostic()
                old_directory = session.directory
                child.finish(report, code=1)
                manager.tick()
                receipt = self.receipt(manager, session)
                self.assertTrue(receipt['cleanup_evidence_verified'])
                self.assertEqual(session.state, 'failed')
                self.assertTrue(session.cleanup_verified)
                self.assertFalse(manager._journal(session.profile)['reserved'])
                self.assertIsNone(session.directory)
                self.assertFalse(old_directory.exists())
                self.assertFalse(self.status(manager)['stop_confirmed'])
                self.assertEqual(receipt['child_claims']['startup_diagnostic_status'],
                                 'valid' if include else 'absent')

    def test_diagnostic_only_focus_and_cleanup_reasons_preserve_failure_authority(self):
        for index, reason in enumerate(('unknown', 'disconnected')):
            with self.subTest(reason=reason):
                manager, session, child = self.prepared(index)
                report = no_originate(session)
                report.pop('cleanup_proof')
                report.update(originate_attempted=True, safe_no_media_started=False,
                              pbx_deadline_armed=True,
                              result={'reason': reason, 'stop_confirmed': True,
                                      'cleanup_errors': ['wire_close_failed', 'assistant_stop_failed']})
                child.finish(report, code=1)
                manager.tick()
                receipt = self.receipt(manager, session)
                self.assertEqual(receipt['child_claims']['result_reason'], reason)
                self.assertEqual(receipt['child_claims']['result_cleanup_errors'],
                                 ['assistant_stop_failed', 'wire_close_failed'])
                self.assertNotIn(reason, control.SIP_COMPLETION_REASONS)
                self.assertEqual(session.state, 'recovery_required')
                self.assertFalse(session.cleanup_verified)

    def test_malformed_optional_diagnostic_cannot_downgrade_to_no_originate_proof(self):
        for index, change in enumerate(({'reason': PRIVATE_MARKER}, {'version': True},
                                        {'extra': 'other'}, {'ami_action': []})):
            with self.subTest(change=change):
                manager, session, child = self.prepared(index)
                report = no_originate(session)
                report['startup_diagnostic'] = dict(diagnostic(), **change)
                child.finish(report, code=1)
                manager.tick()
                receipt = self.receipt(manager, session)
                self.assertEqual(session.state, 'recovery_required')
                self.assertFalse(session.cleanup_verified)
                self.assertTrue(manager._journal(session.profile)['reserved'])
                self.assertEqual(receipt['child_claims']['startup_diagnostic_status'], 'invalid')
                self.assertNotIn('startup_diagnostic', receipt['child_claims'])

    def test_untrusted_or_unavailable_reports_keep_only_manager_metadata(self):
        cases = ('wrong_identity', 'malformed', 'duplicate_keys', 'overflow',
                 'abnormal_exit', 'missing')
        expected = ('identity_mismatch', 'invalid_json', 'invalid_json', 'overflow',
                    'child_unverified', 'unavailable')
        for index, (case, status) in enumerate(zip(cases, expected)):
            with self.subTest(case=case):
                manager, session, child = self.prepared(index)
                report = no_originate(session)
                if case == 'wrong_identity':
                    report['session_id'] = '0' * 32
                child.finish(report, code=3 if case == 'abnormal_exit' else 1)
                if case == 'malformed':
                    child.result = PRIVATE_MARKER.encode()
                elif case == 'duplicate_keys':
                    child.result = b'{"token":1,"token":2}'
                elif case == 'overflow':
                    child.result = PRIVATE_MARKER.encode() * control.MAX_REPORT
                    child.overflow = True
                elif case == 'missing':
                    child.result = None
                manager.tick()
                if case == 'missing':
                    self.clock.advance(control.REPORT_GRACE_SECONDS)
                    manager.tick()
                receipt = self.receipt(manager, session)
                self.assertEqual(receipt['report_status'], status)
                self.assertNotIn('child_claims', receipt)
                self.assertEqual(session.state, 'recovery_required')
                self.assertTrue(manager._journal(session.profile)['reserved'])

    def test_failed_receipt_write_does_not_change_cleanup_or_journal(self):
        for index, confirmed in enumerate((False, True)):
            with self.subTest(confirmed=confirmed):
                manager, session, child = self.prepared(index)
                report = no_originate(session)
                if not confirmed:
                    report.pop('cleanup_proof')
                    report.update(originate_attempted=True, safe_no_media_started=False,
                                  pbx_call_absent=False)
                child.finish(report, code=1)
                original = os.replace

                def replace(source, target):
                    if str(target).endswith('.last-failure.json'):
                        raise OSError(errno.ENOSPC, PRIVATE_MARKER)
                    return original(source, target)

                with mock.patch.object(control.os, 'replace', side_effect=replace):
                    manager.tick()
                self.assertFalse(self.path(manager, session).exists())
                self.assertEqual(session.cleanup_verified, confirmed)
                self.assertEqual(manager._journal(session.profile)['reserved'], not confirmed)
                self.assertIsNone(session.handle)
                self.assertEqual(list(manager.config.state_directory.glob('.voice-failure-*')), [])

    def test_receipt_destination_symlink_never_changes_external_target(self):
        manager, session, child = self.prepared()
        external = self.local / 'unrelated.txt'
        external.write_text(PRIVATE_MARKER)
        self.path(manager, session).symlink_to(external)
        child.finish(no_originate(session), code=1)
        manager.tick()
        self.assertEqual(external.read_text(), PRIVATE_MARKER)
        self.assertFalse(self.path(manager, session).is_symlink())
        self.receipt(manager, session)
        self.assertTrue(session.cleanup_verified)

    def test_normal_stop_does_not_create_a_failure_receipt(self):
        manager, session, child = self.prepared()
        report = no_originate(session)
        report.pop('cleanup_proof')
        report.update(originate_attempted=True, safe_no_media_started=False,
                      pbx_deadline_armed=True, error=None,
                      result={'reason': 'explicit_stop', 'stop_confirmed': True,
                              'cleanup_errors': []})
        child.finish(report)
        manager.tick()
        self.assertEqual(session.state, 'stopped')
        self.assertTrue(session.cleanup_verified)
        self.assertFalse(self.path(manager, session).exists())

    def test_successive_failures_replace_one_receipt_per_physical_phone(self):
        manager, session, child = self.prepared()
        child.finish(no_originate(session), code=1)
        manager.tick()
        first = self.receipt(manager, session)
        state = self.start(manager, target='202', request_id='second-failed-attempt')
        second = manager._sessions[state['session_id']]
        self.runners[-1].finish(no_originate(second), code=1)
        manager.tick()
        receipt = self.receipt(manager, second)
        self.assertNotEqual(first['session_id'], receipt['session_id'])
        self.assertEqual(receipt['session_id'], second.session_id)
        self.assertEqual(self.path(manager, session), self.path(manager, second))
        self.assertEqual(len(list(manager.config.state_directory.glob('*.last-failure.json'))), 1)

    def test_changed_owner_cannot_overwrite_existing_receipt(self):
        manager, session, child = self.prepared()
        receipt_path = self.path(manager, session)
        receipt_path.write_bytes(b'original')
        receipt_path.chmod(0o600)
        original = manager._current[session.profile.phone_id]
        manager._current[session.profile.phone_id] = object()
        child.finish(no_originate(session), code=1)
        try:
            with self.assertRaises(control.ControlError):
                manager.tick()
            self.assertEqual(receipt_path.read_bytes(), b'original')
            self.assertIsNotNone(session.handle)
        finally:
            manager._current[session.profile.phone_id] = original


if __name__ == '__main__':
    unittest.main()
