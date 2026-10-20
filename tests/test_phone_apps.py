import copy
import http.client
import importlib.util
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET


spec = importlib.util.spec_from_file_location('phone_apps', Path(__file__).resolve().parents[1] /
                                            'containers/asterisk/phone_apps.py')
apps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apps)


class FakeGateway:
    def __init__(self):
        self.phones = {
            '201': {'id': '201', 'lines': ['201', '202'], 'room': 'Office & Lab',
                    'port': 18080, 'apps_path': '/apps/' + 'a' * 64},
            '203': {'id': '203', 'lines': ['203', '204'], 'room': 'Test Room',
                    'port': 18081, 'apps_path': '/apps/' + 'b' * 64},
        }
        self.config = {'bind_ipv4': '192.168.50.10', 'phones': list(self.phones.values()),
                       'softphones': [{'extension': '209', 'room': 'Test Computer'}],
                       'services': [{'number': '600', 'label': 'Echo test', 'kind': 'diagnostic'},
                                    {'number': '701', 'label': 'Radio & Music', 'kind': 'radio'}],
                       'dial_allowlist': ['201', '202', '203', '204', '209', '600', '701']}
        self.calls = []
        self.alert = None
        self.stop_state = 'ended'
        self.fail = False
        self.resources = []

    def listing(self):
        return {'phones': [{**phone, 'dnd': False, 'autoanswer': True, 'online': True,
                            'channels': [], 'alert': self.alert} for phone in self.phones.values()]}

    def mutate(self, phone_id, action, payload):
        self.calls.append((phone_id, action, payload))
        if self.fail:
            raise RuntimeError('Private backend URL and token must never be displayed')
        result = {'status': 'accepted', 'action_id': payload['idempotency_key']}
        if action == 'alert_stop':
            result.update(alert_id=payload['alert_id'], state=self.stop_state)
        return result

    def resource(self, phone, data, mime):
        self.resources.append((phone['id'], data, mime))
        return 'http://192.168.50.10:18080/content/opaque'


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.value = {'phone': '201', 'line': '201', 'lines': ['201', '202'],
                      'timezone': 'Etc/UTC', 'gateway_fresh': True, 'operation_result': 'status',
                      'timer': {'line': '201', 'active': False, 'remaining_seconds': None},
                      'alarm': {'line': '201', 'time': '07:30', 'weekdays': [0, 1, 2, 3, 4],
                                'enabled': False, 'configured': False}}

    def schedule(self, phone_id, line, operation='status', **params):
        self.calls.append((phone_id, line, operation, params))
        result = copy.deepcopy(self.value)
        result['line'] = line
        result['operation_result'] = 'status' if operation == 'status' else 'applied'
        return result

    def entities(self, _phone_id):
        return [{'id': 'lamp', 'label': 'Demo lamp', 'state': 'off', 'unit': None, 'control': True},
                {'id': 'temp', 'label': 'Temperature', 'state': '21', 'unit': 'C', 'control': False}]

    def entity_action(self, phone_id, key, action):
        self.calls.append(('entity', phone_id, key, action))
        return {'id': key, 'state': action}

    def cameras(self, _phone_id):
        return [{'id': 'door', 'label': 'Demo Door'}]

    def camera(self, phone_id, key):
        self.calls.append(('camera', phone_id, key))
        return b'PNG test fixture'


