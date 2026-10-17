"""Offline contracts for voice alarm toggles and the private incoming ring group."""
import base64
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import provision
import preflight
import render_k3s
s = provision.phone_services


def wav():
    result = io.BytesIO()
    with wave.open(result, 'wb') as output:
        output.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        output.writeframes(b'\0\0' * 1600)
    return result.getvalue()


class Response(io.BytesIO):
    status = 200


class Client:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def open(self, req, timeout):
        self.requests.append((req, timeout))
        if isinstance(self.payload, Exception):
            raise self.payload
        return Response(json.dumps(self.payload).encode())


class VoiceContracts(unittest.TestCase):
    def setUp(self):
        self.config = {'url': 'http://192.168.50.30:8123', 'token': 'x' * 40,
                       'entry_id': 'a' * 32, 'line_owners': {'201': '201', '202': '201'}}
        self.result = {'schema_version': 1, 'phone': '201', 'line': '202', 'gateway_fresh': True,
                       'operation_result': 'applied',
                       'alarm': {'configured': True, 'enabled': True, 'line': '201'}}

    def test_uses_authenticated_channel_and_stable_call_id_not_callerid(self):
        client = Client({'service_response': self.result})
        for _ in range(2):
            self.assertEqual(s.toggle(self.config, 'PJSIP/202-000000ab', 'call-123', client), 'enabled')
        first, second = [json.loads(req.data) for req, _ in client.requests]
        self.assertEqual(first, second)
        self.assertEqual(first['line'], '202')
        self.assertEqual(first['phone'], '201')
        self.assertEqual(first['operation'], 'toggle_saved_alarm')
        self.assertLessEqual(len(first['request_id']), 80)
        self.assertEqual(client.requests[0][0].full_url, self.config['url'] + s.SCHEDULE_PATH)
        for channel in ('PJSIP/external-sip-callcentric-000000ab', 'PJSIP/209-000000ab', 'Local/202@home-0001;1', '202'):
            self.assertEqual(s.toggle(self.config, channel, 'call-123', client), 'not-confirmed')
        self.assertEqual(len(client.requests), 2)

    def test_only_confirmed_fresh_outcomes_are_announced(self):
        variants = [({'operation_result': 'not_configured', 'alarm': {'configured': False}}, 'not-configured'),
                    ({'alarm': {'configured': True, 'enabled': False, 'line': '201'}}, 'disabled'),
                    ({'operation_result': 'replayed'}, 'enabled'),
                    ({'operation_result': 'outcome_unknown'}, 'not-confirmed'),
                    ({'gateway_fresh': False}, 'not-confirmed'),
                    ({'line': '201'}, 'not-confirmed'),
                    ({'phone': '203'}, 'not-confirmed'),
                    ({'alarm': {'configured': True, 'enabled': True, 'line': '203'}}, 'not-confirmed'),
                    ({'operation_result': 'not_configured'}, 'not-confirmed')]
        for change, expected in variants:
            with self.subTest(change=change):
                client = Client({'service_response': {**self.result, **change}})
                self.assertEqual(s.toggle(self.config, 'PJSIP/202-000000ab', '123', client), expected)
        client = Client(TimeoutError('not logged'))
        self.assertEqual(s.toggle(self.config, 'PJSIP/202-000000ab', '123', client), 'not-confirmed')
        self.assertEqual(len(client.requests), 1)

    def test_public_or_credential_bearing_origins_are_rejected(self):
        for value in ('https://example.test', 'http://8.8.8.8', 'http://192.168.50.30/path',
                      'http://user:secret@192.168.50.30', 'file:///tmp/token'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                s.ha_url(value)


class ServiceProvisioning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        credentials = self.root / 'credentials.json'
        credentials.write_text(json.dumps({'token': 'x' * 40, 'entry_id': 'a' * 32}))
        credentials.chmod(0o600)
        prompts = self.root / 'prompts'
        prompts.mkdir(mode=0o700)
        for name in s.PROMPTS:
            path = prompts / (name + '.wav')
            path.write_bytes(wav())
            path.chmod(0o600)
        self.site = {'node_ipv4': '192.168.50.10', 'lan_cidr': '192.168.50.0/24',
                     'ntp_ipv4': '192.168.50.1',
                     'gateway': {'enabled': True, 'ami_client_ipv4': '192.168.50.10'},
                     'phone_services': {'enabled': True, 'home_assistant_url': 'http://192.168.50.30:8123',
                                        'credentials_file': str(credentials), 'prompts_directory': str(prompts)},
                     'inbound_ring_group': {'enabled': True, 'members': ['201', '209']},
                     'softphones': [{'extension': '209', 'label': 'Desktop', 'source_ipv4': '192.168.50.21', 'transport': 'tcp'}]}
        self.inventory = {'phones': [{'model': 'CP-7945G', 'verified': True, 'mac': '020000000001',
                                     'label': 'Phone', 'firmware_load': 'SIP45.9-4-2SR4-3S',
                                     'lines': [{'extension': '201', 'label': '201'}, {'extension': '202', 'label': '202'}]}]}
        self.out = self.root / 'generated'

    def generated(self):
        provision.generate(self.site, self.inventory, self.out)
        return render_k3s.read_files(self.out / 'asterisk', '.conf')

    def test_private_inputs_config_and_opt_in_routes_are_coherent(self):
        configs = self.generated()
        preflight.check_service_config(self.site, configs, self.inventory)
        preflight.check_phone_config(self.site, configs)
        config = json.loads(configs['phone-services.conf'])
        self.assertEqual(config['line_owners'], {'201': '201', '202': '201'})
        self.assertEqual(base64.b64decode(config['prompts']['enabled']), wav())
        self.assertEqual((self.out / 'asterisk/phone-services.conf').stat().st_mode & 0o777, 0o600)
        self.assertIn('AGI(phone_services.py)', configs['extensions.conf'])
        group = s.group_dialplan(self.site)
        self.assertIn('PJSIP/201', group)
        self.assertIn('PJSIP/209', group)
        self.assertNotIn('PJSIP/202', group)
        self.assertIn('DB(cisco-dnd/201)', group)
        self.assertIn('DB(cisco-dnd/209)', group)
        self.assertIn('Dial(${INBOUND_TARGETS},25,i)', group)
        self.assertIn('Busy(3)', group)
        for code in ('621', '641'):
            self.assertIn(f'MATCH="{code}"', (self.out / 'tftp/dialplan.xml').read_text())
        self.site['phone_services']['enabled'] = False
        self.site['inbound_ring_group']['enabled'] = False
        disabled = self.generated()
        self.assertNotIn('phone-services.conf', disabled)
        self.assertNotIn('exten => 621,', disabled['extensions.conf'])
        self.assertNotIn('exten => 641,', disabled['extensions.conf'])
        preflight.check_service_config(self.site, disabled, self.inventory)

    def test_unknown_secondary_duplicate_and_external_group_members_rejected_before_writes(self):
        for members in (['202'], ['201', '201'], ['299'], ['801'], []):
            with self.subTest(members=members):
                self.site['inbound_ring_group']['members'] = members
                with self.assertRaises(ValueError):
                    self.generated()
                self.assertFalse(self.out.exists())

    def test_stale_prompts_routes_and_physical_ownership_fail_preflight(self):
        configs = self.generated()
        for bad in (configs | {'extensions.conf': configs['extensions.conf'].replace('AGI(phone_services.py)', 'Hangup()')},
                    configs | {'extensions.conf': configs['extensions.conf'] + '\nexten => 641,1,Answer()\n'}):
            with self.assertRaises(ValueError):
                preflight.check_service_config(self.site, bad, self.inventory)
        config = json.loads(configs['phone-services.conf'])
        config['line_owners'] = {'201': '201', '202': '202'}
        with self.assertRaises(ValueError):
            preflight.check_service_config(self.site, configs | {'phone-services.conf': json.dumps(config)}, self.inventory)
        (Path(self.site['phone_services']['prompts_directory']) / 'enabled.wav').write_bytes(b'bad')
        with self.assertRaises((ValueError, wave.Error)):
            preflight.check_service_config(self.site, configs, self.inventory)

    def test_secret_read_refuses_symlinks_and_world_readable_files(self):
        path = Path(self.site['phone_services']['credentials_file'])
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.generated()
        path.chmod(0o600)
        link = self.root / 'link.json'
        link.symlink_to(path)
        self.site['phone_services']['credentials_file'] = str(link)
        with self.assertRaises(ValueError):
            self.generated()
        self.assertFalse(self.out.exists())

    def test_group_target_requires_separate_reviewed_inbound_enable(self):
        self.site['external_sip'] = {'enabled': True, 'public_ipv4': '8.8.8.8', 'accounts': [
            {'provider': 'callcentric', 'enabled': True, 'target': None,
             'credentials_file': str(self.root / 'provider.json'),
             'inbound': {'enabled': True, 'target': '621', 'source_cidrs': ['204.11.192.0/22']}}]}
        path = self.root / 'provider.json'
        path.write_text(json.dumps({'username': '17770000001', 'password': 'test_password_only_123'}))
        path.chmod(0o600)
        configs = self.generated()
        preflight.check_phone_config(self.site, configs)
        self.assertIn('exten => 17770000001,1,Set(TIMEOUT(absolute)=120)\n same => n,Goto(home,621,1)', configs['extensions.conf'])
        self.assertNotIn('Dial(PJSIP/621', configs['extensions.conf'])
        self.site['inbound_ring_group']['enabled'] = False
        with self.assertRaises(ValueError):
            self.generated()


class PromptPreparation(unittest.TestCase):
    def test_fixed_messages_use_private_token_and_same_origin_audio(self):
        import generate_alarm_prompts as prompts
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credential = root / 'credentials.json'
            credential.write_text(json.dumps({'token': 'x' * 40, 'entry_id': 'a' * 32}))
            credential.chmod(0o600)
            seen = []
            def open_request(req, timeout):
                seen.append(req)
                if req.full_url.endswith('/api/tts_get_url'):
                    return Response(json.dumps({'path': '/api/tts_proxy/synthetic.wav',
                                                'url': 'https://ignored.example/never-used'}).encode())
                self.assertEqual(req.full_url, 'http://192.168.50.30:8123/api/tts_proxy/synthetic.wav')
                return Response(wav())
            def ffmpeg(command, **kwargs):
                Path(command[-1]).write_bytes(wav())
                self.assertEqual(kwargs['input'], wav())
                return type('Process', (), {'returncode': 0})()
            client = type('Client', (), {'open': staticmethod(open_request)})()
            with patch.object(prompts.subprocess, 'run', side_effect=ffmpeg):
                prompts.generate('http://192.168.50.30:8123', credential, root / 'prompts', client=client)
            self.assertEqual(len(seen), 8)
            self.assertEqual({json.loads(req.data)['message'] for req in seen if req.data}, set(prompts.TEXT.values()))
            for path in (root / 'prompts').iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                s.validate_audio(path.read_bytes())
            with self.assertRaises(FileExistsError):
                prompts.generate('http://192.168.50.30:8123', credential, root / 'prompts', client=client)

    def test_redirect_audio_paths_are_not_followed(self):
        import generate_alarm_prompts as prompts
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credential = root / 'credentials.json'
            credential.write_text(json.dumps({'token': 'x' * 40}))
            credential.chmod(0o600)
            client = Client({'path': 'https://attacker.invalid/audio'})
            with self.assertRaises(ValueError):
                prompts.generate('http://192.168.50.30', credential, root / 'prompts', client=client)
            self.assertEqual(len(client.requests), 1)

    def test_agi_streams_private_temporary_prompt_and_cleans_up(self):
        config = {'url': 'http://192.168.50.30', 'token': 'x' * 40, 'entry_id': 'a' * 32,
                  'line_owners': {'201': '201'},
                  'prompts': {name: base64.b64encode(wav()).decode() for name in s.PROMPTS}}
        written = io.StringIO()
        class AGIInput(io.StringIO):
            def readline(inner, size=-1):
                if inner.tell() == len(inner.getvalue()):
                    command = written.getvalue().strip()
                    self.assertTrue(command.startswith('STREAM FILE /'))
                    path = Path(command.split()[2] + '.wav')
                    self.assertEqual(path.read_bytes(), wav())
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    inner.played = path
                    return '200 result=0\n'
                return super().readline(size)
        stdin = AGIInput('agi_channel: Local/641@home-00000001;2\nagi_uniqueid: 123.4\n\n')
        with patch.object(s.Path, 'read_text', return_value=json.dumps(config)), \
             patch.object(s.sys, 'stdin', stdin), patch.object(s.sys, 'stdout', written):
            s.main()
        self.assertFalse(stdin.played.exists())
