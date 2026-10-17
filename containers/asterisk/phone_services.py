#!/usr/bin/env python3
"""Private dial-to-toggle alarm service; never accept a caller-supplied URL."""
import base64
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from urllib import parse, request
import wave

PROMPTS = ('enabled', 'disabled', 'not-configured', 'not-confirmed')
SCHEDULE_PATH = '/api/services/cisco_phone_control/schedule?return_response'


def enabled(site):
    config = site.get('phone_services', {})
    if not isinstance(config, dict) or type(config.get('enabled', False)) is not bool:
        raise ValueError('phone_services.enabled must be boolean')
    if set(config) - {'enabled', 'home_assistant_url', 'credentials_file', 'prompts_directory'}:
        raise ValueError('unknown phone_services setting')
    return config.get('enabled', False)


def group_settings(site, members=None):
    value = site.get('inbound_ring_group', {})
    if (not isinstance(value, dict) or set(value) - {'enabled', 'members'}
            or type(value.get('enabled', False)) is not bool):
        raise ValueError('invalid inbound_ring_group configuration')
    if not value.get('enabled', False):
        return None
    if site.get('gateway', {}).get('enabled') is not True:
        raise ValueError('inbound ring group requires the DND-capable gateway')
    selected = value.get('members')
    if (not isinstance(selected, list) or not 1 <= len(selected) <= 50
            or any(not isinstance(line, str) or not re.fullmatch(r'2[0-9]{2}', line) for line in selected)
            or len(set(selected)) != len(selected)):
        raise ValueError('inbound_ring_group.members must be unique local extensions')
    if members is not None and not set(selected) <= set(members):
        raise ValueError('inbound ring members must be Cisco primary lines or declared softphones')
    return selected


def private_file(path, limit):
    if not isinstance(path, (str, Path)) or not Path(path).is_absolute():
        raise ValueError('phone service inputs require absolute private paths')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > limit):
                raise ValueError('phone service inputs must be private owned regular files within size limits')
            data = source.read(limit + 1)
            if len(data) > limit:
                raise ValueError('phone service input exceeds size limit')
            return data
    except OSError:
        raise ValueError('cannot read private phone service input') from None


def ha_url(value):
    url = parse.urlsplit(value)
    if (url.scheme not in ('http', 'https') or url.username or url.password
            or url.path not in ('', '/') or url.query or url.fragment):
        raise ValueError('phone service requires a private HA origin without path or credentials')
    address = ipaddress.IPv4Address(url.hostname)
    if not any(address in ipaddress.IPv4Network(net) for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
        raise ValueError('phone service requires a private HA IPv4 address')
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError('invalid HA port')
    return value.rstrip('/')


def validate_audio(raw):
    if len(raw) > 128 * 1024:
        raise ValueError('phone prompt exceeds 128KiB')
    try:
        with wave.open(io.BytesIO(raw), 'rb') as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 8000, 'NONE'):
                raise ValueError('phone prompts require mono 8kHz signed16 PCM WAV')
            if not 800 <= audio.getnframes() <= 8 * 8000:
                raise ValueError('phone prompts must last 0.1 to 8 seconds')
            if len(audio.readframes(audio.getnframes())) != audio.getnframes() * 2:
                raise ValueError('truncated phone prompt')
    except (wave.Error, EOFError):
        raise ValueError('invalid phone prompt WAV') from None


