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
import sys
import tempfile

RFC1918 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))

def validate_site(site, with_tftp=False):
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
    deployment = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "asterisk", "namespace": ns},
        "spec": {"replicas": 1, "revisionHistoryLimit": 5, "strategy": {"type": "Recreate"},
                 "selector": {"matchLabels": {"app": "asterisk"}},
                 "template": {"metadata": {"labels": {"app": "asterisk", "app.kubernetes.io/part-of": "cisco-home-telephony"}}, "spec": spec}}}
    namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns,
        "labels": {"pod-security.kubernetes.io/enforce": "privileged", "pod-security.kubernetes.io/audit": "restricted",
                   "pod-security.kubernetes.io/warn": "restricted", "app.kubernetes.io/part-of": "cisco-home-telephony"}}}
    return {"apiVersion": "v1", "kind": "List", "items": [namespace, config, deployment]}


def render_tftp(site, configs):
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
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "cisco-tftp", "namespace": ns},
        "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 3600, "ttlSecondsAfterFinished": 600,
            "template": {"metadata": {"labels": {"app": "cisco-tftp", "app.kubernetes.io/part-of": "cisco-home-telephony"}}, "spec": spec}}}
    return {"apiVersion": "v1", "kind": "List", "items": [config, job]}


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
            outputs["tftp.yaml"] = render_tftp(site, read_files(root / "tftp", ".xml"))
        for filename, manifest in outputs.items():
            private_write(root / "rendered" / filename, json.dumps(manifest, indent=2) + "\n")
        print("Rendered private manifests: " + ", ".join(outputs) + ". No cluster changes.")
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Render failed: {exc}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
