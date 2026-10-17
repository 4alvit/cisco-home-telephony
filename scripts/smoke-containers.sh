#!/usr/bin/env bash
# Isolated Docker smoke test with synthetic inventory; never accesses real phone configs.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $# -ne 1 ]]; then echo "Usage: $0 IMAGE_PREFIX" >&2; exit 2; fi
prefix=$1
umask 077
mkdir -p .local
# Keep temporary synthetic fixtures inside ignored workspace storage.
test_root=$(mktemp -d "$PWD/.local/smoke.XXXXXX")
container="cisco-asterisk-smoke-$$"
tftp_container="cisco-tftp-smoke-$$"
asterisk_fixture="cisco-smoke-asterisk:$$"
tftp_fixture="cisco-smoke-tftp:$$"
tftp_network="cisco-tftp-smoke-$$"
cleanup() {
  local status=$?
  if [[ "$status" != 0 ]]; then
    docker logs "$container" 2>/dev/null || true
    docker logs "$tftp_container" 2>/dev/null || true
  fi
  docker rm -f "$container" "$tftp_container" >/dev/null 2>&1 || true
  docker network rm "$tftp_network" >/dev/null 2>&1 || true
  docker image rm "$asterisk_fixture" "$tftp_fixture" >/dev/null 2>&1 || true
  rm -rf "$test_root"
  return "$status"
}
trap cleanup EXIT
python3 - "$test_root" <<'PY'
import json, pathlib, sys
root=pathlib.Path(sys.argv[1])
(root/'site.json').write_text(json.dumps({'node_ipv4':'127.0.0.1','lan_cidr':'127.0.0.0/8','ntp_ipv4':'127.0.0.1',
 'phone_cidrs':['127.0.0.1/32','192.168.60.25/32'],
 'home_assistant':{'enabled':True,'ami_client_ipv4':'127.0.0.1','allow_originate':True,
 'assist':{'enabled':True,'host_ipv4':'127.0.0.1','sip_port':5061}}}))
(root/'phones.json').write_text(json.dumps({'phones':[{'model':'CP-7945G','verified':True,'mac':'020000000001','extension':'201','label':'CI synthetic','firmware_load':'SIP45.9-4-2SR3-1S','transport':'tcp'}]}))
PY
python3 scripts/provision.py --site "$test_root/site.json" --inventory "$test_root/phones.json" --out "$test_root/generated"
# Only synthetic CI configs become readable by container UID 10001.
chmod 0755 "$test_root" "$test_root/generated" "$test_root/generated/asterisk"
chmod 0644 "$test_root/generated/asterisk/"*.conf
# Copy only synthetic configuration into a disposable image. This also works
# when a remote/VM Docker daemon has no bind mount access to the host workspace.
cat > "$test_root/Dockerfile.asterisk" <<'DOCKERFILE'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY --chown=10001:10001 generated/asterisk/ /etc/asterisk/
DOCKERFILE
docker build -q --build-arg "BASE_IMAGE=$prefix/asterisk:22.11.0" -f "$test_root/Dockerfile.asterisk" -t "$asterisk_fixture" "$test_root" >/dev/null
docker run -d --name "$container" --network none --read-only \
  --tmpfs /run/asterisk:uid=10001,gid=10001 \
  --tmpfs /var/lib/asterisk/db:uid=10001,gid=10001 \
  --tmpfs /var/spool/asterisk:uid=10001,gid=10001 \
  --tmpfs /var/log/asterisk:uid=10001,gid=10001 \
  --tmpfs /tmp:uid=10001,gid=10001 \
  "$asterisk_fixture" >/dev/null
ready=0
for attempt in {1..30}; do
  if docker exec "$container" asterisk -rx 'core show uptime' > /dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
if [[ "$ready" != 1 ]]; then docker logs "$container"; echo 'Asterisk did not start' >&2; exit 1; fi
# Allow module initialization to complete, then require the actual generated config.
for attempt in {1..30}; do
  if docker exec "$container" asterisk -rx 'pjsip show endpoints' | grep -E 'Endpoint:.*201' > /dev/null; then break; fi
  sleep 1
