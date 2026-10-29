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

    def invoke(self, args, *, data=None, calls=(), fail=None, username='222'):
        commands, gateway_requests = [], []
        data = inventory() if data is None else data
        class Gateway:
            def __init__(self, *_): pass
            def request(self, path, body=None):
                gateway_requests.append((path, body))
                return data
        def request(_phone, command):
            commands.append(command)
            if command == 'get sip username': return [username]
            if command == 'get sip register': return ['registered']
            if command == 'status call active': return list(calls)
            if fail: raise ls.LifeSizeError(fail)
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
        for args in (['222', 'dnd', 'on'], ['222', 'image', '/tmp/image.png'], ['222', 'play', 'test']):
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
        self.assertNotIn('auto-answer', output.getvalue())

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

    def test_lock_refuses_parallel_local_commands(self):
        with patch.object(ls.Path, 'home', return_value=self.root), ls.device_lock('192.168.50.20'):
            with self.assertRaises(ls.LifeSizeError):
                with ls.device_lock('192.168.50.20'):
                    self.fail('must not acquire a second lock')


if __name__ == '__main__':
    unittest.main()
