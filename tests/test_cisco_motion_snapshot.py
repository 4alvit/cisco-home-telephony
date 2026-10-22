"""Offline motion dispatch contracts: synthetic JPEG, fake HA/PBX, local HTTP only."""
import base64
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, HTTPServer

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/cisco_motion_snapshot.py'
spec = importlib.util.spec_from_file_location('motion_snapshot', SOURCE)
motion = importlib.util.module_from_spec(spec)
spec.loader.exec_module(motion)
# Includes an EXIF-like embedded EOI that must not end the outer JPEG.
JPEG = b'\xff\xd8\xff\xe1\x00\x08A\xff\xd9BCD\xff\xda\x00\x02abc\xff\x00d\xff\xd0e\xff\xd9'


class Response(io.BytesIO):
    def __init__(self, data, *, chunk=16384, content_type='multipart/x-mixed-replace; boundary=frame'):
        super().__init__(data)
        self.chunk = chunk
        self.status = 200
        self.headers = {'Content-Type': content_type}

    def read1(self, size=-1):
        return self.read(min(size, self.chunk))


def inventory(*ids):
    return {'ami_fresh': True, 'observed_at': time.time(), 'phones': [
        {'id': n, 'lines': [n, str(int(n) + 1)], 'online': True, 'dnd': False,
         'dnd_pending': False, 'channels': [], 'alert': None, 'capabilities': ['image']}
        for n in ids or ('201', '203')]}


class FakeHTTP:
    def __init__(self, data=None, error=None):
        self.data = data or inventory()
        self.error = error
        self.posts = []
        self.streams = []
        self.reads = 0
        self.on_read = None
        self.on_post = None
        self.frame = JPEG

    def json(self, target, path, *, body=None):
        if body is None:
            self.reads += 1
            if self.on_read:
                self.on_read(self)
            value = copy.deepcopy(self.data)
            value['observed_at'] = time.time()
            return value
        self.posts.append((path, body))
        if self.on_post:
            self.on_post(self)
        if self.error:
            raise motion.Failure(self.error)
        return {'status': 'accepted'}

    def open(self, target, path, **kwargs):
        value = Response(b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + self.frame
                         + b'\r\n--frame' + self.frame, chunk=3)
        self.streams.append(value)
        return value


