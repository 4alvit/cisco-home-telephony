import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'containers/asterisk'))
from phone_voice_sip import BoundaryError, Channel, Grant, focus
from phone_voice_sip_ami import AmiPort, LeaseWitness
from phone_voice_sip_dialplan import render_dialplan
from tests.test_phone_voice_control import ControlFixture, control
from tests import test_phone_voice_control as control_tests

SESSION = UUID('12345678-1234-4234-8234-123456789012')
PHONE = {'id': '201', 'lines': ['201', '202'], 'device': 'SEP020000000001'}


class Witness:
    def verify(self):
        return


class Gateway:
    def __init__(self):
        self.actions, self.keys = [], []
        self.ringing = self.up = self.hungup = False
        self.foreign = self.wrong_token = self.missing_deadline = False
        self.startup_states, self.variable_states = [], []
        self.observed_state = None
        self.deadline_value = '298'
        self.root = 'cisco-voice-' + SESSION.hex

    def own_channels(self, phone, channels):
        return [item for item in channels if item['Channel'].startswith(('PJSIP/201-', 'PJSIP/202-'))]

    def execute(self, phone, key):
        self.keys.append(key)
        self.up, self.ringing = True, False

    def AMI(self, config):
        parent = self
        class FakeAMI:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def snapshot(self):
                channels = []
                if (parent.ringing or parent.up) and not parent.hungup:
                    state = (parent.startup_states.pop(0) if parent.startup_states else
                             'Up' if parent.up else 'Ringing')
                    parent.observed_state = state
                    channels.append({'Channel': 'PJSIP/201-0001', 'Uniqueid': '99.1',
                                     'Linkedid': parent.root, 'ChannelStateDesc': state})
                if parent.foreign:
                    channels.append({'Channel': 'PJSIP/202-0002', 'Uniqueid': '99.2',
                                     'Linkedid': 'foreign', 'ChannelStateDesc': 'Ringing'})
                return {'201': 'In use' if channels and not parent.hungup else 'Not in use',
                        '202': 'Ringing' if parent.foreign else 'Not in use'}, channels
            def action(self, name, **headers):
                parent.actions.append((name, headers))
                if name == 'DBGet':
                    return [{'Event': 'DBGetResponse', 'Family': headers['Family'], 'Key': headers['Key'], 'Val': '0'}]
                if name == 'Originate':
                    parent.ringing = True
                if name == 'Hangup':
                    if headers['Channel'] == '99.1':
                        parent.hungup = True
                if name == 'Getvar':
                    var = headers['Variable']
                    parent.variable_states.append(parent.observed_state)
                    values = {'VOICE_SIP_SESSION': 'wrong' if parent.wrong_token else SESSION.hex,
                              'VOICE_SIP_END': '1299',
                              'TIMEOUT(absolute)': ('0' if parent.missing_deadline or
                                  parent.observed_state == 'Down' else parent.deadline_value)}
                    return [{'Value': '0' if var.startswith('DB(') else values[var]}]
                return [{'Response': 'Success'}]
        return FakeAMI()


