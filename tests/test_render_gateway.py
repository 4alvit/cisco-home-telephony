"""Gateway manifest invariants; offline, no cluster or Docker required."""
import base64
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
try:
    import render_gateway
finally:
    sys.path.pop(0)


def site_fixture():
    return {'namespace': 'example-phones', 'node_name': 'example-node',
            'node_ipv4': '192.168.50.10',
            'asterisk_image': 'registry.example/asterisk@sha256:' + 'a' * 64,
            'image_pull_secrets': ['example-registry'],
            'gateway': {'enabled': True, 'ami_client_ipv4': '192.168.50.10'}}


def config_fixture():
    return {'persistent_enabled': True, 'bind_ipv4': '192.168.50.10', 'api_port': 18084,
            'api_token': 't' * 48, 'api_clients': ['192.168.50.11'],
            'ami': {'host': '192.168.50.10', 'port': 5038,
                    'username': 'gateway', 'secret': 'synthetic-ami-secret'},
            'dial_allowlist': ['600', '630'], 'ring_allowlist': ['630'],
            'softphones': [{'extension': '209', 'room': 'Example softphone'}],
            'services': [{'number': '600', 'label': 'Example diagnostic', 'kind': 'diagnostic'}],
            'phones': [{'id': '201', 'lines': ['201', '202'], 'room': 'Example phone',
                        'ipv4': '192.168.50.20', 'device': 'SEP020000000001', 'port': 18080,
                        'auth_path': '/auth/' + 'c' * 64,
                        'username': 'u' * 24, 'password': 'p' * 24}]}


class RenderGatewayTests(unittest.TestCase):
    def render(self, site=None, config=None):
        return render_gateway.render(site if site is not None else site_fixture(),
                                     config if config is not None else config_fixture())

    def parts(self):
        value = self.render()
        self.assertEqual(value['kind'], 'List')
        secret, deployment = value['items']
        return secret, deployment

    def test_renders_immutable_secret_and_single_pinned_deployment(self):
        secret, deployment = self.parts()
        self.assertEqual((secret['kind'], secret['type']), ('Secret', 'Opaque'))
        self.assertTrue(secret['immutable'])
        self.assertEqual(set(secret['data']), {'gateway.json'})
        decoded = json.loads(base64.b64decode(secret['data']['gateway.json']))
        self.assertEqual(decoded, config_fixture())
        self.assertEqual(deployment['kind'], 'Deployment')
        self.assertEqual(deployment['metadata']['namespace'], 'example-phones')
        self.assertEqual(deployment['spec']['replicas'], 1)
        self.assertEqual(deployment['spec']['strategy'], {'type': 'Recreate'})

    def test_mounts_only_the_owned_secret_and_host_state(self):
        secret, deployment = self.parts()
        pod = deployment['spec']['template']['spec']
        volumes = {volume['name']: volume for volume in pod['volumes']}
        self.assertEqual(volumes['config']['secret']['secretName'], secret['metadata']['name'])
        self.assertEqual(volumes['config']['secret']['defaultMode'], 0o440)
        self.assertEqual(volumes['state']['hostPath'],
                         {'path': '/var/lib/cisco-phone-gateway', 'type': 'Directory'})
        mounts = {mount['name']: mount for mount in pod['containers'][0]['volumeMounts']}
        self.assertTrue(mounts['config']['readOnly'])
        self.assertNotIn('readOnly', mounts['state'])

    def test_host_network_probes_and_node_affinity_use_the_site_address(self):
        _, deployment = self.parts()
        pod = deployment['spec']['template']['spec']
        self.assertTrue(pod['hostNetwork'])
        self.assertEqual(pod['dnsPolicy'], 'ClusterFirstWithHostNet')
        self.assertFalse(pod['automountServiceAccountToken'])
        terms = pod['affinity']['nodeAffinity'][
            'requiredDuringSchedulingIgnoredDuringExecution']['nodeSelectorTerms']
        self.assertEqual(terms[0]['matchFields'][0]['values'], ['example-node'])
        container = pod['containers'][0]
        for probe in ('readinessProbe', 'livenessProbe'):
            self.assertEqual(container[probe]['tcpSocket'],
                             {'host': '192.168.50.10', 'port': 18084})

    def test_container_is_unprivileged_and_uses_the_site_image(self):
        _, deployment = self.parts()
        pod = deployment['spec']['template']['spec']
        container = pod['containers'][0]
        self.assertEqual(container['image'], site_fixture()['asterisk_image'])
        self.assertEqual(container['securityContext'],
                         {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                          'capabilities': {'drop': ['ALL']}})
        self.assertEqual(pod['securityContext']['runAsUser'], 10001)
        self.assertTrue(pod['securityContext']['runAsNonRoot'])
        self.assertEqual(pod['securityContext']['seccompProfile'], {'type': 'RuntimeDefault'})
        self.assertEqual(pod['imagePullSecrets'], [{'name': 'example-registry'}])

    def test_secret_carries_no_plaintext_credentials(self):
        raw = json.dumps(self.render())
        self.assertNotIn(config_fixture()['api_token'], raw)
        self.assertNotIn(config_fixture()['ami']['secret'], raw)

    def test_refuses_ami_host_off_the_node_address(self):
        config = config_fixture()
        config['ami']['host'] = '192.168.50.99'
        with self.assertRaisesRegex(ValueError, 'reviewed node address'):
            self.render(config=config)

    def test_refuses_without_the_dedicated_local_ami_account(self):
        site = site_fixture()
        site['gateway']['enabled'] = False
        with self.assertRaisesRegex(ValueError, 'local gateway AMI account'):
            self.render(site=site)


if __name__ == '__main__':
    unittest.main()
