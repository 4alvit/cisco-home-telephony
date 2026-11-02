#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $# -ne 1 ]]; then echo "Usage: $0 REGISTRY/PREFIX (build locally; never pushes)" >&2; exit 2; fi
prefix=$1
command -v docker >/dev/null
docker info >/dev/null
# The PBX's multimedia closure uses supported Debian 13 libraries. TFTP has no
# multimedia dependency and keeps its existing distribution independently.
resolve_base() {
    local tag=$1 explicit=$2 digest
    if [[ -n "$explicit" ]]; then
        [[ "$explicit" =~ ^${tag}@sha256:[0-9a-f]{64}$ ]] || {
            echo "Expected $tag@sha256:..." >&2; return 2;
        }
        printf '%s\n' "$explicit"
    else
        digest=$(docker buildx imagetools inspect "$tag" --format '{{.Manifest.Digest}}')
        [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Could not resolve Debian digest' >&2; return 1; }
        printf '%s@%s\n' "$tag" "$digest"
    fi
}
pbx_base=$(resolve_base debian:trixie-slim "${ASTERISK_DEBIAN_IMAGE:-}")
tftp_base=$(resolve_base debian:bookworm-slim "${TFTP_DEBIAN_IMAGE:-}")
printf 'PBX base: %s\nTFTP base: %s\n' "$pbx_base" "$tftp_base"
printf 'PBX Debian package snapshot: 20261101T000000Z\n'
docker build --platform "${BUILD_PLATFORM:-linux/amd64}" --build-arg "DEBIAN_IMAGE=$pbx_base" -t "$prefix/asterisk:22.11.0" containers/asterisk
docker build --platform "${BUILD_PLATFORM:-linux/amd64}" --build-arg "DEBIAN_IMAGE=$tftp_base" -t "$prefix/tftp:bookworm" containers/tftp
# Binary checks do not bind ports or contact the LAN.
docker run --rm "$prefix/asterisk:22.11.0" -V
docker run --rm --entrypoint /usr/sbin/in.tftpd "$prefix/tftp:bookworm" --version
printf 'Built locally. Push each image explicitly, record RepoDigests, then set image digest references in .local/site.json.\n'