class JPEGTests(unittest.TestCase):
    def test_fragmented_mjpeg_returns_first_complete_outer_image(self):
        for chunk in (1, 2, 7, 16384):
            with self.subTest(chunk=chunk):
                self.assertEqual(motion.first_jpeg(Response(b'header\r\n' + JPEG + JPEG, chunk=chunk)), JPEG)

    def test_missing_invalid_type_and_truncated_frame(self):
        for data, kind, expected in [(b'no frames', 'multipart/x-mixed-replace', 'camera_frame_missing'),
                                     (JPEG[:-1], 'image/jpeg', 'camera_frame_missing'),
                                     (JPEG, 'text/html', 'unexpected_camera_type'),
                                     (b'\xff\xd8\xff\xd9', 'image/jpeg', 'invalid_jpeg')]:
            with self.subTest(expected=expected), self.assertRaises(motion.Failure) as ctx:
                motion.first_jpeg(Response(data, content_type=kind))
            self.assertEqual(ctx.exception.reason, expected)

    def test_frame_and_total_stream_have_separate_limits(self):
        with mock.patch.object(motion, 'MAX_FRAME', 20):
            with self.assertRaisesRegex(motion.Failure, 'camera_frame_too_large'):
                motion.first_jpeg(Response(JPEG))
        with mock.patch.object(motion, 'MAX_STREAM', 20):
            with self.assertRaisesRegex(motion.Failure, 'camera_stream_too_large'):
                motion.first_jpeg(Response(b'A' * 32, chunk=7))


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {'gateway': {'base_url': 'http://127.0.0.1', 'token': 'x' * 32},
                       'home_assistant': {'base_url': 'http://127.0.0.1', 'token': 'y' * 32},
                       'cameras': {'front': {'entity_id': 'camera.front_notification', 'label': 'Front'}},
                       'destinations': ['201', '202', '203'], 'state_directory': self.root / 'state',
                       'cooldown_seconds': 60, 'max_event_age_seconds': 90}

    def tearDown(self):
        self.temp.cleanup()

    def run_event(self, http=None, event='event_0001', **kw):
        return motion.dispatch(self.config, 'front', event, time.time(), http=http or FakeHTTP(), **kw)

    def test_aliases_dedup_and_frame_closed_before_posts(self):
        http = FakeHTTP()
        result = self.run_event(http)
        self.assertEqual(result['accepted'], ['201', '203'])
        self.assertEqual(result['counts']['accepted'], 2)
        self.assertEqual(len(http.streams), 1)
        self.assertTrue(http.streams[0].closed)
        self.assertEqual(len(http.posts), 2)
        self.assertIn(' | Event ', http.posts[0][1]['title'])
        self.assertLessEqual(len(http.posts[0][1]['title']), 32)
        self.assertEqual(http.posts[0][1]['idle_close_seconds'], 45)
        self.assertFalse(result['display_verified'])

    def test_event_title_fits_cisco_schema_and_preserves_complete_utc_event_time(self):
        event_at = 1792628291
        suffix = ' | Event 10-22 00:18:11Z'
        for label in ('Front', 'A' * 13, 'B' * 48):
            with self.subTest(label_length=len(label)):
                title = motion.event_title(label, event_at)
                self.assertTrue(title.endswith(suffix))
                self.assertEqual(title, label[:32 - len(suffix)] + suffix)
                self.assertEqual(len(title), min(len(label) + len(suffix), 32))

    def test_busy_offline_dnd_and_alert_no_camera_or_state(self):
        for field, value in [('online', False), ('dnd', True), ('dnd_pending', True),
                             ('channels', [{'state': 'Up'}]), ('alert', {'state': 'ringing'}),
                             ('capabilities', [])]:
            with self.subTest(field=field):
                http = FakeHTTP(inventory('201'))
                http.data['phones'][0][field] = value
                self.config['destinations'] = ['201']
                result = self.run_event(http)
                self.assertEqual(len(result['skipped']), 1)
                self.assertEqual(http.streams, [])
                self.assertFalse((self.root / 'state').exists())

    def test_no_destinations_and_expired_event_are_inert(self):
        http = FakeHTTP()
        self.config['destinations'] = []
        self.assertEqual(self.run_event(http)['reason'], 'no_destinations')
        self.assertEqual(http.reads, 0)
        self.config['destinations'] = ['201']
        result = motion.dispatch(self.config, 'front', 'event_0001', time.time() - 500, http=http)
        self.assertEqual(result['reason'], 'event_stale_or_clock_skew')
        self.assertEqual(http.reads, 0)

    def test_dryrun_reads_only_no_stream_post_or_state_creation(self):
        http = FakeHTTP()
        result = self.run_event(http, dry_run=True)
        self.assertEqual(result['eligible'], ['201', '203'])
        self.assertEqual(http.streams, [])
        self.assertEqual(http.posts, [])
        self.assertFalse((self.root / 'state').exists())

    def test_duplicate_event_and_cross_camera_cooldown_no_fetch(self):
        self.run_event()
        http = FakeHTTP()
        result = self.run_event(http)
        self.assertEqual({x['reason'] for x in result['skipped']}, {'duplicate_event'})
        self.assertEqual(http.streams, [])
        self.config['cameras']['side'] = self.config['cameras']['front']
        result = motion.dispatch(self.config, 'side', 'event_0002', time.time(), http=http)
        self.assertEqual({x['reason'] for x in result['skipped']}, {'cooldown'})
        self.assertEqual(http.streams, [])

    def test_distinct_event_after_cooldown_sends_new_frame_without_control_action(self):
        self.config['destinations'] = ['201', '202']
        http = FakeHTTP(inventory('201'))
        later_frame = JPEG.replace(b'abc', b'xyz')
        with mock.patch.object(motion.time, 'time', return_value=1792628291) as clock:
            first = self.run_event(http, event='event_first')
            clock.return_value += 60
            http.frame = later_frame
            second = self.run_event(http, event='event_second')

        self.assertEqual(first['accepted'], ['201'])
        self.assertEqual(second['accepted'], ['201'])
        self.assertEqual(len(http.streams), 2)
        # Only image requests are allowed: no Exit, key, call or speaker action.
        self.assertEqual([path for path, _ in http.posts], ['/v1/phones/201/image'] * 2)
        bodies = [body for _, body in http.posts]
        self.assertEqual([body['idle_close_seconds'] for body in bodies], [45, 45])
        self.assertEqual([base64.b64decode(body['image_base64']) for body in bodies],
                         [JPEG, later_frame])
        self.assertNotEqual(bodies[0]['idempotency_key'], bodies[1]['idempotency_key'])
        self.assertNotEqual(bodies[0]['title'], bodies[1]['title'])

    def test_cooldown_event_is_dropped_not_replayed_and_original_stays_deduplicated(self):
        self.config['destinations'] = ['201']
        http = FakeHTTP(inventory('201'))
        started = 1792628291
        with mock.patch.object(motion.time, 'time', return_value=started) as clock:
            self.assertEqual(self.run_event(http, event='event_first')['accepted'], ['201'])
            clock.return_value = started + 30
            dropped = self.run_event(http, event='event_dropped')
            self.assertEqual(dropped['skipped'], [{'phone': '201', 'reason': 'cooldown'}])
            self.assertEqual((len(http.streams), len(http.posts)), (1, 1))

            clock.return_value = started + 60
            # Both retries retain their original, still-fresh event timestamps.
            for event_id, event_at in [('event_dropped', started + 30), ('event_first', started)]:
                with self.subTest(event_id=event_id):
                    retry = motion.dispatch(self.config, 'front', event_id, event_at, http=http)
                    self.assertEqual(retry['skipped'], [{'phone': '201', 'reason': 'duplicate_event'}])
                    self.assertEqual((len(http.streams), len(http.posts)), (1, 1))

            # Suppression does not defer the old frame or extend phone cooldown.
            next_event = self.run_event(http, event='event_next')
            self.assertEqual(next_event['accepted'], ['201'])
            self.assertEqual((len(http.streams), len(http.posts)), (2, 2))

    def test_voice_guard_refusal_does_not_fallback_or_retry(self):
        http = FakeHTTP(error='http_busy')
        self.config['destinations'] = ['201']
        result = self.run_event(http)
        self.assertEqual(result['counts']['skipped'], 1)
        self.assertEqual(result['skipped'][0]['reason'], 'http_busy')
        self.assertEqual(len(http.posts), 1)
        self.assertEqual(http.posts[0][0], '/v1/phones/201/image')
        again = FakeHTTP()
        self.run_event(again)
        self.assertEqual(again.posts, [])

    def test_auth_refusal_is_failed_not_retried(self):
        self.config['destinations'] = ['201']
        http = FakeHTTP(error='http_refused')
        result = self.run_event(http)
        self.assertEqual(result['counts']['failed'], 1)
        self.assertEqual(len(http.posts), 1)

    def test_ambiguous_post_claim_persists_and_no_frame_in_ledger(self):
        self.config['destinations'] = ['201']
        result = self.run_event(FakeHTTP(error='network_unknown'))
        self.assertEqual(result['counts']['unknown'], 1)
        db = sqlite3.connect(self.root / 'state/claims.sqlite3')
        self.assertEqual(db.execute('select count(*) from events').fetchone()[0], 1)
        self.assertEqual(db.execute('select count(*) from phones').fetchone()[0], 1)
        db.close()
        self.assertNotIn(JPEG, (self.root / 'state/claims.sqlite3').read_bytes())

    def test_busy_recheck_after_camera_acquisition_skips_post(self):
        http = FakeHTTP()
        def change(obj):
            if obj.reads == 2:
                for row in obj.data['phones']:
                    row['channels'] = [{'state': 'Up'}]
        http.on_read = change
        result = self.run_event(http)
        self.assertEqual({x['reason'] for x in result['skipped']}, {'busy'})
        self.assertEqual(http.posts, [])

    def test_voice_configured_idle_phone_skipped_before_fetch(self):
        http = FakeHTTP(inventory('201'))
        http.data['phones'][0]['capabilities'].append('voice')
        self.config['destinations'] = ['201']
        result = self.run_event(http)
        self.assertEqual(result['skipped'], [{'phone': '201', 'reason': 'voice_configured'}])
        self.assertEqual(http.streams, [])
        self.assertEqual(http.posts, [])

    def test_voice_capability_appearing_on_recheck_skips_post(self):
        http = FakeHTTP(inventory('201'))
        self.config['destinations'] = ['201']
        def change(obj):
            if obj.reads == 2:
                obj.data['phones'][0]['capabilities'].append('voice')
        http.on_read = change
        result = self.run_event(http)
        self.assertEqual(result['skipped'], [{'phone': '201', 'reason': 'voice_configured'}])
        self.assertEqual(len(http.streams), 1)
        self.assertEqual(http.posts, [])

    def test_later_target_dnd_change_during_previous_post_is_respected(self):
        http = FakeHTTP()
        def change(obj):
            obj.data['phones'][1]['dnd'] = True
        http.on_post = change
        result = self.run_event(http)
        self.assertEqual(result['accepted'], ['201'])
        self.assertEqual(result['skipped'], [{'phone': '203', 'reason': 'dnd'}])
        self.assertEqual(len(http.posts), 1)

    def test_ended_alert_does_not_block_delivery(self):
        http = FakeHTTP(inventory('201'))
        self.config['destinations'] = ['201']
        http.data['phones'][0]['alert'] = {'state': 'ended'}
        self.assertEqual(self.run_event(http)['accepted'], ['201'])

    def test_disappearing_later_phone_is_skipped(self):
        http = FakeHTTP()
        http.on_post = lambda obj: obj.data['phones'].pop()
        result = self.run_event(http)
        self.assertEqual(result['accepted'], ['201'])
        self.assertEqual(result['skipped'], [{'phone': '203', 'reason': 'no_longer_in_inventory'}])

    def test_unknown_inventory_boolean_clock_and_duplicate_mapping_refused(self):
        for change in [lambda d: d.update(ami_fresh=1),
                       lambda d: d.update(observed_at=time.time() + 10),
                       lambda d: d['phones'][0].update(online='true'),
                       lambda d: d['phones'][1].update(lines=['201'])]:
            value = inventory()
            change(value)
            with self.assertRaises(motion.Failure):
                motion.select_phones(value, ['201'], time.time())

    def test_explicit_all_bounded_to_ten_physical_phones(self):
        value = inventory(*(str(x) for x in range(201, 222, 2)))
        with self.assertRaisesRegex(motion.Failure, 'too_many_physical_phones'):
            motion.select_phones(value, ['*'], time.time())

    def test_concurrent_process_event_claim_and_phone_cooldown(self):
        # Separate interpreters exercise SQLite locking, not an in-process mock.
        path = self.root / 'state'
        ledger = motion.Claims(path)
        ledger.close()
        code = ('import sys,time;sys.path.insert(0,sys.argv[1]);import cisco_motion_snapshot as m;'
                'd=m.Claims(sys.argv[2]);p,s=d.reserve("event",["201"],1000,60);'
                'print(len(p));d.close()')
        workers = [subprocess.Popen([sys.executable, '-c', code, str(SOURCE.parent), str(path)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        outputs = []
        for worker in workers:
            out, err = worker.communicate(timeout=5)
            self.assertEqual(worker.returncode, 0, err)
            outputs.append(out.strip())
        self.assertEqual(sorted(outputs), [b'0', b'1'])
        first, second = motion.Claims(path), motion.Claims(path)
        self.assertTrue(first.claim_phone('201', 1000, 60))
        self.assertFalse(second.claim_phone('201', 1001, 60))
        self.assertTrue(second.claim_phone('201', 1060, 60))
        first.close(); second.close()


class TransportTests(unittest.TestCase):
    def test_refused_redirect_closes_original_response(self):
        response = Response(b'private upstream body')
        with self.assertRaisesRegex(motion.Failure, '^redirect_refused$'):
            motion.NoRedirect().redirect_request(None, response, 302, '', {}, 'http://elsewhere')
        self.assertTrue(response.closed)

    def test_private_config_and_example_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sample = json.loads((SOURCE.parents[1] / 'examples/motion-notifications.example.json').read_text())
            for name in ('gateway-token.txt', 'ha-token.txt'):
                path = root / name
                path.write_text('a' * 40)
                path.chmod(0o600)
            config = root / 'config.json'
            config.write_text(json.dumps(sample))
            config.chmod(0o600)
            self.assertEqual(motion.load_config(config)['state_directory'], root / 'state')
            sample['cooldown_seconds'] = 59
            config.write_text(json.dumps(sample))
            with self.assertRaisesRegex(motion.Failure, 'invalid_config'):
                motion.load_config(config)
            config.chmod(0o644)
            with self.assertRaisesRegex(motion.Failure, 'private_file_permissions_or_size'):
                motion.load_config(config)

    def test_redirect_no_token_forwarding_and_environment_proxy_ignored(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(self.path)
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', '/secret-target')
                    self.end_headers()
                else:
                    self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
            def log_message(self, *args):
                pass
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.dict(os.environ, {'http_proxy': 'http://127.0.0.1:1', 'NO_PROXY': ''}):
                http = motion.HTTP()
                target = {'base_url': 'http://127.0.0.1:' + str(server.server_port), 'token': 'PRIVATE_TOKEN'}
                self.assertEqual(http.json(target, '/ok'), {})
                with self.assertRaisesRegex(motion.Failure, '^redirect_refused$'):
                    http.json(target, '/redirect')
            self.assertEqual(calls, ['/ok', '/redirect'])
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_slow_read_wall_deadline_closes_response(self):
        response = Response(JPEG)
        class SlowHTTP:
            def open(self, *args, **kwargs):
                response.read1 = lambda n: (time.sleep(.2), b'')[1]
                return response
        with mock.patch.object(motion, 'SOURCE_SECONDS', .03):
            with self.assertRaisesRegex(motion.Failure, 'deadline_exceeded'):
                motion.acquire_frame(SlowHTTP(), {'home_assistant': {}}, {'entity_id': 'camera.test'})
        self.assertTrue(response.closed)

    def test_outer_supervisor_terminates_and_reaps_stalled_worker(self):
        real_popen = subprocess.Popen
        processes = []
        def stalled(*args, **kwargs):
            child = real_popen([sys.executable, '-c', 'import time;time.sleep(30)'], **kwargs)
            processes.append(child)
            return child
        with mock.patch.object(motion.subprocess, 'Popen', stalled):
            result = motion.supervise([], timeout=.04)
        self.assertEqual(result['reason'], 'worker_deadline_exceeded')
        self.assertFalse(result['retry_allowed'])
        self.assertIsNotNone(processes[0].poll())

    def test_supervisor_sigterm_and_sigint_reap_owned_child(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signum=signum), tempfile.TemporaryDirectory() as folder:
                child_file = Path(folder) / 'child.pid'
                # Send cancellation in the creation window before Popen returns
                # to supervise(); its signal mask must preserve the owned handle.
                program = '''
import json, pathlib, subprocess, sys, time
sys.path.insert(0, sys.argv[1])
import cisco_motion_snapshot as motion
real_popen = subprocess.Popen
def create(*args, **kwargs):
    p = real_popen([sys.executable, '-c', 'import time;time.sleep(30)'], **kwargs)
    pathlib.Path(sys.argv[2]).write_text(str(p.pid))
    time.sleep(.2)
    return p
motion.subprocess.Popen = create
print(json.dumps(motion.supervise([], timeout=20)))
'''
                parent = subprocess.Popen([sys.executable, '-c', program,
                                           str(SOURCE.parent), str(child_file)],
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 3
                    while not child_file.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(child_file.exists())
                    child_pid = int(child_file.read_text())
                    parent.send_signal(signum)
                    out, err = parent.communicate(timeout=4)
                    self.assertEqual(parent.returncode, 0, err)
                    self.assertEqual(json.loads(out)['reason'], 'supervisor_cancelled')
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child_pid, 0)
                finally:
                    if parent.poll() is None:
                        parent.kill(); parent.communicate(timeout=2)

    def test_cli_failure_does_not_expose_private_path(self):
        result = subprocess.run([sys.executable, str(SOURCE), '--config', '/PRIVATE_URL_AND_TOKEN',
                                 '--camera', 'front', '--event-id', 'event_0001',
                                 '--event-at', str(time.time())], capture_output=True, timeout=5)
        value = json.loads(result.stdout)
        self.assertEqual(value['reason'], 'private_file_unavailable')
        self.assertNotIn(b'PRIVATE_URL_AND_TOKEN', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
