import copy
import importlib.util
import io
import math
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import uuid


spec = importlib.util.spec_from_file_location('media_runtime', Path(__file__).parents[1] / 'containers/asterisk/media_runtime.py')
media = importlib.util.module_from_spec(spec)
spec.loader.exec_module(media)

SOURCE = {'url': 'https://audio.example/song.mp3', 'title': 'Example song'}


class Response(io.BytesIO):
    status = 200

    def __init__(self, body=b'', headers=None):
        super().__init__(body)
        self.headers = headers or {}

    def getheader(self, name, default=None):
        return self.headers.get(name.lower(), default)


class Connection:
    sock = None

    def close(self):
        pass


class RuntimeValidation(unittest.TestCase):
    def test_validation_has_no_network_and_preserves_url_as_data(self):
        source = {'url': 'https://audio.example/a%20b.mp3?token=$(id)&x=`touch`',
                  'title': 'Name', 'artist': 'Artist', 'album': 'Album', 'duration': 90}
        with patch.object(media.socket, 'getaddrinfo') as dns:
            self.assertEqual(media.validate_source(source), source)
            dns.assert_not_called()
        for value in ('file:///etc/passwd', 'http://user:pass@audio.example/x',
                      'http://audio.example/x#frag', 'https://audio.example/\n',
                      'http://127.0.0.1/x', 'http://169.254.169.254/latest',
                      'http://[::1]/x', 'http://10.0.0.10:8097/song'):
            with self.subTest(url=value), self.assertRaises(media.MediaError):
                media.validate_source({'url': value})

    def test_explicit_lan_origin_is_exact_and_never_authorizes_link_local(self):
        source = {'url': 'http://10.0.0.10:8097/music/track.flac?token=x'}
        self.assertEqual(media.validate_source(source, ['http://10.0.0.10:8097']), source)
        for origins in (['http://10.0.0.10'], ['https://10.0.0.10:8097'], ['http://10.0.0.11:8097']):
            with self.assertRaises(media.MediaError):
                media.validate_source(source, origins)
        with self.assertRaises(media.MediaError):
            media.validate_source({'url': 'http://169.254.169.254/x'}, ['http://169.254.169.254'])

    def test_rebinding_and_mixed_dns_answers_are_rejected_before_connect(self):
        for addresses in (['10.0.0.20'], ['8.8.8.8', '10.0.0.20']):
            answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443)) for address in addresses]
            with patch.object(media.socket, 'getaddrinfo', return_value=answer), \
                    patch.object(media.socket, 'socket') as connect:
                with self.assertRaises(media.MediaError):
                    media.open_media(SOURCE['url'])
                connect.assert_not_called()

    def test_redirect_cannot_enter_unapproved_lan_origin(self):
        response = Response(headers={'location': 'http://10.0.0.30:8097/song'})
        response.status = 302
        connection = Mock()
        connection.getresponse.return_value = response
        socket_instance = Mock()
        addresses = [[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 80))],
                     [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.30', 8097))]]
        with patch.object(media.socket, 'getaddrinfo', side_effect=addresses), \
                patch.object(media.socket, 'socket', return_value=socket_instance), \
                patch.object(media.http.client, 'HTTPConnection', return_value=connection):
            with self.assertRaises(media.MediaError):
                media.open_media('http://audio.example/song')
        socket_instance.connect.assert_called_once_with(('8.8.8.8', 80))
        connection.close.assert_called()

    def test_invalid_metadata_and_unknown_source_fields_rejected(self):
        for values in ({'title': 'x' * 257}, {'artist': 'x\n'}, {'duration': float('nan')},
                       {'duration': True}, {'station': 'echo'}, {'headers': {'Authorization': 'secret'}}):
            with self.subTest(values=values), self.assertRaises(media.MediaError):
                media.validate_source({**SOURCE, **values})
        self.assertEqual(media.validate_source({'station': 'deti-toddlers'}), {'station': 'deti-toddlers'})

    def test_icy_metadata_is_stripped_and_title_is_not_split(self):
        text = b"StreamTitle='Artist - Track - Mix';StreamURL='https://ignored.example/';"
        block = text.ljust(math.ceil(len(text) / 16) * 16, b'\0')
        response = Response(b'abc' + bytes([len(block) // 16]) + block + b'def', {'icy-metaint': '3'})
        updates = []
        reader = media.IcyReader(response, updates.append)
        self.assertEqual(reader.read(8192), b'abc')
        self.assertEqual(reader.read(8192), b'def')
        self.assertEqual(updates, [{'title': 'Artist - Track - Mix', 'artist': '', 'album': ''}])
        for interval in ('-1', '0', '16777217', 'bad'):
            with self.assertRaises(media.MediaError):
                media.IcyReader(Response(headers={'icy-metaint': interval}), updates.append)

    def test_decoder_never_receives_a_url_or_shell_and_only_decodes_audio(self):
        args = media.decoder_args('mp3', 3.5)
        self.assertEqual(args[args.index('-i') + 1], 'pipe:0')
        self.assertEqual(args[args.index('-protocol_whitelist') + 1], 'pipe')
        self.assertIn('-codec_whitelist', args)
        self.assertEqual(args[args.index('-ar') + 1], '16000')
        for option in ('-vn', '-sn', '-dn'):
            self.assertIn(option, args)
        self.assertNotIn(SOURCE['url'], args)
        for fmt in ('hls', 'concat', 'mp3;id', 'image2'):
            with self.assertRaises(media.MediaError):
                media.decoder_args(fmt)

    def test_nonzero_probe_never_trusts_even_complete_metadata(self):
        process = Mock()
        process.stdout = io.BytesIO(b'{"format":{"duration":"120","tags":{"title":"Unverified"}}}')
        process.stdin = None
        process.wait.return_value = process.poll.return_value = 1
        with tempfile.TemporaryFile() as file, \
                patch.object(media.subprocess, 'Popen', return_value=process), \
                patch.object(media.select, 'select', return_value=([process.stdout], [], [])):
            with self.assertRaisesRegex(media.MediaError, 'unsupported_source'):
                media.probe_file(file, 'mp3')

    def test_software_gain_and_mute_preserve_signed_pcm(self):
        data = struct.pack('<5h', -32768, -1000, 0, 1000, 32767)
        self.assertEqual(struct.unpack('<5h', media.gain_pcm(data, .5)), (-16384, -500, 0, 500, 16384))
        self.assertEqual(media.gain_pcm(data, 1), data)
        self.assertEqual(media.gain_pcm(data, .7, True), bytes(10))
        for level in (-1, 1.1, True, float('inf')):
            with self.assertRaises(media.MediaError):
                media.gain_pcm(data, level)


class QueueControls(unittest.TestCase):
    def setUp(self):
        self.session = media.MediaSession(uuid.uuid4(), SOURCE, max_queue=4)
        self.addCleanup(self.session.close)

    def test_enqueue_modes_and_previous_next_do_not_create_calls(self):
        with patch.object(media, 'open_media') as fetch:
            self.session.control('enqueue', source={'station': 'echo'}, mode='add')
            self.session.control('enqueue', source={'station': 'bob-rock'}, mode='next')
            self.assertEqual(self.session.snapshot()['queue_length'], 3)
            self.session.control('next')
            self.assertEqual(self.session.snapshot()['source_id'], '703')
            self.session.control('previous')
            self.assertEqual(self.session.snapshot()['source_id'], SOURCE['url'])
            self.session.control('enqueue', source={'station': 'bob-metal'}, mode='play')
            self.assertEqual(self.session.snapshot()['source_id'], '704')
            with self.assertRaisesRegex(media.MediaError, 'queue_full'):
                self.session.control('enqueue', source=SOURCE, mode='add')
            self.session.control('clear_queue')
            self.assertEqual(self.session.snapshot()['queue_length'], 1)
            self.assertEqual(self.session.snapshot()['source_id'], '704')
            self.session.control('enqueue', source={'station': 'deti-toddlers'}, mode='replace')
            self.assertEqual(self.session.snapshot()['source_id'], '708')
            fetch.assert_not_called()

    def test_invalid_commands_leave_queue_and_audio_state_unchanged(self):
        before = self.session.snapshot()
        cases = [('seek', {'position': 3}), ('volume', {'level': True}), ('mute', {'muted': 1}),
                 ('enqueue', {'source': SOURCE, 'mode': 'invalid'}),
                 ('next', {}), ('previous', {}), ('pause', {'unknown': 1})]
        for action, payload in cases:
            with self.subTest(action=action), self.assertRaises(media.MediaError):
                self.session.control(action, **payload)
            self.assertEqual(self.session.snapshot(), before)

    def test_wrong_uuid_never_fetches_or_decodes(self):
        with patch.object(media, 'open_media') as fetch, patch.object(media.subprocess, 'Popen') as process:
            self.session.start()
            with socket.create_connection(('127.0.0.1', self.session.port), timeout=2) as peer:
                peer.sendall(b'\x01\x00\x10' + uuid.uuid4().bytes)
            self.session.thread.join(3)
            self.assertFalse(self.session.thread.is_alive())
            self.assertEqual(self.session.snapshot()['state'], 'error')
            fetch.assert_not_called()
            process.assert_not_called()

    def test_close_before_handshake_joins_listener_without_fetch(self):
        with patch.object(media, 'open_media') as fetch:
            self.session.start()
            self.session.close()
            self.assertFalse(self.session.thread.is_alive())
            self.assertEqual(self.session.snapshot()['state'], 'idle')
            fetch.assert_not_called()

    def test_connection_can_arrive_after_slow_phone_commands(self):
        # The controller may spend 10 seconds on each of two phone requests.
        # A late, valid call must still reach UUID admission without fetching
        # anything merely because the listener started before those requests.
        self.session.listener.close()
        listener, peer = Mock(), Mock()
        listener.accept.side_effect = [socket.timeout(), (peer, ('127.0.0.1', 12345))]
        self.session.listener = listener
        clock = iter((0, 44))
        with patch.object(media.time, 'monotonic', side_effect=lambda: next(clock, 44)), \
                patch.object(media.radio, 'receive_frame', return_value=(1, self.session.token.bytes)) as received, \
                patch.object(media.threading, 'Thread') as reader, \
                patch.object(media, 'open_media') as fetch:
            reader.return_value.start.side_effect = self.session.stop_event.set
            self.session._serve()
        self.assertEqual(listener.accept.call_count, 2)
        received.assert_called_once_with(peer)
        self.assertEqual(self.session.snapshot()['state'], 'idle')
        fetch.assert_not_called()

    def test_replaced_decoder_eof_or_error_cannot_end_new_track(self):
        for action in ('next', 'replace'):
            for outcome in ('eof', 'error'):
                with self.subTest(action=action, outcome=outcome):
                    session = media.MediaSession(uuid.uuid4(), SOURCE, max_seconds=3)
                    next_source = {**SOURCE, 'url': 'https://audio.example/next.mp3'}
                    session.control('enqueue', source=next_source, mode='add')
                    decoders = []

                    class RacingDecoder:
                        finite, duration = False, None

                        def __init__(self, *_args, **_kwargs):
                            self.closed = False
                            self.index = len(decoders)
                            decoders.append(self)

                        def read_frame(self):
                            if self.index == 0:
                                # Exactly between read_frame and its result,
                                # the user requests a different queue item.
                                if action == 'next':
                                    session.control('next')
                                else:
                                    session.control('enqueue', source=next_source, mode='replace')
                                if outcome == 'error':
                                    raise media.MediaError('decoder_failed')
                                return b''
                            return b'\x01\x00' * 320

                        def close(self, **_kwargs):
                            self.closed = True

                    with patch.object(media, 'Decoder', RacingDecoder):
                        try:
                            session.start()
                            with socket.create_connection(('127.0.0.1', session.port), timeout=2) as peer:
                                peer.sendall(b'\x01\x00\x10' + session.token.bytes)
                                kind, pcm = media.radio.receive_frame(peer)
                                self.assertEqual(kind, 0x12)
                                self.assertEqual(len(pcm), 640)
                                self.assertEqual(session.snapshot()['source_id'], next_source['url'])
                                self.assertEqual(session.snapshot()['state'], 'playing')
                                self.assertEqual(len(decoders), 2)
                                self.assertTrue(decoders[0].closed)
                        finally:
                            session.close()

    def test_controls_during_pacing_apply_before_next_wire_frame(self):
        for action in ('pause', 'mute', 'volume', 'replace', 'seek', 'stop'):
            with self.subTest(action=action):
                session = media.MediaSession(uuid.uuid4(), SOURCE, max_seconds=3)
                session.listener.close()
                session.listener = Mock()
                session.listener.accept.return_value = (Mock(), ('127.0.0.1', 12345))
                sent, decoders, waits = [], [], []

                class Decoder:
                    finite, duration = True, 10

                    def __init__(self, *_args, **_kwargs):
                        self.index, self.reads = len(decoders), 0
                        decoders.append(self)

                    def read_frame(self):
                        self.reads += 1
                        return struct.pack('<320h', *([1000 + self.index * 1000] * 320))

                    def close(self, **_kwargs):
                        return None

                def pacing(_seconds):
                    waits.append(True)
                    if len(waits) == 1:
                        if action == 'replace':
                            session.control('enqueue', source={'station': 'echo'}, mode='replace')
                        elif action == 'seek':
                            session.control('seek', position=4)
                        elif action == 'volume':
                            session.control('volume', level=.2)
                        elif action == 'mute':
                            session.control('mute', muted=True)
                        else:
                            session.control(action)
                    elif action == 'pause':
                        session.control('resume')
                    return session.stop_event.is_set()

                def send(_peer, frame):
                    sent.append(frame)
                    if action != 'pause' or len(sent) == 2:
                        session.stop_event.set()

                try:
                    with patch.object(media, 'Decoder', Decoder), \
                            patch.object(media.threading, 'Thread'), \
                            patch.object(media.radio, 'receive_frame', return_value=(1, session.token.bytes)), \
                            patch.object(media.radio, 'send_audio', side_effect=send), \
                            patch.object(session.stop_event, 'wait', side_effect=pacing):
                        session._serve()
                    samples = [struct.unpack('<320h', frame)[0] for frame in sent]
                    expected = {'pause': [0, 500], 'mute': [0], 'volume': [200],
                                'replace': [1000], 'seek': [1000], 'stop': []}
                    self.assertEqual(samples, expected[action])
                    if action == 'pause':
                        self.assertEqual(decoders[0].reads, 1, 'Resume must retain the paused frame')
                    if action == 'seek':
                        self.assertAlmostEqual(session.position, 4 + media.FRAME_SECONDS)
                finally:
                    session.close()


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg and ffprobe required')
class AudioRuntime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mp3 = subprocess.check_output(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            'aevalsrc=0.2*sin(2*PI*1000*t)+0.2*sin(2*PI*5000*t):s=48000:d=10',
            '-metadata', 'title=Embedded title', '-metadata', 'artist=Embedded artist',
            '-f', 'mp3', 'pipe:1'], stderr=subprocess.DEVNULL, timeout=10)

    def covered_audio(self, fmt, picture_codec):
        """Generate real attached art; no personal music or binary fixtures."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio, picture, output = root / 'audio.mp3', root / 'picture', root / ('covered.' + fmt)
            audio.write_bytes(self.mp3)
            picture.write_bytes(subprocess.check_output([
                'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=16x16',
                '-frames:v', '1', '-c:v', picture_codec, '-f', 'image2pipe', 'pipe:1'],
                stderr=subprocess.DEVNULL, timeout=10))
            subprocess.run(['ffmpeg', '-v', 'error', '-i', str(audio), '-i', str(picture),
                '-map', '0:a:0', '-map', '1:v:0', '-c:a', 'copy' if fmt == 'mp3' else 'flac',
                '-c:v', 'copy', '-disposition:v:0', 'attached_pic', str(output)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True, timeout=10)
            return output.read_bytes()

    def connect(self, *, streaming=False, body=None, headers=None, max_seconds=10):
        data = self.mp3 if body is None else body
        values = {'content-type': 'audio/mpeg'}
        if not streaming:
            values['content-length'] = str(len(data))
        values.update(headers or {})
        opener = patch.object(media, 'open_media', side_effect=lambda *_: (Connection(), Response(data, values)))
        self.opened = opener.start()
        self.addCleanup(opener.stop)
        session = media.MediaSession(uuid.uuid4(), SOURCE, max_seconds=max_seconds).start()
        peer = socket.create_connection(('127.0.0.1', session.port), timeout=3)
        peer.settimeout(3)
        peer.sendall(b'\x01\x00\x10' + session.token.bytes)
        self.addCleanup(lambda: (session.close(), peer.close(), session.thread.join(3)))
        return session, peer

    def frame(self, peer):
        kind, data = media.radio.receive_frame(peer)
        self.assertEqual(kind, 0x12)
        self.assertEqual(len(data), 640)
        return data

    def sounding(self, session, peer):
        for _ in range(500):
            frame = self.frame(peer)
            if any(frame) and session.snapshot()['state'] == 'playing':
                return frame
        self.fail('no decoded audio')

    def silence(self, peer):
        # Controls cannot retract PCM already queued in the receiving TCP
        # socket. Observe the wire transition instead of guessing five frames.
        consecutive = 0
        for _ in range(500):
            consecutive = consecutive + 1 if self.frame(peer) == bytes(640) else 0
            if consecutive == 5:
                return
        self.fail('no sustained silence')

    def test_finite_mp3_wideband_pause_seek_volume_and_hangup_cleanup(self):
        session, peer = self.connect(max_seconds=2 * media.START_TIMEOUT)
        samples = list(struct.unpack('<320h', self.sounding(session, peer)))
        for _ in range(15):
            samples.extend(struct.unpack('<320h', self.frame(peer)))
        def amplitude(frequency):
            phase = 2 * math.pi * frequency / 16000
            return 2 * math.hypot(sum(x * math.cos(phase * i) for i, x in enumerate(samples)),
                                 sum(x * math.sin(phase * i) for i, x in enumerate(samples))) / len(samples)
        self.assertGreater(amplitude(1000), 500)
        self.assertGreater(amplitude(5000), 500)
        status = session.snapshot()
        self.assertTrue(status['seekable'])
        self.assertGreater(status['duration'], 9.9)
        self.assertEqual(status['title'], 'Embedded title')
        self.assertEqual(status['artist'], 'Embedded artist')
        session.control('pause')
        self.silence(peer)
        position = session.snapshot()['position']
        for _ in range(5):
            self.assertEqual(self.frame(peer), bytes(640))
        self.assertEqual(session.snapshot()['position'], position)
        self.assertEqual(session.snapshot()['state'], 'paused')
        session.control('seek', position=1.5)
        for _ in range(500):
            self.frame(peer)
            if session.snapshot()['position'] == 1.5:
                break
        self.assertEqual(session.snapshot()['position'], 1.5)
        session.control('resume')
        self.assertTrue(any(self.sounding(session, peer)))
        session.control('mute', muted=True)
        self.silence(peer)
        session.control('mute', muted=False)
        session.control('volume', level=0)
        self.silence(peer)
        session.control('volume', level=1)
        self.assertTrue(any(self.sounding(session, peer)))
        peer.sendall(b'\x00\x00\x00')
        session.thread.join(3)
        self.assertFalse(session.thread.is_alive())
        self.assertEqual(session.snapshot()['state'], 'idle')
        self.assertIsNotNone(session.decoder.process.poll())
        self.assertTrue(session.decoder.file.closed)

    def test_attached_art_never_blocks_mp3_or_flac_audio(self):
        for fmt, mime in (('mp3', 'audio/mpeg'), ('flac', 'audio/flac')):
            for picture_codec in ('mjpeg', 'png'):
                with self.subTest(format=fmt, picture=picture_codec):
                    data = self.covered_audio(fmt, picture_codec)
                    failures = []
                    original_probe = media.probe_file

                    def probe(*args, **kwargs):
                        try:
                            return original_probe(*args, **kwargs)
                        except media.MediaError as error:
                            failures.append(str(error))
                            raise

                    with patch.object(media, 'probe_file', side_effect=probe):
                        session, peer = self.connect(body=data, headers={'content-type': mime})
                        try:
                            self.assertTrue(any(self.sounding(session, peer)))
                            self.assertIsNone(session.snapshot()['error'])
                            # FFprobe versions may differ in whether they open
                            # excluded picture streams. Never trust a failed probe.
                            if failures:
                                self.assertIsNone(session.snapshot()['duration'])
                                self.assertFalse(session.snapshot()['seekable'])
                                self.assertEqual(session.snapshot()['title'], SOURCE['title'])
                        finally:
                            session.close()
                            peer.close()

    def test_rejected_probe_preserves_audio_but_never_enables_seek(self):
        with patch.object(media, 'probe_file', side_effect=media.MediaError('unsupported_source')):
            session, peer = self.connect()
            self.assertTrue(any(self.sounding(session, peer)))
            self.assertIsNone(session.snapshot()['duration'])
            self.assertFalse(session.snapshot()['seekable'])
            self.assertEqual(session.snapshot()['title'], SOURCE['title'])
            with self.assertRaisesRegex(media.MediaError, 'seek_unavailable'):
                session.control('seek', position=1)

    def test_other_probe_failures_still_prevent_decoding(self):
        errors = [media.MediaError('invalid_metadata'), media.MediaError('decoder_timeout'),
                  FileNotFoundError('ffprobe')]
        for error in errors:
            with self.subTest(error=str(error)), patch.object(media, 'probe_file', side_effect=error):
                session, peer = self.connect()
                session.thread.join(3)
                self.assertFalse(session.thread.is_alive())
                self.assertEqual(session.snapshot()['state'], 'error')
                self.assertIsNone(session.decoder.process)
                self.assertTrue(session.decoder.file.closed)

    def test_invalid_audio_still_fails_when_metadata_probe_is_optional(self):
        session, peer = self.connect(body=b'not an audio file' * 100)
        session.thread.join(3)
        self.assertFalse(session.thread.is_alive())
        self.assertEqual(session.snapshot()['state'], 'error')
        self.assertEqual(session.snapshot()['error'], 'playback_failed')
        self.assertIsNotNone(session.decoder.process)
        self.assertTrue(session.decoder.file.closed)

    def test_cancel_during_metadata_probe_never_starts_a_late_decoder(self):
        entered, release = threading.Event(), threading.Event()

        def blocked_probe(_file, _fmt, stop):
            entered.set()
            release.wait(3)
            self.assertTrue(stop.is_set())
            raise media.MediaError('session_ended')

        with patch.object(media, 'probe_file', side_effect=blocked_probe), \
                patch.object(media.subprocess, 'Popen') as process:
            session, peer = self.connect()
            self.assertTrue(entered.wait(1))
            threading.Timer(.1, release.set).start()
            session.close()
            session.decoder.thread.join(2)
            self.assertFalse(session.decoder.thread.is_alive())
            process.assert_not_called()

    def test_streaming_pause_keeps_socket_and_never_claims_seek(self):
        session, peer = self.connect(streaming=True, headers={'content-length': '99999999999'})
        self.sounding(session, peer)
        self.assertFalse(session.snapshot()['seekable'])
        self.assertIsNone(session.snapshot()['duration'])
        with self.assertRaisesRegex(media.MediaError, 'seek_unavailable'):
            session.control('seek', position=1)
        session.control('pause')
        process = session.decoder.process
        self.silence(peer)
        position = session.snapshot()['position']
        self.assertEqual(self.frame(peer), bytes(640))
        self.assertEqual(session.snapshot()['position'], position)
        session.control('resume')
        self.assertTrue(any(self.sounding(session, peer)))
        self.assertIs(session.decoder.process, process)
        self.assertEqual(self.opened.call_count, 1)
        self.assertGreater(session.snapshot()['position'], position)
        peer.sendall(b'\x03\x00\x01#')
        session.thread.join(3)
        self.assertFalse(session.thread.is_alive())
        self.assertIsNotNone(session.decoder.process.poll())

    def test_queue_next_previous_replace_preserve_socket_and_reap_old_decoder(self):
        # Four decoder startups exercise queue continuity, not the independent
        # whole-call lifetime. Allow each startup its production budget; each
        # sounding() observation remains independently bounded to 500 frames.
        session, peer = self.connect(max_seconds=4 * media.START_TIMEOUT)
        self.sounding(session, peer)
        old = session.decoder.process
        session.control('enqueue', source={**SOURCE, 'url': 'https://audio.example/second.mp3'}, mode='add')
        session.control('next')
        self.sounding(session, peer)
        self.assertIsNotNone(old.poll())
        self.assertEqual(session.snapshot()['queue_index'], 1)
        session.control('previous')
        self.sounding(session, peer)
        self.assertEqual(session.snapshot()['queue_index'], 0)
        session.control('enqueue', source=SOURCE, mode='replace')
        self.sounding(session, peer)
        self.assertEqual(session.snapshot()['queue_length'], 1)
        self.assertEqual(session.snapshot()['session_id'], str(session.token))

    def test_whole_session_deadline_still_ends_audio_and_reaps_decoder(self):
        started = time.monotonic()
        session, peer = self.connect(max_seconds=1)
        while True:
            kind, data = media.radio.receive_frame(peer)
            if kind == 0:
                self.assertEqual(data, b'')
                break
            self.assertEqual(kind, 0x12)
        session.thread.join(3)
        self.assertFalse(session.thread.is_alive())
        self.assertEqual(session.snapshot()['state'], 'idle')
        self.assertGreaterEqual(time.monotonic() - started, .8)
        self.assertLess(time.monotonic() - started, 3)
        if session.decoder.process:
            self.assertIsNotNone(session.decoder.process.poll())

    def test_truncated_finite_download_never_decodes_and_cleans_cache(self):
        session, peer = self.connect(body=self.mp3[:100], headers={'content-length': str(len(self.mp3))})
        session.thread.join(3)
        self.assertFalse(session.thread.is_alive())
        self.assertEqual(session.snapshot()['state'], 'error')
        self.assertIsNone(session.decoder.process)
        self.assertTrue(session.decoder.file.closed)

    def test_curated_radio_uses_existing_policy_and_strips_icy(self):
        interval = 1024
        title = b"StreamTitle='Curated track';"
        block = title.ljust(math.ceil(len(title) / 16) * 16, b'\0')
        body = bytearray()
        for index in range(0, len(self.mp3), interval):
            chunk = self.mp3[index:index + interval]
            body.extend(chunk)
            if len(chunk) == interval:
                body.extend(bytes([len(block) // 16]) + block)
        with patch.object(media.radio, 'open_stream', return_value=(Connection(), Response(bytes(body), {'icy-metaint': str(interval)}))) as opened:
            session = media.MediaSession(uuid.uuid4(), {'station': 'echo', 'artist': 'Earlier artist',
                                                       'album': 'Earlier album'}, max_seconds=10).start()
            try:
                with socket.create_connection(('127.0.0.1', session.port), timeout=3) as peer:
                    peer.sendall(b'\x01\x00\x10' + session.token.bytes)
                    self.sounding(session, peer)
                    self.assertEqual(session.snapshot()['title'], 'Curated track')
                    self.assertEqual(session.snapshot()['artist'], '')
                    self.assertEqual(session.snapshot()['album'], '')
                    self.assertEqual(session.snapshot()['source_id'], '702')
                    self.assertTrue(opened.call_args.kwargs['metadata'])
                    self.assertEqual(opened.call_args.args[0], media.radio.catalog()['echo'])
            finally:
                session.close()

    def test_cancel_during_fetch_never_starts_a_late_decoder(self):
        entered, release = threading.Event(), threading.Event()
        def blocked_fetch(*_):
            entered.set()
            release.wait(3)
            return Connection(), Response(self.mp3, {'content-type': 'audio/mpeg'})
        with patch.object(media, 'open_media', side_effect=blocked_fetch), \
                patch.object(media.subprocess, 'Popen') as process:
            session = media.MediaSession(uuid.uuid4(), SOURCE, max_seconds=10).start()
            with socket.create_connection(('127.0.0.1', session.port), timeout=3) as peer:
                peer.sendall(b'\x01\x00\x10' + session.token.bytes)
                self.assertTrue(entered.wait(1))
                threading.Timer(.1, release.set).start()
                session.close()
            session.decoder.thread.join(2)
            self.assertFalse(session.thread.is_alive())
            self.assertFalse(session.decoder.thread.is_alive())
            process.assert_not_called()


if __name__ == '__main__':
    unittest.main()
