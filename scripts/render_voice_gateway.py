#!/usr/bin/env python3
"""Render a private replacement for one existing gateway; never access a cluster.

Image and capacity receipts are operator/tool attestations, not independent image
or scheduler verification. Read docs/voice-gateway-deployment.md before applying.
"""
import argparse
import base64
import copy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MAX_FILE = 256 * 1024
MAX_MANIFEST = 2 * 1024 * 1024
RUNTIME = '/run/voice'
CONFIG = RUNTIME + '/.local/config'
STATE = RUNTIME + '/.local/state'
MANAGED = 'voice-gateway.cisco-home-telephony/renderer'
IMAGE = re.compile(r'[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}\Z')

# The emptyDir root is kubelet-owned. Only children created by this UID are
# private. Init never mounts or chmods the persistent state volume.
INIT_CODE = """import os, sys
from pathlib import Path
os.umask(0o077)
root = Path('/runtime/private')
for path in (root, root / '.local', root / '.local/config', root / '.local/state'):
    path.mkdir(mode=0o700)
    path.chmod(0o700)
sys.path.insert(0, '/opt/voice')
from stage_private import stage_private
stage_private('/run/voice-inputs', root / '.local/config', '/run/voice-inputs/staging.json')
"""

# Fail closed instead of changing ownership/modes of existing persistent data.
# exec keeps SIGTERM handling in the actual gateway process.
MAIN_CODE = """import os, stat, sys
from pathlib import Path
os.umask(0o077)
path = Path(sys.argv[sys.argv.index('--state-directory') + 1])
for parent in (path, *path.parents):
    if parent.is_symlink():
        raise SystemExit('gateway_state_symlink_refused')
info = path.stat()
if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
    raise SystemExit('gateway_state_requires_current_owner_and_0700')
os.execv('/usr/local/bin/python3', ['/usr/local/bin/python3', '/workspace/containers/asterisk/phone_gateway.py', *sys.argv[1:]])
"""


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def module(name, path):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        value = importlib.util.module_from_spec(spec)
        sys.modules[name] = value
        spec.loader.exec_module(value)
    return sys.modules[name]


gateway = module('_voice_gateway_render_gateway', ROOT / 'containers/asterisk/phone_gateway.py')
control = module('_voice_gateway_render_control', ROOT / 'containers/asterisk/phone_voice_control.py')
stager = module('_voice_gateway_render_stager', ROOT / 'containers/voice/stage_private.py')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def private_path(value, workspace):
    root = Path(workspace).resolve()
    path = Path(value)
    require('..' not in path.parts, 'private_path_traversal')
    if not path.is_absolute():
        path = root / path
    base = root / '.local'
    require(path != base and path.is_relative_to(base), 'private_path_required')
    for parent in (path, *path.parents):
        require(not parent.is_symlink(), 'private_symlink_refused')
        if parent == root:
            break
    return path


def read_private(value, workspace, limit=MAX_FILE):
    path = private_path(value, workspace)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and not info.st_mode & 0o077 and info.st_size <= limit,
                'owner_only_regular_file_required')
        data = stream.read(limit + 1)
    require(len(data) <= limit, 'private_file_too_large')
    return data


def read_json(value, workspace, limit=MAX_FILE):
    return json.loads(read_private(value, workspace, limit), object_pairs_hook=unique_object)


def quantity(value, resource):
    require(isinstance(value, str), 'resource_quantity_must_be_string')
    if resource == 'cpu':
        match = re.fullmatch(r'(0|[1-9][0-9]*)(\.[0-9]{1,3})?(m?)', value)
        require(match is not None, 'invalid_cpu_quantity')
        result = Decimal(value[:-1] if value.endswith('m') else value)
        result *= 1 if value.endswith('m') else 1000
        require(result == result.to_integral_value(), 'sub_millicpu_not_supported')
        return int(result)
    match = re.fullmatch(r'(0|[1-9][0-9]*)(Ki|Mi|Gi|Ti)?', value)
    require(match is not None, 'invalid_memory_quantity')
    return int(match[1]) * (1024 ** ({None: 0, 'Ki': 1, 'Mi': 2, 'Gi': 3, 'Ti': 4}[match[2]]))


