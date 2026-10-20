#!/usr/bin/env python3
"""Generate Cisco 7945G/PBX configuration, or explicitly bootstrap only the PBX."""
import argparse
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import string
import sys
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

_radio_spec = importlib.util.spec_from_file_location(
    "cisco_radio", Path(__file__).resolve().parents[1] / "containers/asterisk/radio.py")
_radio = importlib.util.module_from_spec(_radio_spec)
_radio_spec.loader.exec_module(_radio)
radio_settings = _radio.settings
_services_spec = importlib.util.spec_from_file_location(
    "cisco_phone_services", Path(__file__).resolve().parents[1] / "containers/asterisk/phone_services.py")
phone_services = importlib.util.module_from_spec(_services_spec)
_services_spec.loader.exec_module(phone_services)
_external_spec = importlib.util.spec_from_file_location(
    "cisco_external_sip", Path(__file__).resolve().with_name("external_sip.py"))
external_sip = importlib.util.module_from_spec(_external_spec)
_external_spec.loader.exec_module(external_sip)
_softphone_spec = importlib.util.spec_from_file_location(
    "cisco_softphones", Path(__file__).resolve().with_name("softphones.py"))
softphones = importlib.util.module_from_spec(_softphone_spec)
_softphone_spec.loader.exec_module(softphones)


def alarm_settings(site):
    alarm = site.get("alarms", {})
    if (not isinstance(alarm, dict) or set(alarm) - {"enabled", "duration_seconds"}
            or type(alarm.get("enabled", False)) is not bool):
        raise ValueError("alarms accepts boolean enabled and duration_seconds")
    if not alarm.get("enabled", False):
        return None
    duration = alarm.get("duration_seconds", 15)
    if type(duration) is not int or not 1 <= duration <= 30:
        raise ValueError("alarms.duration_seconds must be 1..30")
    return {"enabled": True, "duration_seconds": duration}


def service_dialplan(site):
    """Return optional service routes without introducing outbound dialing."""
    radio, alarm = radio_settings(site), alarm_settings(site)
    routes, numbers = [], []
    if alarm:
        numbers.append("640")
        routes.append(f'''exten => 640,1,Answer()
 same => n,Set(TIMEOUT(absolute)=35)
 same => n,PlayTones(880/250,0/750)
 same => n,Wait({alarm['duration_seconds']})
 same => n,StopPlayTones()
 same => n,Hangup()
''')
    if radio:
        for station in radio["stations"]:
            number = _radio.catalog()[station]["extension"]
            numbers.append(number)
            routes.append(f'''exten => {number},1,Answer()
 same => n,Set(TIMEOUT(absolute)={radio['max_seconds'] + 30})
 same => n,Set(RADIOSTATUS=failed)
 same => n,AGI(radio.py,{station})
 same => n,GotoIf($["${{RADIOSTATUS}}" = "ok"]?done)
 same => n,PlayTones(480+620/250,0/250)
 same => n,Wait(2)
 same => n,StopPlayTones()
 same => n(done),Hangup()
''')
    if phone_services.enabled(site):
        routes.append(phone_services.dialplan(site))
        numbers.append("641")
    if phone_services.group_settings(site):
        routes.append(phone_services.group_dialplan(site))
        numbers.append("621")
    return ''.join(routes), numbers


def alert_dialplan(site, extensions):
    """Private AMI-only alert context; no timeout, forwarding, or answer audio.

    Originate answers only the Local leg, whose companion waits in WaitForSignal.
    Dial's omitted timeout keeps the physical phone ringing until it terminates
    the call or the operator hangs up the exact owned channel. The U subroutine
    aborts both legs before bridging on manual answer. Neither context is included
    in home. https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/Dialplan_Applications/Dial/
    """
    if not gateway_settings(site) or not extensions:
        return ''
    extensions = sorted(set(extensions))
    if any(not isinstance(ext, str) or not re.fullmatch(r'2[0-9]{2}', ext) for ext in extensions):
        raise ValueError('alert destinations must be inventoried local extensions')
    parts = ['\n[cisco-alert]\n']
    for extension in extensions:
        parts.append(f'''exten => {extension},1,GotoIf($[${{REGEX("^[0-9a-f]{{32}}$" ${{CISCO_ALERT_ID}})}} = 1]?ready:reject)
 same => n(ready),Answer()
 same => n,Dial(PJSIP/{extension},,iU(cisco-alert-answered))
 same => n,Hangup()
 same => n(reject),Hangup(21)
''')
    parts.append('''
[cisco-alert-answered]
exten => s,1,Set(GOSUB_RESULT=ABORT)
 same => n,Return()
''')
    return ''.join(parts)


