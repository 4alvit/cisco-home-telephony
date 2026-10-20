"""Offline contracts for the bounded Cisco voice and camera display prototype."""
import importlib.util
import http.client
import io
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

try:
    from PIL import Image, PngImagePlugin
except ImportError:
    Image = PngImagePlugin = None


spec = importlib.util.spec_from_file_location(
    'phone_voice_display', Path(__file__).resolve().parents[1] /
    'containers/asterisk/phone_voice_display.py')
display = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = display
spec.loader.exec_module(display)


def picture(color='purple', size=(640, 480), mode='RGB', format='PNG', **options):
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, format, **options)
    return buffer.getvalue()


class Clock:
    def __init__(self):
        self.now = 100.0
        self.wall = 1700000000.0

    def advance(self, seconds):
        self.now += seconds
        self.wall += seconds


class DisplayControlTests(unittest.TestCase):
    """Transport and lifecycle controls also run when Pillow is unavailable."""

    def setUp(self):
        self.clock = Clock()
        self.allowed, self.focus = True, 'idle'
        self.stops = []
        self.app = self.make_app()

    def make_app(self, **overrides):
        options = dict(authorize=lambda *_: self.allowed,
                       focus=lambda *_: self.focus,
                       on_stop=lambda *args: self.stops.append(args) or True,
                       clock=lambda: self.clock.now, wall_clock=lambda: self.clock.wall)
        options.update(overrides)
        return display.PhoneVoiceDisplay({'demo': 'http://192.0.2.10/voice'}, **options)

    def page(self, ref):
        return self.app.handle('demo', '/session/' + ref.session_id)

    def test_constructor_rejects_unbounded_and_noninteger_limits(self):
        for field, values in [('refresh_seconds', (True, 1, 6, 3.0)),
                              ('ttl_seconds', (True, 4, 121, 30.0)),
                              ('max_sessions', (True, 0, 65, 2.0))]:
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(display.DisplayError):
                    self.make_app(**{field: value})

    def test_start_read_update_and_stop_require_exact_true_authorization(self):
        ref = self.app.start('demo', mode='camera')
        for allowed in (False, None, 1, 'yes'):
            self.allowed = allowed
            for operation in (lambda: self.app.start('demo', mode='camera'),
                              lambda: self.page(ref),
                              lambda: self.app.update('demo', ref.session_id, text='Denied'),
                              lambda: self.app.stop('demo', ref.session_id)):
                with self.subTest(allowed=allowed), self.assertRaises(display.DisplayError) as caught:
                    operation()
                self.assertEqual(caught.exception.status, 403)
        self.assertFalse(self.stops)
        self.allowed = True
        self.assertEqual(self.page(ref).status, 200)

    def test_start_denied_by_call_preempts_existing_session_without_auto_resume(self):
        ref = self.app.start('demo', mode='camera')
        self.focus = 'call'
        with self.assertRaises(display.DisplayError) as caught:
            self.app.start('demo', mode='camera')
        self.assertEqual(caught.exception.status, 409)
        self.focus = 'idle'
        response = self.page(ref)
        self.assertEqual(response.status, 204)
        self.assertFalse(response.body)
        self.assertFalse(response.allow_present)
        self.assertFalse(self.app.should_present('demo', ref.session_id))
        with self.assertRaises(display.DisplayError):
            self.app.update('demo', ref.session_id, state='Listening')
        explicit = self.app.start('demo', mode='camera')
        self.assertNotEqual(explicit.session_id, ref.session_id)
        self.assertTrue(self.app.should_present('demo', explicit.session_id))

    def test_ttl_expiry_remains_absolute_without_images_or_wall_clock_dependency(self):
        self.app = self.make_app(ttl_seconds=5)
        ref = self.app.start('demo', mode='camera')
        self.clock.advance(4)
        self.clock.wall -= 3600
        self.assertEqual(self.page(ref).status, 200)
        self.app.update('demo', ref.session_id, text='Still waiting')
        self.clock.advance(1)
        self.assertEqual(self.app.cleanup(), 1)
        self.assertEqual(self.app.stats(), {'sessions': 0, 'frames': 0, 'frame_bytes': 0})
        with self.assertRaises(display.DisplayError):
            self.page(ref)


