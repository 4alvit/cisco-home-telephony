"""Exercise independent SIP lines on one physical CP-7945G."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('provision_lines', ROOT / 'scripts/provision.py')
p = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p)


class PhoneLinesTests(unittest.TestCase):
    def setUp(self):
        self.site = {'node_ipv4': '192.168.50.10', 'lan_cidr': '192.168.50.0/24', 'ntp_ipv4': '192.168.50.1'}
        self.legacy = {'model': 'CP-7945G', 'verified': True, 'mac': '02:00:00:00:00:01',
                       'extension': '201', 'label': 'Office', 'firmware_load': 'SIP45.9-4-2SR4-3S'}
        self.phone = copy.deepcopy(self.legacy)
        del self.phone['extension']
        self.phone['lines'] = [{'extension': '201', 'label': 'Office'},
                               {'extension': '202', 'label': 'Private'}]

    def test_two_buttons_have_independent_credentials_and_direct_dial_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            self.assertEqual(p.generate(self.site, {'phones': [self.phone]}, out), 1)
            self.assertEqual(len(list((out / 'tftp').glob('SEP*.cnf.xml'))), 1)
            lines = ET.parse(out / 'tftp/SEP020000000001.cnf.xml').findall('./sipProfile/sipLines/line')
            saved = json.loads((out / 'secrets.json').read_text())
            self.assertEqual(set(saved), {'201', '202'})
            self.assertNotEqual(saved['201'], saved['202'])
            self.assertEqual([line.attrib for line in lines], [{'button': '1'}, {'button': '2'}])
            sip = (out / 'asterisk/pjsip.conf').read_text()
            dial = (out / 'asterisk/extensions.conf').read_text()
            for line, ext, label in zip(lines, ('201', '202'), ('Office', 'Private')):
                with self.subTest(extension=ext):
                    for tag in ('name', 'authName', 'contact'):
                        self.assertEqual(line.findtext(tag), ext)
                    self.assertEqual(line.findtext('featureLabel'), label)
                    self.assertEqual(line.findtext('displayName'), label)
                    self.assertEqual(line.findtext('authPassword'), saved[ext])
                    self.assertEqual(line.findtext('sharedLine'), 'false')
                    self.assertIsNone(line.find('lineIndex'))
                    self.assertEqual(line.findtext('autoAnswer/autoAnswerEnabled'), '0')
                    self.assertIn(f'auth=auth-{ext}\naors={ext}', sip)
                    self.assertIn(f'username={ext}\npassword={saved[ext]}', sip)
                    self.assertIn(f'callerid={label} <{ext}>', sip)
                    self.assertIn(f'exten => {ext},1,Dial(PJSIP/{ext},25)', dial)
            self.assertIn('exten => 620,1,Dial(PJSIP/201,25)', dial)
            self.assertNotIn('Dial(PJSIP/201&PJSIP/202', dial)

    def test_legacy_upgrade_and_downgrade_preserve_existing_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / 'secrets.json').write_text(json.dumps({'201': 'a' * 30}))
            p.generate(self.site, {'phones': [self.legacy]}, out)
            original = (out / 'tftp/SEP020000000001.cnf.xml').read_bytes()
            p.generate(self.site, {'phones': [self.phone]}, out)
            saved = (out / 'secrets.json').read_bytes()
            self.assertEqual(json.loads(saved)['201'], 'a' * 30)
            p.generate(self.site, {'phones': [self.phone]}, out)
            self.assertEqual((out / 'secrets.json').read_bytes(), saved)
            p.generate(self.site, {'phones': [self.legacy]}, out)
            self.assertEqual((out / 'tftp/SEP020000000001.cnf.xml').read_bytes(), original)
            self.assertEqual((out / 'secrets.json').read_bytes(), saved)
            self.assertNotIn('[202]', (out / 'asterisk/pjsip.conf').read_text())

    def test_ring_group_uses_one_primary_line_per_physical_phone(self):
        other = copy.deepcopy(self.phone)
        other['mac'] = '020000000002'
        other['lines'] = [{'extension': '203', 'label': 'Kitchen'}, {'extension': '204', 'label': 'Other'}]
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(p.generate(self.site, {'phones': [self.phone, other]}, tmp), 2)
            dial = (Path(tmp) / 'asterisk/extensions.conf').read_text()
            self.assertIn('exten => 620,1,Dial(PJSIP/201&PJSIP/203,25)', dial)
            for ext in ('202', '204'):
                self.assertIn(f'exten => {ext},1,Dial(PJSIP/{ext},25)', dial)

    def test_explicit_single_line_and_shared_transport_are_supported(self):
        phone = copy.deepcopy(self.phone)
        phone['lines'] = [phone['lines'][0]]
        phone['transport'] = 'udp'
        with tempfile.TemporaryDirectory() as tmp:
            p.generate(self.site, {'phones': [phone]}, tmp)
            xml = ET.parse(Path(tmp) / 'tftp/SEP020000000001.cnf.xml')
            self.assertEqual(len(xml.findall('./sipProfile/sipLines/line')), 1)
            self.assertEqual(xml.findtext('./transportLayerProtocol'), '2')
            self.assertIn('transport=transport-udp', (Path(tmp) / 'asterisk/pjsip.conf').read_text())

    def test_mixed_schema_and_invalid_lines_are_rejected_before_output(self):
        variants = []
        mixed = copy.deepcopy(self.phone); mixed['extension'] = '201'; variants.append(mixed)
        for lines in (None, {}, [], self.phone['lines'] + [{'extension': '203', 'label': 'Third'}],
                      [{'extension': '201'}], [{'extension': '201', 'label': 'Office', 'button': 2}],
                      [{'extension': '630', 'label': 'Service'}], [{'extension': '201', 'label': 'Bad\nlabel'}],
                      [{'extension': '201', 'label': 'First'}, {'extension': '201', 'label': 'Duplicate'}]):
            phone = copy.deepcopy(self.phone); phone['lines'] = lines; variants.append(phone)
        for phone in variants:
            with self.subTest(phone=phone), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / 'not-created'
                with self.assertRaises(ValueError): p.generate(self.site, {'phones': [phone]}, out)
                self.assertFalse(out.exists())

    def test_extensions_are_unique_across_all_lines_and_mac_is_per_device(self):
        for other in (dict(self.legacy, mac='020000000002', extension='202'),
                      dict(self.legacy, mac='020000000001', extension='203')):
            with self.subTest(other=other), self.assertRaises(ValueError):
                p.validate(self.site, {'phones': [self.phone, other]})

    def test_validation_does_not_mutate_input_and_normalizes_each_line(self):
        phone = copy.deepcopy(self.phone); phone['lines'][0]['extension'] = 201
        inventory = {'phones': [phone]}; before = copy.deepcopy(inventory)
        phones, _ = p.validate(self.site, inventory)
        self.assertEqual(inventory, before)
        self.assertEqual(phones[0]['mac'], '020000000001')
        self.assertEqual(phones[0]['lines'][0]['extension'], '201')

    def test_wallpaper_menu_access_is_explicit_without_changing_legacy_default(self):
        for value in (None, True, False):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                phone = copy.deepcopy(self.phone)
                if value is not None:
                    phone['background_image_access'] = value
                p.generate(self.site, {'phones': [phone]}, tmp)
                xml = ET.parse(Path(tmp) / 'tftp/SEP020000000001.cnf.xml')
                self.assertEqual(xml.findtext('./commonProfile/backgroundImageAccess'),
                                 None if value is None else str(value).lower())
                self.assertIsNone(xml.find('.//lineIndex'))

    def test_wallpaper_menu_access_rejects_nonboolean_values_before_output(self):
        for value in (None, 0, 1, 'true', 'false', [], {}):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                phone = copy.deepcopy(self.phone); phone['background_image_access'] = value
                out = Path(tmp) / 'not-created'
                with self.assertRaisesRegex(ValueError, 'background_image_access must be boolean'):
                    p.generate(self.site, {'phones': [phone]}, out)
                self.assertFalse(out.exists())

    def test_example_requires_physical_verification(self):
        with self.assertRaises(ValueError):
            p.validate(self.site, json.loads((ROOT / 'examples/phones.two-lines.example.json').read_text()))


if __name__ == '__main__':
    unittest.main()
