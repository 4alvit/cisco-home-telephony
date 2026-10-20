"""Offline ownership and termination contracts for the disposable radio test."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import signal
import subprocess
import unittest
from unittest.mock import patch


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


if __name__ == "__main__":
    unittest.main()
