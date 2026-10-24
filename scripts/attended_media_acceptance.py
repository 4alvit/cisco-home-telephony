#!/usr/bin/env python3
"""Bounded, explicitly attended, single-file HA or Music Assistant acceptance.

Default is a local plan. Runtime credentials and collector evidence are private
files. A collector attestation is a trust boundary, not cryptographic live proof.
The child normally cleans up; the parent watchdog repeats only exact-owned
cleanup if the child exceeds its budget. No response claims a human heard audio.
The 60-second bound limits this test process, not a delayed server-side action.
An unobserved start is reconciled for eight seconds, then remains explicitly
unknown: the operator must reconcile it without blindly retrying playback or
claiming the phone stopped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit, urlunsplit
import urllib.error
import urllib.request
import uuid

MAX_SECONDS = 60
WORK_SECONDS = 40
DISPATCH_SECONDS = 12


class Refused(RuntimeError):
    pass


def require(value, reason):
    if not value:
        raise Refused(reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_private(path, maximum=1024 * 1024):
    path = Path(path)
    require(path.is_absolute() and path.resolve(strict=True) == path, 'private_path_required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
                and not before.st_mode & 0o077 and before.st_size <= maximum, 'private_file_mode')
        raw = b''
        while len(raw) <= maximum:
            block = os.read(fd, min(65536, maximum + 1 - len(raw)))
            if not block:
                break
            raw += block
        after = os.fstat(fd)
        require(len(raw) <= maximum and (before.st_ino, before.st_size, before.st_mtime_ns) ==
                (after.st_ino, after.st_size, after.st_mtime_ns), 'private_file_changed')
        return raw
    finally:
        os.close(fd)


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def clean_url(value):
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))


def valid_http(value):
    require(isinstance(value, str), 'http_url_required')
    parsed = urlsplit(value)
    require(parsed.scheme in ('http', 'https') and parsed.hostname
            and not parsed.username and not parsed.password and not parsed.fragment,
            'invalid_http_url')
    _ = parsed.port
    return parsed


def validate_spec(spec):
    require(spec.get('schema_version') == 1 and spec.get('route') in ('ha', 'ma'), 'spec_schema')
    require(re.fullmatch(r'[0-9]{2,10}', spec.get('target', '')) is not None, 'spec_target')
    require(re.fullmatch(r'media_player\.[a-z0-9_]+', spec.get('entity_id', '')) is not None,
            'spec_entity')
    require(re.fullmatch(r'[a-f0-9]{32}', spec.get('test_id', '')) is not None, 'spec_test_id')
    fixture = spec.get('fixture', {})
    parsed = valid_http(fixture.get('url'))
    require(spec['test_id'] in unquote(parsed.path).split('/'), 'unique_fixture_path_required')
    require(re.fullmatch(r'[a-f0-9]{64}', fixture.get('sha256', '')) is not None, 'fixture_digest')
    require(type(fixture.get('bytes')) is int and 0 < fixture['bytes'] <= 2 * 1024 * 1024,
            'fixture_size')
    duration = fixture.get('duration_seconds')
    require(type(duration) in (int, float) and math.isfinite(duration) and 1 <= duration <= 30,
            'fixture_duration')
    require(isinstance(fixture.get('title'), str) and 0 < len(fixture['title']) <= 120,
            'fixture_title')
    connections = spec.get('connections', {})
    for name in ('gateway', 'ha') + (('ma', 'ma_stream') if spec['route'] == 'ma' else ()):
        base = valid_http(connections.get(name))
        require(base.path in ('', '/') and not base.query, 'connection_origin_required')
    require(spec.get('max_total_seconds', 60) == 60 and spec.get('volume_level', .15) == .15,
            'fixed_limits_required')
    return spec


def validate_attestation(spec_raw, freeze_raw, expected_sha, attestation, target, now):
    require(digest(freeze_raw) == expected_sha, 'freeze_digest_mismatch')
    require(attestation.get('schema_version') == 1 and attestation.get('collector_verified') is True,
            'collector_attestation_required')
    require(attestation.get('candidate_sha256') == expected_sha
            and attestation.get('test_spec_sha256') == digest(spec_raw)
            and attestation.get('target') == target, 'attestation_binding_mismatch')
    start, end = attestation.get('generated_at_unix'), attestation.get('expires_at_unix')
    require(type(start) in (int, float) and type(end) in (int, float)
            and math.isfinite(start) and math.isfinite(end)
            and 0 <= now - start <= 60 and 60 <= end - now
            and 0 < end - start <= 120, 'attestation_expired_or_invalid')


def flow_matches(source, origin, entity, item_id):
    """MA 2.10 flow grammar, verified against the installed source before use."""
    try:
        parsed, base = urlsplit(source), urlsplit(origin)
        if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
            return False
        parts = unquote(parsed.path).split('/')
        return (len(parts) == 6 and parts[0] == '' and parts[1] == 'flow'
                and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', parts[2]) is not None
                and parts[3] == entity and parts[4] == item_id
                and parts[5].rsplit('.', 1)[0] == entity
                and parts[5].rsplit('.', 1)[-1] in ('mp3', 'flac', 'wav', 'aac', 'ogg', 'pcm'))
    except (TypeError, ValueError):
        return False


def channel_ids(phone):
    return sorted(str(item.get('uniqueid')) for item in phone.get('channels', [])
                  if item.get('uniqueid'))


def unrelated(phones, target):
    return {phone['id']: {'session': phone.get('player', {}).get('session_id'),
                          'uids': channel_ids(phone)} for phone in phones if phone['id'] != target}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Refused('redirect_refused')


class Client:
    def __init__(self, spec, credentials, deadline):
        self.spec, self.credentials, self.deadline = spec, credentials, deadline
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, kind, path, data=None, *, timeout=2):
        remaining = self.deadline - time.monotonic()
        require(remaining > .2, 'deadline_reached')
        token = self.credentials.get(kind + '_token')
        require(isinstance(token, str) and token, 'credential_missing')
        request = urllib.request.Request(self.spec['connections'][kind].rstrip('/') + path,
            data=None if data is None else json.dumps(data).encode(),
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=min(timeout, remaining)) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                require(len(raw) <= 2 * 1024 * 1024, 'oversized_response')
                return json.loads(raw)
        except urllib.error.HTTPError as error:
            raise Refused('http_' + str(error.code)) from None

    def phones(self):
        return self.request('gateway', '/v1/phones')['phones']

    def gateway(self, operation, session=None, **values):
        return self.request('gateway', '/v1/phones/' + self.spec['target'] + '/player_' + operation,
            {'line': self.spec['target'], 'session_id': session,
             'idempotency_key': uuid.uuid4().hex, **values})

    def ha(self, operation, **values):
        return self.request('ha', '/api/services/media_player/' + operation,
                            {'entity_id': self.spec['entity_id'], **values},
                            timeout=DISPATCH_SECONDS if operation == 'play_media' else 2)

    def ma(self, command, **values):
        return self.request('ma', '/api', {'message_id': uuid.uuid4().hex,
                            'command': command, 'args': values},
                            timeout=DISPATCH_SECONDS if command == 'player_queues/play_media' else 2)

    def fixture(self):
        """Verify the exact finite bytes from this consumer host before any mutation."""
        fixture = self.spec['fixture']
        request = urllib.request.Request(fixture['url'], headers={'Accept-Encoding': 'identity'})
        with self.opener.open(request, timeout=min(2, self.deadline - time.monotonic())) as response:
            require(response.getheader('Content-Length') == str(fixture['bytes']), 'finite_fixture_required')
            raw = response.read(fixture['bytes'] + 1)
            require(len(raw) == fixture['bytes'] and digest(raw) == fixture['sha256'],
                    'served_fixture_changed')


class Run:
    def __init__(self, spec, client, journal, clock=time.monotonic, sleep=time.sleep):
        self.spec, self.client, self.journal = spec, client, journal
        self.clock, self.sleep = clock, sleep
        self.state = {'schema_version': 1, 'target': spec['target'], 'route': spec['route'],
                      'test_id': spec['test_id'], 'dispatched': False, 'owned': None,
                      'queue_item_id': None, 'checks': {}, 'cleanup': {},
                      'human_confirmation': {'heard_expected_phrase': 'pending',
                                             'speech_quality': 'pending', 'phone_quiet': 'pending'}}

    def save(self):
        self.journal(self.state)

    def snapshot(self, *, check_unrelated=True):
        phones = self.client.phones()
        selected = [phone for phone in phones if phone['id'] == self.spec['target']]
        require(len(selected) == 1, 'target_not_unique')
        if check_unrelated and 'unrelated' in self.state:
            require(unrelated(phones, self.spec['target']) == self.state['unrelated'],
                    'unrelated_session_changed')
        return selected[0], phones

    def queue(self):
        entity = self.spec['entity_id']
        return (self.client.ma('player_queues/get', queue_id=entity),
                self.client.ma('player_queues/items', queue_id=entity, limit=3))

    def bind(self, phone):
        player = phone.get('player', {})
        session = player.get('session_id')
        if not session:
            return False
        require(player.get('line') == self.spec['target'], 'foreign_player_line')
        uids = channel_ids(phone)
        source = player.get('source_id')
        pending = {'session': session, 'source': source, 'line': player['line']}
        require(self.state.get('pending') in (None, pending), 'pending_session_changed')
        if self.state['owned'] is not None:
            require(len(uids) == 1, 'ambiguous_player_channel')
            owned = {**pending, 'uid': uids[0]}
            require(self.state['owned'] == owned, 'owned_session_changed')
            return True
        if self.spec['route'] == 'ha':
            require(source == clean_url(self.spec['fixture']['url']), 'foreign_player_source')
        else:
            queue, items = self.queue()
            require(len(items) == 1 and queue.get('items') == 1
                    and items[0].get('media_item', {}).get('item_id') == self.spec['fixture']['url'],
                    'foreign_queue_item')
            item_id = items[0].get('queue_item_id')
            require(isinstance(item_id, str) and item_id, 'queue_item_identity_missing')
            previous = self.state.get('queue_item_id')
            require(previous in (None, item_id), 'queue_item_changed')
            require(flow_matches(source, self.spec['connections']['ma_stream'],
                                 self.spec['entity_id'], item_id), 'foreign_ma_flow')
            self.state['queue_item_id'] = item_id
        self.state['pending'] = pending
        self.save()
        # The gateway changes buffering to playing only after validating the
        # SIP marker/UID. A matching pre-call session can be cancelled by its
        # exact token, but an unverified channel must never be called owned.
        if player.get('state') not in ('playing', 'paused') or not uids:
            return False
        require(len(uids) == 1, 'ambiguous_player_channel')
        owned = {**pending, 'uid': uids[0]}
        self.state['owned'] = owned
        self.save()
        return True

    def preflight(self):
        self.client.fixture()
        phone, phones = self.snapshot()
        player = phone.get('player', {})
        require(phone.get('online') and player.get('state') == 'idle'
                and player.get('session_id') is None and not channel_ids(phone), 'target_not_idle')
        require(type(player.get('volume_level')) in (int, float)
                and 0 <= player['volume_level'] <= 1 and type(player.get('is_volume_muted')) is bool,
                'preferences_missing')
        self.state['before'] = {'volume': player['volume_level'], 'muted': player['is_volume_muted']}
        self.state['unrelated'] = unrelated(phones, self.spec['target'])
        if self.spec['route'] == 'ma':
            queue, items = self.queue()
            require(queue and queue.get('state') == 'idle' and queue.get('items') == 0 and items == [],
                    'queue_not_empty_idle')
            require(type(queue.get('autoplay_enabled')) is bool, 'autoplay_state_missing')
            require(self.client.ma('players/get', player_id=self.spec['entity_id']).get('available'),
                    'ma_player_unavailable')
            self.state['before']['autoplay'] = queue['autoplay_enabled']
        self.save()

    def perform(self, stop_at):
        try:
            self.preflight()
            require(self.clock() < stop_at - DISPATCH_SECONDS
                    - self.spec['fixture']['duration_seconds'] - 5,
                    'insufficient_start_budget')
            # Journal intent before each mutating phase; watchdog can restore a
            # preference even when a response is lost after the server applies it.
            self.state['preferences_changed'] = True
            self.save()
            self.client.gateway('volume', volume_level=.15)
            self.client.gateway('mute', is_volume_muted=False)
            if self.spec['route'] == 'ma':
                self.state['autoplay_changed'] = True
                self.save()
                self.client.ma('player_queues/autoplay', queue_id=self.spec['entity_id'], autoplay_enabled=False)
                queue, items = self.queue()
                require(queue.get('autoplay_enabled') is False and queue.get('items') == 0 and items == [],
                        'autoplay_disable_or_queue_preflight_failed')
            # Recheck after preference/queue I/O: never begin a potentially
            # slow dial when its dispatch and finite speech no longer fit.
            require(self.clock() < stop_at - DISPATCH_SECONDS
                    - self.spec['fixture']['duration_seconds'] - 5,
                    'insufficient_dispatch_budget')
            self.state['dispatched'] = True
            self.save()
            if self.spec['route'] == 'ha':
                self.client.ha('play_media', media_content_id=self.spec['fixture']['url'],
                               media_content_type='music', extra={'metadata': {
                                   'title': self.spec['fixture']['title'],
                                   'duration': self.spec['fixture']['duration_seconds']}})
            else:
                self.client.ma('player_queues/play_media', queue_id=self.spec['entity_id'],
                               media=[self.spec['fixture']['url']], option='replace', shuffle=False)
            while self.clock() < stop_at:
                phone, _ = self.snapshot()
                player = phone.get('player', {})
                if not player.get('session_id') and self.state['owned']:
                    self.state['checks']['completed_or_stopped'] = True
                    break
                if player.get('session_id'):
                    self.bind(phone)
                    if player.get('state') == 'playing':
                        self.state['checks']['playing'] = True
                        self.state['checks']['expected_title'] = player.get('title') == self.spec['fixture']['title']
                        self.state['checks']['low_volume_unmuted'] = (
                            abs(player.get('volume_level', -1) - .15) < .001
                            and player.get('is_volume_muted') is False)
                        self.save()
                self.sleep(.25)
        except Exception as error:
            self.state['error'] = str(error) if isinstance(error, Refused) else type(error).__name__
            self.save()
        finally:
            self.cleanup()
        return self.state

    def cleanup(self):
        """Never stop or restore preferences on a mismatching replacement call."""
        if 'before' not in self.state:
            return
        try:
            phone, _ = self.snapshot(check_unrelated=False)
            if (self.state.get('dispatched') and self.state.get('owned') is None
                    and not phone.get('player', {}).get('session_id')):
                # A timed-out start may still complete on the server. Observe
                # a bounded late-arrival window, never dispatch it a second time
                # or declare a missing unobserved session successfully stopped.
                until = self.clock() + 8
                while self.clock() < until and not phone.get('player', {}).get('session_id'):
                    self.sleep(.25)
                    phone, _ = self.snapshot(check_unrelated=False)
                require(phone.get('player', {}).get('session_id'), 'start_outcome_unknown')
            if phone.get('player', {}).get('session_id'):
                require(self.state.get('dispatched'), 'unexpected_session_before_dispatch')
                bound = self.bind(phone)
                owned = self.state['owned'] if bound else self.state['pending']
                self.client.gateway('stop', session=owned['session'])
                if not bound:
                    self.state['cleanup']['pre_call_session_cancelled'] = True
                self.state['checks']['completed_or_stopped'] = True
            stop_wait = self.clock() + 3
            phone, _ = self.snapshot(check_unrelated=False)
            while (not phone.get('player', {}).get('session_id') and channel_ids(phone)
                   and self.state.get('owned')
                   and channel_ids(phone) == [self.state['owned']['uid']]
                   and self.clock() < stop_wait):
                self.sleep(.2)
                phone, _ = self.snapshot(check_unrelated=False)
            player = phone.get('player', {})
            require(player.get('session_id') is None and not channel_ids(phone), 'owned_stop_not_confirmed')
            self.state['cleanup']['owned_call_stopped'] = True
            if self.spec['route'] == 'ma' and self.state.get('autoplay_changed'):
                queue, items = self.queue()
                if items:
                    require(len(items) == 1 and queue.get('items') == 1
                            and items[0].get('media_item', {}).get('item_id') == self.spec['fixture']['url'],
                            'foreign_queue_during_cleanup')
                    item_id = items[0].get('queue_item_id')
                    require(self.state.get('queue_item_id') in (None, item_id), 'queue_changed_during_cleanup')
                    # MA 2.10 waits five seconds after the player becomes idle
                    # before clearing its committed buffer at normal EOF. A
                    # delete sent earlier is silently ignored. Observe, never
                    # retry the mutation, and preserve exact item ownership.
                    settle_until = self.clock() + 8
                    while (queue.get('state') != 'idle'
                           or 'index_in_buffer' not in queue
                           or queue['index_in_buffer'] is not None):
                        require(self.clock() < settle_until, 'queue_not_settled')
                        self.sleep(.25)
                        phone, _ = self.snapshot(check_unrelated=False)
                        require(phone.get('player', {}).get('session_id') is None
                                and not channel_ids(phone), 'session_changed_while_queue_settling')
                        queue, items = self.queue()
                        require(queue.get('items') == 1 and len(items) == 1
                                and items[0].get('queue_item_id') == item_id
                                and items[0].get('media_item', {}).get('item_id')
                                == self.spec['fixture']['url'], 'queue_changed_while_settling')
                    # Exact item deletion cannot delete a concurrent foreign
                    # append. MA may refuse a buffered item; report it pending
                    # instead of falling back to whole-queue clear.
                    again, again_items = self.queue()
                    require([i.get('queue_item_id') for i in again_items] == [item_id]
                            and again.get('items') == 1 and again.get('state') == 'idle'
                            and 'index_in_buffer' in again and again['index_in_buffer'] is None,
                            'queue_changed_before_clear')
                    self.client.ma('player_queues/delete_item', queue_id=self.spec['entity_id'],
                                   item_id_or_index=item_id)
                queue, items = self.queue()
                require(items == [] and queue.get('items') == 0, 'queue_clear_not_confirmed')
                self.state['cleanup']['queue_cleared'] = True
                self.client.ma('player_queues/autoplay', queue_id=self.spec['entity_id'],
                               autoplay_enabled=self.state['before']['autoplay'])
                queue, _ = self.queue()
                require(queue.get('autoplay_enabled') == self.state['before']['autoplay'],
                        'autoplay_restore_not_confirmed')
                self.state['cleanup']['autoplay_restored'] = True
            if self.state.get('preferences_changed'):
                self.client.gateway('volume', volume_level=self.state['before']['volume'])
                self.client.gateway('mute', is_volume_muted=self.state['before']['muted'])
                phone, phones = self.snapshot(check_unrelated=False)
                player = phone['player']
                require(player.get('session_id') is None
                        and abs(player.get('volume_level', -1) - self.state['before']['volume']) < .001
                        and player.get('is_volume_muted') == self.state['before']['muted'],
                        'preference_restore_not_confirmed')
                self.state['cleanup']['preferences_restored'] = True
            _, phones = self.snapshot(check_unrelated=False)
            self.state['cleanup']['unrelated_sessions_unchanged'] = (
                unrelated(phones, self.spec['target']) == self.state['unrelated'])
        except Exception as error:
            self.state['cleanup']['error'] = str(error) if isinstance(error, Refused) else type(error).__name__
        self.save()


def wait_for_worker(worker):
    """Bound the control process, preserving journal recovery on an ESRCH race."""
    try:
        worker.wait(timeout=45)
        return False, False
    except (subprocess.TimeoutExpired, KeyboardInterrupt, InterruptedError) as error:
        try:
            os.killpg(worker.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # It exited between timeout/interrupt and kill; still recover.
        worker.wait(timeout=1)
        return True, not isinstance(error, subprocess.TimeoutExpired)


def cleanup_worker(spec, credentials, deadline, journal, state):
    """Isolate even cleanup DNS/HTTP hangs behind the parent's final deadline."""
    run = Run(spec, Client(spec, credentials, deadline), lambda value: atomic_json(journal, value))
    run.state = state
    run.cleanup()


