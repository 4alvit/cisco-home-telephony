import copy
import contextlib
import importlib.util
import ipaddress
import io
import json
import os
import subprocess
from pathlib import Path
import shlex
import tempfile
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
            self.tables[family, 'mangle'] = (
                {'PREROUTING': 'ACCEPT', 'INPUT': 'ACCEPT', 'FORWARD': 'ACCEPT',
                 'OUTPUT': 'ACCEPT', 'POSTROUTING': 'ACCEPT', 'FOREIGN_MANGLE': '-'},
                [['-A', 'INPUT', '-j', 'FOREIGN_MANGLE'],
                 ['-A', 'FOREIGN_MANGLE', '-j', 'RETURN']])
            self.tables[family, 'filter'] = (
                {'INPUT': 'ACCEPT', 'OUTPUT': 'ACCEPT', 'FORWARD': 'DROP', 'FOREIGN_FILTER': '-'},
                [['-A', 'INPUT', '-j', 'FOREIGN_FILTER'],
                 ['-A', 'FOREIGN_FILTER', '-p', 'tcp', '-m', 'tcp', '--dport', '22', '-j', 'ACCEPT']])

    def seed_pre_mangle(self, cfg, opened=True):
        """Reproduce deployed filter-only rules without invoking the new apply."""
        for family in (4, 6):
            chains, rules = self.tables[family, 'raw']
            chain = cfg['raw_baseline']['chain']
            self.tables[family, 'raw'] = (chains, [r for r in rules if r[1] != chain]
                                          + fw.raw_body(cfg, family, opened))
            chains, rules = self.tables[family, 'filter']
            chains[fw.CHAIN] = '-'
            self.tables[family, 'filter'] = (chains, [fw.hook()] + rules
                                             + fw.filter_body(cfg, family, opened))

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

    def test_check_is_read_only_and_apply_orders_both_input_layers_before_raw(self):
        kernel = Kernel()
        with patch.object(fw, 'run', side_effect=kernel.run):
            before = fw.state(config())
            self.assertTrue(fw.apply(config(), 'check')['kernel_validation'])
            self.assertEqual(kernel.writes, [])
            fw.apply(config(), 'apply')
            after = fw.state(config())
            fw.same_foreign(before, after)
            self.assertEqual([(f, t) for f, t, _ in kernel.writes], [(6, 'mangle'), (4, 'mangle'), (6, 'filter'), (4, 'filter'), (4, 'raw')])
            self.assertIn('open', after['raw4']['states'])
            self.assertIn('open', after['filter4']['states'])
            self.assertTrue(after['filter4']['first'])
            for family in (4, 6):
                self.assertTrue(after[f'mangle{family}']['first'])
                self.assertIn('open', after[f'mangle{family}']['states'])
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
        self.assertEqual([(f, t) for f, t, _ in kernel.writes][-5:], [(4, 'raw'), (6, 'mangle'), (4, 'mangle'), (6, 'filter'), (4, 'filter')])

    def test_rollback_closes_sip_even_with_running_listener(self):
        kernel = Kernel(); kernel.listener = True
        with patch.object(fw, 'run', side_effect=kernel.run):
            fw.apply(config(), 'apply'); kernel.writes.clear()
            result = fw.apply(config(), 'rollback')
        self.assertEqual([(f, t) for f, t, _ in kernel.writes], [(4, 'raw'), (6, 'mangle'), (4, 'mangle'), (6, 'filter'), (4, 'filter')])
        self.assertIn('closed', result['state']['raw4']['states'])
        closed = fw.filter_body(config(), 4, False)
        self.assertEqual(packet_decision(closed, port=5070, state='ESTABLISHED', direction='REPLY'), 'DROP')
        self.assertEqual(packet_decision(closed, src='10.20.30.40'), 'RETURN')

    def test_remove_refuses_listener_and_can_remove_after_it_stops(self):
        kernel = Kernel(); kernel.listener = True
        with patch.object(fw, 'run', side_effect=kernel.run):
            fw.apply(config(), 'apply')
            with self.assertRaises(RuntimeError): fw.apply(config(), 'remove')
            for table in ('mangle', 'filter'):
                for family in (4, 6):
                    self.assertIn('closed', fw.state(config())[f'{table}{family}']['states'])
            kernel.listener = False
            after = fw.apply(config(), 'remove')['state']
        self.assertIn('closed', after['raw4']['states'])
        for table in ('mangle', 'filter'):
            for family in (4, 6):
                self.assertFalse(after[f'{table}{family}']['exists'])

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


