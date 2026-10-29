"""Room metadata migrates without widening the gateway's control surface."""
import copy
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
try:
    import configure_gateway
finally:
    sys.path.pop(0)


class ConfigureGatewayTests(unittest.TestCase):
    def setUp(self):
        self.callback = {
            'device': 'SEP020000000001', 'auth_bind_ipv4': '192.168.50.10', 'auth_port': 18080,
            'auth_path': '/auth/' + 'c' * 64, 'phone_ipv4': '192.168.50.21',
            'username': 'u' * 24, 'password': 'p' * 24}
        self.site = {'node_ipv4': '192.168.50.10', 'phone_cidrs': ['192.168.50.21/32'],
                     'gateway': {'enabled': True, 'ami_client_ipv4': '192.168.50.10'}}
        self.inventory = {'phones': [{
            'model': 'CP-7945G', 'verified': True, 'mac': '02:00:00:00:00:01',
            'lines': [{'extension': '201', 'label': '201'}, {'extension': '202', 'label': '202'}],
            'authentication_url': 'http://192.168.50.10:18080/auth/' + 'c' * 64}]}
        self.ami = {'username': 'gateway', 'secret': 'b' * 48}

    def prepare(self, control_endpoints=()):
        with patch.object(configure_gateway.secrets, 'token_urlsafe', return_value='a' * 48):
            return configure_gateway.prepare(self.site, self.inventory, [self.callback],
                                             self.ami, ['192.168.50.11'], control_endpoints=control_endpoints)

    def add_softphones(self):
        self.site['softphones'] = [
            {'extension': '290', 'label': 'Speaker client', 'source_ipv4': '192.168.50.91',
             'transport': 'udp', 'codecs': ['ulaw', 'alaw']},
            {'extension': '291', 'label': 'Display client', 'source_ipv4': '192.168.50.92',
             'transport': 'tcp'}]

    def test_absent_rooms_preserve_existing_gateway_shape(self):
        result = self.prepare()
        self.assertNotIn('room', result['phones'][0])
        self.assertNotIn('softphones', result)
        self.site['softphones'] = []
        self.assertEqual(self.prepare(), result)

    def test_rooms_and_softphone_metadata_propagate_without_credential_changes(self):
        original = self.prepare()
        self.inventory['phones'][0]['room'] = 'Living Room'
        self.site['softphones'] = [{'extension': '209', 'label': 'Mac', 'room': "Operator's Mac",
                                   'source_ipv4': '192.168.50.90', 'transport': 'tcp'}]
        before = copy.deepcopy((self.site, self.inventory, self.callback, self.ami))
        result = self.prepare()
        self.assertEqual(result['phones'][0].pop('room'), 'Living Room')
        self.assertEqual(result.pop('softphones'), [{'extension': '209', 'room': "Operator's Mac"}])
        self.assertEqual(result['dial_allowlist'], ['201', '202', '209', '600', '601', '620'])
        result['dial_allowlist'].remove('209')
        self.assertEqual(result, original)
        self.assertEqual((self.site, self.inventory, self.callback, self.ami), before)
        self.site['softphones'][0].pop('room')
        self.assertEqual(self.prepare()['softphones'], [{'extension': '209', 'room': None}])

    def test_invalid_inventory_and_softphone_rooms_are_not_normalized(self):
        for room in (None, '', ' Room', 'Room ', 'Room;value=x', 1):
            for kind in ('phone', 'softphone'):
                with self.subTest(room=room, kind=kind):
                    self.inventory['phones'][0].pop('room', None)
                    self.site.pop('softphones', None)
                    if kind == 'phone':
                        self.inventory['phones'][0]['room'] = room
                    else:
                        self.site['softphones'] = [{'extension': '209', 'label': 'Mac', 'room': room,
                                                   'source_ipv4': '192.168.50.90', 'transport': 'tcp'}]
                    with self.assertRaisesRegex(ValueError, 'room must'): self.prepare()

    def test_softphone_cannot_duplicate_a_physical_secondary_line(self):
        self.site['softphones'] = [{'extension': '202', 'label': 'Mac',
                                   'source_ipv4': '192.168.50.90', 'transport': 'tcp'}]
        with self.assertRaisesRegex(ValueError, 'duplicate extension'): self.prepare()

    def test_control_opt_in_changes_only_selected_softphone_metadata(self):
        self.add_softphones()
        before = copy.deepcopy((self.site, self.inventory, self.callback, self.ami))
        baseline = self.prepare()
        enabled = self.prepare(['290'])
        self.assertEqual(enabled['softphones'], [
            {'extension': '290', 'room': None, 'controls_enabled': True},
            {'extension': '291', 'room': None}])
        enabled['softphones'][0].pop('controls_enabled')
        self.assertEqual(enabled, baseline)
        self.assertEqual((self.site, self.inventory, self.callback, self.ami), before)

    def test_control_selection_must_be_existing_softphones(self):
        self.add_softphones()
        for selection in (['201'], ['202'], ['299'], ['290', '201']):
            with self.subTest(selection=selection), self.assertRaisesRegex(
                    configure_gateway.gateway.Refused, 'control_endpoint_must_be_configured_softphone'):
                self.prepare(selection)
        for selection in (None, '290', [290], ['600'], ['290\n'], [[]], ['290'] * 51):
            with self.subTest(selection=selection), self.assertRaisesRegex(
                    configure_gateway.gateway.Refused, 'bad_control_endpoint'):
                self.prepare(selection)

    def test_repeated_control_flags_are_stable_and_defaults_remain_disabled(self):
        self.add_softphones()
        first = self.prepare(['291', '290', '290'])
        self.assertTrue(all(client['controls_enabled'] for client in first['softphones']))
        self.assertEqual(self.prepare(['290', '291']), first)
        self.assertFalse(any('controls_enabled' in client for client in self.prepare()['softphones']))

    def test_cli_passes_repeatable_control_flags_and_refuses_invalid_target_before_output(self):
        self.add_softphones()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory_path, ami_path = root / 'phones.json', root / 'ami.json'
            inventory_path.write_text(json.dumps(self.inventory))
            ami_path.write_text(json.dumps(self.ami))
            common = ['configure_gateway.py', '--site', str(root / 'site.json'),
                      '--inventory', str(inventory_path), '--callback', str(root / 'callback.json'),
                      '--ami-secret', str(ami_path), '--ha-source-ip', '192.168.50.11', '--enable-persistent']
            for flag_values, valid in ((['290', '291'], True), (['202'], False), (['299'], False)):
                with self.subTest(flag_values=flag_values):
                    output = root / ('gateway-' + '-'.join(flag_values) + '.json')
                    args = [*common, '--output', str(output)]
                    for extension in flag_values:
                        args.extend(['--control-endpoint', extension])
                    with patch.object(sys, 'argv', args), \
                            patch.object(configure_gateway, 'load_site', return_value=self.site), \
                            patch.object(configure_gateway, 'load_config', return_value=self.callback), \
                            patch.object(configure_gateway.os, 'umask'), \
                            contextlib.redirect_stdout(io.StringIO()) as printed:
                        if valid:
                            configure_gateway.main()
                        else:
                            with self.assertRaises(configure_gateway.gateway.Refused):
                                configure_gateway.main()
                    self.assertEqual(output.exists(), valid)
                    if valid:
                        generated = json.loads(output.read_text())
                        self.assertTrue(all(client['controls_enabled'] for client in generated['softphones']))
                        self.assertNotIn(generated['api_token'], printed.getvalue())
                        self.assertNotIn(self.ami['secret'], printed.getvalue())


if __name__ == '__main__':
    unittest.main()
