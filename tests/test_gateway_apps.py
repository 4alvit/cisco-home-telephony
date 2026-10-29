"""Exercise XML services through the actual source-restricted HTTP listener."""
import copy
from contextlib import contextmanager
import http.client
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from urllib.parse import urlsplit, urlencode
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from test_phone_gateway import FakeAMI, config, gateway


class GatewayAppsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        value = config()
        value['applications'] = {'enabled': True}
        value['phones'][0]['apps_path'] = '/apps/' + 'd' * 64
        self.gateway = gateway.Gateway(value, self.temp.name)
        self.addCleanup(self.gateway.db.close)
        self.phone = copy.deepcopy(value['phones'][0])
        self.phone['ipv4'] = '127.0.0.1'
        self.base = self.phone['apps_path']
        self.server = gateway.ThreadingHTTPServer(('127.0.0.1', 0), gateway.Handler)
        self.server.gateway, self.server.phone = self.gateway, self.phone
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever,
                         kwargs={'poll_interval': 0.01}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, method='GET', body=None, headers=None, with_headers=False):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            result = (response.status, response.read(), response.getheader('Content-Type'))
            return result + (dict(response.getheaders()),) if with_headers else result
        finally:
            connection.close()

    def test_source_and_nonce_restrictions_precede_app_rendering(self):
        status, raw, mime = self.request(self.base)
        self.assertEqual(status, 200)
        self.assertTrue(mime.startswith('text/xml'))
        self.assertEqual(ET.fromstring(raw).tag, 'CiscoIPPhoneMenu')
        self.assertEqual(self.request('/apps/' + 'e' * 64)[0], 404)
        self.assertEqual(self.request(self.base + 'other')[0], 404)
        self.phone['ipv4'] = '192.168.50.21'
        self.assertEqual(self.request(self.base, headers={'X-Forwarded-For': self.phone['ipv4']})[0], 403)

    def test_confirmed_mutation_runs_once_and_preserves_line_mapping(self):
        status, raw, _ = self.request(self.base + '/set?line=202&setting=autoanswer&enabled=1')
        self.assertFalse(self.gateway.preference('201')['autoanswer'])
        link = ET.fromstring(raw).findtext('MenuItem/URL')
        target = urlsplit(link)
        status, raw, _ = self.request(target.path + '?' + target.query)
        self.assertEqual(status, 200)
        self.assertIn(b'Action completed', raw)
        self.assertTrue(self.gateway.preference('201')['autoanswer'])
        with patch.object(self.gateway, 'mutate') as mutate:
            self.assertIn(b'Confirmation expired', self.request(target.path + '?' + target.query)[1])
            mutate.assert_not_called()
        self.assertIn(b'does not belong', self.request(self.base + '/set?line=299&setting=dnd&enabled=1')[1])

    def test_backend_errors_are_native_xml_without_sensitive_details(self):
        with patch.object(self.gateway.apps, 'handle', side_effect=RuntimeError('private-token-and-url')):
            status, raw, mime = self.request(self.base)
        self.assertEqual(status, 200)
        self.assertEqual(ET.fromstring(raw).tag, 'CiscoIPPhoneText')
        self.assertNotIn(b'private-token', raw)
        self.assertTrue(mime.startswith('text/xml'))

    def test_app_wait_does_not_block_phone_authentication_callback(self):
        entered, finished = threading.Event(), threading.Event()
        def slow(*_args):
            entered.set()
            self.assertTrue(finished.wait(2))
            return self.gateway.apps.error('backend_unavailable')
        with patch.object(self.gateway.apps, 'handle', side_effect=slow):
            thread = threading.Thread(target=lambda: self.request(self.base))
            thread.start()
            self.assertTrue(entered.wait(1))
            fields = {key: self.phone[value] for key, value in
                      [('UserID', 'username'), ('Password', 'password'), ('devicename', 'device')]}
            status, raw, _ = self.request(self.phone['auth_path'] + '?' + urlencode(fields))
            finished.set()
            thread.join(2)
        self.assertEqual((status, raw), (200, b'AUTHORIZED'))
        self.assertFalse(thread.is_alive())

    def test_mutating_http_methods_and_bodies_are_not_accepted(self):
        with patch.object(self.gateway.apps, 'handle') as handle:
            self.assertEqual(self.request(self.base, 'POST')[0], 400)
            self.assertEqual(self.request(self.base, body=b'x')[0], 413)
            handle.assert_not_called()

    def test_disabled_apps_have_no_listener_route(self):
        self.gateway.apps = None
        self.assertEqual(self.request(self.base)[0], 404)

    @staticmethod
    def ha_config():
        return {'url': 'http://192.168.50.2:8123', 'token': 't' * 48,
                'entry_id': 'example-entry',
                'cameras': [{'id': 'door', 'label': 'Door', 'entity_id': 'camera.example'}]}

    def test_camera_configuration_is_strict_and_requires_explicit_ha_optin(self):
        defaults = gateway.camera_preview_options({})
        self.assertEqual(defaults, {'enabled': False, 'refresh_seconds': 3,
                                    'ttl_seconds': 60, 'stale_seconds': 10})
        for options in (None, [], {'enabled': 1}, {'unknown': 1},
                        {'refresh_seconds': True}, {'refresh_seconds': 1},
                        {'refresh_seconds': 6}, {'ttl_seconds': 4},
                        {'ttl_seconds': 121}, {'stale_seconds': 31},
                        {'stale_seconds': 2}, {'ttl_seconds': 10.0}):
            with self.subTest(options=options), self.assertRaises(gateway.Refused):
                gateway.camera_preview_options(options)
        value = copy.deepcopy(self.gateway.config)
        value['applications']['camera_preview'] = {'enabled': True}
        with self.assertRaises(gateway.Refused):
            gateway.validate(value)
        value['applications']['home_assistant'] = self.ha_config()
        gateway.validate(value)
        value['applications']['enabled'] = False
        with self.assertRaises(gateway.Refused):
            gateway.validate(value)

    def test_camera_controller_creation_has_no_ha_fetch_or_phone_action(self):
        value = copy.deepcopy(self.gateway.config)
        value['applications'].update(home_assistant=self.ha_config(),
                                      camera_preview={'enabled': True})
        ha = gateway.app_module('phone_apps_ha')
        with patch.object(ha.HomeAssistantApps, '_request') as fetch, \
                patch.object(gateway, 'execute') as execute:
            instance = gateway.Gateway(value, self.temp.name)
            try:
                self.assertIsNotNone(instance.camera_preview)
                instance.camera_preview.close()
            finally:
                instance.db.close()
        fetch.assert_not_called()
        execute.assert_not_called()

    def test_preview_http_preserves_refresh_cache_headers_and_image_type(self):
        module = gateway.app_module('phone_camera_preview')
        self.gateway.camera_preview = object()
        url = (f'http://{self.gateway.config["bind_ipv4"]}:{self.phone["port"]}'
               + self.base + '/camera-preview/session/' + 'a' * 32)
        result = module.DisplayResponse(
            200, b'<CiscoIPPhoneText/>', 'text/xml; charset=utf-8',
            {'Refresh': '3; url=' + url, 'Cache-Control': 'no-store, no-cache',
             'Pragma': 'no-cache', 'Expires': 'Thu, 01 Jan 1970 00:00:00 GMT'}, True)
        with patch.object(self.gateway.apps, 'handle', return_value=result):
            status, data, mime, headers = self.request(
                self.base + '/camera-preview/session/' + 'a' * 32, with_headers=True)
        self.assertEqual((status, data, mime), (200, result.body, result.content_type))
        self.assertEqual(headers['Refresh'], '3; url=' + url)
        self.assertEqual(headers['Cache-Control'], 'no-store, no-cache')
        result = module.DisplayResponse(200, b'PNG fixture', 'image/png',
                                        {'Cache-Control': 'no-store'}, True)
        with patch.object(self.gateway.apps, 'handle', return_value=result):
            self.assertEqual(self.request(self.base)[0:3], (200, b'PNG fixture', 'image/png'))

    def test_preview_preemption_never_transmits_old_image_or_refresh(self):
        module = gateway.app_module('phone_camera_preview')
        self.gateway.camera_preview = object()
        result = module.DisplayResponse(
            204, b'old private frame', 'image/png', {'Refresh': '3; url=ignored'}, False)
        with patch.object(self.gateway.apps, 'handle', return_value=result):
            status, data, _, headers = self.request(self.base, with_headers=True)
        self.assertEqual((status, data), (204, b''))
        self.assertNotIn('Refresh', headers)

    def test_real_preview_routes_image_refresh_and_permanent_call_preemption(self):
        from PIL import Image
        module = gateway.app_module('phone_camera_preview')
        self.prepare_observer()
        buffer = io.BytesIO()
        Image.new('RGB', (12, 8), (10, 80, 120)).save(buffer, format='PNG')
        def camera(_phone, _camera):
            self.assertFalse(self.held)
            return buffer.getvalue()
        backend = SimpleNamespace(
            cameras=Mock(return_value=[{'id': 'door', 'label': 'Synthetic Door'}]),
            camera=Mock(side_effect=camera))
        self.gateway.apps.backend = backend
        elapsed = [0]
        self.gateway.camera_preview = module.CameraPreviewController(
            backend, {'201': ('201', '202')},
            {'201': self.gateway.apps.url('201', '/camera-preview')},
            authorize=self.gateway._camera_authorize, observe=self.gateway._camera_observe,
            clock=lambda: time.monotonic() + elapsed[0])
        self.addCleanup(self.gateway.camera_preview.close)
        status, data, _, headers = self.request(
            self.base + '/camera-preview?id=door&line=202', with_headers=True)
        self.assertEqual(status, 200)
        root = ET.fromstring(data)
        self.assertEqual(root.tag, 'CiscoIPPhoneImageFile')
        self.assertEqual(root.findtext('Prompt'), 'Capture time unknown; Details')
        self.assertLessEqual(len(root.findtext('Prompt')), 32)
        image_url = urlsplit(root.findtext('URL'))
        page_url = urlsplit(headers['Refresh'].split('url=', 1)[1])
        status, image, mime = self.request(image_url.path + '?' + image_url.query)
        self.assertEqual((status, mime), (200, 'image/png'))
        with Image.open(io.BytesIO(image)) as result:
            self.assertEqual(result.size, (298, 156))
        self.assertEqual(backend.camera.call_count, 1)
        status, data, mime, headers = self.request(
            image_url.path + '?v=99999', with_headers=True)
        self.assertEqual(status, 404)
        self.assertTrue(mime.startswith('text/xml'))
        self.assertNotIn('Refresh', headers)
        self.assertNotIn(buffer.getvalue(), data)
        self.phone['ipv4'] = '192.168.50.21'
        self.assertEqual(self.request(page_url.path)[0], 403)
        self.phone['ipv4'] = '127.0.0.1'
        FakeAMI.endpoints['202'] = 'Ringing'
        status, data, _, headers = self.request(page_url.path, with_headers=True)
        self.assertEqual((status, data), (204, b''))
        self.assertNotIn('Refresh', headers)
        FakeAMI.endpoints['202'] = 'Not in use'
        self.assertEqual(self.request(page_url.path)[0:2], (204, b''))
        self.assertEqual(backend.camera.call_count, 1)
        self.assertFalse(self.held)
        elapsed[0] = 61
        status, _, mime, headers = self.request(page_url.path, with_headers=True)
        self.assertEqual(status, 404)
        self.assertTrue(mime.startswith('text/xml'))
        self.assertNotIn('Refresh', headers)

    def test_preview_headers_cannot_redirect_or_escape_the_phone_namespace(self):
        module = gateway.app_module('phone_camera_preview')
        self.gateway.camera_preview = object()
        for headers in ({'Set-Cookie': 'private'},
                        {'Refresh': '3; url=http://example.invalid/private'},
                        {'Pragma': 'no-cache\r\nX-Secret: private'}):
            with self.subTest(headers=headers):
                result = module.DisplayResponse(
                    200, b'private frame', 'image/png', headers, True)
                with patch.object(self.gateway.apps, 'handle', return_value=result):
                    _, data, mime, returned = self.request(self.base, with_headers=True)
                self.assertTrue(mime.startswith('text/xml'))
                self.assertNotIn(b'private', data)
                self.assertNotIn('Set-Cookie', returned)
                self.assertNotIn('Refresh', returned)
                self.assertNotIn('X-Secret', returned)

    def prepare_observer(self):
        FakeAMI.endpoints = {'201': 'Not in use', '202': 'Not in use'}
        FakeAMI.channels, FakeAMI.calls = [], []
        self.gateway.voice = SimpleNamespace(status=Mock(return_value={
            'state': 'idle', 'active': False, 'error': None}))
        self.gateway._voice_phone_proof = object()
        self.held = False
        @contextmanager
        def reserve(*phones):
            self.assertEqual([phone['id'] for phone in phones], ['201'])
            self.held = True
            try:
                yield
            finally:
                self.held = False
        self.reserve_patch = patch.object(self.gateway, 'reserve_phone_controls', reserve)
        self.reserve_patch.start()
        self.addCleanup(self.reserve_patch.stop)
        self.ami_patch = patch.object(gateway, 'AMI', FakeAMI)
        self.ami_patch.start()
        self.addCleanup(self.ami_patch.stop)
        return self.gateway._phone_camera_context('201')

    def test_camera_observation_requires_every_physical_line_idle(self):
        context = self.prepare_observer()
        with patch.object(self.gateway, 'listing') as listing, \
                patch.object(self.gateway, 'mutate') as mutate, \
                patch.object(gateway, 'execute') as execute:
            value = self.gateway._camera_observe('201', context)
            self.assertEqual(value.lines, {'201': 'idle', '202': 'idle'})
            self.assertEqual(value.voice, 'idle')
            for state in ('In use', 'Ringing', 'Unavailable', 'Unknown', None):
                FakeAMI.endpoints['202'] = state
                value = self.gateway._camera_observe('201', context)
                self.assertNotEqual(value.lines['202'], 'idle')
            FakeAMI.endpoints['202'] = 'Not in use'
            FakeAMI.channels = [{'Channel': 'PJSIP/202-00000001', 'ChannelStateDesc': 'Up'}]
            value = self.gateway._camera_observe('201', context)
            self.assertFalse(all(state == 'idle' for state in value.lines.values()))
            listing.assert_not_called()
            mutate.assert_not_called()
            execute.assert_not_called()
        self.assertFalse(self.held)
        self.assertFalse(FakeAMI.calls)

    def test_camera_observation_refuses_transient_lease_contention(self):
        context = self.prepare_observer()
        @contextmanager
        def busy(*_phones):
            raise gateway.Refused('voice_phone_busy')
            yield
        with patch.object(self.gateway, 'reserve_phone_controls', busy):
            self.assertEqual(self.gateway._camera_observe('201', context).voice, 'unknown')
        self.assertEqual(self.gateway._camera_observe('201', context).voice, 'idle')
        self.assertFalse(self.held)
        self.gateway.voice = None
        self.assertEqual(self.gateway._camera_observe('201', context).voice, 'unknown')

    def test_real_lease_allows_unprofiled_phone_but_blocks_external_owner_and_journal(self):
        core = gateway.app_module('phone_voice_control')
        workspace = Path(self.temp.name).resolve()
        profile = core.Profile(
            '203', ('203', '204'), 'SEP020000000002', '192.168.50.22',
            workspace / '.local/transport.json', workspace / '.local/voice.json',
            10, '192.168.50.10', 24576)
        config = core.ControlConfig(
            workspace, workspace / '.local/voice-state', sys.executable, sys.executable,
            (profile,), True)
        phone = self.gateway.phones['201']
        control_phone = core.ControlPhone('201', ('201', '202'), phone['device'])
        inventory = (control_phone, core.ControlPhone('203', ('203', '204'), profile.device))
        runner = Mock(side_effect=AssertionError('A camera preview cannot launch voice'))
        managers = [core.VoiceControl(
            config, authorize=lambda *_: True, monitor=False,
            runner_factory=runner, control_phones=inventory) for _ in range(2)]
        for manager in managers:
            self.addCleanup(manager.close)
        self.gateway.voice, external = managers
        self.gateway._voice_module = core
        self.gateway._voice_phone_proof = object()
        context = self.gateway._phone_camera_context('201')
        FakeAMI.endpoints = {'201': 'Not in use', '202': 'Not in use'}
        FakeAMI.channels, FakeAMI.calls = [], []
        with patch.object(gateway, 'AMI', FakeAMI), patch.object(gateway, 'execute') as execute:
            with self.assertRaisesRegex(core.ControlError, 'unknown_voice_phone'):
                self.gateway.voice.status('201', context={})
            self.assertEqual(self.gateway._camera_observe('201', context).voice, 'idle')
            # A second real manager holds the flock before writing any journal.
            with external.reserve_control('202', context={}):
                self.assertEqual(self.gateway._camera_observe('201', context).voice, 'unknown')
            self.assertEqual(self.gateway._camera_observe('201', context).voice, 'idle')
            journal = external._paths(control_phone)[1]
            journal.write_text(json.dumps({
                'schema_version': 1, 'phone': '201', 'line': '202',
                'session_id': 'a' * 32, 'state': 'recovery_required',
                'active': False, 'remaining_seconds': 0, 'stop_requested': True,
                'stop_confirmed': False, 'error': 'voice_owner_unavailable',
                'reserved': True}))
            journal.chmod(0o600)
            before = journal.read_bytes()
            self.assertEqual(self.gateway._camera_observe('201', context).voice, 'unknown')
            self.assertEqual(journal.read_bytes(), before)
            execute.assert_not_called()
        self.assertFalse(FakeAMI.calls)
        runner.assert_not_called()

    def test_camera_observation_proof_and_request_start_timestamp(self):
        context = self.prepare_observer()
        self.assertEqual(self.gateway._camera_observe('201', object()).voice, 'unknown')
        self.gateway.voice.status.assert_not_called()
        now = [100.0]
        def slow_snapshot(_ami):
            self.assertTrue(self.held)
            now[0] = 110.0
            return dict(FakeAMI.endpoints), []
        with patch.object(gateway.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(FakeAMI, 'snapshot', slow_snapshot):
            value = self.gateway._camera_observe('201', context)
        self.assertEqual(value.observed_at, 100.0)
        self.assertFalse(self.held)
        with patch.object(FakeAMI, 'snapshot', side_effect=OSError('synthetic AMI failure')):
            self.assertEqual(self.gateway._camera_observe('201', context).voice, 'unknown')
        self.assertFalse(self.held)


if __name__ == '__main__':
    unittest.main()
