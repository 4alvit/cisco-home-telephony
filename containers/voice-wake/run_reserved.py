#!/usr/bin/env python3
"""Run one wake session through the gateway's shared durable media reservation.

The state directory must be the very same host directory used by the gateway's
VoiceControl, mounted below this workspace's .local. Validation is the default:
it reads bounded private files but does not load inference or start hardware.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import threading
import time

# The installed image supplies these paths through PYTHONPATH. Also support a
# checked-out source tree without importing or installing anything globally.
SOURCE_ROOT = Path(__file__).resolve().parents[2]
if (SOURCE_ROOT / "containers/asterisk/phone_voice_control.py").is_file():
    sys.path[:0] = [str(SOURCE_ROOT), str(SOURCE_ROOT / "containers/asterisk")]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import phone_voice_control as control
from stage_models import _validate_bundle, MAX_MODEL_BYTES, MAX_TOTAL_BYTES
from voice_assistant.config import validate_config
from voice_assistant.service import ServiceSettings

UNCERTAIN_CLEANUP_SECONDS = control.UNCERTAIN_CLEANUP_SECONDS


class GuardedSubprocessHandle(control._SubprocessHandle):
    """Stop a directly spawned runner if constructing its reader thread fails."""

    def __init__(self, *args, **kwargs):
        try:
            super().__init__(*args, **kwargs)
        except BaseException:
            process = getattr(self, "process", None)
            if process is not None:
                try:
                    process.terminate()  # Never signal its watchdog process group.
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass  # The durable reservation and outer grace remain authoritative.
                finally:
                    if process.stdout is not None:
                        process.stdout.close()
            raise


@dataclass(frozen=True)
class Inputs:
    workspace: Path
    state_directory: Path
    profile: control.Profile
    transport: dict = field(repr=False)
    gateway: dict = field(repr=False)
    voice: dict = field(repr=False)
    token: str = field(repr=False)
    fingerprints: dict = field(repr=False)


def load_inputs(args) -> Inputs:
    """Validate exact private inputs without socket creation or model inference."""
    workspace = Path(args.workspace).resolve()
    state = control._private_path(args.state_directory, workspace)
    info = state.stat()
    control._require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                     and stat.S_IMODE(info.st_mode) == 0o700,
                     "shared_state_requires_current_owner_and_0700")
    fingerprints = {}

    def read(value, limit=control.MAX_CONFIG):
        path = control._private_path(value, workspace)
        data = control._read(path, limit)
        fingerprints[str(path)] = hashlib.sha256(data).hexdigest()
        return path, data

    def read_json(value):
        path, data = read(value)
        value = json.loads(data)
        control._require(isinstance(value, dict), "private_object_required")
        return path, value

    transport_path, config = read_json(args.transport_config)
    voice_path, voice = read_json(args.voice_config)
    _, gateway = read_json(config["gateway_config"])
    transport, _, _ = control._libraries()
    allowed = {"gateway_config", "line", "bind_ipv4", "bind_port", "phone_rtp_port",
               "duration_seconds", "receive_idle_seconds", "volume", "expected_source_port"}
    control._require(not set(config) - allowed, "invalid_transport_config")
    transport._gateway_module().validate(gateway)
    transport.tx_uri(config["bind_ipv4"], config["bind_port"])
    transport.rx_uri(config["bind_ipv4"], config["phone_rtp_port"], config.get("volume", 20))
    transport._duration(config["duration_seconds"])
    transport._duration(config.get("receive_idle_seconds", 3), 10)
    if config.get("expected_source_port") is not None:
        transport._port(config["expected_source_port"], False)
    control._require(config["bind_ipv4"] == gateway["bind_ipv4"] == gateway["ami"]["host"],
                     "local_node_bind_required")
    phones = [phone for phone in gateway["phones"] if config["line"] in phone["lines"]]
    control._require(len(phones) == 1, "one_selected_phone_required")
    phone = phones[0]
    validate_config(voice)
    settings = ServiceSettings(**voice["service"])
    control._require(settings.enabled
                     and voice["phones"] == {phone["id"]: True}
                     and voice["wake"]["mode"] == "onnx", "bounded_wake_profile_required")
    _, raw_token = read(voice["ha"]["token_file"], 8192)
    token = raw_token.decode("ascii").strip()
    control._require(token and all(33 <= ord(char) <= 126 for char in token), "invalid_voice_token")
    bundle_path, bundle = read_json(voice["wake"]["bundle_file"])
    bundle = _validate_bundle(bundle)
    total = 0
    for entry in bundle["models"].values():
        remaining = MAX_TOTAL_BYTES - total
        control._require(remaining > 0, "private_model_total_exceeded")
        _, data = read(bundle_path.parent / entry["path"], min(MAX_MODEL_BYTES, remaining))
        total += len(data)
        control._require(data and total <= MAX_TOTAL_BYTES
                         and hashlib.sha256(data).hexdigest() == entry["sha256"],
                         "private_model_checksum_or_size_failed")
    # The staged model volume remains read-only for the complete child lifetime.
    voice["wake"]["bundle_file"] = str(bundle_path)
    port = args.display_port
    if port is not None:
        control._require(type(port) is int and 1024 <= port <= 65535
                         and port not in {config["bind_port"], config["phone_rtp_port"],
                                          gateway["api_port"], gateway["ami"]["port"],
                                          *(item["port"] for item in gateway["phones"])},
                         "invalid_voice_display_port")
    ffmpeg = Path(args.ffmpeg)
    control._require(ffmpeg.is_absolute() and ffmpeg.is_file() and os.access(ffmpeg, os.X_OK),
                     "invalid_voice_ffmpeg")
    profile = control.Profile(
        phone["id"], tuple(phone["lines"]), phone["device"], phone["ipv4"],
        transport_path, voice_path, float(config["duration_seconds"]),
        config["bind_ipv4"], config["bind_port"], port,
        mode="wake", rearm_after_turn=settings.rearm_after_turn,
    )
    return Inputs(workspace, state, profile, config, gateway, voice, token, fingerprints)


class ReservedWakeControl(control.VoiceControl):
    """Keep the existing flock/journal/watchdog completion contract unchanged."""

    def __init__(self, args, inputs, *, stop_event=None, **kwargs):
        self.arguments, self.inputs = args, inputs
        self.outcomes = {}
        config = control.ControlConfig(
            inputs.workspace, inputs.state_directory, sys.executable, args.ffmpeg,
            (inputs.profile,), enabled=True,
        )
        factory = kwargs.pop("runner_factory", GuardedSubprocessHandle)

        def checked_factory(*args, **kwargs):
            # Preparation reads and snapshots several private files. A Stop
            # received during that work must prevent a new microphone child,
            # even though start() has already passed its initial cancellation
            # check. The shared owner conservatively retains its reservation
            # when any runner factory refuses after spawn_attempted was set.
            control._require(stop_event is None or not stop_event.is_set(),
                             "wake_cancelled_before_spawn")
            return factory(*args, **kwargs)

        kwargs["runner_factory"] = checked_factory
        super().__init__(config, authorize=lambda *_: True, **kwargs)

    def _mkdir(self):
        # Never create a fresh fallback directory: a missing shared mount must
        # fail rather than silently establishing a second reservation domain.
        path = control._private_path(self.config.state_directory, self.config.workspace)
        info = path.stat()
        control._require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                         and stat.S_IMODE(info.st_mode) == 0o700,
                         "shared_state_requires_current_owner_and_0700")

    def _prepare(self, profile, session):
        latest = load_inputs(self.arguments)
        control._require(latest.fingerprints == self.inputs.fingerprints
                         and latest.profile == self.inputs.profile,
                         "wake_inputs_changed")
        config, gateway, voice = copy.deepcopy((latest.transport, latest.gateway, latest.voice))
        phone = next(item for item in gateway["phones"] if item["id"] == profile.phone_id)
        self._check_profile(profile, config, phone, voice)
        directory = self.config.state_directory / ("run-" + session.session_id)
        directory.mkdir(mode=0o700)
        session.directory = directory
        gateway = {
            "persistent_enabled": True, "bind_ipv4": gateway["bind_ipv4"],
            "api_port": gateway["api_port"], "api_clients": gateway["api_clients"],
            "api_token": "unused-wake-reservation-api-token-00000000",
            "ami": gateway["ami"], "dial_allowlist": [], "ring_allowlist": [],
            "phones": [{key: phone[key] for key in
                        ("id", "lines", "device", "ipv4", "port", "auth_path", "username", "password")}],
        }
        config["gateway_config"] = str(directory / "gateway.json")
        voice["ha"]["token_file"] = str(directory / "ha-token.txt")
        files = {"transport.json": json.dumps(config), "gateway.json": json.dumps(gateway),
                 "voice.json": json.dumps(voice), "ha-token.txt": latest.token + "\n"}
        for name, value in files.items():
            fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                output.write(value)
        argv = [
            self.config.python_executable, str(control.ROOT / "scripts/voice_session.py"),
            "--transport-config", str(directory / "transport.json"),
            "--voice-config", str(directory / "voice.json"),
            "--workspace", str(self.config.workspace), "--ffmpeg", self.config.ffmpeg_executable,
            "--status-file", str(directory / "progress.json"),
            "--run", "--attended", "--exclusive-media", "--enable-microphone",
        ]
        if profile.display_port is not None:
            argv += ["--display-port", str(profile.display_port)]
        return argv

    def _finish(self, session, raw):
        # The shared journal describes safe ownership release, not whether a
        # conversation completed. Keep that protocol and distinguish the two
        # outcomes in this one-shot command's result.
        reason, exit_code = "result_unavailable", session.handle.poll()
        turns, valid_count = 0, True
        try:
            if type(raw) is bytes and len(raw) <= control.MAX_REPORT and not session.handle.overflow:
                value = json.loads(raw)
                if "turns_completed" in value:
                    turns = value["turns_completed"]
                    valid_count = type(turns) is int and 0 <= turns <= 10000
                    if not valid_count:
                        turns = 0
                candidate = value["result"]["reason"]
                if candidate in ("turn_complete", "explicit_stop", "display_stop", "shutdown",
                                 "lease_expired", "receive_idle_timeout", "assistant_error",
                                 "transport_stopped", "start_failed"):
                    reason = candidate
        except (ValueError, TypeError, KeyError):
            pass
        super()._finish(session, raw)
        cleanup = (session.stop_confirmed and session.lease_fd < 0
                   and session.state in ("stopped", "failed"))
        # A rearming session normally ends on Stop or its original deadline.
        # Clean shutdown alone is not evidence that any reply completed.
        replied = (reason == "turn_complete" or turns > 0 and reason in
                   ("explicit_stop", "display_stop", "shutdown", "lease_expired"))
        complete = (cleanup and session.state == "stopped" and exit_code == 0
                    and valid_count and replied)
        self.outcomes[session.session_id] = {
            "cleanup_success": cleanup, "turn_success": complete, "reason": reason,
            "turns_completed": turns,
        }

    def result_summary(self, line):
        value = self.status(line, context=None)
        outcome = self.outcomes.get(value["session_id"], {
            "cleanup_success": False, "turn_success": False, "reason": "result_unavailable",
            "turns_completed": 0,
        })
        return {"success": outcome["turn_success"], **outcome, "voice": value}


def claim_attempt(inputs, attempt_id):
    """Consume one deliberate attempt durably, even if a replacement Pod starts."""
    control._require(isinstance(attempt_id, str)
                     and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}", attempt_id),
                     "explicit_wake_attempt_required")
    key = hashlib.sha256((inputs.profile.device + "\0" + attempt_id).encode()).hexdigest()
    path = inputs.state_directory / ("wake-attempt-" + key + ".consumed")
    # Never remove this file, including on write/fsync/start failure. A failed
    # attempt requires a new explicit ID, not an automatic retry.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(b"wake-attempt-consumed-v1\n")
        output.flush()
        os.fsync(output.fileno())
    directory = os.open(inputs.state_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return "wake-" + key


def drain_cleanup(manager, line, *, uncertain=False, clock=time.monotonic, sleep=time.sleep):
    """Keep PID1 alive for watchdog descendants after a runner or storage failure."""
    try:
        manager.close()
    except Exception:
        uncertain = True
    deadline = clock() + UNCERTAIN_CLEANUP_SECONDS
    while clock() < deadline:
        try:
            manager.tick()
            value = manager.status(line, context=None)
        except Exception:
            uncertain, value = True, None
        if (not uncertain and value is not None and not value["active"]
                and value["state"] not in ("recovery_required", "external_owner")):
            return
        sleep(.05)
    # No lock, consumed marker or recovery journal is deleted at this boundary.


def run(args, inputs):
    """Own one child until verified cleanup, or leave a durable refusal record."""
    stopped = threading.Event()
    manager = ReservedWakeControl(args, inputs, monitor=False, stop_event=stopped)
    previous = {}
    line = inputs.transport["line"]
    uncertain = False
    failure = None
    for number in (signal.SIGINT, signal.SIGTERM):
        previous[number] = signal.signal(number, lambda *_: stopped.set())
    deadline = (time.monotonic() + inputs.profile.duration + control.STOP_GRACE_SECONDS
                + control.RUNNER_COMPLETION_MARGIN_SECONDS)
    try:
        request_id = claim_attempt(inputs, args.attempt_id)
        if stopped.is_set():
            failure = "wake_cancelled_before_start"
        else:
            manager.start(line, request_id, context=None,
                          attended=True, exclusive_media=True, enable_microphone=True)
        while time.monotonic() < deadline:
            if failure:
                break
            if stopped.is_set():
                manager.close()
            manager.tick()
            value = manager.status(line, context=None)
            if value["state"] in ("stopped", "failed", "cancelled", "recovery_required"):
                break
            time.sleep(.05)
        else:
            uncertain, failure = True, "wake_cleanup_deadline"
    except BaseException:
        uncertain = True
        raise
    finally:
        try:
            # Even when start() raises, spawn_attempted records that a child
            # may exist. Unknown outcome must retain the complete grace.
            if any(item.spawn_attempted for item in manager._sessions.values()):
                drain_cleanup(manager, line, uncertain=uncertain)
            else:
                manager.close()
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
    result = manager.result_summary(line)
    if failure:
        result.update(success=False, turn_success=False, error=failure)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport-config", required=True)
    parser.add_argument("--voice-config", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--state-directory", required=True)
    parser.add_argument("--ffmpeg", default="/usr/bin/ffmpeg")
    parser.add_argument("--display-port", type=int)
    parser.add_argument("--attempt-id", help="Stable deliberate attempt ID; required with --run")
    for flag in ("run", "attended", "exclusive-media", "enable-microphone"):
        parser.add_argument("--" + flag, action="store_true")
    args = parser.parse_args(argv)
    try:
        inputs = load_inputs(args)
        if not args.run:
            print(json.dumps({"valid": True, "network_actions": 0, "microphone_started": False}))
            return 0
        control._require(args.attended and args.exclusive_media and args.enable_microphone,
                         "explicit_wake_start_required")
        result = run(args, inputs)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["success"] else 1
    except Exception:
        print(json.dumps({"success": False, "turn_success": False, "cleanup_success": False,
                          "error": "wake_reservation_or_runtime_failed",
                          "automatic_retry": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