def resource_pair(value):
    require(isinstance(value, dict) and set(value) == {'cpu', 'memory'}, 'cpu_and_memory_required')
    return {key: quantity(value[key], key) for key in ('cpu', 'memory')}


def clean_deployment(value):
    value = copy.deepcopy(value)
    require(value.get('apiVersion') == 'apps/v1' and value.get('kind') == 'Deployment',
            'single_deployment_object_required')
    value.pop('status', None)
    meta = value['metadata']
    for key in ('uid', 'resourceVersion', 'generation', 'creationTimestamp', 'managedFields', 'selfLink'):
        meta.pop(key, None)
    annotations = meta.get('annotations', {})
    annotations.pop('kubectl.kubernetes.io/last-applied-configuration', None)
    annotations.pop('deployment.kubernetes.io/revision', None)
    require(not meta.get('deletionTimestamp') and not meta.get('ownerReferences'), 'owned_or_deleting_deployment')
    return value


def validate_context(deployment, config, capacity, receipt, image, max_age, now):
    meta, pod = deployment['metadata'], deployment['spec']['template']['spec']
    require(IMAGE.fullmatch(image) is not None, 'immutable_image_required')
    require(receipt.get('verified') is True and receipt.get('runtime_resolution_verified') is True
            and receipt.get('smoke_passed') is True, 'verified_image_receipt_required')
    require(receipt.get('immutable_image_reference') == image
            and receipt.get('manifest_digest') == image.split('@')[1]
            and re.fullmatch(r'[0-9a-f]{40}', receipt.get('source_commit', '')),
            'image_receipt_mismatch')
    require(receipt.get('node_name') == capacity.get('node_name')
            and receipt.get('kube_context') == capacity.get('kube_context'), 'image_runtime_context_mismatch')
    require(capacity.get('version') == 1 and type(capacity.get('version')) is int
            and capacity.get('kube_context') and capacity.get('namespace') == meta['namespace']
            and capacity.get('deployment') == meta['name'], 'capacity_context_mismatch')
    arch = capacity.get('node_arch')
    require(arch in ('amd64', 'arm64') and receipt.get('platform') == 'linux/' + arch,
            'image_platform_mismatch')
    require(type(max_age) is int and 60 <= max_age <= 3600, 'invalid_capacity_max_age')
    captured = datetime.fromisoformat(capacity['captured_at'].replace('Z', '+00:00'))
    require(captured.utcoffset() is not None, 'capacity_timestamp_requires_timezone')
    require(0 <= (now - captured).total_seconds() <= max_age, 'stale_or_future_capacity')
    require(config['bind_ipv4'] == config['ami']['host'] == capacity['node_ipv4'], 'local_node_bind_required')
    require(pod.get('hostNetwork') is True and not pod.get('hostPID') and not pod.get('hostIPC'),
            'host_network_only_required')
    terms = pod.get('affinity', {}).get('nodeAffinity', {}).get(
        'requiredDuringSchedulingIgnoredDuringExecution', {}).get('nodeSelectorTerms', [])
    required_pin = {'key': 'metadata.name', 'operator': 'In', 'values': [capacity['node_name']]}
    require(terms and all(required_pin in term.get('matchFields', []) for term in terms),
            'exact_node_affinity_required')
    require(not pod.get('nodeName') or pod['nodeName'] == capacity['node_name'], 'node_name_mismatch')
    selectors = pod.get('nodeSelector', {})
    require(selectors.get('kubernetes.io/os', 'linux') == 'linux'
            and selectors.get('kubernetes.io/arch', arch) == arch, 'node_selector_platform_mismatch')
    require(not pod.get('resources') and not pod.get('ephemeralContainers'), 'unsupported_pod_resources')
    # RuntimeClass overhead is injected into admitted Pods and may be absent
    # from this Deployment snapshot, so an offline capacity check cannot use it.
    require(not pod.get('runtimeClassName'), 'runtime_class_not_supported')


