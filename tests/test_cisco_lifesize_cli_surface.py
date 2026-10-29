"""Native LifeSize controls do not change the meaning of Cisco/music commands."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cisco
import cisco_lifesize
from test_cisco_cli import inventory
import test_cisco_lifesize as lifesize_fixture


class LifeSizeCLISurfaceTests(unittest.TestCase):
    def test_native_volume_and_microphone_parser(self):
        args = cisco.parser().parse_args(['222', 'volume', 'ringer', 'set', '7'])
        self.assertEqual((args.context, args.direction, args.value, args.steps), ('ringer', 'set', 7, 1))
        args = cisco.parser().parse_args(['222', 'volume', 'call', 'status'])
        self.assertEqual((args.direction, args.value), ('status', None))
        self.assertEqual(cisco.parser().parse_args(['222', 'mic']).mode, 'status')
        self.assertEqual(cisco.parser().parse_args(['222', 'mic', 'unmute']).mode, 'unmute')
        self.assertEqual(cisco.parser().parse_args(['222', 'automute', 'off']).enabled, 'off')
        self.assertEqual(cisco.parser().parse_args(['222', 'reject']).command, 'reject')
        self.assertNotIn('mic', cisco.MUSIC_COMMANDS)
        self.assertIn('mute', cisco.MUSIC_COMMANDS)

    def test_cisco_volume_never_receives_native_set_or_status(self):
        for suffix in (['status'], ['set', '7'], ['up', '7']):
            with self.subTest(suffix=suffix):
                args = cisco.parser().parse_args(['213', 'volume', 'ringer', *suffix])
                data = inventory()
                phone = data['phones'][0]
                self.assertIn('hardware_volume', phone['capabilities'])
                with self.assertRaises(cisco.Refused) as error:
                    cisco.make_action(args, data, phone)
                self.assertEqual(error.exception.reason, 'absolute_volume_and_readback_require_lifesize')

    def test_cisco_hardware_step_body_remains_unchanged(self):
        args = cisco.parser().parse_args(['213', 'volume', 'ringer', 'up', '--steps', '2'])
        data = inventory()
        action, body = cisco.make_action(args, data, data['phones'][0])
        self.assertEqual(action, 'hardware_volume')
        self.assertEqual({key: value for key, value in body.items() if key != 'idempotency_key'},
                         {'line': '213', 'context': 'ringer', 'direction': 'up', 'steps': 2})

    def test_lifesize_transport_failure_lists_only_native_commands(self):
        with patch.object(cisco, 'load_config', return_value={'lifesize': {'222': {}}}), \
                patch.object(cisco, 'Gateway') as gateway, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            gateway.return_value.request.side_effect = cisco.Refused('synthetic_transport_failure')
            code = cisco.main(['--json', '222'])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(result['offline_commands'], list(cisco_lifesize.AVAILABLE_COMMANDS))
        text = ' '.join(result['offline_commands'])
        for forbidden in ('voice', 'image', 'timer', 'play FILE', 'intercom'):
            self.assertNotIn(forbidden, text)

    def test_native_unknown_mutation_has_same_config_recovery_command(self):
        def dispatch(_name, _args, _devices, _data, remember):
            remember('synthetic-action')
            raise cisco.Refused('lifesize_ssh_outcome_unknown')
        with patch.object(cisco, 'load_config', return_value={'lifesize': {'222': {}}}), \
                patch.object(cisco, 'Gateway') as gateway, \
                patch.object(cisco, 'lifesize_operation', side_effect=dispatch), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            gateway.return_value.request.return_value = inventory()
            code = cisco.main(['--json', '--config', '/tmp/example config.json', '222', 'autoanswer', 'on'])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'not_confirmed')
        self.assertEqual(result['driver'], 'lifesize_ssh')
        self.assertEqual(result['outcome'], 'unknown')
        self.assertIn("'/tmp/example config.json'", result['recovery_command'])
        self.assertTrue(result['recovery_command'].endswith('222 status'))

    def test_status_displays_independent_native_settings(self):
        result = {'driver': 'lifesize_ssh', 'status': 'ok', 'selected_line': '222',
                  'label': 'Conference Phone', 'registration': 'registered', 'calls': [],
                  'dial_allowlist': ['555'], 'available_commands': ['status'],
                  'settings': {'ringer_volume': {'value': 5, 'min': 0, 'max': 10},
                               'call_volume': {'value': 70, 'min': 0, 'max': 100},
                               'autoanswer': False, 'autoanswer_mic_muted': True, 'mic_muted': False}}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            cisco.display(result)
        text = output.getvalue()
        self.assertIn('Ringer volume: 5 (0..10', text)
        self.assertIn('Call speaker volume: 70 (0..100', text)
        self.assertIn('Native auto-answer: off', text)
        self.assertIn('Mute microphone on incoming answer: on', text)
        self.assertIn('Microphone muted: off', text)


class LifeSizeMessageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = lifesize_fixture.LifeSizeTests('test_number_only_is_read_only_with_relevant_examples')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_popup_uses_one_quoted_native_argument_and_redacts_receipt(self):
        code, result, commands, _ = self.fixture.invoke(['222', 'message', 'Dinner is ready'])
        self.assertEqual(code, 0)
        self.assertEqual(commands[-1], 'set system message -t 15 "Notice: Dinner is ready"')
        self.assertEqual(result['duration_seconds'], 15)
        self.assertNotIn('Dinner', json.dumps(result))
        code, result, commands, _ = self.fixture.invoke(['222', 'message', 'Dinner', '--dry-run'])
        self.assertEqual(result['status'], 'dry_run')
        self.assertFalse(self.fixture.writes(commands))

    def test_popup_accepts_bounded_plain_text_file(self):
        path = self.fixture.root / 'message.txt'
        path.write_text('Dinner is ready\n')
        code, _, commands, _ = self.fixture.invoke(['222', 'message', '--text-file', str(path)])
        self.assertEqual(code, 0)
        self.assertEqual(commands[-1], 'set system message -t 15 "Notice: Dinner is ready"')

    def test_ambiguous_popup_text_rejected_before_phone_io(self):
        for value in ('"break out"', 'a\\b', '$(id)', '`id`', 'a;b', 'a\nb', 'Привет', 'a' * 201):
            with self.subTest(value=value):
                code, _, commands, _ = self.fixture.invoke(['222', 'message', value])
                self.assertEqual(code, 1)
                self.assertFalse(commands)

    def test_popup_does_not_cover_an_active_call(self):
        code, result, commands, _ = self.fixture.invoke(['222', 'message', 'Dinner'],
            calls=['12,2,Connected,No,Audio,777,'])
        self.assertEqual(code, 1)
        self.assertEqual(result['reason'], 'lifesize_message_requires_idle_phone')
        self.assertFalse(self.fixture.writes(commands))

    def test_known_async_rows_are_not_mistaken_for_scalar_setting(self):
        phone = cisco_lifesize.Phone(self.fixture.devices['222'], 5)
        response = (b'off\nMS,false\nIC,16,1,Ringback,Audio,600,Example\n'
                    b'IC,17,1,Ring Incoming,Audio,600,Example\nok,00\n')
        with patch.object(cisco_lifesize.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 0, stdout=response, stderr=b'')):
            self.assertFalse(phone.read_setting('audio mute'))
        for row in ('MS,maybe', 'IC,malformed'):
            response = ('off\n' + row + '\nok,00\n').encode()
            with patch.object(cisco_lifesize.subprocess, 'run', return_value=subprocess.CompletedProcess(
                    [], 0, stdout=response, stderr=b'')), self.assertRaises(cisco_lifesize.LifeSizeError):
                phone.read_setting('audio mute')

    def test_phone_452_incoming_row_without_elapsed_time(self):
        row = '21,1,Ring Incoming,Yes,Audio,sip:caller@example.invalid,Caller,No,sip,No'
        calls = cisco_lifesize.parse_calls([row])
        self.assertEqual(calls[0]['state'], 'Ring Incoming')
        self.assertTrue(calls[0]['incoming'])
        code, result, commands, _ = self.fixture.invoke(['222', 'answer'], calls=[row])
        self.assertEqual(code, 0)
        self.assertEqual(result['call_id'], '21')
        self.assertEqual(commands[-1], 'control call answer -t audio 21')
        for bad in (row.replace(',No,sip,No', ',Maybe,sip,No'), row + ',bad-duration'):
            with self.assertRaises(cisco_lifesize.LifeSizeError):
                cisco_lifesize.parse_calls([bad])


if __name__ == '__main__':
    unittest.main()