@unittest.skipUnless(Image, 'Pillow is required for the complete display validation suite')
class PhoneImageTests(unittest.TestCase):
    def test_every_aspect_ratio_is_exact_rgb_phone_size(self):
        for size, mode, color in [((640, 480), 'RGB', 'purple'),
                                  ((8, 90), 'L', 120),
                                  ((1200, 40), 'RGBA', (20, 40, 60, 80)),
                                  ((1, 1), 'P', 0)]:
            with self.subTest(size=size, mode=mode):
                data = display.phone_image(picture(color, size, mode))
                with Image.open(io.BytesIO(data)) as result:
                    result.load()
                    self.assertEqual(result.size, (298, 156))
                    self.assertEqual(result.mode, 'RGB')
                    self.assertEqual(result.format, 'PNG')

    def test_letterboxing_preserves_content_without_stretching(self):
        data = display.phone_image(picture('red', (100, 100)))
        with Image.open(io.BytesIO(data)) as result:
            self.assertEqual(result.getpixel((149, 78)), (255, 0, 0))
            self.assertNotEqual(result.getpixel((0, 78)), (255, 0, 0))
            self.assertEqual(result.getpixel((0, 78)), result.getpixel((297, 78)))

    def test_png_text_and_exif_are_removed(self):
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text('Comment', 'synthetic-camera-secret')
        metadata.add_text('Description', 'synthetic-camera-location')
        exif = Image.Exif()
        exif[270] = 'synthetic-private-description'
        source = picture(pnginfo=metadata, exif=exif)
        data = display.phone_image(source)
        with Image.open(io.BytesIO(data)) as result:
            self.assertFalse(result.getexif())
            self.assertNotIn('Comment', result.info)
            self.assertNotIn('Description', result.info)
        self.assertNotIn(b'synthetic-', data)

    def test_jpeg_exif_orientation_is_applied_before_stripping(self):
        source = Image.new('RGB', (80, 120), 'red')
        source.paste('blue', (40, 0, 80, 120))
        exif = Image.Exif()
        exif[274] = 6  # A clockwise quarter-turn makes the red half the top.
        exif[270] = 'synthetic-private-description'
        buffer = io.BytesIO()
        source.save(buffer, 'JPEG', exif=exif, quality=95)
        with Image.open(io.BytesIO(display.phone_image(buffer.getvalue()))) as result:
            red = result.getpixel((149, 20))
            blue = result.getpixel((149, 135))
            self.assertGreater(red[0], 220)
            self.assertLess(red[2], 25)
            self.assertGreater(blue[2], 220)
            self.assertLess(blue[0], 25)
            self.assertFalse(result.getexif())

    def test_bad_unsupported_oversized_and_truncated_images_fail_safely(self):
        cases = [b'', b'<html>synthetic-private-backend-error</html>',
                 picture(format='GIF'), picture(format='BMP'),
                 b'x' * (2 * 1024 * 1024 + 1),
                 picture(size=(4001, 4000), mode='1', color=1),
                 picture()[:40]]
        for value in cases:
            with self.subTest(length=len(value)), self.assertRaises(display.DisplayError) as caught:
                display.phone_image(value)
            self.assertNotIn('synthetic-private', str(caught.exception))


