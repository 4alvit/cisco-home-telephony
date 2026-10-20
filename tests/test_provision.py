import copy
import importlib.util
import json
from pathlib import Path
import re
import stat
import tempfile
import unittest
import xml.etree.ElementTree as ET

spec=importlib.util.spec_from_file_location('provision',Path(__file__).parents[1]/'scripts/provision.py')
p=importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class ProvisionTest(unittest.TestCase):
    def setUp(self):
        self.site={'node_ipv4':'192.168.50.10','lan_cidr':'192.168.50.0/24','ntp_ipv4':'192.168.50.1'}
        self.inventory={'phones':[{'model':'CP-7945G','verified':True,'mac':'02:00:00:00:00:01','extension':'201','label':'Office','firmware_load':'SIP45.9-4-2SR4-3S'}]}

    def test_rejects_unverified_and_wrong_hardware(self):
        for field,value in [('verified',False),('model','CP-7940'),('firmware_load','SIP42.9-4-2'),('mac','01:00:00:00:00:01'),('extension','911'),('label','Office\n[evil]')]:
            inventory=copy.deepcopy(self.inventory)
            inventory['phones'][0][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):
                p.validate(self.site,inventory)

    def test_duplicate_extension_and_wrong_subnet(self):
        duplicate=copy.deepcopy(self.inventory)
        duplicate['phones'].append(dict(duplicate['phones'][0],mac='020000000002'))
        with self.assertRaises(ValueError):
            p.validate(self.site,duplicate)
        with self.assertRaises(ValueError):
            p.validate(dict(self.site,node_ipv4='10.0.0.1'),self.inventory)

    def test_matching_secrets_idempotence_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)
            p.generate(self.site,self.inventory,out)
            secrets=json.loads((out/'secrets.json').read_text())
            x=ET.parse(out/'tftp/SEP020000000001.cnf.xml')
            self.assertEqual(len(secrets['201']),30)  # CP-7945G authPassword limit
            self.assertEqual(x.findtext('.//authPassword'),secrets['201'])
            self.assertEqual(x.findtext('./sipProfile/sipStack/timerRegisterExpires'),'300')
            self.assertIsNone(x.find('./sipProfile/timerRegisterExpires'))
            self.assertEqual(x.findtext('.//processNodeName'),'192.168.50.10')
            self.assertEqual(x.findtext('.//transportLayerProtocol'),'1')
            self.assertEqual(x.findtext('.//loadInformation'),'SIP45.9-4-2SR4-3S')
            self.assertIn('password='+secrets['201'],(out/'asterisk/pjsip.conf').read_text())
            self.assertEqual(stat.S_IMODE((out/'secrets.json').stat().st_mode),0o600)
            p.generate(self.site,self.inventory,out)
            self.assertEqual(secrets,json.loads((out/'secrets.json').read_text()))
            self.assertNotIn('PJSIP/trunk',(out/'asterisk/extensions.conf').read_text())

    def test_legacy_screen_label_and_password_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.inventory['phones'][0]['label']='Home Office Upstairs'
            p.generate(self.site,self.inventory,tmp)
            x=ET.parse(Path(tmp)/'tftp/SEP020000000001.cnf.xml')
            self.assertEqual(x.findtext('.//phoneLabel'),'Home_Office_')
            self.assertEqual(x.findtext('.//displayName'),'Home Office Upstairs')
            (Path(tmp)/'secrets.json').write_text(json.dumps({'201':'a'*32}))
            with self.assertRaises(ValueError):p.generate(self.site,self.inventory,tmp)

    def test_wideband_capability_and_local_preference_cover_both_lines_and_transports(self):
        for transport in ('tcp', 'udp'):
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as tmp:
                inventory = copy.deepcopy(self.inventory)
                phone = inventory['phones'][0]
                phone.pop('extension')
                phone.update(transport=transport, lines=[
                    {'extension': '201', 'label': '201'},
                    {'extension': '202', 'label': '202'}])
                p.generate(self.site, inventory, tmp)
                root = Path(tmp)
                xml = ET.parse(root / 'tftp/SEP020000000001.cnf.xml')
                self.assertEqual(xml.findtext('./vendorConfig/g722CodecSupport'), '2')
                self.assertIsNone(xml.find('./sipProfile/g722CodecSupport'))
                # Do not introduce an unverified G.722 spelling in SIP45 XML.
                self.assertEqual(xml.findtext('./sipProfile/preferredCodec'), 'g711ulaw')
                config = (root / 'asterisk/pjsip.conf').read_text()
                endpoints = {
                    name: body for name, body in re.findall(
                        r'^\[([^]]+)\]\n(.*?)(?=^\[|\Z)', config, re.M | re.S)
                    if 'type=endpoint\n' in body}
                self.assertEqual(set(endpoints), {'201', '202'})
                for body in endpoints.values():
                    values = dict(line.split('=', 1) for line in body.splitlines() if '=' in line)
                    self.assertEqual(values['allow'].split(','), ['g722', 'ulaw', 'alaw'])
                    self.assertEqual(values['disallow'], 'all')
                    self.assertEqual(values['incoming_call_offer_pref'], 'local')
                    self.assertEqual(values['outgoing_call_offer_pref'], 'local_merge')
                    self.assertEqual(values['transport'], 'transport-' + transport)
                    self.assertEqual(values['direct_media'], 'no')
                    self.assertNotIn('preferred_codec_only', values)
                    self.assertFalse(any(key.startswith('codec_prefs_') for key in values))

    def test_wideband_local_policy_does_not_change_external_or_assist_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credentials = root / 'credentials.json'
            credentials.write_text(json.dumps({
                'username': '17770000001', 'password': 'test_password_only_123'}))
            credentials.chmod(0o600)
            site = copy.deepcopy(self.site)
            site['external_sip'] = {
                'enabled': True, 'public_ipv4': '8.8.8.8',
                'accounts': [{'provider': 'callcentric', 'enabled': True,
                              'credentials_file': str(credentials)}]}
            site['home_assistant'] = {
                'enabled': True, 'ami_client_ipv4': '192.168.50.30',
                'assist': {'enabled': True, 'host_ipv4': '192.168.50.30'}}
            external = p.external_sip.generate(site)
            p.generate(site, self.inventory, root / 'output')
            config = (root / 'output/asterisk/pjsip.conf').read_text()
            self.assertEqual(config.count(external['pjsip']), 1)
            self.assertNotIn('g722', external['pjsip'])
            self.assertNotIn('call_offer_pref', external['pjsip'])
            self.assertIn('allow=ulaw,alaw\n', external['pjsip'])
            assist = next(body for name, body in re.findall(
                r'^\[([^]]+)\]\n(.*?)(?=^\[|\Z)', config, re.M | re.S)
                if name == 'ha-assist')
            self.assertIn('allow=opus\n', assist)
            self.assertNotIn('g722', assist)
            self.assertNotIn('call_offer_pref', assist)

    def test_removed_phone_profile_is_not_served(self):
        with tempfile.TemporaryDirectory() as tmp:
            p.generate(self.site,self.inventory,tmp)
            self.inventory['phones'][0]['mac']='020000000002'
            p.generate(self.site,self.inventory,tmp)
            self.assertFalse((Path(tmp)/'tftp/SEP020000000001.cnf.xml').exists())

    def test_web_access_is_opt_in_per_phone(self):
        inventory=copy.deepcopy(self.inventory)
        inventory['phones'].extend([
            dict(inventory['phones'][0],mac='020000000002',extension='202',web_access=False),
            dict(inventory['phones'][0],mac='020000000003',extension='203',web_access=True),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            p.generate(self.site,inventory,tmp)
            for mac,expected in [('020000000001','1'),('020000000002','1'),('020000000003','0')]:
                with self.subTest(mac=mac):
                    x=ET.parse(Path(tmp)/'tftp'/f'SEP{mac}.cnf.xml')
                    self.assertEqual(x.findtext('./vendorConfig/webAccess'),expected)
                    self.assertEqual(x.findtext('./vendorConfig/sshAccess'),'1')

    def test_invalid_web_access_is_rejected_before_output(self):
        for value in ['true','false',0,1,None,[],{}]:
            with self.subTest(value=value),tempfile.TemporaryDirectory() as tmp:
                inventory=copy.deepcopy(self.inventory)
                inventory['phones'][0]['web_access']=value
                out=Path(tmp)/'not-created'
                with self.assertRaisesRegex(ValueError,'web_access must be boolean'):
                    p.generate(self.site,inventory,out)
                self.assertFalse(out.exists())

    def test_no_symlink_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'out'
            out.mkdir()
            (out/'tftp').symlink_to(Path(tmp),target_is_directory=True)
            with self.assertRaises(ValueError):
                p.generate(self.site,self.inventory,out)


if __name__=='__main__':
    unittest.main()
