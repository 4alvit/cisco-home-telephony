"""Actual loopback SIP/RTP and real H.264 decode, without phones or audio."""
import hashlib
import json
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools.sip_video.demo import (
    DemoError, SyntheticCaller, dependencies, packetize, run_demo,
)
from tools.sip_video.signaling import SipVideoServer


ROOT = Path(__file__).resolve().parents[1]


class SyntheticPeerTests(unittest.TestCase):
    def test_packetizer_single_nal_and_fragmented_nal_have_same_access_unit(self):
        data = b"\x00\x00\x00\x01\x09\xf0\x00\x00\x01\x65" + b"\x55" * 130
        packets = packetize(data, payload_type=110, mtu=64)
        self.assertEqual(len(packets), 4)
        headers = [struct.unpack("!BBHII", p[:12]) for p in packets]
        self.assertEqual([h[2] for h in headers], [1000, 1001, 1002, 1003])
        self.assertEqual({h[3] for h in headers}, {0})
        self.assertEqual([bool(h[1] & 128) for h in headers], [False, False, False, True])
        self.assertTrue(all(h[1] & 127 == 110 for h in headers))
        self.assertEqual(packets[1][12:14], b"\x7c\x85")
        self.assertEqual(packets[-1][13], 0x45)
        self.assertEqual(b"".join(p[14:] for p in packets[1:]), b"\x55" * 130)

    def test_packetizer_rejects_unbounded_or_wrong_inputs(self):
        for kwargs in ({"payload_type": 0}, {"payload_type": True}, {"mtu": 1},
                       {"mtu": 65535}):
            with self.subTest(kwargs=kwargs), self.assertRaises(DemoError):
                packetize(b"\x00\x00\x01\x65\xff", **kwargs)
        with self.assertRaises(DemoError):
            packetize(b"x" * (1024 * 1024 + 1))

    def test_non_2xx_ack_reuses_invite_branch(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sink:
            sink.bind(("127.0.0.1", 0))
            peer = SyntheticCaller(sink.getsockname()[1])
            try:
                peer.to += ";tag=synthetic-receiver"
                peer.invite_final = 487
                wire = peer.request("ACK")
                self.assertIn(b"branch=z9hG4bK-demo-invite", wire)
                peer.invite_final = 200
                self.assertIn(b"branch=z9hG4bK-demo-ack", peer.request("ACK"))
            finally:
                peer.close()

    def test_caller_closes_first_socket_if_second_creation_fails(self):
        first = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(first.close)
        with mock.patch("tools.sip_video.demo.socket.socket",
                        side_effect=[first, OSError("injected socket failure")]):
            with self.assertRaises(OSError):
                SyntheticCaller(1)
        self.assertEqual(first.fileno(), -1)

    def test_demo_closes_server_if_caller_constructor_fails(self):
        servers = []

        def make_server(*args, **kwargs):
            server = SipVideoServer(*args, **kwargs)
            servers.append(server)
            self.addCleanup(server.close)
            return server

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("tools.sip_video.demo.SipVideoServer", side_effect=make_server):
                with mock.patch("tools.sip_video.demo.SyntheticCaller",
                                side_effect=OSError("injected constructor failure")):
                    with self.assertRaises(OSError):
                        run_demo(Path(directory).resolve(), "cancel")
        self.assertEqual(len(servers), 1)
        state = servers[0].snapshot()
        self.assertTrue(state["socket_closed"])
        self.assertFalse(state["worker_alive"])

    def test_probe_absent_ffmpeg_is_explicit_failure(self):
        with mock.patch("tools.sip_video.demo.shutil.which", return_value=None):
            result = dependencies()
        self.assertFalse(any(result.values()))

    def test_output_must_be_fresh_and_never_overwrites_user_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve()
            existing = output / "keep.txt"
            existing.write_text("preserve")
            with self.assertRaises(DemoError):
                run_demo(output, "cancel")
            self.assertEqual(existing.read_text(), "preserve")


class ProtocolIntegrationTests(unittest.TestCase):
    def scenario(self, scenario):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        output = Path(directory.name).resolve()
        result = run_demo(output, scenario)
        self.assertTrue(result["success"])
        self.assertEqual(result["before_fixture_cleanup"]["state"], "closed")
        self.assertTrue(result["after_cleanup"]["socket_closed"])
        self.assertFalse(result["after_cleanup"]["worker_alive"])
        if result["media"]:
            self.assertTrue(result["media"]["socket_closed"])
            self.assertFalse(result["media"]["worker_alive"])
            self.assertFalse(result["media"]["decoder_running"])
        self.assertEqual(json.loads((output / "result.json").read_text()), result)
        return result, output

    def test_real_udp_cancel_closes_without_creating_media(self):
        result, output = self.scenario("cancel")
        statuses = [(row["status"], row["method"]) for row in result["sip_responses"]]
        self.assertIn((180, "INVITE"), statuses)
        self.assertIn((200, "CANCEL"), statuses)
        self.assertIn((487, "INVITE"), statuses)
        self.assertIsNone(result["media"])
        self.assertFalse((output / "frames").exists())

    def test_real_udp_missing_ack_expires_and_discards_media(self):
        result, output = self.scenario("ack-timeout")
        self.assertEqual(result["before_fixture_cleanup"]["close_reason"], "ack_timeout")
        self.assertEqual(result["media"]["decoded_frames"], 0)
        self.assertEqual(result["media"]["frames"], [])
        self.assertFalse((output / "frames").exists())

    def test_real_udp_invalid_rtp_fails_closed_without_output(self):
        result, output = self.scenario("malformed-rtp")
        self.assertIsNotNone(result["media"]["error"])
        self.assertEqual(result["rtp_packets_sent"], 1)
        self.assertEqual(result["media"]["decoded_frames"], 0)
        self.assertFalse((output / "frames").exists())

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for real H264 media proof")
    def test_real_sip_h264_rtp_becomes_three_distinct_bounded_pngs(self):
        self.assertTrue(all(dependencies().values()), "FFmpeg lacks H264/PNG/libx264")
        result, output = self.scenario("complete")
        statuses = [(row["status"], row["method"]) for row in result["sip_responses"]]
        self.assertIn((200, "INVITE"), statuses)
        self.assertIn((200, "BYE"), statuses)
        self.assertEqual(result["before_fixture_cleanup"]["close_reason"], "bye")
        media = result["media"]
        self.assertIsNone(media["error"])
        self.assertGreater(result["rtp_packets_sent"], 3)
        self.assertEqual(media["packets_accepted"], result["rtp_packets_sent"])
        self.assertEqual(media["access_units"], 3)
        self.assertEqual(media["decoded_frames"], 3)
        self.assertEqual(len({row["sha256"] for row in media["frames"]}), 3)
        for row in media["frames"]:
            data = (output / "frames" / row["file"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), row["sha256"])
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack("!II", data[16:24]), (298, 156))
            self.assertEqual(data[24:26], bytes((8, 2)))  # 8-bit RGB
            self.assertEqual((output / "frames" / row["file"]).stat().st_mode & 0o777, 0o600)

    def test_cli_rejects_external_output_before_opening_sockets(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve() / "must-not-be-created"
            process = subprocess.run(
                [sys.executable, "-m", "tools.sip_video", "demo",
                 "--scenario", "cancel", "--output", str(output)],
                cwd=ROOT, capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(process.returncode, 2)
            self.assertFalse(json.loads(process.stdout)["success"])
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
