#!/usr/bin/env python3
"""Local call-in clock and curated Home Assistant media, preserving wideband audio."""
import contextlib
import datetime as dt
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
from urllib import parse, request
from zoneinfo import ZoneInfo

CONFIG = Path('/etc/asterisk/dial-audio.json')
MAX_BYTES = 32 * 1024 * 1024


def validate(config):
    if not isinstance(config, dict) or set(config) != {'credentials_file', 'local_lines', 'services'}:
        raise ValueError('invalid dial audio configuration')
    if not isinstance(config['credentials_file'], str) or not Path(config['credentials_file']).is_absolute():
        raise ValueError('absolute credentials path required')
    lines = config['local_lines']
    if not isinstance(lines, list) or not lines or any(not isinstance(n, str) or not re.fullmatch(r'2\d{2}', n) for n in lines):
        raise ValueError('explicit local line list required')
    services = config['services']
    if not isinstance(services, dict) or not 1 <= len(services) <= 32:
        raise ValueError('invalid service list')
    for number, service in services.items():
        if not re.fullmatch(r'\d{3,8}', number) or number in lines or not isinstance(service, dict):
            raise ValueError('invalid service number')
        if service.get('kind') == 'clock':
            if set(service) != {'kind', 'timezone', 'language', 'engine_id', 'voice'}:
                raise ValueError('invalid clock configuration')
            ZoneInfo(service['timezone'])
            if service['language'] not in ('ru', 'en'):
                raise ValueError('unsupported clock language')
            if not re.fullmatch(r'tts\.[a-z0-9_]+', service['engine_id']):
                raise ValueError('invalid TTS entity')
            if not isinstance(service['voice'], str) or len(service['voice']) > 128:
                raise ValueError('invalid voice')
        elif service.get('kind') == 'media':
            if set(service) != {'kind', 'path', 'max_seconds'}:
                raise ValueError('invalid media configuration')
            path = service['path']
            check_path(path, '/media/')
            if not isinstance(service['max_seconds'], int) or not 1 <= service['max_seconds'] <= 900:
                raise ValueError('invalid media duration')
        else:
            raise ValueError('unsupported service')
    return config


def check_path(path, prefix):
    if not isinstance(path, str) or len(path) > 4096 or not path.isascii():
        raise ValueError('invalid media path')
    decoded = parse.unquote(path)
    if (not path.startswith(prefix) or parse.urlsplit(path).query or parse.urlsplit(path).fragment
            or any(c.isspace() or ord(c) < 32 for c in path) or '\\' in decoded
            or any(p in ('.', '..') for p in decoded.split('/'))):
        raise ValueError('invalid media path')
    return path