def settings(site, line_owners):
    if not enabled(site):
        return None
    if not site.get('gateway', {}).get('enabled') or not line_owners:
        raise ValueError('phone services require a gateway and physical Cisco inventory')
    value = site['phone_services']
    url = ha_url(value['home_assistant_url'])
    credentials = json.loads(private_file(value['credentials_file'], 8192))
    if (not isinstance(credentials, dict) or set(credentials) != {'token', 'entry_id'}
            or not isinstance(credentials['token'], str) or not isinstance(credentials['entry_id'], str)
            or not re.fullmatch(r'[A-Za-z0-9_.-]{32,4096}', credentials['token'])
            or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', credentials['entry_id'])):
        raise ValueError('invalid private HA service credentials')
    prompts, total = {}, 0
    for name in PROMPTS:
        raw = private_file(Path(value['prompts_directory']) / (name + '.wav'), 128 * 1024)
        validate_audio(raw)
        total += len(raw)
        prompts[name] = base64.b64encode(raw).decode('ascii')
    if total > 384 * 1024:
        raise ValueError('combined phone prompts exceed 384KiB')
    return {'url': url, **credentials, 'line_owners': dict(line_owners), 'prompts': prompts}


def dialplan(site):
    if not enabled(site):
        return ''
    return '''exten => 641,1,Answer()
 same => n,Set(TIMEOUT(absolute)=20)
 same => n,AGI(phone_services.py)
 same => n,Hangup()
'''


def group_dialplan(site, members=None):
    lines = group_settings(site, members)
    if lines is None:
        return ''
    parts = ['exten => 621,1,Set(INBOUND_TARGETS=)', ' same => n,Set(INBOUND_SEPARATOR=)']
    for line in lines:
        condition = f'$["${{DB(cisco-dnd/{line})}}" != "1"]'
        parts.extend([
            f' same => n,ExecIf({condition}?Set(INBOUND_TARGETS=${{INBOUND_TARGETS}}${{INBOUND_SEPARATOR}}PJSIP/{line}))',
            f' same => n,ExecIf({condition}?Set(INBOUND_SEPARATOR=&))'])
    parts.extend([' same => n,GotoIf($["${INBOUND_TARGETS}" = ""]?dnd)',
                  ' same => n,Dial(${INBOUND_TARGETS},25,i)', ' same => n,Hangup()',
                  ' same => n(dnd),Busy(3)', ' same => n,Hangup()'])
    return '\n'.join(parts) + '\n'


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def toggle(config, channel, uniqueid, opener=None):
    match = re.fullmatch(r'PJSIP/(2[0-9]{2})-[0-9a-f]+', channel)
    if not match or match[1] not in config['line_owners'] or not 1 <= len(uniqueid) <= 150:
        return 'not-confirmed'
    line = match[1]
    primary = config['line_owners'][line]
    payload = {'entry_id': config['entry_id'], 'phone': primary, 'line': line,
               'operation': 'toggle_saved_alarm',
               'request_id': 'dial-alarm-' + hashlib.sha256(uniqueid.encode()).hexdigest()}
    client = opener or request.build_opener(request.ProxyHandler({}), NoRedirect())
    req = request.Request(ha_url(config['url']) + SCHEDULE_PATH, data=json.dumps(payload).encode(),
                          headers={'Authorization': 'Bearer ' + config['token'], 'Content-Type': 'application/json'})
    # One attempt. Ambiguous network results never cause a second toggle.
    try:
        with client.open(req, timeout=5) as response:
            raw = response.read(65537)
            if response.status != 200 or len(raw) > 65536:
                return 'not-confirmed'
        result = json.loads(raw).get('service_response', {})
        if (result.get('schema_version') != 1 or result.get('phone') != primary
                or result.get('line') != line or result.get('gateway_fresh') is not True):
            return 'not-confirmed'
        alarm = result.get('alarm', {})
        if result.get('operation_result') == 'not_configured':
            return 'not-configured' if alarm.get('configured') is False else 'not-confirmed'
        if (result.get('operation_result') not in ('applied', 'replayed') or alarm.get('configured') is not True
                or alarm.get('line') not in [key for key, owner in config['line_owners'].items() if owner == primary]
                or type(alarm.get('enabled')) is not bool):
            return 'not-confirmed'
        return 'enabled' if alarm['enabled'] else 'disabled'
    except Exception:
        return 'not-confirmed'


def main():
    env = {}
    for _ in range(100):
        raw = sys.stdin.readline(4097)
        if not raw or raw in ('\n', '\r\n'):
            break
        if len(raw) > 4096:
            return
        key, _, value = raw.partition(':')
        env[key] = value.strip()
    else:
        return
    config = json.loads(Path('/etc/asterisk/phone-services.conf').read_text())
    outcome = toggle(config, env.get('agi_channel', ''), env.get('agi_uniqueid', ''))
    data = base64.b64decode(config['prompts'][outcome], validate=True)
    validate_audio(data)
    with tempfile.TemporaryDirectory(prefix='cisco-alarm-') as directory:
        path = Path(directory) / 'prompt.wav'
        path.write_bytes(data)
        path.chmod(0o600)
        print(f'STREAM FILE {path.with_suffix("")} ""', flush=True)
        sys.stdin.readline(4097)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # No secrets, call metadata, HA responses or exception strings in logs.
        raise SystemExit(1)
