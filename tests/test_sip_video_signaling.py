"""Wire-level synthetic tests: no external listeners, phones or media devices."""
import base64
import socket
import time
import threading
import unittest
from unittest.mock import patch

from tools.sip_video.signaling import (
    MAX_CACHE, MAX_MESSAGE, SipError, SipVideoServer, answer_sdp, parse_offer, parse_request,
)


def sdp(*, video_port=25000, connection="127.0.0.1", mode="1", audio=False):
    lines = ["v=0", "o=- 1 1 IN IP4 127.0.0.1", "s=Synthetic video", f"c=IN IP4 {connection}", "t=0 0"]
    if audio:
        lines.extend(["m=audio 25002 RTP/AVP 0", "a=sendrecv"])
    lines.extend([f"m=video {video_port} RTP/AVP 96", "a=rtpmap:96 H264/90000", f"a=fmtp:96 packetization-mode={mode}", "a=sendonly"])
    return ("\r\n".join(lines) + "\r\n").encode()


def request(method, source_port, target_port=25004, *, body=None, seq=1,
            branch="z9hG4bKfirst", tag=None, call_id="synthetic-call", from_tag="caller"):
    if body is None:
        body = sdp() if method == "INVITE" else b""
    lines = [f"{method} sip:video@127.0.0.1:{target_port} SIP/2.0",
             f"Via: SIP/2.0/UDP 127.0.0.1:{source_port};branch={branch};rport",
             f"From: <sip:caller@127.0.0.1:{source_port}>;tag={from_tag}",
             f"To: <sip:video@127.0.0.1:{target_port}>" + (f";tag={tag}" if tag else ""),
             f"Call-ID: {call_id}", f"CSeq: {seq} {method}", "Max-Forwards: 70",
             f"Contact: <sip:caller@127.0.0.1:{source_port}>"]
    if body:
        lines.append("Content-Type: application/sdp")
    lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def headers(data):
    return {name.lower(): value.strip() for name, value in
            (line.split(":", 1) for line in data.decode().split("\r\n\r\n", 1)[0].split("\r\n")[1:])}


def status(data):
    return int(data.split(b" ", 2)[1])


class FakeMedia:
    def __init__(self, offer):
        self.port = 25010
        self.offer = offer
        self.closed = 0
        self.decoded = False

    def close(self, *, decode=True):
        self.closed += 1
        self.decoded = decode

    def snapshot(self):
        return {"closed": self.closed, "decoded": self.decoded}


