"""Intercom smoke evidence and failure diagnostics without Docker or network."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/test-intercom-originate.py'
SPEC = importlib.util.spec_from_file_location('intercom_fixture_diagnostics', SCRIPT)
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


class Refused(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class IntercomFixtureTests(unittest.TestCase):
    def test_wrapped_failure_keeps_categories_and_discards_arbitrary_values(self):
        inner = Refused('ami_action_rejected')
        inner.ami_action, inner.ami_error = 'Originate', 'permission_denied'
        outer = Refused('intercom_not_connected_check_status')
        outer.__context__ = inner
        self.assertEqual(fixture.refusal_diagnostics(outer), [
            {'type': 'Refused', 'reason': 'intercom_not_connected_check_status'},
            {'type': 'Refused', 'reason': 'ami_action_rejected',
             'ami_action': 'Originate', 'ami_error': 'permission_denied'},
        ])
        inner.reason = 'private-token-' + 'x' * 10000
        inner.ami_action, inner.ami_error = {'private': 'secret'}, 'raw SIP secret'
        inner.__context__ = outer
        encoded = json.dumps(fixture.refusal_diagnostics(outer))
        self.assertNotIn('secret', encoded)
        self.assertNotIn('private', encoded)
        self.assertLess(len(encoded), 300)

    def test_old_redirect_and_retransmitted_invite_do_not_prove_new_redirect(self):
        peer = SimpleNamespace(lock=threading.RLock(), calls={
            'old': {'answer': None, 'redirect_sent': True},
            'new': {'answer': None, 'invites': 3},
        })
        evidence = fixture.redirect_evidence({'201': peer}, {'201': {'old'}}, [])
        self.assertEqual(evidence['201']['new_dialogs'], 1)
        self.assertEqual(evidence['201']['redirects_sent'], 0)
        with self.assertRaisesRegex(RuntimeError, 'before intended peer sent 302'):
            fixture.require_redirect_evidence(evidence, '201')
        peer.calls['new']['redirect_sent'] = True
        fixture.require_redirect_evidence(
            fixture.redirect_evidence({'201': peer}, {'201': {'old'}}, []), '201')

    def test_302_evidence_requires_successful_send(self):
        peer = fixture.Phone.__new__(fixture.Phone)
        peer.address, peer.redirect = '127.0.0.2', True
        peer.calls = {'new': {'answer': None}}
        message = 'INVITE sip:202@127.0.0.2 SIP/2.0\r\nCall-ID: new\r\n\r\n'
        with mock.patch.object(fixture.helper.Phone, 'reply', side_effect=OSError('send failed')):
            with self.assertRaises(OSError):
                peer.reply(message, ('127.0.0.1', 5060), '180 Ringing')
        self.assertFalse(peer.calls['new'].get('redirect_sent'))
        with mock.patch.object(fixture.helper.Phone, 'reply') as send:
            peer.reply(message, ('127.0.0.1', 5060), '180 Ringing')
        self.assertEqual(send.call_args.args[2], '302 Moved Temporarily')
        self.assertTrue(peer.calls['new']['redirect_sent'])

    def run_inside(self, *, early_failure=False, diagnostic_failure=False, duplicate_press=False):
        peers = {}
        channels = []
        server = mock.Mock()
        server.config = {'ami': {}}
        server.preference.return_value = {'dnd': False, 'autoanswer': False}
        server.is_intercom_channel.return_value = True
        server.db.execute.return_value.fetchall.return_value = []
        server.stop_intercom.side_effect = lambda *_: channels.clear()
        gateway = SimpleNamespace(Gateway=lambda *_: server, Refused=Refused,
                                  AMI=mock.MagicMock())
        gateway.AMI.return_value.__enter__.return_value.snapshot.return_value = (
            dict.fromkeys(('201', '202', '203', '204'), 'Not in use'), [])

        def phone(address):
            identity = '201' if address.endswith('.2') else '203'
            peer = SimpleNamespace(lock=threading.RLock(), calls={}, redirect=False,
                                   media_packets_sent=1, forward_packets=0, error=None,
                                   answer=lambda: None, close=mock.Mock())
            peers[identity] = peer
            return peer

        def mutate(_phone, _action, body):
            case = body['idempotency_key']
            if case.endswith('secondary-lines'):
                for identity, line in (('201', '202'), ('203', '204')):
                    gateway.execute({'id': identity}, 'Key:Speaker')
                    peers[identity].calls['connected'] = {'answer': 1}
                    channels.append({'Channel': 'PJSIP/' + line + '-0001', 'Linkedid': 'root',
                                     'ChannelStateDesc': 'Up', 'BridgeId': 'bridge'})
                return {'connection_state': 'connected', 'speaker_requested': True,
                        'intercom_id': 'root'}
            if case.endswith('source_redirect'):
                peers['201'].calls['source-redirect'] = {'answer': None, 'redirect_sent': True}
            elif not early_failure:
                gateway.execute({'id': '201'}, 'Key:Speaker')
                if duplicate_press:
                    gateway.execute({'id': '201'}, 'Key:Speaker')
                peers['201'].calls['destination-source'] = {'answer': 2}
                peers['203'].calls['destination-redirect'] = {'answer': None, 'redirect_sent': True}
            error = Refused('intercom_not_connected_check_status')
            error.__context__ = Refused('intercom_call_changed')
            raise error

        server.mutate.side_effect = mutate
        observer = mock.Mock()
        observer.action.return_value = [{'AsteriskVersion': '22.11.0'}]
        observer.channels.side_effect = lambda: list(channels)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(fixture.socket, 'if_nameindex', return_value=[(1, 'lo')]))
            stack.enter_context(mock.patch.object(fixture, 'module', return_value=gateway))
            stack.enter_context(mock.patch.object(fixture.Path, 'read_text', return_value='{}'))
            stack.enter_context(mock.patch.object(fixture, 'ObserverAMI', return_value=observer))
            stack.enter_context(mock.patch.object(fixture, 'Phone', side_effect=phone))
            if diagnostic_failure:
                stack.enter_context(mock.patch.object(fixture, 'refusal_diagnostics', side_effect=ValueError('collector')))
            try:
                return fixture.test_inside()
            finally:
                server.db.close.assert_called_once()
                for peer in peers.values():
                    peer.close.assert_called_once()

    def test_complete_scenarios_require_current_case_redirect_evidence(self):
        result = self.run_inside()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['cases']['source_redirect']['peers']['201']['redirects_sent'], 1)
        target = result['cases']['destination_redirect']['peers']
        self.assertEqual(target['201']['speaker_presses'], 1)
        self.assertEqual(target['203']['redirects_sent'], 1)

    def test_early_failure_reports_underlying_cause_and_zero_speaker(self):
        with self.assertRaisesRegex(RuntimeError, 'before intended peer sent 302') as caught:
            self.run_inside(early_failure=True)
        evidence = caught.exception.intercom_diagnostics
        self.assertEqual(evidence['case'], 'destination_redirect')
        self.assertEqual(evidence['errors'][1]['reason'], 'intercom_call_changed')
        self.assertEqual(evidence['peers']['201']['speaker_presses'], 0)
        self.assertEqual(evidence['peers']['203']['new_dialogs'], 0)

    def test_duplicate_source_speaker_is_still_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'source Speaker not answered exactly once'):
            self.run_inside(duplicate_press=True)

    def test_diagnostic_failure_does_not_mask_early_failure_or_cleanup(self):
        with self.assertRaisesRegex(RuntimeError, 'before intended peer sent 302'):
            self.run_inside(early_failure=True, diagnostic_failure=True)

    def test_created_intercom_container_logs_precede_cleanup_even_if_logging_fails(self):
        for logging_fails in (False, True):
            with self.subTest(logging_fails=logging_fails):
                calls = []

                def run(command, **kwargs):
                    self.assertEqual(command[:3], ['docker', '--context', 'fixture'])
                    action = command[3]
                    calls.append(action)
                    output, code = '', 0
                    if action == 'create':
                        self.assertIn('none', command)
                        self.assertIn('never', command)
                        output = 'created-intercom\n'
                    elif action == 'inspect':
                        output = 'none\n'
                    elif action == 'exec' and command[5] == 'asterisk':
                        output = ('PJSIP/202,8,i' if 'dialplan' in command[-1] else
                                  '\n'.join(' Endpoint: ' + line + '/line' for line in ('201', '202', '203', '204')))
                    elif action == 'exec':
                        code, output = 1, '{"status":"FAIL","error":"synthetic original failure"}'
                    elif action == 'logs':
                        self.assertEqual(command[4:], ['--tail', '80', 'created-intercom'])
                        if logging_fails:
                            raise RuntimeError('log collector failed')
                        output = 'synthetic intercom log'
                    elif action == 'rm':
                        self.assertEqual(command[-1], 'created-intercom')
                    return subprocess.CompletedProcess(command, code, output, '')

                stderr = io.StringIO()
                with mock.patch.object(sys, 'argv', [str(SCRIPT), '--image', 'synthetic', '--docker-context', 'fixture']), \
                        mock.patch.object(fixture.subprocess, 'run', side_effect=run), \
                        contextlib.redirect_stderr(stderr):
                    with self.assertRaisesRegex(RuntimeError, 'synthetic original failure'):
                        fixture.main()
                self.assertEqual(calls[-2:], ['logs', 'rm'])
                if not logging_fails:
                    self.assertIn('Isolated intercom fixture log tail:', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
