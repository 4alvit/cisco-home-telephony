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
    media_objects, observers, sockets = [], [], []

    class Observer:
        def __init__(self, *_):
            self.done, self.ready = threading.Event(), threading.Event()
            self.thread = threading.Thread(target=self.run, name="fixture-observer", daemon=True)
            observers.append(self)

        def run(self):
            self.ready.set()
            self.done.wait(10)

        def status(self):
            return "idle"

    class Media:
        def __init__(self, bind_ip, bind_port, phone_ip, phone_port, *_):
            self.bind_ip, self.bind_port, self.phone_ip, self.phone_port = bind_ip, bind_port, phone_ip, phone_port
            self.receiver = SimpleNamespace(counters={"received": 0})
            self.closed = False
            self.next_input = self.next_output = 0.0
            media_objects.append(self)

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

    class Watchdog:
        def __init__(self, _phone, starts):
            self.starts, self.owned, self.armed = set(starts), set(), False

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

    class Peer(SimulatedHASocket):
        async def send(self, message):
            if scenario == "provider-failure" and isinstance(message, bytes) and len(message) == 1:
                self._event("error", {"code": "synthetic_provider_failure"})
                return
            await super().send(message)

    def build_service(config, workspace, sink, *, on_event, **kwargs):
        async def connect(_url):
            peer = Peer()
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

    runner.transport._GatewayObserver = Observer
    runner.transport.UdpMedia = Media
    runner.transport._Watchdog = Watchdog
    runner.build_service = build_service
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


