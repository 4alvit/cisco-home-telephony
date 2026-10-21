"""Bounded Cisco XML voice and camera UI with an optional temporary HTTP adapter.

PhoneVoiceDisplay performs no networking. VoiceDisplayServer explicitly starts
a source-restricted listener for one foreground controller. Neither pushes to
phones, controls media or fetches camera URLs. The host supplies current focus
and bounded callbacks. See docs/voice-display.md for the integration gate.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import io
import ipaddress
import math
import re
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qsl, urlsplit
import xml.etree.ElementTree as ET


MIME = 'text/xml; charset=utf-8'
IMAGE_SIZE = (298, 156)
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
STATES = ('Listening', 'Thinking', 'Speaking', 'Stopped', 'Error')
TERMINAL = ('Stopped', 'Error')
BACKGROUND = (16, 24, 32)


class DisplayError(Exception):
    def __init__(self, reason, status=400):
        self.reason, self.status = reason, status
        super().__init__(reason)


def require(value, reason, status=400):
    if not value:
        raise DisplayError(reason, status)


def safe_text(value, limit):
    require(isinstance(value, str), 'invalid_text')
    # XML 1.0 excludes C0 controls, unpaired surrogates and U+FFFE/U+FFFF.
    # Bound work as well as retained text, even for an all-control-character input.
    return ''.join(char for char in value[:limit * 4] if char in '\t\n\r' or
                   0x20 <= ord(char) <= 0xD7FF or
                   0xE000 <= ord(char) <= 0xFFFD or
                   0x10000 <= ord(char) <= 0x10FFFF)[:limit]


def phone_image(data):
    """Decode bounded PNG/JPEG, orient and letterbox to an exact RGB phone PNG."""
    require(isinstance(data, bytes) and 0 < len(data) <= MAX_IMAGE_BYTES,
            'invalid_image_size', 413)
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise DisplayError('image_support_unavailable', 503) from None
    try:
        with Image.open(io.BytesIO(data)) as source:
            require(source.format in ('PNG', 'JPEG') and
                    0 < source.width * source.height <= MAX_IMAGE_PIXELS and
                    getattr(source, 'n_frames', 1) == 1, 'invalid_image')
            source.load()
            oriented = ImageOps.exif_transpose(source).convert('RGBA')
            fitted = ImageOps.contain(oriented, IMAGE_SIZE, Image.Resampling.LANCZOS)
            # A fresh canvas strips EXIF, ICC profiles and arbitrary PNG metadata.
            canvas = Image.new('RGB', IMAGE_SIZE, BACKGROUND)
            offset = ((IMAGE_SIZE[0] - fitted.width) // 2,
                      (IMAGE_SIZE[1] - fitted.height) // 2)
            canvas.paste(fitted, offset, fitted.getchannel('A'))
            output = io.BytesIO()
            canvas.save(output, format='PNG')
            return output.getvalue()
    except DisplayError:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise DisplayError('invalid_image') from None


def avatar(state):
    """Small deterministic illustration; no animation or generated assets."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        raise DisplayError('image_support_unavailable', 503) from None
    image = Image.new('RGB', IMAGE_SIZE, BACKGROUND)
    draw = ImageDraw.Draw(image)
    color = {'Listening': '#61dca3', 'Thinking': '#e8c96a',
             'Speaking': '#69bfff', 'Stopped': '#a4adb5', 'Error': '#ff8d83'}[state]
    draw.rounded_rectangle((102, 22, 196, 115), radius=24, outline=color, width=3)
    for x in (129, 167):
        draw.ellipse((x - 4, 52, x + 4, 60), fill=color)
    if state == 'Speaking':
        draw.ellipse((136, 77, 162, 98), outline=color, width=3)
    elif state == 'Stopped':
        draw.rectangle((140, 80, 158, 95), fill=color)
    elif state == 'Error':
        draw.line((149, 75, 149, 86), fill=color, width=3)
        draw.ellipse((147, 91, 151, 95), fill=color)
    else:
        draw.line((136, 87, 162, 87), fill=color, width=3)
    if state == 'Listening':
        for x in (74, 88, 210, 224):
            draw.line((x, 51 if x in (88, 210) else 63,
                       x, 94 if x in (88, 210) else 82), fill=color, width=3)
    if state == 'Thinking':
        for x in (209, 224, 239):
            draw.ellipse((x, 37, x + 6, 43), fill=color)
    draw.text((149, 131), state, fill=color, anchor='mm')
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