@unittest.skipUnless(Image, 'Pillow is required for the complete display validation suite')
class DisplayFixture(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.context = {'principal': 'synthetic-operator'}
        self.bases = {
            'demo-1': 'http://192.0.2.10:18080/voice/demo-1',
            'demo-2': 'http://192.0.2.10:18081/voice/demo-2',
            'demo-3': 'http://192.0.2.10:18082/voice/demo-3',
        }
        self.authorization = []
        self.allowed = True
        self.focus_state = 'idle'
        self.stops = []
        self.stop_result = True
        self.app = self.make_app()

    def authorize(self, phone, action, context):
        self.authorization.append((phone, action, context))
        return self.allowed

    def focus(self, phone, context):
        return self.focus_state

    def on_stop(self, phone, session_id, context):
        self.stops.append((phone, session_id, context))
        return self.stop_result

    def make_app(self, **options):
        values = dict(authorize=self.authorize, focus=self.focus,
                      on_stop=self.on_stop, clock=lambda: self.clock.now,
                      wall_clock=lambda: self.clock.wall)
        values.update(options)
        return display.PhoneVoiceDisplay(self.bases, **values)

    def start(self, phone='demo-1', **options):
        return self.app.start(phone, context=self.context, **options)

    def request(self, url, phone='demo-1', method='GET', app=None):
        parsed = urlsplit(url)
        base_path = urlsplit(self.bases[phone]).path
        # Phone binding is a caller input; retain the original relative path
        # when deliberately trying a URL on a second phone's listener.
        for known in self.bases.values():
            prefix = urlsplit(known).path
            if parsed.path.startswith(prefix + '/'):
                base_path = prefix
                break
        relative = parsed.path[len(base_path):]
        return (app or self.app).handle(phone, relative, parsed.query,
                                        context=self.context, method=method)

    def xml(self, response):
        self.assertEqual(response.status, 200)
        self.assertIn('xml', response.content_type)
        return ET.fromstring(response.body)

    def headers(self, response):
        return {name.lower(): value for name, value in response.headers.items()}

    def image_url(self, response):
        root = self.xml(response)
        self.assertEqual(root.tag, 'CiscoIPPhoneImageFile')
        return root.find('URL').text

    def assert_no_refresh(self, response):
        self.assertNotIn('refresh', self.headers(response))


class VoiceDisplayTests(DisplayFixture):
    def test_all_five_states_have_english_xml_and_terminal_pages_do_not_refresh(self):
        for state in ('Listening', 'Thinking', 'Speaking', 'Stopped', 'Error'):
            with self.subTest(state=state):
                self.app = self.make_app()
                ref = self.start(state=state)
                response = self.request(ref.page_url)
                root = self.xml(response)
                self.assertIn(state, ' '.join(root.itertext()))
                if state in ('Stopped', 'Error'):
                    self.assert_no_refresh(response)
                else:
                    self.assertEqual(root.tag, 'CiscoIPPhoneImageFile')
                    self.assertTrue(response.allow_present)
                    self.assertIn('refresh', self.headers(response))
                self.assertNotIn(b'CiscoIPPhoneExecute', response.body)

    def test_rich_text_and_urls_round_trip_xml_metacharacters(self):
        text = 'A < B & C > D; "quoted" and apostrophe\'s. ' * 20
        ref = self.start(text=text)
        response = self.request(ref.page_url)
        root = self.xml(response)
        urls = [element.text for element in root.iter('URL')]
        self.assertIn(ref.stop_url, urls)
        full = self.request(ref.page_url + '/text')
        full_root = self.xml(full)
        self.assertIn(text, ''.join(full_root.itertext()))
        self.assertIn(b'&lt;', full.body)
        self.assertIn(b'&amp;', full.body)
        self.assertNotIn(b'< B', full.body)

    def test_illegal_xml_characters_are_removed_and_answers_are_bounded(self):
        text = 'A\x00\x01\ud800\ufffe\uffffB & C\n' + 'x' * 5000
        ref = self.start(text=text)
        response = self.request(ref.page_url + '/text')
        root = self.xml(response)
        answer = root.find('Text').text
        self.assertTrue(answer.startswith('AB & C\n'))
        self.assertLessEqual(len(answer), 1200)
        self.assertEqual(display.safe_text('\x00' * 4000 + 'unscanned suffix', 1000), '')

    def test_invalid_states_and_modes_do_not_create_or_mutate_session(self):
        for values in ({'state': 'listening'}, {'state': 'Idle'}, {'state': None},
                       {'mode': 'video'}, {'mode': None}):
            with self.subTest(values=values), self.assertRaises(display.DisplayError):
                self.start(**values)
            self.assertEqual(self.app.stats()['sessions'], 0)
        ref = self.start()
        with self.assertRaises(display.DisplayError):
            self.app.update('demo-1', ref.session_id, state='idle', context=self.context)
        self.assertIn('Listening', ''.join(self.xml(self.request(ref.page_url)).itertext()))

    def test_active_avatar_is_real_phone_png_and_never_cacheable(self):
        ref = self.start()
        page = self.request(ref.page_url)
        image = self.request(self.image_url(page))
        self.assertEqual(image.content_type, 'image/png')
        self.assertEqual(image.status, 200)
        with Image.open(io.BytesIO(image.body)) as result:
            self.assertEqual(result.size, (298, 156))
            self.assertEqual(result.mode, 'RGB')
        for response in (page, image):
            self.assertIn('no-store', self.headers(response).get('cache-control', ''))

    def test_only_exact_true_authorizes_and_every_route_rechecks_identity(self):
        ref = self.start()
        image_url = self.image_url(self.request(ref.page_url))
        for allowed in (False, None, 1, 'yes'):
            self.allowed = allowed
            for operation in (lambda: self.start(phone='demo-2'),
                              lambda: self.app.update('demo-1', ref.session_id, state='Thinking', context=self.context),
                              lambda: self.request(ref.page_url),
                              lambda: self.request(ref.page_url + '/text'),
                              lambda: self.request(image_url),
                              lambda: self.request(ref.stop_url)):
                with self.subTest(allowed=allowed), self.assertRaises(display.DisplayError) as caught:
                    operation()
                self.assertEqual(caught.exception.status, 403)
        self.assertFalse(self.stops)
        self.assertEqual({call[1] for call in self.authorization}, {'start', 'update', 'read', 'stop'})
        self.assertTrue(all(call[2] is self.context for call in self.authorization))

    def test_authorization_exceptions_do_not_leak_or_mutate(self):
        def fail(*_):
            raise RuntimeError('synthetic-private-auth-details')
        self.app = self.make_app(authorize=fail)
        with self.assertRaises(display.DisplayError) as caught:
            self.start()
        self.assertEqual(caught.exception.status, 403)
        self.assertNotIn('synthetic-private', str(caught.exception))
        self.assertEqual(self.app.stats()['sessions'], 0)

    def test_page_frame_text_and_stop_are_bound_to_the_phone(self):
        ref = self.start()
        image_url = self.image_url(self.request(ref.page_url))
        for url in (ref.page_url, image_url, ref.page_url + '/text', ref.stop_url):
            with self.subTest(url=url), self.assertRaises(display.DisplayError) as caught:
                self.request(url, phone='demo-2')
            self.assertIn(caught.exception.status, (404, 409))
        self.assertFalse(self.stops)
        self.assertIn('Listening', ''.join(self.xml(self.request(ref.page_url)).itertext()))

    def test_stop_callback_is_once_even_with_concurrent_requests(self):
        ref = self.start()
        barrier = threading.Barrier(8)
        responses, errors = [], []

        def invoke():
            barrier.wait(timeout=5)
            try:
                responses.append(self.request(ref.stop_url))
            except display.DisplayError as error:
                errors.append(error)

        threads = [threading.Thread(target=invoke) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(self.stops), 1)
        self.assertEqual(self.stops[0], ('demo-1', ref.session_id, self.context))
        self.assertEqual(len(responses) + len(errors), 8)
        for response in responses:
            self.assert_no_refresh(response)
        final = self.request(ref.page_url)
        self.assertIn('Stopped', ''.join(self.xml(final).itertext()))
        self.assert_no_refresh(final)
        self.assertEqual(self.app.stats()['frames'], 0)

    def test_failed_stop_is_terminal_safe_error_and_is_not_retried(self):
        for failure in (False, None, 1, RuntimeError('synthetic-private-stop-details')):
            with self.subTest(failure=str(failure)):
                calls = []

                def callback(*args):
                    calls.append(args)
                    if isinstance(failure, Exception):
                        raise failure
                    return failure

                self.app = self.make_app(on_stop=callback)
                ref = self.start()
                first = self.request(ref.stop_url)
                self.assertIn('Error', ''.join(self.xml(first).itertext()))
                self.assertNotIn(b'synthetic-private', first.body)
                self.assert_no_refresh(first)
                try:
                    self.request(ref.stop_url)
                except display.DisplayError:
                    pass
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.app.stats()['frames'], 0)
                with self.assertRaises(display.DisplayError):
                    self.app.update('demo-1', ref.session_id, state='Listening', context=self.context)

    def test_old_generation_cannot_stop_or_update_explicit_restart(self):
        old = self.start()
        self.request(old.stop_url)
        current = self.start(state='Thinking')
        self.assertNotEqual(old.session_id, current.session_id)
        for operation in (lambda: self.request(old.stop_url),
                          lambda: self.request(old.page_url),
                          lambda: self.app.update('demo-1', old.session_id, state='Speaking', context=self.context)):
            with self.assertRaises(display.DisplayError) as caught:
                operation()
            self.assertIn(caught.exception.status, (404, 409))
        self.assertEqual(len(self.stops), 1)
        self.assertIn('Thinking', ''.join(self.xml(self.request(current.page_url)).itertext()))

    def test_reentrant_stop_cannot_repeat_callback_or_start_new_generation(self):
        calls, pending, rejected = [], [], []

        def callback(*_):
            calls.append(True)
            pending.append(self.request(old.stop_url))
            try:
                self.start(state='Thinking')
            except display.DisplayError as error:
                rejected.append(error.status)
            return True

        self.app = self.make_app(on_stop=callback)
        old = self.start()
        stopped = self.request(old.stop_url)
        self.assertEqual(calls, [True])
        self.assertEqual(rejected, [409])
        self.assertEqual(len(pending), 1)
        self.assert_no_refresh(pending[0])
        self.assertNotIn('Stop confirmed', ''.join(self.xml(pending[0]).itertext()))
        self.assertIn('Stopped', ''.join(self.xml(stopped).itertext()))
        restarted = self.start(state='Thinking')
        response = self.request(restarted.page_url)
        self.assertIn('Thinking', ''.join(self.xml(response).itertext()))
        self.assertIn('refresh', self.headers(response))

    def test_inflight_stop_does_not_claim_confirmation_or_allow_restart(self):
        entered, release = threading.Event(), threading.Event()
        replies, errors = [], []

        def callback(*_):
            entered.set()
            return release.wait(timeout=5)

        self.app = self.make_app(on_stop=callback)
        ref = self.start()

        def stop():
            try:
                replies.append(self.request(ref.stop_url))
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=stop)
        worker.start()
        try:
            self.assertTrue(entered.wait(timeout=5))
            self.assertFalse(self.app.should_present('demo-1', ref.session_id, context=self.context))
            for url in (ref.page_url, ref.stop_url):
                pending = self.request(url)
                text = ''.join(self.xml(pending).itertext())
                self.assertNotIn('Stop confirmed', text)
                self.assertNotEqual(self.xml(pending).find('Title').text, 'Stopped')
                self.assert_no_refresh(pending)
            with self.assertRaises(display.DisplayError) as caught:
                self.start(state='Thinking')
            self.assertEqual(caught.exception.status, 409)
        finally:
            release.set()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertEqual(len(replies), 1)
        self.assertIn('Stop confirmed', ''.join(self.xml(replies[0]).itertext()))

    def test_pending_stop_blocks_restart_after_display_ttl_expires(self):
        entered, release = threading.Event(), threading.Event()
        errors = []

        def callback(*_):
            entered.set()
            return release.wait(timeout=5)

        self.app = self.make_app(on_stop=callback, ttl_seconds=5)
        ref = self.start()

        def stop():
            try:
                self.request(ref.stop_url)
            except display.DisplayError as error:
                errors.append(error)

        worker = threading.Thread(target=stop)
        worker.start()
        try:
            self.assertTrue(entered.wait(timeout=5))
            self.clock.advance(5)
            self.assertEqual(self.app.cleanup(), 1)
            self.assertEqual(self.app.stats()['frames'], 0)
            with self.assertRaises(display.DisplayError) as caught:
                self.start()
            self.assertEqual(caught.exception.status, 409)
        finally:
            release.set()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(all(error.status in (404, 409) for error in errors))
        new = self.start(state='Thinking')
        self.assertNotEqual(ref.session_id, new.session_id)
        self.assertTrue(self.request(new.page_url).allow_present)

    def test_explicit_stop_remains_callable_after_focus_preemption(self):
        ref = self.start()
        self.focus_state = 'ringing'
        self.request(ref.page_url)
        response = self.request(ref.stop_url)
        self.assertEqual(len(self.stops), 1)
        self.assertEqual(self.stops[0][1], ref.session_id)
        self.assertEqual(response.status, 204)
        self.assertFalse(response.allow_present)
        self.request(ref.stop_url)
        self.assertEqual(len(self.stops), 1)

    def test_should_present_never_pushes_terminal_or_preempted_state(self):
        for state in ('Listening', 'Thinking', 'Speaking', 'Stopped', 'Error'):
            self.app = self.make_app()
            ref = self.start(state=state)
            self.assertEqual(self.app.should_present('demo-1', ref.session_id, context=self.context),
                             state not in ('Stopped', 'Error'))
        self.app = self.make_app()
        ref = self.start()
        self.focus_state = 'call'
        self.assertFalse(self.app.should_present('demo-1', ref.session_id, context=self.context))
        self.focus_state = 'idle'
        self.assertFalse(self.app.should_present('demo-1', ref.session_id, context=self.context))

    def test_frame_replacement_does_not_accumulate_and_invalidates_old_revision(self):
        ref = self.start(image=picture('red'))
        old_url = self.image_url(self.request(ref.page_url))
        other = self.start(phone='demo-2', image=picture('green'))
        for color in ('blue', 'purple', 'orange', 'yellow'):
            self.app.update('demo-1', ref.session_id, image=picture(color), context=self.context)
            self.assertEqual(self.app.stats()['frames'], 2)
            self.assertEqual(self.app.stats()['sessions'], 2)
        with self.assertRaises(display.DisplayError):
            self.request(old_url)
        new_url = self.image_url(self.request(ref.page_url))
        self.assertNotEqual(old_url, new_url)
        current = self.request(new_url)
        other_image = self.request(self.image_url(self.request(other.page_url, phone='demo-2')), phone='demo-2')
        self.assertEqual(self.app.stats()['frame_bytes'], len(current.body) + len(other_image.body))
        with Image.open(io.BytesIO(current.body)) as result:
            self.assertEqual(result.getpixel((149, 78)), (255, 255, 0))

    def test_ttl_is_absolute_and_cleanup_removes_frames_at_exact_deadline(self):
        self.app = self.make_app(ttl_seconds=5)
        ref = self.start()
        self.clock.advance(4)
        self.request(ref.page_url)
        self.app.update('demo-1', ref.session_id, state='Thinking', context=self.context)
        self.clock.advance(1)
        self.assertEqual(self.app.cleanup(), 1)
        self.assertEqual(self.app.stats(), {'sessions': 0, 'frames': 0, 'frame_bytes': 0})
        with self.assertRaises(display.DisplayError):
            self.request(ref.page_url)
        self.assertEqual(self.app.cleanup(), 0)

    def test_wall_clock_changes_do_not_extend_or_shorten_monotonic_ttl(self):
        self.app = self.make_app(ttl_seconds=5)
        ref = self.start()
        self.clock.wall -= 3600
        self.clock.now += 4
        self.assertEqual(self.app.cleanup(), 0)
        self.request(ref.page_url)
        self.clock.wall += 7200
        self.clock.now += 1
        self.assertEqual(self.app.cleanup(), 1)

    def test_session_cap_rejects_without_evicting_another_phone(self):
        self.app = self.make_app(max_sessions=2)
        first = self.start()
        second = self.start(phone='demo-2')
        with self.assertRaises(display.DisplayError) as caught:
            self.start(phone='demo-3')
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(self.app.stats()['sessions'], 2)
        self.assertEqual(self.request(first.page_url).status, 200)
        self.assertEqual(self.request(second.page_url, phone='demo-2').status, 200)

    def test_refresh_interval_and_resource_limits_require_bounded_integers(self):
        for name, bad in [('refresh_seconds', [True, 1, 6, 2.5, '3']),
                          ('ttl_seconds', [True, 4, 121, 5.0, '30']),
                          ('max_sessions', [True, 0, 65, 2.0, '2'])]:
            for value in bad:
                with self.subTest(name=name, value=value), self.assertRaises((display.DisplayError, ValueError)):
                    self.make_app(**{name: value})
        for seconds in (2, 3, 4, 5):
            self.app = self.make_app(refresh_seconds=seconds)
            ref = self.start()
            refresh = self.headers(self.request(ref.page_url))['refresh']
            self.assertEqual(int(refresh.split(';', 1)[0]), seconds)

    def test_call_ringing_radio_unknown_focus_preempt_and_never_auto_resume(self):
        for focus in ('call', 'ringing', 'radio', 'unknown', 'Idle', '', None, False):
            with self.subTest(focus=focus):
                self.focus_state = 'idle'
                self.app = self.make_app()
                ref = self.start()
                image_url = self.image_url(self.request(ref.page_url))
                self.focus_state = focus
                response = self.request(ref.page_url)
                self.assertEqual(response.status, 204)
                self.assertFalse(response.body)
                self.assertFalse(response.allow_present)
                self.assert_no_refresh(response)
                self.assertEqual(self.app.stats()['frames'], 0)
                self.focus_state = 'idle'
                after = self.request(ref.page_url)
                self.assertFalse(after.allow_present)
                self.assert_no_refresh(after)
                try:
                    frame = self.request(image_url)
                except display.DisplayError:
                    pass
                else:
                    self.assertEqual(frame.status, 204)
                    self.assertFalse(frame.body)
                with self.assertRaises(display.DisplayError):
                    self.app.update('demo-1', ref.session_id, state='Listening', context=self.context)

    def test_focus_is_checked_on_image_fetch_and_failure_is_closed(self):
        ref = self.start()
        image_url = self.image_url(self.request(ref.page_url))
        self.focus_state = 'ringing'
        response = self.request(image_url)
        self.assertEqual(response.status, 204)
        self.assertFalse(response.body)
        self.assertFalse(response.allow_present)
        self.assertEqual(self.app.stats()['frames'], 0)

        def fail(*_):
            raise RuntimeError('synthetic-private-focus-error')

        self.focus_state = 'idle'
        self.app = self.make_app()
        ref = self.start()
        self.app.focus = fail
        response = self.request(ref.page_url)
        self.assertEqual(response.status, 204)
        self.assertFalse(response.body)

    def test_assistant_focus_is_allowed_but_rich_text_never_overrides_calls(self):
        self.focus_state = 'assistant'
        ref = self.start(text='Synthetic answer')
        self.assertTrue(self.request(ref.page_url).allow_present)
        self.focus_state = 'call'
        response = self.request(ref.page_url + '/text')
        self.assertEqual(response.status, 204)
        self.assertFalse(response.body)
        self.assertFalse(response.allow_present)

    def test_unknown_routes_methods_and_query_injection_are_rejected(self):
        ref = self.start()
        path = '/session/' + ref.session_id
        attempts = [(path, 'x=1', 'GET'), (path, '', 'POST'),
                    (path + '/image', 'v=1&v=2', 'GET'),
                    (path + '/image', 'v=1&phone=demo-2', 'GET'),
                    (path + '/stop', 'session=other', 'GET'),
                    (path + '/../../other', '', 'GET'),
                    ('/session/not-a-token', '', 'GET')]
        for relative, query, method in attempts:
            with self.subTest(path=relative, query=query), self.assertRaises(display.DisplayError):
                self.app.handle('demo-1', relative, query, context=self.context, method=method)
        self.assertFalse(self.stops)