def build_private_files(config, profiles, workspace, enabled):
    # Preserve every field, including future gateway metadata, applications,
    # credentials and the complete physical/softphone inventory.
    files = {'gateway.json': canonical(config)}
    require(len(files['gateway.json'].encode()) <= 128 * 1024, 'gateway_config_too_large')
    require(type(enabled) is bool, 'enable_voice_must_be_boolean')
    if profiles is None:
        require(not enabled, 'profiles_required_to_enable_voice')
        return files
    require(isinstance(profiles, dict) and set(profiles) == {'version', 'profiles'}
            and type(profiles['version']) is int and profiles['version'] == 1
            and isinstance(profiles['profiles'], list) and 1 <= len(profiles['profiles']) <= 32,
            'invalid_profiles_document')
    entries = []
    for index, profile in enumerate(profiles['profiles']):
        require(isinstance(profile, dict) and {'transport_config', 'voice_config'} <= set(profile)
                <= {'transport_config', 'voice_config', 'display_port'}, 'invalid_profile_entry')
        transport = read_json(profile['transport_config'], workspace)
        voice = read_json(profile['voice_config'], workspace)
        require(read_json(transport['gateway_config'], workspace) == config, 'profile_gateway_mismatch')
        require(transport['bind_ipv4'] == config['bind_ipv4'], 'profile_bind_mismatch')
        token = read_private(voice['ha']['token_file'], workspace, 8192).decode('ascii').strip()
        transport['gateway_config'] = CONFIG + '/gateway.json'
        voice['ha']['token_file'] = CONFIG + '/token-' + str(index) + '.txt'
        # Manual profiles never read a wake bundle; remove the local pathname.
        require(voice['wake']['mode'] == 'manual', 'manual_profiles_only')
        voice['wake']['bundle_file'] = CONFIG + '/unused-manual-bundle.json'
        files['transport-' + str(index) + '.json'] = canonical(transport)
        files['voice-' + str(index) + '.json'] = canonical(voice)
        files['token-' + str(index) + '.txt'] = token + '\n'
        entry = {'transport_config': CONFIG + '/transport-' + str(index) + '.json',
                 'voice_config': CONFIG + '/voice-' + str(index) + '.json'}
        if 'display_port' in profile:
            entry['display_port'] = profile['display_port']
        entries.append(entry)
    files['control.json'] = canonical({'version': 1, 'enabled': enabled,
        'state_directory': STATE + '/voice-control', 'python_executable': '/usr/local/bin/python3',
        'ffmpeg_executable': '/usr/bin/ffmpeg', 'profiles': entries})
    require(len(files) <= 128 and all(len(v.encode()) <= MAX_FILE for v in files.values())
            and sum(len(v.encode()) for v in files.values()) <= 700_000, 'private_config_too_large')
    validate_staged_profiles(files)
    return files


def validate_staged_profiles(files):
    """Use the real stager and control loader without executing any providers.

    Executable paths are mapped to this interpreter only for the offline loader's
    existence check. The deployment still uses the fixed image paths. No Gateway
    object is constructed: its constructor would write SQLite state.
    """
    with tempfile.TemporaryDirectory(prefix='voice-gateway-validate-') as directory:
        root = Path(directory).resolve()
        source = root / 'input'
        target = root / '.local/config'
        source.mkdir(mode=0o700)
        target.mkdir(mode=0o700, parents=True)
        translated = copy.deepcopy(files)
        for name, value in list(translated.items()):
            if name == 'gateway.json' or not name.endswith('.json'):
                continue
            data = json.loads(value)
            def remap(path):
                require(path.startswith(RUNTIME + '/'), 'invalid_generated_private_path')
                return str(root) + path[len(RUNTIME):]
            if name.startswith('transport-'):
                data['gateway_config'] = remap(data['gateway_config'])
            elif name.startswith('voice-'):
                data['ha']['token_file'] = remap(data['ha']['token_file'])
                data['wake']['bundle_file'] = remap(data['wake']['bundle_file'])
            elif name == 'control.json':
                data['state_directory'] = remap(data['state_directory'])
                data['python_executable'] = sys.executable
                data['ffmpeg_executable'] = sys.executable
                for entry in data['profiles']:
                    for key in ('transport_config', 'voice_config'):
                        entry[key] = remap(entry[key])
            translated[name] = canonical(data)
        for name, value in translated.items():
            path = source / name
            path.write_text(value)
            path.chmod(0o600)
        manifest = source / 'staging.json'
        manifest.write_text(canonical({'version': 1, 'files': sorted(files)}))
        manifest.chmod(0o600)
        stager.stage_private(source, target, manifest)
        loaded = control.load_control_config(target / 'control.json', root)
        # Cross-profile transport/HTTP ports must not overlap, including a
        # display port colliding with another profile's RTP receive endpoint.
        ports = set()
        config = json.loads(translated['gateway.json'])
        reserved = {config['api_port'], config['ami']['port'], *(p['port'] for p in config['phones'])}
        for profile in loaded.profiles:
            for port in (profile.bind_port, profile.display_port):
                if port is not None:
                    require(port not in ports and port not in reserved, 'profile_port_collision')
                    ports.add(port)