@dataclass(frozen=True)
class Frame:
    data: bytes
    captured_at: float


@dataclass(frozen=True)
class SessionRef:
    session_id: str
    page_url: str
    stop_url: str


@dataclass(frozen=True)
class DisplayResponse:
    status: int
    body: bytes
    content_type: str
    headers: dict
    allow_present: bool


@dataclass
class _Session:
    session_id: str
    mode: str
    state: str
    text: str
    expires: float
    updated_at: float
    frame: bytes = None
    captured_at: float = None
    frame_source: object = None
    revision: int = 1
    last_fetch: float = None
    stop_attempted: bool = False
    stop_pending: bool = False
    preempted: bool = False


class PhoneVoiceDisplay:
    def __init__(self, base_urls, *, authorize, focus, on_stop,
                 refresh_seconds=3, ttl_seconds=120, max_sessions=16,
                 clock=time.monotonic, wall_clock=time.time):
        require(isinstance(base_urls, dict) and 1 <= len(base_urls) <= 64,
                'invalid_base_urls')
        require(type(refresh_seconds) is int and 2 <= refresh_seconds <= 5,
                'invalid_refresh_seconds')
        require(type(ttl_seconds) is int and 5 <= ttl_seconds <= 360,
                'invalid_ttl_seconds')
        require(type(max_sessions) is int and 1 <= max_sessions <= 64,
                'invalid_max_sessions')
        require(all(callable(fn) for fn in (authorize, focus, on_stop, clock, wall_clock)),
                'callbacks_required')
        self.base_urls = {}
        for phone_id, value in base_urls.items():
            require(isinstance(phone_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', phone_id),
                    'invalid_phone_id')
            require(isinstance(value, str) and value.isascii() and len(value) <= 256 and
                    not any(ord(c) <= 32 or ord(c) == 127 for c in value), 'invalid_base_url')
            try:
                parsed = urlsplit(value)
                port = parsed.port
                require(parsed.scheme in ('http', 'https') and parsed.hostname and
                        parsed.username is None and parsed.password is None and
                        not parsed.query and not parsed.fragment and
                        re.fullmatch(r'/[A-Za-z0-9_/-]*', parsed.path) and
                        '//' not in parsed.path and (port is None or 1 <= port <= 65535),
                        'invalid_base_url')
                try:
                    ipaddress.ip_address(parsed.hostname)
                except ValueError:
                    require(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?',
                                         parsed.hostname) and '..' not in parsed.hostname,
                            'invalid_base_url')
            except ValueError:
                raise DisplayError('invalid_base_url') from None
            self.base_urls[phone_id] = value.rstrip('/')
        self.authorize, self.focus, self.on_stop = authorize, focus, on_stop
        self.refresh_seconds, self.ttl_seconds = refresh_seconds, ttl_seconds
        self.max_sessions, self.clock, self.wall_clock = max_sessions, clock, wall_clock
        self._sessions = {}
        self._pending_stops = set()
        self._lock = threading.RLock()
        # Even a stopped/replaced session cannot start a second slow camera fetch.
        self._fetch_locks = {phone_id: threading.Lock() for phone_id in self.base_urls}

    def _authorized(self, phone_id, action, context):
        require(isinstance(phone_id, str) and phone_id in self.base_urls, 'forbidden', 403)
        try:
            allowed = self.authorize(phone_id, action, context) is True
        except Exception:
            allowed = False
        require(allowed, 'forbidden', 403)

    def _has_focus(self, phone_id, context):
        try:
            return self.focus(phone_id, context) in ('idle', 'assistant')
        except Exception:
            return False

    def _require_start_focus(self, phone_id, context):
        if not self._has_focus(phone_id, context):
            with self._lock:
                self._prune()
                row = self._sessions.get(phone_id)
                if row is not None:
                    self._preempt(row)
            raise DisplayError('phone_focus_unavailable', 409)

    def _prune(self):
        now = self.clock()
        expired = [phone_id for phone_id, row in self._sessions.items() if row.expires <= now]
        for phone_id in expired:
            del self._sessions[phone_id]
        return len(expired)

    def cleanup(self):
        """Host maintenance tick; expiry releases memory, never controls hardware."""
        with self._lock:
            return self._prune()

    def stats(self):
        with self._lock:
            self._prune()
            return {'sessions': len(self._sessions),
                    'frames': sum(row.frame is not None for row in self._sessions.values()),
                    'frame_bytes': sum(len(row.frame or b'') for row in self._sessions.values())}

    def _row(self, phone_id, session_id):
        self._prune()
        row = self._sessions.get(phone_id)
        require(isinstance(session_id, str) and re.fullmatch(r'[0-9a-f]{32}', session_id) and
                row is not None and secrets.compare_digest(row.session_id, session_id),
                'expired_or_unknown_session', 404)
        return row

    def _ref(self, phone_id, row):
        url = self.base_urls[phone_id] + '/session/' + row.session_id
        return SessionRef(row.session_id, url, url + '/stop')

    def _timestamp(self, value):
        require(type(value) in (int, float) and math.isfinite(value) and
                0 <= value <= self.wall_clock() + 5, 'invalid_frame_timestamp')
        return float(value)

    def _preempt(self, row):
        row.preempted = True
        row.state, row.text = 'Stopped', 'Display yielded to phone focus.'
        row.frame, row.frame_source = None, None
        row.updated_at = self.wall_clock()

    def start(self, phone_id, *, mode='voice', state='Listening', text='', image=None,
              captured_at=None, frame_source=None, context=None):
        self._authorized(phone_id, 'start', context)
        require(mode in ('voice', 'camera') and state in STATES, 'invalid_display_state')
        require(frame_source is None or (mode == 'camera' and callable(frame_source)),
                'invalid_frame_source')
        self._require_start_focus(phone_id, context)
        text = safe_text(text, 1000)
        with self._lock:
            self._prune()
            require(phone_id not in self._pending_stops, 'stop_pending', 409)
            prior = self._sessions.get(phone_id)
            require(prior is None or (prior.state in TERMINAL and not prior.stop_pending),
                    'display_active', 409)
            require(prior is not None or len(self._sessions) < self.max_sessions,
                    'display_capacity', 429)
        frame_at = None
        if image is not None:
            frame_at = self._timestamp(captured_at if captured_at is not None else
                                       (self.wall_clock() if mode == 'voice' else None))
            image = phone_image(image)
        elif mode == 'voice' and state not in TERMINAL:
            image = avatar(state)
        require(captured_at is None or frame_at is not None, 'invalid_frame_timestamp')
        self._require_start_focus(phone_id, context)
        with self._lock:
            self._prune()
            require(phone_id not in self._pending_stops, 'stop_pending', 409)
            # Concurrent starts must not overwrite each other after conversion.
            require(self._sessions.get(phone_id) is prior, 'display_changed', 409)
            require(prior is not None or len(self._sessions) < self.max_sessions,
                    'display_capacity', 429)
            row = _Session(secrets.token_hex(16), mode, state, text,
                           self.clock() + self.ttl_seconds, self.wall_clock(),
                           frame=image, captured_at=frame_at, frame_source=frame_source,
                           last_fetch=self.clock() if image is not None and mode == 'camera' else None)
            if state in TERMINAL:
                row.frame, row.frame_source = None, None
            self._sessions[phone_id] = row
            return self._ref(phone_id, row)

    def update(self, phone_id, session_id, *, state=None, text=None, image=None,
               captured_at=None, context=None):
        self._authorized(phone_id, 'update', context)
        require(state is None or state in STATES, 'invalid_display_state')
        text = safe_text(text, 1000) if text is not None else None
        with self._lock:
            row = self._row(phone_id, session_id)
            require(row.state not in TERMINAL and not row.stop_attempted,
                    'display_stopped', 409)
            revision, mode = row.revision, row.mode
        if not self._has_focus(phone_id, context):
            with self._lock:
                self._preempt(self._row(phone_id, session_id))
            raise DisplayError('phone_focus_unavailable', 409)
        frame_at, frame = None, None
        if image is not None:
            frame_at = self._timestamp(captured_at if captured_at is not None else
                                       (self.wall_clock() if mode == 'voice' else None))
            frame = phone_image(image)
        elif state is not None and mode == 'voice':
            frame = avatar(state)
        require(captured_at is None or frame_at is not None, 'invalid_frame_timestamp')
        allowed = self._has_focus(phone_id, context)
        with self._lock:
            current = self._row(phone_id, session_id)
            require(current is row and row.revision == revision and not row.stop_attempted and
                    row.state not in TERMINAL, 'display_changed', 409)
            if not allowed:
                self._preempt(row)
                raise DisplayError('phone_focus_unavailable', 409)
            if state is not None:
                row.state = state
            if text is not None:
                row.text = text
            if frame is not None:
                row.frame, row.captured_at = frame, frame_at
                if mode == 'camera':
                    row.last_fetch = self.clock()
            if row.state in TERMINAL:
                row.frame, row.frame_source = None, None
            row.revision += 1
            row.updated_at = self.wall_clock()
            return self._ref(phone_id, row)

    def should_present(self, phone_id, session_id, *, context=None):
        self._authorized(phone_id, 'read', context)
        allowed = self._has_focus(phone_id, context)
        with self._lock:
            row = self._row(phone_id, session_id)
            if not allowed:
                self._preempt(row)
            return (allowed and not row.preempted and not row.stop_attempted and
                    row.state not in TERMINAL)

    def _refresh_camera(self, phone_id, session_id, context):
        fetch_lock = self._fetch_locks[phone_id]
        if not fetch_lock.acquire(blocking=False):
            return
        try:
            with self._lock:
                row = self._row(phone_id, session_id)
                if row.frame_source is None or row.state in TERMINAL or row.preempted or (
                        row.last_fetch is not None and
                        self.clock() - row.last_fetch < self.refresh_seconds):
                    return
                row.last_fetch = self.clock()
                source, revision = row.frame_source, row.revision
            try:
                result = source(phone_id)
                require(isinstance(result, Frame), 'invalid_frame')
                timestamp = self._timestamp(result.captured_at)
                converted = phone_image(result.data)
                failed = False
            except Exception:
                failed = True
            allowed = self._has_focus(phone_id, context)
            with self._lock:
                current = self._row(phone_id, session_id)
                if current is not row or row.revision != revision or row.state in TERMINAL:
                    return
                if not allowed:
                    self._preempt(row)
                elif failed:
                    row.state, row.text = 'Error', 'Camera preview unavailable. Press Stop.'
                    row.frame, row.frame_source = None, None
                else:
                    row.frame, row.captured_at = converted, timestamp
                    row.revision += 1
                row.updated_at = self.wall_clock()
        finally:
            fetch_lock.release()

    @staticmethod
    def _response(status=200, body=b'', content_type=MIME, *, refresh=None, present=True):
        headers = {'Cache-Control': 'no-store, no-cache, must-revalidate',
                   'Pragma': 'no-cache', 'Expires': 'Thu, 01 Jan 1970 00:00:00 GMT'}
        if refresh is not None:
            headers['Refresh'] = refresh
        return DisplayResponse(status, body, content_type, headers, present)

    @staticmethod
    def _element(root, name, text):
        ET.SubElement(root, name).text = text

    def _softkey(self, root, name, url, position):
        key = ET.SubElement(root, 'SoftKeyItem')
        for tag, value in (('Name', name), ('URL', url), ('Position', str(position))):
            self._element(key, tag, value)

    @staticmethod
    def _date(value):
        return datetime.fromtimestamp(value, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

    def _render(self, phone_id, row, *, text_view=False):
        # Caller holds lock, so URL revision, timestamp and frame describe one state.
        if row.preempted:
            return self._response(204, present=False)
        ref = self._ref(phone_id, row)
        active = row.state not in TERMINAL and not row.stop_attempted
        image_view = row.frame is not None and not text_view
        root = ET.Element('CiscoIPPhoneImageFile' if image_view else 'CiscoIPPhoneText')
        title = row.state if row.mode == 'voice' or not active else 'Camera preview'
        self._element(root, 'Title', title)
        if row.mode == 'camera' and row.captured_at is not None:
            stale = self.wall_clock() - row.captured_at > max(10, self.refresh_seconds * 2)
            stamp = ('Stale frame: ' if stale else 'Frame: ') + self._date(row.captured_at)
        else:
            stamp = 'Updated: ' + self._date(row.updated_at)
        self._element(root, 'Prompt', stamp)
        if image_view:
            self._element(root, 'LocationX', '-1')
            self._element(root, 'LocationY', '-1')
            self._element(root, 'URL', ref.page_url + '/image?v=' + str(row.revision))
            self._softkey(root, 'Details', ref.page_url + '/text', 2)
        else:
            value = row.text or ('Camera stills; waiting for a frame.' if row.mode == 'camera'
                                 and active else row.state + '.')
            self._element(root, 'Text', safe_text(value + '\n' + stamp, 1200))
            if active and row.frame is not None:
                self._softkey(root, 'Image', ref.page_url, 2)
        if not row.stop_attempted and row.state != 'Stopped':
            self._softkey(root, 'Stop', ref.stop_url, 1)
        self._softkey(root, 'Exit', 'SoftKey:Exit', 4)
        refresh_url = ref.page_url + ('/text' if text_view else '')
        # Expiry never slides. Last responses do not schedule a fetch past expiry.
        refresh = (str(self.refresh_seconds) + '; url=' + refresh_url
                   if active and self.clock() + self.refresh_seconds < row.expires else None)
        return self._response(body=ET.tostring(root, encoding='utf-8', xml_declaration=True),
                              refresh=refresh)

    def stop(self, phone_id, session_id, *, context=None):
        self._authorized(phone_id, 'stop', context)
        with self._lock:
            row = self._row(phone_id, session_id)
            call_stop = not row.stop_attempted
            if call_stop:
                # Claim once before callback I/O; concurrent polls cannot refresh.
                row.stop_attempted = True
                row.stop_pending = True
                self._pending_stops.add(phone_id)
                row.state, row.text = 'Thinking', 'Stop requested; awaiting confirmation.'
                row.frame, row.frame_source = None, None
                row.revision += 1
                row.updated_at = self.wall_clock()
        if call_stop:
            try:
                try:
                    confirmed = self.on_stop(phone_id, session_id, context) is True
                except Exception:
                    confirmed = False
            finally:
                with self._lock:
                    self._pending_stops.discard(phone_id)
            with self._lock:
                self._prune()
                current = self._sessions.get(phone_id)
                if current is row:
                    row.stop_pending = False
                    row.state = 'Stopped' if confirmed else 'Error'
                    row.text = ('Stop confirmed.' if confirmed else
                                'Stop could not be confirmed. Check the voice controller.')
                    row.updated_at = self.wall_clock()
        allowed = self._has_focus(phone_id, context)
        with self._lock:
            row = self._row(phone_id, session_id)
            if not allowed:
                self._preempt(row)
            return self._render(phone_id, row)

    def handle(self, phone_id, relative_path, query='', *, context=None, method='GET'):
        require(method == 'GET', 'method_not_allowed', 405)
        require(isinstance(relative_path, str) and len(relative_path) <= 96 and
                isinstance(query, str) and len(query) <= 64, 'invalid_request')
        match = re.fullmatch(r'/session/([0-9a-f]{32})(?:/(image|text|stop))?', relative_path)
        require(match, 'not_found', 404)
        session_id, route = match.groups()
        try:
            pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=1)
        except ValueError:
            raise DisplayError('invalid_query') from None
        if route == 'image':
            require(len(pairs) == 1 and pairs[0][0] == 'v' and
                    re.fullmatch(r'[1-9][0-9]{0,15}', pairs[0][1]), 'invalid_query')
        else:
            require(not query, 'invalid_query')
        if route == 'stop':
            return self.stop(phone_id, session_id, context=context)
        self._authorized(phone_id, 'read', context)
        allowed = self._has_focus(phone_id, context)
        with self._lock:
            row = self._row(phone_id, session_id)
            if not allowed:
                self._preempt(row)
            if row.preempted:
                return self._response(204, present=False)
        if route != 'image':
            self._refresh_camera(phone_id, session_id, context)
        allowed = self._has_focus(phone_id, context)
        with self._lock:
            row = self._row(phone_id, session_id)
            if not allowed:
                self._preempt(row)
            if row.preempted:
                return self._response(204, present=False)
            if route == 'image':
                require(row.frame is not None and int(pairs[0][1]) == row.revision,
                        'expired_or_unknown_frame', 404)
                return self._response(body=row.frame, content_type='image/png')
            return self._render(phone_id, row, text_view=route == 'text')


