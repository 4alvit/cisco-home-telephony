"""SIP fixture transaction replay using fake sockets; no network or threads."""
import importlib.util
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


intercom = load('invite_replay_intercom', 'test-intercom-originate.py')
helper = intercom.helper
radio = load('invite_replay_radio', 'test-radio-wideband.py')
ADDRESS = ('127.0.0.1', 5060)


def request(method='INVITE', *, call_id='call-1', cseq=1,
            branch='z9hG4bK-original', sent_by='127.0.0.1:5060'):
    body = ('v=0\r\nc=IN IP4 127.0.0.1\r\nm=audio 7000 RTP/AVP 0\r\n'
            if method == 'INVITE' else '')
    return (f'{method} sip:202@127.0.0.2:5090 SIP/2.0\r\n'
            f'Via: SIP/2.0/UDP {sent_by};branch={branch}\r\n'
            'From: <sip:204@127.0.0.1>;tag=source\r\n'
            'To: <sip:202@127.0.0.2>\r\n'
            f'Call-ID: {call_id}\r\nCSeq: {cseq} {method}\r\n'
            f'Content-Length: {len(body)}\r\n\r\n{body}')


class FakeSocket:
    def __init__(self):
        self.incoming, self.sent = [], []
        self.fail_send = False

    def recvfrom(self, _limit):
        return self.incoming.pop(0)

    def sendto(self, data, address):
        if self.fail_send:
            raise OSError('synthetic send failure')
        self.sent.append((data, address))
        return len(data)


