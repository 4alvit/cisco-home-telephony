#!/usr/bin/env python3
"""Local checks by default; --cluster adds strictly read-only kubectl checks."""
import argparse
import configparser
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET
from render_k3s import load_site, read_files, render_core, render_tftp
from verify_firmware import verify as verify_firmware
from provision import ha_settings


def check_ha_config(site, configs):
    """Reject stale management credentials/ACLs after an operator edits site.json."""
    ha = ha_settings(site)
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(configs.get("manager.conf", "[general]\nenabled=no\n"))
        enabled = parser.getboolean("general", "enabled", fallback=False)
        if enabled != bool(ha):
            raise ValueError("AMI enabled state differs from site.json; rerun provision.py")
        if ha:
            expected = {"permit": f"{ha['client']}/255.255.255.255", "deny": "0.0.0.0/0.0.0.0",
                        "read": "call,dtmf", "write": "system,originate" if ha["originate"] else "system"}
            if parser.get("general", "bindaddr") != site["node_ipv4"] or parser.get("general", "port") != "5038":
                raise ValueError("AMI listener differs from site.json; rerun provision.py")
            if any(parser.get("homeassistant", k) != v for k, v in expected.items()):
                raise ValueError("AMI permissions or client ACL differ from site.json; rerun provision.py")
        assist = ha.get("assist") if ha else None
        route = "Dial(PJSIP/ha-assist/sip:630@"
        actual = configs.get("extensions.conf", "")
        if bool(assist) != (route in actual):
            raise ValueError("Assist route differs from site.json; rerun provision.py")
        if assist and f"{route}{assist['host']}:{assist['port']},60)" not in actual:
            raise ValueError("Assist destination differs from site.json; rerun provision.py")
    except configparser.Error:
        raise ValueError("Invalid manager.conf; rerun provision.py") from None


def inspect_cluster(site):
    base = ["kubectl", "--context", site["kube_context"], "--request-timeout=15s"]
    result = subprocess.run(base + ["get", "node", site["node_name"], "-o", "json"], check=True, capture_output=True, text=True)
    node = json.loads(result.stdout)
    conditions = {c["type"]: c["status"] for c in node["status"].get("conditions", [])}
    if conditions.get("Ready") != "True":
        raise ValueError("Selected node is not Ready")
    if any(conditions.get(c) == "True" for c in ("MemoryPressure", "DiskPressure", "PIDPressure")):
        raise ValueError("Selected node is under resource pressure")
    if node.get("spec", {}).get("unschedulable"):
        raise ValueError("Selected node is cordoned")
    for taint in node.get("spec", {}).get("taints", []):
        if taint.get("effect") in ("NoSchedule", "NoExecute"):
            raise ValueError("Selected node has a blocking taint; choose/review the node, do not remove a taint automatically")
    addresses = {a["address"] for a in node["status"].get("addresses", []) if a["type"] == "InternalIP"}
    if site["node_ipv4"] not in addresses:
        raise ValueError("node_ipv4 does not match a reported node InternalIP")
    info = node["status"]["nodeInfo"]
    if info["operatingSystem"] != "linux" or info["architecture"] not in ("amd64", "arm64"):
        raise ValueError("Node must be Linux amd64/arm64; build the matching image architecture")
    pods = subprocess.run(base + ["get", "pods", "-A", "--field-selector", f"spec.nodeName={site['node_name']}", "-o", "json"],
                          check=True, capture_output=True, text=True)
    other_host_pods = [p["metadata"]["name"] for p in json.loads(pods.stdout)["items"] if p["spec"].get("hostNetwork")
                       and p["metadata"].get("namespace") != site["namespace"]]
    print(f"Node checked: {site['node_name']} {site['node_ipv4']} {info['architecture']} {info['kubeletVersion']}")
    if other_host_pods:
        print("Other hostNetwork workloads exist; operator must check their sockets before deploy.")
    print("Cluster checks passed. Firewall rules and host socket availability require the node-side checks in docs/k3s-deployment.md.")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--site", type=Path, default=Path(".local/site.json"))
    p.add_argument("--cluster", action="store_true")
    p.add_argument("--with-tftp", action="store_true")
    args = p.parse_args()
    try:
        site = load_site(args.site, args.with_tftp)
        root = args.site.resolve().parent
        configs = read_files(root / "asterisk", ".conf")
        check_ha_config(site, configs)
        render_core(site, configs)
        if args.with_tftp:
            xmls = read_files(root / "tftp", ".xml")
            render_tftp(site, xmls)
            _, load_id = verify_firmware(root / "firmware", root / "firmware-manifest.json")
            inventory = json.loads((root / "phones.json").read_text())
            phones = inventory["phones"]
            if not phones or any(p.get("verified") is not True or p.get("model") != "CP-7945G" or p.get("firmware_load") != load_id for p in phones):
                raise ValueError("Verified physical phone inventory must match the reviewed firmware load")
            expected = {"SEP" + p["mac"].replace(":", "").replace("-", "").upper() + ".cnf.xml" for p in phones}
            actual = {name for name in xmls if name.startswith("SEP") and name.endswith(".cnf.xml")}
            if expected != actual:
                raise ValueError("Generated phone XML set does not match inventory; rerun provision.py")
            for name in expected:
                if ET.fromstring(xmls[name]).findtext("loadInformation") != load_id:
                    raise ValueError("Phone XML loadInformation does not match verified firmware")
            print("TFTP firmware checksums, inventory and generated load IDs match.")
        print(f"Local configuration valid: context={site['kube_context']} namespace={site['namespace']} node={site['node_name']}.")
        if args.cluster:
            inspect_cluster(site)
    except (ValueError, OSError, KeyError, TypeError, ET.ParseError, subprocess.CalledProcessError) as exc:
        # kubectl diagnostics can contain sensitive response text; don't echo captured output.
        print(f"Preflight failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
