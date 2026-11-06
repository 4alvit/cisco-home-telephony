"""Hotline auto-answer fencing without changing manual answer or preferences."""
import unittest
from unittest.mock import Mock, patch

import test_phone_gateway as fixtures

FakeAMI, gateway = fixtures.FakeAMI, fixtures.gateway


class HotlineAutoanswerTests(unittest.TestCase):
    request = fixtures.GatewayTests.request
    def setUp(self):
        fixtures.GatewayTests.setUp(self)
        self.channel = {'Channel': 'PJSIP/202-abc', 'Uniqueid': '123.4',
                        'ChannelStateDesc': 'Ringing'}

    def marker_ami(self, marker='', identity='123.4'):
        ami = Mock()
        def action(name, **fields):
            if name == 'Getvar':
                variable = fields['Variable']
                return [{'Response': 'Success', 'Variable': variable,
                         'Value': identity if variable == 'UNIQUEID' else marker}]
            return [{'CoreStartupDate': 'date', 'CoreStartupTime': 'time'}]
        ami.action.side_effect = action
        ami.snapshot.return_value = ({'201': 'Not in use', '202': 'Ringing'}, [self.channel])
        ami.__enter__ = Mock(return_value=ami)
        ami.__exit__ = Mock(return_value=False)
        return ami

    def test_marker_blocks_autoanswer_and_only_confirmed_absence_allows_it(self):
        for marker in ('1', '0', None, 'unknown'):
            with self.subTest(marker=marker):
                self.assertFalse(self.server.automatic_answer_allowed(self.marker_ami(marker), self.channel))
        self.assertTrue(self.server.automatic_answer_allowed(self.marker_ami(), self.channel))

    def test_reused_channel_identity_and_incomplete_or_duplicate_ami_results_block(self):
        self.assertFalse(self.server.automatic_answer_allowed(self.marker_ami(identity='999.9'), self.channel))
        for rows in ([], [{'Response': 'Success'}],
                     [{'Response': 'Success', 'Variable': 'UNIQUEID', 'Value': '123.4'}] * 2):
            ami = Mock(); ami.action.return_value = rows
            with self.subTest(rows=rows):
                self.assertFalse(self.server.automatic_answer_allowed(ami, self.channel))
        ami = Mock(); ami.action.side_effect = gateway.Refused('ami_failed', 503)
        with self.assertRaises(gateway.Refused):
            self.server.automatic_answer_allowed(ami, self.channel)

    def test_background_skips_hotline_without_changing_preferences_or_manual_answer(self):
        self.server.db.execute("UPDATE preferences SET autoanswer=1 WHERE phone='201'")
        self.server.db.commit()
        self.server.dnd_pending.clear()
        self.server.last_pbx_start = ('date', 'time')
        with patch.object(gateway, 'AMI', return_value=self.marker_ami(marker='1')), \
             patch.object(self.server, 'stopped') as stopped:
            stopped.wait.side_effect = [False, False, True]
            self.server.background()
        self.send.assert_not_called()
        self.assertNotIn('123.4', self.server.auto_attempts)
        self.assertTrue(self.server.preference('201')['autoanswer'])
        FakeAMI.channels = [self.channel]
        self.request('answer', line='202', channel=self.channel['Channel'], uniqueid='123.4')
        self.send.assert_called_once_with(self.server.phones['201'], 'Key:Line2')