def state_binding(pod, container):
    args = container.get('command', []) + container.get('args', [])
    require(args.count('--state-directory') == 1, 'explicit_state_directory_required')
    path = args[args.index('--state-directory') + 1]
    require(isinstance(path, str) and PurePosixPath(path).is_absolute() and '..' not in PurePosixPath(path).parts
            and path != '/' and not path.startswith(('/run/voice', '/workspace')), 'unsupported_state_path')
    matches = [m for m in container.get('volumeMounts', []) if m.get('mountPath') == path]
    require(len(matches) == 1, 'exact_persistent_state_mount_required')
    mount = matches[0]
    require(set(mount) <= {'name', 'mountPath', 'readOnly'} and not mount.get('readOnly'),
            'state_subpath_or_readonly_not_supported')
    volumes = [v for v in pod['volumes'] if v['name'] == mount['name']]
    require(len(volumes) == 1 and set(volumes[0]) == {'name', 'hostPath'}
            and volumes[0]['hostPath'].get('type') == 'Directory', 'existing_hostpath_directory_required')
    host = PurePosixPath(volumes[0]['hostPath']['path'])
    require(host.is_absolute() and '..' not in host.parts and str(host) != '/', 'invalid_state_hostpath')
    return path, mount


def render(deployment, config, receipt, capacity, *, image, cpu_request, cpu_limit,
           memory_request='128Mi', memory_limit='512Mi', profiles=None, enable_voice=False,
           workspace=ROOT, grace_seconds=75, max_capacity_age_seconds=900, now=None):
    deployment = clean_deployment(deployment)
    config = copy.deepcopy(config)
    gateway.validate(config)
    now = now or datetime.now(timezone.utc)
    validate_context(deployment, config, capacity, receipt, image, max_capacity_age_seconds, now)
    pod = deployment['spec']['template']['spec']
    require(deployment['spec'].get('replicas') == 1
            and type(deployment['spec']['replicas']) is int
            and deployment['spec'].get('strategy', {}).get('type') == 'Recreate',
            'existing_single_recreate_gateway_required')
    require(len(pod['containers']) == 1, 'single_gateway_container_required')
    container = pod['containers'][0]
    require(container['name'] == 'gateway' and not container.get('lifecycle'), 'unsupported_gateway_container')
    managed = deployment['spec']['template']['metadata'].get('annotations', {}).get(MANAGED) == '1'
    init = pod.get('initContainers', [])
    require(not init or (managed and len(init) == 1 and init[0].get('name') == 'stage-voice-private'
                        and init[0].get('command') == ['/usr/local/bin/python3', '-c', INIT_CODE]),
            'foreign_init_containers_not_supported')
    path, mount = state_binding(pod, container)
    require(type(grace_seconds) is int and 60 <= grace_seconds <= 600, 'grace_must_be_60_to_600_seconds')
    resources = {'requests': {'cpu': cpu_request, 'memory': memory_request},
                 'limits': {'cpu': cpu_limit, 'memory': memory_limit}}
    requests, limits = resource_pair(resources['requests']), resource_pair(resources['limits'])
    available, other = resource_pair(capacity['allocatable']), resource_pair(capacity['other_requests'])
    overhead = {key: quantity(pod.get('overhead', {}).get(key, '0'), key) for key in ('cpu', 'memory')}
    require(set(pod.get('overhead', {})) <= {'cpu', 'memory'}, 'unsupported_pod_overhead')
    init_requests = {'cpu': 10, 'memory': 32 * 1024 * 1024}
    for key in requests:
        require(0 < requests[key] <= limits[key] and available[key] > 0, 'invalid_resource_request_or_limit')
        require(other[key] + max(requests[key], init_requests[key]) + overhead[key] <= available[key],
                'insufficient_' + key + '_capacity')
    files = build_private_files(config, profiles, workspace, enable_voice)
    digest = hashlib.sha256(canonical(files).encode()).hexdigest()
    secret_name = deployment['metadata']['name'][:36].rstrip('-') + '-voice-' + digest[:16]
    secret = {'apiVersion': 'v1', 'kind': 'Secret', 'immutable': True, 'type': 'Opaque',
              'metadata': {'name': secret_name, 'namespace': deployment['metadata']['namespace']},
              'data': {name: base64.b64encode(value.encode()).decode() for name, value in files.items()}}
    secret['data']['staging.json'] = base64.b64encode(canonical(
        {'version': 1, 'files': sorted(files)}).encode()).decode()
    mounts = container.get('volumeMounts', [])
    owned_names = {'voice-runtime', 'voice-inputs'}
    require(len({v['name'] for v in pod['volumes']}) == len(pod['volumes']), 'duplicate_volume_names')
    for existing in mounts:
        destination = existing['mountPath'].rstrip('/')
        if managed and (existing['name'] == 'voice-runtime' or destination == STATE):
            continue
        require(not any(destination == p or destination.startswith(p + '/') or p.startswith(destination + '/')
                        for p in (RUNTIME, '/workspace', '/opt/voice', '/usr/local', '/usr/bin')),
                'image_or_runtime_overlay_not_supported')
    require(managed or not any(v['name'] in owned_names for v in pod['volumes']), 'reserved_volume_name')
    container['volumeMounts'] = [m for m in mounts if m['name'] not in owned_names and m['mountPath'] != STATE]
    container['volumeMounts'] += [{'name': 'voice-runtime', 'mountPath': RUNTIME, 'subPath': 'private'},
                                 {'name': mount['name'], 'mountPath': STATE}]
    pod['volumes'] = [v for v in pod['volumes'] if v['name'] not in owned_names]
    pod['volumes'] += [{'name': 'voice-runtime', 'emptyDir': {'sizeLimit': '8Mi'}},
                       {'name': 'voice-inputs', 'secret': {'secretName': secret_name, 'defaultMode': 0o440}}]
    scratch = [m for m in container['volumeMounts'] if m['mountPath'] == '/tmp']
    require(len(scratch) == 1, 'existing_tmp_emptydir_required')
    volume = next(v for v in pod['volumes'] if v['name'] == scratch[0]['name'])
    require(set(volume) == {'name', 'emptyDir'} and not scratch[0].get('readOnly'), 'tmp_must_be_emptydir')
    volume['emptyDir']['sizeLimit'] = '64Mi'
    security = {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                'capabilities': {'drop': ['ALL']}, 'runAsUser': 10001, 'runAsGroup': 10001, 'runAsNonRoot': True}
    container.update(image=image, imagePullPolicy='IfNotPresent', workingDir='/workspace', resources=resources,
                     securityContext=copy.deepcopy(security), command=['/usr/local/bin/python3', '-c', MAIN_CODE],
                     args=['--config', CONFIG + '/gateway.json', '--state-directory', path])
    if profiles is not None:
        container['args'] += ['--voice-control-config', CONFIG + '/control.json', '--voice-workspace', RUNTIME]
    environment = {'PYTHONPATH': '/workspace:/workspace/containers/asterisk',
                   'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1', 'TMPDIR': '/tmp'}
    container['env'] = [entry for entry in container.get('env', []) if entry['name'] not in environment]
    container['env'] += [{'name': name, 'value': value} for name, value in environment.items()]
    pod.update(automountServiceAccountToken=False, enableServiceLinks=False,
               terminationGracePeriodSeconds=max(grace_seconds, pod.get('terminationGracePeriodSeconds', 0)),
               dnsPolicy='ClusterFirstWithHostNet')
    pod['securityContext'] = {'runAsUser': 10001, 'runAsGroup': 10001, 'runAsNonRoot': True,
                              'fsGroup': 10001, 'seccompProfile': {'type': 'RuntimeDefault'}}
    pod.setdefault('nodeSelector', {})['kubernetes.io/os'] = 'linux'
    pod['nodeSelector']['kubernetes.io/arch'] = capacity['node_arch']
    pod['initContainers'] = [{'name': 'stage-voice-private', 'image': image, 'imagePullPolicy': 'IfNotPresent',
        'command': ['/usr/local/bin/python3', '-c', INIT_CODE], 'securityContext': copy.deepcopy(security),
        'resources': {'requests': {'cpu': '10m', 'memory': '32Mi'}, 'limits': {'cpu': '200m', 'memory': '64Mi'}},
        'volumeMounts': [{'name': 'voice-runtime', 'mountPath': '/runtime'},
                         {'name': 'voice-inputs', 'mountPath': '/run/voice-inputs', 'readOnly': True}]}]
    deployment['spec'].update(replicas=1, strategy={'type': 'Recreate'})
    annotations = deployment['spec']['template']['metadata'].setdefault('annotations', {})
    annotations.update({MANAGED: '1', 'voice-gateway.cisco-home-telephony/config-sha256': digest,
                        'voice-gateway.cisco-home-telephony/source-commit': receipt['source_commit']})
    result = {'apiVersion': 'v1', 'kind': 'List', 'items': [secret, deployment]}
    require(len(canonical(result).encode()) <= MAX_MANIFEST, 'manifest_too_large')
    return result


