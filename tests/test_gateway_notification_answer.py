"""Automatic speech answers only its owned call; all I/O is simulated."""
import copy
import tempfile
import unittest
from unittest.mock import patch

from test_phone_gateway import FakeAMI, config, gateway


class NotificationAnswerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.server = gateway.Gateway(config(), directory.name)
        self.addCleanup(self.server.db.close)
        self.server.dnd_pending.clear()
        self.root = 'announce-' + 'a' * 32 + '-202'
        self.body = {'idempotency_key': self.root, 'line': '202',
                     'channel': 'PJSIP/202-000000aa', 'uniqueid': '1234.56'}
        FakeAMI.channels = [{'Channel': self.body['channel'], 'Uniqueid': self.body['uniqueid'],
                             'Linkedid': self.root, 'ChannelStateDesc': 'Ringing'}]
        FakeAMI.endpoints = {'201': 'Not in use', '202': 'Ringing'}
        FakeAMI.calls = []
        ami = patch.object(gateway, 'AMI', FakeAMI)
        ami.start()
        self.addCleanup(ami.stop)
        execute = patch.object(gateway, 'execute')
        self.sent = execute.start()
        self.addCleanup(execute.stop)
        suspend = patch.object(self.server, 'suspend_hands_free',
                               side_effect=AssertionError('automatic answer must not suspend voice'))
        suspend.start()
        self.addCleanup(suspend.stop)

    def answer(self):
        return self.server.answer_notification('201', self.body)

    def refused(self, reason):
        with self.assertRaises(gateway.Refused) as caught:
            self.answer()
        self.assertEqual(caught.exception.reason, reason)
        self.sent.assert_not_called()

    def test_owned_incoming_call_selects_line_once_and_preserves_preferences(self):
        before = self.server.preference('201')
        result = self.answer()
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['action_id'], self.root)
        self.sent.assert_called_once_with(self.server.phones['201'], 'Key:Line2')
        FakeAMI.channels = []  # A retry after the call ended is only a receipt read.
        self.assertEqual(self.answer(), result)
        self.sent.assert_called_once()
        self.assertEqual(self.server.preference('201'), before)

    def test_ambiguous_phone_write_is_not_repeated(self):
        self.sent.side_effect = OSError('simulated transport uncertainty')
        with self.assertRaises(OSError):
            self.answer()
        with self.assertRaises(gateway.Refused) as caught:
            self.answer()
        self.assertEqual(caught.exception.reason, 'previous_action_outcome_unknown')
        self.sent.assert_called_once()

    def test_reused_channel_name_with_another_uniqueid_is_refused(self):
        FakeAMI.channels[0]['Uniqueid'] = '1234.57'
        self.refused('channel_identity_changed')

    def test_same_channel_and_uniqueid_without_job_ownership_is_refused(self):
        FakeAMI.channels[0]['Linkedid'] = 'another-call'
        self.refused('notification_call_not_owned')

    def test_second_sibling_call_blocks_answer(self):
        FakeAMI.channels.append({'Channel': 'PJSIP/201-000000bb', 'Uniqueid': '1234.57',
                                 'Linkedid': '1234.57', 'ChannelStateDesc': 'Up'})
        self.refused('exactly_one_selected_phone_channel_required')

    def test_already_answered_or_no_longer_ringing_is_refused(self):
        FakeAMI.channels[0]['ChannelStateDesc'] = 'Up'
        self.refused('phone_not_ringing')

    def test_dnd_enabled_after_dispatch_blocks_answer(self):
        self.server.db.execute('UPDATE preferences SET dnd=1 WHERE phone=?', ('201',))
        self.server.db.commit()
        self.refused('do_not_disturb')

    def test_dnd_reconciliation_pending_blocks_answer(self):
        self.server.dnd_pending.add('201')
        self.refused('do_not_disturb')

    def test_active_alert_blocks_answer(self):
        with patch.object(self.server, 'alerts', return_value=[{'phone': '201', 'state': 'ringing'}]), \
                patch.object(self.server, 'reconcile_alerts'):
            self.refused('alert_already_active')

    def test_offline_sibling_blocks_answer(self):
        FakeAMI.endpoints['201'] = 'Unavailable'
        self.refused('phone_unavailable')

    def test_stopping_gateway_does_not_answer(self):
        self.server.stopped.set()
        self.refused('notification_gateway_stopping')

    def test_malformed_or_uncorrelated_request_cannot_write(self):
        valid = copy.deepcopy(self.body)
        for changed in ({'line': '999'}, {'idempotency_key': 'arbitrary-valid-key'},
                        {'channel': 'PJSIP/201-000000aa'}, {'uniqueid': ''},
                        {'extra': True}):
            with self.subTest(changed=changed):
                self.body = dict(valid, **changed)
                with self.assertRaises(gateway.Refused):
                    self.answer()
        self.sent.assert_not_called()

    def test_internal_answer_is_not_a_public_mutation(self):
        with self.assertRaises(gateway.Refused) as caught:
            self.server.mutate('201', 'notification_answer', self.body)
        self.assertEqual(caught.exception.reason, 'unknown_action')
        self.sent.assert_not_called()