class AmiTests(unittest.IsolatedAsyncioTestCase):
    def make(self):
        gateway = Gateway()
        port = AmiPort(gateway, {'ami': {}}, PHONE, '201', SESSION, 1299, 24576,
                       Witness(), clock=lambda: 100, wall_clock=lambda: 1000)
        self.addCleanup(port.stop_event.set)
        return gateway, port

    async def test_scoped_originate_native_timeout_proof_and_single_speaker_attempt(self):
        gateway, port = self.make()
        grant = await port.start()
        self.assertEqual(grant.channel.uniqueid, '99.1')
        self.assertTrue(port.deadline_armed)
        self.assertEqual(gateway.keys, ['Key:Speaker'])
        originate = [value for name, value in gateway.actions if name == 'Originate']
        self.assertEqual(len(originate), 1)
        self.assertEqual(originate[0]['Channel'], 'Local/201@cisco-voice-sip/n')
        self.assertIn('/c(slin16)', originate[0]['Data'])
        self.assertTrue(await port.validate_admission(grant))
        self.assertFalse(await port.validate_admission(grant))
        self.assertTrue(await port.hangup_and_confirm(grant))
        self.assertEqual([value['Channel'] for name, value in gateway.actions if name == 'Hangup'], ['99.1'])
        with self.assertRaises(BoundaryError):
            await port.start()

    async def test_stop_after_speaker_drains_start_and_cleans_exact_call(self):
        gateway, port = self.make()
        original = gateway.execute

        def stop_after_speaker(*args):
            original(*args)
            port.stop_event.set()

        with patch.object(gateway, 'execute', side_effect=stop_after_speaker):
            with self.assertRaisesRegex(BoundaryError, 'explicit_stop'):
                await port.start()
        self.assertTrue(port.originate_attempted)
        self.assertTrue(port.seen_phone)
        self.assertTrue(port.deadline_armed)
        self.assertTrue(port.speaker_attempted)
        self.assertFalse(port._start_active)
        self.assertIsNone(port.grant)
        self.assertEqual(gateway.keys, ['Key:Speaker'])
        self.assertTrue(await port.hangup_and_confirm())
        self.assertEqual([value['Channel'] for name, value in gateway.actions
                          if name == 'Hangup'], ['99.1'])
        self.assertTrue(port.seal_startup_stop())
        with self.assertRaises(BoundaryError):
            await port.start()

    async def test_startup_stop_seal_refuses_unseen_active_or_unfinished_lifecycle(self):
        for field, value in (('start_attempted', False), ('_start_active', True),
                             ('originate_attempted', False), ('seen_phone', False),
                             ('deadline_armed', False), ('absence_confirmed', False),
                             ('used', True), ('thread', object()), ('grant', object())):
            with self.subTest(field=field):
                gateway, port = self.make()
                port.start_attempted = port.originate_attempted = port.seen_phone = True
                port.deadline_armed = port.absence_confirmed = True
                setattr(port, field, value)
                self.assertFalse(port.seal_startup_stop())
                self.assertTrue(port._sealed)
                self.assertTrue(port.stop_event.is_set())
                with self.assertRaises(BoundaryError):
                    await port.start()
                self.assertEqual(gateway.actions, [])

    async def test_mismatched_token_or_missing_native_timeout_never_answers(self):
        for option in ('wrong_token', 'missing_deadline'):
            gateway, port = self.make()
            setattr(gateway, option, True)
            with self.assertRaises(BoundaryError):
                await port.start()
            self.assertEqual(gateway.keys, [])
            await port.hangup_and_confirm()

    async def test_allocated_channel_waits_for_predial_timeout_before_answering(self):
        gateway, port = self.make()
        gateway.startup_states = ['Down']
        grant = await port.start()
        self.assertEqual(grant.channel.uniqueid, '99.1')
        self.assertTrue(port.deadline_armed)
        self.assertNotIn('Down', gateway.variable_states)
        self.assertEqual(gateway.keys, ['Key:Speaker'])
        self.assertEqual(sum(name == 'Originate' for name, _ in gateway.actions), 1)
        self.assertTrue(await port.hangup_and_confirm(grant))

    async def test_allocated_channel_still_requires_valid_identity_and_timeout_when_ringing(self):
        for option, value, reason in (
                ('wrong_token', True, 'call_admission_mismatch'),
                ('missing_deadline', True, 'pbx_deadline_unverified'),
                ('deadline_value', '400', 'pbx_deadline_unverified')):
            with self.subTest(option=option):
                gateway, port = self.make()
                gateway.startup_states = ['Down']
                setattr(gateway, option, value)
                with patch.object(port.stop_event, 'wait', return_value=False):
                    with self.assertRaisesRegex(BoundaryError, reason):
                        await port.start()
                self.assertNotIn('Down', gateway.variable_states)
                self.assertIn('Ringing', gateway.variable_states)
                self.assertFalse(port.deadline_armed)
                self.assertEqual(gateway.keys, [])
                await port.hangup_and_confirm()

    async def test_allocated_channel_wait_is_bounded_by_setup_deadline(self):
        gateway, port = self.make()
        gateway.startup_states = ['Down'] * 20
        now = [100.0]
        port.clock = lambda: now[0]
        def advance(_seconds):
            now[0] += 1
            return False
        with patch.object(port.stop_event, 'wait', side_effect=advance):
            with self.assertRaisesRegex(BoundaryError, 'call_setup_timeout'):
                await port.start()
        self.assertEqual(now[0], 110)
        self.assertEqual(gateway.variable_states, [])
        self.assertEqual(gateway.keys, [])
        self.assertTrue(await port.hangup_and_confirm())

    async def test_stop_during_allocated_channel_wait_prevents_answer(self):
        gateway, port = self.make()
        gateway.startup_states = ['Down']
        with patch.object(port.stop_event, 'wait', side_effect=lambda _: port.stop_event.set()):
            with self.assertRaisesRegex(BoundaryError, 'explicit_stop'):
                await port.start()
        self.assertEqual(gateway.variable_states, [])
        self.assertEqual(gateway.keys, [])
        self.assertTrue(await port.hangup_and_confirm())

    async def test_other_phone_call_during_allocated_wait_still_prevents_answer(self):
        gateway, port = self.make()
        gateway.startup_states = ['Down']
        with patch.object(port.stop_event, 'wait',
                          side_effect=lambda _: setattr(gateway, 'foreign', True)):
            with self.assertRaisesRegex(BoundaryError, 'call_changed'):
                await port.start()
        self.assertEqual(gateway.variable_states, [])
        self.assertEqual(gateway.keys, [])
        self.assertTrue(await port.hangup_and_confirm())
        self.assertEqual([value['Channel'] for name, value in gateway.actions
                          if name == 'Hangup'], ['99.1'])

    async def test_other_phone_line_prevents_originate(self):
        gateway, port = self.make()
        gateway.foreign = True
        with self.assertRaises(BoundaryError):
            await port.start()
        self.assertFalse(port.originate_attempted)
        self.assertFalse(any(name == 'Originate' for name, _ in gateway.actions))

    async def test_unseen_async_originate_cannot_claim_cleanup_from_empty_snapshot(self):
        gateway, port = self.make()
        port.originate_attempted = True
        self.assertFalse(await port.hangup_and_confirm())

    async def test_hangup_preserves_unrelated_phone_channel(self):
        gateway, port = self.make()
        grant = await port.start()
        gateway.foreign = True
        self.assertTrue(await port.hangup_and_confirm(grant))
        self.assertEqual([value['Channel'] for name, value in gateway.actions if name == 'Hangup'], ['99.1'])

    async def test_sealed_unattempted_port_refuses_late_start(self):
        gateway, port = self.make()
        self.assertTrue(port.seal_no_originate())
        with self.assertRaises(BoundaryError):
            await port.start()
        with self.assertRaises(BoundaryError):
            await asyncio.to_thread(port._start)
        self.assertEqual(gateway.actions, [])
        self.assertEqual(gateway.keys, [])

    async def test_queued_start_cannot_dispatch_after_cleanup_seal(self):
        gateway, port = self.make()
        entered, release = threading.Event(), threading.Event()
        original = port._snapshot
        def snapshot(ami):
            value = original(ami)
            entered.set()
            if not release.wait(2):
                raise AssertionError('synthetic release timeout')
            return value
        port._snapshot = snapshot
        task = asyncio.create_task(port.start())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.assertFalse(port.seal_no_originate())  # worker has not drained
        finally:
            release.set()
        with self.assertRaises(BoundaryError):
            await task
        self.assertTrue(port.seal_no_originate())
        self.assertFalse(any(name == 'Originate' for name, _ in gateway.actions))

    async def test_ambiguous_originate_stays_attempted_and_excludes_proof(self):
        gateway, port = self.make()
        factory = gateway.AMI
        def ami(config):
            result = factory(config)
            original = result.action
            def action(name, **kwargs):
                if name == 'Originate':
                    self.assertTrue(port.originate_attempted)
                    raise TimeoutError('synthetic ambiguous originate')
                return original(name, **kwargs)
            result.action = action
            return result
        gateway.AMI = ami
        with self.assertRaises(TimeoutError):
            await port.start()
        self.assertTrue(port.originate_attempted)
        self.assertFalse(port.seal_no_originate())

    async def test_cancelled_start_drains_before_no_originate_proof(self):
        gateway, port = self.make()
        entered, release = threading.Event(), threading.Event()
        original = port._start
        def delayed():
            entered.set()
            if not release.wait(2):
                raise AssertionError('synthetic release timeout')
            return original()
        port._start = delayed
        task = asyncio.create_task(port.start())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertFalse(port.seal_no_originate())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(port.seal_no_originate())
        self.assertEqual(gateway.actions, [])

    def test_dialplan_arms_actual_phone_leg_before_dial_and_rejects_redirects(self):
        text = render_dialplan(['201', '202'])
        self.assertIn('Dial(PJSIP/201,8,ib(cisco-voice-sip-bound^s^1))', text)
        self.assertIn('Set(TIMEOUT(absolute)=${VOICE_SIP_REMAIN})', text)
        self.assertIn('Set(VOICE_SIP_REMAIN=$[${VOICE_SIP_END}-${EPOCH}-1])', text)
        self.assertIn('${VOICE_SIP_REMAIN} > 300', text)
        self.assertNotIn('[home]', text)
        for invalid in ('9999', '201,20', '201\nHangup()'):
            with self.assertRaises(ValueError):
                render_dialplan([invalid])

    def test_lease_requires_matching_inherited_descriptor_and_reserved_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            key = hashlib.sha256(PHONE['device'].encode()).hexdigest()
            path = Path(directory) / (key + '.lock')
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            journal = Path(directory) / (key + '.json')
            journal.write_text(json.dumps({'session_id': SESSION.hex, 'phone': '201',
                                           'reserved': True, 'state': 'running'}))
            journal.chmod(0o600)
            witness = LeaseWitness(fd, directory, PHONE, SESSION)
            try:
                with self.assertRaises(BoundaryError):
                    witness.verify()
                fcntl.flock(fd, fcntl.LOCK_EX)
                witness.verify()
                data = json.loads(journal.read_text())
                data['reserved'] = False
                journal.write_text(json.dumps(data))
                with self.assertRaises(BoundaryError):
                    witness.verify()
            finally:
                os.close(fd)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.gateway = Gateway()
        self.port = AmiPort(self.gateway, {'ami': {}}, PHONE, '201', SESSION, 1299, 24576,
                            Witness(), clock=lambda: 100, wall_clock=lambda: 1000)
        self.channel = {'Channel': 'PJSIP/201-0001', 'Uniqueid': '99.1',
                        'Linkedid': self.gateway.root, 'ChannelStateDesc': 'Up'}
        self.grant = Grant(SESSION, '201', Channel('PJSIP/201-0001', '99.1', '201'), 99, 110)

    def snapshot(self, endpoints, channels=None):
        with self.gateway.AMI({}) as ami:
            ami.snapshot = lambda: (endpoints, [self.channel] if channels is None else channels)
            self.port._snapshot(ami)
        return self.port.snapshot()

    def test_known_conflict_blocks_even_owned_only_channel_snapshot(self):
        for line in PHONE['lines']:
            for state in ('Ringing', 'Ring+Inuse', 'Busy', 'On Hold'):
                with self.subTest(line=line, state=state):
                    endpoints = {'201': 'In use', '202': 'Not in use', line: state}
                    snapshot = self.snapshot(endpoints)
                    self.assertTrue(snapshot.registered)
                    self.assertTrue(snapshot.conflicting_call_state)
                    self.assertEqual(snapshot.channels, (self.grant.channel,))
                    self.assertEqual(focus(self.grant, snapshot, 100), 'call_changed')

    def test_unknown_missing_or_unavailable_line_takes_priority_over_ringing(self):
        for line in PHONE['lines']:
            for state in ('Unknown', 'Unavailable', 'Invalid', None):
                with self.subTest(line=line, state=state):
                    endpoints = {'201': 'Ringing', '202': 'Ringing'}
                    if state is None:
                        del endpoints[line]
                    else:
                        endpoints[line] = state
                    snapshot = self.snapshot(endpoints)
                    self.assertFalse(snapshot.registered)
                    self.assertTrue(snapshot.conflicting_call_state)
                    self.assertEqual(focus(self.grant, snapshot, 100), 'disconnected')

    def test_freshness_and_deadline_take_priority_over_known_conflict(self):
        snapshot = self.snapshot({'201': 'In use', '202': 'Ringing'})
        self.assertEqual(focus(self.grant, snapshot, 101.01), 'unknown')
        self.assertEqual(focus(self.grant, snapshot, 99.5), 'unknown')
        self.assertEqual(focus(self.grant, snapshot, 110), 'lease_expired')

    def test_exact_channel_and_occupancy_requirements_remain_in_force(self):
        endpoints = {'201': 'In use', '202': 'Not in use'}
        snapshot = self.snapshot(endpoints)
        self.assertFalse(snapshot.conflicting_call_state)
        self.assertEqual(focus(self.grant, snapshot, 100), 'owned_call')
        foreign = dict(self.channel, Channel='PJSIP/202-0002', Uniqueid='99.2', Linkedid='foreign')
        for channels in ([], [dict(self.channel, Uniqueid='replacement')], [self.channel, foreign]):
            with self.subTest(channels=channels):
                self.assertEqual(focus(self.grant, self.snapshot(endpoints, channels), 100), 'call_changed')
        self.assertEqual(focus(self.grant, self.snapshot({'201': 'In use', '202': 'In use'}), 100),
                         'call_changed')


