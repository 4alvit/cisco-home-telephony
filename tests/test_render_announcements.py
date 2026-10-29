"""The optional sidecar preserves the existing PBX and private configuration."""
import base64
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import render_announcements as r


class AnnouncementRenderTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((r.ROOT / 'deploy/announcements.example.json').read_text())
        self.sources = {name: '# example source\n' for name in r.SOURCES}
        self.secret = {'kind': 'Secret', 'metadata': {'namespace': 'phones', 'name': 'existing'},
                       'data': r.encoded({'extensions.conf': '[home]\nexten => 600,1,Echo()\n',
                                          'pjsip.conf': 'unchanged endpoint data',
                                          'phone-services.conf': 'private existing HA credentials'})}
        self.deployment = {'kind': 'Deployment', 'apiVersion': 'apps/v1',
            'metadata': {'name': 'asterisk', 'namespace': 'phones', 'resourceVersion': '123',
                         'uid': 'old-uid', 'managedFields': [], 'annotations': {'keep': 'yes'}},
            'status': {'readyReplicas': 1},
            'spec': {'replicas': 1, 'template': {'spec': {'hostNetwork': True,
                'securityContext': {'runAsUser': 10001}, 'containers': [{
                    'name': 'asterisk', 'image': 'registry.example/pbx@sha256:' + 'a' * 64,
                    'env': [{'name': 'KEEP', 'value': 'yes'}],
                    'volumeMounts': [{'name': 'config', 'mountPath': '/etc/asterisk', 'readOnly': True},
                                     {'name': 'spool', 'mountPath': '/var/spool/asterisk'},
                                     {'name': 'tmp', 'mountPath': '/tmp'},
                                     {'name': 'unrelated', 'mountPath': '/other', 'readOnly': True}]}],
                'volumes': [{'name': 'config', 'secret': {'secretName': 'existing', 'defaultMode': 0o440}},
                            {'name': 'spool', 'emptyDir': {'sizeLimit': '64Mi'}},
                            {'name': 'tmp', 'emptyDir': {'sizeLimit': '64Mi'}},
                            {'name': 'unrelated', 'configMap': {'name': 'keep'}}]}}}}

    def render(self):
        return r.render(self.deployment, self.secret, self.config, self.sources,
                        '[announcement-example]\nexten => s,1,Hangup()\n')

    def test_preserves_other_routes_credentials_mounts_and_concurrency(self):
        before = copy.deepcopy((self.deployment, self.secret, self.config))
        result = self.render()
        self.assertEqual((self.deployment, self.secret, self.config), before)
        deployment = result['deployment']
        self.assertEqual(deployment['metadata']['resourceVersion'], '123')
        self.assertEqual(deployment['metadata']['annotations'], {'keep': 'yes'})
        self.assertNotIn('uid', deployment['metadata'])
        self.assertNotIn('status', deployment)
        config = result['resources']['items'][0]
        self.assertEqual(config['data']['pjsip.conf'], self.secret['data']['pjsip.conf'])
        self.assertEqual(config['data']['phone-services.conf'], self.secret['data']['phone-services.conf'])
        extensions = base64.b64decode(config['data']['extensions.conf']).decode()
        self.assertIn('exten => 600,1,Echo()', extensions)
        self.assertEqual(extensions.count('#include announcements.conf'), 1)
        pod = deployment['spec']['template']['spec']
        pbx, sidecar = pod['containers']
        self.assertEqual(pbx['env'], self.deployment['spec']['template']['spec']['containers'][0]['env'])
        self.assertIn({'name': 'unrelated', 'mountPath': '/other', 'readOnly': True}, pbx['volumeMounts'])
        self.assertEqual(sidecar['image'], pbx['image'])
        self.assertNotIn('ports', sidecar)
        self.assertTrue(sidecar['securityContext']['readOnlyRootFilesystem'])
        self.assertIn({'name': 'spool', 'mountPath': '/var/spool/asterisk'}, sidecar['volumeMounts'])
        self.assertTrue(all(resource['immutable'] for resource in result['resources']['items']))

    def test_rerender_does_not_duplicate_include_sidecar_or_mounts(self):
        first = self.render()
        self.deployment = first['deployment']
        self.secret = first['resources']['items'][0]
        second = self.render()
        self.assertEqual(first, second)

    def test_shared_spool_preserves_container_identity_overrides_without_privileges(self):
        pod = self.deployment['spec']['template']['spec']
        pod['securityContext'].update(runAsGroup=10001, runAsNonRoot=True, fsGroup=10001)
        pod['containers'][0]['securityContext'] = {
            'runAsUser': 10002, 'runAsGroup': 10003, 'runAsNonRoot': False,
            'allowPrivilegeEscalation': True, 'capabilities': {'add': ['NET_ADMIN']},
        }
        rendered = self.render()['deployment']['spec']['template']['spec']
        self.assertEqual(rendered['securityContext'], pod['securityContext'])
        security = rendered['containers'][1]['securityContext']
        self.assertEqual(security['runAsUser'], 10002)
        self.assertEqual(security['runAsGroup'], 10003)
        self.assertIs(security['runAsNonRoot'], False)
        self.assertFalse(security['allowPrivilegeEscalation'])
        self.assertEqual(security['capabilities'], {'drop': ['ALL']})

    def test_omitted_container_identity_still_inherits_pod_and_image_defaults(self):
        rendered = self.render()['deployment']['spec']['template']['spec']
        self.assertEqual(rendered['securityContext'], {'runAsUser': 10001})
        security = rendered['containers'][1]['securityContext']
        for field in ('runAsUser', 'runAsGroup', 'runAsNonRoot'):
            self.assertNotIn(field, security)

    def test_rejects_wrong_secret_or_namespace_and_missing_compare_version(self):
        for part, field, value in (('secret', 'name', 'other'), ('secret', 'namespace', 'other'),
                                   ('deployment', 'resourceVersion', None)):
            old = copy.deepcopy((self.secret, self.deployment))
            getattr(self, part)['metadata'][field] = value
            with self.assertRaises(ValueError):
                self.render()
            self.secret, self.deployment = old

    def test_rejects_public_binding_unshared_paths_and_incomplete_source_bundle(self):
        for field, value in (('bind', '0.0.0.0'), ('port', 8080), ('spool', '/tmp/private'),
                              ('agi', '/missing.py')):
            with self.subTest(field=field):
                old = self.config[field]
                self.config[field] = value
                with self.assertRaises(ValueError):
                    self.render()
                self.config[field] = old
        self.sources.pop('phone_announce_agi.py')
        with self.assertRaises(ValueError):
            self.render()

    def test_private_manifest_loader_rejects_symlinks_and_shared_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text('{}')
            path.chmod(0o600)
            self.assertEqual(r.load_private(path), {})
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                r.load_private(path)
            link = path.with_name('link.json')
            link.symlink_to(path)
            with self.assertRaises(OSError):
                r.load_private(link)


if __name__ == '__main__':
    unittest.main()
