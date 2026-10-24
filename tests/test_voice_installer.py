"""Exercise bootstrap identity/order and cleanup without touching host packages."""
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('voice_installer', ROOT / 'containers/voice/install_locked.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def test_official_wheel_is_hash_checked_before_it_is_written(self):
        for valid in (True, False):
            with self.subTest(valid=valid), tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / 'pip.whl'
                data = b'synthetic verified wheel'
                expected = hashlib.sha256(data).hexdigest() if valid else '0' * 64
                with patch.object(installer.urllib.request, 'urlopen', return_value=io.BytesIO(data)) as opened, \
                        patch.object(installer, 'PIP_SHA256', expected):
                    if valid:
                        installer.download_installer(destination)
                        self.assertEqual(destination.read_bytes(), data)
                    else:
                        with self.assertRaises(ValueError):
                            installer.download_installer(destination)
                        self.assertFalse(destination.exists())
                    opened.assert_called_once_with(installer.PIP_URL, timeout=30)

    def test_oversized_response_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(installer.urllib.request, 'urlopen', return_value=io.BytesIO(b'abcd')), \
                patch.object(installer, 'MAX_WHEEL_BYTES', 3):
            with self.assertRaises(ValueError):
                installer.download_installer(Path(directory) / 'pip.whl')

    def test_global_installer_and_pip_free_child_both_end_without_vendors(self):
        for present in (True, False):
            with self.subTest(global_pip=present), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                ensurepip = root / 'ensurepip'; ensurepip.mkdir()
                (ensurepip / 'old-vendor.whl').write_bytes(b'old')
                requirements = root / 'requirements.lock'; requirements.write_text('synthetic==1\n')
                state = {'pip': present}; calls = []; wheel_paths = []

                def spec(name):
                    return object() if (state['pip'] if name == 'pip' else ensurepip.exists()) else None

                def fetch(destination):
                    destination.write_bytes(b'verified wheel'); wheel_paths.append(destination)

                def run(arguments, **kwargs):
                    calls.append(arguments)
                    self.assertEqual(arguments[1], '-I')
                    self.assertNotIn('PYTHONPATH', kwargs['env'])
                    self.assertNotIn('PYTHONHOME', kwargs['env'])
                    if 'uninstall' in arguments:
                        self.assertEqual(arguments[2:4], ['-m', 'pip'])
                        self.assertNotIn(str(wheel_paths[0]), arguments)
                        state['pip'] = False
                    else:
                        self.assertFalse(state['pip'], 'Global pip must be removed before wheel execution')
                        self.assertEqual(arguments[2:4], ['-c', installer.PIP_MAIN])
                        self.assertTrue(Path(arguments[4]).is_file())

                with patch.object(installer, 'download_installer', side_effect=fetch), \
                        patch.object(installer.importlib.util, 'find_spec', side_effect=spec), \
                        patch.object(installer.subprocess, 'run', side_effect=run), \
                        patch.object(installer.sysconfig, 'get_path', return_value=str(root)), \
                        patch.dict(os.environ, {'PYTHONPATH': '/untrusted', 'PYTHONHOME': '/untrusted'}):
                    installer.install_locked(requirements)
                self.assertEqual(sum('uninstall' in call for call in calls), int(present))
                self.assertEqual(sum('install' in call for call in calls), 1)
                self.assertEqual(sum('check' in call for call in calls), 1)
                self.assertFalse(ensurepip.exists())
                self.assertFalse(wheel_paths[0].exists())

    def test_failed_global_uninstall_prevents_dependency_install(self):
        with tempfile.TemporaryDirectory() as directory:
            requirements = Path(directory) / 'requirements.lock'; requirements.write_text('x==1\n')
            with patch.object(installer, 'download_installer'), \
                    patch.object(installer.importlib.util, 'find_spec', return_value=object()), \
                    patch.object(installer.subprocess, 'run') as run:
                with self.assertRaises(RuntimeError):
                    installer.install_locked(requirements)
                self.assertEqual(run.call_count, 1)
                self.assertIn('uninstall', run.call_args.args[0])

    def test_symlink_lock_is_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'real').write_text('x==1\n')
            (root / 'link').symlink_to(root / 'real')
            with patch.object(installer, 'download_installer') as fetch:
                with self.assertRaises(ValueError):
                    installer.install_locked(root / 'link')
                fetch.assert_not_called()
