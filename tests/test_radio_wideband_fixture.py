"""Offline ownership and termination contracts for the disposable radio test."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "radio_wideband_fixture", ROOT / "scripts/test-radio-wideband.py")
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


class Docker:
    """Record exact commands and emulate a daemon that outlives create's CLI."""
    identity = "a" * 64

    def __init__(self, *, fail_create=None, wrong_owner=False, fail_remove=False,
                 absent=False, bad_output=False):
        self.fail_create, self.wrong_owner = fail_create, wrong_owner
        self.fail_remove, self.absent, self.bad_output = fail_remove, absent, bad_output
        self.present = False
        self.commands = []
        self.name = self.nonce = None

    def run(self, command, **options):
        self.commands.append((command, options))
        args = command[1:]
        action = args[0]
        if action == "create":
            self.name = args[args.index("--name") + 1]
            key, self.nonce = args[args.index("--label") + 1].split("=", 1)
            assert key == fixture.OWNER_LABEL
            self.present = not self.absent
            if self.fail_create == "sigterm":
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            if self.fail_create:
                raise self.fail_create
            output = "invalid-id" if self.bad_output else self.identity
            return subprocess.CompletedProcess(command, 0, output + "\n", "")
        if action == "inspect":
            assert args[-1] == self.name
            if not self.present:
                return subprocess.CompletedProcess(command, 1, "", "not found")
            nonce = "other-owner" if self.wrong_owner else self.nonce
            output = [self.identity, "/" + self.name,
                      {fixture.OWNER_LABEL: nonce}, "none"]
            return subprocess.CompletedProcess(command, 0, json.dumps(output), "")
        if action == "rm":
            assert args == ["rm", "-f", self.identity]
            if self.fail_remove:
                raise subprocess.CalledProcessError(1, command)
            self.present = False
            return subprocess.CompletedProcess(command, 0, self.identity + "\n", "")
        if action == "container":
            assert args == ["container", "ls", "--all", "--no-trunc", "--filter",
                            "name=^/" + self.name + "$", "--format", "{{.ID}}"]
            return subprocess.CompletedProcess(command, 0,
                                               self.identity + "\n" if self.present else "", "")
        raise AssertionError("Unexpected Docker operation: " + action)

    def removals(self):
        return [command for command, _ in self.commands if command[1] == "rm"]


