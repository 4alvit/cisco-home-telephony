"""Generic SIP applications must not impersonate or widen access to Cisco phones."""
import copy
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
try:
    import provision
    import preflight
    import render_k3s
    import softphones
finally:
    sys.path.pop(0)


class SoftphoneTests(unittest.TestCase):
    def setUp(self):
        self.client = {'extension':'209','label':'Mac','source_ipv4':'192.168.50.90','transport':'tcp'}
        self.site = {'node_ipv4':'192.168.50.10','lan_cidr':'192.168.50.0/24','ntp_ipv4':'192.168.50.1',
                     'phone_cidrs':['192.168.50.21/32'], 'softphones':[self.client],
                     'gateway':{'enabled':True,'ami_client_ipv4':'192.168.50.10'},
                     'alarms':{'enabled':True}}
        self.inventory = {'phones':[{'model':'CP-7945G','verified':True,'mac':'02:00:00:00:00:01',
            'label':'Office','lines':[{'extension':'201','label':'201'},{'extension':'202','label':'202'}],
            'firmware_load':'SIP45.9-4-2SR4-3S'}]}

    def read_configs(self, path):
        return {p.name:p.read_text() for p in (Path(path)/'asterisk').glob('*.conf')}

    def check(self, configs, site=None):
        site = site or self.site
        preflight.check_phone_config(site, configs)
        preflight.check_service_config(site, configs)
        preflight.check_ha_config(site, configs)

    def test_cisco_profiles_secrets_acl_group_and_alert_helper_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)
            baseline=copy.deepcopy(self.site);baseline.pop('softphones')
            provision.generate(baseline,self.inventory,out)
            preserved={str(p.relative_to(out)):p.read_bytes() for p in out.rglob('*') if p.is_file()
                       and (p.parent.name=='tftp' or p.name.endswith('secret.json') or p.name=='secrets.json')}
            provision.generate(self.site,self.inventory,out)
            for name,value in preserved.items():self.assertEqual((out/name).read_bytes(),value,name)
            config=self.read_configs(out);self.check(config)
            self.assertIn('permit=192.168.50.90/32',config['pjsip.conf'])
            self.assertEqual(provision.phone_networks(self.site),['192.168.50.21/32'])
            self.assertEqual(provision.local_networks(self.site),['192.168.50.0/24','192.168.50.21/32','192.168.50.90/32'])
            helper=config['extensions.conf'].split('[cisco-alert]')[1]
            self.assertNotIn('209',helper)
            group=config['extensions.conf'].split('exten => 620,')[1].split('exten => 640,')[0]
            self.assertNotIn('209',group)
            self.assertIn('context=home',config['pjsip.conf'])
            for number in ('600','601','620','640'):
                self.assertIn('exten => '+number+',',config['extensions.conf'])
            self.assertEqual(len(list((out/'tftp').glob('SEP*.cnf.xml'))),1)

    def test_secrets_are_separate_strong_private_and_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);provision.generate(self.site,self.inventory,out)
            path=out/'softphone-secrets.json';saved=path.read_bytes()
            secret=json.loads(saved)['209'];cisco=json.loads((out/'secrets.json').read_text())
            self.assertRegex(secret,r'^[A-Za-z0-9_-]{24}$')
            self.assertNotIn(secret,cisco.values())
            self.assertNotIn('209',cisco)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
            provision.generate(self.site,self.inventory,out);self.assertEqual(path.read_bytes(),saved)
            removed=copy.deepcopy(self.site);removed['softphones']=[]
            provision.generate(removed,self.inventory,out);self.assertEqual(path.read_bytes(),saved)
            self.assertNotIn('auth-softphone-',self.read_configs(out)['pjsip.conf'])
            provision.generate(self.site,self.inventory,out);self.assertEqual(path.read_bytes(),saved)

    def test_local_softphone_wideband_preference_retains_g711_and_endpoint_identity(self):
        for transport in ('tcp', 'udp'):
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as tmp:
                site = copy.deepcopy(self.site)
                site['softphones'][0]['transport'] = transport
                provision.generate(site, self.inventory, tmp)
                configs = self.read_configs(tmp)
                self.check(configs, site)
                secret = json.loads((Path(tmp) / 'softphone-secrets.json').read_text())['209']
                endpoint, _ = softphones.render_client(site, site['softphones'][0], secret)
                body = endpoint.split('\n[auth-softphone-', 1)[0]
                values = dict(line.split('=', 1) for line in body.splitlines() if '=' in line)
                self.assertEqual(configs['pjsip.conf'].count(endpoint), 1)
                self.assertEqual(values['allow'].split(','), ['g722', 'ulaw', 'alaw'])
                self.assertEqual(values['disallow'], 'all')
                self.assertEqual(values['incoming_call_offer_pref'], 'local_first')
                self.assertEqual(values['outgoing_call_offer_pref'], 'local_merge')
                self.assertEqual(values['transport'], 'transport-' + transport)
                self.assertEqual(values['auth'], 'auth-softphone-209')
                self.assertEqual(values['permit'], '192.168.50.90/32')
                self.assertEqual(values['direct_media'], 'no')
                self.assertNotIn('preferred_codec_only', values)
                self.assertFalse(any(key.startswith('codec_prefs_') for key in values))
                for old in (
                        endpoint.replace('allow=g722,ulaw,alaw', 'allow=ulaw,alaw'),
                        endpoint.replace('incoming_call_offer_pref=local_first\n',
                                         'incoming_call_offer_pref=local\n'),
                        endpoint.replace('outgoing_call_offer_pref=local_merge\n', '')):
                    with self.subTest(stale=old), self.assertRaisesRegex(
                            ValueError, 'softphone configuration differs'):
                        softphones.check(site, dict(configs, **{
                            'pjsip.conf': configs['pjsip.conf'].replace(endpoint, old)}))

    def test_distinct_clients_cannot_share_a_secret_or_reuse_cisco_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);provision.generate(self.site,self.inventory,out)
            cisco=json.loads((out/'secrets.json').read_text())
            for saved in [{'209':cisco['201']},{'209':'x'*24,'210':'x'*24}]:
                (out/'softphone-secrets.json').write_text(json.dumps(saved))
                with self.subTest(saved_keys=list(saved)),self.assertRaises(ValueError):
                    provision.generate(self.site,self.inventory,out)

    def test_room_changes_only_softphone_callerid_without_changing_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            provision.generate(self.site, self.inventory, out)
            before = {p.relative_to(out): p.read_bytes() for p in out.rglob('*') if p.is_file()}
            self.site['softphones'][0]['room'] = "Operator's Mac"
            provision.generate(self.site, self.inventory, out)
            after = {p.relative_to(out): p.read_bytes() for p in out.rglob('*') if p.is_file()}
            key = Path('asterisk/pjsip.conf')
            self.assertEqual(after.pop(key), before.pop(key).replace(b'callerid=Mac <209>', b"callerid=Operator's Mac <209>"))
            self.assertEqual(after, before)
            self.check(self.read_configs(out))

    def test_invalid_room_is_rejected_before_output(self):
        for room in (None, 1, False, [], {}, '', ' Mac', 'Mac ', '\tMac', 'Mac\n',
                     'Mac;permit=all', 'Mac\\name', '"Mac"', 'M\u00e1c', '-Mac', 'A' * 49):
            with self.subTest(room=room), tempfile.TemporaryDirectory() as tmp:
                site = copy.deepcopy(self.site)
                site['softphones'][0]['room'] = room
                out = Path(tmp) / 'not-created'
                with self.assertRaisesRegex(ValueError, 'room must'):
                    provision.generate(site, self.inventory, out)
                self.assertFalse(out.exists())

    def test_invalid_input_fails_before_output(self):
        cases=[None,{},'209',[dict(self.client,extra=True)],[dict(self.client,extension=209)],
            [dict(self.client,extension='620')],[dict(self.client,label='bad\nvalue')],
            [dict(self.client,transport='tls')],[dict(self.client,source_ipv4='8.8.8.8')],
            [dict(self.client,source_ipv4='127.0.0.1')],[dict(self.client,source_ipv4='192.168.50.0/24')],
            [dict(self.client,source_ipv4='host.example')],[dict(self.client,source_ipv4=123)],
            [dict(self.client,source_ipv4='::1')],[self.client,self.client]]
        for clients in cases:
            with self.subTest(value=clients),tempfile.TemporaryDirectory() as tmp:
                site=copy.deepcopy(self.site);site['softphones']=clients;out=Path(tmp)/'not-created'
                with self.assertRaises(ValueError):provision.generate(site,self.inventory,out)
                self.assertFalse(out.exists())

    def test_collision_with_secondary_cisco_line_is_rejected(self):
        site=copy.deepcopy(self.site);site['softphones'][0]['extension']='202'
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'not-created'
            with self.assertRaisesRegex(ValueError,'duplicate extension'):provision.generate(site,self.inventory,out)
            self.assertFalse(out.exists())

    def test_private_ipv4_ranges_and_udp(self):
        for address in ['10.0.0.20','172.16.3.20','172.31.255.20','192.168.50.90']:
            site=copy.deepcopy(self.site);site['softphones'][0].update(source_ipv4=address,transport='udp')
            with self.subTest(address=address),tempfile.TemporaryDirectory() as tmp:
                provision.generate(site,self.inventory,tmp);config=self.read_configs(tmp);self.check(config,site)
                self.assertIn('auth=auth-softphone-209',config['pjsip.conf'])
                self.assertIn('transport=transport-udp\ncontext=home',config['pjsip.conf'])
                self.assertIn('permit='+address+'/32',config['pjsip.conf'])

    def test_softphone_only_does_not_need_fake_inventory_or_tftp(self):
        with tempfile.TemporaryDirectory() as tmp:
            provision.generate(self.site,None,tmp,pbx_only=True)
            config=self.read_configs(tmp);self.check(config)
            self.assertFalse((Path(tmp)/'tftp').exists())
            self.assertFalse((Path(tmp)/'secrets.json').exists())
            self.assertNotIn('[cisco-alert]',config['extensions.conf'])
            self.assertNotIn('exten => 620,',config['extensions.conf'])
            self.assertIn('Dial(PJSIP/209,25)',config['extensions.conf'])

    def test_preflight_rejects_stale_source_transport_or_removed_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            provision.generate(self.site,self.inventory,tmp);config=self.read_configs(tmp)
            for key,value in [('source_ipv4','192.168.50.91'),('transport','udp'),('label','NewLabel'),
                              ('room', "Operator's Mac")]:
                site=copy.deepcopy(self.site);site['softphones'][0][key]=value
                with self.subTest(key=key),self.assertRaises(ValueError):self.check(config,site)
            removed=copy.deepcopy(self.site);removed.pop('softphones')
            with self.assertRaises(ValueError):self.check(config,removed)

    def test_preflight_rejects_widened_softphone_acl_and_duplicate_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            provision.generate(self.site,self.inventory,tmp);config=self.read_configs(tmp)
            for text in [config['pjsip.conf'].replace('permit=192.168.50.90/32','permit=192.168.50.0/24'),
                         config['pjsip.conf']+'\n[209]\ntype=endpoint\n']:
                changed=dict(config,**{'pjsip.conf':text})
                with self.assertRaises(ValueError):self.check(changed)

    def test_preflight_keeps_cisco_acl_narrow(self):
        with tempfile.TemporaryDirectory() as tmp:
            provision.generate(self.site,self.inventory,tmp);config=self.read_configs(tmp)
            config['pjsip.conf']=config['pjsip.conf'].replace('permit=192.168.50.21/32','permit=192.168.50.21/32\npermit=192.168.50.90/32',1)
            with self.assertRaises(ValueError):self.check(config)

    def test_secret_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);out=root/'output';out.mkdir();target=root/'elsewhere';target.write_text('{}')
            (out/'softphone-secrets.json').symlink_to(target)
            with self.assertRaises(ValueError):provision.generate(self.site,self.inventory,out)
            self.assertEqual(target.read_text(),'{}')

    def test_render_validates_softphone_schema(self):
        site=dict(self.site,kube_context='test',namespace='telephony',node_name='voice-node',asterisk_image='example.invalid/asterisk@sha256:'+'a'*64)
        site['softphones']=[dict(self.client,source_ipv4='0.0.0.0')]
        with self.assertRaises(ValueError):render_k3s.validate_site(site)

if __name__=='__main__':unittest.main()
