#!/usr/bin/env python3
"""Render opt-in, free-network SIP profiles; never enables PSTN or changes a network.

The provisioner imports validate/generate/check. The CLI only writes private
fragments for review. It does not register, call, deploy, or contact a provider.
"""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys


PREFIX = "external-sip-"
BEGIN = "; BEGIN managed external SIP (free network only)"
END = "; END managed external SIP (free network only)"
PROVIDERS = {
    "callcentric": {"domain": "sip.callcentric.net", "number": "801",
                    "default_target": "17771234567",
                    "source_cidrs": ("204.11.192.0/22", "199.87.144.0/21")},
    "sip2sip": {"domain": "sip2sip.info", "number": "802",
                "default_target": "4444", "source_cidrs": ()},
    "linphone": {"domain": "sip.linphone.org", "number": "803",
                 "default_target": None, "source_cidrs": ()},
}
PRIVATE = tuple(ipaddress.IPv4Network(value) for value in
                ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def require(ok, message):
    if not ok:
        raise ValueError(message)


def _object(value, allowed, message):
    require(isinstance(value, dict) and not set(value) - set(allowed), message)


def _bool(value, message):
    require(type(value) is bool, message)
    return value


def _public_ip(value):
    try:
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError):
        raise ValueError("external_sip.public_ipv4 requires a real public unicast IPv4") from None
    require(address.is_global and not address.is_multicast, "external_sip.public_ipv4 requires a real public unicast IPv4")
    return str(address)


def _local_networks(site):
    values = [site["lan_cidr"], *site.get("phone_cidrs", [])]
    result = []
    for value in values:
        try:
            network = ipaddress.IPv4Network(value, strict=True)
        except (ValueError, TypeError):
            raise ValueError("external SIP local networks must be canonical private IPv4 CIDRs") from None
        require(any(network.subnet_of(private) for private in PRIVATE),
                "external SIP local networks must be private IPv4 CIDRs")
        if str(network) not in result:
            result.append(str(network))
    return result


def _credentials(path, provider):
    require(isinstance(path, str) and Path(path).is_absolute(),
            "external SIP credentials_file must be an absolute private file path")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r") as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                    and info.st_uid == os.getuid() and info.st_size <= 4096,
                    "external SIP credential file must be owned by this user, regular, mode 0600, and <=4096 bytes")
            data = json.load(stream)
    except (OSError, json.JSONDecodeError, UnicodeError):
        raise ValueError("cannot read private external SIP credentials") from None
    _object(data, ("username", "password"), "external SIP credentials require only username and password")
    username, password = data.get("username"), data.get("password")
    if provider == "callcentric":
        pattern = r"1777[0-9]{7}(?:[0-9]{3})?"
    else:
        pattern = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
    require(isinstance(username, str) and re.fullmatch(pattern, username), "invalid external SIP username")
    # Limit to characters which cannot escape or comment out an Asterisk value.
    # The provider password can be set/reset to this portable alphabet.
    require(isinstance(password, str) and re.fullmatch(r"[A-Za-z0-9_!@%+.,:=/-]{12,128}", password),
            "external SIP password must be 12..128 safe ASCII characters (no spaces, quotes, semicolons, brackets, or backslashes)")
    return {"username": username, "password": password}


def _source_cidrs(values, provider):
    require(isinstance(values, list) and 1 <= len(values) <= 8,
            "external SIP inbound source_cidrs must be a nonempty list")
    result = []
    allowed = [ipaddress.IPv4Network(value) for value in PROVIDERS[provider]["source_cidrs"]]
    for value in values:
        try:
            network = ipaddress.IPv4Network(value, strict=True)
        except (ValueError, TypeError):
            raise ValueError("invalid external SIP inbound source CIDR") from None
        require(network.prefixlen >= 21 and network.network_address.is_global
                and not network.network_address.is_multicast
                and any(network.subnet_of(net) for net in allowed),
                "external SIP inbound sources must be within this provider's reviewed published ranges")
        require(not any(network.overlaps(ipaddress.IPv4Network(net)) for net in result),
                "external SIP inbound source CIDRs must not overlap")
        result.append(str(network))
    return result


