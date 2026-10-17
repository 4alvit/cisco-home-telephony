import copy
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET

spec = importlib.util.spec_from_file_location('external_sip', Path(__file__).parents[1] / 'scripts/external_sip.py')
external = importlib.util.module_from_spec(spec)
spec.loader.exec_module(external)


class ExternalSipTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.credential = self.root / 'credentials.json'
        self.credential.write_text(json.dumps({'username': '17770000001', 'password': 'test_password_only_123'}))
        self.credential.chmod(0o600)
        self.site = {'node_ipv4': '192.168.50.10', 'lan_cidr': '192.168.50.0/24',
                     'phone_cidrs': ['192.168.60.21/32'],
                     'external_sip': {'enabled': True, 'public_ipv4': '8.8.8.8',
                                      'accounts': [{'provider': 'callcentric', 'enabled': True,
                                                    'credentials_file': str(self.credential)}]}}

    def test_disabled_never_reads_credentials_or_emits_configuration(self):
        self.site['external_sip']['enabled'] = False
        self.credential.unlink()
        self.assertEqual(external.generate(self.site), {'pjsip': '', 'dialplan': '', 'numbers': []})
        self.assertEqual(external.generate({}), external.generate(self.site))

    def test_free_only_callcentric_and_no_local_acl_mutation(self):
        original = copy.deepcopy(self.site)
        result = external.generate(self.site)
        self.assertEqual(self.site, original)
        self.assertEqual(result['numbers'], ['801'])
        self.assertIn('bind=192.168.50.10:5070', result['pjsip'])
        self.assertIn('external_media_address=8.8.8.8', result['pjsip'])
        self.assertNotIn('permit=', result['pjsip'])
        self.assertNotIn('type=identify', result['pjsip'])
        self.assertIn('contact_user=17770000001', result['pjsip'])
        self.assertIn('Dial(PJSIP/external-sip-callcentric/sip:17771234567@sip.callcentric.net,30,i)', result['dialplan'])
        self.assertNotIn('${EXTEN}', result['dialplan'])
        self.assertNotIn('include => home', result['dialplan'])
        self.assertNotIn('exten => _X', result['dialplan'])

    def test_all_three_accounts_are_namespaced_and_dns_names_retained(self):
        accounts = self.site['external_sip']['accounts']
        for provider, target in [('sip2sip', '4444'), ('linphone', 'mytestpeer')]:
            credential = self.root / (provider + '.json')
            credential.write_text(json.dumps({'username': 'myaccount', 'password': 'test_password_only_123'}))
            credential.chmod(0o600)
            accounts.append({'provider': provider, 'enabled': True, 'credentials_file': str(credential), 'target': target})
        result = external.generate(self.site)
        self.assertEqual(result['numbers'], ['801', '802', '803'])
        for domain in ('sip.callcentric.net', 'sip2sip.info', 'sip.linphone.org'):
            self.assertIn('server_uri=sip:' + domain + '\n', result['pjsip'])
            self.assertNotIn('server_uri=sip:' + domain + ':5060', result['pjsip'])
        self.assertEqual(external.object_names(self.site)['transports'], {'external-sip-udp'})

    def test_pstn_and_configuration_injection_rejected(self):
        for target in ['+12025550123', '12025550123', '011442012345678', 'sip:4444@sip2sip.info',
                       '17771234567@evil.example', '17771234567\n[evil]', '17771234567;transport=tcp']:
            with self.subTest(target=target):
                site = copy.deepcopy(self.site)
                site['external_sip']['accounts'][0]['target'] = target
                with self.assertRaises(ValueError):
                    external.generate(site)
        self.site['external_sip']['pstn_enabled'] = True
        with self.assertRaises(ValueError):
            external.generate(self.site)

    def test_password_injection_is_rejected_without_echoing_secret(self):
        for password in ['safe_enough_123;evil', 'safe_enough_123\n[evil]', 'safe_enough_123\\evil', 'short']:
            self.credential.write_text(json.dumps({'username': '17770000001', 'password': password}))
            with self.subTest(password=password), self.assertRaises(ValueError) as caught:
                external.generate(self.site)
            self.assertNotIn(password, str(caught.exception))

    def test_secret_file_permissions_symlink_and_nonregular_files_rejected(self):
        self.credential.chmod(0o644)
        with self.assertRaises(ValueError):
            external.validate(self.site)
        fifo = self.root / 'fifo'
        os.mkfifo(fifo, 0o600)
        self.site['external_sip']['accounts'][0]['credentials_file'] = str(fifo)
        with self.assertRaises(ValueError):
            external.validate(self.site)
        self.credential.chmod(0o600)
        link = self.root / 'link.json'
        link.symlink_to(self.credential)
        self.site['external_sip']['accounts'][0]['credentials_file'] = str(link)
        with self.assertRaises(ValueError):
            external.validate(self.site)

    def test_inbound_is_one_fixed_local_line_and_official_sources_only(self):
        account = self.site['external_sip']['accounts'][0]
        account['inbound'] = {'enabled': True, 'target': '201', 'source_cidrs': ['204.11.192.0/22', '199.87.144.0/21']}
        result = external.generate(self.site)
        inbound = result['dialplan'].split('[external-sip-callcentric-inbound]')[1]
        self.assertIn('exten => 17770000001,1,', inbound)
        self.assertIn('Dial(PJSIP/201,25,i)', inbound)
        self.assertNotIn('Goto(home', inbound)
        self.assertNotIn('include', inbound)
        self.assertIn('match=204.11.192.0/22', result['pjsip'])
        for invalid in [['0.0.0.0/0'], ['8.8.8.0/24'], ['192.168.0.0/16'], ['204.11.192.1/22'],
                        ['204.11.192.0/22', '204.11.192.0/24']]:
            with self.subTest(invalid=invalid):
                account['inbound']['source_cidrs'] = invalid
                with self.assertRaises(ValueError):
                    external.generate(self.site)

    def test_inbound_other_providers_remains_blocked(self):
        account = self.site['external_sip']['accounts'][0]
        account.update(provider='sip2sip', inbound={'enabled': True, 'target': '201', 'source_cidrs': ['8.8.8.8/32']})
        with self.assertRaisesRegex(ValueError, 'only.*Callcentric'):
            external.generate(self.site)

    def test_boolean_types_duplicates_and_nat_placeholders_rejected(self):
        for public in ['192.168.1.1', '127.0.0.1', '0.0.0.0', '224.0.0.1', '100.64.0.1', '203.0.113.1', '::1']:
            with self.subTest(public=public):
                site = copy.deepcopy(self.site)
                site['external_sip']['public_ipv4'] = public
                with self.assertRaises(ValueError):
                    external.generate(site)
        for value in ['true', 1, None]:
            site = copy.deepcopy(self.site)
            site['external_sip']['accounts'][0]['enabled'] = value
            with self.assertRaises(ValueError):
                external.generate(site)
        self.site['external_sip']['accounts'] *= 2
        with self.assertRaises(ValueError):
            external.generate(self.site)

    def test_preflight_rejects_stale_disabled_duplicate_and_tampered_configuration(self):
        generated = external.generate(self.site)
        configs = {'pjsip.conf': '[global]\ntype=global\n' + generated['pjsip'],
                   'extensions.conf': '[home]\n' + generated['dialplan']}
        external.check(self.site, configs)
        for bad in [configs['pjsip.conf'].replace('identify_by=ip', 'identify_by=username'),
                    configs['pjsip.conf'] + generated['pjsip'],
                    configs['pjsip.conf'] + '[external-sip-evil]\ntype=endpoint\n']:
            with self.assertRaises(ValueError):
                external.check(self.site, dict(configs, **{'pjsip.conf': bad}))
        with self.assertRaises(ValueError):
            external.check({}, configs)

    def test_cli_writes_only_private_fragments_and_redacts_failure(self):
        site_path = self.root / 'site.json'
        site_path.write_text(json.dumps(self.site))
        output = self.root / 'out'
        proc = subprocess.run([sys.executable, str(Path(__file__).parents[1] / 'scripts/external_sip.py'),
                               '--site', str(site_path), '--out', str(output)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn('test_password', proc.stdout + proc.stderr)
        for filename in output.iterdir():
            self.assertEqual(stat.S_IMODE(filename.stat().st_mode), 0o600)

    def test_provision_integration_preserves_local_acl_and_validates_inbound_inventory(self):
        scripts = str(Path(__file__).parents[1] / 'scripts')
        sys.path.insert(0, scripts)
        try:
            import provision
            import preflight
        finally:
            sys.path.remove(scripts)
        self.site['ntp_ipv4'] = '192.168.50.1'
        inventory = {'phones': [{'model': 'CP-7945G', 'verified': True, 'mac': '020000000001',
                                'extension': '201', 'label': 'Office', 'firmware_load': 'SIP45.9-4-2SR4-3S'}]}
        output = self.root / 'generated'
        provision.generate(self.site, inventory, output)
        configs = {p.name: p.read_text() for p in (output / 'asterisk').glob('*.conf')}
        preflight.check_phone_config(self.site, configs)
        preflight.check_ha_config(self.site, configs)
        self.assertIn('deny=0.0.0.0/0\npermit=192.168.60.21/32', configs['pjsip.conf'])
        dialplan = ET.parse(output / 'tftp/dialplan.xml')
        self.assertIn('801', [x.get('MATCH') for x in dialplan.findall('TEMPLATE')])
        disabled = copy.deepcopy(self.site)
        disabled['external_sip']['enabled'] = False
        with self.assertRaises(ValueError):
            preflight.check_phone_config(disabled, configs)
        self.site['external_sip']['accounts'][0]['inbound'] = {
            'enabled': True, 'target': '209', 'source_cidrs': ['204.11.192.0/22']}
        with self.assertRaises(ValueError):
            provision.generate(self.site, inventory, self.root / 'invalid')
        self.assertFalse((self.root / 'invalid').exists())
        self.site['external_sip']['accounts'][0]['inbound']['target'] = '201'
        provision.generate(self.site, inventory, output)
        configs = {p.name: p.read_text() for p in (output / 'asterisk').glob('*.conf')}
        preflight.check_phone_config(self.site, configs)
        preflight.check_ha_config(self.site, configs)


# Explicit opt-in: uses only a local image and a --network none container.
# No registration or call can reach a provider, even with operator credentials.
@unittest.skipUnless(os.environ.get('ASTERISK_TEST_IMAGE'), 'set ASTERISK_TEST_IMAGE for isolated Asterisk22 parser test')
class ExternalSipRuntimeTest(unittest.TestCase):
    setUp = ExternalSipTest.setUp
    def test_generated_config_loads_in_real_asterisk_without_network(self):
        ExternalSipTest.test_all_three_accounts_are_namespaced_and_dns_names_retained(self)
        result = external.generate(self.site)
        folder = self.root / 'runtime'
        folder.mkdir()
        (folder / 'pjsip.conf').write_text(('[global]\ntype=global\n' + result['pjsip']).replace('192.168.50.10', '127.0.0.1'))
        (folder / 'extensions.conf').write_text('[general]\nstatic=yes\n[home]\nexten => 600,1,Hangup()\n' + result['dialplan'])
        (folder / 'asterisk.conf').write_text('[directories]\nastetcdir => /etc/asterisk\nastmoddir => /usr/lib/asterisk/modules\nastvarlibdir => /var/lib/asterisk\nastdbdir => /var/lib/asterisk/db\nastrundir => /run/asterisk\nastlogdir => /var/log/asterisk\n[options]\nverbose=0\n')
        (folder / 'modules.conf').write_text('[modules]\nautoload=yes\nnoload=chan_sip.so\n')
        (folder / 'logger.conf').write_text('[logfiles]\nconsole => error\n')
        (folder / 'fixture.py').write_text((Path(__file__).with_name('external_sip_fixture.py')).read_text())
        docker = ['docker']
        if os.environ.get('ASTERISK_DOCKER_CONTEXT'):
            docker += ['--context', os.environ['ASTERISK_DOCKER_CONTEXT']]
        run = subprocess.run(docker + ['create', '--network', 'none', '--user', '0:0',
                                      os.environ['ASTERISK_TEST_IMAGE']],
                             capture_output=True, text=True, check=True)
        container = run.stdout.strip()
        try:
            subprocess.run(docker + ['cp', str(folder) + '/.', container + ':/etc/asterisk/'], capture_output=True, check=True)
            subprocess.run(docker + ['start', container], capture_output=True, check=True)
            for _ in range(30):
                ready = subprocess.run(docker + ['exec', container, 'asterisk', '-rx', 'pjsip show registrations'], capture_output=True, text=True)
                if 'Objects found: 3' in ready.stdout:
                    break
                time.sleep(0.2)
            self.assertIn('Objects found: 3', ready.stdout, 'isolated Asterisk did not finish loading')
            for number in ['801', '802', '803']:
                route = subprocess.run(docker + ['exec', container, 'asterisk', '-rx', 'dialplan show ' + number + '@home'], capture_output=True, text=True, check=True)
                self.assertIn('Dial(', route.stdout)
                self.assertIn('1 extension', route.stdout)
            for provider in external.PROVIDERS:
                endpoint = subprocess.run(docker + ['exec', container, 'asterisk', '-rx', 'pjsip show endpoint external-sip-' + provider], capture_output=True, text=True, check=True)
                self.assertIn('external-sip-' + provider, endpoint.stdout)
                self.assertNotIn('Unable to find object', endpoint.stdout)
            registrations = subprocess.run(docker + ['exec', container, 'asterisk', '-rx', 'pjsip show registrations'], capture_output=True, text=True, check=True)
            self.assertIn('Objects found: 3', registrations.stdout)
            if os.environ.get('ASTERISK_REQUIRE_UNBOUND') == '1':
                resolver = subprocess.run(docker + ['exec', container, 'asterisk', '-rx',
                    'module show like res_resolver_unbound'], capture_output=True, text=True, check=True)
                self.assertRegex(resolver.stdout, r'res_resolver_unbound\.so.*Running')
            fixture = subprocess.Popen(docker + ['exec', container, 'python3', '/etc/asterisk/fixture.py'],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(fixture.stdout.readline().strip(), 'READY')
                subprocess.run(docker + ['exec', container, 'asterisk', '-rx',
                    'channel originate PJSIP/external-sip-callcentric/sip:fixture@127.0.0.2:5090 application Wait 5'],
                    capture_output=True, check=True, timeout=10)
                stdout, stderr = fixture.communicate(timeout=12)
                self.assertEqual(fixture.returncode, 0, stderr)
                self.assertTrue(json.loads(stdout)['remote_bye_accepted'])
                self.assertTrue(json.loads(stdout)['unsolicited_request_rejected'])
            finally:
                if fixture.poll() is None:
                    fixture.kill()
                    fixture.communicate()
        finally:
            subprocess.run(docker + ['rm', '-f', container], capture_output=True, check=True)


if __name__ == '__main__':
    unittest.main()
