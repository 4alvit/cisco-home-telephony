#!/usr/bin/env python3
"""Render a private, suspended manual voice pilot Job. Never contact a cluster."""
import argparse
import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = (
    'scripts/voice_session.py',
    'containers/asterisk/phone_voice_transport.py',
    'containers/asterisk/phone_voice_controller.py',
    'containers/asterisk/phone_voice_display.py',
    'containers/asterisk/phone_gateway.py',
    'containers/asterisk/phone_apps.py',
    'containers/asterisk/phone_apps_ha.py',
    'voice_assistant/__init__.py',
    'voice_assistant/config.py',
    'voice_assistant/audio.py',
    'voice_assistant/service.py',
    'voice_assistant/ha.py',
    'voice_assistant/playback.py',
)
WHEEL_NAME = 'websockets-15.0.1-py3-none-any.whl'
# PyPI's published SHA-256 for the optional pure-Python 15.0.1 wheel.
WHEEL_SHA256 = 'f7a866fbc1e97b5c617ee4116daaa09b722101d4a3c170c787450ba409f9736f'
MAX_SOURCE_FILE = 256 * 1024
MAX_CONFIG_BYTES = 256 * 1024
MAX_CONFIGMAP_BYTES = 700_000
MAX_WHEEL_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _module(name, path):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        value = importlib.util.module_from_spec(spec)
        sys.modules[name] = value
        try:
            spec.loader.exec_module(value)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


k3s = _module('_voice_job_k3s', ROOT / 'scripts/render_k3s.py')
transport = _module('_voice_job_transport', ROOT / 'containers/asterisk/phone_voice_transport.py')
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from voice_assistant.config import validate_config as validate_voice_config
from voice_assistant.service import ServiceSettings