def write_private_manifest(value, path, workspace):
    path = private_path(path, workspace)
    data = (json.dumps(value, indent=2) + '\n').encode()
    require(len(data) <= MAX_MANIFEST, 'manifest_too_large')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('deployment', 'gateway-config', 'image', 'image-verification', 'capacity',
                 'cpu-request', 'cpu-limit', 'output'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--workspace', default=str(ROOT))
    parser.add_argument('--profiles')
    parser.add_argument('--enable-voice', action='store_true')
    parser.add_argument('--memory-request', default='128Mi')
    parser.add_argument('--memory-limit', default='512Mi')
    parser.add_argument('--grace-seconds', type=int, default=75)
    parser.add_argument('--max-capacity-age-seconds', type=int, default=900)
    args = parser.parse_args(argv)
    try:
        result = render(read_json(args.deployment, args.workspace, MAX_MANIFEST),
                        read_json(args.gateway_config, args.workspace),
                        read_json(args.image_verification, args.workspace),
                        read_json(args.capacity, args.workspace), image=args.image,
                        cpu_request=args.cpu_request, cpu_limit=args.cpu_limit,
                        memory_request=args.memory_request, memory_limit=args.memory_limit,
                        profiles=read_json(args.profiles, args.workspace) if args.profiles else None,
                        enable_voice=args.enable_voice, workspace=args.workspace,
                        grace_seconds=args.grace_seconds, max_capacity_age_seconds=args.max_capacity_age_seconds)
        write_private_manifest(result, args.output, args.workspace)
    except Exception:
        print('Voice gateway render failed: check private inputs, image receipt, fresh capacity, '
              'supported deployment/state layout and unused output path.', file=sys.stderr)
        return 2
    print('Private replacement gateway rendered; voice ' + ('enabled' if args.enable_voice else 'disabled')
          + '. No cluster access or phone actions. Requests reserve scheduling capacity; they do not guarantee latency.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
