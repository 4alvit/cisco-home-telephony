"""Hotline provisioning stays confined to one physical phone and exact TFTP inputs."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import preflight
import profile_tftp
import provision
import render_k3s


class HotlineProfiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name).resolve() / 'output'
        self.site = {
            'node_ipv4': '192.168.50.10', 'lan_cidr': '192.168.50.0/24',
            'ntp_ipv4': '192.168.50.1', 'namespace': 'telephony', 'node_name': 'example',
            'tftp_image': 'example.invalid/tftp:test',
            'gateway': {'enabled': True, 'ami_client_ipv4': '192.168.50.10'},
            'hotline_ring_group': {'enabled': True, 'extension': '622', 'source': '201',
                                   'members': ['203', '209'], 'timeout': 25},
            'softphones': [{'extension': '209', 'label': 'Desktop',
                           'source_ipv4': '192.168.50.20', 'transport': 'tcp'}],
        }
        self.inventory = {'phones': [
            {'model': 'CP-7945G', 'verified': True, 'mac': '020000000001',
             'label': 'Door', 'firmware_load': 'SIP45.9-4-2SR4-3S', 'ipv4': '192.168.50.101',
             'lines': [{'extension': '201', 'label': '201'}, {'extension': '202', 'label': '202'}]},
            {'model': 'CP-7945G', 'verified': True, 'mac': '020000000002',
             'label': 'Hall', 'firmware_load': 'SIP45.9-4-2SR4-3S', 'ipv4': '192.168.50.102',
             'lines': [{'extension': '203', 'label': '203'}, {'extension': '204', 'label': '204'}]},
        ]}
        self.hotline_name = 'dialplan-hotline-020000000001.xml'
        self.source_name = 'SEP020000000001.cnf.xml'
        self.other_name = 'SEP020000000002.cnf.xml'

    def generate(self, site=None):
        provision.generate(site or self.site, self.inventory, self.out)
        return {p.name: p.read_text() for p in (self.out / 'tftp').iterdir()}

    def test_only_source_profile_changes_and_both_lines_keep_their_identity(self):
        disabled = dict(self.site, hotline_ring_group={'enabled': False})
        original = self.generate(disabled)
        passwords = (self.out / 'secrets.json').read_bytes()
        active = self.generate()
        self.assertEqual(active['dialplan.xml'], original['dialplan.xml'])
        self.assertEqual(active[self.other_name], original[self.other_name])
        self.assertEqual(active[self.source_name], original[self.source_name].replace(
            '<dialTemplate>dialplan.xml</dialTemplate>',
            f'<dialTemplate>{self.hotline_name}</dialTemplate>'))
        self.assertEqual((self.out / 'secrets.json').read_bytes(), passwords)
        phone = ET.fromstring(active[self.source_name])
        self.assertEqual([x.findtext('name') for x in phone.findall('./sipProfile/sipLines/line')],
                         ['201', '202'])
        self.assertIsNone(phone.find('.//lineIndex'))
        self.assertEqual(ET.fromstring(active[self.hotline_name])[0].attrib,
                         {'match': '', 'timeout': '0', 'rewrite': '622'})
        self.assertEqual(profile_tftp.validate_profiles(active, self.site, self.inventory), active)
        self.assertEqual(profile_tftp.read_profiles(self.out / 'tftp', self.site, self.inventory), active)

    def test_profile_deployment_stages_only_exact_validated_files(self):
        files = self.generate()
        manifest = render_k3s.render_profile_tftp(self.site, files, self.inventory)
        config, deployment = manifest['items']
        self.assertEqual(set(config['data']), set(files))
        stage = deployment['spec']['template']['spec']['initContainers'][0]['command'][2]
        self.assertIn(f'cp -L /config/{self.hotline_name} /served/root/{self.hotline_name}', stage)
        self.assertNotIn('/firmware/', stage)

    def test_disabling_hotline_restores_source_profile_and_removes_generated_template(self):
        disabled = dict(self.site, hotline_ring_group={'enabled': False})
        original = self.generate(disabled)
        self.generate()
        restored = self.generate(disabled)
        self.assertEqual(restored, original)
        self.assertNotIn('exten => 622,', (self.out / 'asterisk/extensions.conf').read_text())
        profile_tftp.read_profiles(self.out / 'tftp', disabled, self.inventory)

    def test_generated_cleanup_preserves_unrelated_operator_files(self):
        self.generate()
        operator = self.out / 'tftp/dialplan-hotline-operator.xml'
        operator.write_text('operator-owned')
        self.generate(dict(self.site, hotline_ring_group={'enabled': False}))
        self.assertEqual(operator.read_text(), 'operator-owned')
        self.assertFalse((self.out / 'tftp' / self.hotline_name).exists())

    def test_secondary_or_absent_source_is_rejected_before_creating_output(self):
        for source in ('202', '299'):
            with self.subTest(source=source):
                bad = copy.deepcopy(self.site)
                bad['hotline_ring_group']['source'] = source
                with self.assertRaises(ValueError):
                    provision.generate(bad, self.inventory, self.out)
                self.assertFalse(self.out.exists())
        with self.assertRaises(ValueError):
            provision.generate(self.site, None, self.out, pbx_only=True)
        self.assertFalse(self.out.exists())

    def test_wrong_phone_reference_and_undeclared_file_are_rejected(self):
        files = self.generate()
        changed = dict(files)
        changed[self.other_name] = changed[self.other_name].replace('dialplan.xml', self.hotline_name)
        for bad in (changed, {**files, 'dialplan-hotline-020000000002.xml': files[self.hotline_name]},
                    {k: v for k, v in files.items() if k != self.hotline_name}):
            with self.subTest(names=sorted(bad)), self.assertRaises(ValueError):
                profile_tftp.validate_profiles(bad, self.site, self.inventory)
        with self.assertRaises(ValueError):
            profile_tftp.validate_profiles(files, dict(self.site, hotline_ring_group={'enabled': False}), self.inventory)

    def test_hotline_file_rejects_other_destinations_attributes_and_extra_rules(self):
        files = self.generate()
        rule = '<TEMPLATE match="" timeout="0" rewrite="622"/>'
        for altered in (rule.replace('622', '911'), rule.replace('timeout="0"', 'timeout="4"'),
                        rule.replace('match=""', 'match="2.."'), rule.replace('/>', ' line="1"/>'),
                        rule + rule, '<Other>' + rule + '</Other>'):
            with self.subTest(altered=altered), self.assertRaises(ValueError):
                profile_tftp.validate_profiles(
                    {**files, self.hotline_name: '<DIALTEMPLATE>' + altered + '</DIALTEMPLATE>'},
                    self.site, self.inventory)
        path = self.out / 'tftp' / self.hotline_name
        path.unlink()
        path.symlink_to(self.out / 'tftp/dialplan.xml')
        with self.assertRaises(ValueError):
            profile_tftp.read_profiles(self.out / 'tftp', self.site, self.inventory)

    def test_preflight_requires_exact_inventory_bound_hotline_and_rejects_stale_route(self):
        self.generate()
        configs = render_k3s.read_files(self.out / 'asterisk', '.conf')
        preflight.check_service_config(self.site, configs, self.inventory)
        with self.assertRaises(ValueError):
            preflight.check_service_config(self.site, configs)
        for dial in (configs['extensions.conf'] + '\nexten => 622,1,Hangup()\n',
                     configs['extensions.conf'].replace('PJSIP/203', 'PJSIP/299')):
            with self.assertRaises(ValueError):
                preflight.check_service_config(self.site, {**configs, 'extensions.conf': dial}, self.inventory)
        with self.assertRaises(ValueError):
            preflight.check_service_config(dict(self.site, hotline_ring_group={'enabled': False}), configs, self.inventory)


if __name__ == '__main__':
    unittest.main()
