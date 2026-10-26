"""HA media resolution is bounded and never starts phone or HA playback."""
import asyncio
import importlib.util
import io
import json
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location(
    'cisco_media_source', Path(__file__).resolve().parents[1] / 'scripts/cisco_media_source.py')
source = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(source)
BASE = 'http://192.168.50.10:8123'
TOKEN = 'synthetic-token-' + 'x' * 40
MEDIA_ID = 'media-source://media_source/music/Example/track.mp3'


def payload(**changes):
    return {'url': BASE, 'token': TOKEN, 'timeout': 5, 'media_ids': [MEDIA_ID], **changes}


def result(number=1, url='/media/track.mp3?authSig=synthetic', mime='audio/mpeg', **changes):
    return {'id': number, 'type': 'result', 'success': True,
            'result': {'url': url, 'mime_type': mime}, **changes}


class WebSocket:
    def __init__(self, responses, delay=0):
        self.responses = iter(responses)
        self.delay = delay
        self.sent = []

    async def receive_str(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        value = next(self.responses)
        return value if isinstance(value, str) else json.dumps(value)

    async def send_json(self, value):
        self.sent.append(value)


def websocket(*responses, **kwargs):
    return WebSocket([{'type': 'auth_required'}, {'type': 'auth_ok'}, *responses], **kwargs)


class ExchangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_authentication_precedes_only_sequential_resolution_commands(self):
        ws = websocket(result(), result(2, url='https://media.example.test/second.flac', mime='audio/flac'))
        identifiers = [MEDIA_ID, MEDIA_ID.replace('track.mp3', 'second.flac')]
        urls = await source.exchange(ws, payload(media_ids=identifiers), BASE)
        self.assertEqual(urls, [BASE + '/media/track.mp3?authSig=synthetic',
                                'https://media.example.test/second.flac'])
        self.assertEqual(ws.sent, [
            {'type': 'auth', 'access_token': TOKEN},
            {'id': 1, 'type': 'media_source/resolve_media', 'media_content_id': identifiers[0]},
            {'id': 2, 'type': 'media_source/resolve_media', 'media_content_id': identifiers[1]}])

    async def test_invalid_greeting_never_sends_token(self):
        ws = WebSocket([{'type': 'result'}])
        with self.assertRaises(source.MediaSourceError):
            await source.exchange(ws, payload(), BASE)
        self.assertEqual(ws.sent, [])

    async def test_authentication_failure_never_sends_resolution(self):
        ws = WebSocket([{'type': 'auth_required'}, {'type': 'auth_invalid', 'message': 'private'}])
        with self.assertRaises(source.MediaSourceError) as caught:
            await source.exchange(ws, payload(), BASE)
        self.assertEqual(str(caught.exception), 'media_source_authentication_failed')
        self.assertEqual(len(ws.sent), 1)

    async def test_result_identity_and_boolean_success_are_strict(self):
        invalid = [result(number=2), result(number=True), result(number='1'),
                   result(type='event'), result(success=1), result(success='true'),
                   {'type': 'result', 'success': True, 'result': {}}]
        for response in invalid:
            with self.subTest(response=response), self.assertRaises(source.MediaSourceError):
                await source.exchange(websocket(response), payload(), BASE)

    async def test_resolution_failure_has_no_private_details(self):
        with self.assertRaises(source.MediaSourceError) as caught:
            await source.exchange(websocket(result(success=False, error={'message': TOKEN})), payload(), BASE)
        self.assertEqual(str(caught.exception), 'media_source_resolution_failed')

    async def test_fifty_results_keep_exact_order_and_command_count(self):
        identifiers = [MEDIA_ID.replace('track', str(index)) for index in range(50)]
        ws = websocket(*(result(index + 1, url=f'/media/{index}.mp3') for index in range(50)))
        urls = await source.exchange(ws, payload(media_ids=identifiers), BASE)
        self.assertEqual(urls, [f'{BASE}/media/{index}.mp3' for index in range(50)])
        self.assertEqual(len(ws.sent), 51)

    async def test_duplicate_json_fields_and_oversized_frames_are_rejected(self):
        for raw in ('{"type":"auth_required","type":"auth_ok"}',
                    json.dumps({'type': 'auth_required', 'padding': 'x' * source.MAX_RESPONSE})):
            with self.subTest(length=len(raw)), self.assertRaises(source.MediaSourceError):
                await source.receive(WebSocket([raw]))

    @unittest.skipUnless(importlib.util.find_spec('aiohttp') is not None,
                         'requires optional aiohttp for the loopback redirect integration test')
    async def test_redirect_is_not_followed_and_never_receives_ha_authentication(self):
        received = []
        class Sink(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(('sink', dict(self.headers)))
                self.send_response(400)
                self.end_headers()
            def log_message(self, *_): pass
        sink = ThreadingHTTPServer(('127.0.0.1', 0), Sink)
        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(('source', dict(self.headers)))
                self.send_response(302)
                self.send_header('Location', f'http://127.0.0.1:{sink.server_port}/api/websocket')
                self.end_headers()
            def log_message(self, *_): pass
        redirect = ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
        threads = [threading.Thread(target=server.serve_forever, daemon=True)
                   for server in (sink, redirect)]
        for thread in threads:
            thread.start()
        try:
            # Direct connect permits this isolated loopback test; public payload
            # validation still requires a configured RFC1918 HA endpoint.
            with self.assertRaises(source.MediaSourceError) as caught:
                await source.connect(payload(), f'http://127.0.0.1:{redirect.server_port}')
            self.assertEqual(str(caught.exception), 'media_source_response_invalid')
            self.assertEqual([site for site, _ in received], ['source'])
            self.assertNotIn(TOKEN, json.dumps(received))
            self.assertNotIn('Authorization', received[0][1])
        finally:
            for server in (redirect, sink):
                await asyncio.to_thread(server.shutdown)
                server.server_close()
            for thread in threads:
                thread.join(2)


class WorkerTests(unittest.TestCase):
    def test_invalid_timeouts_counts_and_ids_never_connect(self):
        invalid = [payload(timeout=value) for value in
                   (True, False, 0, -1, 61, float('inf'), float('nan'), 10**10000)]
        invalid += [payload(media_ids=value) for value in
                    ([], [MEDIA_ID] * 51, [None], ['file:///tmp/private'], [MEDIA_ID + '\n'])]
        with patch.object(source, 'connect') as connect:
            for value in invalid:
                with self.subTest(field=type(value['timeout']).__name__):
                    self.assertEqual(source.perform(value),
                                     {'ok': False, 'reason': 'invalid_media_source_request'})
            connect.assert_not_called()

    def test_audio_urls_reject_credentials_fragments_and_non_audio(self):
        for url in ('//elsewhere.example/track.mp3', 'file:///tmp/track.mp3',
                    'http://user:secret@example.test/track.mp3', '/track.mp3#fragment',
                    '/raw space.mp3', '/raw\\path.mp3'):
            with self.subTest(url=url), self.assertRaises(source.MediaSourceError):
                source.audio_url(BASE, {'url': url, 'mime_type': 'audio/mpeg'})
        with self.assertRaises(source.MediaSourceError) as caught:
            source.audio_url(BASE, {'url': '/video.mp4', 'mime_type': 'video/mp4'})
        self.assertEqual(str(caught.exception), 'media_source_audio_required')
        self.assertEqual(source.audio_url(BASE, {'url': '/track.ogg', 'mime_type': 'application/ogg'}),
                         BASE + '/track.ogg')

    def test_final_absolute_audio_url_obeys_gateway_length_limit(self):
        prefix = 'https://media.example.test/'
        exact = prefix + 'a' * (4096 - len(prefix))
        self.assertEqual(source.audio_url(BASE, {'url': exact, 'mime_type': 'audio/mpeg'}), exact)
        relative = '/' + 'a' * (4096 - len(BASE) - 1)
        self.assertEqual(source.audio_url(BASE, {'url': relative, 'mime_type': 'audio/mpeg'}), BASE + relative)
        for oversized in (exact + 'a', relative + 'a'):
            with self.subTest(relative=oversized.startswith('/')):
                with self.assertRaises(source.MediaSourceError) as caught:
                    source.audio_url(BASE, {'url': oversized, 'mime_type': 'audio/mpeg'})
                self.assertEqual(str(caught.exception), 'media_source_response_invalid')

    def test_one_deadline_covers_the_whole_fifty_item_exchange(self):
        ws = websocket(*(result(index + 1) for index in range(50)), delay=.03)
        async def delayed(value, base):
            return await source.exchange(ws, value, base)
        with patch.object(source, 'connect', new=delayed):
            response = source.perform(payload(timeout=1, media_ids=[MEDIA_ID] * 50))
        self.assertEqual(response, {'ok': False, 'reason': 'media_source_timeout'})
        self.assertGreater(len(ws.sent), 2)
        self.assertLess(len(ws.sent), 51)

    def test_missing_direct_dependency_has_fixed_actionable_error(self):
        with patch.dict(sys.modules, aiohttp=None):
            self.assertEqual(source.perform(payload()),
                             {'ok': False, 'reason': 'media_source_dependency_required'})

    def test_unexpected_transport_exception_is_sanitized(self):
        async def failed(*_):
            raise OSError(TOKEN)
        with patch.object(source, 'connect', new=failed):
            self.assertEqual(source.perform(payload()),
                             {'ok': False, 'reason': 'media_source_connection_failed'})

    def test_oversized_or_duplicate_worker_input_is_rejected_before_perform(self):
        for raw in (b'x' * (source.MAX_INPUT + 1), b'{"url":"first","url":"second"}'):
            with patch.object(source.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw))), \
                 patch.object(source.sys, 'stdout', new_callable=io.StringIO) as output, \
                 patch.object(source, 'perform') as perform:
                source.main()
                self.assertEqual(json.loads(output.getvalue()),
                                 {'ok': False, 'reason': 'invalid_media_source_request'})
                perform.assert_not_called()