def intercom_dialplan(site, extensions):
    """AMI-only source legs: never follow a phone's SIP redirect."""
    if not gateway_settings(site) or not extensions:
        return ''
    extensions = sorted(set(extensions))
    if any(not isinstance(ext, str) or not re.fullmatch(r'2[0-9]{2}', ext) for ext in extensions):
        raise ValueError('intercom sources must be inventoried Cisco extensions')
    parts = ['\n[cisco-intercom]\n']
    for extension in extensions:
        parts.append(f'''exten => {extension},1,Dial(PJSIP/{extension},8,i)
 same => n,Hangup()
''')
    return ''.join(parts)


def read_json(path):
    return json.loads(Path(path).read_text())


def private_write(path, text):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f"refusing symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(text)
    path.chmod(0o600)


def phone_networks(site):
    """Phone source ACLs override the PBX LAN; they never implicitly widen it."""
    values = site.get("phone_cidrs", [site["lan_cidr"]])
    if not isinstance(values, list) or not 1 <= len(values) <= 50:
        raise ValueError("phone_cidrs must be a nonempty list of at most 50 IPv4 CIDRs")
    private = tuple(ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
    networks = []
    for value in values:
        if not isinstance(value, str) or "/" not in value:
            raise ValueError("phone_cidrs entries must be explicit IPv4 CIDRs")
        net = ipaddress.IPv4Network(value, strict=True)
        # Match the existing isolated smoke-test exception, never a deployed LAN.
        loopback_test = ipaddress.IPv4Address(site["node_ipv4"]).is_loopback and net.subnet_of(ipaddress.IPv4Network("127.0.0.0/8"))
        if not any(net.subnet_of(n) for n in private) and not loopback_test:
            raise ValueError("phone_cidrs must contain private IPv4 networks")
        if any(net.overlaps(previous) for previous in networks):
            raise ValueError("phone_cidrs must not contain duplicate or overlapping networks")
        networks.append(net)
    return [str(net) for net in networks]


def local_networks(site):
    """NAT classification is distinct from the endpoint source allowlist."""
    return list(dict.fromkeys([site["lan_cidr"], *phone_networks(site), *softphones.networks(site)]))


def ha_settings(site):
    """Opt-in HA management; actual client source IP must be explicitly supplied."""
    ha = site.get("home_assistant", {})
    if not isinstance(ha, dict) or type(ha.get("enabled", False)) is not bool:
        raise ValueError("home_assistant must be an object with boolean enabled")
    if not ha.get("enabled", False):
        return None
    if type(ha.get("allow_originate", False)) is not bool:
        raise ValueError("home_assistant.allow_originate must be boolean")
    def address(value):
        addr = ipaddress.IPv4Address(value)
        nets = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        # Loopback is only allowed for isolated runtime tests on loopback PBX.
        if not any(addr in ipaddress.IPv4Network(n) for n in nets) and not (
            addr.is_loopback and ipaddress.IPv4Address(site["node_ipv4"]).is_loopback
        ):
            raise ValueError("HA addresses must be private unicast IPv4 addresses")
        return str(addr)
    result = {"client": address(ha["ami_client_ipv4"]),
              "originate": ha.get("allow_originate", False)}
    assist = ha.get("assist", {})
    if not isinstance(assist, dict) or type(assist.get("enabled", False)) is not bool:
        raise ValueError("home_assistant.assist.enabled must be boolean")
    if assist.get("enabled", False):
        host = address(assist["host_ipv4"])
        port = assist.get("sip_port", 5060)
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("Assist sip_port must be 1024..65535")
        if host == site["node_ipv4"] and port == 5060:
            raise ValueError("HA VoIP cannot share the Asterisk IP and SIP port")
        result["assist"] = {"host": host, "port": port}
    return result


def manager_config(ha, ip, out):
    if ha is None:
        return "[general]\nenabled=no\nwebenabled=no\n"
    secret_path = out / "ha-ami-secret.json"
    if secret_path.is_symlink():
        raise ValueError("HA AMI secret cannot be a symlink")
    saved = read_json(secret_path) if secret_path.exists() else {
        "username": "homeassistant", "secret": secrets.token_hex(24)}
    if saved.get("username") != "homeassistant" or not re.fullmatch(r"[A-Za-z0-9]{40,64}", saved.get("secret", "")):
        raise ValueError("Invalid HA AMI secret; preserve or rotate it explicitly")
    private_write(secret_path, json.dumps(saved, indent=2) + "\n")
    permissions = "system,originate" if ha["originate"] else "system"
    return f"""[general]
enabled=yes
webenabled=no
port=5038
bindaddr={ip}
displayconnects=no

[homeassistant]
secret={saved['secret']}
deny=0.0.0.0/0.0.0.0
permit={ha['client']}/255.255.255.255
read=call,dtmf
write={permissions}
writetimeout=5000
"""


def gateway_settings(site):
    cfg = site.get("gateway", {})
    if (not isinstance(cfg, dict) or set(cfg) - {"enabled", "ami_client_ipv4"}
            or type(cfg.get("enabled", False)) is not bool):
        raise ValueError("gateway accepts boolean enabled and ami_client_ipv4")
    if not cfg.get("enabled", False):
        return None
    addr = ipaddress.IPv4Address(cfg["ami_client_ipv4"])
    private = any(addr in ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
    if not private and not (addr.is_loopback and ipaddress.IPv4Address(site["node_ipv4"]).is_loopback):
        raise ValueError("gateway AMI client must be a private unicast IPv4 address")
    return {"client": str(addr)}


def gateway_manager(config, site, out):
    gateway = gateway_settings(site)
    if gateway is None:
        return config
    path = out / "gateway-ami-secret.json"
    if path.is_symlink():
        raise ValueError("gateway AMI secret cannot be a symlink")
    saved = read_json(path) if path.exists() else {"username": "cisco_gateway", "secret": secrets.token_hex(24)}
    if (not isinstance(saved, dict) or saved.get("username") != "cisco_gateway"
            or not isinstance(saved.get("secret"), str) or not re.fullmatch(r"[A-Za-z0-9]{40,64}", saved["secret"])):
        raise ValueError("Invalid gateway AMI secret; preserve or rotate it explicitly")
    private_write(path, json.dumps(saved, indent=2) + '\n')
    if ha_settings(site) is None:
        config = f"[general]\nenabled=yes\nwebenabled=no\nport=5038\nbindaddr={site['node_ipv4']}\ndisplayconnects=no\n"
    return config + f"""
[cisco_gateway]
secret={saved['secret']}
deny=0.0.0.0/0.0.0.0
permit={gateway['client']}/255.255.255.255
read=system,call
write=system,call,originate
writetimeout=5000
"""


def phone_lines(phone):
    """Return ordered line definitions; legacy extension occupies button 1."""
    if "lines" in phone:
        return phone["lines"]
    return [{"extension": phone["extension"], "label": phone["label"]}]


def private_phone_url(value, setting="authentication_url"):
    """Limit phone HTTP services to explicit private IPv4 hosts and plain paths."""
    if not isinstance(value, str) or len(value) > 255 or not value.isascii():
        raise ValueError(f"{setting} must be an ASCII private IPv4 HTTP(S) URL")
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{setting} cannot contain whitespace or control characters")
    try:
        parsed = urlsplit(value)
        address = ipaddress.IPv4Address(parsed.hostname)
        port = parsed.port
    except (ValueError, TypeError):
        raise ValueError(f"{setting} requires a private IPv4 literal and valid port") from None
    private = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    if (parsed.scheme not in ("http", "https") or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or "?" in value or "#" in value or port == 0
            or not any(address in ipaddress.IPv4Network(net) for net in private)
            or not re.fullmatch(r"/[A-Za-z0-9_/-]{1,255}", parsed.path)
            or "//" in parsed.path):
        raise ValueError(f"{setting} requires a private HTTP(S) host and plain absolute path without credentials/query/fragment")
    return value


def authentication_url(value):
    """Backward-compatible validator for the phone authentication callback."""
    return private_phone_url(value)


def phone_management_secrets(phones, out):
    """Preserve separate per-device SSH credentials only when SSH is enabled."""
    enabled = [phone for phone in phones if phone.get("ssh_enabled", False)]
    if not enabled:
        return {}
    path = out / "phone-management-secrets.json"
    if path.is_symlink():
        raise ValueError("phone management secrets cannot be a symlink")
    saved = read_json(path) if path.exists() else {}
    if not isinstance(saved, dict):
        raise ValueError("invalid phone management secrets")
    for phone in enabled:
        device = "SEP" + phone["mac"]
        if device not in saved:
            saved[device] = {
                "ssh_username": "u" + secrets.token_hex(5),
                "ssh_password": ''.join(secrets.choice(string.ascii_letters + string.digits)
                                        for _ in range(16)),
            }
        entry = saved[device]
        if (not isinstance(entry, dict) or set(entry) != {"ssh_username", "ssh_password"}
                or not isinstance(entry["ssh_username"], str)
                or not re.fullmatch(r"[a-z][A-Za-z0-9]{7,15}", entry["ssh_username"])
                or not isinstance(entry["ssh_password"], str)
                or not re.fullmatch(r"[A-Za-z0-9]{16}", entry["ssh_password"])):
            raise ValueError("invalid stored SSH credentials; preserve or rotate explicitly")
    private_write(path, json.dumps(saved, indent=2) + '\n')
    return saved


def validate(site, inventory, *, pbx_only=False):
    ha_settings(site)
    radio_settings(site)
    alarm_settings(site)
    gateway_settings(site)
    phone_services.enabled(site)
    phone_services.group_settings(site)
    external = external_sip.validate(site)
    softphones.validate(site)
    phone_networks(site)
    ip = ipaddress.IPv4Address(site["node_ipv4"])
    net = ipaddress.IPv4Network(site["lan_cidr"], strict=True)
    if ip not in net or ip in (net.network_address, net.broadcast_address):
        raise ValueError("node_ipv4 must be a usable address in lan_cidr")
    if pbx_only:
        if inventory is not None:
            raise ValueError("PBX-only mode does not accept a phone inventory")
        if external and any(account["inbound_target"] for account in external["accounts"]):
            raise ValueError("External SIP inbound needs a verified local phone inventory")
        return [], None
    ntp = str(ipaddress.IPv4Address(site["ntp_ipv4"]))
    phones = inventory["phones"]
    if not isinstance(phones, list) or not 1 <= len(phones) <= 50:
        raise ValueError("inventory must contain 1..50 phones")
    seen_mac, seen_ext = set(), set()
    normalized = []
    for phone in phones:
        if not isinstance(phone, dict):
            raise ValueError("each phone must be an object")
        if phone.get("verified") is not True or phone.get("model") != "CP-7945G":
            raise ValueError("each phone requires physical model/MAC verification: CP-7945G, verified:true")
        mac = re.sub(r"[:-]", "", phone["mac"]).upper()
        if not re.fullmatch(r"[0-9A-F]{12}", mac) or int(mac[:2], 16) & 1 or mac == "000000000000":
            raise ValueError("invalid unicast MAC")
        if mac in seen_mac:
            raise ValueError("duplicate MAC")
        seen_mac.add(mac)
        if not isinstance(phone.get("label"), str) or not re.fullmatch(r"[A-Za-z0-9 _-]{1,24}", phone["label"]):
            raise ValueError("phone label must be 1..24 ASCII letters/digits/spaces/_/-")
        if "room" in phone:
            softphones.room_name(phone["room"])
        if "extension" in phone and "lines" in phone:
            raise ValueError("use either legacy extension or lines, not both")
        if "extension" not in phone and "lines" not in phone:
            raise ValueError("each phone requires extension or lines")
        lines = phone_lines(phone)
        if not isinstance(lines, list) or not 1 <= len(lines) <= 2:
            raise ValueError("CP-7945G requires 1..2 lines, ordered by button")
        normalized_lines = []
        for line in lines:
            if not isinstance(line, dict) or set(line) != {"extension", "label"}:
                raise ValueError("each line requires exactly extension and label")
            ext = str(line["extension"])
            if not re.fullmatch(r"2[0-9]{2}", ext):
                raise ValueError("extensions must be in 200..299")
            if ext in seen_ext:
                raise ValueError("duplicate extension across phone lines")
            seen_ext.add(ext)
            label = line["label"]
            if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9 _-]{1,24}", label):
                raise ValueError("line label must be 1..24 ASCII letters/digits/spaces/_/-")
            normalized_lines.append({"extension": ext, "label": label})
        if phone.get("transport", "tcp") not in ("udp", "tcp"):
            raise ValueError("transport must be udp or tcp")
        if type(phone.get("web_access", False)) is not bool:
            raise ValueError("web_access must be boolean")
        if "background_image_access" in phone and type(phone["background_image_access"]) is not bool:
            raise ValueError("background_image_access must be boolean")
        if "authentication_url" in phone:
            authentication_url(phone["authentication_url"])
        for setting in ("services_url", "directory_url"):
            if setting in phone:
                private_phone_url(phone[setting], setting)
        for setting in ("ssh_enabled", "phone_personalization"):
            if setting in phone and type(phone[setting]) is not bool:
                raise ValueError(f"{setting} must be boolean")
        if phone.get("phone_personalization", False) and not (
                phone.get("web_access", False) and phone.get("authentication_url")):
            raise ValueError("phone_personalization requires web_access and authentication_url")
        load = phone.get("firmware_load", "")
        if not re.fullmatch(r"SIP45\.[A-Za-z0-9-]+", load):
            raise ValueError("firmware_load must be the exact SIP45.* load ID, without .loads")
        copy = dict(phone, mac=mac)
        if "lines" in phone:
            copy["lines"] = normalized_lines
        else:
            copy["extension"] = normalized_lines[0]["extension"]
        normalized.append(copy)
    if external and any(account["inbound_target"] and account["inbound_target"] not in seen_ext | {"621"}
                        for account in external["accounts"]):
        raise ValueError("External SIP inbound target must be a verified local phone line")
    clients = softphones.validate(site, seen_ext)
    phone_services.group_settings(site, [phone_lines(p)[0]["extension"] for p in normalized]
                                  + [client["extension"] for client in clients])
    return normalized, ntp


def sub(parent, tag, value=None):
    node = ET.SubElement(parent, tag)
    if value is not None:
        node.text = str(value)
    return node


def phone_xml(phone, ip, ntp, passwords, management=None):
    d = ET.Element("device")
    sub(d, "fullConfig", "true")
    sub(d, "deviceProtocol", "SIP")
    pool = sub(d, "devicePool")
    dt = sub(pool, "dateTimeSetting")
    sub(dt, "dateTemplate", "M/D/YA")
    sub(dt, "timeZone", "Pacific Standard/Daylight Time")
    n = sub(sub(dt, "ntps"), "ntp")
    sub(n, "name", ntp)
    sub(n, "ntpMode", "unicast")
    group = sub(pool, "callManagerGroup")
    members = sub(group, "members")
    member = ET.SubElement(members, "member", {"priority": "0"})
    cm = sub(member, "callManager")
    ports = sub(cm, "ports")
    sub(ports, "ethernetPhonePort", 2000)
    sub(ports, "sipPort", 5060)
    sub(ports, "securedSipPort", 5061)
    sub(cm, "processNodeName", ip)
    profile = sub(d, "sipProfile")
    proxies = sub(profile, "sipProxies")
    for k, v in {"backupProxy": "", "backupProxyPort":5060,"emergencyProxy":"","emergencyProxyPort":5060,"outboundProxy":"","outboundProxyPort":5060,"registerWithProxy":"true"}.items():
        sub(proxies,k,v)
    features=sub(profile, "sipCallFeatures")
    sub(features,"remoteCcEnable","true")
    stack=sub(profile, "sipStack")
    sub(stack,"remotePartyID","true")
    for k,v in {"sipInviteRetx":6,"sipRetx":10,"timerRegisterExpires":300,"timerRegisterDelta":5,"timerInviteExpires":180,"timerKeepAliveExpires":120,"timerT1":500,"timerT2":4000}.items():
        sub(stack,k,v)
    # Retain the known SIP45 preferredCodec token. G.722 is enabled below and
    # preferred by Asterisk's local offer policy, without guessing a new token.
    for k,v in {"enableVad":"false","natEnabled":"false","voipControlPort":5060,"preferredCodec":"g711ulaw","dtmfAvtPayload":101,"dtmfOutofBand":"avt","dtmfDbLevel":3,"phoneLabel":phone["label"].replace(" ","_")[:12],"stutterMsgWaiting":0,"callStats":"false","silentPeriodBetweenCallWaitingBursts":10,"disableLocalSpeedDialConfig":"false","startMediaPort":16384,"stopMediaPort":16482}.items():
        sub(profile,k,v)
    sip_lines = sub(profile,"sipLines")
    for button, definition in enumerate(phone_lines(phone), start=1):
        ext, label = definition["extension"], definition["label"]
        line = ET.SubElement(sip_lines, "line", {"button":str(button)})
        for k,v in {"featureID":9,"featureLabel":label,"proxy":"USECALLMANAGER","port":5060,"name":ext,"displayName":label,"autoAnswer":None}.items():
            sub(line,k,v)
        sub(line.find("autoAnswer"),"autoAnswerEnabled",0)
        for k,v in {"callWaiting":3,"authName":ext,"authPassword":passwords[ext],"sharedLine":"false","messageWaitingLampPolicy":1,"messagesNumber":"","ringSettingIdle":4,"ringSettingActive":5,"contact":ext}.items():
            sub(line,k,v)
    sub(profile,"dialTemplate","dialplan.xml")
    common_profile = sub(d,"commonProfile")
    if "background_image_access" in phone:
        sub(common_profile,"backgroundImageAccess",str(phone["background_image_access"]).lower())
    sub(d,"loadInformation",phone["firmware_load"])
    vendor=sub(d,"vendorConfig")
    sub(vendor,"webAccess",0 if phone.get("web_access",False) else 1)
    sub(vendor,"sshAccess",0 if phone.get("ssh_enabled",False) else 1)
    sub(vendor,"settingsAccess",1)
    # Cisco service-phone reference: 0=default, 1=disabled, 2=enabled.
    # https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cucme/command/reference/cme_cr/cme_s1ht.html
    sub(vendor,"g722CodecSupport",2)
    sub(d,"deviceSecurityMode",1)
    sub(d,"transportLayerProtocol",2 if phone.get("transport","tcp")=="udp" else 1)
    sub(d,"certHash","")
    sub(d,"encrConfig","false")
    if "authentication_url" in phone:
        sub(d,"authenticationURL",phone["authentication_url"])
    for setting, tag in (("services_url", "servicesURL"), ("directory_url", "directoryURL")):
        if setting in phone:
            sub(d, tag, phone[setting])
    if "services_url" in phone or "directory_url" in phone:
        # Internal service entries are explicit, so the firmware need not infer
        # an external URL from its default Service Provisioning policy.
        services = sub(d, "phoneServices")
        sub(services, "provisioning", 0)
        entries = [("1", "Missed Calls", "Application:Cisco/MissedCalls"),
                   ("1", "Received Calls", "Application:Cisco/ReceivedCalls"),
                   ("1", "Placed Calls", "Application:Cisco/PlacedCalls")]
        if "services_url" in phone:
            entries.append(("0", "Home Apps", phone["services_url"]))
        if "directory_url" in phone:
            entries.append(("1", "Phone Directory", phone["directory_url"]))
        for kind, name, url in entries:
            service = ET.SubElement(services, "phoneService", {"type": kind, "category": "0"})
            for tag, value in (("name", name), ("url", url), ("vendor", ""), ("version", "")):
                sub(service, tag, value)
    if "phone_personalization" in phone:
        sub(d,"phonePersonalization",1 if phone["phone_personalization"] else 0)
    if phone.get("ssh_enabled",False):
        if management is None:
            raise ValueError("SSH-enabled phone requires distinct management credentials")
        sub(d,"sshUserId",management["ssh_username"])
        sub(d,"sshPassword",management["ssh_password"])
    ET.indent(d)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(d,encoding="unicode")+'\n'


def generate(site, inventory, out, *, pbx_only=False):
    phones,ntp = validate(site,inventory,pbx_only=pbx_only)
    # Resolve private provider credentials before any output is modified.
    external = external_sip.generate(site)
    clients = softphones.validate(site)
    line_owners = {line["extension"]: phone_lines(p)[0]["extension"] for p in phones for line in phone_lines(p)}
    services = phone_services.settings(site, line_owners)
    phone_services.group_settings(site, [phone_lines(p)[0]["extension"] for p in phones]
                                  + [client["extension"] for client in clients])
    out=Path(out)
    if pbx_only and any((out/name).exists() or (out/name).is_symlink()
                        for name in ("tftp", "secrets.json")):
        raise ValueError("PBX-only mode requires an output without phone artifacts; use a separate directory")
    out.mkdir(parents=True,exist_ok=True,mode=0o700)
    if out.is_symlink():
        raise ValueError("output root cannot be a symlink")
    out.chmod(0o700)
    for folder in (("asterisk",) if pbx_only else ("asterisk","tftp")):
        p=out/folder
        if p.is_symlink():
            raise ValueError("output directories cannot be symlinks")
        p.mkdir(exist_ok=True,mode=0o700)
        p.chmod(0o700)
    secret_path=out/"secrets.json"
    passwords=read_json(secret_path) if secret_path.exists() else {}
    for p in phones:
        for line in phone_lines(p):
            ext = line["extension"]
            passwords.setdefault(ext,secrets.token_hex(15))
            if not isinstance(passwords[ext], str) or not re.fullmatch(r"[A-Za-z0-9]{24,30}", passwords[ext]):
                raise ValueError("secret must be 24..30 alphanumeric characters")
    client_passwords = softphones.prepare_secrets(clients, out, passwords)
    if not pbx_only:
        private_write(secret_path,json.dumps(passwords,indent=2)+'\n')
    if client_passwords is not None:
        private_write(out/"softphone-secrets.json", json.dumps(client_passwords, indent=2)+'\n')
    management = phone_management_secrets(phones, out)
    ip=site["node_ipv4"]
    ha=ha_settings(site)
    gateway=gateway_settings(site)
    local_nets = ''.join(f'local_net={net}\n' for net in local_networks(site))
    phone_permits = '\n'.join(f'permit={net}' for net in phone_networks(site))
    pjsip=['[global]\ntype=global\nuser_agent=HomePBX\n',f'[transport-udp]\ntype=transport\nprotocol=udp\nbind={ip}:5060\n{local_nets}',f'[transport-tcp]\ntype=transport\nprotocol=tcp\nbind={ip}:5060\n{local_nets}']
    dial=['[general]\nstatic=yes\nwriteprotect=yes\n','[home]\n']
    expected=set()
    for p in phones:
        transport=p.get("transport","tcp")
        for line in phone_lines(p):
            ext, label = line["extension"], p.get("room", line["label"])
            pw=passwords[ext]
            # Asterisk 22 uses *_call_offer_pref; codec_prefs_* is reserved.
            # Answer with one common codec so a G.722-capable caller cannot
            # select G.711 from a multi-codec answer. G.711-only peers still work.
            # https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/Module_Configuration/res_pjsip/#incoming_call_offer_pref
            pjsip.append(f'''[{ext}]
type=endpoint
transport=transport-{transport}
context=home
disallow=all
allow=g722,ulaw,alaw
incoming_call_offer_pref=local_first
outgoing_call_offer_pref=local_merge
auth=auth-{ext}
aors={ext}
direct_media=no
media_address={ip}
bind_rtp_to_media_address=yes
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
dtmf_mode=rfc4733
callerid={label} <{ext}>
identify_by=username
deny=0.0.0.0/0
{phone_permits}

[auth-{ext}]
type=auth
auth_type=userpass
username={ext}
password={pw}

[{ext}]
type=aor
max_contacts=1
remove_existing=yes
qualify_frequency=60
''')
            if gateway:
                dial.append(f'''exten => {ext},1,GotoIf($["${{DB(cisco-dnd/{ext})}}" = "1"]?dnd)
 same => n,Dial(PJSIP/{ext},25)
 same => n,Hangup()
 same => n(dnd),Busy(3)
 same => n,Hangup()
''')
            else:
                dial.append(f'exten => {ext},1,Dial(PJSIP/{ext},25)\n same => n,Hangup()\n')
        filename=f'SEP{p["mac"]}.cnf.xml'
        expected.add(filename)
        private_write(out/"tftp"/filename,phone_xml(p,ip,ntp,passwords,management.get("SEP" + p["mac"])))
    for client in clients:
        client_pjsip, client_dialplan = softphones.render_client(site, client, client_passwords[client["extension"]])
        pjsip.append(client_pjsip)
        dial.append(client_dialplan)
    # Only remove old generated phone profiles, leaving other operator files intact.
    for old in (out/"tftp").glob('SEP*.cnf.xml'):
        if old.name not in expected:
            if old.is_symlink():
                raise ValueError("refusing stale symlink")
            old.unlink()
    dial.append('''exten => 600,1,Answer()
 same => n,Echo()
 same => n,Hangup()
exten => 601,1,Answer()
 same => n,Milliwatt()
 same => n,Hangup()
''')
    if phones:
        # Ring a physical handset once through its primary line (button 1).
        # Secondary lines remain individually callable by their own extension.
        ring='&'.join('PJSIP/'+phone_lines(p)[0]['extension'] for p in phones)
        if gateway:
            group = ['exten => 620,1,Set(RING_TARGETS=)', ' same => n,Set(RING_SEPARATOR=)']
            for p in phones:
                primary = phone_lines(p)[0]['extension']
                condition = '$["${DND_PRIMARY}" != "1"]'
                group.extend([
                    f' same => n,Set(DND_PRIMARY=${{DB(cisco-dnd/{primary})}})',
                    f' same => n,ExecIf({condition}?Set(RING_TARGETS=${{RING_TARGETS}}${{RING_SEPARATOR}}PJSIP/{primary}))',
                    f' same => n,ExecIf({condition}?Set(RING_SEPARATOR=&))'])
            group.extend([' same => n,GotoIf($["${RING_TARGETS}" = ""]?dnd)',
                          ' same => n,Dial(${RING_TARGETS},25)', ' same => n,Hangup()',
                          ' same => n(dnd),Busy(3)', ' same => n,Hangup()'])
            dial.append('\n'.join(group) + '\n')
        else:
            dial.append(f'''exten => 620,1,Dial({ring},25)
 same => n,Hangup()
''')
    service_routes, service_numbers = service_dialplan(site)
    dial.append(service_routes)
    dial.append('''
; Internal service only. No emergency or PSTN route is configured.
exten => _X!,1,Hangup(1)
''')
    configs={"pjsip.conf":'\n'.join(pjsip),"extensions.conf":'\n'.join(dial),"rtp.conf":"[general]\nrtpstart=10000\nrtpend=10199\nstrictrtp=yes\nicesupport=no\n","asterisk.conf":"""[directories]
astetcdir => /etc/asterisk
astmoddir => /usr/lib/asterisk/modules
astvarlibdir => /var/lib/asterisk
astdbdir => /var/lib/asterisk/db
astkeydir => /var/lib/asterisk/keys
astdatadir => /var/lib/asterisk
astagidir => /var/lib/asterisk/agi-bin
astspooldir => /var/spool/asterisk
astrundir => /run/asterisk
astlogdir => /var/log/asterisk
[options]
verbose = 2
debug = 0
documentation_language = en_US
""","modules.conf":"[modules]\nautoload=yes\nnoload=chan_sip.so\nnoload=chan_iax2.so\nnoload=chan_mgcp.so\nnoload=chan_skinny.so\nnoload=res_http_websocket.so\nnoload=res_ari.so\n","logger.conf":"[general]\ndateformat=%F %T\n[logfiles]\nconsole => notice,warning,error\n","manager.conf":"[general]\nenabled=no\n","http.conf":"[general]\nenabled=no\n"}
    configs["manager.conf"] = gateway_manager(manager_config(ha, ip, out), site, out)
    radio = radio_settings(site)
    if radio:
        configs["radio.conf"] = json.dumps(radio, indent=2) + '\n'
    else:
        # Never leave an enabled runtime catalog after disabling the feature.
        stale = out / "asterisk/radio.conf"
        if stale.is_symlink():
            raise ValueError("refusing stale radio config symlink")
        stale.unlink(missing_ok=True)
    if services:
        configs["phone-services.conf"] = json.dumps(services, indent=2) + '\n'
    else:
        stale = out / "asterisk/phone-services.conf"
        if stale.is_symlink():
            raise ValueError("refusing stale phone service config symlink")
        stale.unlink(missing_ok=True)
    assist = ha.get("assist") if ha else None
    if assist:
        # No from_user: preserve each phone's caller ID for HA device discovery.
        configs["pjsip.conf"] += f"""
[ha-assist]
type=endpoint
transport=transport-udp
context=ha-deny-inbound
disallow=all
allow=opus
direct_media=no
media_address={ip}
bind_rtp_to_media_address=yes
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
identify_by=ip
deny=0.0.0.0/0
permit={assist['host']}/32

[ha-assist-identify]
type=identify
endpoint=ha-assist
match={assist['host']}/32
"""
        configs["extensions.conf"] += f"""
exten => 630,1,NoOp(Home Assistant Assist)
 same => n,Dial(PJSIP/ha-assist/sip:630@{assist['host']}:{assist['port']},60)
 same => n,Hangup()

[ha-deny-inbound]
exten => _.,1,Hangup(21)
exten => s,1,Hangup(21)
"""
    configs["pjsip.conf"] += external["pjsip"]
    configs["extensions.conf"] += external["dialplan"]
    configs["extensions.conf"] += intercom_dialplan(
        site, [line["extension"] for phone in phones for line in phone_lines(phone)])
    configs["extensions.conf"] += alert_dialplan(
        site, [line["extension"] for phone in phones for line in phone_lines(phone)])
    service_numbers += external["numbers"]
    for name,body in configs.items():
        private_write(out/"asterisk"/name,body)
    if not pbx_only:
        assist_template = '  <TEMPLATE MATCH="630" Timeout="0"/>\n' if assist else ''
        assist_template += ''.join(f'  <TEMPLATE MATCH="{number}" Timeout="0"/>\n' for number in service_numbers)
        private_write(out/"tftp"/"dialplan.xml",'<DIALTEMPLATE>\n  <TEMPLATE MATCH="2.." Timeout="0"/>\n  <TEMPLATE MATCH="600" Timeout="0"/>\n  <TEMPLATE MATCH="601" Timeout="0"/>\n  <TEMPLATE MATCH="620" Timeout="0"/>\n'+assist_template+'</DIALTEMPLATE>\n')
    return len(phones)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--site',default='.local/site.json')
    mode=ap.add_mutually_exclusive_group()
    mode.add_argument('--inventory',help='verified phone inventory (default: .local/phones.json)')
    mode.add_argument('--pbx-only',action='store_true',help='bootstrap PBX without phones, Cisco XML or TFTP')
    ap.add_argument('--out',default='.local')
    args=ap.parse_args()
    os.umask(0o077)
    try:
        inventory = None if args.pbx_only else read_json(args.inventory or '.local/phones.json')
        count=generate(read_json(args.site),inventory,args.out,pbx_only=args.pbx_only)
    except (ValueError,KeyError,OSError,TypeError) as e:
        sys.exit(f'Provisioning stopped: {e}')
    if args.pbx_only:
        print(f'Generated PBX-only configuration in {args.out}; no phone profiles or TFTP files; secrets omitted from output.')
    else:
        print(f'Generated {count} phone profiles and PBX configuration in {args.out}; secrets omitted from output.')


if __name__=='__main__':
    main()
