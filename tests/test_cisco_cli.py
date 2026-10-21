import contextlib
import copy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
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
import cisco_http


TOKEN = 'synthetic-test-token-' + 'x' * 32


def inventory():
    return {'ami_fresh': True, 'observed_at': time.time(),
            'dial_allowlist': ['209', '600', '630', '701'], 'ring_allowlist': ['640'],
            'phones': [{'id': '212', 'lines': ['212', '213'], 'online': True,
                        'dnd': False, 'autoanswer': False, 'dnd_pending': False,
                        'channels': [], 'alert': None,
                        'capabilities': list(cisco.COMMANDS)}]}


def channel(line='213', state='Up'):
    return {'line': line, 'name': 'PJSIP/' + line + '-000000ab',
            'uniqueid': '1234567890.21', 'state': state}


class CLITests(unittest.TestCase):
    def invoke(self, arguments, data=None, error=None):
        data = inventory() if data is None else data
        calls = []

        class FakeGateway:
            def __init__(self, *_): pass
            def request(self, path, body=None):
                calls.append((path, copy.deepcopy(body)))
                if body is None:
                    return data
                if error:
                    raise error
                return {'status': 'accepted', 'action_id': body['idempotency_key']}

        output, errors = io.StringIO(), io.StringIO()
        with patch.object(cisco, 'Gateway', FakeGateway), patch.object(cisco, 'load_config', return_value={}), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cisco.main(['--json', *arguments])
        self.assertEqual(errors.getvalue(), '')
        return code, json.loads(output.getvalue()), calls

    def test_default_is_read_only_status_with_commands(self):
        code, result, calls = self.invoke(['213'])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [('/v1/phones', None)])
        self.assertEqual((result['selected_line'], result['line_button'], result['phone']['id']),
                         ('213', 2, '212'))
        self.assertIn('dial NUMBER', result['available_commands'])
        self.assertNotIn('registered', result['phone'])

    def test_secondary_dial_routes_host_with_exact_selected_line(self):
        code, result, calls = self.invoke(['--idempotency-key', 'my-call-123', '213', 'dial', '600'])
        self.assertEqual(code, 0)
        self.assertEqual(calls[1], ('/v1/phones/212/dial', {
            'line': '213', 'number': '600', 'idempotency_key': 'my-call-123'}))
        self.assertEqual(result['status'], 'accepted')
        self.assertNotIn('connected', result)

    def test_mapping_uses_membership_not_parity(self):
        data = inventory(); data['phones'][0].update(id='218', lines=['218', '220'])
        code, _, calls = self.invoke(['220', 'dial', '209'], data)
        self.assertEqual(code, 0)
        self.assertEqual(calls[1][0], '/v1/phones/218/dial')
        self.assertEqual(calls[1][1]['line'], '220')

    def test_numeric_shorthand_with_global_options(self):
        for args in (['213', '600'], ['--timeout', '5', '213', '600'],
                     ['213', '--timeout=5', '600', '--json'],
                     ['--config', '123', '213', '600', '--idempotency-key', 'test-call-123']):
            with self.subTest(args=args):
                code, result, calls = self.invoke(args)
                self.assertEqual(code, 0)
                self.assertEqual(calls[1][0], '/v1/phones/212/dial')
                self.assertEqual(calls[1][1]['line'], '213')
                self.assertEqual(calls[1][1]['number'], '600')
                self.assertEqual(result['status'], 'accepted')
        code, _, calls = self.invoke(['--dry-run', '213', '600'])
        self.assertEqual(code, 0); self.assertEqual(len(calls), 1)

    def test_numeric_shorthand_does_not_rewrite_explicit_command_arguments(self):
        args = ['213', 'message', '600']
        self.assertEqual(cisco.normalize_shorthand(args), args)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cisco.parser().parse_args(cisco.normalize_shorthand(['213', '60x']))

    def test_device_wide_actions_target_the_same_host(self):
        for command in (['dnd', 'on'], ['autoanswer', 'off'], ['speaker', 'toggle']):
            with self.subTest(command=command):
                code, _, calls = self.invoke(['213', *command])
                self.assertEqual(code, 0)
                self.assertEqual(calls[1][0], '/v1/phones/212/' + command[0])
                self.assertEqual(calls[1][1]['line'], '213')
        _, _, calls = self.invoke(['213', 'dnd'])
        self.assertEqual(len(calls), 1)

    def test_ring_and_alert_preserve_secondary_line(self):
        for command in (['ring'], ['alert', 'timer'], ['alert', 'alarm']):
            code, _, calls = self.invoke(['213', *command])
            self.assertEqual(code, 0)
            self.assertEqual(calls[1][1]['line'], '213')
            if command[0] == 'ring':
                self.assertEqual(calls[1][1]['destination'], '640')

    def test_fresh_exact_channel_identity_is_submitted(self):
        data = inventory(); data['phones'][0]['channels'] = [channel(state='Ringing')]
        for action in ('answer', 'hangup'):
            code, _, calls = self.invoke(['213', action], copy.deepcopy(data))
            self.assertEqual(code, 0)
            self.assertEqual(calls[1][1]['channel'], channel()['name'])
            self.assertEqual(calls[1][1]['uniqueid'], channel()['uniqueid'])

    def test_pickup_alias_only_answers_a_real_ringing_call(self):
        data = inventory(); data['phones'][0]['channels'] = [channel(state='Ringing')]
        code, result, calls = self.invoke(['213', 'pickup'], data)
        self.assertEqual(code, 0); self.assertEqual(result['action'], 'answer')
        self.assertEqual(calls[1][0], '/v1/phones/212/answer')
        self.assertEqual(calls[1][1]['line'], '213')
        code, result, calls = self.invoke(['213', 'pickup'])
        self.assertEqual(code, 1); self.assertEqual(len(calls), 1)
        self.assertIn('speaker_toggle_explicitly', result['reason'])
        data['phones'][0]['channels'][0]['state'] = 'Up'
        self.assertEqual(self.invoke(['213', 'pickup'], data)[0], 1)

    def test_sibling_or_changed_state_cannot_hangup_or_answer(self):
        for channels, action in (([channel('212')], 'hangup'),
                                 ([channel(), channel('212')], 'hangup'),
                                 ([channel()], 'answer')):
            data = inventory(); data['phones'][0]['channels'] = channels
            code, _, calls = self.invoke(['213', action], data)
            self.assertEqual(code, 1)
            self.assertEqual(len(calls), 1)

    def test_stop_alert_requires_exact_line_and_current_identity(self):
        data = inventory(); data['phones'][0]['alert'] = {
            'id': 'a' * 32, 'line': '213', 'kind': 'timer', 'state': 'ringing'}
        code, _, calls = self.invoke(['213', 'stop-alert', '--alert-id', 'a' * 32], data)
        self.assertEqual(code, 0)
        self.assertEqual(calls[1][0], '/v1/phones/212/alert_stop')
        for arguments in (['212', 'stop-alert'], ['213', 'stop-alert', '--alert-id', 'b' * 32]):
            code, _, calls = self.invoke(arguments, data)
            self.assertEqual(code, 1); self.assertEqual(len(calls), 1)

    def test_unlisted_destination_and_softphone_are_refused(self):
        for args in (['213', 'dial', '911'], ['213', 'dial', 'sip:bad@example.net'],
                     ['209'], ['213', 'ring', '701']):
            code, _, calls = self.invoke(args)
            self.assertEqual(code, 1); self.assertEqual(len(calls), 1)

    def test_stale_ami_or_duplicate_mapping_refuses_before_post(self):
        variants = []
        value = inventory(); value['ami_fresh'] = False; variants.append(value)
        value = inventory(); value['observed_at'] -= 100; variants.append(value)
        value = inventory(); value['phones'].append(copy.deepcopy(value['phones'][0])); variants.append(value)
        value = inventory(); value['phones'][0]['channels'] = [channel()]
        value['phones'][0]['channels'][0]['name'] = 'PJSIP/212-000000ab'; variants.append(value)
        for data in variants:
            code, _, calls = self.invoke(['213', 'dial', '600'], data)
            self.assertEqual(code, 1); self.assertEqual(len(calls), 1)

    def test_inventory_timestamp_numeric_boundaries(self):
        now = 1800000000
        cases = (
            ('fresh_integer', now, True), ('fresh_float', now + 0.5, True),
            ('oldest_boundary', now - 90, True), ('future_boundary', now + 5, True),
            ('too_old', now - 90.001, False), ('too_future', now + 5.001, False),
            ('true_boolean', True, False), ('false_boolean', False, False),
            ('nan', float('nan'), False), ('positive_infinity', float('inf'), False),
            ('negative_infinity', float('-inf'), False),
            ('huge_positive', 10 ** 400, False), ('huge_negative', -(10 ** 400), False),
        )
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(cisco.time, 'time', return_value=now) as clock, \
                patch('socket.socket', side_effect=AssertionError('network forbidden')) as sockets, \
                patch('socket.create_connection', side_effect=AssertionError('network forbidden')) as connections, \
                patch('subprocess.Popen', side_effect=AssertionError('subprocess forbidden')) as processes, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                for name, observed, accepted in cases:
                    with self.subTest(case=name):
                        # Booleans must fail by type, even when numerically fresh.
                        clock.return_value = 1.0 if type(observed) is bool else now
                        data = inventory()
                        data['observed_at'] = observed
                        if accepted:
                            self.assertIs(cisco.validate_inventory(data, '213'), data['phones'][0])
                        else:
                            with self.assertRaises(cisco.Refused) as caught:
                                cisco.validate_inventory(data, '213')
                            self.assertEqual(caught.exception.reason, 'gateway_inventory_stale')
            finally:
                self.assertEqual((sockets.call_count, connections.call_count, processes.call_count), (0, 0, 0))
                self.assertFalse(bool(output.getvalue() or errors.getvalue()), 'validator printed output')

    def test_huge_inventory_timestamp_is_structured_refusal_before_mutation(self):
        now = 1800000000
        for sign in (1, -1):
            for command in (['status'], ['dial', '600']):
                for json_mode in (True, False):
                    with self.subTest(sign=sign, command=command[0], json_mode=json_mode):
                        data = inventory()
                        data['observed_at'] = sign * 10 ** 400
                        # A bounded real JSON integer, not a Python-only value.
                        raw = json.dumps(data)
                        self.assertLess(len(raw.encode()), 2048)
                        data = json.loads(raw)
                        output, errors = io.StringIO(), io.StringIO()
                        with patch.object(cisco, 'Gateway') as gateway, \
                                patch.object(cisco, 'load_config', return_value={}), \
                                patch.object(cisco.time, 'time', return_value=now), \
                                patch('socket.socket', side_effect=AssertionError('network forbidden')) as sockets, \
                                patch('socket.create_connection', side_effect=AssertionError('network forbidden')) as connections, \
                                patch('subprocess.Popen', side_effect=AssertionError('subprocess forbidden')) as processes, \
                                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                            gateway.return_value.request.return_value = data
                            try:
                                code = cisco.main((['--json'] if json_mode else []) + ['213', *command])
                            finally:
                                gateway.return_value.request.assert_called_once_with('/v1/phones')
                                self.assertEqual((sockets.call_count, connections.call_count,
                                                  processes.call_count), (0, 0, 0))
                        self.assertEqual(code, 1)
                        response = output.getvalue() if json_mode else errors.getvalue()
                        self.assertEqual(errors.getvalue() if json_mode else output.getvalue(), '')
                        self.assertNotIn('Traceback', response)
                        self.assertEqual(json.loads(response),
                                         {'status': 'refused', 'reason': 'gateway_inventory_stale'})

    def test_dry_run_redacts_screen_content(self):
        code, result, calls = self.invoke(['--dry-run', '213', 'message', 'private message'])
        self.assertEqual(code, 0); self.assertEqual(len(calls), 1)
        self.assertEqual(result['status'], 'dry_run')
        self.assertNotIn('private message', json.dumps(result))
        self.assertTrue(result['content_omitted'])

    def test_title_boundary_matches_gateway_and_default_is_notice(self):
        code, _, calls = self.invoke(['213', 'message', 'Hello', '--title', 'x' * 48])
        self.assertEqual(code, 0)
        self.assertEqual(calls[1][1]['title'], 'x' * 48)
        code, _, calls = self.invoke(['213', 'message', 'Hello', '--title', 'x' * 49])
        self.assertEqual(code, 1); self.assertEqual(len(calls), 1)
        code, _, calls = self.invoke(['213', 'message', 'Hello'])
        self.assertEqual(calls[1][1]['title'], 'Notice')

    def test_human_status_includes_runnable_examples(self):
        data = inventory(); data['dial_allowlist'].append('220')
        result = cisco.status_result(data, data['phones'][0], '213')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            cisco.display(result)
        for example in ('cisco 213 220', 'cisco 213 dial 600',
                        'cisco 213 message "Dinner is ready"', 'cisco 213 dnd on', 'cisco 213 dnd off'):
            self.assertIn(example, output.getvalue())

    def test_image_and_utf8_text_files(self):
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / 'photo.png'; image.write_bytes(b'\x89PNG\r\n\x1a\nsynthetic')
            code, _, calls = self.invoke(['213', 'image', str(image)])
            self.assertEqual(code, 0); self.assertIn('image_base64', calls[1][1])
            text = Path(temp) / 'message.txt'; text.write_text('Hello, world!')
            code, _, calls = self.invoke(['213', 'message', '--text-file', str(text)])
            self.assertEqual(code, 0); self.assertEqual(calls[1][1]['text'], 'Hello, world!')
            image.write_bytes(b'not an image')
            code, _, calls = self.invoke(['213', 'image', str(image)])
            self.assertEqual(code, 1); self.assertEqual(len(calls), 1)

    def test_unknown_outcome_never_retries_or_prints_exception(self):
        code, result, calls = self.invoke(['213', 'dial', '600'], error=OSError(TOKEN))
        self.assertEqual(code, 1); self.assertEqual(len(calls), 2)
        self.assertEqual(result['status'], 'not_confirmed')
        self.assertIn('action_id', result)
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_local_help_never_loads_credentials_or_contacts_gateway(self):
        with patch.object(cisco, 'load_config') as config, patch.object(cisco, 'Gateway') as gateway:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cisco.main(['--json', '213', 'help']), 0)
            self.assertEqual(json.loads(output.getvalue())['status'], 'help')
            config.assert_not_called(); gateway.assert_not_called()

    def test_missing_config_default_has_offline_help_json(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cisco.main(['--json', '--config', '/nonexistent/cisco.json', '213']), 1)
        result = json.loads(output.getvalue())
        self.assertIn('dial NUMBER', result['offline_commands'])


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'config.json'
        self.value = {'gateway_url': 'http://192.168.50.10:18084', 'token': TOKEN, 'transport': 'direct'}

    def save(self):
        self.path.write_text(json.dumps(self.value)); self.path.chmod(0o600)

    def test_private_config_and_relative_private_token_file(self):
        self.save(); self.assertEqual(cisco.load_config(self.path)['token'], TOKEN)
        del self.value['token']; self.value['token_file'] = 'token'
        token = self.path.parent / 'token'; token.write_text(TOKEN + '\n'); token.chmod(0o400)
        self.save(); self.assertEqual(cisco.load_config(self.path)['token'], TOKEN)
        token.chmod(0o644)
        with self.assertRaises(cisco.Refused): cisco.load_config(self.path)

    def test_rejects_symlinks_and_shared_config(self):
        self.save(); self.path.chmod(0o644)
        with self.assertRaises(cisco.Refused): cisco.load_config(self.path)
        self.path.chmod(0o600); link = self.path.parent / 'link'; link.symlink_to(self.path)
        with self.assertRaises(cisco.Refused): cisco.load_config(link)

    def test_nonregular_config_is_rejected_without_blocking(self):
        os.mkfifo(self.path, 0o600)
        with self.assertRaises(cisco.Refused): cisco.load_config(self.path)

    def test_rejects_remote_hosts_userinfo_and_ambiguous_transport(self):
        for url in ('http://example.org', 'http://8.8.8.8', 'http://127.0.0.1',
                    'http://user:pass@192.168.50.10', 'http://192.168.50.10/path',
                    'http://192.168.50.10?token=bad'):
            self.value['gateway_url'] = url; self.save()
            with self.assertRaises(cisco.Refused): cisco.load_config(self.path)
        self.value.update(gateway_url='http://192.168.50.10', kubectl={}); self.save()
        with self.assertRaises(cisco.Refused): cisco.load_config(self.path)

    def test_config_rejects_empty_base_query_and_fragment_markers(self):
        with patch('socket.socket', side_effect=AssertionError('network forbidden')) as sockets, \
                patch('socket.create_connection', side_effect=AssertionError('network forbidden')) as connections, \
                patch('subprocess.Popen', side_effect=AssertionError('subprocess forbidden')) as processes:
            try:
                for suffix in ('?', '#', '/?', '/#'):
                    with self.subTest(suffix=suffix):
                        self.value['gateway_url'] = 'http://192.168.50.10' + suffix
                        self.save()
                        with self.assertRaises(cisco.Refused) as caught:
                            cisco.load_config(self.path)
                        self.assertEqual(caught.exception.reason, 'invalid_private_config')
            finally:
                self.assertEqual((sockets.call_count, connections.call_count, processes.call_count), (0, 0, 0))

    def test_config_retains_private_origins_ports_and_root_slash(self):
        bases = ('http://10.23.45.67', 'https://10.23.45.67:1',
                 'http://172.16.5.6:65535', 'https://172.31.5.6',
                 'http://192.168.50.10:8080', 'https://192.168.50.10:443')
        with patch('socket.socket', side_effect=AssertionError('network forbidden')) as sockets, \
                patch('socket.create_connection', side_effect=AssertionError('network forbidden')) as connections, \
                patch('subprocess.Popen', side_effect=AssertionError('subprocess forbidden')) as processes:
            try:
                for base in bases:
                    for suffix in ('', '/'):
                        with self.subTest(base=base, root_slash=bool(suffix)):
                            self.value['gateway_url'] = base + suffix
                            self.save()
                            self.assertEqual(cisco.load_config(self.path)['gateway_url'], base)
            finally:
                self.assertEqual((sockets.call_count, connections.call_count, processes.call_count), (0, 0, 0))


