#!/usr/bin/env python3
"""Send one HA notification-camera JPEG to available Cisco phones, without calls.

Standalone stdlib helper. Results are delivery requests, never display proof.
Only metadata is retained. No automatic retry follows an ambiguous POST.
"""
import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from urllib import error, parse, request

MAX_FRAME = 2 * 1024 * 1024
MAX_STREAM = 4 * 1024 * 1024
MAX_JSON = 256 * 1024
TOTAL_SECONDS = 48
SOURCE_SECONDS = 15
API_SECONDS = 5
SAFE_ID = re.compile(r'[A-Za-z0-9_-]{8,96}\Z')
LINE = re.compile(r'2[0-9]{2}\Z')


class Failure(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def require(condition, reason):
    if not condition:
        raise Failure(reason)


def object_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def read_private(path, limit):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and not info.st_mode & 0o077 and info.st_size <= limit,
                    'private_file_permissions_or_size')
            data = stream.read(limit + 1)
            require(len(data) <= limit, 'private_file_permissions_or_size')
            return data
    except OSError:
        raise Failure('private_file_unavailable') from None


def base_url(value):
    require(isinstance(value, str) and len(value) <= 2048, 'invalid_base_url')
    try:
        url = parse.urlsplit(value)
        require(url.scheme in ('http', 'https') and url.hostname and not url.username
                and not url.password and not url.query and not url.fragment
                and url.path in ('', '/'), 'invalid_base_url')
        _ = url.port
    except ValueError:
        raise Failure('invalid_base_url') from None
    return value.rstrip('/')


def load_config(path):
    path = Path(path).expanduser().absolute()
    try:
        config = json.loads(read_private(path, 32768), object_pairs_hook=object_pairs)
        require(isinstance(config, dict) and set(config) <= {
            'version', 'gateway', 'home_assistant', 'cameras', 'destinations',
            'state_directory', 'cooldown_seconds', 'max_event_age_seconds'}, 'invalid_config')
        require(type(config.get('version')) is int and config['version'] == 1, 'invalid_config')
        for name in ('gateway', 'home_assistant'):
            item = config[name]
            require(isinstance(item, dict) and set(item) == {'base_url', 'token_file'}, 'invalid_config')
            item['base_url'] = base_url(item['base_url'])
            require(isinstance(item['token_file'], str), 'invalid_config')
            token_path = Path(item['token_file']).expanduser()
            if not token_path.is_absolute():
                token_path = path.parent / token_path
            token = read_private(token_path, 4096).decode('ascii').strip()
            require(bool(re.fullmatch(r'[A-Za-z0-9_.-]{32,4096}', token)), 'invalid_token')
            item['token'] = token
        cameras = config['cameras']
        require(isinstance(cameras, dict) and 1 <= len(cameras) <= 16, 'invalid_cameras')
        for key, item in cameras.items():
            require(bool(re.fullmatch(r'[a-z0-9_]{1,48}', key)) and isinstance(item, dict)
                    and set(item) == {'entity_id', 'label'}, 'invalid_cameras')
            require(isinstance(item['entity_id'], str) and bool(re.fullmatch(
                r'camera\.[a-z0-9_]{1,128}', item['entity_id'])), 'invalid_cameras')
            require(isinstance(item['label'], str) and 1 <= len(item['label']) <= 48
                    and all(ord(c) >= 32 for c in item['label']), 'invalid_cameras')
        targets = config['destinations']
        require(isinstance(targets, list) and len(targets) <= 20
                and (targets == ['*'] or all(isinstance(x, str) and LINE.fullmatch(x) for x in targets)),
                'invalid_destinations')
        for key, default, lower, upper in [('cooldown_seconds', 60, 60, 86400),
                                          ('max_event_age_seconds', 90, 5, 120)]:
            value = config.setdefault(key, default)
            require(type(value) is int and lower <= value <= upper, 'invalid_config')
        require(isinstance(config['state_directory'], str) and config['state_directory'], 'invalid_config')
        state = Path(config['state_directory']).expanduser()
        config['state_directory'] = state if state.is_absolute() else path.parent / state
        return config
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise Failure('invalid_config') from None


@contextmanager
def time_limit(seconds):
    """Main-thread POSIX wall limit, including slow reads and DNS resolution."""
    require(seconds > 0, 'deadline_exceeded')
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def expired(_signum, _frame):
        raise Failure('deadline_exceeded')

    limit = min(seconds, previous_timer[0]) if previous_timer[0] > 0 else seconds
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, limit)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL,
                             max(.000001, previous_timer[0] - (time.monotonic() - started)),
                             previous_timer[1])


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise Failure('redirect_refused')


