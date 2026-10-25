"""Offline HTTP-error boundary regressions; all requests use synthetic fixtures."""
import contextlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError


SOURCE = Path(os.environ.get(
    "HTTP_CANDIDATE", str(Path(__file__).resolve().parents[1] / "scripts" / "cisco_http.py")
))
SPEC = importlib.util.spec_from_file_location("cisco_http_under_test", SOURCE)
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)

TOKEN = "SYNTHETIC_TOKEN_" + "x" * 40
REQUEST_MARKER = "SYNTHETIC_REQUEST_PRIVATE_TEXT"
EXCEPTION_MARKER = "SYNTHETIC_EXCEPTION_PRIVATE_TEXT"
MARKERS = (TOKEN, REQUEST_MARKER, EXCEPTION_MARKER)


class TrackedBody(io.BytesIO):
    def __init__(self, value):
        super().__init__(value)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class MemorySocket:
    """Only the in-memory makefile interface required by HTTPResponse."""
    def __init__(self, wire):
        self.stream = io.BytesIO(wire)

    def makefile(self, mode):
        if mode != "rb":
            raise AssertionError("unexpected response stream mode")
        return self.stream


class TrackedHTTPResponse(http.client.HTTPResponse):
    def __init__(self, status):
        # Five bytes are declared, but only three arrive, with no terminator.
        wire = ("HTTP/1.1 " + str(status) + " Synthetic error\r\n"
                "Transfer-Encoding: chunked\r\n\r\n5\r\nabc").encode("ascii")
        memory = MemorySocket(wire)
        self.source_stream = memory.stream
        self.read_sizes = []
        super().__init__(memory)
        self.begin()

    def read(self, amt=None):
        self.read_sizes.append(amt)
        return super().read(amt)


class HTTPExceptionBody(TrackedBody):
    def read(self, size=-1):
        self.read_sizes.append(size)
        raise http.client.HTTPException(" ".join(MARKERS))


class HTTPErrorBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.external_calls = []
        for name in ("socket.socket", "socket.create_connection", "subprocess.Popen"):
            guard = patch(name, side_effect=AssertionError("external operation forbidden"))
            mocked = guard.start()
            self.addCleanup(guard.stop)
            self.external_calls.append(mocked)

    def assert_no_external_calls(self):
        for mocked in self.external_calls:
            self.assertEqual(mocked.call_count, 0, "unexpected external operation")

    def tearDown(self):
        self.assert_no_external_calls()

    def perform_error(self, api, body, status, *, decoder_error=False, stream=None):
        payload = {
            "url": "http://192.168.50.10:8080", "token": TOKEN,
            "api": api, "timeout": 5,
            "path": worker.SCHEDULE_PATH if api == "home_assistant" else "/v1/phones",
            "body": {"synthetic_request": REQUEST_MARKER},
        }
        if stream is None:
            stream = TrackedBody(body)
        failure = HTTPError(payload["url"], status, " ".join(MARKERS), {}, stream)
        stdout, stderr = io.StringIO(), io.StringIO()
        escaped = None
        result = None
        decoder = (patch.object(worker.json, "loads", side_effect=RecursionError(" ".join(MARKERS)))
                   if decoder_error else contextlib.nullcontext())
        with patch.object(worker.request, "build_opener") as factory:
            factory.return_value.open.side_effect = failure
            try:
                with decoder, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    try:
                        result = worker.perform(payload)
                    except Exception as exc:
                        # Never include untrusted exception text in test output.
                        escaped = type(exc).__name__
            finally:
                self.assertEqual(factory.call_count, 1, "unexpected opener count")
                self.assertEqual(factory.return_value.open.call_count, 1, "request retried")
                self.assertEqual(stream.read_sizes, [4097], "error-body read was not bounded")
                self.assertTrue(stream.closed, "HTTP error stream was not closed")
                if isinstance(stream, TrackedHTTPResponse):
                    self.assertTrue(stream.isclosed(), "HTTP response was not closed")
                    self.assertTrue(stream.source_stream.closed, "response body was not closed")
                self.assert_no_external_calls()
                self.assertFalse(bool(stdout.getvalue() or stderr.getvalue()),
                                 "worker wrote unexpected output")
        if escaped is not None:
            self.fail("perform() raised " + escaped + " instead of returning a fixed HTTP error")
        serialized = json.dumps(result, sort_keys=True)
        self.assertFalse(any(marker in serialized for marker in MARKERS),
                         "worker returned synthetic private text")
        return result

    def assert_error(self, api, body, status, reason, *, decoder_error=False, stream=None):
        result = self.perform_error(api, body, status, decoder_error=decoder_error, stream=stream)
        self.assertEqual(result, {"ok": False, "reason": reason, "http_status": status})

    def assert_body_read_errors(self, api, status, reason):
        response = TrackedHTTPResponse(status)
        self.assertTrue(response.chunked)
        self.assertEqual(response.status, status)
        self.assert_error(api, b"", status, reason, stream=response)
        # Also retain the generic HTTPException contract, not just its
        # IncompleteRead subclass raised by the real parser above.
        self.assert_error(api, b"", status, reason, stream=HTTPExceptionBody(b""))

    def test_gateway_body_read_errors_preserve_safe_http_error(self):
        self.assert_body_read_errors("gateway", 403, "gateway_http_error")

    def test_ha_body_read_errors_preserve_safe_http_error(self):
        self.assert_body_read_errors("home_assistant", 429, "ha_service_http_error")

    def test_decoder_recursion_gateway_preserves_safe_http_error(self):
        self.assert_error("gateway", b"{}", 503, "gateway_http_error", decoder_error=True)

    def test_decoder_recursion_ha_preserves_safe_http_error(self):
        self.assert_error("home_assistant", b"{}", 502, "ha_service_http_error", decoder_error=True)

    def test_known_gateway_reason_preserves_non500_status(self):
        self.assert_error("gateway", b'{"reason":"phone_busy"}', 409, "phone_busy")

    def test_known_voice_reason_preserves_non500_status(self):
        self.assert_error("gateway", b'{"reason":"voice_phone_busy"}', 423, "voice_phone_busy")

    def test_known_ha_message_preserves_non500_status(self):
        self.assert_error("home_assistant", b'{"message":"The alarm belongs to the other line"}',
                          422, "ha_alarm_owned_by_other_line")

    def test_ha_message_takes_precedence_over_generic_reason(self):
        self.assert_error("home_assistant",
                          b'{"reason":"phone_busy","message":"The alarm belongs to the other line"}',
                          409, "ha_alarm_owned_by_other_line")

    def test_ha_message_mapping_is_not_applied_to_gateway(self):
        self.assert_error("gateway",
                          b'{"reason":"phone_busy","message":"The alarm belongs to the other line"}',
                          409, "phone_busy")

    def test_unknown_private_error_text_uses_fixed_fallback(self):
        body = json.dumps({"reason": TOKEN, "message": REQUEST_MARKER,
                           "details": EXCEPTION_MARKER}).encode()
        for api, reason in (("gateway", "gateway_http_error"),
                            ("home_assistant", "ha_service_http_error")):
            with self.subTest(api=api):
                self.assert_error(api, body, 503, reason)

    def test_malformed_error_body_uses_fixed_fallback(self):
        self.assert_error("gateway", ("not-json " + EXCEPTION_MARKER).encode(),
                          502, "gateway_http_error")

    def test_unhashable_reason_uses_fixed_fallback(self):
        self.assert_error("home_assistant", b'{"reason":[]}', 400, "ha_service_http_error")

    def test_gateway_url_rejects_delimiters_and_invalid_origins(self):
        for suffix in ('?', '#', '/?', '/#', '?key=value', '#section'):
            with self.subTest(suffix=suffix), self.assertRaisesRegex(ValueError, '^invalid_gateway_url$'):
                worker.gateway_url('http://192.168.50.10' + suffix)
        invalid = ('http://example.invalid', 'http://8.8.8.8',
                   'http://user:pass@192.168.50.10', 'http://192.168.50.10/other',
                   'http://192.168.50.10:0', 'http://192.168.50.10:65536',
                   'http://192.168.50.10:not-a-port')
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ValueError):
                worker.gateway_url(url)

    def test_perform_rejects_empty_base_markers_before_request(self):
        for api in ('gateway', 'home_assistant'):
            for suffix in ('?', '#', '/?', '/#'):
                with self.subTest(api=api, suffix=suffix):
                    payload = {'url': 'http://192.168.50.10' + suffix, 'token': TOKEN,
                               'api': api, 'timeout': 5,
                               'path': worker.SCHEDULE_PATH if api == 'home_assistant' else '/v1/phones',
                               'body': {} if api == 'home_assistant' else None}
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with patch.object(worker.request, 'Request', wraps=worker.request.Request) as request, \
                            patch.object(worker.request, 'build_opener') as factory, \
                            contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        # A buggy baseline may proceed: keep that path entirely offline.
                        response = factory.return_value.open.return_value.__enter__.return_value
                        response.status = 200
                        response.read.return_value = b'{"service_response":{}}' if api == 'home_assistant' else b'{}'
                        try:
                            result = worker.perform(payload)
                        finally:
                            self.assert_no_external_calls()
                            self.assertFalse(bool(stdout.getvalue() or stderr.getvalue()), 'worker printed output')
                            self.assertEqual((request.call_count, factory.call_count,
                                              factory.return_value.open.call_count), (0, 0, 0))
                    self.assertEqual(result, {'ok': False, 'reason': 'invalid_gateway_response_or_request'})

    def test_valid_private_origins_preserve_request_routes(self):
        cases = (('http://10.23.45.67', 'gateway'), ('https://10.23.45.67:1', 'home_assistant'),
                 ('http://172.16.5.6:65535', 'gateway'), ('https://172.31.5.6', 'home_assistant'),
                 ('http://192.168.50.10:8080', 'home_assistant'), ('https://192.168.50.10:443', 'gateway'))
        for base, api in cases:
            for suffix in ('', '/'):
                with self.subTest(base=base, api=api, root_slash=bool(suffix)):
                    self.assertEqual(worker.gateway_url(base + suffix), base)
                    path = worker.SCHEDULE_PATH if api == 'home_assistant' else '/v1/phones'
                    payload = {'url': base + suffix, 'token': TOKEN, 'api': api,
                               'timeout': 5, 'path': path, 'body': {} if api == 'home_assistant' else None}
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with patch.object(worker.request, 'Request', wraps=worker.request.Request) as request, \
                            patch.object(worker.request, 'build_opener') as factory, \
                            contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        response = factory.return_value.open.return_value.__enter__.return_value
                        response.status = 200
                        body = {'synthetic': True}
                        response.read.return_value = json.dumps(
                            {'service_response': body} if api == 'home_assistant' else body).encode()
                        try:
                            result = worker.perform(payload)
                        finally:
                            self.assert_no_external_calls()
                            self.assertFalse(bool(stdout.getvalue() or stderr.getvalue()), 'worker printed output')
                        self.assertEqual(result, {'ok': True, 'data': body})
                        self.assertEqual((request.call_count, factory.call_count,
                                          factory.return_value.open.call_count), (1, 1, 1))
                        actual_request = factory.return_value.open.call_args.args[0]
                        self.assertEqual(actual_request.full_url, base + path)
                        self.assertEqual(actual_request.selector, path)

    def test_duplicate_success_fields_are_rejected_without_retry(self):
        cases = (
            ('gateway', b'{"acknowledged_steps":0,"acknowledged_steps":1}'),
            ('gateway', b'{"phones":[{"online":false,"online":true}]}'),
            ('home_assistant', b'{"service_response":{},"service_response":{"status":"accepted"}}'),
            ('home_assistant', b'{"service_response":{"status":"refused","status":"accepted"}}'),
        )
        for api, raw in cases:
            with self.subTest(api=api, raw=raw):
                payload = {'url': 'http://192.168.50.10', 'token': TOKEN, 'api': api,
                           'timeout': 5, 'body': {},
                           'path': worker.SCHEDULE_PATH if api == 'home_assistant' else '/v1/phones'}
                response = TrackedBody(raw)
                response.status = 200
                with patch.object(worker.request, 'build_opener') as factory:
                    factory.return_value.open.return_value = response
                    result = worker.perform(payload)
                    self.assertEqual(factory.return_value.open.call_count, 1)
                self.assertTrue(response.closed)
                self.assertEqual(response.read_sizes, [worker.MAX_RESPONSE + 1])
                self.assertEqual(result, {'ok': False, 'reason': 'invalid_gateway_response_or_request'})

    def test_unique_nested_success_fields_keep_existing_values(self):
        expected = {'phones': [{'id': '212', 'online': True}, {'id': '213', 'online': False}],
                    'metadata': {'id': None, 'steps': 1}}
        self.assertEqual(worker.json_loads_unique(json.dumps(expected)), expected)


if __name__ == "__main__":
    unittest.main()