class RadioWidebandOwnership(unittest.TestCase):
    def run_fixture(self, docker, body=None):
        previous = signal.getsignal(signal.SIGTERM)
        try:
            with patch.object(fixture.subprocess, "run", side_effect=docker.run), \
                    patch.object(fixture.secrets, "token_hex", return_value="b" * 32):
                with fixture.isolated_container(["docker"]) as owner:
                    identity = owner.create("synthetic:test")
                    if body:
                        body()
                    self.assertEqual(identity, docker.identity)
        finally:
            self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    def test_success_removes_verified_id_and_confirms_exact_name_absent(self):
        docker = Docker()
        self.run_fixture(docker)
        self.assertEqual(docker.removals(), [["docker", "rm", "-f", docker.identity]])
        self.assertFalse(docker.present)
        self.assertEqual(docker.commands[-1][0][1:3], ["container", "ls"])
        self.assertTrue(all(options["timeout"] <= 15 for _, options in docker.commands))

    def test_create_timeout_recovers_by_nonce_name_and_preserves_original_error(self):
        error = subprocess.TimeoutExpired(["docker", "create"], 15)
        docker = Docker(fail_create=error)
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            self.run_fixture(docker)
        self.assertIs(caught.exception, error)
        self.assertEqual(docker.removals(), [["docker", "rm", "-f", docker.identity]])
        self.assertFalse(docker.present)

    def test_sigterm_during_create_runs_exact_owner_cleanup(self):
        docker = Docker(fail_create="sigterm")
        with self.assertRaisesRegex(RuntimeError, "interrupted by signal"):
            self.run_fixture(docker)
        self.assertEqual(docker.removals(), [["docker", "rm", "-f", docker.identity]])
        self.assertFalse(docker.present)

    def test_create_timeout_waits_for_delayed_owned_container(self):
        error = subprocess.TimeoutExpired(["docker", "create"], 15)
        docker = Docker(fail_create=error, absent=True)

        def finish_daemon_create(_seconds):
            docker.present = True

        with patch.object(fixture.time, "sleep", side_effect=finish_daemon_create) as sleep, \
                self.assertRaises(subprocess.TimeoutExpired) as caught:
            self.run_fixture(docker)
        self.assertIs(caught.exception, error)
        sleep.assert_called_once_with(.25)
        self.assertEqual(docker.removals(), [["docker", "rm", "-f", docker.identity]])
        self.assertFalse(docker.present)

    def test_ownership_mismatch_never_removes_existing_resource(self):
        docker = Docker(wrong_owner=True)
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaisesRegex(RuntimeError, "ownership mismatch") as caught:
            self.run_fixture(docker)
        self.assertEqual(docker.removals(), [])
        self.assertTrue(docker.present)
        self.assertTrue(any("cleanup failed" in note
                            for note in getattr(caught.exception, "__notes__", [])))

    def test_invalid_create_id_is_rejected_but_owned_name_is_cleaned(self):
        docker = Docker(bad_output=True)
        with self.assertRaisesRegex(RuntimeError, "invalid created fixture ID"):
            self.run_fixture(docker)
        self.assertEqual(docker.removals(), [["docker", "rm", "-f", docker.identity]])

    def test_uncertain_create_absence_is_not_reported_as_confirmed_cleanup(self):
        error = subprocess.TimeoutExpired(["docker", "create"], 15)
        docker = Docker(fail_create=error, absent=True)
        with contextlib.redirect_stderr(io.StringIO()), \
                patch.object(fixture, "CREATE_RECOVERY_SECONDS", 0), \
                self.assertRaises(subprocess.TimeoutExpired) as caught:
            self.run_fixture(docker)
        self.assertIs(caught.exception, error)
        self.assertEqual(docker.removals(), [])
        self.assertTrue(any("cleanup unconfirmed" in note
                            for note in getattr(error, "__notes__", [])))

    def test_cleanup_failure_cannot_return_success(self):
        docker = Docker(fail_remove=True)
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_fixture(docker)
        self.assertTrue(docker.present)

    def test_wall_deadline_runs_cleanup_and_retains_original_failure(self):
        docker = Docker(fail_remove=True)

        def timeout():
            signal.getsignal(signal.SIGALRM)(signal.SIGALRM, None)

        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaisesRegex(TimeoutError, "bounded deadline") as caught:
            self.run_fixture(docker, timeout)
        self.assertEqual(docker.removals(), [["docker", "rm", "-f", docker.identity]])
        self.assertTrue(any("cleanup failed" in note
                            for note in getattr(caught.exception, "__notes__", [])))


