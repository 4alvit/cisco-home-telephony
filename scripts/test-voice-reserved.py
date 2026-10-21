#!/usr/bin/env python3
"""Offline acceptance of the durable reserved runner and its real child.

The wrapper, journal, flock, fsync, process handle and voice-session command are
production code. Only device/AMI/datagram and HA boundaries are simulated.
Private models are required by the CLI. Explicit injected test detectors never
count as ONNX evidence. No production grace or cleanup behavior is replaced.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import importlib.metadata
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import platform
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import wave
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
sys.path[:0] = [str(ROOT), str(ROOT / "containers/asterisk")]
SCENARIOS = ("complete", "no-wake", "ha-timeout", "slow-ha", "cancel")
FAULTS = (None, "never-wake", "wrong-pipeline", "drop-input")
PHONE, DEVICE, HOST = "201", "192.168.50.21", "192.168.50.10"
PIPELINE_ID = "fixture-pipeline"
CLEAN_FIELDS = ("transport_stopped", "media_closed", "stop_confirmed", "ha_closed",
                "stream_invalidated", "task_cleared", "queue_cleared", "owner_released",
                "assistant_stopped", "device_directions_released", "watchdog_finished",
                "observer_stop_requested", "observer_stopped")


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


pipeline = module("_reserved_pipeline_fixture", ROOT / "scripts/test-voice-pipeline.py")
runner = module("_reserved_child_runner", ROOT / "scripts/voice_session.py")
WRAPPER_PATH = ROOT / "containers/voice-wake/run_reserved.py"
if not WRAPPER_PATH.is_file():
    WRAPPER_PATH = Path("/opt/voice-wake/run_reserved.py")
reserved = module("_reserved_production_runner", WRAPPER_PATH)
require, digest = pipeline.require, pipeline.digest


def save(path, value):
    """Atomic owner-only fixture evidence, separate from production snapshots."""
    data = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True).encode()
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".fixture-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_descriptor(path):
    path = Path(path)
    require(path.is_absolute() and path.name == "fixture.json" and path.parent.name == ".local",
            "invalid_fixture_descriptor")
    data = json.loads(pipeline.read_private(path, path.parent, 16384))
    require(data["scenario"] in SCENARIOS and data["detector_kind"] in ("onnx", "injected_test")
            and data.get("test_fault") in FAULTS, "invalid_fixture_descriptor")
    require(type(data["duration"]) in (int, float) and 0 < data["duration"] <= 30,
            "invalid_fixture_duration")
    return path, data


@contextlib.contextmanager
def private_workspace():
    """Remove successful fixtures; retain failed state until cleanup is known."""
    directory = tempfile.mkdtemp(prefix="voice-reserved-")
    try:
        yield directory
    except BaseException:
        # This is deliberately not a TemporaryDirectory: a failed identity or
        # process cleanup check must not erase the files an owned child uses.
        raise
    else:
        shutil.rmtree(directory)


def sources():
    """Hash actual loaded sources, checking their fixed origins first."""
    import phone_voice_controller
    import stage_models
    import voice_assistant.audio
    import voice_assistant.config
    import voice_assistant.ha
    import voice_assistant.playback
    import voice_assistant.service
    import voice_assistant.wake
    entries = {
        "scripts/test-voice-pipeline.py": (pipeline, ROOT / "scripts/test-voice-pipeline.py"),
        "scripts/voice_session.py": (runner, ROOT / "scripts/voice_session.py"),
        "run_reserved.py": (reserved, WRAPPER_PATH),
        "stage_models.py": (stage_models, WRAPPER_PATH.parent / "stage_models.py"),
        "phone_voice_control.py": (reserved.control, ROOT / "containers/asterisk/phone_voice_control.py"),
        "phone_voice_transport.py": (runner.transport, ROOT / "containers/asterisk/phone_voice_transport.py"),
        "phone_voice_controller.py": (phone_voice_controller, ROOT / "containers/asterisk/phone_voice_controller.py"),
        "reserved_transport.py": (reserved.control._libraries()[0], ROOT / "containers/asterisk/phone_voice_transport.py"),
        "phone_gateway.py": (runner.transport._gateway_module(), ROOT / "containers/asterisk/phone_gateway.py"),
    }
    for name in ("audio", "config", "ha", "playback", "service", "wake"):
        entries["voice_assistant/" + name + ".py"] = (
            sys.modules["voice_assistant." + name], ROOT / "voice_assistant" / (name + ".py"))
    result = {"scripts/test-voice-reserved.py": digest(SCRIPT.read_bytes())}
    for name, (loaded, expected) in entries.items():
        actual = Path(loaded.__file__).resolve(strict=True)
        require(actual == expected.resolve(strict=True)
                and Path(loaded.__spec__.origin).resolve(strict=True) == actual,
                "loaded_module_origin_mismatch")
        result[name] = digest(actual.read_bytes())
    return result


loaded_sources = sources


def state_is_clean(value):
    return isinstance(value, dict) and all(value.get(key) is True for key in CLEAN_FIELDS)


def lock_available(path):
    """Open a distinct file description; never create or unlink the lock."""
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


@contextlib.contextmanager
def network_guard():
    with pipeline.network_guard() as counters, contextlib.ExitStack() as stack:
        def deny(*_args, **_kwargs):
            counters["external_network_attempts"] += 1
            raise pipeline.FixtureFailure("network_forbidden")
        for name in ("bind", "listen"):
            stack.enter_context(mock.patch.object(socket.socket, name, deny))
        yield counters


def child_worker(descriptor, lease_fd, argv):
    """Install external boundaries, then run the exact prepared child command."""
    path, spec = read_descriptor(descriptor)
    local, workspace = path.parent, path.parent.parent
    # Redundant child-side registration narrows the parent-spawn/record crash
    # window. Failure paths retain the workspace rather than guessing no child.
    save(local / "owned-child.json", {"pid": os.getpid(), "descriptor": str(path)})
    fingerprints = sources()
    # This explicit test-boundary environment does not change production config.
    thread_env = {key: "1" for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS",
                                      "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")}
    os.environ.update(thread_env)
    pcm = pipeline.read_private(local / "positive.pcm", local, 320000)
    require(digest(pcm) == spec["pcm_sha256"], "fixture_pcm_mismatch")
    transport_path = Path(argv[argv.index("--transport-config") + 1])
    voice_path = Path(argv[argv.index("--voice-config") + 1])
    require(transport_path.parent == voice_path.parent
            and transport_path.parent.parent == local / "state"
            and transport_path.parent.name.startswith("run-"), "prepared_snapshot_required")
    prepared = json.loads(pipeline.read_private(local / "prepared.json", local, 16384))
    require(digest(json.dumps(argv).encode()) == prepared["native_argv_sha256"],
            "prepared_argv_changed")
    for name, expected in prepared["sha256"].items():
        require(digest(pipeline.read_private(transport_path.parent / name, local, 262144)) == expected,
                "prepared_snapshot_changed")
    prepared_voice = json.loads(pipeline.read_private(voice_path, local, 262144))
    lease = os.fstat(lease_fd)
    lease_inherited = [lease.st_dev, lease.st_ino] == prepared["lock_identity"]
    require(lease_inherited, "lease_descriptor_not_inherited")
    save(local / "child-ready.json", {"pid": os.getpid(), "lease_inherited": lease_inherited})
    # Emergency fixture bound, separate from the tested production cleanup.
    signal.signal(signal.SIGALRM, lambda *_: os._exit(124))
    signal.alarm(45)
    from voice_assistant.ha import HAClient, AudioPayload
    from voice_assistant.wake import FrozenOnnxWakeDetector, _load_bundle
    peers, detectors, states, errors, streams, watchdogs = [], [], [], [], [], []
    owned = {}
    model_runtime = {}
    wire = pipeline.MemoryDatagram(bytes(len(pcm)) if spec["scenario"] == "no-wake" else pcm)
    original_recv, original_send = wire.recvfrom, wire.sendto

    def recv(maximum):
        if spec.get("test_fault") == "drop-input":
            time.sleep(.01)
            raise socket.timeout()
        data, (_host, port) = original_recv(maximum)
        return data, (DEVICE, port)

    def send(data, address):
        require(address == (DEVICE, 24578), "unexpected_output_target")
        return original_send(data, ("192.0.2.21", 24578))

    wire.recvfrom, wire.sendto = recv, send

    class Media(runner.transport.UdpMedia):
        def __init__(self, *args, max_packets):
            require(args == (HOST, 24576, DEVICE, 24578, 24580), "unexpected_media_config")
            require(type(max_packets) is int
                    and max_packets == runner.transport.packet_budget(spec["duration"]),
                    "unexpected_media_packet_budget")
            super().__init__(*args, max_packets=max_packets)
            self.wire = wire
            owned["media"] = self

        def open(self):
            pipeline.MemoryMedia.open(self)

    class Observer:
        def __init__(self, *_args):
            self.ready, self.done = threading.Event(), threading.Event()
            self.thread = threading.Thread(target=self.run, daemon=True)
            owned["observer"] = self

        def run(self):
            self.ready.set()
            self.done.wait(40)

        def status(self):
            return "idle"

    class Watchdog:
        """Fake device boundary; real watchdog descendant survival is excluded."""
        def __init__(self, _phone, starts):
            self.starts, self.directions = starts, set()
            self.armed = self.finished = False
            watchdogs.append(self)

        def arm(self, _deadline):
            self.armed = True

        def execute(self, uri):
            require(self.armed and (uri in self.starts or uri in runner.transport.stop_uris()),
                    "unexpected_device_command")
            if uri.endswith(":Stop"):
                self.directions.discard(uri.split(":")[0])
            else:
                self.directions.add(uri.split(":")[0])

        def finish(self):
            self.finished = True
            return {"watchdog_armed": self.armed, "watchdog_finished": True,
                    "watchdog_stop_errors": len(self.directions),
                    "watchdog_stop_confirmed": not self.directions}

    def expected_pcm():
        if not detectors or not detectors[0].triggers:
            return b""
        first = detectors[0].triggers[0]
        return b"".join(wire.expected_frame(i) for i in range(first, first + pipeline.UPLOAD_FRAMES))

    class Peer(pipeline.StrictHAPeer):
        def event(self, kind, data=None):
            if spec["scenario"] in ("slow-ha", "ha-timeout", "cancel") \
                    and kind not in ("run-start", "stt-start"):
                return
            super().event(kind, data)

        async def send(self, message):
            await super().send(message)
            if isinstance(message, bytes) and len(message) == 1 and self.eos == 1:
                save(local / "ha-ready.json", {"eos": 1, "at": time.monotonic()})

    async def connect(_client, url):
        require(url == "ws://fixture.invalid/api/websocket", "unexpected_ha_target")
        peer = Peer("complete", expected_pcm)
        peers.append(peer)
        return peer

    reply = next((pcm[i:i + 320] for i in range(0, len(pcm), 320)
                  if any(runner.transport.decode_ulaw(runner.transport.encode_ulaw(pcm[i:i + 320])))), None)
    require(reply is not None, "positive_pcm_silent_after_g711")
    output_pcm16k = pipeline.upsample_8k_frame(reply) * pipeline.REPLY_FRAMES
    wav = io.BytesIO()
    with wave.open(wav, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(output_pcm16k)

    async def fetch(_client, url):
        require(url == "http://fixture.invalid/api/tts_proxy/fixture.wav"
                and len(peers) == 1 and peers[0].eos == 1, "unexpected_tts_fetch")
        return AudioPayload(wav.getvalue(), "audio/wav")

    original_detector, original_builder = FrozenOnnxWakeDetector.from_bundle, runner.build_service

    class InjectedDetector:
        def __init__(self):
            self.frames = 0

        def reset(self):
            self.frames = 0

        def feed(self, frame):
            self.frames += 1
            return (spec.get("test_fault") != "never-wake" and self.frames == 4 and any(frame))

    def detector(bundle, *, private_root):
        require(Path(bundle) == local / "models/bundle.json" and Path(private_root) == local,
                "prepared_bundle_path_mismatch")
        if spec["detector_kind"] == "onnx":
            value = original_detector(bundle, private_root=private_root)
            model_runtime.update(numpy=importlib.metadata.version("numpy"),
                                 onnxruntime=importlib.metadata.version("onnxruntime"),
                                 providers={key: value.get_providers() for key, value in value._sessions.items()})
        else:
            _load_bundle(bundle, private_root)
            value = InjectedDetector()
        value = pipeline.RecordingDetector(value)
        detectors.append(value)
        return value

    def builder(config, root, sink, *, on_event=None, **kwargs):
        require(root == workspace, "prepared_workspace_mismatch")
        owned["prepared_config_used"] = config == prepared_voice
        owned["session"] = sink.session
        wire.session_active = lambda: sink.session.state == "active"

        def event(value):
            states.append(value.state)
            if value.state == "error":
                errors.append(value.reason)
            if owned.get("service"):
                stream = owned["service"]._sessions[PHONE].stream_id
                if stream and stream not in streams:
                    streams.append(stream)
            if on_event:
                on_event(value)

        service = original_builder(config, root, sink, on_event=event, **kwargs)
        owned["service"] = service
        return service

    def snapshot():
        session, service, media = (owned.get(k) for k in ("session", "service", "media"))
        item = service._sessions.get(PHONE) if service else None
        return {
            "transport_stopped": bool(session and session.state == "stopped"),
            "media_closed": bool(media and media.sock is None and wire.closed),
            "stop_confirmed": bool(session and session.stop_confirmed),
            "ha_closed": all(peer.closed for peer in peers),
            "stream_invalidated": bool(item and item.stream_id is None),
            "task_cleared": bool(item and item.task is None),
            "queue_cleared": bool(item and item.queue is None),
            "owner_released": bool(service and service._owner is None),
            "assistant_stopped": bool(item and item.state == "stopped"),
            "device_directions_released": bool(watchdogs) and all(not w.directions for w in watchdogs),
            "watchdog_finished": bool(watchdogs) and all(w.finished for w in watchdogs),
            "observer_stop_requested": bool(owned.get("observer") and owned["observer"].done.is_set()),
            "observer_stopped": bool(owned.get("observer") and not owned["observer"].thread.is_alive()),
        }

    capture, rescue_errors = io.StringIO(), []
    with network_guard() as network, contextlib.ExitStack() as stack:
        for owner, key, replacement in (
            (runner.transport, "UdpMedia", Media), (runner.transport, "_GatewayObserver", Observer),
            (runner.transport, "_Watchdog", Watchdog), (HAClient, "_default_connect", connect),
            (HAClient, "_fetch_audio_async", fetch), (FrozenOnnxWakeDetector, "from_bundle", detector),
            (runner, "build_service", builder),
        ):
            stack.enter_context(mock.patch.object(owner, key, replacement))
        try:
            with contextlib.redirect_stdout(capture):
                code = runner.main(argv)
            native = json.loads(capture.getvalue())
        finally:
            observer = owned.get("observer")
            # Waiting for an already requested normal shutdown does not cause
            # cleanup; an unset done flag remains a failed primary observation.
            if observer and observer.done.is_set() and observer.thread.ident is not None:
                observer.thread.join(.5)
            before = snapshot()
            service = owned.get("service")
            stale = bool(service and streams) and all(
                service.feed_pcm(PHONE, stream, bytes(640)) is False for stream in streams)
            # Emergency fixture teardown never contributes to primary evidence.
            try:
                if owned.get("session"):
                    owned["session"].stop("fixture_rescue")
            except Exception:
                rescue_errors.append("transport_rescue_failed")
            try:
                if service:
                    asyncio.run(service.close())
            except Exception:
                rescue_errors.append("assistant_rescue_failed")
            if observer:
                observer.done.set()
                if observer.thread.ident is not None:
                    observer.thread.join(.5)
    after = snapshot()
    audio = b"".join(bytes(peer.audio) for peer in peers)
    outputs = b"".join(runner.transport.decode_ulaw(frame) for frame in wire.output_payloads)
    triggers = detectors[0].triggers if detectors else []
    expected_output = runner.transport.encode_ulaw(reply)
    clean = state_is_clean(before) and stale and wire.output_after_stop == 0
    evidence = {
        "entrypoint_exit_code": code, "reason": (native.get("result") or {}).get("reason"),
        "turns_completed": native.get("turns_completed"),
        "prepared_config_used": owned.get("prepared_config_used") is True,
        "prepared_snapshot_sha256": prepared["sha256"],
        "native_argv_sha256": prepared["native_argv_sha256"],
        "model_factory_calls": len(detectors),
        "actual_onnx": spec["detector_kind"] == "onnx" and bool(detectors),
        "detector_kind": spec["detector_kind"], "model_runtime": model_runtime,
        "pid": os.getpid(), "parent_pid": os.getppid(), "lease_inherited": lease_inherited,
        "state_before_rescue": before, "cleanup_success": clean,
        "rescue_cleanup_success": not rescue_errors and state_is_clean(after),
        "stale_stream_rejected": stale, "states": states, "assistant_errors": errors,
        "wake_triggers": len(triggers), "wake_frame_indices": triggers,
        "detector_frames": detectors[0].frames if detectors else 0,
        "minimum_silence_frames": len(pcm) // 320,
        "ha_sessions": len(peers), "ha_audio_bytes": len(audio),
        "ha_audio_sha256": digest(audio), "expected_upload_sha256": digest(expected_pcm()),
        "ha_eos": sum(peer.eos for peer in peers),
        "ha_pipeline": peers[0].command.get("pipeline") if peers and peers[0].command else None,
        "upload_matches_postwake_pcm": bool(peers and any(audio) and audio == expected_pcm()
                                            and sum(peer.eos for peer in peers) == 1),
        "output_packets": len(wire.output_packets), "output_nonzero": bool(any(outputs)),
        "output_matches_tts_pcmu": bool(wire.output_payloads)
        and all(frame == expected_output for frame in wire.output_payloads),
        "output_after_stop": wire.output_after_stop,
        "encoded_input_sha256": digest(wire.encoded), "decoded_input_sha256": digest(wire.decoded),
        "decoded_input_bytes": len(wire.decoded), "decoded_input_nonzero": bool(any(wire.decoded)),
        "output_pcm_sha256": digest(outputs), "tts_pcm16k_sha256": digest(output_pcm16k),
        "external_network_attempts": network["external_network_attempts"],
        "source_sha256": fingerprints, "loaded_sources_unchanged": fingerprints == sources(),
        "thread_environment": thread_env,
    }
    save(local / "child.json", evidence)
    signal.alarm(0)
    # The actual handle must receive only the original production JSON report.
    print(capture.getvalue(), end="", flush=True)
    return code


def wrapper_worker(descriptor):
    path, spec = read_descriptor(descriptor)
    local, workspace = path.parent, path.parent.parent
    fingerprints = sources()
    calls, timestamps, handles, managers, children = {}, {}, [], [], []
    original_popen = subprocess.Popen
    codes = {
        reserved.claim_attempt.__code__: "claim_attempt",
        reserved.drain_cleanup.__code__: "drain_cleanup",
        reserved.ReservedWakeControl._prepare.__code__: "prepare",
        reserved.ReservedWakeControl._finish.__code__: "reserved_finish",
        reserved.control.VoiceControl._finish.__code__: "control_finish",
        reserved.control.VoiceControl._write.__code__: "journal_write",
        reserved.control.VoiceControl._request_stop.__code__: "request_stop",
    }

    def profile(frame, event, arg):
        if event == "call" and frame.f_code in codes:
            key = codes[frame.f_code]
            calls[key] = calls.get(key, 0) + 1
            timestamps.setdefault(key, time.monotonic())
        elif event == "c_call" and arg in (os.fsync, fcntl.flock):
            key = "fsync" if arg is os.fsync else "flock"
            calls[key] = calls.get(key, 0) + 1
        elif event == "return" and frame.f_code is reserved.ReservedWakeControl.__init__.__code__:
            managers.append(frame.f_locals["self"])
        elif event == "return" and frame.f_code is reserved.control._SubprocessHandle.__init__.__code__:
            handles.append(frame.f_locals["self"])

    def launch(argv, **kwargs):
        require(argv[:2] == [sys.executable, str(ROOT / "scripts/voice_session.py")],
                "unexpected_prepared_executable")
        require(kwargs.get("shell") is False and kwargs.get("close_fds") is True
                and kwargs.get("start_new_session") is True and len(kwargs["pass_fds"]) == 1,
                "production_process_contract_changed")
        fd = kwargs["pass_fds"][0]
        lease = os.fstat(fd)
        locks = list((local / "state").glob("*.lock"))
        require(len(locks) == 1, "exact_lock_required")
        lock = locks[0].stat()
        require((lock.st_dev, lock.st_ino) == (lease.st_dev, lease.st_ino),
                "lease_lock_identity_mismatch")
        snapshot = Path(argv[argv.index("--transport-config") + 1]).parent
        require(snapshot.parent == local / "state" and snapshot.name.startswith("run-"),
                "prepared_snapshot_required")
        save(local / "prepared.json", {
            "sha256": {name: digest(pipeline.read_private(snapshot / name, local, 262144))
                       for name in ("transport.json", "voice.json", "gateway.json", "ha-token.txt")},
            "lock_identity": [lease.st_dev, lease.st_ino],
            "native_argv_sha256": digest(json.dumps(argv[2:]).encode()),
        })
        transformed = [argv[0], "-I", "-B", str(SCRIPT), "--child", str(path),
                       "--lease-fd", str(fd), "--", *argv[2:]]
        child = original_popen(transformed, **kwargs)
        children.append(child)
        timestamps["child_spawn"] = time.monotonic()
        try:
            save(local / "owned-child.json", {"pid": child.pid, "descriptor": str(path)})
        except BaseException:
            # The real process already exists even if recording its identity
            # fails. Do not hand an untracked process back to the handle.
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
            raise
        return child

    cancellation_done = threading.Event()

    def cancel():
        while not cancellation_done.wait(.02):
            if (local / "ha-ready.json").exists():
                timestamps["external_cancel"] = time.monotonic()
                os.kill(os.getpid(), signal.SIGTERM)
                return

    argv = ["--workspace", str(workspace), "--state-directory", str(local / "state"),
            "--transport-config", str(local / "transport.json"),
            "--voice-config", str(local / "voice.json"), "--ffmpeg", sys.executable,
            "--attempt-id", "offline-reserved-attempt-001", "--run", "--attended",
            "--exclusive-media", "--enable-microphone"]
    capture = io.StringIO()
    cancel_thread = threading.Thread(target=cancel, daemon=True)
    if spec["scenario"] == "cancel" and not any((local / "state").glob("wake-attempt-*.consumed")):
        cancel_thread.start()
    previous = sys.getprofile()
    started = time.monotonic()
    try:
        with network_guard() as network, mock.patch.object(subprocess, "Popen", launch):
            sys.setprofile(profile)
            with contextlib.redirect_stdout(capture):
                code = reserved.main(argv)
    finally:
        sys.setprofile(previous)
        cancellation_done.set()
        if cancel_thread.ident is not None:
            cancel_thread.join(1)
    native = json.loads(capture.getvalue())
    state = local / "state"
    locks = list(state.glob("*.lock"))
    journals = list(state.glob("*.json"))
    journal = json.loads(journals[0].read_bytes()) if len(journals) == 1 else {}
    value = native.get("voice") or {}
    child_exited = bool(children) and all(child.poll() is not None for child in children)
    # No fixture cleanup has occurred. All of these are production-owned facts.
    evidence = {
        "exit_code": code, "native_success": native.get("success") is True,
        "native_cleanup_success": native.get("cleanup_success") is True,
        "native_turn_success": native.get("turn_success") is True,
        "native_turns_completed": native.get("turns_completed"),
        "native_reason": native.get("reason"), "native_error": native.get("error"),
        "child_launches": len(children), "child_pid": children[0].pid if children else None,
        "wrapper_pid": os.getpid(), "child_exited": child_exited,
        "reader_drained": bool(handles) and all(h._done.is_set() for h in handles),
        "snapshot_removed": not list(state.glob("run-*")),
        "lock_available": len(locks) == 1 and lock_available(locks[0]),
        "lock_retained": len(locks) == 1,
        "journal_reserved": journal.get("reserved"), "journal_state": journal.get("state"),
        "stop_requested": value.get("stop_requested") is True,
        "lease_released": bool(managers) and all(
            s.lease_fd < 0 for manager in managers for s in manager._sessions.values()),
        "consumed_marker_persisted": len(list(state.glob("wake-attempt-*.consumed"))) == 1,
        "profile_calls": calls, "elapsed_seconds": time.monotonic() - started,
        "prepared_snapshot_sha256": json.loads((local / "prepared.json").read_bytes())["sha256"]
        if children else None,
        "native_argv_sha256": json.loads((local / "prepared.json").read_bytes())["native_argv_sha256"]
        if children else None,
        "child_elapsed_seconds": time.monotonic() - timestamps["child_spawn"] if children else None,
        "external_cancel_sent": "external_cancel" in timestamps,
        "stop_after_spawn_seconds": timestamps["request_stop"] - timestamps["child_spawn"]
        if children and "request_stop" in timestamps else None,
        "external_network_attempts": network["external_network_attempts"],
        "source_sha256": fingerprints, "loaded_sources_unchanged": fingerprints == sources(),
    }
    print(json.dumps(evidence, sort_keys=True))
    return 0


def terminate_owned(child_pid, descriptor):
    """Kill only a recorded child whose command still names this exact fixture."""
    try:
        if os.getpgid(child_pid) != child_pid:
            return False
        if Path("/proc").is_dir():
            command = Path("/proc") / str(child_pid) / "cmdline"
            arguments = command.read_bytes().split(b"\0")
            matches = os.fsencode(SCRIPT) in arguments and os.fsencode(descriptor) in arguments
        else:
            result = subprocess.run(["ps", "-p", str(child_pid), "-o", "command="],
                                    capture_output=True, timeout=2)
            text = result.stdout.decode("utf-8", errors="replace")
            matches = str(SCRIPT) in text and str(descriptor) in text
        if not matches:
            return False
        os.killpg(child_pid, signal.SIGKILL)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                # When the coordinator is PID1, an orphan may be adopted here.
                os.waitpid(child_pid, os.WNOHANG)
            except ChildProcessError:
                pass
            try:
                os.killpg(child_pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(.02)
        return False
    except (ProcessLookupError, FileNotFoundError):
        return True


def invoke_wrapper(descriptor, timeout):
    """Bound the real wrapper and explicitly clean its separately grouped child."""
    command = [sys.executable, "-I", "-B", str(SCRIPT), "--wrapper", str(descriptor)]
    blocked = False
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        child = subprocess.Popen(command, stdout=output, stderr=errors, start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            while child.poll() is None:
                local = descriptor.parent
                if (local / "child-ready.json").exists() and not blocked:
                    locks = list((local / "state").glob("*.lock"))
                    blocked = len(locks) == 1 and not lock_available(locks[0])
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, timeout)
                time.sleep(.02)
            output.seek(0)
            raw = output.read(262145)
            require(len(raw) <= 262144 and child.returncode == 0, "wrapper_process_failed")
            value = json.loads(raw)
            require(type(value) is dict, "invalid_wrapper_report")
            require(type(value.get("child_launches")) is int
                    and value["child_launches"] in (0, 1), "invalid_wrapper_report")
            if value["child_launches"]:
                require(all(value.get(key) is True for key in (
                    "child_exited", "reader_drained", "native_cleanup_success",
                    "snapshot_removed", "lock_available"))
                    and value.get("journal_reserved") is False,
                    "unconfirmed_wrapper_cleanup")
            value["concurrent_lock_blocked"] = blocked
            return value
        except BaseException:
            try:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                owned = descriptor.parent / "owned-child.json"
                pending = list((descriptor.parent / "state").glob("run-*"))
                record_deadline = time.monotonic() + 2
                while pending and not owned.exists() and time.monotonic() < record_deadline:
                    time.sleep(.02)
                require(not pending or owned.exists(), "owned_child_identity_unavailable")
                if owned.exists():
                    recorded = json.loads(owned.read_bytes())
                    require(recorded["descriptor"] == str(descriptor), "owned_child_descriptor_mismatch")
                    require(terminate_owned(recorded["pid"], descriptor), "owned_child_cleanup_unconfirmed")
            finally:
                # Descriptor/identity/kill failures must never skip reaping the
                # wrapper process that this coordinator directly owns.
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=2)
            raise


def assess(result):
    """The same verifier is used by execution and explicitly labelled mutation tests."""
    try:
        child, wrapper, replay = result["child"], result["wrapper"], result["replay"]
        observed_clean = state_is_clean(child["state_before_rescue"])
        common = (
            child["cleanup_success"] is True and observed_clean and child["stale_stream_rejected"] is True
            and type(child["turns_completed"]) is int
            and type(wrapper["native_turns_completed"]) is int
            and child["turns_completed"] == wrapper["native_turns_completed"]
            and child["rescue_cleanup_success"] is True and child["output_after_stop"] == 0
            and child["prepared_config_used"] is True and child["model_factory_calls"] == 1
            and child["prepared_snapshot_sha256"] == wrapper["prepared_snapshot_sha256"]
            and child["native_argv_sha256"] == wrapper["native_argv_sha256"]
            and wrapper["native_cleanup_success"] is True and wrapper["child_launches"] == 1
            and all(wrapper[key] is True for key in (
                "child_exited", "reader_drained", "lease_inherited", "concurrent_lock_blocked",
                "snapshot_removed", "lock_available", "lock_retained", "lease_released",
                "consumed_marker_persisted", "loaded_sources_unchanged"))
            and wrapper["journal_reserved"] is False and wrapper["journal_state"] in ("stopped", "failed")
            and wrapper["child_pid"] != wrapper["wrapper_pid"]
            and all(wrapper["profile_calls"].get(key, 0) > 0 for key in
                    ("claim_attempt", "prepare", "reserved_finish", "control_finish",
                     "journal_write", "fsync", "flock", "drain_cleanup"))
            and replay["rejected"] is True and replay["child_launches"] == 0
            and replay["marker_unchanged"] is True and replay["journal_unchanged"] is True
            and replay["profile_calls"].get("claim_attempt") == 1
            and replay["profile_calls"].get("prepare", 0) == 0
            and all(part["source_sha256"] == result["source_sha256"]
                    for part in (child, wrapper, replay))
            and result["external_network_attempts"] == 0 and child["loaded_sources_unchanged"] is True)
        if not common:
            return False
        scenario = result["scenario"]
        audio = (child["wake_triggers"] == 1 and child["ha_sessions"] == 1
                 and child["ha_pipeline"] == PIPELINE_ID and child["ha_eos"] == 1
                 and child["upload_matches_postwake_pcm"] is True and child["ha_audio_bytes"] > 0)
        if scenario == "complete":
            return bool(audio and wrapper["exit_code"] == child["entrypoint_exit_code"] == 0
                        and wrapper["native_success"] is True and wrapper["native_turn_success"] is True
                        and child["turns_completed"] == 1 and child["reason"] == "turn_complete"
                        and child["output_packets"] == pipeline.REPLY_FRAMES and child["output_nonzero"] is True
                        and child["output_matches_tts_pcmu"] is True)
        # The wrapper still reports no completed conversation as exit 1.
        # A clean child timeout/Stop is exit 0; it must not fabricate a turn.
        if (wrapper["native_success"] or wrapper["native_turn_success"]
                or child["turns_completed"] != 0 or child["output_packets"] != 0
                or wrapper["exit_code"] != 1):
            return False
        if scenario == "ha-timeout":
            return bool(audio and child["entrypoint_exit_code"] == 1
                        and child["reason"] == "assistant_error"
                        and "turn_timeout" in child["assistant_errors"]
                        and not wrapper["external_cancel_sent"])
        if child["entrypoint_exit_code"] != 0 or wrapper["journal_state"] != "stopped":
            return False
        if scenario == "no-wake":
            if child["wake_triggers"] or child["ha_sessions"] or child["ha_audio_bytes"]:
                return False
            if child["detector_frames"] < child["minimum_silence_frames"]:
                return False
            if child["decoded_input_bytes"] <= 0 or child["decoded_input_nonzero"] is not False:
                return False
        elif not audio:
            return False
        if scenario == "cancel":
            return bool(wrapper["external_cancel_sent"] and wrapper["stop_requested"]
                        and child["reason"] == "shutdown")
        return bool(wrapper["stop_requested"] and not wrapper["external_cancel_sent"]
                    and wrapper["stop_after_spawn_seconds"] >= result["duration_seconds"] - .15
                    and child["reason"] in ("shutdown", "lease_expired"))
    except (KeyError, TypeError, ValueError):
        return False


def run_scenario(scenario, pcm8k, *, detector_kind="injected_test", fixture=None,
                 duration_seconds=3.0, timeout=20.0, test_fault=None):
    require(scenario in SCENARIOS and test_fault in FAULTS, "invalid_scenario")
    require(type(duration_seconds) in (int, float) and math.isfinite(duration_seconds)
            and 0 < duration_seconds <= 30, "invalid_scenario_duration")
    require(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 120,
            "invalid_worker_timeout")
    require(type(pcm8k) is bytes and 0 < len(pcm8k) <= 320000
            and len(pcm8k) % 320 == 0 and any(pcm8k), "invalid_positive_pcm8k")
    require(detector_kind in ("onnx", "injected_test")
            and (detector_kind != "onnx" or isinstance(fixture, pipeline.Fixture)),
            "actual_onnx_requires_private_fixture")
    require(fixture is None or fixture.pcm8k == pcm8k, "fixture_pcm_mismatch")
    baseline = sources()
    with private_workspace() as directory:
        workspace = Path(directory).resolve()
        workspace.chmod(0o700)
        local = workspace / ".local"
        local.mkdir(mode=0o700)
        for name in ("state", "models"):
            (local / name).mkdir(mode=0o700)
        if fixture is None:
            models = {role: ("injected-" + role).encode() for role in ("mel", "embedding", "classifier")}
            manifest = {"format_version": 1, "frontend": "echomuse-oww-16k-v1", "threshold": .5,
                        "models": {role: {"path": role + ".onnx", "sha256": digest(value)}
                                   for role, value in models.items()}}
        else:
            models, manifest = fixture.models, fixture.manifest
        for role, value in models.items():
            save(local / "models" / (role + ".onnx"), value)
        save(local / "models/bundle.json", manifest)
        save(local / "positive.pcm", pcm8k)
        save(local / "ha-token", b"fixture-token")
        save(local / "gateway.json", {
            "persistent_enabled": True, "bind_ipv4": HOST, "api_port": 18084,
            "api_token": "a" * 64, "api_clients": ["192.168.50.11"],
            "ami": {"host": HOST, "port": 5038, "username": "synthetic", "secret": "synthetic-ami"},
            "dial_allowlist": [], "ring_allowlist": [],
            "phones": [{"id": PHONE, "lines": [PHONE, "202"], "ipv4": DEVICE,
                        "device": "SEP020000000001", "port": 18080, "auth_path": "/auth/" + "a" * 64,
                        "username": "u" * 24, "password": "p" * 24}]})
        save(local / "transport.json", {
            "gateway_config": str(local / "gateway.json"), "line": PHONE, "bind_ipv4": HOST,
            "bind_port": 24576, "phone_rtp_port": 24578, "expected_source_port": 24580,
            "duration_seconds": duration_seconds, "receive_idle_seconds": min(10, duration_seconds + 1),
            "volume": 20})
        save(local / "voice.json", {
            "service": {"enabled": True, "rearm_after_turn": False, "preroll_ms": 0,
                        "max_utterance_seconds": .2, "audio_idle_timeout_seconds": .5,
                        "turn_timeout_seconds": .5 if scenario == "ha-timeout" else duration_seconds + 3},
            "phones": {PHONE: True},
            "ha": {"base_url": "http://fixture.invalid", "token_file": str(local / "ha-token"),
                   "pipeline_id": "wrong-fixture-pipeline" if test_fault == "wrong-pipeline" else PIPELINE_ID},
            "wake": {"mode": "onnx", "bundle_file": str(local / "models/bundle.json")}})
        descriptor = local / "fixture.json"
        save(descriptor, {"scenario": scenario, "detector_kind": detector_kind,
                          "duration": duration_seconds, "test_fault": test_fault,
                          "pcm_sha256": digest(pcm8k)})
        started = time.monotonic()
        wrapper = invoke_wrapper(descriptor, timeout)
        require((local / "child.json").exists(), "child_evidence_missing")
        child = json.loads(pipeline.read_private(local / "child.json", local, 131072))
        wrapper["lease_inherited"] = child["lease_inherited"]
        marker = list((local / "state").glob("wake-attempt-*.consumed"))
        require(len(marker) == 1, "attempt_marker_missing")
        identity = (marker[0].stat().st_ino, digest(marker[0].read_bytes()))
        journals = list((local / "state").glob("*.json"))
        require(len(journals) == 1, "journal_missing")
        journal_before = (journals[0].stat().st_ino, digest(journals[0].read_bytes()))
        replay = invoke_wrapper(descriptor, max(.1, timeout - (time.monotonic() - started)))
        replay["marker_unchanged"] = identity == (marker[0].stat().st_ino, digest(marker[0].read_bytes()))
        replay["journal_unchanged"] = journal_before == (journals[0].stat().st_ino, digest(journals[0].read_bytes()))
        replay["rejected"] = (replay["exit_code"] == 2 and replay["child_launches"] == 0
                              and replay["native_error"] == "wake_reservation_or_runtime_failed")
        result = {
            "scenario": scenario, "detector_kind": detector_kind, "actual_onnx": child["actual_onnx"],
            "duration_seconds": duration_seconds, "child": child, "wrapper": wrapper, "replay": replay,
            "source_sha256": baseline,
            "external_network_attempts": child["external_network_attempts"]
            + wrapper["external_network_attempts"] + replay["external_network_attempts"],
            "turn_success": wrapper["native_turn_success"],
            "cleanup_success": bool(child["cleanup_success"] and wrapper["native_cleanup_success"]),
            "physical_io": False, "live_ha": False,
            "runtime": {"python": platform.python_version(), "platform": platform.system(),
                        "machine": platform.machine(), "uid": os.getuid()},
            "elapsed_seconds": time.monotonic() - started,
        }
        result["assertion_success"] = assess(result)
        require(sources() == baseline, "coordinator_source_changed")
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wrapper", help=argparse.SUPPRESS)
    parser.add_argument("--child", help=argparse.SUPPRESS)
    parser.add_argument("--lease-fd", type=int, help=argparse.SUPPRESS)
    for name in ("private-root", "bundle", "positive-wav"):
        parser.add_argument("--" + name)
    parser.add_argument("--scenario", choices=("all", *SCENARIOS), default="all")
    parser.add_argument("--duration-seconds", type=float, default=8)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("native", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    stage = "arguments"
    try:
        if args.child:
            stage = "child"
            return child_worker(args.child, args.lease_fd, args.native[1:])
        if args.wrapper:
            stage = "wrapper"
            return wrapper_worker(args.wrapper)
        require(all((args.private_root, args.bundle, args.positive_wav)),
                "explicit_private_fixture_arguments_required")
        require(math.isfinite(args.duration_seconds) and 0 < args.duration_seconds <= 30,
                "invalid_scenario_duration")
        require(math.isfinite(args.timeout) and 0 < args.timeout <= 120, "invalid_worker_timeout")
        stage = "fixture"
        baseline = sources()
        fixture = pipeline.load_fixture(args.private_root, args.bundle, args.positive_wav)
        scenarios = SCENARIOS if args.scenario == "all" else (args.scenario,)
        deadline, results = time.monotonic() + args.timeout, []
        for scenario in scenarios:
            stage = scenario
            remaining = deadline - time.monotonic()
            require(remaining > 0, "suite_deadline")
            results.append(run_scenario(scenario, fixture.pcm8k, fixture=fixture, detector_kind="onnx",
                                        duration_seconds=args.duration_seconds, timeout=remaining))
        require(sources() == baseline and all(item["source_sha256"] == baseline for item in results),
                "suite_source_changed")
        report = {"assertion_success": all(item["assertion_success"] for item in results),
                  "actual_onnx": all(item["actual_onnx"] for item in results),
                  "runs": results, "fixture": fixture.provenance, "source_sha256": baseline,
                  "physical_io": False, "live_ha": False}
        print(json.dumps(report, sort_keys=True))
        return 0 if report["assertion_success"] else 1
    except Exception as error:
        kind = ("os_error" if isinstance(error, OSError) else
                "timeout" if isinstance(error, subprocess.TimeoutExpired) else "fixture_error")
        print(json.dumps({"assertion_success": False, "error_stage": stage, "error_kind": kind,
                          "errno": error.errno if isinstance(error, OSError) else None,
                          "error": str(error) if isinstance(error, pipeline.FixtureFailure) else "reserved_fixture_failed",
                          "physical_io": False, "live_ha": False}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
