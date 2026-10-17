"""Strict, bounded Cisco callback and fixed-target controller behavior."""
import contextlib
import base64
import copy
import errno
import importlib.util
import http.client
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlencode
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / file)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


p = module('phone_control_test', 'phone_control.py')
provision = module('provision_control_test', 'provision.py')


class PhoneControlTests(unittest.TestCase):
    def setUp(self):
        self.config = p.create_config('192.168.50.21', '192.168.50.10', 18080,
                                      'SEP020000000001', 2700)
        self.values = {'UserID': self.config['username'], 'Password': self.config['password'],
                       'devicename': self.config['device']}

    def check(self, method='GET', values=None, path=None, source=None, headers=None, now=None):
        encoded = urlencode(self.values if values is None else values)
        path = self.config['auth_path'] if path is None else path
        body = b''
        if method == 'GET':
            target = path + '?' + encoded
            headers = {} if headers is None else headers
        else:
            target = path
            body = encoded.encode()
            headers = {'content-type': 'application/x-www-form-urlencoded'} if headers is None else headers
        return p.authorized(self.config, source or self.config['phone_ipv4'], method,
                            target, headers, body, now=now)

    def test_get_post_use_exact_three_fields_and_standard_decoding(self):
        self.assertTrue(self.check())
        self.assertTrue(self.check(method='POST'))
        encoded = urlencode(self.values).replace('UserID=', '%55serID=')
        self.assertTrue(p.authorized(self.config, self.config['phone_ipv4'], 'GET',
                                    self.config['auth_path'] + '?' + encoded, {}, b''))

    def test_generated_credentials_fit_phone_basic_auth_and_retain_entropy(self):
        # SIP45.9-4-2SR4-3S rejects a 65-byte decoded Basic pair before it
        # contacts the callback. Keep generated pairs comfortably below that.
        for field in ('username', 'password'):
            value = self.config[field]
            self.assertEqual(len(value), 24)
            self.assertRegex(value, r'^[A-Za-z0-9_-]{24}$')
            self.assertEqual(len(base64.urlsafe_b64decode(value)), 18)
        self.assertNotEqual(self.config['username'], self.config['password'])
        pair = self.config['username'] + ':' + self.config['password']
        self.assertEqual(len(pair.encode('ascii')), 49)

    def test_basic_credential_boundary_includes_separator_even_when_expired(self):
        for password_length in (30, 31):
            config = dict(self.config, username='u' * 32, password='p' * password_length)
            self.assertIs(p.validate_config(config), config)
        for password_length in (32, 48):
            config = dict(self.config, username='u' * 32, password='p' * password_length)
            for allow_expired in (False, True):
                with self.subTest(length=password_length, allow_expired=allow_expired), \
                     self.assertRaises(ValueError):
                    p.validate_config(config, allow_expired=allow_expired)

    def test_wrong_source_path_each_credential_and_expiry_are_denied(self):
        self.assertFalse(self.check(source='192.168.50.22'))
        self.assertFalse(self.check(path='/auth/' + '0' * 64))
        self.assertFalse(self.check(now=self.config['expires_at']))
        for field in self.values:
            values = dict(self.values); values[field] += 'x'
            self.assertFalse(self.check(values=values), field)
        values = dict(self.values); values['devicename'] = values['devicename'].lower()
        self.assertFalse(self.check(values=values))

    def test_missing_duplicate_extra_case_changed_or_invalid_form_denied(self):
        variants = [list(self.values.items())[:-1], list(self.values.items()) + [('Password', 'x')],
                    list(self.values.items()) + [('extra', 'x')],
                    [('userid' if k == 'UserID' else k, v) for k, v in self.values.items()]]
        for values in variants:
            self.assertFalse(self.check(values=values))
        for query in ('UserID=%GG', 'UserID=%ff', urlencode(self.values) + '&', 'x' * 4097):
            self.assertFalse(p.authorized(self.config, self.config['phone_ipv4'], 'GET',
                                         self.config['auth_path'] + '?' + query, {}, b''))
        self.assertFalse(self.check(method='POST', headers={'content-type': 'text/plain'}))
        self.assertFalse(self.check(method='POST', path=self.config['auth_path'] + '?x=y'))
        self.assertFalse(self.check(headers={'transfer-encoding': 'chunked'}))
        self.assertFalse(self.check(path='http://192.168.50.10' + self.config['auth_path']))

    def test_config_expiration_constraints_and_private_file_safety(self):
        for field, value in (('phone_ipv4', '8.8.8.8'), ('auth_bind_ipv4', '0.0.0.0'),
                             ('auth_port', 80), ('auth_path', '/always'), ('password', 'short'),
                             ('username', 'admin'), ('ttl_seconds', 3000), ('device', 'SEP010000000001')):
            config = dict(self.config); config[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): p.validate_config(config)
        with self.assertRaises(ValueError): p.validate_config(self.config, now=self.config['expires_at'])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            p.write_new(path, json.dumps(self.config).encode())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(p.load_config(path), self.config)
            with self.assertRaises(FileExistsError): p.write_new(path, b'overwrite')
            link = Path(tmp) / 'link'; link.symlink_to(path)
            with self.assertRaises(OSError): p.load_config(link)
            path.chmod(0o644)
            with self.assertRaises(ValueError): p.load_config(path)

    def test_explicit_renew_preserves_device_nonce_credentials_and_refuses_bad_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            old = dict(self.config, created_at=int(time.time()) - 200,
                       expires_at=int(time.time()) - 100, ttl_seconds=100)
            p.write_new(path, json.dumps(old).encode())
            with self.assertRaises(ValueError): p.load_config(path)
            p.renew_config(path, 120)
            renewed = p.load_config(path)
            self.assertEqual(renewed['expires_at'] - renewed['created_at'], 120)
            for field in ('phone_ipv4', 'auth_bind_ipv4', 'auth_port', 'device', 'auth_path', 'username', 'password'):
                self.assertEqual(renewed[field], old[field])
            original = path.read_bytes()
            for ttl in (0, 2701):
                with self.assertRaises(ValueError): p.renew_config(path, ttl)
                self.assertEqual(path.read_bytes(), original)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(tmp).glob('*.renew-*')), [])

    def exchange(self, wire, *, peer='127.0.0.1', expired=False):
        config = dict(self.config, phone_ipv4='127.0.0.1')
        if expired: config['expires_at'] = int(time.time()) - 1
        with p.AuthServer(('127.0.0.1', 0), p.AuthHandler) as server:
            server.config = config
            server.deadline = time.monotonic() + 5
            thread = threading.Thread(target=server.handle_request)
            thread.start()
            with socket.socket() as connection:
                connection.settimeout(3)
                connection.bind((peer, 0))
                connection.connect(server.server_address)
                connection.sendall(wire)
                chunks = []
                while True:
                    try:
                        block = connection.recv(8192)
                    except ConnectionResetError:
                        # Rejecting an oversized unread request may close with TCP RST.
                        break
                    if not block: break
                    chunks.append(block)
            thread.join(5)
            self.assertFalse(thread.is_alive())
            return b''.join(chunks)

    def test_real_http_callback_exact_body_no_logs_and_post(self):
        query = urlencode(self.values).encode()
        get = b'GET ' + self.config['auth_path'].encode() + b'?' + query + b' HTTP/1.1\r\nHost: test\r\n\r\n'
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            response = self.exchange(get)
        self.assertEqual(response.split(b'\r\n\r\n')[1], b'AUTHORIZED')
        self.assertIn(b'Content-Type: text/plain\r\n', response)
        self.assertEqual(out.getvalue() + err.getvalue(), '')
        post = (b'POST ' + self.config['auth_path'].encode() + b' HTTP/1.1\r\nHost: test\r\n'
                b'Content-Type: application/x-www-form-urlencoded\r\nContent-Length: '
                + str(len(query)).encode() + b'\r\n\r\n' + query)
        self.assertTrue(self.exchange(post).endswith(b'\r\n\r\nAUTHORIZED'))
        self.assertTrue(self.exchange(get, expired=True).endswith(b'\r\n\r\nUNAUTHORIZED'))

    def test_callback_restart_reuses_closed_connections_but_refuses_live_listener(self):
        with p.AuthServer(('127.0.0.1', 0), p.AuthHandler) as server:
            server.config = dict(self.config, phone_ipv4='127.0.0.1')
            server.deadline = time.monotonic() + 5
            address = server.server_address
            with self.assertRaises(OSError) as raised:
                p.AuthServer(address, p.AuthHandler)
            self.assertEqual(raised.exception.errno, errno.EADDRINUSE)
            thread = threading.Thread(target=server.handle_request)
            thread.start()
            client = http.client.HTTPConnection(*address, timeout=3)
            try:
                client.request('GET', self.config['auth_path'] + '?' + urlencode(self.values))
                response = client.getresponse()
                self.assertEqual(response.read(), b'AUTHORIZED')
            finally:
                client.close()
            thread.join(5)
            self.assertFalse(thread.is_alive())
        # The server actively closed its accepted connection. A new maintenance
        # process can bind immediately instead of waiting for TCP TIME_WAIT.
        with p.AuthServer(address, p.AuthHandler) as restarted:
            self.assertEqual(restarted.server_address, address)

    def test_real_http_rejects_oversize_duplicate_headers_and_chunking(self):
        for wire in (b'GET /' + b'x' * 4100,
                     b'GET / HTTP/1.1\r\nHost: a\r\nHost: b\r\n\r\n',
                     b'POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n',
                     b'POST / HTTP/1.1\r\nContent-Length: 9999\r\n\r\n'):
            response = self.exchange(wire)
            self.assertIn(response.split(b'\r\n\r\n')[-1], (b'', b'UNAUTHORIZED'))

    def test_absolute_read_deadline_and_expiry_stop(self):
        class SlowSocket:
            def settimeout(self, timeout): pass
            def recv(self, size):
                time.sleep(0.02)
                return b'x'
        start = time.monotonic()
        with self.assertRaises(ValueError): p.read_request(SlowSocket(), start + 0.04)
        self.assertLess(time.monotonic() - start, 0.15)
        # Use a loopback listener only in this isolated test; production validation
        # rejects loopback and privileged/ephemeral ports.
        config = dict(self.config, auth_bind_ipv4='127.0.0.1', auth_port=0,
                      expires_at=time.time() + 0.08)
        with mock.patch.object(p, 'validate_config', return_value=config), contextlib.redirect_stdout(io.StringIO()):
            start = time.monotonic()
            p.serve(config)
        self.assertLess(time.monotonic() - start, 1.0)

    def test_cli_error_never_echoes_exception_with_credentials(self):
        sensitive = self.config['password'] + '?UserID=' + self.config['username']
        with mock.patch('sys.argv', ['phone_control.py', '--config', 'private.json', 'serve']), \
             mock.patch.object(p, 'load_config', side_effect=ValueError(sensitive)), \
             self.assertRaises(SystemExit) as raised:
            p.main()
        self.assertNotIn(sensitive, str(raised.exception))
        self.assertEqual(str(raised.exception), 'Phone management failed; request/configuration details withheld')

    def test_cli_sanitizes_malformed_http_status_and_incomplete_response(self):
        reflected = self.config['password'] + '?UserID=' + self.config['username']
        failures = (http.client.BadStatusLine(reflected),
                    http.client.IncompleteRead(reflected.encode(), 100),
                    http.client.LineTooLong(reflected))
        for failure in failures:
            with self.subTest(error=type(failure).__name__), \
                 mock.patch('sys.argv', ['phone_control.py', '--config', 'private.json',
                                         'screenshot', '--output', 'unused']), \
                 mock.patch.object(p, 'load_config', return_value=self.config), \
                 mock.patch.object(p, 'phone_request', side_effect=failure), \
                 self.assertRaises(SystemExit) as raised:
                p.main()
            self.assertEqual(str(raised.exception), 'Phone management failed; request/configuration details withheld')

    def test_controller_fixed_target_single_key_no_proxy_and_redirect(self):
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = b'<CiscoIPPhoneResponse><ResponseItem Status="0" URL="Key:Settings"/></CiscoIPPhoneResponse>'
        response.__enter__.return_value = response
        opener = mock.MagicMock(); opener.open.return_value = response
        with mock.patch.object(p.request, 'build_opener', return_value=opener) as builder:
            p.check_key_response(p.phone_request(self.config, key='Settings'), 'Settings')
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, 'http://192.168.50.21/CGI/Execute')
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 20)
        xml = ET.fromstring(parse_qs(req.data.decode())['XML'][0])
        self.assertEqual(len(xml), 1)
        self.assertEqual(xml[0].attrib, {'Priority': '2', 'URL': 'Key:Settings'})
        self.assertEqual(builder.call_args.args[0].proxies, {})
        self.assertIsInstance(builder.call_args.args[1], p.NoRedirect)
        with self.assertRaises(ValueError):
            builder.call_args.args[1].redirect_request(None, None, 302, '', {}, 'http://elsewhere/')
        for key in ('Key:Settings', 'SoftKey1', 'Init:Services', 'Dial:123', 'Settings&Key:Soft1'):
            with self.assertRaises(ValueError): p.phone_request(self.config, key=key)

    def test_screenshot_and_key_timeouts_are_bounded_and_never_retried(self):
        for key, timeout in ((None, 5), ('Settings', 20)):
            opener = mock.MagicMock()
            opener.open.side_effect = TimeoutError('response timed out')
            with self.subTest(key=key), \
                 mock.patch.object(p.request, 'build_opener', return_value=opener), \
                 self.assertRaises(TimeoutError):
                p.phone_request(self.config, key=key)
            self.assertEqual(opener.open.call_count, 1)
            self.assertEqual(opener.open.call_args.kwargs['timeout'], timeout)

    def test_documented_physical_line_and_speaker_keys_are_single_idle_requests(self):
        response = mock.MagicMock(); response.status = 200
        response.read.return_value = b'not inspected by this transport-only test'
        response.__enter__.return_value = response
        opener = mock.MagicMock(); opener.open.return_value = response
        for key in ('Line1', 'Line2', 'Speaker', 'Release'):
            with self.subTest(key=key), mock.patch.object(p.request, 'build_opener', return_value=opener):
                p.phone_request(self.config, key=key)
                req = opener.open.call_args.args[0]
                xml = ET.fromstring(parse_qs(req.data.decode())['XML'][0])
                self.assertEqual(len(xml), 1)
                self.assertEqual(xml[0].attrib, {'Priority': '2', 'URL': 'Key:' + key})
        for key in ('Line:2', 'Line0', 'Line3', 'EndCall', 'Offhook', 'Onhook'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                p.phone_request(self.config, key=key)

    def test_active_call_control_requires_explicit_boolean_and_one_allowed_key(self):
        response = mock.MagicMock(); response.status = 200
        response.read.return_value = b'not inspected by this transport-only test'
        response.__enter__.return_value = response
        opener = mock.MagicMock(); opener.open.return_value = response
        for key in ('KeyPad6', 'KeyPad0', 'Speaker', 'Release', 'Soft3'):
            with self.subTest(key=key), mock.patch.object(p.request, 'build_opener', return_value=opener):
                p.phone_request(self.config, key=key, allow_active=True)
                req = opener.open.call_args.args[0]
                xml = ET.fromstring(parse_qs(req.data.decode())['XML'][0])
                self.assertEqual(len(xml), 1)
                self.assertEqual(xml[0].attrib, {'Priority': '0', 'URL': 'Key:' + key})
        for kwargs in ({'allow_active': True}, {'key': 'Settings', 'allow_active': 1},
                       {'key': 'Settings', 'allow_active': 'true'},
                       {'key': 'Dial:600', 'allow_active': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                p.phone_request(self.config, **kwargs)

    def test_image_detection_response_bounds_and_non_success_key_results(self):
        self.assertEqual(p.screenshot_type(b'BM' + b'\x00' * 20), '.bmp')
        self.assertEqual(p.screenshot_type(b'\x89PNG\r\n\x1a\n'), '.png')
        self.assertEqual(p.screenshot_type(b'<CiscoIPPhoneImage/>'), '.xml')
        for body in (b'<html>denied</html>', b'<CiscoIPPhoneError Number="4"/>',
                     b'<!DOCTYPE x><CiscoIPPhoneImage/>'):
            with self.assertRaises(ValueError): p.screenshot_type(body)
        for body in (b'<CiscoIPPhoneError Number="4"/>', b'<CiscoIPPhoneResponse/>',
                     b'<CiscoIPPhoneResponse><ResponseItem Status="1"/></CiscoIPPhoneResponse>',
                     b'<CiscoIPPhoneResponse><ResponseItem Status="0" URL="Key:Soft1"/></CiscoIPPhoneResponse>'):
            with self.assertRaises(ValueError): p.check_key_response(body, 'Settings')
        response = mock.MagicMock(); response.status = 200
        response.__enter__.return_value = response
        response.read.return_value = b'x' * (p.MAX_RESPONSE + 1)
        opener = mock.MagicMock(); opener.open.return_value = response
        with mock.patch.object(p.request, 'build_opener', return_value=opener), self.assertRaises(ValueError):
            p.phone_request(self.config)


class AuthenticationURLTests(unittest.TestCase):
    def test_url_validation_and_opt_in_xml(self):
        valid = 'http://192.168.50.10:18080/auth/' + 'a' * 64
        self.assertEqual(provision.authentication_url(valid), valid)
        self.assertEqual(provision.authentication_url('https://10.0.0.1/auth/check'), 'https://10.0.0.1/auth/check')
        for bad in (None, '', 'http://example.test/auth', 'http://8.8.8.8/auth',
                    'http://127.0.0.1/auth', 'http://192.168.50.10:0/auth',
                    'http://u:p@192.168.50.10/auth', valid + '?x=y', valid + '?',
                    valid + '#fragment', valid + '\n', 'file:///auth',
                    'http://192.168.50.10/%2f../auth', 'http://192.168.50.10//auth'):
            with self.subTest(url=bad), self.assertRaises(ValueError): provision.authentication_url(bad)
        phone = {'mac': '020000000001', 'extension': '201', 'label': 'Office',
                 'firmware_load': 'SIP45.9-4-2SR4-3S'}
        xml = provision.phone_xml(phone, '192.168.50.10', '192.168.50.1', {'201': 'a' * 30})
        self.assertIsNone(ET.fromstring(xml).find('authenticationURL'))
        phone['authentication_url'] = valid
        xml = provision.phone_xml(phone, '192.168.50.10', '192.168.50.1', {'201': 'a' * 30})
        self.assertEqual(ET.fromstring(xml).findtext('authenticationURL'), valid)

    def test_ssh_opt_in_preserves_distinct_credentials_and_personalization_requires_auth(self):
        site = {'node_ipv4': '192.168.50.10', 'lan_cidr': '192.168.50.0/24', 'ntp_ipv4': '192.168.50.1'}
        phone = {'model': 'CP-7945G', 'verified': True, 'mac': '020000000001',
                 'extension': '201', 'label': 'Office', 'firmware_load': 'SIP45.9-4-2SR4-3S'}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            provision.generate(site, {'phones': [phone]}, out)
            secrets_file = out / 'phone-management-secrets.json'
            xml_file = out / 'tftp/SEP020000000001.cnf.xml'
            original = xml_file.read_bytes()
            self.assertFalse(secrets_file.exists())
            self.assertEqual(ET.fromstring(original).findtext('vendorConfig/sshAccess'), '1')
            enabled = dict(phone, ssh_enabled=True, web_access=True, phone_personalization=True,
                           authentication_url='http://192.168.50.10:18080/auth/' + 'b' * 64)
            provision.generate(site, {'phones': [enabled]}, out)
            saved = secrets_file.read_bytes()
            credentials = json.loads(saved)['SEP020000000001']
            self.assertEqual(secrets_file.stat().st_mode & 0o777, 0o600)
            xml = ET.parse(xml_file)
            self.assertEqual(xml.findtext('vendorConfig/sshAccess'), '0')
            self.assertEqual(xml.findtext('sshUserId'), credentials['ssh_username'])
            self.assertEqual(xml.findtext('sshPassword'), credentials['ssh_password'])
            self.assertEqual(xml.findtext('phonePersonalization'), '1')
            self.assertNotEqual(credentials['ssh_password'], json.loads((out / 'secrets.json').read_bytes())['201'])
            provision.generate(site, {'phones': [enabled]}, out)
            self.assertEqual(secrets_file.read_bytes(), saved)
            provision.generate(site, {'phones': [phone]}, out)
            self.assertEqual(secrets_file.read_bytes(), saved)
            self.assertEqual(xml_file.read_bytes(), original)
            for setting in ('ssh_enabled', 'phone_personalization'):
                for value in (None, 1, 'true'):
                    with self.assertRaises(ValueError):
                        provision.validate(site, {'phones': [dict(phone, **{setting: value})]})
            with self.assertRaises(ValueError):
                provision.validate(site, {'phones': [dict(phone, phone_personalization=True)]})
            secrets_file.unlink()
            secrets_file.symlink_to(out / 'secrets.json')
            with self.assertRaisesRegex(ValueError, 'symlink'):
                provision.generate(site, {'phones': [enabled]}, out)


if __name__ == '__main__':
    unittest.main()
