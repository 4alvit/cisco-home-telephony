"""CLI hardware-volume requests retain exact physical-phone/call scope."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from test_cisco_cli import cisco, inventory, channel


class HardwareVolumeCLITests(unittest.TestCase):
    def invoke(self, args, data=None, response_override=None):
        data = inventory() if data is None else data
        calls = []

        class Client:
            def __init__(self, *_): pass

            def request(self, path, body=None):
                calls.append((path, copy.deepcopy(body)))
                if body is None:
                    return data
                result = dict(status='accepted', action_id=body['idempotency_key'],
                              context=body['context'], direction=body['direction'],
                              requested_steps=body['steps'], acknowledged_steps=body['steps'],
                              level_known=False, volume_level=None, audio_output='contextual',
                              physical_context_verified=False)
                result.update(response_override or {})
                return result

        output = io.StringIO()
        with patch.object(cisco, 'Gateway', Client), patch.object(cisco, 'load_config', return_value={}), \
                contextlib.redirect_stdout(output):
            code = cisco.main(['--json', *args])
        return code, json.loads(output.getvalue()), calls

    def test_secondary_line_ringer_maps_to_physical_phone(self):
        code, result, calls = self.invoke(['213', 'volume', 'ringer', 'up', '--steps', '3'])
        self.assertEqual(code, 0)
        path, body = calls[-1]
        self.assertEqual(path, '/v1/phones/212/hardware_volume')
        self.assertEqual({k: v for k, v in body.items() if k != 'idempotency_key'},
                         dict(line='213', context='ringer', direction='up', steps=3))
        self.assertIsNone(result['volume_level'])
        self.assertFalse(result['physical_context_verified'])

    def test_call_pins_exact_selected_line_and_defaults_to_one_step(self):
        data = inventory()
        data['phones'][0]['channels'] = [channel()]
        code, _, calls = self.invoke(['213', 'volume', 'call', 'down'], data)
        self.assertEqual(code, 0)
        body = calls[-1][1]
        self.assertEqual((body['channel'], body['uniqueid'], body['steps']),
                         ('PJSIP/213-000000ab', '1234567890.21', 1))

    def test_call_refuses_absent_sibling_ringing_or_ambiguous_calls(self):
        for channels in ([], [channel('212')], [channel(state='Ringing')],
                         [channel(), channel('212')]):
            with self.subTest(channels=channels):
                data = inventory()
                data['phones'][0]['channels'] = channels
                code, _, calls = self.invoke(['213', 'volume', 'call', 'up'], data)
                self.assertEqual(code, 1)
                self.assertEqual(len(calls), 1)

    def test_ringer_refuses_any_call_offline_or_active_alert(self):
        for extra in ({'channels': [channel('212')]}, {'online': False},
                      {'alert': dict(id='a'*32, line='212', kind='timer', state='ringing')}):
            with self.subTest(extra=extra):
                data = inventory()
                data['phones'][0].update(extra)
                code, _, calls = self.invoke(['213', 'volume', 'ringer', 'down'], data)
                self.assertEqual(code, 1)
                self.assertEqual(len(calls), 1)

    def test_old_gateway_is_not_given_new_action(self):
        data = inventory()
        data['phones'][0]['capabilities'].remove('hardware_volume')
        code, result, calls = self.invoke(['213', 'volume', 'ringer', 'up'], data)
        self.assertEqual(code, 1)
        self.assertEqual(result['reason'], 'command_not_supported_by_gateway')
        self.assertEqual(len(calls), 1)

    def test_dry_run_and_status_do_not_send_keys(self):
        code, result, calls = self.invoke(['--dry-run', '213', 'volume', 'ringer', 'up'])
        self.assertEqual((code, result['status'], len(calls)), (0, 'dry_run', 1))
        _, result, calls = self.invoke(['213'])
        self.assertIn('volume ringer|call up|down [--steps 1..5]', result['available_commands'])
        self.assertEqual(len(calls), 1)

    def test_bad_acknowledgement_is_unknown_and_never_retried(self):
        for change in ({'acknowledged_steps': 0}, {'acknowledged_steps': True},
                       {'context': 'call'}, {'level_known': True}, {'volume_level': .5},
                       {'physical_context_verified': True}, {'audio_output': 'speaker'}):
            with self.subTest(change=change):
                code, result, calls = self.invoke(['213', 'volume', 'ringer', 'up'],
                                                  response_override=change)
                self.assertEqual(code, 1)
                self.assertEqual(result['status'], 'not_confirmed')
                self.assertEqual(len(calls), 2)

    def test_invalid_steps_or_output_picker_rejected_locally(self):
        for tail in (['ringer', 'up', '--steps', '0'], ['ringer', 'up', '--steps', '6'],
                     ['speaker', 'up'], ['call', 'up', '--steps', '1.5']):
            with self.subTest(tail=tail), patch.object(cisco, 'Gateway') as client, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cisco.main(['213', 'volume', *tail])
            client.assert_not_called()
