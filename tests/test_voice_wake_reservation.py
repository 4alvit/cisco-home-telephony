"""Cross-manager reservation tests without sockets, inference or real children."""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import subprocess
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "voice_wake_reserved_test", ROOT / "containers/voice-wake/run_reserved.py")
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)
control = runner.control


def report(*, confirmed=True, reason="turn_complete"):
    return json.dumps({
        "error": None, "cleanup_errors": [],
        "result": {"reason": reason, "stop_confirmed": confirmed},
        "watchdog_armed": True, "watchdog_finished": True,
        "watchdog_stop_errors": 0, "watchdog_stop_confirmed": confirmed,
    }).encode()


class Child:
    def __init__(self):
        self.code, self.raw, self.overflow = None, None, False
        self.terminations, self.kills = 0, 0

    def poll(self): return self.code
    def read_result(self): return self.raw
    def terminate(self): self.terminations += 1
    def kill(self):
        self.kills += 1
        self.code = -9


class ReservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wake-reservation-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.local = self.root / ".local"
        self.local.mkdir(mode=0o700)
        self.state = self.local / "state" / "voice-control"
        self.state.mkdir(parents=True, mode=0o700)
        self.models = self.local / "models"
        self.models.mkdir(mode=0o700)
        self.gateway = {
            "persistent_enabled": True, "bind_ipv4": "192.168.50.10", "api_port": 18084,
            "api_token": "a" * 48, "api_clients": ["192.168.50.11"],
            "ami": {"host": "192.168.50.10", "port": 5038,
                    "username": "synthetic", "secret": "private-ami-test-value"},
            "dial_allowlist": [], "ring_allowlist": [],
            "phones": [{"id": "201", "lines": ["201", "202"], "ipv4": "192.168.50.20",
                        "device": "SEP020000000001", "port": 18080,
                        "auth_path": "/auth/" + "c" * 64, "username": "u" * 24,
                        "password": "p" * 24, "room": "Private example label"}],
        }
        self.transport = {
            "gateway_config": ".local/gateway.json", "line": "202",
            "bind_ipv4": "192.168.50.10", "bind_port": 24576, "phone_rtp_port": 24578,
            "duration_seconds": 30, "receive_idle_seconds": 3, "volume": 20,
        }
        self.voice = {
            "service": {"enabled": True, "rearm_after_turn": False},
            "phones": {"201": True},
            "ha": {"base_url": "http://192.168.50.30:8123", "token_file": ".local/token.txt",
                   "pipeline_id": "synthetic-pipeline"},
            "wake": {"mode": "onnx", "bundle_file": ".local/models/bundle.json"},
        }
        bundle = {"format_version": 1, "frontend": "echomuse-oww-16k-v1",
                  "threshold": .5, "models": {}}
        for role in ("mel", "embedding", "classifier"):
            data = ("synthetic-" + role).encode()
            path = self.models / (role + ".onnx")
            path.write_bytes(data)
            path.chmod(0o600)
            bundle["models"][role] = {"path": path.name, "sha256": hashlib.sha256(data).hexdigest()}
        self.write(self.models / "bundle.json", bundle)
        self.write(self.local / "gateway.json", self.gateway)
        self.write(self.local / "transport.json", self.transport)
        self.write(self.local / "voice.json", self.voice)
        self.write(self.local / "token.txt", "synthetic-private-HA-token")
        self.args = argparse.Namespace(
            workspace=str(self.root), state_directory=str(self.state),
            transport_config=".local/transport.json", voice_config=".local/voice.json",
            ffmpeg=sys.executable, display_port=18090,
            attempt_id="synthetic-attempt-001",
        )
        self.now = 100.0
        self.children, self.launches, self.managers = [], [], []
        self.addCleanup(self.cleanup_managers)

    @staticmethod
    def write(path, value):
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        path.chmod(0o600)

    def factory(self, argv, *, cwd, env, lease_fd):
        os.fstat(lease_fd)  # The descriptor passed to the child is still open.
        self.launches.append((argv, cwd, env, lease_fd))
        child = Child()
        self.children.append(child)
        return child

    def manager(self):
        inputs = runner.load_inputs(self.args)
        instance = runner.ReservedWakeControl(
            self.args, inputs, monitor=False, clock=lambda: self.now, runner_factory=self.factory)
        self.managers.append(instance)
        return instance

    def gateway_manager(self, wake):
        instance = control.VoiceControl(
            wake.config, authorize=lambda *_: True, monitor=False, clock=lambda: self.now,
            runner_factory=lambda *_a, **_k: self.fail("gateway must not launch"))
        self.managers.append(instance)
        return instance

    def cleanup_managers(self):
        for child in self.children:
            if child.code is None:
                child.code, child.raw = 0, report()
        for manager in self.managers:
            manager.tick()
            manager.close()

    def start(self, manager, request_id="synthetic-request-001"):
        return manager.start("202", request_id, context=None,
                             attended=True, exclusive_media=True, enable_microphone=True)

    def finish(self, manager, raw=None):
        child = self.children[-1]
        child.code, child.raw = 0, raw if raw is not None else report()
        manager.tick()

    def test_dry_run_does_not_infer_spawn_open_socket_or_write_state(self):
        inference_loaded_before = "onnxruntime" in sys.modules
        argv = ["--workspace", str(self.root), "--state-directory", str(self.state),
                "--transport-config", ".local/transport.json",
                "--voice-config", ".local/voice.json", "--ffmpeg", sys.executable]
        output = io.StringIO()
        with mock.patch.object(runner, "run", side_effect=AssertionError("no run")), \
                mock.patch("socket.socket", side_effect=AssertionError("no socket")), \
                contextlib.redirect_stdout(output):
            self.assertEqual(runner.main(argv), 0)
        self.assertEqual(json.loads(output.getvalue()),
                         {"valid": True, "network_actions": 0, "microphone_started": False})
        self.assertEqual(list(self.state.iterdir()), [])
        self.assertEqual("onnxruntime" in sys.modules, inference_loaded_before)

    def test_exact_gateway_lock_blocks_wake_before_spawn(self):
        wake = self.manager()
        gateway = self.gateway_manager(wake)
        fd = gateway._acquire(wake.inputs.profile)
        try:
            with self.assertRaisesRegex(control.ControlError, "voice_phone_busy"):
                self.start(wake)
            self.assertEqual(self.launches, [])
        finally:
            os.close(fd)

    def test_wake_blocks_gateway_and_sibling_phone_line_on_same_lock(self):
        wake = self.manager()
        result = self.start(wake)
        self.assertEqual(result["phone"], "201")
        self.assertEqual(result["line"], "202")
        gateway = self.gateway_manager(wake)
        self.assertEqual(gateway._paths(wake.inputs.profile), wake._paths(wake.inputs.profile))
        with self.assertRaisesRegex(control.ControlError, "voice_phone_busy"):
            gateway._acquire(wake.inputs.profile)
        other = self.manager()
        with self.assertRaisesRegex(control.ControlError, "voice_phone_busy"):
            self.start(other)
        self.assertEqual(len(self.launches), 1)
        self.assertTrue(wake._journal(wake.inputs.profile)["reserved"])

    def test_verified_completion_releases_shared_reservation_and_run_snapshot(self):
        wake = self.manager()
        self.start(wake)
        snapshot = next(self.state.glob("run-*"))
        self.finish(wake)
        self.assertFalse(wake._journal(wake.inputs.profile)["reserved"])
        self.assertFalse(snapshot.exists())
        fd = self.gateway_manager(wake)._acquire(wake.inputs.profile)
        os.close(fd)

    def test_unconfirmed_or_malformed_cleanup_retains_journal_and_snapshot(self):
        for raw in (report(confirmed=False), b"malformed"):
            with self.subTest(raw=raw):
                wake = self.manager()
                self.start(wake)
                self.finish(wake, raw)
                self.assertEqual(wake.status("201", context=None)["state"], "recovery_required")
                self.assertTrue(wake._journal(wake.inputs.profile)["reserved"])
                self.assertTrue(list(self.state.glob("run-*")))
                with self.assertRaisesRegex(control.ControlError, "voice_recovery_required"):
                    self.gateway_manager(wake)._acquire(wake.inputs.profile)
                # Isolate synthetic cases; production never deletes these.
                import shutil
                for path in self.state.iterdir():
                    shutil.rmtree(path) if path.is_dir() else path.unlink()

    def test_crash_reservation_blocks_new_manager_after_descriptor_closes(self):
        wake = self.manager()
        self.start(wake)
        current = next(iter(wake._sessions.values()))
        os.close(current.lease_fd)
        current.lease_fd = -1
        with self.assertRaisesRegex(control.ControlError, "voice_recovery_required"):
            self.gateway_manager(wake)._acquire(wake.inputs.profile)

    def test_changed_private_inputs_prevent_launch_after_reservation(self):
        wake = self.manager()
        self.write(self.local / "token.txt", "replacement-private-token")
        with self.assertRaisesRegex(control.ControlError, "voice_start_failed"):
            self.start(wake)
        self.assertEqual(self.launches, [])
        self.assertFalse(wake._journal(wake.inputs.profile)["reserved"])

    def test_model_hash_change_rejected_before_inference_or_launch(self):
        (self.models / "classifier.onnx").write_bytes(b"changed")
        with self.assertRaisesRegex(control.ControlError, "private_model_checksum"):
            self.manager()
        self.assertEqual(self.launches, [])

    def test_model_read_is_bounded_by_remaining_total_before_allocation(self):
        total = sum(path.stat().st_size for path in self.models.glob("*.onnx"))
        with mock.patch.object(runner, "MAX_TOTAL_BYTES", total - 1), \
                mock.patch.object(control, "_read", wraps=control._read) as read:
            with self.assertRaisesRegex(control.ControlError, "private_file_required"):
                runner.load_inputs(self.args)
        model_calls = [call for call in read.call_args_list if call.args[0].suffix == ".onnx"]
        self.assertEqual(len(model_calls), 3)
        self.assertEqual(model_calls[-1].args[1],
                         (self.models / "classifier.onnx").stat().st_size - 1)

    def test_snapshot_preserves_wake_and_minimizes_gateway(self):
        wake = self.manager()
        self.start(wake)
        snapshot = next(self.state.glob("run-*"))
        voice = json.loads((snapshot / "voice.json").read_text())
        gateway = json.loads((snapshot / "gateway.json").read_text())
        self.assertEqual(voice["wake"]["mode"], "onnx")
        self.assertEqual(voice["wake"]["bundle_file"], str(self.models / "bundle.json"))
        self.assertNotIn("room", gateway["phones"][0])
        self.assertNotIn("a" * 48, json.dumps(gateway))
        self.assertEqual({path.name for path in snapshot.iterdir()},
                         {"voice.json", "gateway.json", "transport.json", "ha-token.txt"})

    def test_owner_lease_is_30_seconds_and_killed_child_cannot_confirm_cleanup(self):
        wake = self.manager()
        self.start(wake)
        child = self.children[-1]
        self.now = 129.99
        wake.tick()
        self.assertEqual(child.terminations, 0)
        self.now = 130.0
        wake.tick()
        self.assertEqual(child.terminations, 1)
        self.now += control.STOP_GRACE_SECONDS
        wake.tick()
        self.assertEqual(child.kills, 1)
        child.raw = report()
        wake.tick()
        self.assertEqual(wake.status("201", context=None)["state"], "recovery_required")
        self.assertTrue(wake._journal(wake.inputs.profile)["reserved"])

    def test_missing_or_unsafe_shared_state_is_not_recreated_or_chmodded(self):
        self.state.chmod(0o755)
        with self.assertRaisesRegex(control.ControlError, "shared_state"):
            self.manager()
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o755)
        self.state.rmdir()
        with self.assertRaises(FileNotFoundError):
            self.manager()
        self.assertFalse(self.state.exists())

    def test_rearm_long_lease_and_state_symlink_are_rejected(self):
        self.transport["duration_seconds"] = 31
        self.write(self.local / "transport.json", self.transport)
        with self.assertRaises(ValueError):
            self.manager()
        self.transport["duration_seconds"] = 30
        self.write(self.local / "transport.json", self.transport)
        self.voice["service"]["rearm_after_turn"] = True
        self.write(self.local / "voice.json", self.voice)
        with self.assertRaisesRegex(control.ControlError, "bounded_wake"):
            self.manager()
        self.args.state_directory = str(self.local / "alias")
        (self.local / "alias").symlink_to(self.state, target_is_directory=True)
        with self.assertRaisesRegex(control.ControlError, "symlink"):
            self.manager()

    def test_completion_and_clean_stop_have_different_success_semantics(self):
        for index, reason in enumerate(("turn_complete", "display_stop", "explicit_stop", "shutdown",
                                        "lease_expired")):
            with self.subTest(reason=reason):
                wake = self.manager()
                self.start(wake, "reason-test-" + str(index))
                self.finish(wake, report(reason=reason))
                result = wake.result_summary("202")
                self.assertTrue(result["cleanup_success"])
                self.assertEqual(result["turn_success"], reason == "turn_complete")
                self.assertEqual(result["success"], result["turn_success"])
                self.assertEqual(result["reason"], reason)

    def test_nonzero_child_exit_is_not_a_successful_turn(self):
        wake = self.manager()
        self.start(wake)
        self.children[-1].code, self.children[-1].raw = 1, report()
        wake.tick()
        result = wake.result_summary("202")
        self.assertTrue(result["cleanup_success"])
        self.assertFalse(result["turn_success"])

    def test_readonly_configuration_with_writable_state_supports_real_child_validation(self):
        # Directory permissions model the renderer's RO runtime plus RW state
        # submount. This subprocess validates only: no --run and no microphone.
        for path in (self.root, self.local, self.models, self.local / "state"):
            path.chmod(0o555)
        try:
            wake = self.manager()
            self.start(wake)
            snapshot = next(self.state.glob("run-*"))
            command = [
                sys.executable, str(ROOT / "scripts/voice_session.py"),
                "--transport-config", str(snapshot / "transport.json"),
                "--voice-config", str(snapshot / "voice.json"), "--workspace", str(self.root),
            ]
            completed = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout),
                             {"valid": True, "network_actions": 0, "microphone_started": False})
        finally:
            for path in (self.root, self.local, self.models, self.local / "state"):
                path.chmod(0o700)

    def test_same_attempt_is_consumed_across_clean_wrapper_runs(self):
        first = self.manager()
        def immediate(*args, **kwargs):
            child = self.factory(*args, **kwargs)
            child.code, child.raw = 0, report()
            return child
        first.runner_factory = immediate
        with mock.patch.object(runner, "ReservedWakeControl", return_value=first):
            result = runner.run(self.args, first.inputs)
        self.assertTrue(result["turn_success"])
        self.assertTrue(result["cleanup_success"])
        second = self.manager()
        with mock.patch.object(runner, "ReservedWakeControl", return_value=second), \
                self.assertRaises(FileExistsError):
            runner.run(self.args, second.inputs)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(len(list(self.state.glob("wake-attempt-*.consumed"))), 1)

    def test_sigterm_during_preparation_prevents_child_and_retains_attempt(self):
        real_manager = runner.ReservedWakeControl
        real_prepare = real_manager._prepare
        real_drain = runner.drain_cleanup

        def create(*args, **kwargs):
            instance = real_manager(*args, **kwargs, runner_factory=self.factory)
            self.managers.append(instance)
            return instance

        def prepare_then_stop(instance, *args):
            command = real_prepare(instance, *args)
            os.kill(os.getpid(), signal.SIGTERM)
            return command

        def bounded_drain(manager, line, *, uncertain):
            now = [100.]
            real_drain(manager, line, uncertain=uncertain, clock=lambda: now[0],
                       sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))

        inputs = runner.load_inputs(self.args)
        with mock.patch.object(runner, "ReservedWakeControl", side_effect=create), \
                mock.patch.object(real_manager, "_prepare", new=prepare_then_stop), \
                mock.patch.object(runner, "drain_cleanup", side_effect=bounded_drain), \
                self.assertRaisesRegex(control.ControlError, "voice_start_failed"):
            runner.run(self.args, inputs)
        self.assertEqual(self.launches, [])
        self.assertEqual(len(list(self.state.glob("wake-attempt-*.consumed"))), 1)
        self.assertTrue(self.managers[-1]._journal(inputs.profile)["reserved"])

    def test_concurrent_attempt_claims_have_exactly_one_winner(self):
        inputs = runner.load_inputs(self.args)
        def claim(_):
            try:
                runner.claim_attempt(inputs, self.args.attempt_id)
                return True
            except FileExistsError:
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(claim, range(4)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(len(list(self.state.glob("wake-attempt-*.consumed"))), 1)

    def test_attempt_marker_symlink_and_missing_id_cannot_start_or_overwrite(self):
        inputs = runner.load_inputs(self.args)
        with self.assertRaisesRegex(control.ControlError, "explicit_wake_attempt"):
            runner.claim_attempt(inputs, None)
        runner.claim_attempt(inputs, self.args.attempt_id)
        marker = next(self.state.glob("wake-attempt-*.consumed"))
        marker.unlink()
        target = self.local / "token.txt"
        before = target.read_bytes()
        marker.symlink_to(target)
        with self.assertRaises(FileExistsError):
            runner.claim_attempt(inputs, self.args.attempt_id)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(self.launches, [])


class CleanupGraceTests(unittest.TestCase):
    def drain(self, manager, *, uncertain=False):
        now = [100.]
        def sleep(seconds):
            now[0] += seconds
        runner.drain_cleanup(manager, "202", uncertain=uncertain,
                             clock=lambda: now[0], sleep=sleep)
        return now[0] - 100.

    def test_recovery_keeps_container_alive_for_full_watchdog_grace(self):
        manager = SimpleNamespace(close=mock.Mock(), tick=mock.Mock(),
                                  status=mock.Mock(return_value={"active": False,
                                                               "state": "recovery_required"}))
        elapsed = self.drain(manager)
        self.assertGreaterEqual(elapsed, 55)
        self.assertLess(elapsed, 55.1)

    def test_storage_and_tick_errors_still_drain_full_bounded_grace(self):
        manager = SimpleNamespace(close=mock.Mock(side_effect=OSError("synthetic")),
                                  tick=mock.Mock(side_effect=OSError("synthetic")),
                                  status=mock.Mock())
        elapsed = self.drain(manager)
        self.assertGreaterEqual(elapsed, 55)
        self.assertLess(elapsed, 55.1)

    def test_verified_clean_completion_needs_no_artificial_delay(self):
        manager = SimpleNamespace(close=mock.Mock(), tick=mock.Mock(),
                                  status=mock.Mock(return_value={"active": False, "state": "stopped"}))
        self.assertEqual(self.drain(manager), 0)
        self.assertGreaterEqual(self.drain(manager, uncertain=True), 55)

    def test_exception_after_possible_spawn_uses_grace_before_propagating(self):
        manager = SimpleNamespace(
            _sessions={"example": SimpleNamespace(spawn_attempted=True)},
            start=mock.Mock(side_effect=OSError("synthetic-write-failure")),
            close=mock.Mock(), tick=mock.Mock(),
            status=mock.Mock(return_value={"active": False, "state": "recovery_required"}))
        inputs = SimpleNamespace(transport={"line": "202"}, profile=SimpleNamespace(duration=30))
        args = SimpleNamespace(attempt_id="synthetic-attempt")
        elapsed = []
        real_drain = runner.drain_cleanup
        def bounded(manager, line, *, uncertain):
            now = [100.]
            real_drain(manager, line, uncertain=uncertain, clock=lambda: now[0],
                       sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
            elapsed.append(now[0] - 100.)
        with mock.patch.object(runner, "ReservedWakeControl", return_value=manager), \
                mock.patch.object(runner, "claim_attempt", return_value="test-attempt"), \
                mock.patch.object(runner, "drain_cleanup", side_effect=bounded), \
                self.assertRaisesRegex(OSError, "synthetic-write-failure"):
            runner.run(args, inputs)
        self.assertEqual(len(elapsed), 1)
        self.assertGreaterEqual(elapsed[0], 55)
        self.assertLess(elapsed[0], 55.1)

    def test_failed_reader_construction_terminates_only_direct_child(self):
        process = mock.Mock()
        def fail_after_spawn(instance, *args, **kwargs):
            instance.process = process
            raise RuntimeError("synthetic-reader-thread-failure")
        with mock.patch.object(control._SubprocessHandle, "__init__", new=fail_after_spawn), \
                self.assertRaisesRegex(RuntimeError, "synthetic-reader"):
            runner.GuardedSubprocessHandle([])
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=2)
        process.kill.assert_not_called()
        process.stdout.close.assert_called_once_with()

    def test_failed_construction_kills_only_unresponsive_direct_child(self):
        process = mock.Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired("synthetic", 2), None]
        def fail_after_spawn(instance, *args, **kwargs):
            instance.process = process
            raise RuntimeError("synthetic-reader-thread-failure")
        with mock.patch.object(control._SubprocessHandle, "__init__", new=fail_after_spawn), \
                self.assertRaisesRegex(RuntimeError, "synthetic-reader"):
            runner.GuardedSubprocessHandle([])
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_args_list, [mock.call(timeout=2), mock.call(timeout=2)])


if __name__ == "__main__":
    unittest.main()