class HTTP:
    def __init__(self, opener=None):
        # Ignore HTTP(S)_PROXY and netrc; credentials go to configured origins only.
        self.opener = opener or request.build_opener(request.ProxyHandler({}), NoRedirect())

    def open(self, target, path, *, data=None, timeout=API_SECONDS):
        headers = {'Authorization': 'Bearer ' + target['token'],
                   'Accept': ('multipart/x-mixed-replace,image/jpeg' if
                              path.startswith('/api/camera_proxy_stream/') else 'application/json'),
                   'User-Agent': 'cisco-motion-snapshot/1'}
        if data is not None:
            headers['Content-Type'] = 'application/json'
        req = request.Request(target['base_url'] + path, data=data, headers=headers)
        return self.opener.open(req, timeout=timeout)

    def json(self, target, path, *, body=None):
        data = None if body is None else json.dumps(body, separators=(',', ':')).encode()
        try:
            with time_limit(API_SECONDS), self.open(target, path, data=data) as response:
                require(response.status == 200, 'unexpected_http_status')
                raw = response.read(MAX_JSON + 1)
                require(len(raw) <= MAX_JSON, 'response_too_large')
                result = json.loads(raw, object_pairs_hook=object_pairs)
                require(isinstance(result, dict), 'invalid_response')
                return result
        except error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise Failure('http_busy' if code == 409 else
                          'http_refused' if 400 <= code < 500 else 'http_unknown') from None
        except (OSError, error.URLError):
            raise Failure('network_unknown') from None
        except (ValueError, UnicodeError):
            raise Failure('invalid_response') from None


def jpeg_end(data):
    """Find the first JPEG end, skipping segment payloads and escaped scan bytes."""
    require(data[:2] == b'\xff\xd8', 'invalid_jpeg')
    position, scan, saw_scan = 2, False, False
    while position < len(data):
        if scan:
            marker = data.find(b'\xff', position)
            if marker < 0:
                return None
            position = marker
        require(data[position] == 255, 'invalid_jpeg')
        while position < len(data) and data[position] == 255:
            position += 1
        if position == len(data):
            return None
        code = data[position]
        position += 1
        if scan and (code == 0 or 0xD0 <= code <= 0xD7):
            continue
        scan = False
        if code == 0xD9:
            require(saw_scan, 'invalid_jpeg')
            return position
        require(code not in (0, 0xD8) and not 0xD0 <= code <= 0xD7, 'invalid_jpeg')
        if code == 1:
            continue
        if position + 2 > len(data):
            return None
        length = int.from_bytes(data[position:position + 2], 'big')
        require(length >= 2, 'invalid_jpeg')
        if position + length > len(data):
            return None
        position += length
        if code == 0xDA:
            scan = saw_scan = True
    return None


def first_jpeg(response):
    content_type = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
    require(content_type in ('multipart/x-mixed-replace', 'image/jpeg'), 'unexpected_camera_type')
    total, buffer, started = 0, bytearray(), False
    read = getattr(response, 'read1', response.read)
    while True:
        chunk = read(16384)
        require(bool(chunk), 'camera_frame_missing')
        total += len(chunk)
        require(total <= MAX_STREAM, 'camera_stream_too_large')
        buffer.extend(chunk)
        if not started:
            offset = buffer.find(b'\xff\xd8')
            if offset < 0:
                buffer = buffer[-1:]
                continue
            buffer = buffer[offset:]
            started = True
        end = jpeg_end(buffer)
        if end is not None:
            require(end <= MAX_FRAME, 'camera_frame_too_large')
            return bytes(buffer[:end])
        require(len(buffer) <= MAX_FRAME, 'camera_frame_too_large')


def acquire_frame(http, config, camera):
    try:
        with time_limit(SOURCE_SECONDS), http.open(config['home_assistant'],
                '/api/camera_proxy_stream/' + camera['entity_id'], timeout=SOURCE_SECONDS) as response:
            require(response.status == 200, 'unexpected_camera_status')
            return first_jpeg(response)
    except error.HTTPError as exc:
        exc.close()
        raise Failure('camera_http_refused') from None
    except (OSError, error.URLError):
        raise Failure('camera_network_failed') from None


