#!/usr/bin/env python3
"""Build and smoke an optional wake layer without sending private files to Docker."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
MAX_SOURCE_BYTES = 1024 * 1024
CONTEXT_FILES = (
    "containers/voice-wake/Dockerfile",
    "containers/voice/install_locked.py",
    "containers/voice-wake/requirements.lock",
    "containers/voice-wake/smoke.py",
    "containers/voice-wake/stage_models.py",
    "containers/voice-wake/run_reserved.py",
    "voice_assistant/wake.py",
)
# Pin compatible code in the base to an explicit known Git revision, rather than
# requiring unrelated in-progress edits in this checkout to be in that image.
BASE_FILES = {
    "scripts/voice_session.py": "/workspace/scripts/voice_session.py",
    "scripts/voice_session_sip.py": "/workspace/scripts/voice_session_sip.py",
    "containers/asterisk/phone_voice_sip.py": "/workspace/containers/asterisk/phone_voice_sip.py",
    "containers/asterisk/phone_voice_sip_ami.py": "/workspace/containers/asterisk/phone_voice_sip_ami.py",
    **{f"containers/asterisk/{name}.py": f"/workspace/containers/asterisk/{name}.py"
       for name in ("phone_voice_transport", "phone_voice_controller",
                    "phone_voice_control", "phone_voice_hands_free", "phone_voice_display", "phone_camera_preview", "phone_gateway",
                    "phone_apps", "phone_apps_ha", "phone_player", "media_runtime", "radio")},
    "containers/asterisk/radio-catalog.json": "/workspace/containers/asterisk/radio-catalog.json",
    **{f"voice_assistant/{name}.py": f"/workspace/voice_assistant/{name}.py"
       for name in ("__init__", "config", "audio", "service", "ha", "pipeline", "playback")},
    "containers/voice/requirements.lock": "/opt/voice/requirements.lock",
    "containers/voice/smoke.py": "/opt/voice/smoke.py",
    "containers/voice/stage_private.py": "/opt/voice/stage_private.py",
}
IMMUTABLE_IMAGE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}\Z")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
TAG = re.compile(r"[a-z0-9][a-z0-9._:/-]*\Z")


def run(arguments, *, root=ROOT, capture=False):
    return subprocess.run(arguments, cwd=root, check=True,
                          stdout=subprocess.PIPE if capture else None,
                          text=capture, timeout=1800)


def source_bytes(root: Path, relative: str) -> bytes:
    source = root / relative
    for ancestor in (source, *source.parents):
        if ancestor == root:
            break
        if ancestor.is_symlink():
            raise ValueError("Build source symlinks are not allowed")
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
            raise ValueError("Invalid or oversized build source")
        data = stream.read(MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError("Oversized build source")
    return data


def base_manifest(root: Path, reference: str) -> dict:
    commit = run(["git", "rev-parse", "--verify", "--end-of-options",
                  reference + "^{commit}"], root=root, capture=True).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise ValueError("Invalid base source commit")
    hashes = {}
    for relative, installed in BASE_FILES.items():
        result = subprocess.run(["git", "show", f"{commit}:{relative}"], cwd=root,
                                check=True, stdout=subprocess.PIPE, timeout=15)
        if len(result.stdout) > MAX_SOURCE_BYTES:
            raise ValueError("Oversized base source")
        hashes[installed] = hashlib.sha256(result.stdout).hexdigest()
    return {"version": 1, "base_source_commit": commit, "sha256": hashes}


def snapshot(root: Path, destination: Path, manifest: dict) -> dict:
    hashes = {}
    for relative in CONTEXT_FILES:
        data = source_bytes(root, relative)
        output = destination / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
        output.chmod(0o644)
        hashes[relative] = hashlib.sha256(data).hexdigest()
    output = destination / "base-source.json"
    output.write_text(json.dumps(manifest, sort_keys=True) + "\n")
    output.chmod(0o644)
    return hashes


def build(args, *, root=ROOT) -> dict:
    base_image = getattr(args, "base_image", None)
    base_image_id = getattr(args, "base_image_id", None)
    if bool(base_image) == bool(base_image_id):
        raise ValueError("Supply exactly one --base-image or --base-image-id")
    if base_image and not IMMUTABLE_IMAGE.fullmatch(base_image):
        raise ValueError("--base-image must be an immutable repository@sha256 reference")
    if base_image_id and not IMAGE_ID.fullmatch(base_image_id):
        raise ValueError("--base-image-id must be a complete local sha256 image ID")
    if not TAG.fullmatch(args.tag):
        raise ValueError("Invalid output tag")
    if args.platform not in ("linux/amd64", "linux/arm64"):
        raise ValueError("Only linux/amd64 and linux/arm64 are supported")
    manifest = base_manifest(root, args.base_source_ref)
    run(["docker", "info"], root=root)
    def check_platform(reference):
        actual = run(["docker", "image", "inspect", reference, "--format",
                      "{{.Os}}/{{.Architecture}}"], root=root, capture=True).stdout.strip()
        if actual != args.platform:
            raise ValueError("Image platform does not match --platform")
    temporary_tag = None
    try:
        if base_image_id:
            def check_local_base(reference):
                actual = run(["docker", "image", "inspect", reference, "--format", "{{.Id}}"],
                             root=root, capture=True).stdout.strip()
                if actual != base_image_id:
                    raise ValueError("Local base image ID changed")
            check_local_base(base_image_id)
            check_platform(base_image_id)
            temporary_tag = ("cisco-voice-wake-base:" + base_image_id[7:23]
                             + "-" + secrets.token_hex(8))
            run(["docker", "tag", base_image_id, temporary_tag], root=root)
            check_local_base(temporary_tag)
            base_image = temporary_tag
        else:
            run(["docker", "pull", "--platform", args.platform, base_image], root=root)
            check_platform(base_image)
        with tempfile.TemporaryDirectory(prefix="cisco-voice-wake-build-") as directory:
            context = Path(directory)
            hashes = snapshot(root, context, manifest)
            run(["docker", "build", "--pull=false", "--platform", args.platform,
                 "--build-arg", "VOICE_IMAGE=" + base_image,
                 "-f", str(context / "containers/voice-wake/Dockerfile"),
                 "-t", args.tag, str(context)], root=root)
        if base_image_id:
            check_local_base(temporary_tag)
    finally:
        if temporary_tag:
            # Remove only the alias created here, never the image by ID.
            run(["docker", "image", "rm", "--no-prune", temporary_tag], root=root)
    image_id = run(["docker", "image", "inspect", args.tag, "--format", "{{.Id}}"],
                   root=root, capture=True).stdout.strip()
    if not IMAGE_ID.fullmatch(image_id):
        raise ValueError("Invalid built image ID")
    check_platform(image_id)
    common = ["docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
              "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
              "--user", "10001:10001", "--pids-limit", "128", "--memory", "512m",
              "--cpus", "2", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
              "--entrypoint", "python3", image_id]
    # First verify original manual operation; then the exact optional layer.
    for smoke in ("/opt/voice/smoke.py", "/opt/voice-wake/smoke.py"):
        run(common + [smoke], root=root)
    return {
        "kind": "optional-wake-image", "image_id": image_id, "tag": args.tag,
        "platform": args.platform,
        "base_image": getattr(args, "base_image", None),
        "base_image_id": base_image_id,
        "base_source_commit": manifest["base_source_commit"],
        "context_sha256": hashes, "smoke_passed": True,
        "private_models_included": False, "pushed": False, "deployed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    base = parser.add_mutually_exclusive_group(required=True)
    base.add_argument("--base-image")
    base.add_argument("--base-image-id",
                      help="Full immutable local Docker image ID, for an unpushed CI base")
    parser.add_argument("--base-source-ref", required=True,
                        help="Exact Git commit used to build the manual base image")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--platform", default="linux/amd64")
    args = parser.parse_args()
    try:
        receipt = build(args)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Wake image build failed: {error}\n")
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
