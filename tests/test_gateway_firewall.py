import copy
import importlib.util
from pathlib import Path
import shlex
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('gateway_firewall', Path(__file__).resolve().parents[1] / 'scripts/gateway_firewall.py')
fw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fw)


def config():
    return {'bind_ipv4': '192.168.50.10', 'api_port': 18084, 'api_clients': ['192.168.50.11'],
            'phones': [{'ipv4': '192.168.50.21', 'port': 18080}]}


class FirewallTests(unittest.TestCase):
    def fixture(self, owned=None, hooks=None):
        lines = ['*filter', ':INPUT ACCEPT [42:999]', ':FORWARD DROP [0:0]', ':OUTPUT ACCEPT [2:30]',
                 ':KUBE-SERVICES - [0:0]']
        if owned is not None:
            lines += [':' + fw.CHAIN + ' - [0:0]']
        lines += [shlex.join(rule) for rule in (hooks or [])]
        lines += ['-A INPUT -j KUBE-SERVICES', '-A KUBE-SERVICES -p tcp -m tcp --dport 443 -j ACCEPT']
        lines += [shlex.join(rule) for rule in (owned or [])]
        return '\n'.join(lines + ['COMMIT'])

    def test_rejects_duplicate_ports_addresses_and_public_clients(self):
        for mutation in ({'api_clients': ['8.8.8.8']}, {'api_port': 18080},
                         {'phones': [{'ipv4': '192.168.50.21', 'port': 18080}, {'ipv4': '192.168.50.21', 'port': 18081}]}):
            with self.assertRaises(ValueError): fw.validate({**config(), **mutation})

    def test_exact_sources_return_to_existing_policy_and_ipv6_drops(self):
        v4, v6 = fw.rules(config(), 4), fw.rules(config(), 6)
        self.assertFalse(any('ACCEPT' in rule for rule in v4 + v6))
        self.assertEqual(sum('DROP' in rule for rule in v4), 2)
        self.assertEqual(sum('DROP' in rule for rule in v6), 2)
        self.assertTrue(any('-s' in rule and '192.168.50.21/32' in rule and '18080' in rule for rule in v4))
        self.assertFalse(any('-s' in rule for rule in v6))

    def test_existing_owned_chain_preserves_foreign_fingerprint(self):
        with patch.object(fw, 'run', return_value=self.fixture()):
            before = fw.snapshot(config(), 4)
        with patch.object(fw, 'run', return_value=self.fixture(fw.rules(config(), 4), [fw.hook()])):
            after = fw.snapshot(config(), 4)
        self.assertEqual(before['foreign_sha256'], after['foreign_sha256'])
        self.assertTrue(after['matches'] and after['first'])
        self.assertEqual(fw.payload(config(), 4, after), '*filter\nCOMMIT\n')

    def test_refuses_drift_and_foreign_chain_references(self):
        drift = copy.deepcopy(fw.rules(config(), 4)); drift[0][-1] = 'ACCEPT'
        for raw in (self.fixture(drift, [fw.hook()]), self.fixture(fw.rules(config(), 4),
                       [fw.hook(), ['-A', 'FORWARD', '-j', fw.CHAIN]])):
            with patch.object(fw, 'run', return_value=raw), self.assertRaises(RuntimeError):
                fw.snapshot(config(), 4)

    def test_only_explicit_previous_configuration_allows_migration(self):
        old = config(); old['api_clients'] = ['192.168.50.12']
        raw = self.fixture(fw.rules(old, 4), [fw.hook()])
        with patch.object(fw, 'run', return_value=raw):
            with self.assertRaises(RuntimeError): fw.snapshot(config(), 4)
            before = fw.snapshot(config(), 4, previous=old)
        plan = fw.payload(config(), 4, before)
        self.assertIn('-F ' + fw.CHAIN, plan)
        self.assertNotIn('-F INPUT', plan)
        self.assertNotIn('KUBE', plan)

    def test_remove_only_own_hook_and_chain(self):
        with patch.object(fw, 'run', return_value=self.fixture(fw.rules(config(), 4), [fw.hook()])):
            state = fw.snapshot(config(), 4)
        plan = fw.payload(config(), 4, state, remove=True)
        self.assertIn('-D INPUT', plan)
        self.assertIn('-X ' + fw.CHAIN, plan)
        self.assertNotIn('-F INPUT', plan)


if __name__ == '__main__': unittest.main()