def _read_regular(path, limit):
    """Bound reads and reject devices, directories and final-component links."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        info = os.fstat(source.fileno())
        _require(stat.S_ISREG(info.st_mode) and info.st_size <= limit, 'invalid_or_oversized_file')
        data = source.read(limit + 1)
    _require(len(data) <= limit, 'oversized_file')
    return data


def _no_links(path, base):
    _require(path.is_relative_to(base), 'path_outside_workspace')
    candidate = path
    while candidate != base:
        _require(not candidate.is_symlink(), 'symlink_not_allowed')
        candidate = candidate.parent
    _require(not base.is_symlink(), 'symlink_not_allowed')
    return path


def _private_path(value, workspace):
    root = Path(workspace).resolve()
    path = Path(value)
    _require('..' not in path.parts, 'parent_path_not_allowed')
    if not path.is_absolute():
        path = root / path
    return _no_links(path, root / '.local')


def _json(path):
    return json.loads(_read_regular(path, MAX_CONFIG_BYTES))


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _sha(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def read_sources(source_root=ROOT):
    """Capture only reviewed public modules; never recurse into private assets."""
    root = Path(source_root).resolve()
    data, items = {}, []
    for relative in SOURCE_FILES:
        path = _no_links(root / relative, root)
        content = _read_regular(path, MAX_SOURCE_FILE).decode('utf-8')
        _require('\x00' not in content, 'invalid_source_text')
        compile(content, relative, 'exec')  # Syntax check only; do not execute the snapshot.
        key = relative.replace('/', '__')
        _require(key not in data, 'source_key_collision')
        data[key] = content
        items.append({'key': key, 'path': relative})
    return data, items


def read_websockets_wheel(path):
    path = Path(path)
    _require(path.name == WHEEL_NAME, 'unsupported_websockets_wheel')
    data = _read_regular(path, MAX_WHEEL_BYTES)
    _require(hashlib.sha256(data).hexdigest() == WHEEL_SHA256, 'websockets_wheel_checksum_mismatch')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        _require(0 < len(members) <= 200 and sum(item.file_size for item in members) <= 2 * 1024 * 1024,
                 'oversized_wheel_contents')
        names = set()
        for item in members:
            name = PurePosixPath(item.filename)
            _require(item.filename not in names and not name.is_absolute()
                     and '..' not in name.parts and '\\' not in item.filename and ':' not in item.filename
                     and not stat.S_ISLNK(item.external_attr >> 16)
                     and name.suffix not in ('.so', '.dll', '.pyd', '.dylib', '.pth'),
                     'unsafe_wheel_member')
            names.add(item.filename)
        metadata = archive.read('websockets-15.0.1.dist-info/METADATA').decode('utf-8')
        wheel = archive.read('websockets-15.0.1.dist-info/WHEEL').decode('utf-8')
        _require('Name: websockets' in metadata.splitlines()
                 and 'Version: 15.0.1' in metadata.splitlines()
                 and 'Root-Is-Purelib: true' in wheel.splitlines()
                 and 'Tag: py3-none-any' in wheel.splitlines(), 'unsupported_wheel_metadata')
        _require('websockets/asyncio/client.py' in names, 'websocket_client_missing')
    return data


def _validate(site, config, gateway, voice, token, display_port):
    k3s.validate_site(site)
    transport._gateway_module().validate(gateway)
    validate_voice_config(voice)
    allowed = {'gateway_config', 'line', 'bind_ipv4', 'bind_port', 'phone_rtp_port',
               'duration_seconds', 'receive_idle_seconds', 'volume', 'expected_source_port'}
    _require(isinstance(config, dict) and not set(config) - allowed, 'invalid_transport_config')
    _require(isinstance(config.get('gateway_config'), str), 'gateway_config_required')
    transport.tx_uri(config['bind_ipv4'], config['bind_port'])
    transport.rx_uri(config['bind_ipv4'], config['phone_rtp_port'], config.get('volume', 20))
    transport._duration(config['duration_seconds'])
    transport._duration(config.get('receive_idle_seconds', 3), 10)
    if config.get('expected_source_port') is not None:
        transport._port(config['expected_source_port'], False)
    node = site['node_ipv4']
    _require(config['bind_ipv4'] == gateway['bind_ipv4'] == gateway['ami']['host'] == node,
             'voice_job_requires_local_node_bind_and_ami')
    _require(site.get('gateway', {}).get('enabled') is True
             and site['gateway'].get('ami_client_ipv4') == node, 'local_gateway_account_required')
    matches = [phone for phone in gateway['phones'] if config.get('line') in phone['lines']]
    _require(len(matches) == 1, 'unknown_phone_line')
    phone = matches[0]
    _require(voice['phones'] == {phone['id']: True}, 'one_enabled_selected_phone_required')
    settings = ServiceSettings(**voice['service'])
    _require(settings.enabled and not settings.rearm_after_turn, 'single_manual_turn_required')
    _require(voice['wake']['mode'] == 'manual', 'manual_wake_mode_required')
    _require(settings.max_utterance_seconds <= 30 and settings.turn_timeout_seconds <= 60,
             'voice_time_budget_exceeded')
    _require(isinstance(token, str) and 0 < len(token.strip()) <= 8192
             and all(33 <= ord(char) <= 126 for char in token.strip()), 'invalid_ha_token')
    if display_port is not None:
        _require(type(display_port) is int and 1024 <= display_port <= 65535, 'invalid_display_port')
        used = {gateway['api_port'], config['bind_port'], config['phone_rtp_port'], gateway['ami']['port']}
        used.update(item['port'] for item in gateway['phones'])
        _require(display_port not in used, 'display_port_collision')
    return phone


def render(site, transport_config, gateway_config, voice_config, token, *,
           source_root=ROOT, websockets_wheel=None, voice_image=None,
           display_port=None, name='phone-voice-pilot'):
    """Build inert objects. Explicitly unsuspending the Job authorizes media."""
    site, config, gateway, voice = copy.deepcopy((site, transport_config, gateway_config, voice_config))
    _require(isinstance(name, str) and re.fullmatch(r'[a-z][a-z0-9-]{0,30}[a-z0-9]|[a-z]', name),
             'invalid_job_name')
    phone = _validate(site, config, gateway, voice, token, display_port)
    image = site['asterisk_image'] if voice_image is None else voice_image
    _require(isinstance(image, str)
             and re.fullmatch(r'[^\s]+@sha256:[0-9a-f]{64}', image),
             'immutable_voice_image_required')
    # Only the AMI observer and selected phone's Execute authentication are
    # used. Do not copy unrelated HA credentials, room labels or API tokens.
    gateway = {
        'persistent_enabled': True, 'bind_ipv4': gateway['bind_ipv4'],
        'api_port': gateway['api_port'], 'api_clients': [site['node_ipv4']],
        'api_token': 'voice-pilot-unused-http-api-token-00000000',
        'ami': {key: gateway['ami'][key] for key in ('host', 'port', 'username', 'secret')},
        'dial_allowlist': [], 'ring_allowlist': [],
        'phones': [{key: phone[key] for key in
                    ('id', 'lines', 'ipv4', 'device', 'port', 'auth_path', 'username', 'password')}],
    }
    transport._gateway_module().validate(gateway)
    config['gateway_config'] = '/run/voice/.local/gateway.json'
    voice['ha']['token_file'] = '.local/ha-token.txt'
    voice['wake']['bundle_file'] = '.local/unused-manual-bundle.json'
    private_files = {'transport.json': _canonical(config), 'gateway.json': _canonical(gateway),
                     'voice.json': _canonical(voice), 'ha-token.txt': token.strip() + '\n'}
    _require(sum(len(key.encode()) + len(value.encode()) for key, value in private_files.items())
             <= MAX_CONFIG_BYTES, 'private_config_too_large')
    source_data, source_items = read_sources(source_root)
    binary_data = {}
    if websockets_wheel is not None:
        binary_data[WHEEL_NAME] = base64.b64encode(read_websockets_wheel(websockets_wheel)).decode('ascii')
        source_items.append({'key': WHEEL_NAME, 'path': '.deps/' + WHEEL_NAME})
    _require(len(_canonical({'data': source_data, 'binaryData': binary_data}).encode())
             <= MAX_CONFIGMAP_BYTES, 'source_configmap_too_large')
    namespace = site['namespace']
    source_hash = _sha({'data': source_data, 'binaryData': binary_data})
    source_name = name + '-code-' + source_hash[:16]
    secret_name, secret = k3s.secret(namespace, name + '-private', private_files)
    revision = _sha({'source': source_hash, 'secret': secret_name, 'site': site,
                     'voice_image': image,
                     'display_port': display_port, 'name': name})[:16]
    job_name = name + '-' + revision
    labels = {'app': 'phone-voice-pilot', 'app.kubernetes.io/part-of': 'cisco-home-telephony',
              'voice-pilot-revision': revision}
    source = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'immutable': True,
              'metadata': {'name': source_name, 'namespace': namespace, 'labels': labels,
                           'annotations': {'voice-pilot/source-sha256': source_hash}},
              'data': source_data}
    if binary_data:
        source['binaryData'] = binary_data
        source['metadata']['annotations']['voice-pilot/websockets-sha256'] = WHEEL_SHA256
    secret['metadata']['labels'] = labels
    paths = ['/workspace', '/workspace/containers/asterisk']
    if binary_data:
        paths.insert(0, '/workspace/.deps/' + WHEEL_NAME)
    command = ['python3', '/workspace/scripts/voice_session.py',
               '--transport-config', '/run/voice/.local/transport.json',
               '--voice-config', '/run/voice/.local/voice.json', '--workspace', '/run/voice',
               '--ffmpeg', '/usr/bin/ffmpeg', '--run', '--attended', '--exclusive-media', '--enable-microphone']
    if display_port is not None:
        command += ['--display-port', str(display_port)]
    pod = k3s.pod_base(site)
    pod.update(restartPolicy='Never', enableServiceLinks=False, terminationGracePeriodSeconds=45)
    pod['securityContext'].update(runAsNonRoot=True, runAsUser=10001, runAsGroup=10001, fsGroup=10001)
    pod['containers'] = [{
        'name': 'voice-pilot', 'image': image, 'imagePullPolicy': 'IfNotPresent',
        'workingDir': '/workspace', 'command': command,
        'env': [{'name': 'PYTHONPATH', 'value': ':'.join(paths)},
                {'name': 'PYTHONDONTWRITEBYTECODE', 'value': '1'},
                {'name': 'PYTHONUNBUFFERED', 'value': '1'}, {'name': 'TMPDIR', 'value': '/tmp'}],
        'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                            'capabilities': {'drop': ['ALL']}},
        'resources': {'requests': {'cpu': '100m', 'memory': '128Mi', 'ephemeral-storage': '64Mi'},
                      'limits': {'cpu': '1000m', 'memory': '512Mi', 'ephemeral-storage': '256Mi'}},
        'volumeMounts': [{'name': 'workspace', 'mountPath': '/workspace', 'readOnly': True},
                         {'name': 'private', 'mountPath': '/run/voice/.local', 'readOnly': True},
                         {'name': 'tmp', 'mountPath': '/tmp'}],
    }]
    pod['volumes'] = [
        {'name': 'workspace', 'projected': {'defaultMode': 0o440, 'sources': [
            {'configMap': {'name': source_name, 'items': source_items}},
        ]}},
        # A projected top-level .local would be an AtomicWriter symlink and
        # violate the runtime's private-root guard. A separate Secret mount
        # makes .local the real mount directory; only its files are symlinks.
        {'name': 'private', 'secret': {'secretName': secret_name, 'defaultMode': 0o440,
                                     'items': [{'key': key, 'path': key} for key in sorted(private_files)]}},
        {'name': 'tmp', 'emptyDir': {'sizeLimit': '64Mi'}},
    ]
    job = {'apiVersion': 'batch/v1', 'kind': 'Job',
           'metadata': {'name': job_name, 'namespace': namespace, 'labels': labels},
           'spec': {'suspend': True, 'parallelism': 1, 'completions': 1, 'backoffLimit': 0,
                    'activeDeadlineSeconds': 90, 'ttlSecondsAfterFinished': 300,
                    'template': {'metadata': {'labels': labels}, 'spec': pod}}}
    result = {'apiVersion': 'v1', 'kind': 'List', 'items': [source, secret, job]}
    _require(len(_canonical(result).encode()) <= MAX_MANIFEST_BYTES, 'manifest_too_large')
    return result


def write_private_manifest(manifest, path, workspace=ROOT):
    path = _private_path(path, workspace)
    data = json.dumps(manifest, indent=2).encode() + b'\n'
    _require(len(data) <= MAX_MANIFEST_BYTES, 'manifest_too_large')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', required=True)
    parser.add_argument('--transport-config', required=True)
    parser.add_argument('--voice-config', required=True)
    parser.add_argument('--workspace', default=str(ROOT))
    parser.add_argument('--websockets-wheel', help='optional local, pinned pure-Python 15.0.1 wheel')
    parser.add_argument('--voice-image', help='immutable voice runtime image digest; defaults to site asterisk_image')
    parser.add_argument('--display-port', type=int)
    parser.add_argument('--name', default='phone-voice-pilot')
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        root = Path(args.workspace).resolve()
        site = _json(_private_path(args.site, root))
        config = _json(_private_path(args.transport_config, root))
        gateway = _json(_private_path(config['gateway_config'], root))
        voice = _json(_private_path(args.voice_config, root))
        token = _read_regular(_private_path(voice['ha']['token_file'], root), 8193).decode('ascii')
        wheel = _private_path(args.websockets_wheel, root) if args.websockets_wheel else None
        manifest = render(site, config, gateway, voice, token, source_root=root,
                          websockets_wheel=wheel, voice_image=args.voice_image,
                          display_port=args.display_port, name=args.name)
        write_private_manifest(manifest, args.output, workspace=root)
    except Exception:
        print('Voice Job render failed: check private inputs, bounds, local wheel and exclusive output path.',
              file=sys.stderr)
        return 2
    print('Private suspended voice Job rendered. No cluster changes or phone actions. '
          'Review before explicitly unsuspending with an attendant present.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
