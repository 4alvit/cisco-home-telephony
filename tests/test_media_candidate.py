"""Local fixtures only: candidate identity, privacy, drift and refusal behavior."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('media_candidate', ROOT / 'scripts/media_candidate.py')
subject = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(subject)


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # macOS /var is a symlink: resolve the fixture root, not inputs under test.
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / 'source'
        self.repo.mkdir()
        self.git('init', '-q')
        (self.repo / 'component').mkdir()
        (self.repo / 'component/main.py').write_bytes(b'print("fixture")\n')
        (self.repo / 'component/manifest.json').write_bytes(b'{"version":"test"}\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-q', '-m', 'fixture')
        self.commit = self.git('rev-parse', 'HEAD').strip()
        self.installed = self.root / 'installed'
        shutil.copytree(self.repo / 'component', self.installed)
        self.digest = 'a' * 64
        self.write('observation.json', {'imageID': 'containerd://sha256:' + self.digest})
        self.write('controller.json', {'metadata': {'resourceVersion': '1'},
                                      'spec': {'replicas': 1, 'env': 'CONFIG_PRIVATE_SENTINEL'}})
        (self.root / 'config.json').write_text('{"config":"CONFIG_PRIVATE_SENTINEL"}\n')
        (self.root / 'secret.bin').write_bytes(b'SECRET_PRIVATE_SENTINEL\x00\xff')
        (self.root / 'model.bin').write_bytes(b'MODEL_PRIVATE_SENTINEL')
        self.spec = {
            'schema_version': 1, 'scope': 'radio', 'excluded_scope': ['voice-inference'],
            'required_components': ['home_assistant'],
            'components': {'home_assistant': {
                'role': 'source',
                'source': {'repo': 'source', 'commit': self.commit, 'subdir': 'component'},
                'image': {'expected': 'registry.example.invalid/ha@sha256:' + self.digest,
                          'observation': {'file': 'observation.json', 'pointer': '/imageID'}},
                'controller': {'file': 'controller.json', 'pointer': '/spec'},
                'inputs': {'config': {'file': 'config.json', 'kind': 'config'},
                           'token': {'file': 'secret.bin', 'kind': 'secret'},
                           'model': {'file': 'model.bin', 'kind': 'model'}},
                'installed_tree': 'installed',
            }},
        }
        self.spec['components']['gateway'] = copy.deepcopy(self.component)
        del self.spec['components']['gateway']['installed_tree']
        self.spec['required_components'].append('gateway')
        self.spec_path, self.manifest = self.root / 'spec.json', self.root / 'manifest.json'
        self.save_spec()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True,
                                       stderr=subprocess.DEVNULL)

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value))

    def save_spec(self):
        self.spec_path.write_text(json.dumps(self.spec))

    @property
    def component(self):
        return self.spec['components']['home_assistant']

    def run_candidate(self, action='freeze', manifest=None, digest=None):
        self.save_spec()
        return subject.run(action, self.spec_path, manifest or self.manifest, digest)

    def assert_refuses(self, reason, action='freeze'):
        with self.assertRaisesRegex(subject.Refused, '^' + reason + '$'):
            self.run_candidate(action)

    def test_deterministic_manifest_private_output_and_explicit_nonacceptance(self):
        result = self.run_candidate()
        duplicate = self.root / 'second.json'
        second = self.run_candidate(manifest=duplicate)
        self.assertEqual(self.manifest.read_bytes(), duplicate.read_bytes())
        self.assertEqual(result['manifest_sha256'], second['manifest_sha256'])
        self.assertEqual(self.manifest.stat().st_mode & 0o777, 0o600)
        self.assertFalse(result['acceptance'])
        self.assertEqual(result['status'], 'candidate_frozen')
        verified = self.run_candidate('verify', digest=result['manifest_sha256'])
        self.assertEqual(verified['status'], 'candidate_unchanged')
        raw = self.manifest.read_text()
        for sensitive in ('SECRET_PRIVATE_SENTINEL', 'CONFIG_PRIVATE_SENTINEL',
                          'MODEL_PRIVATE_SENTINEL', str(self.root), 'secret.bin'):
            self.assertNotIn(sensitive, raw)
        record = json.loads(raw)['components']['home_assistant']
        self.assertEqual(record['inputs']['token']['sha256'],
                         hashlib.sha256((self.root / 'secret.bin').read_bytes()).hexdigest())
        self.assertEqual(len(record['installed_tree']['files']), 2)

    def test_raw_config_secret_and_model_drift_even_when_metadata_unchanged(self):
        self.run_candidate()
        for name in ('config.json', 'secret.bin', 'model.bin'):
            with self.subTest(name=name):
                path = self.root / name
                old, before = path.read_bytes(), path.stat()
                path.write_bytes(bytes([old[0] ^ 1]) + old[1:])
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                self.assert_refuses('candidate_drift', 'verify')
                path.write_bytes(old)

    def test_controller_canonical_spec_ignores_volatile_outer_metadata(self):
        self.run_candidate()
        self.write('controller.json', {'spec': {'env': 'CONFIG_PRIVATE_SENTINEL', 'replicas': 1},
                                      'metadata': {'resourceVersion': '999', 'uid': 'volatile'}})
        self.run_candidate('verify')
        self.write('controller.json', {'spec': {'replicas': 2, 'env': 'CONFIG_PRIVATE_SENTINEL'}})
        self.assert_refuses('candidate_drift', 'verify')

    def test_image_must_be_digest_pinned_and_match_observation(self):
        original = self.component['image']['expected']
        for invalid in ('registry.example/ha:latest', 'sha256:' + 'b' * 64, None):
            with self.subTest(invalid=invalid):
                self.component['image']['expected'] = invalid
                with self.assertRaises(subject.Refused):
                    self.run_candidate()
        self.component['image']['expected'] = original
        self.write('observation.json', {'imageID': 'sha256:' + 'b' * 64})
        self.assert_refuses('image_digest_mismatch')

    def test_source_requires_exact_commit_and_safe_tree(self):
        for invalid in ('HEAD', self.commit[:12], '--help', None):
            self.component['source']['commit'] = invalid
            self.assert_refuses('full_source_commit_required')
        self.component['source']['commit'] = self.commit
        for invalid in ('../component', '/component', 'component/../component', 'component\\x'):
            self.component['source']['subdir'] = invalid
            self.assert_refuses('invalid_source_subdir')
        self.component['source']['subdir'] = 'component/main.py'
        self.assert_refuses('source_tree_required')

    def test_installed_changed_missing_or_extra_file_cannot_verify(self):
        self.run_candidate()
        path = self.installed / 'main.py'
        original = path.read_bytes()
        path.write_bytes(b'print("changed")\n')
        self.assert_refuses('installed_tree_does_not_match_source', 'verify')
        path.unlink()
        self.assert_refuses('installed_tree_does_not_match_source', 'verify')
        path.write_bytes(original)
        (self.installed / 'extra.py').write_bytes(b'extra')
        self.assert_refuses('installed_tree_does_not_match_source', 'verify')

    def test_installed_tree_change_during_walk_is_refused(self):
        original = subject.read_descriptor
        changed = False

        def add_file(*args, **kwargs):
            nonlocal changed
            result = original(*args, **kwargs)
            if not changed:
                changed = True
                (self.installed / 'new-during-read.py').write_bytes(b'new')
            return result

        with mock.patch.object(subject, 'read_descriptor', side_effect=add_file):
            with self.assertRaisesRegex(subject.Refused, 'installed_tree_changed'):
                subject.installed_files(self.installed)

    def test_symlinks_in_file_parent_and_installed_tree_are_refused(self):
        (self.root / 'link.bin').symlink_to(self.root / 'secret.bin')
        self.component['inputs']['token']['file'] = 'link.bin'
        with self.assertRaises(OSError):
            self.run_candidate()
        self.component['inputs']['token']['file'] = 'secret.bin'
        (self.root / 'alias').symlink_to(self.installed, target_is_directory=True)
        self.component['installed_tree'] = 'alias'
        with self.assertRaises(OSError):
            self.run_candidate()
        self.component['installed_tree'] = 'installed'
        (self.installed / 'nested-link').symlink_to(self.root / 'secret.bin')
        self.assert_refuses('installed_symlink_refused')

    def test_symlink_in_committed_source_is_refused(self):
        (self.repo / 'component/link').symlink_to('main.py')
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-q', '-m', 'symlink')
        self.component['source']['commit'] = self.git('rev-parse', 'HEAD').strip()
        self.assert_refuses('source_symlink_or_submodule_refused')

    def test_missing_declared_components_inputs_and_installed_tree_refused(self):
        self.spec['required_components'].append('pbx')
        self.assert_refuses('missing_or_extra_components')
        self.spec['required_components'].pop()
        old = self.component['inputs']
        self.component['inputs'] = {}
        self.assert_refuses('declared_input_bytes_required')
        self.component['inputs'] = old
        del self.component['installed_tree']
        self.assert_refuses('installed_component_tree_required')

    def test_source_component_without_image_and_external_dependency_without_source(self):
        del self.component['image']
        del self.component['controller']
        dependency = copy.deepcopy(self.spec['components']['gateway'])
        dependency['role'] = 'runtime_dependency'
        del dependency['source']
        self.spec['components']['ha_core'] = dependency
        self.spec['required_components'].append('ha_core')
        self.run_candidate()
        frozen = json.loads(self.manifest.read_bytes())['components']
        self.assertNotIn('image', frozen['home_assistant'])
        self.assertNotIn('source', frozen['ha_core'])
        self.assertEqual(frozen['ha_core']['role'], 'runtime_dependency')

    def test_reported_fingerprints_are_explicit_and_drift_checked_without_secret_values(self):
        fingerprint = {'sha256': 'c' * 64, 'bytes': 47}
        self.write('fingerprints.json', {'token': fingerprint})
        self.component['inputs']['token'] = {
            'kind': 'secret', 'fingerprint': {'file': 'fingerprints.json', 'pointer': '/token'}}
        self.run_candidate()
        frozen = json.loads(self.manifest.read_bytes())['components']['home_assistant']['inputs']['token']
        self.assertEqual(frozen['mode'], 'collector_reported_raw_file')
        self.assertEqual(frozen['sha256'], fingerprint['sha256'])
        fingerprint['sha256'] = 'd' * 64
        self.write('fingerprints.json', {'token': fingerprint})
        self.assert_refuses('candidate_drift', 'verify')
        for invalid in ({'sha256': 'bad', 'bytes': 1}, {'sha256': 'c' * 64, 'bytes': True},
                        {'sha256': 'c' * 64, 'bytes': 1, 'secret': 'NO'}):
            self.write('fingerprints.json', {'token': invalid})
            self.assert_refuses('invalid_reported_fingerprint', 'verify')

    def test_two_source_components_and_image_controller_pair_required(self):
        del self.spec['components']['gateway']['source']
        self.spec['components']['gateway']['role'] = 'runtime_dependency'
        self.assert_refuses('source_components_required')
        del self.component['controller']
        self.assert_refuses('image_controller_pair_required')

    def test_explicit_scope_and_no_unknown_spec_fields(self):
        self.spec['excluded_scope'] = []
        self.assert_refuses('explicit_excluded_scope_required')
        self.spec['excluded_scope'] = ['voice-inference']
        self.spec['private_payload'] = 'never export'
        self.assert_refuses('invalid_spec')

    def test_manifest_tampering_and_wrong_expected_hash_refused(self):
        result = self.run_candidate()
        with self.assertRaisesRegex(subject.Refused, 'manifest_digest_mismatch'):
            self.run_candidate('verify', digest='b' * 64)
        self.manifest.write_bytes(self.manifest.read_bytes() + b' ')
        with self.assertRaisesRegex(subject.Refused, 'manifest_digest_mismatch'):
            self.run_candidate('verify', digest=result['manifest_sha256'])
        self.assert_refuses('candidate_drift', 'verify')

    def test_freeze_never_overwrites_existing_output(self):
        self.manifest.write_text('keep me')
        with self.assertRaises(FileExistsError):
            self.run_candidate()
        self.assertEqual(self.manifest.read_text(), 'keep me')

    def test_missing_and_special_inputs_return_safe_cli_error(self):
        for name in ('DO_NOT_EXPOSE_THIS_PATH', 'fifo'):
            if name == 'fifo':
                os.mkfifo(self.root / name)
            self.component['inputs']['token']['file'] = name
            self.save_spec()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = subject.main(['freeze', '--spec', str(self.spec_path),
                                     '--manifest', str(self.manifest)])
            self.assertEqual(code, 2)
            result = json.loads(output.getvalue())
            self.assertEqual(result['status'], 'refused')
            self.assertFalse(result['acceptance'])
            self.assertNotIn(name, output.getvalue())

    def test_input_read_bounds_and_mid_read_mutation_refused(self):
        with mock.patch.object(subject, 'MAX_JSON', 4):
            self.assert_refuses('invalid_or_oversized_file')
        original = subject.os.read
        mutated = False

        def changing_read(descriptor, count):
            nonlocal mutated
            data = original(descriptor, count)
            if not mutated:
                mutated = True
                with (self.root / 'secret.bin').open('ab') as output:
                    output.write(b'changed')
            return data

        with mock.patch.object(subject.os, 'read', side_effect=changing_read):
            with self.assertRaisesRegex(subject.Refused, 'file_changed_during_read'):
                subject.read_file(self.root / 'secret.bin')

    def test_git_source_calls_disable_network_lazy_fetch_and_replacement(self):
        with mock.patch.object(subject.subprocess, 'Popen', wraps=subprocess.Popen) as run:
            with mock.patch.dict(os.environ, {'GIT_DIR': '/invalid/private/path'}):
                self.assertEqual(subject.git(self.repo, 'cat-file', '-t', self.commit), b'commit\n')
        args, kwargs = run.call_args
        self.assertIn('protocol.allow=never', args[0])
        self.assertEqual(kwargs['env']['GIT_NO_LAZY_FETCH'], '1')
        self.assertEqual(kwargs['env']['GIT_NO_REPLACE_OBJECTS'], '1')
        self.assertNotIn('GIT_DIR', kwargs['env'])
        self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
        with self.assertRaisesRegex(subject.Refused, 'git_source_unavailable'):
            subject.git(self.repo, 'cat-file', '-t', self.commit, limit=2)


class ParserTests(unittest.TestCase):
    def test_json_duplicate_keys_nonfinite_and_invalid_encoding_refused(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}', b'\xff', b'{'):
            with self.subTest(raw=raw), self.assertRaises(subject.Refused):
                subject.decode(raw)

    def test_json_pointer_arrays_escapes_and_missing_path(self):
        self.assertEqual(subject.section({'a/b': {'~': ['yes']}}, '/a~1b/~0/0'), 'yes')
        for pointer in ('not-a-pointer', '/~2', '/array/01', '/array/3', '/missing'):
            with self.subTest(pointer=pointer), self.assertRaises(subject.Refused):
                subject.section({'array': ['one']}, pointer)

    def test_path_traversal_rejected_without_resolving_it(self):
        for value in ('../secret', 'dir/../secret'):
            with self.assertRaisesRegex(subject.Refused, 'path_traversal'):
                subject.path_from(value, Path.cwd())


if __name__ == '__main__':
    unittest.main()