class ParserTest(unittest.TestCase):
    def test_valid_offer_preserves_audio_position_and_disables_audio_answer(self):
        offer = parse_offer(sdp(audio=True))
        self.assertEqual((offer.payload_type, offer.source_port, offer.packetization_mode), (96, 25000, 1))
        answer = answer_sdp(offer, 25020)
        self.assertIn(b"m=audio 0 RTP/AVP 0\r\na=inactive", answer)
        self.assertLess(answer.index(b"m=audio"), answer.index(b"m=video"))
        self.assertIn(b"m=video 25020 RTP/AVP 96\r\na=recvonly", answer)

    def test_video_offer_without_fmtp_defaults_to_single_nal_mode(self):
        offer = parse_offer(sdp().replace(b"a=fmtp:96 packetization-mode=1\r\n", b""))
        self.assertEqual(offer.packetization_mode, 0)

    def test_optional_parameter_sets_are_bounded_and_typed(self):
        parameters = base64.b64encode(b"\x67test").decode() + "," + base64.b64encode(b"\x68test").decode()
        offer = parse_offer(sdp().replace(b"packetization-mode=1", f"packetization-mode=1;sprop-parameter-sets={parameters};profile-level-id=42e01f".encode()))
        self.assertEqual(offer.sprop_parameter_sets, parameters)
        self.assertEqual(offer.profile_level_id, "42e01f")
        for params in ("garbage", "AAAA,AAAA", "!!!!,!!!!", "A" * 1025):
            with self.subTest(params=params[:10]), self.assertRaises(SipError):
                parse_offer(sdp().replace(b"packetization-mode=1", f"sprop-parameter-sets={params}".encode()))

    def test_unsafe_and_unsupported_sdp_fails_closed(self):
        invalid = [
            sdp(connection="192.0.2.3"), sdp(connection="localhost"), sdp(connection="::1"),
            sdp().replace(b"o=- 1 1 IN IP4 127.0.0.1", b"o=- 1 1 IN IP4 192.0.2.3"),
            sdp(mode="2"), sdp(video_port=0), sdp(video_port=80),
            sdp().replace(b"H264/90000", b"VP8/90000"),
            sdp().replace(b"a=sendonly", b"a=recvonly"),
            sdp().replace(b"a=sendonly", b"a=inactive"),
            sdp() + b"m=video 25001 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n",
            sdp() + b"a=rtcp:25001 IN IP4 192.0.2.3\r\n",
            sdp() + b"a=rtcp-mux\r\n",
            sdp().replace(b"t=0 0\r\n", b"t=0 0\r\na=rtcp-mux\r\n"),
            sdp() + b"a=candidate:1 1 UDP 100 127.0.0.1 25000 typ host\r\n",
            sdp() + b"a=ice-ufrag:abc\r\n", sdp() + b"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:abc\r\n",
            sdp() + b"a=fingerprint:sha-256 abc\r\n",
            sdp() + b"c=IN IP4 192.0.2.3\r\n",
            sdp().replace(b"t=0 0", b"t=1 2"), sdp().replace(b"c=IN IP4 127.0.0.1\r\n", b""),
            sdp().replace(b"packetization-mode=1", b"packetization-mode=1;packetization-mode=0"),
            sdp() + b"a=fmtp:96 packetization-mode=0\r\n",
            sdp().replace(b"packetization-mode=1", b"profile-level-id=xx1234"),
        ]
        for body in invalid:
            with self.subTest(body=body[-80:]), self.assertRaises(SipError):
                parse_offer(body)

    def test_request_validation_rejects_reflection_and_ambiguous_headers(self):
        valid = request("INVITE", 25006)
        parsed = parse_request(valid, ("127.0.0.1", 25006))
        self.assertEqual(parsed.key, ("synthetic-call", "z9hG4bKfirst", 1, "INVITE"))
        invalid = [
            valid.replace(b"SIP/2.0/UDP 127.0.0.1:25006", b"SIP/2.0/UDP 192.0.2.4:25006"),
            valid.replace(b";rport", b";maddr=192.0.2.4"),
            valid.replace(b"Contact: <sip:caller@127.0.0.1:25006>", b"Contact: <sip:caller@192.0.2.4:25006>"),
            valid.replace(b"Max-Forwards: 70", b"Max-Forwards: 70\r\nRoute: <sip:proxy@127.0.0.1>"),
            valid.replace(b"Max-Forwards: 70", b"Max-Forwards: 70\r\nVia: SIP/2.0/UDP 127.0.0.1:25006"),
            valid.replace(b"Max-Forwards: 70", b"Max-Forwards: 70\r\n folded: value"),
            valid.replace(b"Content-Length:", b"l:"),
            valid.replace(b"CSeq: 1 INVITE", b"CSeq: 1 BYE"),
            valid + b"trailing", valid.replace(b"\r\n", b"\n"), valid + b"\x00",
            b"x" * (MAX_MESSAGE + 1),
        ]
        for wire in invalid:
            with self.subTest(wire=wire[:60]), self.assertRaises(SipError):
                parse_request(wire, ("127.0.0.1", 25006))
        for source in (("127.0.0.2", 25006), ("192.0.2.4", 25006), ("127.0.0.1", 25007)):
            with self.subTest(source=source), self.assertRaises(SipError):
                parse_request(valid, source)