class XmlCompatibilityTests(ControlFixture):
    def test_manager_snapshot_is_accepted_by_actual_xml_child_for_default_and_explicit_transport(self):
        for number, selected in enumerate((None, 'xml_rtp')):
            with self.subTest(media_transport=selected):
                if selected is not None:
                    self.transport['media_transport'] = selected
                self.write_inputs()
                manager = self.make_control()
                self.start(manager, request_id=str(number) * 32)
                live_flags = {'--run', '--attended', '--exclusive-media', '--enable-microphone'}
                argv = [value for value in self.launches[-1][0] if value not in live_flags]
                self.assertTrue(argv[1].endswith('/voice_session.py'))
                result = subprocess.run(argv, cwd=self.workspace, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout),
                                 {'valid': True, 'network_actions': 0, 'microphone_started': False})
                self.runners[-1].finish()
                manager.tick()

    def test_xml_child_rejects_non_xml_transport_before_reading_gateway(self):
        transport, _, _ = control._libraries()
        for selected in ('sip_audiosocket', 'unknown', None):
            with self.subTest(media_transport=selected):
                value = dict(self.transport, media_transport=selected,
                             gateway_config='/must-not-be-read')
                path = self.write_private('unsupported-transport.json', value)
                with self.assertRaisesRegex(transport.VoiceTransportError, 'xml_rtp_transport_required'):
                    transport.load_pilot_config(path)


