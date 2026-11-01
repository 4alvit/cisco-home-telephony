"""Opt-in, at-most-once text forwarding to idle displays and guarded speakers.

The journal contains hashes and delivery metadata, never notification text.
An accepted request is not proof that a person saw or heard its contents.
"""
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
from urllib import error, parse, request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LINE = re.compile(r'2[0-9]{2}\Z')
SOURCE = re.compile(r'[a-z][a-z0-9_]{0,47}\Z')
EVENT = re.compile(r'[A-Za-z0-9_.:-]{8,128}\Z')
MAX_SECONDS = 45


class NotificationError(ValueError):
    def __init__(self, reason, status=400):
        self.reason, self.status = reason, status
        super().__init__(reason)


def require(value, reason, status=400):
    if not value:
        raise NotificationError(reason, status)


def validate_config(value):
    require(isinstance(value, dict) and set(value) <= {
        'enabled', 'speech_enabled', 'sources', 'screen_destinations',
        'echo_destinations', 'cooldown_seconds', 'max_event_age_seconds',
        'quiet_hours', 'announcement'}, 'invalid_notification_config')
    value = dict(value)
    for name in ('enabled', 'speech_enabled'):
        value.setdefault(name, False)
        require(type(value[name]) is bool, 'invalid_notification_config')
    sources = value.setdefault('sources', [])
    require(isinstance(sources, list) and len(sources) <= 32 and all(
        isinstance(x, str) and SOURCE.fullmatch(x) for x in sources)
        and len(set(sources)) == len(sources), 'invalid_notification_sources')
    for name in ('screen_destinations', 'echo_destinations'):
        lines = value.setdefault(name, [])
        require(isinstance(lines, list) and len(lines) <= 20 and all(
            isinstance(x, str) and LINE.fullmatch(x) for x in lines)
            or name == 'screen_destinations' and lines == ['*'],
            'invalid_notification_destinations')
        require(len(set(lines)) == len(lines), 'duplicate_notification_destination')
        require(name != 'echo_destinations' or len(lines) <= 16,
                'too_many_notification_echoes')
    require(not set(value['screen_destinations']) & set(value['echo_destinations']),
            'overlapping_notification_destinations')
    for name, default, low, high in (('cooldown_seconds', 60, 60, 86400),
                                    ('max_event_age_seconds', 90, 5, 120)):
        value.setdefault(name, default)
        require(type(value[name]) is int and low <= value[name] <= high,
                'invalid_notification_config')
    quiet = value.get('quiet_hours')
    if quiet is not None:
        require(isinstance(quiet, dict) and set(quiet) == {'start', 'end', 'timezone'},
                'invalid_notification_quiet_hours')
        for name in ('start', 'end'):
            require(isinstance(quiet[name], str) and re.fullmatch(
                r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', quiet[name]),
                'invalid_notification_quiet_hours')
        require(quiet['start'] != quiet['end'] and isinstance(quiet['timezone'], str),
                'invalid_notification_quiet_hours')
        try:
            ZoneInfo(quiet['timezone'])
        except (ValueError, ZoneInfoNotFoundError):
            raise NotificationError('invalid_notification_timezone') from None
        value['quiet_hours'] = dict(quiet)
    endpoint = value.get('announcement')
    require(not value['echo_destinations'] or endpoint is not None,
            'notification_announcement_required')
    if endpoint is not None:
        require(isinstance(endpoint, dict) and set(endpoint) == {'base_url', 'token'},
                'invalid_notification_announcement')
        try:
            url = parse.urlsplit(endpoint['base_url'])
            require(url.scheme == 'http' and url.hostname == '127.0.0.1'
                    and url.port is not None and 1024 <= url.port <= 65535
                    and not url.username and not url.password
                    and not url.query and not url.fragment and url.path in ('', '/'),
                    'notification_announcement_must_be_loopback')
        except (ValueError, TypeError, AttributeError):
            raise NotificationError('invalid_notification_announcement') from None
        require(isinstance(endpoint['token'], str) and re.fullmatch(
            r'[A-Za-z0-9_-]{32,128}', endpoint['token']), 'invalid_notification_announcement_token')
        value['announcement'] = dict(endpoint, base_url=endpoint['base_url'].rstrip('/'))
    return value


def validate_event(value):
    require(isinstance(value, dict) and {'source', 'event_id', 'event_at', 'message'}
            <= set(value) <= {'source', 'event_id', 'event_at', 'title', 'message'},
            'invalid_notification_event')
    value = dict(value)
    value.setdefault('title', 'Home Assistant')
    require(isinstance(value['source'], str) and SOURCE.fullmatch(value['source']),
            'invalid_notification_source')
    require(isinstance(value['event_id'], str) and EVENT.fullmatch(value['event_id']),
            'invalid_notification_event_id')
    require(type(value['event_at']) in (int, float) and math.isfinite(value['event_at']),
            'invalid_notification_timestamp')
    for name, limit in (('title', 128), ('message', 2000)):
        text = value[name]
        require(isinstance(text, str) and 1 <= len(text) <= limit and text.strip()
                and not any((ord(c) < 32 and c not in '\n\t') or ord(c) == 127
                            or 0xD800 <= ord(c) <= 0xDFFF for c in text),
                'invalid_notification_text')
    return value


def quiet_now(config, now):
    quiet = config.get('quiet_hours')
    if quiet is None:
        return False
    current = datetime.fromtimestamp(now, ZoneInfo(quiet['timezone'])).strftime('%H:%M')
    start, end = quiet['start'], quiet['end']
    return start <= current < end if start < end else current >= start or current < end


class Ledger:
    def __init__(self, directory, *, readonly=False):
        self.db = None
        directory = Path(directory).absolute()
        path = directory / 'notifications.sqlite3'
        try:
            if readonly and not path.exists():
                return
            if not readonly:
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = directory.lstat()
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                    and not info.st_mode & 0o077, 'notification_state_permissions', 503)
            if not readonly:
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    os.close(fd)
                except FileExistsError:
                    pass
            info = path.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and not info.st_mode & 0o077, 'notification_state_permissions', 503)
            self.db = sqlite3.connect(path.as_uri() + '?mode=ro' if readonly else str(path),
                                      uri=readonly, timeout=.1)
            if not readonly:
                self.db.execute('PRAGMA synchronous=FULL')
                self.db.execute('CREATE TABLE IF NOT EXISTS events (key TEXT PRIMARY KEY, digest TEXT NOT NULL, claimed REAL NOT NULL)')
                self.db.execute('CREATE TABLE IF NOT EXISTS targets (target TEXT PRIMARY KEY, claimed REAL NOT NULL)')
                self.db.commit()
        except (OSError, sqlite3.Error):
            self.close()
            raise NotificationError('notification_state_unavailable', 503) from None

    def close(self):
        if self.db is not None:
            self.db.close()

    def event(self, key, digest, now, *, readonly=False):
        if self.db is None:
            return True
        try:
            with self.db:
                if not readonly:
                    self.db.execute('BEGIN IMMEDIATE')
                    self.db.execute('DELETE FROM events WHERE claimed < ?', (now - 86400,))
                row = self.db.execute('SELECT digest FROM events WHERE key=?', (key,)).fetchone()
                if row:
                    require(row[0] == digest, 'notification_event_conflict', 409)
                    return False
                require(self.db.execute('SELECT count(*) FROM events').fetchone()[0] < 4096,
                        'notification_ledger_full', 503)
                if not readonly:
                    self.db.execute('INSERT INTO events VALUES (?, ?, ?)', (key, digest, now))
            return True
        except sqlite3.Error:
            raise NotificationError('notification_state_unavailable', 503) from None

    def target(self, target, now, cooldown, *, readonly=False):
        if self.db is None:
            return True
        try:
            with self.db:
                if not readonly:
                    self.db.execute('BEGIN IMMEDIATE')
                row = self.db.execute('SELECT claimed FROM targets WHERE target=?', (target,)).fetchone()
                if row and now - row[0] < cooldown:
                    return False
                require(row is not None or self.db.execute('SELECT count(*) FROM targets').fetchone()[0] < 128,
                        'notification_ledger_full', 503)
                if not readonly:
                    self.db.execute('INSERT OR REPLACE INTO targets VALUES (?, ?)', (target, now))
            return True
        except sqlite3.Error:
            raise NotificationError('notification_state_unavailable', 503) from None


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise NotificationError('announcement_redirect_refused', 503)


