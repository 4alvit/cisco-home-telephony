"""Bounded voice menu tests; fake gateway only, no microphone or phone requests."""
import copy
import threading
import unittest
from types import SimpleNamespace
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from test_phone_apps import apps, FakeGateway


def status(**updates):
    return {'schema_version': 1, 'phone': '201', 'line': '202', 'session_id': 'a' * 32,
            'state': 'running', 'active': True, 'remaining_seconds': 20,
            'stop_requested': False, 'stop_confirmed': False, 'error': None, **updates}


class VoiceGateway(FakeGateway):
    def __init__(self, phone_id='201', lines=('201', '202')):
        super().__init__()
        phone = self.phones.pop('201')
        phone.update(id=phone_id, lines=list(lines))
        self.phones[phone_id] = phone
        self.phone_id = phone_id
        self.voice = object()
        self._voice_profiles = (SimpleNamespace(phone_id=phone_id),)
        self.voice_calls = []
        self.context = object()
        self.value = status(phone=phone_id, line=lines[-1])
        self.response_filter = lambda result: result

    def listing(self):
        raise AssertionError('Voice status/Stop must not fetch AMI inventory')

    def _phone_voice_context(self, phone_id):
        assert phone_id == self.phone_id
        return self.context

    def voice_request(self, line, operation, body, context):
        assert context is self.context
        self.voice_calls.append((line, operation, body))
        if self.fail:
            raise RuntimeError('private URL and credentials')
        value = copy.deepcopy(self.value)
        if operation == 'stop':
            value.update(session_id=body['session_id'], state='stopping', active=True, stop_requested=True)
        result = {'status': 'ok' if operation == 'status' else 'accepted', 'voice': value}
        if operation != 'status':
            result['action_id'] = body['idempotency_key']
        return self.response_filter(result)


