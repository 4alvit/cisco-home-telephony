import copy
import importlib.util
import ipaddress
import os
import subprocess
from pathlib import Path
import shlex
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('external_firewall', Path(__file__).resolve().parents[1] / 'scripts/external_firewall.py')
fw = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fw)


def config():
    chain, owner = 'TEST_TELEPHONY', 'synthetic-telephony/node'
    result = {'bind_ipv4': '192.168.50.10', 'signaling_port': 5070,
              'raw_baseline': {'chain': chain, 'owner': owner}}
    for family in (4, 6):
        body = []
        for proto, port in [('tcp', '5038'), ('tcp', '5060'), ('udp', '5060'), ('udp', fw.RTP),
                            ('udp', '69'), ('udp', '20000:20031')]:
            if family == 4 and port not in ('69', '20000:20031'):
                body += [fw.raw_rule(chain, owner, proto, port, source=s, verdict='RETURN')
                         for s in ('192.168.50.0/24', '10.20.30.40/32', '192.168.60.21/32')]
            body.append(fw.raw_rule(chain, owner, proto, port))
        body.append(fw.raw_rule(chain, owner, verdict='RETURN'))
        result['raw_baseline'][f'ipv{family}_rules'] = body
    return result


class Kernel:
    """Small iptables-save/restore interpreter; no host network access."""
    def __init__(self, cfg=None):
        self.cfg = cfg or config()
        self.tables, self.writes = {}, []
        self.fail = None
        self.listener = False
        for family in (4, 6):
            raw = self.cfg['raw_baseline']
            self.tables[family, 'raw'] = (
                {'PREROUTING': 'ACCEPT', 'OUTPUT': 'ACCEPT', raw['chain']: '-', 'FOREIGN_RAW': '-'},
                [fw.raw_hook(self.cfg), ['-A', 'PREROUTING', '-j', 'FOREIGN_RAW'],
                 ['-A', 'FOREIGN_RAW', '-p', 'tcp', '-m', 'tcp', '--dport', '443', '-j', 'RETURN']]
                + copy.deepcopy(raw[f'ipv{family}_rules']))
            self.tables[family, 'filter'] = (
                {'INPUT': 'ACCEPT', 'OUTPUT': 'ACCEPT', 'FORWARD': 'DROP', 'FOREIGN_FILTER': '-'},
                [['-A', 'INPUT', '-j', 'FOREIGN_FILTER'],
                 ['-A', 'FOREIGN_FILTER', '-p', 'tcp', '-m', 'tcp', '--dport', '22', '-j', 'ACCEPT']])

    def run(self, argv, data=None):
        if argv[0] == 'ss':
            return 'UNCONN 0 0 synthetic:*' if self.listener else ''
        family = 6 if argv[0].startswith('ip6') else 4
        if argv[0].endswith('-save'):
            table = argv[-1]
            chains, rules = self.tables[family, table]
            return '\n'.join(['*' + table] + [f':{c} {p} [13:1234]' for c, p in chains.items()]
                             + [shlex.join(r) for r in rules] + ['COMMIT'])
        if not argv[0].endswith('-restore'):
            raise AssertionError('Unexpected command')
        lines = data.splitlines()
        table = lines[0][1:]
        chains, rules = copy.deepcopy(self.tables[family, table])
        event = (family, table, data)
        if '--test' not in argv and self.fail and self.fail(event):
            self.fail = None
            raise RuntimeError('Injected restore failure')
        for line in lines[1:]:
            if line == 'COMMIT':
                continue
            if line.startswith(':'):
                chains[line.split()[0][1:]] = '-'
                continue
            tokens = shlex.split(line)
            if not tokens:
                continue
            op = tokens[0]
            if op == '-A':
                rules.append(tokens)
            elif op == '-D':
                rules.remove(['-A'] + tokens[1:])
            elif op == '-I':
                if tokens[2] != '1':
                    raise AssertionError('Only first hook insertion expected')
                rules.insert(0, ['-A', tokens[1]] + tokens[3:])
            elif op == '-F':
                rules = [r for r in rules if r[1] != tokens[1]]
            elif op == '-X':
                del chains[tokens[1]]
            else:
                raise AssertionError('Unsupported operation')
        if '--test' not in argv:
            self.tables[family, table] = chains, rules
            self.writes.append(event)
        return ''