class CameraDisplayTests(DisplayFixture):
    def test_camera_requires_timestamp_and_exposes_capture_time(self):
        with self.assertRaises(display.DisplayError):
            self.start(mode='camera', image=picture())
        ref = self.start(mode='camera', image=picture(), captured_at=self.clock.wall)
        text = ''.join(self.xml(self.request(ref.page_url)).itertext())
        self.assertIn('22:13:20', text)
        self.assertIn('UTC', text)

    def test_camera_timestamp_rejects_nonfinite_boolean_and_future_values(self):
        for timestamp in (float('nan'), float('inf'), -1, True, '1700000000', self.clock.wall + 60):
            with self.subTest(timestamp=timestamp), self.assertRaises(display.DisplayError):
                self.start(mode='camera', image=picture(), captured_at=timestamp)
        ref = self.start(mode='camera', image=picture(), captured_at=self.clock.wall - 30)
        self.assertIn('Stale frame', ''.join(self.xml(self.request(ref.page_url)).itertext()))

    def test_camera_fetches_no_more_than_once_per_interval(self):
        calls = []

        def source(phone):
            calls.append(phone)
            return display.Frame(picture('red' if len(calls) == 1 else 'blue'), self.clock.wall)

        ref = self.start(mode='camera', frame_source=source)
        self.assertFalse(calls)
        first = self.request(ref.page_url)
        first_url = self.image_url(first)
        self.request(ref.page_url)
        self.request(first_url)
        self.clock.advance(2)
        self.request(ref.page_url)
        self.assertEqual(calls, ['demo-1'])
        self.clock.advance(1)
        second_url = self.image_url(self.request(ref.page_url))
        self.assertEqual(calls, ['demo-1', 'demo-1'])
        self.assertNotEqual(first_url, second_url)
        self.assertEqual(self.app.stats()['frames'], 1)

    def test_camera_source_exception_is_safe_terminal_error_without_retry(self):
        calls = []

        def fail(phone):
            calls.append(phone)
            raise RuntimeError('synthetic-private-camera-url-and-token')

        ref = self.start(mode='camera', frame_source=fail)
        first = self.request(ref.page_url)
        self.assertIn('Error', ''.join(self.xml(first).itertext()))
        self.assertNotIn(b'synthetic-private', first.body)
        self.assert_no_refresh(first)
        self.clock.advance(10)
        self.request(ref.page_url)
        self.assertEqual(calls, ['demo-1'])
        self.assertEqual(self.app.stats()['frames'], 0)

    def test_preempted_camera_does_not_fetch_or_resume_source(self):
        calls = []

        def source(phone):
            calls.append(phone)
            return display.Frame(picture(), self.clock.wall)

        ref = self.start(mode='camera', frame_source=source)
        self.focus_state = 'ringing'
        self.assertEqual(self.request(ref.page_url).status, 204)
        self.focus_state = 'idle'
        self.clock.advance(10)
        self.assertFalse(self.request(ref.page_url).allow_present)
        self.assertFalse(calls)

    def test_only_one_camera_fetch_can_be_in_flight(self):
        entered, release = threading.Event(), threading.Event()
        calls, errors = [], []

        def source(phone):
            calls.append(phone)
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError('test source timed out')
            return display.Frame(picture(), self.clock.wall)

        ref = self.start(mode='camera', frame_source=source)

        def fetch():
            try:
                self.request(ref.page_url)
            except Exception as error:
                errors.append(error)

        first = threading.Thread(target=fetch)
        second = threading.Thread(target=fetch)
        first.start()
        self.assertTrue(entered.wait(timeout=5))
        second.start()
        release.set()
        first.join(timeout=5)
        second.join(timeout=5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertFalse(errors)
        self.assertEqual(calls, ['demo-1'])

    def test_late_camera_callback_cannot_overwrite_explicit_frame_update(self):
        entered, release = threading.Event(), threading.Event()
        errors = []

        def source(_phone):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError('test source timed out')
            return display.Frame(picture('red'), self.clock.wall)

        ref = self.start(mode='camera', frame_source=source)

        def fetch():
            try:
                self.request(ref.page_url)
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=fetch)
        worker.start()
        try:
            self.assertTrue(entered.wait(timeout=5))
            self.app.update('demo-1', ref.session_id, image=picture('blue'),
                            captured_at=self.clock.wall, context=self.context)
        finally:
            release.set()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        frame = self.request(self.image_url(self.request(ref.page_url)))
        with Image.open(io.BytesIO(frame.body)) as result:
            self.assertEqual(result.getpixel((149, 78)), (0, 0, 255))

    def test_late_camera_callback_cannot_revive_stopped_or_replaced_session(self):
        entered, release = threading.Event(), threading.Event()
        errors, calls = [], []

        def source(phone):
            calls.append(phone)
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError('test source timed out')
            return display.Frame(picture('red'), self.clock.wall)

        old = self.start(mode='camera', frame_source=source)

        def fetch():
            try:
                self.request(old.page_url)
            except display.DisplayError as error:
                errors.append(error)

        worker = threading.Thread(target=fetch)
        worker.start()
        try:
            self.assertTrue(entered.wait(timeout=5))
            self.request(old.stop_url)
            new = self.start(mode='camera', image=picture('blue'),
                             captured_at=self.clock.wall, frame_source=source)
            self.request(new.page_url)
            self.assertEqual(calls, ['demo-1'])
        finally:
            release.set()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(all(error.status in (404, 409) for error in errors))
        frame = self.request(self.image_url(self.request(new.page_url)))
        with Image.open(io.BytesIO(frame.body)) as result:
            self.assertEqual(result.getpixel((149, 78)), (0, 0, 255))
        self.assertEqual(self.app.stats()['frames'], 1)