class _DisplayHTTPServer(HTTPServer):
    """One accept thread plus a fixed ceiling on active request threads."""
    request_queue_size = 8
    allow_reuse_address = True

    def __init__(self, owner):
        self.owner = owner
        self.guard = threading.Lock()
        self.requests = {}
        self.workers = set()
        self.slots = threading.BoundedSemaphore(owner.max_workers)
        super().__init__((owner.bind_ip, owner.port), _DisplayHTTPHandler)

    @staticmethod
    def close_connection(request):
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            request.close()
        except OSError:
            pass

    def process_request(self, request, client_address):
        if client_address[0] != self.owner.phone_ip:
            # Do not let a forbidden peer occupy a worker while dripping
            # request headers. The small refusal is best-effort and never waits.
            try:
                request.setblocking(False)
                request.send(b'HTTP/1.0 403 Forbidden\r\nConnection: close\r\n'
                             b'Content-Length: 0\r\nCache-Control: no-store\r\n\r\n')
            except OSError:
                pass
            self.close_connection(request)
            return
        if self.owner._closed.is_set() or not self.slots.acquire(blocking=False):
            self.close_connection(request)
            return
        worker = threading.Thread(target=self._run_request, args=(request, client_address),
                                  name='voice-display-request', daemon=True)
        with self.guard:
            self.requests[request] = time.monotonic() + self.owner.request_timeout
            self.workers.add(worker)
        try:
            worker.start()
        except Exception:
            with self.guard:
                self.requests.pop(request, None)
                self.workers.discard(worker)
            self.slots.release()
            self.close_connection(request)

    def _run_request(self, request, client_address):
        try:
            request.settimeout(self.owner.request_timeout)
            self.finish_request(request, client_address)
        except Exception:
            pass  # No request targets, credentials, or callback exceptions in logs.
        finally:
            self.close_connection(request)
            with self.guard:
                self.requests.pop(request, None)
                self.workers.discard(threading.current_thread())
            self.slots.release()

    def service_actions(self):
        # An absolute deadline also limits a client dripping bytes before each
        # socket timeout. Arbitrary Python callback execution cannot be killed.
        with self.guard:
            expired = [request for request, deadline in self.requests.items()
                       if deadline <= time.monotonic()]
        for request in expired:
            self.close_connection(request)
        if self.owner.display is not None:
            self.owner.display.cleanup()

    def close_requests(self):
        with self.guard:
            requests, workers = list(self.requests), list(self.workers)
        for request in requests:
            self.close_connection(request)
        deadline = time.monotonic() + self.owner.request_timeout
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=max(0, deadline - time.monotonic()))

    def handle_error(self, request, client_address):
        pass


