#!/usr/bin/env python3
"""Render Kubernetes JSON (valid YAML) without cluster access or secret output."""
import argparse
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
import tempfile
import xml.etree.ElementTree as ET
import zlib

RFC1918 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
WALLPAPER_FILES = {"List.xml", "home.png", "home-thumb.png"}
WALLPAPER_MAX_BYTES = 400_000
WALLPAPER_PATH = "Desktops/320x212x16"

def validate_site(site, with_tftp=False):
    ha = site.get("home_assistant", {})
    if not isinstance(ha, dict) or type(ha.get("enabled", False)) is not bool:
        raise ValueError("home_assistant must be an object with boolean enabled")
    if not isinstance(ha.get("assist", {}), dict):
        raise ValueError("home_assistant.assist must be an object")
    for key in ("radio", "gateway", "alarms", "external_sip"):
        config = site.get(key, {})
        if not isinstance(config, dict) or type(config.get("enabled", False)) is not bool:
            raise ValueError(f"{key} must be an object with boolean enabled")
    for key in ("namespace", "node_name", "node_ipv4", "lan_cidr", "asterisk_image", "kube_context"):
        if not isinstance(site.get(key), str) or not site[key]:
            raise ValueError(f"site.{key} must be a nonempty string")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", site["namespace"]):
        raise ValueError("Invalid namespace")
    if site["namespace"] in ("default", "kube-system", "kube-public", "kube-node-lease"):
        raise ValueError("Use a dedicated namespace")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,251}[a-z0-9]|[a-z0-9]", site["node_name"]):
        raise ValueError("Invalid node_name")
    addr = ipaddress.IPv4Address(site["node_ipv4"])
    net = ipaddress.IPv4Network(site["lan_cidr"], strict=True)
    if not any(net.subnet_of(private) for private in RFC1918) or not 8 <= net.prefixlen <= 30:
        raise ValueError("lan_cidr must be a private IPv4 subnet (/8 through /30)")
    if addr not in net or addr in (net.network_address, net.broadcast_address):
        raise ValueError("node_ipv4 must be a host address inside lan_cidr")
    for key in ("asterisk_image", "tftp_image") if with_tftp else ("asterisk_image",):
        if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", site.get(key, "")):
            raise ValueError(f"site.{key} must use an immutable @sha256 image digest")
    if not isinstance(site.get("image_pull_secrets", []), list):
        raise ValueError("image_pull_secrets must be a list")
    for item in site.get("image_pull_secrets", []):
        if not isinstance(item, str) or not re.fullmatch(r"[a-z0-9.-]+", item):
            raise ValueError("image_pull_secrets must be a list of Kubernetes Secret names")
    return site


def load_site(path, with_tftp=False):
    with Path(path).open() as source:
        return validate_site(json.load(source), with_tftp)


def read_files(directory, suffix):
    files = {}
    for path in sorted(Path(directory).glob(f"*{suffix}")):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Not a regular file: {path.name}")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", path.name):
            raise ValueError("Unsupported config filename")
        files[path.name] = path.read_text()
    if not files:
        raise ValueError(f"No {suffix} files in {directory}")
    if sum(len(k.encode()) + len(v.encode()) for k, v in files.items()) > 700_000:
        raise ValueError("Configuration exceeds safe Kubernetes Secret size; firmware belongs on the node")
    return files


def secret(namespace, prefix, files):
    checksum = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:16]
    name = f"{prefix}-{checksum}"
    return name, {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": namespace,
             "labels": {"app.kubernetes.io/part-of": "cisco-home-telephony"}},
             "type": "Opaque", "immutable": True, "data": {k: base64.b64encode(v.encode()).decode() for k, v in files.items()}}


