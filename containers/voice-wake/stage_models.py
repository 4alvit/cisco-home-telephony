#!/usr/bin/env python3
"""Stage three verified wake models from a read-only private volume.

No inference, downloads, device access or source writes occur here. The caller
creates an empty, owner-only .local/models directory before staging. An init
container must complete successfully before the runtime mounts that directory.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat


FRONTEND_ID = "echomuse-oww-16k-v1"
ROLES = ("mel", "embedding", "classifier")
MAX_MODEL_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MARKER = ".wake-model-stage-in-progress"
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class StageError(ValueError):
    """A fixed public failure category, never private paths or model values."""


def _require(condition, reason):
    if not condition:
        raise StageError(reason)


def _absolute(value):
    path = Path(value)
    _require(path.is_absolute() and ".." not in path.parts,
             "absolute_paths_without_traversal_required")
    return path


def _open_directory(path):
    descriptor = os.open(path.anchor, _DIRECTORY)
    try:
        for component in path.parts[1:]:
            child = os.open(component, _DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _validate_bundle(bundle):
    _require(isinstance(bundle, dict)
             and set(bundle) == {"format_version", "frontend", "threshold", "models"},
             "invalid_bundle_shape")
    _require(type(bundle["format_version"]) is int and bundle["format_version"] == 1
             and bundle["frontend"] == FRONTEND_ID, "unsupported_bundle_format")
    threshold = bundle["threshold"]
    _require(type(threshold) in (int, float) and 0 < threshold <= 1
             and math.isfinite(threshold), "invalid_frozen_threshold")
    models = bundle["models"]
    _require(isinstance(models, dict) and set(models) == set(ROLES),
             "three_model_roles_required")
    normalized = {}
    for role in ROLES:
        item = models[role]
        _require(isinstance(item, dict) and set(item) == {"path", "sha256"},
                 "invalid_model_entry")
        name, digest = item["path"], item["sha256"]
        _require(isinstance(name, str) and 0 < len(name) <= 1024
                 and "\\" not in name and not any(ord(c) < 32 for c in name),
                 "invalid_relative_model_path")
        path = PurePosixPath(name)
        _require(not path.is_absolute() and path.suffix.lower() == ".onnx"
                 and all(part not in ("", ".", "..") for part in name.split("/")),
                 "invalid_relative_model_path")
        _require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest),
                 "invalid_model_checksum")
        normalized[role] = {"path": name, "sha256": digest}
    return {"format_version": 1, "frontend": FRONTEND_ID,
            "threshold": threshold, "models": normalized}


def _read_model(source_fd, name, limit):
    """Open every component relative to the pinned source without following links."""
    parts = PurePosixPath(name).parts
    directory = os.dup(source_fd)
    try:
        for component in parts[:-1]:
            child = os.open(component, _DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            _require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= limit,
                     "model_not_regular_or_oversized")
            data = stream.read(limit + 1)
        _require(0 < len(data) <= limit, "model_not_regular_or_oversized")
        return data
    finally:
        os.close(directory)


def _remove_owned(directory, name, identity):
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (info.st_dev, info.st_ino) == identity:
            os.unlink(name, dir_fd=directory)
            return True
    except FileNotFoundError:
        pass
    return False


def _create(directory, name, data, created):
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
    info = os.fstat(descriptor)
    created.append((name, (info.st_dev, info.st_ino)))
    with os.fdopen(descriptor, "wb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def stage_models(source, destination, bundle):
    """Copy exactly three pinned models and a normalized bundle into fresh storage.

    source and destination are absolute, real directory paths. destination must
    end in .local/models and already be empty, owned by this UID, and mode 0700.
    bundle is a parsed private JSON object. All model paths are relative to
    source, never to the caller's working directory. No source symlinks are
    accepted, including symlinks whose target happens to be inside the volume.
    """
    bundle = _validate_bundle(bundle)
    source, destination = _absolute(source), _absolute(destination)
    _require(destination.parts[-2:] == (".local", "models"),
             "destination_must_end_in_local_models")
    _require(source != destination and source not in destination.parents
             and destination not in source.parents, "source_destination_must_be_separate")
    source_fd = destination_fd = None
    created, completed = [], False
    try:
        source_fd = _open_directory(source)
        destination_fd = _open_directory(destination)
        info = os.fstat(destination_fd)
        _require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
                 "destination_requires_owner_and_mode_0700")
        _require(not os.listdir(destination_fd), "destination_must_be_empty")
        payload, total = [], 0
        for role in ROLES:
            model = bundle["models"][role]
            data = _read_model(source_fd, model["path"],
                               min(MAX_MODEL_BYTES, MAX_TOTAL_BYTES - total))
            total += len(data)
            _require(total <= MAX_TOTAL_BYTES, "model_total_oversized")
            _require(hashlib.sha256(data).hexdigest() == model["sha256"],
                     "model_checksum_mismatch")
            name = role + ".onnx"
            payload.append((name, data))
            model["path"] = name
        encoded = (json.dumps(bundle, sort_keys=True, separators=(",", ":")) + "\n").encode()

        # Claim only an empty directory. A concurrent initializer must fail
        # rather than merging models or treating a partial copy as complete.
        _create(destination_fd, MARKER, b"", created)
        _require(os.listdir(destination_fd) == [MARKER], "destination_changed_during_staging")
        for name, data in payload:
            _create(destination_fd, name, data, created)
        _create(destination_fd, "bundle.json", encoded, created)
        os.fsync(destination_fd)
        _require(_remove_owned(destination_fd, MARKER, created[0][1]),
                 "destination_changed_during_staging")
        completed = True
        return {"staged": True, "models": len(ROLES), "total_bytes": total}
    except OSError:
        raise StageError("model_staging_io_failed") from None
    finally:
        if destination_fd is not None:
            if not completed:
                for name, identity in reversed(created):
                    _remove_owned(destination_fd, name, identity)
            os.close(destination_fd)
        if source_fd is not None:
            os.close(source_fd)
