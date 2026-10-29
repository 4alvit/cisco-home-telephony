import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'containers/asterisk'))
import phone_announce as m
import phone_announce_agi as agi


class FakeAMI:
    def __init__(self):
        self.endpoints = {'210': 'Not in use', '211': 'Not in use', '222': 'Not in use'}
        self.channels = []
        self.actions = []
        self.dnd = set()
        self.fail_originate = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def snapshot(self):
        return self.endpoints, self.channels

    def action(self, action, **values):
        self.actions.append((action, values))
        if action == 'DBGet':
            return [{'Event': 'DBGetResponse', 'Family': values['Family'], 'Key': values['Key'],
                     'Val': '1' if values['Key'] in self.dnd else '0'}]
        if action == 'Originate' and self.fail_originate:
            raise TimeoutError()
        return [{'Response': 'Success'}]


class AnnouncementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {'bind': '127.0.0.1', 'port': 18085, 'token': 'x' * 32,
                       'ami': {'host': '127.0.0.1', 'port': 5038, 'username': 'test', 'secret': 'fixture'},
                       'lines': {'210': '210', '211': '210', '222': '222'},
                       'dial_audio_config': self.temp.name + '/dial-audio.json', 'voice_service': '555',
                       'spool': self.temp.name + '/spool', 'agi': '/opt/cisco-announcements/phone_announce_agi.py'}
        self.now = 1800000000.0
        self.manager = m.Announcer(self.config, clock=lambda: self.now, recover=False)
        self.worker = patch.object(self.manager, 'start_worker').start()
        self.addCleanup(patch.stopall)
        self.request = {'idempotency_key': 'fixture-request', 'lines': ['222'], 'text': 'Dinner is ready'}

    def job(self, lines=None):
        request = dict(self.request)
        if lines:
            request['lines'] = lines
        result = self.manager.submit(request)
        job = self.manager.jobs[result['id']]
        job.update(status='running', duration_seconds=5.0)
        return job, job.data['recipients'][0]

    def channel(self, recipient, **extra):
        return {'Channel': 'PJSIP/' + recipient['line'] + '-00000001', 'Uniqueid': 'pbx.123',
                'Linkedid': recipient['root'], 'ChannelStateDesc': 'Ringing', **extra}

    def test_duplicate_physical_lines_and_unknown_targets_rejected(self):
        for lines in (['210', '211'], ['222', '222'], ['223']):
            with self.subTest(lines=lines), self.assertRaises(m.AnnounceError):
                self.manager.submit({**self.request, 'lines': lines})

    def test_validation_limits_and_private_configuration(self):
        for text in ('', ' ' * 3, 'x' * 1001, 'bad\x00text', '\ud800'):
            with self.subTest(text=repr(text)), self.assertRaises(m.AnnounceError):
                m.request_data({**self.request, 'text': text}, self.config)
        for updates in ({'bind': '0.0.0.0'}, {'token': 'short'}, {'spool': '/tmp/../etc'},
                        {'lines': {'210': 'Kitchen'}}):
            with self.subTest(updates=updates), self.assertRaises(m.AnnounceError):
                m.validate_config({**self.config, **updates})

    def test_dry_run_has_no_jobs_workers_or_audio(self):
        response = self.manager.submit({**self.request, 'dry_run': True})
        self.assertEqual(response['status'], 'dry_run')
        self.assertFalse(self.manager.jobs)
        self.assertFalse(list(Path(self.config['spool']).iterdir()))
        self.worker.assert_not_called()

    def test_idempotency_and_no_transcript_in_public_or_journal(self):
        first = self.manager.submit(self.request)
        second = self.manager.submit(self.request)
        self.assertEqual(first, second)
        self.worker.assert_called_once()
        with self.assertRaises(m.AnnounceError) as caught:
            self.manager.submit({**self.request, 'text': 'Different message'})
        self.assertEqual(caught.exception.reason, 'idempotency_conflict')
        journal = (Path(self.config['spool']) / first['id'] / 'job.json').read_text()
        for value in (journal, json.dumps(first)):
            self.assertNotIn(self.request['text'], value)
            self.assertNotIn(self.config['token'], value)

    def test_dispatch_skips_offline_busy_sibling_and_dnd(self):
        scenarios = ('offline', 'busy', 'sibling', 'dnd')
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                job, recipient = self.job(['210'])
                recipient.update(status='queued', reason=None)
                ami = FakeAMI()
                if scenario == 'offline':
                    ami.endpoints['210'] = 'Unavailable'
                elif scenario == 'busy':
                    ami.channels = [{'Channel': 'PJSIP/210-1234'}]
                elif scenario == 'sibling':
                    ami.endpoints['211'] = 'In use'
                else:
                    ami.dnd.add('211')
                self.manager.dispatch(job, recipient, ami)
                self.assertEqual(recipient['status'], 'skipped')
                self.assertFalse(any(action == 'Originate' for action, _ in ami.actions))

    def test_originate_is_scoped_finite_and_has_numeric_caller(self):
        job, recipient = self.job()
        ami = FakeAMI()
        self.manager.dispatch(job, recipient, ami)
        action = [values for name, values in ami.actions if name == 'Originate'][0]
        self.assertEqual(action['Channel'], 'Local/222@cisco-announcement/n')
        self.assertEqual(action['ChannelId'], recipient['root'])
        self.assertEqual(action['OtherChannelId'], recipient['root'] + '-b')
        self.assertRegex(recipient['caller_id'], r'^8[0-9]{8}$')
        self.assertEqual(action['CallerID'], 'Announcement <' + recipient['caller_id'] + '>')
        self.assertEqual(action['Timeout'], '30000')
        self.assertIn('__ANNOUNCE_END=1800000180', action['Variable'])
        self.assertEqual(recipient['dispatched_at'], self.now)

    def test_unknown_originate_is_never_replayed(self):
        job, recipient = self.job()
        ami = FakeAMI()
        ami.fail_originate = True
        self.manager.dispatch(job, recipient, ami)
        self.assertEqual(recipient['status'], 'stopping')
        self.assertTrue(recipient['cancel'])
        self.manager.observe(job, recipient, ami, [])
        self.assertEqual(recipient['status'], 'stopping')
        self.now += 181
        self.manager.observe(job, recipient, ami, [])
        self.assertEqual(recipient['status'], 'stopped')
        self.assertEqual(sum(a == 'Originate' for a, _ in ami.actions), 1)

    def test_owned_channel_status_and_exact_stop(self):
        job, recipient = self.job()
        recipient['dispatched_at'] = self.now
        channel = self.channel(recipient)
        unrelated = {**channel, 'Uniqueid': 'other.4', 'Linkedid': 'other'}
        ami = FakeAMI()
        self.manager.observe(job, recipient, ami, [channel, unrelated])
        self.assertEqual(recipient['status'], 'ringing')
        self.assertEqual(recipient['uniqueid'], 'pbx.123')
        self.manager.stop(job.data['id'])
        self.manager.observe(job, recipient, ami, [channel, unrelated])
        self.manager.observe(job, recipient, ami, [channel, unrelated])
        hangs = [v for a, v in ami.actions if a == 'Hangup']
        self.assertEqual(hangs, [{'Channel': 'pbx.123', 'Cause': '16'}])
        self.manager.observe(job, recipient, ami, [unrelated])
        self.assertEqual(recipient['status'], 'stopped')

    def test_short_call_receipt_completes_without_poll_observing_call(self):
        job, recipient = self.job()
        recipient['dispatched_at'] = self.now
        agi.receipt(job.directory, job.data['id'], '222', recipient['root'], 'completed')
        self.manager.observe(job, recipient, FakeAMI(), [])
        self.assertEqual(recipient['status'], 'completed')
        self.assertIsNone(recipient['reason'])

    def test_wrong_receipt_cannot_complete(self):
        job, recipient = self.job()
        agi.receipt(job.directory, job.data['id'], '222', 'unrelated.2', 'completed')
        with self.assertRaises(m.AnnounceError):
            self.manager.observe(job, recipient, FakeAMI(), [])

    def test_restart_only_stops_never_originate(self):
        job, _ = self.job()
        with patch.object(m.Announcer, 'start_worker') as worker:
            recovered = m.Announcer(self.config, clock=lambda: self.now)
        self.assertTrue(recovered.jobs[job.data['id']].stop.is_set())
        self.assertEqual(worker.call_args.args[1], None)

    def test_dead_worker_reconciles_cleanup_without_replaying_speech(self):
        job, _ = self.job()
        job.thread = Mock()
        job.thread.is_alive.return_value = False
        self.worker.reset_mock()
        self.manager.reconcile()
        self.worker.assert_called_once_with(job, None)
        self.assertTrue(job.stop.is_set())
        self.assertEqual(job.data['status'], 'stopping')
        self.worker.reset_mock()
        job.thread.is_alive.return_value = True
        self.manager.reconcile()
        self.worker.assert_not_called()

    def test_get_rechecks_ringing_identity_without_call_mutations(self):
        job, recipient = self.job()
        recipient.update(status='ringing', channel='PJSIP/222-old', uniqueid='old.1')
        ami = FakeAMI()
        self.manager.ami_factory = lambda _config: ami
        response = self.manager.get(job.data['id'])
        self.assertEqual(response['recipients'][0]['status'], 'queued')
        self.assertIsNone(response['recipients'][0]['channel'])
        ami.channels = [self.channel(recipient)]
        response = self.manager.get(job.data['id'])
        self.assertEqual(response['recipients'][0]['uniqueid'], 'pbx.123')
        self.assertFalse(ami.actions)

    def test_worker_synthesizes_once_and_removes_audio_after_short_call(self):
        job, recipient = self.job()
        ami = FakeAMI()
        self.manager.ami_factory = lambda _config: ami
        def synth(_config, _text, directory):
            (directory / 'speech.sln16').write_bytes(b'fixture')
            return 1.0
        self.manager.synth = Mock(side_effect=synth)
        original_snapshot = ami.snapshot
        def snapshot():
            if any(a == 'Originate' for a, _ in ami.actions):
                agi.receipt(job.directory, job.data['id'], '222', recipient['root'], 'completed')
            return original_snapshot()
        ami.snapshot = snapshot
        self.manager.run(job, self.request['text'])
        self.manager.synth.assert_called_once()
        self.assertEqual(job.data['status'], 'finished')
        self.assertEqual(recipient['status'], 'completed')
        self.assertFalse((job.directory / 'speech.sln16').exists())

    def test_stop_before_dispatch_never_synthesizes_or_calls(self):
        job, recipient = self.job()
        self.manager.ami_factory = lambda _config: FakeAMI()
        self.manager.synth = Mock()
        self.manager.stop(job.data['id'])
        self.manager.run(job, self.request['text'])
        self.manager.synth.assert_not_called()
        self.assertEqual(recipient['status'], 'stopped')
        self.assertEqual(job.data['status'], 'stopped')

    def test_speech_uses_existing_voice_and_prefixes_silence(self):
        job, _ = self.job()
        settings = {'services': {'555': {'kind': 'clock', 'voice': 'configured-voice'}},
                    'credentials_file': '/private/credentials'}
        ha = Mock()
        ha.speech.return_value = b'audio-fixture'
        def encode(args, **kwargs):
            self.assertEqual(args[args.index('-ar') + 1], '16000')
            self.assertEqual(args[args.index('-protocol_whitelist') + 1], 'file')
            Path(args[-1]).write_bytes(b'\x01\x02' * 1600)
        Path(self.config['dial_audio_config']).write_text('{}')
        with patch.object(m.dial_audio, 'validate', return_value=settings), patch.object(
                m.dial_audio, 'credentials', return_value=('origin', 'private')), patch.object(
                m.dial_audio, 'HomeAssistant', return_value=ha), patch.object(m.subprocess, 'run', side_effect=encode):
            duration = m.synthesize(self.config, self.request['text'], job.directory)
        self.assertEqual(duration, 0.5)
        self.assertEqual((job.directory / 'speech.sln16').read_bytes()[:12800], b'\0' * 12800)
        self.assertFalse((job.directory / 'source.audio').exists())
        self.assertFalse((job.directory / 'decoded.pcm').exists())
        ha.speech.assert_called_once_with(settings['services']['555'], self.request['text'])

    def test_agi_only_plays_owned_channel_and_records_completion(self):
        job, recipient = self.job()
        audio = job.directory / 'speech.sln16'
        audio.write_bytes(b'\0' * 16000)
        audio.chmod(0o600)
        source = io.StringIO('agi_uniqueid: ' + recipient['root'] + '\nagi_channel: '
                             'Local/222@cisco-announcement-000001;1\n\n200 result=0 endpos=8000\n')
        output = io.StringIO()
        code = agi.play([job.data['id'], '222', self.config['spool']], source, output, clock=lambda: self.now)
        self.assertEqual(code, 0)
        self.assertIn('STREAM FILE ', output.getvalue())
        self.assertEqual(self.manager.receipt(job, recipient), 'completed')
        bad = io.StringIO('agi_uniqueid: unrelated.1\nagi_channel: Local/222@cisco-announcement-000001;1\n\n')
        with self.assertRaises(ValueError):
            agi.play([job.data['id'], '222', self.config['spool']], bad, io.StringIO(), clock=lambda: self.now)

    def test_agi_hangup_is_failure_not_success(self):
        job, recipient = self.job()
        audio = job.directory / 'speech.sln16'
        audio.write_bytes(b'\0' * 16000)
        audio.chmod(0o600)
        source = io.StringIO('agi_uniqueid: ' + recipient['root'] + '\nagi_channel: '
                             'Local/222@cisco-announcement-000001;1\n\n200 result=-1\n')
        self.assertEqual(agi.play([job.data['id'], '222', self.config['spool']], source,
                                 io.StringIO(), clock=lambda: self.now), 1)
        self.assertEqual(self.manager.receipt(job, recipient), 'failed')

    def test_dialplan_has_bounded_legs_and_ignores_forwarding(self):
        text = (ROOT / 'deploy/announcement-dialplan.conf').read_text()
        self.assertIn('Dial(PJSIP/${EXTEN},30,ib(', text)
        self.assertEqual(text.count('Set(TIMEOUT(absolute)='), 2)
        self.assertNotIn('include =>', text)


if __name__ == '__main__':
    unittest.main()
