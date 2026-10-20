#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 1 || -z "$1" || "$1" == -* ]]; then
    echo "Usage: $0 LOCAL_IMAGE (isolated synthetic smoke; no pulls or phone actions)" >&2
    exit 2
fi
command -v docker >/dev/null
# Resolve the existing local image once. Running its ID cannot pull a new tag.
image_id=$(docker image inspect "$1" --format '{{.Id}}')
[[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Invalid local image ID' >&2; exit 1; }
docker run --rm --pull never --network none --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
    --cap-drop ALL --security-opt no-new-privileges \
    --user 10001:10001 --pids-limit 64 --memory 512m --cpus 1 \
    --entrypoint python3 "$image_id" /opt/voice/smoke.py