def run_fixture(*, scenario="all", timeout_seconds=30.0):
    """Run real process/HTTP/lifecycle seams; return a path-free JSON summary."""
    if scenario not in ("all", *SCENARIOS) or type(timeout_seconds) not in (int, float) or not 10 <= timeout_seconds <= 120:
        raise ValueError("invalid_fixture_arguments")
    if sys.version_info < (3, 11):
        raise ValueError("fixture_requires_python_3_11")
    deadline = time.monotonic() + timeout_seconds
    started = time.monotonic()
    gateway_module = load_module("_voice_integration_gateway", ROOT / "containers/asterisk/phone_gateway.py")
    core = gateway_module.app_module("phone_voice_control")
    children, launches, requests, runs, commands = [], [], [], [], []
    checks = {}
    summary = {"success": False, "scenario": scenario, "checks": checks, "runs": runs,
               "cli_commands": commands,
               "boundaries": {"real": ["cisco.main CLI in subprocess", "HTTP framing and bearer/source checks",
                    "Gateway.attach_voice", "VoiceControl locks/journal/config snapshots", "_SubprocessHandle and SIGTERM",
                    "voice_session.main/run_session", "VoiceSession", "PhoneVoiceController", "build_service", "AssistantService",
                    "HAClient protocol", "WavePlayback", "RtpPCMSink"],
                    "simulated": ["CLI exact synthetic-host to loopback socket mapping",
                    "post-validation loopback API source allowlist", "runner executable bootstrap adapter",
                    "AMI/focus observer", "UDP PCM media and physical device commands", "independent physical watchdog",
                    "HA WebSocket peer and TTS fetch"]}, "external_network_attempts": 0}
    temporary = tempfile.TemporaryDirectory(prefix="voice-integration-")
    workspace = Path(temporary.name).resolve()
    workspace.chmod(0o700)
    local = workspace / ".local"
    local.mkdir(mode=0o700)
    evidence_directory = local / "evidence"
    evidence_directory.mkdir(mode=0o700)
    listener = server_thread = gateway = None
    selected_scenario = ["complete"]
    token = secrets.token_hex(24)
    parent_guard = network_guard()
    parent_network = parent_guard.__enter__()

    def remaining():
        value = deadline - time.monotonic()
        require(value > 0, "fixture_deadline")
        return value

    def runner_factory(argv, **kwargs):
        index = len(launches)
        evidence = evidence_directory / (str(index) + ".json")
        bootstrap = [argv[0], str(SCRIPT), "--fixture-worker", selected_scenario[0], str(evidence), "--", *argv[2:]]
        handle = core._SubprocessHandle(bootstrap, **kwargs)
        children.append(handle.process)
        launches.append({"scenario": selected_scenario[0], "evidence": evidence, "handle": handle})
        return handle

    def cli(line, action, *options, bad_token=False):
        argv = [sys.executable, str(SCRIPT), "--fixture-cli", str(listener.server_port), "--",
                "--config", str(local / ("bad-client.json" if bad_token else "client.json")),
                "--json", "--timeout", "2", line, "voice", action, *options]
        env = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
               "LANG": "C.UTF-8"}
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=workspace)
        children.append(process)
        try:
            stdout, _stderr = process.communicate(timeout=min(4, remaining()))
        except BaseException:
            process.kill()
            process.communicate(timeout=2)
            raise
        require(len(stdout) <= 64 * 1024, "cli_output_excessive")
        value = json.loads(stdout)
        network = json.loads(_stderr)["fixture_network"]
        summary["external_network_attempts"] += network["external_attempts"]
        commands.append({"line": "primary" if line == "201" else "sibling", "action": action,
                         "exit_code": process.returncode, "status": value.get("status"),
                         "reason": value.get("reason"), "invalid_bearer": bad_token,
                         "flags": [option for option in options if option.startswith("--")]})
        require(token.encode() not in stdout and b"synthetic-integration-token" not in stdout, "public_secret_leak")
        return process.returncode, value

    flags = ("--attended", "--exclusive-media", "--enable-microphone")
    try:
        config = {"persistent_enabled": True, "bind_ipv4": SYNTHETIC_HOST, "api_port": 18084,
            "api_token": token, "api_clients": ["192.168.50.11"],
            "ami": {"host": SYNTHETIC_HOST, "port": 5038, "username": "synthetic", "secret": "synthetic-ami"},
            "dial_allowlist": [], "ring_allowlist": [],
            "phones": [{"id": "201", "lines": ["201", "202"], "ipv4": "192.168.50.21",
                        "device": "SEP020000000001", "port": 18080, "auth_path": "/auth/" + "a" * 64,
                        "username": "u" * 24, "password": "p" * 24}]}
        save(local / "gateway.json", config)
        save(local / "transport.json", {"gateway_config": str(local / "gateway.json"), "line": "201",
            "bind_ipv4": SYNTHETIC_HOST, "bind_port": 24576, "phone_rtp_port": 24578,
            "duration_seconds": 6, "receive_idle_seconds": 2, "volume": 20})
        save(local / "voice.json", {"service": {"enabled": True, "rearm_after_turn": False,
            "preroll_ms": 0, "max_utterance_seconds": .12, "audio_idle_timeout_seconds": .1,
            "turn_timeout_seconds": 5}, "phones": {"201": True},
            "ha": {"base_url": "http://127.0.0.1:9", "token_file": ".local/ha-token.txt", "pipeline_id": "synthetic"},
            "wake": {"mode": "manual", "bundle_file": ".local/unused.json"}})
        descriptor = os.open(local / "ha-token.txt", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write("synthetic-integration-token")
        save(local / "control.json", {"version": 1, "enabled": True, "state_directory": ".local/owner",
            "python_executable": sys.executable, "ffmpeg_executable": sys.executable,
            "profiles": [{"transport_config": ".local/transport.json", "voice_config": ".local/voice.json"}]})
        gateway = gateway_module.Gateway(config, local / "gateway-state")
        (local / "gateway-state/gateway.sqlite3").chmod(0o600)
        gateway.config["api_clients"] = ["127.0.0.1"]
        original_owner = core.VoiceControl
        core.VoiceControl = lambda config, **kwargs: original_owner(config, runner_factory=runner_factory, **kwargs)
        try:
            gateway.attach_voice(local / "control.json", workspace)
        finally:
            core.VoiceControl = original_owner

        class Handler(gateway_module.Handler):
            def reply(self, status, data, content_type="application/json"):
                requests.append({"method": self.command, "operation": self.path.rsplit("_", 1)[-1], "status": status})
                return super().reply(status, data, content_type)

        listener = gateway_module.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        listener.gateway, listener.phone, listener.daemon_threads = gateway, None, True
        server_thread = threading.Thread(target=listener.serve_forever, kwargs={"poll_interval": .01}, name="fixture-http", daemon=True)
        server_thread.start()
        client = {"gateway_url": f"http://{SYNTHETIC_HOST}:{listener.server_port}", "token": token, "transport": "direct"}
        save(local / "client.json", client)
        save(local / "bad-client.json", dict(client, token="b" * 48))
        code, value = cli("202", "status")
        checks["initial_idle"] = code == 0 and value["voice"]["state"] == "idle"
        code, value = cli("202", "status", bad_token=True)
        checks["wrong_bearer_denied"] = code != 0 and value.get("reason") == "forbidden"
        gateway.config["api_clients"] = ["127.0.0.2"]
        code, value = cli("202", "status")
        checks["source_denied"] = code != 0 and value.get("reason") == "forbidden"
        gateway.config["api_clients"] = ["127.0.0.1"]
        code, _ = cli("202", "start", "--idempotency-key", "missing-flags")
        checks["missing_flags_no_launch"] = code != 0 and not launches
        code, value = cli("202", "start", "--idempotency-key", "dry-validation", "--dry-run", *flags)
        checks["dry_run_no_launch"] = code == 0 and value["status"] == "dry_run" and not launches

        for item in (SCENARIOS if scenario == "all" else (scenario,)):
            selected_scenario[0] = item
            index = len(launches)
            code, value = cli("202", "start", "--idempotency-key", "fixture-" + item, *flags)
            require(code == 0, "start_not_accepted")
            session_id = value["voice"]["session_id"]
            require(value["voice"]["phone"] == "201" and value["voice"]["line"] == "202", "wrong_owner_identity")
            start_exit = code
            code, sibling = cli("201", "status")
            identity_ok = code == 0 and sibling["voice"]["session_id"] == session_id and sibling["voice"]["phone"] == "201"
            code, repeat = cli("201", "start", "--idempotency-key", "fixture-" + item, *flags)
            repeat_ok = code == 0 and repeat["voice"]["session_id"] == session_id and len(launches) == index + 1
            if item == "stop":
                code, refusal = cli("201", "start", "--idempotency-key", "competing-start", *flags)
                checks["sibling_concurrent_start_denied"] = code != 0 and refusal.get("reason") == "voice_phone_busy"
                while True:
                    remaining()
                    evidence_path = launches[index]["evidence"]
                    if evidence_path.exists() and json.loads(evidence_path.read_text()).get("playback_started"):
                        break
                    require(launches[index]["handle"].poll() is None, "runner_exited_before_playback")
                    time.sleep(.01)
                code, stopped = cli("201", "stop", "--session-id", session_id)
                checks["stop_accepted_via_sibling"] = code == 0 and stopped["voice"]["session_id"] == session_id
            while True:
                remaining()
                code, terminal = cli("201", "status")
                if not terminal["voice"]["active"]:
                    break
                time.sleep(.02)
            handle = launches[index]["handle"]
            handle.process.wait(timeout=min(2, remaining()))
            evidence = json.loads(launches[index]["evidence"].read_text())
            expected = "failed" if item == "provider-failure" else "stopped"
            require(terminal["voice"]["state"] == expected and terminal["voice"]["stop_confirmed"], "unexpected_terminal_state")
            require(evidence["ha_closed"] and evidence["media_closed"] and evidence["observer_stopped"], "worker_resources_not_closed")
            require(evidence["ha_audio_bytes"] > 0 and evidence["input_frames"] > 0, "real_audio_path_not_exercised")
            require(evidence["frames_at_close"] == evidence["output_frames"] and evidence["send_after_close"] == 0, "output_after_close")
            if item == "stop":
                require(0 < evidence["output_frames"] < 150 and evidence["runner_reason"] == "shutdown", "stop_did_not_cancel_playback")
            if item == "complete":
                require(evidence["output_frames"] == 50 and evidence["runner_reason"] == "turn_complete", "turn_not_completed")
            if item == "provider-failure":
                require(evidence["output_frames"] == 0 and "error" in evidence["assistant_states"], "provider_failure_not_observed")
            require(identity_ok and repeat_ok, "sibling_or_idempotency_failure")
            summary["external_network_attempts"] += evidence["external_network_attempts"]
            runs.append({**{key: evidence[key] for key in ("scenario", "input_frames", "output_frames", "assistant_states",
                         "ha_audio_bytes", "runner_exit", "runner_reason", "send_after_close", "ha_closed", "media_closed", "observer_stopped")},
                         "cli_start_exit": start_exit, "terminal_state": expected, "stop_confirmed": True,
                         "sibling_owner_identity": identity_ok, "duplicate_request_one_launch": repeat_ok,
                         "fault_injected": item == "provider-failure"})
        checks["run_snapshots_removed"] = not list((local / "owner").glob("run-*"))
        locks = list((local / "owner").glob("*.lock"))
        journals = list((local / "owner").glob("*.json"))
        require(len(locks) == 1 and len(journals) == 1, "missing_physical_owner_files")
        summary["owner_files"] = {"locks": len(locks), "journals": len(journals)}
        checks["lease_released"] = False
        for path in locks:
            with path.open("r+b") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(stream, fcntl.LOCK_UN)
        checks["lease_released"] = True
        checks["owner_journal_unreserved"] = all(json.loads(path.read_text()).get("reserved") is False for path in journals)
        checks["private_permissions"] = all(not path.stat().st_mode & 0o077 for path in workspace.rglob("*") if path.is_file() or path.is_dir())
        require(all(checks.values()) and summary["external_network_attempts"] == 0, "fixture_check_failed")
        summary["success"] = True
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        summary["error"] = str(error) if isinstance(error, FixtureFailure) else type(error).__name__
    finally:
        cleanup_errors = []

        def cleanup(label, action):
            try:
                action()
            except BaseException:
                cleanup_errors.append(label)

        if gateway and gateway.voice:
            cleanup("owner_close_failed", gateway.voice.close)
        for child in children:
            def reap(child=child):
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=2)
            cleanup("child_reap_failed", reap)
        if gateway and gateway.voice and gateway.voice._thread:
            cleanup("owner_join_failed", lambda: gateway.voice._thread.join(2))
        if listener:
            # shutdown() waits for serve_forever(); it deadlocks if start()
            # failed before that thread could ever run.
            if server_thread and server_thread.is_alive():
                cleanup("listener_shutdown_failed", listener.shutdown)
            cleanup("listener_close_failed", listener.server_close)
        if server_thread and server_thread.ident is not None:
            cleanup("listener_join_failed", lambda: server_thread.join(2))
        if gateway:
            cleanup("database_close_failed", gateway.db.close)
        cleanup("workspace_cleanup_failed", temporary.cleanup)
        summary["external_network_attempts"] += parent_network["external_attempts"]
        cleanup("network_guard_restore_failed", lambda: parent_guard.__exit__(None, None, None))
        summary["cleanup"] = {"workspace_removed": not workspace.exists(),
            "children_reaped": all(child.poll() is not None for child in children),
            "listener_closed": server_thread is None or not server_thread.is_alive(),
            "owner_monitor_stopped": gateway is None or not gateway.voice or not gateway.voice._thread.is_alive(),
            "run_snapshots_removed": checks.get("run_snapshots_removed", False)}
        summary["cleanup_errors"] = cleanup_errors
        summary["success"] = (summary["success"] and not cleanup_errors
                              and summary["external_network_attempts"] == 0 and all(summary["cleanup"].values()))
        summary["launches"] = len(launches)
        summary["http_requests"] = requests
        summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return summary


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
