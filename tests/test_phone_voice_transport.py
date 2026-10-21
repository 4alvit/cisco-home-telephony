"""Synthetic and loopback-only checks for the bounded Cisco voice transport."""
import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "phone_voice_transport_test", ROOT / "containers/asterisk/phone_voice_transport.py"
)
v = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = v
SPEC.loader.exec_module(v)

SILENCE = b"\xff" * 160
SOURCE = ("192.0.2.20", 24000)


def packet(sequence=1, timestamp=160, ssrc=7, payload=SILENCE, *, first=0x80,
           second=0, extra=b""):
    """Build wire packets independently of the production packet builder."""
    return struct.pack("!BBHII", first, second, sequence, timestamp, ssrc) + extra + payload


class LegacyUriTests(unittest.TestCase):
    def test_documented_legacy_uri_shapes_and_boundaries(self):
        self.assertEqual(v.tx_uri("192.0.2.10", 20480), "RTPTx:192.0.2.10:20480")
        self.assertEqual(v.rx_uri("192.0.2.10", 32768, 0), "RTPRx:192.0.2.10:32768:0")
        self.assertEqual(v.rx_uri("192.0.2.10", 24000, 100), "RTPRx:192.0.2.10:24000:100")
        self.assertEqual(v.rx_uri("192.0.2.10", 24000, None), "RTPRx:192.0.2.10:24000")
        self.assertEqual(v.stop_uris(), ("RTPTx:Stop", "RTPRx:Stop"))

    def test_addresses_are_canonical_literal_allowed_unicast_only(self):
        for value in ("192.168.10.20", "10.1.2.3", "172.16.1.2", "127.0.0.1",
                      "192.0.2.1", "198.51.100.1", "203.0.113.1"):
            with self.subTest(address=value):
                self.assertEqual(v.tx_uri(value, 24000), "RTPTx:" + value + ":24000")
        for value in (None, 2130706433, "", "localhost", "example.invalid", "192.000.2.1",
                      "192.0.2.1\n", " 192.0.2.1", "192.0.2.1:24000", "192.0.2.1&x=1",
                      "0.0.0.0", "224.0.0.1", "255.255.255.255", "8.8.8.8",
                      "169.254.169.254", "::1"):
            for builder in (v.tx_uri, v.rx_uri):
                with self.subTest(address=value, builder=builder.__name__), \
                     self.assertRaises(v.VoiceTransportError):
                    builder(value, 24000)

    def test_ports_and_volume_reject_coercion_injection_and_out_of_range(self):
        for port in (True, False, None, "24000", 24000.0, 0, -2, 20478, 32770, 24001,
                     "24000:RTPRx:Stop"):
            for builder in (v.tx_uri, v.rx_uri):
                with self.subTest(port=port, builder=builder.__name__), \
                     self.assertRaises(v.VoiceTransportError):
                    builder("192.0.2.10", port)
        for volume in (True, False, "20", 20.0, -1, 101, "20&x=1"):
            with self.subTest(volume=volume), self.assertRaises(v.VoiceTransportError):
                v.rx_uri("192.0.2.10", 24000, volume)


class G711PacketTests(unittest.TestCase):
    def test_known_mulaw_vectors_and_pcm_byte_order(self):
        self.assertEqual(v.decode_ulaw(bytes((0xff, 0x7f, 0x00, 0x80))),
                         struct.pack("<hhhh", 0, 0, -32124, 32124))
        self.assertEqual(v.encode_ulaw(struct.pack("<hhh", 0, -32124, 32124)),
                         bytes((0xff, 0x00, 0x80)))
        self.assertEqual(v.encode_ulaw(struct.pack("<hh", -32768, 32767)), bytes((0x00, 0x80)))
        self.assertEqual(v.decode_ulaw(SILENCE), b"\0" * 320)
        with self.assertRaises(v.VoiceTransportError):
            v.encode_ulaw(b"\0")

    def test_packet_sender_rollover_and_marker(self):
        sender = v.RtpSender(sequence=65535, timestamp=0xffffffa0, ssrc=0x12345678)
        first = v.parse_rtp(sender.packet(SILENCE))
        second = v.parse_rtp(sender.packet(b"\x7f" * 160))
        self.assertEqual((first.sequence, first.timestamp, first.ssrc, first.payload),
                         (65535, 0xffffffa0, 0x12345678, SILENCE))
        self.assertEqual((second.sequence, second.timestamp, second.ssrc, second.payload),
                         (0, 64, 0x12345678, b"\x7f" * 160))
        self.assertTrue(first.marker)
        self.assertFalse(second.marker)
        for size in (0, 1, 159, 161, 2048):
            with self.subTest(size=size), self.assertRaises(v.VoiceTransportError):
                sender.packet(b"\xff" * size)

    def test_parser_handles_csrc_extension_and_padding_without_leaking_headers(self):
        # One CSRC, a one-word extension, four bytes of padding, and marker bit.
        extra = struct.pack("!IHHI", 11, 0xabcd, 1, 0x12345678)
        wire = packet(first=0xb1, second=0x80, extra=extra, payload=SILENCE + b"\0\0\0\4")
        parsed = v.parse_rtp(wire)
        self.assertEqual(parsed.payload, SILENCE)
        self.assertEqual((parsed.sequence, parsed.timestamp, parsed.ssrc, parsed.marker),
                         (1, 160, 7, True))

    def test_parser_rejects_malformed_or_nonlegacy_packets(self):
        variants = [b"", b"\x80" * 11, packet(first=0x40), packet(second=8),
                    packet(payload=b"\0" * 159), packet(payload=b"\0" * 161),
                    packet(first=0x81, payload=b""),
                    packet(first=0x90, extra=struct.pack("!HH", 0xabcd, 0xffff)),
                    packet(first=0xa0, payload=SILENCE + b"\0"),
                    packet(first=0xa0, payload=SILENCE + b"\xff"),
                    packet(payload=b"\0" * 2048)]
        for wire in variants:
            with self.subTest(prefix=wire[:16], size=len(wire)), \
                 self.assertRaises(v.VoiceTransportError):
                v.parse_rtp(wire)


