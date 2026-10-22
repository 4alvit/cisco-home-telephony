"""Authenticated adapters and ordinary-call handoff, with no network I/O."""
import contextlib
import copy
from email.message import Message
import io
import json
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

from test_phone_gateway import FakeAMI, config, gateway


class GatewayHandsFreeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.gateway = gateway.Gateway(config(), self.directory.name)
        self.addCleanup(self.gateway.db.close)
        self.core = gateway.app_module('phone_voice_control')
        self.gateway._voice_module = self.core
        self.gateway._voice_phone_proof = object()
        self.gateway.voice = mock.Mock()
        self.supervisor = mock.Mock()
        self.gateway.hands_free = self.supervisor
        self.view = {'schema_version': 1, 'phone': '201', 'line': '202',
                     'enabled': False, 'state': 'disabled', 'session_id': None,
                     'session_seconds': 300, 'consecutive_failures': 0,
                     'retry_after_seconds': 0, 'error': None}
        for action in ('enable', 'disable', 'status'):
            getattr(self.supervisor, action).return_value = dict(self.view)
        self.events = []
        self.inhibited = set()
        self.reserved = set()

        @contextlib.contextmanager
        def suspend(phone_id, *, resume=True, wait_for_call=False, defer_ticket=False):
            self.events.append(('suspend', phone_id, resume))
            self.assertEqual(wait_for_call, defer_ticket)
            self.inhibited.add(phone_id)
            try:
                yield True
            finally:
                self.inhibited.remove(phone_id)
                self.events.append(('resume', phone_id))

        @contextlib.contextmanager
        def reserve(phone_id, *, context):
            self.assertIn(phone_id, self.inhibited)
            self.events.append(('reserve', phone_id))
            self.reserved.add(phone_id)
            try:
                yield
            finally:
                self.reserved.remove(phone_id)
                self.events.append(('release', phone_id))

        self.supervisor.suspend_for_control.side_effect = suspend
        self.supervisor.mark_control_dispatch.side_effect = lambda phones: self.events.append(('dispatch', tuple(phones)))
        self.gateway.voice.reserve_control.side_effect = reserve
        self.ami = mock.patch.object(gateway, 'AMI', FakeAMI)
        self.ami.start()
        self.addCleanup(self.ami.stop)
        self.addCleanup(setattr, FakeAMI, 'channels', [])
        self.addCleanup(setattr, FakeAMI, 'calls', [])
        self.addCleanup(setattr, FakeAMI, 'endpoints', {'201': 'Not in use', '202': 'Not in use'})
        FakeAMI.channels, FakeAMI.calls = [], []
        FakeAMI.endpoints = {'201': 'Not in use', '202': 'Not in use'}

    def handler(self, operation, *, method='GET', body=None):
        raw = b'' if body is None else json.dumps(body).encode()
        handler = object.__new__(gateway.Handler)
        handler.server = SimpleNamespace(gateway=self.gateway, phone=None)
        handler.client_address = ('192.168.50.11', 12345)
        handler.command = method
        handler.path = '/v1/phones/202/voice_hands_free_' + operation
        handler.rfile = io.BytesIO(raw)
        handler.headers = Message()
        handler.headers['Authorization'] = 'Bearer ' + config()['api_token']
        handler.headers['Content-Type'] = 'application/json'
        handler.headers['Content-Length'] = str(len(raw))
        handler.reply = mock.Mock()
        return handler

    def test_status_has_separate_schema_and_no_ami_or_finite_status(self):
        with mock.patch.object(gateway, 'AMI', side_effect=AssertionError('read-only status')):
            handler = self.handler('status')
            handler.do_GET()
        self.assertEqual(handler.reply.call_args.args, (200, {'status': 'ok', 'hands_free': self.view}))
        self.gateway.voice.status.assert_not_called()
        self.supervisor.status.assert_called_once_with('202', context={
            'source': '192.168.50.11', 'authorization': ('Bearer ' + config()['api_token'],)})

    def test_enable_disable_use_existing_auth_and_exact_flags(self):
        body = {'idempotency_key': 'synthetic-enable', 'enable_microphone': True,
                'exclusive_media': True, 'dry_run': True}
        handler = self.handler('enable', method='POST', body=body)
        handler.do_POST()
        self.assertEqual(handler.reply.call_args.args[0], 200)
        self.assertEqual(handler.reply.call_args.args[1]['action_id'], body['idempotency_key'])
        self.assertEqual(self.supervisor.enable.call_args.args, ('202', 'synthetic-enable'))
        for flag in ('enable_microphone', 'exclusive_media', 'dry_run'):
            self.assertIs(self.supervisor.enable.call_args.kwargs[flag], True)
        handler = self.handler('disable', method='POST', body={'idempotency_key': 'synthetic-disable'})
        handler.do_POST()
        self.assertEqual(handler.reply.call_args.args[0], 200)
        self.assertEqual(self.supervisor.disable.call_args.args, ('202', 'synthetic-disable'))

    def test_bad_methods_shapes_queries_and_auth_never_dispatch(self):
        for op, method, body in (
                ('status', 'POST', None), ('enable', 'GET', None),
                ('disable', 'GET', None), ('enable', 'POST', {'idempotency_key': 'synthetic', 'enable_microphone': 1}),
                ('enable', 'POST', {'idempotency_key': 'synthetic', 'context': {}}),
                ('disable', 'POST', {'idempotency_key': 'synthetic', 'session_id': 'a' * 32}),
                ('status?include_playback=1', 'GET', None)):
            with self.subTest(op=op, method=method, body=body):
                handler = self.handler(op, method=method, body=body)
                handler.do_GET()
                self.assertIn(handler.reply.call_args.args[0], (400, 404))
        handler = self.handler('enable', method='POST', body={'idempotency_key': 'synthetic'})
        del handler.headers['Authorization']
        handler.do_POST()
        self.assertEqual(handler.reply.call_args.args[0], 403)
        self.supervisor.enable.assert_not_called()
        self.supervisor.disable.assert_not_called()
        self.supervisor.status.assert_not_called()

    def test_call_actions_hold_inhibit_and_physical_reservation_through_io(self):
        for action, extra in (('dial', {'number': '600'}), ('ring', {'destination': '640'}),
                              ('alert', {'kind': 'timer'}), ('speaker', {'mode': 'toggle'}),
                              ('answer', {'channel': 'PJSIP/202-abcd', 'uniqueid': 'synthetic-call'})):
            with self.subTest(action=action):
                self.events.clear()
                FakeAMI.calls = []
                FakeAMI.channels = ([{'Channel': 'PJSIP/202-abcd', 'Uniqueid': 'synthetic-call',
                                      'ChannelStateDesc': 'Ringing'}] if action == 'answer' else [])
                original = FakeAMI.action

                def io_guard(*args, **kwargs):
                    self.assertEqual(self.inhibited, {'201'})
                    self.assertEqual(self.reserved, {'201'})
                    self.events.append(('io',))

                def action_guard(ami, *args, **kwargs):
                    io_guard()
                    return original(ami, *args, **kwargs)

                with mock.patch.object(gateway, 'execute', side_effect=io_guard), \
                     mock.patch.object(FakeAMI, 'action', action_guard):
                    result = self.gateway.mutate('201', action, dict(
                        idempotency_key='handoff-' + action, line='202', **extra))
                self.assertEqual(result['status'], 'accepted')
                self.assertEqual(self.events[0], ('suspend', '201', action != 'speaker'))
                self.assertEqual(self.supervisor.suspend_for_control.call_args.kwargs['wait_for_call'],
                                 action in ('dial', 'ring', 'alert'))
                self.assertEqual(self.events[1], ('reserve', '201'))
                self.assertEqual(self.events[-2:], [('release', '201'), ('resume', '201')])
                self.assertIn(('io',), self.events)
                if action in ('dial', 'ring', 'alert'):
                    self.assertLess(self.events.index(('dispatch', ('201',))), self.events.index(('io',)))
                self.gateway.db.execute("UPDATE alerts SET state='ended'")
                self.gateway.db.commit()

    def test_intercom_inhibits_both_phones_until_both_reservations_release(self):
        other = dict(copy.deepcopy(config()['phones'][0]), id='203', lines=['203', '204'],
                     device='SEP001122334466', ipv4='192.168.50.22', port=18081)
        self.gateway.phones['203'] = other
        self.gateway.config['dial_allowlist'].append('203')
        FakeAMI.endpoints.update({'203': 'Not in use', '204': 'Not in use'})

        def intercom_guard(*args):
            self.assertEqual(self.inhibited, {'201', '203'})
            self.assertEqual(self.reserved, {'201', '203'})
            return {'line': '201', 'number': '203'}

        with mock.patch.object(self.gateway, '_intercom_reserved', side_effect=intercom_guard):
            result = self.gateway.mutate('201', 'intercom', {
                'idempotency_key': 'synthetic-intercom', 'number': '203'})
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(self.events[:4], [('suspend', '201', True), ('suspend', '203', True),
                                         ('reserve', '201'), ('reserve', '203')])
        self.assertFalse(self.inhibited or self.reserved)

    def test_rejected_validation_does_not_record_a_dispatched_call(self):
        for action, extra in (('dial', {'number': '999999'}),
                              ('ring', {'destination': '999999'}), ('alert', {'kind': 'invalid'})):
            with self.subTest(action=action), mock.patch.object(gateway, 'execute') as execute:
                with self.assertRaises(gateway.Refused):
                    self.gateway.mutate('201', action, dict(idempotency_key='bad-' + action, **extra))
                execute.assert_not_called()
                self.supervisor.mark_control_dispatch.assert_not_called()
        self.assertEqual(FakeAMI.calls, [])

    def test_uncertain_cleanup_refuses_ordinary_action_without_io(self):
        self.supervisor.suspend_for_control.side_effect = self.core.ControlError('voice_recovery_required')
        with mock.patch.object(gateway, 'AMI') as ami, mock.patch.object(gateway, 'execute') as execute:
            with self.assertRaises(gateway.Refused) as caught:
                self.gateway.mutate('201', 'dial', {'idempotency_key': 'failed-handoff', 'number': '600'})
        self.assertEqual(caught.exception.reason, 'voice_recovery_required')
        ami.assert_not_called()
        execute.assert_not_called()
        self.gateway.voice.reserve_control.assert_not_called()

    def test_motion_image_uses_passive_guard_and_finite_reservation_without_handoff(self):
        self.gateway.dnd_pending.clear()
        @contextlib.contextmanager
        def passive(phone_id):
            self.inhibited.add(phone_id)
            self.events.append(('passive', phone_id))
            try:
                yield
            finally:
                self.inhibited.remove(phone_id)
                self.events.append(('passive_done', phone_id))
        self.supervisor.reserve_idle_notification.side_effect = passive

        def display(phone, action, body):
            self.assertEqual(self.inhibited, {'201'})
            self.assertEqual(self.reserved, {'201'})
            self.assertEqual(action, 'image')
            self.assertEqual(body['idle_close_seconds'], 45)
            self.events.append(('display',))

        with mock.patch.object(self.gateway, 'notify', side_effect=display):
            result = self.gateway.mutate('201', 'motion_image', {'idempotency_key': 'passive-motion'})
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(self.events, [('passive', '201'), ('reserve', '201'),
                                      ('display',), ('release', '201'), ('passive_done', '201')])
        self.supervisor.suspend_for_control.assert_not_called()
        self.supervisor.mark_control_dispatch.assert_not_called()
        self.gateway.voice.stop.assert_not_called()

    def test_motion_image_refusal_and_listing_are_fail_closed(self):
        for reason in ('hands_free_enabled', 'voice_phone_busy', 'voice_state_unavailable'):
            with self.subTest(reason=reason):
                self.supervisor.reserve_idle_notification.side_effect = self.core.ControlError(reason)
                self.assertEqual(self.gateway.motion_image_admission(self.gateway.phones['201']),
                                 {'eligible': False, 'reason': reason})
                with mock.patch.object(gateway, 'AMI') as ami, mock.patch.object(self.gateway, 'notify') as notify:
                    with self.assertRaises(gateway.Refused) as caught:
                        self.gateway.mutate('201', 'motion_image', {'idempotency_key': 'blocked-motion'})
                self.assertEqual(caught.exception.reason, reason)
                ami.assert_not_called()
                notify.assert_not_called()
        self.gateway.voice.reserve_control.assert_not_called()
        self.supervisor.suspend_for_control.assert_not_called()

    def test_motion_listing_hides_unknown_error_details(self):
        self.supervisor.reserve_idle_notification.side_effect = OSError('sensitive path')
        self.assertEqual(self.gateway.motion_image_admission(self.gateway.phones['201']),
                         {'eligible': False, 'reason': 'voice_state_unavailable'})

    def test_idle_observation_requires_both_lines_no_channels_and_fresh_complete_data(self):
        self.assertEqual(self.gateway._hands_free_observe('201'), 'idle')
        for state, expected in (('In use', 'busy'), ('Ringing', 'busy'),
                                ('Unavailable', 'unknown'), ('invented', 'unknown')):
            FakeAMI.endpoints['202'] = state
            self.assertEqual(self.gateway._hands_free_observe('201'), expected)
        FakeAMI.endpoints['202'] = 'Not in use'
        FakeAMI.channels = [{'Channel': 'PJSIP/201-abcd', 'Uniqueid': 'synthetic'}]
        self.assertEqual(self.gateway._hands_free_observe('201'), 'call')
        FakeAMI.channels = []
        with mock.patch.object(gateway.time, 'monotonic', side_effect=[10, 11.01]):
            self.assertEqual(self.gateway._hands_free_observe('201'), 'unknown')
        with mock.patch.object(gateway, 'AMI', side_effect=OSError('private detail')):
            self.assertEqual(self.gateway._hands_free_observe('201'), 'unknown')
        self.gateway.db.execute('UPDATE preferences SET dnd=1 WHERE phone=?', ('201',))
        self.assertEqual(self.gateway._hands_free_observe('201'), 'busy')
        self.gateway.stopped.set()
        self.assertEqual(self.gateway._hands_free_observe('201'), 'unknown')

    def test_shutdown_waits_for_durable_policy_settlement_after_finite_cleanup(self):
        self.gateway.voice.reserve_shutdown.side_effect = lambda *a, **kw: contextlib.nullcontext()
        self.supervisor.settle_shutdown.side_effect = [False, True]
        self.addCleanup(self.gateway.release_voice_shutdown)
        with mock.patch.object(gateway.time, 'sleep') as pause:
            self.assertTrue(self.gateway.close_voice())
        self.assertEqual(self.supervisor.settle_shutdown.call_count, 2)
        self.assertEqual(self.gateway.voice.tick.call_count, 2)
        pause.assert_called_once()


if __name__ == '__main__':
    unittest.main()
