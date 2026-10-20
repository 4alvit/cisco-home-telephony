"""Opt-in, poll-driven HA camera stills for existing Cisco display sessions.

No listener, network client, phone command or worker starts on import. The host
provides the existing allowlisted HA adapter and a fresh physical-phone/voice
observer. HTTP polling drives bounded, serialized acquisition; images stay in
the display's memory. A proxy acquisition time is never a camera capture time.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import importlib.util
import math
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET


def _display_module():
    name = __name__ + '_display'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).with_name('phone_voice_display.py'))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            del sys.modules[name]
            raise
    return sys.modules[name]


_display = _display_module()
DisplayResponse = _display.DisplayResponse
DisplayError = _display.DisplayError
SessionRef = _display.SessionRef
require = _display.require


@dataclass(frozen=True)
class PhoneObservation:
    """Monotonic observation time, exact physical lines, and voice ownership."""
    observed_at: float
    lines: dict
    voice: str


@dataclass
class _Preview:
    ref: SessionRef
    camera_id: str
    label: str
    line: str
    expires: float
    next_fetch: float
    state: str = 'active'
    acquired_at: float = None
    acquired_monotonic: float = None
    message: str = 'Waiting for a camera still.'


class CameraPreviewController:
    """Compose HA.camera with PhoneVoiceDisplay; expose only through a trusted host."""

    def __init__(self, backend, phones, base_urls, *, authorize, observe,
                 refresh_seconds=3, ttl_seconds=60, stale_seconds=10,
                 clock=time.monotonic, wall_clock=time.time):
        require(isinstance(phones, dict) and 1 <= len(phones) <= 64,
                'invalid_camera_phones')
        seen = set()
        self.phones = {}
        for phone, lines in phones.items():
            require(isinstance(phone, str) and re.fullmatch(r'2[0-9]{2}', phone)
                    and isinstance(lines, (list, tuple)) and 1 <= len(lines) <= 2
                    and all(isinstance(line, str) and re.fullmatch(r'2[0-9]{2}', line)
                            for line in lines)
                    and phone in lines and len(set(lines)) == len(lines)
                    and not seen.intersection(lines), 'invalid_camera_phones')
            seen.update(lines)
            self.phones[phone] = tuple(lines)
        require(isinstance(base_urls, dict) and set(base_urls) == set(phones),
                'invalid_camera_phones')
        require(type(refresh_seconds) is int and 2 <= refresh_seconds <= 5,
                'invalid_refresh_seconds')
        require(type(ttl_seconds) is int and 5 <= ttl_seconds <= 120,
                'invalid_ttl_seconds')
        require(type(stale_seconds) is int and refresh_seconds <= stale_seconds <= 30,
                'invalid_stale_seconds')
        require(all(callable(fn) for fn in (authorize, observe, clock, wall_clock))
                and callable(getattr(backend, 'cameras', None))
                and callable(getattr(backend, 'camera', None)), 'callbacks_required')
        self.backend, self.authorize, self.observe = backend, authorize, observe
        self.clock, self.wall_clock = clock, wall_clock
        self.refresh_seconds, self.ttl_seconds = refresh_seconds, ttl_seconds
        self.stale_seconds = stale_seconds
        self._lock = threading.RLock()
        self._fetch_locks = {phone: threading.Lock() for phone in phones}
        self._observation_locks = {phone: threading.Lock() for phone in phones}
        self._rows = {}
        self._closed = False
        self._clear_context = object()
        self._display = _display.PhoneVoiceDisplay(
            base_urls, authorize=self._display_authorized, focus=self._focus, on_stop=self._on_stop,
            refresh_seconds=refresh_seconds, ttl_seconds=ttl_seconds,
            max_sessions=len(phones), clock=clock, wall_clock=wall_clock)

    def _display_authorized(self, phone, action, context):
        # A private cleanup capability may only invalidate our own display.
        # It cannot start/read a session or invoke a host control callback.
        if context is self._clear_context:
            return phone in self.phones and action == 'update'
        return self.authorize(phone, action, context)

    def _authorized(self, phone, action, context):
        require(isinstance(phone, str) and phone in self.phones, 'forbidden', 403)
        try:
            allowed = self.authorize(phone, action, context) is True
        except Exception:
            allowed = False
        require(allowed, 'forbidden', 403)
        with self._lock:
            require(not self._closed, 'camera_controller_closed', 503)
            return self._display

    def _focus(self, phone, context):
        if context is self._clear_context:
            return 'unknown'  # Public display update preempts and clears its frame.
        with self._lock:
            if self._closed:
                return 'unknown'
            row = self._rows.get(phone)
            wait = float(self.refresh_seconds)
            if row is not None:
                wait = min(wait, max(0.0, row.expires - self.clock()))
        try:
            # Concurrent image/page requests must not mistake our own brief
            # observer reservation for a competing voice owner.
            lock = self._observation_locks[phone]
            if not lock.acquire(timeout=wait):
                return 'unknown'
            try:
                with self._lock:
                    if self._closed:
                        return 'unknown'
                value = self.observe(phone, context)
            finally:
                lock.release()
            if not isinstance(value, PhoneObservation):
                return 'unknown'
            age = self.clock() - value.observed_at
            valid = (type(value.observed_at) in (int, float)
                     and math.isfinite(value.observed_at)
                     and 0 <= age <= self.refresh_seconds
                     and isinstance(value.lines, dict)
                     and set(value.lines) == set(self.phones[phone])
                     and all(state == 'idle' for state in value.lines.values())
                     and value.voice == 'idle')
            return 'idle' if valid else 'unknown'
        except Exception:
            return 'unknown'

    def _on_stop(self, phone, session, _context):
        with self._lock:
            row = self._rows.get(phone)
            if row is not None and row.ref.session_id == session:
                row.state, row.message = 'stopped', 'Preview stopped.'
                row.acquired_at = row.acquired_monotonic = None
        return True  # Local preview only; never stop a call or voice session.

    def cleanup(self):
        with self._lock:
            now = self.clock()
            expired = [phone for phone, row in self._rows.items() if row.expires <= now]
            for phone in expired:
                del self._rows[phone]
            if self._display is not None:
                self._display.cleanup()
            return len(expired)

    def close(self):
        """Revoke all URLs now. In-flight bounded reads finish and are discarded."""
        with self._lock:
            self._closed = True
            self._rows.clear()
            self._display = None

    def stats(self):
        self.cleanup()
        with self._lock:
            if self._closed:
                return {'sessions': 0, 'frames': 0, 'frame_bytes': 0}
            return self._display.stats()

    def _row(self, phone, session):
        self.cleanup()
        with self._lock:
            row = self._rows.get(phone)
            require(not self._closed and row is not None
                    and row.ref.session_id == session, 'expired_or_unknown_session', 404)
            return row

    def start(self, phone, camera_id, *, line=None, context=None):
        display = self._authorized(phone, 'start', context)
        line = phone if line is None else line
        require(line in self.phones[phone], 'invalid_line')
        require(isinstance(camera_id, str) and
                re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', camera_id), 'unknown_camera', 404)
        lock = self._fetch_locks[phone]
        require(lock.acquire(blocking=False), 'camera_fetch_busy', 409)
        try:
            if self._focus(phone, context) != 'idle':
                with self._lock:
                    prior = self._rows.get(phone)
                if prior is not None:
                    self._finish(display, phone, prior, 'yielded',
                                 'Preview yielded to phone focus.', context)
                raise DisplayError('phone_focus_unavailable', 409)
            try:
                cameras = self.backend.cameras(phone)
                require(isinstance(cameras, list) and len(cameras) <= 8,
                        'invalid_camera_catalog', 503)
                found = [item for item in cameras if isinstance(item, dict)
                         and item.get('id') == camera_id]
                require(len(found) == 1, 'unknown_camera', 404)
                label = _display.safe_text(found[0]['label'], 48)
            except DisplayError:
                raise
            except Exception:
                raise DisplayError('camera_unavailable', 503) from None
            self.cleanup()
            try:
                ref = display.start(phone, mode='camera', state='Thinking',
                                    text='Waiting for a camera still.', context=context)
            except DisplayError as error:
                if error.reason == 'phone_focus_unavailable':
                    with self._lock:
                        prior = self._rows.get(phone)
                    if prior is not None:
                        self._finish(display, phone, prior, 'yielded',
                                     'Preview yielded to phone focus.', context)
                raise
            with self._lock:
                require(not self._closed, 'camera_controller_closed', 503)
                self._rows[phone] = _Preview(
                    ref, camera_id, label, line, self.clock() + self.ttl_seconds,
                    self.clock())
            return ref
        finally:
            lock.release()

    @staticmethod
    def _empty():
        return DisplayResponse(204, b'', _display.MIME,
                               {'Cache-Control': 'no-store'}, False)

    def _finish(self, display, phone, row, state, message, context):
        with self._lock:
            if self._rows.get(phone) is not row or row.state != 'active':
                return
            row.state, row.message = state, message
            row.acquired_at = row.acquired_monotonic = None
        try:
            display.update(phone, row.ref.session_id,
                           state='Stopped' if state == 'yielded' else 'Error',
                           text=message, context=self._clear_context)
        except DisplayError:
            pass  # Expiry/focus loss already revokes display access.

    def _refresh(self, display, phone, row, context):
        lock = self._fetch_locks[phone]
        if not lock.acquire(blocking=False):
            return
        try:
            with self._lock:
                if (self._closed or self._rows.get(phone) is not row or
                        row.state != 'active' or self.clock() < row.next_fetch
                        or self.clock() >= row.expires):
                    return
            if self._focus(phone, context) != 'idle':
                self._finish(display, phone, row, 'yielded', 'Preview yielded to phone focus.', context)
                return
            with self._lock:
                # A bounded observer may still consume the remaining lifetime.
                # Do not start camera I/O after expiry or after a concurrent Stop.
                if (self._closed or self._rows.get(phone) is not row
                        or row.state != 'active' or self.clock() >= row.expires):
                    return
            try:
                # The existing HA adapter accepts configured aliases only and
                # bounds URL policy, response size, decoding and request time.
                image = self.backend.camera(phone, row.camera_id)
                acquired, acquired_mono = self.wall_clock(), self.clock()
                require(type(image) is bytes, 'invalid_camera_image')
            except Exception:
                self._finish(display, phone, row, 'error',
                             'Camera preview unavailable. Start a new preview to retry.', context)
                return
            if self._focus(phone, context) != 'idle':
                self._finish(display, phone, row, 'yielded', 'Preview yielded to phone focus.', context)
                return
            with self._lock:
                if (self._closed or self._rows.get(phone) is not row or
                        row.state != 'active' or self.clock() >= row.expires):
                    return
            try:
                # Display's timestamp field is a rendering primitive. Public
                # camera captions below explicitly name its acquisition origin.
                display.update(phone, row.ref.session_id, image=image,
                               captured_at=acquired, text='Camera capture time unavailable.',
                               context=context)
            except DisplayError:
                self._finish(display, phone, row, 'error',
                             'Camera preview unavailable. Start a new preview to retry.', context)
                return
            with self._lock:
                if not self._closed and self._rows.get(phone) is row and row.state == 'active':
                    row.acquired_at, row.acquired_monotonic = acquired, acquired_mono
                    # Schedule from completion: a slow source never queues frames.
                    row.next_fetch = self.clock() + self.refresh_seconds
        finally:
            lock.release()

    def _caption(self, response, row):
        if response.status != 200 or response.content_type != _display.MIME:
            return response
        root = ET.fromstring(response.body)
        stamp = ('No current still.' if row.acquired_at is None else
                 'Acquired: ' + datetime.fromtimestamp(row.acquired_at, timezone.utc)
                 .strftime('%Y-%m-%d %H:%M:%S UTC'))
        root.find('Title').text = ('Camera preview' if row.state == 'active'
                                   else 'Preview stopped' if row.state == 'stopped'
                                   else 'Preview unavailable')
        root.find('Prompt').text = stamp + '; capture time unknown'
        text = root.find('Text')
        if text is not None:
            text.text = (row.label + '\n' + row.message + '\n' + stamp
                         + '\nCamera capture time unavailable.'
                         '\nAcquisition does not prove a new camera frame.'
                         '\nStop ends this preview only. Press Exit to leave.')
        return DisplayResponse(response.status, ET.tostring(root, encoding='utf-8',
                               xml_declaration=True), response.content_type,
                               response.headers, response.allow_present)

    def _error_page(self, row):
        root = ET.Element('CiscoIPPhoneText')
        title = 'Preview stopped' if row.state == 'stopped' else 'Preview unavailable'
        for name, text in (('Title', title), ('Prompt', 'No current still.'),
                           ('Text', row.message + '\nPress Exit and start a new preview.')):
            ET.SubElement(root, name).text = text
        key = ET.SubElement(root, 'SoftKeyItem')
        for name, text in (('Name', 'Exit'), ('URL', 'SoftKey:Exit'), ('Position', '4')):
            ET.SubElement(key, name).text = text
        return DisplayResponse(200, ET.tostring(root, encoding='utf-8', xml_declaration=True),
                               _display.MIME, {'Cache-Control': 'no-store'}, True)

    def handle(self, phone, relative_path, query='', *, context=None, method='GET'):
        display = self._authorized(phone, 'read', context)
        require(method == 'GET', 'method_not_allowed', 405)
        require(isinstance(relative_path, str) and len(relative_path) <= 96
                and isinstance(query, str) and len(query) <= 64, 'invalid_request')
        match = re.fullmatch(r'/session/([0-9a-f]{32})(?:/(image|text|stop))?', relative_path)
        require(match, 'not_found', 404)
        session, route = match.groups()
        # Validate before invoking any backend, including on unknown queries.
        if route == 'image':
            require(re.fullmatch(r'v=[1-9][0-9]{0,15}', query), 'invalid_query')
        else:
            require(not query, 'invalid_query')
        row = self._row(phone, session)
        if route == 'stop':
            response = display.stop(phone, session, context=context)
            return self._caption(response, row)
        if row.state == 'yielded':
            return self._empty()
        if self._focus(phone, context) != 'idle':
            self._finish(display, phone, row, 'yielded', 'Preview yielded to phone focus.', context)
            return self._empty()
        with self._lock:
            stale = (row.state == 'active' and row.acquired_monotonic is not None
                     and self.clock() - row.acquired_monotonic >= self.stale_seconds)
        if stale:
            self._finish(display, phone, row, 'error',
                         'The last acquired still expired. Start a new preview to retry.', context)
        elif route != 'image' and row.state == 'active':
            self._refresh(display, phone, row, context)
        # A completed slow fetch may have exhausted the session lifetime.
        self._row(phone, session)
        if row.state == 'yielded' or self._focus(phone, context) != 'idle':
            self._finish(display, phone, row, 'yielded', 'Preview yielded to phone focus.', context)
            return self._empty()
        if row.state == 'error':
            self._authorized(phone, 'read', context)
            require(route != 'image', 'expired_or_unknown_frame', 404)
            return self._error_page(row)
        response = display.handle(phone, relative_path, query, context=context, method=method)
        if not response.allow_present:
            self._finish(display, phone, row, 'yielded', 'Preview yielded to phone focus.', context)
            return self._empty()
        with self._lock:
            if self._closed or self._rows.get(phone) is not row:
                return self._empty()
            if row.state == 'yielded':
                return self._empty()
            if row.state in ('stopped', 'error'):
                require(route != 'image', 'expired_or_unknown_frame', 404)
                return self._error_page(row)
            stale = (row.state == 'active' and row.acquired_monotonic is not None
                     and self.clock() - row.acquired_monotonic >= self.stale_seconds)
            if not stale:
                return self._caption(response, row)
        # Focus observations themselves may be slow. Recheck age after the
        # final display read, not just before HA acquisition.
        self._finish(display, phone, row, 'error',
                     'The last acquired still expired. Start a new preview to retry.', context)
        require(route != 'image', 'expired_or_unknown_frame', 404)
        return self._error_page(row)


def main(argv=None):
    """Offline executable example: synthetic still and XML; no network/listener."""
    import argparse
    import io
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo-directory', type=Path, required=True,
                        help='Create a new directory containing synthetic preview XML/PNG')
    args = parser.parse_args(argv)
    from PIL import Image, ImageDraw
    image = Image.new('RGB', (640, 360), '#17324d')
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 65, 560, 285), outline='#57dfbf', width=8)
    draw.text((220, 170), 'SYNTHETIC CAMERA', fill='white')
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')

    class DemoBackend:
        def cameras(self, _phone):
            return [{'id': 'demo', 'label': 'Synthetic camera'}]

        def camera(self, _phone, _key):
            return buffer.getvalue()

    controller = CameraPreviewController(
        DemoBackend(), {'201': ('201', '202')},
        {'201': 'http://192.0.2.10:18080/apps/demo/camera-preview'},
        authorize=lambda *_: True,
        observe=lambda *_: PhoneObservation(time.monotonic(),
                                             {'201': 'idle', '202': 'idle'}, 'idle'))
    ref = controller.start('201', 'demo')
    response = controller.handle('201', '/session/' + ref.session_id)
    url = urlsplit(ET.fromstring(response.body).findtext('URL'))
    image_response = controller.handle(
        '201', '/session/' + ref.session_id + '/image', url.query)
    args.demo_directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, data in (('preview.xml', response.body), ('still.png', image_response.body),
                       ('result.json', (json.dumps({
                           'mode': 'offline synthetic example', 'network_started': False,
                           'camera_capture_time': None,
                           'refresh_seconds': controller.refresh_seconds,
                           'ttl_seconds': controller.ttl_seconds,
                           'stats': controller.stats()}, indent=2) + '\n').encode())):
        path = args.demo_directory / name
        path.write_bytes(data)
        path.chmod(0o600)
    controller.close()
    print('Offline example written. No listener, camera connection or phone command was started.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
