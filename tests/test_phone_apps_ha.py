"""HA adapter boundaries: identity, allowlists, no redirects and confirmations."""
import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib import error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'containers' / 'asterisk'))
import phone_apps_ha as ha


def config():
    return {'url': 'http://192.168.50.10:8123', 'token': 'synthetic-token-' + 'x' * 40,
            'entry_id': 'synthetic_entry',
            'entities': [{'id': 'lamp', 'label': 'Desk lamp', 'entity_id': 'light.desk', 'control': True},
                         {'id': 'weather', 'label': 'Temperature', 'entity_id': 'sensor.temperature'}],
            'cameras': [{'id': 'view', 'label': 'Camera', 'entity_id': 'camera.test_view'}]}


def schedule():
    return {'schema_version': 1, 'phone': '212', 'line': '212', 'lines': ['212', '213'],
            'timezone': 'America/Los_Angeles', 'gateway_fresh': True,
            'timer': {'line': '212', 'id': None, 'active': False, 'deadline': None, 'remaining_seconds': None},
            'alarm': {'line': '212', 'time': '07:30', 'weekdays': [0, 1, 2, 3, 4],
                      'configured': False, 'enabled': False},
            'alert': None, 'operation_result': 'status'}


class Response(io.BytesIO):
    status = 200
    headers = {}


