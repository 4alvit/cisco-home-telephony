"""Synthetic AMI races must never hang up a replacement channel."""
import copy
import tempfile
import unittest
from unittest.mock import patch

from test_phone_gateway import config, gateway


class ReusingAMI:
    def __init__(self, linkedid, *, reuse=True):
        self.channel = {'Channel': 'PJSIP/201-abc', 'Uniqueid': '123.1',
                        'Linkedid': linkedid, 'ChannelStateDesc': 'Up'}
        self.hangups = []
        self.requests = []
        self.reuse = reuse

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def snapshot(self):
        return ({'201': 'In use', '202': 'Not in use'},
                [copy.deepcopy(self.channel)] if self.channel else [])

    def action(self, name, **fields):
        if name != 'Hangup':
            return [{'Response': 'Success'}]
        # The original call ends just after the last snapshot. Asterisk can
        # resolve either a channel name or Uniqueid, but only the latter still
        # identifies the caller whose ownership was checked.
        if self.reuse:
            self.channel.update(Uniqueid='123.2', Linkedid='123.2')
        target = fields['Channel']
        self.requests.append(target)
        if target in (self.channel['Channel'], self.channel['Uniqueid']):
            self.hangups.append(self.channel['Uniqueid'])
            self.channel = None
            return [{'Response': 'Success'}]
        raise gateway.Refused('ami_action_rejected', 503)


class HangupIdentityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.server = gateway.Gateway(config(), directory.name)
        self.addCleanup(self.server.db.close)

    def test_manual_hangup_does_not_follow_a_reused_channel_name(self):
        ami = ReusingAMI('123.1')
        body = {'idempotency_key': 'hangup-race-123', 'line': '201',
                'channel': 'PJSIP/201-abc', 'uniqueid': '123.1'}
        with patch.object(gateway, 'AMI', return_value=ami):
            with self.assertRaisesRegex(gateway.Refused, 'ami_action_rejected'):
                self.server.mutate('201', 'hangup', body)
            with self.assertRaisesRegex(gateway.Refused, 'previous_action_outcome_unknown'):
                self.server.mutate('201', 'hangup', body)
        self.assert_replacement_survives(ami)

    def assert_replacement_survives(self, ami):
        self.assertEqual(ami.requests, ['123.1'])
        self.assertEqual(ami.hangups, [])
        self.assertEqual(ami.channel['Uniqueid'], '123.2')

    def test_alert_cleanup_does_not_follow_a_reused_channel_name(self):
        alert = {'id': 'a' * 32, 'line': '201'}
        ami = ReusingAMI('cisco-alert-' + alert['id'] + '-a')
        self.server.stop_alert_channels(ami, alert)
        self.assert_replacement_survives(ami)

    def test_intercom_cleanup_does_not_follow_a_reused_channel_name(self):
        root = 'cisco-intercom-' + 'a' * 32
        ami = ReusingAMI(root)
        self.server.stop_intercom(ami, root)
        self.assert_replacement_survives(ami)

    def test_original_call_still_ends_by_exact_uniqueid(self):
        alert = {'id': 'a' * 32, 'line': '201'}
        for action, linkedid in (('hangup', '123.1'),
                                 ('alert', 'cisco-alert-' + alert['id'] + '-a'),
                                 ('intercom', 'cisco-intercom-' + 'a' * 32)):
            with self.subTest(action=action):
                ami = ReusingAMI(linkedid, reuse=False)
                if action == 'hangup':
                    with patch.object(gateway, 'AMI', return_value=ami):
                        self.server.mutate('201', 'hangup', {
                            'idempotency_key': 'hangup-exact-123', 'line': '201',
                            'channel': 'PJSIP/201-abc', 'uniqueid': '123.1'})
                elif action == 'alert':
                    self.server.stop_alert_channels(ami, alert)
                else:
                    self.server.stop_intercom(ami, linkedid)
                self.assertEqual(ami.requests, ['123.1'])
                self.assertEqual(ami.hangups, ['123.1'])
                self.assertIsNone(ami.channel)

    def test_intercom_cleanup_refuses_missing_uniqueid(self):
        root = 'cisco-intercom-' + 'a' * 32
        ami = ReusingAMI(root)
        ami.channel.pop('Uniqueid')
        with self.assertRaisesRegex(gateway.Refused, 'intercom_channel_identity_missing'):
            self.server.stop_intercom(ami, root)
        self.assertEqual(ami.requests, [])


if __name__ == '__main__':
    unittest.main()
