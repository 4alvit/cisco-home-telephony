"""Offline gateway voice adapters: fake supervisor, listeners and phone I/O."""
import copy
from email.message import Message
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from test_phone_gateway import FakeAMI, config, gateway


def voice_status():
    return {'schema_version': 1, 'phone': '201', 'line': '202', 'session_id': 'a' * 32,
            'state': 'stopped', 'active': False, 'remaining_seconds': 0,
            'stop_requested': True, 'stop_confirmed': True, 'error': None}


class GatewayVoiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.gateway = gateway.Gateway(config(), self.directory.name)
        self.addCleanup(self.gateway.db.close)
        self.core = gateway.app_module('phone_voice_control')
        self.voice = mock.Mock()
        for action in ('status', 'start', 'stop'):
            getattr(self.voice, action).return_value = voice_status()
        phone = config()['phones'][0]
        self.profile = SimpleNamespace(phone_id=phone['id'], lines=tuple(phone['lines']),
                                       device=phone['device'], ipv4=phone['ipv4'])
        self.registry = SimpleNamespace(profiles=(self.profile,), enabled=True)
        self.module = SimpleNamespace(load_control_config=mock.Mock(return_value=self.registry),
            TokenSourceAuthorizer=self.core.TokenSourceAuthorizer,
            VoiceControl=mock.Mock(return_value=self.voice), ControlError=self.core.ControlError,
            STOP_GRACE_SECONDS=45)

    def attach(self):
        with mock.patch.object(gateway, 'app_module', return_value=self.module):
            self.gateway.attach_voice('synthetic-private-registry', self.directory.name)

    def handler(self, operation, *, method='GET', body=b'', headers=None, source='192.168.50.11'):
        handler = object.__new__(gateway.Handler)
        handler.server = SimpleNamespace(gateway=self.gateway, phone=None)
        handler.client_address = (source, 12345)
        handler.command = method
        handler.path = '/v1/phones/202/voice_' + operation
        handler.rfile = io.BytesIO(body)
        handler.headers = Message()
        pairs = [('Authorization', 'Bearer ' + config()['api_token']),
                 ('Content-Type', 'application/json'), ('Content-Length', str(len(body)))]
        for key, value in pairs if headers is None else headers:
            handler.headers[key] = value
        handler.reply = mock.Mock()
        return handler

    def test_unattached_gateway_is_unavailable_without_loading_voice(self):
        self.assertIsNone(self.gateway.voice)
        with mock.patch.object(gateway, 'app_module') as load:
            handler = self.handler('status')
            handler.do_GET()
        self.assertEqual(handler.reply.call_args.args[0], 503)
        load.assert_not_called()

    def test_attachment_checks_physical_inventory_before_constructing_manager(self):
        for field, value in (('phone_id', '203'), ('lines', ('201', '204')),
                             ('device', 'SEPFFFFFFFFFFFF'), ('ipv4', '192.168.50.99')):
            before = getattr(self.profile, field)
            setattr(self.profile, field, value)
            with self.subTest(field=field), self.assertRaises(gateway.Refused):
                self.attach()
            setattr(self.profile, field, before)
        self.module.VoiceControl.assert_not_called()

    def test_api_and_phone_contexts_require_distinct_trusted_proofs(self):
        self.attach()
        authorize = self.module.VoiceControl.call_args.kwargs['authorize']
        context = {'source': '192.168.50.11', 'authorization': ['Bearer ' + config()['api_token']]}
        self.assertTrue(authorize(context, 'start', '201'))
        self.assertFalse(authorize(dict(context, source='192.168.50.12'), 'start', '201'))
        self.assertFalse(authorize(dict(context, authorization=context['authorization'] * 2), 'start', '201'))
        phone_context = self.gateway._phone_voice_context('201')
        self.assertTrue(authorize(phone_context, 'stop', '201'))
        self.assertFalse(authorize({'phone': '201', '_phone_proof': object()}, 'stop', '201'))
        self.assertFalse(authorize(phone_context, 'stop', '203'))
        with self.assertRaises(gateway.Refused):
            self.gateway._phone_voice_context('202')

    def test_status_and_stop_bypass_inventory_ami_and_mutate(self):
        self.attach()
        with mock.patch.object(gateway, 'AMI', side_effect=AssertionError('no AMI')), \
             mock.patch.object(self.gateway, 'listing', side_effect=AssertionError('no inventory')), \
             mock.patch.object(self.gateway, 'mutate', side_effect=AssertionError('no mutation dispatch')):
            handler = self.handler('status')
            handler.do_GET()
            self.assertEqual(handler.reply.call_args.args, (200, {'status': 'ok', 'voice': voice_status()}))
            body = {'idempotency_key': 'synthetic-stop', 'session_id': 'a' * 32}
            handler = self.handler('stop', method='POST', body=json.dumps(body).encode())
            handler.do_POST()
        self.assertEqual(handler.reply.call_args.args[0], 200)
        self.voice.status.assert_called_once_with('202', context={
            'source': '192.168.50.11', 'authorization': ('Bearer ' + config()['api_token'],)})
        self.assertEqual(self.voice.stop.call_args.args, ('202', 'a' * 32))

    def test_start_preserves_explicit_flags_and_correlation(self):
        self.attach()
        body = {'idempotency_key': 'synthetic-start', 'attended': True,
                'exclusive_media': True, 'enable_microphone': True, 'dry_run': True}
        handler = self.handler('start', method='POST', body=json.dumps(body).encode())
        handler.do_POST()
        self.assertEqual(handler.reply.call_args.args[1]['action_id'], 'synthetic-start')
        self.assertEqual(self.voice.start.call_args.args, ('202', 'synthetic-start'))
        for flag in ('attended', 'exclusive_media', 'enable_microphone', 'dry_run'):
            self.assertIs(self.voice.start.call_args.kwargs[flag], True)

    def test_source_and_bearer_denial_precede_dispatch(self):
        self.attach()
        for headers, source in (([], '192.168.50.11'),
                ([('Authorization', 'Bearer wrong')], '192.168.50.11'),
                ([('Authorization', 'Bearer ' + config()['api_token'])] * 2, '192.168.50.11'),
                ([('Authorization', 'Bearer ' + config()['api_token']),
                  ('X-Forwarded-For', '192.168.50.11')], '192.168.50.12')):
            with self.subTest(headers=len(headers), source=source):
                handler = self.handler('status', headers=headers, source=source)
                handler.do_GET()
                self.assertEqual(handler.reply.call_args.args[0], 403)
        self.voice.status.assert_not_called()

    def test_invalid_framing_duplicate_json_unknown_fields_and_flags_do_not_start(self):
        self.attach()
        invalid = [b'{', b'[]', b'{"idempotency_key":"synthetic-a","idempotency_key":"synthetic-b"}',
                   b'{"idempotency_key":"synthetic-a","context":{}}',
                   b'{"idempotency_key":"synthetic-a","attended":1}']
        for body in invalid:
            with self.subTest(body=body):
                handler = self.handler('start', method='POST', body=body)
                handler.do_POST()
                self.assertEqual(handler.reply.call_args.args[0], 400)
        for name in ('Transfer-Encoding', 'Content-Length'):
            handler = self.handler('start', method='POST', body=b'{}')
            handler.headers[name] = 'chunked' if name == 'Transfer-Encoding' else '2'
            handler.do_POST()
            self.assertEqual(handler.reply.call_args.args[0], 400)
        self.voice.start.assert_not_called()

    def test_wrong_methods_and_stop_session_id_do_not_dispatch(self):
        self.attach()
        for operation, method in (('status', 'POST'), ('start', 'GET'), ('stop', 'GET')):
            handler = self.handler(operation, method=method)
            handler.do_GET()
            self.assertEqual(handler.reply.call_args.args[0], 400)
        handler = self.handler('stop', method='POST', body=json.dumps(
            {'idempotency_key': 'synthetic-stop', 'session_id': 'invalid'}).encode())
        handler.do_POST()
        self.assertEqual(handler.reply.call_args.args[0], 400)
        self.assertEqual(self.voice.method_calls, [])

    def test_core_refusal_uses_fixed_reason_and_status(self):
        self.attach()
        self.voice.status.side_effect = self.core.ControlError('voice_recovery_required', 409)
        handler = self.handler('status')
        handler.do_GET()
        self.assertEqual(handler.reply.call_args.args, (409, {
            'status': 'refused', 'reason': 'voice_recovery_required'}))

    def test_listing_advertises_voice_only_for_attached_profiles(self):
        with mock.patch.object(gateway, 'AMI', FakeAMI):
            self.assertNotIn('voice', self.gateway.listing()['phones'][0]['capabilities'])
            self.attach()
            self.assertIn('voice', self.gateway.listing()['phones'][0]['capabilities'])

    def test_shutdown_wait_is_bounded_and_does_not_claim_cleanup(self):
        self.attach()
        for state, active in (('stopping', True), ('recovery_required', False)):
            self.voice.reset_mock()
            self.voice.status.return_value = dict(voice_status(), state=state,
                                                  active=active, stop_confirmed=False)
            with self.subTest(state=state), \
                 mock.patch.object(gateway.time, 'monotonic', side_effect=(0, 0, 50)), \
                 mock.patch.object(gateway.time, 'sleep') as sleep:
                self.gateway.close_voice()
            self.voice.close.assert_called_once()
            self.voice.tick.assert_called_once()
            sleep.assert_called_once_with(0.05)
            self.assertFalse(self.voice.status.return_value['stop_confirmed'])

    def test_shutdown_close_tick_or_status_failure_preserves_full_grace(self):
        self.attach()
        for method in ('close', 'tick', 'status'):
            self.voice.reset_mock(side_effect=True)
            self.voice.status.return_value = voice_status()
            getattr(self.voice, method).side_effect = RuntimeError('synthetic private storage error')
            with self.subTest(method=method), \
                 mock.patch.object(gateway.time, 'monotonic', side_effect=(0, 0, 50)) as clock, \
                 mock.patch.object(gateway.time, 'sleep') as sleep:
                self.gateway.close_voice()
            self.assertEqual(clock.call_count, 3)
            sleep.assert_called_once_with(0.05)

    def test_main_keeps_phone_callbacks_alive_until_voice_cleanup_finishes(self):
        path = Path(self.directory.name) / 'gateway.json'
        path.write_text(json.dumps(config()))
        events = []
        instance = mock.Mock()
        instance.phones = self.gateway.phones
        instance.config = self.gateway.config
        instance.voice.close.side_effect = lambda: events.append('voice-close')
        instance.close_voice.side_effect = lambda **_kwargs: events.append('voice-finish')
        listeners = [mock.Mock(), mock.Mock()]
        for listener in listeners:
            listener.shutdown.side_effect = lambda: events.append('listener-shutdown')
            listener.server_close.side_effect = lambda: events.append('listener-close')
        with mock.patch.object(gateway, 'Gateway', return_value=instance), \
             mock.patch.object(gateway, 'ThreadingHTTPServer', side_effect=listeners), \
             mock.patch.object(gateway.threading, 'Thread'), \
             mock.patch.object(gateway.threading, 'Event'), \
             mock.patch.object(gateway.signal, 'signal'), \
             mock.patch.object(gateway.os, 'umask'), \
             mock.patch.object(gateway.sys, 'argv', ['gateway', '--config', str(path),
                 '--state-directory', self.directory.name, '--voice-control-config', 'synthetic',
                 '--voice-workspace', self.directory.name]):
            gateway.main()
        instance.attach_voice.assert_called_once_with('synthetic', self.directory.name)
        self.assertLess(events.index('voice-close'), events.index('voice-finish'))
        self.assertLess(events.index('voice-finish'), events.index('listener-shutdown'))


if __name__ == '__main__':
    unittest.main()
