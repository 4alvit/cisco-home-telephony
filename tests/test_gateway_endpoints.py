"""Opt-in SIP controls, callback admission, and exact-identity hangup."""
import http.client
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_phone_gateway import FakeAMI as BaseAMI, config, gateway


class FakeAMI(BaseAMI):
    watchers = []

    def prepare_callback_watch(self, handler):
        self.watchers.append(handler)

    def finish_callback_watch(self):
        pass


def endpoint_config():
    value = config()
    value['softphones'] = [{'extension': '230', 'controls_enabled': True}, {'extension': '231'}]
    return value


def channel(extension='230', uniqueid='123.1', state='Up', suffix='abc'):
    return {'Channel': 'PJSIP/' + extension + '-' + suffix, 'Uniqueid': uniqueid, 'ChannelStateDesc': state}


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.server = gateway.Gateway(endpoint_config(), self.directory.name)
        self.addCleanup(self.server.db.close)
        self.server.dnd_pending.discard('230')
        FakeAMI.calls, FakeAMI.channels = [], []
        FakeAMI.watchers = []
        FakeAMI.endpoints = {'201': 'Not in use', '202': 'Not in use', '230': 'Not in use'}
        ami = patch.object(gateway, 'AMI', FakeAMI)
        ami.start(); self.addCleanup(ami.stop)
        execute = patch.object(gateway, 'execute')
        self.execute = execute.start(); self.addCleanup(execute.stop)

    def request(self, action, key='endpoint-test-123', **body):
        return self.server.endpoint_mutate('230', action, {'idempotency_key': key, **body})

    def test_explicit_opt_in_and_backward_compatible_directory(self):
        for extension in ('201', '231', '232'):
            with self.assertRaisesRegex(gateway.Refused, 'unknown_endpoint'):
                self.server.endpoint_listing(extension)
            with self.assertRaisesRegex(gateway.Refused, 'unknown_endpoint'):
                self.server.endpoint_mutate(extension, 'call', {'number': '600', 'idempotency_key': 'disabled-123'})
        for enabled in ('true', 1, None, []):
            value = endpoint_config(); value['softphones'][0]['controls_enabled'] = enabled
            with self.assertRaisesRegex(gateway.Refused, 'bad_softphone_controls_enabled'):
                gateway.validate(value)
        self.assertEqual(self.server.listing()['softphones'], [
            {'extension': '230', 'room': None, 'online': True, 'controls_enabled': True},
            {'extension': '231', 'room': None, 'online': False}])
        FakeAMI.endpoints['230'] = FakeAMI.endpoints['231'] = 'Unknown'
        listing = self.server.listing()['softphones']
        self.assertFalse(listing[0]['online'])
        self.assertTrue(listing[1]['online'])  # Legacy metadata behavior is preserved.

    def test_fresh_status_and_only_supported_capabilities(self):
        FakeAMI.channels = [channel(state='Ringing'), channel('231')]
        result = self.server.endpoint_listing('230')
        self.assertTrue(result['ami_fresh'] and result['configured'] and result['available'])
        self.assertEqual(result['calls'], [{'channel': 'PJSIP/230-abc', 'uniqueid': '123.1', 'state': 'Ringing'}])
        self.assertEqual(result['call_state'], 'ringing')
        self.assertEqual(result['capabilities'], {
            'call': True, 'hangup': True, 'dnd': True, 'answer': False, 'auto_answer': False, 'media': False})
        FakeAMI.channels = [channel()]
        self.assertEqual(self.server.endpoint_listing('230')['call_state'], 'in_call')
        FakeAMI.channels = []
        FakeAMI.endpoints['230'] = 'Unavailable'
        self.assertEqual(self.server.endpoint_listing('230')['call_state'], 'unavailable')
        FakeAMI.endpoints.pop('230')
        self.assertFalse(self.server.endpoint_listing('230')['configured'])
        self.execute.assert_not_called()

    def test_callback_dials_allowlisted_home_route_after_local_answer(self):
        result = self.request('call', number='600')
        self.assertEqual((result['mode'], result['status']), ('callback', 'accepted'))
        self.assertEqual(len(FakeAMI.calls), 1)
        action, values = FakeAMI.calls[0]
        values = dict(values)
        self.assertRegex(values.pop('ChannelId'), r'^cisco-callback-[a-f0-9]{32}$')
        self.assertRegex(values.pop('action_id'), r'^[a-f0-9]{24}$')
        self.assertEqual((action, values), ('Originate', {
            'Channel': 'PJSIP/230', 'Context': 'home', 'Exten': '600', 'Priority': '1',
            'Timeout': '20000', 'Async': 'true', 'CallerID': 'Home callback <600>'}))
        self.assertEqual(self.server.endpoint_listing('230')['call_state'], 'calling')
        self.execute.assert_not_called()
        for number in ('911', 'sip:test@example.org', '600\r\nAction:Command', 600):
            with self.assertRaisesRegex(gateway.Refused, 'number_not_allowed'):
                self.request('call', key='not-allowed-123', number=number)

    def test_callback_gap_and_durable_replay(self):
        result = self.request('call', number='600')
        with self.assertRaisesRegex(gateway.Refused, 'callback_pending'):
            self.request('call', key='second-key-123', number='600')
        restarted = gateway.Gateway(endpoint_config(), self.directory.name)
        self.addCleanup(restarted.db.close)
        self.assertEqual(restarted.endpoint_mutate('230', 'call', {
            'number': '600', 'idempotency_key': 'endpoint-test-123'}), result)
        self.assertEqual(sum(name == 'Originate' for name, _ in FakeAMI.calls), 1)
        FakeAMI.channels = [channel(uniqueid=FakeAMI.calls[0][1]['ChannelId'])]
        self.server.endpoint_listing('230')
        FakeAMI.channels = []
        self.assertEqual(self.server.endpoint_listing('230')['call_state'], 'idle')
        self.request('call', key='next-call-123', number='600')
        self.assertEqual(sum(name == 'Originate' for name, _ in FakeAMI.calls), 2)

    def test_unknown_originate_cannot_be_replayed_or_duplicated(self):
        with patch.object(FakeAMI, 'action', side_effect=OSError('synthetic lost acknowledgement')) as action:
            with self.assertRaises(OSError): self.request('call', number='600')
            with self.assertRaisesRegex(gateway.Refused, 'previous_action_outcome_unknown'):
                self.request('call', number='600')
            with self.assertRaisesRegex(gateway.Refused, 'callback_pending'):
                self.request('call', key='different-key-123', number='600')
            self.assertEqual(action.call_count, 1)

    def test_unobserved_callback_never_expires_into_permission_to_redial(self):
        with patch.object(gateway.time, 'time', return_value=1000):
            self.request('call', number='600')
        with patch.object(gateway.time, 'time', return_value=1021):
            with self.assertRaisesRegex(gateway.Refused, 'callback_pending'):
                self.request('call', key='still-pending-123', number='600')
        with patch.object(gateway.time, 'time', return_value=100000):
            result = self.server.endpoint_listing('230')
            self.assertEqual((result['callback_state'], result['call_state']), ('unknown', 'unknown'))
            with self.assertRaisesRegex(gateway.Refused, 'callback_pending'):
                self.request('call', key='new-callback-123', number='600')
        self.assertEqual(sum(name == 'Originate' for name, _ in FakeAMI.calls), 1)

    def test_unrelated_call_or_hangup_cannot_retire_pending_originate(self):
        self.request('call', number='600')
        FakeAMI.channels = [channel()]
        self.server.endpoint_listing('230')
        self.request('hangup', key='unrelated-hangup-123', channel='PJSIP/230-abc', uniqueid='123.1')
        FakeAMI.channels = []
        with self.assertRaisesRegex(gateway.Refused, 'callback_pending'):
            self.request('call', key='unrelated-ended-123', number='600')

    def test_correlated_no_answer_and_fast_hangup_retire_only_owned_attempt(self):
        self.request('call', number='600')
        first = FakeAMI.calls[-1][1]
        first_watcher = FakeAMI.watchers[-1]
        for event in ({'Event': 'Hangup', 'Uniqueid': 'unrelated'},
                      {'Event': 'OriginateResponse', 'ActionID': 'other', 'Response': 'Failure'},
                      {'Event': 'OriginateResponse', 'ActionID': first['action_id'], 'Response': 'Success'}):
            self.assertFalse(first_watcher(event))
        self.assertTrue(first_watcher({'Event': 'OriginateResponse',
                                      'ActionID': first['action_id'], 'Response': 'Failure'}))
        self.assertEqual(self.server.endpoint_listing('230')['callback_state'], 'idle')
        self.request('call', key='second-attempt-123', number='600')
        second = FakeAMI.calls[-1][1]
        # A delayed event belonging to the retired attempt cannot clear a new one.
        first_watcher({'Event': 'Hangup', 'Uniqueid': first['ChannelId']})
        with self.assertRaisesRegex(gateway.Refused, 'callback_pending'):
            self.request('call', key='third-attempt-123', number='600')
        self.assertTrue(FakeAMI.watchers[-1]({'Event': 'Hangup', 'Uniqueid': second['ChannelId']}))
        self.assertEqual(self.server.endpoint_listing('230')['callback_state'], 'idle')

    def test_explicit_originate_rejection_is_terminal_but_timeout_is_not(self):
        error = gateway.Refused('ami_action_rejected', 503)
        error.ami_action = 'Originate'
        with patch.object(FakeAMI, 'action', side_effect=error):
            with self.assertRaises(gateway.Refused): self.request('call', number='600')
        self.assertEqual(self.server.endpoint_listing('230')['callback_state'], 'idle')
        with patch.object(FakeAMI, 'action', side_effect=gateway.Refused('ami_timeout', 503)):
            with self.assertRaises(gateway.Refused):
                self.request('call', key='timeout-attempt-123', number='600')
        self.assertEqual(self.server.endpoint_listing('230')['callback_state'], 'starting')

    def test_call_requires_fresh_idle_available_endpoint(self):
        for state, owned, reason in [
                ('Unavailable', [], 'endpoint_unavailable'), ('Unknown', [], 'endpoint_unavailable'),
                ('Busy', [], 'endpoint_busy'), ('Not in use', [channel()], 'endpoint_busy')]:
            FakeAMI.endpoints['230'], FakeAMI.channels = state, owned
            with self.assertRaisesRegex(gateway.Refused, reason): self.request('call', number='600')
        self.assertEqual(FakeAMI.calls, [])

    def test_hangup_rejects_stale_ambiguous_and_foreign_channels(self):
        for owned, reason in [([], 'exactly_one_endpoint_channel_required'),
                ([channel(uniqueid='123.2')], 'channel_identity_changed'),
                ([channel(uniqueid=None)], 'channel_identity_changed'),
                ([channel(), channel(suffix='def', uniqueid='123.2')], 'exactly_one_endpoint_channel_required'),
                ([channel('231')], 'exactly_one_endpoint_channel_required')]:
            FakeAMI.channels = owned
            with self.assertRaisesRegex(gateway.Refused, reason):
                self.request('hangup', channel='PJSIP/230-abc', uniqueid='123.1')
        with self.assertRaisesRegex(gateway.Refused, 'invalid_channel_identity'):
            self.request('hangup', channel='PJSIP/231-abc', uniqueid='123.1')
        self.assertEqual(FakeAMI.calls, [])

    def test_hangup_does_not_follow_reused_channel_name(self):
        FakeAMI.channels = [channel()]
        def reuse(_ami, name, **values):
            FakeAMI.channels = [channel(uniqueid='123.2')]
            self.assertEqual((name, values['Channel']), ('Hangup', '123.1'))
            raise gateway.Refused('ami_action_rejected', 503)
        with patch.object(FakeAMI, 'action', reuse):
            with self.assertRaisesRegex(gateway.Refused, 'ami_action_rejected'):
                self.request('hangup', channel='PJSIP/230-abc', uniqueid='123.1')
        self.assertEqual(FakeAMI.channels[0]['Uniqueid'], '123.2')
        with self.assertRaisesRegex(gateway.Refused, 'previous_action_outcome_unknown'):
            self.request('hangup', channel='PJSIP/230-abc', uniqueid='123.1')

    def test_exact_hangup_when_endpoint_is_unavailable(self):
        FakeAMI.channels = [channel(), channel('231')]
        FakeAMI.endpoints['230'] = 'Unavailable'
        self.request('hangup', channel='PJSIP/230-abc', uniqueid='123.1')
        self.assertEqual(FakeAMI.calls, [('Hangup', {'Channel': '123.1', 'Cause': '16'})])
        self.execute.assert_not_called()

    def test_dnd_persists_and_reconciles_only_opted_in_clients(self):
        self.assertTrue(self.request('dnd', enabled=True)['dnd'])
        self.assertEqual(FakeAMI.calls, [('DBPut', {'Family': 'cisco-dnd', 'Key': '230', 'Val': '1'})])
        with self.assertRaisesRegex(gateway.Refused, 'do_not_disturb'):
            self.request('call', key='call-during-dnd', number='600')
        restarted = gateway.Gateway(endpoint_config(), self.directory.name)
        self.addCleanup(restarted.db.close)
        self.assertTrue(restarted.preference('230')['dnd'])
        restarted.sync_dnd(FakeAMI({}))
        self.assertEqual(FakeAMI.calls[-1], ('DBPut', {'Family': 'cisco-dnd', 'Key': '230', 'Val': '1'}))
        self.assertNotIn('231', [values.get('Key') for _, values in FakeAMI.calls])

    def test_partial_dnd_stays_pending_and_claimed(self):
        with patch.object(FakeAMI, 'action', side_effect=gateway.Refused('ami_timeout', 503)):
            with self.assertRaisesRegex(gateway.Refused, 'ami_timeout'): self.request('dnd', enabled=True)
        self.assertTrue(self.server.preference('230')['dnd'])
        self.assertIn('230', self.server.dnd_pending)
        with self.assertRaisesRegex(gateway.Refused, 'previous_action_outcome_unknown'):
            self.request('dnd', enabled=True)

    def test_global_idempotency_and_strict_action_shapes(self):
        self.request('dnd', enabled=True)
        with self.assertRaisesRegex(gateway.Refused, 'idempotency_key_conflict'):
            self.server.mutate('201', 'dnd', {'enabled': True, 'idempotency_key': 'endpoint-test-123'})
        for body in ({'enabled': True}, {'enabled': 1, 'idempotency_key': 'bad-bool-123'},
                     {'enabled': True, 'idempotency_key': 'extra-field-123', 'autoanswer': True}):
            with self.assertRaises(gateway.Refused): self.server.endpoint_mutate('230', 'dnd', body)
        for action in ('answer', 'media_play', 'speaker', 'autoanswer'):
            with self.assertRaisesRegex(gateway.Refused, 'unknown_action'): self.request(action)


