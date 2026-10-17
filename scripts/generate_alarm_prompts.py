#!/usr/bin/env python3
"""Generate private, fixed alarm prompts with the configured HA TTS engine."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib import request

_spec = importlib.util.spec_from_file_location(
    'phone_services', Path(__file__).resolve().parents[1] / 'containers/asterisk/phone_services.py')
services = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(services)
TEXT = {
    'enabled': 'Будильник включён.',
    'disabled': 'Будильник выключен.',
    'not-configured': 'Будильник не настроен. Сначала задайте время и дни недели.',
    'not-confirmed': 'Не удалось подтвердить изменение будильника. Проверьте настройки.',
}


def generate(origin, credentials_file, output, engine_id='tts.local_russian_speech', client=None):
    origin = services.ha_url(origin)
    if not re.fullmatch(r'tts\.[a-z0-9_]+', engine_id):
        raise ValueError('invalid TTS entity')
    credentials = json.loads(services.private_file(credentials_file, 8192))
    token = credentials['token']
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{32,4096}', token):
        raise ValueError('invalid private token')
    output = Path(output)
    # Refuse overwrites; changing the voice requires an explicitly new directory.
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    client = client or request.build_opener(request.ProxyHandler({}), services.NoRedirect())
    for name, message in TEXT.items():
        req = request.Request(origin + '/api/tts_get_url',
                              data=json.dumps({'engine_id': engine_id, 'message': message,
                                               'language': 'ru', 'cache': True}).encode(),
                              headers={'Authorization': 'Bearer ' + token,
                                       'Content-Type': 'application/json'})
        with client.open(req, timeout=30) as response:
            body = response.read(65537)
            if response.status != 200 or len(body) > 65536:
                raise ValueError('TTS request failed')
        # Only the same-origin relative proxy path is accepted, never response.url.
        path = json.loads(body).get('path', '')
        if not re.fullmatch(r'/api/tts_proxy/[A-Za-z0-9_.-]+', path):
            raise ValueError('unsupported TTS proxy path')
        with client.open(request.Request(origin + path), timeout=30) as response:
            audio = response.read(2 * 1024 * 1024 + 1)
            if response.status != 200 or len(audio) > 2 * 1024 * 1024:
                raise ValueError('TTS audio failed')
        converted = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', 'pipe:0',
                                    '-ac', '1', '-ar', '8000', '-c:a', 'pcm_s16le',
                                    '-f', 'wav', str(output / (name + '.wav'))], input=audio,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
        if converted.returncode:
            raise ValueError('audio conversion failed')
        target = output / (name + '.wav')
        target.chmod(0o600)
        services.validate_audio(target.read_bytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ha-url', required=True)
    parser.add_argument('--credentials-file', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--engine-id', default='tts.local_russian_speech')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        generate(args.ha_url, args.credentials_file, args.out, args.engine_id)
    except Exception:
        print('Prompt generation failed; verify the private credentials, TTS engine, ffmpeg, and new output directory.', file=sys.stderr)
        return 1
    print('Four private alarm prompts generated; no call was placed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