class ServerFixture:
    def make_server(self, **options):
        values = dict(bind_ip='127.0.0.1', port=0, phone_id='demo', phone_ip='127.0.0.1',
                      focus=lambda: 'idle', on_stop=lambda: True)
        values.update(options)
        server = display.VoiceDisplayServer(**values)
        self.addCleanup(server.close)
        return server

    def request(self, server, path, *, method='GET', headers=None, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', server.port, timeout=2)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return (response.status, dict((key.lower(), value) for key, value in response.getheaders()),
                    response.read(), response.version)
        finally:
            connection.close()

    def raw_request(self, server, request):
        with socket.create_connection(('127.0.0.1', server.port), timeout=2) as connection:
            connection.sendall(request)
            response = http.client.HTTPResponse(connection)
            response.begin()
            return response.status, response.read()

    def camera_session(self, server):
        # An image-free fixture exercises HTTP controls without requiring Pillow.
        return server.display.start('demo', mode='camera', context=server._control_context)

    @staticmethod
    def path(ref, suffix=''):
        return urlsplit(ref.page_url).path + suffix


class VoiceDisplayServerControlTests(ServerFixture, unittest.TestCase):
    def test_constructor_opens_no_socket_and_context_binds_an_ephemeral_loopback_port(self):
        with patch('socket.socket', side_effect=AssertionError('constructor opened a socket')):
            server = self.make_server()
        self.assertIsNone(server.session_ref)
        with server as started:
            self.assertIs(started, server)
            self.assertGreater(server.port, 0)
            self.assertEqual(server.base_url, 'http://127.0.0.1:' + str(server.port) + '/voice')
            port = server.port
            self.assertEqual(self.request(server, '/voice/missing')[0], 404)
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', port), timeout=1)
        server.close()

    def test_source_ip_is_authoritative_despite_forwarding_headers(self):
        server = self.make_server(phone_ip='127.0.0.2').start()
        for headers in ({}, {'X-Forwarded-For': '127.0.0.2',
                              'X-Real-IP': '127.0.0.2',
                              'Forwarded': 'for=127.0.0.2'}):
            with self.subTest(headers=headers):
                status, response_headers, body, version = self.request(server, '/voice/session/' + 'a' * 32,
                                                                        headers=headers)
                self.assertEqual(status, 403)
                self.assertNotIn(b'127.0.0.2', body)
                self.assertEqual(response_headers.get('connection', '').lower(), 'close')
                self.assertEqual(version, 10)

    def test_forbidden_incomplete_request_never_takes_a_worker_slot(self):
        server = self.make_server(phone_ip='127.0.0.2', max_workers=1).start()
        with patch.object(server._server, '_run_request',
                          side_effect=AssertionError('forbidden peer got a worker')) as worker:
            with socket.create_connection(('127.0.0.1', server.port), timeout=1) as connection:
                # Even no request line must be refused before allocating a worker.
                response = http.client.HTTPResponse(connection)
                response.begin()
                self.assertEqual(response.status, 403)
                self.assertEqual(response.read(), b'')
            self.assertFalse(worker.called)
            with server._server.guard:
                self.assertEqual(len(server._server.workers), 0)
                self.assertEqual(len(server._server.requests), 0)

    def test_bodyless_relative_get_and_supported_method_are_enforced(self):
        stops = []
        server = self.make_server(on_stop=lambda: stops.append(True) or True).start()
        ref = self.camera_session(server)
        path = self.path(ref)
        self.assertEqual(self.request(server, path, method='POST')[0], 405)
        malformed = [
            'GET http://127.0.0.1/voice/session/example HTTP/1.1\r\nHost: localhost\r\n\r\n',
            'GET ' + path + '#fragment HTTP/1.1\r\nHost: localhost\r\n\r\n',
            'GET ' + path + ' HTTP/1.1\r\nHost: localhost\r\nContent-Length: 1\r\n\r\nx',
            'GET ' + path + ' HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n',
            'GET ' + path + ' HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\nContent-Length: 1\r\n\r\nx',
        ]
        for wire in malformed:
            with self.subTest(request=wire):
                self.assertEqual(self.raw_request(server, wire.encode('ascii'))[0], 400)
        self.assertFalse(stops)
        self.assertEqual(self.request(server, path)[0], 200)

    def test_http_refresh_and_call_focus_suppression_preserve_library_contract(self):
        focus = ['idle']
        server = self.make_server(focus=lambda: focus[0], refresh_seconds=2).start()
        ref = self.camera_session(server)
        status, headers, body, version = self.request(server, self.path(ref))
        self.assertEqual(status, 200)
        self.assertEqual(version, 10)
        self.assertIn('no-store', headers['cache-control'])
        self.assertTrue(headers['refresh'].startswith('2;'))
        self.assertEqual(headers['connection'].lower(), 'close')
        self.assertEqual(ET.fromstring(body).tag, 'CiscoIPPhoneText')
        focus[0] = 'ringing'
        status, headers, body, _ = self.request(server, self.path(ref))
        self.assertEqual(status, 204)
        self.assertFalse(body)
        self.assertNotIn('refresh', headers)
        focus[0] = 'idle'
        self.assertEqual(self.request(server, self.path(ref))[0], 204)

    def test_http_stop_calls_zero_argument_callback_exactly_once(self):
        calls = []
        server = self.make_server(on_stop=lambda: calls.append(True) or True).start()
        ref = self.camera_session(server)
        for _ in range(2):
            status, headers, body, _ = self.request(server, self.path(ref, '/stop'))
            self.assertEqual(status, 200)
            self.assertIn('Stopped', ''.join(ET.fromstring(body).itertext()))
            self.assertNotIn('refresh', headers)
        self.assertEqual(calls, [True])
        server.close()
        self.assertEqual(calls, [True])

    def test_incomplete_request_times_out_and_server_remains_usable(self):
        server = self.make_server(request_timeout=0.2).start()
        with socket.create_connection(('127.0.0.1', server.port), timeout=2) as connection:
            connection.sendall(b'GET /voice/ HTTP/1.1\r\nHost: localhost\r\n')
            try:
                while connection.recv(1024):
                    pass
            except ConnectionResetError:
                pass
        ref = self.camera_session(server)
        self.assertEqual(self.request(server, self.path(ref))[0], 200)

    def test_worker_saturation_closes_excess_connection_without_another_callback(self):
        entered, release = threading.Event(), threading.Event()
        block, calls = [False], []

        def focus():
            if block[0]:
                calls.append(True)
                entered.set()
                release.wait(timeout=2)
            return 'idle'

        server = self.make_server(focus=focus, max_workers=1, request_timeout=2).start()
        ref = self.camera_session(server)
        block[0] = True
        first = socket.create_connection(('127.0.0.1', server.port), timeout=2)
        try:
            request = ('GET ' + self.path(ref) + ' HTTP/1.1\r\nHost: localhost\r\n\r\n').encode('ascii')
            first.sendall(request)
            self.assertTrue(entered.wait(timeout=1))
            with socket.create_connection(('127.0.0.1', server.port), timeout=1) as excess:
                try:
                    excess.sendall(request)
                    self.assertEqual(excess.recv(1024), b'')
                except ConnectionResetError:
                    pass
            self.assertEqual(calls, [True])
        finally:
            block[0] = False
            release.set()
        try:
            response = http.client.HTTPResponse(first)
            response.begin()
            self.assertEqual(response.status, 200)
            response.read()
        finally:
            first.close()


