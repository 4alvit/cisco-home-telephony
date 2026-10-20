"""Synthetic supervisor checks; no voice runner, phone or microphone is started."""
from __future__ import annotations

import contextlib
import copy
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "phone_voice_control_test", ROOT / "containers/asterisk/phone_voice_control.py")
control = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = control
SPEC.loader.exec_module(control)

PRIVATE_MARKER = "synthetic-private-token-and-transcript"
ALLOWED = {"source": "192.168.50.11", "authorized": True}


def completed_report(reason="turn_complete", *, confirmed=True):
    return {
        "error": None, "cleanup_errors": [],
        "result": {"reason": reason, "stop_confirmed": confirmed},
        "watchdog_armed": True, "watchdog_finished": True,
        "watchdog_stop_errors": 0, "watchdog_stop_confirmed": confirmed,
        "transcript": PRIVATE_MARKER, "token": PRIVATE_MARKER,
    }


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeRunner:
    """All process state and output are explicitly controlled by each test."""

    def __init__(self):
        self.returncode = None
        self.result = None
        self.overflow = False
        self.terminations = 0
        self.kills = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminations += 1

    def kill(self):
        self.kills += 1

    def read_result(self):
        return self.result

    def finish(self, report=None, code=0):
        self.result = json.dumps(report or completed_report()).encode()
        self.returncode = code


class ControlFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="voice-control-test-")
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name).resolve()
        self.local = self.workspace / ".local"
        self.local.mkdir(mode=0o700)
        self.clock = FakeClock()
        self.runners = []
        self.launches = []
        self.controls = []
        self.gateway = {
            "persistent_enabled": True, "bind_ipv4": "192.168.50.10", "api_port": 18084,
            "api_token": "a" * 48, "api_clients": ["192.168.50.11"],
            "ami": {"host": "192.168.50.10", "port": 5038,
                    "username": "synthetic-gateway", "secret": PRIVATE_MARKER},
            "dial_allowlist": [], "ring_allowlist": [],
            "phones": [{"id": "201", "lines": ["201", "202"], "ipv4": "192.168.50.20",
                        "device": "SEP020000000001", "port": 18080,
                        "auth_path": "/auth/" + "c" * 64,
                        "username": "u" * 24, "password": "p" * 24}],
        }
        self.transport = {
            "gateway_config": str(self.local / "gateway.json"), "line": "201",
            "bind_ipv4": "192.168.50.10", "bind_port": 24576, "phone_rtp_port": 24578,
            "duration_seconds": 10, "receive_idle_seconds": 3, "volume": 20,
        }
        self.voice = {
            "service": {"enabled": True, "rearm_after_turn": False}, "phones": {"201": True},
            "ha": {"base_url": "http://192.168.50.30:8123", "token_file": ".local/token.txt",
                   "pipeline_id": "synthetic-pipeline"},
            "wake": {"mode": "manual", "bundle_file": ".local/unused-bundle.json"},
        }
        self.configuration = {
            "version": 1, "enabled": True, "state_directory": ".local/control",
            "python_executable": sys.executable,
            "ffmpeg_executable": sys.executable,
            "profiles": [{"transport_config": ".local/transport.json",
                          "voice_config": ".local/voice.json", "display_port": None}],
        }
        self.write_inputs()
        self.addCleanup(self.finish_controls)

    def write_private(self, name, value):
        path = self.local / name
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        path.chmod(0o600)
        return path

    def write_inputs(self):
        for name, data in (("gateway", self.gateway), ("transport", self.transport),
                           ("voice", self.voice), ("control", self.configuration)):
            self.write_private(name + ".json", data)
        self.write_private("token.txt", PRIVATE_MARKER + "\n")

    def load(self):
        return control.load_control_config(self.local / "control.json", self.workspace)

    def launch(self, argv, *, cwd, env, lease_fd):
        runner = FakeRunner()
        self.runners.append(runner)
        self.launches.append((list(argv), cwd, dict(env), lease_fd))
        return runner

    def make_control(self, *, authorize=None, runner_factory=None, config=None):
        if authorize is None:
            authorize = lambda context, action, phone_id: (
                context == ALLOWED and action in {"start", "status", "stop"} and phone_id == "201")
        instance = control.VoiceControl(
            self.load() if config is None else config, authorize=authorize,
            runner_factory=runner_factory or self.launch, clock=self.clock, monitor=False)
        self.controls.append(instance)
        return instance

    def finish_controls(self):
        for runner in self.runners:
            runner.finish()
        for instance in self.controls:
            instance.tick()
            instance.close()
            instance.tick()
            # Release only synthetic retained recovery leases at test teardown.
            for session in instance._sessions.values():
                if session.lease_fd >= 0:
                    os.close(session.lease_fd)
                    session.lease_fd = -1

    def start(self, instance, target="201", request_id="a" * 32, **kwargs):
        return instance.start(target, request_id, context=ALLOWED, attended=True,
                              exclusive_media=True, enable_microphone=True, **kwargs)

    def status(self, instance, target="201", **kwargs):
        return instance.status(target, context=ALLOWED, **kwargs)

    def stop(self, instance, session_id, target="201"):
        return instance.stop(target, session_id, context=ALLOWED)

    def assert_safe(self, value):
        encoded = json.dumps(value)
        self.assertNotIn(PRIVATE_MARKER, encoded)
        self.assertNotIn(str(self.workspace), encoded)
        self.assertNotIn("192.168.50.30", encoded)
        self.assertNotIn("synthetic-pipeline", encoded)


