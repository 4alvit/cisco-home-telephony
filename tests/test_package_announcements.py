"""Release archives contain only reproducible, explicitly committed source files."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/package-announcements.py'
SPEC = importlib.util.spec_from_file_location('package_announcements', SCRIPT)
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)


@unittest.skipUnless(shutil.which('git'), 'git is required for source archive tests')
class AnnouncementPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'
        self.root.mkdir()
        self.source = b"VALUE = 'tracked fixture'\n"
        for name in set(package.CLI + package.RUNTIME):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.source if name.endswith('.py') else b'Tracked fixture\n')
        self.git('init', '--quiet')
        self.commit()
        self.revision = self.git('rev-parse', 'HEAD').decode().strip()
        self.patch_root = patch.object(package, 'ROOT', self.root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL)

    def commit(self):
        self.git('add', '--all')
        self.git('-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgSign=false',
                 '-c', 'user.name=Release fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '--quiet', '-m', 'Fixture')

    def build(self, name='release'):
        target = Path(self.temp.name) / name
        package.build(self.revision, target)
        return target

    def test_bundles_have_exact_manifest_scope_and_hashes(self):
        target = self.build()
        for bundle, expected in (('cisco-cli', set(package.CLI + package.SHARED)),
                                 ('announcement-service', set(package.RUNTIME))):
            with tarfile.open(target / (bundle + '.tar.gz'), 'r:gz') as archive:
                self.assertEqual(set(archive.getnames()), expected | {'SOURCE.json'})
                manifest = json.load(archive.extractfile('SOURCE.json'))
                self.assertEqual(set(manifest['files']), expected)
                self.assertEqual(manifest['source_commit'], self.revision)
                for name, digest in manifest['files'].items():
                    self.assertEqual(digest, hashlib.sha256(archive.extractfile(name).read()).hexdigest())
                for item in archive.getmembers():
                    self.assertTrue(item.isfile())
                    self.assertEqual((item.mtime, item.uid, item.gid), (0, 0, 0))

    def test_rebuild_is_byte_identical_and_checksums_cover_all_assets(self):
        first, second = self.build('first'), self.build('second')
        self.assertEqual(sorted(p.name for p in first.iterdir()), sorted(p.name for p in second.iterdir()))
        for path in first.iterdir():
            self.assertEqual(path.read_bytes(), (second / path.name).read_bytes(), path.name)
        sums = {}
        for line in (first / 'SHA256SUMS').read_text().splitlines():
            digest, name = line.split('  ', 1)
            sums[name] = digest
            self.assertEqual(digest, hashlib.sha256((first / name).read_bytes()).hexdigest())
        self.assertEqual(set(sums), {'SOURCE.json', 'cisco-cli.tar.gz', 'announcement-service.tar.gz'})

    def test_dirty_tracked_and_private_untracked_files_cannot_enter_release(self):
        (self.root / package.CLI[0]).write_bytes(b'SECRET_LOCAL_EDIT\n')
        private = self.root / '.local/private.json'
        private.parent.mkdir()
        private.write_bytes(b'SECRET_UNTRACKED\n')
        target = self.build()
        with tarfile.open(target / 'cisco-cli.tar.gz', 'r:gz') as archive:
            self.assertEqual(archive.extractfile(package.CLI[0]).read(), self.source)
            self.assertFalse(any(name.startswith('.local') for name in archive.getnames()))
        for path in target.iterdir():
            self.assertNotIn(b'SECRET_', path.read_bytes())

    def test_requires_full_lowercase_commit_hash_not_branch_tag_or_abbreviation(self):
        for revision in ('HEAD', 'main', 'v1.0.0', self.revision[:12], 'A' * 40,
                         self.revision + ';touch-not-executed'):
            with self.subTest(revision=revision), patch.object(package, 'git') as git:
                with self.assertRaises(ValueError):
                    package.build(revision, Path(self.temp.name) / 'invalid')
                git.assert_not_called()

    def test_missing_allowlisted_source_fails_without_creating_output(self):
        (self.root / package.CLI[0]).unlink()
        self.commit()
        revision = self.git('rev-parse', 'HEAD').decode().strip()
        target = Path(self.temp.name) / 'missing'
        with self.assertRaisesRegex(ValueError, 'committed regular file'):
            package.build(revision, target)
        self.assertFalse(target.exists())

    def test_allowlisted_symlink_is_rejected_even_when_target_exists(self):
        path = self.root / package.SHARED[0]
        path.unlink()
        outside = Path(self.temp.name) / 'private-outside-release'
        outside.write_text('SECRET_OUTSIDE')
        path.symlink_to(outside)
        self.commit()
        revision = self.git('rev-parse', 'HEAD').decode().strip()
        target = Path(self.temp.name) / 'symlink'
        with self.assertRaisesRegex(ValueError, 'committed regular file'):
            package.build(revision, target)
        self.assertFalse(target.exists())

    def test_invalid_python_is_rejected_before_output_is_created(self):
        (self.root / package.CLI[0]).write_text('def invalid syntax\n')
        self.commit()
        revision = self.git('rev-parse', 'HEAD').decode().strip()
        target = Path(self.temp.name) / 'invalid-python'
        with self.assertRaises(SyntaxError):
            package.build(revision, target)
        self.assertFalse(target.exists())

    def test_existing_output_directory_is_never_overwritten(self):
        target = self.build()
        previous = (target / 'SHA256SUMS').read_bytes()
        with self.assertRaises(FileExistsError):
            package.build(self.revision, target)
        self.assertEqual((target / 'SHA256SUMS').read_bytes(), previous)


if __name__ == '__main__':
    unittest.main()
