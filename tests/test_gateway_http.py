"""Real HTTP framing/authentication tests using synthetic loopback listeners."""
import copy
import http.client
import json
import tempfile
import threading
import unittest
from urllib.parse import urlencode
from unittest.mock import patch
from test_phone_gateway import FakeAMI, config, gateway


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.gateway = gateway.Gateway(config(), self.directory.name)
        self.addCleanup(self.gateway.db.close)
        self.gateway.config['api_clients'] = ['127.0.0.1']
        self.api = self.listen(None)
        self.phone = copy.deepcopy(config()['phones'][0])
        self.phone['ipv4'] = '127.0.0.1'
        self.callback = self.listen(self.phone)

    def listen(self, phone):
        server = gateway.ThreadingHTTPServer(('127.0.0.1', 0), gateway.Handler)
        server.gateway, server.phone = self.gateway, phone
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def request(self, server, path, method='GET', body=None, headers=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read(), response.getheader('Cache-Control')
        finally:
            connection.close()

    def test_api_requires_bearer_and_actual_source_not_forwarded_header(self):
        with patch.object(self.gateway, 'listing', return_value={'phones': []}):
            status, _, _ = self.request(self.api, '/v1/phones')
            self.assertEqual(status, 403)
            headers = {'Authorization': 'Bearer ' + config()['api_token']}
            self.assertEqual(self.request(self.api, '/v1/phones', headers=headers)[0], 200)
            self.gateway.config['api_clients'] = ['192.168.50.11']
            headers['X-Forwarded-For'] = '192.168.50.11'
            self.assertEqual(self.request(self.api, '/v1/phones', headers=headers)[0], 403)

    def test_cisco_actual_auth_fields_and_device_identity(self):
        fields = {'UserID': self.phone['username'], 'Password': self.phone['password'], 'devicename': self.phone['device']}
        path = self.phone['auth_path']
        status, body, cache = self.request(self.callback, path + '?' + urlencode(fields))
        self.assertEqual((status, body, cache), (200, b'AUTHORIZED', 'no-store'))
        fields['devicename'] = 'SEP000000000000'
        self.assertEqual(self.request(self.callback, path + '?' + urlencode(fields))[1], b'UNAUTHORIZED')
        fields['DeviceName'] = fields.pop('devicename')
        self.assertEqual(self.request(self.callback, path + '?' + urlencode(fields))[0], 403)

    def test_resources_cannot_cross_phone_identity(self):
        self.gateway.resources[('203', '/content/test')] = (gateway.time.time() + 60, b'private', 'text/xml')
        self.assertEqual(self.request(self.callback, '/content/test')[0], 404)
        self.gateway.resources[('201', '/content/test')] = (gateway.time.time() + 60, b'own', 'text/xml')
        self.assertEqual(self.request(self.callback, '/content/test')[:2], (200, b'own'))

    def test_malformed_json_and_transfer_encoding_do_not_mutate(self):
        headers = {'Authorization': 'Bearer ' + config()['api_token'], 'Content-Type': 'application/json'}
        with patch.object(self.gateway, 'mutate') as mutate:
            self.assertEqual(self.request(self.api, '/v1/phones/201/dial', 'POST', b'{', headers)[0], 400)
            headers['Transfer-Encoding'] = 'chunked'
            self.assertEqual(self.request(self.api, '/v1/phones/201/dial', 'POST', b'{}', headers)[0], 400)
            mutate.assert_not_called()

    def test_alert_stop_reaches_gateway_through_authenticated_http(self):
        headers = {'Authorization': 'Bearer ' + config()['api_token'], 'Content-Type': 'application/json'}
        FakeAMI.channels, FakeAMI.calls = [], []
        FakeAMI.endpoints = {'201': 'Not in use', '202': 'Not in use'}
        with patch.object(gateway, 'AMI', FakeAMI), patch.object(gateway, 'execute') as execute:
            status, raw, _ = self.request(self.api, '/v1/phones/201/alert', 'POST',
                json.dumps({'kind': 'timer', 'idempotency_key': 'http-alert-create'}), headers)
            self.assertEqual(status, 200)
            alert_id = json.loads(raw)['alert_id']
            root = 'cisco-alert-' + alert_id
            FakeAMI.channels = [
                {'Channel': 'Local/201@cisco-alert-a1;1', 'Uniqueid': root + '-a',
                 'Linkedid': root + '-a', 'ChannelStateDesc': 'Up'},
                {'Channel': 'PJSIP/201-a1', 'Uniqueid': '123.45',
                 'Linkedid': root + '-a', 'ChannelStateDesc': 'Ringing'}]
            unrelated = {'Channel': 'PJSIP/203-a2', 'Uniqueid': '999.1',
                         'Linkedid': '999.1', 'ChannelStateDesc': 'Up'}
            FakeAMI.channels.append(unrelated)

            def hangup(_ami, name, **values):
                self.assertEqual((name, values['Channel']), ('Hangup', 'Local/201@cisco-alert-a1;1'))
                FakeAMI.calls.append((name, values))
                FakeAMI.channels = [unrelated]
                return [{'Response': 'Success'}]

            body = json.dumps({'alert_id': alert_id, 'idempotency_key': 'http-alert-stop'})
            with patch.object(FakeAMI, 'action', hangup):
                status, raw, _ = self.request(self.api, '/v1/phones/201/alert_stop', 'POST', body, headers)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw)['state'], 'ended')
                self.assertEqual(json.loads(raw)['alert_id'], alert_id)
                self.assertEqual(self.request(self.api, '/v1/phones/201/alert_stop', 'POST', body, headers)[0], 200)
            self.assertEqual(FakeAMI.channels, [unrelated])
            self.assertEqual(sum(name == 'Hangup' for name, _ in FakeAMI.calls), 1)
            execute.assert_not_called()

    def test_underscore_routes_keep_authentication_and_action_allowlist(self):
        body = json.dumps({'alert_id': 'a' * 32, 'idempotency_key': 'http-rejected-stop'})
        headers = {'Authorization': 'Bearer ' + config()['api_token'], 'Content-Type': 'application/json'}
        with patch.object(self.gateway, 'mutate') as mutate:
            self.assertEqual(self.request(self.api, '/v1/phones/201/alert_stop', 'POST', body)[0], 403)
            for action in ('alert__stop', '_alert_stop', 'alert_stop_', 'alert_stop/extra'):
                self.assertEqual(self.request(self.api, '/v1/phones/201/' + action, 'POST', body, headers)[0], 404)
            mutate.assert_not_called()
        status, raw, _ = self.request(self.api, '/v1/phones/201/not_an_action', 'POST', body, headers)
        self.assertEqual((status, json.loads(raw)['reason']), (404, 'unknown_action'))


if __name__ == '__main__': unittest.main()
