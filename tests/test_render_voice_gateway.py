"""Offline migration contract and refusal tests; no Docker or cluster required."""
import base64
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('render_voice_gateway_test', ROOT / 'scripts/render_voice_gateway.py')
r = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = r
SPEC.loader.exec_module(r)


def gateway_fixture():
    return {'persistent_enabled': True, 'bind_ipv4': '192.168.50.10', 'api_port': 18084,
        'api_token': 'a' * 48, 'api_clients': ['192.168.50.11'],
        'ami': {'host': '192.168.50.10', 'port': 5038, 'username': 'example', 'secret': 'example-secret'},
        'dial_allowlist': ['600', '630'], 'ring_allowlist': ['630'],
        'softphones': [{'extension': '209', 'room': 'Example softphone'}],
        'services': [{'number': '600', 'label': 'Example diagnostic', 'kind': 'diagnostic'}],
        'future_setting': {'preserve': ['complete', 'object']},
        'phones': [{'id': '201', 'lines': ['201', '202'], 'room': 'Example phone',
                    'ipv4': '192.168.50.20', 'device': 'SEP020000000001', 'port': 18080,
                    'auth_path': '/auth/' + 'c' * 64, 'username': 'u' * 24, 'password': 'p' * 24}]}


def deployment_fixture():
    return {'apiVersion': 'apps/v1', 'kind': 'Deployment',
        'metadata': {'name': 'phone-gateway', 'namespace': 'example-phones', 'uid': 'server-only',
                     'resourceVersion': '10', 'managedFields': [],
                     'annotations': {'operator-note': 'preserve', 'deployment.kubernetes.io/revision': '3'}},
        'status': {'readyReplicas': 1},
        'spec': {'replicas': 1, 'strategy': {'type': 'Recreate'},
                 'selector': {'matchLabels': {'app': 'phone-gateway'}},
                 'template': {'metadata': {'labels': {'app': 'phone-gateway'}}, 'spec': {
            'hostNetwork': True, 'dnsPolicy': 'ClusterFirstWithHostNet',
            'affinity': {'nodeAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': {
                'nodeSelectorTerms': [{'matchFields': [{'key': 'metadata.name', 'operator': 'In',
                                                       'values': ['example-node']}]}]}}},
            'nodeSelector': {'kubernetes.io/os': 'linux'}, 'imagePullSecrets': [{'name': 'example-registry'}],
            'containers': [{'name': 'gateway', 'image': 'registry.example/old@sha256:' + 'f' * 64,
                'command': ['python3', '/usr/local/lib/cisco-phone-gateway.py', '--config',
                            '/config/gateway.json', '--state-directory', '/state'],
                'env': [{'name': 'EXAMPLE_SETTING', 'value': 'keep'}],
                'ports': [{'name': 'api', 'containerPort': 18084}],
                'readinessProbe': {'tcpSocket': {'host': '192.168.50.10', 'port': 18084}},
                'volumeMounts': [{'name': 'config', 'mountPath': '/config', 'readOnly': True},
                                 {'name': 'state', 'mountPath': '/state'},
                                 {'name': 'tmp', 'mountPath': '/tmp'}]}],
            'volumes': [{'name': 'config', 'secret': {'secretName': 'old-config'}},
                        {'name': 'state', 'hostPath': {'path': '/var/lib/example-phone-gateway', 'type': 'Directory'}},
                        {'name': 'tmp', 'emptyDir': {'sizeLimit': '16Mi'}}]}}}}


class RenderVoiceGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        (self.workspace / '.local').mkdir(mode=0o700)
        self.now = datetime.now(timezone.utc)
        self.gateway = gateway_fixture()
        self.deployment = deployment_fixture()
        self.image = 'registry.example/voice@sha256:' + 'abcdef01' * 8
        self.receipt = {'verified': True, 'runtime_resolution_verified': True, 'smoke_passed': True,
                        'node_name': 'example-node', 'kube_context': 'example-context',
                        'immutable_image_reference': self.image, 'manifest_digest': self.image.split('@')[1],
                        'source_commit': '0123456789' * 4, 'platform': 'linux/amd64'}
        self.capacity = {'version': 1, 'kube_context': 'example-context', 'node_name': 'example-node',
            'node_ipv4': '192.168.50.10', 'node_arch': 'amd64', 'namespace': 'example-phones',
            'deployment': 'phone-gateway', 'captured_at': self.now.isoformat(),
            'allocatable': {'cpu': '2', 'memory': '4Gi'}, 'other_requests': {'cpu': '1', 'memory': '1Gi'}}
        self.transport = {'gateway_config': '.local/gateway.json', 'line': '201',
                          'bind_ipv4': '192.168.50.10', 'bind_port': 24576, 'phone_rtp_port': 24578,
                          'duration_seconds': 10, 'receive_idle_seconds': 3, 'volume': 20}
        self.voice = {'service': {'enabled': True, 'rearm_after_turn': False}, 'phones': {'201': True},
                      'ha': {'base_url': 'http://192.168.50.30:8123', 'pipeline_id': 'example-pipeline',
                             'token_file': '.local/ha-token.txt'},
                      'wake': {'mode': 'manual', 'bundle_file': '.local/unused-bundle.json'}}
        self.profiles = {'version': 1, 'profiles': [{'transport_config': '.local/transport.json',
                                                   'voice_config': '.local/voice.json', 'display_port': 18090}]}
        self.write('gateway.json', self.gateway)
        self.write('transport.json', self.transport)
        self.write('voice.json', self.voice)
        self.write('ha-token.txt', 'synthetic-token\n')

    def write(self, name, value):
        path = self.workspace / '.local' / name
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        path.chmod(0o600)
        return path

    def render(self, **kwargs):
        defaults = dict(image=self.image, cpu_request='100m', cpu_limit='1', workspace=self.workspace, now=self.now)
        defaults.update(kwargs)
        return r.render(self.deployment, self.gateway, self.receipt, self.capacity, **defaults)

    def pod(self, result):
        return result['items'][1]['spec']['template']['spec']

    def files(self, result):
        return {k: base64.b64decode(v).decode() for k, v in result['items'][0]['data'].items()}

    def test_full_gateway_config_and_inputs_preserved(self):
        before = copy.deepcopy((self.gateway, self.deployment, self.capacity, self.receipt))
        result = self.render()
        self.assertEqual(json.loads(self.files(result)['gateway.json']), self.gateway)
        self.assertEqual((self.gateway, self.deployment, self.capacity, self.receipt), before)

    def test_single_recreate_identity_affinity_and_non_runtime_settings_preserved(self):
        result = self.render()
        new = result['items'][1]
        pod = self.pod(result)
        oldpod = self.deployment['spec']['template']['spec']
        self.assertEqual(new['metadata']['name'], 'phone-gateway')
        self.assertEqual(new['spec']['selector'], self.deployment['spec']['selector'])
        self.assertEqual(new['spec']['replicas'], 1)
        self.assertEqual(new['spec']['strategy'], {'type': 'Recreate'})
        self.assertEqual(pod['affinity'], oldpod['affinity'])
        self.assertEqual(pod['imagePullSecrets'], oldpod['imagePullSecrets'])
        self.assertEqual(pod['containers'][0]['ports'], oldpod['containers'][0]['ports'])
        self.assertEqual(pod['containers'][0]['readinessProbe'], oldpod['containers'][0]['readinessProbe'])
        self.assertEqual(pod['terminationGracePeriodSeconds'], 180)
        self.assertNotIn('status', new)
        self.assertNotIn('uid', new['metadata'])
        self.assertEqual(new['metadata']['annotations'], {'operator-note': 'preserve'})

    def test_disabled_default_without_profiles_is_valid(self):
        result = self.render()
        self.assertNotIn('control.json', self.files(result))
        self.assertNotIn('--voice-control-config', self.pod(result)['containers'][0]['args'])

    def test_profiles_staged_disabled_and_explicit_enable(self):
        for enabled in (False, True):
            result = self.render(profiles=self.profiles, enable_voice=enabled)
            files = self.files(result)
            control = json.loads(files['control.json'])
            self.assertIs(control['enabled'], enabled)
            self.assertEqual(control['state_directory'], '/run/voice/.local/state/voice-control')
            self.assertEqual(control['python_executable'], '/usr/local/bin/python3')
            self.assertEqual(control['ffmpeg_executable'], '/usr/bin/ffmpeg')
            self.assertEqual(json.loads(files['transport-0.json'])['gateway_config'], r.CONFIG + '/gateway.json')
            self.assertEqual(json.loads(files['voice-0.json'])['ha']['token_file'], r.CONFIG + '/token-0.txt')
            self.assertEqual(files['token-0.txt'], 'synthetic-token\n')
            self.assertNotIn(str(self.workspace), json.dumps(result))

    def test_enable_requires_profiles_and_empty_profiles_refused(self):
        with self.assertRaisesRegex(ValueError, 'profiles_required'):
            self.render(enable_voice=True)
        with self.assertRaisesRegex(ValueError, 'invalid_profiles'):
            self.render(profiles={'version': 1, 'profiles': []})

    def test_state_mount_stays_exact_and_alias_preserves_journals(self):
        result = self.render(profiles=self.profiles)
        pod = self.pod(result)
        old = self.deployment['spec']['template']['spec']
        self.assertEqual(next(v for v in pod['volumes'] if v['name'] == 'state'), old['volumes'][1])
        mounts = pod['containers'][0]['volumeMounts']
        self.assertIn({'name': 'state', 'mountPath': '/state'}, mounts)
        self.assertIn({'name': 'state', 'mountPath': r.STATE}, mounts)
        self.assertEqual(pod['containers'][0]['args'][3], '/state')
        self.assertNotIn('state', [m['name'] for m in pod['initContainers'][0]['volumeMounts']])
        self.assertNotIn('chown', r.MAIN_CODE + r.INIT_CODE)

    def test_private_staging_and_code_stay_image_owned(self):
        result = self.render(profiles=self.profiles)
        secret = result['items'][0]
        pod = self.pod(result)
        self.assertIs(secret['immutable'], True)
        files = self.files(result)
        self.assertEqual(json.loads(files.pop('staging.json'))['files'], sorted(files))
        for container in pod['containers'] + pod['initContainers']:
            self.assertEqual(container['image'], self.image)
            self.assertEqual(container['securityContext']['runAsUser'], 10001)
            self.assertIs(container['securityContext']['readOnlyRootFilesystem'], True)
            self.assertNotIn('/workspace', [m['mountPath'] for m in container['volumeMounts']])
        self.assertIn({'name': 'voice-runtime', 'mountPath': '/run/voice', 'subPath': 'private'},
                      pod['containers'][0]['volumeMounts'])
        self.assertIn('path.chmod(0o700)', r.INIT_CODE)
        self.assertFalse(pod['automountServiceAccountToken'])

    def test_actual_stager_and_control_loader_used_without_gateway_construction(self):
        with mock.patch.object(r.gateway, 'Gateway', side_effect=AssertionError('no SQLite writes')), \
             mock.patch.object(r.stager, 'stage_private', wraps=r.stager.stage_private) as stage, \
             mock.patch.object(r.control, 'load_control_config', wraps=r.control.load_control_config) as load:
            self.render(profiles=self.profiles)
        stage.assert_called_once()
        load.assert_called_once()

    def test_offline_validation_does_not_rewrite_non_path_config_values(self):
        self.gateway['ami']['secret'] = 'credential-containing-/run/voice-text'
        self.gateway['future_setting'] = {'value': '/run/voice/leave-this-unchanged'}
        self.voice['ha']['pipeline_id'] = 'pipeline-/run/voice-marker'
        self.write('gateway.json', self.gateway)
        self.write('voice.json', self.voice)
        actual = r.control.load_control_config
        def inspect_staged(path, workspace):
            observed = json.loads((Path(path).parent / 'gateway.json').read_text())
            voice = json.loads((Path(path).parent / 'voice-0.json').read_text())
            self.assertEqual(observed, self.gateway)
            self.assertEqual(voice['ha']['pipeline_id'], self.voice['ha']['pipeline_id'])
            return actual(path, workspace)
        with mock.patch.object(r.control, 'load_control_config', side_effect=inspect_staged):
            self.render(profiles=self.profiles)

    def test_init_creates_owner_only_regular_files_and_retry_fails_without_changes(self):
        source = self.workspace / 'inputs'
        source.mkdir()
        (source / 'gateway.json').write_text('{}')
        (source / 'staging.json').write_text('{"version":1,"files":["gateway.json"]}')
        runtime = self.workspace / 'runtime'
        runtime.mkdir()
        runtime.chmod(0o2770)
        code = r.INIT_CODE.replace('/runtime/private', str(runtime / 'private')).replace(
            '/run/voice-inputs', str(source)).replace('/opt/voice', str(ROOT / 'containers/voice'))
        completed = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        root = runtime / 'private'
        config = root / '.local/config/gateway.json'
        for path in (root, root / '.local', root / '.local/config', root / '.local/state'):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            self.assertEqual(path.stat().st_uid, os.getuid())
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertFalse(config.is_symlink())
        before = config.read_bytes()
        repeated = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertNotEqual(repeated.returncode, 0)
        self.assertEqual(config.read_bytes(), before)

    def test_main_bootstrap_checks_state_without_modifying_it_and_execs_gateway(self):
        state = self.workspace / 'state'
        state.mkdir(mode=0o700)
        database = state / 'gateway.sqlite3'
        database.write_bytes(b'preserved database')
        args = ['bootstrap', '--config', '/config/gateway.json', '--state-directory', str(state)]
        old_umask = os.umask(0o077)
        try:
            with mock.patch.object(sys, 'argv', args), mock.patch.object(os, 'execv') as execute:
                exec(r.MAIN_CODE, {})
            self.assertEqual(execute.call_args.args[0], '/usr/local/bin/python3')
            self.assertIn('/workspace/containers/asterisk/phone_gateway.py', execute.call_args.args[1])
            self.assertEqual(database.read_bytes(), b'preserved database')
            state.chmod(0o755)
            with mock.patch.object(sys, 'argv', args), mock.patch.object(os, 'execv') as execute:
                with self.assertRaisesRegex(SystemExit, 'requires_current_owner'):
                    exec(r.MAIN_CODE, {})
            execute.assert_not_called()
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o755)
        finally:
            os.umask(old_umask)

    def test_repeat_upgrade_preserves_state_and_replaces_only_owned_staging(self):
        first = self.render(profiles=self.profiles)
        self.deployment = first['items'][1]
        second = self.render(profiles=self.profiles)
        self.assertEqual(first, second)
        third = self.render()
        self.assertNotIn('--voice-control-config', self.pod(third)['containers'][0]['args'])
        self.assertEqual(len([v for v in self.pod(third)['volumes'] if v['name'] == 'state']), 1)

    def test_receipt_rejects_mutable_image_false_flags_and_mismatch(self):
        with self.assertRaisesRegex(ValueError, 'immutable_image'):
            self.render(image='registry.example/voice:latest')
        for key in ('verified', 'runtime_resolution_verified', 'smoke_passed'):
            with self.subTest(key=key):
                self.receipt[key] = False
                with self.assertRaisesRegex(ValueError, 'verified_image_receipt'):
                    self.render()
                self.receipt[key] = True
        self.receipt['manifest_digest'] = 'sha256:' + 'f' * 64
        with self.assertRaisesRegex(ValueError, 'image_receipt_mismatch'):
            self.render()

    def test_wrong_image_architecture_refused(self):
        self.capacity['node_arch'] = 'arm64'
        with self.assertRaisesRegex(ValueError, 'image_platform'):
            self.render()

    def test_image_runtime_context_is_bound_to_selected_node(self):
        for key in ('node_name', 'kube_context'):
            original = self.receipt[key]
            self.receipt[key] = 'other'
            with self.assertRaisesRegex(ValueError, 'image_runtime_context'):
                self.render()
            self.receipt[key] = original

    def test_unsafe_existing_replica_count_or_rollout_refused(self):
        self.deployment['spec']['replicas'] = 2
        with self.assertRaisesRegex(ValueError, 'existing_single_recreate'):
            self.render()
        self.deployment['spec']['replicas'] = 1
        self.deployment['spec']['strategy'] = {'type': 'RollingUpdate'}
        with self.assertRaisesRegex(ValueError, 'existing_single_recreate'):
            self.render()

    def test_capacity_is_fresh_scoped_and_cpu_fits(self):
        for key, value in [('deployment', 'other'), ('namespace', 'other'),
                           ('captured_at', (self.now - timedelta(seconds=901)).isoformat()),
                           ('captured_at', (self.now + timedelta(seconds=1)).isoformat())]:
            original = self.capacity[key]
            self.capacity[key] = value
            with self.assertRaises(ValueError):
                self.render()
            self.capacity[key] = original
        self.capacity['other_requests']['cpu'] = '1950m'
        with self.assertRaisesRegex(ValueError, 'insufficient_cpu'):
            self.render()

    def test_scheduler_accounts_init_request_when_main_is_smaller(self):
        self.capacity['other_requests']['cpu'] = '1995m'
        with self.assertRaisesRegex(ValueError, 'insufficient_cpu'):
            self.render(cpu_request='1m')
        self.capacity['other_requests']['cpu'] = '1990m'
        self.render(cpu_request='1m')

    def test_overhead_and_memory_fit_checked(self):
        self.capacity['other_requests']['cpu'] = '1900m'
        self.deployment['spec']['template']['spec']['overhead'] = {'cpu': '1m'}
        with self.assertRaisesRegex(ValueError, 'insufficient_cpu'):
            self.render()
        self.deployment['spec']['template']['spec'].pop('overhead')
        self.capacity['other_requests']['memory'] = '4Gi'
        with self.assertRaisesRegex(ValueError, 'insufficient_memory'):
            self.render()

    def test_resources_require_positive_requests_not_above_limits(self):
        for kwargs in ({'cpu_request': '0'}, {'cpu_request': '2', 'cpu_limit': '1'},
                       {'memory_request': '1Gi', 'memory_limit': '128Mi'}, {'cpu_request': '0.1m'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.render(**kwargs)

    def test_node_affinity_requires_every_or_term_to_pin_node(self):
        pod = self.deployment['spec']['template']['spec']
        terms = pod['affinity']['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']['nodeSelectorTerms']
        terms.append({'matchExpressions': [{'key': 'kubernetes.io/os', 'operator': 'In', 'values': ['linux']}]})
        with self.assertRaisesRegex(ValueError, 'exact_node_affinity'):
            self.render()

    def test_bind_and_ami_must_match_reviewed_node(self):
        self.capacity['node_ipv4'] = '192.168.50.12'
        with self.assertRaisesRegex(ValueError, 'local_node_bind'):
            self.render()

    def test_sidecars_foreign_init_and_pod_resources_refused(self):
        pod = self.deployment['spec']['template']['spec']
        for key, value in [('containers', pod['containers'] * 2),
                           ('initContainers', [{'name': 'foreign'}]),
                           ('resources', {'requests': {'cpu': '1'}})]:
            original = copy.deepcopy(pod.get(key))
            pod[key] = value
            with self.assertRaises(ValueError):
                self.render()
            if original is None:
                pod.pop(key)
            else:
                pod[key] = original

    def test_runtime_class_admission_overhead_cannot_bypass_capacity_check(self):
        self.deployment['spec']['template']['spec']['runtimeClassName'] = 'example-runtime'
        with self.assertRaisesRegex(ValueError, 'runtime_class_not_supported'):
            self.render()

    def test_workspace_overlay_and_unknown_owned_name_refused(self):
        pod = self.deployment['spec']['template']['spec']
        pod['containers'][0]['volumeMounts'].append({'name': 'overlay', 'mountPath': '/workspace'})
        with self.assertRaisesRegex(ValueError, 'overlay_not_supported'):
            self.render()
        pod['containers'][0]['volumeMounts'].pop()
        pod['volumes'].append({'name': 'voice-runtime', 'emptyDir': {}})
        with self.assertRaisesRegex(ValueError, 'reserved_volume_name'):
            self.render()

    def test_ephemeral_pvc_subpath_state_and_unprepared_hostpath_refused(self):
        pod = self.deployment['spec']['template']['spec']
        original = copy.deepcopy(pod['volumes'][1])
        for replacement in ({'name': 'state', 'emptyDir': {}},
                            {'name': 'state', 'persistentVolumeClaim': {'claimName': 'example'}},
                            {'name': 'state', 'hostPath': {'path': '/state', 'type': 'DirectoryOrCreate'}}):
            pod['volumes'][1] = replacement
            with self.assertRaisesRegex(ValueError, 'existing_hostpath_directory'):
                self.render()
        pod['volumes'][1] = original
        pod['containers'][0]['volumeMounts'][1]['subPath'] = 'new'
        with self.assertRaisesRegex(ValueError, 'state_subpath'):
            self.render()

    def test_grace_floor_and_existing_longer_grace_preserved(self):
        with self.assertRaisesRegex(ValueError, 'grace_must'):
            self.render(grace_seconds=179)
        self.deployment['spec']['template']['spec']['terminationGracePeriodSeconds'] = 240
        self.assertEqual(self.pod(self.render())['terminationGracePeriodSeconds'], 240)

    def test_large_inventory_requires_listener_close_margin(self):
        template = self.gateway['phones'][0]
        self.gateway['softphones'] = []
        self.gateway['phones'] = [
            dict(template, id=str(200 + index), lines=[str(200 + index)],
                 ipv4='192.168.50.' + str(20 + index), port=20000 + index,
                 device='SEP' + format(index + 1, '012X'))
            for index in range(32)]
        required = int(r.control.EXTERNAL_OWNER_GRACE_SECONDS) + 32 + 11
        self.assertGreater(required, 180)
        with self.assertRaisesRegex(ValueError, 'grace_must'):
            self.render(grace_seconds=required - 1)
        self.assertEqual(self.pod(self.render(grace_seconds=required))['terminationGracePeriodSeconds'],
                         required)

    def test_profile_gateway_inventory_must_match_complete_object(self):
        different = copy.deepcopy(self.gateway)
        different['api_clients'] = ['192.168.50.12']
        self.write('gateway.json', different)
        with self.assertRaisesRegex(ValueError, 'profile_gateway_mismatch'):
            self.render(profiles=self.profiles)

    def test_profile_duplicates_and_port_collision_refused(self):
        self.profiles['profiles'] *= 2
        with self.assertRaises(Exception):
            self.render(profiles=self.profiles)
        self.profiles['profiles'] = self.profiles['profiles'][:1]
        self.gateway['api_port'] = self.transport['bind_port']
        self.write('gateway.json', self.gateway)
        with self.assertRaisesRegex(ValueError, 'profile_port_collision'):
            self.render(profiles=self.profiles)

    def test_manual_single_phone_bounded_profiles_only(self):
        for branch, key, value in [('wake', 'mode', 'onnx'), ('service', 'rearm_after_turn', True),
                                   ('service', 'enabled', False), ('phones', '209', True)]:
            voice = copy.deepcopy(self.voice)
            voice[branch][key] = value
            self.write('voice.json', voice)
            with self.assertRaises(Exception):
                self.render(profiles=self.profiles)

    def test_private_inputs_reject_links_world_readable_and_outside_paths(self):
        path = self.workspace / '.local/gateway.json'
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'owner_only'):
            r.read_json(path, self.workspace)
        path.chmod(0o600)
        alias = self.workspace / '.local/alias.json'
        alias.symlink_to(path)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            r.read_json(alias, self.workspace)
        with self.assertRaisesRegex(ValueError, 'private_path'):
            r.read_json(self.workspace / 'outside.json', self.workspace)
        with self.assertRaisesRegex(ValueError, 'traversal'):
            r.read_json('.local/../outside.json', self.workspace)

    def test_duplicate_json_key_refused(self):
        path = self.write('duplicate.json', '{"version":1,"version":2}')
        with self.assertRaisesRegex(ValueError, 'duplicate_json'):
            r.read_json(path, self.workspace)

    def test_output_exclusive_owner_only_and_private(self):
        result = self.render()
        path = self.workspace / '.local/result.json'
        r.write_private_manifest(result, path, self.workspace)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            r.write_private_manifest(result, path, self.workspace)
        with self.assertRaises(ValueError):
            r.write_private_manifest(result, self.workspace / 'public.json', self.workspace)

    def test_cli_success_and_sanitized_failures(self):
        for name, data in [('deployment', self.deployment), ('receipt', self.receipt), ('capacity', self.capacity)]:
            self.write(name + '.json', data)
        args = ['--workspace', str(self.workspace), '--deployment', '.local/deployment.json',
                '--gateway-config', '.local/gateway.json', '--image-verification', '.local/receipt.json',
                '--capacity', '.local/capacity.json', '--image', self.image, '--cpu-request', '100m',
                '--cpu-limit', '1', '--output', '.local/result.json']
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(r.main(args), 0)
        self.assertIn('voice disabled', output.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(r.main(args), 2)
        self.assertNotIn(self.gateway['api_token'], error.getvalue())
        self.assertNotIn(str(self.workspace), error.getvalue())


if __name__ == '__main__':
    unittest.main()
