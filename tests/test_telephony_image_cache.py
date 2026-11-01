import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('telephony_image_cache', ROOT / 'scripts/telephony_image_cache.py')
cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache)


def encoded(value):
    return json.dumps(value, sort_keys=True).encode()


def digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


class Runtime:
    def __init__(self, config, *, present=False, pinned=False):
        self.config = config
        self.images = {}
        self.calls = []
        self.fail_after_import = False
        self.import_missing = False
        self.health_failures = {}
        self.import_unusable = False
        if present:
            for archive in config['archives']:
                for ref in archive['images']:
                    self.images[ref] = {'digest': ref.split('@')[1], 'labels': {}}
                    if pinned:
                        self.images[ref]['labels'] = {cache.PIN: 'pinned', cache.OWNER: 'v1'}

    def snapshot(self):
        self.calls.append('snapshot')
        return copy.deepcopy(self.images)

    def unusable(self, config, snapshot):
        return copy.deepcopy(self.health_failures)

    def restore(self, stream, platform):
        self.calls.append('import')
        if not self.import_missing:
            for archive in self.config['archives']:
                for ref in archive['images']:
                    self.images[ref] = {'digest': ref.split('@')[1], 'labels': {}}
        if not self.import_unusable:
            self.health_failures.clear()
        if self.fail_after_import:
            raise cache.Refused('runtime_command_timeout')

    def pin(self, ref):
        self.calls.append('pin')
        self.images[ref]['labels'].update({cache.PIN: 'pinned', cache.OWNER: 'v1'})


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'images.tar'
        self.entries = {}

        def blob(value, kind):
            raw = encoded(value) if isinstance(value, dict) else value
            identity = digest(raw)
            self.entries['blobs/sha256/' + identity[7:]] = raw
            return {'mediaType': kind, 'digest': identity, 'size': len(raw)}

        config = blob({'architecture': 'amd64', 'os': 'linux'}, cache.CONFIG)
        layer = blob(b'fixture layer', 'application/vnd.oci.image.layer.v1.tar')
        descriptor = blob({'schemaVersion': 2, 'config': config, 'layers': [layer]}, cache.MANIFEST)
        self.ref = 'localhost/cisco-home-telephony/test@' + descriptor['digest']
        descriptor['annotations'] = {'io.containerd.image.name': self.ref,
                                     'org.opencontainers.image.ref.name': self.ref}
        self.index = {'schemaVersion': 2, 'manifests': [descriptor]}
        self.entries['index.json'] = encoded(self.index)
        self.entries['oci-layout'] = encoded({'imageLayoutVersion': '1.0.0'})
        self.write()
        self.config = {'version': 1, 'platform': 'linux/amd64', 'archives': [self.archive]}

    def write(self, extras=()):
        with tarfile.open(self.path, 'w') as tar:
            for name, content in self.entries.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(content)
                tar.addfile(entry, io.BytesIO(content))
            for entry, content in extras:
                tar.addfile(entry, io.BytesIO(content))
        self.archive = {'path': str(self.path), 'sha256': hashlib.sha256(self.path.read_bytes()).hexdigest(),
                        'images': [self.ref]}

    def verify(self, *, platform='linux/amd64'):
        with self.path.open('rb') as stream:
            return cache.verify_archive(stream, self.archive, platform)

    def reconcile(self, runtime, **kwargs):
        with mock.patch.object(cache, 'open_trusted', side_effect=lambda path, limit: open(path, 'rb')):
            return cache.reconcile(self.config, runtime, **kwargs)

    def test_valid_oci_archive(self):
        self.assertEqual(self.verify()[2], self.path.stat().st_size)

    def test_standard_ctr_export_directories_and_digest_suffix(self):
        descriptor = self.index['manifests'][0]
        descriptor['annotations']['org.opencontainers.image.ref.name'] = '@' + descriptor['digest']
        self.entries['index.json'] = encoded(self.index)
        directories = []
        for name in ('blobs/', 'blobs/sha256/'):
            entry = tarfile.TarInfo(name)
            entry.type = tarfile.DIRTYPE
            directories.append((entry, b''))
        self.write(directories)
        self.assertEqual(self.verify()[2], self.path.stat().st_size)

    def test_unexpected_directory_rejected(self):
        entry = tarfile.TarInfo('unapproved/')
        entry.type = tarfile.DIRTYPE
        self.write([(entry, b'')])
        with self.assertRaisesRegex(cache.Refused, 'unexpected_archive_directory'):
            self.verify()

    def test_archive_checksum_failure(self):
        self.archive['sha256'] = '0' * 64
        with self.assertRaisesRegex(cache.Refused, 'archive_checksum_mismatch'):
            self.verify()

    def test_blob_checksum_failure_even_with_approved_outer_hash(self):
        name = next(name for name, data in self.entries.items() if data == b'fixture layer')
        self.entries[name] = b'corrupt layer'
        self.write()
        with self.assertRaisesRegex(cache.Refused, 'oci_blob_checksum_mismatch'):
            self.verify()

    def test_wrong_platform(self):
        with self.assertRaisesRegex(cache.Refused, 'oci_platform_mismatch'):
            self.verify(platform='linux/arm64')

    def test_undeclared_image_reference(self):
        self.index['manifests'][0]['annotations']['io.containerd.image.name'] = 'example.com/foreign@' + 'sha256:' + 'a' * 64
        self.entries['index.json'] = encoded(self.index)
        self.write()
        with self.assertRaisesRegex(cache.Refused, 'unexpected_oci_image'):
            self.verify()

    def test_alternate_name_cannot_create_an_extra_image(self):
        self.index['manifests'][0]['annotations']['org.opencontainers.image.ref.name'] = 'unapproved:latest'
        self.entries['index.json'] = encoded(self.index)
        self.write()
        with self.assertRaisesRegex(cache.Refused, 'ambiguous_oci_image_name'):
            self.verify()

    def test_unreferenced_blob_rejected(self):
        self.entries['blobs/sha256/' + 'b' * 64] = b'extra'
        self.write()
        with self.assertRaisesRegex(cache.Refused, 'unreferenced_oci_blob'):
            self.verify()

    def test_duplicate_member_rejected(self):
        entry = tarfile.TarInfo('index.json')
        entry.size = 2
        self.write([(entry, b'{}')])
        with self.assertRaisesRegex(cache.Refused, 'duplicate_archive_member'):
            self.verify()

    def test_unsafe_tar_members_rejected(self):
        for name, kind in [('../escape', tarfile.REGTYPE), ('index-link', tarfile.SYMTYPE),
                           ('hard-link', tarfile.LNKTYPE), ('/absolute', tarfile.REGTYPE)]:
            with self.subTest(name=name):
                entry = tarfile.TarInfo(name)
                entry.type = kind
                entry.linkname = 'index.json'
                self.write([(entry, b'')])
                with self.assertRaisesRegex(cache.Refused, 'unexpected_archive_member'):
                    self.verify()

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaisesRegex(cache.Refused, 'duplicate_json_key'):
            cache.parse_json(b'{"version":1,"version":2}')

    def test_check_is_read_only_for_missing_images(self):
        runtime = Runtime(self.config)
        report = self.reconcile(runtime)
        self.assertEqual(report['status'], 'unavailable')
        self.assertEqual(report['missing'], [self.ref])
        self.assertEqual(runtime.calls, ['snapshot'])

    def test_healthy_timer_does_not_rehash_or_import(self):
        runtime = Runtime(self.config, present=True, pinned=True)
        with mock.patch.object(cache, 'verify_archive', side_effect=AssertionError('unnecessary hashing')):
            report = self.reconcile(runtime, apply=True)
        self.assertEqual(report['status'], 'ready')
        self.assertFalse(report['archives_verified'])
        self.assertNotIn('import', runtime.calls)
        self.assertNotIn('pin', runtime.calls)

    def test_missing_image_is_imported_and_pinned_once(self):
        runtime = Runtime(self.config)
        report = self.reconcile(runtime, apply=True)
        self.assertEqual(report['status'], 'ready')
        self.assertEqual(report['archives_imported'], 1)
        self.assertEqual(runtime.calls.count('import'), 1)
        self.assertEqual(runtime.calls.count('pin'), 1)
        self.reconcile(runtime, apply=True)
        self.assertEqual(runtime.calls.count('import'), 1)
        self.assertEqual(runtime.calls.count('pin'), 1)

    def test_existing_matching_image_only_needs_pin(self):
        runtime = Runtime(self.config, present=True)
        self.assertEqual(self.reconcile(runtime)['status'], 'unavailable')
        self.assertEqual(self.reconcile(runtime, apply=True)['status'], 'ready')
        self.assertNotIn('import', runtime.calls)
        self.assertEqual(runtime.calls.count('pin'), 1)

    def test_wrong_target_fails_before_mutations(self):
        runtime = Runtime(self.config, present=True)
        runtime.images[self.ref]['digest'] = 'sha256:' + '0' * 64
        with self.assertRaisesRegex(cache.Refused, 'existing_image_target_mismatch'):
            self.reconcile(runtime, apply=True)
        self.assertEqual(runtime.calls, ['snapshot'])

    def test_conflicting_owner_fails_before_mutations(self):
        runtime = Runtime(self.config, present=True)
        runtime.images[self.ref]['labels'][cache.OWNER] = 'foreign'
        with self.assertRaisesRegex(cache.Refused, 'conflicting_image_owner'):
            self.reconcile(runtime, apply=True)
        self.assertEqual(runtime.calls, ['snapshot'])

    def test_bad_archive_cannot_import(self):
        runtime = Runtime(self.config)
        self.config['archives'][0]['sha256'] = '0' * 64
        with self.assertRaisesRegex(cache.Refused, 'archive_checksum_mismatch'):
            self.reconcile(runtime, apply=True)
        self.assertEqual(runtime.calls, ['snapshot'])

    def test_import_must_be_observed_before_pinning(self):
        runtime = Runtime(self.config)
        runtime.import_missing = True
        with self.assertRaisesRegex(cache.Refused, 'import_did_not_restore_images'):
            self.reconcile(runtime, apply=True)
        self.assertNotIn('pin', runtime.calls)

    def test_ambiguous_import_reconciles_without_duplicate_import(self):
        runtime = Runtime(self.config)
        runtime.fail_after_import = True
        with self.assertRaisesRegex(cache.Refused, 'runtime_command_timeout'):
            self.reconcile(runtime, apply=True)
        runtime.fail_after_import = False
        self.assertEqual(self.reconcile(runtime, apply=True)['status'], 'ready')
        self.assertEqual(runtime.calls.count('import'), 1)

    def test_existing_metadata_with_missing_content_is_not_ready(self):
        runtime = Runtime(self.config, present=True, pinned=True)
        runtime.health_failures[self.ref] = 'content_incomplete_or_not_unpacked'
        report = self.reconcile(runtime)
        self.assertEqual(report['status'], 'unavailable')
        self.assertEqual(report['unusable'], runtime.health_failures)
        self.assertNotIn('import', runtime.calls)

    def test_existing_but_unusable_image_is_restored_and_checked(self):
        for reason in ('content_incomplete_or_not_unpacked', 'cri_image_unavailable',
                       'cri_config_mismatch', 'cri_reference_mismatch', 'cri_alias_unavailable'):
            with self.subTest(reason=reason):
                runtime = Runtime(self.config, present=True, pinned=True)
                runtime.health_failures[self.ref] = reason
                report = self.reconcile(runtime, apply=True)
                self.assertEqual(report['status'], 'ready')
                self.assertEqual(report['unusable'], {})
                self.assertEqual(runtime.calls.count('import'), 1)

    def test_unusable_after_import_refuses_without_loop_or_pin(self):
        runtime = Runtime(self.config, present=True)
        runtime.health_failures[self.ref] = 'cri_alias_unavailable'
        runtime.import_unusable = True
        with self.assertRaisesRegex(cache.Refused, 'import_did_not_restore_image_usability'):
            self.reconcile(runtime, apply=True)
        self.assertEqual(runtime.calls.count('import'), 1)
        self.assertNotIn('pin', runtime.calls)

    def test_unusable_image_still_requires_approved_archive(self):
        runtime = Runtime(self.config, present=True)
        runtime.health_failures[self.ref] = 'content_incomplete_or_not_unpacked'
        self.config['archives'][0]['sha256'] = '0' * 64
        with self.assertRaisesRegex(cache.Refused, 'archive_checksum_mismatch'):
            self.reconcile(runtime, apply=True)
        self.assertNotIn('import', runtime.calls)

    def health_fixture(self):
        descriptor = self.index['manifests'][0]
        manifest = json.loads(self.entries['blobs/sha256/' + descriptor['digest'][7:]])
        snapshot = {self.ref: {'digest': descriptor['digest'], 'labels': {}}}
        status = {'status': {'id': manifest['config']['digest'], 'repoDigests': [self.ref]}}
        return snapshot, manifest, status

    def run_health(self, snapshot, manifest, status, *, ready=None):
        if ready is None:
            ready = self.ref + '\n'
        calls = []
        def command(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv == ['images', 'check', '--quiet']:
                return ready
            if argv[:2] == ['content', 'get']:
                return encoded(manifest).decode() if manifest is not None else None
            if argv[:1] == ['inspecti']:
                self.assertEqual(kwargs['family'], 'crictl')
                self.assertEqual(argv[-1], self.ref)
                return encoded(status).decode() if status is not None else None
            self.fail('Unexpected runtime mutation or command: ' + repr(argv))
        runtime = cache.Runtime()
        with mock.patch.object(runtime, 'command', side_effect=command):
            return runtime.unusable(self.config, snapshot), calls

    def test_content_and_cri_identity_are_verified(self):
        failures, calls = self.run_health(*self.health_fixture())
        self.assertEqual(failures, {})
        self.assertEqual([call[0][0] for call in calls], ['images', 'content', 'inspecti'])

    def test_missing_content_fails_before_cri(self):
        failures, calls = self.run_health(*self.health_fixture(), ready='')
        self.assertEqual(failures, {self.ref: 'content_incomplete_or_not_unpacked'})
        self.assertEqual(len(calls), 1)

    def test_cri_absence_wrong_config_and_wrong_reference(self):
        snapshot, manifest, status = self.health_fixture()
        cases = [(None, 'cri_image_unavailable'), ({}, 'cri_image_unavailable'),
                 ({'status': {'id': 'sha256:' + 'a' * 64, 'repoDigests': [self.ref]}}, 'cri_config_mismatch'),
                 ({'status': {'id': status['status']['id'], 'repoDigests': []}}, 'cri_reference_mismatch')]
        for value, reason in cases:
            with self.subTest(reason=reason):
                failures, _ = self.run_health(snapshot, manifest, value)
                self.assertEqual(failures, {self.ref: reason})

    def test_cri_alias_must_exist_under_normalized_runtime_name(self):
        snapshot, manifest, status = self.health_fixture()
        alias = 'import-2026-10-22@' + self.ref.rsplit('@', 1)[1]
        status['status']['repoDigests'].append(alias)
        failures, _ = self.run_health(snapshot, manifest, status)
        self.assertEqual(failures, {self.ref: 'cri_alias_unavailable'})
        snapshot[alias] = {'digest': self.ref.rsplit('@', 1)[1], 'labels': {}}
        failures, _ = self.run_health(snapshot, manifest, status)
        self.assertEqual(failures, {self.ref: 'cri_alias_unavailable'})
        snapshot[cache.normalized_reference(alias)] = snapshot[alias]
        failures, _ = self.run_health(snapshot, manifest, status)
        self.assertEqual(failures, {})

    def test_cri_alias_with_different_target_is_not_ready(self):
        snapshot, manifest, status = self.health_fixture()
        snapshot[self.ref]['digest'] = 'sha256:' + 'a' * 64
        failures, _ = self.run_health(snapshot, manifest, status)
        self.assertEqual(failures, {self.ref: 'cri_alias_unavailable'})

    def test_unrelated_runtime_images_are_not_required_to_be_ready(self):
        snapshot, manifest, status = self.health_fixture()
        snapshot['docker.io/library/unrelated:latest'] = {'digest': 'sha256:' + 'b' * 64, 'labels': {}}
        failures, _ = self.run_health(snapshot, manifest, status)
        self.assertEqual(failures, {})

    def test_default_registry_reference_normalization(self):
        digest_suffix = '@sha256:' + 'a' * 64
        for source, expected in [('import-2026-10-22', 'docker.io/library/import-2026-10-22'),
                                 ('example/image', 'docker.io/example/image'),
                                 ('docker.io/example', 'docker.io/library/example'),
                                 ('index.docker.io/library/example', 'docker.io/library/example'),
                                 ('localhost/approved/image', 'localhost/approved/image'),
                                 ('registry.example:5000/example', 'registry.example:5000/example')]:
            self.assertEqual(cache.normalized_reference(source + digest_suffix), expected + digest_suffix)

    def test_explicit_archive_verification_remains_read_only(self):
        runtime = Runtime(self.config, present=True, pinned=True)
        report = self.reconcile(runtime, verify=True)
        self.assertTrue(report['archives_verified'])
        self.assertEqual(runtime.calls, ['snapshot'])

    def test_all_needed_archives_are_verified_before_any_import(self):
        other = copy.deepcopy(self.archive)
        other['images'] = ['localhost/cisco-home-telephony/second@sha256:' + 'a' * 64]
        other['sha256'] = '0' * 64
        self.config['archives'].append(other)
        runtime = Runtime(self.config)
        with self.assertRaisesRegex(cache.Refused, 'archive_checksum_mismatch'):
            self.reconcile(runtime, apply=True)
        self.assertEqual(runtime.calls, ['snapshot'])

    def test_archive_change_before_import_is_rejected(self):
        runtime = Runtime(self.config)
        actual = cache.verify_archive
        def changed(stream, archive, platform):
            observed = actual(stream, archive, platform)
            return (*observed[:-1], observed[-1] + 1)
        with mock.patch.object(cache, 'verify_archive', side_effect=changed), \
                self.assertRaisesRegex(cache.Refused, 'archive_changed_before_import'):
            self.reconcile(runtime, apply=True)
        self.assertNotIn('import', runtime.calls)

    def test_ctr_table_parses_size_columns_and_labels(self):
        parsed = cache.parse_images('REF TYPE DIGEST SIZE PLATFORMS LABELS\n'
                                    f'{self.ref} {cache.MANIFEST} {self.ref.split("@")[1]} 10.2 MiB linux/amd64 '
                                    f'{cache.PIN}=pinned,{cache.OWNER}=v1\n')
        self.assertEqual(cache.inspect(self.config, parsed), ([], []))

    def test_unrecognized_or_duplicate_runtime_rows_fail_closed(self):
        row = f'{self.ref} {cache.MANIFEST} {self.ref.split("@")[1]} 10.2 MiB linux/amd64 -\n'
        for text in ('arbitrary output', 'REF TYPE DIGEST SIZE PLATFORMS LABELS\n' + row + row):
            with self.subTest(text=text), self.assertRaises(cache.Refused):
                cache.parse_images(text)

    def test_config_duplicate_refs_rejected(self):
        value = copy.deepcopy(self.config)
        value['archives'][0]['images'] *= 2
        with mock.patch.object(cache, 'open_trusted', return_value=io.BytesIO(encoded(value))), \
                mock.patch.object(cache, 'trusted_path'), \
                self.assertRaisesRegex(cache.Refused, 'duplicate_image_reference'):
            cache.load_config('/etc/example.json')

    def test_lock_rejects_overlapping_recovery(self):
        with mock.patch.object(cache, 'LOCK_DIRECTORY', Path(self.temp.name) / 'lock'), \
                mock.patch.object(cache, 'ROOT_UID', os.getuid()), \
                mock.patch.object(cache, 'trusted_path'):
            with cache.exclusive_lock():
                with self.assertRaisesRegex(cache.Refused, 'recovery_already_running'):
                    with cache.exclusive_lock():
                        self.fail('concurrent lock accepted')
            with cache.exclusive_lock():
                pass

    def test_runtime_commands_use_only_local_containerd_and_inherited_archive(self):
        commands = []
        def run(argv, **kwargs):
            commands.append((argv, kwargs))
            self.assertGreater(kwargs['timeout'], 0)
            self.assertLessEqual(kwargs['timeout'], 90)
            return types.SimpleNamespace(returncode=0)
        with mock.patch.object(cache.subprocess, 'run', side_effect=run):
            runtime = cache.Runtime()
            with self.path.open('rb') as stream:
                descriptor = stream.fileno()
                runtime.restore(stream, 'linux/amd64')
            runtime.pin(self.ref)
        self.assertEqual(commands[0][0], ['/usr/local/bin/k3s', 'ctr', '--namespace', 'k8s.io',
                                          'images', 'import', '--platform', 'linux/amd64',
                                          '/proc/self/fd/' + str(descriptor)])
        self.assertEqual(commands[0][1]['pass_fds'], (descriptor,))
        self.assertEqual(commands[1][0][4:6], ['images', 'label'])
        self.assertTrue(all('pull' not in command[0] for command in commands))

    def test_cri_command_is_bounded_and_failures_are_distinct_from_timeout(self):
        def run(argv, **kwargs):
            self.assertEqual(argv, ['/usr/local/bin/k3s', 'crictl', 'inspecti', '--quiet',
                                    '--output', 'json', self.ref])
            self.assertLessEqual(kwargs['timeout'], 90)
            return types.SimpleNamespace(returncode=1)
        with mock.patch.object(cache.subprocess, 'run', side_effect=run):
            self.assertIsNone(cache.Runtime().command(['inspecti', '--quiet', '--output', 'json', self.ref],
                                                       family='crictl', allow_failure=True))
        with mock.patch.object(cache.subprocess, 'run', side_effect=cache.subprocess.TimeoutExpired('crictl', 10)):
            with self.assertRaisesRegex(cache.Refused, 'runtime_command_timeout'):
                cache.Runtime().command(['inspecti', self.ref], family='crictl', allow_failure=True)

    def test_trusted_path_rejects_symlinks_writable_paths_and_hardlinks(self):
        safe_dir = types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_nlink=1)
        safe_file = types.SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_nlink=1)
        cases = [types.SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0, st_nlink=1),
                 types.SimpleNamespace(st_mode=stat.S_IFREG | 0o666, st_uid=0, st_nlink=1),
                 types.SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=1, st_nlink=1),
                 types.SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_nlink=2)]
        for bad in cases:
            def fake_lstat(path):
                return bad if str(path) == '/etc/example' else safe_dir
            with self.subTest(bad=bad), mock.patch.object(Path, 'lstat', fake_lstat), self.assertRaises(cache.Refused):
                cache.trusted_path('/etc/example')
        def writable_parent(path):
            if str(path) == '/etc':
                return types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_uid=0, st_nlink=1)
            return safe_file if str(path) == '/etc/example' else safe_dir
        with mock.patch.object(Path, 'lstat', writable_parent), self.assertRaisesRegex(cache.Refused, 'untrusted_parent'):
            cache.trusted_path('/etc/example')

    def test_unit_is_bounded_and_does_not_restart_services(self):
        unit = (ROOT / 'deploy/telephony-image-cache.service').read_text()
        timer = (ROOT / 'deploy/telephony-image-cache.timer').read_text()
        self.assertIn('TimeoutStartSec=300', unit)
        self.assertIn('KillMode=control-group', unit)
        self.assertIn('ExecStart=/usr/local/sbin/telephony-image-cache --apply', unit)
        self.assertNotIn('systemctl', unit)
        self.assertIn('OnBootSec=60', timer)
        self.assertIn('OnUnitActiveSec=120', timer)


if __name__ == '__main__':
    unittest.main()