class IncomingRadioNegotiation(unittest.TestCase):
    def test_media_fixture_preserves_generated_marker_and_clear(self):
        provision = fixture.module("media_fixture_provision", ROOT / "scripts/provision.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provision.generate(
                {"node_ipv4": "127.0.0.1", "lan_cidr": "127.0.0.0/8", "ntp_ipv4": "127.0.0.1",
                 "gateway": {"enabled": True, "ami_client_ipv4": "127.0.0.1"},
                 "radio": {"enabled": True, "stations": ["echo"], "max_seconds": 30}},
                {"phones": [{"model": "CP-7945G", "verified": True, "mac": "020000000001",
                             "label": "Fixture", "extension": "201", "transport": "udp",
                             "firmware_load": "SIP45.9-4-2SR4-3S"}]}, root)
            original = (root / "asterisk/extensions.conf").read_text()
            secret = json.loads((root / "gateway-ami-secret.json").read_text())
        configured = fixture.media_fixture_dialplan(original)
        self.assertIn("Set(CISCO_RADIO=1:702:${UNIQUEID})", configured)
        self.assertIn("same => n(cisco-radio-v1),Set(CISCO_RADIO=1:702:${UNIQUEID})", configured)
        self.assertIn("AGI(/etc/asterisk/radio_fixture_agi.py,echo)\n same => n,Set(CISCO_RADIO=)", configured)
        self.assertIn("exten => 799,1,Goto(home,702,1)", configured)
        self.assertEqual(secret['username'], 'cisco_gateway')
        for invalid in ('', original + original):
            with self.assertRaisesRegex(RuntimeError, 'generated radio route missing'):
                fixture.media_fixture_dialplan(invalid)

    @staticmethod
    def answer(payloads):
        mappings = {0: "PCMU/8000", 8: "PCMA/8000", 9: "G722/8000",
                    101: "telephone-event/8000", 102: "L16/16000"}
        return ("v=0\r\nc=IN IP4 127.0.0.1\r\nm=audio 10000 RTP/AVP "
                + " ".join(str(number) for number in payloads) + "\r\n"
                + "".join(f"a=rtpmap:{number} {mappings[number]}\r\n" for number in payloads))

    @staticmethod
    def peer(codec="G722", payload=9):
        peer = fixture.Phone.__new__(fixture.Phone)
        peer.codec, peer.payload = codec, payload
        peer.incoming, peer.mixed_offer = True, codec == "G722"
        peer.call_id, peer.local_from = "fixture-call", "<sip:201@127.0.0.2>;tag=local"
        peer.remote_to, peer.answer_payloads, peer.bye = None, None, False
        peer.send_request, peer.reply = Mock(), Mock()
        return peer

    def test_actual_generated_policy_preserved_in_loopback_incoming_endpoint(self):
        provision = fixture.module("incoming_fixture_provision", ROOT / "scripts/provision.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provision.generate(
                {"node_ipv4": "127.0.0.1", "lan_cidr": "127.0.0.0/8", "ntp_ipv4": "127.0.0.1"},
                {"phones": [{"model": "CP-7945G", "verified": True, "mac": "020000000001",
                             "label": "Fixture", "extension": "201", "transport": "udp",
                             "firmware_load": "SIP45.9-4-2SR4-3S"}]}, root)
            generated = (root / "asterisk/pjsip.conf").read_text()
        configured = fixture.incoming_fixture_pjsip(generated)
        original = generated.split("[201]\ntype=endpoint\n")[1].split("\n[", 1)[0]
        endpoint = configured.split("[201]\ntype=endpoint\n")[1].split("\n[", 1)[0]
        for key in ("allow", "disallow", "incoming_call_offer_pref", "outgoing_call_offer_pref"):
            self.assertEqual([line for line in original.splitlines() if line.startswith(key + "=")],
                             [line for line in endpoint.splitlines() if line.startswith(key + "=")])
        self.assertIn("incoming_call_offer_pref=local_first\n", endpoint)
        self.assertNotIn("\nauth=", "\n" + endpoint)
        self.assertIn("context=radio-smoke\n", endpoint)
        self.assertIn("identify_by=ip\n", endpoint)
        self.assertIn("[radio-fixture-loopback]\ntype=identify\nendpoint=201\nmatch=127.0.0.2/32\n",
                      configured)
        # Even the old policy is copied unchanged; the live SDP/RTP oracle must
        # expose its bug instead of this fixture silently correcting the input.
        old = fixture.incoming_fixture_pjsip(
            generated.replace("incoming_call_offer_pref=local_first\n",
                              "incoming_call_offer_pref=local\n"))
        self.assertIn("incoming_call_offer_pref=local\n", old)

    def test_recorded_old_local_broad_answer_is_rejected(self):
        # Sanitized actual failure: preference sorting alone left PCMU/PCMA
        # in the answer, and the live call used PCMU instead of G.722.
        with self.assertRaisesRegex(RuntimeError, "exactly one voice codec"):
            fixture.selected_answer(self.answer([9, 0, 8, 101]), 9, "G722")

    def test_g722_only_answer_and_pcmu_only_fallback_are_accepted(self):
        self.assertEqual([9, 101], fixture.selected_answer(self.answer([9, 101]), 9, "G722"))
        self.assertEqual([0], fixture.selected_answer(self.answer([0]), 0, "PCMU"))
        for payloads in ([0, 101], [102, 9, 101], [9, 9, 101]):
            with self.subTest(payloads=payloads), self.assertRaises(RuntimeError):
                fixture.selected_answer(self.answer(payloads), 9, "G722")

    def test_incoming_offer_matches_pcmu_first_cisco_order(self):
        peer = self.peer()
        peer.dial()
        args = peer.send_request.call_args.args
        self.assertEqual(("INVITE", "sip:799@127.0.0.1", "<sip:799@127.0.0.1>"), args[:3])
        self.assertIn("m=audio 6000 RTP/AVP 0 8 18 102 9 116 101\r\n", args[3])
        self.assertIn("a=rtpmap:102 L16/16000\r\n", args[3])
        self.assertIn("a=fmtp:116 mode=20\r\n", args[3])
        fallback = self.peer("PCMU", 0)
        fallback.dial()
        body = fallback.send_request.call_args.args[3]
        self.assertIn("m=audio 6000 RTP/AVP 0\r\n", body)
        self.assertNotIn("G722", body)

    def test_incoming_answer_is_acked_and_only_its_bye_completes(self):
        for contact in ("sip:127.0.0.1:5060", "sip:799@127.0.0.1:5060;transport=UDP"):
            peer = self.peer()
            message = (f"SIP/2.0 200 OK\r\nFrom: {peer.local_from}\r\n"
                       "To: <sip:799@127.0.0.1>;tag=remote\r\nCall-ID: fixture-call\r\n"
                       f"CSeq: 1 INVITE\r\nContact: <{contact}>\r\n\r\n"
                       + self.answer([9, 101]))
            peer.incoming_message(message, ("127.0.0.1", 5060))
            peer.send_request.assert_called_once_with(
                "ACK", contact, "<sip:799@127.0.0.1>;tag=remote")
            self.assertEqual([9, 101], peer.answer_payloads)
            bye = ("BYE sip:201@127.0.0.2 SIP/2.0\r\n"
                   f"To: {peer.local_from}\r\nFrom: {peer.remote_to}\r\n"
                   "Call-ID: fixture-call\r\nCSeq: 2 BYE\r\n\r\n")
            with self.assertRaisesRegex(RuntimeError, "dialog changed"):
                peer.incoming_message(bye.replace("fixture-call", "unrelated"), ("127.0.0.1", 5060))
            self.assertFalse(peer.bye)
            peer.incoming_message(bye, ("127.0.0.1", 5060))
            self.assertTrue(peer.bye)

    def test_bad_answer_is_acked_before_the_codec_oracle_fails(self):
        peer = self.peer()
        message = (f"SIP/2.0 200 OK\r\nFrom: {peer.local_from}\r\n"
                   "To: <sip:799@127.0.0.1>;tag=remote\r\nCall-ID: fixture-call\r\n"
                   "CSeq: 1 INVITE\r\nContact: <sip:127.0.0.1:5060>\r\n\r\n"
                   + self.answer([9, 0, 8, 101]))
        with self.assertRaisesRegex(RuntimeError, "exactly one voice codec"):
            peer.incoming_message(message, ("127.0.0.1", 5060))
        self.assertEqual("ACK", peer.send_request.call_args.args[0])
        self.assertIsNone(peer.answer_payloads)


if __name__ == "__main__":
    unittest.main()
