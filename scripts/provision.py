#!/usr/bin/env python3
"""Generate Cisco 7945G/PBX configuration, or explicitly bootstrap only the PBX."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sys
import xml.etree.ElementTree as ET


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


def validate(site, inventory, *, pbx_only=False):
    ha_settings(site)
    ip = ipaddress.IPv4Address(site["node_ipv4"])
    net = ipaddress.IPv4Network(site["lan_cidr"], strict=True)
    if ip not in net or ip in (net.network_address, net.broadcast_address):
        raise ValueError("node_ipv4 must be a usable address in lan_cidr")
    if pbx_only:
        if inventory is not None:
            raise ValueError("PBX-only mode does not accept a phone inventory")
        return [], None
    ntp = str(ipaddress.IPv4Address(site["ntp_ipv4"]))
    phones = inventory["phones"]
    if not 1 <= len(phones) <= 50:
        raise ValueError("inventory must contain 1..50 phones")
    seen_mac, seen_ext = set(), set()
    for phone in phones:
        if phone.get("verified") is not True or phone.get("model") != "CP-7945G":
            raise ValueError("each phone requires physical model/MAC verification: CP-7945G, verified:true")
        mac = re.sub(r"[:-]", "", phone["mac"]).upper()
        if not re.fullmatch(r"[0-9A-F]{12}", mac) or int(mac[:2], 16) & 1 or mac == "000000000000":
            raise ValueError("invalid unicast MAC")
        ext = str(phone["extension"])
        if not re.fullmatch(r"2[0-9]{2}", ext):
            raise ValueError("extensions must be in 200..299")
        if mac in seen_mac or ext in seen_ext:
            raise ValueError("duplicate MAC or extension")
        seen_mac.add(mac)
        seen_ext.add(ext)
        phone["mac"], phone["extension"] = mac, ext
        if not re.fullmatch(r"[A-Za-z0-9 _-]{1,24}", phone["label"]):
            raise ValueError("phone label must be 1..24 ASCII letters/digits/spaces/_/-")
        if phone.get("transport", "tcp") not in ("udp", "tcp"):
            raise ValueError("transport must be udp or tcp")
        load = phone.get("firmware_load", "")
        if not re.fullmatch(r"SIP45\.[A-Za-z0-9-]+", load):
            raise ValueError("firmware_load must be the exact SIP45.* load ID, without .loads")
    return phones, ntp


def sub(parent, tag, value=None):
    node = ET.SubElement(parent, tag)
    if value is not None:
        node.text = str(value)
    return node


def phone_xml(phone, ip, ntp, password):
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
    for k,v in {"enableVad":"false","natEnabled":"false","voipControlPort":5060,"preferredCodec":"g711ulaw","dtmfAvtPayload":101,"dtmfOutofBand":"avt","dtmfDbLevel":3,"phoneLabel":phone["label"].replace(" ","_")[:12],"stutterMsgWaiting":0,"callStats":"false","silentPeriodBetweenCallWaitingBursts":10,"disableLocalSpeedDialConfig":"false","startMediaPort":16384,"stopMediaPort":16482}.items():
        sub(profile,k,v)
    line = ET.SubElement(sub(profile,"sipLines"), "line", {"button":"1"})
    for k,v in {"featureID":9,"featureLabel":phone["label"],"proxy":"USECALLMANAGER","port":5060,"name":phone["extension"],"displayName":phone["label"],"autoAnswer":None}.items():
        sub(line,k,v)
    sub(line.find("autoAnswer"),"autoAnswerEnabled",0)
    for k,v in {"callWaiting":3,"authName":phone["extension"],"authPassword":password,"sharedLine":"false","messageWaitingLampPolicy":1,"messagesNumber":"","ringSettingIdle":4,"ringSettingActive":5,"contact":phone["extension"]}.items():
        sub(line,k,v)
    sub(profile,"dialTemplate","dialplan.xml")
    sub(d,"commonProfile")
    sub(d,"loadInformation",phone["firmware_load"])
    vendor=sub(d,"vendorConfig")
    sub(vendor,"webAccess",1)
    sub(vendor,"sshAccess",1)
    sub(vendor,"settingsAccess",1)
    sub(d,"deviceSecurityMode",1)
    sub(d,"transportLayerProtocol",2 if phone.get("transport","tcp")=="udp" else 1)
    sub(d,"certHash","")
    sub(d,"encrConfig","false")
    ET.indent(d)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(d,encoding="unicode")+'\n'


def generate(site, inventory, out, *, pbx_only=False):
    phones,ntp = validate(site,inventory,pbx_only=pbx_only)
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
        passwords.setdefault(p["extension"],secrets.token_hex(15))
        if not re.fullmatch(r"[A-Za-z0-9]{24,30}", passwords[p["extension"]]):
            raise ValueError("secret must be 24..30 alphanumeric characters")
    if not pbx_only:
        private_write(secret_path,json.dumps(passwords,indent=2)+'\n')
    ip=site["node_ipv4"]
    ha=ha_settings(site)
    pjsip=['[global]\ntype=global\nuser_agent=HomePBX\n',f'[transport-udp]\ntype=transport\nprotocol=udp\nbind={ip}:5060\nlocal_net={site["lan_cidr"]}\n',f'[transport-tcp]\ntype=transport\nprotocol=tcp\nbind={ip}:5060\nlocal_net={site["lan_cidr"]}\n']
    dial=['[general]\nstatic=yes\nwriteprotect=yes\n','[home]\n']
    expected=set()
    for p in phones:
        ext=p["extension"]
        pw=passwords[ext]
        transport=p.get("transport","tcp")
        pjsip.append(f'''[{ext}]
type=endpoint
transport=transport-{transport}
context=home
disallow=all
allow=ulaw,alaw
auth=auth-{ext}
aors={ext}
direct_media=no
media_address={ip}
bind_rtp_to_media_address=yes
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
dtmf_mode=rfc4733
callerid={p["label"]} <{ext}>
identify_by=username
deny=0.0.0.0/0
permit={site["lan_cidr"]}

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
        dial.append(f'exten => {ext},1,Dial(PJSIP/{ext},25)\n same => n,Hangup()\n')
        filename=f'SEP{p["mac"]}.cnf.xml'
        expected.add(filename)
        private_write(out/"tftp"/filename,phone_xml(p,ip,ntp,pw))
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
        ring='&'.join('PJSIP/'+p['extension'] for p in phones)
        dial.append(f'''exten => 620,1,Dial({ring},25)
 same => n,Hangup()
''')
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
    configs["manager.conf"] = manager_config(ha, ip, out)
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
    for name,body in configs.items():
        private_write(out/"asterisk"/name,body)
    if not pbx_only:
        assist_template = '  <TEMPLATE MATCH="630" Timeout="0"/>\n' if assist else ''
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