class AMICallbackEventsTests(unittest.TestCase):
    def test_buffered_events_survive_async_ack_and_context_exit(self):
        from test_gateway_ami_deadline import memory_ami
        wire = (
            b'Response: Success\r\nActionID: mask\r\n\r\n'
            b'Response: Success\r\nActionID: originate\r\n\r\n'
            b'Event: OriginateResponse\r\nActionID: other\r\nResponse: Failure\r\n\r\n'
            b'Event: OriginateResponse\r\nActionID: originate\r\nResponse: Success\r\n\r\n'
            b'Event: Hangup\r\nUniqueid: owned-callback\r\n\r\n')
        ami = memory_ami(wire)
        events = []
        def handler(event):
            events.append(event)
            return event.get('Event') == 'Hangup' and event.get('Uniqueid') == 'owned-callback'
        with patch.object(gateway.secrets, 'token_hex', return_value='mask'):
            ami.prepare_callback_watch(handler)
        ami.action('Originate', action_id='originate', ChannelId='owned-callback')
        self.assertEqual(events, [])
        with patch.object(gateway.threading, 'Thread') as thread:
            ami.finish_callback_watch()
            observe = thread.call_args.kwargs['target']
            thread.return_value.start.assert_called_once()
            ami.__exit__()
            self.assertFalse(ami.file.closed)
        observe()
        self.assertTrue(ami.file.closed)
        self.assertTrue(ami._callback_done)
        self.assertEqual([event['Event'] for event in events], ['OriginateResponse', 'OriginateResponse', 'Hangup'])

    def test_event_reader_uses_full_deadline_for_normal_ringing_silence(self):
        from test_gateway_ami_deadline import memory_ami
        ami = memory_ami(b'Event: Hangup\r\nUniqueid: owned-callback\r\n\r\n')
        ami._event_handler = lambda event: event.get('Uniqueid') == 'owned-callback'
        ami._callback_done = False
        with patch.object(gateway.threading, 'Thread') as thread:
            ami.finish_callback_watch()
            observe = thread.call_args.kwargs['target']
        observe()
        self.assertTrue(ami._callback_done)
        self.assertTrue(any(call.args[0] > 5 for call in ami.sock.settimeout.call_args_list))

    def test_failure_before_ack_is_observed_and_needs_no_reader_handoff(self):
        from test_gateway_ami_deadline import memory_ami
        ami = memory_ami(
            b'Event: OriginateResponse\r\nActionID: originate\r\nResponse: Failure\r\n\r\n'
            b'Response: Success\r\nActionID: originate\r\n\r\n')
        ami._event_handler = lambda event: event.get('ActionID') == 'originate' and event.get('Response') == 'Failure'
        ami._callback_done = False
        ami.action('Originate', action_id='originate')
        with patch.object(gateway.threading, 'Thread') as thread:
            ami.finish_callback_watch()
            thread.assert_not_called()
        ami.__exit__()
        self.assertTrue(ami.file.closed)


class EndpointHTTPTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.gateway = gateway.Gateway(endpoint_config(), directory.name)
        self.addCleanup(self.gateway.db.close)
        self.gateway.config['api_clients'] = ['127.0.0.1']
        self.gateway.dnd_pending.discard('230')
        self.http = gateway.ThreadingHTTPServer(('127.0.0.1', 0), gateway.Handler)
        self.http.gateway, self.http.phone = self.gateway, None
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        FakeAMI.calls, FakeAMI.channels = [], []
        FakeAMI.endpoints = {'230': 'Not in use'}
        ami = patch.object(gateway, 'AMI', FakeAMI)
        ami.start(); self.addCleanup(ami.stop)

    def request(self, path, method='GET', body=None, authenticated=True):
        headers = {'Content-Type': 'application/json'}
        if authenticated: headers['Authorization'] = 'Bearer ' + config()['api_token']
        connection = http.client.HTTPConnection(*self.http.server_address, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_routes_keep_authentication_source_and_method_checks(self):
        self.assertEqual(self.request('/v1/endpoints/230', authenticated=False)[0], 403)
        self.assertEqual(self.request('/v1/endpoints/230')[0], 200)
        self.assertEqual(self.request('/v1/endpoints/231')[0], 404)
        self.assertEqual(self.request('/v1/endpoints/230/call')[0], 404)
        self.assertEqual(self.request('/v1/endpoints/230', 'POST', '{}')[0], 404)
        self.gateway.config['api_clients'] = ['192.168.50.11']
        self.assertEqual(self.request('/v1/endpoints/230')[0], 403)

    def test_authenticated_callback_and_strict_json(self):
        for body in ('{', '[]', '{"number":"600","number":"630","idempotency_key":"duplicate-123"}'):
            self.assertEqual(self.request('/v1/endpoints/230/call', 'POST', body)[0], 400)
        self.assertEqual(FakeAMI.calls, [])
        status, result = self.request('/v1/endpoints/230/call', 'POST', json.dumps({
            'number': '600', 'idempotency_key': 'http-endpoint-123'}))
        self.assertEqual((status, result['mode']), (200, 'callback'))


if __name__ == '__main__':
    unittest.main()
