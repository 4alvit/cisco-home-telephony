"""Exercise the standalone synthetic fixture across its real process/HTTP seams.

These are integration assertions, not a replacement for gateway, owner, runner,
or protocol unit tests. Only the fixture's loopback listener is contacted.
"""

import importlib.util
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/test-voice-integration.py"
spec = importlib.util.spec_from_file_location("voice_integration_fixture_under_test", SCRIPT)
fixture = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixture
spec.loader.exec_module(fixture)


def run_script(*args, timeout=45):
    """Interrupt a timed-out fixture so its real finally block can reap workers.

    SIGINT targets the parent first and allows eight seconds for orderly cleanup.
    Only a still-hung parent receives the process-group SIGKILL fallback. Detached
    worker sessions are not covered by that ultimate kill; this helper does not
    promise cleanup after forced parent death or a host crash.
    """
    environment = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8",
                   "HTTP_PROXY": "http://192.0.2.2:9", "HTTPS_PROXY": "http://192.0.2.2:9",
                   "ALL_PROXY": "http://192.0.2.2:9", "NO_PROXY": ""}
    with tempfile.TemporaryDirectory(prefix="integration-invocation-") as directory:
        process = subprocess.Popen([sys.executable, "-B", str(SCRIPT), *args],
            cwd=directory, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:
            try:
                process.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate(timeout=5)
            raise
    return process.returncode, stdout, stderr


@unittest.skipIf(sys.version_info < (3, 11), "Standalone fixture requires Python 3.11+")
class IntegratedFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exit_code, cls.stdout, cls.stderr = run_script("--scenario", "all", "--json")
        cls.summary = json.loads(cls.stdout)
        cls.runs = {row["scenario"]: row for row in cls.summary.get("runs", [])}

    def test_standalone_cli_completes_all_scenarios_and_is_bounded(self):
        self.assertEqual(self.exit_code, 0, self.stdout + self.stderr)
        self.assertTrue(self.summary["success"], self.summary)
        self.assertEqual(self.summary["scenario"], "all")
        self.assertEqual(set(self.runs), {"complete", "stop", "provider-failure"})
        self.assertEqual(self.summary["launches"], 3)
        self.assertGreater(self.summary["elapsed_seconds"], 0)
        self.assertLess(self.summary["elapsed_seconds"], 35)

    def test_real_http_authentication_denials_do_not_launch_workers(self):
        checks = self.summary["checks"]
        for name in ("initial_idle", "wrong_bearer_denied", "source_denied",
                     "missing_flags_no_launch", "dry_run_no_launch"):
            with self.subTest(check=name):
                self.assertIs(checks[name], True)
        requests = self.summary["http_requests"]
        status_requests = [r for r in requests if r["operation"] == "status"]
        self.assertEqual([(r["method"], r["operation"], r["status"]) for r in status_requests[:3]],
                         [("GET", "status", 200), ("GET", "status", 403), ("GET", "status", 403)])
        policy_requests = [r for r in requests if r["operation"] == "hands_free_status"]
        if policy_requests:
            self.assertEqual(requests[1], {"method": "GET", "operation": "hands_free_status", "status": 200})
            self.assertTrue(all(r["method"] == "GET" and r["status"] == 200 for r in policy_requests))
        self.assertTrue(any(r["method"] == "POST" and r["operation"] == "start"
                            and r["status"] == 400 for r in requests))
        self.assertEqual(self.summary["launches"], len(self.runs))

    def test_sibling_lines_share_one_physical_owner_and_idempotent_start(self):
        for row in self.runs.values():
            with self.subTest(scenario=row["scenario"]):
                self.assertIs(row["sibling_owner_identity"], True)
                self.assertIs(row["duplicate_request_one_launch"], True)
                self.assertEqual(row["cli_start_exit"], 0)
                expected_exit = 1 if row["scenario"] == "provider-failure" else 0
                self.assertEqual(row["cli_terminal_exit"], expected_exit)
                self.assertEqual(row["cli_terminal_replay_exit"], expected_exit)
        self.assertIs(self.summary["checks"]["sibling_concurrent_start_denied"], True)
        self.assertIs(self.summary["checks"]["stop_accepted_via_sibling"], True)

    def test_complete_turn_uses_real_assistant_protocol_and_pcm_playback_path(self):
        row = self.runs["complete"]
        self.assertEqual(row["terminal_state"], "stopped")
        self.assertEqual(row["runner_reason"], "turn_complete")
        self.assertEqual(row["runner_exit"], 0)
        self.assertGreater(row["input_frames"], 0)
        self.assertEqual(row["ha_audio_bytes"], 3840)
        self.assertEqual(row["output_frames"], 50, "One second must reach 50 actual 20 ms sink writes")
        for state in ("listening", "thinking", "speaking", "stopped"):
            self.assertIn(state, row["assistant_states"])
        self.assertLess(row["assistant_states"].index("listening"), row["assistant_states"].index("speaking"))

    def test_stop_interrupts_playback_via_real_worker_signal_and_closes_everything(self):
        row = self.runs["stop"]
        self.assertEqual(row["terminal_state"], "stopped")
        self.assertEqual(row["runner_reason"], "shutdown")
        self.assertEqual(row["runner_exit"], 0, "Confirmed clean Stop is not a runtime failure")
        self.assertGreater(row["output_frames"], 0)
        self.assertLess(row["output_frames"], 150, "Stop must interrupt the three-second fixture reply")
        self.assertIn("speaking", row["assistant_states"])
        self.assertIs(row["stop_confirmed"], True)
        self.assertTrue(any(r["method"] == "POST" and r["operation"] == "stop"
                            and r["status"] == 200 for r in self.summary["http_requests"]))

    def test_provider_failure_is_failed_not_fabricated_success_and_has_no_playback(self):
        row = self.runs["provider-failure"]
        self.assertEqual(row["terminal_state"], "failed")
        self.assertEqual(row["runner_reason"], "assistant_error")
        self.assertEqual(row["runner_exit"], 1)
        self.assertGreater(row["ha_audio_bytes"], 0)
        self.assertGreater(row["input_frames"], 0)
        self.assertEqual(row["output_frames"], 0)
        self.assertIn("error", row["assistant_states"])
        self.assertNotIn("speaking", row["assistant_states"])
        self.assertIs(row["stop_confirmed"], True)
        failed = {"state": "failed", "error": "voice_session_failed",
                  "active": False, "stop_confirmed": True}
        expected = fixture.FixtureCoordinator._expected_cli_exit
        self.assertTrue(expected("provider-failure", 1, {"voice": failed}))
        self.assertFalse(expected("complete", 1, {"voice": failed}))
        self.assertFalse(expected("provider-failure", 2, {"voice": failed}))
        self.assertFalse(expected("provider-failure", 0, {"voice": failed}))
        for changes in ({"state": "recovery_required"}, {"error": "voice_start_failed"},
                        {"active": True}, {"stop_confirmed": False}):
            with self.subTest(unexpected_failure=changes):
                self.assertFalse(expected("provider-failure", 1, {"voice": dict(failed, **changes)}))

    def test_resources_and_owner_reservations_are_released_after_each_outcome(self):
        for row in self.runs.values():
            with self.subTest(scenario=row["scenario"]):
                self.assertIs(row["ha_closed"], True)
                self.assertIs(row["media_closed"], True)
                self.assertIs(row["observer_stopped"], True)
                self.assertEqual(row["send_after_close"], 0)
        self.assertEqual(self.summary["owner_files"], {"locks": 1, "journals": 1})
        self.assertEqual(self.summary["policy_files"], {"locks": 1, "journals": 0})
        for name in ("lease_released", "owner_journal_unreserved", "run_snapshots_removed", "private_permissions"):
            self.assertIs(self.summary["checks"][name], True)
        for name in ("workspace_removed", "children_reaped", "listener_closed", "owner_monitor_stopped", "run_snapshots_removed"):
            self.assertIs(self.summary["cleanup"][name], True)
        self.assertEqual(self.summary["cleanup_errors"], [])

    def test_report_states_remaining_simulations_without_leaking_private_inputs(self):
        real = " ".join(self.summary["boundaries"]["real"])
        simulated = " ".join(self.summary["boundaries"]["simulated"])
        for name in ("cisco", "HTTP", "VoiceControl", "_SubprocessHandle", "voice_session",
                     "PhoneVoiceController", "AssistantService", "HAClient", "WavePlayback", "RtpPCMSink"):
            self.assertIn(name, real)
        for name in ("loopback", "bootstrap", "AMI", "UDP", "watchdog", "HA"):
            self.assertIn(name, simulated)
        self.assertEqual(self.summary["external_network_attempts"], 0)
        self.assertNotIn("synthetic-integration-token", self.stdout + self.stderr)
        self.assertNotIn("synthetic-ami", self.stdout + self.stderr)
        self.assertNotIn("Bearer ", self.stdout + self.stderr)
        self.assertNotIn(str(ROOT / ".local"), self.stdout + self.stderr)


class FixtureSafetyTests(unittest.TestCase):
    @unittest.skipIf(sys.version_info < (3, 11), "Standalone fixture requires Python 3.11+")
    def test_failed_scenario_keeps_terminal_state_and_worker_report_before_cleanup(self):
        expected = fixture.FixtureCoordinator._expected_cli_exit

        def reject_terminal_failure(scenario, code, response):
            if response["voice"]["state"] == "failed":
                return False
            return expected(scenario, code, response)

        with mock.patch.object(fixture.FixtureCoordinator, "_expected_cli_exit",
                               new=staticmethod(reject_terminal_failure)):
            summary = fixture.run_fixture(scenario="provider-failure", timeout_seconds=30)
        self.assertFalse(summary["success"])
        self.assertEqual(summary["error"], "unexpected_cli_exit")
        self.assertEqual(summary["runs"], [])
        terminal = summary["cli_commands"][-1]
        self.assertEqual((terminal["exit_code"], terminal["status"]), (1, "ok"))
        self.assertEqual(terminal["voice"]["state"], "failed")
        self.assertEqual(terminal["voice"]["error"], "voice_session_failed")
        self.assertEqual(terminal["voice"]["phase"], "error")
        self.assertFalse(terminal["voice"]["active"])
        self.assertTrue(terminal["voice"]["stop_confirmed"])
        self.assertEqual(len(summary["worker_diagnostics"]), 1)
        worker = summary["worker_diagnostics"][0]
        self.assertEqual((worker["scenario"], worker["process_exit"], worker["report_state"]),
                         ("provider-failure", 1, "available"))
        self.assertEqual(worker["error"], "voice_session_failed")
        self.assertEqual(worker["reason"], "assistant_error")
        self.assertEqual(worker["cleanup_errors"], [])
        self.assertTrue(worker["stop_confirmed"])
        self.assertTrue(worker["watchdog_stop_confirmed"])
        self.assertGreater(worker["input_frames"], 0)
        self.assertEqual(worker["output_frames"], 0)
        self.assertNotIn("run_snapshots_removed", summary["checks"])
        self.assertEqual(summary["snapshot_observation"], {"checked": True, "remaining_count": 0})
        self.assertTrue(all(summary["cleanup"].values()), summary)
        self.assertEqual(summary["cleanup_errors"], [])

    def test_retained_snapshot_is_observed_before_workspace_cleanup(self):
        coordinator = fixture.FixtureCoordinator("complete", 30)
        try:
            retained = coordinator.local / "owner" / "run-synthetic"
            retained.mkdir(parents=True, mode=0o700)
        finally:
            coordinator.cleanup()
        summary = coordinator.summary
        self.assertNotIn("run_snapshots_removed", summary["checks"])
        self.assertEqual(summary["snapshot_observation"], {"checked": True, "remaining_count": 1})
        self.assertFalse(summary["cleanup"]["run_snapshots_removed"])
        self.assertTrue(summary["cleanup"]["workspace_removed"])
        self.assertFalse(retained.exists())
        self.assertFalse(summary["success"])

    def test_failure_diagnostics_are_bounded_allowlisted_and_path_free(self):
        coordinator = fixture.FixtureCoordinator("complete", 30)
        marker = "synthetic-private-report-detail"
        try:
            voice = coordinator._voice_diagnostic({
                "state": "recovery_required", "error": "voice_stop_unconfirmed",
                "phase": "error", "phase_reason": "shutdown", "active": False,
                "stop_requested": True, "stop_confirmed": False,
                "session_id": marker, "phone": marker, "token": marker,
            })
            self.assertEqual(set(voice), {"state", "error", "phase", "phase_reason", "active",
                                          "stop_requested", "stop_confirmed"})
            self.assertEqual(coordinator._voice_diagnostic({
                "state": marker, "error": marker, "phase": marker, "phase_reason": marker,
                "active": marker, "stop_requested": 1, "stop_confirmed": 0}), {})
            handle = mock.Mock(overflow=False)
            handle.poll.return_value = 1
            handle.read_result.return_value = json.dumps({
                "error": marker, "token": marker, "output_frames": 10 ** 50,
                "cleanup_errors": ["assistant_cleanup_failed", marker],
                "result": {"reason": marker, "input_frames": True, "stop_confirmed": False},
            }).encode()
            launch = {"scenario": "complete", "handle": handle}
            diagnostic = coordinator._worker_diagnostic(launch)
            self.assertEqual(diagnostic["report_state"], "available")
            self.assertEqual(diagnostic["error"], "unrecognized")
            self.assertEqual(diagnostic["reason"], "unrecognized")
            self.assertEqual(diagnostic["cleanup_errors"], ["assistant_cleanup_failed", "unrecognized"])
            self.assertNotIn("output_frames", diagnostic)
            self.assertNotIn("input_frames", diagnostic)
            self.assertNotIn(marker, json.dumps([voice, diagnostic]))
            for raw in (None, b"invalid", b"x" * (fixture.DIAGNOSTIC_REPORT_LIMIT + 1)):
                handle.read_result.return_value = raw
                row = coordinator._worker_diagnostic(launch)
                self.assertEqual(row["report_state"], "unavailable" if raw is None else "invalid")
                self.assertNotIn("error", row)
        finally:
            coordinator.cleanup()

    @unittest.skipIf(sys.version_info < (3, 11), "Standalone fixture requires Python 3.11+")
    def test_http_thread_start_failure_closes_unstarted_listener_without_shutdown_wait(self):
        original_load = fixture.load_module
        original_start = threading.Thread.start
        original_temporary = tempfile.TemporaryDirectory
        listeners, workspaces, failures = [], [], []
        marker = "synthetic-private-thread-start-detail"

        def load(name, path):
            module = original_load(name, path)
            if name == "_voice_integration_gateway":
                original_server = module.ThreadingHTTPServer

                def server(*args, **kwargs):
                    instance = original_server(*args, **kwargs)
                    # shutdown() waits for serve_forever(), so calling it here
                    # is a regression. Fail promptly instead of hanging tests.
                    instance.shutdown = mock.Mock(side_effect=AssertionError("unstarted_listener_shutdown"))
                    instance.server_close = mock.Mock(wraps=instance.server_close)
                    listeners.append(instance)
                    self.addCleanup(instance.server_close)
                    return instance

                module.ThreadingHTTPServer = server
            return module

        def start(thread, *args, **kwargs):
            if thread.name == "fixture-http":
                failures.append(True)
                raise RuntimeError(marker)
            return original_start(thread, *args, **kwargs)

        def temporary(*args, **kwargs):
            directory = original_temporary(*args, **kwargs)
            workspaces.append(Path(directory.name))
            self.addCleanup(directory.cleanup)
            return directory

        with mock.patch.object(fixture, "load_module", side_effect=load), \
             mock.patch.object(threading.Thread, "start", new=start), \
             mock.patch.object(tempfile, "TemporaryDirectory", side_effect=temporary), \
             mock.patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected child")) as popen:
            summary = fixture.run_fixture(scenario="complete", timeout_seconds=30)
        self.assertEqual(failures, [True])
        self.assertFalse(summary["success"])
        self.assertEqual(summary["error"], "RuntimeError")
        self.assertNotIn(marker, json.dumps(summary))
        self.assertLess(summary["elapsed_seconds"], 5)
        self.assertEqual(summary["launches"], 0)
        popen.assert_not_called()
        self.assertEqual(len(listeners), 1)
        listeners[0].shutdown.assert_not_called()
        listeners[0].server_close.assert_called_once()
        self.assertEqual(listeners[0].socket.fileno(), -1)
        self.assertTrue(workspaces)
        self.assertTrue(all(not path.exists() for path in workspaces))
        for name in ("workspace_removed", "children_reaped", "listener_closed", "owner_monitor_stopped"):
            self.assertIs(summary["cleanup"][name], True)
        self.assertEqual(summary["cleanup_errors"], [])

    def test_helper_timeout_first_allows_sigint_cleanup_without_forced_kill(self):
        expired = subprocess.TimeoutExpired("synthetic-fixture", .01)
        process = mock.Mock(pid=12345)
        process.communicate.side_effect = [expired, ("", "")]
        with mock.patch.object(subprocess, "Popen", return_value=process), \
             mock.patch.object(os, "killpg") as force_kill:
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                run_script("--scenario", "stop", timeout=.01)
        self.assertIs(caught.exception, expired)
        process.send_signal.assert_called_once_with(signal.SIGINT)
        self.assertEqual(process.communicate.call_args_list, [mock.call(timeout=.01), mock.call(timeout=8)])
        force_kill.assert_not_called()

    def test_helper_only_forces_kill_after_graceful_cleanup_deadline(self):
        expired = subprocess.TimeoutExpired("synthetic-fixture", .01)
        process = mock.Mock(pid=12345)
        process.communicate.side_effect = [expired, subprocess.TimeoutExpired("synthetic-fixture", 8), ("", "")]
        with mock.patch.object(subprocess, "Popen", return_value=process), \
             mock.patch.object(os, "killpg") as force_kill:
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                run_script("--scenario", "stop", timeout=.01)
        self.assertIs(caught.exception, expired)
        process.send_signal.assert_called_once_with(signal.SIGINT)
        self.assertEqual(process.communicate.call_args_list,
                         [mock.call(timeout=.01), mock.call(timeout=8), mock.call(timeout=5)])
        force_kill.assert_called_once_with(process.pid, signal.SIGKILL)

    @unittest.skipIf(sys.version_info < (3, 11), "Standalone fixture requires Python 3.11+")
    def test_keyboard_interrupt_with_active_real_worker_reaps_children_and_removes_workspace(self):
        """SIGINT's KeyboardInterrupt path must execute the real fixture finally."""
        original_popen = subprocess.Popen
        original_temporary = tempfile.TemporaryDirectory
        children, workers, workspaces = [], [], []
        interrupted = []
        before = set(threading.enumerate())

        def popen(argv, *args, **kwargs):
            process = original_popen(argv, *args, **kwargs)
            children.append(process)
            if "--fixture-worker" in argv:
                workers.append(process)
            elif "--fixture-cli" in argv:
                real_communicate = process.communicate

                def communicate(*args, **kwargs):
                    output = real_communicate(*args, **kwargs)
                    # An accepted Start response follows registration of the
                    # real worker. This checkpoint does not depend on whether
                    # a fast loop happened to need a polling sleep.
                    if not interrupted and any(worker.poll() is None for worker in workers):
                        self.assertIs(threading.current_thread(), threading.main_thread())
                        self.assertEqual(json.loads(output[0])["status"], "accepted")
                        interrupted.append(True)
                        raise KeyboardInterrupt("synthetic fixture deadline")
                    return output

                process.communicate = communicate
            return process

        def temporary(*args, **kwargs):
            directory = original_temporary(*args, **kwargs)
            workspaces.append(Path(directory.name))
            self.addCleanup(directory.cleanup)
            return directory

        def fallback_cleanup():
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)

        self.addCleanup(fallback_cleanup)
        with mock.patch.object(subprocess, "Popen", side_effect=popen), \
             mock.patch.object(tempfile, "TemporaryDirectory", side_effect=temporary):
            try:
                returned = fixture.run_fixture(scenario="stop", timeout_seconds=30)
            except KeyboardInterrupt as error:
                self.assertEqual(str(error), "synthetic fixture deadline")
            else:
                self.fail("Fixture returned without reaching active-worker interrupt: " + json.dumps(returned))
        self.assertEqual(interrupted, [True], "Interrupt only after an actual worker launch")
        self.assertEqual(len(workers), 1)
        self.assertTrue(all(child.poll() is not None for child in children))
        self.assertTrue(workspaces)
        self.assertTrue(all(not workspace.exists() for workspace in workspaces))
        leaked = [thread.name for thread in threading.enumerate() if thread not in before
                  and thread.name in {"fixture-http", "voice-control", "voice-result"}]
        self.assertEqual(leaked, [])

    def test_invalid_direct_arguments_fail_before_workspace_socket_or_process_creation(self):
        cases = [{"scenario": "unknown"}, {"timeout_seconds": 0}, {"timeout_seconds": 9},
                 {"timeout_seconds": 121}, {"timeout_seconds": math.nan},
                 {"timeout_seconds": math.inf}, {"timeout_seconds": True}]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), \
                 mock.patch.object(fixture.tempfile, "TemporaryDirectory", side_effect=AssertionError("workspace created")), \
                 mock.patch.object(fixture.socket, "socket", side_effect=AssertionError("socket opened")), \
                 mock.patch.object(fixture.subprocess, "Popen", side_effect=AssertionError("process opened")):
                with self.assertRaises(ValueError):
                    fixture.run_fixture(**kwargs)

    def test_import_does_not_open_network_start_processes_or_create_workspace(self):
        code = (
            "import runpy,socket,subprocess,tempfile; from unittest.mock import patch; "
            "failure=AssertionError('import side effect'); "
            "a=patch('socket.socket',side_effect=failure); a.start(); "
            "b=patch('subprocess.Popen',side_effect=failure); b.start(); "
            "c=patch('tempfile.TemporaryDirectory',side_effect=failure); c.start(); "
            f"runpy.run_path({str(SCRIPT)!r},run_name='offline_import_test')"
        )
        result = subprocess.run([sys.executable, "-B", "-c", code],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_network_guard_denies_connect_dns_and_datagrams_before_os_io(self):
        original = (fixture.socket.socket.connect, fixture.socket.socket.connect_ex,
                    fixture.socket.socket.sendto, fixture.socket.getaddrinfo)
        with self.assertRaisesRegex(RuntimeError, "synthetic context failure"):
            with fixture.network_guard() as counters:
                for operation in (
                    lambda: fixture.socket.socket.connect(object(), ("192.0.2.2", 443)),
                    lambda: fixture.socket.socket.connect_ex(object(), ("192.0.2.2", 443)),
                    lambda: fixture.socket.socket.sendto(object(), b"synthetic", ("192.0.2.2", 443)),
                    lambda: fixture.socket.getaddrinfo("example.invalid", 443),
                ):
                    with self.assertRaises(OSError):
                        operation()
                self.assertEqual(counters["external_attempts"], 4)
                self.assertEqual(counters["mapped_connections"], 0)
                raise RuntimeError("synthetic context failure")
        self.assertEqual((fixture.socket.socket.connect, fixture.socket.socket.connect_ex,
                          fixture.socket.socket.sendto, fixture.socket.getaddrinfo), original)

    def test_network_guard_only_maps_exact_synthetic_cli_destination(self):
        fake_socket = object()
        with mock.patch.object(fixture.socket.socket, "connect", return_value=None) as connect, \
             mock.patch.object(fixture.socket, "getaddrinfo", return_value=["synthetic-address"]) as dns:
            with fixture.network_guard(12345) as counters:
                fixture.socket.socket.connect(fake_socket, (fixture.SYNTHETIC_HOST, 12345))
                connect.assert_called_once_with(fake_socket, ("127.0.0.1", 12345))
                addresses = fixture.socket.getaddrinfo(fixture.SYNTHETIC_HOST, 12345)
                self.assertEqual(addresses, ["synthetic-address"])
                dns.assert_called_once_with(fixture.SYNTHETIC_HOST, 12345)
                for address in ((fixture.SYNTHETIC_HOST, 12346), ("127.0.0.1", 12345), ("192.0.2.2", 12345)):
                    with self.assertRaises(OSError):
                        fixture.socket.socket.connect(fake_socket, address)
                self.assertEqual(counters["mapped_connections"], 1)
                self.assertEqual(counters["external_attempts"], 3)
            self.assertEqual(connect.call_count, 1)

    @unittest.skipIf(sys.version_info < (3, 11), "Standalone fixture requires Python 3.11+")
    def test_cleanup_exception_does_not_skip_worker_listener_or_workspace_cleanup(self):
        original_load = fixture.load_module
        marker = "synthetic-private-cleanup-detail"

        def load(name, path):
            module = original_load(name, path)
            if name == "_voice_integration_gateway":
                original_gateway = module.Gateway

                class GatewayWithCleanupFailure(original_gateway):
                    def attach_voice(self, *args, **kwargs):
                        result = super().attach_voice(*args, **kwargs)
                        real_close = self.voice.close

                        def close_then_fail():
                            real_close()
                            raise RuntimeError(marker)

                        self.voice.close = close_then_fail
                        return result

                module.Gateway = GatewayWithCleanupFailure
            return module

        with mock.patch.object(fixture, "load_module", side_effect=load):
            summary = fixture.run_fixture(scenario="complete", timeout_seconds=30)
        self.assertFalse(summary["success"])
        self.assertEqual(summary["cleanup_errors"], ["owner_close_failed"])
        self.assertEqual(summary["launches"], 1)
        self.assertEqual(summary["runs"][0]["runner_reason"], "turn_complete")
        self.assertTrue(all(summary["cleanup"].values()), summary)
        self.assertNotIn(marker, json.dumps(summary))


if __name__ == "__main__":
    unittest.main()
