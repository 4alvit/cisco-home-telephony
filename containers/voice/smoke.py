#!/usr/bin/env python3
"""Synthetic image smoke, intended only for Docker --network none.

No live session flags, microphone APIs, audio devices or phone calls are used.
All generated media and configuration exist only in the container's /tmp.
"""
import asyncio
import importlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def reject_network(event, _arguments):
    if event in ('socket.connect', 'socket.connect_ex', 'socket.bind'):
        raise RuntimeError('network_operation_forbidden_in_smoke')


def check_imports():
    require(sys.version_info[:2] in ((3, 11), (3, 12)), 'unsupported_python')
    require(os.geteuid() == 10001, 'smoke_requires_nonroot_uid_10001')
    locked = {}
    for line in Path('/opt/voice/requirements.lock').read_text().splitlines():
        match = re.fullmatch(r'([A-Za-z0-9_-]+)==([^\s]+)\s*\\?', line)
        if match:
            name, version = match.groups()
            require(importlib.metadata.version(name) == version, 'installed_version_mismatch')
            locked[name] = version
    require({'aiohttp', 'Pillow', 'websockets'} <= locked.keys(), 'incomplete_runtime_lock')
    for module in ('aiohttp', 'websockets.asyncio.client', 'PIL.Image',
                   'phone_voice_transport', 'phone_voice_controller', 'phone_voice_display',
                   'phone_gateway', 'phone_apps', 'phone_apps_ha', 'voice_assistant',
                   'voice_assistant.config', 'voice_assistant.audio', 'voice_assistant.service',
                   'voice_assistant.ha', 'voice_assistant.playback'):
        importlib.import_module(module)
    from PIL import Image
    encoded = io.BytesIO()
    Image.new('RGB', (8, 8), (20, 40, 60)).save(encoded, format='PNG')
    with Image.open(io.BytesIO(encoded.getvalue())) as decoded:
        require(decoded.size == (8, 8) and decoded.getpixel((0, 0)) == (20, 40, 60),
                'pillow_roundtrip_failed')
    return locked


def check_ffmpeg():
    from voice_assistant.playback import FFmpegDecoder
    pcm = b'\0' * 3200  # 100 ms of synthetic mono 16-kHz silence.
    result = subprocess.run(
        ['/usr/bin/ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
         '-protocol_whitelist', 'pipe', '-f', 's16le', '-ar', '16000', '-ac', '1',
         '-i', 'pipe:0', '-f', 'flac', 'pipe:1'],
        input=pcm, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=True)
    require(0 < len(result.stdout) < 1024 * 1024, 'invalid_synthetic_flac')
    decoded = asyncio.run(FFmpegDecoder('/usr/bin/ffmpeg', max_seconds=1)(result.stdout, 'audio/flac'))
    require(decoded.data == pcm and decoded.sample_rate == 16000 and decoded.channels == 1,
            'ffmpeg_synthetic_decode_failed')


def check_cli():
    with tempfile.TemporaryDirectory(prefix='voice-smoke-') as directory:
        root = Path(directory)
        local = root / '.local'
        local.mkdir(mode=0o700)
        gateway = {
            'persistent_enabled': True, 'bind_ipv4': '192.168.50.10', 'api_port': 18084,
            'api_token': 'a' * 48, 'api_clients': ['192.168.50.11'],
            'ami': {'host': '192.168.50.10', 'port': 5038,
                    'username': 'synthetic-gateway', 'secret': 'synthetic-ami-secret'},
            'dial_allowlist': [], 'ring_allowlist': [],
            'phones': [{'id': '201', 'lines': ['201', '202'], 'ipv4': '192.168.50.20',
                        'device': 'SEP020000000001', 'port': 18080,
                        'auth_path': '/auth/' + 'c' * 64,
                        'username': 'u' * 24, 'password': 'p' * 24}],
        }
        transport = {
            'gateway_config': str(local / 'gateway.json'), 'line': '201',
            'bind_ipv4': '192.168.50.10', 'bind_port': 24576, 'phone_rtp_port': 24578,
            'duration_seconds': 1, 'receive_idle_seconds': 1, 'volume': 10,
        }
        voice = {
            'service': {'enabled': True, 'rearm_after_turn': False}, 'phones': {'201': True},
            'ha': {'base_url': 'http://192.168.50.30:8123',
                   'token_file': '.local/token.txt', 'pipeline_id': 'synthetic-pipeline'},
            'wake': {'mode': 'manual', 'bundle_file': '.local/unused-bundle.json'},
        }
        for name, value in (('gateway', gateway), ('transport', transport), ('voice', voice)):
            path = local / (name + '.json')
            path.write_text(json.dumps(value))
            path.chmod(0o600)
        (local / 'token.txt').write_text('synthetic-token\n')
        (local / 'token.txt').chmod(0o600)
        # Construction exercises the real private-path/token loader. It does
        # not activate a stream or connect to HA; the audit hook enforces that.
        from voice_assistant.config import build_service
        class SyntheticSink:
            async def write_pcm(self, *_args):
                raise RuntimeError('unexpected_audio_output')

            async def stop(self, *_args):
                pass
        service = build_service(voice, root, SyntheticSink())
        asyncio.run(service.close())
        command = [sys.executable, '/workspace/scripts/voice_session.py']
        subprocess.run(command + ['--help'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=10, check=True)
        result = subprocess.run(
            command + ['--transport-config', str(local / 'transport.json'),
                       '--voice-config', str(local / 'voice.json'), '--workspace', str(root)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=True)
        require(json.loads(result.stdout) ==
                {'valid': True, 'network_actions': 0, 'microphone_started': False},
                'cli_dry_run_failed')


def main():
    sys.addaudithook(reject_network)
    locked = check_imports()
    check_ffmpeg()
    check_cli()
    print(json.dumps({'image_smoke': 'passed', 'dependencies': locked,
                      'synthetic_ffmpeg_decode': True, 'cli_dry_run': True,
                      'network_actions': 0, 'microphone_started': False}, sort_keys=True))


if __name__ == '__main__':
    main()
