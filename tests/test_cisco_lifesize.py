import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cisco
import cisco_lifesize as ls


def inventory():
    return {'ami_fresh': True, 'observed_at': time.time(),
            'dial_allowlist': ['555', '777'], 'ring_allowlist': [],
            'phones': [{'id': '212', 'lines': ['212', '213'], 'online': True,
                        'dnd': False, 'dnd_pending': False, 'autoanswer': False,
                        'channels': [], 'capabilities': ['dial'], 'alert': None}]}


class LifeSizeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('key', 'known_hosts'):
            (self.root / name).write_text('synthetic fixture')
            (self.root / name).chmod(0o600)
        self.device = {'host': '192.168.50.20', 'identity_file': 'key',
                       'known_hosts_file': 'known_hosts', 'legacy_rsa': True,
                       'label': 'Conference Phone'}
        self.devices = ls.validate_config({'222': self.device}, self.root / 'cli.json')

    def invoke(self, args, *, data=None, calls=(), fail=None, username='222',
               settings=None, responses=None, persist=True):
        commands, gateway_requests = [], []
        data = inventory() if data is None else data
        current = {'volume ring-tone': '5', 'volume speaker': '70', 'audio mute': 'off',
                   'call auto-answer': 'off', 'call auto-mute': 'on', 'call do-not-disturb': 'off'}
        current.update(settings or {})
        responses = dict(responses or {})
        class Gateway:
            def __init__(self, *_): pass
            def request(self, path, body=None):
                gateway_requests.append((path, body))
                return data
        def request(_phone, command):
            commands.append(command)
            if command in responses:
                response = responses[command]
                return response(commands.count(command)) if callable(response) else response
            if command == 'get sip username': return [username]
            if command == 'get sip register': return ['registered']
            if command == 'status call active': return list(calls)
            if command.startswith('get ') and command[4:] in current:
                return [current[command[4:]]]
            if fail: raise ls.LifeSizeError(fail)
            if command.startswith('set ') and persist:
                name, value = command[4:].rsplit(' ', 1)
                current[name] = value
            return []
        with patch.object(cisco, 'load_config', return_value={'lifesize': self.devices}), \
                patch.object(cisco, 'Gateway', Gateway), patch.object(ls.Phone, 'request', request), \
                patch.object(ls.Path, 'home', return_value=self.root), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = cisco.main(['--json', *args])
        return code, json.loads(output.getvalue()), commands, gateway_requests

    def test_shorthand_calls_selected_native_phone_not_cisco_gateway(self):
        code, result, commands, requests = self.invoke(['222', '555'])
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['selected_line'], '222')
        self.assertEqual(commands[-1], 'control call dial -p sip 555')
        self.assertEqual(requests, [('/v1/phones', None)])

    def test_number_only_is_read_only_with_relevant_examples(self):
        code, result, commands, _ = self.invoke(['222'])
        self.assertEqual(code, 0)
        self.assertTrue(result['registered'])
        self.assertEqual(result['calls'], [])
        self.assertIn('hangup', result['available_commands'])
        self.assertNotIn('image', ' '.join(result['available_commands']))
        self.assertFalse(any(c.startswith('control') for c in commands))

    def test_busy_phone_unallowed_destinations_and_wrong_line_do_not_dial(self):
        variants = [(['222', '555'], {'calls': ['12,2,Connected,No,Audio,777,']}),
                    (['222', '999'], {}), (['222', '555'], {'username': '223'}),
                    (['222', 'dial', '555;reboot'], {}),
                    (['222', 'dial', '555', '--idempotency-key', 'my-key'], {})]
        for args, kwargs in variants:
            with self.subTest(args=args, kwargs=kwargs):
                code, result, commands, _ = self.invoke(args, **kwargs)
                self.assertEqual(code, 1)
                self.assertEqual(result['status'], 'refused')
                self.assertFalse(any(c.startswith('control') for c in commands))

    def test_dry_run_makes_no_mutation(self):
        code, result, commands, _ = self.invoke(['222', '777', '--dry-run'])
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'dry_run')
        self.assertEqual(result['number'], '777')
        self.assertFalse(any(c.startswith('control') for c in commands))

    def test_hangup_targets_exact_observed_call_never_all(self):
        call = '12,2,Connected,No,Audio,sip:777,Example,No,sip,No,0:00:02'
        code, result, commands, _ = self.invoke(['222', 'hangup'], calls=[call])
        self.assertEqual(code, 0)
        self.assertEqual(commands[-1], 'control call hangup 12')
        self.assertEqual(result['call_id'], '12')
        for calls in ([], [call, '13,3,Ringing,Yes,Audio,212,']):
            self.assertEqual(self.invoke(['222', 'hangup'], calls=calls)[0], 1)

    def test_lost_mutation_response_is_not_confirmed_and_never_retried(self):
        code, result, commands, _ = self.invoke(['222', '555'], fail='lifesize_ssh_outcome_unknown')
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'not_confirmed')
        self.assertIn('action_id', result)
        self.assertEqual(commands.count('control call dial -p sip 555'), 1)

    def test_unsupported_cisco_controls_do_not_touch_native_phone(self):
        for args in (['222', 'speaker', 'toggle'], ['222', 'image', '/tmp/image.png'],
                     ['222', 'play', 'test'], ['222', 'mute'], ['222', 'timer', 'start', '1']):
            code, result, commands, _ = self.invoke(args)
            self.assertEqual(code, 1)
            self.assertEqual(result['reason'], 'command_not_supported_by_lifesize')
            self.assertEqual(commands, [])

    def test_directory_adds_native_phone_without_claiming_unobserved_reachability(self):
        for args in (['directory'], ['222', 'directory']):
            code, result, commands, _ = self.invoke(args)
            self.assertEqual(code, 0)
            self.assertEqual(commands, [])
            phone = next(p for p in result['directory'] if p['id'] == '222')
            self.assertEqual(phone['type'], 'lifesize')
            self.assertIsNone(phone['online'])
            self.assertEqual(phone['selected'], args[0] == '222')
            self.assertNotIn(self.device['host'], json.dumps(result))
            self.assertNotIn('known_hosts', json.dumps(result))

    def test_duplicate_cisco_mapping_and_stale_inventory_refused(self):
        data = inventory(); data['phones'][0]['lines'] = ['212', '222']
        self.assertEqual(self.invoke(['222', '555'], data=data)[0], 1)
        data = inventory(); data['observed_at'] -= 100
        self.assertEqual(self.invoke(['222', '555'], data=data)[0], 1)

    def test_config_validates_hosts_aliases_keys_and_duplicate_devices(self):
        self.assertEqual(self.devices['222']['identity_file'], str(self.root / 'key'))
        variants = [{'host': '8.8.8.8'}, {'host': '-oProxyCommand=x'}, {'host_key_alias': 'x\ny'},
                    {'identity_file': '\x00x'}, {'legacy_rsa': 1}, {'command': 'reboot'}]
        for change in variants:
            device = self.device | change
            with self.subTest(change=change), self.assertRaises(ls.LifeSizeError):
                ls.validate_config({'222': device}, self.root / 'cli.json')
        with self.assertRaises(ls.LifeSizeError):
            ls.validate_config({'222': self.device, '223': self.device}, self.root / 'cli.json')

    def test_ssh_pins_identity_and_host_key_without_global_config_or_password(self):
        phone = ls.Phone(self.devices['222'], 5)
        with patch.object(ls.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 0, stdout=b'registered\n\nok,00\n', stderr=b'')) as run:
            self.assertEqual(phone.request('get sip register'), ['registered'])
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ['ssh', '-F', '/dev/null'])
        self.assertIn('StrictHostKeyChecking=yes', argv)
        self.assertIn('IdentityAgent=none', argv)
        self.assertIn('HostKeyAlgorithms=+ssh-rsa', argv)
        self.assertNotIn('shell', run.call_args.kwargs)
        self.assertEqual(argv[-1], 'get sip register')

    def test_private_cli_config_loads_native_fragment_and_human_status(self):
        path = self.root / 'cli.json'
        path.write_text(json.dumps({'gateway_url': 'http://192.168.50.1:8080',
                                    'token': 'x' * 40, 'lifesize': {'222': self.device}}))
        path.chmod(0o600)
        config = cisco.load_config(path)
        self.assertEqual(config['lifesize'], self.devices)
        _, result, _, _ = self.invoke(['222'])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            cisco.display(result)
        self.assertIn('cisco 222 555', output.getvalue())
        self.assertIn('cisco 222 777', output.getvalue())
        self.assertIn('cisco 222 autoanswer', output.getvalue())

    def test_call_parser_rejects_malformed_or_duplicate_call_identity(self):
        for rows in (['unexpected'], ['1;reboot,2,Connected,No,Audio,777,'],
                     ['1,2,Connected,No,Audio,777,'] * 2):
            with self.subTest(rows=rows), self.assertRaises(ls.LifeSizeError):
                ls.parse_calls(rows)

    def test_phone_452_extended_status_has_same_exact_call_identity(self):
        calls = ls.parse_calls(['7,1,Connected,No,Audio,sip:600,600,No,sip,No,0:00:02'])
        self.assertEqual(calls, [{'id': '7', 'state': 'Connected', 'incoming': False,
                                 'number': 'sip:600'}])

    def test_ssh_errors_do_not_leak_remote_output(self):
        phone = ls.Phone(self.devices['222'], 5)
        for response in (subprocess.CompletedProcess([], 255, stdout=b'private', stderr=b'secret'),
                         subprocess.CompletedProcess([], 0, stdout=b'error,08\n', stderr=b'')):
            with patch.object(ls.subprocess, 'run', return_value=response), self.assertRaises(ls.LifeSizeError):
                phone.request('get sip register')

    @staticmethod
    def writes(commands):
        return [c for c in commands if c.startswith(('set ', 'control '))]

    def test_all_settings_status_commands_are_read_only_during_calls(self):
        for suffix in ([], ['status'], ['destinations']):
            with self.subTest(suffix=suffix):
                code, result, commands, _ = self.invoke(['222', *suffix],
                    calls=['12,2,Connected,No,Audio,777,'])
                self.assertEqual(code, 0)
                self.assertEqual(result['settings']['ringer_volume']['value'], 5)
                self.assertEqual(result['settings']['call_volume']['max'], 100)
                self.assertTrue(result['settings']['autoanswer_mic_muted'])
                self.assertFalse(result['settings']['autoanswer'])
                self.assertFalse(result['settings']['dnd'])
                self.assertFalse(result['settings']['mic_muted'])
                self.assertIn('microphone', result['capabilities'])
                self.assertEqual(self.writes(commands), [])

    def test_targeted_settings_read_only_the_requested_native_setting(self):
        for suffix, native, key in (
                (['volume', 'ringer', 'status'], 'volume ring-tone', 'ringer_volume'),
                (['volume', 'call', 'status'], 'volume speaker', 'call_volume'),
                (['autoanswer'], 'call auto-answer', 'autoanswer'),
                (['automute'], 'call auto-mute', 'autoanswer_mic_muted'),
                (['dnd', 'status'], 'call do-not-disturb', 'dnd'),
                (['mic', 'status'], 'audio mute', 'mic_muted')):
            with self.subTest(suffix=suffix):
                code, result, commands, _ = self.invoke(['222', *suffix],
                    calls=['12,2,Connected,No,Audio,777,'])
                self.assertEqual(code, 0)
                self.assertEqual(list(result['settings']), [key])
                self.assertEqual(commands, ['get sip username', 'get sip register',
                                             'status call active', 'get ' + native])
                self.assertEqual(self.writes(commands), [])

    def test_autoanswer_dnd_and_automute_write_only_their_setting_and_read_back(self):
        for suffix, native, key, expected in (
                (['autoanswer', 'on'], 'call auto-answer', 'autoanswer', True),
                (['automute', 'off'], 'call auto-mute', 'autoanswer_mic_muted', False),
                (['dnd', 'on'], 'call do-not-disturb', 'dnd', True)):
            with self.subTest(suffix=suffix):
                code, result, commands, requests = self.invoke(['222', *suffix],
                    calls=['12,2,Connected,No,Audio,777,'] if key == 'dnd' else [])
                self.assertEqual(code, 0)
                self.assertTrue(result['confirmed'])
                self.assertEqual(result['settings'][key], expected)
                self.assertEqual(self.writes(commands), ['set ' + native + ' ' + suffix[-1]])
                self.assertEqual(commands[-1], 'status call active' if key == 'dnd' else 'get ' + native)
                self.assertEqual(requests, [('/v1/phones', None)])
                self.assertEqual(list(result['settings']), [key])
                self.assertEqual([c for c in commands if c.startswith('get ') and c[4:] in (
                    name for name, _ in ls.SETTINGS.values())], ['get ' + native] * 3)

    def test_relative_native_volume_clamps_at_real_limits(self):
        for context, direction, start, expected, native, calls in (
                ('ringer', 'up', '9', 10, 'volume ring-tone', []),
                ('ringer', 'down', '1', 0, 'volume ring-tone', []),
                ('call', 'up', '99', 100, 'volume speaker', ['12,2,Connected,No,Audio,777,']),
                ('call', 'down', '1', 0, 'volume speaker', ['12,2,Connected,No,Audio,777,'])):
            with self.subTest(context=context, direction=direction):
                code, result, commands, _ = self.invoke(
                    ['222', 'volume', context, direction, '--steps', '3'],
                    settings={native: start}, calls=calls)
                self.assertEqual(code, 0)
                self.assertEqual(result['value'], expected)
                self.assertEqual(result['settings'][context + '_volume']['value'], expected)
                self.assertEqual(self.writes(commands), [f'set {native} {expected}'])

    def test_absolute_volume_and_mic_target_one_connected_call(self):
        call = '12,2,Connected,No,Audio,777,'
        for suffix, expected in ((['volume', 'call', 'set', '42'], 'set volume speaker 42'),
                                 (['mic', 'mute'], 'set audio mute on')):
            with self.subTest(suffix=suffix):
                code, result, commands, _ = self.invoke(['222', *suffix], calls=[call])
                self.assertEqual(code, 0)
                self.assertEqual(result['call_id'], '12')
                self.assertEqual(self.writes(commands), [expected])
                self.assertTrue(result['confirmed'])
        code, result, commands, _ = self.invoke(['222', 'mic', 'unmute'], calls=[call],
                                               settings={'audio mute': 'on'})
        self.assertEqual(code, 0)
        self.assertEqual(self.writes(commands), ['set audio mute off'])

    def test_changed_settings_require_the_correct_call_context(self):
        active = ['12,2,Connected,No,Audio,777,']
        for suffix in (['volume', 'ringer', 'set', '8'], ['autoanswer', 'on'],
                       ['automute', 'off']):
            code, result, commands, _ = self.invoke(['222', *suffix], calls=active)
            self.assertEqual(code, 1)
            self.assertEqual(result['reason'], 'lifesize_setting_requires_idle_phone')
            self.assertEqual(self.writes(commands), [])
        for calls in ([], ['12,2,Ringing,Yes,Audio,777,'],
                      active + ['13,3,Connected,No,Audio,555,']):
            for suffix in (['volume', 'call', 'up'], ['mic', 'mute'], ['dnd', 'on']):
                code, result, commands, _ = self.invoke(['222', *suffix], calls=calls)
                self.assertEqual(code, 1)
                self.assertEqual(result['reason'], 'lifesize_requires_one_connected_call')
                self.assertEqual(self.writes(commands), [])

    def test_noop_and_dry_run_settings_never_write(self):
        for suffix in (['volume', 'call', 'set', '70'], ['autoanswer', 'off'],
                       ['automute', 'on'], ['dnd', 'off'], ['mic', 'unmute']):
            code, result, commands, _ = self.invoke(['222', *suffix],
                                                   calls=['12,2,Ringing,Yes,Audio,777,'])
            self.assertEqual(code, 0)
            self.assertEqual(result['status'], 'ok')
            self.assertFalse(result['changed'])
            self.assertIn('registration', result)
            self.assertNotIn('action_id', result)
            self.assertEqual(self.writes(commands), [])
        for suffix in (['volume', 'ringer', 'set', '9'], ['autoanswer', 'on'],
                       ['automute', 'off'], ['dnd', 'on']):
            code, result, commands, _ = self.invoke(['222', *suffix, '--dry-run'],
                calls=['12,2,Connected,No,Audio,777,'] if suffix[0] == 'dnd' else [])
            self.assertEqual(code, 0)
            self.assertEqual(result['status'], 'dry_run')
            self.assertEqual(self.writes(commands), [])

    def test_dnd_change_requires_connected_call_in_both_directions(self):
        for value, initial in (('on', 'off'), ('off', 'on')):
            for calls in ([], ['12,2,Ringback,Yes,Audio,777,']):
                code, result, commands, _ = self.invoke(['222', 'dnd', value], calls=calls,
                    settings={'call do-not-disturb': initial})
                self.assertEqual(code, 1)
                self.assertEqual(result['reason'], 'lifesize_requires_one_connected_call')
                self.assertEqual(self.writes(commands), [])
            code, result, commands, _ = self.invoke(['222', 'dnd', value],
                calls=['12,2,Connected,No,Audio,777,'], settings={'call do-not-disturb': initial})
            self.assertEqual(code, 0)
            self.assertEqual(result['call_id'], '12')
            self.assertEqual(result['settings']['dnd'], value == 'on')
            self.assertEqual(self.writes(commands), ['set call do-not-disturb ' + value])

    def test_invalid_volume_arguments_are_rejected_before_phone_io(self):
        for suffix in (['ringer', 'set', '11'], ['call', 'set', '101'], ['call', 'set', '-1'],
                       ['ringer', 'set'], ['ringer', 'up', '5'],
                       ['ringer', 'set', '5', '--steps', '2'], ['call', 'status', '--steps', '2']):
            code, result, commands, _ = self.invoke(['222', 'volume', *suffix])
            self.assertEqual(code, 1)
            self.assertEqual(result['status'], 'refused')
            self.assertEqual(commands, [])

    def test_invalid_native_setting_response_prevents_all_mutation(self):
        for native, rows, suffix in (
                ('volume ring-tone', ['11'], ['volume', 'ringer', 'up']),
                ('volume speaker', ['-1'], ['volume', 'call', 'up']),
                ('volume speaker', ['70', '71'], ['volume', 'call', 'up']),
                ('audio mute', ['OFF'], ['mic', 'mute']),
                ('call auto-answer', ['maybe'], ['autoanswer', 'on']),
                ('call auto-mute', [], ['automute', 'off']),
                ('call do-not-disturb', ['on\nreboot'], ['dnd', 'on'])):
            code, result, commands, _ = self.invoke(['222', *suffix],
                                                   responses={'get ' + native: rows})
            self.assertEqual(code, 1)
            self.assertEqual(result['reason'], 'invalid_lifesize_setting')
            self.assertEqual(self.writes(commands), [])

    def test_answer_pickup_and_reject_use_exact_incoming_audio_call(self):
        incoming = '12,2,Ringing,Yes,Audio,777,'
        for command, native in (('answer', 'control call answer -t audio 12'),
                                ('pickup', 'control call answer -t audio 12'),
                                ('reject', 'control call answer -r 12')):
            for state in ('Ringing', 'Ringback', 'Ring Incoming'):
                code, result, commands, _ = self.invoke(['222', command],
                    calls=[f'12,2,{state},Yes,Audio,777,'])
                self.assertEqual(code, 0)
                self.assertEqual(self.writes(commands), [native])
            for calls in ([], ['12,2,Ringing,No,Audio,777,'], ['12,2,Ringback,No,Audio,777,'],
                          ['12,2,Ring Incoming,No,Audio,777,'],
                          ['12,2,Connected,Yes,Audio,777,'], [incoming, '13,3,Ringing,Yes,Audio,555,']):
                code, result, commands, _ = self.invoke(['222', command], calls=calls)
                self.assertEqual(code, 1)
                self.assertEqual(self.writes(commands), [])

    def test_call_and_setting_races_refuse_before_dispatch(self):
        first = ['12,2,Connected,No,Audio,777,']
        for changed in ([], ['13,2,Connected,No,Audio,777,'], ['12,2,Connected,Yes,Audio,777,'],
                        ['12,2,Connected,No,Audio,555,'], ['12,2,Ringing,No,Audio,777,']):
            code, result, commands, _ = self.invoke(['222', 'volume', 'call', 'up'], calls=first,
                responses={'status call active': lambda n, changed=changed: first if n == 1 else changed})
            self.assertEqual(code, 1)
            self.assertEqual(result['reason'], 'lifesize_call_context_changed')
            self.assertNotIn('action_id', result)
            self.assertEqual(self.writes(commands), [])
        code, result, commands, _ = self.invoke(['222', 'volume', 'ringer', 'up'],
            responses={'get volume ring-tone': lambda n: ['5' if n == 1 else '6']})
        self.assertEqual(code, 1)
        self.assertEqual(result['reason'], 'lifesize_setting_changed')
        self.assertEqual(self.writes(commands), [])

    def test_unconfirmed_setting_readback_or_write_is_never_retried(self):
        for kwargs in ({'persist': False}, {'fail': 'lifesize_ssh_outcome_unknown'}):
            code, result, commands, _ = self.invoke(['222', 'autoanswer', 'on'], **kwargs)
            self.assertEqual(code, 1)
            self.assertEqual(result['status'], 'not_confirmed')
            self.assertIn('action_id', result)
            self.assertEqual(self.writes(commands), ['set call auto-answer on'])

    def test_call_controls_and_messages_do_not_depend_on_unrelated_settings(self):
        broken = {'get ' + native: ['invalid'] for native, _ in ls.SETTINGS.values()}
        for suffix, calls in (
                (['555'], []), (['hangup'], ['12,2,Connected,No,Audio,777,']),
                (['answer'], ['12,2,Ringing,Yes,Audio,777,']),
                (['pickup'], ['12,2,Ringing,Yes,Audio,777,']),
                (['reject'], ['12,2,Ringing,Yes,Audio,777,']),
                (['message', 'Test'], [])):
            with self.subTest(suffix=suffix):
                code, result, commands, _ = self.invoke(['222', *suffix], calls=calls, responses=broken)
                self.assertEqual(code, 0)
                self.assertEqual(result['status'], 'accepted')
                self.assertFalse(set(commands) & set(broken))
                self.assertEqual(len(commands), 5)

    def test_disabling_autoanswer_ignores_broken_automute_getter(self):
        code, result, commands, _ = self.invoke(['222', 'autoanswer', 'off'],
            settings={'call auto-answer': 'on'}, responses={'get call auto-mute': ['invalid']})
        self.assertEqual(code, 0)
        self.assertFalse(result['settings']['autoanswer'])
        self.assertNotIn('get call auto-mute', commands)
        self.assertEqual(self.writes(commands), ['set call auto-answer off'])

    def test_call_change_during_setting_dispatch_is_not_confirmed_and_not_rolled_back(self):
        first = ['12,2,Connected,No,Audio,777,']
        for suffix, expected in ((['volume', 'call', 'up'], 'set volume speaker 71'),
                                 (['mic', 'mute'], 'set audio mute on'),
                                 (['dnd', 'on'], 'set call do-not-disturb on')):
            for final in ([], ['13,3,Connected,Yes,Audio,555,']):
                with self.subTest(suffix=suffix, final=final):
                    code, result, commands, _ = self.invoke(['222', *suffix], calls=first,
                        responses={'status call active': lambda n, final=final: first if n < 3 else final})
                    self.assertEqual(code, 1)
                    self.assertEqual(result['status'], 'not_confirmed')
                    self.assertEqual(result['reason'], 'lifesize_call_context_changed_after_setting')
                    self.assertIn('action_id', result)
                    self.assertEqual(self.writes(commands), [expected])

    def test_lock_refuses_parallel_local_commands(self):
        with patch.object(ls.Path, 'home', return_value=self.root), ls.device_lock('192.168.50.20'):
            with self.assertRaises(ls.LifeSizeError):
                with ls.device_lock('192.168.50.20'):
                    self.fail('must not acquire a second lock')


if __name__ == '__main__':
    unittest.main()