def submit_announcement(config, body, timeout):
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
    req = request.Request(config['base_url'] + '/v1/announcements',
                          data=json.dumps(body).encode(), headers={
                              'Authorization': 'Bearer ' + config['token'],
                              'Content-Type': 'application/json'})
    try:
        with opener.open(req, timeout=min(5, timeout)) as response:
            require(response.status == 200, 'announcement_response_unknown', 503)
            raw = response.read(65537)
            require(len(raw) <= 65536, 'announcement_response_unknown', 503)
            result = json.loads(raw)
            return validate_announcement(result, body)
    except error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise NotificationError('announcement_refused' if 400 <= code < 500
                                else 'announcement_response_unknown', 503) from None
    except (OSError, ValueError, error.URLError):
        raise NotificationError('announcement_response_unknown', 503) from None


def validate_announcement(result, body):
    expected_id = hashlib.sha256(body['idempotency_key'].encode()).hexdigest()[:32]
    states = {'queued', 'submitting', 'submitted', 'playing', 'completed', 'skipped',
              'failed', 'stopping', 'stopped', 'unknown'}
    terminal = {'completed', 'skipped', 'failed', 'stopped', 'unknown'}
    require(isinstance(result, dict) and result.get('id') == expected_id
            and result.get('status') in {'accepted', 'running', 'stopping', 'finished', 'stopped', 'failed'}
            and isinstance(result.get('lines'), list)
            and len(result['lines']) == len(body['lines'])
            and all(isinstance(line, str) for line in result['lines'])
            and set(result['lines']) == set(body['lines'])
            and isinstance(result.get('recipients'), list)
            and len(result['recipients']) == len(body['lines']),
            'announcement_response_unknown', 503)
    seen = set()
    for row in result['recipients']:
        require(isinstance(row, dict) and isinstance(row.get('line'), str)
                and row['line'] in body['lines'] and row['line'] not in seen
                and row.get('phone_id') == row['line'] and row.get('driver') == 'ha_satellite'
                and 'channel' in row and row['channel'] is None
                and 'uniqueid' in row and row['uniqueid'] is None
                and row.get('status') in states,
                'announcement_response_unknown', 503)
        require(result['status'] not in {'finished', 'stopped', 'failed'} or row['status'] in terminal,
                'announcement_response_unknown', 503)
        seen.add(row['line'])
    return {'id': expected_id, 'status': result['status'], 'recipients': [
        {'line': row['line'], 'status': row['status']} for row in result['recipients']]}