def select_phones(inventory, destinations, now):
    require(inventory.get('ami_fresh') is True, 'inventory_not_fresh')
    observed = inventory.get('observed_at')
    require(type(observed) in (float, int) and math.isfinite(observed)
            and 0 <= now - observed <= 5, 'inventory_stale_or_clock_skew')
    phones = inventory.get('phones')
    require(isinstance(phones, list) and len(phones) <= 32, 'invalid_inventory')
    mapping, rows = {}, {}
    for phone in phones:
        require(isinstance(phone, dict), 'invalid_inventory')
        lines = phone.get('lines')
        require(isinstance(lines, list) and 1 <= len(lines) <= 2 and phone.get('id') == lines[0],
                'invalid_inventory')
        for line in lines:
            require(isinstance(line, str) and LINE.fullmatch(line) and line not in mapping,
                    'invalid_inventory')
            mapping[line] = phone['id']
        require(all(type(phone.get(x)) is bool for x in ('online', 'dnd', 'dnd_pending')),
                'invalid_inventory')
        require(isinstance(phone.get('channels'), list)
                and isinstance(phone.get('capabilities'), list)
                and all(isinstance(x, str) for x in phone['capabilities']), 'invalid_inventory')
        alert = phone.get('alert')
        require('alert' in phone and (alert is None or isinstance(alert, dict)
                and isinstance(alert.get('state'), str)), 'invalid_inventory')
        rows[phone['id']] = phone
    requested = list(rows) if destinations == ['*'] else destinations
    require(all(x in mapping for x in requested), 'destination_not_in_inventory')
    ids = list(dict.fromkeys(mapping[x] for x in requested))
    require(len(ids) <= 10, 'too_many_physical_phones')
    selected, skipped = [], []
    for phone_id in ids:
        phone = rows[phone_id]
        reason = ('offline' if not phone['online'] else
                  'voice_configured' if 'voice' in phone['capabilities'] else
                  'dnd' if phone['dnd'] or phone['dnd_pending'] else
                  'busy' if phone['channels'] else
                  'alert' if phone['alert'] is not None and phone['alert']['state'] != 'ended' else
                  'image_unavailable' if 'image' not in phone['capabilities'] else None)
        if reason:
            skipped.append({'phone': phone_id, 'reason': reason})
        else:
            selected.append(phone_id)
    return selected, skipped


class Claims:
    """Bounded metadata ledger; exclusive event claims survive uncertain outcomes."""
    def __init__(self, directory, *, readonly=False):
        self.db = None
        self.directory = Path(directory)
        self.path = self.directory / 'claims.sqlite3'
        if readonly and not self.path.exists():
            return
        try:
            if not readonly:
                self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.directory.lstat()
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                    and not info.st_mode & 0o077, 'state_permissions')
            if not readonly:
                try:
                    fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                    os.close(fd)
                except FileExistsError:
                    pass
            info = self.path.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and not info.st_mode & 0o077, 'state_permissions')
            target = self.path.as_uri() + '?mode=ro' if readonly else str(self.path)
            self.db = sqlite3.connect(target, uri=readonly, timeout=.1)
            if not readonly:
                self.db.execute('PRAGMA journal_mode=DELETE')
                self.db.execute('PRAGMA synchronous=FULL')
                self.db.execute('CREATE TABLE IF NOT EXISTS events (key TEXT PRIMARY KEY, claimed REAL NOT NULL)')
                self.db.execute('CREATE TABLE IF NOT EXISTS phones (phone TEXT PRIMARY KEY, claimed REAL NOT NULL)')
                self.db.commit()
        except (OSError, sqlite3.Error):
            self.close()
            raise Failure('state_unavailable') from None

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def reserve(self, event, phones, now, cooldown, *, readonly=False):
        if self.db is None:
            return phones, []
        try:
            if not readonly:
                self.db.execute('BEGIN IMMEDIATE')
                self.db.execute('DELETE FROM events WHERE claimed < ?', (now - 86400,))
            if self.db.execute('SELECT 1 FROM events WHERE key=?', (event,)).fetchone():
                return [], [{'phone': x, 'reason': 'duplicate_event'} for x in phones]
            require(self.db.execute('SELECT count(*) FROM events').fetchone()[0] < 4096,
                    'event_ledger_full')
            selected, skipped = [], []
            for phone in phones:
                old = self.db.execute('SELECT claimed FROM phones WHERE phone=?', (phone,)).fetchone()
                if old and now - old[0] < cooldown:
                    skipped.append({'phone': phone, 'reason': 'cooldown'})
                else:
                    selected.append(phone)
            if not readonly:
                self.db.execute('INSERT INTO events VALUES (?, ?)', (event, now))
            return selected, skipped
        except sqlite3.Error:
            raise Failure('state_busy_or_unavailable') from None
        finally:
            if not readonly:
                self.db.commit()

    def claim_phone(self, phone, now, cooldown):
        try:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT claimed FROM phones WHERE phone=?', (phone,)).fetchone()
            if row and now - row[0] < cooldown:
                return False
            require(self.db.execute('SELECT count(*) FROM phones').fetchone()[0] < 64
                    or row is not None, 'phone_ledger_full')
            self.db.execute('INSERT OR REPLACE INTO phones VALUES (?, ?)', (phone, now))
            return True
        except sqlite3.Error:
            raise Failure('state_busy_or_unavailable') from None
        finally:
            self.db.commit()


