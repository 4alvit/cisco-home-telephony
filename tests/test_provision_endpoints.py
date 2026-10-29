"""Offline endpoint additions preserve the current configuration and credentials."""
import copy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
try:
    import provision_endpoints as endpoints
finally:
    sys.path.pop(0)


class ProvisionEndpointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / 'source'
        self.source.mkdir()
        self.out = self.root / 'candidate'
        self.secrets = self.root / 'endpoint-secrets.json'
        self.site = {
            'node_ipv4': '192.168.50.10', 'gateway': {'enabled': True},
            'softphones': [
                {'extension': '290', 'label': 'Speaker client', 'source_ipv4': '192.168.50.91',
                 'transport': 'udp', 'codecs': ['ulaw', 'alaw']},
                {'extension': '291', 'label': 'Display client', 'source_ipv4': '192.168.50.92',
                 'transport': 'tcp', 'codecs': ['g722', 'ulaw', 'alaw']},
            ]}
        self.original = {
            'pjsip.conf': b'; manually tuned live snapshot\n[transport-udp]\ntype=transport\nprotocol=udp\nbind=192.168.50.10:5060\n'
                          b'[transport-tcp]\ntype=transport\nprotocol=tcp\nbind=192.168.50.10:5060\n'
                          b'[201]\ntype=endpoint\nallow=g722,ulaw,alaw\ncustom_value=keep\n'
                          b'[auth-201]\ntype=auth\npassword=ExistingCiscoPassword1234',
            'extensions.conf': b'; keep custom service and all whitespace\n[general]\nstatic=yes\n[home]\n'
                               b'exten => 201,1,Dial(PJSIP/201,25)\n same => n,Hangup()\n'
                               b'exten => 631,1,Playback(custom-service)\n'
                               b'[custom-context]\nexten => 700,1,Hangup()\n'
                               b'[home](+)\nexten => 642,1,Playback(custom-additive-service)',
            'manager.conf': b'[general]\nenabled=yes\n; custom manager options\n',
            'preserve.bin': b'\x00\xff\r\nuntouched',
        }
        self.write_source(self.original)

    def write_source(self, configs):
        for name, data in configs.items():
            (self.source / name).write_bytes(data)

    def generate(self, source=None, site=None, out=None, secrets=None):
        return endpoints.generate(source or self.source, site if site is not None else self.site,
                                  out or self.out, secrets or self.secrets)

    def files(self, directory):
        return {path.name: path.read_bytes() for path in directory.iterdir()}

    def assert_no_output(self):
        self.assertFalse(self.out.exists())
        self.assertFalse(self.secrets.exists())

    def test_additions_preserve_all_unmanaged_bytes_and_use_private_outputs(self):
        self.assertEqual(self.generate(), (2, 4))
        self.assertEqual(self.files(self.source), self.original)
        result = self.files(self.out)
        self.assertEqual(set(result), set(self.original))
        for name, original in self.original.items():
            actual = endpoints.strip_managed(result[name])[0] if name in endpoints.TARGETS else result[name]
            self.assertEqual(actual, original, name)
            self.assertEqual(stat.S_IMODE((self.out / name).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.secrets.stat().st_mode), 0o600)
        self.assertIn(b'allow=ulaw,alaw\n', result['pjsip.conf'])
        self.assertIn(b'allow=g722,ulaw,alaw\n', result['pjsip.conf'])
        self.assertIn(b'permit=192.168.50.91/32\n', result['pjsip.conf'])
        self.assertIn(b'permit=192.168.50.92/32\n', result['pjsip.conf'])
        self.assertIn(b'[home]\n' + endpoints.BEGIN, result['extensions.conf'])
        self.assertIn(b'${DB(cisco-dnd/290)}', result['extensions.conf'])

    def test_reruns_and_previous_output_as_source_are_identical(self):
        self.generate()
        first, secrets = self.files(self.out), self.secrets.read_bytes()
        self.generate()
        self.assertEqual(self.files(self.out), first)
        self.assertEqual(self.secrets.read_bytes(), secrets)
        next_out = self.root / 'next'
        self.generate(source=self.out, out=next_out)
        self.assertEqual(self.files(next_out), first)
        self.assertEqual(self.secrets.read_bytes(), secrets)

    def test_additive_routes_honor_dnd_without_gateway_enabled_in_manifest(self):
        for setting in (None, False):
            with self.subTest(gateway_enabled=setting):
                site = copy.deepcopy(self.site)
                if setting is None:
                    site.pop('gateway')
                else:
                    site['gateway']['enabled'] = setting
                self.generate(site=site)
                dialplan = (self.out / 'extensions.conf').read_text()
                for client in site['softphones']:
                    extension = client['extension']
                    self.assertIn(f'exten => {extension},1,GotoIf($["${{DB(cisco-dnd/{extension})}}" = "1"]?dnd)', dialplan)
                self.assertEqual(dialplan.count(' same => n(dnd),Busy(3)\n'), len(site['softphones']))
                self.assertEqual(endpoints.strip_managed(dialplan.encode())[0], self.original['extensions.conf'])
    def test_removal_restores_original_bytes_and_retains_credentials(self):
        self.generate()
        saved = self.secrets.read_bytes()
        removed = copy.deepcopy(self.site)
        removed['softphones'] = []
        next_out = self.root / 'removed'
        self.generate(source=self.out, site=removed, out=next_out)
        self.assertEqual(self.files(next_out), self.original)
        self.assertEqual(self.secrets.read_bytes(), saved)

    def test_changed_codec_and_client_set_preserve_existing_secrets(self):
        self.generate()
        saved = json.loads(self.secrets.read_bytes())
        changed = copy.deepcopy(self.site)
        changed['softphones'] = [dict(changed['softphones'][0], codecs=['alaw'])]
        next_out = self.root / 'changed'
        self.generate(source=self.out, site=changed, out=next_out)
        self.assertEqual(json.loads(self.secrets.read_bytes()), saved)
        self.assertIn(b'allow=alaw\n', (next_out / 'pjsip.conf').read_bytes())
        self.assertNotIn(b'[291]', (next_out / 'pjsip.conf').read_bytes())

    def test_initially_empty_endpoint_list_preserves_snapshot_and_saved_credentials(self):
        empty = dict(self.site, softphones=[])
        self.generate(site=empty)
        self.assertEqual(self.files(self.out), self.original)
        self.assertEqual(json.loads(self.secrets.read_bytes()), {})
        saved = {'290': 'x' * 24}
        self.secrets.write_text(json.dumps(saved))
        self.generate(site=empty)
        self.assertEqual(self.files(self.out), self.original)
        self.assertEqual(json.loads(self.secrets.read_bytes()), saved)

    def test_optional_dial_audio_adds_owned_lines_and_preserves_existing_services(self):
        audio = json.loads((ROOT / 'deploy/dial-audio.example.json').read_text())
        original = (json.dumps(audio, indent=4) + '\n').encode()
        (self.source / 'dial-audio.json').write_bytes(original)
        self.generate()
        result = json.loads((self.out / 'dial-audio.json').read_bytes())
        self.assertEqual(result, dict(audio, local_lines=['201', '202', '209', '290', '291']))
        self.assertEqual((self.source / 'dial-audio.json').read_bytes(), original)
        for name in self.original:
            data = (self.out / name).read_bytes()
            self.assertEqual(endpoints.strip_managed(data)[0] if name in endpoints.TARGETS else data,
                             self.original[name])
        first, saved = self.files(self.out), self.secrets.read_bytes()
        self.generate()
        self.assertEqual(self.files(self.out), first)
        next_out = self.root / 'next'
        self.generate(source=self.out, out=next_out)
        self.assertEqual(self.files(next_out), first)
        self.assertEqual(self.secrets.read_bytes(), saved)
        removed = self.root / 'removed'
        self.generate(source=next_out, site=dict(self.site, softphones=[]), out=removed)
        self.assertEqual(json.loads((removed / 'dial-audio.json').read_bytes()), audio)
        self.assertEqual(self.secrets.read_bytes(), saved)

    def test_dial_audio_keeps_existing_line_order_and_removes_only_retired_owned_lines(self):
        audio = json.loads((ROOT / 'deploy/dial-audio.example.json').read_text())
        audio['local_lines'] = ['201', '290', '209']
        (self.source / 'dial-audio.json').write_text(json.dumps(audio))
        self.generate()
        first = self.files(self.out)
        next_out = self.root / 'next'
        self.generate(source=self.out, out=next_out)
        self.assertEqual(self.files(next_out), first)
        changed = dict(self.site, softphones=[dict(self.site['softphones'][1]),
                                            dict(self.site['softphones'][0], extension='292')])
        removed = self.root / 'changed'
        self.generate(source=next_out, site=changed, out=removed)
        self.assertEqual(json.loads((removed / 'dial-audio.json').read_bytes())['local_lines'],
                         ['201', '209', '291', '292'])

    def test_optional_dial_audio_is_not_created_when_absent(self):
        self.generate()
        self.assertNotIn('dial-audio.json', self.files(self.out))

    def test_malformed_dial_audio_fails_before_any_output(self):
        audio = json.loads((ROOT / 'deploy/dial-audio.example.json').read_text())
        bad = [b'{', b'[]', b'{}', b'{"local_lines": [], "local_lines": ["201"]}']
        bad.extend(json.dumps(dict(audio, local_lines=lines)).encode()
                   for lines in ('201', [], [201], ['201', '201'], ['2\u0661\u0661']))
        bad.extend(json.dumps(value).encode() for value in (
            dict(audio, credentials_file='relative.conf'),
            dict(audio, services={'555': {'kind': 'unsupported'}}),
            dict(audio, services={'777': {'kind': 'media', 'path': '/media/../private', 'max_seconds': 60}}),
            dict(audio, services={'777': {'kind': 'media', 'path': '/media/a.mp3', 'max_seconds': float('nan')}})))
        for data in bad:
            with self.subTest(data=data):
                (self.source / 'dial-audio.json').write_bytes(data)
                with self.assertRaisesRegex(ValueError, 'invalid dial-audio.json'):
                    self.generate()
                self.assert_no_output()

    def test_unmanaged_endpoint_auth_and_dialplan_collisions_fail_before_output(self):
        for name, collision in (
                ('pjsip.conf', b'\n[290]\ntype=endpoint\n'),
                ('pjsip.conf', b'\n[ 290 ]\ntype=endpoint\n'),
                ('pjsip.conf', b'\n[auth-softphone-291]\ntype=auth\n'),
                ('extensions.conf', b'\nexten => 290,1,Hangup()\n'),
                ('extensions.conf', b'\nexten = 291/201,1,Hangup()\n'),
                ('other.conf', b'[other]\nexten => 290,hint,PJSIP/290\n')):
            with self.subTest(name=name, collision=collision):
                self.write_source(self.original)
                path = self.source / name
                path.write_bytes(self.original.get(name, b'') + collision)
                with self.assertRaisesRegex(ValueError, 'collides'):
                    self.generate()
                self.assert_no_output()
                if name not in self.original:
                    path.unlink()

    def test_malformed_duplicate_and_unpaired_markers_fail_before_output(self):
        for suffix in (
                endpoints.BEGIN,
                endpoints.END + endpoints.BEGIN,
                endpoints.BEGIN + endpoints.END + endpoints.BEGIN + endpoints.END,
                endpoints.BEGIN.replace(b'; BEGIN', b'; BROKEN') + endpoints.END,
                endpoints.BEGIN + endpoints.END):
            with self.subTest(suffix=suffix):
                self.write_source(self.original)
                (self.source / 'pjsip.conf').write_bytes(self.original['pjsip.conf'] + b'\n' + suffix)
                with self.assertRaisesRegex(ValueError, 'marker'):
                    self.generate()
                self.assert_no_output()

    def test_owned_credentials_require_the_original_stable_secret_file(self):
        self.generate()
        next_out = self.root / 'next'
        missing = self.root / 'missing-secrets.json'
        with self.assertRaisesRegex(ValueError, 'stable secrets'):
            self.generate(source=self.out, out=next_out, secrets=missing)
        self.assertFalse(next_out.exists())
        self.assertFalse(missing.exists())
        changed = json.loads(self.secrets.read_bytes())
        changed['290'] = 'x' * 24
        self.secrets.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'stable secrets'):
            self.generate(source=self.out, out=next_out)
        self.assertFalse(next_out.exists())
        self.assertEqual(json.loads(self.secrets.read_bytes()), changed)

    def test_invalid_input_transport_and_home_fail_before_output(self):
        for invalid in (
                dict(self.site, node_ipv4='8.8.8.8'),
                dict(self.site, gateway={'enabled': 'yes'}),
                dict(self.site, softphones=[dict(self.site['softphones'][0], codecs=[])])):
            with self.subTest(site=invalid), self.assertRaises(ValueError):
                self.generate(site=invalid)
            self.assert_no_output()
        (self.source / 'pjsip.conf').write_bytes(self.original['pjsip.conf'].replace(b'[transport-udp]', b'[different]'))
        with self.assertRaisesRegex(ValueError, 'transport'):
            self.generate()
        self.assert_no_output()
        self.write_source(self.original)
        (self.source / 'extensions.conf').write_bytes(self.original['extensions.conf'].replace(b'[home]', b'[other]'))
        with self.assertRaisesRegex(ValueError, 'home'):
            self.generate()
        self.assert_no_output()

    def test_same_or_nested_paths_and_secret_inside_snapshot_are_rejected(self):
        for out, secrets in ((self.source, self.secrets), (self.source / 'nested', self.secrets),
                             (self.out, self.source / 'secrets.json'), (self.out, self.out / 'secrets.json')):
            with self.subTest(out=out, secrets=secrets), self.assertRaises((ValueError, FileNotFoundError)):
                self.generate(out=out, secrets=secrets)
            self.assertEqual(self.files(self.source), self.original)
            self.assert_no_output()

    def test_symlinks_and_nonregular_inputs_are_rejected(self):
        linked_source = self.root / 'linked-source'
        linked_source.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.generate(source=linked_source)
        self.out.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.generate()
        self.out.unlink()
        self.secrets.symlink_to(self.source / 'manager.conf')
        with self.assertRaises(ValueError):
            self.generate()
        self.secrets.unlink()
        extra = self.source / 'linked.conf'
        extra.symlink_to(self.source / 'manager.conf')
        with self.assertRaises(ValueError):
            self.generate()
        extra.unlink()
        os.mkfifo(extra)
        with self.assertRaises(ValueError):
            self.generate()
        extra.unlink()
        self.assert_no_output()

    def test_output_symlink_and_unexpected_file_do_not_change_existing_files(self):
        self.out.mkdir()
        target = self.root / 'untouched'
        target.write_bytes(b'keep')
        link = self.out / 'manager.conf'
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            self.generate()
        self.assertEqual(target.read_bytes(), b'keep')
        link.unlink()
        link = self.out / 'unexpected.conf'
        link.write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError, 'absent'):
            self.generate()
        self.assertEqual(link.read_bytes(), b'keep')
        self.assertFalse(self.secrets.exists())

    def test_invalid_or_reused_saved_secrets_fail_before_output(self):
        for saved in ({'290': 'short'}, {'290': 'x' * 24, '291': 'x' * 24},
                      {'290': 'ExistingCiscoPassword1234'}):
            with self.subTest(saved_keys=list(saved)):
                self.secrets.write_text(json.dumps(saved))
                self.secrets.chmod(0o600)
                with self.assertRaises(ValueError):
                    self.generate()
                self.assertFalse(self.out.exists())
                self.assertEqual(json.loads(self.secrets.read_bytes()), saved)

    def test_cli_reports_counts_without_private_names_or_credentials(self):
        manifest = self.root / 'endpoints.json'
        manifest.write_text(json.dumps(self.site))
        manifest.chmod(0o600)
        process = subprocess.run([
            sys.executable, str(ROOT / 'scripts/provision_endpoints.py'),
            '--config-dir', str(self.source), '--endpoints', str(manifest),
            '--secrets', str(self.secrets), '--out', str(self.out),
        ], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout, 'Prepared 2 managed SIP endpoints across 4 configuration files.\n')
        self.assertEqual(process.stderr, '')
        for secret in json.loads(self.secrets.read_bytes()).values():
            self.assertNotIn(secret, process.stdout + process.stderr)

    def test_cli_refuses_to_overwrite_endpoint_input_in_output(self):
        self.out.mkdir()
        manifest = self.out / 'manager.conf'
        saved = json.dumps(self.site).encode()
        manifest.write_bytes(saved)
        manifest.chmod(0o600)
        process = subprocess.run([
            sys.executable, str(ROOT / 'scripts/provision_endpoints.py'),
            '--config-dir', str(self.source), '--endpoints', str(manifest),
            '--secrets', str(self.secrets), '--out', str(self.out),
        ], capture_output=True, text=True)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn('outside the output', process.stderr)
        self.assertEqual(manifest.read_bytes(), saved)
        self.assertFalse(self.secrets.exists())


if __name__ == '__main__':
    unittest.main()