class _DisplayHTTPHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    server_version = 'VoiceDisplay'
    sys_version = ''

    def log_message(self, *_):
        pass

    def parse_request(self):
        if not super().parse_request():
            return False
        if self.client_address[0] != self.server.owner.phone_ip:
            self._reply(403)
            return False
        return True

    def send_error(self, code, message=None, explain=None):
        self._reply(code)

    def _reply(self, status, body=b'', content_type='text/plain; charset=utf-8', headers=None):
        self.close_connection = True
        self.send_response(status)
        self.send_header('Connection', 'close')
        if status != 204:
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
        for key, value in (headers or {'Cache-Control': 'no-store'}).items():
            if key in ('Cache-Control', 'Pragma', 'Expires', 'Refresh'):
                self.send_header(key, value)
        self.end_headers()
        if body and status != 204:
            self.wfile.write(body)

    def _dispatch(self):
        owner = self.server.owner
        try:
            require(self.client_address[0] == owner.phone_ip, 'forbidden', 403)
            require(not owner._closed.is_set(), 'display_closed', 503)
            require(self.command == 'GET', 'method_not_allowed', 405)
            require(len(self.path) <= 1024 and len(self.headers) <= 32 and
                    sum(len(key) + len(value) for key, value in self.headers.items()) <= 8192,
                    'invalid_request')
            lengths = self.headers.get_all('Content-Length', [])
            require(not self.headers.get_all('Transfer-Encoding', []) and
                    not self.headers.get_all('Expect', []) and
                    (not lengths or lengths == ['0']), 'invalid_request')
            target = urlsplit(self.path)
            require(not target.scheme and not target.netloc and not target.fragment and
                    target.path.startswith('/voice/'), 'invalid_request')
            result = owner.display.handle(owner.phone_id, target.path[len('/voice'):],
                                          target.query, context=owner._phone_context)
            self._reply(result.status, result.body, result.content_type, result.headers)
        except DisplayError as error:
            self._reply(error.status)
        except ValueError:
            self._reply(400)
        except (OSError, TimeoutError):
            self.close_connection = True
        except Exception:
            self._reply(503)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _dispatch


