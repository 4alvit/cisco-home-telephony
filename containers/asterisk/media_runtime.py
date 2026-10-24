"""Controllable, bounded per-call music for the gateway's owned AudioSocket.

The gateway owns call admission, exact AMI identity and command idempotency.
This module never originates, answers or hangs up a SIP channel. It accepts one
loopback AudioSocket UUID, discards microphone input, and emits slin16 frames.
URLs remain data: only Python fetches HTTP; decoders receive pipe input only.
"""
from __future__ import annotations

import contextlib
import http.client
import importlib.util
import ipaddress
import json
import math
from pathlib import Path
import re
import select
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
from urllib.parse import urljoin, urlsplit
import uuid

_spec = importlib.util.spec_from_file_location('cisco_media_radio', Path(__file__).with_name('radio.py'))
radio = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(radio)

FRAME_BYTES, FRAME_SECONDS, SAMPLE_RATE = 640, .02, 16000
MAX_BYTES, MAX_QUEUE, MAX_SECONDS = 64 * 1024 * 1024, 50, 7200
IO_TIMEOUT, START_TIMEOUT = 8, 30
CONNECT_TIMEOUT = 45  # Includes AMI setup and both authenticated phone commands.
FORMATS = {'audio/mpeg': 'mp3', 'audio/mp3': 'mp3', 'audio/aac': 'aac',
           'audio/aacp': 'aac', 'audio/flac': 'flac', 'audio/x-flac': 'flac',
           'audio/ogg': 'ogg', 'application/ogg': 'ogg', 'audio/wav': 'wav',
           'audio/x-wav': 'wav', 'audio/wave': 'wav'}
CODECS = 'mp3,mp3float,aac,aac_fixed,flac,vorbis,opus,libopus,pcm_s16le,pcm_s24le,pcm_s32le,pcm_f32le,pcm_u8'


class MediaError(ValueError):
    """A fixed public category; never include URLs or remote response bodies."""


def _number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def _text(value):
    return isinstance(value, str) and len(value) <= 256 and not re.search(r'[\x00-\x1f\x7f]', value)


def _url(url):
    if (not isinstance(url, str) or not 1 <= len(url) <= 4096 or not url.isascii()
            or re.search(r'[\x00-\x20\x7f\\]', url)):
        raise MediaError('invalid_source')
    try:
        value = urlsplit(url)
        if (value.scheme not in ('http', 'https') or not value.hostname or
                value.username is not None or value.password is not None or
                value.fragment or '%' in value.hostname or
                (value.port is not None and not 1 <= value.port <= 65535)):
            raise ValueError
    except ValueError:
        raise MediaError('invalid_source') from None
    return value


def _origin(url):
    value = _url(url)
    return value.scheme, value.hostname.lower(), value.port or (443 if value.scheme == 'https' else 80)


def _origins(allowed):
    if not isinstance(allowed, (tuple, list)) or len(allowed) > 16:
        raise MediaError('invalid_origins')
    result = set()
    for item in allowed:
        parsed = _url(item)
        if parsed.path not in ('', '/') or parsed.query:
            raise MediaError('invalid_origins')
        result.add(_origin(item))
    return result


def validate_source(source, allowed_origins=()):
    """Validate before call admission; this performs no DNS or network access."""
    _origins(allowed_origins)
    if (not isinstance(source, dict) or set(source) - {'station', 'url', 'title', 'artist', 'album', 'duration'}
            or ('station' in source) == ('url' in source)):
        raise MediaError('invalid_source')
    result = dict(source)
    if 'station' in result:
        if not isinstance(result['station'], str) or result['station'] not in radio.catalog():
            raise MediaError('invalid_source')
    else:
        parsed = _url(result['url'])
        # Reject literal forbidden destinations even before DNS/admission.
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None:
            _check_addresses([str(address)], _origin(result['url']) in _origins(allowed_origins))
    if any(not _text(result[key]) for key in ('title', 'artist', 'album') if key in result):
        raise MediaError('invalid_metadata')
    if 'duration' in result and not _number(result['duration'], 0, MAX_SECONDS):
        raise MediaError('invalid_metadata')
    return result


def _check_addresses(addresses, approved):
    if not addresses:
        raise MediaError('source_address_rejected')
    for item in addresses:
        address = ipaddress.ip_address(item)
        if (address.is_loopback or address.is_link_local or address.is_multicast or
                address.is_unspecified or address.is_reserved or
                not (address.is_global or approved and address.is_private)):
            raise MediaError('source_address_rejected')