class InviteReplayTests(unittest.TestCase):
    def peer(self, *, redirect=False, base=False):
        kind = helper.Phone if base else intercom.Phone
        peer = kind.__new__(kind)
        peer.address, peer.redirect = '127.0.0.2', redirect
        peer.calls, peer.error = {}, None
        peer.sip, peer.audio, peer.forward = FakeSocket(), object(), object()
        peer.audio_packets = peer.forward_packets = 0
        peer.lock, peer.stopped = threading.RLock(), threading.Event()
        return peer

    def receive(self, peer, message, *, failed=False):
        peer.stopped.clear()
        peer.sip.incoming.append((message.encode('ascii'), ADDRESS))

        def ready(*_args):
            if peer.sip.incoming:
                return [peer.sip], [], []
            peer.stopped.set()
            return [], [], []

        with mock.patch.object(helper.select, 'select', side_effect=ready):
            peer.listen()
        if not failed:
            self.assertIsNone(peer.error)

    def test_provisional_response_replays_without_reentering_reply_override(self):
        peer, message = self.peer(), request()
        with mock.patch.object(peer, 'reply', wraps=peer.reply) as reply:
            self.receive(peer, message)
            call = dict(peer.calls['call-1'])
            self.receive(peer, message)
        self.assertEqual(reply.call_count, 1)
        self.assertEqual(peer.sip.sent, [peer.sip.sent[0]] * 2)
        self.assertEqual(len(peer.calls), 1)
        self.assertEqual(peer.calls['call-1']['invites'], 2)
        for field in ('request', 'address', 'first_invite'):
            self.assertEqual(peer.calls['call-1'][field], call[field])

    def test_answered_intercom_replays_exact_sdp_contact_and_status(self):
        peer, message = self.peer(), request()
        self.receive(peer, message)
        peer.answer()
        final = peer.sip.sent[-1]
        self.assertIn(b'SIP/2.0 200 OK\r\n', final[0])
        self.assertIn(b'Content-Type: application/sdp\r\n', final[0])
        self.assertIn(b'Contact: <sip:202@127.0.0.2:5090>', final[0])
        self.receive(peer, message)
        self.assertEqual(peer.sip.sent[-1], final)

    def test_alert_final_responses_replay(self):
        for mode, status in (('answer', b'200 OK'), ('reject', b'603 Decline'),
                             ('forward', b'302 Moved Temporarily')):
            with self.subTest(mode=mode):
                peer, message = self.peer(base=True), request()
                self.receive(peer, message)
                peer.respond(peer.calls['call-1'], mode)
                final = peer.sip.sent[-1]
                self.assertTrue(final[0].startswith(b'SIP/2.0 ' + status))
                self.receive(peer, message)
                self.assertEqual(peer.sip.sent[-1], final)

    def test_redirect_replay_ignores_new_scenario_flag_and_preserves_evidence(self):
        peer, message = self.peer(redirect=True), request()
        self.receive(peer, message)
        final, rejected = peer.sip.sent[-1], peer.calls['call-1']['reject']
        peer.redirect = False
        self.receive(peer, message)
        self.assertEqual(peer.sip.sent[-1], final)
        self.assertEqual(peer.calls['call-1']['reject'], rejected)
        evidence = intercom.redirect_evidence({'201': peer}, {'201': {'call-1'}}, [])
        self.assertEqual(evidence['201']['new_dialogs'], 0)
        self.assertEqual(evidence['201']['redirects_sent'], 0)
        peer.redirect = True
        self.receive(peer, request(call_id='call-2'))
        peer.redirect = False
        self.receive(peer, request(call_id='call-2'))
        evidence = intercom.redirect_evidence({'201': peer}, {'201': {'call-1'}}, [])
        self.assertEqual(evidence['201']['new_dialogs'], 1)
        self.assertEqual(evidence['201']['redirects_sent'], 1)
        self.assertEqual(peer.calls['call-2']['invites'], 2)

    def test_cancel_updates_invite_response_to_487_not_cancel_200(self):
        peer, message = self.peer(), request()
        self.receive(peer, message)
        self.receive(peer, request('CANCEL'))
        self.assertTrue(peer.sip.sent[-2][0].startswith(b'SIP/2.0 200 OK'))
        final = peer.sip.sent[-1]
        self.assertTrue(final[0].startswith(b'SIP/2.0 487 Request Terminated'))
        cancelled = peer.calls['call-1']['cancel']
        self.receive(peer, message)
        self.assertEqual(peer.sip.sent[-1], final)
        self.assertEqual(peer.calls['call-1']['cancel'], cancelled)

    def test_non_invite_responses_do_not_replace_invite_cache(self):
        peer, message = self.peer(), request()
        self.receive(peer, message)
        peer.answer()
        final = peer.sip.sent[-1]
        self.receive(peer, request('OPTIONS'))
        self.receive(peer, request('ACK'))
        self.receive(peer, request('BYE', cseq=2))
        self.receive(peer, message)
        self.assertEqual(peer.sip.sent[-1], final)

    def test_different_transactions_never_reuse_original_answer(self):
        for change in ({'call_id': 'call-2'}, {'cseq': 2},
                       {'branch': 'z9hG4bK-other'}, {'sent_by': '127.0.0.1:5070'}):
            with self.subTest(change=change):
                peer, message = self.peer(), request()
                self.receive(peer, message)
                peer.answer()
                final = peer.sip.sent[-1]
                self.receive(peer, request(**change))
                self.assertTrue(peer.sip.sent[-1][0].startswith(b'SIP/2.0 180 Ringing'))
                self.assertNotEqual(peer.sip.sent[-1], final)
                self.assertEqual(peer.calls['call-1']['request'], message)
                self.receive(peer, message)
                self.assertEqual(peer.sip.sent[-1], final)

    def test_transaction_variants_do_not_grow_a_cache_within_one_call(self):
        peer, message = self.peer(), request()
        self.receive(peer, message)
        peer.answer()
        final, fields = peer.sip.sent[-1], set(peer.calls['call-1'])
        for cseq in range(2, 102):
            self.receive(peer, request(cseq=cseq))
        self.assertEqual(len(peer.calls), 1)
        self.assertEqual(set(peer.calls['call-1']), fields)
        self.assertEqual(peer.calls['call-1']['invites'], 101)
        self.receive(peer, message)
        self.assertEqual(peer.sip.sent[-1], final)

    def test_failed_send_does_not_replace_last_successful_response(self):
        peer, message = self.peer(), request()
        self.receive(peer, message)
        provisional = peer.sip.sent[-1]
        peer.sip.fail_send = True
        with self.assertRaisesRegex(OSError, 'synthetic send failure'):
            peer.reply(message, ADDRESS, '200 OK', 'unsent body', 'sip:unsent@127.0.0.2')
        peer.sip.fail_send = False
        self.receive(peer, message)
        self.assertEqual(peer.sip.sent[-1], provisional)

    def test_first_send_failure_does_not_invent_a_response(self):
        peer, message = self.peer(), request()
        peer.sip.fail_send = True
        self.receive(peer, message, failed=True)
        self.assertIn('synthetic send failure', peer.error)
        self.assertEqual(peer.sip.sent, [])
        peer.sip.fail_send, peer.error = False, None
        self.receive(peer, message)
        self.assertEqual(len(peer.sip.sent), 1)
        self.assertTrue(peer.sip.sent[0][0].startswith(b'SIP/2.0 180 Ringing'))

    def test_radio_reply_alias_without_call_inventory_remains_supported(self):
        peer = SimpleNamespace(sip=FakeSocket())
        radio.Phone.reply(peer, request(), ADDRESS, '200 OK', 'v=0\r\n',
                          'sip:201@127.0.0.2:5090')
        self.assertTrue(peer.sip.sent[0][0].startswith(b'SIP/2.0 200 OK'))
        self.assertIn(b'Contact: <sip:201@127.0.0.2:5090>', peer.sip.sent[0][0])
        self.assertFalse(hasattr(peer, 'calls'))


if __name__ == '__main__':
    unittest.main()
