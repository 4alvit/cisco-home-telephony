"""Deterministic codec/protocol oracles, independent of sockets or Asterisk."""

import itertools
import struct
import unittest

from tools.l16_lab import protocol as p


def sdp(payload=102, mapping="L16/16000", *, direction="sendrecv"):
    return ("v=0\r\no=fixture 1 1 IN IP4 127.0.0.1\r\ns=L16 lab\r\n"
            "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
            f"m=audio 18000 RTP/AVP {payload}\r\na=rtpmap:{payload} {mapping}\r\n"
            f"a={direction}\r\n")


def report(fault="clean", **kwargs):
    fixture = p.fixture_packets(fault, **kwargs)
    return p.analyze_rtp(fixture.observations, fixture.codec, fixture.expected_samples)


def codes(result):
    return {item["code"] for item in result["findings"]}


class PCMTest(unittest.TestCase):
    def test_signed_wire_bytes_use_independent_known_literals(self):
        samples = (-32768, -257, -1, 0, 1, 258, 32767)
        big = bytes.fromhex("8000 feff ffff 0000 0001 0102 7fff")
        little = bytes.fromhex("0080 fffe ffff 0000 0100 0201 ff7f")
        self.assertEqual(p.pack_pcm16(samples), big)
        self.assertEqual(p.unpack_pcm16(big), samples)
        self.assertEqual(p.pack_pcm16(samples, "little"), little)
        self.assertEqual(p.unpack_pcm16(little, "little"), samples)
        self.assertNotEqual(p.unpack_pcm16(big, "little"), samples)
        self.assertNotEqual(big, little)

    def test_pcm_bounds_reject_odd_truncated_and_noninteger_samples(self):
        for invalid in ([], [32768], [-32769], [True], [1.0], [0] * 2049):
            with self.subTest(invalid=str(invalid)[:30]), self.assertRaises(p.ProtocolError):
                p.pack_pcm16(invalid)
        for invalid in (b"", b"a", b"abc", b"x" * 4098, bytearray(b"ab")):
            with self.subTest(length=len(invalid)), self.assertRaises(p.ProtocolError):
                p.unpack_pcm16(invalid)
        with self.assertRaises(p.ProtocolError):
            p.pack_pcm16([1], "native")

    def test_audiosocket_header_is_big_endian_and_16000_pcm_is_little_endian(self):
        little = bytes.fromhex("0201") * 320
        wire = p.audiosocket_frame(little)
        self.assertEqual(wire[:3], bytes.fromhex("12 0280"))
        self.assertEqual(wire[3:], little)
        parsed = p.parse_audiosocket_frame(wire)
        self.assertEqual(parsed.message_type, 0x12)
        self.assertEqual(p.unpack_pcm16(parsed.payload, "little"), (258,) * 320)
        self.assertNotEqual(parsed.payload, p.pack_pcm16((258,) * 320))

    def test_audiosocket_control_frames_and_invalid_lengths(self):
        for wire, kind in ((b"\x01\x00\x10" + bytes(range(16)), 1),
                           (b"\x00\x00\x00", 0), (b"\x03\x00\x01#", 3),
                           (b"\xff\x00\x01\x04", 255)):
            self.assertEqual(p.parse_audiosocket_frame(wire).message_type, kind)
        for wire in (b"", b"\x12\x00\x01x", b"\x12\x00\x04xx",
                     b"\x00\x00\x01x", b"\x01\x00\x02xx", b"\x03\x00\x01x",
                     b"\x11\x00\x02xx", b"\xff\x00\x00", b"\x00\x00\x00extra"):
            with self.subTest(wire=wire), self.assertRaises(p.ProtocolError):
                p.parse_audiosocket_frame(wire)


