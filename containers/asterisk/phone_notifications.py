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
        'speech_destinations', 'echo_destinations', 'cooldown_seconds', 'max_event_age_seconds',
        'quiet_hours', 'announcement'}, 'invalid_notification_config')
    value = dict(value)
    for name in ('enabled', 'speech_enabled'):
        value.setdefault(name, False)
        require(type(value[name]) is bool, 'invalid_notification_config')
    sources = value.setdefault('sources', [])
    require(isinstance(sources, list) and len(sources) <= 32 and all(
        isinstance(x, str) and SOURCE.fullmatch(x) for x in sources)
        and len(set(sources)) == len(sources), 'invalid_notification_sources')
    for name in ('screen_destinations', 'speech_destinations', 'echo_destinations'):
        lines = value.setdefault(name, [])
        require(isinstance(lines, list) and len(lines) <= 20 and all(
            isinstance(x, str) and LINE.fullmatch(x) for x in lines)
            or name in ('screen_destinations', 'speech_destinations') and lines == ['*'],
            'invalid_notification_destinations')
        require(len(set(lines)) == len(lines), 'duplicate_notification_destination')
        require(name != 'echo_destinations' or len(lines) <= 16,
                'too_many_notification_echoes')
    for left, right in (('screen_destinations', 'speech_destinations'),
                        ('screen_destinations', 'echo_destinations'),
                        ('speech_destinations', 'echo_destinations')):
        require(not set(value[left]) & set(value[right]),
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
    require(not (value['echo_destinations'] or value['speech_destinations']) or endpoint is not None,
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


def announcement_request(config, body, timeout, *, job_id=None, stop=False):
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
    path = '/v1/announcements'
    if job_id is not None:
        require(re.fullmatch(r'[0-9a-f]{32}', job_id), 'invalid_announcement_id')
        path += '/' + job_id + ('/stop' if stop else '')
    payload = {} if stop else body if job_id is None else None
    req = request.Request(config['base_url'] + path,
                          data=json.dumps(payload).encode() if payload is not None else None,
                          headers={'Authorization': 'Bearer ' + config['token'],
                                   'Content-Type': 'application/json'})
    try:
        with opener.open(req, timeout=min(5, timeout)) as response:
            require(response.status == 200, 'announcement_response_unknown', 503)
            raw = response.read(65537)
            require(len(raw) <= 65536, 'announcement_response_unknown', 503)
            return validate_announcement(json.loads(raw), body)
    except error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise NotificationError('announcement_refused' if 400 <= code < 500
                                else 'announcement_response_unknown', 503) from None
    except (OSError, ValueError, error.URLError):
        raise NotificationError('announcement_response_unknown', 503) from None


def submit_announcement(config, body, timeout):
    return announcement_request(config, body, timeout)


def read_announcement(config, body, timeout):
    return announcement_request(config, body, timeout,
        job_id=hashlib.sha256(body['idempotency_key'].encode()).hexdigest()[:32])


def stop_announcement(config, body, timeout):
    return announcement_request(config, body, timeout,
        job_id=hashlib.sha256(body['idempotency_key'].encode()).hexdigest()[:32], stop=True)


def validate_announcement(result, body):
    expected_id = hashlib.sha256(body['idempotency_key'].encode()).hexdigest()[:32]
    routes = body.get('expected_routes', {line: 'ha_satellite' for line in body['lines']})
    states = {'queued', 'submitting', 'submitted', 'ringing', 'playing', 'completed', 'skipped',
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
    if 'sip' in routes.values():
        require(type(result.get('expires_at')) in (int, float)
                and math.isfinite(result['expires_at']) and result['expires_at'] > 0,
                'announcement_response_unknown', 503)
    seen = set()
    recipients = []
    for row in result['recipients']:
        require(isinstance(row, dict) and isinstance(row.get('line'), str)
                and row['line'] in body['lines'] and row['line'] not in seen
                and row.get('phone_id') == row['line'] and row.get('driver') == routes[row['line']]
                and row.get('status') in states and 'channel' in row and 'uniqueid' in row,
                'announcement_response_unknown', 503)
        channel, unique = row['channel'], row['uniqueid']
        if routes[row['line']] == 'ha_satellite':
            require(channel is None and unique is None and row['status'] != 'ringing',
                    'announcement_response_unknown', 503)
        else:
            require(routes[row['line']] == 'sip'
                    and isinstance(row.get('caller_id'), str)
                    and re.fullmatch(r'[0-9]{3,32}', row['caller_id'])
                    and ((channel is None and unique is None)
                        or (isinstance(channel, str) and re.fullmatch(
                            r'PJSIP/' + row['line'] + r'-[A-Za-z0-9]+', channel)
                            and isinstance(unique, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', unique)))
                    and (row['status'] not in ('ringing', 'playing') or channel is not None),
                    'announcement_response_unknown', 503)
        require(result['status'] not in {'finished', 'stopped', 'failed'} or row['status'] in terminal,
                'announcement_response_unknown', 503)
        seen.add(row['line'])
        recipients.append({name: row.get(name) for name in
                           ('line', 'phone_id', 'driver', 'status', 'channel', 'uniqueid', 'caller_id')})
    return {'id': expected_id, 'status': result['status'], 'recipients': recipients,
            'expires_at': result.get('expires_at')}


def phone_targets(inventory, destinations, now, *, speech=False):
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
        capability = 'answer' if speech else 'notification'
        reason = ('notification_unavailable' if capability not in row.get('capabilities', [])
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
    return allowed, skipped, selected


def screen_targets(inventory, destinations, now):
    allowed, skipped, _ = phone_targets(inventory, destinations, now)
    return allowed, skipped


class Dispatcher:
    def __init__(self, config, gateway, state_directory, *, clock=time.time,
                 monotonic=time.monotonic, announcer=submit_announcement,
                 reader=read_announcement, stopper=stop_announcement,
                 sleep=time.sleep, thread_factory=threading.Thread):
        self.config = validate_config(config)
        self.gateway, self.directory = gateway, state_directory
        self.clock, self.monotonic, self.announcer = clock, monotonic, announcer
        self.lock = threading.Lock()
        self.reader, self.stopper = reader, stopper
        self.sleep, self.thread_factory = sleep, thread_factory
        self.worker = None

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
            require(self.worker is None or not self.worker.is_alive(),
                    'notification_speech_busy', 409)
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
            phones, speakers = [], []
            if cfg['screen_destinations'] or cfg['speech_destinations']:
                try:
                    inventory = self.gateway.listing()
                    phones, screen_skipped, screens = phone_targets(
                        inventory, cfg['screen_destinations'], self.clock())
                    speakers, speech_skipped, spoken = phone_targets(
                        inventory, cfg['speech_destinations'], self.clock(), speech=True)
                    require(not set(screens) & set(spoken) and not any(
                        line in cfg['echo_destinations']
                        for row in inventory['phones'] for line in row['lines']),
                        'overlapping_notification_destinations')
                    require(len(spoken) + len(cfg['echo_destinations']) <= 16,
                            'too_many_notification_speakers')
                    result['skipped'].extend(screen_skipped + speech_skipped)
                except NotificationError as exc:
                    if exc.reason in ('overlapping_notification_destinations', 'too_many_notification_speakers'):
                        raise
                    phones, speakers = [], []
                    result['skipped'].append({'target': 'phones', 'reason': 'inventory_unavailable'})
                except Exception:
                    phones, speakers = [], []
                    result['skipped'].append({'target': 'phones', 'reason': 'inventory_unavailable'})
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
            speech = []
            for line in speakers + cfg['echo_destinations']:
                if not cfg['speech_enabled']:
                    result['skipped'].append({'target': line, 'reason': 'speech_disabled'})
                elif not ledger.target(line, self.clock(), cfg['cooldown_seconds'], readonly=dry_run):
                    result['skipped'].append({'target': line, 'reason': 'cooldown'})
                else:
                    speech.append(line)
            if speech:
                self.running()
                self.fresh(event)
                require(not quiet_now(cfg, self.clock()), 'notification_quiet_hours', 409)
                require(self.monotonic() < deadline, 'notification_deadline', 503)
                if dry_run:
                    result['eligible'].extend(speech)
                else:
                    try:
                        request_body = {
                            'idempotency_key': 'notification-' + key,
                            'lines': speech, 'text': event['message'][:1000],
                            'expected_routes': {line: 'sip' if line in speakers else 'ha_satellite'
                                                for line in speech}}
                        job = self.announcer(cfg['announcement'], request_body,
                            max(.1, deadline - self.monotonic()))
                        if any(line in speakers for line in speech):
                            self.start_answers(request_body, event['event_at'])
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
                        result['unknown'].extend({'target': line, 'reason': 'delivery_unknown'} for line in speech)
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

    def start_answers(self, body, event_at):
        # The worker holds routing/identity metadata only, never message text.
        observed = {key: value for key, value in body.items() if key != 'text'}
        self.worker = self.thread_factory(target=self.answer_job, args=(observed, event_at),
                                          name='notification-answer', daemon=True)
        try:
            self.worker.start()
        except Exception:
            try:
                self.stopper(self.config['announcement'], observed, 5)
            except Exception:
                pass
            raise

    def answer_job(self, body, event_at):
        deadline = self.monotonic() + 180
        attempted = set()
        pending = {line for line, route in body['expected_routes'].items() if route == 'sip'}
        terminal = {'finished', 'failed', 'stopped'}
        try:
            while self.monotonic() < deadline:
                self.running()
                if pending:
                    self.fresh({'event_at': event_at})
                    require(not quiet_now(self.config, self.clock()), 'notification_quiet_hours')
                job = self.reader(self.config['announcement'], body, 5)
                if job['status'] in terminal:
                    return
                require(job['status'] != 'stopping' and job['expires_at'] > self.clock(),
                        'announcement_no_longer_active')
                for recipient in job['recipients']:
                    if recipient['status'] in ('playing', 'completed', 'skipped', 'failed', 'stopped', 'unknown'):
                        pending.discard(recipient['line'])
                    if (recipient['driver'] != 'sip' or recipient['status'] != 'ringing'
                            or recipient['line'] in attempted):
                        continue
                    # Only fresh, currently allowed notifications may open a microphone.
                    self.running()
                    self.fresh({'event_at': event_at})
                    require(not quiet_now(self.config, self.clock()), 'notification_quiet_hours')
                    current = self.reader(self.config['announcement'], body, 5)
                    require(current['status'] not in terminal | {'stopping'}
                            and current['expires_at'] > self.clock(), 'announcement_no_longer_active')
                    row = next(item for item in current['recipients'] if item['line'] == recipient['line'])
                    require(row['status'] == 'ringing' and all(row[key] == recipient[key]
                        for key in ('channel', 'uniqueid', 'phone_id', 'caller_id')),
                        'announcement_call_changed')
                    self.answer_recipient(job['id'], row, event_at)
                    attempted.add(row['line'])
                    pending.discard(row['line'])
                self.sleep(.35)
            raise NotificationError('announcement_monitor_deadline')
        except Exception:
            # One bounded owned-job stop; native PBX timeouts remain the fallback.
            # Never retry an uncertain answer or start another announcement.
            try:
                self.stopper(self.config['announcement'], body, 5)
            except Exception:
                pass

    def answer_recipient(self, job_id, recipient, event_at):
        inventory = self.gateway.listing()
        require(isinstance(inventory, dict) and inventory.get('ami_fresh') is True
                and type(inventory.get('observed_at')) in (int, float)
                and 0 <= self.clock() - inventory['observed_at'] <= 5,
                'notification_inventory_stale')
        rows = [row for row in inventory.get('phones', []) if row.get('id') == recipient['phone_id']]
        require(len(rows) == 1, 'announcement_phone_changed')
        phone = rows[0]
        require(recipient['line'] in phone.get('lines', []) and phone.get('dnd') is False
                and phone.get('dnd_pending') is False and len(phone.get('channels', [])) == 1
                and (phone.get('alert') is None or phone['alert'].get('state') == 'ended'),
                'announcement_phone_changed')
        call = phone['channels'][0]
        require(call.get('line') == recipient['line'] and call.get('state') == 'Ringing'
                and call.get('name') == recipient['channel'] and call.get('uniqueid') == recipient['uniqueid'],
                'announcement_call_changed')
        self.running()
        self.fresh({'event_at': event_at})
        require(not quiet_now(self.config, self.clock()), 'notification_quiet_hours')
        key = 'announce-' + job_id + '-' + recipient['line']
        response = self.gateway.answer_notification(phone['id'], {
            'line': recipient['line'], 'channel': call['name'], 'uniqueid': call['uniqueid'],
            'idempotency_key': key})
        require(response.get('status') == 'accepted' and response.get('action_id') == key,
                'announcement_answer_not_confirmed')

    def fresh(self, event):
        require(-5 <= self.clock() - event['event_at'] <= self.config['max_event_age_seconds'],
                'notification_event_stale', 409)