done
docker exec "$container" asterisk -rx 'pjsip show endpoints' | grep -E 'Endpoint:.*201' > /dev/null
docker exec "$container" asterisk -rx 'pjsip show transports' | grep -E 'transport-tcp.*127.0.0.1:5060' > /dev/null
docker exec "$container" asterisk -rx 'pjsip show transports' | grep -E 'transport-udp.*127.0.0.1:5060' > /dev/null
docker exec "$container" asterisk -rx 'dialplan show 600@home' | grep 'Echo' > /dev/null
docker exec "$container" asterisk -rx 'dialplan show 601@home' | grep 'Milliwatt' > /dev/null
docker exec "$container" asterisk -rx 'dialplan show 620@home' | grep 'PJSIP/201' > /dev/null
docker exec "$container" asterisk -rx 'module show like chan_pjsip' | grep -E 'chan_pjsip.so.*Running' > /dev/null
docker exec "$container" asterisk -rx 'module show like codec_opus_open_source' | grep -E 'codec_opus_open_source.so.*Running' > /dev/null
docker exec "$container" asterisk -rx 'core show translation paths ulaw' | grep -E '\(ulaw@8000\).*\(opus@48000\)' > /dev/null
docker exec "$container" asterisk -rx 'core show translation paths opus' | grep -E '\(opus@48000\).*\(ulaw@8000\)' > /dev/null
docker exec "$container" asterisk -rx 'dialplan show 630@home' | grep 'ha-assist/sip:630@127.0.0.1:5061' > /dev/null
python3 tests/ami_runtime.py "$container" "$test_root/generated/ha-ami-secret.json"
docker exec "$container" asterisk -rx 'core show version'
if docker logs "$container" 2>&1 | grep -E 'Could not create an object of type|Unable to bind|Could not create.*transport' > /dev/null; then
  docker logs "$container"
  echo 'Asterisk configuration object or bind error' >&2
  exit 1
fi
docker run --rm --network none --entrypoint /usr/sbin/in.tftpd "$prefix/tftp:bookworm" --version
mkdir "$test_root/tftp"
printf 'tftp-smoke-ok\n' > "$test_root/tftp/health.txt"
chmod 0755 "$test_root/tftp"
chmod 0644 "$test_root/tftp/health.txt"
# Exercise the exact Kubernetes init script against a root-owned, fsGroup-writable
# emptyDir analogue. UID 10001 must never chmod the /served volume root.
chmod 0755 "$test_root/generated/tftp"
chmod 0644 "$test_root/generated/tftp/"*.xml
python3 - "$test_root/tftp-init.sh" <<'PYINIT'
import importlib.util, pathlib, sys
spec=importlib.util.spec_from_file_location('render', 'scripts/render_k3s.py')
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
site={'namespace':'test','node_name':'test','node_ipv4':'127.0.0.1','tftp_image':'test'}
manifest=module.render_tftp(site, {'dialplan.xml':'<DIALTEMPLATE/>'})
script=manifest['items'][1]['spec']['template']['spec']['initContainers'][0]['command'][2]
pathlib.Path(sys.argv[1]).write_text(script+'\n test -s /served/root/health.txt && test -s /served/root/dialplan.xml\n')
PYINIT
chmod 0644 "$test_root/tftp-init.sh"
cat > "$test_root/Dockerfile.tftp" <<'DOCKERFILE'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY tftp/ /srv/tftp/
COPY tftp/ /firmware/
COPY generated/tftp/ /config/
COPY tftp-init.sh /init.sh
DOCKERFILE
docker build -q --build-arg "BASE_IMAGE=$prefix/tftp:bookworm" -f "$test_root/Dockerfile.tftp" -t "$tftp_fixture" "$test_root" >/dev/null
docker run --rm --network none --read-only --user 10001:10001 --cap-drop ALL \
  --tmpfs /served:uid=0,gid=10001,mode=2770 \
  --entrypoint /bin/sh "$tftp_fixture" -ec 'sh /init.sh'
# tftpd uses getaddrinfo(AI_ADDRCONFIG): a loopback-only namespace cannot resolve
# even numeric bind addresses. An internal bridge supplies IPv4 without egress.
docker network create --internal "$tftp_network" >/dev/null
docker run -d --name "$tftp_container" --network "$tftp_network" --read-only \
  --cap-drop ALL --cap-add NET_BIND_SERVICE --cap-add SYS_CHROOT --cap-add SETUID --cap-add SETGID \
  --tmpfs /tmp \
  "$tftp_fixture" --foreground --user tftp --secure \
  --address 127.0.0.1:69 --port-range 20000:20031 --blocksize 1468 /srv/tftp >/dev/null
sleep 1
docker exec "$tftp_container" timeout 10 tftp 127.0.0.1 -m binary -c get health.txt /tmp/health.txt
docker exec "$tftp_container" cat /tmp/health.txt | grep -x 'tftp-smoke-ok' > /dev/null
echo 'Isolated smoke passed: Asterisk, AMI permissions/actions, G.711/Opus translation, Assist route, and TFTP RRQ.'
