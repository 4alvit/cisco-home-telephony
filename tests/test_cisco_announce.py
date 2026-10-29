"""Announcements remain finite, privately configured and exact-call scoped."""
import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cisco
import cisco_announce as announce
import cisco_http
import cisco_lifesize as native
from test_cisco_cli import inventory, channel

KEY = 'announcement-test-key'
IDENTIFIER = announce.job_id(KEY)


def configuration():
    return {'announcements': {'api': 'announcements', 'groups': {'demo': ['212', '222']}},
            'lifesize': {'222': {'host': '192.168.50.20'}}}


def job(lines=('212',), status='finished', recipient_status='completed'):
    return {'id': IDENTIFIER, 'status': status, 'lines': list(lines),
            'expires_at': time.time()+180, 'duration_seconds': 3,
            'recipients': [{'line': line, 'phone_id': line, 'status': recipient_status,
                'channel': f'PJSIP/{line}-000000ab', 'uniqueid': '1234.'+line,
                'caller_id': '812345678', 'reason': None} for line in lines]}


def direct_configuration():
    config = configuration()
    config['announcements']['targets'] = {
        line: {'driver': 'ha_satellite', 'phone_id': line} for line in ('223', '224', '225', '226')}
    config['announcements']['groups']['speakers'] = ['223', '224', '225', '226']
    return config


def direct_job(lines=('223',), status='finished', recipient_status='completed'):
    result = job(lines, status, recipient_status)
    for recipient in result['recipients']:
        recipient.update(driver='ha_satellite', channel=None, uniqueid=None)
    return result


