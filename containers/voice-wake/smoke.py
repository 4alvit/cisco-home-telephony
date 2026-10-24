#!/usr/bin/env python3
"""Check the optional inference layer without models, microphones or networking."""
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import sys


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def reject_network(event, _arguments):
    if event in ("socket.connect", "socket.connect_ex", "socket.bind"):
        raise RuntimeError("network_operation_forbidden_in_wake_smoke")


def check_base(manifest_path=Path("/opt/voice-wake/base-source.json")):
    manifest = json.loads(manifest_path.read_text())
    require(manifest.get("version") == 1 and bool(manifest.get("sha256")),
            "invalid_base_source_manifest")
    for name, expected in manifest["sha256"].items():
        actual = hashlib.sha256(Path(name).read_bytes()).hexdigest()
        require(actual == expected, "base_source_revision_mismatch")
    return manifest["base_source_commit"]


def check_dependencies(lock_path=Path("/opt/voice-wake/requirements.lock")):
    require(sys.version_info[:2] in ((3, 11), (3, 12)), "unsupported_python")
    versions = {}
    for line in lock_path.read_text().splitlines():
        match = re.fullmatch(r"([A-Za-z0-9_-]+)==([^\s]+)\s*\\?", line)
        if match:
            name, version = match.groups()
            require(importlib.metadata.version(name) == version,
                    "wake_dependency_version_mismatch")
            versions[name] = version
    require({"numpy", "onnxruntime", "protobuf"} <= versions.keys(),
            "incomplete_wake_lock")
    return versions


def check_installer_absence(temporary_directory=Path("/tmp")):
    require(all(importlib.util.find_spec(name) is None for name in ("pip", "ensurepip")),
            "runtime_installers_must_be_absent")
    try:
        importlib.metadata.version("pip")
    except importlib.metadata.PackageNotFoundError:
        pass
    else:
        raise RuntimeError("runtime_pip_metadata_must_be_absent")
    require(not any(temporary_directory.glob("voice-installer-*")),
            "installer_wheel_must_be_removed")
    require(not (temporary_directory / "install_locked.py").exists(),
            "build_helper_must_be_removed")


def identity_model():
    """An ONNX IR8/opset13 float Identity graph, no weights or external data.

    Encode the tiny protobuf directly so the smoke test needs no ONNX exporter.
    All values and message lengths are below 128 (one-byte varints).
    """
    def number(field, value):
        return bytes((field << 3, value))

    def message(field, value):
        require(len(value) < 128, "synthetic_protobuf_message_too_large")
        return bytes(((field << 3) | 2, len(value))) + value

    def value_info(name):
        shape = message(1, number(1, 1))  # one dimension of size 1
        tensor = number(1, 1) + message(2, shape)  # FLOAT, shape [1]
        return message(1, name) + message(2, message(1, tensor))

    node = message(1, b"x") + message(2, b"y") + message(4, b"Identity")
    graph = (message(1, node) + message(2, b"wake-smoke")
             + message(11, value_info(b"x")) + message(12, value_info(b"y")))
    return number(1, 8) + message(7, graph) + message(8, number(2, 13))


def check_onnx():
    import numpy as np
    from voice_assistant.wake import (
        FRONTEND_ID, CLASSIFIER_FRAMES, HOP_BYTES, REFRACTORY_HOPS, _onnx_session,
    )
    require(FRONTEND_ID == "echomuse-oww-16k-v1"
            and CLASSIFIER_FRAMES == 16 and HOP_BYTES == 2560
            and REFRACTORY_HOPS == 19, "unexpected_frozen_frontend")
    session = _onnx_session(identity_model())
    require(session.get_providers() == ["CPUExecutionProvider"],
            "unexpected_inference_provider")
    samples = np.array([0.125], dtype=np.float32)
    result = session.run(None, {"x": samples})
    require(len(result) == 1 and np.array_equal(result[0], samples),
            "synthetic_onnx_inference_failed")
    return True


def main():
    sys.addaudithook(reject_network)
    require(os.geteuid() == 10001, "smoke_requires_nonroot_uid_10001")
    check_installer_absence()
    source = check_base()
    dependencies = check_dependencies()
    check_onnx()
    # Imports exercise the staged entry points without invoking a live session.
    import stage_models
    import run_reserved
    require(callable(stage_models.stage_models) and callable(run_reserved.main),
            "wake_runtime_helpers_missing")
    print(json.dumps({
        "wake_image_smoke": "passed", "base_source_commit": source,
        "dependencies": dependencies, "synthetic_onnx_cpu_inference": True,
        "private_models_loaded": False, "network_actions": 0,
        "microphone_started": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
