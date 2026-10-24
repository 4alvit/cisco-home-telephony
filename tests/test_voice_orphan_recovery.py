"""Adversarial orphan-recovery checks; synthetic disk/clock/proofs, no phone I/O."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'containers/asterisk' / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


control = load_module('orphan_control_test', 'phone_voice_control.py')
hands_free = load_module('orphan_hands_free_test', 'phone_voice_hands_free.py')
AUTH = object()
SID = 'a' * 32
PRIVATE = 'synthetic-secret-not-for-public-output'


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class LiveRunner:
    def poll(self):
        return None

    def terminate(self):
        raise AssertionError('recovery must not signal a live owner')

    def kill(self):
        raise AssertionError('recovery must not kill a live owner')


class VoiceOrphanRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='voice-orphan-test-')
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.state = self.workspace / '.local' / 'state'
        self.clock = Clock()
        self.profile = control.Profile(
            '201', ('201', '202'), 'SEP020000000001', '192.0.2.20',
            self.workspace / '.local/transport.json',
            self.workspace / '.local/voice.json', 60, '192.0.2.10', 24576,
            mode='wake', rearm_after_turn=True, media_transport='sip_audiosocket')
        self.config = control.ControlConfig(self.workspace, self.state, sys.executable,
                                             sys.executable, (self.profile,), True)
        self.manager = control.VoiceControl(
            self.config, authorize=lambda context, action, phone: context is AUTH,
            runner_factory=self.unexpected_launch, clock=self.clock, monitor=False)
        self.manager._mkdir()
        self.lock, self.finite = self.manager._paths(self.profile)
        self.lock.touch(mode=0o600)
        self.key = self.lock.stem
        self.hf_lock = self.state / (self.key + '.hands-free.lock')
        self.hf_lock.touch(mode=0o600)
        self.hf_path = self.state / (self.key + '.hands-free.json')
        old = control._Session(self.profile, SID, 'original-request', '201', -1,
            state='recovery_required', stop_requested=True, stop_confirmed=False,
            error='voice_stop_unconfirmed', phase='error', phase_reason='voice_session_failed',
            turns_completed=2)
        finite = self.manager._view(old)
        finite.update(request_id=old.request_id, reserved=True)
        self.write_json(self.finite, finite)
        history = [{'id': hashlib.sha256(b'old-enable').hexdigest(), 'action': 'enable'},
                   {'id': hashlib.sha256(b'old-disable').hexdigest(), 'action': 'disable'}]
        seen = bytearray(hands_free.BLOOM_BYTES)
        for event in history:
            for n in hands_free.HandsFreeSupervisor._positions(event['id']):
                seen[n // 8] |= 1 << (n % 8)
        self.write_json(self.hf_path, {
            'version': 1, 'phone': '201', 'line': '201', 'enabled': False,
            'generation': 'b' * 32, 'failures': 2, 'error': 'hands_free_recovery_required',
            'pending': True, 'control_pending': None, 'retry_pending': False,
            'history': history, 'seen': seen.hex()})
        self.supervisor = hands_free.HandsFreeSupervisor(
            self.manager, lambda phone: self.fail('recovery must not start an idle monitor read'),
            lambda phone: AUTH, monitor=False)
        self.proof_calls = []
        old_wall = time.time() - control.EXTERNAL_OWNER_GRACE_SECONDS - 60
        os.utime(self.finite, (old_wall, old_wall))
        self.original_finite = self.finite.read_bytes()
        self.original_hf = self.hf_path.read_bytes()
        self.lock_ids = [self.inode(p) for p in (self.lock, self.hf_lock)]
        self.addCleanup(self.release_fixture_fds)

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value, sort_keys=True))
        path.chmod(0o600)

    @staticmethod
    def inode(path):
        s = path.stat()
        return s.st_dev, s.st_ino

    def unexpected_launch(self, *args, **kwargs):
        self.fail('recovery must never launch a voice session')

    def release_fixture_fds(self):
        # Close only synthetic in-process fixture descriptors; do not call Stop.
        for entry in self.supervisor._entries.values():
            self.supervisor._release(entry)
        for session in self.manager._sessions.values():
            if session.lease_fd >= 0:
                os.close(session.lease_fd)
                session.lease_fd = -1

    def digest(self):
        return hashlib.sha256(self.finite.read_bytes()).hexdigest()

    def recover(self, *, sid=SID, digest=None, verify=None, context=AUTH):
        return self.manager.recover('201', sid, self.digest() if digest is None else digest,
            context=context, verify_cleanup=self.proof if verify is None else verify)

    def proof(self, **request):
        self.proof_calls.append(request)
        self.assertEqual(request['profile'], self.profile)
        self.assertEqual(request['session_id'], SID)
        self.assertIsInstance(request['challenge'], str)
        self.assertGreater(request['deadline'], self.clock())
        samples = []
        for _ in range(2):
            samples.append({'observed_at': self.clock(), 'device': self.profile.device,
                'idle_lines': list(self.profile.lines), 'phone_or_related_channels': [],
                'streams': [{'slot': slot, 'status': 'Not Ready', 'tx_packets': 0,
                    'rx_packets': 0, 'tx_octets': 0, 'rx_octets': 0} for slot in range(5)]})
            self.clock.advance(0.25)
        return {'version': 1, 'session_id': request['session_id'],
                'challenge': request['challenge'], 'samples': samples}

    def assert_unchanged(self, finite=None, hf=None):
        self.assertEqual(self.finite.read_bytes(), self.original_finite if finite is None else finite)
        self.assertEqual(self.hf_path.read_bytes(), self.original_hf if hf is None else hf)
        self.assertEqual([self.inode(p) for p in (self.lock, self.hf_lock)], self.lock_ids)

    def assert_refused(self, **kwargs):
        with self.assertRaises(control.ControlError) as caught:
            self.recover(**kwargs)
        self.assertNotIn(PRIVATE, str(caught.exception))
        return caught.exception

    def test_success_preserves_failure_history_and_does_not_enable_or_confirm_old_stop(self):
        before = json.loads(self.original_finite)
        before_hf = json.loads(self.original_hf)
        result = self.recover()
        after = json.loads(self.finite.read_bytes())
        after_hf = json.loads(self.hf_path.read_bytes())
        expected = dict(before, state='failed', reserved=False, active=False, remaining_seconds=0)
        self.assertEqual(after, expected)
        self.assertEqual(after_hf, dict(before_hf, pending=False, error=None))
        self.assertFalse(after['stop_confirmed'])
        self.assertFalse(after_hf['enabled'])
        self.assertEqual(len(self.proof_calls), 1)
        self.assertEqual(result['status'], 'ORPHAN_RECOVERY_COMPLETE')
        self.assertEqual(result['session_id'], SID)
        self.assertIs(result['stop_confirmed'], False)
        self.assertEqual(result['prior_journal_sha256'], hashlib.sha256(self.original_finite).hexdigest())
        self.assertEqual(result['journal_sha256'], hashlib.sha256(self.finite.read_bytes()).hexdigest())
        archived_bytes = [f.read_bytes() for f in self.state.rglob('*') if f.is_file()]
        self.assertIn(self.original_finite, archived_bytes)
        self.assertIn(self.original_hf, archived_bytes)
        self.assertEqual([self.inode(p) for p in (self.lock, self.hf_lock)], self.lock_ids)
        encoded = json.dumps(result)
        self.assertNotIn(PRIVATE, encoded)
        self.assertNotIn(str(self.workspace), encoded)

    def test_eligibility_read_does_not_probe_or_modify_any_journal(self):
        paths = set(self.state.rglob('*'))
        result = self.manager.recovery_status('201', context=AUTH)
        self.assertIs(result['eligible'], True)
        self.assertEqual(result['session_id'], SID)
        self.assertEqual(result['journal_sha256'], self.digest())
        self.assertEqual(set(self.state.rglob('*')), paths)
        self.assert_unchanged()
        self.assertEqual(self.proof_calls, [])
        self.assertNotIn(str(self.workspace), json.dumps(result))

    def reentrant_control(self, action):
        entry = self.supervisor._entries['201']
        original = (entry.generation, copy.deepcopy(entry.history), bytes(entry.seen))
        def during_proof(**request):
            with self.assertRaises(control.ControlError):
                if action == 'enable':
                    self.supervisor.enable('201', 'racing-enable', context=AUTH,
                        enable_microphone=True, exclusive_media=True)
                else:
                    self.supervisor.disable('201', 'racing-disable', context=AUTH)
            self.assert_unchanged()
            self.assertEqual((entry.generation, entry.history, bytes(entry.seen)), original)
            return self.proof(**request)
        self.recover(verify=during_proof)
        self.assertFalse(json.loads(self.hf_path.read_bytes())['enabled'])

    def test_enable_during_cleanup_verification_is_rejected_without_remembering_key(self):
        self.reentrant_control('enable')

    def test_disable_during_cleanup_verification_is_rejected_without_remembering_key(self):
        self.reentrant_control('disable')

    def attach_current_orphan(self):
        session = control._Session(self.profile, SID, 'original-request', '201', -1,
            state='recovery_required', stop_requested=True, stop_confirmed=False,
            error='voice_stop_unconfirmed', phase='error', phase_reason='voice_session_failed',
            turns_completed=2)
        self.manager._sessions[SID] = session
        self.manager._current['201'] = session
        self.manager._requests[session.request_id] = session
        return session

    def test_matching_in_memory_orphan_keeps_truthful_cached_failure_after_success(self):
        session = self.attach_current_orphan()
        self.recover()
        self.assertEqual(session.state, 'failed')
        self.assertTrue(session.cleanup_verified)
        self.assertFalse(session.stop_confirmed)
        self.assertEqual(session.error, 'voice_stop_unconfirmed')
        self.assertEqual(session.turns_completed, 2)
        self.assertFalse(self.manager.status('201', context=AUTH)['stop_confirmed'])

    def test_contradictory_finite_cache_is_rejected_before_phone_proof(self):
        session = self.attach_current_orphan()
        for name, value in (('stop_confirmed', True), ('request_id', 'different-request'),
                            ('turns_completed', 99)):
            with self.subTest(field=name):
                old = getattr(session, name)
                setattr(session, name, value)
                self.assert_refused()
                self.assert_unchanged()
                self.assertEqual(self.proof_calls, [])
                self.assertFalse(self.manager.recovery_status('201', context=AUTH)['eligible'])
                setattr(session, name, old)

    def test_second_commit_failure_preserves_archive_and_blocks_new_control(self):
        module = self.manager._recovery_module()
        replace = module._replace
        calls = 0
        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('synthetic second-commit failure')
            return replace(*args, **kwargs)
        with mock.patch.object(module, '_replace', side_effect=fail_second):
            self.assert_refused()
        finite = json.loads(self.finite.read_bytes())
        self.assertEqual(finite['state'], 'failed')
        self.assertFalse(finite['reserved'])
        self.assertFalse(finite['stop_confirmed'])
        self.assertEqual(self.hf_path.read_bytes(), self.original_hf)
        self.assertIn('201', self.manager._recovering)
        for action in ('enable', 'disable'):
            with self.subTest(action=action), self.assertRaises(control.ControlError):
                if action == 'enable':
                    self.supervisor.enable('201', 'after-partial-enable', context=AUTH,
                        enable_microphone=True, exclusive_media=True)
                else:
                    self.supervisor.disable('201', 'after-partial-disable', context=AUTH)
        archives = list(self.state.glob('.orphan-recovery-*'))
        self.assertEqual(len(archives), 1)
        archive = archives[0]
        self.assertEqual((archive / 'finite.before.json').read_bytes(), self.original_finite)
        self.assertEqual((archive / 'hands-free.before.json').read_bytes(), self.original_hf)
        commit = json.loads((archive / 'commit.json').read_bytes())
        self.assertEqual(commit['after']['finite'], self.digest())
        self.assertEqual(commit['before']['hands_free'], hashlib.sha256(self.original_hf).hexdigest())
        self.assertFalse(json.loads(self.hf_path.read_bytes())['enabled'])

    def test_stale_hands_free_cache_is_not_accepted_in_place_of_disk_state(self):
        entry = self.supervisor._entries['201']
        for name, value in (('generation', 'f' * 32), ('history', []),
                            ('seen', bytearray(hands_free.BLOOM_BYTES))):
            with self.subTest(field=name):
                old = getattr(entry, name)
                setattr(entry, name, value)
                self.assert_refused()
                self.assert_unchanged()
                self.assertEqual(self.proof_calls, [])
                setattr(entry, name, old)

    def test_hands_free_cache_changes_during_proof_are_not_overwritten(self):
        entry = self.supervisor._entries['201']
        old = entry.generation
        def during_proof(**request):
            result = self.proof(**request)
            entry.generation = 'f' * 32
            return result
        self.assert_refused(verify=during_proof)
        self.assert_unchanged()
        self.assertEqual(entry.generation, 'f' * 32)
        entry.generation = old

    def test_finite_cache_changes_during_proof_are_not_overwritten(self):
        session = control._Session(self.profile, SID, 'original-request', '201', -1,
            state='recovery_required', stop_requested=True, stop_confirmed=False,
            error='voice_stop_unconfirmed', phase='error', phase_reason='voice_session_failed',
            turns_completed=2)
        self.manager._sessions[SID] = session
        self.manager._current['201'] = session
        self.manager._requests[session.request_id] = session
        def during_proof(**request):
            result = self.proof(**request)
            session.turns_completed += 1
            return result
        self.assert_refused(verify=during_proof)
        self.assert_unchanged()
        self.assertEqual(session.turns_completed, 3)

    def test_second_recovery_during_proof_cannot_take_over_original_transaction(self):
        def during_proof(**request):
            self.assert_refused(verify=lambda **kwargs: self.fail('second proof invoked'))
            self.assert_unchanged()
            return self.proof(**request)
        self.recover(verify=during_proof)
        self.assertEqual(len(self.proof_calls), 1)

    def test_archive_failure_occurs_before_any_journal_commit_and_releases_fences(self):
        module = self.manager._recovery_module()
        backup = module._backup
        count = 0
        def fail_second(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('synthetic archive failure ' + PRIVATE)
            return backup(*args, **kwargs)
        with mock.patch.object(module, '_backup', side_effect=fail_second):
            self.assert_refused()
        self.assert_unchanged()
        self.assertNotIn('201', self.manager._recovering)
        for path in (self.lock, self.hf_lock):
            fd = os.open(path, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)

    def test_diagnostics_and_run_evidence_are_retained_byte_for_byte(self):
        run = self.state / ('run-' + SID)
        run.mkdir(mode=0o700)
        report = run / 'result.json'
        failure = self.state / (self.key + '.last-failure.json')
        self.write_json(report, {'private_evidence': PRIVATE, 'session_id': SID})
        self.write_json(failure, {'error': PRIVATE, 'session_id': SID})
        saved = [(path, path.read_bytes(), self.inode(path)) for path in (report, failure)]
        self.recover()
        for path, raw, inode in saved:
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(self.inode(path), inode)

    def test_unexpired_external_owner_horizon_does_not_probe_or_release(self):
        now = time.time()
        os.utime(self.finite, (now, now))
        self.assert_refused()
        self.assert_unchanged()
        self.assertEqual(self.proof_calls, [])

    def test_wrong_sid_stale_hash_and_unauthorized_requests_do_not_probe_or_write(self):
        for kwargs in ({'sid': 'c' * 32}, {'digest': '0' * 64}, {'context': object()}):
            with self.subTest(kwargs=tuple(kwargs)):
                self.assert_refused(**kwargs)
                self.assert_unchanged()
                self.assertEqual(self.proof_calls, [])

    def test_live_finite_lease_and_foreign_hands_free_owner_are_not_adopted(self):
        for path in (self.lock, self.hf_lock):
            with self.subTest(path=path.name):
                fd = os.open(path, os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.assert_refused()
                    self.assert_unchanged()
                    self.assertEqual(self.proof_calls, [])
                finally:
                    os.close(fd)

    def test_live_handle_is_not_terminated_even_with_matching_failed_disk_journal(self):
        session = control._Session(self.profile, SID, 'original-request', '201', -1,
                                   state='recovery_required', handle=LiveRunner())
        self.manager._sessions[SID] = session
        self.manager._current['201'] = session
        self.manager._requests[session.request_id] = session
        self.assert_refused()
        self.assert_unchanged()
        self.assertEqual(self.proof_calls, [])

    def test_newer_current_session_cannot_be_recovered_through_old_sid(self):
        successor = control._Session(self.profile, 'd' * 32, 'new-request', '201', -1,
                                     state='running', handle=LiveRunner())
        self.manager._sessions[successor.session_id] = successor
        self.manager._current['201'] = successor
        self.manager._requests[successor.request_id] = successor
        self.assert_refused()
        self.assert_unchanged()
        self.assertEqual(self.proof_calls, [])

    def test_hands_free_on_or_outstanding_call_ticket_is_not_silently_disabled(self):
        original = json.loads(self.original_hf)
        for change in ({'enabled': True}, {'control_pending': {'id': 'e' * 32,
                       'generation': original['generation'], 'observed': False}}):
            with self.subTest(change=change):
                self.write_json(self.hf_path, dict(original, **change))
                changed = self.hf_path.read_bytes()
                self.assert_refused()
                self.assert_unchanged(hf=changed)
                self.assertEqual(self.proof_calls, [])
        self.hf_path.write_bytes(self.original_hf)

    def test_private_journal_and_symlink_boundaries_are_enforced(self):
        for path in (self.finite, self.hf_path):
            with self.subTest(path=path.name, kind='permissions'):
                path.chmod(0o644)
                self.assert_refused()
                self.assert_unchanged()
                self.assertEqual(self.proof_calls, [])
                path.chmod(0o600)
            with self.subTest(path=path.name, kind='symlink'):
                saved = path.with_suffix('.saved')
                path.rename(saved)
                path.symlink_to(saved)
                try:
                    self.assert_refused()
                    self.assertEqual(saved.read_bytes(),
                                     self.original_finite if path == self.finite else self.original_hf)
                    self.assertEqual(self.proof_calls, [])
                finally:
                    path.unlink()
                    saved.rename(path)

    def test_unconfirmed_active_or_foreign_phone_proof_never_releases_reservation(self):
        mutations = [
            lambda p: p.update(session_id='f' * 32),
            lambda p: p.update(challenge='wrong-challenge'),
            lambda p: p['samples'][1].update(device='SEP020000000099'),
            lambda p: p['samples'][1].update(idle_lines=['201']),
            lambda p: p['samples'][1].update(phone_or_related_channels=[{'channel': PRIVATE}]),
            lambda p: p['samples'][0]['streams'][2].update(status='Active'),
            lambda p: p['samples'][1]['streams'][2].update(tx_packets=1),
            lambda p: p['samples'][1]['streams'][2].update(rx_octets=1),
            lambda p: p['samples'][1]['streams'].pop(),
            lambda p: p['samples'][1]['streams'][4].update(slot=3),
            lambda p: p['samples'][1]['streams'][2].update(tx_packets=True),
            lambda p: p['samples'][1]['streams'][2].update(rx_packets=-1),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutations.index(mutation)):
                def bad(**request):
                    value = self.proof(**request)
                    mutation(value)
                    return value
                self.assert_refused(verify=bad)
                self.assert_unchanged()

    def test_missing_or_exceptional_cleanup_proof_has_no_write_or_secret_output(self):
        for output in (None, False, {}, {'error': PRIVATE}):
            with self.subTest(output=type(output).__name__):
                self.assert_refused(verify=lambda **kwargs: output)
                self.assert_unchanged()
        def failed(**kwargs):
            raise TimeoutError(PRIVATE)
        self.assert_refused(verify=failed)
        self.assert_unchanged()

    def test_cleanup_deadline_and_stale_or_future_sample_timestamps_are_rejected(self):
        def late(**request):
            value = self.proof(**request)
            self.clock.advance(31)
            return value
        self.assert_refused(verify=late)
        self.assert_unchanged()
        for offset in (-100, 100):
            with self.subTest(offset=offset):
                def bad_time(**request):
                    value = self.proof(**request)
                    for sample in value['samples']:
                        sample['observed_at'] += offset
                    return value
                self.assert_refused(verify=bad_time)
                self.assert_unchanged()

    def test_two_copies_of_one_snapshot_do_not_prove_settled_phone(self):
        def repeated(**request):
            value = self.proof(**request)
            value['samples'][1] = copy.deepcopy(value['samples'][0])
            return value
        self.assert_refused(verify=repeated)
        self.assert_unchanged()

    def test_finite_and_hands_free_cas_are_rechecked_after_external_observation(self):
        for path in (self.finite, self.hf_path):
            with self.subTest(path=path.name):
                changed = []
                def replace_during_probe(**request):
                    value = self.proof(**request)
                    raw = json.loads(path.read_bytes())
                    raw['session_id' if path == self.finite else 'generation'] = 'e' * 32
                    self.write_json(path, raw)
                    changed.append(path.read_bytes())
                    return value
                self.assert_refused(verify=replace_during_probe)
                self.assert_unchanged(finite=changed[0] if path == self.finite else None,
                                      hf=changed[0] if path == self.hf_path else None)
                path.write_bytes(self.original_finite if path == self.finite else self.original_hf)
                if path == self.finite:
                    old_wall = time.time() - control.EXTERNAL_OWNER_GRACE_SECONDS - 60
                    os.utime(path, (old_wall, old_wall))

    def test_active_or_already_unreserved_record_is_not_an_orphan(self):
        original = json.loads(self.original_finite)
        for change in ({'active': True}, {'reserved': False}, {'state': 'running'}):
            with self.subTest(change=change):
                self.write_json(self.finite, dict(original, **change))
                changed = self.finite.read_bytes()
                self.assert_refused()
                self.assert_unchanged(finite=changed)
                self.assertEqual(self.proof_calls, [])
        self.finite.write_bytes(self.original_finite)


if __name__ == '__main__':
    unittest.main()