class AnnouncementCLITests(unittest.TestCase):
    def invoke(self, arguments, *, reply=None, failure=None, config=None):
        cfg = configuration() if config is None else config
        requests = []
        class Gateway:
            def __init__(self, config, *_): self.announcement = config.get('api') == 'announcements'
            def request(self, path, body=None):
                requests.append((self.announcement, path, copy.deepcopy(body)))
                if path == '/v1/phones': return inventory()
                if failure and body is not None: raise cisco.Refused(failure)
                return copy.deepcopy(reply or job())
        with patch.object(cisco, 'load_config', return_value=cfg), patch.object(cisco, 'Gateway', Gateway), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = cisco.main(['--json', '--idempotency-key', KEY, *arguments])
        return code, json.loads(output.getvalue()), requests

    def test_say_and_aliases_route_before_native_driver(self):
        for command in ('say', 'announce', 'voicemsg'):
            code, result, requests = self.invoke(['222', command, 'A', 'test', 'announcement'], reply=job(('222',)))
            self.assertEqual(code, 0)
            self.assertEqual(result['id'], IDENTIFIER)
            self.assertEqual(requests[-1][2]['text'], 'A test announcement')
            self.assertNotIn('A test announcement', json.dumps(result))
            self.assertNotIn('driver', result)

    def test_group_and_explicit_list_have_same_canonical_targets(self):
        for target in ('@demo', '222,212'):
            code, result, requests = self.invoke([target, 'say', 'Example'], reply=job(('212', '222')))
            self.assertEqual(code, 0)
            self.assertEqual(result['lines'], ['212', '222'])
            self.assertEqual(requests[-1][2]['lines'], ['212', '222'])

    def test_other_commands_do_not_accept_group_targets(self):
        code, result, requests = self.invoke(['@demo', 'status'])
        self.assertEqual(code, 1)
        self.assertEqual(result['reason'], 'extension_requires_three_digit_cisco_line')
        self.assertEqual(requests, [])

    def test_invalid_selection_has_no_backend_mutation(self):
        for target in ('212,213', '212,212', '299', '@absent', '212;222'):
            with self.subTest(target=target):
                code, _, requests = self.invoke([target, 'say', 'Example'])
                self.assertEqual(code, 1)
                self.assertFalse(any(body is not None for _, _, body in requests))

    def test_text_file_and_dry_run_do_not_answer_or_echo_content(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'text.txt';path.write_text('Private spoken message')
            with patch.object(announce, 'answer_once') as answer:
                code, result, requests = self.invoke(['212', 'say', '--text-file', str(path), '--dry-run'],
                    reply=job(status='dry_run', recipient_status='queued'))
            self.assertEqual(code, 0)
            self.assertEqual(result['status'], 'dry_run')
            self.assertEqual(requests[-1][2]['text'], 'Private spoken message')
            self.assertNotIn('Private spoken message', json.dumps(result))
            answer.assert_not_called()

    def test_text_validation_precedes_mutation(self):
        for args in (['212', 'say'], ['212', 'say', 'a'*1001],
                     ['212', 'say', 'text', '--text-file', '/tmp/not-opened'], ['212', 'say', 'bad\x00text']):
            code, _, requests = self.invoke(args)
            self.assertEqual(code, 1)
            self.assertFalse(any(body is not None for _, _, body in requests))

    def test_unknown_submission_has_deterministic_recovery_and_no_retry(self):
        code, result, requests = self.invoke(['212', 'say', 'Sensitive'], failure='network_outcome_unknown')
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'not_confirmed')
        self.assertEqual(result['id'], IDENTIFIER)
        self.assertIn('say-status '+IDENTIFIER, result['recovery_command'])
        self.assertNotIn('Sensitive', json.dumps(result))
        self.assertEqual(sum(body is not None for _, _, body in requests), 1)

    def test_stop_checks_exact_job_targets_before_post(self):
        code, result, requests = self.invoke(['222', 'say-stop', IDENTIFIER], reply=job(status='running'))
        self.assertEqual(code, 1)
        self.assertEqual(result['reason'], 'announcement_targets_changed')
        self.assertFalse(any(body is not None for _, _, body in requests))

    def test_status_does_not_need_phone_inventory_or_answer(self):
        code, result, requests = self.invoke(['212', 'say-status', IDENTIFIER])
        self.assertEqual(code, 0)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][1], '/v1/announcements/'+IDENTIFIER)

    def test_unexpected_fields_are_filtered_from_receipts(self):
        value = job(); value.update(text='SECRET', token='SECRET')
        value['recipients'][0]['address'] = 'SECRET'
        code, result, _ = self.invoke(['212', 'say-status', IDENTIFIER], reply=value)
        self.assertEqual(code, 0)
        self.assertNotIn('SECRET', json.dumps(result))

    def test_skipped_recipient_is_reported_as_partial_failure(self):
        reply = job(); reply['recipients'][0].update(status='skipped', reason='busy')
        code, result, _ = self.invoke(['212', 'say', 'Example'], reply=reply)
        self.assertEqual(code, 1)
        self.assertEqual(result['recipients'][0]['reason'], 'busy')

    def test_direct_group_uses_one_number_per_device_and_no_cisco_inventory(self):
        lines = ('223', '224', '225', '226')
        with patch.object(announce, 'answer_once') as answer, patch.object(native, 'Phone') as phone:
            code, result, requests = self.invoke(['@speakers', 'voicemsg', 'Example'],
                reply=direct_job(lines), config=direct_configuration())
        self.assertEqual(code, 0)
        self.assertEqual(result['lines'], list(lines))
        self.assertFalse(any(path == '/v1/phones' for _, path, _ in requests))
        self.assertEqual(requests[0][2]['expected_routes'], {line: 'ha_satellite' for line in lines})
        answer.assert_not_called()
        phone.assert_not_called()

    def test_direct_dry_run_never_opens_phone_or_answers(self):
        with patch.object(announce, 'prepare_native') as prepare, patch.object(announce, 'answer_once') as answer:
            code, result, requests = self.invoke(['223', 'say', 'Example', '--dry-run'],
                reply=direct_job(status='dry_run', recipient_status='queued'), config=direct_configuration())
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'dry_run')
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0][2]['dry_run'])
        prepare.assert_not_called()
        answer.assert_not_called()

    def test_client_server_route_mismatch_is_not_accepted(self):
        with patch.object(announce, 'answer_once') as answer:
            code, result, _ = self.invoke(['223', 'say', 'Example'], reply=job(('223',)),
                                         config=direct_configuration())
        self.assertEqual(code, 1)
        self.assertEqual(result['reason'], 'announcement_route_changed')
        answer.assert_not_called()

    def test_unknown_direct_outcome_is_not_success_for_send_status_or_stop(self):
        for args in (['223', 'say', 'Example'], ['223', 'say-status', IDENTIFIER],
                     ['223', 'say-stop', IDENTIFIER]):
            code, result, requests = self.invoke(args, reply=direct_job(recipient_status='unknown'),
                                                 config=direct_configuration())
            self.assertEqual(code, 1)
            self.assertEqual(result['recipients'][0]['status'], 'unknown')
            self.assertLessEqual(sum(body is not None for _, _, body in requests), 1)


