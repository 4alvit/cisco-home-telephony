#!/usr/bin/env python3
"""Validate and render optional local SIP applications independently of Cisco devices."""
import ipaddress
import json
from pathlib import Path
import re
import secrets
import stat

PRIVATE = tuple(ipaddress.IPv4Network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
SECRET_PATTERN = r'[A-Za-z0-9_-]{24,30}'
MARKER = 'HOME_CLIENT_KIND=softphone'
ROOM_PATTERN = r"[A-Za-z0-9][A-Za-z0-9 _'()-]{0,47}"


def room_name(value):
    """Accept a short caller-ID name without Asterisk configuration syntax."""
    if not isinstance(value, str) or not re.fullmatch(ROOM_PATTERN, value) or value != value.strip():
        raise ValueError('room must be 1..48 ASCII letters/digits/spaces/_/\'/(/)/-, start with a letter or digit, and have no surrounding whitespace')
    return value


def validate(site, cisco_extensions=()):
    values = site.get('softphones', [])
    if not isinstance(values, list) or len(values) > 50:
        raise ValueError('softphones must be a list of at most 50 clients')
    used = set(cisco_extensions)
    result = []
    for client in values:
        required = {'extension', 'label', 'source_ipv4', 'transport'}
        if not isinstance(client, dict) or not required <= set(client) or set(client) - required - {'room'}:
            raise ValueError('each softphone requires extension, label, source_ipv4 and transport, with optional room')
        extension = client['extension']
        if not isinstance(extension, str) or not re.fullmatch(r'2[0-9]{2}', extension):
            raise ValueError('softphone extension must be a string in 200..299')
        if extension in used:
            raise ValueError('duplicate extension across Cisco lines and softphones')
        used.add(extension)
        if not isinstance(client['label'], str) or not re.fullmatch(r'[A-Za-z0-9 _-]{1,24}', client['label']):
            raise ValueError('softphone label must be 1..24 ASCII letters/digits/spaces/_/-')
        if 'room' in client:
            room_name(client['room'])
        if client['transport'] not in ('tcp', 'udp'):
            raise ValueError('softphone transport must be tcp or udp')
        value = client['source_ipv4']
        if not isinstance(value, str):
            raise ValueError('softphone source_ipv4 must be a private IPv4 literal')
        try:
            address = ipaddress.IPv4Address(value)
        except ipaddress.AddressValueError:
            raise ValueError('softphone source_ipv4 must be a private IPv4 literal') from None
        if not any(address in network for network in PRIVATE):
            raise ValueError('softphone source_ipv4 must be a private IPv4 literal')
        result.append(dict(client, source_ipv4=str(address)))
    return result


def networks(site):
    return list(dict.fromkeys(client['source_ipv4'] + '/32' for client in validate(site)))


def prepare_secrets(clients, out, cisco_passwords):
    """Read and validate before writing; preserve disabled client credentials for reuse."""
    if not clients:
        return None
    path = Path(out) / 'softphone-secrets.json'
    if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.stat().st_mode)):
        raise ValueError('softphone secrets must be a regular private file')
    saved = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(saved, dict) or any(
        not isinstance(k, str) or not re.fullmatch(r'2[0-9]{2}', k)
        or not isinstance(v, str) or not re.fullmatch(SECRET_PATTERN, v) for k, v in saved.items()
    ):
        raise ValueError('invalid softphone secrets; preserve or rotate explicitly')
    if len(set(saved.values())) != len(saved) or set(saved.values()) & set(cisco_passwords.values()):
        raise ValueError('each softphone requires a distinct secret, separate from Cisco credentials')
    used = set(saved.values()) | set(cisco_passwords.values())
    for client in clients:
        if client['extension'] not in saved:
            secret = secrets.token_urlsafe(18)
            while secret in used:
                secret = secrets.token_urlsafe(18)
            saved[client['extension']] = secret
            used.add(secret)
    return saved


def render_client(site, client, password):
    """Use the existing LAN transport and home services, with an exact endpoint ACL."""
    if not isinstance(password, str) or not re.fullmatch(SECRET_PATTERN, password):
        raise ValueError('invalid softphone secret')
    extension, label = client['extension'], client.get('room', client['label'])
    # Answer with the best common codec, retaining G.711-only client support.
    # These are active Asterisk 22 policies; codec_prefs_* is reserved.
    # https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/Module_Configuration/res_pjsip/#incoming_call_offer_pref
    pjsip = f'''[{extension}]
type=endpoint
transport=transport-{client['transport']}
context=home
disallow=all
allow=g722,ulaw,alaw
incoming_call_offer_pref=local_first
outgoing_call_offer_pref=local_merge
auth=auth-softphone-{extension}
aors={extension}
direct_media=no
media_address={site['node_ipv4']}
bind_rtp_to_media_address=yes
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
dtmf_mode=rfc4733
callerid={label} <{extension}>
identify_by=username
set_var={MARKER}
deny=0.0.0.0/0
permit={client['source_ipv4']}/32

[auth-softphone-{extension}]
type=auth
auth_type=userpass
username={extension}
password={password}

[{extension}]
type=aor
max_contacts=1
remove_existing=yes
qualify_frequency=60
'''
    if site.get('gateway', {}).get('enabled', False):
        dialplan = f'''exten => {extension},1,GotoIf($["${{DB(cisco-dnd/{extension})}}" = "1"]?dnd)
 same => n,Dial(PJSIP/{extension},25)
 same => n,Hangup()
 same => n(dnd),Busy(3)
 same => n,Hangup()
'''
    else:
        dialplan = f'exten => {extension},1,Dial(PJSIP/{extension},25)\n same => n,Hangup()\n'
    return pjsip, dialplan


def check(site, configs):
    """Reject changed/stale generic ACLs, transports, routes and authentication objects."""
    clients = validate(site)
    expected = {client['extension'] for client in clients}
    text = configs.get('pjsip.conf', '')
    sections = re.findall(r'^\[([^\]]+)\]\n(.*?)(?=^\[|\Z)', text, re.M | re.S)
    marked = [name for name, body in sections if re.search(r'^set_var=' + re.escape(MARKER) + r'\s*$', body, re.M)]
    if len(marked) != len(expected) or set(marked) != expected:
        raise ValueError('softphone endpoint set differs from site.json; rerun provision.py')
    auth_names = [name for name, _ in sections if name.startswith('auth-softphone-')]
    if sorted(auth_names) != sorted('auth-softphone-' + n for n in expected):
        raise ValueError('softphone authentication set differs from site.json; rerun provision.py')
    passwords = []
    for client in clients:
        extension = client['extension']
        endpoint_sections = [body for name, body in sections if name == extension]
        if len(endpoint_sections) != 2:
            raise ValueError('softphone endpoint/AOR must each occur once')
        body = next(body for name, body in sections if name == 'auth-softphone-' + extension)
        found = re.findall(r'^password=(.*)$', body, re.M)
        if len(found) != 1:
            raise ValueError('softphone authentication must have one secret')
        pjsip, dialplan = render_client(site, client, found[0])
        if text.count(pjsip) != 1 or configs.get('extensions.conf', '').count(dialplan) != 1:
            raise ValueError('softphone configuration differs from site.json; rerun provision.py')
        passwords.append(found[0])
    if len(passwords) != len(set(passwords)):
        raise ValueError('softphone credentials must be distinct')
    return expected