def open_media(url, allowed_origins=()):
    """Pin every connection to checked DNS results; revalidate every redirect."""
    allowed = _origins(allowed_origins)
    for redirect in range(4):
        parsed = _url(url)
        scheme, host, port = _origin(url)
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        _check_addresses([item[4][0] for item in addresses], (scheme, host, port) in allowed)
        connection = http.client.HTTPConnection(host, port, timeout=IO_TIMEOUT)
        try:
            for family, kind, protocol, _, target in addresses:
                sock = socket.socket(family, kind, protocol)
                sock.settimeout(IO_TIMEOUT)
                try:
                    sock.connect(target)
                    if scheme == 'https':
                        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
                    connection.sock = sock
                    break
                except OSError:
                    sock.close()
            else:
                raise MediaError('source_unavailable')
            target = (parsed.path or '/') + ('?' + parsed.query if parsed.query else '')
            connection.request('GET', target, headers={'User-Agent': 'HomeTelephony/2.0',
                               'Icy-MetaData': '1', 'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location or redirect == 3:
                    raise MediaError('source_redirect_rejected')
                url = urljoin(url, location)
                connection.close()
                continue
            if response.status != 200:
                raise MediaError('source_unavailable')
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise MediaError('source_encoding_rejected')
            return connection, response
        except BaseException:
            connection.close()
            raise
    raise MediaError('source_redirect_rejected')


class IcyReader:
    """Strip bounded ICY metadata blocks before bytes reach the audio decoder."""
    def __init__(self, response, metadata):
        self.response, self.metadata = response, metadata
        interval = response.getheader('icy-metaint')
        if interval is not None and (not interval.isdigit() or not 1 <= int(interval) <= 16 * 1024 * 1024):
            raise MediaError('invalid_icy_metadata')
        self.interval = int(interval) if interval else 0
        self.remaining = self.interval

    def _exact(self, count):
        data = bytearray()
        while len(data) < count:
            part = self.response.read(count - len(data))
            if not part:
                raise MediaError('truncated_source')
            data.extend(part)
        return bytes(data)

    def read(self, count):
        if self.interval and not self.remaining:
            length = self._exact(1)[0] * 16
            if length:
                raw = self._exact(length).rstrip(b'\0')
                text = raw.decode('utf-8', errors='replace')
                match = re.search(r"(?:^|;)\s*StreamTitle='(.*?)';", text)
                if match:
                    title = match[1]
                    # StreamTitle is arbitrary display text, not a guaranteed
                    # artist/title delimiter (including Music Assistant flow).
                    # Basic ICY has no structured artist or album. Clear old
                    # track metadata instead of attaching it to the new title.
                    values = {'title': title, 'artist': '', 'album': ''}
                    if all(_text(value) for value in values.values()):
                        self.metadata(values)
            self.remaining = self.interval
        count = min(count, self.remaining) if self.interval else count
        data = self.response.read1(count) if hasattr(self.response, 'read1') else self.response.read(count)
        if self.interval:
            self.remaining -= len(data)
        return data


def decoder_args(fmt, position=0):
    if fmt not in set(FORMATS.values()) or not _number(position, 0, MAX_SECONDS):
        raise MediaError('unsupported_source')
    return ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-protocol_whitelist', 'pipe', '-codec_whitelist', CODECS,
            '-threads', '1', '-filter_threads', '1', '-probesize', '32768',
            '-f', fmt, '-i', 'pipe:0', '-ss', str(position), '-map', '0:a:0',
            '-vn', '-sn', '-dn', '-ac', '1', '-ar', str(SAMPLE_RATE),
            '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1']


def _terminate(process):
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
    for stream in (process.stdin, process.stdout):
        if stream:
            with contextlib.suppress(OSError, ValueError):
                stream.close()


def probe_file(file, fmt, stop=None):
    """Read only small metadata output; finite local input is already bounded."""
    file.seek(0)
    # A generated, inherited descriptor provides seekable input for MP3
    # duration. No caller path or URL is handed to ffprobe.
    command = ['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
               '-codec_whitelist', CODECS, '-f', fmt, '-i', f'/dev/fd/{file.fileno()}',
               '-select_streams', 'a:0', '-show_entries',
               'format=duration:format_tags=title,artist,album:stream=duration:stream_tags=title,artist,album',
               '-of', 'json']
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, pass_fds=(file.fileno(),))
    output, deadline = bytearray(), time.monotonic() + 10
    try:
        while time.monotonic() < deadline:
            if stop is not None and stop.is_set():
                raise MediaError('session_ended')
            if not select.select([process.stdout], [], [], .1)[0]:
                continue
            block = process.stdout.read1(4096)
            if not block:
                break
            output.extend(block)
            if len(output) > 65536:
                raise MediaError('invalid_metadata')
        else:
            raise MediaError('decoder_timeout')
        if process.wait(timeout=1):
            raise MediaError('unsupported_source')
        document = json.loads(output)
        result = {}
        for item in [document.get('format', {}), *document.get('streams', [])]:
            try:
                duration = float(item.get('duration', 'nan'))
                if _number(duration, .001, MAX_SECONDS):
                    result['duration'] = duration
            except (TypeError, ValueError):
                pass
            tags = item.get('tags', {})
            if isinstance(tags, dict):
                for key in ('title', 'artist', 'album'):
                    if _text(tags.get(key)):
                        result[key] = tags[key]
        return result
    except (json.JSONDecodeError, TypeError, AttributeError, subprocess.TimeoutExpired):
        raise MediaError('invalid_metadata') from None
    finally:
        _terminate(process)
        file.seek(0)