class ReceiverTests(unittest.TestCase):
    def test_invalid_or_wrong_source_first_packet_cannot_pin_session(self):
        receiver = v.RtpReceiver(SOURCE[0])
        self.assertEqual(receiver.ingest(packet(), ("192.0.2.21", SOURCE[1])), [])
        self.assertEqual(receiver.ingest(b"broken", SOURCE), [])
        self.assertEqual(receiver.ingest(packet(second=8), SOURCE), [])
        self.assertIsNone(receiver.source_port)
        self.assertIsNone(receiver.ssrc)
        self.assertEqual(receiver.ingest(packet(), SOURCE), [SILENCE])
        self.assertEqual((receiver.source_port, receiver.ssrc), (SOURCE[1], 7))
        self.assertEqual(receiver.counters["source_rejected"], 1)
        self.assertEqual(receiver.counters["malformed"], 2)

    def test_pinned_port_ssrc_and_explicit_source_port_do_not_change(self):
        receiver = v.RtpReceiver(SOURCE[0], expected_port=SOURCE[1])
        self.assertEqual(receiver.ingest(packet(), (SOURCE[0], SOURCE[1] + 2)), [])
        self.assertIsNone(receiver.ssrc)
        self.assertEqual(receiver.ingest(packet(), SOURCE), [SILENCE])
        for wrong, source in ((packet(2, 320, 8), SOURCE),
                              (packet(2, 320), (SOURCE[0], SOURCE[1] + 2)),
                              (packet(2, 320), ("192.0.2.21", SOURCE[1]))):
            self.assertEqual(receiver.ingest(wrong, source), [])
        self.assertEqual(receiver.ingest(packet(2, 320), SOURCE), [SILENCE])
        self.assertEqual((receiver.source_port, receiver.ssrc), (SOURCE[1], 7))

    def test_sequence_and_timestamp_wrap_are_a_continuous_stream(self):
        receiver = v.RtpReceiver(SOURCE[0])
        for sequence, timestamp in ((65534, 0xffffff60), (65535, 0), (0, 160), (1, 320)):
            with self.subTest(sequence=sequence):
                self.assertEqual(receiver.ingest(packet(sequence, timestamp), SOURCE), [SILENCE])
        self.assertEqual(receiver.flush(), [])

    def test_small_reordering_delivers_once_in_order(self):
        receiver = v.RtpReceiver(SOURCE[0], reorder_window=5)
        first, second, third = (bytes((x,)) * 160 for x in (0x10, 0x20, 0x30))
        self.assertEqual(receiver.ingest(packet(1, 160, payload=first), SOURCE), [first])
        self.assertEqual(receiver.ingest(packet(3, 480, payload=third), SOURCE), [])
        self.assertEqual(receiver.ingest(packet(3, 480, payload=third), SOURCE), [])
        self.assertEqual(receiver.ingest(packet(2, 320, payload=second), SOURCE), [second, third])
        self.assertEqual(receiver.ingest(packet(2, 320, payload=second), SOURCE), [])
        self.assertEqual(receiver.counters["duplicates"], 1)
        self.assertEqual(receiver.counters["late"], 1)
        self.assertEqual(receiver.flush(), [])

    def test_bad_timestamp_and_large_jump_do_not_poison_next_valid_frame(self):
        receiver = v.RtpReceiver(SOURCE[0], max_dropout=50)
        self.assertEqual(receiver.ingest(packet(1, 160), SOURCE), [SILENCE])
        self.assertEqual(receiver.ingest(packet(2, 321), SOURCE), [])
        self.assertEqual(receiver.ingest(packet(5000, 800000), SOURCE), [])
        self.assertEqual(receiver.ingest(packet(2, 320), SOURCE), [SILENCE])

    def test_gap_flush_inserts_only_bounded_silence_then_releases_payload(self):
        receiver = v.RtpReceiver(SOURCE[0], reorder_window=5, max_dropout=50)
        receiver.ingest(packet(1, 160), SOURCE)
        payload = b"\x20" * 160
        self.assertEqual(receiver.ingest(packet(4, 640, payload=payload), SOURCE), [])
        self.assertEqual(receiver.flush(), [SILENCE, SILENCE, payload])
        self.assertEqual(receiver.counters["lost"], 2)
        self.assertEqual(receiver.flush(), [])
        self.assertEqual(receiver.ingest(packet(60000, 9600000), SOURCE), [])
        self.assertEqual(receiver.flush(), [])

    def test_pending_audio_is_bounded_under_long_synthetic_loss_pattern(self):
        receiver = v.RtpReceiver(SOURCE[0], reorder_window=5, max_dropout=50)
        receiver.ingest(packet(1, 160), SOURCE)
        for sequence in range(3, 500, 2):
            frames = receiver.ingest(packet(sequence, sequence * 160), SOURCE)
            self.assertLessEqual(len(frames), 51)
            self.assertLessEqual(len(receiver.pending), 5)
        self.assertLessEqual(len(receiver.flush()), 5)


def bound_loopback_socket():
    """Reserve an available documented Cisco port without contacting a phone."""
    for port in range(20480, 32769, 2):
        connection = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            connection.bind(("127.0.0.1", port))
        except OSError:
            connection.close()
        else:
            connection.settimeout(1)
            return connection
    raise unittest.SkipTest("No free loopback UDP port in the documented Cisco range")