class VoiceDisplayServer:
    """Temporary source-restricted HTTP adapter for one foreground controller.

    ``focus()`` returns the current focus string; ``on_stop()`` synchronously
    confirms cancellation with exactly True. Both are zero-argument callbacks
    and must bound their own work. No phone push or media command is sent here.
    """
    def __init__(self, bind_ip, port, phone_id, phone_ip, *, focus, on_stop,
                 refresh_seconds=3, ttl_seconds=120, max_workers=4, request_timeout=3.0,
                 clock=time.monotonic, wall_clock=time.time):
        require(isinstance(bind_ip, str) and isinstance(phone_ip, str),
                'invalid_display_address')
        try:
            bind, peer = ipaddress.IPv4Address(bind_ip), ipaddress.IPv4Address(phone_ip)
        except (ValueError, TypeError, ipaddress.AddressValueError):
            raise DisplayError('invalid_display_address') from None
        require(not any(ip.is_unspecified or ip.is_multicast or int(ip) == 0xffffffff
                        for ip in (bind, peer)), 'invalid_display_address')
        require(type(port) is int and 0 <= port <= 65535, 'invalid_display_port')
        require(isinstance(phone_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', phone_id),
                'invalid_phone_id')
        require(callable(focus) and callable(on_stop), 'callbacks_required')
        require(type(max_workers) is int and 1 <= max_workers <= 8, 'invalid_worker_limit')
        require(type(request_timeout) in (int, float) and math.isfinite(request_timeout) and
                0.1 <= request_timeout <= 30, 'invalid_request_timeout')
        require(type(refresh_seconds) is int and 2 <= refresh_seconds <= 5,
                'invalid_refresh_seconds')
        require(type(ttl_seconds) is int and 5 <= ttl_seconds <= 360, 'invalid_ttl_seconds')
        require(callable(clock) and callable(wall_clock), 'callbacks_required')
        self.bind_ip, self.phone_ip, self.phone_id = str(bind), str(peer), phone_id
        self.port, self.focus, self.on_stop = port, focus, on_stop
        self.max_workers, self.request_timeout = max_workers, request_timeout
        self.refresh_seconds, self.ttl_seconds = refresh_seconds, ttl_seconds
        self.clock, self.wall_clock = clock, wall_clock
        self.display, self.session_ref, self.base_url = None, None, None
        self._server, self._thread = None, None
        self._lifecycle_lock, self._status_lock = threading.Lock(), threading.Lock()
        self._closed = threading.Event()
        self._control_context, self._phone_context = object(), object()

    def _authorize(self, phone_id, action, context):
        if self._closed.is_set() or phone_id != self.phone_id:
            return False
        if context is self._control_context:
            return action in ('start', 'update', 'read')
        return context is self._phone_context and action in ('read', 'stop')

    def start(self):
        with self._lifecycle_lock:
            require(not self._closed.is_set(), 'display_closed', 409)
            if self._server is not None:
                return self
            server = _DisplayHTTPServer(self)
            try:
                self.port = server.server_port
                self.base_url = f'http://{self.bind_ip}:{self.port}/voice'
                self.display = PhoneVoiceDisplay(
                    {self.phone_id: self.base_url}, authorize=self._authorize,
                    focus=lambda *_: self.focus(), on_stop=lambda *_: self.on_stop(),
                    refresh_seconds=self.refresh_seconds, ttl_seconds=self.ttl_seconds,
                    max_sessions=1, clock=self.clock, wall_clock=self.wall_clock)
                self._thread = threading.Thread(target=server.serve_forever,
                                                kwargs={'poll_interval': 0.05},
                                                name='voice-display-listener', daemon=True)
                self._server = server
                self._thread.start()
            except Exception:
                self._server = None
                server.server_close()
                raise
            return self

    @staticmethod
    def _state(state):
        require(isinstance(state, str) and state.lower() in
                ('listening', 'thinking', 'speaking', 'stopped', 'error'), 'invalid_display_state')
        return state.title()

    def _active(self):
        require(self._server is not None and not self._closed.is_set(), 'display_not_running', 409)
        return self.display

    def start_status(self, state, text=''):
        with self._status_lock:
            display = self._active()
            ref = display.start(self.phone_id, state=self._state(state), text=text,
                                context=self._control_context)
            with self._lifecycle_lock:
                if self._closed.is_set():
                    with display._lock:
                        display._sessions.clear()
                    raise DisplayError('display_closed', 409)
                self.session_ref = ref
            return ref

    def update_status(self, state, text=''):
        with self._status_lock:
            with self._lifecycle_lock:
                display = self._active()
                ref = self.session_ref
                require(ref is not None, 'display_not_started', 409)
            return display.update(self.phone_id, ref.session_id,
                                  state=self._state(state), text=text, context=self._control_context)

    def close(self):
        with self._lifecycle_lock:
            if self._closed.is_set():
                return
            self._closed.set()
            server, thread = self._server, self._thread
            self.session_ref = None
        if server is not None:
            server.shutdown()
            server.server_close()
            server.close_requests()
            if thread is not threading.current_thread():
                thread.join(timeout=self.request_timeout)
        if self.display is not None:
            with self.display._lock:
                self.display._sessions.clear()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()