def validate(site):
    """Validate everything (including credentials) before the caller writes files."""
    config = site.get("external_sip", {})
    _object(config, ("enabled", "public_ipv4", "signaling_port", "accounts"),
            "external_sip has unknown fields; no PSTN or custom routes are supported")
    if not _bool(config.get("enabled", False), "external_sip.enabled must be boolean"):
        return None
    public = _public_ip(config.get("public_ipv4"))
    port = config.get("signaling_port", 5070)
    require(type(port) is int and 5070 <= port <= 5099, "external SIP signaling_port must be 5070..5099")
    try:
        node = ipaddress.IPv4Address(site["node_ipv4"])
    except (ValueError, KeyError, TypeError):
        raise ValueError("external SIP needs a private node_ipv4") from None
    require(any(node in net for net in PRIVATE), "external SIP needs a private node_ipv4")
    accounts = config.get("accounts")
    require(isinstance(accounts, list) and 1 <= len(accounts) <= 3, "external SIP requires 1..3 account profiles")
    active, seen = [], set()
    for account in accounts:
        _object(account, ("provider", "enabled", "credentials_file", "target", "inbound"),
                "external SIP account has unknown fields")
        provider = account.get("provider")
        require(isinstance(provider, str) and provider in PROVIDERS and provider not in seen,
                "external SIP provider must be supported and unique")
        seen.add(provider)
        if not _bool(account.get("enabled", False), "external SIP account enabled must be boolean"):
            continue
        spec = PROVIDERS[provider]
        target = account.get("target", spec["default_target"])
        if provider == "callcentric":
            pattern = r"1777[0-9]{7}(?:[0-9]{3})?"
        elif provider == "sip2sip":
            pattern = r"(?:3333|4444|[A-Za-z][A-Za-z0-9_.-]{0,63})"
        else:
            pattern = r"[A-Za-z][A-Za-z0-9_.-]{0,63}"
        require(isinstance(target, str) and re.fullmatch(pattern, target),
                "target must be a free-network peer name, not a PSTN number or arbitrary SIP URI")
        inbound = account.get("inbound", {})
        _object(inbound, ("enabled", "target", "source_cidrs"), "external SIP inbound has unknown fields")
        enabled = _bool(inbound.get("enabled", False), "external SIP inbound enabled must be boolean")
        sources, destination = [], None
        if enabled:
            require(provider == "callcentric", "inbound is supported only for reviewed Callcentric source ranges")
            destination = inbound.get("target")
            require(isinstance(destination, str) and re.fullmatch(r"2[0-9]{2}", destination),
                    "external SIP inbound target must be one local 200..299 line")
            sources = _source_cidrs(inbound.get("source_cidrs"), provider)
        active.append({"provider": provider, "domain": spec["domain"], "number": spec["number"],
                       "target": target, "inbound_target": destination, "source_cidrs": sources,
                       **_credentials(account.get("credentials_file"), provider)})
    require(active, "external SIP enabled requires at least one enabled account")
    return {"public_ipv4": public, "node_ipv4": str(node), "port": port,
            "local_networks": _local_networks(site), "accounts": active}