class ManagerTests(ControlFixture):
    def setUp(self):
        super().setUp()
        self.transport['media_transport'] = 'sip_audiosocket'
        self.voice['wake']['mode'] = 'onnx'
        self.write_inputs()

    def report(self, session):
        return {'transport': 'sip_audiosocket', 'session_id': session,
                'originate_attempted': True, 'pbx_deadline_armed': True,
                'pbx_call_absent': True, 'safe_no_media_started': False,
                'cleanup_errors': [], 'error': None, 'turns_completed': 2,
                'result': {'reason': 'explicit_stop', 'stop_confirmed': True, 'cleanup_errors': []}}

    def test_exact_session_sip_report_releases_without_xml_watchdog_claim(self):
        manager = self.make_control()
        state = self.start(manager)
        argv = self.launches[-1][0]
        self.assertTrue(any(value.endswith('/voice_session_sip.py') for value in argv))
        self.assertEqual(argv[argv.index('--session-id') + 1], state['session_id'])
        self.runners[-1].finish(self.report(state['session_id']))
        manager.tick()
        result = self.status(manager)
        self.assertEqual(result['state'], 'stopped')
        self.assertTrue(result['stop_confirmed'])
        with patch.object(self.runners[-1], 'read_result',
                          side_effect=AssertionError('completed child was polled again')):
            manager.tick()

    def test_mismatched_session_cannot_release(self):
        manager = self.make_control()
        self.start(manager)
        self.runners[-1].finish(self.report('0' * 32))
        manager.tick()
        self.assertEqual(self.status(manager)['state'], 'recovery_required')

    def test_inner_cleanup_failure_or_missing_proof_cannot_release(self):
        for index, errors in enumerate((['wire_close_failed'], None, 'missing')):
            self.configuration['state_directory'] = '.local/inner-cleanup-' + str(index)
            self.write_inputs()
            manager = self.make_control()
            state = self.start(manager)
            report = self.report(state['session_id'])
            if errors == 'missing':
                report['result'].pop('cleanup_errors')
            else:
                report['result']['cleanup_errors'] = errors
            self.runners[-1].finish(report)
            manager.tick()
            self.assertEqual(self.status(manager)['state'], 'recovery_required')
            self.assertFalse(manager._sessions[state['session_id']].cleanup_verified)

    def test_legacy_unattempted_boolean_cannot_release(self):
        manager = self.make_control()
        state = self.start(manager)
        report = self.report(state['session_id'])
        report.update(originate_attempted=False, safe_no_media_started=True,
                      pbx_deadline_armed=False, pbx_call_absent=False, result=None,
                      error='voice_session_failed')
        self.runners[-1].finish(report)
        manager.tick()
        result = self.status(manager)
        self.assertEqual(result['state'], 'recovery_required')
        self.assertFalse(result['stop_confirmed'])
        self.assertFalse(manager._sessions[state['session_id']].cleanup_verified)

    def test_unseen_ambiguous_originate_retains_reservation(self):
        manager = self.make_control()
        state = self.start(manager)
        report = self.report(state['session_id'])
        report.update(result=None, pbx_call_absent=False, error='voice_session_failed')
        self.runners[-1].finish(report)
        manager.tick()
        self.assertEqual(self.status(manager)['state'], 'recovery_required')

    def test_normal_sip_report_rejects_nonstandard_exit_and_incomplete_output(self):
        for index, code in enumerate((None, True, False, -9, -15, 2)):
            with self.subTest(code=code):
                self.configuration['state_directory'] = '.local/exit-' + str(index)
                self.write_inputs()
                manager = self.make_control()
                state = self.start(manager)
                child = self.runners[-1]
                child.finish(self.report(state['session_id']), code=code)
                manager.tick()
                self.assertFalse(manager._sessions[state['session_id']].cleanup_verified)
                self.assertTrue(manager._journal(manager.config.profiles[0])['reserved'])
        self.configuration['state_directory'] = '.local/normal-incomplete'
        self.write_inputs()
        manager = self.make_control()
        state = self.start(manager, request_id='different-request')
        child = self.runners[-1]
        child.finish(self.report(state['session_id']))
        manager._finish(manager._sessions[state['session_id']], child.result + b'\n')
        self.assertEqual(self.status(manager)['state'], 'recovery_required')

    def test_normal_sip_report_requires_current_owner_before_any_terminal_write(self):
        manager = self.make_control()
        state = self.start(manager)
        session = manager._sessions[state['session_id']]
        path = manager._paths(session.profile)[1]
        original = path.read_bytes()
        changed = json.loads(original)
        changed['request_id'] = 'other-request'
        path.write_text(json.dumps(changed))
        before = path.read_bytes()
        self.runners[-1].finish(self.report(state['session_id']))
        try:
            with self.assertRaises(control.ControlError):
                manager.tick()
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(session.cleanup_verified)
            self.assertGreaterEqual(session.lease_fd, 0)
        finally:
            path.write_bytes(original)


