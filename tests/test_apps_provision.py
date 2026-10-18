"""Private app migration is exact, repeatable, and independent of SIP secrets."""
import copy
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
try:
    import configure_gateway
    import configure_phone_apps as apps
    import provision
finally:
    sys.path.pop(0)


class AppProvisionTests(unittest.TestCase):
    def setUp(self):
        self.site = {'node_ipv4': '192.168.50.10', 'lan_cidr': '192.168.50.0/24',
                     'ntp_ipv4': '192.168.50.1', 'phone_cidrs': ['192.168.50.21/32'],
                     'gateway': {'enabled': True, 'ami_client_ipv4': '192.168.50.10'}}
        self.callback = {'device': 'SEP020000000001', 'auth_bind_ipv4': '192.168.50.10',
                         'auth_port': 18080, 'auth_path': '/auth/' + 'c' * 64,
                         'phone_ipv4': '192.168.50.21', 'username': 'u' * 24, 'password': 'p' * 24}
        self.inventory = {'phones': [{'model': 'CP-7945G', 'verified': True, 'mac': '02:00:00:00:00:01',
                                     'label': '201_202', 'firmware_load': 'SIP45.9-4-2SR4-3S',
                                     'lines': [{'extension': '201', 'label': '201'},
                                               {'extension': '202', 'label': '202'}],
                                     'authentication_url': 'http://192.168.50.10:18080/auth/' + 'c' * 64}]}
        self.ami = {'username': 'gateway', 'secret': 'b' * 48}
        self.config = self.gateway(self.inventory)

    def gateway(self, inventory):
        with patch.object(configure_gateway.secrets, 'token_urlsafe', return_value='a' * 48):
            return configure_gateway.prepare(self.site, inventory, [self.callback], self.ami, ['192.168.50.11'])

    def test_migration_preserves_inputs_credentials_and_stable_paths(self):
        before = copy.deepcopy((self.config, self.inventory))
        with patch.object(apps.secrets, 'token_hex', return_value='d' * 64):
            config, inventory = apps.prepare(self.config, self.inventory)
        self.assertEqual((self.config, self.inventory), before)
        self.assertEqual(config['phones'][0]['apps_path'], '/apps/' + 'd' * 64)
        preserved = copy.deepcopy(config)
        preserved.pop('applications')
        preserved['phones'][0].pop('apps_path')
        self.assertEqual(preserved, self.config)
        with patch.object(apps.secrets, 'token_hex', side_effect=AssertionError('must reuse nonce')):
            self.assertEqual(apps.prepare(config, inventory), (config, inventory))
            recovered, same_inventory = apps.prepare(self.config, inventory)
        self.assertEqual((recovered, same_inventory), (config, inventory))
        self.assertEqual(self.gateway(inventory)['phones'][0]['apps_path'], config['phones'][0]['apps_path'])
        self.assertEqual(self.gateway(inventory)['applications'], {'enabled': True})

    def test_cross_phone_and_callback_mapping_are_rejected(self):
        for mutation in ('mac', 'lines', 'authentication_url', 'verified'):
            inventory = copy.deepcopy(self.inventory)
            phone = inventory['phones'][0]
            if mutation == 'mac': phone['mac'] = '02:00:00:00:00:02'
            if mutation == 'lines': phone['lines'].reverse()
            if mutation == 'authentication_url': phone[mutation] += '/other'
            if mutation == 'verified': phone[mutation] = False
            with self.subTest(mutation=mutation), self.assertRaises(apps.gateway.Refused):
                apps.prepare(self.config, inventory)

    def test_existing_app_urls_are_not_silently_replaced(self):
        config, inventory = apps.prepare(self.config, self.inventory)
        for field, value in (
                ('services_url', inventory['phones'][0]['services_url'] + '/'),
                ('services_url', 'http://192.168.50.99:18080' + config['phones'][0]['apps_path']),
                ('directory_url', inventory['phones'][0]['directory_url'] + '/other'),
                ('services_url', 'http://192.168.50.10:18080/apps/' + 'c' * 64)):
            altered = copy.deepcopy(inventory)
            altered['phones'][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(apps.gateway.Refused):
                apps.prepare(config, altered)
        inventory['phones'][0].pop('directory_url')
        with self.assertRaises(apps.gateway.Refused): apps.prepare(config, inventory)

    def test_optional_urls_have_private_literal_hosts_and_plain_paths(self):
        bad_urls = ['http://example.com/apps/a', 'http://8.8.8.8/apps/a',
                    'http://127.0.0.1/apps/a', 'http://169.254.169.254/apps/a',
                    'http://192.168.50.10:0/apps/a', 'http://192.168.50.10:70000/apps/a',
                    'http://user:password@192.168.50.10/apps/a',
                    'http://192.168.50.10/apps/a?token=x', 'http://192.168.50.10/apps/a#frag',
                    'http://192.168.50.10/apps/../a', 'http://192.168.50.10/apps/%2e/a',
                    'http://192.168.50.10/apps//a', 'http://192.168.50.10/apps/a\n',
                    'ftp://192.168.50.10/apps/a']
        for field in ('services_url', 'directory_url'):
            for url in bad_urls:
                inventory = copy.deepcopy(self.inventory)
                inventory['phones'][0][field] = url
                with self.subTest(field=field, url=url), self.assertRaises(ValueError):
                    provision.validate(self.site, inventory)

    def test_xml_services_are_opt_in_and_preserve_both_lines_and_native_logs(self):
        _, inventory = apps.prepare(self.config, self.inventory)
        for selected, enabled in ((self.inventory, False), (inventory, True)):
            phones, ntp = provision.validate(self.site, selected)
            tree = ET.fromstring(provision.phone_xml(phones[0], self.site['node_ipv4'], ntp,
                                                    {'201': 'a' * 30, '202': 'b' * 30}))
            self.assertEqual([line.findtext('name') for line in tree.findall('./sipProfile/sipLines/line')],
                             ['201', '202'])
            if not enabled:
                self.assertIsNone(tree.find('phoneServices'))
                self.assertIsNone(tree.find('servicesURL'))
                continue
            self.assertEqual(tree.findtext('servicesURL'), inventory['phones'][0]['services_url'])
            self.assertEqual(tree.findtext('directoryURL'), inventory['phones'][0]['directory_url'])
            self.assertEqual(tree.findtext('phoneServices/provisioning'), '0')
            services = tree.findall('phoneServices/phoneService')
            self.assertEqual([entry.findtext('name') for entry in services],
                             ['Missed Calls', 'Received Calls', 'Placed Calls', 'Home Apps', 'Phone Directory'])
            self.assertEqual([entry.get('type') for entry in services], ['1', '1', '1', '0', '1'])
            self.assertTrue(all(entry.get('category') == '0' for entry in services))

    def test_private_outputs_are_new_and_never_overwrite(self):
        config, inventory = apps.prepare(self.config, self.inventory)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'apps'
            apps.write_outputs(out, config, inventory)
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
            for name, expected in (('gateway.json', config), ('phones.json', inventory)):
                path = out / name
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(apps.read_private(path), expected)
            with self.assertRaises(FileExistsError): apps.write_outputs(out, {}, {})
            self.assertEqual(json.loads((out / 'gateway.json').read_text()), config)
            (out / 'gateway.json').chmod(0o644)
            with self.assertRaises(ValueError): apps.read_private(out / 'gateway.json')
            link = Path(tmp) / 'link'
            link.symlink_to(out / 'phones.json')
            with self.assertRaises(OSError): apps.read_private(link)


if __name__ == '__main__':
    unittest.main()
