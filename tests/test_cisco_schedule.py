import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cisco
import cisco_http


def config():
    return {'home_assistant': {'api': 'home_assistant', 'entry_id': 'synthetic-entry-123',
                               'gateway_url': 'http://192.168.50.11:8123',
                               'token': 'synthetic.jwt.' + 'a' * 40, 'transport': 'direct'}}


def gateway_inventory():
    return {'ami_fresh': True, 'observed_at': time.time(), 'dial_allowlist': ['600'], 'ring_allowlist': ['640'],
            'phones': [{'id': '212', 'lines': ['212', '213'], 'online': True, 'dnd': False,
                        'dnd_pending': False, 'autoanswer': False, 'channels': [], 'alert': None,
                        'capabilities': list(cisco.COMMANDS)}]}


def schedule():
    return {'schema_version': 1, 'phone': '212', 'lines': ['212', '213'], 'line': '213', 'timezone': 'America/Los_Angeles', 'gateway_fresh': True,
            'timer': {'line': '213', 'id': None, 'deadline': None, 'remaining_seconds': None, 'active': False},
            'alarm': {'line': '212', 'time': '07:00', 'weekdays': [0, 1, 2, 3, 4], 'enabled': False, 'configured': False},
            'alert': None, 'status': 'Ready', 'operation_result': 'applied'}