def validate_png(data, width, height):
    """Validate a bounded, non-interlaced RGB8 PNG without image dependencies."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Wallpaper image is not a PNG")
    offset, chunks, compressed, ended = 8, [], bytearray(), False
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError("Truncated wallpaper PNG chunk")
        size = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + size
        if end > len(data) or not re.fullmatch(b"[A-Za-z]{4}", kind) or kind[2] & 32:
            raise ValueError("Invalid wallpaper PNG chunk")
        content = data[offset + 8:offset + 8 + size]
        crc = struct.unpack_from(">I", data, offset + 8 + size)[0]
        if zlib.crc32(kind + content) & 0xffffffff != crc:
            raise ValueError("Wallpaper PNG CRC mismatch")
        if not chunks and kind != b"IHDR":
            raise ValueError("Wallpaper PNG must start with IHDR")
        if kind == b"IHDR":
            if chunks or size != 13 or struct.unpack(">IIBBBBB", content) != (width, height, 8, 2, 0, 0, 0):
                raise ValueError(f"Wallpaper PNG must be {width}x{height}, RGB8, non-interlaced")
        elif kind == b"IDAT":
            if b"IDAT" in chunks and chunks[-1] != b"IDAT":
                raise ValueError("Wallpaper PNG IDAT chunks must be consecutive")
            compressed.extend(content)
        elif kind == b"IEND":
            if size or b"IDAT" not in chunks or end != len(data):
                raise ValueError("Invalid wallpaper PNG end")
            ended = True
        elif kind == b"PLTE":
            if b"PLTE" in chunks or b"IDAT" in chunks or not 0 < size <= 768 or size % 3:
                raise ValueError("Invalid wallpaper PNG palette")
        elif not kind[0] & 32:
            raise ValueError("Unsupported critical wallpaper PNG chunk")
        chunks.append(kind)
        offset = end
    if not ended:
        raise ValueError("Wallpaper PNG lacks IEND")
    expected = height * (1 + width * 3)
    try:
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(bytes(compressed), expected + 1)
    except zlib.error as exc:
        raise ValueError("Invalid wallpaper PNG compressed data") from exc
    if len(pixels) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("Wallpaper PNG decompressed size does not match its dimensions")
    if any(pixels[row * (1 + width * 3)] > 4 for row in range(height)):
        raise ValueError("Invalid wallpaper PNG scanline filter")


def validate_wallpaper(files):
    if not isinstance(files, dict) or set(files) != WALLPAPER_FILES or any(type(v) is not bytes for v in files.values()):
        raise ValueError("Wallpaper must contain exactly List.xml, home.png and home-thumb.png as bytes")
    if sum(map(len, files.values())) > WALLPAPER_MAX_BYTES or len(files["List.xml"]) > 4096:
        raise ValueError("Wallpaper exceeds its size limit")
    xml = files["List.xml"]
    if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
        raise ValueError("Wallpaper XML declarations are not allowed")
    try:
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
        root = ET.fromstring(xml.decode("utf-8"), parser=parser)
    except (ET.ParseError, UnicodeError) as exc:
        raise ValueError("Invalid wallpaper List.xml") from exc
    expected = {"Image": f"TFTP:{WALLPAPER_PATH}/home-thumb.png", "URL": f"TFTP:{WALLPAPER_PATH}/home.png"}
    if (root.tag != "CiscoIPPhoneImageList" or root.attrib or (root.text or "").strip()
            or len(root) != 1 or root[0].tag != "ImageItem" or root[0].attrib != expected
            or len(root[0]) or (root[0].text or "").strip() or (root[0].tail or "").strip()):
        raise ValueError("Wallpaper List.xml must contain only the approved ImageItem paths")
    validate_png(files["home.png"], 320, 212)
    validate_png(files["home-thumb.png"], 80, 53)
    return files


def read_wallpaper(directory):
    """Read only the optional three-file sibling directory; never follow symlinks."""
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError("Wallpaper directory must not be a symlink")
    if not directory.exists():
        return None
    if not directory.is_dir() or {p.name for p in directory.iterdir()} != WALLPAPER_FILES:
        raise ValueError("Wallpaper directory must contain exactly the three approved files")
    files = {}
    for name in sorted(WALLPAPER_FILES):
        path = directory / name
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("Wallpaper inputs must be regular files, not symlinks")
        with path.open("rb") as source:
            files[name] = source.read(WALLPAPER_MAX_BYTES + 1)
    return validate_wallpaper(files)


def binary_secret(namespace, prefix, files):
    data = {name: base64.b64encode(content).decode("ascii") for name, content in files.items()}
    checksum = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]
    name = f"{prefix}-{checksum}"
    return name, {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": namespace,
        "labels": {"app.kubernetes.io/part-of": "cisco-home-telephony"}},
        "type": "Opaque", "immutable": True, "data": data}


def pod_base(site):
    result = {"hostNetwork": True, "dnsPolicy": "ClusterFirstWithHostNet", "automountServiceAccountToken": False,
        "terminationGracePeriodSeconds": 45,
        "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
            {"matchFields": [{"key": "metadata.name", "operator": "In", "values": [site["node_name"]]}]}]}}},
        "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
        "nodeSelector": {"kubernetes.io/os": "linux"}}
    if site.get("image_pull_secrets"):
        result["imagePullSecrets"] = [{"name": name} for name in site["image_pull_secrets"]]
    return result


def render_core(site, configs):
    required = {"asterisk.conf", "pjsip.conf", "extensions.conf", "rtp.conf", "modules.conf", "logger.conf"}
    if required - configs.keys():
        raise ValueError("Missing Asterisk configs: " + ", ".join(sorted(required - configs.keys())))
    ns = site["namespace"]
    name, config = secret(ns, "asterisk-config", configs)
    spec = pod_base(site)
    spec["securityContext"].update({"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001})
    spec["containers"] = [{"name": "asterisk", "image": site["asterisk_image"], "imagePullPolicy": "IfNotPresent",
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
        "ports": [{"name": "sip-udp", "containerPort": 5060, "protocol": "UDP"}, {"name": "sip-tcp", "containerPort": 5060, "protocol": "TCP"}],
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1000m", "memory": "512Mi"}},
        "startupProbe": {"exec": {"command": ["asterisk", "-rx", "core show uptime"]}, "periodSeconds": 5, "failureThreshold": 30},
        "readinessProbe": {"exec": {"command": ["/bin/sh", "-ec", "transports=$(asterisk -rx 'pjsip show transports'); printf '%s\\n' \"$transports\" | grep -Eq 'Transport:.*transport-udp[[:space:]]+udp.*:5060'; printf '%s\\n' \"$transports\" | grep -Eq 'Transport:.*transport-tcp[[:space:]]+tcp.*:5060'"]}, "periodSeconds": 10},
        "livenessProbe": {"exec": {"command": ["asterisk", "-rx", "core show uptime"]}, "periodSeconds": 30, "failureThreshold": 3},
        "volumeMounts": [{"name": "config", "mountPath": "/etc/asterisk", "readOnly": True}] + [
            {"name": n, "mountPath": p} for n, p in (("run", "/run/asterisk"), ("db", "/var/lib/asterisk/db"),
            ("spool", "/var/spool/asterisk"), ("log", "/var/log/asterisk"), ("tmp", "/tmp"))]}]
    spec["volumes"] = [{"name": "config", "secret": {"secretName": name, "defaultMode": 0o440}}] + [
        {"name": n, "emptyDir": {"sizeLimit": "64Mi"}} for n in ("run", "db", "spool", "log", "tmp")]
    ha = site.get("home_assistant", {})
    if ha.get("enabled") or site.get("gateway", {}).get("enabled"):
        spec["containers"][0]["ports"].append({"name": "ami", "containerPort": 5038, "protocol": "TCP"})
        # Linux exposes IPv4 socket addresses in host byte order on our supported
        # amd64/arm64 nodes. Match the configured bind address, local AMI port and
        # LISTEN state, not an unrelated peer connection to remote port 5038.
        ami_address = ipaddress.IPv4Address(site["node_ipv4"]).packed[::-1].hex().upper()
        probe = spec["containers"][0]["readinessProbe"]
        probe["timeoutSeconds"] = 5
        probe["exec"]["command"][2] += (
            "; grep -Eq '^[[:space:]]*[0-9]+:[[:space:]]+"
            + ami_address + ":13AE[[:space:]]+[[:xdigit:]]{8}:[[:xdigit:]]{4}"
            "[[:space:]]+0A[[:space:]]' /proc/net/tcp"
        )
        if ha.get("assist", {}).get("enabled"):
            probe["exec"]["command"][2] += (
                "; asterisk -rx 'module show like codec_opus_open_source'"
                " | grep -Eq '^[[:space:]]*codec_opus_open_source[.]so[[:space:]].*[[:space:]][0-9]+[[:space:]]+Running([[:space:]]|$)'"
                "; asterisk -rx 'pjsip show endpoint ha-assist'"
                " | grep -Eq '^[[:space:]]*Endpoint:[[:space:]]+ha-assist[[:space:]/]'"
                "; asterisk -rx 'dialplan show 630@home'"
                " | grep -Fq 'Dial(PJSIP/ha-assist/'"
            )
    if site.get("radio", {}).get("enabled"):
        probe = spec["containers"][0]["readinessProbe"]
        probe["timeoutSeconds"] = 5
        probe["exec"]["command"][2] += (
            "; test -x /var/lib/asterisk/agi-bin/radio.py; command -v ffmpeg >/dev/null"
            "; asterisk -rx 'module show like app_audiosocket'"
            " | grep -Eq '^[[:space:]]*app_audiosocket[.]so[[:space:]].*[[:space:]][0-9]+[[:space:]]+Running([[:space:]]|$)'"
        )
    if site.get("external_sip", {}).get("enabled"):
        probe = spec["containers"][0]["readinessProbe"]
        probe["timeoutSeconds"] = 5
        probe["exec"]["command"][2] += (
            "; asterisk -rx 'module show like res_resolver_unbound'"
            " | grep -Eq '^[[:space:]]*res_resolver_unbound[.]so[[:space:]].*[[:space:]][0-9]+[[:space:]]+Running([[:space:]]|$)'"
        )
    deployment = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "asterisk", "namespace": ns},
        "spec": {"replicas": 1, "revisionHistoryLimit": 5, "strategy": {"type": "Recreate"},
                 "selector": {"matchLabels": {"app": "asterisk"}},
                 "template": {"metadata": {"labels": {"app": "asterisk", "app.kubernetes.io/part-of": "cisco-home-telephony"}}, "spec": spec}}}
    namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns,
        "labels": {"pod-security.kubernetes.io/enforce": "privileged", "pod-security.kubernetes.io/audit": "restricted",
                   "pod-security.kubernetes.io/warn": "restricted", "app.kubernetes.io/part-of": "cisco-home-telephony"}}}
    return {"apiVersion": "v1", "kind": "List", "items": [namespace, config, deployment]}


def render_tftp(site, configs, wallpaper=None):
    ns = site["namespace"]
    name, config = secret(ns, "cisco-provision", configs)
    spec = pod_base(site)
    spec["restartPolicy"] = "Never"
    spec["initContainers"] = [{"name": "stage", "image": site["tftp_image"], "command": ["/bin/sh", "-ec", """
        mkdir -p /served/root
        for f in /firmware/*; do
            [ -e "$f" ] || continue
            [ -f "$f" ] && [ ! -L "$f" ] || { echo 'Only regular firmware files are allowed' >&2; exit 1; }
            case "$f" in *.xml|*.cnf) echo 'Configuration must come from Secret' >&2; exit 1;; esac
            cp "$f" /served/root/
        done
        cp -L /config/*.xml /served/root/
        chmod 0755 /served/root
        chmod 0644 /served/root/*
        """], "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "runAsNonRoot": True,
            "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
        "volumeMounts": [{"name": "firmware", "mountPath": "/firmware", "readOnly": True},
                         {"name": "config", "mountPath": "/config", "readOnly": True}, {"name": "served", "mountPath": "/served"}]}]
    spec["securityContext"].update({"fsGroup": 10001})
    spec["containers"] = [{"name": "tftp", "image": site["tftp_image"],
        "args": ["--foreground", "--user", "tftp", "--secure", "--address", f"{site['node_ipv4']}:69",
                 "--port-range", "20000:20031", "--blocksize", "1468", "--verbose", "/srv/tftp"],
        "securityContext": {"runAsUser": 0, "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
           "capabilities": {"drop": ["ALL"], "add": ["NET_BIND_SERVICE", "SYS_CHROOT", "SETUID", "SETGID"]}},
        "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}, "limits": {"cpu": "500m", "memory": "128Mi"}},
        "ports": [{"name": "tftp", "containerPort": 69, "protocol": "UDP"}],
        "volumeMounts": [{"name": "served", "mountPath": "/srv/tftp", "subPath": "root", "readOnly": True}]}]
    spec["volumes"] = [{"name": "config", "secret": {"secretName": name, "defaultMode": 0o440}},
        {"name": "firmware", "hostPath": {"path": "/var/lib/cisco-tftp", "type": "Directory"}},
        {"name": "served", "emptyDir": {"sizeLimit": "512Mi"}}]
    items = [config]
    if wallpaper is not None:
        validate_wallpaper(wallpaper)
        wallpaper_name, wallpaper_secret = binary_secret(ns, "cisco-wallpaper", wallpaper)
        items.append(wallpaper_secret)
        spec["volumes"].append({"name": "wallpaper", "secret": {"secretName": wallpaper_name, "defaultMode": 0o440}})
        spec["initContainers"][0]["volumeMounts"].append({"name": "wallpaper", "mountPath": "/wallpaper", "readOnly": True})
        spec["initContainers"][0]["command"][2] += """
        mkdir -p /served/root/Desktops/320x212x16
        cp -L /wallpaper/List.xml /served/root/Desktops/320x212x16/List.xml
        cp -L /wallpaper/home.png /served/root/Desktops/320x212x16/home.png
        cp -L /wallpaper/home-thumb.png /served/root/Desktops/320x212x16/home-thumb.png
        chmod 0755 /served/root/Desktops /served/root/Desktops/320x212x16
        chmod 0644 /served/root/Desktops/320x212x16/List.xml /served/root/Desktops/320x212x16/home.png /served/root/Desktops/320x212x16/home-thumb.png
        """
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "cisco-tftp", "namespace": ns},
        "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 3600, "ttlSecondsAfterFinished": 600,
            "template": {"metadata": {"labels": {"app": "cisco-tftp", "app.kubernetes.io/part-of": "cisco-home-telephony"}}, "spec": spec}}}
    return {"apiVersion": "v1", "kind": "List", "items": items + [job]}


def private_write(path, content):
    path = Path(path)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("Refusing symlink for private rendered output")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, temp_name = tempfile.mkstemp(prefix=".manifest-", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(content)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, default=Path(".local/site.json"))
    parser.add_argument("--with-tftp", action="store_true")
    args = parser.parse_args()
    try:
        site = load_site(args.site, args.with_tftp)
        root = args.site.resolve().parent
        core = render_core(site, read_files(root / "asterisk", ".conf"))
        outputs = {"core.yaml": core}
        if args.with_tftp:
            outputs["tftp.yaml"] = render_tftp(site, read_files(root / "tftp", ".xml"), read_wallpaper(root / "wallpaper"))
        for filename, manifest in outputs.items():
            private_write(root / "rendered" / filename, json.dumps(manifest, indent=2) + "\n")
        print("Rendered private manifests: " + ", ".join(outputs) + ". No cluster changes.")
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Render failed: {exc}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