def profile_policy():
    return {'version': 1, 'bind_ipv4': '192.168.50.10',
            'phone_ipv4': ['10.20.30.41', '172.16.2.21', '192.168.60.21']}


def profile_config():
    # Construct independently of migrate(), so a migration bug cannot also
    # silently redefine the expected policy used by every runtime test.
    return {**config(), 'version': 2, 'profile_tftp': profile_policy()}


class ProfileTftpFirewallTests(unittest.TestCase):
    def test_pre_mangle_v1_and_v2_upgrade_preserves_earlier_filter_accept(self):
        for cfg in (config(), profile_config()):
            for opened in (False, True):
                kernel = Kernel()
                kernel.seed_pre_mangle(cfg, opened)
                for family in (4, 6):
                    kernel.tables[family, 'filter'][1].insert(0, ['-A', 'INPUT', '-j', 'ACCEPT'])
                with self.subTest(version=cfg.get('version', 1), opened=opened), \
                        patch.object(fw, 'run', side_effect=kernel.run):
                    before = fw.state(cfg)
                    self.assertFalse(before['mangle4']['exists'])
                    self.assertFalse(before['mangle6']['exists'])
                    self.assertFalse(fw.apply(cfg, 'status')['profile_tftp']['effective'])
                    fw.apply(cfg, 'check')
                    self.assertEqual(kernel.writes, [])
                    fw.apply(cfg, 'apply')
                    after = fw.state(cfg)
                    fw.same_foreign(before, after)
                    for family in (4, 6):
                        self.assertTrue(after[f'mangle{family}']['first'])
                        self.assertTrue(after[f'filter{family}']['attached'])
                        self.assertFalse(after[f'filter{family}']['first'])
                        parent = [r for r in kernel.tables[family, 'filter'][1] if r[1] == 'INPUT']
                        self.assertEqual(parent[:2], [['-A', 'INPUT', '-j', 'ACCEPT'], fw.hook()])
                        actual = [r for r in kernel.tables[family, 'mangle'][1] if r[1] == fw.CHAIN]
                        self.assertEqual(actual, fw.filter_body(cfg, family, True))
                    if 'profile_tftp' in cfg:
                        self.assertTrue(fw.apply(cfg, 'status')['profile_tftp']['effective'])
                    tables = copy.deepcopy(kernel.tables)
                    fw.apply(cfg, 'apply')
                    self.assertEqual(kernel.tables, tables)

    def test_foreign_filter_prepend_does_not_change_readiness_or_get_reordered(self):
        cfg = profile_config(); kernel = Kernel()
        with patch.object(fw, 'run', side_effect=kernel.run):
            fw.apply(cfg, 'apply')
            for family in (4, 6):
                kernel.tables[family, 'filter'][1].insert(0, ['-A', 'INPUT', '-j', 'ACCEPT'])
            tables = copy.deepcopy(kernel.tables)
            self.assertTrue(fw.apply(cfg, 'status')['profile_tftp']['effective'])
            fw.apply(cfg, 'apply')
            self.assertEqual(kernel.tables, tables)
            # Exact filter protection is still required, even with early guards.
            kernel.tables[4, 'filter'][1].remove(fw.hook())
            self.assertFalse(fw.apply(cfg, 'status')['profile_tftp']['effective'])

    def test_mangle_owned_drift_and_foreign_references_refuse_before_writes(self):
        for family in (4, 6):
            for kind in ('body', 'duplicate_hook', 'foreign_reference', 'dangling'):
                cfg = profile_config(); kernel = Kernel()
                with patch.object(fw, 'run', side_effect=kernel.run):
                    fw.apply(cfg, 'apply')
                    chains, rules = kernel.tables[family, 'mangle']
                    if kind == 'body':
                        next(r for r in rules if r[1] == fw.CHAIN)[-1] = 'ACCEPT'
                    elif kind == 'duplicate_hook':
                        rules.append(fw.hook())
                    elif kind == 'foreign_reference':
                        rules.append(['-A', 'OUTPUT', '-j', fw.CHAIN])
                    else:
                        del chains[fw.CHAIN]
                    kernel.writes.clear()
                    with self.subTest(family=family, kind=kind), self.assertRaises(RuntimeError):
                        fw.apply(cfg, 'apply')
                    self.assertEqual(kernel.writes, [])

    def test_input_layer_open_failures_close_raw_and_both_layers(self):
        for failed in ((6, 'mangle'), (4, 'mangle'), (6, 'filter'), (4, 'filter'), (4, 'raw')):
            cfg = profile_config(); kernel = Kernel()
            kernel.fail = lambda event: event[:2] == failed
            with self.subTest(failed=failed), patch.object(fw, 'run', side_effect=kernel.run):
                with self.assertRaisesRegex(RuntimeError, 'closed INPUT guard retained'):
                    fw.apply(cfg, 'apply')
                after = fw.state(cfg)
                self.assertEqual([(f, t) for f, t, _ in kernel.writes][-5:],
                                 [(4, 'raw'), (6, 'mangle'), (4, 'mangle'), (6, 'filter'), (4, 'filter')])
                self.assertIn('closed', after['raw4']['states'])
                for table in ('mangle', 'filter'):
                    for family in (4, 6):
                        self.assertIn('closed', after[f'{table}{family}']['states'])
                        self.assertTrue(after[f'{table}{family}']['attached'])
                self.assertFalse(fw.apply(cfg, 'status')['profile_tftp']['effective'])

    def test_mangle_kernel_preflight_failure_has_no_mutations(self):
        cfg = profile_config(); kernel = Kernel()
        def reject_mangle(argv, data=None):
            if argv[0].endswith('-restore') and data.startswith('*mangle\n'):
                raise RuntimeError('Synthetic unsupported mangle rule')
            return kernel.run(argv, data)
        with patch.object(fw, 'run', side_effect=reject_mangle), self.assertRaises(RuntimeError):
            fw.apply(cfg, 'apply')
        self.assertEqual(kernel.writes, [])

    def test_remove_closes_raw_then_both_layers_before_deleting_guards(self):
        cfg = profile_config(); kernel = Kernel()
        with patch.object(fw, 'run', side_effect=kernel.run):
            before = fw.state(cfg)
            fw.apply(cfg, 'apply'); kernel.writes.clear()
            after = fw.apply(cfg, 'remove')['state']
            self.assertEqual([(f, t) for f, t, _ in kernel.writes],
                             [(4, 'raw'), (6, 'mangle'), (4, 'mangle'), (6, 'filter'), (4, 'filter'),
                              (4, 'filter'), (6, 'filter'), (4, 'mangle'), (6, 'mangle')])
            fw.same_foreign(before, after)
            for table in ('mangle', 'filter'):
                for family in (4, 6):
                    self.assertFalse(after[f'{table}{family}']['exists'])

    def test_exact_phone_and_destination_rules_with_default_drops(self):
        cfg = profile_config()
        self.assertEqual(fw.validate(cfg), cfg)
        raw = fw.raw_body(cfg, 4, True)
        filtered = fw.filter_body(cfg, 4, True)
        old_raw = fw.raw_body(config(), 4, True)
        inserted = []
        for port in ('69', '20000:20031'):
            expected = []
            for source in cfg['profile_tftp']['phone_ipv4']:
                expected.append(['-A', cfg['raw_baseline']['chain'], '-s', source + '/32',
                                 '-d', cfg['bind_ipv4'] + '/32', '-p', 'udp', '-m', 'udp',
                                 '--dport', port, '-m', 'comment', '--comment',
                                 cfg['raw_baseline']['owner'], '-j', 'RETURN'])
            drop = fw.raw_rule(cfg['raw_baseline']['chain'], cfg['raw_baseline']['owner'], 'udp', port)
            index = raw.index(drop)
            self.assertEqual(raw[index - len(expected):index], expected)
            inserted.extend(expected)
            filter_rules = [r for r in filtered if '--dport' in r and r[r.index('--dport') + 1] == port]
            expected_filter = [
                ['-A', fw.CHAIN, '-s', source + '/32', '-d', cfg['bind_ipv4'] + '/32',
                 '-p', 'udp', '-m', 'udp', '--dport', port, '-m', 'comment',
                 '--comment', fw.OWNER, '-j', 'RETURN']
                for source in cfg['profile_tftp']['phone_ipv4']]
            expected_filter.append(['-A', fw.CHAIN, '-p', 'udp', '-m', 'udp', '--dport', port,
                                    '-m', 'comment', '--comment', fw.OWNER, '-j', 'DROP'])
            self.assertEqual(filter_rules, expected_filter)
        self.assertEqual([r for r in raw if r not in inserted], old_raw)
        for body in (raw, filtered):
            for port in (69, 20000, 20031):
                for source in cfg['profile_tftp']['phone_ipv4']:
                    with self.subTest(port=port, source=source, layer=body is filtered):
                        self.assertEqual(packet_decision(body, src=source, port=port), 'RETURN')
                        self.assertEqual(packet_decision(body, src=source, dst='192.168.50.11', port=port), 'DROP')
                for source in ('192.168.50.13', '10.20.30.40', '192.168.60.22', '198.51.100.20'):
                    self.assertEqual(packet_decision(body, src=source, port=port), 'DROP')
            self.assertFalse(any('ACCEPT' in r or 'RELATED' in r for r in body))
        for port in (68, 70, 19999, 20032):
            self.assertEqual(packet_decision(raw, port=port), packet_decision(old_raw, port=port))

    def test_ipv6_and_external_rtp_behavior_remain_unchanged(self):
        cfg = profile_config()
        for opened in (False, True):
            self.assertEqual(fw.raw_body(cfg, 6, opened), config()['raw_baseline']['ipv6_rules'])
            for family in (4, 6):
                body = fw.filter_body(cfg, family, opened)
                without_tftp = [r for r in body if '--dport' not in r
                                or r[r.index('--dport') + 1] not in ('69', '20000:20031')]
                self.assertEqual(without_tftp, fw.filter_body(config(), family, opened))
                for port in (69, 20000, 20031):
                    if family == 6:
                        self.assertEqual(packet_decision(body, src='fd00::21', dst='fd00::10', port=port,
                                                        state='ESTABLISHED', direction='REPLY'), 'DROP')
                    elif not opened:
                        self.assertEqual(packet_decision(body, src='192.168.60.21', port=port), 'DROP')

    def test_policy_schema_host_and_count_boundaries(self):
        valid = profile_config()
        valid['profile_tftp']['phone_ipv4'] = [f'10.20.30.{last}' for last in range(100, 228)]
        self.assertEqual(fw.validate(valid), valid)
        policies = [
            {}, {**profile_policy(), 'extra': True}, {**profile_policy(), 'version': True},
            {**profile_policy(), 'version': 2}, {**profile_policy(), 'bind_ipv4': '192.168.50.11'},
            *[{**profile_policy(), 'phone_ipv4': value} for value in (
                [], '192.168.60.21', [True], ['192.168.50.10'], ['192.168.60.21/32'],
                ['192.168.60.0/24'], ['198.51.100.20'], ['fd00::21'], ['192.168.060.21'],
                ['192.168.60.21', '192.168.60.21'], list(reversed(profile_policy()['phone_ipv4'])),
                [f'10.20.30.{last}' for last in range(100, 229)]
            )]
        ]
        for policy in policies:
            cfg = profile_config(); cfg['profile_tftp'] = policy
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                fw.validate(cfg)
        for version in (True, 1, 3):
            cfg = profile_config(); cfg['version'] = version
            with self.subTest(version=version), self.assertRaises(ValueError):
                fw.validate(cfg)
        cfg = profile_config(); cfg['extra'] = True
        with self.assertRaises(ValueError): fw.validate(cfg)
        cfg = profile_config(); del cfg['version']
        with self.assertRaises(ValueError): fw.validate(cfg)

    def test_v2_requires_closed_exact_tftp_baselines_in_both_families(self):
        for family in (4, 6):
            for port in ('69', '20000:20031'):
                cfg = profile_config(); raw = cfg['raw_baseline']
                raw[f'ipv{family}_rules'].remove(fw.raw_rule(raw['chain'], raw['owner'], 'udp', port))
                # This is intentionally still a valid legacy capture schema.
                legacy = {k: copy.deepcopy(cfg[k]) for k in ('bind_ipv4', 'signaling_port', 'raw_baseline')}
                fw.validate(legacy)
                with self.subTest(family=family, missing=port), self.assertRaises(ValueError):
                    fw.validate(cfg)
            for overlap in ('68:70', '19999:20000', '20031:20032'):
                cfg = profile_config(); raw = cfg['raw_baseline']
                raw[f'ipv{family}_rules'].insert(0, fw.raw_rule(raw['chain'], raw['owner'], 'udp', overlap))
                with self.subTest(family=family, overlap=overlap), self.assertRaises(ValueError):
                    fw.validate(cfg)
        cfg = profile_config(); raw = cfg['raw_baseline']
        raw['ipv4_rules'].insert(0, fw.raw_rule(raw['chain'], raw['owner'], 'udp', '69',
                                               source='192.168.60.21/32', verdict='RETURN'))
        with self.assertRaises(ValueError): fw.validate(cfg)

    def test_migrate_is_pure_preserves_capture_and_rejects_v2_input(self):
        legacy, policy = config(), profile_policy()
        original, original_policy = copy.deepcopy(legacy), copy.deepcopy(policy)
        with patch.object(fw, 'run', side_effect=AssertionError('Migration must not access the firewall')):
            migrated = fw.migrate(legacy, policy)
            self.assertEqual(migrated, profile_config())
            self.assertEqual(legacy, original)
            self.assertEqual(policy, original_policy)
            with self.assertRaises(ValueError): fw.migrate(migrated, policy)
            migrated['raw_baseline']['ipv4_rules'].clear()
            migrated['profile_tftp']['phone_ipv4'].clear()
        self.assertEqual(legacy, original)
        self.assertEqual(policy, original_policy)

    def test_applied_v1_migrates_without_recapture_and_preserves_foreign_rules(self):
        for old_mode, name in (('apply', 'legacy_open'), ('rollback', 'legacy_closed')):
            kernel = Kernel(); cfg = profile_config()
            with self.subTest(old_mode=old_mode), patch.object(fw, 'run', side_effect=kernel.run):
                kernel.seed_pre_mangle(config(), opened=old_mode == 'apply')
                before = fw.state(cfg)
                self.assertIn(name, before['filter4']['states'])
                if old_mode == 'apply': self.assertIn('legacy_open', before['raw4']['states'])
                kernel.writes.clear()
                status = fw.apply(cfg, 'status')
                self.assertEqual(status['policy_version'], 2)
                self.assertEqual(status['profile_tftp'], {'configured': True, 'effective': False, 'phone_count': 3})
                self.assertEqual(kernel.writes, [])
                fw.apply(cfg, 'check')
                self.assertEqual(kernel.writes, [])
                fw.apply(cfg, 'apply')
                self.assertEqual([(f, t) for f, t, _ in kernel.writes], [(6, 'mangle'), (4, 'mangle'), (6, 'filter'), (4, 'filter'), (4, 'raw')])
                self.assertTrue(fw.apply(cfg, 'status')['profile_tftp']['effective'])
                fw.same_foreign(before, fw.state(cfg))
                tables = copy.deepcopy(kernel.tables)
                fw.apply(cfg, 'apply')
                self.assertEqual(kernel.tables, tables)
                with self.assertRaises(ValueError):
                    fw.capture(cfg['bind_ipv4'], cfg['signaling_port'], cfg['raw_baseline']['chain'], cfg['raw_baseline']['owner'])

    def test_effective_status_requires_all_guards_and_recovers_closed_boot_baseline(self):
        cfg = profile_config(); kernel = Kernel()
        with patch.object(fw, 'run', side_effect=kernel.run):
            status = fw.apply(config(), 'status')
            self.assertEqual(status['policy_version'], 1)
            self.assertEqual(status['profile_tftp'], {'configured': False, 'effective': False, 'phone_count': 0})
            fw.apply(cfg, 'apply')
            for family in (4, 6):
                chains, rules = kernel.tables[family, 'mangle']
                rules.remove(fw.hook()); rules.append(fw.hook())
                self.assertFalse(fw.apply(cfg, 'status')['profile_tftp']['effective'])
                fw.apply(cfg, 'apply')
                self.assertTrue(fw.apply(cfg, 'status')['profile_tftp']['effective'])
            fw.transition(cfg, 4, 'filter', opened=False)
            self.assertFalse(fw.apply(cfg, 'status')['profile_tftp']['effective'])
            fw.apply(cfg, 'apply')
            rawchain = cfg['raw_baseline']['chain']
            chains, rules = kernel.tables[4, 'raw']
            kernel.tables[4, 'raw'] = (chains, [r for r in rules if r[1] != rawchain]
                                      + copy.deepcopy(cfg['raw_baseline']['ipv4_rules']))
            self.assertFalse(fw.apply(cfg, 'status')['profile_tftp']['effective'])
            fw.apply(cfg, 'apply')
            self.assertTrue(fw.apply(cfg, 'status')['profile_tftp']['effective'])

    def test_unknown_profile_drift_refuses_without_writes(self):
        cfg = profile_config(); kernel = Kernel()
        with patch.object(fw, 'run', side_effect=kernel.run):
            fw.apply(cfg, 'apply')
            rule = next(r for r in kernel.tables[4, 'raw'][1] if '--dport' in r
                        and r[r.index('--dport') + 1] == '69' and '-s' in r)
            rule[rule.index('-s') + 1] = '192.168.60.0/24'
            kernel.writes.clear()
            with self.assertRaises(RuntimeError): fw.apply(cfg, 'apply')
            self.assertEqual(kernel.writes, [])

    def test_rollback_and_failed_open_restore_original_raw_before_closed_filters(self):
        for fail_open in (False, True):
            cfg = profile_config(); kernel = Kernel()
            with self.subTest(fail_open=fail_open), patch.object(fw, 'run', side_effect=kernel.run):
                before = fw.state(cfg)
                if fail_open:
                    kernel.fail = lambda event: event[1] == 'raw'
                    with self.assertRaisesRegex(RuntimeError, 'closed INPUT guard retained'):
                        fw.apply(cfg, 'apply')
                else:
                    fw.apply(cfg, 'apply'); kernel.writes.clear()
                    fw.apply(cfg, 'rollback')
                state = fw.state(cfg)
                self.assertEqual([(f, t) for f, t, _ in kernel.writes][-5:], [(4, 'raw'), (6, 'mangle'), (4, 'mangle'), (6, 'filter'), (4, 'filter')])
                self.assertIn('closed', state['raw4']['states'])
                self.assertIn('closed', state['filter4']['states'])
                raw = [r for r in kernel.tables[4, 'raw'][1] if r[1] == cfg['raw_baseline']['chain']]
                self.assertEqual(raw, cfg['raw_baseline']['ipv4_rules'])
                for port in (69, 20000, 20031):
                    self.assertEqual(packet_decision(fw.filter_body(cfg, 4, False), src='192.168.60.21', port=port), 'DROP')
                self.assertFalse(fw.apply(cfg, 'status')['profile_tftp']['effective'])
                fw.same_foreign(before, state)

    def test_failed_closure_is_reported_not_claimed_closed(self):
        cfg = profile_config(); kernel = Kernel()
        with patch.object(fw, 'run', side_effect=kernel.run): fw.apply(cfg, 'apply')
        def refuse_raw_write(argv, data=None):
            if argv[0].endswith('-restore') and '--test' not in argv and data.startswith('*raw\n'):
                raise RuntimeError('Synthetic raw writer failure')
            return kernel.run(argv, data)
        with patch.object(fw, 'run', side_effect=refuse_raw_write):
            with self.assertRaisesRegex(RuntimeError, 'automatic closure could not be verified'):
                fw.apply(cfg, 'rollback')

    def test_private_config_exclusive_write_and_read_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'new-policy.json'
            cfg = profile_config()
            fw.write_private(path, cfg)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(fw.read_private_json(path), cfg)
            saved = path.read_bytes()
            with self.assertRaises(OSError): fw.write_private(path, config())
            self.assertEqual(path.read_bytes(), saved)
            link = Path(directory) / 'linked-policy.json'; link.symlink_to(path)
            with self.assertRaises(OSError): fw.write_private(link, config())
            self.assertEqual(path.read_bytes(), saved)
            path.chmod(0o644)
            with self.assertRaises(ValueError): fw.read_private_json(path)

    def test_cli_migration_writes_new_policy_without_firewall_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, policy, target = root / 'old.json', root / 'phones.json', root / 'v2.json'
            fw.write_private(old, config()); fw.write_private(policy, profile_policy())
            old_bytes, policy_bytes = old.read_bytes(), policy.read_bytes()
            argv = ['external_firewall.py', 'migrate', '--config', str(old),
                    '--profile-tftp-policy', str(policy), '--output', str(target)]
            with patch('sys.argv', argv), patch.object(fw, 'run', side_effect=AssertionError('No firewall access')) as run, \
                    contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(fw.main(), 0)
            run.assert_not_called()
            self.assertEqual(fw.read_private_json(target), profile_config())
            self.assertEqual((old.read_bytes(), policy.read_bytes()), (old_bytes, policy_bytes))
            self.assertEqual(error.getvalue(), '')
            self.assertFalse(json.loads(output.getvalue())['changes_applied'])


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
        self.assertIn('PASS: version 2 kernel TFTP', result.stdout)
        self.assertIn('PASS: early mangle INPUT', result.stdout)


if __name__ == '__main__': unittest.main()