class AnnouncementConfigTests(unittest.TestCase):
    def config(self):
        return {'gateway_url': 'http://127.0.0.1:18085', 'transport': 'kubectl',
                'token_file': 'announcement.token', 'groups': {'demo': ['212', '222']},
                'kubectl': {'context': 'lab', 'namespace': 'telephony',
                            'selector': 'app=asterisk', 'container': 'announcer'}}

    def test_private_token_is_relative_to_configuration_file(self):
        calls = []
        def read(path, limit, **kwargs):
            calls.append((path, limit, kwargs)); return b'x'*48
        result = announce.validate_config(self.config(), '/tmp/private/config.json', read)
        self.assertEqual(result['api'], 'announcements')
        self.assertEqual(calls, [(Path('/tmp/private/announcement.token'), 512, {'private': True})])

    def test_invalid_configuration_never_reads_token(self):
        for update in ({'transport': 'direct'}, {'gateway_url': 'http://localhost:18085'},
                       {'gateway_url': 'http://127.0.0.1:18085/api'},
                       {'gateway_url': 'http://user:secret@127.0.0.1:18085'},
                       {'gateway_url': 'http://127.0.0.1:18085?'}, {'token': 'x'*48}):
            value = self.config(); value.update(update)
            with self.subTest(update=update), self.assertRaises((ValueError, announce.AnnouncementError)):
                announce.validate_config(value, '/tmp/config.json', lambda *_a, **_k: self.fail('token read'))

    def test_groups_reject_duplicates_and_non_extension_targets(self):
        for members in (['212', '212'], ['911'], ['212;222'], list(range(17)), []):
            value = self.config(); value['groups'] = {'demo': members}
            with self.assertRaises(announce.AnnouncementError):
                announce.validate_config(value, '/tmp/config.json', lambda *_a, **_k: b'x'*48)

    def test_direct_targets_require_exactly_one_number_per_device(self):
        valid = self.config()
        valid['targets'] = {'223': {'driver': 'ha_satellite', 'phone_id': '223'}}
        result = announce.validate_config(valid, '/tmp/config.json', lambda *_a, **_k: b'x'*48)
        self.assertEqual(result['targets'], valid['targets'])
        for invalid in ([], {'911': {'driver': 'ha_satellite', 'phone_id': '911'}},
                        {'223': {'driver': 'ha_satellite', 'phone_id': '224'}},
                        {'223': {'driver': 'cisco', 'phone_id': '223'}},
                        {'223': {'driver': 'ha_satellite', 'phone_id': '223', 'entity_id': 'hidden'}}):
            value = self.config(); value['targets'] = invalid
            with self.assertRaises(announce.AnnouncementError):
                announce.validate_config(value, '/tmp/config.json', lambda *_a, **_k: b'x'*48)

    def test_direct_target_cannot_shadow_native_or_cisco_phone(self):
        value = direct_configuration()
        value['announcements']['targets']['212'] = {'driver': 'ha_satellite', 'phone_id': '212'}
        value['announcements']['targets']['222'] = {'driver': 'ha_satellite', 'phone_id': '222'}
        for line in ('212', '222'):
            with self.assertRaises(announce.AnnouncementError):
                announce.known_targets([line], value, inventory(), cisco.validate_inventory)

    def test_direct_receipts_never_contain_sip_call_or_alias(self):
        for mutate in (lambda r: r.update(channel='PJSIP/223-abc', uniqueid='1.223'),
                       lambda r: r.update(status='ringing'), lambda r: r.update(phone_id='224')):
            value = direct_job(); mutate(value['recipients'][0])
            with self.assertRaises(announce.AnnouncementError):
                announce.validate_job(value, IDENTIFIER, ['223'])
        value = direct_job(status='running', recipient_status='playing')
        self.assertEqual(announce.validate_job(value, IDENTIFIER, ['223'])['recipients'][0]['status'], 'playing')

    def test_loopback_is_only_for_dedicated_api_and_fixed_paths(self):
        base = {'url': 'http://127.0.0.1:18085', 'token': 'x'*48,
                'timeout': 2, 'body': None, 'path': '/v1/announcements/'+IDENTIFIER}
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, *_): return b'{}'
        with patch.object(cisco_http.request, 'build_opener') as opener:
            opener.return_value.open.return_value = Response()
            for api in ('gateway', 'home_assistant'):
                self.assertFalse(cisco_http.perform(dict(base, api=api))['ok'])
            opener.assert_not_called()
            self.assertTrue(cisco_http.perform(dict(base, api='announcements'))['ok'])
            self.assertEqual(opener.call_count, 1)
            for path, body in (('/v1/phones', None), ('/v1/announcements', None),
                               ('/v1/announcements/'+IDENTIFIER, {}),
                               ('/v1/announcements/'+IDENTIFIER+'/stop', None)):
                self.assertFalse(cisco_http.perform(dict(base, api='announcements', path=path, body=body))['ok'])
            self.assertEqual(opener.call_count, 1)

    def test_response_validation_rejects_changed_identity_and_nonfinite_values(self):
        mutations = [lambda v: v.update(id='0'*32), lambda v: v.update(expires_at=True),
            lambda v: v.update(duration_seconds=float('nan')),
            lambda v: v.update(status='unexpected'),
            lambda v: v['recipients'][0].update(channel='PJSIP/213-000000ab'),
            lambda v: v['recipients'][0].update(caller_id='caller;command'),
            lambda v: v['recipients'][0].update(reason='secret\nvalue'),
            lambda v: v['recipients'][0].update(status='ringing', channel=None, uniqueid=None)]
        for mutate in mutations:
            value = job(); mutate(value)
            with self.assertRaises(announce.AnnouncementError):
                announce.validate_job(value, IDENTIFIER, ['212'])


