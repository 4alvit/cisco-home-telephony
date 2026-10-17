import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('phone_gateway', Path(__file__).resolve().parents[1] / 'containers/asterisk/phone_gateway.py')
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


def config():
    return {'persistent_enabled': True, 'bind_ipv4': '192.168.50.10', 'api_port': 18084,
            'api_token': 'a' * 48, 'api_clients': ['192.168.50.11'],
            'ami': {'host': '192.168.50.10', 'port': 5038, 'username': 'gateway', 'secret': 'b' * 48},
            'dial_allowlist': ['600', '630', '701'], 'ring_allowlist': ['640', '701'],
            'phones': [{'id': '201', 'lines': ['201', '202'], 'ipv4': '192.168.50.21', 'device': 'SEP001122334455',
                        'port': 18080, 'auth_path': '/auth/' + 'c' * 64, 'username': 'u' * 24, 'password': 'p' * 24}]}


class FakeAMI:
    channels = []
    calls = []
    endpoints = {'201': 'Not in use', '202': 'Not in use'}

    def __init__(self, _config): pass
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def snapshot(self): return copy.deepcopy(self.endpoints), copy.deepcopy(self.channels)
    def action(self, name, **values): self.calls.append((name, values)); return [{'Response': 'Success'}]


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.server = gateway.Gateway(config(), self.directory.name)
        self.addCleanup(self.server.db.close)
        FakeAMI.channels, FakeAMI.calls = [], []
        FakeAMI.endpoints = {'201': 'Not in use', '202': 'Not in use'}
        self.ami = patch.object(gateway, 'AMI', FakeAMI)
        self.ami.start(); self.addCleanup(self.ami.stop)
        self.execute = patch.object(gateway, 'execute')
        self.send = self.execute.start(); self.addCleanup(self.execute.stop)

    def request(self, action, **body):
        return self.server.mutate('201', action, {'idempotency_key': 'test-123456', **body})

    def test_requires_explicit_persistent_mode_and_unique_lines(self):
        value = config(); value['persistent_enabled'] = False
        with self.assertRaises(gateway.Refused): gateway.validate(value)
        value = config(); value['phones'][0]['lines'] = ['201', '201']
        with self.assertRaises(gateway.Refused): gateway.validate(value)

    def test_dial_uses_real_line_key_not_invalid_dial_suffix(self):
        self.request('dial', line='202', number='600')
        self.assertEqual([call.args[1] for call in self.send.call_args_list], ['Key:Line2', 'Dial:600'])

    def test_no_arbitrary_external_or_uri_dialing(self):
        for number in ('911', '112', '+18005551212', 'sip:arbitrary@example.org', '600\r\nAction:Command'):
            with self.assertRaises(gateway.Refused): self.request('dial', number=number)
        self.send.assert_not_called()

    def test_busy_phone_blocks_new_dial_and_speaker(self):
        FakeAMI.channels = [{'Channel': 'PJSIP/202-abc', 'Uniqueid': '123.4', 'ChannelStateDesc': 'Up'}]
        for action, values in [('dial', {'number': '600'}), ('speaker', {'mode': 'toggle'})]:
            with self.assertRaises(gateway.Refused): self.request(action, **values)
        self.send.assert_not_called()

    def test_ring_idempotency_survives_restart(self):
        first = self.request('ring', destination='640')
        again = gateway.Gateway(config(), self.directory.name)
        self.addCleanup(again.db.close)
        self.assertEqual(again.mutate('201', 'ring', {'destination': '640', 'idempotency_key': 'test-123456'}), first)
        self.assertEqual(len(FakeAMI.calls), 1)

    def alert_channels(self, alert_id, line='201'):
        root = 'cisco-alert-' + alert_id
        return [
            {'Channel': f'Local/{line}@cisco-alert-a1;1', 'Uniqueid': root + '-a',
             'Linkedid': root + '-a', 'ChannelStateDesc': 'Up'},
            {'Channel': f'Local/{line}@cisco-alert-a1;2', 'Uniqueid': root + '-b',
             'Linkedid': root + '-a', 'ChannelStateDesc': 'Up'},
            {'Channel': f'PJSIP/{line}-a1', 'Uniqueid': '123.45',
             'Linkedid': root + '-a', 'ChannelStateDesc': 'Ringing'}]

    def test_alert_uses_indefinite_private_dialplan_without_auto_answer_or_audio(self):
        result = self.request('alert', line='202', kind='timer')
        self.assertIsNone(result['ring_timeout_seconds'])
        action, values = FakeAMI.calls[-1]
        self.assertEqual(action, 'Originate')
        self.assertEqual(values['Channel'], 'Local/202@cisco-alert/n')
        self.assertEqual(values['Application'], 'WaitForSignal')
        self.assertEqual(values['Data'], 'cisco-alert-' + result['alert_id'])
        self.assertEqual(values['Timeout'], '5000')  # Local Answer only, never the phone Dial.
        self.assertNotIn('Exten', values)
        self.assertEqual(values['CallerID'], 'Timer <640>')
        self.send.assert_not_called()
        self.assertEqual(self.server.latest_alert('201')['state'], 'starting')

    def test_alert_identity_and_autoanswer_exclusion_survive_gateway_restart(self):
        result = self.request('alert', kind='alarm')
        FakeAMI.channels = self.alert_channels(result['alert_id'])
        restarted = gateway.Gateway(config(), self.directory.name)
        self.addCleanup(restarted.db.close)
        phone = restarted.listing()['phones'][0]
        self.assertEqual(phone['alert']['id'], result['alert_id'])
        self.assertEqual(phone['alert']['state'], 'ringing')
        self.assertTrue(restarted.is_alert_channel(FakeAMI.channels[-1]))
        with self.assertRaisesRegex(gateway.Refused, 'alert_requires_manual_answer_or_stop'):
            restarted.mutate('201', 'answer', {'idempotency_key': 'answer-12345',
                             'channel': 'PJSIP/201-a1', 'uniqueid': '123.45'})
        self.send.assert_not_called()
        self.assertEqual(self.request('alert', kind='alarm')['alert_id'], result['alert_id'])
        self.assertEqual(sum(action == 'Originate' for action, _ in FakeAMI.calls), 1)

    def test_new_alert_refused_while_another_is_starting_even_without_channel(self):
        self.request('alert', kind='timer')
        with self.assertRaisesRegex(gateway.Refused, 'alert_already_active'):
            self.server.mutate('201', 'alert', {'idempotency_key': 'different-1234', 'kind': 'alarm'})
        self.assertEqual(sum(action == 'Originate' for action, _ in FakeAMI.calls), 1)

    def test_stop_alert_only_hangs_its_exact_linked_channels_even_if_phone_offline(self):
        result = self.request('alert', kind='timer')
        FakeAMI.channels = self.alert_channels(result['alert_id']) + [
            {'Channel': 'PJSIP/202-b2', 'Uniqueid': '200.1', 'Linkedid': '200.1', 'ChannelStateDesc': 'Up'}]
        FakeAMI.endpoints = {'201': 'Unavailable', '202': 'Unavailable'}
        def stop(name, **values):
            FakeAMI.calls.append((name, values))
            if name == 'Hangup':
                FakeAMI.channels = [item for item in FakeAMI.channels if item['Uniqueid'] == '200.1']
            return [{'Response': 'Success'}]
        with patch.object(FakeAMI, 'action', side_effect=stop):
            stopped = self.server.mutate('201', 'alert_stop', {
                'idempotency_key': 'stop-alert-1234', 'alert_id': result['alert_id']})
        self.assertEqual(stopped['state'], 'ended')
        self.assertEqual(self.server.latest_alert('201')['reason'], 'stopped')
        self.assertEqual([row['Uniqueid'] for row in FakeAMI.channels], ['200.1'])
        self.assertEqual(sum(action == 'Hangup' for action, _ in FakeAMI.calls), 1)

    def test_cancel_before_channel_creation_catches_delayed_originate(self):
        result = self.request('alert', kind='alarm')
        self.server.mutate('201', 'alert_stop', {'idempotency_key': 'stop-delayed-123', 'alert_id': result['alert_id']})
        self.assertEqual(self.server.latest_alert('201')['state'], 'stopping')
        FakeAMI.channels = self.alert_channels(result['alert_id'])
        self.server.listing()
        self.assertTrue(any(action == 'Hangup' for action, _ in FakeAMI.calls))
        self.assertTrue(self.server.is_alert_channel(FakeAMI.channels[-1]))

    def test_manual_call_end_finishes_alert_without_repeating_it(self):
        result = self.request('alert', kind='alarm')
        FakeAMI.channels = self.alert_channels(result['alert_id'])
        self.server.listing()
        FakeAMI.channels = []
        alert = self.server.listing()['phones'][0]['alert']
        self.assertEqual(alert['state'], 'ended')
        self.assertEqual(alert['reason'], 'call_ended')
        self.server.listing()
        self.assertEqual(sum(action == 'Originate' for action, _ in FakeAMI.calls), 1)

    def test_terminal_alert_stays_cancelled_through_late_originate_and_ambiguous_stop(self):
        result = self.request('alert', kind='alarm')
        self.server.db.execute('UPDATE alerts SET created=0 WHERE id=?', (result['alert_id'],))
        self.server.db.commit()
        self.assertEqual(self.server.listing()['phones'][0]['alert']['state'], 'ended')
        FakeAMI.channels = self.alert_channels(result['alert_id'])
        with patch.object(FakeAMI, 'action', side_effect=OSError('Hangup response lost')):
            with self.assertRaises(OSError):
                self.server.listing()
        self.assertEqual(self.server.alerts()[0]['stop_requested'], 1)
        restarted = gateway.Gateway(config(), self.directory.name)
        self.addCleanup(restarted.db.close)
        for _ in range(2):
            previous = sum(action == 'Hangup' for action, _ in FakeAMI.calls)
            self.assertEqual(restarted.listing()['phones'][0]['alert']['state'], 'stopping')
            self.assertGreater(sum(action == 'Hangup' for action, _ in FakeAMI.calls), previous)
        FakeAMI.channels = []
        self.assertEqual(restarted.listing()['phones'][0]['alert']['state'], 'ended')
        self.assertEqual(sum(action == 'Originate' for action, _ in FakeAMI.calls), 1)

    def test_lost_originate_response_retains_cancellable_identity_without_retry(self):
        with patch.object(FakeAMI, 'action', side_effect=OSError('response lost')):
            with self.assertRaises(OSError):
                self.request('alert', kind='timer')
        alert = self.server.latest_alert('201')
        self.assertEqual(alert['state'], 'unknown')
        with self.assertRaisesRegex(gateway.Refused, 'previous_action_outcome_unknown'):
            self.request('alert', kind='timer')
        stopped = self.server.mutate('201', 'alert_stop', {
            'idempotency_key': 'stop-unknown-123', 'alert_id': alert['id']})
        self.assertEqual(stopped['state'], 'stopping')

    def test_invalid_or_unknown_alert_identity_never_hangs_up_a_call(self):
        self.request('alert', kind='alarm')
        with self.assertRaisesRegex(gateway.Refused, 'alert_not_found'):
            self.server.mutate('201', 'alert_stop', {'idempotency_key': 'bad-stop-1234', 'alert_id': '0' * 32})
        self.assertFalse(any(action == 'Hangup' for action, _ in FakeAMI.calls))

    def test_ambiguous_failure_never_repeats_dial(self):
        self.send.side_effect = OSError('connection interrupted')
        with self.assertRaises(OSError): self.request('dial', number='600')
        with self.assertRaisesRegex(gateway.Refused, 'previous_action_outcome_unknown'):
            self.request('dial', number='600')
        self.assertEqual(self.send.call_count, 1)

    def test_outgoing_ring_is_not_incoming_ringing(self):
        FakeAMI.channels = [{'Channel': 'PJSIP/201-abc', 'Uniqueid': '123.4', 'ChannelStateDesc': 'Ring'}]
        with self.assertRaisesRegex(gateway.Refused, 'phone_not_ringing'):
            self.request('answer', channel='PJSIP/201-abc', uniqueid='123.4')
        self.send.assert_not_called()

    def test_answer_and_hangup_reject_reused_or_foreign_channel(self):
        FakeAMI.channels = [{'Channel': 'PJSIP/201-abc', 'Uniqueid': '123.5', 'ChannelStateDesc': 'Ringing'}]
        for action in ('answer', 'hangup'):
            with self.assertRaisesRegex(gateway.Refused, 'channel_identity_changed'):
                self.request(action, channel='PJSIP/201-abc', uniqueid='123.4')
        self.assertEqual(FakeAMI.calls, [])
        self.send.assert_not_called()

    def test_answer_uses_matching_line_only(self):
        FakeAMI.channels = [{'Channel': 'PJSIP/202-def', 'Uniqueid': '123.5', 'ChannelStateDesc': 'Ringing'}]
        self.request('answer', line='202', channel='PJSIP/202-def', uniqueid='123.5')
        self.assertEqual(self.send.call_args.args[1], 'Key:Line2')

    def test_dnd_persists_and_blocks_alarm_without_a_call(self):
        self.request('dnd', enabled=True)
        self.assertEqual([call[0] for call in FakeAMI.calls], ['DBPut', 'DBPut'])
        with self.assertRaisesRegex(gateway.Refused, 'do_not_disturb'):
            self.server.mutate('201', 'ring', {'idempotency_key': 'alarm-123456'})
        self.assertNotIn('Originate', [call[0] for call in FakeAMI.calls])

    def test_partial_dnd_update_persists_desired_state_for_reconciliation(self):
        with patch.object(FakeAMI, 'action', side_effect=[{'Response': 'Success'}, OSError('lost AMI')]):
            with self.assertRaises(OSError): self.request('dnd', enabled=True)
        self.assertTrue(self.server.preference('201')['dnd'])
        self.assertIn('201', self.server.dnd_pending)
        self.server.sync_dnd(FakeAMI({}))
        self.assertEqual([call[1]['Val'] for call in FakeAMI.calls], ['1', '1'])
        with self.assertRaisesRegex(gateway.Refused, 'previous_action_outcome_unknown'):
            self.request('dnd', enabled=True)

    def test_resources_are_phone_scoped_and_expire(self):
        phone = config()['phones'][0]
        uri = self.server.resource(phone, b'<safe/>', 'text/xml')
        path = gateway.parse.urlsplit(uri).path
        self.assertIn(('201', path), self.server.resources)
        self.assertNotIn(('203', path), self.server.resources)
        with patch.object(gateway.time, 'time', return_value=gateway.time.time() + 121):
            self.server.resource(phone, b'<next/>', 'text/xml')
        self.assertNotIn(('201', path), self.server.resources)

    def test_message_is_escaped_and_pushed_as_idle_http_url(self):
        self.request('message', text='<unsafe>&literal', title='Private')
        call = self.send.call_args
        self.assertTrue(call.args[1].startswith('http://192.168.50.10:18080/content/'))
        self.assertEqual(call.kwargs['priority'], 2)
        content = next(iter(self.server.resources.values()))[1]
        self.assertIn(b'&lt;unsafe&gt;&amp;literal', content)


if __name__ == '__main__': unittest.main()
