#!/usr/bin/env python3
"""Offline, bounded G.711/wake/Assist pipeline acceptance with private fixtures.

Actual ONNX inference and production audio/controller code are exercised.
Datagram I/O, phone commands, and the HA peer are in-memory substitutes.
No microphone, speaker, live HA configuration, or network endpoint is used.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from dataclasses import dataclass
import hashlib
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import socket
import stat
import struct
import sys
import tempfile
import time
import wave
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "containers/asterisk")]
import phone_voice_transport as transport
from phone_voice_controller import PhoneVoiceController, RtpPCMSink, playback_frames, upsample_8k_frame
from voice_assistant.ha import AudioPayload, HAClient
from voice_assistant.playback import WavePlayback
from voice_assistant.service import AssistantService, ServiceSettings

SCENARIOS = ("complete", "stop", "preempt", "provider-failure", "no-wake")
PHONE = "fixture-phone"
FRAME = 320
UPLOAD_FRAMES = 10
REPLY_FRAMES = 20
MAX_WAV_BYTES = 2 * 1024 * 1024


class FixtureFailure(ValueError):
    """Only fixed, non-private codes belong in this exception."""


def require(condition, code):
    if not condition:
        raise FixtureFailure(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


@dataclass
class Fixture:
    pcm8k: bytes
    manifest: dict
    models: dict[str, bytes]
    provenance: dict


def read_private(path, root, limit):
    """Read bounded owner-only regular files, rejecting every symlink component."""
    path = Path(path)
    if not path.is_absolute():
        path = root / path
    require(".." not in path.parts, "private_path_required")
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise FixtureFailure("private_path_required") from None
    require(relative.parts, "private_file_required")
    current = root
    for part in relative.parts:
        current /= part
        require(not current.is_symlink(), "private_symlink_rejected")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and not info.st_mode & 0o077 and info.st_size <= limit,
                "private_bounded_regular_file_required")
        data = stream.read(limit + 1)
    require(len(data) <= limit, "private_file_too_large")
    return data


def load_fixture(private_root, bundle, positive_wav):
    root = Path(private_root)
    require(root.is_absolute() and root.name == ".local" and root.is_dir()
            and not root.is_symlink() and ".." not in root.parts,
            "explicit_private_root_required")
    # Do not normalize away a symlink supplied in the root itself or its parents.
    require(all(not p.is_symlink() for p in (root, *root.parents)), "private_symlink_rejected")
    raw_manifest = read_private(bundle, root, 1024 * 1024)
    manifest = json.loads(raw_manifest, object_pairs_hook=unique)
    from voice_assistant.wake import FRONTEND_ID, _threshold
    require(type(manifest) is dict and manifest.get("format_version") == 1
            and type(manifest.get("format_version")) is int
            and manifest.get("frontend") == FRONTEND_ID, "invalid_bundle")
    _threshold(manifest.get("threshold"))
    require(type(manifest.get("models")) is dict
            and set(manifest["models"]) == {"mel", "embedding", "classifier"}, "invalid_bundle")
    bundle_path = Path(bundle)
    if not bundle_path.is_absolute():
        bundle_path = root / bundle_path
    models = {}
    staged_manifest = {"format_version": 1, "frontend": FRONTEND_ID,
                       "threshold": manifest["threshold"], "models": {}}
    for role, entry in manifest["models"].items():
        require(type(entry) is dict and set(entry) == {"path", "sha256"}
                and type(entry["path"]) is str, "invalid_bundle_model")
        name = Path(entry["path"])
        require(not name.is_absolute() and ".." not in name.parts
                and name.suffix.lower() == ".onnx", "invalid_bundle_model")
        model = read_private(bundle_path.parent / name, root, 16 * 1024 * 1024)
        require(digest(model) == entry["sha256"], "model_digest_mismatch")
        models[role] = model
        staged_manifest["models"][role] = {"path": role + ".onnx", "sha256": digest(model)}
    require(sum(map(len, models.values())) <= 32 * 1024 * 1024, "model_total_too_large")
    wav_bytes = read_private(positive_wav, root, MAX_WAV_BYTES)
    with wave.open(io.BytesIO(wav_bytes), "rb") as source:
        require(source.getnchannels() == 1 and source.getsampwidth() == 2
                and source.getframerate() == 16000 and source.getcomptype() == "NONE",
                "positive_wav_must_be_mono_pcm16_16000")
        count = source.getnframes()
        require(0 < count <= 20 * 16000, "positive_wav_duration_outside_bounds")
        pcm = source.readframes(count)
        require(len(pcm) == count * 2, "positive_wav_truncated")
    require(any(pcm), "positive_wav_must_be_nonzero")
    # This is explicit fixture preparation, not the outgoing RTP resampler.
    pcm8k = b"".join(next(playback_frames(pcm[i:i + 640], 16000))
                    for i in range(0, len(pcm), 640))
    return Fixture(pcm8k, staged_manifest, models, {
        "source_wav_sha256": digest(wav_bytes), "source_pcm16k_sha256": digest(pcm),
        "prepared_pcm8k_sha256": digest(pcm8k),
        "bundle_sha256": digest(raw_manifest),
        "model_sha256": {role: digest(value) for role, value in models.items()},
        "source_seconds": count / 16000,
        "preparation": "pair-average 16-to-8 kHz; final 20 ms frame zero-padded",
    })


class VerifiedOnnxFactory:
    """Only the explicit CLI fixture path constructs a real ONNX detector."""
    def __init__(self, bundle, private_root):
        self.bundle, self.private_root = bundle, private_root

    def __call__(self, _phone):
        from voice_assistant.wake import FrozenOnnxWakeDetector
        detector = FrozenOnnxWakeDetector.from_bundle(self.bundle, private_root=self.private_root)
        self.runtime = {
            "numpy": importlib.metadata.version("numpy"),
            "onnxruntime": importlib.metadata.version("onnxruntime"),
            "providers": {role: model.get_providers() for role, model in detector._sessions.items()},
        }
        return detector


@contextlib.contextmanager
def network_guard():
    counters = {"external_network_attempts": 0}

    def deny(*_args, **_kwargs):
        counters["external_network_attempts"] += 1
        raise FixtureFailure("network_forbidden")

    with contextlib.ExitStack() as stack:
        for owner, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                            (socket.socket, "sendto"), (socket, "getaddrinfo"),
                            (socket, "create_connection")):
            stack.enter_context(mock.patch.object(owner, name, deny))
        yield counters


class StrictHAPeer:
    """Validate exact post-wake PCM and one EOS before producing fixture events."""
    def __init__(self, scenario, expected_pcm):
        self.scenario, self.expected_pcm = scenario, expected_pcm
        self.messages = asyncio.Queue()
        self.messages.put_nowait(json.dumps({"type": "auth_required"}))
        self.authenticated = self.started = self.closed = False
        self.audio = bytearray()
        self.eos = 0
        self.command = None
        self.request_id = None

    def put(self, value):
        self.messages.put_nowait(json.dumps(value))

    def event(self, kind, data=None):
        self.put({"id": self.request_id, "type": "event",
                  "event": {"type": kind, "data": data or {}}})

    async def send(self, message):
        require(not self.closed, "ha_send_after_close")
        if isinstance(message, bytes):
            require(self.started and message[:1] == b"\x07" and self.eos == 0,
                    "invalid_ha_audio_sequence")
            if len(message) == 1:
                expected = self.expected_pcm()
                require(bool(expected) and any(expected) and bytes(self.audio) == expected,
                        "ha_pcm_lineage_mismatch")
                self.eos += 1
                if self.scenario == "provider-failure":
                    self.event("error", {"code": "fixture_provider_failure"})
                    return
                self.event("stt-end", {"stt_output": {"text": "Fixture request."}})
                self.event("intent-start")
                self.event("intent-end", {"intent_output": {
                    "conversation_id": "fixture-conversation",
                    "response": {"speech": {"plain": {"speech": "Fixture response."}}}}})
                self.event("tts-start", {"tts_input": "Fixture response."})
                self.event("tts-end", {"tts_output": {
                    "url": "/api/tts_proxy/fixture.wav", "mime_type": "audio/wav"}})
                self.event("run-end")
                return
            require(len(message) == 641, "ha_audio_must_be_20ms_pcm16k")
            self.audio.extend(message[1:])
            expected = self.expected_pcm()
            require(len(self.audio) <= len(expected) and expected.startswith(self.audio),
                    "ha_pcm_lineage_mismatch")
            return
        require(isinstance(message, str), "invalid_ha_message")
        value = json.loads(message)
        if value.get("type") == "auth":
            require(not self.authenticated and value.get("access_token") == "fixture-token",
                    "invalid_ha_auth")
            self.authenticated = True
            self.put({"type": "auth_ok"})
            return
        if value.get("type") == "ping":
            require(self.authenticated and self.started and self.eos == 1
                    and value.get("id") == 2 and self.scenario != "provider-failure",
                    "invalid_ha_completion_ping")
            self.put({"id": 2, "type": "pong"})
            return
        require(self.authenticated and not self.started
                and value.get("type") == "assist_pipeline/run"
                and value.get("start_stage") == "stt" and value.get("end_stage") == "tts"
                and value.get("pipeline") == "fixture-pipeline"
                and value.get("input", {}).get("sample_rate") == 16000,
                "invalid_ha_pipeline_request")
        self.started, self.command, self.request_id = True, value, value["id"]
        self.put({"id": self.request_id, "type": "result", "success": True, "result": None})
        self.event("run-start", {"runner_data": {"stt_binary_handler_id": 7}})
        self.event("stt-start")

    async def recv(self):
        return await self.messages.get()

    async def close(self):
        self.closed = True


class RecordingDetector:
    def __init__(self, detector):
        self.detector = detector
        self.frames = 0
        self.triggers = []

    @property
    def ready(self):
        return getattr(self.detector, "ready", True)

    def feed(self, pcm):
        index = self.frames
        self.frames += 1
        detected = self.detector.feed(pcm)
        if detected:
            self.triggers.append(index)
        return detected

    def reset(self):
        self.detector.reset()


class MemoryDatagram:
    """A paced datagram boundary; all RTP/codec work remains production code."""
    def __init__(self, pcm8k):
        self.frames = [pcm8k[i:i + FRAME] for i in range(0, len(pcm8k), FRAME)]
        self.sender = transport.RtpSender(sequence=100, timestamp=16000, ssrc=1234)
        self.position = 0
        self.next_input = time.monotonic()
        self.closed = False
        self.encoded = bytearray()
        self.decoded = bytearray()
        self.output_payloads = []
        self.output_packets = []
        self.output_after_stop = 0
        self.on_output = lambda: None
        self.session_active = lambda: True

    def frame(self, index):
        return self.frames[index] if index < len(self.frames) else bytes(FRAME)

    def expected_frame(self, index):
        return upsample_8k_frame(transport.decode_ulaw(transport.encode_ulaw(self.frame(index))))

    def recvfrom(self, _maximum):
        require(not self.closed, "receive_after_close")
        wait = self.next_input - time.monotonic()
        if wait > 0:
            time.sleep(min(wait, .01))
            raise socket.timeout()
        pcm = self.frame(self.position)
        self.position += 1
        payload = transport.encode_ulaw(pcm)
        self.encoded.extend(payload)
        self.decoded.extend(transport.decode_ulaw(payload))
        self.next_input = time.monotonic() + .02
        return self.sender.packet(payload), ("192.0.2.21", 24580)

    def sendto(self, data, address):
        if self.closed or not self.session_active():
            self.output_after_stop += 1
            raise FixtureFailure("output_after_stop")
        require(address == ("192.0.2.21", 24578), "unexpected_output_target")
        packet = transport.parse_rtp(data)
        if self.output_packets:
            previous = self.output_packets[-1]
            require(packet.sequence == (previous.sequence + 1) & 65535
                    and packet.timestamp == (previous.timestamp + 160) & 0xffffffff
                    and packet.ssrc == previous.ssrc and not packet.marker,
                    "invalid_output_rtp_sequence")
        else:
            require(packet.marker, "missing_first_packet_marker")
        self.output_packets.append(packet)
        self.output_payloads.append(packet.payload)
        self.on_output()
        return len(data)

    def close(self):
        self.closed = True


class MemoryMedia(transport.UdpMedia):
    def __init__(self, wire):
        super().__init__("192.0.2.10", 24576, "192.0.2.21", 24578, 24580)
        self.wire = wire

    def open(self):
        require(self.sock is None and not self.wire.closed, "media_already_used")
        self.sock = self.wire


async def _run_scenario(scenario, pcm8k, detector_factory, *,
                        detector_kind, duration_seconds, network_counters):
    """Run production pipeline code; supplied detectors are explicit test seams."""
    require(scenario in SCENARIOS, "unknown_scenario")
    require(detector_kind in ("injected_test", "onnx"), "unknown_detector_kind")
    require(detector_kind != "onnx" or isinstance(detector_factory, VerifiedOnnxFactory),
            "actual_onnx_requires_verified_factory")
    require(type(duration_seconds) in (int, float) and math.isfinite(duration_seconds)
            and 0 < duration_seconds <= 30, "invalid_scenario_duration")
    require(type(pcm8k) is bytes and 0 < len(pcm8k) <= 20 * 16000
            and len(pcm8k) % FRAME == 0 and any(pcm8k), "invalid_positive_pcm8k")
    started = time.monotonic()
    input_pcm = bytes(len(pcm8k)) if scenario == "no-wake" else pcm8k
    wire = MemoryDatagram(input_pcm)
    media = MemoryMedia(wire)
    commands, states, peers, stream_ids = [], [], [], []
    focus = ["idle"]
    armed = []
    session = transport.VoiceSession(commands.append, media, lambda: focus[0],
        duration=duration_seconds, enable_microphone=True, enable_playback=True,
        media_reserved=True, arm=armed.append)
    wire.session_active = lambda: session.state == "active"
    detector = RecordingDetector(detector_factory(PHONE))

    def expected_pcm():
        if not detector.triggers:
            return b""
        first = detector.triggers[0]
        return b"".join(wire.expected_frame(i) for i in range(first, first + UPLOAD_FRAMES))

    async def connect(url):
        require(url == "ws://fixture.invalid/api/websocket", "unexpected_ha_target")
        peer = StrictHAPeer(scenario, expected_pcm)
        peers.append(peer)
        return peer

    # Repeat a nonzero source frame as a deterministic fixture-derived TTS reply.
    reply_frame = next((pcm8k[i:i + FRAME] for i in range(0, len(pcm8k), FRAME)
                       if any(transport.decode_ulaw(transport.encode_ulaw(pcm8k[i:i + FRAME])))), None)
    require(reply_frame is not None, "positive_pcm_silent_after_g711")
    reply_pcm16k = upsample_8k_frame(reply_frame) * REPLY_FRAMES
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(reply_pcm16k)
    reply_wav = buffer.getvalue()

    async def fetch(url):
        require(url == "/api/tts_proxy/fixture.wav"
                and len(peers) == 1 and peers[0].eos == 1, "unexpected_tts_fetch")
        return AudioPayload(reply_wav, "audio/wav")

    client = HAClient("http://fixture.invalid", "fixture-token", pipeline_id="fixture-pipeline",
                      connect=connect, timeout=3, max_input_bytes=UPLOAD_FRAMES * 640)
    sink = RtpPCMSink(session, PHONE)
    playback = WavePlayback(fetch, sink, max_seconds=1)
    settings = ServiceSettings(enabled=True, rearm_after_turn=False, preroll_ms=0,
                               max_utterance_seconds=UPLOAD_FRAMES * .02,
                               audio_idle_timeout_seconds=.5, turn_timeout_seconds=4)
    service = None

    def event(value):
        states.append(value.state)
        if service is not None:
            stream = service._sessions[PHONE].stream_id
            if stream and stream not in stream_ids:
                stream_ids.append(stream)

    service = AssistantService(settings, {PHONE: True}, lambda _: detector, client, playback, event)
    controller = PhoneVoiceController(session, service, PHONE, duration=duration_seconds,
                                      receive_idle_seconds=1)
    loop = asyncio.get_running_loop()

    def output():
        if len(wire.output_packets) == 1:
            if scenario == "stop":
                loop.call_soon_threadsafe(controller.request_stop)
            elif scenario == "preempt":
                focus[0] = "ringing"

    wire.on_output = output
    failure = None
    result = None
    rescue_errors = []

    def owned_state():
        owner = service._sessions[PHONE]
        return {
            "transport_stopped": session.state == "stopped",
            "transport_reason": session.reason,
            "stop_confirmed": session.stop_confirmed,
            "media_closed": wire.closed and media.sock is None,
            "ha_closed": all(p.closed for p in peers),
            "assistant_state": service.status(PHONE).state,
            "stream_invalidated": owner.stream_id is None,
            "task_cleared": owner.task is None,
            "queue_cleared": owner.queue is None,
            "owner_released": service._owner is None,
            "output_after_stop": wire.output_after_stop,
            "output_packets": len(wire.output_packets),
            "remote_stop_commands": [c for c in commands if c.endswith(":Stop")],
        }

    def state_is_clean(value):
        return (all(value[key] is True for key in (
            "transport_stopped", "stop_confirmed", "media_closed", "ha_closed",
            "stream_invalidated", "task_cleared", "queue_cleared", "owner_released"))
            and value["assistant_state"] == "stopped"
            and value["output_after_stop"] == 0
            and value["remote_stop_commands"] == list(transport.stop_uris()))

    try:
        result = await asyncio.wait_for(controller.run(mode="wake"), duration_seconds + 2)
    except Exception:
        failure = "pipeline_execution_failed"
    finally:
        # Freeze acceptance evidence before test teardown can repair a broken
        # controller. A claimed result alone never proves ownership release.
        try:
            controller_state = owned_state()
            stale = bool(stream_ids) and all(
                service.feed_pcm(PHONE, sid, bytes(640)) is False for sid in stream_ids)
        finally:
            try:
                session.stop("fixture_cleanup")
            except Exception:
                rescue_errors.append("transport_rescue_failed")
            try:
                await service.close()
            except Exception:
                rescue_errors.append("assistant_rescue_failed")
    rescue_state = owned_state()
    output_pcm = b"".join(transport.decode_ulaw(p) for p in wire.output_payloads)
    uploaded = b"".join(bytes(p.audio) for p in peers)
    eos = sum(p.eos for p in peers)
    cleanup = bool(result and result.stop_confirmed is True and result.assistant_state == "stopped"
                   and state_is_clean(controller_state) and stale)
    rescue_cleanup = not rescue_errors and state_is_clean(rescue_state)
    expected_output = transport.encode_ulaw(reply_frame)
    output_matches = bool(wire.output_payloads) and all(p == expected_output for p in wire.output_payloads)
    upload_matches = bool(peers) and eos == 1 and uploaded == expected_pcm() and any(uploaded)
    reason = result.reason if result else failure
    turn_success = bool(result and reason == "turn_complete" and cleanup and upload_matches
                        and len(wire.output_packets) == REPLY_FRAMES and output_matches and any(output_pcm))
    common = (failure is None and cleanup and stale and bool(armed) and rescue_cleanup
              and network_counters["external_network_attempts"] == 0)
    if scenario == "no-wake":
        success = common and not detector.triggers and not peers and not wire.output_packets \
            and reason in ("lease_expired", "transport_stopped")
    elif scenario == "complete":
        success = common and len(detector.triggers) == 1 and turn_success
    elif scenario == "provider-failure":
        success = common and len(detector.triggers) == 1 and upload_matches \
            and reason == "assistant_error" and not wire.output_packets and "error" in states
    else:
        success = common and len(detector.triggers) == 1 and upload_matches and output_matches and any(output_pcm) \
            and 0 < len(wire.output_packets) < REPLY_FRAMES
        success = success and (reason == "explicit_stop" if scenario == "stop" else
                               reason == "transport_stopped"
                               and controller_state["transport_reason"] == "preempted_ringing")
    return {
        "success": bool(success), "turn_success": turn_success, "cleanup_success": cleanup,
        "controller_state_before_rescue": controller_state,
        "rescue_cleanup_success": rescue_cleanup, "rescue_errors": rescue_errors,
        "scenario": scenario, "detector_kind": detector_kind, "actual_onnx": detector_kind == "onnx",
        "settings": {"preroll_ms": 0, "upload_frames": UPLOAD_FRAMES,
                     "max_utterance_seconds": UPLOAD_FRAMES * .02,
                     "duration_seconds": duration_seconds, "rearm_after_turn": False,
                     "live_profile_loaded": False},
        "reason": reason, "transport_reason": controller_state["transport_reason"], "states": states,
        "wake_triggers": len(detector.triggers), "wake_frame_indices": detector.triggers,
        "input_frames": result.input_frames if result else 0,
        "encoded_input_bytes": len(wire.encoded), "encoded_input_sha256": digest(wire.encoded),
        "decoded_input_bytes": len(wire.decoded), "decoded_input_sha256": digest(wire.decoded),
        "ha_sessions": len(peers), "ha_audio_bytes": len(uploaded), "ha_audio_sha256": digest(uploaded),
        "ha_eos": eos, "upload_matches_postwake_pcm": bool(upload_matches),
        "expected_upload_sha256": digest(expected_pcm()), "ha_sample_rate": 16000 if peers else None,
        "tts_pcm16k_sha256": digest(reply_pcm16k), "tts_source_frame_sha256": digest(reply_frame),
        "output_packets": len(wire.output_packets), "output_pcm_bytes": len(output_pcm),
        "output_pcm_sha256": digest(output_pcm), "output_matches_tts_pcmu": output_matches,
        "output_nonzero": bool(any(output_pcm)), "output_after_stop": wire.output_after_stop,
        "ha_closed": controller_state["ha_closed"], "media_closed": controller_state["media_closed"],
        "stop_confirmed": controller_state["stop_confirmed"], "stale_stream_rejected": stale,
        "remote_stop_commands": controller_state["remote_stop_commands"],
        "external_network_attempts": network_counters["external_network_attempts"],
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


async def run_scenario(scenario, pcm8k, detector_factory, *,
                       detector_kind="injected_test", duration_seconds=12.0):
    """Test seam: injected detectors never count as actual ONNX acceptance."""
    # Cover model construction as well as the asynchronous turn. This is a
    # Python I/O guard around trusted fixtures, not an OS process sandbox.
    with network_guard() as counters:
        return await _run_scenario(scenario, pcm8k, detector_factory,
                                   detector_kind=detector_kind,
                                   duration_seconds=duration_seconds,
                                   network_counters=counters)


async def run_suite(scenarios, fixture, factory, duration_seconds):
    results = []
    for scenario in scenarios:
        results.append(await run_scenario(scenario, fixture.pcm8k, factory,
                       detector_kind="onnx", duration_seconds=duration_seconds))
    return {"success": all(r["success"] for r in results), "runs": results,
            "fixture": fixture.provenance, "actual_onnx": True,
            "runtime": factory.runtime,
            "source_sha256": {name: digest((ROOT / name).read_bytes()) for name in (
                "scripts/test-voice-pipeline.py", "containers/asterisk/phone_voice_transport.py",
                "containers/asterisk/phone_voice_controller.py", "voice_assistant/service.py",
                "voice_assistant/ha.py", "voice_assistant/playback.py", "voice_assistant/wake.py",
                "voice_assistant/audio.py")},
            "physical_io": False, "live_ha": False,
            "boundaries": {"real": ["ONNX wake detector", "G.711 codec", "RTP sender/receiver",
                "UdpMedia processing", "VoiceSession", "PhoneVoiceController", "AssistantService",
                "HAClient protocol", "WavePlayback", "RtpPCMSink"],
                "simulated": ["datagram socket", "phone command acknowledgement", "focus observer",
                              "HA WebSocket peer", "fixture-derived TTS response"]}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-root")
    parser.add_argument("--bundle")
    parser.add_argument("--positive-wav")
    parser.add_argument("--scenario", choices=("all", *SCENARIOS), default="all")
    parser.add_argument("--timeout", type=float, default=120, help="Total async runtime limit, 1..120 seconds.")
    parser.add_argument("--duration-seconds", type=float, default=20, help="Per-scenario media lease, 0..30 seconds.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        require(args.private_root and args.bundle and args.positive_wav, "explicit_private_fixtures_required")
        require(math.isfinite(args.timeout) and 1 <= args.timeout <= 120, "invalid_total_timeout")
        require(math.isfinite(args.duration_seconds) and 0 < args.duration_seconds <= 30,
                "invalid_scenario_duration")
        fixture = load_fixture(args.private_root, args.bundle, args.positive_wav)
        with tempfile.TemporaryDirectory(prefix="voice-pipeline-") as temporary:
            private = Path(temporary) / ".local"
            private.mkdir(mode=0o700)
            for role, content in fixture.models.items():
                fd = os.open(private / (role + ".onnx"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as output:
                    output.write(content)
            manifest = private / "bundle.json"
            fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                json.dump(fixture.manifest, output)
            factory = VerifiedOnnxFactory(manifest, private)
            scenarios = SCENARIOS if args.scenario == "all" else (args.scenario,)
            async def execute():
                return await asyncio.wait_for(run_suite(scenarios, fixture, factory, args.duration_seconds),
                                              timeout=args.timeout)
            report = asyncio.run(execute())
    except Exception as error:
        report = {"success": False, "error": str(error) if isinstance(error, FixtureFailure)
                  else "private_fixture_or_pipeline_failure", "physical_io": False, "live_ha": False}
    print(json.dumps(report, indent=None if args.json else 2, sort_keys=True))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
