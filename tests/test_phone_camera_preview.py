"""Offline camera-preview contracts; synthetic pixels, clocks and callbacks only."""

from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import sys
import threading
import unittest
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

try:
    from PIL import Image, PngImagePlugin
except ImportError:
    Image = PngImagePlugin = None


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "camera_preview_contract", ROOT / "containers/asterisk/phone_camera_preview.py")
preview = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = preview
SPEC.loader.exec_module(preview)


class Clock:
    def __init__(self):
        self.now = 100.0
        self.wall = 1_700_000_000.0

    def advance(self, seconds):
        self.now += seconds
        self.wall += seconds


def picture(color="purple"):
    output = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Comment", "synthetic-private-camera-metadata")
    Image.new("RGB", (640, 480), color).save(output, "PNG", pnginfo=metadata)
    return output.getvalue()


class Backend:
    """Only catalog/image reads exist; there is no phone or media-control API."""

    def __init__(self, data):
        self.data = data
        self.catalog = [{"id": "front", "label": "Synthetic front camera"}]
        self.catalog_calls = []
        self.calls = []
        self.hook = None
        self.lock = threading.Lock()
        self.active = {}
        self.maximum = {}

    def cameras(self, phone):
        self.catalog_calls.append(phone)
        return list(self.catalog)

    def camera(self, phone, camera):
        with self.lock:
            self.calls.append((phone, camera))
            self.active[phone] = self.active.get(phone, 0) + 1
            self.maximum[phone] = max(self.maximum.get(phone, 0), self.active[phone])
        try:
            return self.hook(phone, camera) if self.hook else self.data
        finally:
            with self.lock:
                self.active[phone] -= 1


