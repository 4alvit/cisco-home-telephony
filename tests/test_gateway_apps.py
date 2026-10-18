"""Exercise XML services through the actual source-restricted HTTP listener."""
import copy
import http.client
import tempfile
import threading
import unittest
from urllib.parse import urlsplit, urlencode
from unittest.mock import patch
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

    def request(self, path, method='GET', body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read(), response.getheader('Content-Type')
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


if __name__ == '__main__':
    unittest.main()