class ClientTests(unittest.TestCase):
    def client(self, transport='kubectl'):
        return SimpleNamespace(
            config={'gateway_url': BASE, 'token': TOKEN, 'transport': transport,
                    'kubectl': {'container': 'ha'}}, timeout=5, pod=None,
            resolve_pod=Mock(return_value='synthetic-pod'), kube_args=Mock(return_value=['kubectl']),
            run=Mock(return_value={'ok': True, 'urls': [BASE + '/media/track.mp3']}))

    def test_kubernetes_worker_uses_stdin_and_reuses_selected_pod(self):
        client = self.client()
        for _ in range(2):
            self.assertEqual(source.resolve_media(client, [MEDIA_ID]), [BASE + '/media/track.mp3'])
        client.resolve_pod.assert_called_once_with()
        command = client.run.call_args.args[0]
        self.assertEqual(command[:8], ['kubectl', 'exec', '-i', 'synthetic-pod', '-c', 'ha', '--', 'python3'])
        self.assertNotIn(TOKEN, ' '.join(command))
        self.assertNotIn(MEDIA_ID, ' '.join(command))
        self.assertEqual(json.loads(client.run.call_args.kwargs['input_data']), payload())

    def test_remote_response_count_shape_and_error_reason_are_strict(self):
        invalid = [None, {'ok': 1, 'urls': []}, {'ok': True, 'urls': []},
                   {'ok': True, 'urls': [BASE + '/one', BASE + '/two']},
                   {'ok': True, 'urls': [None]}, {'ok': False, 'reason': TOKEN}]
        client = self.client()
        for response in invalid:
            client.run.return_value = response
            with self.subTest(response=response), self.assertRaises(source.MediaSourceError) as caught:
                source.resolve_media(client, [MEDIA_ID])
            self.assertEqual(str(caught.exception), 'media_source_response_invalid')

    def test_direct_result_and_known_worker_error(self):
        client = self.client('direct')
        with patch.object(source, 'perform', return_value={'ok': True, 'urls': ['/media/track.mp3']}):
            self.assertEqual(source.resolve_media(client, [MEDIA_ID]), [BASE + '/media/track.mp3'])
        client.run.assert_not_called()
        with patch.object(source, 'perform', return_value={
                'ok': False, 'reason': 'media_source_authentication_failed'}):
            with self.assertRaises(source.MediaSourceError) as caught:
                source.resolve_media(client, [MEDIA_ID])
            self.assertEqual(str(caught.exception), 'media_source_authentication_failed')


if __name__ == '__main__':
    unittest.main()