class VoiceMenuTests(unittest.TestCase):
    def setUp(self):
        self.gateway = VoiceGateway()
        self.now = 100
        self.app = apps.PhoneApps(self.gateway, clock=lambda: self.now)

    def get(self, path='/voice', query='line=202', phone='201'):
        data, _ = self.app.handle(phone, path, query)
        return ET.fromstring(data)

    def click(self, url, phone='201'):
        parsed = urlsplit(url)
        base = self.gateway.phones[phone]['apps_path']
        return self.get(parsed.path[len(base):] or '/', parsed.query, phone)

    def key(self, root, name):
        return next(row.find('URL').text for row in root.findall('SoftKeyItem') if row.find('Name').text == name)

    def test_home_optional_and_status_is_read_only_even_on_sibling_alias(self):
        root = self.get('/')
        self.assertIn('Voice Assistant', [item.text for item in root.findall('MenuItem/Name')])
        self.assertEqual(self.gateway.voice_calls, [])
        root = self.get(query='line=201')
        self.assertIn('Session line: 202', root.find('Text').text)
        self.click(self.key(root, 'Refresh'))
        self.assertEqual([row[1] for row in self.gateway.voice_calls], ['status', 'status'])
        self.gateway.voice = None
        root = self.get('/')
        self.assertNotIn('Voice Assistant', [item.text for item in root.findall('MenuItem/Name')])
        with self.assertRaises(apps.AppError):
            self.get()

    def test_unconfigured_phone_hides_voice_and_cannot_prepare_start(self):
        root = self.get('/', 'line=203', phone='203')
        self.assertNotIn('Voice Assistant', [item.text for item in root.findall('MenuItem/Name')])
        for route in ('/voice', '/voice-start', '/voice-stop'):
            query = 'line=203' + ('&session=' + 'a' * 32 if route == '/voice-stop' else '')
            with self.assertRaises(apps.AppError) as raised:
                self.get(route, query, phone='203')
            self.assertEqual(raised.exception.reason, 'voice_control_unavailable')
        self.assertEqual(self.app.tokens, {})
        self.assertEqual(self.gateway.voice_calls, [])

    def test_start_has_explicit_one_use_bound_consent(self):
        root = self.get('/voice-start')
        text = root.find('MenuItem/Name').text
        self.assertIn('Microphone <=30s', text)
        self.assertIn('I am here', text)
        self.assertIn('audio reserved', text)
        url = root.find('MenuItem/URL').text
        self.assertEqual(self.gateway.voice_calls, [])
        with self.assertRaises(apps.AppError):
            self.get('/apply', urlsplit(url).query, '203')
        result = self.click(url)
        self.assertEqual(result.find('Title').text, 'Voice Assistant')
        line, action, body = self.gateway.voice_calls[0]
        self.assertEqual((line, action), ('202', 'start'))
        self.assertIs(body['attended'], True)
        self.assertIs(body['exclusive_media'], True)
        self.assertIs(body['enable_microphone'], True)
        with self.assertRaises(apps.AppError):
            self.click(url)
        self.assertEqual(len(self.gateway.voice_calls), 1)

    def test_concurrent_confirmation_executes_once_and_failure_consumes(self):
        url = self.get('/voice-start').find('MenuItem/URL').text
        failures = []
        def confirm():
            try:
                self.click(url)
            except apps.AppError:
                failures.append(True)
        threads = [threading.Thread(target=confirm) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.gateway.voice_calls), 1)
        self.assertEqual(len(failures), 7)
        self.gateway.fail = True
        url = self.get('/voice-start').find('MenuItem/URL').text
        with self.assertRaises(apps.AppError) as raised:
            self.click(url)
        self.assertEqual(raised.exception.reason, 'voice_request_failed')
        self.assertNotIn('private', str(raised.exception))
        with self.assertRaises(apps.AppError):
            self.click(url)
        self.assertEqual(len(self.gateway.voice_calls), 2)

    def test_start_requires_nonidle_session_response(self):
        self.gateway.value = status(state='idle', active=False, session_id=None)
        url = self.get('/voice-start').find('MenuItem/URL').text
        with self.assertRaises(apps.AppError) as raised:
            self.click(url)
        self.assertEqual(raised.exception.reason, 'voice_response_invalid')
        with self.assertRaises(apps.AppError):
            self.click(url)
        self.assertEqual(len(self.gateway.voice_calls), 1)

    def test_expired_confirmation_does_not_start(self):
        url = self.get('/voice-start').find('MenuItem/URL').text
        self.now += apps.TOKEN_TTL
        with self.assertRaises(apps.AppError):
            self.click(url)
        self.assertEqual(self.gateway.voice_calls, [])

    def test_stop_retains_displayed_session_after_current_session_changes(self):
        root = self.get(query='line=201')
        stop = self.key(root, 'Stop')
        self.gateway.value = status(session_id='b' * 32)
        result = self.click(stop)
        self.assertIn('Stop confirmed: No', result.find('Text').text)
        self.assertEqual(self.gateway.voice_calls[-1], ('201', 'stop',
                         {'session_id': 'a' * 32, 'idempotency_key': 'apps-voice-stop-' + 'a' * 32}))
        self.assertEqual([item[1] for item in self.gateway.voice_calls], ['status', 'stop'])
        self.click(stop)
        self.assertEqual(self.gateway.voice_calls[-1], self.gateway.voice_calls[-2])
        self.assertNotIn('/voice-stop', self.key(result, 'Refresh'))

    def test_safe_terminal_states_offer_start_and_recovery_does_not(self):
        for state in ('idle', 'stopped', 'cancelled', 'failed'):
            with self.subTest(state=state):
                self.gateway.value = status(state=state, active=False,
                                            session_id=None if state == 'idle' else 'a' * 32)
                root = self.get()
                self.assertIn('/voice-start', self.key(root, 'Start'))
                self.click(self.key(root, 'Start'))
        self.gateway.value = status(state='recovery_required', active=False, error='voice_stop_unconfirmed')
        root = self.get()
        self.assertNotIn('Start', [key.find('Name').text for key in root.findall('SoftKeyItem')])
        self.assertIn('Cleanup may be unconfirmed', root.find('Text').text)

    def use_two_line_phone(self):
        self.gateway = VoiceGateway(phone_id='212', lines=('212', '213'))
        self.app = apps.PhoneApps(self.gateway, clock=lambda: self.now)

    def test_external_owner_has_guidance_without_gateway_start_or_stop_on_either_alias(self):
        self.use_two_line_phone()
        for state, error in (('recovery_required', 'voice_owner_unavailable'),
                             ('external_owner', None),
                             ('external_owner', 'voice_owner_unavailable')):
            for line in ('212', '213'):
                with self.subTest(state=state, error=error, line=line):
                    self.gateway.voice_calls.clear()
                    self.gateway.value = status(phone='212', line='213', state=state,
                                                active=False, error=error)
                    root = self.get(query='line=' + line, phone='212')
                    self.assertEqual(
                        [key.find('Name').text for key in root.findall('SoftKeyItem')],
                        ['Refresh', 'Home', 'Exit'])
                    text = root.find('Text').text
                    self.assertIn('Session line: 213', text)
                    self.assertIn('Stop confirmed: No', text)
                    self.assertIn("cannot reach the session's controller", text)
                    self.assertIn('live Listening, Thinking or Speaking screen', text)
                    self.assertIn('stop that exact Job', text)
                    self.assertIn('Do not restart until cleanup is confirmed', text)
                    self.click(self.key(root, 'Refresh'), phone='212')
                    self.assertEqual(self.gateway.voice_calls,
                                     [(line, 'status', {}), (line, 'status', {})])

    def test_local_stop_remains_session_bound_on_either_alias_including_recovery(self):
        self.use_two_line_phone()
        cases = [('starting', None), ('running', None), ('stopping', None),
                 ('recovery_required', 'voice_start_uncertain'),
                 ('recovery_required', 'voice_stop_unconfirmed')]
        for state, error in cases:
            for line in ('212', '213'):
                with self.subTest(state=state, error=error, line=line):
                    self.gateway.voice_calls.clear()
                    self.gateway.value = status(
                        phone='212', line='213', state=state, error=error,
                        active=state in ('starting', 'running', 'stopping'))
                    root = self.get(query='line=' + line, phone='212')
                    self.assertNotIn('Start', [key.find('Name').text
                                              for key in root.findall('SoftKeyItem')])
                    self.assertNotIn("cannot reach the session's controller", root.find('Text').text)
                    result = self.click(self.key(root, 'Stop'), phone='212')
                    self.assertEqual(self.gateway.voice_calls, [
                        (line, 'status', {}),
                        (line, 'stop', {'session_id': 'a' * 32,
                                        'idempotency_key': 'apps-voice-stop-' + 'a' * 32}),
                    ])
                    self.assertIn('Stop confirmed: No', result.find('Text').text)

    def test_local_terminal_states_keep_start_on_either_alias(self):
        self.use_two_line_phone()
        for state in ('idle', 'stopped', 'cancelled', 'failed'):
            for line in ('212', '213'):
                with self.subTest(state=state, line=line):
                    self.gateway.voice_calls.clear()
                    self.gateway.value = status(
                        phone='212', line='213', state=state, active=False,
                        session_id=None if state == 'idle' else 'a' * 32)
                    root = self.get(query='line=' + line, phone='212')
                    self.assertNotIn('Stop', [key.find('Name').text
                                             for key in root.findall('SoftKeyItem')])
                    confirmation = self.click(self.key(root, 'Start'), phone='212')
                    self.assertEqual(confirmation.find('Title').text, 'Confirm action')
                    self.assertEqual(self.gateway.voice_calls, [(line, 'status', {})])

    def test_busy_message_points_to_guidance_without_promising_gateway_stop(self):
        data, _ = apps.PhoneApps.error('voice_phone_busy')
        text = ET.fromstring(data).find('Text').text
        self.assertIn('cleanup is unconfirmed', text)
        self.assertIn('status and Stop guidance', text)
        self.assertNotIn('Open Voice Assistant and stop', text)

    def test_malformed_query_or_metadata_is_rejected_without_leaking(self):
        for query in ('line=204', 'line=202&session=' + 'A' * 32, 'line=202&session=' + 'a' * 32 + '&extra=x'):
            with self.assertRaises(apps.AppError):
                self.get('/voice-stop', query)
        self.assertEqual(self.gateway.voice_calls, [])
        for change in ({'schema_version': True}, {'phone': '203'}, {'line': '204'}, {'state': []},
                       {'active': 1}, {'stop_confirmed': 'yes'}, {'session_id': None},
                       {'remaining_seconds': float('nan')}, {'remaining_seconds': 10 ** 1000},
                       {'remaining_seconds': 31}, {'error': 'private token'}, {'transcript': 'private'}):
            with self.subTest(change=change):
                self.gateway.value = status(**change)
                with self.assertRaises(apps.AppError) as raised:
                    self.get()
                self.assertEqual(raised.exception.reason, 'voice_response_invalid')
        self.gateway.value = status()
        self.gateway.response_filter = lambda result: {**result, 'action_id': 'wrong-id'}
        with self.assertRaises(apps.AppError):
            self.get('/voice-stop', 'line=202&session=' + 'a' * 32)
        self.gateway.response_filter = lambda result: {**result, 'voice': status(session_id='b' * 32)}
        with self.assertRaises(apps.AppError):
            self.get('/voice-stop', 'line=202&session=' + 'a' * 32)


if __name__ == '__main__':
    unittest.main()
