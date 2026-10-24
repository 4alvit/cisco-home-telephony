#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $# -ne 1 || ! "$1" =~ ^[a-z0-9][a-z0-9./:_-]*[a-z0-9]$ && ! "$1" =~ ^[a-z0-9]$ ]]; then
    echo "Usage: $0 REGISTRY/PREFIX (build and smoke locally; never pushes)" >&2
    exit 2
fi
prefix=$1
platform=${BUILD_PLATFORM:-linux/amd64}
case "$platform" in linux/amd64|linux/arm64) ;; *) echo 'Unsupported BUILD_PLATFORM' >&2; exit 2 ;; esac
command -v docker >/dev/null
command -v python3 >/dev/null
docker info >/dev/null

if [[ -n "${PYTHON_IMAGE:-}" ]]; then
    base=$PYTHON_IMAGE
else
    digest=$(docker buildx imagetools inspect python:3.12-slim-bookworm --format '{{.Manifest.Digest}}')
    [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Could not resolve Python digest' >&2; exit 1; }
    base="python:3.12-slim-bookworm@$digest"
fi
[[ "$base" =~ ^python:3\.(11|12)-slim-bookworm@sha256:[0-9a-f]{64}$ ]] || {
    echo 'PYTHON_IMAGE must be an official Python 3.11/3.12 slim-bookworm digest reference' >&2
    exit 2
}

# Never pass the repository root to Docker: .local may hold household secrets.
context=$(mktemp -d "${TMPDIR:-/tmp}/cisco-voice-build.XXXXXXXX")
trap 'rm -rf "$context"' EXIT
python3 - "$PWD" "$context" <<'PY'
from pathlib import Path
import os
import stat
import sys

root, target = (Path(value).resolve() for value in sys.argv[1:])
files = (
    'scripts/voice_session.py',
    'scripts/voice_session_sip.py',
    'containers/asterisk/phone_voice_transport.py',
    'containers/asterisk/phone_voice_controller.py',
    'containers/asterisk/phone_voice_control.py',
    'containers/asterisk/phone_voice_hands_free.py',
    'containers/asterisk/phone_voice_sip.py',
    'containers/asterisk/phone_voice_sip_ami.py',
    'containers/asterisk/phone_voice_display.py',
    'containers/asterisk/phone_camera_preview.py',
    'containers/asterisk/phone_gateway.py',
    'containers/asterisk/phone_player.py',
    'containers/asterisk/media_runtime.py',
    'containers/asterisk/radio.py',
    'containers/asterisk/radio-catalog.json',
    'containers/asterisk/phone_apps.py',
    'containers/asterisk/phone_apps_ha.py',
    'voice_assistant/__init__.py', 'voice_assistant/config.py',
    'voice_assistant/audio.py', 'voice_assistant/service.py',
    'voice_assistant/ha.py', 'voice_assistant/pipeline.py', 'voice_assistant/playback.py',
    'containers/voice/Dockerfile', 'containers/voice/requirements.lock',
    'containers/voice/install_locked.py',
    'containers/voice/smoke.py', 'containers/voice/stage_private.py',
)
for relative in files:
    source = root / relative
    for ancestor in (source, *source.parents):
        if ancestor == root:
            break
        if ancestor.is_symlink():
            raise SystemExit('Build source symlinks are not allowed')
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise SystemExit('Invalid or oversized build source')
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise SystemExit('Oversized build source')
    output = target / relative
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    output.chmod(0o644)
PY

printf 'Pinned base: %s\n' "$base"
printf 'Debian package snapshot: 20261019T000000Z\n'
docker build --platform "$platform" --build-arg "PYTHON_IMAGE=$base" \
    -f "$context/containers/voice/Dockerfile" -t "$prefix/voice:pilot" "$context"
bash scripts/smoke-voice-image.sh "$prefix/voice:pilot"
printf 'Built and smoke-tested locally: %s/voice:pilot. Nothing pushed or deployed.\n' "$prefix"
