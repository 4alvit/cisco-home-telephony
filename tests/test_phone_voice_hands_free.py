"""Local desired-state/lease tests. No sockets, subprocesses or audio are used."""
from __future__ import annotations

from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'containers/asterisk' / filename)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


control = module('hands_free_control_test', 'phone_voice_control.py')
hands_free = module('hands_free_supervisor_test', 'phone_voice_hands_free.py')
PROOF = object()


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Runner:
    def __init__(self):
        self.code = None
        self.raw = None
        self.overflow = False
        self.stops = 0
        self.on_stop = None

    def poll(self):
        return self.code

    def terminate(self):
        self.stops += 1
        if self.on_stop:
            self.on_stop()

    def kill(self):
        raise AssertionError('test must never force-kill')

    def read_result(self):
        return self.raw

    def finish(self, session, reason, *, error=None, confirmed=True):
        self.code = 0 if error is None else 1
        self.raw = json.dumps({'transport': 'sip_audiosocket', 'session_id': session.session_id,
            'safe_no_media_started': False, 'originate_attempted': True,
            'pbx_deadline_armed': True, 'pbx_call_absent': confirmed,
            'cleanup_errors': [], 'result': {'reason': reason, 'stop_confirmed': confirmed,
                'cleanup_errors': []}, 'error': error, 'turns_completed': 1}).encode()


class HandsFreeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='hands-free-test-')
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name).resolve()
        self.state = self.workspace / '.local' / 'state'
        self.profile = control.Profile('201', ('201', '202'), 'SEP020000000001',
            '192.0.2.20', self.workspace / '.local/transport.json',
            self.workspace / '.local/voice.json', 60, '192.0.2.10', 24576,
            mode='wake', rearm_after_turn=True, media_transport='sip_audiosocket')
        self.config = control.ControlConfig(self.workspace, self.state, sys.executable,
                                            sys.executable, (self.profile,), True)
        self.clock = Clock()
        self.runners = []
        self.managers = []
        self.supers = []
        self.observation = 'idle'
        self.observations = 0
        self.manager, self.supervisor = self.new_manager()
        self.addCleanup(self.cleanup)

    def factory(self, *args, **kwargs):
        runner = Runner()
        self.runners.append(runner)
        return runner

    def observe(self, phone_id):
        self.assertEqual(phone_id, '201')
        self.assertFalse(self.manager._lock._is_owned())
        self.observations += 1
        return self.observation

    def new_manager(self, config=None):
        manager = control.VoiceControl(config or self.config,
            authorize=lambda context, action, phone: context is PROOF,
            runner_factory=self.factory, clock=self.clock, monitor=False)
        manager._prepare = lambda p, s: []
        supervisor = hands_free.HandsFreeSupervisor(manager, self.observe,
            lambda phone_id: PROOF, monitor=False)
        self.managers.append(manager)
        self.supers.append(supervisor)
        return manager, supervisor

    def cleanup(self):
        for manager, supervisor in zip(self.managers, self.supers):
            supervisor.close()
            for session in manager._sessions.values():
                if session.handle is not None:
                    session.handle.finish(session, 'explicit_stop')
            manager.tick()
            supervisor.tick()
            # Recovery tests intentionally retain an unresolved record; release
            # only this process's desired-state fd, never alter the finite fence.
            for entry in supervisor._entries.values():
                supervisor._release(entry)

    def enable(self, key='enable-0001', line='201', supervisor=None, **kwargs):
        return (supervisor or self.supervisor).enable(line, key, context=PROOF,
            enable_microphone=True, exclusive_media=True, **kwargs)

    def disable(self, key='disable-0001'):
        return self.supervisor.disable('201', key, context=PROOF)

    def status(self, supervisor=None):
        return (supervisor or self.supervisor).status('201', context=PROOF)

    def start(self):
        self.supervisor.tick()
        self.clock.advance(2)
        self.supervisor.tick()
        return self.manager._current['201']

    def finish(self, reason, *, error=None, confirmed=True):
        session = self.manager._current['201']
        session.handle.finish(session, reason, error=error, confirmed=confirmed)
        self.manager.tick()
        self.supervisor.tick()
        return session

    def record(self):
        return json.loads(self.supervisor._paths(self.supervisor._entries['201'])[1].read_text())

    def test_idle_notification_is_temporary_and_does_not_create_journal(self):
        entry = self.supervisor._entries['201']
        _, journal = self.supervisor._paths(entry)
        with self.supervisor.reserve_idle_notification('201'):
            self.assertEqual(entry.suspended, 1)
            self.assertIsNotNone(entry.fd)
            self.supervisor.tick()
            self.assertIsNotNone(entry.fd)
            self.assertFalse(journal.exists())
        self.assertEqual(entry.suspended, 0)
        self.assertIsNone(entry.fd)
        self.assertFalse(journal.exists())
        self.assertEqual(self.runners, [])

    def test_idle_notification_refuses_enabled_pending_and_uncertain_states(self):
        entry = self.supervisor._entries['201']
        for field, value in (('enabled', True), ('pending', True), ('launching', True),
                             ('blocked', True), ('storage_failed', True),
                             ('retry_pending', True), ('control_pending', {'id': 'pending'})):
            with self.subTest(field=field), mock.patch.object(entry, field, value):
                with self.assertRaises(control.ControlError):
                    with self.supervisor.reserve_idle_notification('201'):
                        self.fail('unsafe state admitted')
                self.assertEqual(entry.suspended, 0)
        self.assertEqual(self.runners, [])

    def test_idle_notification_rechecks_external_state_without_repair_writes(self):
        self.enable()
        self.disable()
        entry = self.supervisor._entries['201']
        _, journal = self.supervisor._paths(entry)
        self.supervisor.tick()
        self.assertIsNone(entry.fd)
        record = json.loads(journal.read_text())
        record['enabled'], record['pending'] = True, True
        journal.write_text(json.dumps(record))
        before = journal.read_bytes()
        with self.assertRaises(control.ControlError):
            with self.supervisor.reserve_idle_notification('201'):
                self.fail('stale disabled state admitted')
        self.assertEqual(journal.read_bytes(), before)
        self.assertIsNone(entry.fd)
        self.assertFalse(entry.enabled)
        self.assertEqual(self.runners, [])

    def test_idle_notification_refuses_another_desired_state_owner(self):
        _, other = self.new_manager()
        with other.reserve_idle_notification('201'):
            with self.assertRaises(control.ControlError) as caught:
                with self.supervisor.reserve_idle_notification('201'):
                    self.fail('external owner admitted')
            self.assertEqual(caught.exception.reason, 'hands_free_owner_unavailable')
        with self.supervisor.reserve_idle_notification('201'):
            pass

    def test_enable_during_idle_notification_waits_until_dispatch_finishes(self):
        with self.supervisor.reserve_idle_notification('201'):
            self.enable()
            self.supervisor.tick()
            self.clock.advance(3)
            self.supervisor.tick()
            self.assertEqual(self.runners, [])
        self.assertTrue(self.status()['enabled'])
        session = self.start()
        self.assertEqual(len(self.runners), 1)
        self.assertEqual(session.handle.stops, 0)

    def test_idle_notification_exception_releases_owner(self):
        with self.assertRaisesRegex(RuntimeError, 'synthetic'):
            with self.supervisor.reserve_idle_notification('201'):
                raise RuntimeError('synthetic')
        entry = self.supervisor._entries['201']
        self.assertEqual(entry.suspended, 0)
        self.assertIsNone(entry.fd)
        _, other = self.new_manager()
        with other.reserve_idle_notification('201'):
            pass

    def test_default_status_and_dry_run_are_read_only(self):
        self.assertEqual(self.status()['state'], 'disabled')
        self.assertFalse(self.state.exists())
        dry = self.enable(dry_run=True)
        self.assertEqual(dry['network_actions'], 0)
        self.assertFalse(dry['microphone_started'])
        self.assertFalse(self.state.exists())
        self.assertEqual(self.observations, 0)

    def test_authentication_precedes_state_access(self):
        with self.assertRaises(control.ControlError) as raised:
            self.supervisor.enable('999', 'enable-0001', context={}, enable_microphone=True,
                                   exclusive_media=True)
        self.assertEqual(raised.exception.reason, 'forbidden')
        self.assertFalse(self.state.exists())

    def test_opt_in_requires_supported_profile_and_consent(self):
        with self.assertRaises(control.ControlError):
            self.supervisor.enable('201', 'enable-0001', context=PROOF)
        _, unsupported = self.new_manager(replace(self.config,
            profiles=(replace(self.profile, duration=20),)))
        with self.assertRaises(control.ControlError) as raised:
            self.enable(supervisor=unsupported)
        self.assertEqual(raised.exception.reason, 'hands_free_not_supported')
        self.assertFalse(self.state.exists())

    def test_enable_waits_for_two_idle_observations_and_retains_line(self):
        result = self.enable(line='202')
        self.assertEqual(result['state'], 'waiting_idle')
        self.assertEqual(len(self.runners), 0)
        self.enable('enable-0002', line='201')
        self.assertEqual(self.status()['line'], '202')
        self.supervisor.tick()
        self.clock.advance(1.9)
        self.supervisor.tick()
        self.assertFalse(self.runners)
        self.clock.advance(.1)
        self.supervisor.tick()
        self.assertEqual(len(self.runners), 1)
        self.assertEqual(self.status()['state'], 'running')
        self.assertEqual(self.manager._current['201'].line, '202')

    def test_busy_resets_quiet_period_without_starting(self):
        self.enable()
        self.supervisor.tick()
        self.clock.advance(10)
        self.observation = 'busy'
        self.supervisor.tick()
        self.observation = 'idle'
        self.supervisor.tick()
        self.assertFalse(self.runners)
        self.clock.advance(2)
        self.supervisor.tick()
        self.assertEqual(len(self.runners), 1)

    def test_unknown_observations_back_off_then_fault(self):
        self.enable()
        self.observation = 'unknown'
        self.supervisor.tick()
        self.assertEqual(self.status()['retry_after_seconds'], 5)
        self.clock.advance(5)
        self.supervisor.tick()
        self.assertEqual(self.status()['retry_after_seconds'], 10)
        self.clock.advance(10)
        self.supervisor.tick()
        self.assertEqual(self.status()['error'], 'hands_free_observation_failed')
        self.observation = 'idle'
        self.clock.advance(100)
        self.supervisor.tick()
        self.assertFalse(self.runners)
        self.assertEqual(self.record()['error'], 'hands_free_observation_failed')

    def test_finite_manual_start_refused_while_enabled(self):
        self.enable()
        with self.assertRaises(control.ControlError) as raised:
            self.manager.start('201', 'manual-0001', context=PROOF, attended=True,
                               exclusive_media=True, enable_microphone=True)
        self.assertEqual(raised.exception.reason, 'hands_free_enabled')

    def test_exact_stop_persists_disable_before_signal(self):
        self.enable()
        session = self.start()
        self.runners[-1].on_stop = lambda: self.assertFalse(self.record()['enabled'])
        self.manager.stop('201', session.session_id, context=PROOF)
        self.assertFalse(self.status()['enabled'])
        self.finish('explicit_stop')
        self.clock.advance(100)
        self.supervisor.tick()
        self.assertEqual(len(self.runners), 1)

    def test_lease_rotation_is_early_and_requires_cleanup_plus_quiet(self):
        self.enable()
        first = self.start()
        self.clock.advance(54.9)
        self.supervisor.tick()
        self.assertFalse(first.stop_requested)
        self.clock.advance(.1)
        self.supervisor.tick()
        self.assertEqual(first.requested_reason, 'lease_expired')
        self.assertEqual(len(self.runners), 1)
        self.supervisor.tick()
        self.assertEqual(self.runners[0].stops, 1)
        self.finish('explicit_stop')
        self.assertEqual(first.completion_reason, 'lease_expired')
        self.assertEqual(len(self.runners), 1)
        self.clock.advance(2)
        self.supervisor.tick()
        self.assertEqual(len(self.runners), 2)

    def test_old_session_stop_cannot_disable_renewed_generation(self):
        self.enable()
        first = self.start()
        self.finish('lease_expired')
        self.clock.advance(2)
        self.supervisor.tick()
        self.assertEqual(len(self.runners), 2)
        self.manager.stop('201', first.session_id, context=PROOF)
        self.assertTrue(self.status()['enabled'])

    def test_positive_call_preemption_resumes_when_idle(self):
        self.enable()
        self.start()
        self.observation = 'busy'
        self.finish('call_preempted')
        self.clock.advance(20)
        self.supervisor.tick()
        self.assertEqual(len(self.runners), 1)
        self.observation = 'idle'
        self.supervisor.tick()
        self.clock.advance(2)
        self.supervisor.tick()
        self.assertEqual(len(self.runners), 2)

    def test_manual_and_ambiguous_transport_end_disable(self):
        for reason in ('peer_ended', 'peer_closed', 'dtmf_stop', 'call_changed', 'explicit_stop'):
            with self.subTest(reason=reason):
                self.enable('enable-' + reason)
                self.start()
                self.finish(reason)
                self.assertFalse(self.status()['enabled'])
                self.assertFalse(self.record()['enabled'])

    def test_three_clean_failures_exhaust_retries_despite_completed_turn(self):
        self.enable()
        self.start()
        for index, delay in enumerate((5, 10, 0), 1):
            self.finish('assistant_error', error='voice_session_failed')
            self.assertEqual(self.status()['consecutive_failures'], index)
            if delay:
                self.assertEqual(self.status()['retry_after_seconds'], delay)
                self.clock.advance(delay)
                self.start()
        self.assertEqual(self.status()['error'], 'hands_free_retry_exhausted')
        self.clock.advance(1000)
        self.supervisor.tick()
        self.assertEqual(len(self.runners), 3)

    def test_clean_restart_preserves_retry_budget_and_full_conservative_delay(self):
        self.enable()
        self.start()
        for expected_failures, delay in ((1, 5), (2, 10)):
            self.finish('assistant_error', error='voice_session_failed')
            self.assertTrue(self.record()['retry_pending'])
            self.clock.advance(2)
            self.supervisor.close()
            self.manager, self.supervisor = self.new_manager()
            state = self.status()
            self.assertEqual(state['state'], 'backoff')
            self.assertEqual(state['consecutive_failures'], expected_failures)
            self.assertEqual(state['retry_after_seconds'], delay)
            launches = len(self.runners)
            observations = self.observations
            self.supervisor.tick()
            self.clock.advance(delay - .1)
            self.supervisor.tick()
            self.assertEqual(len(self.runners), launches)
            self.assertEqual(self.observations, observations)
            self.clock.advance(.1)
            self.supervisor.tick()
            self.assertFalse(self.record()['retry_pending'])
            self.clock.advance(2)
            self.supervisor.tick()
            self.assertEqual(len(self.runners), launches + 1)
        self.finish('assistant_error', error='voice_session_failed')
        self.assertEqual(self.status()['consecutive_failures'], 3)
        self.assertEqual(self.status()['error'], 'hands_free_retry_exhausted')
        self.assertFalse(self.record()['retry_pending'])

    def test_fatal_error_with_same_name_and_counter_is_not_restored_as_retry(self):
        self.enable()
        entry = self.supervisor._entries['201']
        entry.failures = 1
        self.supervisor._fault(entry, 'hands_free_start_failed')
        self.supervisor.close()
        _, second = self.new_manager()
        self.assertEqual(self.status(second)['state'], 'faulted')
        self.assertEqual(self.status(second)['consecutive_failures'], 1)
        self.clock.advance(100)
        second.tick()
        self.assertFalse(self.runners)

    def test_legacy_retry_error_without_explicit_marker_stays_fail_closed(self):
        self.enable()
        self.start()
        self.finish('assistant_error', error='voice_session_failed')
        self.supervisor.close()
        path = self.supervisor._paths(self.supervisor._entries['201'])[1]
        data = json.loads(path.read_text())
        del data['retry_pending']
        path.write_text(json.dumps(data))
        _, second = self.new_manager()
        self.assertEqual(self.status(second)['state'], 'faulted')
        second.tick()
        self.assertEqual(len(self.runners), 1)

    def test_invalid_retry_marker_combinations_fail_closed(self):
        self.enable()
        self.start()
        self.finish('assistant_error', error='voice_session_failed')
        self.supervisor.close()
        path = self.supervisor._paths(self.supervisor._entries['201'])[1]
        baseline = json.loads(path.read_text())
        changes = ({'retry_pending': 1}, {'enabled': False}, {'failures': 0}, {'failures': 3},
                   {'error': 'hands_free_state_unavailable'}, {'pending': True},
                   {'control_pending': {'id': 'a' * 32, 'generation': 'b' * 32, 'observed': True}})
        for change in changes:
            with self.subTest(change=change):
                path.write_text(json.dumps(dict(baseline, **change)))
                _, second = self.new_manager()
                self.assertEqual(self.status(second)['state'], 'faulted')
                second.tick()
                second.close()
        self.assertEqual(len(self.runners), 1)

    def test_full_lease_resets_failures(self):
        self.enable()
        self.start()
        self.finish('assistant_error', error='voice_session_failed')
        self.clock.advance(5)
        self.start()
        self.finish('lease_expired')
        self.assertEqual(self.status()['consecutive_failures'], 0)

    def test_unconfirmed_cleanup_faults_and_never_renews(self):
        self.enable()
        self.start()
        self.finish('lease_expired', confirmed=False)
        self.assertEqual(self.status()['error'], 'hands_free_recovery_required')
        self.clock.advance(100)
        self.supervisor.tick()
        self.assertEqual(len(self.runners), 1)

    def test_second_manager_cannot_own_enabled_phone(self):
        self.enable()
        _, second = self.new_manager()
        self.assertEqual(self.status(second)['error'], 'hands_free_owner_unavailable')
        with self.assertRaises(control.ControlError):
            self.enable('enable-second', supervisor=second)
        second.tick()
        self.assertFalse(self.runners)

    def test_clean_idle_restart_restores_desired_state(self):
        self.enable()
        self.supervisor.close()
        _, second = self.new_manager()
        self.assertTrue(self.status(second)['enabled'])
        self.assertEqual(self.status(second)['state'], 'waiting_idle')
        self.assertFalse(self.runners)

    def test_clean_active_shutdown_preserves_intent_and_clears_pending(self):
        self.enable()
        session = self.start()
        self.manager.close()
        self.assertEqual(session.requested_reason, 'shutdown')
        self.finish('explicit_stop')
        self.assertEqual(session.completion_reason, 'shutdown')
        self.assertTrue(self.record()['enabled'])
        self.assertFalse(self.record()['pending'])
        self.manager, self.supervisor = self.new_manager()
        self.assertTrue(self.status()['enabled'])
        self.assertEqual(self.status()['state'], 'waiting_idle')
        self.supervisor.tick()
        self.assertEqual(len(self.runners), 1)
        self.clock.advance(2)
        self.supervisor.tick()
        self.assertEqual(len(self.runners), 2)
        self.assertNotEqual(self.manager._current['201'].session_id, session.session_id)

    def check_manual_end_during_shutdown(self, reason):
        self.enable()
        session = self.start()
        self.manager.close()
        self.assertEqual(session.requested_reason, 'shutdown')
        self.finish(reason)
        self.assertEqual(session.completion_reason, reason)
        self.assertTrue(session.cleanup_verified)
        self.assertFalse(self.record()['enabled'])
        self.assertFalse(self.record()['pending'])
        self.manager, self.supervisor = self.new_manager()
        self.assertFalse(self.status()['enabled'])
        self.assertEqual(self.status()['state'], 'disabled')
        launches = len(self.runners)
        self.supervisor.tick()
        self.clock.advance(2)
        self.supervisor.tick()
        self.assertEqual(len(self.runners), launches)

    def test_dtmf_stop_during_shutdown_stays_disabled_after_restart(self):
        self.check_manual_end_during_shutdown('dtmf_stop')

    def test_peer_ended_during_shutdown_stays_disabled_after_restart(self):
        self.check_manual_end_during_shutdown('peer_ended')

    def test_peer_closed_during_shutdown_stays_disabled_after_restart(self):
        self.check_manual_end_during_shutdown('peer_closed')

    def test_call_changed_during_shutdown_stays_disabled_after_restart(self):
        self.check_manual_end_during_shutdown('call_changed')

    def test_close_after_manager_finish_before_supervisor_settlement(self):
        self.enable()
        session = self.start()
        session.handle.finish(session, 'lease_expired')
        self.manager.tick()
        self.assertTrue(session.cleanup_verified)
        self.supervisor.close()
        self.supervisor.tick()
        self.assertFalse(self.record()['pending'])
        _, second = self.new_manager()
        self.assertEqual(self.status(second)['state'], 'waiting_idle')

    def test_interrupted_attempt_restart_fails_closed(self):
        self.enable()
        entry = self.supervisor._entries['201']
        entry.pending = True
        self.supervisor._persist(entry)
        self.supervisor._release(entry)
        _, second = self.new_manager()
        self.assertEqual(self.status(second)['error'], 'hands_free_recovery_required')
        second.tick()
        self.assertFalse(self.runners)

    def test_old_enable_cannot_undo_disable_after_restart(self):
        self.enable()
        self.disable()
        self.supervisor.tick()
        _, second = self.new_manager()
        self.enable(supervisor=second)
        self.assertFalse(self.status(second)['enabled'])

    def test_evicted_request_still_cannot_resurrect(self):
        self.enable()
        self.disable()
        for index in range(80):
            self.disable('disable-%04d-new' % index)
        with self.assertRaises(control.ControlError) as raised:
            self.enable()
        self.assertEqual(raised.exception.reason, 'voice_request_id_conflict')
        self.assertFalse(self.status()['enabled'])
        self.assertLessEqual(len(self.record()['history']), hands_free.HISTORY_SIZE)

    def test_record_is_private_and_has_no_authorization_material(self):
        self.enable()
        path = self.supervisor._paths(self.supervisor._entries['201'])[1]
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(self.record()), {'version', 'phone', 'line', 'enabled', 'generation',
            'failures', 'error', 'pending', 'control_pending', 'retry_pending', 'history', 'seen'})

    def test_invalid_or_symlink_record_fails_closed(self):
        self.enable()
        self.supervisor.close()
        path = self.supervisor._paths(self.supervisor._entries['201'])[1]
        path.write_text('{"version":1,"version":1}')
        _, second = self.new_manager()
        self.assertEqual(self.status(second)['state'], 'faulted')
        second.tick()
        self.assertFalse(self.runners)

    def test_persistence_failure_blocks_start(self):
        with mock.patch.object(hands_free.os, 'replace', side_effect=OSError('synthetic')):
            with self.assertRaises(control.ControlError):
                self.enable()
        self.clock.advance(100)
        self.supervisor.tick()
        self.assertFalse(self.runners)
        self.assertEqual(self.status()['state'], 'faulted')

    def test_failed_manual_disable_during_rotation_stays_faulted_after_restart(self):
        self.enable()
        self.start()
        self.clock.advance(55)
        self.supervisor.tick()
        with mock.patch.object(hands_free.os, 'replace', side_effect=OSError('synthetic')):
            with self.assertRaises(control.ControlError):
                self.disable()
        self.finish('explicit_stop')
        self.assertEqual(self.record()['error'], 'hands_free_state_unavailable')
        self.assertTrue(self.record()['enabled'])
        self.supervisor.close()
        _, second = self.new_manager()
        self.assertEqual(self.status(second)['state'], 'faulted')
        second.tick()
        self.assertEqual(len(self.runners), 1)

    def test_owner_conflict_is_fatal_not_an_unbounded_start_retry(self):
        self.enable()
        with mock.patch.object(self.manager, 'start',
                side_effect=control.ControlError('voice_phone_busy')) as start:
            self.supervisor.tick()
            self.clock.advance(2)
            self.supervisor.tick()
            self.clock.advance(1000)
            self.supervisor.tick()
        self.assertEqual(start.call_count, 1)
        self.assertEqual(self.status()['error'], 'hands_free_owner_unavailable')

    def test_prepare_failure_is_fatal_without_repeated_provider_initialization(self):
        self.enable()
        def fail_prepare(profile, session):
            raise ValueError('invalid local configuration')
        self.manager._prepare = fail_prepare
        self.start()
        self.assertEqual(self.status()['error'], 'hands_free_start_failed')
        self.clock.advance(1000)
        self.supervisor.tick()
        self.assertEqual(len(self.manager._sessions), 1)
        self.assertFalse(self.runners)

    def test_disable_during_prepare_cancels_launch_without_deadlock(self):
        entered, release = threading.Event(), threading.Event()
        def prepare(profile, session):
            self.assertFalse(self.manager._lock._is_owned())
            entered.set()
            self.assertTrue(release.wait(3))
            return []
        self.manager._prepare = prepare
        self.enable()
        self.supervisor.tick()
        self.clock.advance(2)
        worker = threading.Thread(target=self.supervisor.tick)
        worker.start()
        self.assertTrue(entered.wait(3))
        self.disable()
        release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.runners)
        self.assertFalse(self.record()['enabled'])
        self.assertTrue(self.manager._current['201'].cleanup_verified)

    def test_disable_during_observation_prevents_start(self):
        self.enable()
        self.supervisor.tick()
        self.clock.advance(2)
        def observe(phone):
            self.disable()
            return 'idle'
        self.supervisor.observe = observe
        self.supervisor.tick()
        self.assertFalse(self.runners)

    def test_control_context_inhibits_enable_from_initially_disabled_state(self):
        with self.supervisor.suspend_for_control('201') as managed:
            self.assertFalse(managed)
            self.enable()
            self.supervisor.tick()
            self.clock.advance(20)
            self.supervisor.tick()
            self.assertFalse(self.runners)
        self.start()
        self.assertEqual(len(self.runners), 1)

    def test_control_context_manual_speaker_disables_desired(self):
        self.enable()
        with self.supervisor.suspend_for_control('201', resume=False) as managed:
            self.assertTrue(managed)
            self.assertFalse(self.record()['enabled'])
        self.clock.advance(100)
        self.supervisor.tick()
        self.assertFalse(self.runners)

    def test_control_context_waits_for_exact_cleanup(self):
        self.enable()
        session = self.start()
        self.runners[-1].on_stop = lambda: self.runners[-1].finish(session, 'explicit_stop')
        def tick_manager():
            # The callback only marks child completion; manager owns verification.
            self.manager.tick()
        self.runners[-1].on_stop = lambda: (
            self.runners[-1].finish(session, 'explicit_stop'), tick_manager())
        with self.supervisor.suspend_for_control('201', timeout=.1) as managed:
            self.assertTrue(managed)
            self.assertTrue(session.cleanup_verified)
            self.assertEqual(session.completion_reason, 'call_preempted')
            self.supervisor.tick()
            self.assertEqual(len(self.runners), 1)
        self.start()
        self.assertEqual(len(self.runners), 2)

    def test_async_call_ack_cannot_resume_before_observed_call_and_quiet(self):
        self.enable()
        with self.supervisor.suspend_for_control('201', wait_for_call=True):
            self.assertIsNotNone(self.record()['control_pending'])
        self.supervisor.tick()
        self.clock.advance(2)
        self.supervisor.tick()
        self.assertFalse(self.runners)
        self.clock.advance(3)
        self.observation = 'call'
        self.supervisor.tick()
        self.assertTrue(self.record()['control_pending']['observed'])
        self.observation = 'idle'
        self.supervisor.tick()
        self.assertFalse(self.runners)
        self.clock.advance(2)
        self.supervisor.tick()
        self.assertEqual(len(self.runners), 1)
        self.assertIsNone(self.record()['control_pending'])

    def test_call_ticket_survives_restart_after_ack(self):
        self.enable()
        with self.supervisor.suspend_for_control('201', wait_for_call=True):
            pass
        self.supervisor.close()
        _, second = self.new_manager()
        self.assertEqual(self.status(second)['error'], 'hands_free_recovery_required')
        second.tick()
        self.clock.advance(20)
        second.tick()
        self.assertFalse(self.runners)
        with self.assertRaises(control.ControlError):
            self.enable('enable-new', supervisor=second)

    def test_unobserved_dispatch_timeout_faults_without_clearing_ticket(self):
        self.enable()
        with self.supervisor.suspend_for_control('201', wait_for_call=True):
            pass
        self.observation = 'busy'  # DND or a pending alert is not call evidence.
        self.supervisor.tick()
        self.clock.advance(31)
        self.supervisor.tick()
        self.assertEqual(self.status()['error'], 'hands_free_recovery_required')
        self.assertIsNotNone(self.record()['control_pending'])
        self.assertFalse(self.runners)

    def test_disable_and_context_return_keep_unresolved_ticket(self):
        self.enable()
        with self.supervisor.suspend_for_control('201', wait_for_call=True):
            self.disable()
        self.assertFalse(self.status()['enabled'])
        self.assertIsNotNone(self.record()['control_pending'])
        with self.assertRaises(control.ControlError):
            self.enable('enable-after-disable')
        self.clock.advance(31)
        self.supervisor.tick()
        self.observation = 'call'
        self.supervisor.tick()
        self.observation = 'idle'
        self.supervisor.tick()
        self.clock.advance(2)
        self.supervisor.tick()
        self.assertIsNone(self.record()['control_pending'])
        self.assertFalse(self.runners)
        self.assertEqual(self.status()['state'], 'faulted')
        self.enable('enable-after-reconciliation')
        self.start()
        self.assertEqual(len(self.runners), 1)

    def test_ambiguous_action_exception_faults_and_keeps_ticket(self):
        self.enable()
        with self.assertRaises(OSError):
            with self.supervisor.suspend_for_control('201', wait_for_call=True):
                raise OSError('response lost after dispatch')
        self.assertEqual(self.status()['error'], 'hands_free_recovery_required')
        self.assertIsNotNone(self.record()['control_pending'])
        self.assertFalse(self.runners)

    def test_deferred_dispatch_validation_failure_leaves_no_ticket(self):
        self.enable()
        with self.assertRaises(ValueError):
            with self.supervisor.suspend_for_control('201', wait_for_call=True, defer_ticket=True):
                self.assertIsNone(self.record()['control_pending'])
                raise ValueError('invalid destination before dispatch')
        self.assertIsNone(self.record()['control_pending'])
        self.assertIsNone(self.status()['error'])
        self.start()
        self.assertEqual(len(self.runners), 1)

    def test_deferred_dispatch_marks_then_preserves_ticket_on_ambiguous_error(self):
        self.enable()
        with self.assertRaises(OSError):
            with self.supervisor.suspend_for_control('201', wait_for_call=True, defer_ticket=True):
                self.supervisor.mark_control_dispatch(('201',))
                self.assertIsNotNone(self.record()['control_pending'])
                raise OSError('response lost after dispatch')
        self.assertEqual(self.status()['error'], 'hands_free_recovery_required')
        self.assertIsNotNone(self.record()['control_pending'])

    def test_dispatch_mark_requires_selected_control_context(self):
        self.enable()
        with self.assertRaises(control.ControlError):
            self.supervisor.mark_control_dispatch(('201',))
        with self.supervisor.suspend_for_control('201', wait_for_call=False):
            with self.assertRaises(control.ControlError):
                self.supervisor.mark_control_dispatch(('201',))
        self.assertIsNone(self.record()['control_pending'])

    def test_ticket_write_failure_prevents_external_action(self):
        self.enable()
        with mock.patch.object(hands_free.os, 'replace', side_effect=OSError('synthetic')):
            with self.assertRaises(control.ControlError):
                with self.supervisor.suspend_for_control('201', wait_for_call=True):
                    self.fail('external action must not execute before durable ticket')
        self.assertFalse(self.runners)

    def test_stale_second_manager_cannot_finite_start_or_control_enabled_owner(self):
        second_manager, second = self.new_manager()
        self.enable()
        with self.assertRaises(control.ControlError) as raised:
            second_manager.start('201', 'finite-second', context=PROOF, attended=True,
                                 exclusive_media=True, enable_microphone=True)
        self.assertEqual(raised.exception.reason, 'hands_free_owner_unavailable')
        with self.assertRaises(control.ControlError):
            with second.suspend_for_control('201'):
                self.fail('foreign persistent owner must retain physical control')
        self.assertFalse(self.runners)

    def test_stale_manager_revalidates_interrupted_record_after_acquiring_policy(self):
        second_manager, second = self.new_manager()
        self.enable()
        entry = self.supervisor._entries['201']
        entry.pending = True
        self.supervisor._persist(entry)
        self.supervisor._release(entry)
        with self.assertRaises(control.ControlError):
            second_manager.start('201', 'finite-second', context=PROOF, attended=True,
                                 exclusive_media=True, enable_microphone=True)
        second.tick()
        self.clock.advance(2)
        second.tick()
        self.assertFalse(self.runners)
        self.assertEqual(self.status(second)['error'], 'hands_free_recovery_required')

    def test_disabled_control_context_keeps_policy_fence_against_other_enable(self):
        _, second = self.new_manager()
        with self.supervisor.suspend_for_control('201'):
            self.supervisor.tick()
            with self.assertRaises(control.ControlError):
                self.enable('enable-second', supervisor=second)
        self.supervisor.tick()
        self.enable('enable-second-after', supervisor=second)
        self.assertTrue(self.status(second)['enabled'])

    def test_failed_load_cannot_be_skipped_by_second_mutation(self):
        self.enable()
        self.supervisor.close()
        path = self.supervisor._paths(self.supervisor._entries['201'])[1]
        path.write_text('{"version":1,"version":1}')
        _, second = self.new_manager()
        for _ in range(2):
            with self.assertRaises(control.ControlError):
                self.enable('retry-corrupt-state', supervisor=second)
        self.assertEqual(path.read_text(), '{"version":1,"version":1}')
        self.assertIsNone(second._entries['201'].fd)

    def test_failed_enable_receipt_is_not_replayed_as_durable_success(self):
        with mock.patch.object(hands_free.os, 'replace', side_effect=OSError('synthetic')):
            for _ in range(2):
                with self.assertRaises(control.ControlError):
                    self.enable()
        self.assertFalse(self.supervisor._entries['201'].history)
        self.enable()
        self.assertTrue(self.record()['enabled'])
        self.assertEqual(len(self.record()['history']), 1)

    def test_failed_disable_receipt_is_not_replayed_as_durable_success(self):
        self.enable()
        with mock.patch.object(hands_free.os, 'replace', side_effect=OSError('synthetic')):
            for _ in range(2):
                with self.assertRaises(control.ControlError):
                    self.disable()
        self.assertTrue(self.record()['enabled'])
        self.disable()
        self.assertFalse(self.record()['enabled'])

    def test_shutdown_can_preserve_durable_faulted_call_ticket(self):
        self.enable()
        with self.supervisor.suspend_for_control('201', wait_for_call=True):
            pass
        self.clock.advance(31)
        self.supervisor.tick()
        self.supervisor.close()
        self.assertTrue(self.supervisor.settle_shutdown())
        self.assertIsNotNone(self.record()['control_pending'])
        self.assertEqual(self.record()['error'], 'hands_free_recovery_required')

    def test_shutdown_reports_unpersisted_storage_failure(self):
        with mock.patch.object(hands_free.os, 'replace', side_effect=OSError('synthetic')):
            with self.assertRaises(control.ControlError):
                self.enable()
        self.supervisor.close()
        self.assertFalse(self.supervisor.settle_shutdown())

    def test_shutdown_settlement_does_not_wait_for_blocked_observation(self):
        entered, release = threading.Event(), threading.Event()
        self.enable()
        session = self.start()
        self.manager.close()
        session.handle.finish(session, 'explicit_stop')
        self.manager.tick()
        # Simulate an in-flight observation worker holding its pass mutex.
        def blocked_worker():
            with self.supervisor._pass_lock:
                entered.set()
                release.wait(3)
        worker = threading.Thread(target=blocked_worker)
        worker.start()
        self.assertTrue(entered.wait(3))
        try:
            self.assertTrue(self.supervisor.settle_shutdown())
            self.assertFalse(self.record()['pending'])
            self.assertIsNone(self.supervisor._entries['201'].fd)
        finally:
            release.set()
            worker.join(3)

    def test_control_timeout_releases_inhibit_without_clearing_finite_owner(self):
        self.enable()
        session = self.start()
        with self.assertRaises(control.ControlError):
            with self.supervisor.suspend_for_control('201', timeout=0):
                self.fail('must not admit while cleanup pending')
        self.assertEqual(self.supervisor._entries['201'].suspended, 0)
        self.assertGreaterEqual(session.lease_fd, 0)
        self.assertFalse(session.cleanup_verified)


if __name__ == '__main__':
    unittest.main()