@unittest.skipUnless(Image, 'Pillow is required for HTTP status and PNG validation')
class VoiceDisplayServerStatusTests(ServerFixture, unittest.TestCase):
    def test_lowercase_and_title_statuses_keep_one_session_and_expose_real_png(self):
        server = self.make_server().start()
        ref = server.start_status('listening', text='A < B & C')
        self.assertEqual(server.session_ref, ref)
        status, _, body, _ = self.request(server, self.path(ref))
        self.assertEqual(status, 200)
        root = ET.fromstring(body)
        self.assertEqual(root.find('Title').text, 'Listening')
        image_target = urlsplit(root.find('URL').text)
        status, headers, image_bytes, _ = self.request(server, image_target.path + '?' + image_target.query)
        self.assertEqual(status, 200)
        self.assertEqual(headers['content-type'], 'image/png')
        with Image.open(io.BytesIO(image_bytes)) as image:
            self.assertEqual(image.size, (298, 156))
            self.assertEqual(image.mode, 'RGB')
        for state in ('Thinking', 'speaking'):
            updated = server.update_status(state, text='Updated & escaped')
            self.assertEqual(updated.session_id, ref.session_id)
            self.assertEqual(server.session_ref.session_id, ref.session_id)
            _, _, body, _ = self.request(server, self.path(ref))
            self.assertEqual(ET.fromstring(body).find('Title').text, state.title())
        server.update_status('error')
        _, _, body, _ = self.request(server, self.path(ref))
        self.assertEqual(ET.fromstring(body).find('Title').text, 'Error')
        terminal = server.start_status('stopped')
        _, _, body, _ = self.request(server, self.path(terminal))
        self.assertEqual(ET.fromstring(body).find('Title').text, 'Stopped')

    def test_update_requires_existing_session_and_cannot_rearm_a_stopped_session(self):
        server = self.make_server().start()
        with self.assertRaises(display.DisplayError):
            server.update_status('listening')
        ref = server.start_status('Listening')
        self.assertEqual(self.request(server, self.path(ref, '/stop'))[0], 200)
        with self.assertRaises(display.DisplayError):
            server.update_status('Listening')
        self.assertEqual(server.session_ref.session_id, ref.session_id)
        server.close()
        self.assertIsNone(server.session_ref)

    def test_close_clears_frames_sessions_and_socket_without_implicit_stop(self):
        calls = []
        server = self.make_server(on_stop=lambda: calls.append(True) or True).start()
        server.start_status('Speaking')
        self.assertEqual(server.display.stats()['frames'], 1)
        port = server.port
        server.close()
        server.close()
        self.assertIsNone(server.session_ref)
        self.assertEqual(server.display.stats(), {'sessions': 0, 'frames': 0, 'frame_bytes': 0})
        self.assertFalse(calls)
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', port), timeout=1)


if __name__ == '__main__':
    unittest.main()
