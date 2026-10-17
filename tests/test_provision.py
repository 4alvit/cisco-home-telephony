import copy
import importlib.util
import json
from pathlib import Path
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