class ControlConfigurationTests(ControlFixture):
    def add_second_profile(self, *, bind_port=24580, display_port=18091):
        phone = copy.deepcopy(self.gateway["phones"][0])
        phone.update(id="203", lines=["203", "204"], ipv4="192.168.50.21",
                     device="SEP020000000002", port=18081)
        self.gateway["phones"].append(phone)
        self.configuration["profiles"][0]["display_port"] = 18090
        self.configuration["profiles"].append({
            "transport_config": ".local/transport-second.json",
            "voice_config": ".local/voice-second.json", "display_port": display_port})
        self.write_inputs()
        self.write_private("transport-second.json", dict(self.transport, line="203", bind_port=bind_port))
        voice = copy.deepcopy(self.voice)
        voice["phones"] = {"203": True}
        self.write_private("voice-second.json", voice)

    def test_load_and_constructor_do_not_create_state_or_start_process(self):
        config = self.load()
        self.assertEqual(config.workspace, self.workspace)
        instance = self.make_control(config=config)
        self.assertFalse((self.local / "control").exists())
        self.assertEqual(self.launches, [])
        self.assertFalse(self.status(instance)["active"])

    def test_duration_over_thirty_seconds_is_rejected(self):
        self.transport["duration_seconds"] = 31
        self.write_inputs()
        with self.assertRaises(control.ControlError):
            self.load()

    def test_ffmpeg_requires_an_explicit_absolute_executable(self):
        nonexecutable = self.write_private("not-executable", "synthetic")
        for value in ("ffmpeg", "/nonexistent/synthetic/ffmpeg", str(self.local), str(nonexecutable)):
            with self.subTest(value=value):
                self.configuration["ffmpeg_executable"] = value
                self.write_inputs()
                with self.assertRaises(control.ControlError) as caught:
                    self.load()
                self.assertEqual(caught.exception.reason, "invalid_voice_ffmpeg")

    def test_ffmpeg_outside_child_path_is_passed_as_one_absolute_argument(self):
        executable = self.local / "synthetic ffmpeg"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        self.configuration["ffmpeg_executable"] = str(executable)
        self.write_inputs()
        instance = self.make_control()
        self.start(instance)
        argv, _, env, _ = self.launches[0]
        self.assertEqual(argv[argv.index("--ffmpeg") + 1], str(executable))
        self.assertNotIn(str(self.local), env["PATH"].split(os.pathsep))

    def test_wake_and_automatic_rearm_are_rejected(self):
        for section, field, value in (("wake", "mode", "onnx"),
                                      ("service", "rearm_after_turn", True)):
            with self.subTest(field=field):
                before = copy.deepcopy(self.voice)
                self.voice[section][field] = value
                self.write_inputs()
                with self.assertRaises(control.ControlError):
                    self.load()
                self.voice = before

    def test_private_config_permissions_and_symlinks_are_rejected(self):
        path = self.local / "control.json"
        path.chmod(0o644)
        with self.assertRaises(control.ControlError):
            self.load()
        path.chmod(0o600)
        saved = self.local / "saved-control.json"
        path.rename(saved)
        path.symlink_to(saved)
        with self.assertRaises(control.ControlError):
            self.load()

    def test_duplicate_profiles_cannot_assign_two_owners_to_one_phone(self):
        self.configuration["profiles"].append(copy.deepcopy(self.configuration["profiles"][0]))
        self.write_inputs()
        with self.assertRaises(control.ControlError):
            self.load()

    def test_loaded_identity_is_immutable_and_changed_identity_prevents_launch(self):
        config = self.load()
        with self.assertRaises(AttributeError):
            config.profiles[0].phone_id = "203"
        instance = self.make_control(config=config)
        self.gateway["phones"][0]["ipv4"] = "192.168.50.21"
        self.write_inputs()
        with self.assertRaises(control.ControlError):
            self.start(instance)
        self.assertEqual(self.launches, [])
        self.assertEqual(self.status(instance)["state"], "failed")

    def test_different_phones_cannot_share_local_rtp_endpoint(self):
        self.add_second_profile(bind_port=self.transport["bind_port"])
        with self.assertRaises(control.ControlError):
            self.load()

    def test_different_phones_cannot_share_local_display_endpoint(self):
        self.add_second_profile(bind_port=24580, display_port=18090)
        with self.assertRaises(control.ControlError):
            self.load()

    def test_different_phone_ips_may_share_destination_rtp_port(self):
        self.add_second_profile(bind_port=24580, display_port=18091)
        config = self.load()
        self.assertEqual(len(config.profiles), 2)
        self.assertEqual({profile.phone_id for profile in config.profiles}, {"201", "203"})
        self.assertEqual(self.launches, [])

    def test_changed_local_bind_endpoint_is_rejected_for_start_and_dry_run(self):
        original = copy.deepcopy(self.transport)
        cases = [(field, value, dry_run)
                 for field, value in (("bind_ipv4", "192.168.50.12"), ("bind_port", 24580))
                 for dry_run in (False, True)]
        for index, (field, value, dry_run) in enumerate(cases):
            with self.subTest(field=field, dry_run=dry_run):
                self.transport = copy.deepcopy(original)
                self.configuration["state_directory"] = ".local/bind-check-" + str(index)
                self.write_inputs()
                instance = self.make_control()
                self.transport[field] = value
                self.write_private("transport.json", self.transport)
                with self.assertRaises(control.ControlError):
                    self.start(instance, dry_run=dry_run)
                self.assertEqual(self.launches, [])


