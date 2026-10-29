#!/usr/bin/env python3
"""Private, finite HA speech jobs for explicitly configured Asterisk endpoints."""
from __future__ import annotations

import argparse
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import threading
import time
from urllib import request

import dial_audio
from phone_gateway import AMI

ID = re.compile(r'[0-9a-f]{32}')
LINE = re.compile(r'2[0-9]{2}')
TERMINAL = frozenset(('completed', 'skipped', 'failed', 'stopped', 'unknown'))
JOB_TERMINAL = frozenset(('finished', 'failed', 'stopped'))
MAX_JOBS, MAX_HISTORY, HISTORY_SECONDS = 4, 512, 86400
MAX_AUDIO = 120 * 32000
PUBLIC_RECIPIENT = ('line', 'phone_id', 'driver', 'status', 'channel', 'uniqueid', 'caller_id', 'reason')
SATELLITE_PROTOCOL = 'cisco-announcement-v1'
SATELLITE_GUARDS = ('atomic_idle_guard', 'dedup', 'owned_cancel', 'honest_completion', 'deadline_enforced')


class AnnounceError(Exception):
    def __init__(self, reason, status=409):
        self.reason, self.status = reason, status


def require(value, reason, status=400):
    if not value:
        raise AnnounceError(reason, status)


def validate_config(value):
    required = {'bind', 'port', 'token', 'ami', 'lines', 'dial_audio_config', 'voice_service', 'spool', 'agi'}
    require(isinstance(value, dict) and required <= set(value) <= required | {'satellites'},
        'invalid_config')
    require(value['bind'] == '127.0.0.1' and type(value['port']) is int
            and 1024 <= value['port'] <= 65535, 'loopback_listener_required')
    require(isinstance(value['token'], str) and re.fullmatch(r'[A-Za-z0-9_-]{32,128}', value['token']),
            'invalid_token')
    require(isinstance(value['lines'], dict) and 1 <= len(value['lines']) <= 128
            and all(isinstance(k, str) and LINE.fullmatch(k) and isinstance(v, str)
                    and LINE.fullmatch(v) for k, v in value['lines'].items()),
            'invalid_lines')
    ami = value['ami']
    require(isinstance(ami, dict) and set(ami) == {'host', 'port', 'username', 'secret'}, 'invalid_ami')
    require(type(ami['port']) is int and 1 <= ami['port'] <= 65535, 'invalid_ami')
    require(all(isinstance(ami[k], str) and 1 <= len(ami[k]) <= 256
                and not re.search(r'[\r\n\x00]', ami[k]) for k in ('host', 'username', 'secret')), 'invalid_ami')
    require(isinstance(value['voice_service'], str) and re.fullmatch(r'[0-9]{3,8}', value['voice_service']),
            'invalid_voice_service')
    for name in ('dial_audio_config', 'spool', 'agi'):
        path = value[name]
        require(isinstance(path, str) and re.fullmatch(r'/[A-Za-z0-9_./-]{1,1023}', path)
                and '..' not in Path(path).parts and str(Path(path)) == path, 'invalid_path')
    satellites = value.get('satellites', {})
    require(isinstance(satellites, dict) and len(satellites) <= 16, 'invalid_satellites')
    identities = set()
    for line, satellite in satellites.items():
        require(isinstance(line, str) and value['lines'].get(line) == line
                and sum(physical == line for physical in value['lines'].values()) == 1,
                'one_line_per_satellite_required')
        require(isinstance(satellite, dict) and set(satellite) == {'entity_id', 'domain'}
                and isinstance(satellite['entity_id'], str)
                and re.fullmatch(r'assist_satellite\.[a-z0-9_]{1,128}', satellite['entity_id'])
                and isinstance(satellite['domain'], str)
                and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', satellite['domain']), 'invalid_satellite')
        require(satellite['entity_id'] not in identities, 'duplicate_satellite')
        identities.add(satellite['entity_id'])
    return value


def request_data(value, config):
    require(isinstance(value, dict) and not set(value) - {'idempotency_key', 'lines', 'text', 'dry_run', 'expected_routes'}
            and {'idempotency_key', 'lines', 'text'} <= set(value), 'invalid_request')
    key, lines, text = value['idempotency_key'], value['lines'], value['text']
    require(isinstance(key, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', key), 'invalid_idempotency_key')
    require(isinstance(lines, list) and 1 <= len(lines) <= 16 and all(
        isinstance(n, str) and n in config['lines'] for n in lines), 'invalid_targets')
    physical = [config['lines'][n] for n in lines]
    require(len(set(physical)) == len(physical), 'duplicate_physical_target')
    routes = {line: 'ha_satellite' if config['lines'][line] in config.get('satellites', {}) else 'sip'
              for line in lines}
    require('ha_satellite' not in routes.values() or 'expected_routes' in value, 'expected_routes_required')
    require('expected_routes' not in value or value['expected_routes'] == routes, 'route_mismatch')
    require(isinstance(text, str) and 1 <= len(text) <= 1000 and text.strip()
            and not any(ord(c) < 32 and c not in '\n\t' for c in text)
            and not any(0xD800 <= ord(c) <= 0xDFFF for c in text), 'invalid_text')
    require(type(value.get('dry_run', False)) is bool, 'invalid_dry_run')
    payload_data = {'lines': lines, 'text': text}
    if 'ha_satellite' in routes.values():
        payload_data['routes'] = routes
    payload = json.dumps(payload_data, ensure_ascii=True, sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:32], hashlib.sha256(payload.encode()).hexdigest()


def root_id(job_id, line):
    return 'announce-' + job_id + '-' + line


def caller_id(job_id, line):
    return '8' + str(int(hashlib.sha256((job_id + line).encode()).hexdigest()[:12], 16) % 100000000).zfill(8)


def related(recipient, channels):
    roots = {recipient['root'], recipient['root'] + '-b'}
    return [c for c in channels if roots.intersection((c.get('Uniqueid'), c.get('Linkedid')))]


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not stat.S_IMODE(info.st_mode) & 0o077, 'unsafe_spool', 503)


def atomic_json(path, data):
    temporary = path.with_name(path.name + '.tmp-' + str(threading.get_ident()))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(data, output, ensure_ascii=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def synthesize(config, text, directory):
    settings = dial_audio.validate(json.loads(Path(config['dial_audio_config']).read_text()))
    service = settings['services'][config['voice_service']]
    require(service['kind'] == 'clock', 'configured_voice_required')
    ha = dial_audio.HomeAssistant(*dial_audio.credentials(settings['credentials_file']))
    media_id = None
    if config.get('satellites'):
        # Preserve the original asset: direct playback must not inherit SIP's
        # sample rate. Both transports still use exactly one TTS request.
        body, _ = ha.fetch('/api/tts_get_url', payload={
            'engine_id': service['engine_id'], 'language': service['language'],
            'options': {'voice': service['voice']}, 'message': text, 'cache': False,
        }, limit=65536)
        path = dial_audio.check_path(json.loads(body)['path'], '/api/tts_proxy/')
        # The API origin can differ from HA's device-reachable media origin.
        # Adapters resolve this validated HA-local path using their HA URL.
        media_id = path
        raw = ha.audio(path)
    else:
        raw = ha.speech(service, text)
    require(len(raw) <= 8 * 1024 * 1024, 'speech_too_large')
    source, decoded, target = directory / 'source.audio', directory / 'decoded.pcm', directory / 'speech.sln16'
    try:
        source.write_bytes(raw)
        subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
            '-protocol_whitelist', 'file', '-i', str(source), '-vn', '-sn', '-dn',
            '-t', '120.1', '-ac', '1', '-ar', '16000', '-f', 's16le', '-c:a', 'pcm_s16le',
            '-fs', str(MAX_AUDIO + 32000), str(decoded)], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25)
        pcm = decoded.read_bytes()
        require(640 <= len(pcm) and len(pcm) % 2 == 0 and len(pcm) + 12800 <= MAX_AUDIO,
                'speech_duration_rejected')
        target.write_bytes(b'\0' * 12800 + pcm)  # Allow the speaker path to settle.
        target.chmod(0o600)
        duration = target.stat().st_size / 32000
        return {'duration_seconds': duration, 'media_id': media_id} if media_id else duration
    finally:
        source.unlink(missing_ok=True)
        decoded.unlink(missing_ok=True)


class SatelliteAPI:
    """Guarded HA contract only; standard announce cannot safely cancel owned jobs."""

    def __init__(self, config):
        settings = dial_audio.validate(json.loads(Path(config['dial_audio_config']).read_text()))
        self.base, self.token = dial_audio.credentials(settings['credentials_file'])
        self.opener = request.build_opener(request.ProxyHandler({}), dial_audio.NoRedirect())

    def call(self, satellite, operation, **values):
        require(operation in ('capabilities', 'submit', 'status', 'stop'), 'invalid_satellite_operation')
        path = '/api/services/' + satellite['domain'] + '/announcement_' + operation + '?return_response'
        payload = json.dumps({'entity_id': satellite['entity_id'], **values}).encode()
        req = request.Request(self.base + path, data=payload, headers={
            'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json',
            'Accept-Encoding': 'identity'})
        # Services enqueue/observe device jobs; none waits for playback here.
        with self.opener.open(req, timeout=6) as response:
            require(response.status == 200, 'satellite_service_failed', 503)
            body = response.read(65537)
            require(len(body) <= 65536, 'invalid_satellite_response', 503)
        result = json.loads(body).get('service_response')
        require(isinstance(result, dict), 'invalid_satellite_response', 503)
        return result


class Job:
    def __init__(self, data, directory):
        self.data, self.directory = data, directory
        self.lock, self.stop = threading.RLock(), threading.Event()
        self.thread = None
        self.retry_after = 0

    def save(self):
        with self.lock:
            atomic_json(self.directory / 'job.json', self.data)

    def update(self, recipient=None, **values):
        with self.lock:
            (self.data if recipient is None else recipient).update(values)
            self.save()

    def public(self, *, dry_run=False):
        with self.lock:
            return {'id': self.data['id'], 'status': 'dry_run' if dry_run else self.data['status'],
                    'lines': list(self.data['lines']), 'created_at': self.data['created_at'],
                    'expires_at': self.data['expires_at'], 'duration_seconds': self.data['duration_seconds'],
                    'recipients': [{k: r[k] for k in PUBLIC_RECIPIENT} for r in self.data['recipients']]}


class Announcer:
    def __init__(self, config, *, ami_factory=AMI, synth=synthesize, clock=time.time, recover=True,
                 satellite_factory=SatelliteAPI):
        self.config, self.ami_factory, self.synth, self.clock = validate_config(config), ami_factory, synth, clock
        self.satellite_factory = satellite_factory
        self.spool = Path(config['spool'])
        private_directory(self.spool)
        self.jobs, self.lock = {}, threading.RLock()
        self.shutdown = threading.Event()
        self.monitor = None
        if recover:
            self.recover()

    def recover(self):
        for directory in sorted(self.spool.iterdir()):
            if not ID.fullmatch(directory.name) or not directory.is_dir() or directory.is_symlink():
                continue
            path = directory / 'job.json'
            require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 262144,
                    'invalid_journal', 503)
            data = json.loads(path.read_text())
            require(data['id'] == directory.name and data['status'] in JOB_TERMINAL | {
                'accepted', 'running', 'stopping'} and len(data['recipients']) <= 16, 'invalid_journal', 503)
            for r in data['recipients']:
                require(LINE.fullmatch(r['line']) and r['root'] == root_id(data['id'], r['line']),
                        'invalid_journal', 503)
                r.setdefault('driver', 'sip')
                require(r['driver'] in ('sip', 'ha_satellite'), 'invalid_journal', 503)
                r.setdefault('stop_attempted', False)
            job = Job(data, directory)
            self.jobs[data['id']] = job
            if data['status'] not in JOB_TERMINAL:
                job.stop.set()
                job.update(status='stopping')
                self.start_worker(job, None)
            else:
                self.remove_audio(job)
        self.prune()

    @staticmethod
    def remove_audio(job):
        for name in ('speech.sln16', 'source.audio', 'decoded.pcm'):
            (job.directory / name).unlink(missing_ok=True)

    def reconcile(self):
        with self.lock:
            for job in self.jobs.values():
                if (job.data['status'] not in JOB_TERMINAL and job.thread is not None
                        and not job.thread.is_alive() and time.monotonic() >= job.retry_after):
                    job.stop.set()
                    job.update(status='stopping')
                    self.start_worker(job, None)

    def start_monitor(self):
        def watch():
            while not self.shutdown.wait(10):
                try:
                    self.reconcile()
                except Exception:
                    pass  # Preserve journals; retry cleanup on the next interval.
        self.monitor = threading.Thread(target=watch, name='announcement-reconcile', daemon=True)
        self.monitor.start()

    def prune(self):
        for key, job in list(self.jobs.items()):
            if job.data['status'] in JOB_TERMINAL and self.clock() - job.data['created_at'] > HISTORY_SECONDS:
                for path in job.directory.iterdir():
                    if path.name in ('job.json', 'speech.sln16', 'source.audio', 'decoded.pcm') or re.fullmatch(
                            r'receipt-2[0-9]{2}\.json', path.name):
                        path.unlink(missing_ok=True)
                try:
                    job.directory.rmdir()
                except OSError:
                    pass
                del self.jobs[key]

    def submit(self, request):
        key, payload_hash = request_data(request, self.config)
        with self.lock:
            if key in self.jobs:
                job = self.jobs[key]
                require(job.data['payload_hash'] == payload_hash, 'idempotency_conflict', 409)
                return job.public(dry_run=request.get('dry_run', False))
            now = self.clock()
            recipients = [{'line': line, 'phone_id': self.config['lines'][line], 'status': 'queued',
                           'driver': 'ha_satellite' if self.config['lines'][line] in self.config.get('satellites', {}) else 'sip',
                           'channel': None, 'uniqueid': None, 'caller_id': caller_id(key, line), 'reason': None,
                           'root': root_id(key, line), 'dispatched_at': None, 'seen': False, 'answered': False,
                           'hangup_attempted': [], 'stop_attempted': False, 'cancel': False} for line in request['lines']]
            data = {'id': key, 'payload_hash': payload_hash, 'status': 'accepted', 'lines': request['lines'],
                    'created_at': now, 'expires_at': now + 180, 'duration_seconds': None, 'recipients': recipients}
            job = Job(data, self.spool / key)
            if request.get('dry_run', False):
                return job.public(dry_run=True)
            self.prune()
            active = [j for j in self.jobs.values() if j.data['status'] not in JOB_TERMINAL]
            require(len(active) < MAX_JOBS and len(self.jobs) < MAX_HISTORY, 'announcement_capacity', 503)
            occupied = {r['phone_id'] for j in active for r in j.data['recipients'] if r['status'] not in TERMINAL}
            for r in recipients:
                if r['phone_id'] in occupied:
                    r.update(status='skipped', reason='already_reserved')
            private_directory(job.directory)
            job.save()
            self.jobs[key] = job
            self.start_worker(job, request['text'])
            return job.public()

    def start_worker(self, job, text):
        job.thread = threading.Thread(target=self.run, args=(job, text), name='announcement-' + job.data['id'], daemon=True)
        job.thread.start()

    def get(self, key):
        with self.lock:
            require(key in self.jobs, 'unknown_announcement', 404)
            job = self.jobs[key]
        result = job.public()
        if result['status'] in JOB_TERMINAL:
            return result
        if all(r['driver'] == 'ha_satellite' or r['status'] in TERMINAL for r in job.data['recipients']):
            return result
        try:
            with self.ami_factory(self.config['ami']) as ami:
                _, channels = ami.snapshot()
        except Exception:
            channels = None
        # GET never changes a call. Its fresh observation prevents a CLI from
        # answering a different call using an old worker snapshot.
        with job.lock:
            result = job.public()
            for item, recipient in zip(result['recipients'], job.data['recipients']):
                if recipient['driver'] == 'ha_satellite':
                    continue
                if item['status'] in TERMINAL:
                    continue
                item.update(channel=None, uniqueid=None)
                if channels is None:
                    item.update(status='queued', reason='state_unavailable')
                    continue
                if job.stop.is_set() or recipient['cancel'] or self.clock() >= job.data['expires_at']:
                    item.update(status='stopping')
                    continue
                physical = [c for c in related(recipient, channels) if c.get('Uniqueid') and
                            c.get('Channel', '').startswith('PJSIP/' + recipient['line'] + '-')]
                if len(physical) == 1 and physical[0].get('ChannelStateDesc') in ('Ringing', 'Up'):
                    channel = physical[0]
                    item.update(status='ringing' if channel['ChannelStateDesc'] == 'Ringing' else 'playing',
                                channel=channel['Channel'], uniqueid=channel['Uniqueid'])
                else:
                    item.update(status='queued')
            return result

    def stop(self, key):
        with self.lock:
            require(key in self.jobs, 'unknown_announcement', 404)
            job = self.jobs[key]
            if job.data['status'] not in JOB_TERMINAL:
                job.stop.set()
                job.update(status='stopping')
            return job.public()

    def dnd(self, ami, line):
        try:
            rows = ami.action('DBGet', complete='DBGetComplete', Family='cisco-dnd', Key=line)
        except Exception as error:
            if getattr(error, 'ami_error', None) == 'database_entry_not_found':
                return False
            raise
        values = [r.get('Val') for r in rows if r.get('Event') == 'DBGetResponse'
                  and r.get('Family') == 'cisco-dnd' and r.get('Key') == line]
        require(len(values) == 1 and values[0] in ('0', '1'), 'invalid_dnd_state', 503)
        return values[0] == '1'

    def satellite_result(self, job, recipient, result):
        require(isinstance(result, dict) and result.get('request_id') == recipient['root'],
                'uncorrelated_satellite_response', 503)
        state = result.get('status')
        require(state in {'accepted', 'playing', 'completed', 'skipped', 'failed', 'stopped', 'unknown'},
                'invalid_satellite_status', 503)
        reason = result.get('reason')
        # Responses are private integration input. Never leak arbitrary HA text,
        # URLs or credentials through a public job receipt.
        if reason is not None and (not isinstance(reason, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,95}', reason)):
            reason = 'satellite_reported_error'
        job.update(recipient, status='submitted' if state == 'accepted' else state,
                   reason=reason, channel=None, uniqueid=None)

    def dispatch_satellite(self, job, recipient):
        require(recipient['driver'] == 'ha_satellite', 'wrong_announcement_driver', 503)
        if recipient['dispatched_at'] is not None or recipient['status'] in TERMINAL:
            return  # A timed-out submit is never replayed.
        if job.stop.is_set():
            job.update(recipient, status='stopped', reason='stopped')
            return
        satellite = self.config.get('satellites', {}).get(recipient['phone_id'])
        if satellite is None:
            job.update(recipient, status='skipped', reason='satellite_not_configured')
            return
        # The CLI's existing DND switch lives in AstDB. Native guards also check
        # device quiet mode, but must not silently bypass a PBX DND preference.
        try:
            with self.ami_factory(self.config['ami']) as ami:
                if self.dnd(ami, recipient['line']):
                    job.update(recipient, status='skipped', reason='dnd')
                    return
        except Exception:
            job.update(recipient, status='skipped', reason='state_unavailable')
            return
        try:
            api = self.satellite_factory(self.config)
            capabilities = api.call(satellite, 'capabilities')
            require(capabilities.get('protocol') == SATELLITE_PROTOCOL
                    and all(capabilities.get(key) is True for key in SATELLITE_GUARDS),
                    'satellite_guards_unavailable', 503)
        except Exception:
            job.update(recipient, status='skipped', reason='satellite_guards_unavailable')
            return
        if job.stop.is_set():
            job.update(recipient, status='stopped', reason='stopped')
            return
        if self.clock() + (job.data['duration_seconds'] or 120) + 8 >= job.data['expires_at']:
            job.update(recipient, status='skipped', reason='preparation_budget_exhausted')
            return
        try:
            dial_audio.check_path(job.data.get('media_id'), '/api/tts_proxy/')
        except ValueError:
            job.update(recipient, status='failed', reason='prepared_media_invalid')
            return
        job.update(recipient, status='submitting', dispatched_at=self.clock())
        try:
            result = api.call(satellite, 'submit', request_id=recipient['root'],
                              media_id=job.data['media_id'], expires_at=int(job.data['expires_at']),
                              preannounce=False, busy_policy='skip')
            self.satellite_result(job, recipient, result)
        except Exception:
            job.update(recipient, status='submitted', reason='submission_unknown')

    def observe_satellite(self, job, recipient):
        if recipient['status'] in TERMINAL:
            return
        if recipient['dispatched_at'] is None:
            job.update(recipient, status='stopped', reason='stopped')
            return
        # Persist the outcome once native deadline enforcement must have run.
        # A deadline is never evidence that the person heard the message.
        if self.clock() > job.data['expires_at'] + 10:
            job.update(recipient, status='unknown', reason='completion_unconfirmed',
                       channel=None, uniqueid=None)
            return
        satellite = self.config.get('satellites', {}).get(recipient['phone_id'])
        if satellite is None:
            job.update(recipient, status='unknown', reason='satellite_configuration_changed')
            return
        stopping = job.stop.is_set() or self.clock() >= job.data['expires_at']
        operation = 'status'
        if stopping and not recipient['stop_attempted']:
            # Journal before the write: retries must not stop a later job.
            job.update(recipient, status='stopping', stop_attempted=True)
            operation = 'stop'
        try:
            api = self.satellite_factory(self.config)
            result = api.call(satellite, operation, request_id=recipient['root'])
            self.satellite_result(job, recipient, result)
        except Exception:
            job.update(recipient, reason='stop_unconfirmed' if operation == 'stop' else 'state_unavailable')

    def dispatch(self, job, recipient, ami):
        require(recipient['driver'] == 'sip', 'wrong_announcement_driver', 503)
        endpoints, channels = ami.snapshot()
        siblings = {line for line, physical in self.config['lines'].items() if physical == recipient['phone_id']}
        state = endpoints.get(recipient['line'])
        if state in (None, 'Unavailable', 'Invalid'):
            job.update(recipient, status='skipped', reason='offline')
            return
        if state != 'Not in use' or any(c.get('Channel', '').split('/')[0] == 'PJSIP'
                and c['Channel'].split('/', 1)[1].split('-', 1)[0] in siblings for c in channels):
            job.update(recipient, status='skipped', reason='busy')
            return
        if any(endpoints.get(line) not in ('Not in use', 'Unavailable', 'Invalid', None) for line in siblings):
            job.update(recipient, status='skipped', reason='busy')
            return
        if any(self.dnd(ami, line) for line in siblings):
            job.update(recipient, status='skipped', reason='dnd')
            return
        if job.stop.is_set():
            job.update(recipient, status='stopped', reason='stopped')
            return
        if self.clock() + (job.data['duration_seconds'] or 120) + 32 >= job.data['expires_at']:
            job.update(recipient, status='skipped', reason='preparation_budget_exhausted')
            return
        job.update(recipient, dispatched_at=self.clock())
        try:
            ami.action('Originate', action_id=recipient['root'],
                Channel='Local/' + recipient['line'] + '@cisco-announcement/n',
                ChannelId=recipient['root'], OtherChannelId=recipient['root'] + '-b',
                Application='AGI', Data=','.join((self.config['agi'], job.data['id'], recipient['line'], self.config['spool'])),
                CallerID='Announcement <' + recipient['caller_id'] + '>', Timeout='30000', Async='true',
                Variable='__ANNOUNCE_JOB=' + job.data['id'] + ',__ANNOUNCE_LINE=' + recipient['line']
                         + ',__ANNOUNCE_END=' + str(int(job.data['expires_at'])))
        except Exception as error:
            if getattr(error, 'ami_action', None) == 'Originate' and getattr(error, 'reason', None) == 'ami_action_rejected':
                job.update(recipient, status='failed', reason='originate_rejected')
            else:
                job.update(recipient, status='stopping', reason='originate_unknown', cancel=True)

    def receipt(self, job, recipient):
        path = job.directory / ('receipt-' + recipient['line'] + '.json')
        if not path.exists():
            return None
        require(not path.is_symlink() and path.stat().st_size <= 4096, 'invalid_receipt', 503)
        data = json.loads(path.read_text())
        require(data.get('job_id') == job.data['id'] and data.get('line') == recipient['line']
                and data.get('uniqueid') in (recipient['root'], recipient['root'] + '-b')
                and data.get('status') in ('playing', 'completed', 'failed'), 'invalid_receipt', 503)
        return data['status']

    def cleanup(self, job, recipient, ami, owned):
        for channel in owned:
            uid = channel.get('Uniqueid')
            if not uid or uid in recipient['hangup_attempted']:
                continue
            job.update(recipient, hangup_attempted=recipient['hangup_attempted'] + [uid])
            try:
                ami.action('Hangup', Channel=uid, Cause='16')
            except Exception:
                pass  # Never replay an uncertain hangup; native deadlines remain armed.

    def observe(self, job, recipient, ami, channels):
        require(recipient['driver'] == 'sip', 'wrong_announcement_driver', 503)
        if recipient['status'] in TERMINAL:
            return
        owned = related(recipient, channels)
        physical = {c['Uniqueid']: c for c in owned if c.get('Channel', '').startswith(
            'PJSIP/' + recipient['line'] + '-') and c.get('Uniqueid')}
        receipt = self.receipt(job, recipient)
        if owned:
            recipient['seen'] = True
        expired = self.clock() >= job.data['expires_at']
        if len(physical) > 1:
            recipient.update(cancel=True, reason='call_conflict')
        if job.stop.is_set() or recipient['cancel'] or expired:
            self.cleanup(job, recipient, ami, owned)
            reason = recipient['reason'] or ('deadline' if expired else 'stopped')
            done = not owned and (recipient['seen'] or recipient['dispatched_at'] is None or expired)
            job.update(recipient, status='stopped' if done else 'stopping', channel=None,
                       uniqueid=None, reason=reason)
            return
        if physical:
            channel = next(iter(physical.values()))
            state = channel.get('ChannelStateDesc')
            job.update(recipient, status='playing' if state == 'Up' else 'ringing' if state == 'Ringing' else 'queued',
                       channel=channel['Channel'], uniqueid=channel['Uniqueid'],
                       answered=recipient['answered'] or state == 'Up')
        elif not owned and (recipient['seen'] or receipt):
            reason = None if receipt == 'completed' else 'playback_failed' if receipt == 'failed' else (
                'call_ended' if recipient['answered'] else 'not_answered')
            job.update(recipient, status='completed' if receipt == 'completed' else 'failed',
                       channel=None, uniqueid=None, reason=reason)
        elif recipient['dispatched_at'] is not None and self.clock() - recipient['dispatched_at'] > 35:
            # Keep watching an ambiguous asynchronous originate until its native
            # deadline; it must never cause a second call or escape later cleanup.
            job.update(recipient, status='stopping', cancel=True, reason='not_answered',
                       channel=None, uniqueid=None)

    def run(self, job, text):
        try:
            if text is not None and not job.stop.is_set() and any(
                    r['status'] == 'queued' for r in job.data['recipients']):
                try:
                    prepared = self.synth(self.config, text, job.directory)
                    duration = prepared['duration_seconds'] if isinstance(prepared, dict) else prepared
                    require(type(duration) in (float, int) and 0 < duration <= 120,
                            'preparation_failed')
                    require(self.clock() + duration + 35 < job.data['expires_at'],
                            'preparation_budget_exhausted')
                    job.update(duration_seconds=duration, status='running',
                               media_id=prepared.get('media_id') if isinstance(prepared, dict) else None)
                except Exception as error:
                    reason = 'preparation_budget_exhausted' if getattr(error, 'reason', None) == (
                        'preparation_budget_exhausted') else 'preparation_failed'
                    for recipient in job.data['recipients']:
                        if recipient['status'] not in TERMINAL:
                            job.update(recipient, status='failed', reason=reason)
                    job.update(status='failed')
                    return
                finally:
                    text = None  # Do not retain the transcript during call polling.
                for recipient in job.data['recipients']:
                    if recipient['status'] == 'queued':
                        if recipient['driver'] == 'ha_satellite':
                            self.dispatch_satellite(job, recipient)
                            continue
                        try:
                            with self.ami_factory(self.config['ami']) as ami:
                                self.dispatch(job, recipient, ami)
                        except Exception:
                            if recipient['dispatched_at'] is None:
                                job.update(recipient, status='skipped', reason='state_unavailable')
                            else:
                                job.update(recipient, status='stopping', cancel=True, reason='originate_unknown')
            while any(r['status'] not in TERMINAL for r in job.data['recipients']):
                for recipient in job.data['recipients']:
                    if recipient['driver'] == 'ha_satellite':
                        self.observe_satellite(job, recipient)
                pending_sip = [r for r in job.data['recipients']
                               if r['driver'] == 'sip' and r['status'] not in TERMINAL]
                if pending_sip:
                    try:
                        with self.ami_factory(self.config['ami']) as ami:
                            _, channels = ami.snapshot()
                            for recipient in pending_sip:
                                self.observe(job, recipient, ami, channels)
                    except Exception:
                        # Calls have a PBX-side absolute timeout even if this process
                        # or AMI becomes unavailable. Preserve uncertainty in status.
                        if self.clock() > job.data['expires_at'] + 45:
                            job.update(status='stopping')
                            return
                if any(r['status'] not in TERMINAL for r in job.data['recipients']):
                    time.sleep(0.35)
            job.update(status='stopped' if job.stop.is_set() else 'finished')
        except Exception:
            job.stop.set()
            job.update(status='stopping')
        finally:
            if job.data['status'] in JOB_TERMINAL:
                self.remove_audio(job)
            else:
                job.retry_after = time.monotonic() + 30

    def close(self):
        self.shutdown.set()
        if self.monitor:
            self.monitor.join(1)
        with self.lock:
            active = [j for j in self.jobs.values() if j.data['status'] not in JOB_TERMINAL]
            for job in active:
                job.stop.set()
                job.update(status='stopping')
        deadline = time.monotonic() + 25
        for job in active:
            if job.thread:
                job.thread.join(max(0, deadline - time.monotonic()))


class Handler(BaseHTTPRequestHandler):
    server_version = 'PhoneAnnouncements/1'

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_args):
        pass

    def reply(self, status, body):
        data = json.dumps(body, ensure_ascii=True).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_api(self, write=False):
        try:
            manager = self.server.announcer
            require(hmac.compare_digest(self.headers.get('Authorization', '').encode(),
                ('Bearer ' + manager.config['token']).encode()), 'unauthorized', 401)
            match = re.fullmatch(r'/v1/announcements(?:/([0-9a-f]{32})(/stop)?)?', self.path)
            require(match is not None, 'not_found', 404)
            key, stopping = match.groups()
            if not write:
                require(key and not stopping, 'not_found', 404)
                result = manager.get(key)
            else:
                require(not self.headers.get('Transfer-Encoding'), 'invalid_body')
                length = self.headers.get('Content-Length', '')
                require(length.isdigit() and 1 <= int(length) <= 16384, 'invalid_body')
                body = json.loads(self.rfile.read(int(length)))
                if stopping:
                    require(body == {}, 'invalid_request')
                    result = manager.stop(key)
                else:
                    require(key is None, 'not_found', 404)
                    result = manager.submit(body)
            self.reply(200, result)
        except AnnounceError as error:
            self.reply(error.status, {'status': 'refused', 'reason': error.reason})
        except (ValueError, UnicodeError):
            self.reply(400, {'status': 'refused', 'reason': 'invalid_body'})
        except Exception:
            self.reply(503, {'status': 'refused', 'reason': 'request_failed'})

    def do_GET(self):
        self.handle_api()

    def do_POST(self):
        self.handle_api(write=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    manager = Announcer(json.loads(Path(args.config).read_text()))
    server = ThreadingHTTPServer((manager.config['bind'], manager.config['port']), Handler)
    server.daemon_threads = True
    server.announcer = manager
    manager.start_monitor()

    def stop(_number, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        manager.close()


if __name__ == '__main__':
    main()
