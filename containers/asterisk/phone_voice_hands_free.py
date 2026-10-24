"""Opt-in desired-state supervisor for finite, exclusively owned SIP voice legs.

This module performs no phone I/O itself. Fresh idle observations and trusted
per-phone authorization are supplied by the gateway. It never clears a finite
session's ownership journal or lock.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time

QUIET_SECONDS = 2.0
POLL_SECONDS = 0.25
MAX_FAILURES = 3
HISTORY_SIZE = 64
BLOOM_BYTES = 1024
MAX_RECORD = 32768
CONTROL_WAIT_SECONDS = 30.0
ACTIVE = frozenset(('starting', 'running', 'stopping'))
PUBLIC_ERRORS = frozenset(('hands_free_state_unavailable', 'hands_free_owner_unavailable',
    'hands_free_recovery_required', 'hands_free_observation_failed',
    'hands_free_retry_exhausted', 'hands_free_start_failed'))
MANUAL_END = frozenset(('peer_ended', 'dtmf_stop', 'call_changed', 'explicit_stop',
                        'display_stop', 'user_stop', 'peer_closed'))


@dataclass
class _Desired:
    profile: object
    line: str
    enabled: bool = False
    state: str = 'disabled'
    generation: str = field(default_factory=lambda: secrets.token_hex(16))
    failures: int = 0
    error: str | None = None
    pending: bool = False
    control_pending: dict | None = None
    control_deadline: float = 0.0
    history: list = field(default_factory=list)
    committed_history: list = field(default_factory=list)
    committed_seen: bytes = field(default_factory=lambda: bytes(BLOOM_BYTES))
    seen: bytearray = field(default_factory=lambda: bytearray(BLOOM_BYTES))
    fd: int | None = None
    session: object = None
    retry_at: float = 0.0
    retry_pending: bool = False
    idle_since: float | None = None
    unknowns: int = 0
    suspended: int = 0
    control_contexts: list = field(default_factory=list)
    blocked: bool = False
    storage_failed: bool = False
    launching: bool = False


class HandsFreeSupervisor:
    """One desired-state owner per physical phone, sharing VoiceControl's RLock."""

    def __init__(self, manager, observe, context_for, *, monitor=True):
        self.manager, self.observe, self.context_for = manager, observe, context_for
        self.clock = manager.clock
        self._entries = {p.phone_id: _Desired(p, p.phone_id) for p in manager.config.profiles}
        self._closed = False
        manager.hands_free = self
        self._wake = threading.Event()
        self._pass_lock = threading.Lock()
        self._thread = None
        with manager._lock:
            for entry in self._entries.values():
                try:
                    self._load(entry)
                    if entry.enabled or entry.control_pending is not None:
                        self._own(entry)
                except Exception as exc:
                    self._fault(entry, self._exception_category(exc), persist=False)
        if monitor:
            self._thread = threading.Thread(target=self._monitor, name='voice-hands-free', daemon=True)
            self._thread.start()

    def _error(self, reason, status=409):
        return sys.modules[type(self.manager).__module__].ControlError(reason, status)

    def _require(self, condition, reason, status=409):
        if not condition:
            raise self._error(reason, status)

    @staticmethod
    def _exception_category(exc):
        return ('hands_free_owner_unavailable' if getattr(exc, 'reason', None) ==
                'hands_free_owner_unavailable' else 'hands_free_recovery_required')

    def _paths(self, entry):
        # VoiceControl validates the private directory and physical identity.
        base = self.manager._paths(entry.profile)[0].parent
        key = hashlib.sha256(entry.profile.device.encode()).hexdigest()
        return base / (key + '.hands-free.lock'), base / (key + '.hands-free.json')

    def _eligible(self, entry):
        p = entry.profile
        self._require(self.manager.config.enabled, 'voice_control_disabled', 403)
        self._require(p.media_transport == 'sip_audiosocket' and p.mode == 'wake'
                      and p.rearm_after_turn is True and p.duration >= 30,
                      'hands_free_not_supported', 400)

    def _acquire(self, entry):
        if entry.fd is not None:
            return
        self.manager._mkdir()
        path, _ = self._paths(entry)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            self._require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                          and not info.st_mode & 0o077, 'hands_free_state_unavailable', 503)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise self._error('hands_free_owner_unavailable') from None
            entry.fd = fd
        except Exception:
            os.close(fd)
            raise

    def _own(self, entry):
        if entry.fd is not None:
            return
        self._acquire(entry)
        try:
            self._load(entry)
            if entry.enabled or entry.control_pending is not None:
                self._restore(entry)
        except Exception:
            self._release(entry)
            self._fault(entry, 'hands_free_recovery_required', persist=False)
            raise self._error('hands_free_recovery_required') from None

    @staticmethod
    def _unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result

    def _load(self, entry):
        _, path = self._paths(entry)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            self._require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                          and not info.st_mode & 0o077 and info.st_size <= MAX_RECORD,
                          'hands_free_state_unavailable', 503)
            raw = source.read(MAX_RECORD + 1)
        data = json.loads(raw, object_pairs_hook=self._unique)
        fields = {'version', 'phone', 'line', 'enabled', 'generation', 'failures',
                  'error', 'pending', 'history', 'seen'}
        self._require(type(data) is dict and fields <= set(data) <= fields | {'control_pending', 'retry_pending'}
                      and type(data['version']) is int and data['version'] == 1
                      and data['phone'] == entry.profile.phone_id
                      and data['line'] in entry.profile.lines
                      and type(data['enabled']) is bool and type(data['pending']) is bool
                      and type(data['generation']) is str
                      and re.fullmatch('[0-9a-f]{32}', data['generation'])
                      and type(data['failures']) is int and 0 <= data['failures'] <= MAX_FAILURES
                      and (data['error'] is None or type(data['error']) is str and data['error'] in PUBLIC_ERRORS)
                      and type(data['history']) is list and len(data['history']) <= HISTORY_SIZE
                      and type(data['seen']) is str and re.fullmatch('[0-9a-f]{2048}', data['seen']),
                      'hands_free_recovery_required')
        for item in data['history']:
            self._require(type(item) is dict and set(item) == {'id', 'action'}
                          and type(item['id']) is str and re.fullmatch('[0-9a-f]{64}', item['id'])
                          and item['action'] in ('enable', 'disable'), 'hands_free_recovery_required')
        self._require(len({item['id'] for item in data['history']}) == len(data['history']),
                      'hands_free_recovery_required')
        ticket = data.get('control_pending')
        self._require(ticket is None or type(ticket) is dict
            and set(ticket) == {'id', 'generation', 'observed'}
            and all(type(ticket[key]) is str and re.fullmatch('[0-9a-f]{32}', ticket[key])
                    for key in ('id', 'generation'))
            and type(ticket['observed']) is bool, 'hands_free_recovery_required')
        retry_pending = data.get('retry_pending', False)
        self._require(type(retry_pending) is bool and (not retry_pending or
            data['enabled'] and data['failures'] in (1, 2)
            and data['error'] == 'hands_free_start_failed' and not data['pending']
            and ticket is None), 'hands_free_recovery_required')
        entry.retry_pending = retry_pending
        entry.retry_at = self.clock() + 5 * (2 ** (data['failures'] - 1)) if retry_pending else 0
        entry.control_pending, entry.control_deadline = ticket, 0.0
        entry.line, entry.enabled = data['line'], data['enabled']
        entry.generation, entry.failures = data['generation'], data['failures']
        entry.error, entry.pending = data['error'], data['pending']
        entry.history, entry.seen = data['history'], bytearray.fromhex(data['seen'])
        entry.committed_history, entry.committed_seen = list(entry.history), bytes(entry.seen)
        entry.state = ('backoff' if retry_pending else 'faulted' if entry.error
                       else 'waiting_idle' if entry.enabled else 'disabled')
        entry.blocked = entry.error is not None and not retry_pending

    def _persist(self, entry):
        self._require(entry.fd is not None, 'hands_free_owner_unavailable')
        _, path = self._paths(entry)
        data = {'version': 1, 'phone': entry.profile.phone_id, 'line': entry.line,
                'enabled': entry.enabled, 'generation': entry.generation, 'failures': entry.failures,
                'error': entry.error, 'pending': entry.pending, 'control_pending': entry.control_pending,
                'retry_pending': entry.retry_pending,
                'history': entry.history, 'seen': entry.seen.hex()}
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix='.hands-free-', dir=path.parent)
            with os.fdopen(fd, 'w') as stream:
                json.dump(data, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            entry.committed_history, entry.committed_seen = list(entry.history), bytes(entry.seen)
            entry.storage_failed = False
        except OSError:
            entry.retry_pending = False
            entry.storage_failed = True
            entry.history, entry.seen = list(entry.committed_history), bytearray(entry.committed_seen)
            entry.blocked, entry.state, entry.error = True, 'faulted', 'hands_free_state_unavailable'
            raise self._error('hands_free_state_unavailable', 503) from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def _restore(self, entry):
        self._eligible(entry)
        prior = self.manager._journal(entry.profile)
        # An interrupted pending attempt cannot distinguish a physical hangup
        # from an intended renewal. Require explicit recovery instead of redial.
        if entry.pending or entry.control_pending or prior and prior.get('reserved') is not False:
            self._fault(entry, 'hands_free_recovery_required')
        elif entry.retry_pending:
            entry.state = 'backoff'
        elif not entry.error:
            entry.state = 'waiting_idle'

    def _fault(self, entry, error, *, persist=True):
        entry.retry_pending = False
        entry.state, entry.error, entry.blocked = 'faulted', error, True
        entry.idle_since = None
        if persist and entry.fd is not None:
            try:
                self._persist(entry)
            except Exception:
                pass

    def _view(self, entry, line):
        session = entry.session
        return {'schema_version': 1, 'phone': entry.profile.phone_id, 'line': entry.line,
                'enabled': entry.enabled, 'state': entry.state,
                'session_id': session.session_id if session is not None else None,
                'session_seconds': entry.profile.duration, 'consecutive_failures': entry.failures,
                'retry_after_seconds': round(max(0, entry.retry_at - self.clock()), 1)
                    if entry.state == 'backoff' else 0,
                'error': entry.error}

    @staticmethod
    def _positions(digest):
        raw = bytes.fromhex(digest)
        return [int.from_bytes(raw[i:i + 2], 'big') % (BLOOM_BYTES * 8) for i in (0, 2, 4, 6)]

    def _request(self, entry, request_id, action):
        self._require(type(request_id) is str and re.fullmatch('[A-Za-z0-9_.:-]{8,128}', request_id),
                      'voice_request_id_required', 400)
        digest = hashlib.sha256(request_id.encode()).hexdigest()
        for item in entry.history:
            if item['id'] == digest:
                self._require(item['action'] == action, 'voice_request_id_conflict')
                return False
        positions = self._positions(digest)
        if all(entry.seen[n // 8] & (1 << (n % 8)) for n in positions):
            # Fixed-size tombstones have no false negatives: replaying even an
            # evicted Enable can never undo a later Disable. A rare collision
            # refuses a fresh operation; callers can supply a new request key.
            raise self._error('voice_request_id_conflict')
        return digest

    def _remember(self, entry, digest, action):
        for n in self._positions(digest):
            entry.seen[n // 8] |= 1 << (n % 8)
        entry.history = (entry.history + [{'id': digest, 'action': action}])[-HISTORY_SIZE:]

    def enable(self, target, request_id, *, context, enable_microphone=False,
               exclusive_media=False, dry_run=False):
        p = self.manager._profile(target, context, 'hands_free_enable')
        with self.manager._lock:
            self._require(not self._closed and not self.manager._closed, 'voice_control_closed', 503)
            self._require(p.phone_id not in self.manager._recovering, 'voice_phone_busy')
            entry = self._entries[p.phone_id]
            self._eligible(entry)
            self._require(enable_microphone is True and exclusive_media is True,
                          'explicit_voice_start_required', 400)
            self._require(type(dry_run) is bool, 'invalid_voice_control_config', 400)
            self._require(type(request_id) is str and re.fullmatch('[A-Za-z0-9_.:-]{8,128}', request_id),
                          'voice_request_id_required', 400)
            if dry_run:
                return {'schema_version': 1, 'phone': p.phone_id, 'line': target, 'valid': True,
                        'network_actions': 0, 'microphone_started': False,
                        'duration_seconds': p.duration}
            self._own(entry)
            digest = self._request(entry, request_id, 'enable')
            if not digest:
                return self._view(entry, target)
            self._require(entry.control_pending is None, 'hands_free_recovery_required')
            if not entry.enabled or entry.blocked:
                current = self.manager._current.get(p.phone_id)
                prior = self.manager._journal(p)
                self._require(not current or current.lease_fd < 0, 'voice_phone_busy')
                self._require(not prior or prior.get('reserved') is False,
                              'voice_recovery_required')
                entry.enabled, entry.line = True, target
                entry.generation = secrets.token_hex(16)
                entry.failures, entry.unknowns = 0, 0
                entry.error, entry.blocked, entry.pending = None, False, False
                entry.session, entry.idle_since = None, None
                entry.retry_pending = False
                entry.retry_at, entry.state = 0, 'waiting_idle'
            self._remember(entry, digest, 'enable')
            self._persist(entry)
            self._wake.set()
            return self._view(entry, target)

    def disable(self, target, request_id, *, context):
        p = self.manager._profile(target, context, 'hands_free_disable')
        with self.manager._lock:
            self._require(p.phone_id not in self.manager._recovering, 'voice_phone_busy')
            entry = self._entries[p.phone_id]
            self._own(entry)
            digest = self._request(entry, request_id, 'disable')
            if not digest:
                return self._view(entry, target)
            self._remember(entry, digest, 'disable')
            self._disable(entry)
            return self._view(entry, target)

    def _disable(self, entry):
        self._require(entry.profile.phone_id not in self.manager._recovering, 'voice_phone_busy')
        prior_enabled = entry.enabled
        entry.enabled, entry.generation = False, secrets.token_hex(16)
        entry.retry_pending = False
        entry.idle_since, entry.retry_at = None, 0
        entry.error, entry.blocked = None, False
        session = entry.session
        entry.state = 'stopping' if session is not None and session.lease_fd >= 0 else 'disabled'
        try:
            self._persist(entry)  # Desired state is durable before signalling.
        except Exception:
            entry.enabled = prior_enabled
            raise
        finally:
            if session is not None and session.lease_fd >= 0:
                self.manager._request_stop(session, reason='explicit_stop')
            self._wake.set()

    def manual_stop(self, phone_id, *, session_id=None):
        with self.manager._lock:
            entry = self._entries.get(phone_id)
            if entry is not None and (entry.enabled or entry.pending):
                if session_id is not None and (entry.session is None
                        or entry.session.session_id != session_id
                        or self.manager._current.get(phone_id) is not entry.session):
                    return
                self._disable(entry)

    def status(self, target, *, context):
        p = self.manager._profile(target, context, 'hands_free_status')
        with self.manager._lock:
            return self._view(self._entries[p.phone_id], target)

    def _notification_idle(self, entry):
        self._require(not entry.enabled, 'hands_free_enabled')
        self._require(not entry.blocked and not entry.storage_failed
                      and entry.error is None, 'voice_state_unavailable')
        session = entry.session
        self._require(not entry.pending and not entry.launching
                      and not entry.retry_pending and entry.control_pending is None
                      and not entry.suspended
                      and (session is None or session.lease_fd < 0
                           and session.state not in ACTIVE and session.cleanup_verified),
                      'voice_phone_busy')

    @contextmanager
    def reserve_idle_notification(self, phone_id):
        """Admit a passive image only while supervised capture is disabled.

        Hold the desired-state owner fence and inhibit launch during the image
        request. Unlike call handoff this never stops a session or edits its
        journal. The caller must additionally reserve the finite voice lease.
        A later explicit enable may proceed after the request; the native image
        is idle-only and its close timer names only that image application.
        """
        entry, acquired, entered = None, False, False
        with self.manager._lock:
            self._require(not self._closed and not self.manager._closed,
                          'voice_control_closed', 503)
            self._require(phone_id not in self.manager._recovering, 'voice_phone_busy')
            entry = self._entries.get(phone_id)
            if entry is not None:
                self._notification_idle(entry)
                if entry.fd is None:
                    # A status cached while another process owned the phone is
                    # not authoritative. Read under its owner flock, without
                    # _own/_restore: refusal must not repair or persist state.
                    probe = _Desired(entry.profile, entry.line)
                    try:
                        self._acquire(probe)
                        self._load(probe)
                        self._notification_idle(probe)
                        self._load(entry)
                        entry.fd, probe.fd = probe.fd, None
                        acquired = True
                    finally:
                        self._release(probe)
                entry.suspended += 1
                entered = True
        try:
            yield
        finally:
            if entered:
                with self.manager._lock:
                    entry.suspended -= 1
                    # Concurrent explicit enable retains its normal ownership;
                    # otherwise the read-only probe leaves no lasting owner.
                    if acquired and not entry.enabled and not entry.pending \
                            and entry.control_pending is None and not entry.launching \
                            and not entry.suspended and entry.session is None:
                        self._release(entry)
                    self._wake.set()

    def admit(self, phone_id, generation):
        with self.manager._lock:
            self._require(phone_id not in self.manager._recovering, 'voice_phone_busy')
            entry = self._entries[phone_id]
            if generation is None:
                self._own(entry)
                self._require(not entry.enabled and not entry.launching and not entry.blocked
                              and entry.control_pending is None,
                              'hands_free_enabled')
                return
            self._require(not self._closed and entry.enabled and not entry.blocked
                          and entry.fd is not None and not entry.suspended
                          and entry.launching and entry.generation == generation,
                          'hands_free_start_cancelled')

    def started(self, session, generation):
        if generation is not None:
            with self.manager._lock:
                entry = self._entries[session.profile.phone_id]
                entry.session = session
                entry.state = 'starting'

    def _retry(self, entry, error='hands_free_start_failed'):
        entry.failures = min(MAX_FAILURES, entry.failures + 1)
        entry.pending, entry.idle_since = False, None
        if entry.failures >= MAX_FAILURES:
            self._fault(entry, 'hands_free_retry_exhausted')
        else:
            entry.retry_pending = True
            entry.error = error
            entry.state = 'backoff'
            entry.retry_at = self.clock() + 5 * (2 ** (entry.failures - 1))
            self._persist(entry)

    def _settle(self, entry):
        if entry.profile.phone_id in self.manager._recovering:
            return
        session = entry.session
        if session is None:
            return
        if session.lease_fd >= 0 or session.state in ACTIVE:
            if (entry.enabled and not self._closed and not session.stop_requested
                    and session.started is not None
                    and self.clock() - session.started >= session.profile.duration - 5):
                self.manager._request_stop(session, reason='lease_expired')
            entry.state = 'stopping' if session.stop_requested else session.state
            return
        if not session.cleanup_verified:
            self._fault(entry, 'hands_free_recovery_required')
            return
        entry.pending = False
        reason = getattr(session, 'completion_reason', None)
        if entry.blocked:
            entry.state = 'faulted'
            self._persist(entry)
        elif not entry.enabled:
            entry.state = 'disabled'
            self._persist(entry)
        elif reason in MANUAL_END:
            # A confirmed manual end still wins when shutdown was also requested.
            self._disable(entry)
        elif self._closed and getattr(session, 'requested_reason', None) == 'shutdown':
            entry.state = 'waiting_idle'
            self._persist(entry)
        elif reason in ('lease_expired', 'call_preempted') and session.error is None:
            if reason == 'lease_expired':
                entry.failures = 0
            entry.error, entry.state, entry.idle_since = None, 'waiting_idle', None
            self._persist(entry)
        elif not getattr(session, 'spawn_attempted', True):
            self._fault(entry, 'hands_free_start_failed')
        else:
            self._retry(entry)
        entry.session = None

    def tick(self):
        """One pass. Network observations and preparation never hold manager lock."""
        if not self._pass_lock.acquire(blocking=False):
            return
        try:
            for entry in self._entries.values():
                with self.manager._lock:
                    if entry.profile.phone_id in self.manager._recovering:
                        continue
                    if entry.session is not None:
                        self._settle(entry)
                    if self._closed or not entry.enabled and entry.control_pending is None:
                        if not entry.launching and not entry.suspended and (entry.session is None
                                or entry.session.lease_fd < 0 and entry.session.state not in ACTIVE):
                            self._release(entry)
                        continue
                    if (entry.blocked and entry.control_pending is None
                            or entry.session is not None or entry.launching or entry.suspended):
                        continue
                    if (entry.control_pending is not None and not entry.control_pending['observed']
                            and self.clock() >= entry.control_deadline and not entry.blocked):
                        self._fault(entry, 'hands_free_recovery_required')
                    if self.clock() < entry.retry_at:
                        continue
                    if entry.enabled and not entry.blocked:
                        entry.error, entry.state = None, 'waiting_idle'
                        if entry.retry_pending:
                            entry.retry_pending = False
                            self._persist(entry)
                    generation = entry.generation
                try:
                    observation = self.observe(entry.profile.phone_id)
                except Exception:
                    observation = 'unknown'
                with self.manager._lock:
                    if (entry.profile.phone_id in self.manager._recovering
                            or self._closed or entry.suspended or entry.generation != generation
                            or (not entry.enabled or entry.blocked) and entry.control_pending is None):
                        continue
                    ticket = entry.control_pending
                    if ticket is not None and observation == 'call' and not ticket['observed']:
                        ticket['observed'] = True
                        self._persist(entry)
                    if ticket is not None and not ticket['observed']:
                        entry.idle_since = None
                        continue
                    if observation != 'idle':
                        entry.idle_since = None
                        if observation in ('busy', 'call'):
                            entry.unknowns = 0
                        else:
                            entry.unknowns += 1
                            if entry.unknowns >= MAX_FAILURES:
                                self._fault(entry, 'hands_free_observation_failed')
                            else:
                                entry.state = 'backoff'
                                entry.retry_at = self.clock() + 5 * (2 ** (entry.unknowns - 1))
                        continue
                    entry.unknowns = 0
                    if entry.idle_since is None:
                        entry.idle_since = self.clock()
                        continue
                    if self.clock() - entry.idle_since < QUIET_SECONDS:
                        continue
                    if entry.control_pending is not None:
                        entry.control_pending = None
                        self._persist(entry)
                    if not entry.enabled or entry.blocked:
                        continue
                    entry.launching, entry.pending, entry.state = True, True, 'starting'
                    try:
                        self._persist(entry)
                    except Exception:
                        entry.launching = False
                        continue
                    line = entry.line
                failure = None
                try:
                    self.manager.start(line, 'hands-free:' + secrets.token_hex(16),
                        context=self.context_for(entry.profile.phone_id), attended=True,
                        exclusive_media=True, enable_microphone=True,
                        _hands_free_generation=generation)
                except Exception as exc:
                    failure = exc
                with self.manager._lock:
                    entry.launching = False
                    if entry.session is not None:
                        self._settle(entry)
                    elif entry.generation != generation or not entry.enabled or self._closed:
                        entry.pending = False
                        self._persist(entry)
                    elif failure is not None:
                        if getattr(failure, 'reason', None) == 'hands_free_start_cancelled':
                            entry.pending, entry.state, entry.idle_since = False, 'waiting_idle', None
                            self._persist(entry)
                        else:
                            category = ('hands_free_owner_unavailable'
                                if getattr(failure, 'reason', None) == 'voice_phone_busy'
                                else 'hands_free_start_failed')
                            self._fault(entry, category)
        finally:
            self._pass_lock.release()

    def _release(self, entry):
        if entry.fd is not None:
            os.close(entry.fd)
            entry.fd = None

    @staticmethod
    def _supports_hands_free(entry):
        p = entry.profile
        return (p.media_transport == 'sip_audiosocket' and p.mode == 'wake'
                and p.rearm_after_turn and p.duration >= 30)

    def mark_control_dispatch(self, phone_ids):
        """Durably fence selected phones immediately before a gateway dispatch.

        Validation is completed for every selected phone before writing any
        ticket. The caller must not dispatch if this method raises.
        """
        if isinstance(phone_ids, str):
            phone_ids = (phone_ids,)
        with self.manager._lock:
            selected = []
            for phone_id in dict.fromkeys(phone_ids):
                self._require(phone_id not in self.manager._recovering, 'voice_phone_busy')
                entry = self._entries.get(phone_id)
                if entry is None or not self._supports_hands_free(entry):
                    continue
                contexts = [item for item in entry.control_contexts
                            if item['owner'] == threading.get_ident() and item['wait_for_call']]
                self._require(entry.suspended > 0 and entry.fd is not None and contexts,
                              'hands_free_recovery_required')
                self._require(entry.control_pending is None, 'hands_free_recovery_required')
                selected.append((entry, contexts[-1]))
            for entry, context in selected:
                ticket = {'id': secrets.token_hex(16), 'generation': entry.generation,
                          'observed': False}
                context['id'], context['generation'] = ticket['id'], ticket['generation']
                entry.retry_pending = False
                entry.control_pending = ticket
                entry.control_deadline = self.clock() + CONTROL_WAIT_SECONDS
                self._persist(entry)

    @contextmanager
    def suspend_for_control(self, phone_id, *, resume=True, wait_for_call=False,
                            defer_ticket=False, timeout=55.0):
        """Suspend renewal while a gateway action holds the ordinary phone fence.

        Callers must still use VoiceControl.reserve_control after this context
        admits them. With defer_ticket, call mark_control_dispatch after all
        validation and immediately before the first external call mutation.
        """
        context = {'id': None, 'generation': None, 'wait_for_call': wait_for_call,
                   'owner': threading.get_ident()}
        with self.manager._lock:
            self._require(phone_id not in self.manager._recovering, 'voice_phone_busy')
            entry = self._entries.get(phone_id)
            if entry is not None:
                self._own(entry)
            managed = entry is not None and (entry.enabled or entry.session is not None or entry.launching)
            if entry is not None:
                entry.suspended += 1
                entry.control_contexts.append(context)
                entry.idle_since = None
                session = entry.session
                try:
                    if not resume and (entry.enabled or entry.pending):
                        self._disable(entry)
                    elif session is not None and session.lease_fd >= 0:
                        self.manager._request_stop(session, reason='call_preempted')
                except Exception:
                    entry.suspended -= 1
                    entry.control_contexts.remove(context)
                    raise
        try:
            deadline = time.monotonic() + timeout
            while entry is not None:
                with self.manager._lock:
                    session = entry.session
                    pending = entry.launching or session is not None and session.lease_fd >= 0
                    if not pending:
                        self._require(session is None or session.cleanup_verified,
                                      'voice_recovery_required')
                        break
                self._require(time.monotonic() < deadline, 'voice_phone_busy')
                self._wake.wait(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
                self._wake.clear()
            if entry is not None and wait_for_call and not defer_ticket:
                self.mark_control_dispatch((phone_id,))
            yield managed
        except BaseException:
            if entry is not None and context['id'] is not None:
                with self.manager._lock:
                    if (entry.generation == context['generation'] and entry.control_pending is not None
                            and entry.control_pending['id'] == context['id']):
                        self._fault(entry, 'hands_free_recovery_required')
            raise
        finally:
            if entry is not None:
                with self.manager._lock:
                    entry.suspended -= 1
                    entry.control_contexts.remove(context)
                    entry.idle_since = None
                    self._wake.set()

    def close(self):
        """Prevent new starts immediately; preserve desired state after clean stop."""
        with self.manager._lock:
            if self._closed:
                return
            self._closed = True
            for entry in self._entries.values():
                entry.idle_since = None
                if entry.session is not None and entry.session.lease_fd >= 0:
                    self.manager._request_stop(entry.session, reason='shutdown')
                elif entry.session is None and not entry.launching and not entry.suspended:
                    self._release(entry)
            self._wake.set()

    def settle_shutdown(self):
        """Flush completed owned children without waiting for the observation worker."""
        with self.manager._lock:
            if not self._closed:
                return False
            clean = True
            for entry in self._entries.values():
                if entry.session is not None:
                    self._settle(entry)
                if (entry.launching or entry.suspended or entry.session is not None
                        and (entry.session.lease_fd >= 0 or entry.session.state in ACTIVE)):
                    clean = False
                    continue
                self._release(entry)
                if entry.pending or entry.storage_failed:
                    clean = False
            return clean

    def _monitor(self):
        while True:
            try:
                self.tick()
            except Exception:
                # Any unexpected state/storage failure blocks fresh capture.
                with self.manager._lock:
                    for entry in self._entries.values():
                        if entry.enabled:
                            self._fault(entry, 'hands_free_state_unavailable', persist=False)
            with self.manager._lock:
                if self._closed and all(entry.fd is None for entry in self._entries.values()):
                    return
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()
