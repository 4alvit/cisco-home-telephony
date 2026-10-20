"""Safety and wire tests for the owned, synthetic Asterisk laboratory."""
from contextlib import contextmanager, redirect_stdout
import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools.firmware_lab import storage
from tools.l16_lab import __main__ as cli
from tools.l16_lab import asterisk as lab
from tools.l16_lab import protocol


class PlanAndConfigurationTest(unittest.TestCase):
    def test_description_has_no_engine_or_network_side_effects(self):
        with patch.object(lab.subprocess, "run") as run, patch.object(lab.socket, "socket") as sock:
            plan = lab.describe_plan()
        run.assert_not_called()
        sock.assert_not_called()
        self.assertEqual(len(plan["cases"]), 5)
        self.assertEqual(plan["network"], "none")
        self.assertEqual(plan["published_ports"], [])
        self.assertFalse(plan["physical_phone_access"])

    def test_native_absolute_timeout_precedes_any_media_and_configuration_is_local(self):
        files = lab.fixture_configs()
        dialplan = files["extensions.conf"]
        self.assertLess(dialplan.index("TIMEOUT(absolute)="), dialplan.index("Answer()"))
        self.assertLess(dialplan.index("TIMEOUT(absolute)="), dialplan.index("AudioSocket/"))
        self.assertEqual(dialplan.count("TIMEOUT(absolute)="), 3)
        self.assertIn("exten => 9002,1,Set(TIMEOUT(absolute)=3)", dialplan)
        self.assertIn("[l16-sentinel]", dialplan)
        self.assertNotIn("Local/", dialplan)
        self.assertIn("/c(slin16)", dialplan)
        self.assertIn("bind=127.0.0.1:5060", files["pjsip.conf"])
        self.assertIn("allow=slin16,g722", files["pjsip.conf"])
        self.assertIn("match=127.0.0.2/32", files["pjsip.conf"])
        self.assertIn("enabled=no", files["manager.conf"])

    def test_real_staging_is_readable_to_container_user_under_private_or_standard_umask(self):
        class StagingInspected(Exception):
            pass

        @contextmanager
        def inert_container(_target):
            yield SimpleNamespace(create=Mock(return_value="a" * 64))

        for mask in (0o077, 0o022):
            with self.subTest(umask=oct(mask)):
                target = Mock()
                target.verify_local.return_value = target
                snapshots = []

                def inspect_copy(args, **_kwargs):
                    self.assertEqual(args[0], "cp")
                    self.assertEqual(args[2], "a" * 64 + ":/")
                    root = Path(args[1].removesuffix("/."))
                    self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
                    directories, files = [], []
                    for path in root.rglob("*"):
                        mode = stat.S_IMODE(path.stat().st_mode)
                        if path.is_dir():
                            directories.append(path.relative_to(root).as_posix())
                            self.assertEqual(mode, 0o755)
                            self.assertEqual(mode & stat.S_IXOTH, stat.S_IXOTH)
                        else:
                            files.append(path.relative_to(root).as_posix())
                            self.assertEqual(mode, 0o644)
                            self.assertEqual(mode & stat.S_IROTH, stat.S_IROTH)
                            self.assertTrue(path.read_bytes())
                    snapshots.append((directories, files))
                    raise StagingInspected

                target.command.side_effect = inspect_copy
                previous = os.umask(mask)
                try:
                    with patch.object(lab, "DockerTarget", return_value=target), \
                            patch.object(lab, "isolated_container", inert_container):
                        with self.assertRaises(StagingInspected):
                            lab.run_isolated("synthetic:local", docker_context="synthetic-lab")
                finally:
                    inherited_after = os.umask(previous)
                self.assertEqual(inherited_after, mask)
                self.assertEqual(target.command.call_count, 1)
                directories, files = snapshots[0]
                self.assertEqual(set(directories), {"etc", "etc/asterisk", "opt", "opt/l16-lab",
                                                    "opt/l16-lab/tools", "opt/l16-lab/tools/l16_lab"})
                expected = {"etc/asterisk/" + name for name in lab.fixture_configs()}
                expected.update({"opt/l16-lab/tools/l16_lab/asterisk.py",
                                 "opt/l16-lab/tools/l16_lab/protocol.py"})
                self.assertEqual(set(files), expected)

    def test_cli_reports_stay_private_under_private_or_standard_umask(self):
        report = {"status": "PASS", "synthetic_only": True}
        for mask in (0o077, 0o022):
            with self.subTest(umask=oct(mask)), tempfile.TemporaryDirectory() as temporary:
                repository = Path(temporary).resolve()
                destination = repository / ".local" / "l16" / "private-result"
                output = io.StringIO()

                def create_output(path):
                    return storage.create_private_output(path, repository=repository)

                previous = os.umask(mask)
                try:
                    with patch.object(cli, "create_private_output", side_effect=create_output), \
                            patch.object(lab, "run_isolated", return_value=report) as run, \
                            patch.object(lab, "DockerTarget") as docker, redirect_stdout(output):
                        result = cli.main([
                            "asterisk", "--image", "synthetic:local",
                            "--docker-context", "synthetic-lab", "--execute",
                            "--output", str(destination),
                        ])
                finally:
                    inherited_after = os.umask(previous)

                self.assertEqual(inherited_after, mask)
                self.assertEqual(result, 0)
                self.assertEqual(json.loads(output.getvalue()), {
                    "status": "PASS", "mode": "asterisk",
                    "report": str(destination / "report.json"),
                })
                run.assert_called_once_with("synthetic:local", docker_context="synthetic-lab",
                                            docker_socket=None)
                docker.assert_not_called()
                for directory in (repository / ".local", destination.parent, destination):
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
                saved = destination / "report.json"
                self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
                self.assertEqual(json.loads(saved.read_text()), report)

    def test_missing_or_ambiguous_engine_is_rejected(self):
        for kwargs in ({}, {"docker_context": "x", "docker_socket": "/tmp/socket"},
                       {"docker_context": "--host"}, {"docker_context": "x y"},
                       {"docker_socket": "tcp://host"}, {"docker_socket": "relative"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(lab.LabError):
                lab.DockerTarget(**kwargs)

    def test_ambient_docker_routing_is_removed(self):
        target = lab.DockerTarget(docker_context="synthetic-lab")
        env = {name: "unexpected" for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_API_VERSION")}
        with patch.dict(os.environ, env), patch.object(lab.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")) as run:
            target.command(["version"])
        args, kwargs = run.call_args
        self.assertEqual(args[0][:3], ["docker", "--context", "synthetic-lab"])
        self.assertTrue(all(name not in kwargs["env"] for name in env))

    def test_only_real_local_socket_context_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "engine.sock")
            with socket.socket(socket.AF_UNIX) as sock:
                sock.bind(path)
                target = lab.DockerTarget(docker_socket=path)
                self.assertIs(target.verify_local(), target)
                context = lab.DockerTarget(docker_context="synthetic-lab")
                context.command = Mock(return_value=SimpleNamespace(stdout=json.dumps("unix://" + path)))
                self.assertIs(context.verify_local(), context)
            for endpoint in ("tcp://127.0.0.1:2375", "ssh://localhost", "unix://relative"):
                context.command = Mock(return_value=SimpleNamespace(stdout=json.dumps(endpoint)))
                with self.subTest(endpoint=endpoint), self.assertRaises(lab.LabError):
                    context.verify_local()
            regular = Path(tmp) / "regular"
            regular.write_text("not a socket")
            with self.assertRaises(lab.LabError):
                lab.DockerTarget(docker_socket=str(regular)).verify_local()


class OwnedContainerTest(unittest.TestCase):
    def target(self):
        return SimpleNamespace(command=Mock())

    def metadata(self, owner, *, label=None, ports=None, network="none"):
        return SimpleNamespace(returncode=0, stdout=json.dumps([
            "a" * 64, "/" + owner.name,
            {lab.OWNER_LABEL: owner.nonce if label is None else label}, network, ports]))

    def test_create_never_publishes_ports_or_pulls_and_requires_ownership(self):
        target = self.target()
        owner = lab.OwnedContainer(target)
        target.command.side_effect = [SimpleNamespace(stdout="a" * 64 + "\n"), self.metadata(owner)]
        self.assertEqual(owner.create("synthetic:local"), "a" * 64)
        command = target.command.call_args_list[0].args[0]
        self.assertEqual(command[command.index("--pull") + 1], "never")
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertIn("--memory", command)
        self.assertIn("--pids-limit", command)
        self.assertIn("--cap-drop", command)
        self.assertEqual(command[command.index("--user") + 1], "10001:10001")
        for forbidden in ("--publish", "-p", "--privileged", "--volume", "-v", "--mount", "--network=host"):
            self.assertNotIn(forbidden, command)
        self.assertTrue(owner.created)

    def test_cleanup_refuses_wrong_owner_identity_network_or_ports(self):
        for changes in ({"label": "wrong"}, {"network": "bridge"}, {"ports": {"5060/udp": [{"HostPort": "5060"}]}}):
            target = self.target()
            owner = lab.OwnedContainer(target)
            owner.attempted, owner.created = True, True
            owner.identity = "a" * 64
            target.command.return_value = self.metadata(owner, **changes)
            with self.subTest(changes=changes), self.assertRaises(lab.LabError):
                owner.cleanup()
            self.assertFalse(any(call.args[0][0] == "rm" for call in target.command.call_args_list))

    def test_cleanup_removes_only_verified_identity_then_confirms_absence(self):
        target = self.target()
        owner = lab.OwnedContainer(target)
        owner.attempted, owner.created = True, True
        owner.identity = "a" * 64
        target.command.side_effect = [self.metadata(owner), SimpleNamespace(returncode=0), SimpleNamespace(stdout="")]
        owner.cleanup()
        self.assertEqual(target.command.call_args_list[1].args[0], ["rm", "-f", "a" * 64])
        self.assertEqual(target.command.call_args_list[2].args[0][0:2], ["container", "ls"])

    def test_uncertain_create_recovers_late_owned_container(self):
        target = self.target()
        owner = lab.OwnedContainer(target)
        owner.attempted = True
        target.command.side_effect = [SimpleNamespace(returncode=1), SimpleNamespace(stdout=""),
                                      self.metadata(owner), SimpleNamespace(returncode=0), SimpleNamespace(stdout="")]
        with patch.object(lab.time, "sleep"):
            owner.cleanup()
        self.assertEqual(target.command.call_args_list[-2].args[0], ["rm", "-f", "a" * 64])

    def test_uncertain_create_absence_cannot_report_cleanup_success(self):
        target = self.target()
        owner = lab.OwnedContainer(target)
        owner.attempted = True
        target.command.side_effect = [SimpleNamespace(returncode=1), SimpleNamespace(stdout="")]
        with patch.object(lab.time, "monotonic", side_effect=[0, 11]), self.assertRaisesRegex(lab.LabError, "unconfirmed"):
            owner.cleanup()


class WireAndLifecycleTest(unittest.TestCase):
    def test_l16_directional_patterns_are_repeatable_and_nonoverlapping(self):
        incoming = lab.pcm_pattern("incoming", "L16")
        outgoing = lab.pcm_pattern("outgoing", "L16")
        self.assertEqual(incoming, lab.pcm_pattern("incoming", "L16"))
        self.assertEqual(outgoing, lab.pcm_pattern("outgoing", "L16"))
        self.assertEqual(len(incoming), lab.FRAME_COUNT * 640)
        self.assertNotIn(incoming[6400:32000], outgoing)
        self.assertNotIn(outgoing[6400:32000], incoming)

    def test_duplex_oracle_accepts_correct_path_and_rejects_swaps_or_reflection(self):
        incoming = lab.pcm_pattern("incoming", "L16")
        outgoing = lab.pcm_pattern("outgoing", "L16")

        def big_endian(pcm):
            return b"".join(pcm[index:index + 2][::-1] for index in range(0, len(pcm), 2))

        result = lab._check_l16_duplex(big_endian(outgoing), incoming)
        self.assertEqual(result["duplex_exact_samples"], 12800)
        for label, rtp, audio in (
            ("swapped directions", big_endian(incoming), outgoing),
            ("incoming reflected to RTP", big_endian(incoming), incoming),
            ("outgoing reflected to AudioSocket", big_endian(outgoing), outgoing),
            ("missing RTP", b"", incoming),
            ("missing AudioSocket", big_endian(outgoing), b""),
            ("wrong RTP byte order", outgoing, incoming),
        ):
            with self.subTest(label=label), self.assertRaisesRegex(lab.LabError, "byte identity"):
                lab._check_l16_duplex(rtp, audio)

    def test_run_case_rejects_reflected_media_without_any_bridge(self):
        # Each side receives its own signal. This must not establish duplex
        # delivery, even though packet counts, clocks and cleanup look valid.
        pcm = lab.pcm_pattern("incoming", "L16")
        reflected = bytearray(pcm)
        reflected[0::2], reflected[1::2] = reflected[1::2], reflected[0::2]
        codec = protocol.parse_audio_sdp(lab._sdp(102, "L16")).codecs[0]
        packets = [(index * .02, protocol.make_rtp(
            bytes(reflected[index * 640:(index + 1) * 640]),
            102, 1000 + index, 100000 + index * 320, 123456))
            for index in range(lab.FRAME_COUNT)]
        peer = SimpleNamespace(error=None, confirmed=True, bye=True, payload=102,
                               codec=codec, packets=packets, rtp=SimpleNamespace(sendto=Mock()),
                               target_rtp=("127.0.0.1", 10000), dial=Mock(), close=Mock(),
                               offer_sdp="synthetic", answer_sdp="synthetic")

        def self_echo_audio(outgoing):
            return SimpleNamespace(error=None, connected=True, ended=True,
                                   received=bytearray(outgoing), frames=lab.FRAME_COUNT, close=Mock())

        with patch.object(lab, "SipPeer", return_value=peer), \
                patch.object(lab, "AudioSocketPeer", side_effect=self_echo_audio), \
                patch.object(lab, "_no_channels", return_value=True), patch.object(lab.time, "sleep"):
            with self.assertRaisesRegex(lab.LabError, "byte identity"):
                lab.run_case("reflection_regression", True, "L16", 102)

    def test_outgoing_uses_offered_l16_payload_and_incoming_does_not_accept_remapping(self):
        peer = object.__new__(lab.SipPeer)
        peer.codec_name, peer.incoming, peer.payload = "L16", False, None
        body = lab._sdp(117, "L16", 10002).replace("127.0.0.2", "127.0.0.1")
        self.assertEqual(peer._select(body).payload_type, 117)
        self.assertEqual(peer.target_rtp, ("127.0.0.1", 10002))
        peer.incoming, peer.payload = True, 102
        with self.assertRaisesRegex(lab.LabError, "changed offered"):
            peer._select(body)
        peer.payload = 117
        for changed in (body.replace("127.0.0.1", "192.0.2.1"), body.replace("10002", "5050")):
            with self.assertRaises(lab.LabError):
                peer._select(changed)

    def test_incoming_answer_rejects_unoffered_codec_rate_channels_or_direction(self):
        peer = object.__new__(lab.SipPeer)
        peer.codec_name, peer.incoming, peer.payload = "L16", True, 102
        body = lab._sdp(102, "L16", 10002).replace("127.0.0.2", "127.0.0.1")
        invalid = [body.replace("RTP/AVP 102", "RTP/AVP 102 9") + "a=rtpmap:9 G722/8000\r\n",
                   body.replace("L16/16000/1", "L16/8000/1"),
                   body.replace("L16/16000/1", "L16/16000/2"),
                   body.replace("a=sendrecv", "a=sendonly")]
        for answer in invalid:
            with self.subTest(answer=answer), self.assertRaises((lab.LabError, protocol.ProtocolError)):
                peer._select(answer)

    def test_sip_second_socket_failure_closes_first_socket(self):
        first = Mock()
        with patch.object(lab.socket, "socket", side_effect=[first, OSError("injected creation failure")]), self.assertRaises(OSError):
            lab.SipPeer("L16", 102, True)
        first.close.assert_called_once()

    def test_audiosocket_setup_failure_closes_socket(self):
        listener = Mock()
        listener.setsockopt.side_effect = OSError("injected setup failure")
        with patch.object(lab.socket, "socket", return_value=listener), self.assertRaises(OSError):
            lab.AudioSocketPeer(b"x" * 640)
        listener.close.assert_called_once()

    def test_audiosocket_close_before_connect_stops_worker(self):
        with patch.object(lab, "AUDIO_PORT", 0):
            peer = lab.AudioSocketPeer(b"x" * 640)
            peer.close()
        self.assertFalse(peer.thread.is_alive())
        self.assertEqual(peer.listener.fileno(), -1)
        self.assertIsNone(peer.error)

    def test_real_fragmented_audiosocket_handshake_and_duplex_little_endian_frames(self):
        samples = [1000, -2000, 3000, -4000] * 80
        outgoing = protocol.pack_pcm16(samples, "little")
        incoming = protocol.pack_pcm16(list(reversed(samples)), "little")
        with patch.object(lab, "AUDIO_PORT", 0), patch.object(lab, "FRAME_COUNT", 3):
            peer = lab.AudioSocketPeer(outgoing * 3)
            try:
                with socket.create_connection(peer.listener.getsockname(), timeout=1) as conn:
                    conn.settimeout(1)
                    hello = b"\x01\x00\x10" + lab.TOKEN.bytes
                    conn.sendall(hello[:2])
                    conn.sendall(hello[2:] + protocol.audiosocket_frame(incoming) * 3)
                    received = bytearray()
                    while True:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        received.extend(chunk)
                frames = []
                while received:
                    size = int.from_bytes(received[1:3], "big")
                    frames.append(protocol.parse_audiosocket_frame(bytes(received[:size + 3])))
                    del received[:size + 3]
                self.assertEqual([frame.payload for frame in frames if frame.message_type == 0x12], [outgoing] * 3)
                self.assertEqual(frames[-1].message_type, 0)
                self.assertEqual(bytes(peer.received), incoming * 3)
                self.assertTrue(peer.ended)
                self.assertIsNone(peer.error)
            finally:
                peer.close()

    def test_channel_identity_snapshot_uses_uniqueid_not_reused_name(self):
        listing = "PJSIP/lab-00000001!l16-lab!9002!2!Up!Dial!synthetic\n"
        detail = " -- General --\n           Name: PJSIP/lab-00000001\n       UniqueID: 1234567890.12\n"
        with patch.object(lab.subprocess, "run", side_effect=[
                SimpleNamespace(returncode=0, stdout=listing), SimpleNamespace(returncode=0, stdout=detail)]):
            self.assertEqual(lab._channel_snapshot(), {"PJSIP/lab-00000001": "1234567890.12"})

    def test_channel_snapshot_accepts_audiosocket_address_with_port(self):
        name = "AudioSocket/127.0.0.1:9019-11111111-2222-4333-8444-555555555555"
        listing = SimpleNamespace(returncode=0, stdout=name + "!l16-lab!9001!3!Up\n")
        detail = SimpleNamespace(returncode=0, stdout="       UniqueID: 1234567890.13\n")
        with patch.object(lab.subprocess, "run", side_effect=[listing, detail]) as run:
            self.assertEqual(lab._channel_snapshot(), {name: "1234567890.13"})
        self.assertEqual(run.call_args_list[1].args[0],
                         ["asterisk", "-rx", "core show channel " + name])

    def test_channel_inspection_error_or_missing_uniqueid_cannot_mean_target_gone(self):
        listing = "PJSIP/lab-00000001!l16-lab!9002!2!Up!Dial!synthetic\n"
        for detail in (SimpleNamespace(returncode=1, stdout="UniqueID: 1234567890.12\n"),
                       SimpleNamespace(returncode=0, stdout="Channel is not known\n"),
                       SimpleNamespace(returncode=0, stdout="UniqueID: \n")):
            with self.subTest(detail=detail), patch.object(lab.subprocess, "run", side_effect=[
                    SimpleNamespace(returncode=0, stdout=listing), detail]):
                with self.assertRaisesRegex(lab.LabError, "channel identity"):
                    lab._channel_snapshot()

    def test_hold_open_audiosocket_waits_for_pbx_termination_without_sending_audio(self):
        with patch.object(lab, "AUDIO_PORT", 0):
            peer = lab.AudioSocketPeer(b"", hold_open=True)
            try:
                with socket.create_connection(peer.listener.getsockname(), timeout=1) as conn:
                    conn.settimeout(.05)
                    conn.sendall(b"\x01\x00\x10" + lab.TOKEN.bytes)
                    with self.assertRaises(socket.timeout):
                        conn.recv(1)
                    conn.sendall(b"\x00\x00\x00")
                lab._wait(lambda: peer.ended, 1, "synthetic PBX termination")
                self.assertIsNone(peer.error)
            finally:
                peer.close()

    def test_synthetic_pcm_directions_are_distinct_and_byte_asymmetric(self):
        incoming, outgoing = (lab.pcm_pattern(direction, "L16") for direction in ("incoming", "outgoing"))
        self.assertEqual(len(incoming), 640 * lab.FRAME_COUNT)
        self.assertNotEqual(incoming, outgoing)
        swapped = bytearray(incoming)
        swapped[0::2], swapped[1::2] = swapped[1::2], swapped[0::2]
        self.assertNotEqual(incoming, swapped)

    def test_inside_execution_rejects_host_context_before_any_commands(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(lab.subprocess, "run") as run:
            with self.assertRaisesRegex(lab.LabError, "owned container marker"):
                lab.run_inside()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