class ScheduleTests(unittest.TestCase):
    def invoke(self, args, *, response=None, configured=True, failure=None, gateway_failure=False):
        calls = []
        class FakeGateway:
            def __init__(self, settings, _timeout): self.settings = settings
            def request(self, path, body=None):
                calls.append((self.settings.get('api', 'gateway'), path, copy.deepcopy(body)))
                if path == '/v1/phones':
                    if gateway_failure:
                        raise cisco.Refused('network_outcome_unknown')
                    return gateway_inventory()
                if failure:
                    raise failure
                return copy.deepcopy(schedule() if response is None else response)
        with patch.object(cisco, 'Gateway', FakeGateway), \
                patch.object(cisco, 'load_config', return_value=config() if configured else {}), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            code = cisco.main(['--json', '213', *args])
        self.assertEqual(errors.getvalue(), '')
        return code, json.loads(output.getvalue()), calls

    def test_timer_start_is_one_atomic_service_on_exact_line(self):
        code, result, calls = self.invoke(['timer', 'start', '5', '--idempotency-key', 'request-1234'])
        self.assertEqual(code, 0)
        self.assertEqual(calls[0], ('home_assistant', cisco.SCHEDULE_PATH, {
            'entry_id': 'synthetic-entry-123', 'line': '213',
            'operation': 'timer_start', 'minutes': 5, 'replace': False, 'request_id': 'request-1234'}))
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['status'], 'accepted')

    def test_timer_boundaries_and_explicit_replace(self):
        for minutes in ('1', '1440'):
            code, _, calls = self.invoke(['timer', 'start', minutes, '--replace'])
            self.assertEqual(code, 0); self.assertTrue(calls[0][2]['replace'])
        for minutes in ('0', '1441', '-1'):
            code, _, calls = self.invoke(['timer', 'start', minutes])
            self.assertEqual(code, 1); self.assertEqual(len(calls), 0)

    def test_alarm_days_aliases_and_explicit_enable(self):
        for value, expected in [('weekdays', list(range(5))), ('weekend', [5, 6]),
                                ('daily', list(range(7))), ('fri,mon,sun', [0, 4, 6])]:
            code, _, calls = self.invoke(['alarm', 'set', '07:30', '--days', value, '--enable'])
            self.assertEqual(code, 0)
            self.assertEqual(calls[0][2]['weekdays'], expected)
            self.assertEqual(calls[0][2]['line'], '213')
            self.assertEqual(calls[0][2]['time'], '07:30')
            self.assertTrue(calls[0][2]['enabled'])
        code, _, calls = self.invoke(['alarm', 'set', '23:59', '--days', 'sat', '--replace'])
        self.assertEqual(code, 0); self.assertFalse(calls[0][2]['enabled']); self.assertTrue(calls[0][2]['replace'])

    def test_invalid_time_or_weekdays_never_posts(self):
        for at, days in [('24:00', 'mon'), ('7:00', 'mon'), ('12:60', 'mon'),
                         ('12:00', 'mon,mon'), ('12:00', 'monday'), ('12:00', '')]:
            code, _, calls = self.invoke(['alarm', 'set', at, '--days', days])
            self.assertEqual(code, 1); self.assertEqual(len(calls), 0)

    def test_cancel_and_alarm_toggles_preserve_ownership_without_override(self):
        for args, operation in [(['timer', 'cancel'], 'timer_cancel'),
                                (['alarm', 'on'], 'alarm_enable'), (['alarm', 'off'], 'alarm_disable')]:
            code, _, calls = self.invoke(args)
            self.assertEqual(code, 0)
            self.assertEqual(calls[0][2]['operation'], operation)
            self.assertEqual(calls[0][2]['line'], '213')
            self.assertNotIn('replace', calls[0][2])

    def test_status_lists_both_slot_owners_and_timezone(self):
        for args in (['schedule'], ['timer'], ['alarm'], ['timer', 'status'], ['alarm', 'status']):
            code, result, calls = self.invoke(args)
            self.assertEqual(code, 0)
            self.assertEqual(calls[0][2]['operation'], 'status')
            self.assertNotIn('request_id', calls[0][2])
            self.assertEqual(result['schedule']['alarm']['line'], '212')
            self.assertEqual(result['schedule']['timezone'], 'America/Los_Angeles')
            self.assertNotIn('status', result['schedule'])  # Do not echo free-form server text.

    def test_default_status_includes_schedules_and_reports_partial_reads(self):
        for args in ([], ['status']):
            code, result, calls = self.invoke(args)
            self.assertEqual(code, 0)
            self.assertEqual(result['phone']['id'], '212')
            self.assertEqual(result['schedule']['alarm']['line'], '212')
            self.assertEqual(calls[1][2]['operation'], 'status')
        code, result, _ = self.invoke([], failure=cisco.Refused('ha_service_http_error', http_status=404))
        self.assertEqual(code, 1); self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['phone']['online'])
        self.assertEqual(result['schedule_error']['http_status'], 404)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            cisco.display(result)
        self.assertIn('Schedule status unavailable', output.getvalue())
        self.assertIn('Line 213 / button 2', output.getvalue())

    def test_dry_run_only_reads_service_status(self):
        code, result, calls = self.invoke(['timer', 'start', '5', '--dry-run'])
        self.assertEqual(code, 0); self.assertEqual(result['status'], 'dry_run')
        self.assertEqual(calls[0][2]['operation'], 'status')
        self.assertNotIn('entry_id', result['request'])

    def test_schedule_requires_opt_in_ha_config(self):
        code, result, calls = self.invoke(['timer', 'start', '5'], configured=False)
        self.assertEqual(code, 1)
        self.assertEqual(result['reason'], 'home_assistant_schedule_config_required')
        self.assertEqual(len(calls), 0)

    def test_missing_service_or_unknown_outcome_never_retries(self):
        for failure in (cisco.Refused('ha_service_http_error', http_status=404),
                        cisco.Refused('network_outcome_unknown')):
            code, result, calls = self.invoke(['timer', 'start', '5'], failure=failure)
            self.assertEqual(code, 1); self.assertEqual(len(calls), 1)
            self.assertEqual(result['status'], 'not_confirmed'); self.assertIn('action_id', result)
        value = schedule(); value['operation_result'] = 'outcome_unknown'
        code, result, calls = self.invoke(['timer', 'start', '5'], response=value)
        self.assertEqual(code, 1); self.assertEqual(result['reason'], 'ha_schedule_outcome_unknown')
        self.assertEqual(len(calls), 1)
        self.assertFalse(result['schedule']['timer']['active'])

    def test_gateway_down_does_not_block_ha_status_or_owned_cancellation(self):
        data = schedule(); data['gateway_fresh'] = False
        for args in (['schedule'], ['timer', 'cancel'], ['alarm', 'off']):
            code, result, calls = self.invoke(args, response=data, gateway_failure=True)
            self.assertEqual(code, 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], 'home_assistant')
            self.assertNotIn('phone', calls[0][2])
            self.assertEqual(result['phone_id'], '212')
            self.assertFalse(result['schedule']['gateway_fresh'])
        code, result, calls = self.invoke([], response=data, gateway_failure=True)
        self.assertEqual(code, 1); self.assertEqual(result['status'], 'partial')
        self.assertIn('gateway_error', result)
        self.assertFalse(result['schedule']['timer']['active'])

    def test_response_must_match_host_line_and_typed_schedule(self):
        for key, value in [('phone', '218'), ('line', '212'), ('schema_version', 2)]:
            data = schedule(); data[key] = value
            code, _, _ = self.invoke(['schedule'], response=data)
            self.assertEqual(code, 1)
        data = schedule(); data['timer']['line'] = '219'
        self.assertEqual(self.invoke(['schedule'], response=data)[0], 1)
        data = schedule(); data['timer']['deadline'] = float('nan')
        self.assertEqual(self.invoke(['schedule'], response=data)[0], 1)
        data = schedule(); data['lines'] = ['213', '213']
        self.assertEqual(self.invoke(['schedule'], response=data)[0], 1)

    def test_ha_service_errors_use_fixed_codes_never_raw_messages(self):
        payload = {'url': 'http://192.168.50.11:8123', 'api': 'home_assistant',
                   'token': 'synthetic.jwt.' + 'h' * 40, 'timeout': 10,
                   'path': cisco.SCHEDULE_PATH, 'body': {'operation': 'timer_start'}}
        for message, expected in [
            ('A timer is already pending; explicit replace is required', 'ha_timer_replace_required'),
            ('The timer belongs to the other line', 'ha_timer_owned_by_other_line'),
            ('sensitive unexpected server detail', 'ha_service_http_error')]:
            body = io.BytesIO(json.dumps({'message': message}).encode())
            error = HTTPError(payload['url'], 500, 'Server Error', {}, body)
            with patch.object(cisco_http.request.OpenerDirector, 'open', side_effect=error):
                result = cisco_http.perform(payload)
            self.assertEqual(result['reason'], expected)
            self.assertNotIn(message, json.dumps(result))

    def test_separate_ha_token_and_explicit_transport_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            base = {'gateway_url': 'http://192.168.50.10:18084', 'transport': 'direct', 'token': 'g' * 48,
                    'home_assistant': {'url': 'http://192.168.50.11:8123', 'transport': 'direct',
                                       'entry_id': 'synthetic-entry-123', 'token_file': 'ha-token'}}
            token = Path(directory) / 'ha-token'; token.write_text('synthetic.jwt.' + 'h' * 40); token.chmod(0o600)
            path.write_text(json.dumps(base)); path.chmod(0o600)
            value = cisco.load_config(path)
            self.assertEqual(value['home_assistant']['api'], 'home_assistant')
            self.assertNotEqual(value['token'], value['home_assistant']['token'])
            del base['home_assistant']['transport']; path.write_text(json.dumps(base))
            with self.assertRaises(cisco.Refused): cisco.load_config(path)

    def test_http_worker_strips_changed_states_and_blocks_other_ha_services(self):
        payload = {'url': 'http://192.168.50.11:8123', 'api': 'home_assistant',
                   'token': 'synthetic.jwt.' + 'h' * 40, 'timeout': 10,
                   'path': cisco.SCHEDULE_PATH, 'body': {'operation': 'status'}}
        class Response(io.BytesIO):
            status = 200
        raw = json.dumps({'changed_states': [{'secret': 'do-not-forward'}], 'service_response': schedule()}).encode()
        with patch.object(cisco_http.request.OpenerDirector, 'open', return_value=Response(raw)):
            result = cisco_http.perform(payload)
        self.assertTrue(result['ok']); self.assertNotIn('do-not-forward', json.dumps(result))
        payload['path'] = '/api/services/homeassistant/restart?return_response'
        with patch.object(cisco_http.request.OpenerDirector, 'open') as request:
            self.assertFalse(cisco_http.perform(payload)['ok']); request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
