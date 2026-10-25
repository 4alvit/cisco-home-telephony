"""Hardware volume ownership and replay regression tests; no network or phone I/O."""
import contextlib
import tempfile
import threading
import unittest
from unittest import mock
from types import SimpleNamespace

from test_phone_gateway import FakeAMI, config, gateway


class HardwareVolumeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.server = gateway.Gateway(config(), self.directory.name)
        self.addCleanup(self.server.db.close)
        self.addCleanup(setattr, FakeAMI, 'channels', [])
        self.addCleanup(setattr, FakeAMI, 'calls', [])
        self.addCleanup(setattr, FakeAMI, 'endpoints',
                        {'201': 'Not in use', '202': 'Not in use'})
        FakeAMI.channels, FakeAMI.calls = [], []
        FakeAMI.endpoints = {'201': 'Not in use', '202': 'Not in use'}
        patcher = mock.patch.object(gateway, 'AMI', FakeAMI)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(gateway, 'execute')
        self.execute = patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, *, key='volume-test-1234', server=None, **changes):
        body = {'line': '201', 'context': 'ringer', 'direction': 'up',
                'steps': 1, 'idempotency_key': key}
        body.update(changes)
        return (server or self.server).mutate('201', 'hardware_volume', body)

    def call(self, line='202', state='Up', uniqueid='123.45'):
        channel = {'Channel': 'PJSIP/' + line + '-a1', 'Uniqueid': uniqueid,
                   'ChannelStateDesc': state}
        FakeAMI.channels = [channel]
        FakeAMI.endpoints[line] = 'In use'
        return {'line': line, 'context': 'call', 'channel': channel['Channel'],
                'uniqueid': uniqueid}

    def assert_claim_retained(self, **changes):
        before = self.execute.call_count
        with self.assertRaises(gateway.Refused) as caught:
            self.request(**changes)
        self.assertEqual(caught.exception.reason, 'previous_action_outcome_unknown')
        self.assertEqual(self.execute.call_count, before)

    def test_inventory_advertises_separate_hardware_capability(self):
        phone = self.server.listing()['phones'][0]
        self.assertIn('hardware_volume', phone['capabilities'])

    def test_secondary_line_ringer_keys_stay_on_physical_phone(self):
        result = self.request(line='202', steps=3)
        self.assertEqual([call.args[1] for call in self.execute.call_args_list],
                         ['Key:VolUp'] * 3)
        self.assertTrue(all(call.args[0]['id'] == '201' for call in self.execute.call_args_list))
        self.assertEqual(FakeAMI.calls, [])
        for key, value in {'context': 'ringer', 'direction': 'up', 'requested_steps': 3,
                           'acknowledged_steps': 3, 'level_known': False,
                           'volume_level': None, 'audio_output': 'contextual',
                           'physical_context_verified': False}.items():
            self.assertEqual(result[key], value)

    def test_single_up_call_uses_selected_line_and_never_switches_audio_route(self):
        result = self.request(direction='down', steps=2, **self.call())
        self.assertEqual([call.args[1] for call in self.execute.call_args_list],
                         ['Key:VolDwn', 'Key:VolDwn'])
        self.assertEqual(result['context'], 'call')
        self.assertEqual(result['acknowledged_steps'], 2)
        self.assertFalse(result['physical_context_verified'])
        self.assertEqual(FakeAMI.calls, [])

    def test_invalid_shapes_do_not_send_keys(self):
        variants = [dict(steps=x) for x in (True, False, 0, 6, -1, 1.0, '1', None)]
        variants += [dict(context=x) for x in ('auto', 'speaker', None)]
        variants += [dict(direction=x) for x in ('toggle', 'UP', None)]
        variants += [dict(line=x) for x in ('203', 201, None)]
        variants += [{'volume_level': .5}, {'uri': 'Key:Speaker'},
                     {'channel': 'PJSIP/201-a1', 'uniqueid': '123.45'}]
        for index, changes in enumerate(variants):
            with self.subTest(changes=changes), self.assertRaises(gateway.Refused):
                self.request(key='invalid-volume-' + str(index), **changes)
        self.execute.assert_not_called()
        self.assertEqual(FakeAMI.calls, [])

    def test_missing_required_fields_rejected(self):
        body = {'line': '201', 'context': 'ringer', 'direction': 'up',
                'steps': 1, 'idempotency_key': 'missing-volume-123'}
        for name in body:
            with self.subTest(name=name), self.assertRaises(gateway.Refused):
                self.server.mutate('201', 'hardware_volume',
                                   {key: value for key, value in body.items() if key != name})
        self.execute.assert_not_called()

    def test_unknown_or_softphone_target_does_not_send_keys(self):
        self.server.config['softphones'] = [{'extension': '209', 'room': 'Desk'}]
        for target in ('209', '999'):
            with self.subTest(target=target), self.assertRaises(gateway.Refused):
                self.server.mutate(target, 'hardware_volume', {
                    'line': target, 'context': 'ringer', 'direction': 'up',
                    'steps': 1, 'idempotency_key': 'unknown-volume-' + target})
        self.execute.assert_not_called()

    def test_ringer_refuses_call_on_either_line(self):
        for line in ('201', '202'):
            for state in ('Up', 'Ringing', 'Down'):
                self.call(line=line, state=state)
                with self.subTest(line=line, state=state), self.assertRaises(gateway.Refused):
                    self.request(key='busy-volume-' + line + state)
        self.execute.assert_not_called()
        self.assertEqual(FakeAMI.calls, [])

    def test_ringer_requires_both_endpoints_explicitly_idle(self):
        for value in ('In use', 'Ringing', 'Unavailable', 'Invalid', None, 'unknown'):
            FakeAMI.endpoints['202'] = value
            with self.subTest(value=value), self.assertRaises(gateway.Refused):
                self.request(key='endpoint-volume-' + str(value))
        FakeAMI.endpoints.pop('202')
        with self.assertRaises(gateway.Refused):
            self.request(key='endpoint-volume-missing')
        self.execute.assert_not_called()

    def test_ringer_refuses_pending_alert_before_channel_exists(self):
        self.server.db.execute('INSERT INTO alerts VALUES (?, ?, ?, ?, ?, NULL, ?, 0, 0)',
                               ('a' * 32, '201', '202', 'alarm', 'starting', 1.0))
        self.server.db.commit()
        with self.assertRaises(gateway.Refused):
            self.request()
        self.execute.assert_not_called()
        self.assertEqual(FakeAMI.calls, [])

    def test_call_requires_exact_current_identity_on_selected_line(self):
        original = self.call()
        for changes in ({'channel': 'PJSIP/202-b2'}, {'uniqueid': '123.46'},
                        {'line': '201'}, {'channel': None}, {'uniqueid': None}):
            body = {**original, **changes}
            with self.subTest(changes=changes), self.assertRaises(gateway.Refused):
                self.request(**body)
        self.execute.assert_not_called()

    def test_call_requires_an_established_not_ringing_or_unknown_channel(self):
        for state in ('Ringing', 'Ring', 'Down', 'Busy', 'Unknown', None):
            with self.subTest(state=state), self.assertRaises(gateway.Refused):
                self.request(key='state-volume-' + str(state), **self.call(state=state))
        self.execute.assert_not_called()

    def test_call_refuses_a_second_sibling_or_same_line_channel(self):
        for line in ('201', '202'):
            body = self.call()
            FakeAMI.channels.append({'Channel': 'PJSIP/' + line + '-b2',
                                     'Uniqueid': '123.46', 'ChannelStateDesc': 'Up'})
            with self.subTest(line=line), self.assertRaises(gateway.Refused):
                self.request(key='multiple-volume-' + line, **body)
        self.execute.assert_not_called()

    def test_call_without_channel_identity_or_live_channel_is_refused(self):
        with self.assertRaises(gateway.Refused):
            self.request(context='call')
        with self.assertRaises(gateway.Refused):
            self.request(context='call', channel='PJSIP/201-a1', uniqueid='123.45')
        self.execute.assert_not_called()

    def test_ringer_new_call_between_keys_stops_and_retains_claim(self):
        def new_call(*args, **kwargs):
            self.call(line='202')
        self.execute.side_effect = new_call
        with self.assertRaises(gateway.Refused) as caught:
            self.request(steps=3)
        self.assertGreaterEqual(caught.exception.status, 500)
        self.assertEqual(self.execute.call_count, 1)
        self.assert_claim_retained(steps=3)

    def test_replaced_call_between_keys_is_not_touched(self):
        body = self.call()
        def replace(*args, **kwargs):
            self.call(uniqueid='123.99')
        self.execute.side_effect = replace
        with self.assertRaises(gateway.Refused) as caught:
            self.request(steps=3, **body)
        self.assertGreaterEqual(caught.exception.status, 500)
        self.assertEqual(self.execute.call_count, 1)
        self.assert_claim_retained(steps=3, **body)

    def test_call_ending_between_keys_never_changes_ringer_volume(self):
        body = self.call()
        def ended(*args, **kwargs):
            FakeAMI.channels = []
            FakeAMI.endpoints = {'201': 'Not in use', '202': 'Not in use'}
        self.execute.side_effect = ended
        with self.assertRaises(gateway.Refused) as caught:
            self.request(steps=2, **body)
        self.assertGreaterEqual(caught.exception.status, 500)
        self.assertEqual(self.execute.call_count, 1)
        self.assert_claim_retained(steps=2, **body)

    def test_final_recheck_does_not_claim_verified_success_after_context_change(self):
        self.execute.side_effect = lambda *a, **k: self.call(line='202')
        with self.assertRaises(gateway.Refused) as caught:
            self.request(steps=1)
        self.assertGreaterEqual(caught.exception.status, 500)
        self.assertEqual(self.execute.call_count, 1)
        self.assert_claim_retained(steps=1)

    def test_ambiguous_phone_timeout_retains_claim_after_restart(self):
        self.execute.side_effect = TimeoutError('synthetic timeout')
        with self.assertRaises(gateway.Refused) as caught:
            self.request(steps=3)
        self.assertGreaterEqual(caught.exception.status, 500)
        self.assertEqual(self.execute.call_count, 1)
        restart = gateway.Gateway(config(), self.directory.name)
        self.addCleanup(restart.db.close)
        self.assert_claim_retained(server=restart, steps=3)

    def test_successful_replay_after_restart_sends_no_additional_keys(self):
        first = self.request(steps=2)
        restart = gateway.Gateway(config(), self.directory.name)
        self.addCleanup(restart.db.close)
        self.assertEqual(self.request(server=restart, steps=2), first)
        self.assertEqual(self.execute.call_count, 2)
        with self.assertRaises(gateway.Refused) as caught:
            self.request(server=restart, steps=3)
        self.assertEqual(caught.exception.reason, 'idempotency_key_conflict')
        self.assertEqual(self.execute.call_count, 2)

    def fake_player(self, *, record=None, pending=False):
        self.server.player = SimpleNamespace(
            sessions={} if record is None else {'201': record},
            pending_cleanup={'201': object()} if pending else {},
            snapshot=mock.Mock(side_effect=AssertionError('Do not reconcile player state')),
            observe=mock.Mock(side_effect=AssertionError('Do not reconcile player state')))

    def test_pending_player_start_or_cleanup_blocks_ringer_without_reconciliation(self):
        for index, (record, pending) in enumerate((
                ({'line': '201', 'uid': None}, False),
                (None, True),
                ({'line': '201', 'uid': '123.45'}, False))):
            self.fake_player(record=record, pending=pending)
            with self.subTest(record=record, pending=pending), self.assertRaises(gateway.Refused):
                self.request(key='pending-volume-' + str(index))
            self.server.player.snapshot.assert_not_called()
            self.server.player.observe.assert_not_called()
        self.execute.assert_not_called()
        self.assertEqual(FakeAMI.calls, [])

    def test_active_player_call_requires_matching_record_and_no_pending_cleanup(self):
        body = self.call()
        for index, (record, pending) in enumerate((
                ({'line': '202', 'uid': None}, False),
                ({'line': '202', 'uid': '123.99'}, False),
                ({'line': '201', 'uid': '123.45'}, False),
                ({'line': '202', 'uid': '123.45'}, True))):
            self.fake_player(record=record, pending=pending)
            with self.subTest(record=record, pending=pending), self.assertRaises(gateway.Refused):
                self.request(key='player-volume-' + str(index), **body)
        self.execute.assert_not_called()
        self.fake_player(record={'line': '202', 'uid': '123.45'})
        result = self.request(key='player-volume-matching', **body)
        self.assertEqual(result['acknowledged_steps'], 1)
        self.server.player.snapshot.assert_not_called()
        self.server.player.observe.assert_not_called()
        self.assertEqual(FakeAMI.calls, [])

    def test_expired_queue_wait_sends_no_keys_and_leaves_no_claim(self):
        now = [100.0]
        class BusyLock:
            def __enter__(self):
                now[0] += 20
                return self
            def __exit__(self, *args):
                pass
            def acquire(self, *, timeout):
                self.timeout = timeout
                now[0] += 20
                return False
            def release(self):
                raise AssertionError('Unacquired lock must not be released')
        self.server.lock = BusyLock()
        with mock.patch.object(gateway.time, 'monotonic', side_effect=lambda: now[0]):
            with self.assertRaises(gateway.Refused) as caught:
                self.request()
        self.assertGreaterEqual(caught.exception.status, 500)
        self.execute.assert_not_called()
        self.assertLessEqual(self.server.lock.timeout, 15)
        self.assertIsNone(self.server.db.execute(
            'SELECT response FROM operations WHERE key=?', ('volume-test-1234',)).fetchone())

    def test_real_lock_contention_times_out_without_poisoning_replay(self):
        locked, release = threading.Event(), threading.Event()

        def hold_lock():
            with self.server.lock:
                locked.set()
                release.wait(2)

        holder = threading.Thread(target=hold_lock)
        holder.start()
        try:
            self.assertTrue(locked.wait(1))
            # Consume most of the budget before admission, leaving a short
            # real acquire timeout without a wall-clock speed assertion.
            with mock.patch.object(gateway.time, 'monotonic', side_effect=[100, 114.99]):
                with self.assertRaises(gateway.Refused) as caught:
                    self.request()
            self.assertEqual(caught.exception.reason, 'hardware_volume_timeout')
            self.execute.assert_not_called()
            self.assertIsNone(self.server.db.execute(
                'SELECT response FROM operations WHERE key=?', ('volume-test-1234',)).fetchone())
        finally:
            release.set()
            holder.join(2)
        self.assertFalse(holder.is_alive())
        accepted = self.request()
        self.assertEqual(self.request(), accepted)
        self.assertEqual(self.execute.call_count, 1)

    def test_queue_wait_consumes_the_same_operation_budget(self):
        now = [100.0]
        class DelayedLock:
            released = False
            def __enter__(self):
                now[0] += 10
                return self
            def __exit__(self, *args):
                self.release()
            def acquire(self, *, timeout):
                now[0] += 10
                return True
            def release(self):
                self.released = True
        self.server.lock = DelayedLock()
        self.execute.side_effect = lambda *args, **kwargs: now.__setitem__(0, now[0] + 6)
        with mock.patch.object(gateway.time, 'monotonic', side_effect=lambda: now[0]):
            with self.assertRaises(gateway.Refused) as caught:
                self.request()
        self.assertGreaterEqual(caught.exception.status, 500)
        self.assertEqual(self.execute.call_count, 1)
        self.assertTrue(self.server.lock.released)
        self.assertEqual(self.server.db.execute(
            'SELECT response FROM operations WHERE key=?', ('volume-test-1234',)).fetchone(), (None,))

    def test_lock_obtained_after_deadline_is_released_without_dispatch(self):
        now = [100.0]
        class LateLock:
            released = False
            def __enter__(self):
                now[0] += 16
                return self
            def __exit__(self, *args):
                self.release()
            def acquire(self, *, timeout):
                now[0] += 16
                return True
            def release(self):
                self.released = True
        self.server.lock = LateLock()
        with mock.patch.object(gateway.time, 'monotonic', side_effect=lambda: now[0]):
            with self.assertRaises(gateway.Refused):
                self.request()
        self.execute.assert_not_called()
        self.assertTrue(self.server.lock.released)
        self.assertIsNone(self.server.db.execute(
            'SELECT response FROM operations WHERE key=?', ('volume-test-1234',)).fetchone())

    def test_budget_expiring_after_key_stops_and_retains_claim(self):
        now = [100.0]
        def delayed(*args, **kwargs):
            now[0] += 16.0
        self.execute.side_effect = delayed
        with mock.patch.object(gateway.time, 'monotonic', side_effect=lambda: now[0]):
            with self.assertRaises(gateway.Refused) as caught:
                self.request(steps=5)
        self.assertGreaterEqual(caught.exception.status, 500)
        self.assertEqual(self.execute.call_count, 1)
        self.assert_claim_retained(steps=5)

    def test_snapshot_failure_after_first_key_stops_and_retains_claim(self):
        original = FakeAMI.snapshot
        def snapshot(ami):
            if self.execute.call_count:
                raise TimeoutError('synthetic AMI timeout')
            return original(ami)
        with mock.patch.object(FakeAMI, 'snapshot', snapshot):
            with self.assertRaises(gateway.Refused) as caught:
                self.request(steps=3)
        self.assertGreaterEqual(caught.exception.status, 500)
        self.assertEqual(self.execute.call_count, 1)
        self.assert_claim_retained(steps=3)

    def attach_passive_guards(self):
        self.server._voice_module = gateway.app_module('phone_voice_control')
        self.server._voice_phone_proof = object()
        self.server.voice = mock.Mock()
        self.server.hands_free = mock.Mock()
        held = set()
        @contextlib.contextmanager
        def passive(phone_id):
            held.add('passive')
            try:
                yield
            finally:
                held.remove('passive')
        @contextlib.contextmanager
        def finite(phone_id, *, context):
            self.assertIn('passive', held)
            held.add('finite')
            try:
                yield
            finally:
                held.remove('finite')
        self.server.hands_free.reserve_idle_notification.side_effect = passive
        self.server.voice.reserve_control.side_effect = finite
        return held

    def assert_no_voice_handoff(self):
        self.server.hands_free.suspend_for_control.assert_not_called()
        self.server.hands_free.mark_control_dispatch.assert_not_called()
        self.server.hands_free.disable.assert_not_called()
        self.server.voice.stop.assert_not_called()

    def test_passive_guards_held_during_every_key_without_handoff(self):
        held = self.attach_passive_guards()
        self.execute.side_effect = lambda *a, **k: self.assertEqual(held, {'passive', 'finite'})
        self.request(steps=3)
        self.assertEqual(held, set())
        self.assert_no_voice_handoff()

    def test_hands_free_active_or_uncertain_refuses_without_stopping_it(self):
        self.attach_passive_guards()
        for index, reason in enumerate(('hands_free_enabled', 'voice_phone_busy',
                                       'hands_free_state_unavailable', 'voice_recovery_required')):
            self.server.hands_free.reserve_idle_notification.side_effect = (
                self.server._voice_module.ControlError(reason))
            for context in ('ringer', 'call'):
                body = self.call() if context == 'call' else {}
                with self.subTest(reason=reason, context=context), self.assertRaises(gateway.Refused):
                    self.request(key='hf-volume-' + str(index) + context, **body)
        self.execute.assert_not_called()
        self.server.voice.reserve_control.assert_not_called()
        self.assert_no_voice_handoff()

    def test_finite_voice_lease_refuses_without_stopping_it(self):
        self.attach_passive_guards()
        self.server.voice.reserve_control.side_effect = (
            self.server._voice_module.ControlError('voice_phone_busy'))
        with self.assertRaises(gateway.Refused):
            self.request()
        self.execute.assert_not_called()
        self.assert_no_voice_handoff()


if __name__ == '__main__':
    unittest.main()