def screen_targets(inventory, destinations, now):
    require(isinstance(inventory, dict) and inventory.get('ami_fresh') is True,
            'notification_inventory_unavailable', 503)
    observed = inventory.get('observed_at')
    require(type(observed) in (int, float) and math.isfinite(observed)
            and 0 <= now - observed <= 5, 'notification_inventory_stale', 503)
    rows, mapping = {}, {}
    require(isinstance(inventory.get('phones'), list) and len(inventory['phones']) <= 32,
            'notification_inventory_invalid', 503)
    for row in inventory['phones']:
        require(isinstance(row, dict) and isinstance(row.get('lines'), list)
                and 1 <= len(row['lines']) <= 2 and row.get('id') == row['lines'][0],
                'notification_inventory_invalid', 503)
        for line in row['lines']:
            require(isinstance(line, str) and LINE.fullmatch(line) and line not in mapping,
                    'notification_inventory_invalid', 503)
            mapping[line] = row['id']
        rows[row['id']] = row
    selected = list(rows) if destinations == ['*'] else destinations
    require(all(line in mapping for line in selected), 'notification_destination_unknown', 503)
    selected = list(dict.fromkeys(mapping[line] for line in selected))
    require(len(selected) <= 10, 'too_many_notification_phones')
    allowed, skipped = [], []
    for phone in selected:
        row = rows[phone]
        require({'online', 'dnd', 'dnd_pending', 'channels', 'alert', 'capabilities'} <= set(row)
                and all(type(row[name]) is bool for name in ('online', 'dnd', 'dnd_pending'))
                and isinstance(row['channels'], list) and isinstance(row['capabilities'], list),
                'notification_inventory_invalid', 503)
        guard = row.get('notification')
        reason = ('notification_unavailable' if 'notification' not in row.get('capabilities', [])
                  else 'offline' if row.get('online') is not True
                  else 'dnd' if row.get('dnd') is not False or row.get('dnd_pending') is not False
                  else 'busy' if row.get('channels') != []
                  else 'alert' if row.get('alert') is not None and (
                      not isinstance(row['alert'], dict) or row['alert'].get('state') != 'ended')
                  else 'voice_busy_or_unknown' if not isinstance(guard, dict)
                       or guard.get('eligible') is not True else None)
        if reason:
            skipped.append({'target': phone, 'reason': reason})
        else:
            allowed.append(phone)
    return allowed, skipped