@unittest.skipUnless(Image, "Pillow is required for synthetic camera-preview contracts")
class CameraPreviewTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.phones = {"201": ("201", "202"), "203": ("203", "204")}
        self.bases = {
            "201": "http://192.0.2.10:18080/apps/first/camera-preview",
            "203": "http://192.0.2.10:18081/apps/second/camera-preview",
        }
        self.context = {"principal": "synthetic-viewer"}
        self.allowed = True
        self.authorization = []
        self.observations = {}
        self.backend = Backend(picture())
        self.app = self.make_app()

    def authorize(self, phone, action, context):
        self.authorization.append((phone, action, context))
        if isinstance(self.allowed, Exception):
            raise self.allowed
        return self.allowed

    def observe(self, phone, context):
        self.assertIs(context, self.context)
        if phone in self.observations:
            value = self.observations[phone]
            if isinstance(value, Exception):
                raise value
            return value
        return preview.PhoneObservation(
            self.clock.now, {line: "idle" for line in self.phones[phone]}, "idle")

    def make_app(self, **overrides):
        values = dict(authorize=self.authorize, observe=self.observe,
                      clock=lambda: self.clock.now, wall_clock=lambda: self.clock.wall)
        values.update(overrides)
        return preview.CameraPreviewController(
            self.backend, self.phones, self.bases, **values)

    def start(self, phone="201", camera="front", **options):
        return self.app.start(phone, camera, context=self.context, **options)

    def request(self, url, *, phone="201", method="GET"):
        parsed = urlsplit(url)
        for base in self.bases.values():
            prefix = urlsplit(base).path
            if parsed.path.startswith(prefix + "/"):
                return self.app.handle(phone, parsed.path[len(prefix):], parsed.query,
                                       context=self.context, method=method)
        raise AssertionError("Test URL is outside the synthetic display routes")

    def xml(self, response):
        self.assertEqual(response.status, 200)
        self.assertIn("xml", response.content_type)
        root = ET.fromstring(response.body)
        for field in ("Title", "Prompt"):
            self.assertLessEqual(len(root.findtext(field, "")), 32, field)
        return root

    def image_url(self, response):
        xml = self.xml(response)
        self.assertEqual(xml.tag, "CiscoIPPhoneImageFile")
        return xml.findtext("URL")

    def assert_yielded(self, response):
        self.assertEqual(response.status, 204)
        self.assertEqual(response.body, b"")
        self.assertFalse(response.allow_present)
        self.assertNotIn("refresh", {key.lower() for key in response.headers})

    def assert_no_image(self, url):
        try:
            response = self.request(url)
        except preview.DisplayError as error:
            self.assertIn(error.status, (404, 409))
        else:
            self.assert_yielded(response)

    def assert_terminal_page(self, response):
        self.assertNotIn("refresh", {key.lower() for key in response.headers})
        self.assertNotIn(b"synthetic-private", response.body)
        self.assertEqual(self.app.stats()["frames"], 0)

    def spawn(self, operation):
        result, errors = [], []

        def worker():
            try:
                result.append(operation())
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return thread, result, errors

    def block_backend(self, phone="201"):
        entered, release = threading.Event(), threading.Event()

        def image(selected, _camera):
            if selected == phone:
                entered.set()
                if not release.wait(timeout=5):
                    raise AssertionError("Synthetic image source was not released")
            return self.backend.data

        self.backend.hook = image
        return entered, release

    def test_start_is_inert_and_first_page_acquires_one_phone_sized_image(self):
        ref = self.start(line="202")
        self.assertRegex(ref.session_id, r"^[0-9a-f]{32}$")
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.backend.catalog_calls, ["201"])
        self.assertEqual(self.app.stats(), {"sessions": 1, "frames": 0, "frame_bytes": 0})
        page = self.request(ref.page_url)
        url = self.image_url(page)
        response = self.request(url)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "image/png")
        with Image.open(io.BytesIO(response.body)) as image:
            self.assertEqual(image.size, (298, 156))
            self.assertEqual(image.mode, "RGB")
            self.assertFalse(image.getexif())
            self.assertNotIn("Comment", image.info)
        self.assertNotIn(b"synthetic-private", response.body)
        self.assertEqual(self.backend.calls, [("201", "front")])
        for item in (page, response):
            self.assertIn("no-store", item.headers["Cache-Control"])
        for value in self.xml(page).itertext():
            self.assertFalse(value.startswith(("Key:", "Dial:", "RTP", "Init:")))

    def test_caption_distinguishes_acquisition_from_unknown_capture_time(self):
        ref = self.start()
        page = self.request(ref.page_url)
        self.assertEqual(self.xml(page).findtext("Prompt"), "Capture time unknown; Details")
        details = self.request(ref.page_url + "/text")
        text = "".join(self.xml(details).itertext())
        self.assertIn("Acquired: 2023-11-14 22:13:20 UTC", text)
        self.assertIn("Camera capture time unavailable.", text)
        self.assertIn("Acquisition does not prove a new camera frame.", text)
        self.assertIn("Stop ends this preview only.", text)
        self.assertEqual(len(self.backend.calls), 1)
        self.clock.advance(3)
        repeated = self.request(ref.page_url)
        stamp = datetime.fromtimestamp(self.clock.wall, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        self.assertIn(stamp, self.xml(self.request(ref.page_url + "/text")).findtext("Text"))
        self.assertEqual(len(self.backend.calls), 2)
        self.assertIn("capture time", "".join(self.xml(repeated).itertext()).lower())

    def test_refresh_interval_and_image_revision_do_not_slide_on_raw_image_reads(self):
        ref = self.start()
        first = self.image_url(self.request(ref.page_url))
        self.clock.advance(2.9)
        self.request(first)
        self.request(ref.page_url + "/text")
        self.assertEqual(len(self.backend.calls), 1)
        self.clock.advance(.1)
        second = self.image_url(self.request(ref.page_url))
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.backend.calls), 2)
        self.assert_no_image(first)
        self.assertEqual(self.request(second).status, 200)

    def test_constructor_rejects_unbounded_intervals_and_duplicate_physical_lines(self):
        for key, values in (
            ("refresh_seconds", (True, 1, 6, 3.0)),
            ("ttl_seconds", (True, 4, 121, 60.0)),
            ("stale_seconds", (True, 2, 31, 10.0)),
        ):
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(preview.DisplayError):
                    self.make_app(**{key: value})
        self.phones["203"] = ("203", "202")
        with self.assertRaises(preview.DisplayError):
            self.make_app()

    def test_alias_and_line_validation_precede_camera_fetches(self):
        for camera in ("unknown", "http://example.invalid/private", "../front"):
            with self.subTest(camera=camera), self.assertRaises(preview.DisplayError):
                self.start(camera=camera)
        with self.assertRaises(preview.DisplayError):
            self.start(line="204")
        self.assertEqual(self.backend.calls, [])

    def test_malformed_routes_methods_and_queries_never_fetch(self):
        ref = self.start()
        for url, method in (
            (ref.page_url + "?camera=front", "GET"),
            (ref.page_url + "?v=1", "GET"),
            (ref.page_url + "/image?v=1&v=2", "GET"),
            (ref.page_url + "/image?v=0", "GET"),
            (ref.page_url + "/unknown", "GET"),
            (ref.page_url, "POST"),
        ):
            with self.subTest(url=url, method=method), self.assertRaises(preview.DisplayError):
                self.request(url, method=method)
        self.assertEqual(self.backend.calls, [])

    def test_authorization_requires_exact_true_for_start_pages_frames_and_stop(self):
        ref = self.start()
        image = self.image_url(self.request(ref.page_url))
        for allowed in (False, None, 1, "yes", RuntimeError("synthetic-private-auth")):
            self.allowed = allowed
            for operation in (
                lambda: self.start(phone="203"),
                lambda: self.request(ref.page_url),
                lambda: self.request(ref.page_url + "/text"),
                lambda: self.request(image),
                lambda: self.request(ref.stop_url),
            ):
                with self.subTest(allowed=str(allowed)), self.assertRaises(preview.DisplayError) as caught:
                    operation()
                self.assertEqual(caught.exception.status, 403)
                self.assertNotIn("synthetic-private", str(caught.exception))
        self.assertEqual(len(self.backend.calls), 1)
        self.assertTrue(all(item[2] is self.context for item in self.authorization))

    def test_sessions_and_images_cannot_be_read_or_stopped_through_another_phone(self):
        ref = self.start()
        image = self.image_url(self.request(ref.page_url))
        for url in (ref.page_url, ref.page_url + "/text", image, ref.stop_url):
            with self.subTest(url=url), self.assertRaises(preview.DisplayError):
                self.request(url, phone="203")
        self.assertEqual(self.request(image).status, 200)
        self.assertEqual(len(self.backend.calls), 1)

    def test_start_requires_both_physical_lines_and_voice_to_be_truly_idle(self):
        observations = [
            preview.PhoneObservation(self.clock.now, {"201": "idle"}, "idle"),
            preview.PhoneObservation(self.clock.now, {"201": "idle", "202": "ringing"}, "idle"),
            preview.PhoneObservation(self.clock.now, {"201": "idle", "202": "idle"}, "external_owner"),
            preview.PhoneObservation(self.clock.now - 3.01, {"201": "idle", "202": "idle"}, "idle"),
        ]
        for value in observations:
            self.observations["201"] = value
            with self.subTest(value=value), self.assertRaises(preview.DisplayError) as caught:
                self.start()
            self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.backend.catalog_calls, [])
        self.assertEqual(self.backend.calls, [])

    def test_observation_boundary_accepts_exact_refresh_age_but_rejects_older(self):
        self.observations["201"] = preview.PhoneObservation(
            self.clock.now - 3, {"201": "idle", "202": "idle"}, "idle")
        ref = self.start()
        self.request(ref.page_url)
        self.clock.advance(.001)
        self.assert_yielded(self.request(ref.page_url))
        self.observations.clear()
        self.assert_yielded(self.request(ref.page_url))

    def test_nonidle_line_permanently_yields_page_and_invalidates_image(self):
        for line in ("201", "202"):
            for state in ("ringing", "talking", "held", "offhook", "busy", "unknown", "unavailable"):
                with self.subTest(line=line, state=state):
                    self.app = self.make_app()
                    self.observations.clear()
                    ref = self.start()
                    image = self.image_url(self.request(ref.page_url))
                    calls = len(self.backend.calls)
                    lines = {"201": "idle", "202": "idle"}
                    lines[line] = state
                    self.observations["201"] = preview.PhoneObservation(self.clock.now, lines, "idle")
                    self.assert_yielded(self.request(image))
                    self.assertEqual(self.app.stats()["frames"], 0)
                    self.observations.clear()
                    self.clock.advance(3)
                    self.assert_yielded(self.request(ref.page_url))
                    self.assert_no_image(image)
                    self.assertEqual(len(self.backend.calls), calls)

    def test_missing_stale_invalid_observations_and_voice_ownership_fail_closed(self):
        lines = {"201": "idle", "202": "idle"}
        bad = [
            None, {"lines": lines}, RuntimeError("synthetic-private-observer"),
            preview.PhoneObservation(self.clock.now, {"201": "idle"}, "idle"),
            preview.PhoneObservation(self.clock.now, {**lines, "299": "idle"}, "idle"),
            preview.PhoneObservation(float("nan"), lines, "idle"),
            preview.PhoneObservation(float("inf"), lines, "idle"),
            preview.PhoneObservation(True, lines, "idle"),
            preview.PhoneObservation(self.clock.now + .001, lines, "idle"),
            preview.PhoneObservation(self.clock.now - 3.001, lines, "idle"),
        ]
        bad.extend(preview.PhoneObservation(self.clock.now, lines, voice)
                   for voice in ("running", "starting", "stopping", "external_owner",
                                 "recovery_required", "unknown", "", None))
        for value in bad:
            with self.subTest(value=value):
                self.app = self.make_app()
                self.observations.clear()
                ref = self.start()
                image = self.image_url(self.request(ref.page_url))
                self.observations["201"] = value
                self.assert_yielded(self.request(ref.page_url))
                self.assertEqual(self.app.stats()["frames"], 0)
                self.observations.clear()
                self.assert_yielded(self.request(ref.page_url))
                self.assert_no_image(image)

    def test_raw_image_never_refreshes_and_stale_frame_expires_at_inclusive_boundary(self):
        ref = self.start()
        image = self.image_url(self.request(ref.page_url))
        self.clock.advance(9.999)
        self.assertEqual(self.request(image).status, 200)
        self.assertEqual(len(self.backend.calls), 1)
        self.clock.advance(.001)
        self.assert_no_image(image)
        terminal = self.request(ref.page_url)
        self.assert_terminal_page(terminal)
        self.clock.advance(3)
        self.assert_terminal_page(self.request(ref.page_url))
        self.assertEqual(len(self.backend.calls), 1)

    def test_staleness_uses_monotonic_acquisition_age_not_wall_clock(self):
        ref = self.start()
        image = self.image_url(self.request(ref.page_url))
        self.clock.now += 10
        self.clock.wall -= 3600
        self.assert_no_image(image)
        self.assert_terminal_page(self.request(ref.page_url))
        self.assertEqual(len(self.backend.calls), 1)

    def test_absolute_ttl_is_not_extended_by_refresh_or_wall_clock_changes(self):
        self.app = self.make_app(ttl_seconds=5)
        ref = self.start()
        self.request(ref.page_url)
        self.clock.advance(3)
        image = self.image_url(self.request(ref.page_url))
        self.clock.wall -= 7200
        self.clock.now += 2
        self.assertEqual(self.app.cleanup(), 1)
        self.assertEqual(self.app.stats(), {"sessions": 0, "frames": 0, "frame_bytes": 0})
        with self.assertRaises(preview.DisplayError) as caught:
            self.request(image)
        self.assertEqual(caught.exception.status, 404)

    def test_stop_is_local_exact_session_and_old_token_cannot_stop_replacement(self):
        old = self.start()
        image = self.image_url(self.request(old.page_url))
        self.assert_terminal_page(self.request(old.stop_url))
        self.assert_terminal_page(self.request(old.stop_url))
        self.assert_no_image(image)
        current = self.start(line="202")
        self.assertNotEqual(old.session_id, current.session_id)
        with self.assertRaises(preview.DisplayError):
            self.request(old.stop_url)
        self.assertEqual(self.request(current.page_url).status, 200)
        self.assertEqual(len(self.backend.calls), 2)

    def test_fetch_error_invalidates_previous_frame_and_does_not_retry(self):
        ref = self.start()
        image = self.image_url(self.request(ref.page_url))

        def failure(*_):
            raise RuntimeError("synthetic-private-upstream-secret")

        self.backend.hook = failure
        self.clock.advance(3)
        self.assert_terminal_page(self.request(ref.page_url))
        self.assert_no_image(image)
        self.backend.hook = None
        self.clock.advance(3)
        self.assert_terminal_page(self.request(ref.page_url))
        self.assertEqual(len(self.backend.calls), 2)

    def test_invalid_image_bytes_are_terminal_without_exposing_upstream_text(self):
        ref = self.start()
        self.backend.data = b"<html>synthetic-private-upstream-secret</html>"
        self.assert_terminal_page(self.request(ref.page_url))
        self.clock.advance(3)
        self.assert_terminal_page(self.request(ref.page_url + "/text"))
        self.assertEqual(len(self.backend.calls), 1)

    def test_stats_are_only_safe_aggregate_counts(self):
        ref = self.start()
        self.request(ref.page_url)
        value = self.app.stats()
        self.assertEqual(set(value), {"sessions", "frames", "frame_bytes"})
        self.assertEqual(value["sessions"], 1)
        self.assertEqual(value["frames"], 1)
        self.assertGreater(value["frame_bytes"], 0)
        self.assertNotIn(ref.session_id, json.dumps(value))
        self.assertNotIn("front", json.dumps(value))

    def test_concurrent_polls_do_not_accumulate_or_overlap_same_phone_fetches(self):
        ref = self.start()
        entered, release = self.block_backend()
        thread, results, errors = self.spawn(lambda: self.request(ref.page_url))
        try:
            self.assertTrue(entered.wait(timeout=2))
            for _ in range(5):
                self.assertEqual(self.request(ref.page_url).status, 200)
            self.assertEqual(len(self.backend.calls), 1)
        finally:
            release.set()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(len(results), 1)
        self.assertEqual(self.backend.maximum["201"], 1)
        self.request(ref.page_url)
        self.assertEqual(len(self.backend.calls), 1)

    def test_concurrent_page_and_image_serialize_phone_observation_without_self_preemption(self):
        ref = self.start()
        image = self.image_url(self.request(ref.page_url))
        entered, release = threading.Event(), threading.Event()
        second_authorized, contention = threading.Event(), threading.Event()
        reservation = threading.Lock()
        pause_once = [True]
        second_ident = [None]
        original_authorize = self.app.authorize

        def authorize(phone, action, context):
            if threading.get_ident() == second_ident[0]:
                second_authorized.set()
            return original_authorize(phone, action, context)

        def observe(phone, context):
            # Model the host's nonblocking physical-phone reservation. Parallel
            # observer calls would return unknown even though no call exists.
            if not reservation.acquire(blocking=False):
                contention.set()
                return preview.PhoneObservation(self.clock.now, {}, "unknown")
            try:
                if pause_once[0]:
                    pause_once[0] = False
                    entered.set()
                    if not release.wait(timeout=5):
                        raise AssertionError("Synthetic observation was not released")
                return self.observe(phone, context)
            finally:
                reservation.release()

        self.app.authorize = authorize
        self.app.observe = observe
        first, page_responses, page_errors = self.spawn(lambda: self.request(ref.page_url))
        second = None
        try:
            self.assertTrue(entered.wait(timeout=2))

            def request_image():
                second_ident[0] = threading.get_ident()
                return self.request(image)

            second, image_responses, image_errors = self.spawn(request_image)
            self.assertTrue(second_authorized.wait(timeout=2))
            # Once authorized, a competing request must wait for observation,
            # not finish early with a false unknown/focus-loss response.
            second.join(timeout=.1)
            self.assertTrue(second.is_alive())
            self.assertFalse(contention.is_set())
            self.clock.advance(1)
        finally:
            release.set()
            first.join(timeout=3)
            if second:
                second.join(timeout=3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertFalse(page_errors)
        self.assertFalse(image_errors)
        self.assertEqual([item.status for item in page_responses], [200])
        self.assertEqual([item.status for item in image_responses], [200])
        self.assertFalse(contention.is_set())
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.request(image).status, 200)
        self.assertEqual(self.request(ref.page_url).status, 200)

    def test_observation_mutex_wait_is_bounded_and_close_after_acquire_skips_observer(self):
        class RecordingLock:
            def __init__(self, after_acquire=None):
                self.timeouts = []
                self.releases = 0
                self.after_acquire = after_acquire

            def acquire(self, *, timeout):
                self.timeouts.append(timeout)
                if self.after_acquire:
                    self.after_acquire()
                    return True
                return False

            def release(self):
                self.releases += 1

        for elapsed, expected_wait in ((0, 3.0), (4, 1.0), (5, 0.0)):
            with self.subTest(elapsed=elapsed):
                self.app = self.make_app(ttl_seconds=5)
                self.start()
                self.clock.advance(elapsed)
                observed = []
                self.app.observe = lambda *_: observed.append(True)
                lock = RecordingLock()
                self.app._observation_locks["201"] = lock
                self.assertEqual(self.app._focus("201", self.context), "unknown")
                self.assertEqual(lock.timeouts, [expected_wait])
                self.assertEqual(lock.releases, 0)
                self.assertEqual(observed, [])

        self.app = self.make_app()
        observed = []
        self.app.observe = lambda *_: observed.append(True)
        lock = RecordingLock(after_acquire=self.app.close)
        self.app._observation_locks["201"] = lock
        self.assertEqual(self.app._focus("201", self.context), "unknown")
        self.assertEqual(lock.timeouts, [3.0])
        self.assertEqual(lock.releases, 1)
        self.assertEqual(observed, [])

    def test_slow_camera_does_not_block_other_physical_phone(self):
        first = self.start()
        second = self.start(phone="203")
        entered, release = self.block_backend()
        worker, _, errors = self.spawn(lambda: self.request(first.page_url))
        other = None
        try:
            self.assertTrue(entered.wait(timeout=2))
            other, responses, other_errors = self.spawn(
                lambda: self.request(second.page_url, phone="203"))
            other.join(timeout=2)
            self.assertFalse(other.is_alive(), "An unrelated phone must not share the fetch lock")
            self.assertFalse(other_errors)
            self.assertEqual(len(responses), 1)
            self.assertEqual(responses[0].status, 200)
        finally:
            release.set()
            worker.join(timeout=3)
            if other:
                other.join(timeout=3)
        self.assertFalse(errors)

    def test_stop_while_fetching_is_responsive_and_discards_late_image(self):
        ref = self.start()
        entered, release = self.block_backend()
        worker, _, errors = self.spawn(lambda: self.request(ref.page_url))
        stopper = None
        try:
            self.assertTrue(entered.wait(timeout=2))
            stopper, responses, stop_errors = self.spawn(lambda: self.request(ref.stop_url))
            stopper.join(timeout=2)
            self.assertFalse(stopper.is_alive(), "Stop must not wait for image network I/O")
            self.assertFalse(stop_errors)
            self.assert_terminal_page(responses[0])
        finally:
            release.set()
            worker.join(timeout=3)
            if stopper:
                stopper.join(timeout=3)
        self.assertFalse(errors)
        self.assert_terminal_page(self.request(ref.page_url))
        self.assertEqual(len(self.backend.calls), 1)

    def test_focus_loss_during_fetch_discards_image_and_never_auto_resumes(self):
        ref = self.start()
        entered, release = self.block_backend()
        worker, responses, errors = self.spawn(lambda: self.request(ref.page_url))
        try:
            self.assertTrue(entered.wait(timeout=2))
            self.observations["201"] = preview.PhoneObservation(
                self.clock.now, {"201": "idle", "202": "ringing"}, "idle")
        finally:
            release.set()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assert_yielded(responses[0])
        self.observations.clear()
        self.assert_yielded(self.request(ref.page_url))
        self.assertEqual(self.app.stats()["frames"], 0)
        self.assertEqual(len(self.backend.calls), 1)

    def test_expiry_during_fetch_never_retains_or_serves_late_image(self):
        self.app = self.make_app(ttl_seconds=5)
        ref = self.start()
        entered, release = self.block_backend()
        worker, responses, errors = self.spawn(lambda: self.request(ref.page_url))
        try:
            self.assertTrue(entered.wait(timeout=2))
            self.clock.advance(5)
        finally:
            release.set()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        for response in responses:
            self.assert_yielded(response)
        for error in errors:
            self.assertIsInstance(error, preview.DisplayError)
            self.assertEqual(error.status, 404)
        self.assertEqual(self.app.stats(), {"sessions": 0, "frames": 0, "frame_bytes": 0})

    def test_expiry_during_prefetch_observation_never_starts_camera_read(self):
        self.app = self.make_app(ttl_seconds=5)
        ref = self.start()
        observations = [0]

        def slow_prefetch_observation(phone, context):
            observations[0] += 1
            if observations[0] == 2:
                # Initial handle focus is quick. The second observation, just
                # before the backend read, exhausts this session's lifetime.
                self.clock.advance(5)
            return self.observe(phone, context)

        self.app.observe = slow_prefetch_observation
        with self.assertRaises(preview.DisplayError) as caught:
            self.request(ref.page_url)
        self.assertEqual(caught.exception.status, 404)
        self.assertGreaterEqual(observations[0], 2)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.app.stats(), {"sessions": 0, "frames": 0, "frame_bytes": 0})

    def test_new_session_cannot_overlap_stopped_inflight_fetch(self):
        old = self.start()
        entered, release = self.block_backend()
        worker, _, errors = self.spawn(lambda: self.request(old.page_url))
        try:
            self.assertTrue(entered.wait(timeout=2))
            self.request(old.stop_url)
            with self.assertRaises(preview.DisplayError) as caught:
                self.start()
            self.assertEqual(caught.exception.status, 409)
        finally:
            release.set()
            worker.join(timeout=3)
        self.assertFalse(errors)
        current = self.start()
        self.assertNotEqual(old.session_id, current.session_id)
        self.request(current.page_url)
        self.assertEqual(self.backend.maximum["201"], 1)

    def test_close_revokes_urls_and_discards_inflight_image(self):
        ref = self.start()
        entered, release = self.block_backend()
        worker, responses, errors = self.spawn(lambda: self.request(ref.page_url))
        try:
            self.assertTrue(entered.wait(timeout=2))
            self.app.close()
        finally:
            release.set()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        for response in responses:
            self.assert_yielded(response)
        for error in errors:
            self.assertIsInstance(error, preview.DisplayError)
            self.assertIn(error.status, (404, 503))
        with self.assertRaises(preview.DisplayError):
            self.request(ref.page_url)
        self.assertEqual(self.app.stats(), {"sessions": 0, "frames": 0, "frame_bytes": 0})

    def test_slow_acquisition_schedules_next_fetch_from_completion(self):
        ref = self.start()

        def slow(*_):
            self.clock.advance(4)
            return self.backend.data

        self.backend.hook = slow
        page = self.request(ref.page_url)
        stamp = datetime.fromtimestamp(self.clock.wall, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        self.assertIn(stamp, self.xml(self.request(ref.page_url + "/text")).findtext("Text"))
        self.clock.advance(2)
        self.request(ref.page_url)
        self.assertEqual(len(self.backend.calls), 1)
        self.clock.advance(1)
        self.request(ref.page_url)
        self.assertEqual(len(self.backend.calls), 2)

    def test_refused_start_after_observed_focus_loss_latches_existing_preview(self):
        ref = self.start()
        image = self.image_url(self.request(ref.page_url))
        self.observations["201"] = preview.PhoneObservation(
            self.clock.now, {"201": "idle", "202": "ringing"}, "idle")
        with self.assertRaises(preview.DisplayError):
            self.start()
        self.observations.clear()
        self.assert_yielded(self.request(ref.page_url))
        self.assert_no_image(image)
        self.assertEqual(len(self.backend.calls), 1)

    def test_final_display_focus_loss_latches_preview_without_later_fetch(self):
        for route in ("page", "image"):
            with self.subTest(route=route):
                self.app = self.make_app()
                ref = self.start()
                image = self.image_url(self.request(ref.page_url))
                calls = len(self.backend.calls)
                original_focus = self.app._display.focus
                lose_once = [True]

                def final_focus(phone, context):
                    if lose_once[0]:
                        lose_once[0] = False
                        self.observations[phone] = preview.PhoneObservation(
                            self.clock.now, {"201": "idle", "202": "ringing"}, "idle")
                        try:
                            return original_focus(phone, context)
                        finally:
                            self.observations.clear()
                    return original_focus(phone, context)

                # Only the composed display sees this late focus transition;
                # all preceding controller observations remain idle.
                self.app._display.focus = final_focus
                self.assert_yielded(self.request(ref.page_url if route == "page" else image))
                self.clock.advance(3)
                self.assert_yielded(self.request(ref.page_url))
                self.assert_no_image(image)
                self.assertEqual(self.app.stats()["frames"], 0)
                self.assertEqual(len(self.backend.calls), calls)

    def test_stop_after_display_read_discards_prepared_page_and_image(self):
        for route in ("page", "image"):
            with self.subTest(route=route):
                self.app = self.make_app()
                ref = self.start()
                image = self.image_url(self.request(ref.page_url))
                calls = len(self.backend.calls)
                original_handle = self.app._display.handle
                prepared = []

                def stop_after_read(*args, **kwargs):
                    response = original_handle(*args, **kwargs)
                    prepared.append(response)
                    # Deterministically interleave a public Stop after display
                    # rendering, before the controller's final response guard.
                    self.request(ref.stop_url)
                    return response

                self.app._display.handle = stop_after_read
                if route == "image":
                    with self.assertRaises(preview.DisplayError) as caught:
                        self.request(image)
                    self.assertEqual(caught.exception.status, 404)
                    self.assertEqual(prepared[0].content_type, "image/png")
                else:
                    response = self.request(ref.page_url)
                    self.assertEqual(self.xml(prepared[0]).tag, "CiscoIPPhoneImageFile")
                    page = self.xml(response)
                    self.assertEqual(page.tag, "CiscoIPPhoneText")
                    self.assertEqual(page.findtext("Title"), "Preview stopped")
                    self.assertIsNone(page.find("URL"))
                    self.assert_terminal_page(response)
                self.assertEqual(len(prepared), 1)
                self.app._display.handle = original_handle
                self.assert_no_image(image)
                self.clock.advance(3)
                self.assert_terminal_page(self.request(ref.page_url))
                self.assertEqual(self.app.stats()["frames"], 0)
                self.assertEqual(len(self.backend.calls), calls)

    def test_final_display_observation_cannot_serve_newly_stale_frame(self):
        for route in ("page", "image"):
            with self.subTest(route=route):
                self.app = self.make_app()
                ref = self.start()
                image = self.image_url(self.request(ref.page_url))
                calls = len(self.backend.calls)
                original_focus = self.app._display.focus
                delay_once = [True]

                def slow_final_focus(phone, context):
                    if delay_once[0]:
                        delay_once[0] = False
                        self.clock.advance(10)
                    return original_focus(phone, context)

                self.app._display.focus = slow_final_focus
                if route == "image":
                    self.assert_no_image(image)
                else:
                    response = self.request(ref.page_url)
                    self.assertEqual(self.xml(response).tag, "CiscoIPPhoneText")
                    self.assertIn(b"expired", response.body)
                    self.assert_terminal_page(response)
                self.assert_no_image(image)
                self.assert_terminal_page(self.request(ref.page_url))
                self.assertEqual(len(self.backend.calls), calls)

    def test_display_start_focus_rejection_latches_previous_preview(self):
        ref = self.start()
        image = self.image_url(self.request(ref.page_url))
        original_focus = self.app._display.focus
        lose_once = [True]

        def late_start_focus(phone, context):
            if lose_once[0]:
                lose_once[0] = False
                self.observations[phone] = preview.PhoneObservation(
                    self.clock.now, {"201": "idle", "202": "ringing"}, "idle")
                try:
                    return original_focus(phone, context)
                finally:
                    self.observations.clear()
            return original_focus(phone, context)

        self.app._display.focus = late_start_focus
        with self.assertRaises(preview.DisplayError) as caught:
            self.start()
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.reason, "phone_focus_unavailable")
        self.clock.advance(3)
        self.assert_yielded(self.request(ref.page_url))
        self.assert_no_image(image)
        self.assertEqual(self.app.stats()["frames"], 0)
        self.assertEqual(len(self.backend.calls), 1)

    def test_authorization_revoked_during_refresh_cannot_resurrect_cached_frame(self):
        ref = self.start()
        old_image = self.image_url(self.request(ref.page_url))
        self.clock.advance(3)

        def revoke(*_):
            self.allowed = False
            return self.backend.data

        self.backend.hook = revoke
        with self.assertRaises(preview.DisplayError) as caught:
            self.request(ref.page_url)
        self.assertEqual(caught.exception.status, 403)
        self.allowed = True
        self.backend.hook = None
        self.assert_no_image(old_image)
        self.assert_terminal_page(self.request(ref.page_url))
        self.assertEqual(len(self.backend.calls), 2)


if __name__ == "__main__":
    unittest.main()
