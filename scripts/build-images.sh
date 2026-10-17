#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $# -ne 1 ]]; then echo "Usage: $0 REGISTRY/PREFIX (build locally; never pushes)" >&2; exit 2; fi
prefix=$1
command -v docker >/dev/null
docker info >/dev/null
# Resolve a mutable distribution tag once, then use exactly that manifest for both builds.
base_digest=$(docker buildx imagetools inspect debian:bookworm-slim --format '{{.Manifest.Digest}}')
[[ "$base_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Could not resolve Debian digest' >&2; exit 1; }
base="debian:bookworm-slim@$base_digest"
printf 'Base: %s\n' "$base"
docker build --platform "${BUILD_PLATFORM:-linux/amd64}" --build-arg "DEBIAN_IMAGE=$base" -t "$prefix/asterisk:22.11.0" containers/asterisk
docker build --platform "${BUILD_PLATFORM:-linux/amd64}" --build-arg "DEBIAN_IMAGE=$base" -t "$prefix/tftp:bookworm" containers/tftp
# Binary checks do not bind ports or contact the LAN.
docker run --rm "$prefix/asterisk:22.11.0" -V
docker run --rm --entrypoint /usr/sbin/in.tftpd "$prefix/tftp:bookworm" --version
printf 'Built locally. Push each image explicitly, record RepoDigests, then set image digest references in .local/site.json.\n'
