"""Intercom ownership and automatic-answer checks use synthetic phones only."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_phone_gateway import config, gateway
import test_cisco_cli as cli_tests
from test_alert_dialplan import provision, preflight


def pair_config():
    value = config()
    second = copy.deepcopy(value['phones'][0])
    second.update(id='203', lines=['203', '204'], ipv4='192.168.50.22',
                  device='SEP001122334466', port=18081, auth_path='/auth/' + 'd' * 64)
    value['phones'].append(second)
    value['dial_allowlist'] += ['201', '202', '203', '204', '209']
    return value


class IntercomAMI:
    def __init__(self):
        self.root, self.clock, self.pressed = None, 0, set()
        self.calls, self.ended = [], set()
        self.intruder, self.split_bridge, self.fail_originate = False, False, False
        self.link_suffix = ''
        self.optimized = False

    def __enter__(self): return self
    def __exit__(self, *_): pass

    def action(self, name, **fields):
        self.calls.append((name, fields))
        if name == 'Originate':
            self.root = fields['ChannelId']
            if self.fail_originate:
                raise OSError('synthetic ambiguous result')
        if name == 'Hangup':
            self.ended.add(fields['Channel'])
        return [{'Response': 'Success'}]

    def snapshot(self):
        self.clock += .5
        endpoints = dict.fromkeys(('201', '202', '203', '204'), 'Not in use')
        channels = []
        if self.root:
            if not self.optimized:
                for suffix, half in (('', '1'), ('-b', '2')):
                    name = 'Local/202@cisco-intercom-000000ab;' + half
                    if name not in self.ended:
                        channels.append({'Channel': name, 'Uniqueid': self.root + suffix,
                                         'Linkedid': self.root, 'ChannelStateDesc': 'Up'})
            for phone, line in (('201', '202'), ('203', '204')):
                if phone == '203' and '201' not in self.pressed and not self.intruder:
                    continue
                name = 'PJSIP/' + line + '-000000ab'
                if name in self.ended:
                    continue
                unrelated = phone == '203' and self.intruder
                channels.append({'Channel': name,
                                 'Uniqueid': ('unrelated.1' if unrelated else 'test.' + phone),
                                 'Linkedid': 'unrelated.1' if unrelated else self.root + self.link_suffix,
                                 'CallerIDNum': '202',  # Spoofed CID must never grant ownership.
                                 'ChannelStateDesc': 'Up' if phone in self.pressed else 'Ringing',
                                 'BridgeId': phone if self.split_bridge else 'test-bridge'})
        return endpoints, channels

    def execute(self, phone, uri):
        if uri != 'Key:Speaker':
            raise AssertionError('Intercom must request speaker answer, not a line toggle')
        self.pressed.add(phone['id'])
        self.optimized = self.pressed == {'201', '203'}


class IntercomTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = temp.name
        self.server = gateway.Gateway(pair_config(), temp.name)
        self.addCleanup(self.server.db.close)
        self.ami = IntercomAMI()
        for target, replacement in (('AMI', lambda _: self.ami), ('execute', self.ami.execute)):
            patcher = patch.object(gateway, target, replacement)
            patcher.start(); self.addCleanup(patcher.stop)
        for target, replacement in (('monotonic', lambda: self.ami.clock), ('sleep', lambda _: None)):
            patcher = patch.object(gateway.time, target, replacement)
            patcher.start(); self.addCleanup(patcher.stop)

    def call(self, **extra):
        return self.server.mutate('201', 'intercom',
            {'line': '202', 'number': '204', 'idempotency_key': 'intercom-test-123', **extra})

    def test_two_exact_secondary_lines_bridge_and_preferences_do_not_change(self):
        before = [self.server.preference(p) for p in ('201', '203')]
        result = self.call()
        self.assertEqual(result['connection_state'], 'connected')
        self.assertTrue(result['speaker_requested'])
        self.assertEqual(self.ami.pressed, {'201', '203'})
        action, fields = self.ami.calls[0]
        self.assertEqual((action, fields['Channel'], fields['Data']),
                         ('Originate', 'Local/202@cisco-intercom', 'PJSIP/204,8,i'))
        self.assertEqual(fields['OtherChannelId'], fields['ChannelId'] + '-b')
        self.assertEqual(fields['CallerID'], '202 <202>')
        self.assertEqual(before, [self.server.preference(p) for p in ('201', '203')])
        self.assertEqual(result, self.call())
        self.assertEqual(sum(action == 'Originate' for action, _ in self.ami.calls), 1)

    def test_optimized_physical_legs_linked_to_other_local_id_are_owned(self):
        self.ami.link_suffix = '-b'
        result = self.call()
        self.assertEqual(result['connection_state'], 'connected')
        self.assertTrue(self.ami.optimized)
        _, channels = self.ami.snapshot()
        self.assertEqual(len(channels), 2)
        self.assertTrue(all(self.server.is_intercom_channel(ch) for ch in channels))
        self.assertEqual(len(self.server.intercom_channels(self.ami.root, channels)), 2)

    def test_cleanup_includes_both_local_roots_and_both_linked_physical_legs(self):
        root = 'cisco-intercom-' + 'b' * 32
        self.ami.root, self.ami.intruder = root, False
        self.ami.pressed.add('201')
        self.ami.link_suffix = '-b'
        _, channels = self.ami.snapshot()
        self.assertEqual(len(channels), 4)
        self.server.stop_intercom(self.ami, root)
        self.assertEqual(self.ami.ended, {ch['Channel'] for ch in channels})

    def test_same_handset_softphone_service_and_unlisted_destinations_refused(self):
        for destination in ('201', '202', '209', '600', '911', 'sip:204@example.invalid'):
            with self.subTest(destination=destination), self.assertRaises(gateway.Refused):
                self.call(number=destination)
        self.assertEqual(self.ami.calls, [])

    def test_dnd_on_either_phone_blocks_before_originate(self):
        for phone in ('201', '203'):
            self.server.db.execute('UPDATE preferences SET dnd=(phone=?)', (phone,))
            with self.assertRaisesRegex(gateway.Refused, 'do_not_disturb'):
                self.call()
        self.assertEqual(self.ami.calls, [])

    def test_busy_sibling_and_unavailable_destination_block_before_originate(self):
        original = self.ami.snapshot
        def busy():
            endpoints, channels = original()
            channels.append({'Channel': 'PJSIP/203-ff', 'Uniqueid': 'other', 'ChannelStateDesc': 'Up'})
            return endpoints, channels
        with patch.object(self.ami, 'snapshot', busy), self.assertRaisesRegex(gateway.Refused, 'phone_busy'):
            self.call()
        def offline():
            endpoints, channels = original(); endpoints['204'] = 'Unavailable'
            return endpoints, channels
        with patch.object(self.ami, 'snapshot', offline), self.assertRaisesRegex(gateway.Refused, 'phone_unavailable'):
            self.call()
        self.assertEqual(self.ami.calls, [])

    def test_concurrent_unrelated_call_with_same_caller_id_is_never_answered_or_stopped(self):
        self.ami.intruder = True
        with self.assertRaisesRegex(gateway.Refused, 'intercom_not_connected'):
            self.call()
        self.assertNotIn('203', self.ami.pressed)
        self.assertNotIn('PJSIP/204-000000ab', self.ami.ended)
        self.assertIn('PJSIP/202-000000ab', self.ami.ended)

    def test_up_channels_without_common_bridge_are_not_success_and_get_cleaned_up(self):
        self.ami.split_bridge = True
        with self.assertRaisesRegex(gateway.Refused, 'intercom_not_connected'):
            self.call()
        self.assertEqual(self.ami.ended, {'PJSIP/202-000000ab', 'PJSIP/204-000000ab'})
        with self.assertRaisesRegex(gateway.Refused, 'previous_action_outcome_unknown'):
            self.call()

    def test_restart_never_resumes_autoanswer_and_retains_late_originate_cleanup(self):
        root = 'cisco-intercom-' + 'a' * 32
        self.server.db.execute('INSERT INTO intercoms VALUES (?, ?, ?)', (root, 'starting', gateway.time.time()))
        self.server.db.commit()
        restarted = gateway.Gateway(pair_config(), self.path)
        self.addCleanup(restarted.db.close)
        self.ami.root = root
        self.ami.link_suffix = '-b'
        channels = self.ami.snapshot()[1]
        self.assertTrue(all(restarted.is_intercom_channel(ch) for ch in channels))
        restarted.reconcile_intercoms(self.ami, channels)
        self.assertEqual(self.ami.pressed, set())
        self.assertIn('PJSIP/202-000000ab', self.ami.ended)
        self.assertTrue(any(name.endswith(';2') for name in self.ami.ended))

    def test_ambiguous_originate_is_not_retried(self):
        self.ami.fail_originate = True
        with self.assertRaisesRegex(gateway.Refused, 'intercom_not_connected'):
            self.call()
        with self.assertRaisesRegex(gateway.Refused, 'previous_action_outcome_unknown'):
            self.call()
        self.assertEqual(sum(name == 'Originate' for name, _ in self.ami.calls), 1)


class IntercomDialplanTests(unittest.TestCase):
    def setUp(self):
        self.site = {'node_ipv4': '192.168.50.10', 'lan_cidr': '192.168.50.0/24',
                     'ntp_ipv4': '192.168.50.1',
                     'gateway': {'enabled': True, 'ami_client_ipv4': '192.168.50.10'},
                     'softphones': [{'extension': '209', 'label': 'Fixture',
                                     'source_ipv4': '192.168.50.90', 'transport': 'tcp'}]}
        self.inventory = {'phones': [{'model': 'CP-7945G', 'verified': True, 'mac': '020000000001',
                           'label': 'Fixture', 'firmware_load': 'SIP45.9-4-2SR4-3S',
                           'lines': [{'extension': '201', 'label': '201'},
                                     {'extension': '202', 'label': '202'}]}]}

    def test_source_helper_ignores_forwarding_and_excludes_softphones_services_and_patterns(self):
        with tempfile.TemporaryDirectory() as directory:
            provision.generate(self.site, self.inventory, directory)
            configs = {p.name: p.read_text() for p in (Path(directory) / 'asterisk').glob('*.conf')}
        preflight.check_service_config(self.site, configs)
        preflight.check_ha_config(self.site, configs)
        helper = provision.intercom_dialplan(self.site, ['201', '202'])
        self.assertIn(helper, configs['extensions.conf'])
        for line in ('201', '202'):
            self.assertIn(f'exten => {line},1,Dial(PJSIP/{line},8,i)', helper)
        for value in ('209', '600', 'include', 'exten => _'):
            self.assertNotIn(value, helper)
        self.assertEqual(provision.intercom_dialplan(self.site, []), '')
        self.assertEqual(provision.intercom_dialplan(dict(self.site, gateway={'enabled': False}), ['201']), '')

    def test_preflight_rejects_unsafe_stale_or_public_source_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            provision.generate(self.site, self.inventory, directory)
            configs = {p.name: p.read_text() for p in (Path(directory) / 'asterisk').glob('*.conf')}
        original = configs['extensions.conf']
        helper = provision.intercom_dialplan(self.site, ['201', '202'])
        for changed in (original.replace(helper, ''), original + helper,
                        original.replace('PJSIP/201,8,i', 'PJSIP/201,8'),
                        original.replace(helper, helper + 'exten => 209,1,Dial(PJSIP/209,8,i)\n'),
                        original + '\n[other]\ninclude => cisco-intercom\n'):
            with self.subTest(changed=changed[-60:]), self.assertRaises(ValueError):
                preflight.check_service_config(self.site, dict(configs, **{'extensions.conf': changed}))
        with self.assertRaises(ValueError):
            preflight.check_service_config(dict(self.site, gateway={'enabled': False}), configs)


class IntercomCLI(unittest.TestCase):
    invoke = cli_tests.CLITests.invoke
    def test_call_alias_uses_existing_dial_action(self):
        code, result, calls = self.invoke(['213', 'call', '600'])
        self.assertEqual(code, 0)
        self.assertEqual(calls[-1][0], '/v1/phones/212/dial')

    def pair(self):
        data = cli_tests.inventory()
        second = copy.deepcopy(data['phones'][0]); second.update(id='218', lines=['218', '220'])
        data['phones'].append(second); data['dial_allowlist'] += ['212', '213', '218', '220']
        return data

    def test_intercom_alias_and_nonconsecutive_mapping_dry_run(self):
        for command in ('intercom', 'drop-in'):
            code, result, calls = self.invoke(['213', command, '220', '--dry-run'], self.pair())
            self.assertEqual(code, 0)
            self.assertEqual(result['action'], 'intercom')
            self.assertEqual(result['request']['line'], '213')
            self.assertEqual(result['request']['number'], '220')
            self.assertEqual(len(calls), 1)

    def test_intercom_rejects_same_phone_and_non_cisco_targets(self):
        for destination in ('212', '213', '209', '600'):
            self.assertEqual(self.invoke(['213', 'intercom', destination], self.pair())[0], 1)

    def test_intercom_dry_run_rejects_dnd_before_post(self):
        data = self.pair(); data['phones'][1]['dnd'] = True
        code, result, calls = self.invoke(['213', 'intercom', '220', '--dry-run'], data)
        self.assertEqual((code, result['reason'], len(calls)), (1, 'do_not_disturb', 1))