def no_sip_report(session, challenge):
    return {
        'transport': 'sip_audiosocket', 'session_id': session, 'recorded_audio': False,
        'error': 'voice_session_failed', 'result': None, 'cleanup_errors': [],
        'turns_completed': 0, 'originate_attempted': False, 'speaker_attempted': False,
        'pbx_deadline_armed': False, 'pbx_call_absent': True, 'safe_no_media_started': True,
        'input_frames': 0, 'output_frames': 0,
        'cleanup_proof': {'version': 1, 'kind': 'safe_no_sip_originate', 'transport': 'sip_audiosocket',
                          'session_id': session, 'challenge': challenge,
                          'originate_attempted': False, 'cleanup_complete': True},
    }


def startup_stop_report(session):
    report = no_sip_report(session.session_id, session.launch_challenge)
    report.update(originate_attempted=True, speaker_attempted=True,
                  pbx_deadline_armed=True, safe_no_media_started=False,
                  startup_diagnostic={'version': 1, 'reason': 'explicit_stop',
                      'error_type': 'BoundaryError', 'stage': 'sip_start',
                      'ami_stage': 'guard', 'ami_action': 'other', 'ami_error': 'other'})
    report['cleanup_proof'].update(kind='safe_sip_startup_stop', originate_attempted=True,
                                  startup_drained=True, startup_sealed=True, phone_seen=True)
    return report