class SDPTest(unittest.TestCase):
    def test_dynamic_payload_comes_from_sdp_with_omitted_or_explicit_mono(self):
        for payload in (102, 118, 123):
            for mapping in ("L16/16000", "L16/16000/1", "l16/16000", "L16-256/16000"):
                parsed = p.parse_audio_sdp(sdp(payload, mapping).encode("ascii"))
                selected = p.select_l16(parsed)
                self.assertEqual(selected.payload_type, payload)
                self.assertEqual(selected.clock_rate, 16000)
                self.assertEqual(selected.channels, 1)
                self.assertEqual(parsed.address, "127.0.0.1")
                self.assertEqual(parsed.port, 18000)

    def test_stereo_other_rates_and_static_l16_are_not_16000_mono(self):
        for payload, mapping in ((102, "L16/16000/2"), (102, "L16/8000"),
                                 (10, "L16/44100/2"), (11, "L16/44100/1")):
            with self.subTest(mapping=mapping), self.assertRaises(p.ProtocolError):
                p.select_l16(p.parse_audio_sdp(sdp(payload, mapping)))
        for payload in (10, 11):
            with self.assertRaisesRegex(p.ProtocolError, "static"):
                p.parse_audio_sdp(sdp(payload))

    def test_g722_uses_16000_samples_but_8000_clock(self):
        parsed = p.parse_audio_sdp(sdp(9, "G722/8000"))
        codec = parsed.codecs[0]
        self.assertEqual(codec.sample_rate, 16000)
        self.assertEqual(codec.clock_rate, 8000)
        self.assertEqual(codec.timestamp_step(20), 160)
        self.assertEqual(p.select_l16(p.parse_audio_sdp(sdp())).timestamp_step(20), 320)
        with self.assertRaises(p.ProtocolError):
            p.parse_audio_sdp(sdp(9, "G722/16000"))

    def test_conflicting_unoffered_missing_and_bad_mappings_rejected(self):
        invalid = (
            sdp() + "a=rtpmap:102 L16/8000\r\n",
            sdp() + "a=rtpmap:103 L16/16000\r\n",
            sdp().replace("a=rtpmap:102 L16/16000\r\n", ""),
            sdp().replace("RTP/AVP 102", "RTP/AVP 102 102"),
            sdp().replace("L16/16000", "L16/16000/3"),
            sdp().replace("18000", "99999"),
            sdp().replace("127.0.0.1\r\nt=", "224.0.0.1\r\nt="),
            sdp().replace("c=IN IP4 127.0.0.1", "c=IN IP4 example.invalid"),
            sdp() + "m=audio 19000 RTP/AVP 9\r\n",
            sdp() + "a=inactive\r\n",
            sdp() + "\x00",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(p.ProtocolError):
                p.parse_audio_sdp(value)

    def test_media_address_and_direction_override_session_and_hold_parse(self):
        value = sdp(direction="recvonly").replace("t=0 0\r\n", "t=0 0\r\na=sendonly\r\n")
        value += "c=IN IP4 127.0.0.2\r\n"
        parsed = p.parse_audio_sdp(value)
        self.assertEqual(parsed.address, "127.0.0.2")
        self.assertEqual(parsed.direction, "recvonly")
        inactive = p.parse_audio_sdp(sdp(direction="inactive").replace("18000", "0"))
        self.assertEqual(inactive.port, 0)
        self.assertEqual(inactive.direction, "inactive")


class RTPWireTest(unittest.TestCase):
    def test_header_uses_independent_network_byte_order_literal(self):
        payload = bytes.fromhex("0102 8000")
        expected = bytes.fromhex("80e6 fffe ffffff80 12345678 0102 8000")
        self.assertEqual(p.make_rtp(payload, 102, 65534, 0xFFFFFF80, 0x12345678, True), expected)
        packet = p.parse_rtp(expected)
        self.assertEqual((packet.payload_type, packet.sequence, packet.timestamp, packet.ssrc),
                         (102, 65534, 0xFFFFFF80, 0x12345678))
        self.assertTrue(packet.marker)
        self.assertEqual(packet.payload, payload)

    def test_csrc_extension_and_padding_are_not_mistaken_for_pcm(self):
        header = struct.pack("!BBHII", 0xB1, 102, 1, 320, 1)
        wire = (header + b"CSRC" + bytes.fromhex("beef 0001") + b"ext!"
                + b"\x01\x02" + b"\x00\x02")
        self.assertEqual(p.parse_rtp(wire).payload, b"\x01\x02")

    def test_malformed_rtp_and_constructed_bounds(self):
        valid = p.make_rtp(b"ab", 102, 1, 320, 1)
        for wire in (b"", valid[:12], bytes([0]) + valid[1:],
                     bytes([0x8F]) + valid[1:], bytes([0x90]) + valid[1:],
                     bytes([0xA0]) + valid[1:-1] + b"\0",
                     struct.pack("!BBHIIHH", 0x90, 102, 1, 1, 1, 0, 65) + b"x" * 300):
            with self.subTest(wire=wire[:20]), self.assertRaises(p.ProtocolError):
                p.parse_rtp(wire)
        for args in ((b"", 102, 1, 1, 1), (b"a", 128, 1, 1, 1),
                     (b"a", 102, 65536, 1, 1), (b"a", 102, 1, -1, 1),
                     (b"a", 102, 1, 1, True), (b"a" * 4097, 102, 1, 1, 1)):
            with self.subTest(args=str(args)[:30]), self.assertRaises(p.ProtocolError):
                p.make_rtp(*args)


class DiagnosticsTest(unittest.TestCase):
    def test_clean_l16_wraps_sequence_and_timestamp_without_false_loss(self):
        fixture = p.fixture_packets(payload_type=123)
        sequences = [p.parse_rtp(wire).sequence for _, wire in fixture.observations]
        timestamps = [p.parse_rtp(wire).timestamp for _, wire in fixture.observations]
        self.assertEqual(sequences, [65533, 65534, 65535, 0, 1, 2])
        self.assertLess(timestamps[2], timestamps[1])
        result = p.analyze_rtp(fixture.observations, fixture.codec, fixture.expected_samples)
        self.assertTrue(result["ok"])
        self.assertEqual(result["missing_packets"], 0)
        self.assertEqual(result["expected_frame_bytes"], 640)
        self.assertEqual(result["expected_timestamp_step"], 320)
        self.assertEqual(result["clock_checks"], 5)
        self.assertEqual(result["pacing_intervals"], 5)
        self.assertEqual(result["byte_order"]["status"], "matched_big_endian")

    def test_each_mutant_has_a_specific_diagnostic(self):
        expected = {"byteorder": "little_endian_on_l16_wire", "payload-type": "payload_type",
                    "clock": "rtp_clock", "pacing": "packet_pacing", "drop": "packet_loss",
                    "reorder": "packet_reordering"}
        for fault, diagnostic in expected.items():
            with self.subTest(fault=fault):
                result = report(fault)
                self.assertFalse(result["ok"])
                self.assertIn(diagnostic, codes(result))
        self.assertEqual(report("drop")["missing_packets"], 1)
        self.assertEqual(report("reorder")["missing_packets"], 0)
        self.assertEqual(report("reorder")["reordered_packets"], 1)

    def test_g722_timestamp_mutant_is_not_confused_with_16000_decoded_audio(self):
        clean = report(encoding="G722", payload_type=9)
        self.assertTrue(clean["ok"])
        self.assertEqual(clean["sample_rate"], 16000)
        self.assertEqual(clean["rtp_clock_rate"], 8000)
        self.assertEqual(clean["expected_timestamp_step"], 160)
        self.assertEqual(clean["expected_frame_bytes"], 160)
        self.assertEqual(clean["negotiated_payload_type"], 9)
        self.assertEqual(clean["byte_order"]["status"], "not_checked")
        self.assertIn("rtp_clock", codes(report("clock", encoding="G722")))
        with self.assertRaises(p.ProtocolError):
            p.fixture_packets(payload_type=9, encoding="L16")

    def test_without_reference_byteorder_is_not_invented(self):
        swapped = p.fixture_packets("byteorder")
        unknown = p.analyze_rtp(swapped.observations, swapped.codec)
        self.assertTrue(unknown["ok"])
        self.assertEqual(unknown["byte_order"]["status"], "not_checked")
        no_match = p.analyze_rtp(swapped.observations, swapped.codec, (1,) * 320)
        self.assertEqual(no_match["byte_order"]["status"], "no_known_match")
        self.assertIn("known_pattern_mismatch", codes(no_match))
        self.assertNotIn("little_endian_on_l16_wire", codes(no_match))

    def test_symmetric_silence_is_ambiguous_not_endian_proof(self):
        fixture = p.fixture_packets()
        silence = [(arrival, wire[:12] + b"\0" * 640) for arrival, wire in fixture.observations]
        result = p.analyze_rtp(silence, fixture.codec, (0,) * 320)
        self.assertTrue(result["ok"])
        self.assertEqual(result["byte_order"]["status"], "ambiguous")
        self.assertEqual(result["byte_order"]["ambiguous_matches"], 6)

    def test_malformed_wrong_ssrc_size_and_duplicates_get_separate_findings(self):
        fixture = p.fixture_packets()
        observations = list(fixture.observations)
        observations[1] = (observations[1][0], observations[0][1])
        observations[2] = (observations[2][0], observations[2][1][:8] + b"xxxx" + observations[2][1][12:])
        observations[3] = (observations[3][0], observations[3][1][:-1])
        observations[4] = (observations[4][0], b"bad")
        result = p.analyze_rtp(observations, fixture.codec)
        self.assertEqual(result["duplicate_packets"], 1)
        self.assertEqual(result["ssrc_changes"], 1)
        self.assertEqual(result["frame_size_mismatches"], 1)
        self.assertEqual(result["malformed_packets"], 1)
        self.assertTrue({"duplicate_packet", "ssrc_change", "frame_size", "malformed_rtp"} <= codes(result))

    def test_timing_jitter_tolerance_and_empty_capture_are_explicit(self):
        fixture = p.fixture_packets()
        jitter = [(arrival + (0.003 if index % 2 else 0), wire)
                  for index, (arrival, wire) in enumerate(fixture.observations)]
        self.assertTrue(p.analyze_rtp(jitter, fixture.codec)["ok"])
        self.assertIn("packet_pacing", codes(p.analyze_rtp(jitter, fixture.codec, pacing_tolerance_ms=1)))
        empty = p.analyze_rtp([], fixture.codec)
        self.assertFalse(empty["ok"])
        self.assertIn("no_negotiated_media", codes(empty))

    def test_bounded_analysis_rejects_invalid_clock_observations_and_infinite_input(self):
        fixture = p.fixture_packets()
        valid = fixture.observations[0][1]
        for observations in ([(float("nan"), valid)], [(float("inf"), valid)],
                             [(True, valid)], [(2.0, valid), (1.0, valid)],
                             [(0.0, valid), (61.0, valid)], [(0.0, valid, "extra")]):
            with self.subTest(observations=str(observations)[:50]), self.assertRaises(p.ProtocolError):
                p.analyze_rtp(observations, fixture.codec)
        with self.assertRaisesRegex(p.ProtocolError, "packet limit"):
            p.analyze_rtp(itertools.repeat((0.0, valid)), fixture.codec)
        with self.assertRaises(p.ProtocolError):
            p.analyze_rtp(fixture.observations, fixture.codec, 12)
        with self.assertRaises(p.ProtocolError):
            p.analyze_rtp(fixture.observations, fixture.codec, pacing_tolerance_ms=float("nan"))
        with self.assertRaises(p.ProtocolError):
            p.analyze_rtp(fixture.observations, fixture.codec, packet_ms=True)


if __name__ == "__main__":
    unittest.main()