def gain_pcm(data, level, muted=False):
    """Software gain before G.722 encoding; saturate signed 16-bit samples."""
    if not _number(level, 0, 1) or type(muted) is not bool or len(data) % 2:
        raise MediaError('invalid_volume')
    if muted or level == 0:
        return bytes(len(data))
    if level == 1:
        return data
    values = struct.unpack('<' + 'h' * (len(data) // 2), data)
    return struct.pack('<' + 'h' * len(values), *(max(-32768, min(32767, round(x * level))) for x in values))


class Decoder:
    """One current track: bounded fetch/cache, decoder, and cancellation."""
    def __init__(self, source, origins, max_bytes, metadata, position=0, cached=None):
        self.source, self.origins, self.max_bytes = source, origins, max_bytes
        self.metadata, self.position = metadata, position
        self.stop = threading.Event()
        self.process_lock = threading.Lock()
        self.close_lock = threading.Lock()
        self.ready = threading.Event()
        self.process = self.connection = self.response = None
        self.file, self.format = cached or (None, None)
        self.error, self.duration, self.finite = None, None, False
        self.keep_file = False
        self.pending = bytearray()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            if self.file is not None:
                self.finite = True
            elif 'station' in self.source:
                station = radio.catalog()[self.source['station']]
                self.format = station['format']
                if station.get('hls'):
                    chunks = radio.hls_chunks(station, self.stop)
                else:
                    self.connection, self.response = radio.open_stream(station, metadata=True)
                    reader = IcyReader(self.response, self.metadata)
                    chunks = iter(lambda: reader.read(8192), b'')
            else:
                self.connection, self.response = open_media(self.source['url'], self.origins)
                mime = self.response.getheader('Content-Type', '').split(';', 1)[0].lower()
                self.format = FORMATS.get(mime)
                if self.format is None:
                    raise MediaError('unsupported_source')
                reader = IcyReader(self.response, self.metadata)
                length = self.response.getheader('Content-Length')
                if length is not None and (not length.isdigit() or not 0 < int(length) <= 2**63 - 1):
                    raise MediaError('invalid_source_length')
                # Music Assistant may advertise a synthetic 12-hour length on
                # a live flow. Only small, completed downloads become seekable.
                self.finite = length is not None and int(length) <= self.max_bytes and not reader.interval
                if self.finite:
                    self.file = tempfile.TemporaryFile()
                    left, deadline = int(length), time.monotonic() + START_TIMEOUT - 10
                    while left and not self.stop.is_set():
                        if time.monotonic() >= deadline:
                            raise MediaError('source_timeout')
                        data = reader.read(min(left, 65536))
                        if not data:
                            raise MediaError('truncated_source')
                        self.file.write(data)
                        left -= len(data)
                    self.connection.close()
                else:
                    chunks = iter(lambda: reader.read(8192), b'')
            if self.stop.is_set():
                return
            if self.finite:
                info = probe_file(self.file, self.format, self.stop)
                self.duration = info.get('duration')
                self.metadata(info)
                self.file.seek(0)
            with self.process_lock:
                if self.stop.is_set():
                    return
                self.process = subprocess.Popen(decoder_args(self.format, self.position),
                    stdin=self.file if self.finite else subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            self.ready.set()
            if not self.finite:
                # Streams have a per-call budget as well as the session deadline.
                left = MAX_SECONDS * 128000 + self.max_bytes
                for data in chunks:
                    if self.stop.is_set():
                        break
                    left -= len(data)
                    if left < 0:
                        raise MediaError('source_too_large')
                    view = memoryview(data)
                    while view and not self.stop.is_set():
                        written = self.process.stdin.write(view)
                        if not written:
                            raise MediaError('decoder_failed')
                        view = view[written:]
                with contextlib.suppress(OSError, ValueError):
                    self.process.stdin.close()
        except (OSError, ValueError, http.client.HTTPException):
            if not self.stop.is_set():
                self.error = 'source_failed'
        finally:
            self.ready.set()
            if self.connection:
                self.connection.close()
            if self.stop.is_set() and self.file and not self.keep_file:
                with contextlib.suppress(OSError):
                    self.file.close()

    def read_frame(self):
        """None means buffering; b'' means terminal EOF; otherwise one frame."""
        if self.error:
            raise MediaError(self.error)
        if self.process is None:
            return None
        while len(self.pending) < FRAME_BYTES and select.select([self.process.stdout], [], [], 0)[0]:
            data = self.process.stdout.read(FRAME_BYTES - len(self.pending))
            if not data:
                if self.pending:
                    data = bytes(self.pending).ljust(FRAME_BYTES, b'\0')
                    self.pending.clear()
                    return data
                code = self.process.poll()
                if code is None:
                    return None
                if code != 0:
                    raise MediaError('decoder_failed')
                return b''
            self.pending.extend(data)
        if len(self.pending) == FRAME_BYTES:
            data = bytes(self.pending)
            self.pending.clear()
            return data
        return None

    def close(self, keep_file=False):
        self.keep_file = keep_file
        self.stop.set()
        with self.close_lock:
            if self.connection:
                sock = self.connection.sock
                if sock:
                    with contextlib.suppress(OSError):
                        sock.shutdown(socket.SHUT_RDWR)
                self.connection.close()
            with self.process_lock:
                _terminate(self.process)
            if self.thread is not threading.current_thread():
                self.thread.join(timeout=1)
            if self.file and not keep_file and not self.thread.is_alive():
                with contextlib.suppress(OSError):
                    self.file.close()
        return (self.file, self.format) if keep_file else None


class MediaSession:
    """Thread-safe controls for one admitted call, never for a replacement call."""
    def __init__(self, token, source, allowed_origins=(), max_seconds=MAX_SECONDS,
                 max_bytes=MAX_BYTES, max_queue=MAX_QUEUE):
        if (type(token) is not uuid.UUID or not _number(max_seconds, 1, MAX_SECONDS)
                or type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES
                or type(max_queue) is not int or not 1 <= max_queue <= MAX_QUEUE):
            raise MediaError('invalid_session')
        self.token, self.origins = token, tuple(allowed_origins)
        self.max_seconds, self.max_bytes, self.max_queue = max_seconds, max_bytes, max_queue
        self.queue = [validate_source(source, self.origins)]
        self.index, self.generation = 0, 0
        self.lock, self.stop_event = threading.RLock(), threading.Event()
        self.state, self.error, self.position = 'buffering', None, 0.0
        self.duration, self.seekable = None, False
        self.volume, self.muted, self.paused = .5, False, False
        self.metadata = {}
        self.decoder = self.peer = self.thread = None
        self.seek_position = None
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.listener.settimeout(.2)
        self.port = self.listener.getsockname()[1]

    def start(self):
        with self.lock:
            if self.thread is not None or self.stop_event.is_set():
                raise MediaError('session_already_started')
            self.thread = threading.Thread(target=self._serve, daemon=True)
            self.thread.start()
        return self

    def _metadata(self, generation, values):
        with self.lock:
            if generation == self.generation:
                self.metadata.update({key: value for key, value in values.items()
                                      if key in ('title', 'artist', 'album') and _text(value)})

    def snapshot(self):
        with self.lock:
            source = self.queue[self.index]
            station = radio.catalog().get(source.get('station'), {})
            return {'session_id': str(self.token), 'source_id': source.get('url', station.get('extension')),
                    'state': self.state, 'position': round(self.position, 3), 'duration': self.duration,
                    'seekable': self.seekable, 'title': self.metadata.get('title', source.get('title', station.get('name', ''))),
                    'artist': self.metadata.get('artist', source.get('artist', '')),
                    'album': self.metadata.get('album', source.get('album', '')),
                    'volume_level': self.volume, 'is_volume_muted': self.muted,
                    'queue_index': self.index, 'queue_length': len(self.queue), 'error': self.error}

    def _change(self, index):
        self.index, self.generation = index, self.generation + 1
        self.position, self.duration, self.seekable = 0.0, None, False
        self.paused, self.state, self.metadata, self.seek_position = False, 'buffering', {}, None

    def control(self, action, **payload):
        source = None
        if action == 'enqueue':
            source = validate_source(payload.get('source'), self.origins)
        expected = {'pause': set(), 'resume': set(), 'stop': set(), 'next': set(), 'previous': set(), 'clear_queue': set(),
                    'seek': {'position'}, 'volume': {'level'}, 'mute': {'muted'}, 'enqueue': {'source', 'mode'}}
        if action not in expected or set(payload) != expected[action]:
            raise MediaError('invalid_control')
        if action == 'stop':
            self.close()
            return self.snapshot()
        with self.lock:
            if self.stop_event.is_set():
                raise MediaError('session_ended')
            if action in ('pause', 'resume'):
                self.paused = action == 'pause'
                self.state = 'paused' if self.paused else 'buffering'
            elif action == 'seek':
                if not self.seekable or not _number(payload['position'], 0, self.duration):
                    raise MediaError('seek_unavailable')
                self.seek_position = float(payload['position'])
                self.state = 'paused' if self.paused else 'buffering'
            elif action == 'volume':
                if not _number(payload['level'], 0, 1):
                    raise MediaError('invalid_volume')
                self.volume = float(payload['level'])
            elif action == 'mute':
                if type(payload['muted']) is not bool:
                    raise MediaError('invalid_volume')
                self.muted = payload['muted']
            elif action == 'enqueue':
                mode = payload['mode']
                if mode not in ('replace', 'play', 'add', 'next'):
                    raise MediaError('invalid_queue_mode')
                if mode == 'replace':
                    self.queue = [source]
                    self._change(0)
                else:
                    if len(self.queue) >= self.max_queue:
                        raise MediaError('queue_full')
                    index = len(self.queue) if mode == 'add' else self.index + 1
                    self.queue.insert(index, source)
                    if mode == 'play':
                        self._change(index)
            elif action in ('next', 'previous'):
                index = self.index + (1 if action == 'next' else -1)
                if not 0 <= index < len(self.queue):
                    raise MediaError('queue_boundary')
                self._change(index)
            elif action == 'clear_queue':
                self.queue, self.index = [self.queue[self.index]], 0
            return self.snapshot()

    def _receive(self):
        try:
            while not self.stop_event.is_set():
                if not select.select([self.peer], [], [], .2)[0]:
                    continue
                kind, data = radio.receive_frame(self.peer)
                if kind in (0, 0xff) or kind == 3 and data == b'#':
                    break
                if (kind not in (0x10, 0x12, 3) or
                        kind in (0x10, 0x12) and (not data or len(data) % 2) or
                        kind == 3 and (len(data) != 1 or data not in b'0123456789ABCD*#')):
                    raise MediaError('invalid_audio_packet')
        except (OSError, ValueError, EOFError):
            pass
        self.stop_event.set()

    def _serve(self):
        try:
            accept_deadline = time.monotonic() + CONNECT_TIMEOUT
            while not self.stop_event.is_set():
                try:
                    self.peer, address = self.listener.accept()
                    break
                except socket.timeout:
                    if time.monotonic() >= accept_deadline:
                        raise MediaError('session_connect_timeout') from None
            else:
                return
            self.peer.settimeout(3)
            kind, data = radio.receive_frame(self.peer)
            if address[0] != '127.0.0.1' or kind != 1 or data != self.token.bytes:
                raise MediaError('session_identity_mismatch')
            self.listener.close()
            self.peer.settimeout(IO_TIMEOUT)
            threading.Thread(target=self._receive, daemon=True).start()
            deadline = time.monotonic() + self.max_seconds
            generation, next_frame, last_audio = -1, time.monotonic(), time.monotonic()
            pending_frame = None
            while not self.stop_event.is_set() and time.monotonic() < deadline:
                with self.lock:
                    changed = generation != self.generation
                    seeking = self.seek_position
                    source, current_generation = dict(self.queue[self.index]), self.generation
                    self.seek_position = None
                if changed or seeking is not None:
                    pending_frame = None
                    cached = self.decoder.close(keep_file=not changed) if self.decoder else None
                    with self.lock:
                        if self.stop_event.is_set():
                            if cached and cached[0]:
                                cached[0].close()
                            break
                        if current_generation != self.generation:
                            # A newer queue command arrived while the previous
                            # decoder was being reaped. Do not fetch old media.
                            if cached and cached[0]:
                                cached[0].close()
                            continue
                        self.decoder = Decoder(source, self.origins, self.max_bytes,
                            lambda info, gen=current_generation: self._metadata(gen, info),
                            position=seeking or 0, cached=cached)
                    generation, last_audio = current_generation, time.monotonic()
                    if seeking is not None:
                        with self.lock:
                            self.position = seeking
                with self.lock:
                    paused = self.paused
                    self.duration = self.decoder.duration
                    self.seekable = self.decoder.finite and self.duration is not None
                # Preserve position for both finite tracks and Music Assistant
                # flows. Bounded decoder/TCP backpressure replaces buffering or
                # silently dropping live bytes; never reopen a paused URL.
                failure = None
                try:
                    frame = (None if paused else pending_frame if pending_frame is not None
                             else self.decoder.read_frame())
                except (OSError, ValueError, EOFError) as error:
                    frame, failure = None, error
                with self.lock:
                    # EOF and errors belong to the decoder that produced them,
                    # not a queue item selected concurrently by Next/Replace.
                    if generation != self.generation:
                        continue
                    if failure is not None:
                        self.state, self.error = 'error', 'playback_failed'
                        self.stop_event.set()
                        break
                    if frame == b'':
                        if self.index + 1 < len(self.queue):
                            self._change(self.index + 1)
                            continue
                        self.stop_event.set()
                        break
                now = time.monotonic()
                if frame is not None:
                    last_audio = now
                elif paused:
                    last_audio = now
                elif not paused and now - last_audio > (START_TIMEOUT if not self.position else IO_TIMEOUT):
                    raise MediaError('source_timeout')
                if frame is not None:
                    pending_frame = frame
                if self.stop_event.wait(max(0, next_frame - now)):
                    break
                with self.lock:
                    # Controls can arrive during pacing. Serialize this bounded
                    # socket write with their acknowledgement, and never emit a
                    # frame from a replaced track or a pre-seek position. Keep a
                    # paused frame so Resume does not skip any decoded samples.
                    if self.stop_event.is_set():
                        break
                    if generation != self.generation or self.seek_position is not None:
                        pending_frame = None
                        continue
                    paused = self.paused
                    self.state = 'paused' if paused else 'playing' if pending_frame is not None else 'buffering'
                    audio = (bytes(FRAME_BYTES) if paused or pending_frame is None else
                             gain_pcm(pending_frame, self.volume, self.muted))
                    radio.send_audio(self.peer, audio)
                    if not paused and pending_frame is not None:
                        self.position += FRAME_SECONDS
                        pending_frame = None
                next_frame = max(next_frame + FRAME_SECONDS, time.monotonic() - FRAME_SECONDS)
        except (OSError, ValueError, EOFError):
            with self.lock:
                if not self.stop_event.is_set():
                    self.state, self.error = 'error', 'playback_failed'
        finally:
            self._shutdown()

    def _shutdown(self):
        self.stop_event.set()
        if self.decoder:
            self.decoder.close()
        if self.peer:
            with contextlib.suppress(OSError):
                self.peer.sendall(b'\x00\x00\x00')
                self.peer.shutdown(socket.SHUT_RDWR)
            self.peer.close()
        self.listener.close()
        with self.lock:
            if self.state != 'error':
                self.state = 'idle'

    def close(self):
        # Serialize cancellation with replacement decoder creation; otherwise
        # a concurrent Next could create a worker after cleanup captured it.
        with self.lock:
            self.stop_event.set()
            decoder = self.decoder
        with contextlib.suppress(OSError):
            self.listener.shutdown(socket.SHUT_RDWR)
        if self.peer:
            with contextlib.suppress(OSError):
                self.peer.shutdown(socket.SHUT_RDWR)
        self.listener.close()
        if decoder:
            decoder.close()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)
        with self.lock:
            if self.state != 'error':
                self.state = 'idle'
