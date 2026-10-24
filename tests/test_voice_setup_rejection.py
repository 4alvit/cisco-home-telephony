"""Exact rejected snapshots improve private diagnostics, not call admission."""
import copy
import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_phone_voice_sip_ami as fixture
from tests.test_voice_session_sip import runner

control = runner.control


def row(root, linked=None):
    return {'Channel': 'PJSIP/201-0001', 'Uniqueid': '123.1',
            'Linkedid': root if linked is None else linked, 'ChannelStateDesc': 'Ringing'}


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.root = 'cisco-voice-' + fixture.SESSION.hex

    def test_no_raw_fields_and_exact_relation(self):
        item = row(self.root)
        item.update(CallerIDNum='secret-caller', Variables={'password': 'secret-value'},
                    audio=b'secret-audio', ChannelStateDesc='secret-state')
        value = control.project_setup_rejection(self.root, [item, item], [item, item])
        self.assertTrue(control.valid_setup_rejection(value))
        self.assertEqual(value['predicate'], 'multiple_phone_channels')
        self.assertEqual(value['rows'][0]['linkedid_root'], 'root')
        self.assertEqual(value['rows'][0]['uniqueid_root'], 'unrelated')
        self.assertEqual(value['rows'][0]['state'], 'Unknown')
        self.assertEqual(value['rows'][0]['channel_sha256'],
                         hashlib.sha256(item['Channel'].encode()).hexdigest())
        for private in ('secret-', 'CallerID', 'Variables', self.root, 'PJSIP/201', '123.1'):
            self.assertNotIn(private, json.dumps(value))

    def test_counts_and_identifiers_are_bounded(self):
        item = row(self.root)
        item.update(Uniqueid=['not-text'], Linkedid='x' * 257)
        rows = [item] * 70000
        value = control.project_setup_rejection(self.root, rows, rows)
        self.assertEqual(value['own_count'], 65535)
        self.assertEqual(len(value['rows']), 2)
        self.assertTrue(value['own_count_capped'] and value['rows_truncated'])
        self.assertEqual(value['rows'][0]['uniqueid_root'], 'invalid')
        self.assertIsNone(value['rows'][0]['linkedid_sha256'])
        self.assertLessEqual(len(json.dumps(value).encode()), 1024)

    def test_malformed_nested_types_and_fields_are_rejected(self):
        item = row(self.root, 'another-call')
        good = control.project_setup_rejection(self.root, [item], [item])
        for mutate in (lambda v: v.update(own_count=True),
                       lambda v: v.update(channel_count=0),
                       lambda v: v.update(channel_count_capped=True),
                       lambda v: v.update(rows_truncated=1),
                       lambda v: v.update(extra='private'),
                       lambda v: v.update(rows=[]),
                       lambda v: v.update(predicate='multiple_phone_channels'),
                       lambda v: v['rows'][0].update(state=[]),
                       lambda v: v['rows'][0].update(CallerID='private'),
                       lambda v: v['rows'][0].update(channel_sha256='raw-channel')):
            changed = copy.deepcopy(good)
            mutate(changed)
            self.assertFalse(control.valid_setup_rejection(changed))

    def test_version_two_requires_exact_failure_context(self):
        item = row(self.root, 'another-call')
        projection = control.project_setup_rejection(self.root, [item], [item])
        port = SimpleNamespace(startup_stage='setup_snapshot', rejected_setup=projection)
        good = runner.startup_diagnostic(runner.BoundaryError('call_changed'), 'sip_start', port)
        self.assertEqual(good['version'], 2)
        self.assertTrue(control.valid_startup_diagnostic(good))
        for change in ({'stage': 'peer_accept'}, {'ami_stage': 'complete'},
                       {'reason': 'explicit_stop'}, {'version': 1}, {'version': True},
                       {'rejected_setup': None}, {'extra': 'private'}):
            self.assertFalse(control.valid_startup_diagnostic(dict(good, **change)))
        port.rejected_setup['rows'].clear()
        self.assertTrue(control.valid_startup_diagnostic(good))
        self.assertEqual(runner.startup_diagnostic(
            runner.BoundaryError('call_changed'), 'sip_start', port)['version'], 1)


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def rejected(self, mode, broken_projection=False):
        gateway = fixture.Gateway()
        original_ami = gateway.AMI

        def ami(config):
            client = original_ami(config)
            snapshot = client.snapshot

            def changed():
                endpoints, rows = snapshot()
                if rows:
                    if mode == 'duplicate':
                        rows.append(dict(rows[0]))
                    elif mode == 'unrelated':
                        rows[0]['Linkedid'] = 'other-call'
                return endpoints, rows
            client.snapshot = changed
            return client

        gateway.AMI = ami
        port = fixture.AmiPort(gateway, {'ami': {}}, fixture.PHONE, '201', fixture.SESSION,
                               1299, 24576, fixture.Witness(), clock=lambda: 100,
                               wall_clock=lambda: 1000)
        if broken_projection:
            with patch('phone_voice_sip_ami.project_setup_rejection',
                       side_effect=RuntimeError('private detail')):
                with self.assertRaisesRegex(fixture.BoundaryError, '^call_changed$'):
                    await port.start()
        else:
            with self.assertRaisesRegex(fixture.BoundaryError, '^call_changed$'):
                await port.start()
        self.assertEqual(port.startup_stage, 'setup_snapshot')
        self.assertEqual(gateway.keys, [])
        self.assertEqual([name for name, _ in gateway.actions].count('Originate'), 1)
        self.assertFalse(any(name == 'Getvar' for name, _ in gateway.actions))
        return gateway, port

    async def test_real_rejection_captures_first_true_predicate_without_extra_io(self):
        for mode, predicate in [('duplicate', 'multiple_phone_channels'),
                                ('unrelated', 'unrelated_phone_channel')]:
            with self.subTest(mode=mode):
                gateway, port = await self.rejected(mode)
                self.assertEqual(port.rejected_setup['predicate'], predicate)
                saved = copy.deepcopy(port.rejected_setup)
                await port.hangup_and_confirm()
                self.assertEqual(port.rejected_setup, saved)

    async def test_diagnostic_failure_does_not_change_rejection(self):
        _, port = await self.rejected('duplicate', broken_projection=True)
        self.assertIsNone(port.rejected_setup)
        await port.hangup_and_confirm()
