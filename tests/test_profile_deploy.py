"""Profile-only integration: no PBX inputs, strict files and private outputs."""
import base64
import json
import os
import runpy
import shlex
import shutil
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_k3s import fixture, render
from test_profile_tftp import inventory, files

ROOT = Path(__file__).resolve().parents[1]


class ProfileDeploymentTests(unittest.TestCase):
    def test_profile_manifest_has_only_inventory_files_and_no_firmware_or_pbx(self):
        site = fixture()
        manifest = render.render_profile_tftp(site, files(), inventory())
        self.assertEqual([item['kind'] for item in manifest['items']], ['Secret', 'Deployment'])
        secret, deployment = manifest['items']
        self.assertEqual(set(secret['data']), set(files()))
        self.assertEqual({k: base64.b64decode(v).decode() for k, v in secret['data'].items()}, files())
        self.assertTrue(secret['immutable'])
        self.assertEqual(deployment['metadata']['name'], 'cisco-tftp-profiles')
        self.assertEqual(deployment['spec']['replicas'], 1)
        self.assertEqual(deployment['spec']['strategy'], {'type': 'Recreate'})
        pod = deployment['spec']['template']['spec']
        self.assertEqual(pod['restartPolicy'], 'Always')
        self.assertFalse(pod.get('hostPID', False))
        self.assertFalse(pod.get('shareProcessNamespace', False))
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual({v['name'] for v in pod['volumes']}, {'config', 'served', 'ownership'})
        self.assertNotIn('/var/lib/cisco-tftp"', json.dumps(pod))
        self.assertNotIn('asterisk', json.dumps(manifest))
        stage = pod['initContainers'][0]['command'][2]
        self.assertNotIn('*', stage)
        for name in files():
            self.assertIn(f'cp -L /config/{name} /served/root/{name}', stage)
        self.assertIn('chmod 0444', stage)
        self.assertIn('chmod 00555', stage)

    def test_both_modes_share_lock_and_real_readiness_and_readonly_serve(self):
        for manifest in (render.render_profile_tftp(fixture(), files(), inventory()),
                         render.render_tftp(fixture(), files())):
            pod = manifest['items'][-1]['spec']['template']['spec']
            daemon = pod['containers'][0]
            self.assertIn({'name': 'CISCO_TFTP_LOCK', 'value': '/run/cisco-tftp/owner.lock'}, daemon['env'])
            ownership = next(v for v in pod['volumes'] if v['name'] == 'ownership')
            self.assertEqual(ownership['hostPath']['path'], '/var/lib/cisco-tftp-control')
            self.assertEqual(daemon['readinessProbe']['exec']['command'], ['/usr/local/bin/cisco-tftp', '--check'])
            self.assertIn('--ipv4', daemon['args'])
            self.assertTrue(next(v for v in daemon['volumeMounts'] if v['name'] == 'served')['readOnly'])
            self.assertNotIn('--create', daemon['args'])

    def test_renderer_rejects_extra_firmware_and_mismatched_profile(self):
        for bad in (dict(files(), **{'image.loads': 'bytes'}),
                    dict(files(), **{'SEP02000000000A.cnf.xml': '<device/>'})):
            with self.assertRaises(ValueError):
                render.render_profile_tftp(fixture(), bad, inventory())

    def test_stage_clears_inherited_setgid_without_changing_volume_root(self):
        chmod = shutil.which("gchmod") or shutil.which("chmod")
        version = subprocess.run([chmod, "--version"], capture_output=True, text=True) if chmod else None
        if version is None or "GNU coreutils" not in version.stdout:
            self.skipTest("GNU chmod required to reproduce preserved directory SGID")
        from test_wallpaper import wallpaper
        check_root = runpy.run_path(str(ROOT / "containers/tftp/runtime.py"))["check_root"]
        for profiles_only in (False, True):
            with self.subTest(profiles_only=profiles_only), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                for name in ("firmware", "config", "wallpaper", "served", "bin"):
                    (root / name).mkdir()
                (root / "bin/chmod").symlink_to(chmod)
                for name, content in files().items():
                    (root / "config" / name).write_text(content)
                for name, content in wallpaper().items():
                    (root / "wallpaper" / name).write_bytes(content)
                (root / "firmware/pilot.sbn").write_bytes(b"synthetic firmware")
                served = root / "served/root"
                served.mkdir()
                directories = [root / "served", served, served / "Desktops", served / "Desktops/320x212x16"]
                for directory in directories[:2]:
                    # Seed the emptyDir and staging root with SGID even on hosts
                    # that do not propagate the bit to new subdirectories.
                    directory.chmod(0o2770)
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o2770)
                manifest = (render.render_profile_tftp(fixture(), files(), inventory(), wallpaper())
                            if profiles_only else render.render_tftp(fixture(), files(), wallpaper()))
                command = manifest["items"][-1]["spec"]["template"]["spec"]["initContainers"][0]["command"]
                script = command[2]
                for name in ("firmware", "config", "wallpaper", "served"):
                    script = script.replace("/" + name, shlex.quote(str(root / name)))
                env = dict(os.environ, PATH=str(root / "bin") + os.pathsep + os.environ["PATH"])
                try:
                    result = subprocess.run(command[:2] + [script], env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(stat.S_IMODE((root / "served").stat().st_mode), 0o2770)
                    for directory in directories[1:]:
                        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o555)
                    for path in served.rglob("*"):
                        if path.is_file():
                            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o444)
                    check_root(str(served))
                finally:
                    for directory in directories:
                        if directory.exists():
                            directory.chmod(0o770)

    def make_inputs(self, root):
        (root / 'site.json').write_text(json.dumps(fixture()))
        phones = inventory()
        address_map = {'version': 1, 'phones': [{'mac': p['mac'], 'ipv4': p.pop('ipv4')} for p in phones['phones']]}
        (root / 'phones.json').write_text(json.dumps(phones))
        (root / 'addresses.json').write_text(json.dumps(address_map))
        (root / 'tftp').mkdir()
        for name, text in files().items():
            (root / 'tftp' / name).write_text(text + '<!-- secret-marker-never-print -->')
        return ['--site', str(root / 'site.json'), '--profiles-only', '--phone-addresses', str(root / 'addresses.json')]

    def test_cli_profiles_does_not_need_firmware_or_asterisk_and_no_kube_in_local_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            args = self.make_inputs(root)
            (root / 'bin').mkdir()
            kubectl = root / 'bin/kubectl'
            kubectl.write_text('#!/bin/sh\necho unexpected-kubectl >&2\nexit 99\n')
            kubectl.chmod(0o700)
            env = dict(os.environ, PATH=str(root / 'bin') + os.pathsep + os.environ['PATH'])
            for command in ([sys.executable, 'scripts/preflight.py'],
                            [sys.executable, 'scripts/render_k3s.py'], ['bash', 'scripts/deploy.sh']):
                result = subprocess.run(command + args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('secret-marker', result.stdout + result.stderr)
                self.assertNotIn('unexpected-kubectl', result.stdout + result.stderr)
            self.assertFalse((root / 'rendered/core.yaml').exists())
            self.assertFalse((root / 'rendered/tftp.yaml').exists())
            policy = json.loads((root / 'rendered/profile-tftp-policy.json').read_text())
            self.assertEqual(policy, {'version': 1, 'bind_ipv4': fixture()['node_ipv4'], 'phone_ipv4': ['192.168.50.100']})
            for path in (root / 'rendered').iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_cli_rejects_missing_address_map_and_extra_firmware(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            args = self.make_inputs(root)
            result = subprocess.run([sys.executable, 'scripts/preflight.py'] + args[:-2], cwd=ROOT, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            (root / 'tftp/image.sbn').write_text('never serve')
            result = subprocess.run([sys.executable, 'scripts/preflight.py'] + args, cwd=ROOT, capture_output=True)
            self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