def packet_decision(body, *, src='198.51.100.20', dst='192.168.50.10', port=10000,
                    state='NEW', direction='ORIGINAL'):
    for rule in body:
        def arg(flag):
            return rule[rule.index(flag) + 1]
        if '-s' in rule and ipaddress.ip_address(src) not in ipaddress.ip_network(arg('-s')):
            continue
        if '-d' in rule and ipaddress.ip_address(dst) not in ipaddress.ip_network(arg('-d')):
            continue
        if '-p' in rule and arg('-p') != 'udp':
            continue
        if '--dport' in rule:
            values = list(map(int, arg('--dport').split(':')))
            if not values[0] <= port <= values[-1]:
                continue
        if '--ctstate' in rule and state not in arg('--ctstate').split(','):
            continue
        if '--ctdir' in rule and arg('--ctdir') != direction:
            continue
        return rule[-1]
    raise AssertionError('No decision')


class ExternalFirewallTests(unittest.TestCase):
    def test_stateful_packet_boundary_and_old_exact_sources(self):
        cfg = config()
        raw, body = fw.raw_body(cfg, 4, True), fw.filter_body(cfg, 4, True)
        self.assertEqual(packet_decision(raw), 'RETURN')
        self.assertEqual(packet_decision(fw.raw_body(cfg, 4)), 'DROP')
        for port in (5070, 10000, 10199):
            for state, direction in [('NEW', 'ORIGINAL'), ('NEW', 'REPLY'), ('ESTABLISHED', 'ORIGINAL'),
                                     ('RELATED', 'REPLY'), ('UNTRACKED', 'REPLY'), ('INVALID', 'REPLY')]:
                self.assertEqual(packet_decision(body, port=port, state=state, direction=direction), 'DROP')
            self.assertEqual(packet_decision(body, port=port, state='ESTABLISHED', direction='REPLY'), 'RETURN')
        for source in ('192.168.50.13', '10.20.30.40', '192.168.60.21'):
            self.assertEqual(packet_decision(body, src=source), 'RETURN')
            self.assertEqual(packet_decision(body, src=source, port=5070), 'DROP')
        self.assertEqual(packet_decision(body, src='192.168.60.22'), 'DROP')
        self.assertEqual(packet_decision(body, port=10200), 'RETURN')
        self.assertEqual(packet_decision(fw.raw_body(cfg, 4, True), dst='192.168.50.99'), 'DROP')
        self.assertFalse(any('ACCEPT' in r for r in body))

    def test_ipv6_is_closed_in_both_modes(self):
        for opened in (False, True):
            body = fw.filter_body(config(), 6, opened)
            for port in (5070, 10000, 10199):
                self.assertEqual(packet_decision(body, src='2001:db8::1', dst='2001:db8::2', port=port,
                                                  state='ESTABLISHED', direction='REPLY'), 'DROP')
            self.assertEqual(fw.raw_body(config(), 6, opened), config()['raw_baseline']['ipv6_rules'])

    def test_invalid_broad_sources_and_raw_injection_rejected(self):
        for mutation in ('source', 'owner', 'early_return', 'target', 'destination', 'missing_drop', 'chain'):
            cfg = config(); body = cfg['raw_baseline']['ipv4_rules']
            rtp = next(r for r in body if fw.RTP in r and '-s' in r)
            if mutation == 'source': rtp[rtp.index('-s') + 1] = '0.0.0.0/0'
            elif mutation == 'owner': rtp[rtp.index('--comment') + 1] = 'foreign'
            elif mutation == 'early_return': body.insert(0, body[-1])
            elif mutation == 'target': rtp[-1] = 'ACCEPT'
            elif mutation == 'destination': rtp[2:2] = ['-d', '192.168.50.10/32']
            elif mutation == 'missing_drop': body.remove(next(r for r in body if fw.RTP in r and '-s' not in r))
            elif mutation == 'chain': cfg['raw_baseline']['chain'] = 'INPUT'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): fw.validate(cfg)

    def test_temporary_tftp_policy_cannot_be_captured_or_restored(self):
        for port in ('69', '20000:20031'):
            cfg = config(); raw = cfg['raw_baseline']
            raw['ipv4_rules'].insert(0, fw.raw_rule(raw['chain'], raw['owner'], 'udp', port,
                source='192.168.60.21/32', verdict='RETURN'))
            with self.subTest(port=port), self.assertRaisesRegex(ValueError, 'TFTP closed'):
                fw.validate(cfg)

    def test_capture_is_private_baseline_only_and_migrated_capture_rejected(self):
        kernel = Kernel()
        with patch.object(fw, 'run', side_effect=kernel.run):
            cfg = fw.capture('192.168.50.10', 5070, 'TEST_TELEPHONY', 'synthetic-telephony/node')
            self.assertEqual(cfg, config())
            fw.apply(cfg, 'apply')
            with self.assertRaises(ValueError): fw.capture('192.168.50.10', 5070, 'TEST_TELEPHONY', 'synthetic-telephony/node')

    def test_check_is_read_only_and_apply_orders_both_filters_before_raw(self):
        kernel = Kernel()
        with patch.object(fw, 'run', side_effect=kernel.run):
            before = fw.state(config())
            self.assertTrue(fw.apply(config(), 'check')['kernel_validation'])
            self.assertEqual(kernel.writes, [])
            fw.apply(config(), 'apply')
            after = fw.state(config())
            fw.same_foreign(before, after)
            self.assertEqual([(f, t) for f, t, _ in kernel.writes], [(6, 'filter'), (4, 'filter'), (4, 'raw')])
            self.assertIn('open', after['raw4']['states'])
            self.assertIn('open', after['filter4']['states'])
            self.assertTrue(after['filter4']['first'])
            fw.apply(config(), 'apply')  # idempotent, also preserves foreign rules
            fw.same_foreign(after, fw.state(config()))

    def test_unrecognized_drift_or_foreign_reference_refuses_all_writes(self):
        for kind in ('raw', 'hook', 'filter'):
            kernel = Kernel()
            if kind == 'raw': kernel.tables[4, 'raw'][1][-1][-1] = 'ACCEPT'
            elif kind == 'hook': kernel.tables[4, 'raw'][1].append(['-A', 'OUTPUT', '-j', 'TEST_TELEPHONY'])
            else:
                kernel.tables[4, 'filter'][0][fw.CHAIN] = '-'
                kernel.tables[4, 'filter'][1].append(['-A', fw.CHAIN, '-j', 'ACCEPT'])
            with self.subTest(kind=kind), patch.object(fw, 'run', side_effect=kernel.run), self.assertRaises(RuntimeError):
                fw.apply(config(), 'apply')
            self.assertEqual(kernel.writes, [])

    def test_apply_failure_restores_raw_before_closing_filters(self):
        kernel = Kernel()
        kernel.fail = lambda event: event[1] == 'raw'
        with patch.object(fw, 'run', side_effect=kernel.run):
            with self.assertRaisesRegex(RuntimeError, 'closed INPUT guard retained'): fw.apply(config(), 'apply')
            state = fw.state(config())
        self.assertIn('closed', state['raw4']['states'])
        self.assertIn('closed', state['filter4']['states'])
        self.assertEqual([(f, t) for f, t, _ in kernel.writes][-3:], [(4, 'raw'), (6, 'filter'), (4, 'filter')])

    def test_rollback_closes_sip_even_with_running_listener(self):
        kernel = Kernel(); kernel.listener = True
        with patch.object(fw, 'run', side_effect=kernel.run):
            fw.apply(config(), 'apply'); kernel.writes.clear()
            result = fw.apply(config(), 'rollback')
        self.assertEqual([(f, t) for f, t, _ in kernel.writes], [(4, 'raw'), (6, 'filter'), (4, 'filter')])
        self.assertIn('closed', result['state']['raw4']['states'])
        closed = fw.filter_body(config(), 4, False)
        self.assertEqual(packet_decision(closed, port=5070, state='ESTABLISHED', direction='REPLY'), 'DROP')
        self.assertEqual(packet_decision(closed, src='10.20.30.40'), 'RETURN')

    def test_remove_refuses_listener_and_can_remove_after_it_stops(self):
        kernel = Kernel(); kernel.listener = True
        with patch.object(fw, 'run', side_effect=kernel.run):
            fw.apply(config(), 'apply')
            with self.assertRaises(RuntimeError): fw.apply(config(), 'remove')
            self.assertIn('closed', fw.state(config())['filter4']['states'])
            kernel.listener = False
            after = fw.apply(config(), 'remove')['state']
        self.assertIn('closed', after['raw4']['states'])
        self.assertFalse(after['filter4']['exists'] or after['filter6']['exists'])

    def test_legacy_closed_rewrite_can_be_explicitly_reapplied(self):
        kernel = Kernel()
        with patch.object(fw, 'run', side_effect=kernel.run):
            fw.apply(config(), 'apply')
            rawchain = config()['raw_baseline']['chain']
            chains, body = kernel.tables[4, 'raw']
            kernel.tables[4, 'raw'] = (chains, [r for r in body if r[1] != rawchain] + fw.raw_body(config(), 4))
            self.assertIn('closed', fw.state(config())['raw4']['states'])
            fw.apply(config(), 'apply')
            self.assertIn('open', fw.state(config())['raw4']['states'])

    def test_raw_relaxation_changes_only_one_destination_limited_rule(self):
        cfg = config()
        before, after = fw.raw_body(cfg, 4), fw.raw_body(cfg, 4, True)
        inserted = [r for r in after if r not in before]
        self.assertEqual(len(inserted), 1)
        self.assertEqual(inserted[0][inserted[0].index('-d') + 1], '192.168.50.10/32')
        self.assertEqual([r for r in after if r not in inserted], before)
        self.assertEqual(after[after.index(inserted[0]) + 1][-1], 'DROP')


class KernelRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('EXTERNAL_FIREWALL_TEST_IMAGE'), 'Set EXTERNAL_FIREWALL_TEST_IMAGE for isolated kernel checks')
    def test_disconnected_kernel_namespace(self):
        # Only source and synthetic fixtures cross stdin; no workspace/secrets
        # mount, provider connection, host network or privileged container.
        root = Path(__file__).resolve().parents[1]
        code = "import types\nfw = types.ModuleType('external_firewall')\n"
        code += 'exec(' + repr((root / 'scripts/external_firewall.py').read_text()) + ', fw.__dict__)\n'
        code += (root / 'tests/external_firewall_kernel.py').read_text()
        code += '\nexercise(fw, ' + repr(config()) + ')\n'
        cmd = ['docker']
        if os.environ.get('EXTERNAL_FIREWALL_DOCKER_CONTEXT'):
            cmd += ['--context', os.environ['EXTERNAL_FIREWALL_DOCKER_CONTEXT']]
        cmd += ['run', '--rm', '--network', 'none', '--cap-add', 'NET_ADMIN', '--user', '0',
                '--entrypoint', 'python3', '-i', os.environ['EXTERNAL_FIREWALL_TEST_IMAGE'], '-']
        result = subprocess.run(cmd, input=code, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('PASS: kernel parser', result.stdout)


if __name__ == '__main__': unittest.main()