class AppsHATests(unittest.TestCase):
    def setUp(self):
        self.app = ha.HomeAssistantApps(config())

    def test_only_private_ip_origin_without_userinfo_path_or_redirect(self):
        for url in ('http://ha.local', 'http://127.0.0.1:8123', 'http://169.254.169.254',
                    'http://8.8.8.8', 'http://192.168.50.10/path', 'http://user@192.168.50.10',
                    'http://192.168.50.10?token=x', 'http://192.168.50.10/#x',
                    'http://192.168.50.10:70000', 'https://[::1]'):
            with self.subTest(url=url), self.assertRaises(ha.AppsHAError):
                ha.validate_config({**config(), 'url': url})
        value = ha.validate_config({**config(), 'url': 'https://192.168.50.10/'})
        self.assertEqual(value['url'], 'https://192.168.50.10')
        self.assertIsNone(ha.NoRedirect().redirect_request(None))

    def test_config_cannot_escalate_entity_control_or_duplicate_keys(self):
        cases = [
            {'id': 'bad', 'label': 'Bad', 'entity_id': 'lock.front', 'control': True},
            {'id': 'bad', 'label': 'Bad', 'entity_id': 'light.bad/../../api/services'},
            {'id': 'bad', 'label': 'Bad', 'entity_id': 'light.test', 'url': 'http://elsewhere'},
            {'id': 'bad', 'label': 'Bad\nHeader', 'entity_id': 'light.test'},
        ]
        for entry in cases:
            with self.subTest(entry=entry), self.assertRaises(ha.AppsHAError):
                ha.validate_config({**config(), 'entities': [entry]})
        with self.assertRaises(ha.AppsHAError):
            ha.validate_config({**config(), 'entities': [config()['entities'][0]] * 2})
        with self.assertRaises(ha.AppsHAError):
            ha.validate_config({**config(), 'cameras': [{'id': 'view', 'label': 'View', 'entity_id': 'sensor.test'}]})

    def test_schedule_checks_line_identity_and_strips_unrelated_attributes(self):
        value = schedule()
        value['private'] = 'secret'
        with patch.object(self.app, '_request', return_value={'service_response': value, 'changed_states': ['secret']}):
            clean = self.app.schedule('212', '212')
            self.assertNotIn('private', clean)
            value['line'] = '213'
            with self.assertRaisesRegex(ha.AppsHAError, 'ha_schedule_identity_mismatch'):
                self.app.schedule('212', '212')

    def test_timer_start_validates_input_idempotency_and_actual_result(self):
        value = schedule()
        value['operation_result'] = 'applied'
        with patch.object(self.app, '_request', return_value={'service_response': value}) as call:
            for minutes in (True, 0, 1441, '5'):
                with self.subTest(minutes=minutes), self.assertRaises(ha.AppsHAError):
                    self.app.schedule('212', '212', 'timer_start', request_id='apps-test-1', minutes=minutes)
            with self.assertRaisesRegex(ha.AppsHAError, 'schedule_request_id_required'):
                self.app.schedule('212', '212', 'timer_start', minutes=5)
            call.assert_not_called()
            with self.assertRaisesRegex(ha.AppsHAError, 'ha_change_not_confirmed'):
                self.app.schedule('212', '212', 'timer_start', request_id='apps-test-1', minutes=5)
            value['timer'].update(active=True, id='a' * 32, deadline=12345, remaining_seconds=300)
            result = self.app.schedule('212', '212', 'timer_start', request_id='apps-test-1', minutes=5)
            self.assertTrue(result['timer']['active'])
            self.assertEqual(call.call_args.args[1]['request_id'], 'apps-test-1')

    def test_alarm_enable_and_disable_do_not_create_unsaved_alarm(self):
        for operation in ('alarm_enable', 'alarm_disable'):
            with self.subTest(operation=operation), patch.object(
                    self.app, '_request', return_value={'service_response': schedule()}) as call:
                result = self.app.schedule('212', '212', operation, request_id='apps-test-1')
                self.assertEqual(result['operation_result'], 'not_configured')
                self.assertEqual(call.call_count, 1)
                self.assertEqual(call.call_args.args[1]['operation'], 'status')

    def test_weekly_alarm_normalizes_days_and_checks_result(self):
        value = schedule()
        value['operation_result'] = 'applied'
        value['alarm'].update(configured=True, enabled=True, time='06:45', weekdays=[0, 2, 4])
        with patch.object(self.app, '_request', return_value={'service_response': value}) as call:
            result = self.app.schedule('212', '212', 'alarm_set', request_id='apps-test-1',
                                       time='06:45', weekdays=[4, 0, 2, 0], enabled=True)
            self.assertEqual(result['alarm']['weekdays'], [0, 2, 4])
            self.assertEqual(call.call_args.args[1]['weekdays'], [0, 2, 4])
            for days in ([], [7], [True]):
                with self.subTest(days=days), self.assertRaises(ha.AppsHAError):
                    self.app.schedule('212', '212', 'alarm_set', request_id='apps-test-1',
                                      time='06:45', weekdays=days)

    def test_uncertain_schedule_is_not_reported_as_confirmed(self):
        value = schedule()
        value['operation_result'] = 'outcome_unknown'
        with patch.object(self.app, '_request', return_value={'service_response': value}):
            self.assertEqual(self.app.schedule('212', '212', 'timer_cancel',
                             request_id='apps-test-1')['operation_result'], 'outcome_unknown')

    def test_entity_allowlist_read_and_write_are_minimal_and_confirmed(self):
        def request(path, body=None, **_):
            if path == '/api/services/light/turn_on':
                self.assertEqual(body, {'entity_id': 'light.desk'})
                return []
            if path == '/api/states/light.desk':
                return {'entity_id': 'light.desk', 'state': 'on', 'attributes': {'secret': 'hidden'}}
            if path == '/api/states/sensor.temperature':
                return {'entity_id': 'sensor.temperature', 'state': '21.5',
                        'attributes': {'unit_of_measurement': '°C', 'entity_picture': '/secret'}}
            self.fail(path)
        with patch.object(self.app, '_request', side_effect=request) as call:
            self.assertEqual(self.app.entity_action('212', 'lamp', 'on')['state'], 'on')
            rows = self.app.entities('212')
            self.assertEqual(rows[1]['unit'], '°C')
            self.assertNotIn('entity_id', rows[0])
            self.assertNotIn('attributes', rows[0])
            before = call.call_count
            for key, action in [('unlisted', 'on'), ('weather', 'on'), ('lamp', 'toggle')]:
                with self.subTest(key=key), self.assertRaises(ha.AppsHAError):
                    self.app.entity_action('212', key, action)
            self.assertEqual(call.call_count, before)

    def test_entity_wrong_identity_fails_and_write_mismatch_not_confirmed(self):
        with patch.object(self.app, '_request', return_value={'entity_id': 'light.other', 'state': 'on'}):
            self.assertEqual(self.app.entities('212')[0]['state'], 'unavailable')
        with patch.object(self.app, '_request', side_effect=[[], {'entity_id': 'light.desk', 'state': 'off'}]):
            with self.assertRaisesRegex(ha.AppsHAError, 'ha_change_not_confirmed'):
                self.app.entity_action('212', 'lamp', 'on')

    def test_network_error_is_sanitized_no_retry_and_token_only_in_header(self):
        failure = error.URLError('http://secret:token@host')
        with patch.object(self.app.opener, 'open', side_effect=failure) as call:
            with self.assertRaisesRegex(ha.AppsHAError, '^ha_request_outcome_unknown$'):
                self.app.schedule('212', '212')
            self.assertEqual(call.call_count, 1)
            req = call.call_args.args[0]
            self.assertNotIn(self.app.config['token'], req.full_url)
            self.assertEqual(req.get_header('Authorization'), 'Bearer ' + self.app.config['token'])
            self.assertNotIn(self.app.config['token'].encode(), req.data)

    def test_response_body_limit_and_http_errors_never_leak_raw_details(self):
        with patch.object(self.app.opener, 'open', return_value=Response(b'x' * (ha.MAX_JSON + 1))):
            with self.assertRaisesRegex(ha.AppsHAError, 'ha_response_too_large'):
                self.app.schedule('212', '212')
        failure = error.HTTPError('http://secret', 400, 'secret', {},
                                  io.BytesIO(json.dumps({'message': 'The timer belongs to the other line'}).encode()))
        with patch.object(self.app.opener, 'open', side_effect=failure):
            with self.assertRaisesRegex(ha.AppsHAError, '^ha_timer_owned_by_other_line$'):
                self.app.schedule('212', '212')

    def test_camera_proxy_resize_and_metadata_allowlist(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest('Pillow is installed in the runtime image')
        source = io.BytesIO()
        Image.new('RGB', (640, 480), 'purple').save(source, 'JPEG')
        with patch.object(self.app, '_request', return_value=source.getvalue()) as call:
            rendered = self.app.camera('212', 'view')
            self.assertEqual(Image.open(io.BytesIO(rendered)).size, (298, 156))
            self.assertEqual(Image.open(io.BytesIO(rendered)).format, 'PNG')
            self.assertEqual(call.call_args.args[0], '/api/camera_proxy/camera.test_view')
            self.assertEqual(self.app.cameras('212'), [{'id': 'view', 'label': 'Camera'}])
        with patch.object(self.app, '_request', return_value=b'<html>not a picture</html>'):
            with self.assertRaisesRegex(ha.AppsHAError, 'invalid_camera_image'):
                self.app.camera('212', 'view')
        with self.assertRaises(ha.AppsHAError):
            self.app.camera('212', 'http://arbitrary-host')


if __name__ == '__main__':
    unittest.main()