class TokenSourceAuthorizerTests(unittest.TestCase):
    def test_exact_source_and_one_exact_bearer_header_are_required(self):
        token = "synthetic-token-" + "a" * 32
        auth = control.TokenSourceAuthorizer(token, ["192.168.50.11"])
        valid = {"source": "192.168.50.11", "authorization": ["Bearer " + token]}
        self.assertIs(auth(valid, "start", "201"), True)
        bad = (None, {}, dict(valid, source="192.168.50.12"),
               dict(valid, authorization=[]),
               dict(valid, authorization=valid["authorization"] * 2),
               dict(valid, authorization=["bearer " + token]),
               dict(valid, authorization=["Bearer incorrect"]),
               dict(valid, authorization="Bearer " + token),
               dict(valid, source="192.168.50.12", forwarded_for="192.168.50.11"))
        for context in bad:
            with self.subTest(context=context):
                self.assertIs(auth(context, "start", "201"), False)


class ControlAuthorizationTests(ControlFixture):
    def test_denied_source_and_credentials_prevent_process_and_state_writes(self):
        instance = self.make_control()
        contexts = (None, {"source": "192.168.50.12", "authorized": True},
                    {"source": "192.168.50.11", "authorized": False})
        for context in contexts:
            for action in ("start", "status", "stop"):
                with self.subTest(context=context, action=action), self.assertRaises(control.ControlError):
                    if action == "start":
                        instance.start("201", "a" * 32, context=context, attended=True,
                                       exclusive_media=True, enable_microphone=True)
                    elif action == "status":
                        instance.status("201", context=context)
                    else:
                        instance.stop("201", "b" * 32, context=context)
        self.assertEqual(self.launches, [])
        self.assertFalse((self.local / "control").exists())

    def test_denial_precedes_private_input_read_and_preparation(self):
        instance = self.make_control()
        with mock.patch.object(control, "_inputs", side_effect=AssertionError("must not read")) as read, \
             mock.patch.object(instance, "_prepare", side_effect=AssertionError("must not prepare")) as prepare:
            with self.assertRaises(control.ControlError) as caught:
                instance.start("202", "a" * 32, context=None, attended=True,
                               exclusive_media=True, enable_microphone=True)
        self.assertEqual(caught.exception.status, 403)
        read.assert_not_called()
        prepare.assert_not_called()
        self.assertEqual(self.launches, [])

    def test_authorization_requires_literal_true_and_sanitizes_exceptions(self):
        for value in (None, False, 0, 1, "true"):
            with self.subTest(value=value):
                instance = self.make_control(authorize=lambda *_args: value)
                with self.assertRaises(control.ControlError):
                    self.start(instance)
        def fail(*_args):
            raise RuntimeError(PRIVATE_MARKER)
        instance = self.make_control(authorize=fail)
        with self.assertRaises(control.ControlError) as caught:
            self.start(instance)
        self.assertNotIn(PRIVATE_MARKER, str(caught.exception))
        self.assertEqual(self.launches, [])

    def test_each_live_flag_requires_literal_true(self):
        instance = self.make_control()
        for missing in ("attended", "exclusive_media", "enable_microphone"):
            for value in (False, None, 1, "true"):
                flags = dict(attended=True, exclusive_media=True, enable_microphone=True)
                flags[missing] = value
                with self.subTest(flag=missing, value=value), self.assertRaises(control.ControlError):
                    instance.start("201", "a" * 32, context=ALLOWED, **flags)
        self.assertEqual(self.launches, [])


