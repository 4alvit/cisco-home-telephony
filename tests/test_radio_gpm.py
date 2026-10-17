"""Official child-radio resolver bounds, token isolation and redirect policy."""
import importlib.util
import io
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('radio_gpm', ROOT/'containers/asterisk/radio.py')
radio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(radio)


class Response(io.BytesIO):
    def __init__(self, body=b'', status=200, **headers):
        super().__init__(body)
        self.status, self.headers = status, headers

    def getheader(self, name, default=None):
        return self.headers.get(name, default)


class ChildRadioTests(unittest.TestCase):
    def setUp(self):
        self.station = radio.catalog()['deti-toddlers']
        self.url = 'https://srv01.gpmradio.ru:443/stream/pro/aac/64/327?token=' + 'fixture_token_1234567890'

    def document(self, url=None):
        return {'status':1, 'result':[{'format':'AAC', 'urlStream':url or self.url}]}

    def resolve(self, document=None, raw=None):
        connection = Mock()
        response = Response(raw if raw is not None else json.dumps(document).encode())
        with patch.object(radio, 'open_stream', return_value=(connection,response)) as opener:
            try:
                value = radio.gpm_stream_url(self.station)
                opener.assert_called_once_with(self.station, self.station['url'], {'application/json'}, purpose='api')
                return value
            finally:
                connection.close.assert_called_once()

    def test_fixed_channels_and_numbers_are_distinct_and_only_use_official_api(self):
        for name, extension, channel in [('deti-toddlers','708',327), ('deti-lullabies','709',95), ('deti-songs','710',93)]:
            station = radio.catalog()[name]
            self.assertEqual(station['extension'], extension)
            self.assertEqual(station['channel_id'], channel)
            self.assertEqual(station['source'], f'https://detifm.ru/channel/{channel}')
            radio.checked_url(station['url'], station, 'api')
            self.assertNotIn('token=', json.dumps(station))

    def test_api_requires_success_schema_and_bounded_json(self):
        for document in ({'status':True,'result':[]}, {'status':0,'result':[]},
                         {'status':1,'result':{}}, {'status':1,'result':[]},
                         {'status':1,'result':[{}]*17}, self.document('file:///etc/passwd'),
                         {'status':1,'result':self.document()['result']*2}):
            with self.subTest(document=document), self.assertRaises(ValueError):
                self.resolve(document)
        for raw in (b'x'*(radio.GPM_API_LIMIT+1), b'{bad json', b'['*2000):
            with self.subTest(length=len(raw)), self.assertRaises(ValueError):
                self.resolve(raw=raw)

    def test_approved_entry_selected_without_using_http_or_alternate_ports(self):
        document = self.document()
        document['result'][:0] = [
            {'format':'AAC','urlStream':self.url.replace('https:','http:').replace(':443',':80')},
            {'format':'AAC','urlStream':self.url.replace('srv01','srv11').replace(':443',':8443')}]
        self.assertEqual(self.resolve(document), self.url)

    def test_tokens_are_resolved_fresh_and_never_written_to_catalog(self):
        before = radio.CATALOG.read_bytes()
        self.assertEqual(self.resolve(self.document()), self.url)
        second = self.url.replace('fixture_token_1234567890', 'fixture_token_0987654321')
        self.assertEqual(self.resolve(self.document(second)), second)
        self.assertEqual(before, radio.CATALOG.read_bytes())

    def test_stream_policy_rejects_wrong_channel_path_host_port_and_query(self):
        invalid = [self.url.replace('/327?', '/95?'), self.url.replace('/aac/64/', '/mp3/128/'),
                   self.url.replace(':443', ':8443'), self.url.replace('https:', 'http:'),
                   self.url.replace('srv01.gpmradio.ru', 'srv01.gpmradio.ru.evil.example'),
                   self.url.replace('srv01.gpmradio.ru', '127.0.0.1'),
                   self.url.replace('/stream/pro/', '/stream/pro/../'),
                   self.url+'&other=1', self.url+'&token=duplicate_token_12345',
                   self.url.replace('token=', 'url='), self.url+'#fragment',
                   self.url.replace('https://', 'https://user:pass@')]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ValueError) as error:
                radio.checked_url(url, self.station)
            self.assertNotIn('fixture_token', str(error.exception))
        radio.checked_url(self.url, self.station)

    def test_api_and_audio_url_policies_are_not_interchangeable(self):
        for url,purpose in [(self.url,'api'), (self.station['url'],'stream'),
                            (self.station['url'].replace('/327/', '/95/'),'api'),
                            (self.station['url']+'&next=anything','api')]:
            with self.assertRaises(ValueError):
                radio.checked_url(url,self.station,purpose)
        bad = dict(self.station,channel_id=999)
        with self.assertRaises(ValueError): radio.checked_url(self.url,bad)

    def network(self, connections):
        stack = __import__('contextlib').ExitStack()
        stack.enter_context(patch.object(radio, 'public_addresses', return_value=[
            (socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))]))
        stack.enter_context(patch.object(radio.socket, 'socket', return_value=Mock()))
        stack.enter_context(patch.object(radio.ssl, 'create_default_context', return_value=Mock()))
        factory=stack.enter_context(patch.object(radio.http.client,'HTTPConnection',side_effect=connections))
        return stack, factory

    def test_api_and_audio_use_same_user_agent_and_pinned_connector(self):
        api, audio = Mock(), Mock()
        api.getresponse.return_value = Response(json.dumps(self.document()).encode(), **{'Content-Type':'application/json'})
        audio.getresponse.return_value = Response(b'', **{'Content-Type':'audio/aac'})
        stack, factory = self.network([api,audio])
        with stack:
            connection,_ = radio.open_stream(self.station)
        self.assertIs(connection,audio)
        self.assertEqual(factory.call_count,2)
        self.assertEqual(api.request.call_args.kwargs['headers']['User-Agent'], radio.USER_AGENT)
        self.assertEqual(audio.request.call_args.kwargs['headers']['User-Agent'], radio.USER_AGENT)
        self.assertEqual(api.request.call_args.args[1], '/api/channel/getListServersChannel/327/channel/?dataFormat=json')
        self.assertTrue(audio.request.call_args.args[1].startswith('/stream/pro/aac/64/327?token='))
        api.close.assert_called_once()

    def test_redirect_cannot_cross_api_audio_boundary_before_another_connection(self):
        for url,purpose,redirect in [(self.station['url'],'api',self.url),
                                     (self.url,'stream',self.station['url'])]:
            connection=Mock()
            connection.getresponse.return_value=Response(status=302,Location=redirect)
            stack,factory=self.network([connection])
            with stack, self.assertRaises(ValueError):
                radio.open_stream(self.station,url,purpose=purpose)
            self.assertEqual(factory.call_count,1)
            connection.close.assert_called()


if __name__ == '__main__':
    unittest.main()
