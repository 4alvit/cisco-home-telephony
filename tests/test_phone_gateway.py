import base64
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from PIL import Image

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

    def test_rooms_and_softphones_are_public_metadata_only(self):
        self.server.config['phones'][0]['room'] = 'Living Room'
        self.server.config['softphones'] = [{'extension': '209', 'room': "Operator's Mac"}, {'extension': '210'}]
        gateway.validate(self.server.config)
        FakeAMI.endpoints['209'] = 'Not in use'
        listing = self.server.listing()
        self.assertEqual(listing['phones'][0]['room'], 'Living Room')
        self.assertEqual(listing['softphones'], [
            {'extension': '209', 'room': "Operator's Mac", 'online': True},
            {'extension': '210', 'room': None, 'online': False}])
        self.assertEqual(set(listing['phones'][0]), {
            'id', 'lines', 'room', 'dnd', 'autoanswer', 'dnd_pending', 'alert', 'online', 'channels', 'capabilities',
            'motion_image', 'media'})
        self.assertEqual(listing['phones'][0]['motion_image'], {'eligible': True, 'reason': None})
        encoded = json.dumps(listing)
        for private in ('a' * 48, 'b' * 48, 'u' * 24, 'p' * 24, 'c' * 64,
                        '192.168.50.', 'SEP001122334455'):
            self.assertNotIn(private, encoded)
        for state in ('In use', 'Unavailable', 'Invalid', None):
            FakeAMI.endpoints['209'] = state
            with self.subTest(state=state):
                self.assertEqual(self.server.listing()['softphones'][0]['online'], state == 'In use')
        with self.assertRaises(gateway.Refused) as caught:
            self.server.mutate('209', 'dial', {'number': '600', 'idempotency_key': 'metadata-only-209'})
        self.assertEqual(caught.exception.reason, 'unknown_phone')
        self.send.assert_not_called()

    def test_missing_and_null_gateway_rooms_are_unassigned_without_config_mutation(self):
        before = copy.deepcopy(self.server.config)
        self.assertEqual(self.server.listing()['softphones'], [])
        self.assertIsNone(self.server.listing()['phones'][0]['room'])
        self.assertEqual(self.server.config, before)
        self.server.config['phones'][0]['room'] = None
        self.server.config['softphones'] = [{'extension': '209', 'room': None}]
        gateway.validate(self.server.config)
        self.assertIsNone(self.server.listing()['phones'][0]['room'])
        self.assertIsNone(self.server.listing()['softphones'][0]['room'])

    def test_room_metadata_validation_rejects_unsafe_or_ambiguous_values(self):
        for room in (False, 42, [], {}, '', ' Room', 'Room ', 'Room\n', 'Room\t', 'Room;value=x',
                     'Room/Name', 'Room\\Name', '"Room"', 'R\u00f6om', '_Room', 'A' * 49):
            for kind in ('phone', 'softphone'):
                with self.subTest(room=room, kind=kind):
                    value = config()
                    if kind == 'phone':
                        value['phones'][0]['room'] = room
                    else:
                        value['softphones'] = [{'extension': '209', 'room': room}]
                    with self.assertRaises(gateway.Refused) as caught:
                        gateway.validate(value)
                    self.assertEqual(caught.exception.reason, 'bad_room')
        for room in ('1', 'A' * 48, "Kid's Room (South)_2-3"):
            value = config(); value['phones'][0]['room'] = room
            self.assertIs(gateway.validate(value), value)

    def test_softphone_metadata_has_unique_valid_extensions_and_no_private_fields(self):
        for clients in (None, {}, '209', [{'extension': 209}], [{'extension': '620'}],
                        [{'extension': '202'}], [{'extension': '209'}, {'extension': '209'}],
                        [{'extension': '209', 'password': 'secret'}],
                        [{'extension': '209', 'source_ipv4': '192.168.50.90'}],
                        [{'room': 'Mac'}], [None], [{'extension': '209'}] * 51):
            with self.subTest(clients=clients):
                value = config(); value['softphones'] = clients
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

    def test_preflight_refusal_releases_the_key_for_a_same_request_retry(self):
        FakeAMI.endpoints = {'201': 'Unavailable', '202': 'Unavailable'}
        with self.assertRaisesRegex(gateway.Refused, 'phone_unavailable'):
            self.request('dial', number='600')
        FakeAMI.endpoints = {'201': 'Not in use', '202': 'Not in use'}
        result = self.request('dial', number='600')
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(self.send.call_count, 2)

    def test_ambiguous_transport_failure_still_keeps_the_key_claimed(self):
        self.send.side_effect = OSError('connection interrupted')
        with self.assertRaises(OSError):
            self.request('dial', number='600')
        with self.assertRaisesRegex(gateway.Refused, 'previous_action_outcome_unknown'):
            self.request('dial', number='600')
        self.assertEqual(self.send.call_count, 1)

    def test_motion_admission_reports_unexpected_failures_without_leaking_details(self):
        stream = io.StringIO()
        with patch.object(self.server, 'reserve_motion_image',
                          side_effect=RuntimeError('storage offline')):
            with patch('sys.stderr', stream):
                value = self.server.motion_image_admission(self.server.phones['201'])
        self.assertEqual(value, {'eligible': False, 'reason': 'voice_state_unavailable'})
        self.assertIn('RuntimeError', stream.getvalue())
        self.assertNotIn('storage offline', stream.getvalue())

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

    def test_message_is_plain_text_and_foregrounds_once_on_idempotent_replay(self):
        result = self.request('message', text='<unsafe>&literal', title='Private')
        self.assertEqual(result['status'], 'accepted')
        call = self.send.call_args
        self.assertTrue(call.args[1].startswith('http://192.168.50.10:18080/content/'))
        self.assertEqual(call.kwargs, {'priority': 0})
        content = next(iter(self.server.resources.values()))[1]
        self.assertIn(b'&lt;unsafe&gt;&amp;literal', content)
        document = ET.fromstring(content)
        self.assertEqual(document.tag, 'CiscoIPPhoneText')
        self.assertEqual(document.attrib, {})
        self.assertEqual([child.tag for child in document], ['Title', 'Prompt', 'Text', 'SoftKeyItem'])
        self.assertEqual(document.findtext('Title'), 'Private')
        self.assertEqual(document.findtext('Text'), '<unsafe>&literal')
        self.assertEqual(document.findtext('SoftKeyItem/URL'), 'SoftKey:Exit')
        resources = dict(self.server.resources)
        self.assertEqual(self.request('message', text='<unsafe>&literal', title='Private'), result)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.server.resources, resources)
        self.assertEqual(FakeAMI.calls, [])

    def test_notification_titles_fit_handset_schema_at_api_boundaries(self):
        for action in ('message', 'image'):
            for length in (32, 33, 48):
                with self.subTest(action=action, length=length):
                    title = 'A' * (length - 1) + '&'
                    body = ({'title': title, 'text': 'Body remains complete.'}
                            if action == 'message' else
                            self.image_request_body('title-test', title, (0, 0, 0)))
                    self.server.resources.clear()
                    self.server.notify(self.server.phones['201'], action, body)
                    pages = [value[1] for value in self.server.resources.values()
                             if value[2].startswith('text/xml')]
                    self.assertEqual(len(pages), 1)
                    document = ET.fromstring(pages[0])
                    self.assertEqual(document.findtext('Title'), title[:32])
                    self.assertLessEqual(len(document.findtext('Prompt')), 32)
                    if action == 'message':
                        self.assertEqual(document.findtext('Text'), body['text'])

    def test_foreground_message_still_rejects_a_call_on_either_line(self):
        for line in ('201', '202'):
            with self.subTest(line=line):
                FakeAMI.channels = [{'Channel': f'PJSIP/{line}-abc', 'Uniqueid': '123.4',
                                     'ChannelStateDesc': 'Up'}]
                with self.assertRaisesRegex(gateway.Refused, 'phone_busy'):
                    self.request('message', text='A manual message')
                self.send.assert_not_called()
                self.assertEqual(self.server.resources, {})
                self.assertEqual(FakeAMI.calls, [])

    def test_ambiguous_foreground_message_failure_never_resends(self):
        self.send.side_effect = OSError('response lost')
        with self.assertRaises(OSError):
            self.request('message', text='A manual message')
        with self.assertRaisesRegex(gateway.Refused, 'previous_action_outcome_unknown'):
            self.request('message', text='A manual message')
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.send.call_args.kwargs, {'priority': 0})
        self.assertEqual(FakeAMI.calls, [])

    def image_request_body(self, key, title, color):
        with Image.new('RGB', (3, 2), color) as image:
            output = io.BytesIO()
            image.save(output, format='PNG')
        return {'idempotency_key': key, 'title': title,
                'image_base64': base64.b64encode(output.getvalue()).decode('ascii')}

    def test_consecutive_images_keep_distinct_immutable_resources_and_idle_only_pushes(self):
        # Exercise real image decoding and resources, but not firmware rendering.
        samples = [('snapshot-first', 'Motion A <entry>', (255, 0, 0)),
                   ('snapshot-second', 'Motion B & garden', (0, 0, 255))]
        previous_resources = {}
        all_urls = []
        for index, (key, title, color) in enumerate(samples, start=1):
            with self.subTest(snapshot=key):
                body = self.image_request_body(key, title, color)
                result = self.server.mutate('201', 'image', body)
                self.assertEqual(result['status'], 'accepted')
                self.assertEqual(result['action_id'], key)
                self.assertEqual(result['resource_ttl_seconds'], 120)
                self.assertEqual(self.send.call_count, index)
                push = self.send.call_args
                self.assertEqual(push.args[0], self.server.phones['201'])
                self.assertEqual(push.kwargs, {'priority': 2})
                xml_url = gateway.parse.urlsplit(push.args[1])
                self.assertEqual((xml_url.scheme, xml_url.netloc),
                                 ('http', '192.168.50.10:18080'))
                self.assertTrue(xml_url.path.startswith('/content/'))
                self.assertFalse(xml_url.query or xml_url.fragment)
                xml_resource = self.server.resources[('201', xml_url.path)]
                self.assertEqual(xml_resource[2], 'text/xml; charset=utf-8')
                document = ET.fromstring(xml_resource[1])
                self.assertEqual(document.tag, 'CiscoIPPhoneImageFile')
                self.assertEqual(document.attrib, {})
                self.assertEqual(document.findtext('Title'), title)
                image_url = gateway.parse.urlsplit(document.findtext('URL'))
                self.assertEqual((image_url.scheme, image_url.netloc),
                                 (xml_url.scheme, xml_url.netloc))
                self.assertTrue(image_url.path.startswith('/content/'))
                self.assertFalse(image_url.query or image_url.fragment)
                image_resource = self.server.resources[('201', image_url.path)]
                self.assertEqual(image_resource[2], 'image/png')
                with Image.open(io.BytesIO(image_resource[1])) as decoded:
                    self.assertEqual(decoded.format, 'PNG')
                    self.assertEqual(decoded.mode, 'RGB')
                    self.assertLessEqual(decoded.width, 298)
                    self.assertLessEqual(decoded.height, 156)
                    self.assertEqual(decoded.getextrema(), tuple((v, v) for v in color))
                self.assertEqual(document.findtext('SoftKeyItem/Name'), 'Exit')
                self.assertEqual(document.findtext('SoftKeyItem/URL'), 'SoftKey:Exit')
                self.assertEqual(document.findtext('SoftKeyItem/Position'), '1')
                self.assertEqual(document.findall('.//ExecuteItem'), [])
                for resource_key, value in previous_resources.items():
                    self.assertEqual(self.server.resources[resource_key], value)
                self.assertEqual(len(self.server.resources), index * 2)
                previous_resources = dict(self.server.resources)
                all_urls.extend((push.args[1], document.findtext('URL')))
        self.assertEqual(len(set(all_urls)), 4)
        self.assertEqual(FakeAMI.calls, [])

    def test_idle_image_close_is_opt_in_and_scoped_to_a_unique_application(self):
        app_ids = []
        for seconds in (10, 120):
            with self.subTest(seconds=seconds):
                body = self.image_request_body(f'idle-close-{seconds}', 'Motion', (0, 0, 255))
                body['idle_close_seconds'] = seconds
                self.server.mutate('201', 'image', body)
                push = self.send.call_args
                self.assertEqual(push.kwargs, {'priority': 2})
                path = gateway.parse.urlsplit(push.args[1]).path
                document = ET.fromstring(self.server.resources[('201', path)][1])
                app_id = document.get('appId')
                self.assertRegex(app_id, r'^image_[0-9a-f]{32}$')
                self.assertEqual(document.attrib, {
                    'appId': app_id,
                    'onAppFocusGained': f'App:Close:1:{seconds}:{app_id}',
                })
                self.assertEqual(document.findall('.//ExecuteItem'), [])
                app_ids.append(app_id)
        self.assertEqual(len(set(app_ids)), 2)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(FakeAMI.calls, [])

    def test_motion_image_has_scoped_idle_close_and_idempotent_replay(self):
        self.server.dnd_pending.clear()
        body = self.image_request_body('motion-notification', 'Motion', (0, 0, 255))
        result = self.server.mutate('201', 'motion_image', body)
        push = self.send.call_args
        self.assertEqual(push.kwargs, {'priority': 2})
        path = gateway.parse.urlsplit(push.args[1]).path
        document = ET.fromstring(self.server.resources[('201', path)][1])
        app_id = document.get('appId')
        self.assertRegex(app_id, r'^image_[0-9a-f]{32}$')
        self.assertEqual(document.get('onAppFocusGained'), f'App:Close:1:45:{app_id}')
        self.assertEqual(self.server.mutate('201', 'motion_image', body), result)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(FakeAMI.calls, [])

    def test_motion_image_requires_45_second_idle_close(self):
        for value in (None, True, 44, 46, '45', 45.0):
            with self.subTest(value=value):
                body = self.image_request_body('bad-motion-close', 'Motion', (0, 0, 255))
                body['idle_close_seconds'] = value
                with self.assertRaises(gateway.Refused) as caught:
                    self.server.mutate('201', 'motion_image', body)
                self.assertEqual(caught.exception.reason, 'bad_idle_close_seconds')
        self.send.assert_not_called()

    def test_motion_image_rechecks_dnd_call_endpoint_and_alert_before_display(self):
        body = self.image_request_body('guard-motion', 'Motion', (0, 0, 255))
        for reason in ('dnd', 'dnd_pending', 'channel', 'endpoint', 'alert'):
            with self.subTest(reason=reason):
                self.server.db.execute('UPDATE preferences SET dnd=?', (reason == 'dnd',))
                self.server.dnd_pending = {'201'} if reason == 'dnd_pending' else set()
                FakeAMI.channels = ([{'Channel': 'PJSIP/202-abcd', 'Uniqueid': 'call'}]
                                    if reason == 'channel' else [])
                FakeAMI.endpoints['202'] = 'In use' if reason == 'endpoint' else 'Not in use'
                alerts = [{'phone': '201', 'state': 'ringing'}] if reason == 'alert' else []
                with patch.object(self.server, 'reconcile_alerts'), \
                     patch.object(self.server, 'alerts', return_value=alerts):
                    with self.assertRaises(gateway.Refused):
                        self.server.mutate('201', 'motion_image', body)
        self.send.assert_not_called()
        self.assertEqual(self.server.resources, {})

    def test_invalid_idle_image_close_is_rejected_before_any_effects(self):
        for seconds in (None, True, False, -1, 0, 5, 9, 121, 86400, 45.0, '45', [], {}):
            with self.subTest(seconds=seconds), patch.object(gateway, 'AMI') as ami:
                body = self.image_request_body('invalid-idle-close', 'Motion', (0, 0, 255))
                body['idle_close_seconds'] = seconds
                with self.assertRaises(gateway.Refused) as caught:
                    self.server.mutate('201', 'image', body)
                self.assertEqual(caught.exception.reason, 'bad_idle_close_seconds')
                self.assertEqual(caught.exception.status, 400)
                ami.assert_not_called()
                self.send.assert_not_called()
                self.assertEqual(self.server.resources, {})
                self.assertEqual(self.server.db.execute('SELECT count(*) FROM operations').fetchone()[0], 0)

    def test_idle_close_is_rejected_for_every_non_image_action(self):
        for action in ('dial', 'intercom', 'answer', 'hangup', 'speaker', 'message',
                       'dnd', 'autoanswer', 'ring', 'alert', 'alert_stop'):
            with self.subTest(action=action), patch.object(gateway, 'AMI') as ami:
                with self.assertRaises(gateway.Refused) as caught:
                    self.request(action, idle_close_seconds=45)
                self.assertEqual(caught.exception.reason, 'idle_close_image_only')
                self.assertEqual(caught.exception.status, 400)
                ami.assert_not_called()
                self.send.assert_not_called()
                self.assertEqual(self.server.resources, {})
                self.assertEqual(self.server.db.execute('SELECT count(*) FROM operations').fetchone()[0], 0)

    def test_replaying_idle_close_image_does_not_reschedule_or_replace_application(self):
        first = self.image_request_body('idle-close-first', 'First', (255, 0, 0))
        first['idle_close_seconds'] = 45
        result = self.server.mutate('201', 'image', first)
        second = self.image_request_body('idle-close-second', 'Second', (0, 0, 255))
        second['idle_close_seconds'] = 45
        self.server.mutate('201', 'image', second)
        resources, pushes = dict(self.server.resources), list(self.send.call_args_list)
        self.assertEqual(self.server.mutate('201', 'image', first), result)
        self.assertEqual(self.server.resources, resources)
        self.assertEqual(self.send.call_args_list, pushes)
        restarted = gateway.Gateway(config(), self.directory.name)
        self.addCleanup(restarted.db.close)
        self.assertEqual(restarted.mutate('201', 'image', first), result)
        self.assertEqual(restarted.resources, {})
        self.assertEqual(self.send.call_args_list, pushes)

    def test_replaying_accepted_image_after_newer_image_does_not_push_old_content(self):
        first_body = self.image_request_body('snapshot-first', 'First', (255, 0, 0))
        first_result = self.server.mutate('201', 'image', first_body)
        self.server.mutate('201', 'image',
                           self.image_request_body('snapshot-second', 'Second', (0, 0, 255)))
        resources = dict(self.server.resources)
        pushes = list(self.send.call_args_list)
        self.assertEqual(self.server.mutate('201', 'image', first_body), first_result)
        self.assertEqual(self.server.resources, resources)
        self.assertEqual(self.send.call_args_list, pushes)
        restarted = gateway.Gateway(config(), self.directory.name)
        self.addCleanup(restarted.db.close)
        self.assertEqual(restarted.mutate('201', 'image', first_body), first_result)
        self.assertEqual(restarted.resources, {})
        self.assertEqual(self.send.call_args_list, pushes)
        self.assertEqual(FakeAMI.calls, [])


if __name__ == '__main__': unittest.main()