class ControlLifecycleTests(ControlFixture):
    def test_either_line_alias_uses_same_physical_session(self):
        instance = self.make_control()
        started = self.start(instance, "202")
        first = self.status(instance, "201")
        second = self.status(instance, "202")
        self.assertEqual(started["phone"], "201")
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(first["session_id"], started["session_id"])
        self.assertEqual(len(self.runners), 1)
        self.assertTrue(first["active"])
        self.assert_safe(first)

    def test_same_request_is_replayed_without_second_launch_or_lease_extension(self):
        instance = self.make_control()
        started = self.start(instance)
        self.clock.advance(2)
        replayed = self.start(instance, "202")
        self.assertEqual(replayed["session_id"], started["session_id"])
        self.assertLess(replayed["remaining_seconds"], started["remaining_seconds"])
        self.assertEqual(len(self.runners), 1)

    def test_dry_run_reused_key_validates_without_replaying_or_touching_session(self):
        instance = self.make_control()
        started = self.start(instance)
        journal = next((self.local / "control").glob("*.json"))
        for completed in (False, True):
            if completed:
                self.runners[0].finish()
                instance.tick()
            before = journal.read_bytes()
            before_status = self.status(instance)
            validation = self.start(instance, "202", dry_run=True)
            self.assertTrue(validation["valid"])
            self.assertEqual(validation["line"], "202")
            self.assertEqual(validation["network_actions"], 0)
            self.assertFalse(validation["microphone_started"])
            self.assertNotIn("session_id", validation)
            self.assertEqual(journal.read_bytes(), before)
            self.assertEqual(self.status(instance), before_status)
            self.assertEqual(len(self.runners), 1)
        replacement = self.make_control()
        validation = self.start(replacement, "202", dry_run=True)
        self.assertTrue(validation["valid"])
        self.assertEqual(self.status(replacement)["session_id"], started["session_id"])
        self.assertEqual(len(self.runners), 1)

    def test_dry_run_reused_key_rechecks_changed_profile(self):
        instance = self.make_control()
        self.start(instance)
        self.transport["bind_port"] += 2
        self.write_inputs()
        with self.assertRaises(control.ControlError) as caught:
            self.start(instance, dry_run=True)
        self.assertEqual(caught.exception.reason, "voice_profile_changed")
        self.assertEqual(len(self.runners), 1)

    def test_other_request_cannot_replace_active_session(self):
        instance = self.make_control()
        started = self.start(instance)
        with self.assertRaises(control.ControlError):
            self.start(instance, "202", "b" * 32)
        self.assertEqual(self.status(instance)["session_id"], started["session_id"])
        self.assertEqual(len(self.runners), 1)

    def test_cross_instance_lock_prevents_duplicate_process(self):
        first, second = self.make_control(), self.make_control()
        self.start(first)
        with self.assertRaises(control.ControlError):
            self.start(second, "202", "b" * 32)
        self.assertEqual(len(self.runners), 1)

    def test_simultaneous_duplicate_starts_spawn_one_runner(self):
        instance = self.make_control()
        barrier = threading.Barrier(3)
        results, errors = [], []
        def run(line):
            try:
                barrier.wait(timeout=2)
                results.append(self.start(instance, line))
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=run, args=(line,)) for line in ("201", "202")]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=2)
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["session_id"], results[1]["session_id"])
        self.assertEqual(len(self.runners), 1)

    def test_stop_is_idempotent_and_waits_for_confirmed_child_result(self):
        instance = self.make_control()
        started = self.start(instance)
        for _ in range(2):
            pending = self.stop(instance, started["session_id"], "202")
            self.assertTrue(pending["stop_requested"])
            self.assertFalse(pending["stop_confirmed"])
        self.assertEqual(self.runners[0].terminations, 1)
        self.runners[0].finish(completed_report("shutdown"), code=1)
        instance.tick()
        stopped = self.status(instance)
        self.assertFalse(stopped["active"])
        self.assertTrue(stopped["stop_confirmed"])
        self.assertIsNone(stopped["error"])
        self.stop(instance, started["session_id"])
        self.assertEqual(self.runners[0].terminations, 1)
        self.assert_safe(stopped)

    def test_stale_stop_cannot_terminate_a_new_explicit_session(self):
        instance = self.make_control()
        first = self.start(instance)
        self.runners[0].finish()
        instance.tick()
        second = self.start(instance, request_id="b" * 32)
        self.assertNotEqual(first["session_id"], second["session_id"])
        old = self.stop(instance, first["session_id"])
        self.assertEqual(old["session_id"], first["session_id"])
        self.assertFalse(old["active"])
        self.assertEqual(self.runners[1].terminations, 0)
        self.assertEqual(self.status(instance)["session_id"], second["session_id"])

    def test_stop_during_preparation_cancels_before_runner_launch(self):
        instance = self.make_control()
        entered, release = threading.Event(), threading.Event()
        original = instance._prepare
        results, errors = [], []
        def prepare(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError("synthetic preparation gate timed out")
            return original(*args)
        def start():
            try:
                results.append(self.start(instance))
            except Exception as error:
                errors.append(error)
        with mock.patch.object(instance, "_prepare", side_effect=prepare):
            thread = threading.Thread(target=start)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                starting = self.status(instance, "202")
                self.assertEqual(starting["state"], "starting")
                pending = self.stop(instance, starting["session_id"], "202")
                self.assertTrue(pending["stop_requested"])
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["state"], "cancelled")
        self.assertFalse(results[0]["active"])
        self.assertEqual(self.launches, [])
        self.assertEqual(list((self.local / "control").glob("run-*")), [])
        self.start(instance, request_id="b" * 32)
        self.assertEqual(len(self.runners), 1)

    def test_ledger_is_reserved_before_spawn_and_snapshot_is_private(self):
        def launch(argv, **kwargs):
            self.assertGreaterEqual(kwargs["lease_fd"], 0)
            self.assertEqual(os.fstat(kwargs["lease_fd"]).st_mode & 0o777, 0o600)
            ledger_paths = list((self.local / "control").glob("*.json"))
            self.assertEqual(len(ledger_paths), 1)
            ledger = json.loads(ledger_paths[0].read_text())
            self.assertIs(ledger["reserved"], True)
            self.assertEqual(ledger["state"], "starting")
            self.assert_safe(ledger)
            self.assertEqual(argv[:2], [sys.executable, str(ROOT / "scripts/voice_session.py")])
            for flag in ("--run", "--attended", "--exclusive-media", "--enable-microphone"):
                self.assertIn(flag, argv)
            self.assertNotIn(PRIVATE_MARKER, repr(argv))
            self.assertNotIn(PRIVATE_MARKER, repr(kwargs["env"]))
            self.assertNotIn("SYNTHETIC_CONTROL_SECRET", kwargs["env"])
            snapshot = Path(argv[argv.index("--transport-config") + 1]).parent
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o700)
            for path in snapshot.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            return self.launch(argv, **kwargs)
        with mock.patch.dict(os.environ, {"SYNTHETIC_CONTROL_SECRET": PRIVATE_MARKER}):
            instance = self.make_control(runner_factory=launch)
            self.start(instance)
        self.runners[0].finish()
        instance.tick()
        self.assertEqual(list((self.local / "control").glob("run-*")), [])

    def test_dry_run_does_not_write_state_or_launch(self):
        instance = self.make_control()
        result = self.start(instance, dry_run=True)
        self.assertIs(result["valid"], True)
        self.assertEqual(result["network_actions"], 0)
        self.assertIs(result["microphone_started"], False)
        self.assertEqual(self.launches, [])
        self.assertFalse((self.local / "control").exists())

    def test_uncertain_launch_failure_is_sanitized_and_reserves_recovery(self):
        def fail(*_args, **_kwargs):
            raise OSError(PRIVATE_MARKER)
        instance = self.make_control(runner_factory=fail)
        with self.assertRaises(control.ControlError) as caught:
            self.start(instance)
        self.assertNotIn(PRIVATE_MARKER, str(caught.exception))
        failed = self.status(instance)
        self.assertEqual(failed["state"], "recovery_required")
        self.assertFalse(failed["active"])
        self.assert_safe(failed)
        replacement = self.make_control()
        with self.assertRaises(control.ControlError):
            self.start(replacement, request_id="b" * 32)
        self.assertEqual(self.runners, [])

    def test_close_is_idempotent_requests_stop_and_prevents_new_start(self):
        instance = self.make_control()
        self.start(instance)
        instance.close()
        instance.close()
        self.assertEqual(self.runners[0].terminations, 1)
        with self.assertRaises(control.ControlError):
            self.start(instance, request_id="b" * 32)
        self.assertFalse(self.status(instance)["stop_confirmed"])

    def test_close_signals_every_phone_despite_one_journal_failure(self):
        ControlConfigurationTests.add_second_profile(self)
        instance = self.make_control(authorize=lambda *_args: True)
        self.start(instance)
        self.start(instance, target="203", request_id="second-phone-start")
        original = instance._write

        def write(session):
            if session.profile.phone_id == "201":
                raise OSError(PRIVATE_MARKER)
            return original(session)

        with mock.patch.object(instance, "_write", side_effect=write):
            with self.assertRaises(control.ControlError) as caught:
                instance.close()
        self.assertEqual(caught.exception.reason, "voice_state_unavailable")
        self.assertEqual([runner.terminations for runner in self.runners], [1, 1])
        self.assertTrue(self.status(instance)["stop_requested"])
        self.assertTrue(self.status(instance, "204")["stop_requested"])
        self.assertNotIn(PRIVATE_MARKER, str(caught.exception))

    def test_successful_completion_requires_new_request_to_restart(self):
        instance = self.make_control()
        started = self.start(instance)
        self.runners[0].finish()
        instance.tick()
        for _ in range(3):
            instance.tick()
        terminal = self.status(instance)
        self.assertFalse(terminal["active"])
        self.assertTrue(terminal["stop_confirmed"])
        replayed = self.start(instance)
        self.assertEqual(replayed["session_id"], started["session_id"])
        self.assertEqual(len(self.runners), 1)
        self.assert_safe(terminal)

    def test_crash_without_cleanup_proof_blocks_automatic_and_explicit_restart(self):
        instance = self.make_control()
        self.start(instance)
        self.runners[0].returncode = -9
        instance.tick()
        self.clock.advance(control.REPORT_GRACE_SECONDS)
        instance.tick()
        status = self.status(instance)
        self.assertEqual(status["state"], "recovery_required")
        self.assertFalse(status["stop_confirmed"])
        with self.assertRaises(control.ControlError):
            self.start(instance, request_id="b" * 32)
        self.assertEqual(len(self.runners), 1)
        self.assert_safe(status)

    def test_missing_complete_output_waits_only_for_bounded_report_grace(self):
        instance = self.make_control()
        self.start(instance)
        self.runners[0].returncode = 0
        instance.tick()
        self.assertNotEqual(self.status(instance)["state"], "recovery_required")
        self.clock.advance(control.REPORT_GRACE_SECONDS - 0.01)
        instance.tick()
        self.assertNotEqual(self.status(instance)["state"], "recovery_required")
        self.clock.advance(0.02)
        instance.tick()
        status = self.status(instance)
        self.assertEqual(status["state"], "recovery_required")
        self.assertFalse(status["stop_confirmed"])
        replacement = self.make_control()
        self.assertEqual(self.status(replacement)["state"], "recovery_required")
        with self.assertRaises(control.ControlError):
            self.start(replacement, request_id="b" * 32)
        self.assertEqual(len(self.runners), 1)

    def test_complete_report_arriving_within_grace_can_confirm_cleanup(self):
        instance = self.make_control()
        self.start(instance)
        self.runners[0].returncode = 1
        instance.tick()
        self.clock.advance(control.REPORT_GRACE_SECONDS / 2)
        self.runners[0].result = json.dumps(completed_report("shutdown")).encode()
        instance.tick()
        status = self.status(instance)
        self.assertEqual(status["state"], "stopped")
        self.assertTrue(status["stop_confirmed"])

    def test_completed_request_replayed_after_manager_restart_never_relaunches(self):
        instance = self.make_control()
        started = self.start(instance)
        self.runners[0].finish()
        instance.tick()
        instance.close()
        replacement = self.make_control()
        replayed = self.start(replacement, "202")
        self.assertEqual(replayed["session_id"], started["session_id"])
        self.assertFalse(replayed["active"])
        self.assertEqual(len(self.runners), 1)

    def test_current_status_follows_newer_manager_but_explicit_query_preserves_history(self):
        first, second = self.make_control(), self.make_control()
        old = self.start(first)
        self.runners[0].finish()
        first.tick()
        self.assertTrue(self.status(first)["stop_confirmed"])

        newer = self.start(second, "202", request_id="b" * 32)
        self.assertNotEqual(newer["session_id"], old["session_id"])
        for line in ("201", "202"):
            with self.subTest(phase="newer_active", line=line):
                current = self.status(first, line)
                self.assertEqual(current["session_id"], newer["session_id"])
                self.assertEqual(current["state"], "recovery_required")
                self.assertFalse(current["stop_confirmed"])
                historical = self.status(first, line, session_id=old["session_id"])
                self.assertEqual(historical["session_id"], old["session_id"])
                self.assertEqual(historical["state"], "stopped")
                self.assertTrue(historical["stop_confirmed"])

        self.runners[1].finish()
        second.tick()
        for line in ("201", "202"):
            with self.subTest(phase="newer_complete", line=line):
                current = self.status(first, line)
                self.assertEqual(current["session_id"], newer["session_id"])
                self.assertEqual(current["state"], "stopped")
                self.assertTrue(current["stop_confirmed"])
                self.assertFalse(current["active"])
        historical = self.status(first, session_id=old["session_id"])
        self.assertEqual(historical["session_id"], old["session_id"])
        self.assertTrue(historical["stop_confirmed"])

    def test_deadline_requests_stop_once_without_extending_lease(self):
        instance = self.make_control()
        self.start(instance)
        self.clock.advance(10)
        instance.tick()
        status = self.status(instance)
        self.assertTrue(status["stop_requested"])
        self.assertFalse(status["stop_confirmed"])
        self.assertEqual(status["remaining_seconds"], 0)
        instance.tick()
        self.assertEqual(self.runners[0].terminations, 1)
        self.clock.advance(120)
        instance.tick()
        self.assertEqual(self.runners[0].kills, 1)
        self.assertFalse(self.status(instance)["stop_confirmed"])
        self.runners[0].finish()
        instance.tick()
        self.assertEqual(self.status(instance)["state"], "recovery_required")
        self.assertFalse(self.status(instance)["stop_confirmed"])

    def test_output_overflow_requests_one_stop_and_cannot_confirm_cleanup(self):
        instance = self.make_control()
        self.start(instance)
        self.runners[0].overflow = True
        instance.tick()
        instance.tick()
        self.assertEqual(self.runners[0].terminations, 1)
        self.assertTrue(self.status(instance)["stop_requested"])
        self.runners[0].finish()
        instance.tick()
        self.assertEqual(self.status(instance)["state"], "recovery_required")
        self.assertFalse(self.status(instance)["stop_confirmed"])

    def test_bad_or_incomplete_cleanup_report_cannot_confirm_stop(self):
        bad_reports = (
            b"not-json " + PRIVATE_MARKER.encode(),
            b"[]", b"{}", b'{"result":',
            b"x" * (control.MAX_REPORT + 1),
            json.dumps(completed_report(confirmed=False)).encode(),
            json.dumps(dict(completed_report(), watchdog_finished=False)).encode(),
            json.dumps(dict(completed_report(), watchdog_armed=1)).encode(),
            json.dumps(dict(completed_report(), watchdog_stop_errors=False)).encode(),
        )
        for index, report in enumerate(bad_reports):
            with self.subTest(report=index):
                # Each independent state directory models a fresh supervisor.
                self.configuration["state_directory"] = ".local/control-" + str(index)
                self.write_inputs()
                instance = self.make_control()
                self.start(instance)
                self.runners[-1].result = report
                self.runners[-1].returncode = 0
                instance.tick()
                status = self.status(instance)
                self.assertFalse(status["stop_confirmed"])
                self.assertEqual(status["state"], "recovery_required")
                self.assert_safe(status)

    def test_media_stop_evidence_does_not_release_other_failed_cleanup(self):
        instance = self.make_control()
        self.start(instance)
        report = dict(completed_report(), cleanup_errors=[PRIVATE_MARKER])
        self.runners[0].finish(report)
        instance.tick()
        status = self.status(instance)
        self.assertTrue(status["stop_confirmed"])
        self.assertEqual(status["state"], "recovery_required")
        with self.assertRaises(control.ControlError):
            self.start(instance, request_id="b" * 32)
        self.assert_safe(status)

    def test_runner_stdout_and_failure_details_do_not_reach_supervisor_output(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            instance = self.make_control()
            self.start(instance)
            self.runners[0].finish(completed_report())
            instance.tick()
            status = self.status(instance)
        self.assert_safe(status)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")

    def test_runner_reason_preserves_failure_even_when_cleanup_is_confirmed(self):
        instance = self.make_control()
        for index, reason in enumerate(("assistant_error", "receive_idle_timeout", "transport_stopped",
                                        "lease_expired", "start_failed", PRIVATE_MARKER)):
            with self.subTest(reason=reason):
                self.start(instance, request_id="classification-" + str(index))
                self.runners[-1].finish(completed_report(reason))
                instance.tick()
                result = self.status(instance)
                self.assertEqual(result["state"], "failed")
                self.assertEqual(result["error"], "voice_session_failed")
                self.assertTrue(result["stop_confirmed"])
                self.assert_safe(result)

    def test_explicit_stop_and_completed_turn_remain_clean_terminal_results(self):
        instance = self.make_control()
        for index, reason in enumerate(("turn_complete", "explicit_stop", "display_stop", "shutdown")):
            with self.subTest(reason=reason):
                self.start(instance, request_id="completion-" + str(index))
                self.runners[-1].finish(completed_report(reason))
                instance.tick()
                result = self.status(instance)
                self.assertEqual(result["state"], "stopped")
                self.assertIsNone(result["error"])
                self.assertTrue(result["stop_confirmed"])


class SubprocessHandleTests(unittest.TestCase):
    def make_handle(self):
        process = mock.Mock()
        process.stdout.fileno.return_value = 123
        argv = ["/synthetic/python", "/synthetic/voice_session.py"]
        with mock.patch.object(control.subprocess, "Popen", return_value=process) as popen, \
             mock.patch.object(control.threading, "Thread") as thread:
            handle = control._SubprocessHandle(argv, cwd="/synthetic", env={}, lease_fd=7)
        thread.return_value.start.assert_called_once()
        return handle, process, popen

    def test_process_launch_is_direct_discards_stderr_and_inherits_only_lease(self):
        handle, process, popen = self.make_handle()
        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["shell"], False)
        self.assertIs(kwargs["close_fds"], True)
        self.assertIs(kwargs["start_new_session"], True)
        self.assertEqual(kwargs["pass_fds"], (7,))
        self.assertEqual(kwargs["stdin"], control.subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], control.subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], control.subprocess.PIPE)
        handle.terminate()
        handle.kill()
        process.terminate.assert_called_once()
        process.kill.assert_called_once()

    def test_result_is_unavailable_until_complete_eof(self):
        handle, process, _ = self.make_handle()
        self.assertIsNone(handle.read_result())
        def read(_fd, _limit):
            self.assertIsNone(handle.read_result())
            return next(chunks)
        chunks = iter((b'{"synthetic":', b"true}", b""))
        with mock.patch.object(control.os, "read", side_effect=read):
            handle._drain()
        self.assertEqual(handle.read_result(), b'{"synthetic":true}')
        self.assertFalse(handle.overflow)
        process.stdout.close.assert_called_once()

    def test_output_drain_remains_bounded_and_marks_overflow(self):
        handle, process, _ = self.make_handle()
        chunks = [b"x" * 4096] * (control.MAX_REPORT // 4096 + 2) + [b""]
        with mock.patch.object(control.os, "read", side_effect=chunks):
            handle._drain()
        self.assertEqual(len(handle.read_result()), control.MAX_REPORT)
        self.assertTrue(handle.overflow)
        process.stdout.close.assert_called_once()

    def test_output_read_error_is_not_mistaken_for_complete_valid_report(self):
        handle, process, _ = self.make_handle()
        with mock.patch.object(control.os, "read", side_effect=OSError(PRIVATE_MARKER)):
            handle._drain()
        self.assertTrue(handle.overflow)
        self.assertEqual(handle.read_result(), b"")
        process.stdout.close.assert_called_once()

    def test_real_synthetic_child_retains_inherited_lock_until_exit(self):
        # The child only validates an inherited FD and waits for a local file.
        # It never imports the voice runner or accesses a socket/audio device.
        helper = (
            "import os, pathlib, sys, time\n"
            "os.fstat(int(sys.argv[1]))\n"
            "pathlib.Path(sys.argv[2]).write_text('ready')\n"
            "deadline = time.monotonic() + 1.0\n"
            "while not pathlib.Path(sys.argv[3]).exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.005)\n"
        )
        with tempfile.TemporaryDirectory(prefix="voice-lock-test-") as directory:
            root = Path(directory)
            lock_path, ready, release = (root / name for name in ("lease", "ready", "release"))
            owner = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            contender = None
            handle = None
            try:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                handle = control._SubprocessHandle(
                    [sys.executable, "-c", helper, str(owner), str(ready), str(release)],
                    cwd=directory, env={"PYTHONDONTWRITEBYTECODE": "1"}, lease_fd=owner)
                deadline = time.monotonic() + 0.75
                while not ready.exists() and time.monotonic() < deadline and handle.poll() is None:
                    time.sleep(0.005)
                self.assertTrue(ready.exists(), "synthetic lock holder did not become ready")
                os.close(owner)
                owner = None
                contender = os.open(lock_path, os.O_RDWR)
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                release.touch()
                self.assertEqual(handle.process.wait(timeout=0.75), 0)
                self.assertTrue(handle._done.wait(0.25))
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(handle.read_result(), b"")
            finally:
                release.touch()
                if handle is not None and handle.poll() is None:
                    handle.kill()
                    handle.process.wait(timeout=0.5)
                if handle is not None:
                    handle._done.wait(0.25)
                for descriptor in (owner, contender):
                    if descriptor is not None:
                        os.close(descriptor)


class ControlCLIValidationTests(ControlFixture):
    def test_validate_cli_reports_no_network_actions_and_never_spawns(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
             mock.patch.object(control.subprocess, "Popen") as popen:
            code = control.main(["validate", "--config", str(self.local / "control.json"),
                                 "--workspace", str(self.workspace)])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertIs(report["valid"], True)
        self.assertEqual(report["network_actions"], 0)
        self.assertIs(report["microphone_started"], False)
        self.assert_safe(report)
        popen.assert_not_called()
        self.assertFalse((self.local / "control").exists())


class ControlCLIResponseTests(unittest.TestCase):
    def response(self):
        return {"schema_version": 1, "phone": "201", "line": "202",
                "session_id": "a" * 32, "state": "stopped", "active": False,
                "remaining_seconds": 0, "stop_requested": True, "stop_confirmed": True,
                "error": None, "transcript": PRIVATE_MARKER}

    def invoke(self, response, *, action="status", dry_run=False, envelope=None):
        adapter = mock.Mock()
        args = [action, "--client-config", "synthetic-private-client.json", "--line", "202"]
        body = None
        expected = {"voice": response}
        if action == "start":
            key = "synthetic-start-request"
            args += ["--request-id", key, "--attended", "--exclusive-media", "--enable-microphone"]
            if dry_run:
                args.append("--dry-run")
            body = {"idempotency_key": key, "attended": True, "exclusive_media": True,
                    "enable_microphone": True, "dry_run": dry_run}
            expected.update(status="accepted", action_id=key)
        elif action == "stop":
            session_id = "a" * 32
            args += ["--session-id", session_id]
            body = {"idempotency_key": "voice-stop-" + session_id, "session_id": session_id}
            expected.update(status="accepted", action_id=body["idempotency_key"])
        adapter.Gateway.return_value.request.return_value = expected if envelope is None else envelope
        adapter.load_config.return_value = {}
        output = io.StringIO()
        with mock.patch.dict(sys.modules, {"cisco": adapter}), contextlib.redirect_stdout(output):
            code = control.main(args)
        adapter.Gateway.return_value.request.assert_called_once_with(
            "/v1/phones/202/voice_" + action, body)
        return code, json.loads(output.getvalue())

    def test_status_client_uses_requested_line_and_omits_untrusted_text(self):
        response = self.response()
        response["error"] = PRIVATE_MARKER
        code, report = self.invoke(response)
        self.assertEqual(code, 0)
        self.assertEqual(report["phone"], "201")
        self.assertNotIn("transcript", report)
        self.assertNotIn(PRIVATE_MARKER, json.dumps(report))

    def test_invalid_status_metadata_is_refused_without_echoing_response(self):
        changes = ({"line": PRIVATE_MARKER}, {"session_id": PRIVATE_MARKER},
                   {"schema_version": True}, {"active": 1},
                   {"remaining_seconds": float("nan")}, {"remaining_seconds": 31})
        for change in changes:
            with self.subTest(field=next(iter(change))):
                code, report = self.invoke(dict(self.response(), **change))
                self.assertEqual(code, 2)
                self.assertIs(report["automatic_retry"], False)
                self.assertNotIn(PRIVATE_MARKER, json.dumps(report))

    def test_mutations_require_exact_accepted_status_and_submitted_action_id(self):
        for action in ("start", "stop"):
            key = "synthetic-start-request" if action == "start" else "voice-stop-" + "a" * 32
            expected = {"voice": self.response(), "status": "accepted", "action_id": key}
            code, _ = self.invoke(self.response(), action=action, envelope=expected)
            self.assertEqual(code, 0)
            bad = [dict(expected, status=value) for value in (None, True, "Accepted", "refused")]
            bad += [dict(expected, action_id=value) for value in (None, 1, PRIVATE_MARKER)]
            bad += [{key: value for key, value in expected.items() if key != missing}
                    for missing in ("status", "action_id")]
            for envelope in bad:
                with self.subTest(action=action, status=envelope.get("status"),
                                  action_id=envelope.get("action_id")):
                    code, report = self.invoke(self.response(), action=action, envelope=envelope)
                    self.assertEqual(code, 2)
                    self.assertIs(report["automatic_retry"], False)
                    self.assertNotIn(PRIVATE_MARKER, json.dumps(report))

    def test_stop_response_must_identify_requested_session(self):
        for session_id in (None, "b" * 32):
            with self.subTest(session_id=session_id):
                response = dict(self.response(), session_id=session_id)
                code, report = self.invoke(response, action="stop")
                self.assertEqual(code, 2)
                self.assertIs(report["automatic_retry"], False)

    def dry_response(self):
        return {"schema_version": 1, "phone": "201", "line": "202",
                "valid": True, "network_actions": 0,
                "microphone_started": False, "duration_seconds": 10,
                "transcript": PRIVATE_MARKER}

    def test_dry_validation_is_accepted_only_for_explicit_start_dry_run(self):
        response = self.dry_response()
        code, report = self.invoke(response, action="start", dry_run=True)
        self.assertEqual(code, 0)
        self.assertIs(report["valid"], True)
        self.assertEqual(report["network_actions"], 0)
        self.assertIs(report["microphone_started"], False)
        self.assertNotIn(PRIVATE_MARKER, json.dumps(report))
        for action in ("status", "start", "stop"):
            with self.subTest(action=action):
                code, report = self.invoke(response, action=action)
                self.assertEqual(code, 2)
                self.assertIs(report["automatic_retry"], False)

    def test_dry_validation_fields_require_literal_types_and_bounded_duration(self):
        changes = ({"schema_version": True}, {"schema_version": 1.0}, {"schema_version": 2},
                   {"valid": 1}, {"valid": False}, {"network_actions": False},
                   {"network_actions": 0.0}, {"network_actions": 1},
                   {"microphone_started": 0}, {"microphone_started": True},
                   {"duration_seconds": 0}, {"duration_seconds": -1},
                   {"duration_seconds": 30.01}, {"duration_seconds": True},
                   {"duration_seconds": "10"}, {"duration_seconds": float("inf")},
                   {"duration_seconds": float("nan")}, {"phone": PRIVATE_MARKER},
                   {"line": "201"})
        invalid = [dict(self.dry_response(), **change) for change in changes]
        invalid += [{key: value for key, value in self.dry_response().items() if key != missing}
                    for missing in ("schema_version", "valid", "network_actions",
                                    "microphone_started", "duration_seconds", "phone", "line")]
        for index, response in enumerate(invalid):
            with self.subTest(invalid_case=index):
                code, report = self.invoke(response, action="start", dry_run=True)
                self.assertEqual(code, 2)
                self.assertIs(report["automatic_retry"], False)
        for duration in (0.1, 30):
            with self.subTest(duration=duration):
                code, _ = self.invoke(dict(self.dry_response(), duration_seconds=duration),
                                      action="start", dry_run=True)
                self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