def generate(site, out=None):
    """Return fragments; out is accepted for the provisioner interface, never written."""
    config = validate(site)
    result = {"pjsip": "", "dialplan": "", "numbers": []}
    if config is None:
        return result
    local = "\n".join("local_net=" + value for value in config["local_networks"])
    pjsip = [BEGIN, f"""[{PREFIX}udp]
type=transport
protocol=udp
bind={config['node_ipv4']}:{config['port']}
external_signaling_address={config['public_ipv4']}
external_signaling_port={config['port']}
external_media_address={config['public_ipv4']}
{local}
"""]
    routes, inbound = [BEGIN, "[home](+)"], []
    for account in config["accounts"]:
        name = PREFIX + account["provider"]
        user, domain = account["username"], account["domain"]
        acl = ("deny=0.0.0.0/0\n" + "\n".join("permit=" + value for value in account["source_cidrs"])) if account["source_cidrs"] else ""
        # No incoming username identification, registration line matching, or
        # dynamic AOR contacts. Without an identify object unsolicited requests
        # cannot select this endpoint. Do not deny all on outbound-only endpoints:
        # endpoint ACLs also inspect legitimate in-dialog requests such as BYE.
        pjsip.append(f"""[{name}-auth]
type=auth
auth_type=userpass
username={user}
password={account['password']}

[{name}-registration]
type=registration
transport={PREFIX}udp
outbound_auth={name}-auth
server_uri=sip:{domain}
client_uri=sip:{user}@{domain}
contact_user={user}
retry_interval=60
forbidden_retry_interval=0
expiration=300
max_retries=3
auth_rejection_permanent=yes

[{name}]
type=endpoint
transport={PREFIX}udp
context={name}-inbound
disallow=all
allow=ulaw,alaw
outbound_auth={name}-auth
aors={name}-aor
from_user={user}
from_domain={domain}
direct_media=no
media_address={config['node_ipv4']}
bind_rtp_to_media_address=yes
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
dtmf_mode=rfc4733
timers=no
allow_transfer=no
identify_by=ip
{acl}

[{name}-aor]
type=aor
contact=sip:{domain}
qualify_frequency=0
""")
        if account["source_cidrs"]:
            matches = "\n".join("match=" + value for value in account["source_cidrs"])
            pjsip.append(f"[{name}-identify]\ntype=identify\nendpoint={name}\n{matches}\n")
        number = account["number"]
        result["numbers"].append(number)
        routes.append(f"""exten => {number},1,NoOp(Allowlisted external SIP peer)
 same => n,Set(TIMEOUT(absolute)=120)
 same => n,Dial(PJSIP/{name}/sip:{account['target']}@{domain},30,i)
 same => n,Hangup()
""")
        inbound.append(f"[{name}-inbound]")
        if account["inbound_target"]:
            destination = account["inbound_target"]
            dnd = site.get("gateway", {}).get("enabled", False) is True
            first = (f'GotoIf($["${{DB(cisco-dnd/{destination})}}" = "1"]?decline)' if dnd
                     else "NoOp(Allowlisted provider inbound)")
            inbound.append(f"""exten => {user},1,{first}
 same => n,Set(TIMEOUT(absolute)=120)
 same => n,Dial(PJSIP/{destination},25,i)
 same => n,Hangup()
""")
            if dnd:
                inbound.append(" same => n(decline),Busy(3)\n same => n,Hangup()\n")
        inbound.append("exten => _.,1,Hangup(21)\nexten => s,1,Hangup(21)\nexten => i,1,Hangup(21)\nexten => t,1,Hangup(21)\n")
    result["pjsip"] = "\n".join([*pjsip, END, ""])
    result["dialplan"] = "\n".join([*routes, *inbound, END, ""])
    return result


def object_names(site):
    """Names preflight may exclude from the local-phone ACL comparison only."""
    config = validate(site)
    if config is None:
        return {"endpoints": set(), "transports": set()}
    return {"endpoints": {PREFIX + account["provider"] for account in config["accounts"]},
            "transports": {PREFIX + "udp"}}


def check(site, configs):
    """Verify exact managed fragments; reject stale, duplicate, or injected profiles."""
    expected = generate(site)
    for key, filename in (("pjsip", "pjsip.conf"), ("dialplan", "extensions.conf")):
        text = configs.get(filename, "")
        fragment = expected[key]
        if not fragment:
            require(PREFIX not in text and BEGIN not in text and END not in text,
                    "disabled external SIP has stale configuration; rerun provision.py")
            continue
        require(text.count(BEGIN) == text.count(END) == 1 and text.count(fragment) == 1,
                "external SIP configuration differs from private profile; rerun provision.py")
        remainder = text.replace(fragment, "", 1)
        require(PREFIX not in remainder, "external SIP objects must occur only in their managed fragment")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = generate(json.loads(args.site.read_text()))
        require(not args.out.exists(), "output directory must be new")
        args.out.mkdir(parents=True, mode=0o700)
        for name, key in (("pjsip.fragment.conf", "pjsip"), ("extensions.fragment.conf", "dialplan")):
            fd = os.open(args.out / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(result[key])
    except (ValueError, TypeError, KeyError, OSError):
        print("External SIP preparation failed; check private input, credentials permissions, and output path.", file=sys.stderr)
        return 1
    print("Private external SIP fragments prepared; no network or deployment actions performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