def inputs(args):
    raw = read_private(args.manifest)
    spec = validate_spec(json.loads(raw))
    freeze = read_private(args.freeze)
    require(digest(freeze) == args.freeze_sha256, 'freeze_digest_mismatch')
    if args.run or args.worker:
        require(args.attended, 'attendance_required')
        validate_attestation(raw, freeze, args.freeze_sha256,
                             json.loads(read_private(args.attestation)), spec['target'], time.time())
    return spec


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--freeze', type=Path, required=True)
    parser.add_argument('--freeze-sha256', required=True)
    parser.add_argument('--attestation', type=Path)
    parser.add_argument('--credentials', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--attended', action='store_true')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--deadline', type=float, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    spec = inputs(args)
    if not args.run and not args.worker:
        print(json.dumps({'mode': 'plan', 'target': spec['target'], 'route': spec['route'],
                          'network_actions': 0, 'audio_started': False,
                          'candidate_sha256': args.freeze_sha256,
                          'human_confirmation': 'pending'}))
        return 0
    require(args.credentials and args.attestation, 'runtime_private_inputs_required')
    credentials = json.loads(read_private(args.credentials))
    journal = args.output_dir / 'journal.private.json'
    if args.worker:
        require(args.deadline is not None and 0 < args.deadline - time.monotonic() <= 60,
                'worker_deadline_required')
        client = Client(spec, credentials, args.deadline)
        run = Run(spec, client, lambda state: atomic_json(journal, state))
        run.perform(args.deadline - (MAX_SECONDS - WORK_SECONDS))
        return 0
    require(args.output_dir.is_absolute() and not args.output_dir.exists(), 'new_output_directory_required')
    args.output_dir.mkdir(mode=0o700)
    deadline = time.monotonic() + MAX_SECONDS
    command = [sys.executable, str(Path(__file__).resolve()), '--worker', '--attended',
               '--manifest', str(args.manifest), '--freeze', str(args.freeze),
               '--freeze-sha256', args.freeze_sha256, '--attestation', str(args.attestation),
               '--credentials', str(args.credentials), '--output-dir', str(args.output_dir),
               '--deadline', str(deadline)]
    worker = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True)
    watchdog = False
    interrupted = False
    def interrupt(_signum, _frame):
        raise InterruptedError('operator_interrupted')
    old_handlers = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        watchdog, interrupted = wait_for_worker(worker)
    finally:
        for sig in old_handlers:
            signal.signal(sig, signal.SIG_IGN)  # Finish bounded cleanup on repeated interrupts.
    require(journal.exists(), 'worker_failed_before_journal')
    state = json.loads(read_private(journal))
    if watchdog or state.get('cleanup', {}).get('error'):
        cleanup = multiprocessing.get_context('spawn').Process(
            target=cleanup_worker, args=(spec, credentials, deadline, journal, state))
        cleanup.start()
        cleanup.join(timeout=max(0, deadline - time.monotonic() - .2))
        if cleanup.is_alive():
            cleanup.kill()
            cleanup.join(timeout=.2)
            state = json.loads(read_private(journal))
            state['cleanup']['error'] = 'cleanup_deadline_reached'
        else:
            state = json.loads(read_private(journal))
    required_cleanup = ['owned_call_stopped', 'preferences_restored', 'unrelated_sessions_unchanged']
    if spec['route'] == 'ma':
        required_cleanup += ['queue_cleared', 'autoplay_restored']
    state['watchdog_triggered'] = watchdog
    state['operator_interrupted'] = interrupted
    state['elapsed_seconds'] = round(MAX_SECONDS - (deadline - time.monotonic()), 3)
    state['candidate_sha256'] = args.freeze_sha256
    state['test_spec_sha256'] = digest(read_private(args.manifest))
    state['machine_passed'] = (not state.get('error') and not state['cleanup'].get('error')
        and not watchdog and state['elapsed_seconds'] <= 60
        and all(state['checks'].get(key) for key in ('playing', 'expected_title', 'low_volume_unmuted'))
        and all(state['cleanup'].get(key) for key in required_cleanup))
    # Persist private exact identities, print only a small, non-sensitive summary.
    atomic_json(args.output_dir / 'receipt.private.json', state)
    for sig, handler in old_handlers.items():
        signal.signal(sig, handler)
    print(json.dumps({key: state[key] for key in
        ('target', 'route', 'machine_passed', 'watchdog_triggered', 'elapsed_seconds', 'human_confirmation')}))
    return 0 if state['machine_passed'] else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'status': 'refused', 'reason': str(error) if isinstance(error, Refused)
                          else type(error).__name__, 'human_confirmation': 'pending'}))
        raise SystemExit(2) from None
