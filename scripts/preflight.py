#!/usr/bin/env python3
"""Local checks by default; --cluster adds strictly read-only kubectl checks."""
import argparse
import configparser
import json
import re
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET
from render_k3s import (load_site, read_files, read_wallpaper, render_core, render_tftp,
                        render_profile_tftp, profile_inputs, tftp_arguments)
from verify_firmware import verify as verify_firmware
from provision import ha_settings, phone_networks, local_networks, radio_settings, service_dialplan, gateway_settings, external_sip, alert_dialplan, intercom_dialplan, softphones, phone_services, phone_lines


def check_service_config(site, configs, inventory=None):
    expected = radio_settings(site)
    actual = json.loads(configs["radio.conf"]) if "radio.conf" in configs else None
    if expected != actual:
        raise ValueError("Radio settings differ from site.json; rerun provision.py")
    routes, _ = service_dialplan(site)
    dialplan = configs.get("extensions.conf", "")
    if routes and routes not in dialplan:
        raise ValueError("Service routes differ from site.json; rerun provision.py")
    if not expected and "AGI(radio.py," in dialplan:
        raise ValueError("Disabled radio has stale routes; rerun provision.py")
    if not site.get("alarms", {}).get("enabled", False) and "exten => 640," in dialplan:
        raise ValueError("Disabled alarms have stale routes; rerun provision.py")
    generic = {client['extension'] for client in softphones.validate(site)}
    extensions = sorted(set(re.findall(r'^\[(2[0-9]{2})\]$', configs.get('pjsip.conf', ''), re.M)) - generic)
    service_config = json.loads(configs["phone-services.conf"]) if "phone-services.conf" in configs else None
    if phone_services.enabled(site):
        owners = service_config.get("line_owners", {}) if isinstance(service_config, dict) else {}
        if (set(owners) != set(extensions) or any(primary not in owners or owners[primary] != primary
                                                for primary in owners.values())):
            raise ValueError("Phone service inventory differs from local endpoints; rerun provision.py")
        if inventory is not None:
            expected_owners = {str(line["extension"]): str(phone_lines(phone)[0]["extension"])
                               for phone in inventory["phones"] for line in phone_lines(phone)}
            if owners != expected_owners:
                raise ValueError("Phone service ownership differs from inventory; rerun provision.py")
        if service_config != phone_services.settings(site, owners):
            raise ValueError("Phone service inputs differ from generated config; rerun provision.py")
    elif service_config is not None or "AGI(phone_services.py)" in dialplan or "exten => 641," in dialplan:
        raise ValueError("Disabled phone services have stale configuration; rerun provision.py")
    group_candidates = ([str(phone_lines(phone)[0]["extension"]) for phone in inventory["phones"]]
                        if inventory is not None else extensions) + list(generic)
    if phone_services.group_settings(site, group_candidates) is None and "exten => 621," in dialplan:
        raise ValueError("Disabled inbound ring group has a stale route; rerun provision.py")
    for number, feature in (("621", bool(phone_services.group_settings(site))),
                            ("641", phone_services.enabled(site))):
        if feature and len(re.findall(r"^exten => " + number + r",1,", dialplan, re.M)) != 1:
            raise ValueError("Phone service route is missing or duplicated; rerun provision.py")
    alerts = alert_dialplan(site, extensions)
    if alerts and dialplan.count(alerts) != 1:
        raise ValueError('Alert helper differs from inventoried endpoints; rerun provision.py')
    remainder = dialplan.replace(alerts, '', 1) if alerts else dialplan
    if re.search(r'^\[cisco-alert(?:-answered)?\]', remainder, re.M | re.I):
        raise ValueError('Stale or duplicate alert context; rerun provision.py')
    if re.search(r'^\s*include\s*=>\s*cisco-alert\b', dialplan, re.M | re.I):
        raise ValueError('Alert context must not be reachable through a dialplan include')
    intercom = intercom_dialplan(site, extensions)
    intercom_contexts = re.findall(r'^\[cisco-intercom\][^\n]*\n.*?(?=^\[|\Z)', dialplan, re.M | re.S | re.I)
    if intercom and (len(intercom_contexts) != 1 or intercom_contexts[0].strip() != intercom.strip()):
        raise ValueError('Intercom helper differs from inventoried endpoints; rerun provision.py')
    if not intercom and intercom_contexts:
        raise ValueError('Stale or duplicate intercom context; rerun provision.py')
    if re.search(r'^\s*include\s*=>\s*cisco-intercom\b', dialplan, re.M | re.I):
        raise ValueError('Intercom context must not be reachable through a dialplan include')


