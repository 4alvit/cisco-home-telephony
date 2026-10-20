#!/usr/bin/env python3
"""Offline acceptance through the production voice-session command entry point.

Only device/network boundaries are simulated. The real configuration loaders,
builder, wake detector, controller, codec and cleanup run unchanged. An outer
worker deadline also bounds synchronous model loading. No household config,
microphone, speaker, live HA endpoint or downloaded asset is used.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.metadata
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import platform
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
SCENARIOS = ("complete", "no-wake")
FLAGS = ("--run", "--attended", "--exclusive-media", "--enable-microphone")
PHONE = "201"
HOST, DEVICE = "192.168.50.10", "192.168.50.21"


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Fixed adjacent helper and fixed baked production entry point. No user-supplied
# source root or import path is accepted by this executable.
pipeline = _module("_entrypoint_pipeline_fixture", ROOT / "scripts/test-voice-pipeline.py")
runner = _module("_entrypoint_production_runner", ROOT / "scripts/voice_session.py")
from voice_assistant.ha import HAClient, AudioPayload
from voice_assistant.wake import FrozenOnnxWakeDetector, FRONTEND_ID, _load_bundle

require, digest = pipeline.require, pipeline.digest


def _save(path, value):
    data = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True).encode()
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
        output.write(data)


def _fingerprint(value):
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def run_scenario(scenario, pcm8k, *, detector_factory=None, fixture=None,
                 duration_seconds=12.0, config_mutator=None, entrypoint_flags=None):
    """Synchronous main-thread test seam; injected detectors never claim ONNX.

    config_mutator receives synthetic dictionaries before the real JSON loaders.
    entrypoint_flags permits tests of the production command's explicit gates.
    Neither seam is exposed by the executable CLI.
    """
    require(scenario in SCENARIOS, "unknown_scenario")
    require(type(duration_seconds) in (int, float) and math.isfinite(duration_seconds)
            and 0 < duration_seconds <= 30, "invalid_scenario_duration")
    require(type(pcm8k) is bytes and 0 < len(pcm8k) <= 20 * 16000
            and len(pcm8k) % pipeline.FRAME == 0 and any(pcm8k), "invalid_positive_pcm8k")
    require(threading.current_thread() is threading.main_thread(), "main_thread_required")
    require(detector_factory is not None or isinstance(fixture, pipeline.Fixture),
            "actual_onnx_requires_private_fixture")
    require(fixture is None or fixture.pcm8k == pcm8k, "fixture_pcm_mismatch")
    flags = FLAGS if entrypoint_flags is None else tuple(entrypoint_flags)
    require(len(set(flags)) == len(flags) and all(flag in FLAGS for flag in flags),
            "invalid_entrypoint_flags")
    started = time.monotonic()
    evidence = {"build_service_calls": 0, "transport_load_calls": 0, "voice_load_calls": 0,
                "model_factory_calls": 0, "media_opened": 0, "controller_modes": []}
    peers, detectors, streams, observers, watchdogs = [], [], [], [], []
    owned = {}
    model_runtime = {}
    input_pcm = bytes(len(pcm8k)) if scenario == "no-wake" else pcm8k

    class Datagram(pipeline.MemoryDatagram):
        def recvfrom(self, maximum):
            data, (_host, port) = super().recvfrom(maximum)
            return data, (DEVICE, port)

        def sendto(self, data, address):
            require(address == (DEVICE, 24578), "unexpected_output_target")
            return super().sendto(data, ("192.0.2.21", 24578))

    wire = Datagram(input_pcm)
    original_media = runner.transport.UdpMedia

    class Media(original_media):
        def __init__(self, *args):
            require(args == (HOST, 24576, DEVICE, 24578, 24580), "unexpected_media_config")
            super().__init__(*args)
            self.wire = wire
            owned["media"] = self

        def open(self):
            evidence["media_opened"] += 1
            pipeline.MemoryMedia.open(self)

    class Observer:
        def __init__(self, _gateway, _private, phone):
            require(phone["id"] == PHONE and phone["ipv4"] == DEVICE, "unexpected_observer_phone")
            self.ready, self.done = threading.Event(), threading.Event()
            self.thread = threading.Thread(target=self._run, daemon=True)
            observers.append(self)

        def _run(self):
            self.ready.set()
            self.done.wait(35)

        def status(self):
            return "idle"

    class Watchdog:
        """Device acknowledgement boundary, not a test of the real supervisor."""
        def __init__(self, phone, starts):
            require(phone["id"] == PHONE, "unexpected_watchdog_phone")
            self.starts = tuple(starts)
            require(self.starts == (runner.transport.tx_uri(HOST, 24576),
                                    runner.transport.rx_uri(HOST, 24578, 20)),
                    "unexpected_device_start")
            self.owned, self.commands = set(), []
            self.armed = self.finished = False
            watchdogs.append(self)

        def arm(self, deadline):
            require(time.monotonic() < deadline <= time.monotonic() + 31,
                    "invalid_device_lease")
            self.armed = True

        def execute(self, uri):
            require(self.armed and not self.finished, "device_not_armed")
            require(uri in self.starts or uri in runner.transport.stop_uris(),
                    "unexpected_device_command")
            self.commands.append(uri)
            direction = uri.split(":", 1)[0]
            if uri.endswith(":Stop"):
                self.owned.discard(direction)
            else:
                require(direction not in self.owned, "duplicate_device_start")
                self.owned.add(direction)

        def finish(self):
            self.finished = True
            # Do not repair an unclosed session: expose it to the acceptance
            # assertions even if the production report were to claim success.
            return {"watchdog_armed": self.armed, "watchdog_finished": True,
                    "watchdog_stop_errors": len(self.owned),
                    "watchdog_stop_confirmed": not self.owned}

    def expected_pcm():
        if not detectors or not detectors[0].triggers:
            return b""
        first = detectors[0].triggers[0]
        return b"".join(wire.expected_frame(index)
                        for index in range(first, first + pipeline.UPLOAD_FRAMES))

    async def connect(_client, url):
        require(url == "ws://fixture.invalid/api/websocket", "unexpected_ha_target")
        peer = pipeline.StrictHAPeer("complete", expected_pcm)
        peers.append(peer)
        return peer

    reply_frame = next((pcm8k[index:index + pipeline.FRAME]
                        for index in range(0, len(pcm8k), pipeline.FRAME)
                        if any(runner.transport.decode_ulaw(runner.transport.encode_ulaw(
                            pcm8k[index:index + pipeline.FRAME])))), None)
    require(reply_frame is not None, "positive_pcm_silent_after_g711")
    reply_pcm = pipeline.upsample_8k_frame(reply_frame) * pipeline.REPLY_FRAMES
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(reply_pcm)

    async def fetch(_client, target):
        require(target == "http://fixture.invalid/api/tts_proxy/fixture.wav"
                and len(peers) == 1 and peers[0].eos == 1, "unexpected_tts_fetch")
        return AudioPayload(buffer.getvalue(), "audio/wav")

    original_builder = runner.build_service
    original_voice_load = runner.load_config
    original_transport_load = runner.transport.load_pilot_config
    original_detector = FrozenOnnxWakeDetector.from_bundle
    original_run = runner.PhoneVoiceController.run

    def load_voice(path):
        evidence["voice_load_calls"] += 1
        value = original_voice_load(path)
        owned["loaded_voice"] = value
        evidence["loaded_voice_sha256"] = _fingerprint(value)
        return value

    def load_transport(path):
        evidence["transport_load_calls"] += 1
        value = original_transport_load(path)
        owned["loaded_transport"] = value[0]
        evidence["loaded_transport_sha256"] = _fingerprint(value[0])
        return value

    def build(config, workspace, sink, *, on_event=None, **kwargs):
        evidence["build_service_calls"] += 1
        evidence["built_voice_sha256"] = _fingerprint(config)
        evidence["loaded_config_matches_build"] = config is owned.get("loaded_voice")
        owned["session"] = sink.session
        wire.session_active = lambda: sink.session.state == "active"

        def observe(event):
            service = owned.get("service")
            if service:
                stream = service._sessions[PHONE].stream_id
                if stream and stream not in streams:
                    streams.append(stream)
            if on_event:
                on_event(event)

        service = original_builder(config, workspace, sink, on_event=observe, **kwargs)
        owned["service"] = service
        return service

    async def observe_run(controller, *, mode="manual"):
        evidence["controller_modes"].append(mode)
        return await original_run(controller, mode=mode)

    def snapshot():
        session, service, media = (owned.get(key) for key in ("session", "service", "media"))
        owner = service._sessions.get(PHONE) if service else None
        return {
            "transport_stopped": bool(session and session.state == "stopped"),
            "transport_reason": session.reason if session else None,
            "stop_confirmed": bool(session and session.stop_confirmed),
            "media_closed": bool(media and media.sock is None and wire.closed),
            "ha_closed": all(peer.closed for peer in peers),
            "assistant_state": service.status(PHONE).state if owner else None,
            "stream_invalidated": bool(owner and owner.stream_id is None),
            "task_cleared": bool(owner and owner.task is None),
            "queue_cleared": bool(owner and owner.queue is None),
            "owner_released": bool(service and service._owner is None),
            "observer_stop_requested": bool(observers) and all(item.done.is_set() for item in observers),
            "watchdog_finished": bool(watchdogs) and all(item.finished for item in watchdogs),
            "device_directions_released": bool(watchdogs) and all(not item.owned for item in watchdogs),
            "output_after_stop": wire.output_after_stop,
            "output_packets": len(wire.output_packets),
        }

    def clean(state):
        return all(value is True for key, value in state.items()
                   if key not in ("assistant_state", "output_after_stop", "output_packets", "transport_reason")) \
            and state["assistant_state"] == "stopped" and state["output_after_stop"] == 0

    captured, native, failure = io.StringIO(), {}, None
    exit_code = None
    rescue_errors = []
    with tempfile.TemporaryDirectory(prefix="voice-entrypoint-") as directory:
        workspace = Path(directory).resolve()
        workspace.chmod(0o700)
        local = workspace / ".local"
        local.mkdir(mode=0o700)
        if fixture is None:
            models = {role: ("injected-test-" + role).encode()
                      for role in ("mel", "embedding", "classifier")}
            manifest = {"format_version": 1, "frontend": FRONTEND_ID, "threshold": .5,
                        "models": {role: {"path": role + ".onnx", "sha256": digest(data)}
                                   for role, data in models.items()}}
        else:
            models, manifest = fixture.models, fixture.manifest
        for role, data in models.items():
            _save(local / (role + ".onnx"), data)
        bundle = local / "bundle.json"
        _save(bundle, manifest)
        _save(local / "ha-token", b"fixture-token")
        gateway = {
            "persistent_enabled": True, "bind_ipv4": HOST, "api_port": 18084,
            "api_token": "a" * 64, "api_clients": ["192.168.50.11"],
            "ami": {"host": HOST, "port": 5038, "username": "synthetic", "secret": "synthetic-ami"},
            "dial_allowlist": [], "ring_allowlist": [],
            "phones": [{"id": PHONE, "lines": [PHONE, "202"], "ipv4": DEVICE,
                        "device": "SEP020000000001", "port": 18080,
                        "auth_path": "/auth/" + "a" * 64, "username": "u" * 24, "password": "p" * 24}]}
        _save(local / "gateway.json", gateway)
        transport = {"gateway_config": str(local / "gateway.json"), "line": PHONE,
                     "bind_ipv4": HOST, "bind_port": 24576, "phone_rtp_port": 24578,
                     "expected_source_port": 24580, "duration_seconds": duration_seconds,
                     "receive_idle_seconds": 1, "volume": 20}
        voice = {"service": {"enabled": True, "rearm_after_turn": False, "preroll_ms": 0,
                             "max_utterance_seconds": pipeline.UPLOAD_FRAMES * .02,
                             "audio_idle_timeout_seconds": .5, "turn_timeout_seconds": 4},
                 "phones": {PHONE: True},
                 "ha": {"base_url": "http://fixture.invalid", "token_file": ".local/ha-token",
                        "pipeline_id": "fixture-pipeline"},
                 "wake": {"mode": "onnx", "bundle_file": ".local/bundle.json"}}
        if config_mutator:
            config_mutator(transport, voice)
        _save(local / "transport.json", transport)
        _save(local / "voice.json", voice)
        evidence["config_sha256"] = {"voice": _fingerprint(voice), "transport": _fingerprint(transport)}

        def from_bundle(path, *, private_root):
            evidence["model_factory_calls"] += 1
            require(path == bundle and private_root == local, "builder_bundle_path_mismatch")
            if detector_factory is None:
                detector = original_detector(path, private_root=private_root)
                model_runtime.update(
                    numpy=importlib.metadata.version("numpy"),
                    onnxruntime=importlib.metadata.version("onnxruntime"),
                    providers={role: model.get_providers() for role, model in detector._sessions.items()})
            else:
                # Exercise production private manifest/hash validation even in
                # fast unit tests; fake model bytes are never sent to ONNX.
                _load_bundle(path, private_root)
                detector = detector_factory(PHONE)
            recorded = pipeline.RecordingDetector(detector)
            detectors.append(recorded)
            return recorded

        argv = ["--transport-config", str(local / "transport.json"),
                "--voice-config", str(local / "voice.json"), "--workspace", str(workspace), *flags]
        with pipeline.network_guard() as network, contextlib.ExitStack() as stack:
            def deny_listener(*_args, **_kwargs):
                network["external_network_attempts"] += 1
                raise pipeline.FixtureFailure("network_forbidden")

            for method in ("bind", "listen"):
                stack.enter_context(mock.patch.object(socket.socket, method, deny_listener))
            for owner, name, replacement in (
                (runner.transport, "UdpMedia", Media),
                (runner.transport, "_GatewayObserver", Observer),
                (runner.transport, "_Watchdog", Watchdog),
                (HAClient, "_default_connect", connect),
                (HAClient, "_fetch_audio_async", fetch),
                (FrozenOnnxWakeDetector, "from_bundle", from_bundle),
                (runner, "load_config", load_voice),
                (runner.transport, "load_pilot_config", load_transport),
                (runner, "build_service", build),
                (runner.PhoneVoiceController, "run", observe_run),
            ):
                stack.enter_context(mock.patch.object(owner, name, replacement))
            try:
                with contextlib.redirect_stdout(captured):
                    exit_code = runner.main(argv)
                native = json.loads(captured.getvalue())
                require(type(native) is dict, "invalid_entrypoint_report")
            except Exception:
                failure = "entrypoint_execution_failed"
            finally:
                # Production run_session.finally is part of the tested path.
                # Only the cleanup below is fixture rescue; never let it repair
                # the evidence used for acceptance.
                before = snapshot()
                service = owned.get("service")
                stale = bool(service and streams) and all(
                    service.feed_pcm(PHONE, stream, bytes(640)) is False for stream in streams)
                session = owned.get("session")
                # Independent rescue attempts ensure a broken transport stop
                # cannot skip release of the in-memory provider or observer.
                try:
                    if session:
                        # The synthetic supervisor has already finished. Let
                        # emergency fixture cleanup release its owned mock I/O.
                        for watchdog in watchdogs:
                            watchdog.finished = False
                        session.stop("fixture_rescue")
                except Exception:
                    rescue_errors.append("fixture_transport_rescue_failed")
                try:
                    if service:
                        asyncio.run(service.close())
                except Exception:
                    rescue_errors.append("fixture_assistant_rescue_failed")
                try:
                    for observer in observers:
                        observer.done.set()
                        if observer.thread.ident is not None:
                            observer.thread.join(1)
                except Exception:
                    rescue_errors.append("fixture_observer_rescue_failed")
                try:
                    for watchdog in watchdogs:
                        watchdog.finish()
                except Exception:
                    rescue_errors.append("fixture_watchdog_rescue_failed")
            after = snapshot()
    triggers = detectors[0].triggers if detectors else []
    uploaded = b"".join(bytes(peer.audio) for peer in peers)
    eos = sum(peer.eos for peer in peers)
    output_pcm = b"".join(runner.transport.decode_ulaw(value) for value in wire.output_payloads)
    upload_matches = bool(peers and eos == 1 and uploaded == expected_pcm() and any(uploaded))
    expected_output = runner.transport.encode_ulaw(reply_frame)
    output_matches = bool(wire.output_payloads) and all(value == expected_output for value in wire.output_payloads)
    result = native.get("result") or {}
    detector_frames = detectors[0].frames if detectors else 0
    # The negative control must actually process the entire matched input.
    # Merely reaching its lease deadline (or seeing a few silent frames) is not
    # evidence that this detector rejected the supplied input duration.
    minimum_silence_frames = len(pcm8k) // pipeline.FRAME
    cleanup = bool(clean(before) and stale and not native.get("cleanup_errors"))
    rescue_clean = bool(not rescue_errors and clean(after))
    config_matches = bool(evidence.get("loaded_config_matches_build")
                          and evidence.get("loaded_voice_sha256") == evidence["config_sha256"]["voice"]
                          and evidence.get("built_voice_sha256") == evidence["config_sha256"]["voice"]
                          and evidence.get("loaded_transport_sha256") == evidence["config_sha256"]["transport"])
    common = (failure is None and cleanup and rescue_clean and config_matches
              and evidence["build_service_calls"] == evidence["model_factory_calls"] == 1
              and evidence["voice_load_calls"] == evidence["transport_load_calls"] == 1
              and evidence["controller_modes"] == ["wake"] and evidence["media_opened"] == 1
              and before["output_packets"] == len(wire.output_packets)
              and network["external_network_attempts"] == 0 and not native.get("error"))
    turn = bool(common and exit_code == 0 and result.get("reason") == "turn_complete"
                and len(triggers) == 1 and upload_matches and output_matches and any(output_pcm)
                and len(wire.output_packets) == pipeline.REPLY_FRAMES)
    success = turn if scenario == "complete" else bool(
        common and exit_code == 1 and result.get("reason") in ("lease_expired", "transport_stopped")
        and before["transport_reason"] == "lease_expired"
        and detector_frames >= minimum_silence_frames
        and result.get("input_frames", 0) >= minimum_silence_frames and len(wire.decoded) > 0
        and not triggers and not peers and not wire.output_packets
        and "armed" in native.get("states", []) and not any(wire.decoded))
    return {
        **evidence, "scenario": scenario, "assertion_success": success, "turn_success": turn,
        "entrypoint_exit_code": exit_code, "entrypoint_error": failure or native.get("error"),
        "entrypoint_reason": result.get("reason"), "states": native.get("states", []),
        "detector_kind": "injected_test" if detector_factory is not None else "onnx",
        "actual_onnx": bool(detector_factory is None and detectors),
        "model_runtime": model_runtime, "loaded_config_matches_build": config_matches,
        "cleanup_success": cleanup, "rescue_cleanup_success": rescue_clean,
        "entrypoint_state_before_rescue": before, "rescue_errors": rescue_errors,
        "stop_confirmed": before["stop_confirmed"], "stale_stream_rejected": stale,
        "wake_triggers": len(triggers), "wake_frame_indices": triggers,
        "detector_frames": detector_frames, "minimum_silence_frames": minimum_silence_frames,
        "input_frames": result.get("input_frames", 0),
        "encoded_input_bytes": len(wire.encoded), "encoded_input_sha256": digest(wire.encoded),
        "decoded_input_bytes": len(wire.decoded), "decoded_input_sha256": digest(wire.decoded),
        "ha_sessions": len(peers), "ha_audio_bytes": len(uploaded), "ha_audio_sha256": digest(uploaded),
        "ha_eos": eos, "upload_matches_postwake_pcm": upload_matches,
        "expected_upload_sha256": digest(expected_pcm()),
        "tts_pcm16k_sha256": digest(reply_pcm), "tts_source_frame_sha256": digest(reply_frame),
        "output_packets": len(wire.output_packets), "output_pcm_bytes": len(output_pcm),
        "output_pcm_sha256": digest(output_pcm), "output_matches_tts_pcmu": output_matches,
        "output_nonzero": bool(any(output_pcm)), "output_after_stop": before["output_after_stop"],
        "external_network_attempts": network["external_network_attempts"],
        "settings": {"preroll_ms": 0, "upload_frames": pipeline.UPLOAD_FRAMES,
                     "duration_seconds": duration_seconds, "rearm_after_turn": False,
                     "live_profile_loaded": False},
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def loaded_sources():
    """Verify module origins, then hash those actual loaded source files."""
    import phone_voice_controller
    import voice_assistant.audio
    import voice_assistant.config
    import voice_assistant.ha
    import voice_assistant.playback
    import voice_assistant.service
    import voice_assistant.wake
    modules = {
        "scripts/test-voice-pipeline.py": pipeline,
        "scripts/voice_session.py": runner,
        "containers/asterisk/phone_voice_transport.py": runner.transport,
        "containers/asterisk/phone_voice_controller.py": phone_voice_controller,
        "containers/asterisk/phone_gateway.py": runner.transport._gateway_module(),
        "voice_assistant/config.py": voice_assistant.config,
        "voice_assistant/service.py": voice_assistant.service,
        "voice_assistant/ha.py": voice_assistant.ha,
        "voice_assistant/playback.py": voice_assistant.playback,
        "voice_assistant/wake.py": voice_assistant.wake,
        "voice_assistant/audio.py": voice_assistant.audio,
    }
    sources = {"scripts/test-voice-entrypoint.py": digest(SCRIPT.read_bytes())}
    for name, module in modules.items():
        expected = (ROOT / name).resolve(strict=True)
        actual = Path(module.__file__).resolve(strict=True)
        origin = Path(module.__spec__.origin).resolve(strict=True)
        require(actual == expected and origin == expected, "loaded_module_origin_mismatch")
        sources[name] = digest(actual.read_bytes())
    return sources


def run_suite(args):
    sources = loaded_sources()
    fixture = pipeline.load_fixture(args.private_root, args.bundle, args.positive_wav)
    scenarios = SCENARIOS if args.scenario == "all" else (args.scenario,)
    runs = [run_scenario(scenario, fixture.pcm8k, fixture=fixture,
                         duration_seconds=args.duration_seconds) for scenario in scenarios]
    require(loaded_sources() == sources, "loaded_source_changed_during_run")
    return {
        "assertion_success": all(run["assertion_success"] for run in runs),
        "actual_onnx": all(run["actual_onnx"] for run in runs), "runs": runs,
        "fixture": fixture.provenance, "physical_io": False, "live_ha": False,
        "runtime": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                    "platform": platform.system(), "machine": platform.machine(), "uid": os.getuid()},
        "source_sha256": sources, "loaded_module_origins_verified": True,
        "boundaries": {"real": ["voice_session.main", "JSON config loaders", "build_service",
                               "ONNX wake detector", "G.711/RTP", "VoiceSession",
                               "PhoneVoiceController", "AssistantService", "HAClient", "WavePlayback"],
                       "simulated": ["datagram socket", "device supervisor/acknowledgement",
                                     "idle observer", "HA WebSocket and TTS HTTP response"]},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("private-root", "bundle", "positive-wav"):
        parser.add_argument("--" + name)
    parser.add_argument("--scenario", choices=("all", *SCENARIOS), default="all")
    parser.add_argument("--duration-seconds", type=float, default=12)
    parser.add_argument("--timeout", type=float, default=120, help="Total worker deadline, 1..120 seconds.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable evidence (the default).")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        require(all((args.private_root, args.bundle, args.positive_wav)),
                "explicit_private_fixture_arguments_required")
        require(math.isfinite(args.timeout) and 1 <= args.timeout <= 120, "invalid_worker_timeout")
        require(math.isfinite(args.duration_seconds) and 0 < args.duration_seconds <= 30,
                "invalid_scenario_duration")
        if args.worker:
            report = run_suite(args)
        else:
            command = [sys.executable, "-I", "-B", str(SCRIPT), "--worker", "--private-root", args.private_root,
                       "--bundle", args.bundle, "--positive-wav", args.positive_wav,
                       "--scenario", args.scenario, "--duration-seconds", str(args.duration_seconds),
                       "--timeout", str(args.timeout), "--json"]
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                child = subprocess.Popen(command, stdout=output, stderr=errors, start_new_session=True)
                try:
                    child.wait(timeout=args.timeout)
                except subprocess.TimeoutExpired:
                    child.terminate()
                    try:
                        child.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=2)
                    raise pipeline.FixtureFailure("entrypoint_worker_deadline") from None
                output.seek(0)
                raw = output.read(2 * 1024 * 1024 + 1)
                require(len(raw) <= 2 * 1024 * 1024, "worker_report_too_large")
                try:
                    report = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise pipeline.FixtureFailure("invalid_worker_report") from None
                require(type(report) is dict, "invalid_worker_report")
                require(child.returncode == 0 or report.get("assertion_success") is False,
                        "worker_failed_without_valid_report")
        print(json.dumps(report, sort_keys=True))
        return 0 if report.get("assertion_success") is True else 1
    except Exception as error:
        code = str(error) if isinstance(error, pipeline.FixtureFailure) else "entrypoint_fixture_failed"
        print(json.dumps({"assertion_success": False, "error": code,
                          "physical_io": False, "live_ha": False}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