class UdpLoopbackTests(unittest.TestCase):
    def test_real_loopback_receive_decode_and_paced_send(self):
        peer = bound_loopback_socket()
        self.addCleanup(peer.close)
        reservation = bound_loopback_socket()
        bind_port = reservation.getsockname()[1]
        reservation.close()
        now = [100.0]
        media = v.UdpMedia("127.0.0.1", bind_port, "127.0.0.1",
                           phone_port=peer.getsockname()[1],
                           expected_source_port=peer.getsockname()[1],
                           clock=lambda: now[0])
        self.addCleanup(media.close)
        media.open()
        peer.sendto(packet(), ("127.0.0.1", bind_port))
        self.assertEqual(media.receive(), [b"\0" * 320])

        self.assertTrue(media.send_frame(SILENCE))
        first, source = peer.recvfrom(2048)
        self.assertEqual(source, ("127.0.0.1", bind_port))
        first = v.parse_rtp(first)
        self.assertEqual(first.payload, SILENCE)
        self.assertFalse(media.send_frame(SILENCE))
        now[0] += 0.021
        self.assertTrue(media.send_frame(SILENCE))
        second = v.parse_rtp(peer.recvfrom(2048)[0])
        self.assertEqual(second.sequence, (first.sequence + 1) & 0xffff)
        self.assertEqual(second.timestamp, (first.timestamp + 160) & 0xffffffff)
        self.assertEqual(second.ssrc, first.ssrc)

    def test_real_loopback_rejects_wrong_source_port_without_pinning(self):
        peer = bound_loopback_socket()
        wrong_peer = bound_loopback_socket()
        self.addCleanup(peer.close)
        self.addCleanup(wrong_peer.close)
        reservation = bound_loopback_socket()
        bind_port = reservation.getsockname()[1]
        reservation.close()
        media = v.UdpMedia("127.0.0.1", bind_port, "127.0.0.1",
                           expected_source_port=peer.getsockname()[1])
        self.addCleanup(media.close)
        media.open()
        wrong_peer.sendto(packet(), ("127.0.0.1", bind_port))
        self.assertEqual(media.receive(), [])
        self.assertIsNone(media.receiver.ssrc)
        peer.sendto(packet(), ("127.0.0.1", bind_port))
        self.assertEqual(media.receive(), [b"\0" * 320])
        self.assertEqual(media.receiver.ssrc, 7)
        self.assertEqual(media.receiver.counters["source_rejected"], 1)


class UdpBoundTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.media = v.UdpMedia("192.0.2.10", 24000, SOURCE[0], phone_port=24002,
                               max_packets=2, clock=lambda: self.now)
        self.connection = mock.Mock()
        self.media.sock = self.connection
        self.addCleanup(self.media.close)

    def test_deadline_blocks_socket_io_even_with_remaining_packet_budget(self):
        self.media.deadline = self.now
        for action in (self.media.receive, lambda: self.media.send_frame(SILENCE)):
            with self.assertRaisesRegex(v.VoiceTransportError, "duration_expired"):
                action()
        self.connection.recvfrom.assert_not_called()
        self.connection.sendto.assert_not_called()

    def test_five_minute_packet_budget_remains_finite_and_duration_sized(self):
        self.assertEqual(v.packet_budget(30), 1600)
        self.assertEqual(v.packet_budget(180), 9100)
        self.assertEqual(v.packet_budget(300), 15100)
        self.assertEqual(v.MAX_RTP_PACKETS, 15100)
        for duration in (0, -1, 301, True, '300', 10 ** 400, float('inf'), float('nan')):
            with self.subTest(duration=duration), self.assertRaises(v.VoiceTransportError):
                v.packet_budget(duration)
        for count in (0, True, 15101):
            with self.subTest(count=count), self.assertRaises(v.VoiceTransportError):
                v.UdpMedia('192.0.2.10', 24000, SOURCE[0], max_packets=count)

        self.media.max_packets = v.packet_budget(300)
        self.media.counters['sent'] = 15000
        self.assertTrue(self.media.send_frame(SILENCE))
        self.media.counters['sent'] = self.media.max_packets
        with self.assertRaisesRegex(v.VoiceTransportError, 'send_budget_exhausted'):
            self.media.send_frame(SILENCE)
        self.media.counters['datagrams'] = 15000
        self.connection.recvfrom.return_value = (b'bad', SOURCE)
        self.assertEqual(self.media.receive(), [])
        self.media.counters['datagrams'] = self.media.max_packets
        with self.assertRaisesRegex(v.VoiceTransportError, 'receive_budget_exhausted'):
            self.media.receive()

    def test_untrusted_datagrams_consume_finite_receive_budget(self):
        self.connection.recvfrom.return_value = (b"bad", ("192.0.2.21", 24000))
        self.assertEqual(self.media.receive(), [])
        self.assertEqual(self.media.receive(), [])
        with self.assertRaisesRegex(v.VoiceTransportError, "receive_budget_exhausted"):
            self.media.receive()
        self.assertEqual(self.connection.recvfrom.call_count, 2)
        self.assertEqual(self.media.counters["datagrams"], 2)
        self.assertEqual(self.media.receiver.counters["source_rejected"], 2)

    def test_send_budget_and_delayed_consumer_do_not_allow_catchup_burst(self):
        self.media.max_packets = 3
        self.assertTrue(self.media.send_frame(SILENCE))
        self.now += 1
        self.assertTrue(self.media.send_frame(SILENCE))
        self.assertFalse(self.media.send_frame(SILENCE))
        self.now += 1
        self.assertTrue(self.media.send_frame(SILENCE))
        self.now += 1
        with self.assertRaisesRegex(v.VoiceTransportError, "send_budget_exhausted"):
            self.media.send_frame(SILENCE)
        self.assertEqual(self.connection.sendto.call_count, 3)

    def test_lost_packet_wait_is_flushed_on_bounded_receive_timeout(self):
        self.media.max_packets = 5
        self.connection.recvfrom.side_effect = [
            (packet(1, 160), SOURCE), (packet(3, 480, payload=b"\x00" * 160), SOURCE),
            socket.timeout(), socket.timeout()]
        self.assertEqual(self.media.receive(), [b"\0" * 320])
        self.assertEqual(self.media.receive(), [])
        self.now += 0.05
        self.assertEqual(self.media.receive(), [])
        self.now += 0.06
        self.assertEqual(self.media.receive(), [b"\0" * 320, struct.pack("<h", -32124) * 160])
        self.assertEqual(self.media.receiver.pending, {})

    def test_close_discards_pending_audio_and_prevents_further_io(self):
        self.media.receiver.ingest(packet(1, 160), SOURCE)
        self.media.receiver.ingest(packet(3, 480), SOURCE)
        self.assertTrue(self.media.receiver.pending)
        self.media.close()
        self.assertEqual(self.media.receiver.pending, {})
        self.connection.close.assert_called_once()
        with self.assertRaisesRegex(v.VoiceTransportError, "media_closed"):
            self.media.receive()


class SessionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.media = v.UdpMedia("192.0.2.10", 24000, SOURCE[0], phone_port=24002)
        self.media.open = mock.Mock()
        self.media.close = mock.Mock()
        self.execute = mock.Mock()
        self.status = mock.Mock(return_value="idle")

    def session(self, **kwargs):
        options = {"duration": 2, "enable_microphone": True, "media_reserved": True,
                   "clock": lambda: self.now}
        options.update(kwargs)
        return v.VoiceSession(self.execute, self.media, self.status, **options)

    def assert_stops(self, *directions):
        observed = [call for call in self.execute.call_args_list
                    if call.args[0].endswith(":Stop")]
        self.assertEqual(observed, [mock.call(direction + ":Stop") for direction in directions])

    def test_construction_has_no_io_and_start_requires_explicit_mode_and_reservation(self):
        self.session()
        self.execute.assert_not_called()
        self.status.assert_not_called()
        self.media.open.assert_not_called()
        for options in ({"enable_microphone": False}, {"media_reserved": False}):
            with self.subTest(options=options), self.assertRaises(v.VoiceTransportError):
                self.session(**options).start()
        self.execute.assert_not_called()
        self.media.open.assert_not_called()

    def test_pilot_duration_and_explicit_flags_reject_coercion_before_io(self):
        invalid = [{"duration": value} for value in
                   (0, -1, 301, True, "10", 10 ** 400, float("nan"), float("inf"))]
        invalid += [{field: value} for field in
                    ("enable_microphone", "enable_playback", "media_reserved")
                    for value in (1, "true", None)]
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(v.VoiceTransportError):
                self.session(**options).start()
        self.execute.assert_not_called()
        self.media.open.assert_not_called()

    def test_start_and_explicit_stop_confirm_owned_direction_and_are_one_shot(self):
        session = self.session()
        self.assertIs(session.start(), session)
        self.assertEqual(session.state, "active")
        self.media.open.assert_called_once()
        self.execute.assert_called_once_with("RTPTx:192.0.2.10:24000")
        session.stop()
        self.assertEqual((session.state, session.reason), ("stopped", "explicit_stop"))
        self.assertTrue(session.stop_confirmed)
        self.assert_stops("RTPTx")
        self.media.close.assert_called_once()
        count = self.execute.call_count
        session.stop()
        self.assertFalse(session.check())
        self.assertEqual(self.execute.call_count, count)
        with self.assertRaises(v.VoiceTransportError):
            session.start()
        self.assertEqual(self.execute.call_count, count)

    def test_playback_only_explicit_opt_in_never_requests_microphone(self):
        session = self.session(enable_microphone=False, enable_playback=True, volume=17)
        session.start()
        self.execute.assert_called_once_with("RTPRx:192.0.2.10:24002:17")
        session.stop()
        self.assert_stops("RTPRx")

    def test_every_nonidle_status_refuses_start_before_media_commands(self):
        for status in ("call", "ringing", "radio", "disconnected", "unknown"):
            self.status.return_value = status
            with self.subTest(status=status), self.assertRaises(v.VoiceTransportError):
                self.session(enable_playback=True).start()
        self.execute.assert_not_called()
        self.media.open.assert_not_called()

    def test_partial_start_failure_attempts_both_stops_and_closes_media(self):
        def execute(uri):
            if uri.startswith("RTPRx:") and not uri.endswith(":Stop"):
                raise OSError("synthetic start failure")
        self.execute.side_effect = execute
        session = self.session(enable_playback=True)
        with self.assertRaises(OSError):
            session.start()
        self.assertEqual(session.state, "stopped")
        self.assert_stops("RTPTx", "RTPRx")
        self.media.close.assert_called_once()
        self.assertTrue(session.stop_confirmed)

    def test_ambiguous_first_start_stops_only_attempted_direction(self):
        def execute(uri):
            if uri == "RTPTx:192.0.2.10:24000":
                raise TimeoutError("synthetic ambiguous command result")
        self.execute.side_effect = execute
        session = self.session(enable_playback=True)
        with self.assertRaises(TimeoutError):
            session.start()
        self.assert_stops("RTPTx")

    def test_confirmed_first_rejection_does_not_stop_an_existing_stream(self):
        self.execute.side_effect = v.CommandRejected("synthetic_busy")
        session = self.session(enable_playback=True)
        with self.assertRaises(v.CommandRejected):
            session.start()
        self.assert_stops()
        self.assertEqual(self.execute.call_count, 1)
        self.assertEqual(session.state, "stopped")

    def test_confirmed_second_rejection_stops_only_accepted_first_direction(self):
        def execute(uri):
            if uri.startswith("RTPRx:") and not uri.endswith(":Stop"):
                raise v.CommandRejected("synthetic_busy")
        self.execute.side_effect = execute
        session = self.session(enable_playback=True)
        with self.assertRaises(v.CommandRejected):
            session.start()
        self.assert_stops("RTPTx")

    def test_failed_stop_does_not_skip_other_direction_or_report_confirmation(self):
        session = self.session(enable_playback=True)
        session.start()
        def execute(uri):
            if uri == "RTPTx:Stop":
                raise OSError("private failure contents must stay private")
        self.execute.side_effect = execute
        session.stop()
        self.assert_stops("RTPTx", "RTPRx")
        self.assertFalse(session.stop_confirmed)
        self.assertEqual(session.state, "stopped")
        self.assertTrue(session.stop_errors)
        self.assertNotIn("private failure", repr(session.stop_errors))
        self.media.close.assert_called_once()

    def test_local_close_failure_cannot_skip_remote_stop_commands(self):
        session = self.session(enable_playback=True)
        session.start()
        self.media.close.side_effect = OSError("synthetic socket close failure")
        session.stop()
        self.assert_stops("RTPTx", "RTPRx")
        self.assertEqual(session.state, "stopped")

    def test_context_manager_stops_when_consumer_raises(self):
        session = self.session()
        with self.assertRaisesRegex(RuntimeError, "synthetic consumer"):
            with session:
                raise RuntimeError("synthetic consumer")
        self.assert_stops("RTPTx")
        self.assertEqual(session.state, "stopped")

    def test_arm_failure_never_starts_phone_audio(self):
        arm = mock.Mock(side_effect=OSError("synthetic watchdog failure"))
        session = self.session(arm=arm)
        with self.assertRaises(OSError):
            session.start()
        self.execute.assert_not_called()
        self.media.close.assert_called_once()
        self.assertEqual(session.state, "stopped")

    def test_slow_status_check_cannot_start_audio_after_deadline(self):
        def status():
            self.now += 3
            return "idle"
        self.status.side_effect = status
        session = self.session()
        with self.assertRaisesRegex(v.VoiceTransportError, "duration_expired"):
            session.start()
        self.execute.assert_not_called()
        self.media.open.assert_not_called()

    def test_explicit_stop_during_status_check_cannot_be_overridden_by_idle_result(self):
        session = self.session()
        calls = []
        def status():
            calls.append(None)
            if len(calls) == 2:
                session.stop()
            return "idle"
        self.status.side_effect = status
        with self.assertRaises(v.VoiceTransportError):
            session.start()
        self.assertEqual((session.state, session.reason), ("stopped", "explicit_stop"))
        self.execute.assert_not_called()

    def test_call_arriving_between_direction_starts_prevents_playback(self):
        def execute(uri):
            if uri.startswith("RTPTx:") and not uri.endswith(":Stop"):
                self.status.return_value = "call"
        self.execute.side_effect = execute
        session = self.session(enable_playback=True)
        with self.assertRaisesRegex(v.VoiceTransportError, "preempted_call"):
            session.start()
        self.assertEqual(self.execute.call_args_list,
                         [mock.call("RTPTx:192.0.2.10:24000"),
                          mock.call("RTPTx:Stop")])

    def test_status_provider_failure_stops_active_session(self):
        session = self.session()
        session.start()
        self.status.side_effect = OSError("synthetic observer failure")
        self.assertFalse(session.check())
        self.assertEqual(session.reason, "preempted_unknown")
        self.assert_stops("RTPTx")

    def test_deadline_stops_media_and_does_not_extend_on_check(self):
        session = self.session()
        session.start()
        self.assertEqual(session.deadline, 102.0)
        self.now = 101.0
        self.assertTrue(session.check())
        self.assertEqual(session.deadline, 102.0)
        self.now = 102.0
        self.assertFalse(session.check())
        self.assertEqual(session.state, "stopped")
        self.assert_stops("RTPTx")

    def test_five_minute_session_arms_one_deadline_and_expires_without_extension(self):
        arm = mock.Mock()
        session = self.session(duration=300, arm=arm)
        session.start()
        arm.assert_called_once_with(400.0)
        self.assertEqual(self.media.deadline, 400.0)
        for now in (130.0, 280.0, 399.99):
            self.now = now
            self.assertTrue(session.check())
            self.assertEqual(session.deadline, 400.0)
        self.now = 400.0
        self.assertFalse(session.check())
        self.assertEqual(session.reason, 'duration_expired')
        self.assertTrue(session.stop_confirmed)
        self.assert_stops('RTPTx')
        arm.assert_called_once()

    def test_call_ringing_radio_disconnect_and_unknown_preempt_without_auto_resume(self):
        for state in ("call", "ringing", "radio", "disconnected", "unknown"):
            with self.subTest(state=state):
                self.execute.reset_mock()
                self.media.open.reset_mock()
                self.media.close.reset_mock()
                self.status.return_value = "idle"
                session = self.session(enable_playback=True)
                session.start()
                self.status.return_value = state
                self.assertFalse(session.check())
                self.assertEqual(session.state, "stopped")
                self.assert_stops("RTPTx", "RTPRx")
                self.status.return_value = "idle"
                count = self.execute.call_count
                self.assertFalse(session.check())
                self.assertEqual(self.execute.call_count, count)

    def test_pcm_boundary_and_call_preemption_before_consumer_io(self):
        pcm = b"\0" * 320
        self.media.receive = mock.Mock(return_value=[pcm])
        self.media.send_pcm = mock.Mock(return_value=True)
        session = self.session(enable_playback=True)
        session.start()
        self.assertEqual(session.poll(), [pcm])
        self.assertTrue(session.play(pcm))
        self.media.send_pcm.assert_called_once_with(pcm)
        self.status.return_value = "call"
        self.assertFalse(session.play(pcm))
        self.assertEqual(session.poll(), [])
        self.assertEqual(self.media.receive.call_count, 1)
        self.assertEqual(self.media.send_pcm.call_count, 1)
        self.assert_stops("RTPTx", "RTPRx")

    def test_queued_playback_rechecks_permission_after_obtaining_session_lock(self):
        pcm = b"\0" * 320
        self.media.send_pcm = mock.Mock(return_value=True)
        session = self.session(enable_playback=True)
        session.start()
        allowed, waiting = threading.Event(), threading.Event()
        allowed.set()
        original_lock = session._lock
        owner = threading.get_ident()
        results, failures, guard_values = [], [], []

        class ObservedLock:
            def __enter__(self):
                if threading.get_ident() != owner:
                    waiting.set()
                return original_lock.__enter__()

            def __exit__(self, *args):
                return original_lock.__exit__(*args)

        def guard():
            permitted = allowed.is_set()
            guard_values.append(permitted)
            return permitted

        def play():
            try:
                results.append(session.play(pcm, allowed=guard))
            except Exception as error:
                failures.append(error)

        session._lock = ObservedLock()
        worker = threading.Thread(target=play)
        with session._lock:
            worker.start()
            self.assertTrue(waiting.wait(1), "playback did not reach the held session lock")
            self.assertEqual(guard_values, [])
            allowed.clear()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results, [False])
        self.assertEqual(guard_values, [False])
        self.media.send_pcm.assert_not_called()
        self.assertEqual(session.state, "active")
        self.assert_stops()

    def test_playback_allows_literal_true_and_preserves_omitted_guard_behavior(self):
        pcm = b"\0" * 320
        self.media.send_pcm = mock.Mock(return_value=True)
        session = self.session(enable_playback=True)
        session.start()
        permitted = mock.Mock(return_value=True)
        self.assertTrue(session.play(pcm, allowed=permitted))
        permitted.assert_called_once_with()
        self.assertTrue(session.play(pcm))
        self.assertEqual(self.media.send_pcm.call_args_list, [mock.call(pcm), mock.call(pcm)])
        self.assertEqual(session.state, "active")
        self.assert_stops()

    def test_playback_guard_requires_literal_true_and_does_not_stop_session(self):
        self.media.send_pcm = mock.Mock(return_value=True)
        session = self.session(enable_playback=True)
        session.start()
        for value in (False, None, 0, 1, "true", object()):
            with self.subTest(value=value):
                self.assertFalse(session.play(b"\0" * 320, allowed=lambda: value))
                self.assertEqual(session.state, "active")
        self.media.send_pcm.assert_not_called()
        self.assert_stops()

    def test_playback_cancellation_during_status_check_is_observed_before_send(self):
        self.media.send_pcm = mock.Mock(return_value=True)
        session = self.session(enable_playback=True)
        session.start()
        allowed = threading.Event()
        allowed.set()
        def status():
            allowed.clear()
            return "idle"
        self.status.side_effect = status
        self.assertFalse(session.play(b"\0" * 320, allowed=allowed.is_set))
        self.media.send_pcm.assert_not_called()
        self.assertEqual(session.state, "active")
        self.assert_stops()

    def test_playback_guard_exception_fails_closed_without_stopping_session(self):
        self.media.send_pcm = mock.Mock(return_value=True)
        session = self.session(enable_playback=True)
        session.start()
        guard = mock.Mock(side_effect=RuntimeError("synthetic guard failure"))
        self.assertFalse(session.play(b"\0" * 320, allowed=guard))
        guard.assert_called_once_with()
        self.media.send_pcm.assert_not_called()
        self.assertEqual(session.state, "active")
        self.assert_stops()

    def test_media_consumer_failure_stops_owned_direction(self):
        self.media.receive = mock.Mock(side_effect=OSError("synthetic media failure"))
        session = self.session()
        session.start()
        self.assertEqual(session.poll(), [])
        self.assertEqual(session.state, "stopped")
        self.assert_stops("RTPTx")

    def test_concurrent_stop_follows_inflight_start_without_reviving_session(self):
        entered, release, stop_requested = (threading.Event() for _ in range(3))
        failures = []
        def execute(uri):
            if uri == "RTPTx:192.0.2.10:24000":
                entered.set()
                if not release.wait(1):
                    raise RuntimeError("synthetic start release timed out")
        self.execute.side_effect = execute
        session = self.session()
        def start():
            try:
                session.start()
            except Exception as error:
                failures.append(error)
        def stop():
            stop_requested.set()
            try:
                session.stop()
            except Exception as error:
                failures.append(error)
        starter = threading.Thread(target=start)
        stopper = threading.Thread(target=stop)
        starter.start()
        try:
            self.assertTrue(entered.wait(1))
            stopper.start()
            self.assertTrue(stop_requested.wait(1))
        finally:
            release.set()
            starter.join(2)
            if stopper.ident is not None:
                stopper.join(2)
        self.assertFalse(starter.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(session.state, "stopped")
        self.assert_stops("RTPTx")
        self.assertEqual(self.execute.call_args_list[-1], mock.call("RTPTx:Stop"))


class GatewayAndPilotTests(unittest.TestCase):
    def test_ami_status_checks_channels_and_registration_on_both_lines(self):
        gateway = v._gateway_module()
        phone = {"lines": ["201", "202"]}
        ami = mock.MagicMock()
        ami.__enter__.return_value = ami
        with mock.patch.object(gateway, "AMI", return_value=ami):
            ami.snapshot.return_value = ({"201": "Not in use", "202": "Not in use"}, [])
            self.assertEqual(v.gateway_phone_status(gateway, {"ami": {}}, phone), "idle")
            for line in phone["lines"]:
                ami.snapshot.return_value = (
                    {"201": "Not in use", "202": "Not in use"},
                    [{"Channel": "PJSIP/" + line + "-00000001", "ChannelStateDesc": "Up"}])
                self.assertEqual(v.gateway_phone_status(gateway, {"ami": {}}, phone), "call")
            for bad_state in (None, "Unavailable", "Invalid"):
                ami.snapshot.return_value = ({"201": "Not in use", "202": bad_state}, [])
                self.assertEqual(v.gateway_phone_status(gateway, {"ami": {}}, phone), "disconnected")
            ami.snapshot.return_value = ({"201": "Not in use", "202": "Ringing"}, [])
            self.assertEqual(v.gateway_phone_status(gateway, {"ami": {}}, phone), "call")

    def run_main(self, args, *, failure=None):
        config = {"duration_seconds": 2}
        result = {"error": None, "stop_confirmed": True, "watchdog_finished": True,
                  "cleanup_confirmed": True}
        with mock.patch.object(v, "load_pilot_config", return_value=(config, {}, {})) as load, \
             mock.patch.object(v, "run_pilot", return_value=result, side_effect=failure) as run, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            code = v.main(["--config", "unused-private-config.json", *args])
        return code, json.loads(output.getvalue()), load, run

    def test_default_dry_run_never_invokes_live_pilot_even_with_media_flags(self):
        for args in ([], ["--enable-microphone"], ["--playback-tone"]):
            with self.subTest(args=args):
                code, result, load, run = self.run_main(args)
                self.assertEqual(code, 0)
                self.assertEqual(result["mode"], "dry_run")
                self.assertEqual(result["network_actions"], 0)
                load.assert_called_once()
                run.assert_not_called()

    def test_live_pilot_requires_attended_reservation_and_explicit_media(self):
        for args in (["--run-pilot"],
                     ["--run-pilot", "--attended", "--enable-microphone"],
                     ["--run-pilot", "--exclusive-media", "--enable-microphone"],
                     ["--run-pilot", "--attended", "--exclusive-media"]):
            with self.subTest(args=args):
                code, result, load, run = self.run_main(args)
                self.assertEqual(code, 2)
                self.assertEqual(result["network_actions"], 0)
                load.assert_not_called()
                run.assert_not_called()

    def test_live_flags_pass_only_explicit_modes(self):
        code, _result, _load, run = self.run_main(
            ["--run-pilot", "--attended", "--exclusive-media", "--playback-tone"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.kwargs, {"enable_microphone": False, "playback_tone": True})

    def test_failure_after_live_pilot_entry_cannot_claim_zero_network_actions(self):
        code, result, _load, run = self.run_main(
            ["--run-pilot", "--attended", "--exclusive-media", "--enable-microphone"],
            failure=OSError("private configuration must not be printed"))
        self.assertNotEqual(code, 0)
        self.assertNotEqual(result.get("network_actions"), 0)
        self.assertNotIn("private configuration", json.dumps(result))
        run.assert_called_once()


class WatchdogTests(unittest.TestCase):
    TX = "RTPTx:192.0.2.10:24000"
    RX = "RTPRx:192.0.2.10:24002:20"

    def supervise(self, messages, execute=None, *, send_error_at=None):
        pipe = mock.Mock()
        pipe.poll.return_value = True
        pipe.recv.side_effect = [
            ("execute", index + 1, message[1]) if isinstance(message, tuple) else message
            for index, message in enumerate(messages)]
        sent = []
        def send(value):
            sent.append(value)
            if send_error_at is not None and len(sent) == send_error_at:
                raise BrokenPipeError("synthetic lost parent")
        pipe.send.side_effect = send
        execute = execute or mock.Mock()
        sleep = mock.Mock()
        result = v._watchdog_loop(pipe, execute, 30, (self.TX, self.RX),
                                  clock=lambda: 0, sleep=sleep, retry_delay=0.01)
        pipe.close.assert_called_once()
        return result, execute, sent, sleep

    def test_confirmed_rejection_cannot_obtain_cleanup_ownership(self):
        execute = mock.Mock(side_effect=v.CommandRejected("synthetic_busy"))
        result, execute, sent, sleep = self.supervise(
            [("execute", self.TX), "finish"], execute)
        execute.assert_called_once_with(self.TX)
        self.assertEqual(sent[1], {"request_id": 1, "result": "rejected"})
        self.assertFalse(result["watchdog_triggered"])
        sleep.assert_not_called()

    def test_second_direction_rejection_only_cleans_accepted_first(self):
        def action(uri):
            if uri == self.RX:
                raise v.CommandRejected("synthetic_busy")
        result, execute, _sent, sleep = self.supervise(
            [("execute", self.TX), ("execute", self.RX), "finish"], mock.Mock(side_effect=action))
        self.assertEqual(execute.call_args_list,
                         [mock.call(self.TX), mock.call(self.RX),
                          mock.call("RTPTx:Stop"), mock.call("RTPTx:Stop")])
        self.assertTrue(result["watchdog_triggered"])
        sleep.assert_called_once_with(0.01)

    def test_confirmed_stop_releases_ownership_without_global_cleanup(self):
        result, execute, _sent, sleep = self.supervise(
            [("execute", self.TX), ("execute", "RTPTx:Stop"), "finish"])
        self.assertEqual(execute.call_args_list, [mock.call(self.TX), mock.call("RTPTx:Stop")])
        self.assertFalse(result["watchdog_triggered"])
        sleep.assert_not_called()

    def test_ambiguous_start_keeps_delayed_cleanup_after_immediate_stop(self):
        def action(uri):
            if uri == self.TX:
                raise TimeoutError("synthetic uncertain start")
        result, execute, sent, sleep = self.supervise(
            [("execute", self.TX), ("execute", "RTPTx:Stop"), "finish"],
            mock.Mock(side_effect=action))
        self.assertEqual(sent[1], {"request_id": 1, "result": "error"})
        self.assertEqual(execute.call_args_list, [mock.call(self.TX)] + [mock.call("RTPTx:Stop")] * 3)
        self.assertTrue(result["watchdog_triggered"])
        sleep.assert_called_once_with(0.01)

    def test_parent_eof_or_lost_reply_cleans_owned_direction(self):
        for messages, send_error_at in (([("execute", self.TX), EOFError()], None),
                                         ([("execute", self.TX)], 2)):
            with self.subTest(send_error_at=send_error_at):
                result, execute, _sent, _sleep = self.supervise(messages, send_error_at=send_error_at)
                self.assertEqual(execute.call_args_list,
                                 [mock.call(self.TX), mock.call("RTPTx:Stop"), mock.call("RTPTx:Stop")])
                self.assertTrue(result["watchdog_triggered"])

    def test_unlisted_start_and_unowned_stop_cannot_reach_execute(self):
        result, execute, sent, _sleep = self.supervise(
            [("execute", "RTPTx:192.0.2.99:24000"), ("execute", "RTPRx:Stop"), "finish"])
        execute.assert_not_called()
        self.assertEqual(sent[1:3], [{"request_id": 1, "result": "rejected"},
                                   {"request_id": 2, "result": "ok"}])
        self.assertFalse(result["watchdog_triggered"])

    def test_command_arriving_at_deadline_cannot_start_audio(self):
        now = [0]
        pipe = mock.Mock()
        def poll(_timeout):
            now[0] = 30
            return True
        pipe.poll.side_effect = poll
        pipe.recv.return_value = ("execute", 1, self.TX)
        execute = mock.Mock()
        result = v._watchdog_loop(pipe, execute, 30, (self.TX,), clock=lambda: now[0], sleep=mock.Mock())
        execute.assert_not_called()
        self.assertFalse(result["watchdog_triggered"])

    def test_hard_execute_timeout_terminates_and_kills_stuck_worker(self):
        context = mock.Mock()
        parent, child, worker = mock.Mock(), mock.Mock(), mock.Mock()
        context.Pipe.return_value = parent, child
        context.Process.return_value = worker
        parent.poll.return_value = False
        worker.pid = 123
        worker.is_alive.return_value = True
        with mock.patch.object(v.multiprocessing, "get_context", return_value=context), \
             self.assertRaisesRegex(v.VoiceTransportError, "execute_timeout"):
            v._bounded_execute({}, self.TX, 30)
        worker.start.assert_called_once()
        worker.terminate.assert_called_once()
        worker.kill.assert_called_once()
        parent.close.assert_called_once()
        self.assertTrue(child.close.called)

    def test_execute_worker_trusts_only_documented_definite_rejections(self):
        for code in (1, 2, 3, 4, 5, 6, 0, 7, True, "1", None):
            pipe = mock.Mock()
            gateway = mock.Mock()
            error = OSError("synthetic private response")
            error.response_code = code
            gateway.execute.side_effect = error
            with self.subTest(code=code), \
                 mock.patch.object(v, "_gateway_module", return_value=gateway), \
                 mock.patch.object(v.signal, "signal"), \
                 mock.patch.object(v.time, "monotonic", return_value=0):
                v._execute_worker(pipe, {}, self.TX, 30)
            expected = "rejected" if type(code) is int and code in (1, 4) else "error"
            pipe.send.assert_called_once_with(expected)
            pipe.close.assert_called_once()

    def test_late_reply_cannot_acknowledge_a_different_command(self):
        watchdog = v._Watchdog({}, (self.TX,))
        watchdog.pipe = mock.Mock()
        watchdog.pipe.poll.return_value = True
        watchdog.pipe.recv.side_effect = [
            {"request_id": 0, "result": "ok"}, {"request_id": 1, "result": "rejected"}]
        with self.assertRaises(v.CommandRejected):
            watchdog.execute(self.TX)
        watchdog.pipe.send.assert_called_once_with(("execute", 1, self.TX))
        self.assertEqual(watchdog.pipe.recv.call_count, 2)

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX parent-exit semantics")
    def test_real_supervisor_survives_abrupt_parent_exit_without_network(self):
        script = textwrap.dedent("""
            import importlib.util, json, multiprocessing, os, pathlib, sys, time
            spec = importlib.util.spec_from_file_location('voice_parent_exit_test', sys.argv[1])
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            output = pathlib.Path(sys.argv[2])
            parent, child = multiprocessing.Pipe()
            tx = 'RTPTx:192.0.2.10:24000'
            pid = os.fork()
            if pid == 0:
                parent.close()
                def write(value):
                    with output.open('a') as target:
                        target.write(json.dumps(value) + '\\n')
                result = module._watchdog_loop(child, lambda uri: write({'uri': uri}),
                    time.monotonic() + 2, (tx,), retry_delay=0.01)
                write(result)
                os._exit(0)
            child.close()
            if not parent.poll(1) or parent.recv() != {'ready': True}:
                os._exit(2)
            parent.send(('execute', 1, tx))
            if not parent.poll(1) or parent.recv() != {'request_id': 1, 'result': 'ok'}:
                os._exit(3)
            os._exit(0)
        """)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "synthetic-watchdog.jsonl"
            run = subprocess.run([sys.executable, "-c", script,
                                  str(ROOT / "containers/asterisk/phone_voice_transport.py"), str(output)],
                                 capture_output=True, text=True, timeout=5)
            self.assertEqual(run.returncode, 0, run.stderr)
            events = [json.loads(line) for line in output.read_text().splitlines()]
        self.assertEqual([event["uri"] for event in events if "uri" in event],
                         [self.TX, "RTPTx:Stop", "RTPTx:Stop"])
        self.assertTrue(events[-1]["watchdog_triggered"])
        self.assertEqual(events[-1]["watchdog_stop_errors"], 0)


if __name__ == "__main__":
    unittest.main()
