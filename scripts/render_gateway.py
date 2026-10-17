#!/usr/bin/env python3
"""Render a private, separately deployed phone-control gateway manifest."""
import argparse
import importlib.util
import json
import os
from pathlib import Path

from render_k3s import load_site, secret

MODULE = Path(__file__).resolve().parents[1] / 'containers/asterisk/phone_gateway.py'
spec = importlib.util.spec_from_file_location('phone_gateway', MODULE)
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


def render(site, config):
    gateway.validate(config)
    if config['bind_ipv4'] != site['node_ipv4'] or config['ami']['host'] != site['node_ipv4']:
        raise ValueError('Gateway and local Asterisk must use the reviewed node address')
    if not site.get('gateway', {}).get('enabled') or site['gateway'].get('ami_client_ipv4') != site['node_ipv4']:
        raise ValueError('Enable the dedicated local gateway AMI account first')
    namespace = site['namespace']
    name, config_secret = secret(namespace, 'phone-gateway', {'gateway.json': json.dumps(config, sort_keys=True)})
    labels = {'app': 'phone-gateway', 'app.kubernetes.io/part-of': 'cisco-home-telephony'}
    deployment = {'apiVersion': 'apps/v1', 'kind': 'Deployment',
        'metadata': {'name': 'phone-gateway', 'namespace': namespace, 'labels': labels},
        'spec': {'replicas': 1, 'strategy': {'type': 'Recreate'}, 'selector': {'matchLabels': {'app': 'phone-gateway'}},
            'template': {'metadata': {'labels': labels}, 'spec': {
                'hostNetwork': True, 'dnsPolicy': 'ClusterFirstWithHostNet', 'automountServiceAccountToken': False,
                'nodeSelector': {'kubernetes.io/os': 'linux'},
                'affinity': {'nodeAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': {'nodeSelectorTerms': [
                    {'matchFields': [{'key': 'metadata.name', 'operator': 'In', 'values': [site['node_name']]}]}]}}},
                'securityContext': {'runAsUser': 10001, 'runAsGroup': 10001, 'runAsNonRoot': True,
                                    'fsGroup': 10001, 'seccompProfile': {'type': 'RuntimeDefault'}},
                'imagePullSecrets': [{'name': value} for value in site.get('image_pull_secrets', [])],
                'containers': [{'name': 'gateway', 'image': site['asterisk_image'], 'imagePullPolicy': 'IfNotPresent',
                    'command': ['python3', '/usr/local/lib/cisco-phone-gateway.py', '--config', '/config/gateway.json',
                                '--state-directory', '/state'],
                    'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                        'capabilities': {'drop': ['ALL']}},
                    'resources': {'requests': {'cpu': '20m', 'memory': '64Mi'}, 'limits': {'cpu': '500m', 'memory': '256Mi'}},
                    'readinessProbe': {'tcpSocket': {'host': config['bind_ipv4'], 'port': config['api_port']},
                                       'initialDelaySeconds': 2, 'periodSeconds': 5},
                    'livenessProbe': {'tcpSocket': {'host': config['bind_ipv4'], 'port': config['api_port']},
                                      'initialDelaySeconds': 15, 'periodSeconds': 20},
                    'volumeMounts': [{'name': 'config', 'mountPath': '/config', 'readOnly': True},
                                     {'name': 'state', 'mountPath': '/state'}, {'name': 'tmp', 'mountPath': '/tmp'}]}],
                'volumes': [{'name': 'config', 'secret': {'secretName': name, 'defaultMode': 0o440}},
                            {'name': 'state', 'hostPath': {'path': '/var/lib/cisco-phone-gateway', 'type': 'Directory'}},
                            {'name': 'tmp', 'emptyDir': {'sizeLimit': '16Mi'}}]}}}}
    return {'apiVersion': 'v1', 'kind': 'List', 'items': [config_secret, deployment]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    value = render(load_site(args.site), json.loads(Path(args.config).read_text()))
    path = Path(args.output)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as target:
        json.dump(value, target, indent=2)
        target.write('\n')
    print('Private gateway manifest rendered; no cluster changes. Review host firewall and callback handover first.')


if __name__ == '__main__':
    main()