class AnnouncementAnswerTests(unittest.TestCase):
    def test_direct_target_cannot_invoke_native_answer_even_with_forged_ringing(self):
        result = announce.answer_once(direct_configuration(), 5, IDENTIFIER, ['223'],
            job(('223',), status='running', recipient_status='ringing')['recipients'][0],
            {'driver': 'ha_satellite', 'phone_id': '223'},
            lambda *_: self.fail('gateway must not be opened'), cisco.validate_inventory, threading.Event())
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(result['reason'], 'direct_announcement_has_no_sip_answer')

    def test_prepared_native_transport_avoids_login_and_identity_reads_while_ringing(self):
        reply = job(('222',), status='running', recipient_status='ringing')
        commands = []
        class PreparedPhone:
            def snapshot(self, _):
                raise AssertionError('identity reads must finish before ringing')
            def request(self, command):
                commands.append(command)
                return ['12,2,Ringing,Yes,Audio,812345678,']
        class Gateway:
            def __init__(self, *_): pass
            def request(self, *_): return reply
        with patch.object(native, 'Phone', side_effect=AssertionError('unexpected new connection')), \
                patch.object(native, 'device_lock', lambda _: contextlib.nullcontext()):
            result = announce.answer_once(configuration(), 5, IDENTIFIER, ['222'], reply['recipients'][0],
                {'driver': 'lifesize', 'phone_id': '222'}, Gateway, cisco.validate_inventory,
                threading.Event(), {'phone': PreparedPhone(), 'reason': None})
        self.assertEqual(result['status'], 'acknowledged')
        self.assertEqual(commands, ['status call active', 'status call active',
                                    'control call answer -t audio 12'])

    def test_failed_native_preparation_never_reconnects_or_answers(self):
        reply = job(('222',), status='running', recipient_status='ringing')
        with patch.object(native, 'Phone', side_effect=AssertionError('unexpected reconnect')), \
                patch.object(native, 'device_lock', lambda _: contextlib.nullcontext()):
            result = announce.answer_once(configuration(), 5, IDENTIFIER, ['222'], reply['recipients'][0],
                {'driver': 'lifesize', 'phone_id': '222'}, lambda *_: None, cisco.validate_inventory,
                threading.Event(), {'phone': None, 'reason': 'lifesize_connection_unavailable'})
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(result['reason'], 'lifesize_connection_unavailable')

    def cisco_answer(self, *, mutate_phone=None, mutate_server=None, cancelled=False, failure=False):
        reply = job(status='running', recipient_status='ringing')
        recipient = copy.deepcopy(reply['recipients'][0])
        data = inventory(); call = channel('212', 'Ringing'); call['uniqueid'] = recipient['uniqueid']
        data['phones'][0]['channels'] = [call]
        if mutate_phone: mutate_phone(data['phones'][0])
        if mutate_server: mutate_server(reply)
        requests = []
        class Gateway:
            def __init__(self, *_): pass
            def request(self, path, body=None):
                requests.append((path, body))
                if path.startswith('/v1/announcements/'): return reply
                if path == '/v1/phones': return data
                if failure: raise cisco.Refused('network_outcome_unknown')
                return {'status': 'accepted', 'action_id': body['idempotency_key']}
        stop = threading.Event()
        if cancelled: stop.set()
        result = announce.answer_once(configuration(), 5, IDENTIFIER, ['212'], recipient,
            {'driver': 'cisco', 'phone_id': '212'}, Gateway, cisco.validate_inventory, stop)
        return result, requests

    def test_cisco_answer_uses_exact_line_channel_and_uniqueid(self):
        result, requests = self.cisco_answer()
        self.assertEqual(result['status'], 'acknowledged')
        self.assertEqual(requests[-1], ('/v1/phones/212/answer', {
            'line': '212', 'channel': 'PJSIP/212-000000ab', 'uniqueid': '1234.212',
            'idempotency_key': 'announce-'+IDENTIFIER+'-212'}))

    def test_cisco_answer_refuses_call_replacement_dnd_or_concurrent_call(self):
        mutations = [lambda p: p.update(dnd=True), lambda p: p.update(dnd_pending=True),
            lambda p: p['channels'][0].update(uniqueid='different'),
            lambda p: p['channels'][0].update(state='Up'),
            lambda p: p['channels'].append(channel('213', 'Ringing'))]
        for mutate in mutations:
            result, requests = self.cisco_answer(mutate_phone=mutate)
            self.assertEqual(result['status'], 'refused')
            self.assertFalse(any(body is not None for _, body in requests))

    def test_cisco_answer_rechecks_server_ownership_and_local_cancellation(self):
        for mutate in (lambda v: v.update(status='stopping'),
                       lambda v: v.update(expires_at=time.time()-1),
                       lambda v: v['recipients'][0].update(caller_id='899999999')):
            result, requests = self.cisco_answer(mutate_server=mutate)
            self.assertEqual(result['status'], 'refused')
            self.assertFalse(any(body is not None for _, body in requests))
        result, requests = self.cisco_answer(cancelled=True)
        self.assertEqual(result['reason'], 'announcement_cancelled')
        self.assertFalse(any(body is not None for _, body in requests))

    def test_lost_answer_response_is_not_retried(self):
        result, requests = self.cisco_answer(failure=True)
        self.assertEqual(result['status'], 'not_confirmed')
        self.assertEqual(sum(body is not None for _, body in requests), 1)

    def native_answer(self, first, second=None):
        reply = job(('222',), status='running', recipient_status='ringing')
        commands = []
        class Phone:
            def __init__(self, *_): pass
            def snapshot(self, line):
                if line != '222': raise AssertionError('wrong line')
                return {'calls': native.parse_calls(first)}
            def request(self, command):
                commands.append(command)
                return first if second is None else second
        class Gateway:
            def __init__(self, *_): pass
            def request(self, *_): return reply
        with patch.object(native, 'Phone', Phone), patch.object(native, 'device_lock', lambda _: contextlib.nullcontext()):
            result = announce.answer_once(configuration(), 5, IDENTIFIER, ['222'], reply['recipients'][0],
                {'driver': 'lifesize', 'phone_id': '222'}, Gateway, cisco.validate_inventory, threading.Event())
        return result, commands

    def test_native_answer_checks_numeric_caller_and_never_changes_preferences(self):
        for caller in ('812345678', 'sip:812345678@192.168.50.10', 'sips:812345678@[::1]:5061'):
            result, commands = self.native_answer([f'12,2,Ringing,Yes,Audio,{caller},'])
            self.assertEqual(result['status'], 'acknowledged', (caller, result))
            self.assertEqual(commands, ['status call active', 'control call answer -t audio 12'])

    def test_native_answer_refuses_unrelated_replaced_or_multiple_calls(self):
        valid = '12,2,Ringing,Yes,Audio,812345678,'
        for first, second in ((['12,2,Ringing,Yes,Audio,555,'], None),
                              ([valid], ['13,2,Ringing,Yes,Audio,812345678,']),
                              ([valid, '13,3,Ringing,Yes,Audio,555,'], None),
                              (['12,2,Ringing,No,Audio,812345678,'], None)):
            result, commands = self.native_answer(first, second)
            self.assertEqual(result['status'], 'refused')
            self.assertFalse(any(command.startswith('control ') for command in commands))


