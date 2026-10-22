"""Radio state and control must never select an unrelated phone call."""
import copy
import tempfile
import unittest
from unittest.mock import patch

from test_phone_gateway import config, gateway, FakeAMI


class MediaAMI(FakeAMI):
    markers = {}
    snapshots = []
    supported = {'701', '708'}
    support_values = []

    def snapshot(self):
        if self.snapshots:
            return copy.deepcopy(self.snapshots.pop(0))
        return super().snapshot()

    def action(self, name, **values):
        self.calls.append((name, values))
        if name == 'Getvar':
            if values['Variable'].startswith('DIALPLAN_EXISTS('):
                source = values['Variable'].split(',')[1]
                value = (self.support_values.pop(0) if self.support_values else
                         '1' if source in self.supported else '0')
                return [{'Response': 'Success', 'Variable': values['Variable'], 'Value': value}]
            return [{'Response': 'Success', 'Variable': 'CISCO_RADIO',
                     'Value': self.markers.get(values['Channel'], '')}]
        return [{'Response': 'Success'}]


def radio(line='201', source='701', uid='123.4', bridge='bridge-one'):
    channel = {'Channel': f'PJSIP/{line}-abc', 'Uniqueid': uid, 'Linkedid': uid,
               'Context': 'home', 'Exten': source, 'Application': 'AGI',
               'ApplicationData': 'radio.py,matryoshka', 'ChannelStateDesc': 'Up',
               'BridgeId': bridge}
    audio = {'Channel': 'AudioSocket/127.0.0.1:32000-12345678-1234-1234-1234-123456789abc',
             'Uniqueid': '123.5', 'Linkedid': uid, 'ChannelStateDesc': 'Up', 'BridgeId': bridge}
    return channel, audio


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        value = config()
        value['dial_allowlist'] += ['708']
        value['services'] = [
            {'number': '701', 'label': 'Radio One', 'kind': 'radio'},
            {'number': '708', 'label': 'Radio Two', 'kind': 'radio'},
            {'number': '600', 'label': 'Echo test', 'kind': 'diagnostic'}]
        self.server = gateway.Gateway(value, self.directory.name)
        self.addCleanup(self.server.db.close)
        self.server.dnd_pending.clear()
        MediaAMI.channels, MediaAMI.calls, MediaAMI.markers, MediaAMI.snapshots = [], [], {}, []
        MediaAMI.supported, MediaAMI.support_values = {'701', '708'}, []
        MediaAMI.endpoints = {'201': 'Not in use', '202': 'Not in use'}
        self.patch_ami = patch.object(gateway, 'AMI', MediaAMI)
        self.patch_ami.start()
        self.addCleanup(self.patch_ami.stop)
        self.patch_execute = patch.object(gateway, 'execute')
        self.execute = self.patch_execute.start()
        self.addCleanup(self.patch_execute.stop)

    def install_radio(self, **kwargs):
        channel, audio = radio(**kwargs)
        MediaAMI.channels = [channel, audio]
        MediaAMI.markers[channel['Channel']] = f"1:{channel['Exten']}:{channel['Uniqueid']}"
        MediaAMI.endpoints[channel['Channel'].split('/')[1].split('-')[0]] = 'In use'
        return channel, audio

    def media(self):
        return self.server.listing()['phones'][0]['media']

    def request(self, action, **body):
        return self.server.mutate('201', action, {'idempotency_key': 'media-test-0001', **body})

    def assert_blank(self, state, reason):
        self.assertEqual(self.media(), {'state': state, 'reason': reason,
                                      'source': None, 'line': None, 'channel': None, 'uniqueid': None})

    def test_idle_and_capability(self):
        self.assert_blank('idle', None)
        self.assertIn('media', self.server.listing()['phones'][0]['capabilities'])

    def test_old_pbx_idle_never_advertises_or_starts_unstoppable_media(self):
        MediaAMI.supported.clear()
        phone = self.server.listing()['phones'][0]
        self.assertNotIn('media', phone['capabilities'])
        self.assert_blank('unknown', 'unavailable')
        with patch.object(self.server, 'suspend_hands_free') as suspend, \
                patch.object(self.server, 'reserve_phone_controls') as reserve:
            with self.assertRaises(gateway.Refused) as caught:
                self.request('media_play', line='201', source='701')
            self.assertEqual(caught.exception.reason, 'media_unavailable')
            suspend.assert_not_called()
            reserve.assert_not_called()
        self.execute.assert_not_called()
        self.assertTrue(all(name == 'Getvar' for name, _ in MediaAMI.calls))

    def test_every_advertised_source_needs_current_loaded_route_support(self):
        MediaAMI.supported.remove('708')
        phone = self.server.listing()['phones'][0]
        self.assertNotIn('media', phone['capabilities'])
        self.assert_blank('unknown', 'unavailable')
        MediaAMI.supported.add('708')
        self.assertIn('media', self.server.listing()['phones'][0]['capabilities'])

    def test_marked_radio_remains_stoppable_after_dialplan_rollback(self):
        for bridge, expected in (('bridge-one', 'playing'), ('', 'buffering')):
            with self.subTest(state=expected):
                channel, _ = self.install_radio(bridge=bridge)
                MediaAMI.supported.clear()
                phone = self.server.listing()['phones'][0]
                self.assertEqual(phone['media']['state'], expected)
                self.assertIn('media', phone['capabilities'])
                identity = {key: phone['media'][key] for key in ('source', 'line', 'channel', 'uniqueid')}
                self.server.mutate('201', 'media_stop', {
                    **identity, 'idempotency_key': 'rollback-stop-' + expected})
                self.assertEqual(MediaAMI.calls[-1], ('Hangup', {'Channel': channel['Uniqueid'], 'Cause': '16'}))
                MediaAMI.channels = []
                MediaAMI.endpoints = {'201': 'Not in use', '202': 'Not in use'}
                phone = self.server.listing()['phones'][0]
                self.assertEqual(phone['media']['state'], 'unknown')
                self.assertEqual(phone['media']['reason'], 'unavailable')
                self.assertNotIn('media', phone['capabilities'])

    def test_route_support_lost_during_handoff_refuses_phone_io(self):
        MediaAMI.support_values = ['1', '0']
        with self.assertRaises(gateway.Refused) as caught:
            self.request('media_play', line='201', source='701')
        self.assertEqual(caught.exception.reason, 'media_unavailable')
        self.execute.assert_not_called()
        self.assertEqual(len(MediaAMI.calls), 2)

    def test_route_support_requires_exact_success_variable_and_value(self):
        variable = 'DIALPLAN_EXISTS(home,701,cisco-radio-v1)'
        for response in ([], [{'Response': 'Success', 'Variable': variable, 'Value': ''}],
                         [{'Response': 'Success', 'Variable': variable, 'Value': '01'}],
                         [{'Response': 'Success', 'Variable': variable, 'Value': 1}],
                         [{'Response': 'Success', 'Variable': 'other', 'Value': '1'}],
                         [{'Response': 'Error', 'Variable': variable, 'Value': '1'}]):
            with self.subTest(response=response), \
                    patch.object(MediaAMI, 'action', return_value=response), \
                    patch.object(self.server, 'suspend_hands_free') as suspend:
                with self.assertRaises(gateway.Refused):
                    self.request('media_play', line='201', source='701')
                suspend.assert_not_called()
        self.execute.assert_not_called()

    def test_offline_then_online_is_not_optimistically_playing(self):
        MediaAMI.endpoints['201'] = 'Unavailable'
        self.assert_blank('blocked', 'offline')
        MediaAMI.endpoints['201'] = 'Not in use'
        self.assert_blank('idle', None)

    def test_dnd_blocks_idle_media(self):
        self.server.db.execute('UPDATE preferences SET dnd=1 WHERE phone=?', ('201',))
        self.assert_blank('blocked', 'dnd')

    def test_busy_endpoint_without_channel_is_not_idle(self):
        for state in ('In use', 'Ringing', 'Busy', 'Ring+Inuse', 'On Hold'):
            with self.subTest(state=state):
                MediaAMI.endpoints['202'] = state
                self.assert_blank('blocked', 'call')

    def test_unknown_endpoint_without_channel_is_not_a_known_call(self):
        MediaAMI.endpoints['202'] = 'Unknown'
        self.assert_blank('unknown', 'unavailable')

    def test_ordinary_and_voice_calls_are_not_music(self):
        channel, _ = self.install_radio()
        channel['Exten'] = '600'
        self.assert_blank('blocked', 'call')
        channel['Linkedid'] = 'cisco-voice-' + 'a' * 32
        self.assert_blank('blocked', 'voice')
        self.assertFalse(any(name == 'Getvar' and values.get('Variable') == 'CISCO_RADIO'
                             for name, values in MediaAMI.calls))

    def test_manually_dialled_radio_on_either_line_is_observed(self):
        for line in ('201', '202'):
            with self.subTest(line=line):
                channel, _ = self.install_radio(line=line, source='708')
                self.assertEqual(self.media(), {'state': 'playing', 'reason': None, 'source': '708',
                    'line': line, 'channel': channel['Channel'], 'uniqueid': channel['Uniqueid']})
        self.execute.assert_not_called()

    def test_marked_call_without_bridge_is_buffering(self):
        channel, _ = self.install_radio(bridge='')
        MediaAMI.channels = [channel]
        self.assertEqual(self.media()['state'], 'buffering')

    def test_up_phone_alone_is_not_playing(self):
        channel, _ = self.install_radio()
        MediaAMI.channels = [channel]
        self.assert_blank('unknown', 'ambiguous')

    def test_foreign_disconnected_or_different_bridge_audio_is_not_playing(self):
        for field, value in (('Linkedid', '999.1'), ('ChannelStateDesc', 'Down'),
                             ('BridgeId', 'foreign-bridge'), ('Channel', 'PJSIP/999-abc')):
            with self.subTest(field=field):
                _, audio = self.install_radio()
                audio[field] = value
                self.assert_blank('unknown', 'ambiguous')

    def test_extra_bridge_member_and_two_handset_calls_are_ambiguous(self):
        channel, audio = self.install_radio()
        MediaAMI.channels.append({**audio, 'Channel': 'PJSIP/999-cde'})
        self.assert_blank('unknown', 'ambiguous')
        MediaAMI.channels = [channel, audio, {**channel, 'Channel': 'PJSIP/202-bcd'}]
        self.assert_blank('unknown', 'ambiguous')

    def test_unsupported_old_pbx_does_not_identify_radio(self):
        self.install_radio()
        MediaAMI.markers.clear()
        self.assert_blank('unknown', 'unmarked')

    def test_marker_uid_source_or_version_mismatch_refuses_identity(self):
        channel, _ = self.install_radio()
        for marker in ('1:701:999.4', '1:708:123.4', '2:701:123.4', None, 'x' * 300):
            with self.subTest(marker=marker):
                MediaAMI.markers[channel['Channel']] = marker
                self.assert_blank('unknown', 'unavailable')

    def test_returned_agi_marker_is_not_active_radio(self):
        channel, _ = self.install_radio()
        channel['Application'] = 'PlayTones'
        self.assert_blank('unknown', 'unavailable')

    def test_context_and_service_kind_are_required(self):
        channel, _ = self.install_radio()
        channel['Context'] = 'foreign'
        self.assert_blank('blocked', 'call')
        channel['Context'], channel['Exten'] = 'home', '600'
        self.assert_blank('blocked', 'call')

    def test_play_uses_selected_line_and_is_idempotent(self):
        first = self.request('media_play', line='202', source='708')
        again = self.request('media_play', line='202', source='708')
        self.assertEqual(first, again)
        self.assertEqual(first['source'], '708')
        self.assertEqual([c.args[1] for c in self.execute.call_args_list], ['Key:Line2', 'Dial:708'])

    def test_invalid_play_input_never_suspends_voice_or_dials(self):
        invalid = [{'line': '201', 'source': '600'}, {'line': '201', 'source': 'https://example.org/a.mp3'},
                   {'line': '999', 'source': '701'}, {'line': '201', 'source': '701', 'url': 'x'},
                   {'source': '701'}, {'line': '201', 'source': ['701']}]
        with patch.object(self.server, 'suspend_hands_free') as suspend:
            for body in invalid:
                with self.subTest(body=body), self.assertRaises(gateway.Refused):
                    self.request('media_play', **body)
            suspend.assert_not_called()
        self.execute.assert_not_called()

    def test_dnd_busy_and_source_replacement_do_not_dial(self):
        self.server.db.execute('UPDATE preferences SET dnd=1 WHERE phone=?', ('201',))
        with self.assertRaises(gateway.Refused):
            self.request('media_play', line='201', source='701')
        self.server.db.execute('UPDATE preferences SET dnd=0 WHERE phone=?', ('201',))
        self.install_radio(source='708')
        with self.assertRaises(gateway.Refused):
            self.request('media_play', line='201', source='701')
        self.execute.assert_not_called()

    def test_offline_sibling_and_pending_dnd_do_not_dial(self):
        for state in ('Unavailable', 'Ringing', 'Unknown'):
            with self.subTest(state=state):
                MediaAMI.endpoints['202'] = state
                with self.assertRaises(gateway.Refused):
                    self.request('media_play', line='201', source='701')
        MediaAMI.endpoints['202'] = 'Not in use'
        self.server.dnd_pending.add('201')
        with self.assertRaises(gateway.Refused):
            self.request('media_play', line='201', source='701')
        self.execute.assert_not_called()

    def test_alert_and_finite_voice_prevent_play(self):
        self.server.db.execute('INSERT INTO alerts VALUES (?, ?, ?, ?, ?, NULL, ?, 0, 0)',
                               ('a' * 32, '201', '201', 'timer', 'starting', gateway.time.time()))
        with self.assertRaises(gateway.Refused):
            self.request('media_play', line='201', source='701')
        self.server.db.execute("UPDATE alerts SET state='ended'")
        with patch.object(self.server, 'reserve_phone_controls', side_effect=gateway.Refused('voice_phone_busy')):
            with self.assertRaises(gateway.Refused):
                self.request('media_play', line='201', source='701')
        self.execute.assert_not_called()

    def test_stop_requires_previous_exact_radio_identity(self):
        channel, _ = self.install_radio(line='202')
        body = {'line': '202', 'source': '701', 'channel': channel['Channel'], 'uniqueid': channel['Uniqueid']}
        self.assertEqual(self.request('media_stop', **body)['status'], 'accepted')
        self.assertEqual([c for c in MediaAMI.calls if c[0] == 'Hangup'],
                         [('Hangup', {'Channel': channel['Uniqueid'], 'Cause': '16'})])
        self.request('media_stop', **body)
        self.assertEqual(len([c for c in MediaAMI.calls if c[0] == 'Hangup']), 1)

    def test_stop_cannot_resolve_replacement_ordinary_or_radio_call(self):
        for source in ('600', '701'):
            with self.subTest(source=source):
                channel, _ = self.install_radio(source=source, uid='999.1')
                with self.assertRaises(gateway.Refused):
                    self.request('media_stop', line='201', source='701', channel=channel['Channel'], uniqueid='123.4')
        self.assertFalse(any(name == 'Hangup' for name, _ in MediaAMI.calls))

    def test_replacement_between_marker_read_and_final_snapshot_refuses_stop(self):
        channel, audio = self.install_radio()
        replacement = {**channel, 'Uniqueid': '999.1', 'Linkedid': '999.1'}
        MediaAMI.snapshots = [(MediaAMI.endpoints, [channel, audio]),
                              (MediaAMI.endpoints, [replacement, audio])]
        with self.assertRaises(gateway.Refused):
            self.request('media_stop', line='201', source='701', channel=channel['Channel'], uniqueid='123.4')
        self.assertFalse(any(name == 'Hangup' for name, _ in MediaAMI.calls))

    def test_old_unmarked_pbx_does_not_authorize_stop(self):
        channel, _ = self.install_radio()
        MediaAMI.markers.clear()
        with self.assertRaises(gateway.Refused):
            self.request('media_stop', line='201', source='701', channel=channel['Channel'], uniqueid='123.4')
        self.assertFalse(any(name == 'Hangup' for name, _ in MediaAMI.calls))

    def test_getvar_failure_is_not_a_fresh_success(self):
        self.install_radio()
        with patch.object(MediaAMI, 'action', side_effect=gateway.Refused('ami_timeout', 503)):
            with self.assertRaises(gateway.Refused):
                self.media()


if __name__ == '__main__':
    unittest.main()