class WireTest(unittest.TestCase):
    def setUp(self):
        self.media = []
        self.servers = []
        self.sockets = []

    def tearDown(self):
        for server in self.servers:
            server.close()
        for peer in self.sockets:
            peer.close()

    def factory(self, offer):
        handle = FakeMedia(offer)
        self.media.append(handle)
        return handle

    def server(self, **kwargs):
        server = SipVideoServer(self.factory, **kwargs).start()
        self.servers.append(server)
        self.assertEqual(server._socket.getsockname()[0], "127.0.0.1")
        return server

    def peer(self):
        peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        peer.bind(("127.0.0.1", 0))
        peer.settimeout(1)
        self.sockets.append(peer)
        return peer

    def send(self, peer, server, method, **kwargs):
        wire = request(method, peer.getsockname()[1], server.port, **kwargs)
        peer.sendto(wire, ("127.0.0.1", server.port))
        return wire

    def receive(self, peer, expected):
        response, source = peer.recvfrom(MAX_MESSAGE)
        self.assertEqual(source[0], "127.0.0.1")
        self.assertEqual(status(response), expected, response)
        return response

    def connect(self, server, peer):
        self.send(peer, server, "INVITE")
        self.receive(peer, 100)
        self.receive(peer, 180)
        response = self.receive(peer, 200)
        tag = headers(response)["to"].split(";tag=")[1]
        self.send(peer, server, "ACK", tag=tag, branch="z9hG4bKack")
        self.wait_state(server, "established")
        return tag

    def wait_state(self, server, expected):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            snap = server.snapshot()
            if snap["state"] == expected:
                return snap
            time.sleep(.01)
        self.fail(f"state was {server.snapshot()}, expected {expected}")

    def test_negotiates_ack_and_bye_closes_once_with_idempotent_retransmissions(self):
        server, peer = self.server(), self.peer()
        invite = self.send(peer, server, "INVITE", body=sdp(audio=True))
        self.receive(peer, 100)
        provisional = self.receive(peer, 180)
        peer.sendto(invite, ("127.0.0.1", server.port))
        self.assertEqual(self.receive(peer, 180), provisional)
        final = self.receive(peer, 200)
        self.assertIn(b"m=audio 0 RTP/AVP 0", final)
        peer.sendto(invite, ("127.0.0.1", server.port))
        self.assertEqual(self.receive(peer, 200), final)
        self.assertEqual(len(self.media), 1)
        tag = headers(final)["to"].split(";tag=")[1]
        self.send(peer, server, "ACK", tag=tag, branch="z9hG4bKack")
        self.wait_state(server, "established")
        bye = self.send(peer, server, "BYE", tag=tag, seq=2, branch="z9hG4bKbye")
        final_bye = self.receive(peer, 200)
        self.wait_state(server, "closed")
        peer.sendto(bye, ("127.0.0.1", server.port))
        self.assertEqual(self.receive(peer, 200), final_bye)
        self.assertEqual(self.media[0].closed, 1)
        self.assertEqual(server.snapshot()["close_reason"], "bye")
        self.assertTrue(self.media[0].decoded)
        server.close()
        server.close()
        self.assertEqual(self.media[0].closed, 1)
        self.assertFalse(server.snapshot()["worker_alive"])
        self.assertTrue(server.snapshot()["socket_closed"])

    def test_invite_response_survives_bounded_other_transaction_cache(self):
        server, peer = self.server(), self.peer()
        self.connect(server, peer)
        for index in range(MAX_CACHE + 2):
            self.send(peer, server, "OPTIONS", seq=index + 2,
                      branch="z9hG4bKoption" + str(index))
            self.receive(peer, 200)
        self.send(peer, server, "INVITE")
        response = self.receive(peer, 200)
        self.assertEqual(headers(response)["cseq"], "1 INVITE")
        self.assertEqual(len(self.media), 1)

    def test_cancel_matches_transaction_and_never_opens_media(self):
        server, peer = self.server(answer_delay=.4), self.peer()
        invite = self.send(peer, server, "INVITE")
        self.receive(peer, 100)
        self.receive(peer, 180)
        self.send(peer, server, "CANCEL", branch="z9hG4bKwrong")
        self.receive(peer, 481)
        self.assertEqual(server.snapshot()["state"], "ringing")
        cancel = self.send(peer, server, "CANCEL")
        cancelled = self.receive(peer, 200)
        terminated = self.receive(peer, 487)
        self.assertEqual(headers(terminated)["cseq"], "1 INVITE")
        self.wait_state(server, "closed")
        peer.sendto(cancel, ("127.0.0.1", server.port))
        self.assertEqual(self.receive(peer, 200), cancelled)
        peer.sendto(invite, ("127.0.0.1", server.port))
        self.assertEqual(self.receive(peer, 487), terminated)
        time.sleep(.45)
        self.assertEqual(self.media, [])
        self.assertEqual(server.snapshot()["close_reason"], "cancelled")

    def test_cancel_after_answer_does_not_terminate_dialog(self):
        server, peer = self.server(), self.peer()
        self.connect(server, peer)
        self.send(peer, server, "CANCEL")
        self.receive(peer, 200)
        self.assertEqual(server.snapshot()["state"], "established")
        self.assertEqual(self.media[0].closed, 0)

    def test_wrong_peer_tag_callid_and_old_cseq_cannot_end_dialog(self):
        server, peer = self.server(), self.peer()
        tag = self.connect(server, peer)
        for changes in ({"tag": "wrong"}, {"call_id": "wrong"}, {"from_tag": "wrong"}):
            options = {"tag": tag, "seq": 2, "branch": "z9hG4bK" + str(len(str(changes)))}
            options.update(changes)
            self.send(peer, server, "BYE", **options)
            self.receive(peer, 481)
        other = self.peer()
        self.send(other, server, "BYE", tag=tag, seq=2, branch="z9hG4bKother")
        self.receive(other, 481)
        self.send(peer, server, "BYE", tag=tag, seq=1, branch="z9hG4bKold")
        self.receive(peer, 500)
        self.assertEqual(server.snapshot()["state"], "established")
        self.assertEqual(self.media[0].closed, 0)

    def test_ack_must_belong_to_the_answered_dialog(self):
        server, peer = self.server(ack_timeout=.6), self.peer()
        self.send(peer, server, "INVITE")
        self.receive(peer, 100)
        self.receive(peer, 180)
        response = self.receive(peer, 200)
        tag = headers(response)["to"].split(";tag=")[1]
        self.send(peer, server, "ACK", tag="wrong", branch="z9hG4bKwrongack")
        time.sleep(.03)
        self.assertEqual(server.snapshot()["state"], "awaiting_ack")
        other = self.peer()
        self.send(other, server, "ACK", tag=tag, branch="z9hG4bKotherack")
        time.sleep(.03)
        self.assertEqual(server.snapshot()["state"], "awaiting_ack")
        self.send(peer, server, "ACK", tag=tag, branch="z9hG4bKcorrectack")
        self.wait_state(server, "established")

    def test_ringing_timeout_never_creates_media(self):
        server, peer = self.server(answer_delay=.4, call_timeout=.1), self.peer()
        self.send(peer, server, "INVITE")
        self.receive(peer, 100)
        self.receive(peer, 180)
        self.receive(peer, 480)
        self.assertEqual(self.wait_state(server, "closed")["close_reason"], "call_timeout")
        self.assertEqual(self.media, [])

    def test_missing_ack_closes_media_and_200_retransmits(self):
        server, peer = self.server(ack_timeout=.7), self.peer()
        self.send(peer, server, "INVITE")
        self.receive(peer, 100)
        self.receive(peer, 180)
        final = self.receive(peer, 200)
        self.assertEqual(self.receive(peer, 200), final)
        snap = self.wait_state(server, "closed")
        self.assertEqual(snap["close_reason"], "ack_timeout")
        self.assertEqual(self.media[0].closed, 1)
        self.assertFalse(self.media[0].decoded)

    def test_established_call_and_idle_server_have_finite_lifetimes(self):
        server, peer = self.server(call_timeout=.2), self.peer()
        self.connect(server, peer)
        self.assertEqual(self.wait_state(server, "closed")["close_reason"], "call_timeout")
        self.assertEqual(self.media[0].closed, 1)
        idle = self.server(server_timeout=.1)
        self.assertEqual(self.wait_state(idle, "closed")["close_reason"], "server_timeout")
        idle.close()
        self.assertFalse(idle.snapshot()["worker_alive"])
        self.assertTrue(idle.snapshot()["socket_closed"])

    def test_shutdown_while_ringing_or_established_releases_resources(self):
        for establish in (False, True):
            server, peer = self.server(answer_delay=.01 if establish else 1), self.peer()
            if establish:
                self.connect(server, peer)
            else:
                self.send(peer, server, "INVITE")
                self.receive(peer, 100)
                self.receive(peer, 180)
            server.close()
            self.assertEqual(server.snapshot()["state"], "closed")
            self.assertEqual(server.snapshot()["close_reason"], "server_closed")
            self.assertTrue(server.snapshot()["socket_closed"])
            self.assertFalse(server.snapshot()["worker_alive"])
        self.assertEqual(self.media[0].closed, 1)
        self.assertFalse(self.media[0].decoded)

    def test_unsupported_offer_gets_488_and_other_dialog_is_busy(self):
        server, peer = self.server(), self.peer()
        self.send(peer, server, "INVITE", body=sdp(mode="2"), branch="z9hG4bKbad")
        self.receive(peer, 488)
        self.assertEqual(self.media, [])
        self.connect(server, peer)
        other = self.peer()
        self.send(other, server, "INVITE", call_id="second", branch="z9hG4bKsecond")
        self.receive(other, 486)
        self.assertEqual(len(self.media), 1)

    def test_malformed_udp_drops_without_reflecting_or_starting_media(self):
        server, peer, victim = self.server(), self.peer(), self.peer()
        malformed = request("INVITE", victim.getsockname()[1], server.port)
        for data in (malformed, b"not SIP", b"x" * (MAX_MESSAGE + 1)):
            peer.sendto(data, ("127.0.0.1", server.port))
        peer.settimeout(.1)
        victim.settimeout(.1)
        for endpoint in (peer, victim):
            with self.assertRaises(socket.timeout):
                endpoint.recvfrom(MAX_MESSAGE)
        self.assertEqual(server.snapshot()["dropped_messages"], 3)
        self.assertEqual(self.media, [])

    def test_media_start_failure_returns_500_and_closes_partial_handle(self):
        server, peer = self.server(), self.peer()
        def bad_factory(offer):
            handle = self.factory(offer)
            handle.port = 80
            return handle
        server.media_factory = bad_factory
        self.send(peer, server, "INVITE")
        self.receive(peer, 100)
        self.receive(peer, 180)
        self.receive(peer, 500)
        self.assertEqual(self.wait_state(server, "closed")["close_reason"], "media_start_failed")
        self.assertEqual(self.media[0].closed, 1)

    def test_socket_startup_failures_close_acquired_descriptor(self):
        real_socket = socket.socket
        for failing_method in ("bind", "settimeout", "getsockname"):
            class FailingSocket(real_socket):
                pass
            def fail(*_args, **_kwargs):
                raise OSError("injected socket setup failure")
            setattr(FailingSocket, failing_method, fail)
            acquired = FailingSocket(socket.AF_INET, socket.SOCK_DGRAM)
            server = SipVideoServer(self.factory)
            with self.subTest(method=failing_method), patch(
                    "tools.sip_video.signaling.socket.socket", return_value=acquired):
                with self.assertRaisesRegex(OSError, "injected"):
                    server.start()
            self.assertEqual(acquired.fileno(), -1)
            server.close()
            server.close()
            snap = server.snapshot()
            self.assertEqual((snap["state"], snap["close_reason"]), ("closed", "start_failed"))
            self.assertTrue(snap["socket_closed"])
            self.assertFalse(snap["worker_alive"])
            self.assertEqual(self.media, [])
        server = SipVideoServer(self.factory)
        with patch("tools.sip_video.signaling.socket.socket", side_effect=OSError("injected creation failure")):
            with self.assertRaisesRegex(OSError, "injected"):
                server.start()
        server.close()
        self.assertTrue(server.snapshot()["socket_closed"])
        self.assertEqual(server.snapshot()["close_reason"], "start_failed")

    def test_thread_start_failure_is_closed_and_close_does_not_join_unstarted_worker(self):
        server = SipVideoServer(self.factory)
        with patch("tools.sip_video.signaling.threading.Thread.start",
                   side_effect=RuntimeError("injected thread start failure")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                server.start()
        server.close()
        server.close()
        snap = server.snapshot()
        self.assertEqual((snap["state"], snap["close_reason"]), ("closed", "start_failed"))
        self.assertTrue(snap["socket_closed"])
        self.assertFalse(snap["worker_alive"])
        self.assertEqual(self.media, [])

    def test_partial_thread_start_failure_joins_worker_without_holding_its_lock(self):
        real_start = threading.Thread.start
        def start_then_fail(worker):
            real_start(worker)
            raise RuntimeError("injected failure after thread started")
        server = SipVideoServer(self.factory)
        with patch("tools.sip_video.signaling.threading.Thread.start", start_then_fail):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                server.start()
        server.close()
        snap = server.snapshot()
        self.assertEqual((snap["state"], snap["close_reason"]), ("closed", "start_failed"))
        self.assertTrue(snap["socket_closed"])
        self.assertFalse(snap["worker_alive"])
        self.assertEqual(self.media, [])

    def test_invalid_time_bounds_and_second_start_are_rejected(self):
        for option in ("answer_delay", "call_timeout", "ack_timeout", "server_timeout"):
            for value in (-1, float("nan"), float("inf"), True, 1000):
                with self.subTest(option=option, value=value), self.assertRaises(ValueError):
                    SipVideoServer(self.factory, **{option: value})
        server = self.server()
        with self.assertRaises(RuntimeError):
            server.start()


if __name__ == "__main__":
    unittest.main()