class SipNoOriginateTests(control_tests.NoMediaCleanupTests):
    """Run the PR12 owner/EOF/exit/journal durability matrix for SIP as well."""
    def setUp(self):
        super().setUp()
        self.transport['media_transport'] = 'sip_audiosocket'
        self.voice['wake']['mode'] = 'onnx'
        self.write_inputs()

    def prepared(self, index=0):
        manager, session, child, _ = super().prepared(index)
        return manager, session, child, no_sip_report(session.session_id, session.launch_challenge)

    def test_malformed_or_contradictory_proofs_never_fall_back_to_stop_ack(self):
        mutations = []
        for field, values in {
            'version': (True, 2, '1'), 'kind': ('safe_no_media_started',),
            'transport': ('xml_rtp', None), 'session_id': ('0' * 32, None),
            'challenge': ('0' * 64, None, True), 'originate_attempted': (True, 0, None),
            'cleanup_complete': (False, 1, None),
        }.items():
            for value in values:
                mutations.append(lambda r, f=field, v=value: r['cleanup_proof'].__setitem__(f, v))
        for field in no_sip_report('', '')['cleanup_proof']:
            mutations.append(lambda r, f=field: r['cleanup_proof'].pop(f))
        for field, values in {
            'transport': ('xml_rtp',), 'session_id': ('0' * 32,), 'recorded_audio': (True, 0),
            'turns_completed': (1, False), 'input_frames': (1, False), 'output_frames': (1, False),
            'originate_attempted': (True, 0), 'speaker_attempted': (True, 0),
            'pbx_deadline_armed': (True, 0), 'pbx_call_absent': (False, 1),
            'safe_no_media_started': (False, 1), 'cleanup_errors': (['failed'], None),
            'result': ({'stop_confirmed': True},), 'error': (None,),
        }.items():
            for value in values:
                mutations.append(lambda r, f=field, v=value: r.__setitem__(f, v))
        mutations += [lambda r: r['cleanup_proof'].update(extra=True),
                      lambda r: r.update(watchdog_armed=True, watchdog_finished=True,
                                         watchdog_stop_errors=0, watchdog_stop_confirmed=True),
                      lambda r: r.pop('cleanup_proof')]
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                manager, session, child, report = self.prepared(index)
                mutate(report)
                child.finish(report, code=1)
                manager.tick()
                self.assertEqual(self.status(manager)['state'], 'recovery_required')
                self.assertFalse(session.cleanup_verified)
                self.assertTrue(manager._journal(session.profile)['reserved'])

    def test_incomplete_output_and_duplicate_json_keys_cannot_release(self):
        for index, kind in enumerate(('eof', 'top-duplicate', 'proof-duplicate')):
            manager, session, child, report = self.prepared(index)
            raw = json.dumps(report)
            if kind == 'top-duplicate':
                raw = raw[:-1] + ', "originate_attempted": false}'
            elif kind == 'proof-duplicate':
                raw = raw.replace('"version": 1', '"version": 1, "version": 1')
            child.returncode, child.result = 1, None if kind == 'eof' else raw.encode()
            manager.tick()
            self.clock.advance(control.REPORT_GRACE_SECONDS + 1)
            manager.tick()
            self.assertFalse(session.cleanup_verified)
            self.assertTrue(manager._journal(session.profile)['reserved'])

    def test_xml_and_sip_proofs_cannot_cross_profile_boundaries(self):
        manager, session, child, report = self.prepared()
        child.finish(control_tests.no_media_report(session.session_id, session.launch_challenge), code=1)
        manager.tick()
        self.assertFalse(session.cleanup_verified)
        self.assertTrue(manager._journal(session.profile)['reserved'])
        self.transport.pop('media_transport')
        self.voice['wake']['mode'] = 'manual'
        manager, session, child, _ = self.prepared(1)
        child.finish(no_sip_report(session.session_id, session.launch_challenge), code=1)
        manager.tick()
        self.assertFalse(session.cleanup_verified)
        self.assertTrue(manager._journal(session.profile)['reserved'])


