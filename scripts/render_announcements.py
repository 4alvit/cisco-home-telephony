#!/usr/bin/env python3
"""Render an additive announcement sidecar rollout from saved Kubernetes objects."""
import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import stat

ROOT = Path(__file__).resolve().parents[1]
CODE_PATH = '/opt/cisco-announcements'
CONFIG_PATH = '/etc/cisco-announcements/config.json'
SOURCES = ('phone_announce.py', 'phone_announce_agi.py', 'dial_audio.py', 'phone_gateway.py')


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def encoded(data):
    return {name: base64.b64encode(value.encode()).decode() for name, value in data.items()}


def resource(kind, prefix, namespace, data):
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]
    return {'apiVersion': 'v1', 'kind': kind,
            'metadata': {'name': prefix + '-' + digest, 'namespace': namespace},
            'immutable': True, **({'type': 'Opaque'} if kind == 'Secret' else {}), 'data': data}


def upsert(items, value):
    matches = [i for i, item in enumerate(items) if item['name'] == value['name']]
    require(len(matches) <= 1, 'duplicate_managed_entry')
    if matches:
        items[matches[0]] = value
    else:
        items.append(value)


def render(deployment, secret, config, source_files, dialplan):
    require(deployment.get('kind') == 'Deployment' and secret.get('kind') == 'Secret',
            'saved_deployment_and_secret_required')
    require(config.get('bind') == '127.0.0.1' and config.get('port') == 18085
            and config.get('spool') == '/var/spool/asterisk/announcements'
            and config.get('agi') == CODE_PATH + '/phone_announce_agi.py',
            'renderer_requires_loopback_api_and_shared_spool_paths')
    require(set(source_files) == set(SOURCES), 'complete_announcement_sources_required')
    namespace = deployment['metadata']['namespace']
    require(secret['metadata'].get('namespace') == namespace, 'namespace_mismatch')
    result = copy.deepcopy(deployment)
    result.pop('status', None)
    for key in ('uid', 'managedFields', 'generation', 'creationTimestamp'):
        result['metadata'].pop(key, None)
    # Keep resourceVersion for an optimistic-concurrency `kubectl replace`.
    require(result['metadata'].get('resourceVersion'), 'deployment_resource_version_required')
    annotations = result['metadata'].get('annotations', {})
    for key in ('deployment.kubernetes.io/revision', 'kubectl.kubernetes.io/last-applied-configuration'):
        annotations.pop(key, None)
    pod = result['spec']['template']['spec']
    containers, volumes = pod['containers'], pod['volumes']
    pbx = next((c for c in containers if c['name'] == 'asterisk'), None)
    require(pbx is not None, 'asterisk_container_required')
    mounts = {m['mountPath']: m for m in pbx.get('volumeMounts', [])}
    require('/etc/asterisk' in mounts and '/var/spool/asterisk' in mounts,
            'existing_config_and_spool_mounts_required')
    configuration = next(v for v in volumes if v['name'] == mounts['/etc/asterisk']['name'])
    require(configuration.get('secret', {}).get('secretName') == secret['metadata']['name'],
            'configuration_secret_changed')
    files = copy.deepcopy(secret['data'])
    extensions = base64.b64decode(files['extensions.conf'], validate=True).decode()
    include = '#include announcements.conf'
    if include not in {line.strip() for line in extensions.splitlines()}:
        extensions = extensions.rstrip() + '\n\n' + include + '\n'
    files.update(encoded({'extensions.conf': extensions, 'announcements.conf': dialplan}))
    pbx_secret = resource('Secret', 'announcements-pbx', namespace, files)
    api_secret = resource('Secret', 'announcements-api', namespace,
                          encoded({'config.json': json.dumps(config, sort_keys=True)}))
    code = resource('ConfigMap', 'announcements-code', namespace, source_files)
    configuration['secret']['secretName'] = pbx_secret['metadata']['name']
    for value in (
        {'name': 'announcements-code', 'configMap': {'name': code['metadata']['name'], 'defaultMode': 0o555}},
        {'name': 'announcements-config', 'secret': {'secretName': api_secret['metadata']['name'], 'defaultMode': 0o440}},
    ):
        upsert(volumes, value)
    code_mount = {'name': 'announcements-code', 'mountPath': CODE_PATH, 'readOnly': True}
    upsert(pbx.setdefault('volumeMounts', []), code_mount)
    probe = ['python3', '-c',
             "import socket; s=socket.create_connection(('127.0.0.1',18085),2); s.close()"]
    announcer = {'name': 'announcer', 'image': pbx['image'],
        'imagePullPolicy': pbx.get('imagePullPolicy', 'IfNotPresent'),
        'command': ['python3', '-B', CODE_PATH + '/phone_announce.py', '--config', CONFIG_PATH],
        'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                            'capabilities': {'drop': ['ALL']}},
        'resources': {'requests': {'cpu': '20m', 'memory': '64Mi'},
                      'limits': {'cpu': '1', 'memory': '256Mi'}},
        'readinessProbe': {'exec': {'command': probe}, 'initialDelaySeconds': 3, 'periodSeconds': 5},
        'livenessProbe': {'exec': {'command': probe}, 'initialDelaySeconds': 20, 'periodSeconds': 20},
        'volumeMounts': [copy.deepcopy(mounts['/etc/asterisk']),
                        copy.deepcopy(mounts['/var/spool/asterisk']), code_mount,
                        {'name': 'announcements-config', 'mountPath': '/etc/cisco-announcements', 'readOnly': True}]}
    # The AGI and service share owner-only audio and journal files. Preserve
    # container identity overrides; omitted fields still inherit the unchanged
    # pod security context or the identical image's default identity.
    for field in ('runAsUser', 'runAsGroup', 'runAsNonRoot'):
        if field in pbx.get('securityContext', {}):
            announcer['securityContext'][field] = copy.deepcopy(pbx['securityContext'][field])
    if '/tmp' in mounts:
        announcer['volumeMounts'].append(copy.deepcopy(mounts['/tmp']))
    upsert(containers, announcer)
    return {'resources': {'apiVersion': 'v1', 'kind': 'List',
                          'items': [pbx_secret, api_secret, code]}, 'deployment': result}


def load_private(path):
    fd = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and not info.st_mode & 0o077 and info.st_size <= 8 * 1024 * 1024,
                'private_json_requires_regular_owner_only_file')
        return json.load(stream)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployment', required=True)
    parser.add_argument('--asterisk-secret', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--output-directory', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    sources = {name: (ROOT / 'containers/asterisk' / name).read_text() for name in SOURCES}
    output = render(load_private(args.deployment), load_private(args.asterisk_secret),
                    load_private(args.config), sources, (ROOT / 'deploy/announcement-dialplan.conf').read_text())
    directory = Path(args.output_directory).expanduser()
    directory.mkdir(mode=0o700)  # A new directory avoids overwriting a reviewed rollout.
    for name, content in output.items():
        path = directory / (name + '.json')
        path.write_text(json.dumps(content, indent=2) + '\n')
        path.chmod(0o600)
    print('Private resources and concurrency-checked deployment replacement rendered.')


if __name__ == '__main__':
    main()
