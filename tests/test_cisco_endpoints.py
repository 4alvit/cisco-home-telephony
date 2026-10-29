"""Opted-in SIP clients use fresh gateway observations and bounded callbacks."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cisco
import cisco_http


def inventory():
    return {'ami_fresh': True, 'observed_at': time.time(),
            'dial_allowlist': ['290', '291', '600'], 'ring_allowlist': ['640'],
            'phones': [{'id': '212', 'lines': ['212', '213'], 'online': True,
                        'dnd': False, 'autoanswer': False, 'dnd_pending': False,
                        'channels': [], 'alert': None, 'capabilities': list(cisco.COMMANDS)}],
            'softphones': [{'extension': '290', 'room': 'Speaker client', 'online': True,
                            'controls_enabled': True},
                           {'extension': '291', 'room': 'Read only client', 'online': True}]}


def endpoint_status():
    return {'extension': '290', 'room': 'Speaker client', 'ami_fresh': True,
            'observed_at': time.time(), 'configured': True, 'available': True,
            'call_state': 'idle', 'calling': False, 'ringing': False, 'in_call': False,
            'dnd': False, 'dnd_pending': False, 'calls': [], 'dial_allowlist': ['290', '291', '600'],
            'capabilities': {'call': True, 'hangup': True, 'dnd': True,
                             'answer': False, 'auto_answer': False, 'media': False}}


def call():
    return {'channel': 'PJSIP/290-000000ab', 'uniqueid': '1234567890.21', 'state': 'Up'}


class EndpointCLITests(unittest.TestCase):
    def invoke(self, args, *, data=None, state=None, response=None, failure=None,
               before_dispatch=False, config=None, human=False):
        data = inventory() if data is None else data
        state = endpoint_status() if state is None else state
        requests = []

        class Gateway:
            def __init__(self, *_):
                self.on_dispatch = None

            def request(self, path, body=None):
                requests.append((path, copy.deepcopy(body)))
                if path == '/v1/phones':
                    return copy.deepcopy(data)
                if body is None:
                    return copy.deepcopy(state)
                if failure and before_dispatch:
                    raise failure
                if self.on_dispatch:
                    self.on_dispatch()
                if failure:
                    raise failure
                result = {'status': 'accepted', 'action_id': body['idempotency_key'], 'extension': '290'}
                if path.endswith('/call'):
                    result.update(number=body['number'], mode='callback', ring_timeout_seconds=20)
                elif path.endswith('/hangup'):
                    result.update(channel=body['channel'], uniqueid=body['uniqueid'])
                elif path.endswith('/dnd'):
                    result['dnd'] = body['enabled']
                result.update(response or {})
                return result

        with patch.object(cisco, 'Gateway', Gateway), \
                patch.object(cisco, 'load_config', return_value=config or {}), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            code = cisco.main(args if human else ['--json', *args])
        return code, output.getvalue() if human else json.loads(output.getvalue()), requests, errors.getvalue()

    def test_default_status_is_fresh_filtered_and_shows_callback_examples(self):
        state = endpoint_status()
        state.update(password='do-not-print', ipv4='do-not-print')
        code, result, requests, _ = self.invoke(['290'], state=state)
        self.assertEqual(code, 0)
        self.assertEqual(requests, [('/v1/phones', None), ('/v1/endpoints/290', None)])
        self.assertEqual(result['driver'], 'sip_endpoint')
        self.assertEqual(result['endpoint']['call_state'], 'idle')
        self.assertIn('cisco 290 600', result['examples'])
        self.assertIn('call NUMBER', result['available_commands'])
        self.assertFalse(set(result['available_commands']) & {'answer', 'speaker toggle', 'play', 'pickup'})
        self.assertNotIn('do-not-print', json.dumps(result))
        self.assertIn('Answer on the device', result['note'])
        code, text, _, _ = self.invoke(['290'], human=True)
        self.assertEqual(code, 0)
        self.assertIn('call state: idle', text)
        self.assertIn('cisco 290 600', text)

    def test_opt_in_does_not_change_cisco_only_validation_or_legacy_clients(self):
        with self.assertRaises(cisco.Refused) as rejected:
            cisco.validate_inventory(inventory(), '290')
        self.assertEqual(rejected.exception.reason, 'line_not_in_cisco_gateway_inventory')
        for args in (['291'], ['291', '600'], ['291', 'dnd', 'on']):
            code, result, requests, _ = self.invoke(args)
            self.assertEqual((code, result['reason']), (1, 'line_not_in_cisco_gateway_inventory'))
            self.assertEqual(requests, [('/v1/phones', None)])
        for invalid in ('true', 1, None):
            data = inventory(); data['softphones'][0]['controls_enabled'] = invalid
            code, result, requests, _ = self.invoke(['290'], data=data)
            self.assertEqual((code, result['reason']), (1, 'invalid_softphone_controls'))
            self.assertEqual(len(requests), 1)

    def test_optional_callback_state_is_validated_and_displayed(self):
        for callback_state in ('idle', 'starting', 'active', 'unknown'):
            state = dict(endpoint_status(), callback_state=callback_state)
            code, result, requests, _ = self.invoke(['290'], state=state)
            self.assertEqual(code, 0)
            self.assertEqual(result['endpoint']['callback_state'], callback_state)
            self.assertEqual(len(requests), 2)
            code, output, _, _ = self.invoke(['290'], state=state, human=True)
            self.assertEqual(code, 0)
            self.assertIn('Callback state: ' + callback_state, output)
            if callback_state == 'unknown':
                self.assertIn('Inspect the endpoint and reconcile the pending callback', result['note'])
                self.assertIn('do not retry blindly', output)
        code, result, _, _ = self.invoke(['290'])
        self.assertEqual(code, 0)
        self.assertNotIn('callback_state', result['endpoint'])
        for invalid in (None, True, [], {}, 'unrecognized'):
            state = dict(endpoint_status(), callback_state=invalid)
            code, result, requests, _ = self.invoke(['290'], state=state)
            self.assertEqual((code, result['reason']), (1, 'invalid_callback_state'))
            self.assertEqual(len(requests), 2)

    def test_nonidle_callback_state_blocks_a_new_call_before_dispatch(self):
        for callback_state in ('starting', 'active', 'unknown'):
            state = dict(endpoint_status(), callback_state=callback_state)
            code, result, requests, _ = self.invoke(['290', '600'], state=state)
            self.assertEqual((code, result['reason']), (1, 'callback_pending'))
            self.assertEqual(len(requests), 2)

    def test_shorthand_dial_and_call_use_the_same_callback_contract(self):
        for command in (['600'], ['dial', '600'], ['call', '600']):
            code, result, requests, _ = self.invoke(['290', *command, '--idempotency-key', 'callback-test'])
            self.assertEqual(code, 0)
            self.assertEqual(requests[-1], ('/v1/endpoints/290/call', {
                'number': '600', 'idempotency_key': 'callback-test'}))
            self.assertEqual(len(requests), 3)
            self.assertEqual(result['mode'], 'callback')
            self.assertEqual(result['ring_timeout_seconds'], 20)
            self.assertIn('this endpoint rings first', result['note'])
            self.assertNotIn('connected', result)

    def test_call_requires_idle_available_allowed_destination_and_no_dnd(self):
        changes = ({'available': False}, {'configured': False, 'available': False},
                   {'call_state': 'calling', 'calling': True}, {'calls': [call()]},
                   {'dnd': True}, {'dnd_pending': True}, {'call_state': 'unknown'},
                   {'ringing': True}, {'in_call': True})
        for change in changes:
            state = endpoint_status(); state.update(change)
            code, result, requests, _ = self.invoke(['290', '600'], state=state)
            self.assertEqual((code, result['status']), (1, 'refused'))
            self.assertEqual(len(requests), 2)
        for number in ('290', '911', 'sip:600@example.invalid'):
            code, _, requests, _ = self.invoke(['290', 'call', number])
            self.assertEqual(code, 1)
            self.assertEqual(len(requests), 2)

    def test_hangup_uses_one_fresh_exact_channel_and_uniqueid(self):
        state = endpoint_status(); state.update(calls=[call()], call_state='in_call', in_call=True)
        code, result, requests, _ = self.invoke(['290', 'hangup'], state=state)
        self.assertEqual(code, 0)
        self.assertEqual(requests[-1][0], '/v1/endpoints/290/hangup')
        self.assertEqual(requests[-1][1]['channel'], call()['channel'])
        self.assertEqual(requests[-1][1]['uniqueid'], call()['uniqueid'])
        self.assertEqual(result['uniqueid'], call()['uniqueid'])
        for calls in ([], [call(), dict(call(), channel='PJSIP/290-000000ac', uniqueid='1234567890.22')],
                      [dict(call(), channel='PJSIP/291-000000ab')], [dict(call(), uniqueid=None)], [call(), call()]):
            state['calls'] = calls
            code, _, requests, _ = self.invoke(['290', 'hangup'], state=state)
            self.assertEqual(code, 1)
            self.assertEqual(len(requests), 2)

    def test_dnd_read_and_change_are_endpoint_scoped(self):
        for command in (['dnd'], ['dnd', 'status']):
            code, result, requests, _ = self.invoke(['290', *command])
            self.assertEqual((code, result['status']), (0, 'ok'))
            self.assertEqual(len(requests), 2)
        for enabled in ('on', 'off'):
            code, result, requests, _ = self.invoke(['290', 'dnd', enabled])
            self.assertEqual(code, 0)
            self.assertEqual(requests[-1][0], '/v1/endpoints/290/dnd')
            self.assertIs(requests[-1][1]['enabled'], enabled == 'on')
            self.assertIs(result['dnd'], enabled == 'on')

    def test_unsupported_remote_device_controls_never_mutate(self):
        for command in (['answer'], ['pickup'], ['speaker', 'toggle'], ['autoanswer', 'on'],
                        ['intercom', '291'], ['volume', 'ringer', 'up'], ['message', 'Hello']):
            code, result, requests, _ = self.invoke(['290', *command])
            self.assertEqual((code, result['reason']), (1, 'command_not_supported_by_endpoint'))
            self.assertTrue(all(body is None for _, body in requests))

    def test_dry_run_validates_status_without_post(self):
        for command in (['600'], ['dnd', 'on'], ['hangup']):
            state = endpoint_status()
            if command == ['hangup']:
                state.update(calls=[call()], call_state='in_call', in_call=True)
            code, result, requests, _ = self.invoke(['290', *command, '--dry-run'], state=state)
            self.assertEqual((code, result['status']), (0, 'dry_run'))
            self.assertEqual(len(requests), 2)

    def test_invalid_fresh_status_prevents_post_and_ha_fallback(self):
        changes = ({'extension': '291'}, {'ami_fresh': False}, {'observed_at': time.time() - 100},
                   {'observed_at': 10 ** 400}, {'observed_at': True}, {'available': 'yes'},
                   {'calls': None}, {'capabilities': {'call': True}})
        for change in changes:
            state = endpoint_status(); state.update(change)
            code, result, requests, _ = self.invoke(['290'], state=state, config={'home_assistant': {}})
            self.assertEqual((code, result['status']), (1, 'refused'))
            self.assertEqual(len(requests), 2)
            self.assertNotIn('answer', result['offline_commands'])
            self.assertNotIn('speaker toggle', result['offline_commands'])

    def test_lost_or_mismatched_mutation_response_is_unknown_without_retry(self):
        for response in ({'action_id': 'different'}, {'extension': '291'}, {'mode': 'connected'},
                         {'number': '291'}, {'ring_timeout_seconds': True}):
            code, result, requests, _ = self.invoke(['290', '600', '--idempotency-key', 'once-only'], response=response)
            self.assertEqual((code, result['status']), (1, 'not_confirmed'))
            self.assertEqual(result['action_id'], 'once-only')
            self.assertEqual(len(requests), 3)
        for before_dispatch in (False, True):
            code, result, requests, _ = self.invoke(['290', '600'],
                failure=cisco.Refused('network_outcome_unknown'), before_dispatch=before_dispatch)
            self.assertEqual(code, 1)
            self.assertEqual(result['status'], 'refused' if before_dispatch else 'not_confirmed')
            self.assertEqual(len(requests), 3)
            if not before_dispatch:
                self.assertIn('Do not retry blindly', result['note'])
                self.assertEqual(result['outcome'], 'unknown')
                self.assertTrue(result['recovery_command'].endswith('290 status'))

    def test_directory_distinguishes_controlled_and_read_only_clients(self):
        code, result, requests, _ = self.invoke(['290', 'directory'])
        self.assertEqual(code, 0)
        selected = [entry for entry in result['directory'] if entry['selected']]
        self.assertEqual(selected[0]['id'], '290')
        self.assertIs(selected[0]['controls_enabled'], True)
        self.assertNotIn('controls_enabled', next(entry for entry in result['directory'] if entry['id'] == '291'))
        self.assertEqual(requests, [('/v1/phones', None)])
        code, output, _, _ = self.invoke(['directory'], human=True)
        self.assertIn('SIP callback, hangup and DND', output)
        self.assertIn('softphone (directory only; no Cisco controls)', output)

    def test_lifesize_native_driver_takes_precedence_over_generic_opt_in(self):
        native_result = {'status': 'ok', 'driver': 'lifesize_ssh'}
        with patch.object(cisco, 'lifesize_operation', return_value=native_result) as native:
            code, result, requests, _ = self.invoke(['290', '600'], config={'lifesize': {'290': {}}})
        self.assertEqual((code, result), (0, native_result))
        self.assertEqual(native.call_args.args[0], 'run')
        self.assertEqual(requests, [('/v1/phones', None)])


class EndpointHTTPTests(unittest.TestCase):
    def invoke(self, path, body):
        payload = {'url': 'http://192.168.50.10:8080', 'token': 'x' * 40,
                   'timeout': 5, 'path': path, 'body': body}
        with patch.object(cisco_http.request, 'build_opener') as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b'{}'
            result = cisco_http.perform(payload)
        return result, opener.return_value.open.call_count

    def test_only_exact_read_and_mutation_routes_are_allowed(self):
        for path, body in (('/v1/endpoints/290', None), ('/v1/endpoints/290/call', {}),
                           ('/v1/endpoints/290/hangup', {}), ('/v1/endpoints/290/dnd', {})):
            self.assertEqual(self.invoke(path, body), ({'ok': True, 'data': {}}, 1))
        for path, body in (('/v1/endpoints/290/status', None), ('/v1/endpoints/290', {}),
                           ('/v1/endpoints/290/call', None), ('/v1/endpoints/290/answer', {}),
                           ('/v1/endpoints/290?extra=true', None), ('/v1/endpoints/290/../291', None)):
            result, count = self.invoke(path, body)
            self.assertFalse(result['ok'])
            self.assertEqual(count, 0)


if __name__ == '__main__':
    unittest.main()