def event_fresh(event_at, now, age):
    require(type(event_at) in (int, float) and math.isfinite(event_at)
            and -5 <= now - event_at <= age, 'event_stale_or_clock_skew')


def event_title(label, event_at):
    suffix = datetime.fromtimestamp(event_at, timezone.utc).strftime(' | Event %m-%d %H:%M:%SZ')
    # CiscoIPPhoneDisplayableType limits Title to 32 characters.
    return label[:32 - len(suffix)] + suffix


def dispatch(config, camera_key, event_id, event_at, *, dry_run=False, http=None):
    result = {'status': 'ok', 'dry_run': dry_run, 'accepted': [], 'skipped': [],
              'failed': [], 'unknown': [], 'display_verified': False}
    ledger, pending, inflight = None, [], None
    try:
        with time_limit(TOTAL_SECONDS):
            require(camera_key in config['cameras'], 'camera_not_allowlisted')
            require(isinstance(event_id, str) and SAFE_ID.fullmatch(event_id), 'invalid_event_id')
            event_fresh(event_at, time.time(), config['max_event_age_seconds'])
            if not config['destinations']:
                result['reason'] = 'no_destinations'
                return result
            http = http or HTTP()
            inventory = http.json(config['gateway'], '/v1/phones')
            phones, skipped = select_phones(inventory, config['destinations'], time.time())
            result['skipped'].extend(skipped)
            if not phones:
                return result
            event = hashlib.sha256((camera_key + '\0' + event_id).encode()).hexdigest()
            ledger = Claims(config['state_directory'], readonly=dry_run)
            phones, skipped = ledger.reserve(event, phones, time.time(),
                                              config['cooldown_seconds'], readonly=dry_run)
            result['skipped'].extend(skipped)
            if dry_run:
                result['eligible'] = phones
                return result
            if not phones:
                return result
            pending = list(phones)
            event_fresh(event_at, time.time(), config['max_event_age_seconds'])
            frame = acquire_frame(http, config, config['cameras'][camera_key])
            encoded = base64.b64encode(frame).decode('ascii')
            del frame
            for phone in phones:
                # Each earlier POST may take5s; refresh each physical target just
                # before dispatch, not once for the whole batch. The server still
                # owns the final voice reservation/AMI/call admission checks.
                try:
                    eligible, skipped = select_phones(
                        http.json(config['gateway'], '/v1/phones'), [phone], time.time())
                except Failure as exc:
                    if exc.reason != 'destination_not_in_inventory':
                        raise
                    eligible, skipped = [], [{'phone': phone, 'reason': 'no_longer_in_inventory'}]
                event_fresh(event_at, time.time(), config['max_event_age_seconds'])
                if not eligible:
                    result['skipped'].extend(skipped)
                    pending.remove(phone)
                    continue
                # Claim BEFORE any POST; ambiguous network failures consume the cooldown.
                if not ledger.claim_phone(phone, time.time(), config['cooldown_seconds']):
                    result['skipped'].append({'phone': phone, 'reason': 'cooldown'})
                    pending.remove(phone)
                    continue
                body = {'line': phone, 'title': event_title(config['cameras'][camera_key]['label'], event_at),
                        'image_base64': encoded,
                        'idempotency_key': 'motion-' + hashlib.sha256((event + phone).encode()).hexdigest()}
                try:
                    inflight = phone
                    response = http.json(config['gateway'], '/v1/phones/' + phone + '/image', body=body)
                    require(response.get('status') == 'accepted', 'invalid_response')
                    result['accepted'].append(phone)
                except Failure as exc:
                    kind = ('skipped' if exc.reason == 'http_busy' else
                            'failed' if exc.reason == 'http_refused' else 'unknown')
                    result[kind].append({'phone': phone, 'reason': exc.reason})
                inflight = None
                pending.remove(phone)
    except Failure as exc:
        result['status'], result['reason'] = 'refused', exc.reason
    except Exception:
        # Never expose upstream bodies, tokens, URLs, paths, image bytes or exception text.
        result['status'], result['reason'] = 'refused', 'internal_failure'
    finally:
        if inflight is not None:
            if inflight not in result['accepted'] and not any(
                    x['phone'] == inflight for x in result['unknown']):
                result['unknown'].append({'phone': inflight, 'reason': 'interrupted_post'})
            if inflight in pending:
                pending.remove(inflight)
        result['skipped'].extend({'phone': x, 'reason': result.get('reason', 'not_attempted')}
                                 for x in pending)
        if ledger is not None:
            ledger.close()
        result['counts'] = {key: len(result[key]) for key in ('accepted', 'skipped', 'failed', 'unknown')}
    if result['failed'] or result['unknown']:
        result['status'] = 'partial' if result['accepted'] else 'refused'
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--camera', required=True)
    parser.add_argument('--event-id', required=True)
    parser.add_argument('--event-at', required=True, type=float)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.worker:
        # The parent blocks termination only around creation/handle assignment.
        signal.pthread_sigmask(signal.SIG_UNBLOCK, (signal.SIGTERM, signal.SIGINT))
    if not args.worker:
        try:
            result = supervise(argv)
        except Exception:
            result = {'status': 'refused', 'reason': 'supervision_failure',
                      'delivery_outcome': 'unknown', 'retry_allowed': False,
                      'display_verified': False}
        print(json.dumps(result, sort_keys=True, separators=(',', ':')))
        return 0 if result.get('status') in ('ok', 'partial') else 1
    try:
        config = load_config(args.config)
        result = dispatch(config, args.camera, args.event_id, args.event_at, dry_run=args.dry_run)
    except Failure as exc:
        result = {'status': 'refused', 'reason': exc.reason, 'display_verified': False}
    except Exception:
        result = {'status': 'refused', 'reason': 'internal_failure', 'display_verified': False}
    print(json.dumps(result, sort_keys=True, separators=(',', ':')))
    return 0 if result['status'] in ('ok', 'partial') else 1


