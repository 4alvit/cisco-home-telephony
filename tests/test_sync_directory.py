"""Directory-only updates preserve all existing gateway access and identities."""
import copy
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
try:
    import sync_directory
finally:
    sys.path.pop(0)


class SyncDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.site = {'node_ipv4': '192.168.50.10', 'lan_cidr': '192.168.50.0/24',
                     'ntp_ipv4': '192.168.50.1', 'phone_cidrs': ['192.168.50.21/32'],
                     'gateway': {'enabled': True, 'ami_client_ipv4': '192.168.50.10'},
                     'softphones': [{'extension': '209', 'label': 'Mac', 'room': "Operator's Mac",
                                    'source_ipv4': '192.168.50.90', 'transport': 'tcp'}]}
        self.inventory = {'phones': [{
            'model': 'CP-7945G', 'verified': True, 'mac': '02:00:00:00:00:01', 'label': '201',
            'lines': [{'extension': '201', 'label': '201'}, {'extension': '202', 'label': '202'}],
            'firmware_load': 'SIP45.9-4-2SR4-3S', 'room': 'Living Room'}]}
        self.existing = {'persistent_enabled': True, 'bind_ipv4': '192.168.50.10', 'api_port': 18084,
                         'api_token': 'a' * 48, 'api_clients': ['192.168.50.11'],
                         'ami': {'host': '192.168.50.10', 'port': 5038, 'username': 'gateway', 'secret': 'b' * 48},
                         'dial_allowlist': ['201', '202', '209', '600', '630', '701'], 'ring_allowlist': ['640'],
                         'phones': [{'id': '201', 'lines': ['201', '202'], 'ipv4': '192.168.50.21',
                                     'device': 'SEP020000000001', 'port': 18080, 'auth_path': '/auth/' + 'c' * 64,
                                     'username': 'u' * 24, 'password': 'p' * 24, 'room': 'Old Room'}]}

    def update(self):
        return sync_directory.update(self.site, self.inventory, self.existing)

    def test_only_directory_metadata_changes_without_mutating_inputs(self):
        before = copy.deepcopy((self.site, self.inventory, self.existing))
        result = self.update()
        expected = copy.deepcopy(self.existing)
        expected['phones'][0]['room'] = 'Living Room'
        expected['softphones'] = [{'extension': '209', 'room': "Operator's Mac"}]
        expected['services'] = [{'number': '600', 'label': 'Echo test', 'kind': 'diagnostic'}]
        self.assertEqual(result, expected)
        self.assertEqual((self.site, self.inventory, self.existing), before)
        self.assertEqual(sync_directory.update(self.site, self.inventory, result), result)

    def test_absent_room_removes_prior_name_and_empty_metadata_preserves_dial_permissions(self):
        self.inventory['phones'][0].pop('room')
        self.existing['softphones'] = [{'extension': '209', 'room': 'Old Mac'}]
        self.site['softphones'] = []
        result = self.update()
        self.assertNotIn('room', result['phones'][0])
        self.assertEqual(result['softphones'], [])
        self.assertEqual(result['dial_allowlist'], self.existing['dial_allowlist'])
        self.existing.pop('softphones')
        self.assertNotIn('softphones', self.update())

    def test_softphone_without_room_is_explicitly_unassigned(self):
        self.site['softphones'][0].pop('room')
        self.assertEqual(self.update()['softphones'], [{'extension': '209', 'room': None}])

    def test_device_or_line_drift_is_rejected(self):
        variants = []
        value = copy.deepcopy(self.existing); value['phones'][0]['device'] = 'SEP020000000002'; variants.append(value)
        value = copy.deepcopy(self.existing); value['phones'][0].update(id='202', lines=['202', '201']); variants.append(value)
        value = copy.deepcopy(self.existing); value['phones'][0]['lines'] = ['201']; variants.append(value)
        value = copy.deepcopy(self.existing)
        second = dict(value['phones'][0], id='203', lines=['203', '204'], device='SEP020000000002',
                      ipv4='192.168.50.22', port=18081)
        value['phones'].append(second); variants.append(value)
        value = copy.deepcopy(value); value['phones'][1]['device'] = value['phones'][0]['device']; variants.append(value)
        for value in variants:
            with self.subTest(value=value['phones']):
                with self.assertRaisesRegex(ValueError, 'physical'):
                    sync_directory.update(self.site, self.inventory, value)

    def test_invalid_source_is_rejected_before_rendering(self):
        self.inventory['phones'][0]['room'] = None
        with self.assertRaisesRegex(ValueError, 'room must'): self.update()
        self.inventory['phones'][0].pop('room')
        self.inventory['phones'][0]['verified'] = False
        with self.assertRaisesRegex(ValueError, 'verification'): self.update()

    def test_no_implicit_dial_permission_widening(self):
        self.existing['dial_allowlist'].remove('209')
        before = copy.deepcopy(self.existing)
        with self.assertRaisesRegex(ValueError, 'review dial permissions separately'): self.update()
        self.assertEqual(self.existing, before)

    def test_cli_creates_private_output_and_never_overwrites_an_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = [sys.executable, str(ROOT / 'scripts/sync_directory.py')]
            for name, value in (('site', self.site), ('phones', self.inventory), ('gateway', self.existing)):
                path = root / (name + '.json'); path.write_text(json.dumps(value))
                command.extend(['--' + name, str(path)])
            output = root / 'new.json'; command.extend(['--output', str(output)])
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(json.loads(output.read_text()), self.update())
            original = output.read_bytes()
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_bytes(), original)
            self.assertNotIn(self.existing['api_token'], result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