class SipStartupStopTests(ControlFixture):
    """An acknowledged Stop during startup needs its own exact cleanup proof."""

    def setUp(self):
        super().setUp()
        self.transport['media_transport'] = 'sip_audiosocket'
        self.voice['wake']['mode'] = 'onnx'
        self.write_inputs()

    def prepared(self, index=0, *, stop=True):
        self.configuration['state_directory'] = '.local/startup-stop-' + str(index)
        self.write_inputs()
        manager = self.make_control()
        state = self.start(manager)
        session = manager._sessions[state['session_id']]
        if stop:
            self.stop(manager, session.session_id)
        return manager, session, self.runners[-1], startup_stop_report(session)

    def assert_reserved(self, manager, session):
        self.assertFalse(session.cleanup_verified)
        self.assertTrue(manager._journal(session.profile)['reserved'])

    def test_exact_parent_stop_releases_without_a_fabricated_session_result(self):
        manager, session, child, report = self.prepared()
        previous_directory = session.directory
        self.assertIsNone(report['result'])
        child.finish(report, code=1)
        manager.tick()
        state = self.status(manager)
        self.assertEqual(state['state'], 'stopped')
        self.assertIsNone(state['error'])
        self.assertTrue(state['stop_requested'])
        self.assertTrue(state['stop_confirmed'])
        self.assertEqual(state['turns_completed'], 0)
        self.assertTrue(session.cleanup_verified)
        self.assertFalse(manager._journal(session.profile)['reserved'])
        self.assertLess(session.lease_fd, 0)
        self.assertIsNone(session.directory)
        self.assertFalse(previous_directory.exists())
        self.assertEqual(session.completion_reason, 'explicit_stop')

    def test_unsolicited_proof_cannot_release_without_parent_stop(self):
        manager, session, child, report = self.prepared(stop=False)
        child.finish(report, code=1)
        manager.tick()
        self.assert_reserved(manager, session)

    def test_incomplete_parent_stop_identity_cannot_release(self):
        for index, (field, value) in enumerate((('stop_at', None),
                ('stop_requested', False), ('requested_reason', None))):
            with self.subTest(field=field):
                manager, session, child, report = self.prepared(index)
                setattr(session, field, value)
                child.finish(report, code=1)
                manager.tick()
                self.assert_reserved(manager, session)

    def test_missing_mismatched_or_contradictory_startup_stop_proofs_stay_reserved(self):
        mutations = [lambda r: r.pop('cleanup_proof'),
                     lambda r: r.pop('startup_diagnostic'),
                     lambda r: r['startup_diagnostic'].update(reason='call_setup_timeout'),
                     lambda r: r['startup_diagnostic'].update(stage='peer_accept'),
                     lambda r: r['startup_diagnostic'].update(error_type='RuntimeError')]
        for field, values in {
            'kind': ('safe_no_sip_originate',), 'session_id': ('0' * 32,),
            'challenge': ('0' * 64, None), 'version': (True,),
            'originate_attempted': (False,), 'startup_drained': (False,),
            'startup_sealed': (False,), 'phone_seen': (False,), 'cleanup_complete': (False,),
        }.items():
            for value in values:
                mutations.append(lambda r, f=field, v=value: r['cleanup_proof'].__setitem__(f, v))
        for field in ('challenge', 'startup_drained', 'startup_sealed', 'phone_seen'):
            mutations.append(lambda r, f=field: r['cleanup_proof'].pop(f))
        for field, values in {
            'cleanup_errors': (['connection_cleanup_failed'],), 'pbx_call_absent': (False,),
            'pbx_deadline_armed': (False,), 'originate_attempted': (False,),
            'safe_no_media_started': (True,), 'recorded_audio': (True,),
            'turns_completed': (1,), 'input_frames': (1, False), 'output_frames': (1,),
            'speaker_attempted': (1,), 'result': ({'stop_confirmed': True},),
        }.items():
            for value in values:
                mutations.append(lambda r, f=field, v=value: r.__setitem__(f, v))
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                manager, session, child, report = self.prepared(index)
                mutate(report)
                child.finish(report, code=1)
                manager.tick()
                self.assert_reserved(manager, session)

    def test_first_parent_stop_reason_is_preserved(self):
        for index, reason in enumerate(('lease_expired', 'call_preempted', 'shutdown')):
            with self.subTest(reason=reason):
                manager, session, child, report = self.prepared(index, stop=False)
                with manager._lock:
                    manager._request_stop(session, reason=reason)
                child.finish(report, code=1)
                manager.tick()
                self.assertEqual(session.state, 'stopped')
                self.assertTrue(session.cleanup_verified)
                self.assertEqual(session.completion_reason, reason)

    def test_killed_abnormal_live_or_incomplete_child_cannot_release(self):
        for index, (code, killed, complete) in enumerate((
                (-9, True, True), (1, True, True), (2, False, True),
                (0, False, True), (None, False, True), (1, False, False))):
            with self.subTest(code=code, killed=killed, complete=complete):
                manager, session, child, report = self.prepared(index)
                child.finish(report, code=code)
                session.killed = killed
                if not complete:
                    child.result = None
                manager.tick()
                self.clock.advance(control.REPORT_GRACE_SECONDS + 1)
                manager.tick()
                self.assert_reserved(manager, session)

    def test_changed_owner_is_preserved_instead_of_accepting_cleanup(self):
        manager, session, child, report = self.prepared()
        path = manager._paths(session.profile)[1]
        original = path.read_bytes()
        changed = json.loads(original)
        changed['session_id'] = 'f' * 32
        path.write_text(json.dumps(changed))
        before = path.read_bytes()
        child.finish(report, code=1)
        try:
            with self.assertRaises(control.ControlError):
                manager.tick()
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(session.cleanup_verified)
            self.assertGreaterEqual(session.lease_fd, 0)
        finally:
            path.write_bytes(original)


if __name__ == '__main__':
    unittest.main()