class SupervisorCancelled(BaseException):
    pass


def stop_owned_child(process):
    """Finish only this supervisor's child, then collect its exit status."""
    if process is None:
        return True
    if process.poll() is not None:
        process.communicate()
        return True
    process.terminate()
    try:
        process.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            return False
    return process.returncode is not None


def unknown_delivery(reason):
    return {'status': 'refused', 'reason': reason, 'delivery_outcome': 'unknown',
            'retry_allowed': False, 'display_verified': False}


def supervise(argv, *, timeout=50):
    """Bound DNS stalls and reap the owned child on timeout or cancellation.

    SIGTERM/SIGINT are blocked while creating the child so cancellation cannot
    land between fork/exec and assigning its process handle. SIGKILL cannot be
    handled; callers should use the normal terminating signals first.
    """
    process = None
    signals = (signal.SIGTERM, signal.SIGINT)
    previous_handlers = {sig: signal.getsignal(sig) for sig in signals}
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
    result = unknown_delivery('supervision_failure')

    def cancelled(_signum, _frame):
        # Repeated termination must not interrupt the bounded cleanup path.
        for sig in signals:
            signal.signal(sig, signal.SIG_IGN)
        raise SupervisorCancelled()

    try:
        for sig in signals:
            signal.signal(sig, cancelled)
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), '--worker', *argv],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        output, _ = process.communicate(timeout=timeout)
        require(len(output) <= 65536, 'invalid_worker_result')
        result = json.loads(output, object_pairs_hook=object_pairs)
        require(isinstance(result, dict) and result.get('status') in ('ok', 'partial', 'refused'),
                'invalid_worker_result')
    except subprocess.TimeoutExpired:
        result = unknown_delivery('worker_deadline_exceeded')
    except (SupervisorCancelled, KeyboardInterrupt):
        result = unknown_delivery('supervisor_cancelled')
    except (Failure, ValueError, UnicodeError):
        result = unknown_delivery('invalid_worker_result')
    finally:
        for sig in signals:
            signal.signal(sig, signal.SIG_IGN)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        try:
            if not stop_owned_child(process):
                result = unknown_delivery('worker_reap_unconfirmed')
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
    return result


if __name__ == '__main__':
    sys.exit(main())