class PhoneAppsTests(unittest.TestCase):
    def setUp(self):
        self.gateway, self.backend = FakeGateway(), FakeBackend()
        self.now = 100
        self.app = apps.PhoneApps(self.gateway, self.backend, clock=lambda: self.now)

    def get(self, path='/', query='', phone='201'):
        data, mime = self.app.handle(phone, path, query)
        self.assertEqual(mime, 'text/xml; charset=utf-8')
        return ET.fromstring(data)

    def click(self, url, phone='201'):
        parsed = urlsplit(url)
        base = self.gateway.phones[phone]['apps_path']
        self.assertTrue(parsed.path.startswith(base))
        return self.get(parsed.path[len(base):] or '/', parsed.query, phone)

    def confirmation(self, path='/set', query='line=202&setting=dnd&enabled=1'):
        root = self.get(path, query)
        return root.find('MenuItem/URL').text

    def refresh(self, root):
        return self.click(next(key.find('URL').text for key in root.findall('SoftKeyItem')
                               if key.find('Name').text == 'Refresh'))

    def test_root_is_native_menu_and_preserves_selected_line(self):
        root = self.get(query='line=202')
        self.assertEqual(root.tag, 'CiscoIPPhoneMenu')
        self.assertEqual(len(root.findall('MenuItem')), 8)
        for url in root.findall('MenuItem/URL'):
            self.assertEqual(parse_qs(urlsplit(url.text).query)['line'], ['202'])
        self.assertFalse(self.gateway.calls)
        self.assertFalse(self.backend.calls)

    def test_line_selection_only_offers_this_phone(self):
        root = self.get('/lines')
        self.assertEqual([item.text for item in root.findall('MenuItem/Name')], ['Line 201', 'Line 202'])
        with self.assertRaises(apps.AppError):
            self.get(query='line=204')

    def test_directory_contains_all_numbers_and_xml_escapes_labels(self):
        root = self.get('/directory')
        names = [item.text for item in root.findall('MenuItem/Name')]
        self.assertEqual(len(names), 7)
        self.assertIn('202 - Office & Lab', names)
        self.assertIn('209 - Test Computer', names)
        self.assertIn('701 - Radio & Music', names)
        self.assertFalse(self.gateway.calls)

    def test_directory_filters_do_not_expose_undialable_numbers(self):
        self.gateway.config['dial_allowlist'].remove('203')
        self.assertNotIn('203', [item[0] for item in self.app.catalog()])
        root = self.get('/directory', 'category=services')
        self.assertEqual(len(root.findall('MenuItem')), 2)
        root = self.get('/radio')
        self.assertEqual(root.find('MenuItem/Name').text, '701 - Radio & Music')

    def test_directory_paginates_with_every_number_reachable(self):
        for number in range(710, 810):
            self.gateway.config['services'].append({'number': str(number), 'label': 'Demo ' + str(number), 'kind': 'radio'})
            self.gateway.config['dial_allowlist'].append(str(number))
        root, seen = self.get('/directory'), []
        for _ in range(10):
            self.assertLessEqual(len(root.findall('MenuItem')), 32)
            seen.extend(item.find('Name').text.split(' - ')[0] for item in root.findall('MenuItem')
                        if ' - ' in item.find('Name').text)
            next_urls = [item.find('URL').text for item in root.findall('MenuItem')
                         if item.find('Name').text == 'Next page']
            if not next_urls:
                break
            root = self.click(next_urls[0])
        self.assertEqual(len(seen), 107)
        self.assertEqual(len(set(seen)), 107)
        with self.assertRaises(apps.AppError): self.get('/directory', 'page=9999')

    def test_call_requires_confirmation_and_uses_exact_sibling_line(self):
        url = self.confirmation('/call', 'line=202&number=203')
        self.assertFalse(self.gateway.calls)
        root = self.click(url)
        self.assertEqual(root.find('Title').text, 'Action completed')
        phone, action, body = self.gateway.calls[0]
        self.assertEqual((phone, action, body['line'], body['number']), ('201', 'dial', '202', '203'))
        self.assertRegex(body['idempotency_key'], '^apps-[0-9a-f]{32}$')
        with self.assertRaises(apps.AppError): self.click(url)
        self.assertEqual(len(self.gateway.calls), 1)

    def test_mutations_are_phone_bound_and_expire(self):
        url = self.confirmation()
        query = urlsplit(url).query
        with self.assertRaises(apps.AppError): self.get('/apply', query, phone='203')
        self.assertFalse(self.gateway.calls)
        self.now += apps.TOKEN_TTL
        with self.assertRaises(apps.AppError): self.click(url)
        self.assertFalse(self.gateway.calls)

    def test_failed_network_action_is_not_replayed(self):
        url = self.confirmation()
        self.gateway.fail = True
        with self.assertRaises(RuntimeError): self.click(url)
        with self.assertRaises(apps.AppError): self.click(url)
        self.assertEqual(len(self.gateway.calls), 1)

    def test_only_one_concurrent_confirmation_can_execute(self):
        url, successes, failures = self.confirmation(), [], []
        barrier = threading.Barrier(2)
        def invoke():
            barrier.wait()
            try:
                self.click(url)
                successes.append(True)
            except apps.AppError:
                failures.append(True)
        threads = [threading.Thread(target=invoke) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual((len(successes), len(failures), len(self.gateway.calls)), (1, 1, 1))

    def test_pending_tokens_are_bounded_per_phone(self):
        first = self.confirmation()
        for _ in range(apps.MAX_TOKENS_PER_PHONE + 2): self.confirmation()
        self.assertEqual(len(self.app.tokens), apps.MAX_TOKENS_PER_PHONE)
        with self.assertRaises(apps.AppError): self.click(first)

    def test_query_injection_and_unknown_actions_are_rejected_without_effects(self):
        attempts = [('/', 'line=201&line=202'), ('/', 'phone=203'), ('/', 'line=201%00'),
                    ('/', 'line=%FF'), ('/', 'line=201&'), ('/set', 'setting=ami&enabled=1'),
                    ('/set', 'setting=dnd&enabled=true'), ('/call', 'number=911'),
                    ('/call', 'number=http%3A%2F%2Fevil.test'), ('/apply', 'token=x&line=202'),
                    ('/../../v1/phones', ''), ('/prepare', 'op=timer_cancel&minutes=2'),
                    ('/prepare', 'op=shell'), ('/home', 'url=http%3A%2F%2Fevil.test')]
        for path, query in attempts:
            with self.subTest(path=path, query=query), self.assertRaises(apps.AppError): self.get(path, query)
        self.assertFalse(self.gateway.calls)
        self.assertFalse(self.backend.calls)

    def test_xml_input_forms_have_bounded_fields_and_no_effects(self):
        for route, expected in (('/timer', 1), ('/alarm', 2)):
            root = self.get(route, 'line=202')
            self.assertEqual(root.tag, 'CiscoIPPhoneInput')
            self.assertEqual(len(root.findall('InputItem')), expected)
            self.assertIn('line=202', root.find('URL').text)
        self.assertFalse(self.gateway.calls)
        self.assertTrue(all(call[2] == 'status' for call in self.backend.calls))

    def test_timer_start_validates_and_never_replaces_sibling_schedule(self):
        for value in ('0', '1441', '-1', '1.5', '00000', 'http://bad'):
            with self.subTest(value=value), self.assertRaises(apps.AppError):
                self.get('/prepare', 'op=timer_start&minutes=' + value)
        self.backend.value['timer'].update(line='202', active=True)
        with self.assertRaises(apps.AppError): self.get('/prepare', 'op=timer_start&minutes=5')
        self.backend.value['timer']['active'] = False
        url = self.confirmation('/prepare', 'op=timer_start&minutes=5&line=202')
        self.click(url)
        phone, line, operation, params = self.backend.calls[-1]
        self.assertEqual((phone, line, operation, params['minutes'], params['replace']),
                         ('201', '202', 'timer_start', 5, False))
        self.assertRegex(params['request_id'], '^apps-[0-9a-f]{32}$')

    def test_alarm_form_days_and_time_and_enable(self):
        for at, days in (('2400', '12345'), ('1260', '12345'), ('730', '1'), ('0730', '0'),
                         ('0730', '11'), ('0730', '12345678'), ('0730', '')):
            with self.subTest(at=at, days=days), self.assertRaises(apps.AppError):
                self.get('/prepare', 'op=alarm_set&at=' + at + '&days=' + days)
        url = self.confirmation('/prepare', 'op=alarm_set&at=0730&days=531&line=202')
        self.click(url)
        params = self.backend.calls[-1][3]
        self.assertEqual(params['time'], '07:30')
        self.assertEqual(params['weekdays'], [0, 2, 4])
        self.assertTrue(params['enabled'])
        self.assertFalse(params['replace'])

    def test_cancel_absent_timer_and_enable_absent_alarm_are_noops(self):
        for op in ('timer_cancel', 'alarm_enable', 'alarm_disable'):
            root = self.get('/prepare', 'op=' + op)
            self.assertEqual(root.tag, 'CiscoIPPhoneText')
            self.assertIn('Nothing was changed', root.find('Text').text)
        self.assertTrue(all(call[2] == 'status' for call in self.backend.calls))
        self.assertFalse(self.app.tokens)

    def test_cancel_expired_countdown_still_stops_its_owned_ringing_alert(self):
        self.backend.value['timer'].update(active=False, line='202')
        self.backend.value['alert'] = {'id': 'e' * 32, 'kind': 'timer', 'state': 'ringing', 'line': '202'}
        url = self.confirmation('/prepare', 'line=202&op=timer_cancel')
        self.assertEqual(self.backend.calls[-1][2], 'status')
        self.click(url)
        self.assertEqual(self.backend.calls[-1][1:3], ('202', 'timer_cancel'))
        self.assertRegex(self.backend.calls[-1][3]['request_id'], '^apps-[0-9a-f]{32}$')

    def test_cancel_expired_timer_checks_alert_line_even_if_slot_claims_selected_line(self):
        self.backend.value['timer'].update(active=False, line='201')
        self.backend.value['alert'] = {'id': 'e' * 32, 'kind': 'timer', 'state': 'ringing', 'line': '202'}
        with self.assertRaises(apps.AppError):
            self.get('/prepare', 'line=201&op=timer_cancel')
        self.assertFalse(self.app.tokens)
        self.assertTrue(all(call[2] == 'status' for call in self.backend.calls))

    def test_inactive_countdown_and_terminal_or_alarm_alert_remain_noops(self):
        for kind, state in (('timer', 'ended'), ('alarm', 'ringing')):
            self.backend.value['alert'] = {'id': 'e' * 32, 'kind': kind, 'state': state, 'line': '201'}
            root = self.get('/prepare', 'op=timer_cancel')
            self.assertEqual(root.tag, 'CiscoIPPhoneText')
            self.assertIn('Nothing was changed', root.find('Text').text)
        self.assertFalse(self.app.tokens)

    def test_stale_gateway_does_not_claim_an_expired_timer_has_no_alert(self):
        self.backend.value['gateway_fresh'] = False
        url = self.confirmation('/prepare', 'op=timer_cancel')
        self.click(url)
        self.assertEqual(self.backend.calls[-1][2], 'timer_cancel')

    def test_existing_sibling_alarm_requires_selecting_its_line(self):
        self.backend.value['alarm'].update(configured=True, line='202')
        for op in ('alarm_enable', 'alarm_disable', 'alarm_set&at=0730&days=1'):
            with self.subTest(op=op), self.assertRaises(apps.AppError):
                self.get('/prepare', 'op=' + op)
        url = self.confirmation('/prepare', 'line=202&op=alarm_disable')
        self.click(url)
        self.assertEqual(self.backend.calls[-1][1:3], ('202', 'alarm_disable'))

    def test_schedule_identity_mismatch_never_issues_confirmation(self):
        self.backend.value['phone'] = '203'
        with self.assertRaises(apps.AppError): self.get('/prepare', 'op=timer_start&minutes=5')
        self.assertFalse(self.app.tokens)

    def test_stop_alert_uses_observed_owner_and_identity(self):
        root = self.get('/stop-alert')
        self.assertIn('no active', root.find('Text').text)
        self.gateway.alert = {'id': 'f' * 32, 'line': '202', 'state': 'ringing'}
        url = self.confirmation('/stop-alert', 'line=201')
        self.click(url)
        self.assertEqual(self.gateway.calls[0][1], 'alert_stop')
        self.assertEqual(self.gateway.calls[0][2]['alert_id'], 'f' * 32)
        self.assertEqual(self.gateway.calls[0][2]['line'], '202')

    def test_alert_stop_pending_is_not_completed_and_refresh_only_reads_status(self):
        for state in ('starting', 'ringing', 'stopping', 'unknown'):
            with self.subTest(state=state):
                self.gateway.calls.clear()
                self.gateway.stop_state = state
                self.gateway.alert = {'id': 'f' * 32, 'line': '202', 'kind': 'timer', 'state': state}
                url = self.confirmation('/stop-alert', 'line=201')
                root = self.click(url)
                self.assertEqual(root.find('Title').text, 'Stop requested')
                text = root.find('Text').text
                self.assertIn('cleanup is still pending', text)
                self.assertIn('may continue ringing', text)
                self.assertIn('Line: 202', text)
                self.assertIn('Alert ID: ' + 'f' * 32, text)
                self.assertIn('State: ' + state, text)
                refreshed = self.refresh(root)
                self.assertEqual(refreshed.find('Title').text, 'Phone Status')
                self.assertIn('Alert: timer on line 202, ' + state, refreshed.find('Text').text)
                self.refresh(refreshed)
                self.assertEqual(len(self.gateway.calls), 1)
                with self.assertRaises(apps.AppError):
                    self.click(url)
                self.assertEqual(len(self.gateway.calls), 1)

    def test_alert_stop_ended_is_reported_as_stopped(self):
        self.gateway.alert = {'id': 'f' * 32, 'line': '202', 'kind': 'alarm', 'state': 'ringing'}
        root = self.click(self.confirmation('/stop-alert', 'line=201'))
        self.assertEqual(root.find('Title').text, 'Alert stopped')
        self.assertIn('State: ended', root.find('Text').text)
        self.assertNotIn('pending', root.find('Text').text)
        self.gateway.alert['state'] = 'ended'
        refreshed = self.refresh(root)
        self.assertIn('Last alert: alarm on line 202, ended', refreshed.find('Text').text)
        self.assertEqual(len(self.gateway.calls), 1)

    def test_alert_stop_rejects_unconfirmed_or_mismatched_result_without_replay(self):
        self.gateway.alert = {'id': 'f' * 32, 'line': '202', 'kind': 'timer', 'state': 'ringing'}
        changes = ({'state': None}, {'state': []}, {'state': 'private response text'},
                   {'alert_id': 'e' * 32}, {'action_id': 'wrong-action'}, {'status': 'ok'})
        for change in changes:
            with self.subTest(change=change):
                url = self.confirmation('/stop-alert', 'line=201')
                token = parse_qs(urlsplit(url).query)['token'][0]
                response = {'status': 'accepted', 'action_id': 'apps-' + token,
                            'alert_id': 'f' * 32, 'state': 'ended', **change}
                with patch.object(self.gateway, 'mutate', return_value=response) as mutate:
                    with self.assertRaises(apps.AppError):
                        self.click(url)
                    with self.assertRaises(apps.AppError):
                        self.click(url)
                    mutate.assert_called_once()
        data, _ = apps.PhoneApps.error('alert_stop_not_confirmed')
        text = ET.fromstring(data).find('Text').text
        self.assertIn('unconfirmed', text)
        self.assertIn('Phone Status', text)

    def test_schedule_shows_expired_timer_alert_and_refresh_does_not_cancel_it(self):
        self.backend.value['timer'].update(line='202', active=False)
        self.backend.value['alert'] = {'id': 'f' * 32, 'line': '202', 'kind': 'timer', 'state': 'ringing'}
        root = self.get('/schedule-status', 'line=201')
        text = root.find('Text').text
        self.assertIn('Timer line 202: off', text)
        self.assertIn('Alert: timer on line 202, ringing', text)
        self.assertIn('a timer alert has not ended', text)
        self.assertIn('Use Stop ringing alert', text)
        self.refresh(root)
        self.assertEqual([call[1:3] for call in self.backend.calls],
                         [('201', 'status'), ('201', 'status')])
        self.assertFalse(self.gateway.calls)
        self.assertFalse(self.app.tokens)

    def test_schedule_alerts_distinguish_active_ended_absent_and_stale(self):
        for kind in ('timer', 'alarm'):
            for state in ('starting', 'ringing', 'stopping', 'unknown', 'ended'):
                with self.subTest(kind=kind, state=state):
                    self.backend.value['alert'] = {'id': 'f' * 32, 'line': '202',
                                                  'kind': kind, 'state': state}
                    text = self.get('/schedule-status').find('Text').text
                    label = 'Last alert' if state == 'ended' else 'Alert'
                    self.assertIn(f'{label}: {kind} on line 202, {state}', text)
                    if state == 'ended' or kind == 'alarm':
                        self.assertNotIn('a timer alert has not ended', text)
        self.backend.value['alert'] = None
        self.assertIn('Alert: None', self.get('/schedule-status').find('Text').text)
        self.backend.value['gateway_fresh'] = False
        text = self.get('/schedule-status').find('Text').text
        self.assertIn('Current alert state is unknown', text)
        self.assertNotIn('Alert: None', text)
        self.backend.value['alert'] = {'id': 'f' * 32, 'line': '202', 'kind': 'timer', 'state': 'ringing'}
        text = self.get('/schedule-status').find('Text').text
        self.assertIn('Current alert state is unknown', text)
        self.assertIn('Last observed alert: timer on line 202, ringing', text)
        self.assertNotIn('Use Stop ringing alert', text)
        self.assertTrue(all(call[2] == 'status' for call in self.backend.calls))
        self.assertFalse(self.gateway.calls)

    def test_schedule_and_phone_status_render_without_side_effects(self):
        self.backend.value['timer'].update(active=True, remaining_seconds=93)
        self.backend.value['alarm'].update(configured=True, enabled=True)
        schedule = self.get('/schedule-status').find('Text').text
        self.assertIn('93 sec remaining', schedule)
        self.assertIn('07:30 Mon Tue Wed Thu Fri', schedule)
        status = self.get('/status').find('Text').text
        self.assertIn('Registered: Yes', status)
        self.assertIn('Office & Lab', status)
        self.assertFalse(self.gateway.calls)

    def test_optional_backend_is_explicitly_unavailable(self):
        self.app.backend = None
        root = self.get('/schedules')
        self.assertIn('not configured', root.find('Text').text)
        for route in ('/home', '/timer', '/alarm', '/cameras'):
            with self.subTest(route=route), self.assertRaises(apps.AppError): self.get(route)

    def test_entity_read_only_and_allowlist_cannot_be_bypassed(self):
        root = self.get('/entity', 'id=temp')
        self.assertEqual(root.find('Text').text, '21 C')
        for query in ('id=temp&enabled=1', 'id=unknown&enabled=1', 'id=lamp&enabled=toggle'):
            with self.subTest(query=query), self.assertRaises(apps.AppError): self.get('/entity-set', query)
        self.assertFalse(self.backend.calls)

    def test_entity_mutation_is_confirmed_and_phone_scoped(self):
        url = self.confirmation('/entity-set', 'id=lamp&enabled=1&line=202')
        self.assertFalse(self.backend.calls)
        self.click(url)
        self.assertEqual(self.backend.calls, [('entity', '201', 'lamp', 'on')])

    def test_camera_returns_native_image_with_scoped_ephemeral_resource(self):
        root = self.get('/camera', 'id=door')
        self.assertEqual(root.tag, 'CiscoIPPhoneImageFile')
        self.assertEqual(root.find('URL').text, 'http://192.168.50.10:18080/content/opaque')
        self.assertEqual(self.gateway.resources, [('201', b'PNG test fixture', 'image/png')])
        self.assertEqual(self.backend.calls, [('camera', '201', 'door')])
        with self.assertRaises(apps.AppError): self.get('/camera', 'id=camera.arbitrary')

    def test_error_never_reflects_unknown_backend_details(self):
        root = ET.fromstring(self.app.error('secret=http://private/token')[0])
        self.assertNotIn('private', ET.tostring(root).decode())
        self.assertNotIn('token', ET.tostring(root).decode())
        self.assertIn('Do not repeat', root.find('Text').text)

    def test_safe_text_removes_invalid_xml_characters(self):
        value = self.app.text('201', '201', 'Demo\ud800', 'First\x00 & second\uffff')
        root = ET.fromstring(value[0])
        self.assertEqual(root.find('Text').text, 'First & second')


class PhoneAppsHTTPTests(unittest.TestCase):
    """Exercise the real gateway handler and its phone/source boundary."""
    def setUp(self):
        gateway_spec = importlib.util.spec_from_file_location('apps_http_gateway',
                Path(__file__).resolve().parents[1] / 'containers/asterisk/phone_gateway.py')
        gateway_module = importlib.util.module_from_spec(gateway_spec)
        gateway_spec.loader.exec_module(gateway_module)
        self.gateway = FakeGateway()
        self.gateway.apps = apps.PhoneApps(self.gateway, FakeBackend())
        self.gateway.resources = {}
        self.phone = self.gateway.phones['201']
        self.phone.update(ipv4='127.0.0.1', auth_path='/auth/' + 'c' * 64)
        self.server = gateway_module.ThreadingHTTPServer(('127.0.0.1', 0), gateway_module.Handler)
        self.server.gateway, self.server.phone = self.gateway, self.phone
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path='', query='', method='GET', body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        target = self.phone['apps_path'] + path + ('?' + query if query else '')
        try:
            connection.request(method, target, body=body, headers=headers or {})
            result = connection.getresponse()
            return result.status, result.read(), dict(result.getheaders())
        finally:
            connection.close()

    def test_private_xml_menu_has_no_store_and_correct_content_type(self):
        status, data, headers = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(ET.fromstring(data).tag, 'CiscoIPPhoneMenu')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['Content-Type'], apps.MIME)

    def test_forwarded_header_does_not_bypass_actual_phone_source(self):
        self.phone['ipv4'] = '192.168.50.22'
        self.assertEqual(self.request(headers={'X-Forwarded-For': '192.168.50.22'})[0], 403)
        self.assertFalse(self.gateway.calls)

    def test_similar_path_prefix_does_not_match_application(self):
        self.assertEqual(self.request('extra')[0], 404)

    def test_bad_query_is_native_error_page_but_post_and_body_are_rejected(self):
        status, data, _ = self.request(query='line=201&line=202')
        self.assertEqual(status, 200)
        self.assertEqual(ET.fromstring(data).tag, 'CiscoIPPhoneText')
        self.assertEqual(self.request(method='POST')[0], 400)
        self.assertEqual(self.request(body=b'x')[0], 413)
        self.assertFalse(self.gateway.calls)

    def test_backend_failure_and_replay_render_safely_without_second_action(self):
        status, data, _ = self.request('/set', 'setting=dnd&enabled=1&line=202')
        self.assertEqual(status, 200)
        target = urlsplit(ET.fromstring(data).find('MenuItem/URL').text)
        self.gateway.fail = True
        for _ in range(2):
            status, data, _ = self.request('/apply', target.query)
            self.assertEqual(status, 200)
            self.assertEqual(ET.fromstring(data).tag, 'CiscoIPPhoneText')
            self.assertNotIn(b'Private backend URL', data)
        self.assertEqual(len(self.gateway.calls), 1)


if __name__ == '__main__':
    unittest.main()