def check_phone_config(site, configs):
    """Do not deploy stale phone permissions after changing the source allowlist."""
    expected = phone_networks(site)
    external = external_sip.object_names(site)
    external_sip.check(site, configs)
    generic = softphones.check(site, configs)
    sections, current = [], None
    for line in configs.get("pjsip.conf", "").splitlines():
        line = line.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = {"name": line[1:-1]}
            sections.append(current)
        elif current is not None and "=" in line:
            key, value = (part.strip() for part in line.split("=", 1))
            current.setdefault(key, []).append(value)
    transports = set()
    for section in sections:
        if section.get("type") == ["transport"]:
            if section["name"] in external["transports"]:
                continue
            transports.add(section["name"])
            if section.get("local_net") != local_networks(site):
                raise ValueError("PJSIP local networks differ from site.json; rerun provision.py")
        if (section.get("type") == ["endpoint"] and section["name"] != "ha-assist"
                and section["name"] not in external["endpoints"] and section["name"] not in generic):
            if section.get("deny") != ["0.0.0.0/0"] or section.get("permit") != expected:
                raise ValueError("Phone source ACL differs from site.json; rerun provision.py")
    if transports != {"transport-udp", "transport-tcp"}:
        raise ValueError("PJSIP transports differ from generated configuration; rerun provision.py")


def check_ha_config(site, configs):
    """Reject stale management credentials/ACLs after an operator edits site.json."""
    ha = ha_settings(site)
    gateway = gateway_settings(site)
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(configs.get("manager.conf", "[general]\nenabled=no\n"))
        enabled = parser.getboolean("general", "enabled", fallback=False)
        if enabled != bool(ha or gateway):
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
        if bool(gateway) != parser.has_section("cisco_gateway"):
            raise ValueError("Gateway enabled state differs from site.json; rerun provision.py")
        if gateway:
            expected = {"permit": f"{gateway['client']}/255.255.255.255", "deny": "0.0.0.0/0.0.0.0",
                        "read": "system,call", "write": "system,call,originate"}
            if any(parser.get("cisco_gateway", k) != v for k, v in expected.items()):
                raise ValueError("Gateway AMI settings differ from site.json; rerun provision.py")
            if parser.get("general", "bindaddr") != site["node_ipv4"] or parser.get("general", "port") != "5038":
                raise ValueError("Gateway AMI listener differs from site.json; rerun provision.py")
            for extension in re.findall(r"^exten => (2[0-9]{2}),1,", actual, re.M):
                if f'exten => {extension},1,GotoIf($["${{DB(cisco-dnd/{extension})}}" = "1"]?dnd)' not in actual:
                    raise ValueError("Gateway DND routes differ from site.json; rerun provision.py")
        elif "DB(cisco-dnd/" in actual:
            raise ValueError("Disabled gateway has stale DND routes; rerun provision.py")
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
    tftp_arguments(p)
    args = p.parse_args()
    try:
        site = load_site(args.site, args.with_tftp or args.tftp_only or args.profiles_only)
        root = args.site.resolve().parent
        if args.phone_addresses and not args.profiles_only:
            raise ValueError("--phone-addresses requires --profiles-only")
        if args.profiles_only:
            inventory, profiles = profile_inputs(root, site, args.phone_addresses)
            render_profile_tftp(site, profiles, inventory, read_wallpaper(root / "wallpaper"))
            print("Profile-only file allowlist, inventory and fixed IPv4 policy validated; no firmware or PBX inputs read.")
        if not (args.profiles_only or args.tftp_only):
            configs = read_files(root / "asterisk", ".conf")
            check_phone_config(site, configs)
            check_ha_config(site, configs)
            inventory = (json.loads((root / "phones.json").read_text())
                         if phone_services.enabled(site) or phone_services.group_settings(site) else None)
            check_service_config(site, configs, inventory)
            render_core(site, configs)
        if args.with_tftp or args.tftp_only:
            xmls = read_files(root / "tftp", ".xml")
            render_tftp(site, xmls, read_wallpaper(root / "wallpaper"))
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
