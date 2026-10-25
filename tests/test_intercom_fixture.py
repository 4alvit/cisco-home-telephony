"""Intercom smoke evidence and failure diagnostics without Docker or network."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
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

    def test_ownership_collector_failure_preserves_original_error_and_cleanup(self):
        with mock.patch.object(fixture, 'ownership_diagnostics', side_effect=ValueError('private collector')):
            with self.assertRaisesRegex(RuntimeError, 'before intended peer sent 302'):
                self.run_inside(early_failure=True)

    def test_repeat_keeps_failure_and_runs_remaining_independent_attempts(self):
        output = io.StringIO()
        with mock.patch.object(sys, 'argv', [str(SCRIPT), '--image', 'synthetic', '--repeat', '3']), \
                mock.patch.object(fixture, 'run_fixture', side_effect=[
                    {'status': 'PASS'}, RuntimeError('classified failure'), {'status': 'PASS'}]) as run, \
                contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, '1 of 3 isolated intercom attempts failed'):
                fixture.main()
        self.assertEqual(run.call_count, 3)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([row['attempt'] for row in rows], [1, 2, 3])
        self.assertEqual([row['status'] for row in rows], ['PASS', 'FAIL', 'PASS'])
        self.assertEqual(rows[1]['error'], 'classified failure')

    def test_repeat_creates_and_cleans_distinct_network_none_containers(self):
        created, removed, copied = [], [], []

        def run(command, **kwargs):
            action, output = command[1], ''
            if action == 'create':
                self.assertIn('none', command)
                self.assertIn('never', command)
                created.append('fresh-' + str(len(created) + 1))
                output = created[-1] + '\n'
            elif action == 'inspect':
                output = 'none\n'
            elif action == 'cp':
                copied.append(command[2])
            elif action == 'exec' and command[3] == 'asterisk':
                output = ('PJSIP/202,8,i' if 'dialplan' in command[-1] else
                          '\n'.join(' Endpoint: ' + line + '/line' for line in ('201', '202', '203', '204')))
            elif action == 'exec':
                output = '{"status":"PASS"}'
            elif action == 'rm':
                removed.append(command[-1])
            return subprocess.CompletedProcess(command, 0, output, '')

        output = io.StringIO()
        with mock.patch.object(sys, 'argv', [str(SCRIPT), '--image', 'synthetic', '--repeat', '2']), \
                mock.patch.object(fixture.subprocess, 'run', side_effect=run), \
                contextlib.redirect_stdout(output):
            fixture.main()
        self.assertEqual(created, ['fresh-1', 'fresh-2'])
        self.assertEqual(removed, created)
        self.assertEqual(len(set(copied)), 2)
        self.assertEqual([json.loads(line)['attempt'] for line in output.getvalue().splitlines()], [1, 2])

    def test_repeat_is_bounded_before_any_container_operation(self):
        for count in ('0', '-1', '11'):
            with self.subTest(count=count), \
                    mock.patch.object(sys, 'argv', [str(SCRIPT), '--image', 'synthetic', '--repeat', count]), \
                    mock.patch.object(fixture, 'run_fixture') as run, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    fixture.main()
                run.assert_not_called()

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


def _intercom_reserved(self, owned, related, root='private-current-root', number='202', current=None):
    """Synthetic traceback frame with the same diagnostic inputs as the guard."""
    current = owned if current is None else current
    raise RuntimeError('private-error-payload')


class OwnershipDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        from test_intercom import gateway, pair_config
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.server = gateway.Gateway(pair_config(), directory.name)
        self.addCleanup(self.server.db.close)
        self.server.db.execute('INSERT INTO intercoms VALUES (?, ?, ?)',
                               ('private-previous-root', 'stopping', 1))

    @staticmethod
    def row(**changes):
        return {'Channel': 'PJSIP/202-private-channel-secret', 'Uniqueid': 'private-channel-uid',
                'Linkedid': 'private-previous-root', 'ChannelStateDesc': 'Ringing',
                'CallerIDName': 'private-person', 'URI': 'https://private.example/?token=private-token',
                **changes}

    def collect(self, owned, related, *, wrapped=False, **kwargs):
        try:
            try:
                _intercom_reserved(self.server, owned, related, **kwargs)
            except RuntimeError:
                if wrapped:
                    raise Refused('intercom_not_connected_check_status') from None
                raise
        except Exception as error:
            return fixture.ownership_diagnostics(error)

    def test_distinguishes_previous_operation_and_self_linked_new_channel(self):
        for linked in ('private-previous-root', 'private-previous-root-b'):
            with self.subTest(linked=linked):
                result = self.collect([self.row(Linkedid=linked)], [])
                self.assertIs(result['owned_matches_prior_operation'], True)
                self.assertEqual(result['owned_belongs_to_operation'], [False])
                self.assertEqual(result['owned_matches_selected_line'], [True])
                self.assertEqual(result['owned_states'], ['Ringing'])
        result = self.collect([self.row(Linkedid='private-channel-uid')], [])
        self.assertIs(result['owned_matches_prior_operation'], False)
        self.assertEqual(result['owned_linkedid_is_self'], [True])
        self.assertEqual(result['owned_linkedid_is_root'], [False])

    def test_current_operation_and_wrapped_error_do_not_export_ids(self):
        channel = self.row(Linkedid='private-current-root-b')
        result = self.collect([channel], [dict(channel)], wrapped=True)
        self.assertIs(result['owned_matches_prior_operation'], False)
        self.assertEqual(result['owned_belongs_to_operation'], [True])
        self.assertEqual(result['owned_linkedid_is_root'], [True])
        self.assertNotIn('private', json.dumps(result))

    def test_only_first_four_rows_are_classified(self):
        rows = [self.row(Linkedid='unknown') for _ in range(4)] + [self.row()]
        result = self.collect(rows, [], current=rows * 5)
        self.assertIs(result['owned_matches_prior_operation'], False)
        self.assertEqual(result['owned_count'], 5)
        self.assertEqual(result['owned_distinct_identities'], 1)
        self.assertEqual(result['snapshot_count'], 25)
        self.assertEqual(result['snapshot_distinct_identities'], 1)
        for value in result.values():
            if isinstance(value, list):
                self.assertEqual(len(value), 4)

    def test_identity_counts_distinguish_snapshot_replacement_from_another_call(self):
        earlier = self.row(ChannelStateDesc='Down')
        later = self.row(ChannelStateDesc='Ringing')
        other_identity = self.row(Uniqueid='private-other-call')
        other_name = self.row(Channel='PJSIP/204-private-other-channel')
        result = self.collect([earlier, later], [],
                              current=[earlier, later, other_identity, other_name])
        self.assertEqual(result['owned_count'], 2)
        self.assertEqual(result['owned_distinct_identities'], 1)
        self.assertEqual(result['snapshot_count'], 4)
        self.assertEqual(result['snapshot_distinct_identities'], 3)

    def test_missing_frame_and_empty_owned_are_bounded(self):
        self.assertEqual(fixture.ownership_diagnostics(RuntimeError('private')), {})
        result = self.collect([], [])
        self.assertEqual(result['owned_count'], 0)
        self.assertEqual(result['owned_states'], [])
        self.assertIs(result['owned_matches_prior_operation'], False)

    def test_unknown_states_and_raw_identifiers_never_leak(self):
        result = self.collect([self.row(ChannelStateDesc='private-' + 'x' * 100000)], [])
        encoded = json.dumps(result)
        self.assertEqual(result['owned_states'], ['other'])
        self.assertLess(len(encoded), 800)
        for private in ('private', 'PJSIP/', 'https://', 'token=', 'CallerID', '202'):
            self.assertNotIn(private, encoded)
        self.assertTrue(all(type(value) in (int, bool, list) for value in result.values()))


if __name__ == '__main__':
    unittest.main()