def credentials(path):
    raw = Path(path).read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError('credentials input too large')
    config = json.loads(raw)
    url = parse.urlsplit(config['url'])
    address = ipaddress.IPv4Address(url.hostname)
    private = any(address in ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
    if (not private or url.scheme not in ('http', 'https') or url.username or url.password
            or url.path not in ('', '/') or url.query or url.fragment):
        raise ValueError('private HA origin required')
    if not isinstance(config['token'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{32,4096}', config['token']):
        raise ValueError('invalid HA credentials')
    return config['url'].rstrip('/'), config['token']


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class HomeAssistant:
    def __init__(self, base, token):
        self.base, self.token = base, token
        self.opener = request.build_opener(request.ProxyHandler({}), NoRedirect())

    def fetch(self, path, *, payload=None, limit=MAX_BYTES):
        headers = {'Authorization': 'Bearer ' + self.token, 'Accept-Encoding': 'identity'}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers['Content-Type'] = 'application/json'
        req = request.Request(self.base + path, data=data, headers=headers)
        with self.opener.open(req, timeout=20) as response:
            if response.status != 200:
                raise ValueError('HA request failed')
            content = response.headers.get_content_type()
            body = response.read(limit + 1)
            if not body or len(body) > limit:
                raise ValueError('HA response size rejected')
        return body, content

    def audio(self, path):
        body, content = self.fetch(path)
        if not content.startswith('audio/') and content not in ('application/ogg', 'application/octet-stream'):
            raise ValueError('HA returned non-audio content')
        return body

    def speech(self, service, message):
        body, _ = self.fetch('/api/tts_get_url', payload={
            'engine_id': service['engine_id'], 'language': service['language'],
            'options': {'voice': service['voice']}, 'message': message, 'cache': False,
        }, limit=65536)
        # Ignore the absolute URL advertised by HA; credentials stay on the configured origin.
        path = check_path(json.loads(body)['path'], '/api/tts_proxy/')
        return self.audio(path)


def russian_number(number, feminine=False):
    units = ['ноль', 'один', 'два', 'три', 'четыре', 'пять', 'шесть', 'семь', 'восемь', 'девять']
    if feminine:
        units[1:3] = ['одна', 'две']
    teens = ['десять', 'одиннадцать', 'двенадцать', 'тринадцать', 'четырнадцать',
             'пятнадцать', 'шестнадцать', 'семнадцать', 'восемнадцать', 'девятнадцать']
    tens = ['', '', 'двадцать', 'тридцать', 'сорок', 'пятьдесят']
    if number < 10:
        return units[number]
    if number < 20:
        return teens[number - 10]
    return tens[number // 10] + (' ' + units[number % 10] if number % 10 else '')


def plural(number, forms):
    if 11 <= number % 100 <= 14:
        return forms[2]
    return forms[0] if number % 10 == 1 else forms[1] if 2 <= number % 10 <= 4 else forms[2]


def clock_message(now, language):
    if language == 'en':
        return f'The local time is {now:%I:%M %p}.'
    return (f'Сейчас {russian_number(now.hour)} {plural(now.hour, ("час", "часа", "часов"))}, '
            f'{russian_number(now.minute, True)} {plural(now.minute, ("минута", "минуты", "минут"))}.')


def prepare_audio(ha, service, directory, *, now=None):
    clock = service['kind'] == 'clock'
    now = now or (lambda: dt.datetime.now(ZoneInfo(service['timezone'])))
    for attempt in range(2):
        timestamp = now() if clock else None
        raw = ha.speech(service, clock_message(timestamp, service['language'])) if clock else ha.audio(service['path'])
        source, target = directory / 'source.audio', directory / 'playback.sln16'
        source.write_bytes(raw)
        subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                        '-protocol_whitelist', 'file', '-i', str(source), '-vn', '-sn', '-dn',
                        '-t', str(20 if clock else service['max_seconds']), '-ac', '1', '-ar', '16000',
                        '-f', 's16le', '-c:a', 'pcm_s16le', '-fs', str(MAX_BYTES), str(target)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25)
        if not target.exists() or not 3200 <= target.stat().st_size <= MAX_BYTES:
            raise ValueError('decoded audio size rejected')
        # Never announce the previous minute after synthesis crosses the boundary.
        if not clock or timestamp.strftime('%Y%m%d%H%M%z') == now().strftime('%Y%m%d%H%M%z'):
            return target.with_suffix('')
    raise ValueError('clock changed while synthesizing')


def command(value):
    print(value, flush=True)
    response = sys.stdin.readline(4097)
    if not re.match(r'200 result=\d+(?:\s|$)', response):
        raise EOFError('call ended or command failed')
    return response


def environment():
    result = {}
    for _ in range(100):
        line = sys.stdin.readline(4097)
        if line in ('\n', '\r\n'):
            return result
        if not line or len(line) > 4096 or ':' not in line:
            raise ValueError('invalid AGI environment')
        key, value = line.split(':', 1)
        result[key] = value.strip()
    raise ValueError('AGI environment too large')


def interrupted(*_args):
    raise InterruptedError('call interrupted')


def main():
    os.umask(0o077)
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGALRM):
        signal.signal(sig, interrupted)
    env = environment()
    config = validate(json.loads(CONFIG.read_text()))
    match = re.fullmatch(r'PJSIP/(2\d{2})-[0-9a-f]+', env.get('agi_channel', ''))
    if not match or match[1] not in config['local_lines'] or len(sys.argv) != 2:
        raise ValueError('local caller required')
    service = config['services'][sys.argv[1]]
    signal.alarm((20 if service['kind'] == 'clock' else service['max_seconds']) + 70)
    # Four bounded workers; no shared queue or cross-call cancellation.
    with contextlib.ExitStack() as stack:
        for slot in range(4):
            handle = stack.enter_context(open(f'/tmp/dial-audio-slot-{slot}', 'a'))
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                continue
        else:
            raise ValueError('audio workers busy')
        directory = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='dial-audio-')))
        audio = prepare_audio(HomeAssistant(*credentials(config['credentials_file'])), service, directory)
        command(f'STREAM FILE {audio} ""')
        command('SET VARIABLE DIALAUDIOSTATUS "ok"')
    signal.alarm(0)


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt):
        # Keep credentials, HA response bodies, titles and caller data out of logs.
        raise SystemExit(1)