class Dispatcher:
    def __init__(self, config, gateway, state_directory, *, clock=time.time,
                 monotonic=time.monotonic, announcer=submit_announcement):
        self.config = validate_config(config)
        self.gateway, self.directory = gateway, state_directory
        self.clock, self.monotonic, self.announcer = clock, monotonic, announcer
        self.lock = threading.Lock()

    def running(self):
        stopped = getattr(self.gateway, 'stopped', None)
        require(stopped is None or not stopped.is_set(), 'notification_gateway_stopping', 503)

    def dispatch(self, event, *, dry_run=False):
        result = {'status': 'ok', 'dry_run': dry_run, 'accepted': [], 'skipped': [],
                  'unknown': [], 'eligible': [], 'display_verified': False,
                  'playback_verified': False}
        ledger = None
        acquired = False
        try:
            require(type(dry_run) is bool, 'invalid_notification_dry_run')
            event = validate_event(event)
            cfg = self.config
            if not cfg['enabled']:
                result['reason'] = 'disabled'
                return result
            require(event['source'] in cfg['sources'], 'notification_source_not_allowed', 403)
            self.running()
            self.fresh(event)
            acquired = self.lock.acquire(blocking=False)
            require(acquired, 'notification_dispatch_busy', 409)
            deadline = self.monotonic() + MAX_SECONDS
            key = hashlib.sha256((event['source'] + '\0' + event['event_id']).encode()).hexdigest()
            digest = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
            ledger = Ledger(self.directory, readonly=dry_run)
            if not ledger.event(key, digest, self.clock(), readonly=dry_run):
                result['reason'] = 'duplicate_event'
                return result
            if quiet_now(cfg, self.clock()):
                result['reason'] = 'quiet_hours'
                return result
            phones = []
            if cfg['screen_destinations']:
                try:
                    inventory = self.gateway.listing()
                    phones, skipped = screen_targets(inventory, cfg['screen_destinations'], self.clock())
                    require(not any(line in cfg['echo_destinations']
                                    for row in inventory['phones'] for line in row['lines']),
                            'overlapping_notification_destinations')
                    result['skipped'].extend(skipped)
                except NotificationError as exc:
                    if exc.reason == 'overlapping_notification_destinations':
                        raise
                    phones = []
                    result['skipped'].append({'target': 'screens', 'reason': 'inventory_unavailable'})
                except Exception:
                    phones = []
                    result['skipped'].append({'target': 'screens', 'reason': 'inventory_unavailable'})
            for phone in phones:
                self.running()
                self.fresh(event)
                require(not quiet_now(cfg, self.clock()), 'notification_quiet_hours', 409)
                require(self.monotonic() < deadline, 'notification_deadline', 503)
                if not ledger.target(phone, self.clock(), cfg['cooldown_seconds'], readonly=dry_run):
                    result['skipped'].append({'target': phone, 'reason': 'cooldown'})
                    continue
                if dry_run:
                    result['eligible'].append(phone)
                    continue
                try:
                    self.gateway.mutate(phone, 'notification', {
                        'idempotency_key': 'notification-' + hashlib.sha256((key + phone).encode()).hexdigest(),
                        'line': phone, 'title': event['title'][:32], 'text': event['message'],
                        'idle_close_seconds': 45})
                    result['accepted'].append({'target': phone, 'route': 'screen'})
                except Exception as exc:
                    # No retries even for a preflight refusal; a changed state
                    # later must not resurrect this notification.
                    known = getattr(exc, 'before_mutation', False) or getattr(exc, 'status', 500) < 500
                    result['skipped' if known else 'unknown'].append({
                        'target': phone, 'reason': 'gateway_refused' if known else 'delivery_unknown'})
            echoes = []
            for line in cfg['echo_destinations']:
                if not cfg['speech_enabled']:
                    result['skipped'].append({'target': line, 'reason': 'speech_disabled'})
                elif not ledger.target(line, self.clock(), cfg['cooldown_seconds'], readonly=dry_run):
                    result['skipped'].append({'target': line, 'reason': 'cooldown'})
                else:
                    echoes.append(line)
            if echoes:
                self.running()
                self.fresh(event)
                require(not quiet_now(cfg, self.clock()), 'notification_quiet_hours', 409)
                require(self.monotonic() < deadline, 'notification_deadline', 503)
                if dry_run:
                    result['eligible'].extend(echoes)
                else:
                    try:
                        request_body = {
                            'idempotency_key': 'notification-' + key,
                            'lines': echoes, 'text': event['message'][:1000],
                            'expected_routes': {line: 'ha_satellite' for line in echoes}}
                        job = self.announcer(cfg['announcement'], request_body,
                            max(.1, deadline - self.monotonic()))
                        for row in job['recipients']:
                            if row['status'] in ('failed', 'skipped', 'stopped'):
                                result['skipped'].append({'target': row['line'], 'job_id': job['id'],
                                                          'reason': 'announcement_' + row['status']})
                            elif row['status'] == 'unknown':
                                result['unknown'].append({'target': row['line'], 'job_id': job['id'],
                                                          'reason': 'delivery_unknown'})
                            else:
                                result['accepted'].append({'target': row['line'], 'route': 'speech',
                                                           'job_id': job['id'], 'state': row['status']})
                    except Exception:
                        result['unknown'].extend({'target': line, 'reason': 'delivery_unknown'} for line in echoes)
            if result['unknown']:
                result['status'] = 'partial' if result['accepted'] else 'refused'
        except NotificationError as exc:
            result['status'], result['reason'] = ('partial' if result['accepted'] else 'refused'), exc.reason
        except Exception:
            result['status'], result['reason'] = 'refused', 'notification_internal_failure'
        finally:
            if ledger is not None:
                ledger.close()
            if acquired:
                self.lock.release()
        return result

    def fresh(self, event):
        require(-5 <= self.clock() - event['event_at'] <= self.config['max_event_age_seconds'],
                'notification_event_stale', 409)
