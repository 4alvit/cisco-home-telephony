#!/usr/bin/env python3
"""Synthetic image smoke, intended only for Docker --network none.

No live session flags, microphone APIs, audio devices or phone calls are used.
All generated media and configuration exist only in the container's /tmp.
"""
import asyncio
import fcntl
import hashlib
import importlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import re
import stat
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
                   'phone_voice_transport', 'phone_voice_controller', 'phone_voice_control',
                   'phone_voice_display', 'stage_private',
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


def check_cli(*, python_executable='/usr/local/bin/python3',
              ffmpeg_executable='/usr/bin/ffmpeg',
              cli_script='/workspace/scripts/voice_session.py'):
    with tempfile.TemporaryDirectory(prefix='voice-smoke-') as directory:
        root = Path(directory).resolve()
        local = root / '.local'
        local.mkdir(mode=0o700)
        config = local / 'config'
        config.mkdir(mode=0o700)
        state = local / 'state'
        state.mkdir(mode=0o700)
        state_sentinel = state / 'persistent-owner-journal.txt'
        state_sentinel.write_text('synthetic state must remain unchanged\n')
        state_sentinel.chmod(0o600)
        sentinel_stat = state_sentinel.stat()

        def require_sentinel():
            info = state_sentinel.stat()
            require(state_sentinel.read_text() == 'synthetic state must remain unchanged\n'
                    and (info.st_dev, info.st_ino, info.st_mode, info.st_uid)
                    == (sentinel_stat.st_dev, sentinel_stat.st_ino,
                        sentinel_stat.st_mode, sentinel_stat.st_uid),
                    'persistent_state_changed')
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
            'gateway_config': str(config / 'gateway.json'), 'line': '201',
            'bind_ipv4': '192.168.50.10', 'bind_port': 24576, 'phone_rtp_port': 24578,
            'duration_seconds': 1, 'receive_idle_seconds': 1, 'volume': 10,
        }
        voice = {
            'service': {'enabled': True, 'rearm_after_turn': False}, 'phones': {'201': True},
            'ha': {'base_url': 'http://192.168.50.30:8123',
                   'token_file': '.local/config/token.txt', 'pipeline_id': 'synthetic-pipeline'},
            'wake': {'mode': 'manual', 'bundle_file': '.local/unused-bundle.json'},
        }
        control = {
            'version': 1, 'enabled': False, 'state_directory': '.local/state/voice-control',
            'python_executable': python_executable, 'ffmpeg_executable': ffmpeg_executable,
            'profiles': [{'transport_config': '.local/config/transport.json',
                          'voice_config': '.local/config/voice.json', 'display_port': None}],
        }
        # Model Kubernetes AtomicWriter: public file links point through ..data
        # to a read-only version directory. The controller refuses these links
        # directly, so production staging must turn them into real private files.
        inputs = root / 'inputs'
        inputs.mkdir()
        version = inputs / '..synthetic-version'
        version.mkdir()
        values = {name + '.json': json.dumps(value)
                  for name, value in (('gateway', gateway), ('transport', transport),
                                      ('voice', voice), ('control', control))}
        values['token.txt'] = 'synthetic-token\n'
        values['staging.json'] = json.dumps({'version': 1, 'files': list(values)})
        for name, value in values.items():
            (version / name).write_text(value)
            (version / name).chmod(0o440)
            (inputs / name).symlink_to(Path('..data') / name)
        (inputs / '..data').symlink_to(version.name, target_is_directory=True)
        from stage_private import stage_private, StageError
        require(stage_private(inputs, config, inputs / 'staging.json') == 5,
                'secret_staging_failed')
        require(all(not path.is_symlink() and path.stat().st_uid == os.geteuid()
                    and stat.S_IMODE(path.stat().st_mode) == 0o600
                    for path in config.iterdir()), 'staged_file_permissions_failed')
        try:
            stage_private(inputs, config, inputs / 'staging.json')
        except StageError:
            pass
        else:
            raise RuntimeError('staging_overwrote_existing_config')
        from phone_voice_control import load_control_config, VoiceControl
        loaded = load_control_config(config / 'control.json', root)
        supervisor = VoiceControl(loaded, authorize=lambda *_: True, monitor=False)
        try:
            require(supervisor.status('201', context={})['state'] == 'idle', 'control_status_failed')
        finally:
            supervisor.close()
        # Exercise the actual gateway adapter without opening its listeners or
        # starting its PBX synchronization/background loop. The state DB here is
        # synthetic scratch, independent of the persistent-state sentinel.
        from phone_gateway import Gateway
        adapter = Gateway(gateway, root / 'synthetic-gateway-state')
        try:
            adapter.attach_voice(config / 'control.json', root)
            response = adapter.voice_request('201', 'status', {}, {
                'source': '192.168.50.11', 'authorization': ('Bearer ' + 'a' * 48,)})
            require(response['voice']['state'] == 'idle', 'gateway_voice_status_failed')
            # Attachment and status are read-only. Shutdown deliberately creates
            # empty lock files to fence late external owners until listeners close.
            require(list(state.iterdir()) == [state_sentinel], 'status_created_state')
            require_sentinel()
            require(adapter.close_voice(), 'gateway_voice_shutdown_incomplete')
            locks = state / 'voice-control'
            lock = locks / (hashlib.sha256(gateway['phones'][0]['device'].encode()).hexdigest()
                            + '.lock')
            require(set(state.iterdir()) == {state_sentinel, locks}
                    and list(locks.iterdir()) == [lock], 'unexpected_shutdown_state')
            for path, mode in ((locks, 0o700), (lock, 0o600)):
                info = path.lstat()
                require(not path.is_symlink() and info.st_uid == os.geteuid()
                        and stat.S_IMODE(info.st_mode) == mode,
                        'shutdown_state_permissions_failed')
            require(stat.S_ISREG(lock.stat().st_mode) and lock.read_bytes() == b'',
                    'shutdown_lock_not_empty')
            lock_inode = lock.stat().st_ino
            with lock.open('r+b') as probe:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    raise RuntimeError('shutdown_fence_not_retained')
                # No listeners were opened in this smoke; simulate their closure.
                adapter.release_voice_shutdown()
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(probe, fcntl.LOCK_UN)
            require(lock.stat().st_ino == lock_inode, 'shutdown_lock_was_replaced')
            require_sentinel()
        finally:
            try:
                adapter.close_voice()
            finally:
                try:
                    adapter.release_voice_shutdown()
                finally:
                    adapter.db.close()
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
        command = [sys.executable, cli_script]
        subprocess.run(command + ['--help'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=10, check=True)
        result = subprocess.run(
            command + ['--transport-config', str(config / 'transport.json'),
                       '--voice-config', str(config / 'voice.json'), '--workspace', str(root)],
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
                      'atomicwriter_staging': True, 'control_validation': True,
                      'gateway_voice_attachment': True,
                      'gateway_shutdown_fencing': True,
                      'network_actions': 0, 'microphone_started': False}, sort_keys=True))


if __name__ == '__main__':
    main()