class TransportTests(unittest.TestCase):
    def config(self):
        return {'gateway_url': 'http://192.168.50.10:18084', 'token': TOKEN, 'transport': 'kubectl',
                'kubectl': {'context': 'home-k3s', 'namespace': 'home-assistant',
                            'selector': 'app=homeassistant', 'container': 'homeassistant'}}

    def pods(self):
        return {'items': [{'metadata': {'name': 'homeassistant-abc'}, 'status': {
            'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}],
            'containerStatuses': [{'name': 'homeassistant', 'ready': True}]}}]}

    def test_explicit_kubectl_uses_fixed_argv_stdin_and_same_pod(self):
        value = inventory()
        responses = [self.pods(), {'ok': True, 'data': value}, {'ok': True, 'data': {'status': 'accepted'}}]
        def run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, json.dumps(responses.pop(0)).encode(), b'')
        client = cisco.Gateway(self.config(), 10)
        with patch.object(cisco.subprocess, 'run', side_effect=run) as process:
            self.assertEqual(client.request('/v1/phones'), value)
            client.request('/v1/phones/212/dial', {'line': '213', 'number': '600'})
        self.assertEqual(process.call_count, 3)
        for call in process.call_args_list:
            args = call.args[0]
            self.assertNotIn(TOKEN, repr(args)); self.assertFalse(call.kwargs.get('shell', False))
            self.assertEqual(args[:5], ['kubectl', '--context', 'home-k3s', '--namespace', 'home-assistant'])
        execute = process.call_args_list[1]
        self.assertIn('homeassistant-abc', execute.args[0])
        payload = json.loads(execute.kwargs['input'])
        self.assertEqual(payload['token'], TOKEN)
        self.assertEqual(payload['timeout'], 10)

    def test_ambiguous_pod_never_executes_and_stderr_never_leaks(self):
        pods = self.pods(); pods['items'] *= 2
        with patch.object(cisco.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(pods).encode(), b'')) as process:
            with self.assertRaisesRegex(cisco.Refused, ''):
                cisco.Gateway(self.config()).request('/v1/phones')
            self.assertEqual(process.call_count, 1)
        with patch.object(cisco.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, b'', TOKEN.encode())):
            with self.assertRaises(cisco.Refused) as raised:
                cisco.Gateway(self.config()).request('/v1/phones')
            self.assertNotIn(TOKEN, raised.exception.reason)

    def test_http_exception_is_sanitized(self):
        payload = {'url': 'http://192.168.50.10', 'token': TOKEN,
                   'path': '/v1/phones', 'body': None, 'timeout': 2}
        with patch.object(cisco_http.request.OpenerDirector, 'open', side_effect=http.client.BadStatusLine(TOKEN)):
            result = cisco_http.perform(payload)
        self.assertEqual(result, {'ok': False, 'reason': 'network_outcome_unknown'})

    def test_real_http_json_redirect_refusal_and_response_cap(self):
        paths = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                paths.append(self.path)
                if len(paths) == 1:
                    self.send_response(302); self.send_header('Location', '/secret-destination'); self.end_headers()
                else:
                    self.send_response(200); self.end_headers()
                    self.wfile.write(b'x' * (cisco_http.MAX_RESPONSE + 1))
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            payload = {'url': 'http://192.168.50.10', 'token': TOKEN,
                       'path': '/v1/phones', 'body': None, 'timeout': 2}
            local = 'http://127.0.0.1:' + str(server.server_port)
            with patch.object(cisco_http, 'gateway_url', return_value=local), \
                    patch.dict(os.environ, {'http_proxy': 'http://127.0.0.1:1', 'HTTP_PROXY': 'http://127.0.0.1:1'}):
                result = cisco_http.perform(payload)
                self.assertEqual(result['http_status'], 302)
                self.assertEqual(paths, ['/v1/phones'])
                self.assertEqual(cisco_http.perform(payload)['reason'], 'invalid_gateway_response')
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