class AnnouncementPollingTests(unittest.TestCase):
    def test_mixed_group_only_answers_the_sip_recipient(self):
        args = cisco.parser().parse_args(['--idempotency-key', KEY, '212,223', 'say', 'Example'])
        running = job(('212',), status='running', recipient_status='ringing')
        running['lines'].append('223')
        running['recipients'].extend(direct_job(status='running', recipient_status='playing')['recipients'])
        finished = copy.deepcopy(running); finished['status'] = 'finished'
        for recipient in finished['recipients']: recipient['status'] = 'completed'
        bodies = []
        class Gateway:
            def __init__(self, *_): pass
            def request(self, path, body=None):
                if path == '/v1/phones': return inventory()
                if body is not None:
                    bodies.append(body)
                    return running
                return finished
        with patch.object(announce, 'answer_once', return_value={'line': '212', 'status': 'acknowledged'}) as answer:
            result, code = announce.run(args, direct_configuration(), gateway_factory=Gateway,
                inventory_validator=cisco.validate_inventory, read_file=cisco.read_file, sleep=lambda _: None)
        self.assertEqual(code, 0)
        self.assertEqual(answer.call_count, 1)
        self.assertEqual(answer.call_args.args[4]['line'], '212')
        self.assertEqual(bodies[0]['expected_routes'], {'212': 'sip', '223': 'ha_satellite'})
        self.assertEqual(result['recipients'][1]['driver'], 'ha_satellite')

    def test_native_connection_and_identity_are_ready_before_submission_and_closed_afterward(self):
        args = cisco.parser().parse_args(['--idempotency-key', KEY, '222', 'say', 'Example'])
        events = []
        class Phone:
            def __init__(self, *_): pass
            @contextlib.contextmanager
            def connection(self):
                events.append('connect')
                try: yield self
                finally: events.append('close')
            def snapshot(self, line):
                events.append('verify-' + line)
                return {'registered': True, 'calls': []}
        class Gateway:
            def __init__(self, *_): pass
            def request(self, path, body=None):
                if path == '/v1/phones': return inventory()
                events.append('submit')
                return job(('222',))
        with patch.object(native, 'Phone', Phone):
            result, code = announce.run(args, configuration(), gateway_factory=Gateway,
                inventory_validator=cisco.validate_inventory, read_file=cisco.read_file)
        self.assertEqual(code, 0)
        self.assertEqual(events, ['connect', 'verify-222', 'submit', 'close'])

    def test_group_answers_run_in_parallel_and_uncertain_answers_are_never_retried(self):
        args = cisco.parser().parse_args(['--idempotency-key', KEY, '212,222', 'say', 'Example'])
        attempts, polls = [], []
        started = threading.Event()
        lock = threading.Lock()
        class Gateway:
            def __init__(self, *_): pass
            def request(self, path, body=None):
                if path == '/v1/phones': return inventory()
                if body is None: polls.append(path)
                if len(polls) >= 3:
                    return job(('212', '222'), status='finished', recipient_status='failed')
                return job(('212', '222'), status='running', recipient_status='ringing')
        def answer(_config, _timeout, _id, _lines, recipient, *_rest):
            with lock:
                attempts.append(recipient['line'])
                if len(attempts) == 2: started.set()
            if not started.wait(2): raise AssertionError('answer workers ran serially')
            return {'line': recipient['line'], 'status': 'not_confirmed', 'reason': 'network_outcome_unknown'}
        def pause(_duration):
            if not started.wait(2): raise AssertionError('answer workers were not started')
        with patch.object(announce, 'answer_once', answer):
            result, code = announce.run(args, configuration(), gateway_factory=Gateway,
                inventory_validator=cisco.validate_inventory, read_file=cisco.read_file, sleep=pause)
        self.assertEqual(code, 1)
        self.assertEqual(sorted(attempts), ['212', '222'])
        self.assertEqual(len(result['answers']), 2)
        self.assertEqual(len(polls), 3)

    def test_polling_deadline_preserves_job_for_read_only_recovery(self):
        args = cisco.parser().parse_args(['--idempotency-key', KEY, '212', 'say', 'Example'])
        requests = []
        class Gateway:
            def __init__(self, *_): pass
            def request(self, path, body=None):
                requests.append((path, body))
                if path == '/v1/phones': return inventory()
                return job(status='accepted', recipient_status='queued')
        ticks = iter((0, 241))
        result, code = announce.run(args, configuration(), gateway_factory=Gateway,
            inventory_validator=cisco.validate_inventory, read_file=cisco.read_file, clock=lambda: next(ticks))
        self.assertEqual(code, 1)
        self.assertEqual(result['reason'], 'announcement_wait_timeout')
        self.assertEqual(result['id'], IDENTIFIER)
        self.assertEqual(sum(body is not None for _, body in requests), 1)
        self.assertFalse(any(path.endswith('/stop') for path, _ in requests))
