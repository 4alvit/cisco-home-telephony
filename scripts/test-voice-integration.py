#!/usr/bin/env python3
"""Hardware-free CLI/gateway/voice-runner integration fixture (Python 3.11+).

Runs only synthetic, bounded scenarios in a disposable private workspace.
Real HTTP is loopback-only. Physical media, AMI, watchdog and HA are simulated.
No configuration, endpoint, credential or audio input is accepted from users.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import wave

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
SYNTHETIC_HOST = "192.168.50.10"
SCENARIOS = ("complete", "stop", "provider-failure")


class FixtureFailure(RuntimeError):
    """Only fixed fixture assertions may appear in the public error field."""


def require(condition, message):
    if not condition:
        raise FixtureFailure(message)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def save(path, value):
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream)
    os.replace(temporary, path)


@contextlib.contextmanager
def network_guard(mapping=None):
    """Allow one exact synthetic CLI target, mapped to a loopback listener."""
    original_connect, original_dns = socket.socket.connect, socket.getaddrinfo
    original_connect_ex, original_sendto = socket.socket.connect_ex, socket.socket.sendto
    counters = {"mapped_connections": 0, "external_attempts": 0}

    def connect(sock, address):
        if mapping and address == (SYNTHETIC_HOST, mapping):
            counters["mapped_connections"] += 1
            return original_connect(sock, ("127.0.0.1", mapping))
        counters["external_attempts"] += 1
        raise OSError("fixture_network_denied")

    def dns(host, port, *args, **kwargs):
        if mapping and host == SYNTHETIC_HOST and int(port) == mapping:
            return original_dns(host, port, *args, **kwargs)
        counters["external_attempts"] += 1
        raise OSError("fixture_dns_denied")

    def deny_datagram(*_args, **_kwargs):
        counters["external_attempts"] += 1
        raise OSError("fixture_datagram_denied")

    socket.socket.connect, socket.getaddrinfo = connect, dns
    socket.socket.connect_ex, socket.socket.sendto = connect, deny_datagram
    try:
        yield counters
    finally:
        socket.socket.connect, socket.getaddrinfo = original_connect, original_dns
        socket.socket.connect_ex, socket.socket.sendto = original_connect_ex, original_sendto


def cli_bootstrap(port, argv):
    sys.path.insert(0, str(ROOT / "scripts"))
    import cisco
    with network_guard(port) as counters:
        code = cisco.main(argv)
    print(json.dumps({"fixture_network": counters}), file=sys.stderr)
    return code


def fixture_worker(scenario, evidence_path, argv):
    """Run the production session entry point with physical boundaries replaced."""
    sys.path.insert(0, str(ROOT))
    runner = load_module("_voice_integration_session", ROOT / "scripts/voice_session.py")
    from voice_assistant.ha import AudioPayload
    from voice_assistant.simulation import SimulatedHASocket
    production_build_service = runner.build_service

    evidence = {"scenario": scenario, "playback_started": False, "output_frames": 0,
                "frames_at_close": None, "send_after_close": 0, "ha_audio_bytes": 0,
                "ha_closed": False, "media_closed": False, "observer_stopped": False,
                "assistant_class": "AssistantService", "assistant_states": [], "physical_io": False}
    observers = []
    sockets = []

    class SyntheticObserver:
        def __init__(self, *_):
            self.done = threading.Event()
            self.ready = threading.Event()
            self.thread = threading.Thread(target=self.run, name="fixture-observer", daemon=True)
            observers.append(self)

        def run(self):
            self.ready.set()
            self.done.wait(10)

        def status(self):
            return "idle"

    class SyntheticMedia:
        def __init__(self, bind_ip, bind_port, phone_ip, phone_port, *_):
            self.bind_ip = bind_ip
            self.bind_port = bind_port
            self.phone_ip = phone_ip
            self.phone_port = phone_port
            self.receiver = SimpleNamespace(counters={"received": 0})
            self.closed = False
            self.next_input = 0.0
            self.next_output = 0.0

        def open(self):
            self.closed = False

        def receive(self):
            time.sleep(.003)
            if self.closed or time.monotonic() < self.next_input:
                return []
            self.next_input = time.monotonic() + .02
            self.receiver.counters["received"] += 1
            return [bytes(320)]

        def send_pcm(self, frame):
            require(len(frame) == 320, "invalid_synthetic_output_frame")
            if self.closed:
                evidence["send_after_close"] += 1
                return False
            if time.monotonic() < self.next_output:
                return False
            self.next_output = time.monotonic() + .02
            evidence["output_frames"] += 1
            if not evidence["playback_started"]:
                evidence["playback_started"] = True
                save(evidence_path, evidence)
            return True

        def close(self):
            if not self.closed:
                evidence["frames_at_close"] = evidence["output_frames"]
            self.closed = True
            evidence["media_closed"] = True

    class SyntheticWatchdog:
        def __init__(self, _phone, starts):
            self.starts = set(starts)
            self.owned = set()
            self.armed = False

        def arm(self, _deadline):
            self.armed = True

        def execute(self, uri):
            require(self.armed, "fixture_watchdog_not_armed")
            if uri.endswith(":Stop"):
                self.owned.discard(uri.split(":", 1)[0])
            else:
                require(uri in self.starts, "unexpected_device_command")
                self.owned.add(uri.split(":", 1)[0])

        def finish(self):
            return {"watchdog_armed": self.armed, "watchdog_finished": True,
                    "watchdog_stop_errors": len(self.owned), "watchdog_stop_confirmed": not self.owned}

    class SyntheticHAPeer(SimulatedHASocket):
        async def send(self, message):
            if scenario == "provider-failure" and isinstance(message, bytes) and len(message) == 1:
                self._event("error", {"code": "synthetic_provider_failure"})
                return
            await super().send(message)

    def build_service_with_synthetic_ha(config, workspace, sink, *, on_event, **kwargs):
        async def connect(_url):
            peer = SyntheticHAPeer()
            sockets.append(peer)
            return peer

        async def fetch(_url):
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(bytes(16000 * 2 * (3 if scenario == "stop" else 1)))
            return AudioPayload(buffer.getvalue(), "audio/wav")

        def event(value):
            evidence["assistant_states"].append(value.state)
            on_event(value)

        service = production_build_service(config, workspace, sink, on_event=event, connect=connect, **kwargs)
        service.playback.fetch_audio = fetch
        return service

    runner.transport._GatewayObserver = SyntheticObserver
    runner.transport.UdpMedia = SyntheticMedia
    runner.transport._Watchdog = SyntheticWatchdog
    runner.build_service = build_service_with_synthetic_ha
    capture = io.StringIO()
    with network_guard() as guard:
        with contextlib.redirect_stdout(capture):
            code = runner.main(argv)
    for observer in observers:
        observer.done.set()
        observer.thread.join(1)
    evidence.update(observer_stopped=all(not item.thread.is_alive() for item in observers),
                    ha_audio_bytes=sum(peer.audio_bytes for peer in sockets),
                    ha_closed=bool(sockets) and all(peer.closed for peer in sockets),
                    external_network_attempts=guard["external_attempts"], runner_exit=code)
    raw = capture.getvalue().strip()
    report = json.loads(raw) if raw else {"error": "missing_runner_report"}
    evidence["input_frames"] = report.get("result", {}).get("input_frames", 0) if report.get("result") else 0
    evidence["runner_reason"] = (report.get("result") or {}).get("reason")
    save(evidence_path, evidence)
    print(raw, flush=True)
    return code


class FixtureCoordinator:
    """Own one disposable run and keep production seams separate from setup.

    Gateway/VoiceControl own session behavior. This coordinator supplies only
    synthetic inputs, starts the real entry points, and checks their evidence.
    Its resources stay explicit so cleanup can run after any stage fails.
    """

    START_FLAGS = ("--attended", "--exclusive-media", "--enable-microphone")

    def __init__(self, scenario, timeout_seconds):
        self.scenario = scenario
        self.deadline = time.monotonic() + timeout_seconds
        self.started = time.monotonic()
        self.gateway_module = load_module(
            "_voice_integration_gateway", ROOT / "containers/asterisk/phone_gateway.py")
        self.core = self.gateway_module.app_module("phone_voice_control")
        self.children = []
        self.launches = []
        self.requests = []
        self.runs = []
        self.commands = []
        self.checks = {}
        self.summary = self._new_summary()

        self.temporary = tempfile.TemporaryDirectory(prefix="voice-integration-")
        self.workspace = Path(self.temporary.name).resolve()
        self.workspace.chmod(0o700)
        self.local = self.workspace / ".local"
        self.local.mkdir(mode=0o700)
        self.evidence_directory = self.local / "evidence"
        self.evidence_directory.mkdir(mode=0o700)
        self.listener = None
        self.server_thread = None
        self.gateway = None
        self.current_scenario = "complete"
        self.token = secrets.token_hex(24)
        self.parent_guard = network_guard()
        self.parent_network = self.parent_guard.__enter__()

    def _new_summary(self):
        return {
            "success": False,
            "scenario": self.scenario,
            "checks": self.checks,
            "runs": self.runs,
            "cli_commands": self.commands,
            "boundaries": {
                "real": [
                    "cisco.main CLI in subprocess",
                    "HTTP framing and bearer/source checks",
                    "Gateway.attach_voice",
                    "VoiceControl locks/journal/config snapshots",
                    "_SubprocessHandle and SIGTERM",
                    "voice_session.main/run_session",
                    "VoiceSession",
                    "PhoneVoiceController",
                    "build_service",
                    "AssistantService",
                    "HAClient protocol",
                    "WavePlayback",
                    "RtpPCMSink",
                ],
                "simulated": [
                    "CLI exact synthetic-host to loopback socket mapping",
                    "post-validation loopback API source allowlist",
                    "runner executable bootstrap adapter",
                    "AMI/focus observer",
                    "UDP PCM media and physical device commands",
                    "independent physical watchdog",
                    "HA WebSocket peer and TTS fetch",
                ],
            },
            "external_network_attempts": 0,
        }

    def remaining(self):
        value = self.deadline - time.monotonic()
        require(value > 0, "fixture_deadline")
        return value

    def setup_gateway(self):
        """Build the real gateway/owner around private synthetic configuration."""
        config = self._write_synthetic_configs()
        self.gateway = self.gateway_module.Gateway(config, self.local / "gateway-state")
        (self.local / "gateway-state/gateway.sqlite3").chmod(0o600)
        # Production validation requires RFC1918 addresses. Only this fixture's
        # already-validated source list changes to its actual loopback peer.
        self.gateway.config["api_clients"] = ["127.0.0.1"]
        self._attach_voice_owner()
        self._start_http_listener()
        self._write_client_configs()

    def _write_synthetic_configs(self):
        config = {
            "persistent_enabled": True,
            "bind_ipv4": SYNTHETIC_HOST,
            "api_port": 18084,
            "api_token": self.token,
            "api_clients": ["192.168.50.11"],
            "ami": {"host": SYNTHETIC_HOST, "port": 5038,
                    "username": "synthetic", "secret": "synthetic-ami"},
            "dial_allowlist": [],
            "ring_allowlist": [],
            "phones": [{
                "id": "201", "lines": ["201", "202"], "ipv4": "192.168.50.21",
                "device": "SEP020000000001", "port": 18080,
                "auth_path": "/auth/" + "a" * 64,
                "username": "u" * 24, "password": "p" * 24,
            }],
        }
        save(self.local / "gateway.json", config)
        save(self.local / "transport.json", {
            "gateway_config": str(self.local / "gateway.json"), "line": "201",
            "bind_ipv4": SYNTHETIC_HOST, "bind_port": 24576, "phone_rtp_port": 24578,
            "duration_seconds": 6, "receive_idle_seconds": 2, "volume": 20,
        })
        save(self.local / "voice.json", {
            "service": {"enabled": True, "rearm_after_turn": False, "preroll_ms": 0,
                        "max_utterance_seconds": .12, "audio_idle_timeout_seconds": .1,
                        "turn_timeout_seconds": 5},
            "phones": {"201": True},
            "ha": {"base_url": "http://127.0.0.1:9", "token_file": ".local/ha-token.txt",
                   "pipeline_id": "synthetic"},
            "wake": {"mode": "manual", "bundle_file": ".local/unused.json"},
        })
        descriptor = os.open(self.local / "ha-token.txt", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write("synthetic-integration-token")
        save(self.local / "control.json", {
            "version": 1, "enabled": True, "state_directory": ".local/owner",
            "python_executable": sys.executable, "ffmpeg_executable": sys.executable,
            "profiles": [{"transport_config": ".local/transport.json",
                          "voice_config": ".local/voice.json"}],
        })
        return config

    def _attach_voice_owner(self):
        production_owner = self.core.VoiceControl

        def owner_with_fixture_runner(config, **kwargs):
            return production_owner(config, runner_factory=self._spawn_runner, **kwargs)

        self.core.VoiceControl = owner_with_fixture_runner
        try:
            self.gateway.attach_voice(self.local / "control.json", self.workspace)
        finally:
            self.core.VoiceControl = production_owner

    def _spawn_runner(self, argv, **kwargs):
        """Keep the production process handle; substitute only the worker bootstrap."""
        index = len(self.launches)
        evidence = self.evidence_directory / (str(index) + ".json")
        bootstrap = [argv[0], str(SCRIPT), "--fixture-worker", self.current_scenario,
                     str(evidence), "--", *argv[2:]]
        handle = self.core._SubprocessHandle(bootstrap, **kwargs)
        # Record ownership before returning to VoiceControl's accepted Start.
        self.children.append(handle.process)
        self.launches.append({"scenario": self.current_scenario, "evidence": evidence, "handle": handle})
        return handle

    def _start_http_listener(self):
        request_log = self.requests

        class RecordingHandler(self.gateway_module.Handler):
            def reply(self, status, data, content_type="application/json"):
                request_log.append({"method": self.command,
                                    "operation": self.path.rsplit("_", 1)[-1], "status": status})
                return super().reply(status, data, content_type)

        self.listener = self.gateway_module.ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
        self.listener.gateway = self.gateway
        self.listener.phone = None
        self.listener.daemon_threads = True
        self.server_thread = threading.Thread(
            target=self.listener.serve_forever, kwargs={"poll_interval": .01},
            name="fixture-http", daemon=True)
        self.server_thread.start()

    def _write_client_configs(self):
        client = {
            "gateway_url": f"http://{SYNTHETIC_HOST}:{self.listener.server_port}",
            "token": self.token,
            "transport": "direct",
        }
        save(self.local / "client.json", client)
        save(self.local / "bad-client.json", dict(client, token="b" * 48))

    def cli(self, line, action, *options, bad_token=False):
        """Run the actual CLI parser/HTTP client in a guarded child process."""
        config_name = "bad-client.json" if bad_token else "client.json"
        argv = [sys.executable, str(SCRIPT), "--fixture-cli", str(self.listener.server_port), "--",
                "--config", str(self.local / config_name),
                "--json", "--timeout", "2", line, "voice", action, *options]
        env = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
               "LANG": "C.UTF-8"}
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env, cwd=self.workspace)
        self.children.append(process)
        try:
            stdout, stderr = process.communicate(timeout=min(4, self.remaining()))
        except BaseException:
            process.kill()
            process.communicate(timeout=2)
            raise
        require(len(stdout) <= 64 * 1024, "cli_output_excessive")
        value = json.loads(stdout)
        network = json.loads(stderr)["fixture_network"]
        self.summary["external_network_attempts"] += network["external_attempts"]
        self.commands.append({
            "line": "primary" if line == "201" else "sibling",
            "action": action,
            "exit_code": process.returncode,
            "status": value.get("status"),
            "reason": value.get("reason"),
            "invalid_bearer": bad_token,
            "flags": [option for option in options if option.startswith("--")],
        })
        require(self.token.encode() not in stdout and b"synthetic-integration-token" not in stdout,
                "public_secret_leak")
        return process.returncode, value

    def check_start_guards(self):
        code, value = self.cli("202", "status")
        self.checks["initial_idle"] = code == 0 and value["voice"]["state"] == "idle"
        code, value = self.cli("202", "status", bad_token=True)
        self.checks["wrong_bearer_denied"] = code != 0 and value.get("reason") == "forbidden"
        self.gateway.config["api_clients"] = ["127.0.0.2"]
        code, value = self.cli("202", "status")
        self.checks["source_denied"] = code != 0 and value.get("reason") == "forbidden"
        self.gateway.config["api_clients"] = ["127.0.0.1"]
        code, _ = self.cli("202", "start", "--idempotency-key", "missing-flags")
        self.checks["missing_flags_no_launch"] = code != 0 and not self.launches
        code, value = self.cli("202", "start", "--idempotency-key", "dry-validation",
                               "--dry-run", *self.START_FLAGS)
        self.checks["dry_run_no_launch"] = code == 0 and value["status"] == "dry_run" and not self.launches

    def run_scenario(self, scenario):
        self.current_scenario = scenario
        launch_index = len(self.launches)
        request_id = "fixture-" + scenario
        start_exit, value = self.cli("202", "start", "--idempotency-key", request_id, *self.START_FLAGS)
        require(start_exit == 0, "start_not_accepted")
        session_id = value["voice"]["session_id"]
        require(value["voice"]["phone"] == "201" and value["voice"]["line"] == "202", "wrong_owner_identity")

        code, sibling = self.cli("201", "status")
        identity_ok = (code == 0 and sibling["voice"]["session_id"] == session_id
                       and sibling["voice"]["phone"] == "201")
        code, repeat = self.cli("201", "start", "--idempotency-key", request_id, *self.START_FLAGS)
        repeat_ok = (code == 0 and repeat["voice"]["session_id"] == session_id
                     and len(self.launches) == launch_index + 1)

        launch = self.launches[launch_index]
        if scenario == "stop":
            self._stop_during_playback(launch, session_id)
        terminal = self._wait_for_terminal()
        launch["handle"].process.wait(timeout=min(2, self.remaining()))
        evidence = json.loads(launch["evidence"].read_text())
        expected_state = self._verify_scenario(scenario, terminal, evidence, identity_ok, repeat_ok)
        self._record_scenario(scenario, evidence, start_exit, expected_state, identity_ok, repeat_ok)

    def _stop_during_playback(self, launch, session_id):
        code, refusal = self.cli("201", "start", "--idempotency-key", "competing-start", *self.START_FLAGS)
        self.checks["sibling_concurrent_start_denied"] = code != 0 and refusal.get("reason") == "voice_phone_busy"
        self._wait_for_playback(launch)
        code, stopped = self.cli("201", "stop", "--session-id", session_id)
        self.checks["stop_accepted_via_sibling"] = code == 0 and stopped["voice"]["session_id"] == session_id

    def _wait_for_playback(self, launch):
        while True:
            self.remaining()
            evidence_path = launch["evidence"]
            if evidence_path.exists() and json.loads(evidence_path.read_text()).get("playback_started"):
                return
            require(launch["handle"].poll() is None, "runner_exited_before_playback")
            time.sleep(.01)

    def _wait_for_terminal(self):
        while True:
            self.remaining()
            _, terminal = self.cli("201", "status")
            if not terminal["voice"]["active"]:
                return terminal
            time.sleep(.02)

    @staticmethod
    def _verify_scenario(scenario, terminal, evidence, identity_ok, repeat_ok):
        expected = "failed" if scenario == "provider-failure" else "stopped"
        require(terminal["voice"]["state"] == expected and terminal["voice"]["stop_confirmed"],
                "unexpected_terminal_state")
        require(evidence["ha_closed"] and evidence["media_closed"] and evidence["observer_stopped"],
                "worker_resources_not_closed")
        require(evidence["ha_audio_bytes"] > 0 and evidence["input_frames"] > 0,
                "real_audio_path_not_exercised")
        require(evidence["frames_at_close"] == evidence["output_frames"] and evidence["send_after_close"] == 0,
                "output_after_close")
        if scenario == "stop":
            require(0 < evidence["output_frames"] < 150 and evidence["runner_reason"] == "shutdown",
                    "stop_did_not_cancel_playback")
        if scenario == "complete":
            require(evidence["output_frames"] == 50 and evidence["runner_reason"] == "turn_complete",
                    "turn_not_completed")
        if scenario == "provider-failure":
            require(evidence["output_frames"] == 0 and "error" in evidence["assistant_states"],
                    "provider_failure_not_observed")
        require(identity_ok and repeat_ok, "sibling_or_idempotency_failure")
        return expected

    def _record_scenario(self, scenario, evidence, start_exit, expected_state, identity_ok, repeat_ok):
        self.summary["external_network_attempts"] += evidence["external_network_attempts"]
        row = {}
        for key in ("scenario", "input_frames", "output_frames", "assistant_states", "ha_audio_bytes",
                    "runner_exit", "runner_reason", "send_after_close", "ha_closed", "media_closed", "observer_stopped"):
            row[key] = evidence[key]
        row.update(cli_start_exit=start_exit, terminal_state=expected_state, stop_confirmed=True,
                   sibling_owner_identity=identity_ok, duplicate_request_one_launch=repeat_ok,
                   fault_injected=scenario == "provider-failure")
        self.runs.append(row)

    def verify_owner_cleanup(self):
        owner_directory = self.local / "owner"
        self.checks["run_snapshots_removed"] = not list(owner_directory.glob("run-*"))
        locks = list(owner_directory.glob("*.lock"))
        journals = list(owner_directory.glob("*.json"))
        require(len(locks) == 1 and len(journals) == 1, "missing_physical_owner_files")
        self.summary["owner_files"] = {"locks": len(locks), "journals": len(journals)}
        self.checks["lease_released"] = False
        for path in locks:
            with path.open("r+b") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(stream, fcntl.LOCK_UN)
        self.checks["lease_released"] = True
        self.checks["owner_journal_unreserved"] = all(
            json.loads(path.read_text()).get("reserved") is False for path in journals)
        self.checks["private_permissions"] = all(
            not path.stat().st_mode & 0o077
            for path in self.workspace.rglob("*") if path.is_file() or path.is_dir())
        require(all(self.checks.values()) and self.summary["external_network_attempts"] == 0,
                "fixture_check_failed")
        self.summary["success"] = True

    @staticmethod
    def _reap_child(child):
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)

    def cleanup(self):
        """Attempt every teardown step, retaining the original bounded order."""
        cleanup_errors = []

        def attempt(label, action):
            try:
                action()
            except BaseException:
                cleanup_errors.append(label)

        if self.gateway and self.gateway.voice:
            attempt("owner_close_failed", self.gateway.voice.close)
        for child in self.children:
            attempt("child_reap_failed", lambda child=child: self._reap_child(child))
        if self.gateway and self.gateway.voice and self.gateway.voice._thread:
            attempt("owner_join_failed", lambda: self.gateway.voice._thread.join(2))
        if self.listener:
            # shutdown() waits for serve_forever(); it deadlocks if start()
            # failed before that thread could ever run.
            if self.server_thread and self.server_thread.is_alive():
                attempt("listener_shutdown_failed", self.listener.shutdown)
            attempt("listener_close_failed", self.listener.server_close)
        if self.server_thread and self.server_thread.ident is not None:
            attempt("listener_join_failed", lambda: self.server_thread.join(2))
        if self.gateway:
            attempt("database_close_failed", self.gateway.db.close)
        attempt("workspace_cleanup_failed", self.temporary.cleanup)
        self.summary["external_network_attempts"] += self.parent_network["external_attempts"]
        attempt("network_guard_restore_failed", lambda: self.parent_guard.__exit__(None, None, None))
        self.summary["cleanup"] = {
            "workspace_removed": not self.workspace.exists(),
            "children_reaped": all(child.poll() is not None for child in self.children),
            "listener_closed": self.server_thread is None or not self.server_thread.is_alive(),
            "owner_monitor_stopped": (self.gateway is None or not self.gateway.voice
                                      or not self.gateway.voice._thread.is_alive()),
            "run_snapshots_removed": self.checks.get("run_snapshots_removed", False),
        }
        self.summary["cleanup_errors"] = cleanup_errors
        self.summary["success"] = (self.summary["success"] and not cleanup_errors
                                   and self.summary["external_network_attempts"] == 0
                                   and all(self.summary["cleanup"].values()))
        self.summary["launches"] = len(self.launches)
        self.summary["http_requests"] = self.requests
        self.summary["elapsed_seconds"] = round(time.monotonic() - self.started, 3)


def run_fixture(*, scenario="all", timeout_seconds=30.0):
    """Run real process/HTTP/lifecycle seams; return a path-free JSON summary."""
    if scenario not in ("all", *SCENARIOS) or type(timeout_seconds) not in (int, float) or not 10 <= timeout_seconds <= 120:
        raise ValueError("invalid_fixture_arguments")
    if sys.version_info < (3, 11):
        raise ValueError("fixture_requires_python_3_11")

    fixture = FixtureCoordinator(scenario, timeout_seconds)
    try:
        fixture.setup_gateway()
        fixture.check_start_guards()
        scenarios = SCENARIOS if scenario == "all" else (scenario,)
        for selected_scenario in scenarios:
            fixture.run_scenario(selected_scenario)
        fixture.verify_owner_cleanup()
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        fixture.summary["error"] = str(error) if isinstance(error, FixtureFailure) else type(error).__name__
    finally:
        fixture.cleanup()
    return fixture.summary


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--fixture-cli":
        require(args[2] == "--", "invalid_cli_bootstrap")
        return cli_bootstrap(int(args[1]), args[3:])
    if args and args[0] == "--fixture-worker":
        require(args[3] == "--" and args[1] in SCENARIOS, "invalid_worker_bootstrap")
        return fixture_worker(args[1], Path(args[2]), args[4:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("all", *SCENARIOS), default="all")
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument("--json", action="store_true", help="JSON is always emitted")
    parsed = parser.parse_args(args)
    try:
        result = run_fixture(scenario=parsed.scenario, timeout_seconds=parsed.timeout_seconds)
    except ValueError as error:
        result = {"success": False, "error": str(error)}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
