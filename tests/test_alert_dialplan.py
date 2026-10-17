"""Alert routing and readiness regression; live SIP behavior has its own isolated fixture."""
import importlib.util
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
try:
    import provision
    import preflight
    import render_k3s
finally:
    sys.path.pop(0)


class AlertDialplanTest(unittest.TestCase):
    def setUp(self):
        self.site = {'node_ipv4':'192.168.50.10','lan_cidr':'192.168.50.0/24',
                     'ntp_ipv4':'192.168.50.1', 'gateway':{'enabled':True,'ami_client_ipv4':'192.168.50.10'}}
        self.inventory = {'phones':[{'model':'CP-7945G','verified':True,'mac':'020000000001',
            'label':'Fixture','firmware_load':'SIP45.9-4-2SR4-3S','lines':[{'extension':'201','label':'201'},
                                                      {'extension':'202','label':'202'}]}]}

    def test_exact_inventory_only_no_home_include_and_no_ring_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            provision.generate(self.site, self.inventory, directory)
            configs = {p.name:p.read_text() for p in (Path(directory)/'asterisk').glob('*.conf')}
            preflight.check_service_config(self.site, configs)
            preflight.check_ha_config(self.site, configs)
            alerts = configs['extensions.conf'].split('[cisco-alert]\n',1)[1]
            for number in ('201','202'):
                self.assertIn('Dial(PJSIP/'+number+',,iU(cisco-alert-answered))', alerts)
            self.assertNotIn('PJSIP/203', alerts)
            self.assertNotIn('exten => _', alerts)
            self.assertNotIn('TIMEOUT(', alerts)
            self.assertNotIn('Play', alerts)
            self.assertIn('GOSUB_RESULT=ABORT', alerts)
            self.assertIn('REGEX("^[0-9a-f]{32}$" ${CISCO_ALERT_ID})', alerts)
            self.assertNotIn('include => cisco-alert', configs['extensions.conf'])

    def test_preflight_rejects_missing_changed_duplicate_and_included_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            provision.generate(self.site, self.inventory, directory)
            configs = {p.name:p.read_text() for p in (Path(directory)/'asterisk').glob('*.conf')}
            original = configs['extensions.conf']
            bad_values = [original.replace(',,iU(', ',20,iU('),
                          original.split('[cisco-alert]')[0],
                          original + provision.alert_dialplan(self.site,['201','202']),
                          original + '\n[evil]\ninclude => cisco-alert\n',
                          original.replace('PJSIP/202,,iU', 'PJSIP/203,,iU')]
            for bad in bad_values:
                with self.subTest(bad=bad[-80:]), self.assertRaises(ValueError):
                    preflight.check_service_config(self.site, dict(configs, **{'extensions.conf':bad}))
            disabled = dict(self.site, gateway={'enabled':False})
            with self.assertRaises(ValueError):
                preflight.check_service_config(disabled, configs)

    def test_gateway_disable_removes_helpers_and_preserves_phone_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provision.generate(self.site,self.inventory,root)
            xml = (root/'tftp/SEP020000000001.cnf.xml').read_bytes()
            password = (root/'secrets.json').read_bytes()
            self.site['gateway']['enabled'] = False
            provision.generate(self.site,self.inventory,root)
            self.assertNotIn('[cisco-alert', (root/'asterisk/extensions.conf').read_text())
            self.assertEqual(xml, (root/'tftp/SEP020000000001.cnf.xml').read_bytes())
            self.assertEqual(password, (root/'secrets.json').read_bytes())

    def test_gateway_readiness_requires_running_signal_module_and_application(self):
        site = dict(self.site, namespace='telephony',node_name='fixture',asterisk_image='fixture@sha256:'+'a'*64)
        configs = {name + '.conf': '; fixture\n' for name in ('asterisk','extensions','logger','modules','pjsip','rtp')}
        command = list(render_k3s.render_core(site, configs)['items'][2]['spec']['template']['spec']
                       ['containers'][0]['readinessProbe']['exec']['command'])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root/'asterisk'
            binary.write_text('''#!/bin/sh
case "$2" in
 'pjsip show transports') printf '%s\\n' 'Transport: transport-udp udp 0 0 192.168.50.10:5060' 'Transport: transport-tcp tcp 0 0 192.168.50.10:5060' ;;
 'module show like app_signal') printf '%s\\n' "$TEST_SIGNAL" ;;
 'core show application WaitForSignal') printf '%s\\n' "$TEST_APP" ;;
 *) exit 98 ;;
esac
''')
            binary.chmod(0o700)
            tcp = root/'tcp'
            tcp.write_text(' 0: 0A32A8C0:13AE 00000000:0000 0A 00000000:00000000\n')
            command[2] = command[2].replace('/proc/net/tcp', shlex.quote(str(tcp)))
            env = dict(os.environ,PATH=directory+os.pathsep+os.environ['PATH'])
            for module, app, success in (
                    ('app_signal.so Signaling 0 Running extended','WaitForSignal(signalname,[signaltimeout])',True),
                    ('app_signal.so Signaling 0 Not Running extended','WaitForSignal(signalname,[signaltimeout])',False),
                    ('app_signal.so Signaling 0 Running extended','Application not found',False)):
                env.update(TEST_SIGNAL=module,TEST_APP=app)
                result = subprocess.run(command,env=env,capture_output=True,timeout=5)
                self.assertEqual(result.returncode == 0, success)
        site['gateway']['enabled'] = False
        ordinary = render_k3s.render_core(site,configs)['items'][2]['spec']['template']['spec']['containers'][0]['readinessProbe']['exec']['command'][2]
        self.assertNotIn('app_signal', ordinary)
        self.assertNotIn('WaitForSignal', ordinary)


if __name__ == '__main__':
    unittest.main()
