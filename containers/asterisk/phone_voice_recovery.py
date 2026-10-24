"""Exact-session administrative unreservation after fresh phone quiescence.

This is not a Stop acknowledgement. The original journal, error, counters and
diagnostics survive. No phone I/O occurs here: the server supplies a trusted
deadline-bound proof adapter which must finish and reap its work before return.
"""
from __future__ import annotations

import fcntl
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time

PROOF_SECONDS = 30.0
MAX_BYTES = 256 * 1024


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid,
            info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns)


def _read(path, control):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        control._require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
                         and before.st_nlink == 1 and not before.st_mode & 0o077
                         and before.st_size <= MAX_BYTES, 'voice_recovery_file_invalid')
        raw = stream.read(MAX_BYTES + 1)
        control._require(len(raw) <= MAX_BYTES and _identity(before) ==
                         _identity(os.fstat(stream.fileno())), 'voice_recovery_file_changed')
    control._require(_identity(path.lstat()) == _identity(before), 'voice_recovery_file_changed')
    value = json.loads(raw, object_pairs_hook=control._unique_object)
    control._require(type(value) is dict, 'voice_recovery_file_invalid')
    return raw, value, before


def _owner(manager, profile, sid, control):
    control._require(not manager._closed, 'voice_control_closed', 503)
    current = manager._current.get(profile.phone_id)
    control._require(current is None or current.session_id == sid,
                     'voice_recovery_session_changed')
    # Refuse even an exited-but-unreaped handle. The normal owner must settle it.
    for session in manager._sessions.values():
        if session.profile.phone_id == profile.phone_id:
            control._require(session.lease_fd < 0 and session.handle is None
                             and session.state not in ('starting', 'running', 'stopping'),
                             'voice_phone_busy')
    if current is not None:
        control._require(current.lease_fd < 0 and current.handle is None
                         and current.state == 'recovery_required', 'voice_phone_busy')
    return current


def _finite(value, profile, sid, control):
    control._safe_status(value)
    control._require(type(sid) is str and re.fullmatch('[0-9a-f]{32}', sid)
                     and value.get('session_id') == sid and value.get('phone') == profile.phone_id
                     and value.get('line') in profile.lines
                     and value.get('state') == 'recovery_required'
                     and value.get('active') is False and value.get('reserved') is True
                     and value.get('stop_confirmed') is False, 'voice_recovery_session_changed')


def _finite_cache(manager, current, value, control):
    if current is not None:
        view = manager._view(current)
        control._require(value.get('request_id') == current.request_id and
                         all(value.get(key) == item for key, item in view.items()
                             if key != 'remaining_seconds'), 'voice_recovery_session_changed')


def _hands(value, profile, control):
    control._require(value.get('version') == 1 and value.get('phone') == profile.phone_id
                     and value.get('line') in profile.lines and value.get('enabled') is False
                     and value.get('control_pending') is None
                     and value.get('retry_pending') is False
                     and value.get('error') in (None, 'hands_free_recovery_required'),
                     'hands_free_disable_required')


def _desired(entry):
    # Compare the manager cache to the strict freshly parsed durable journal,
    # including replay protection. Neither view alone authorizes recovery.
    names = ('line', 'enabled', 'generation', 'failures', 'error', 'pending',
             'control_pending', 'retry_pending', 'history', 'seen',
             'committed_history', 'committed_seen')
    return copy.deepcopy({name: getattr(entry, name) for name in names})


def _cache(entry):
    names = ('state', 'retry_at', 'idle_since', 'unknowns', 'suspended',
             'control_contexts', 'blocked', 'storage_failed', 'launching', 'fd')
    return (_desired(entry), copy.deepcopy({name: getattr(entry, name) for name in names}),
            id(entry.session))


def _flock(path, control):
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        control._require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                         and info.st_nlink == 1 and not info.st_mode & 0o077,
                         'voice_recovery_file_invalid')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _lock_current(path, fd, control):
    a, b = path.lstat(), os.fstat(fd)
    control._require(_identity(a) == _identity(b) and stat.S_ISREG(b.st_mode)
                     and b.st_uid == os.getuid() and b.st_nlink == 1
                     and not b.st_mode & 0o077, 'voice_recovery_lock_changed')


def _proof(value, profile, sid, challenge, start, end, control):
    need = control._require
    need(type(value) is dict and set(value) == {'version', 'session_id', 'challenge', 'samples'}
         and type(value['version']) is int and value['version'] == 1
         and value['session_id'] == sid and value['challenge'] == challenge,
         'voice_recovery_proof_invalid')
    samples = value['samples']
    need(type(samples) is list and len(samples) == 2, 'voice_recovery_proof_invalid')
    counters, times = [], []
    for sample in samples:
        need(type(sample) is dict and set(sample) == {
            'observed_at', 'device', 'idle_lines', 'phone_or_related_channels', 'streams'},
            'voice_recovery_proof_invalid')
        stamp = sample['observed_at']
        need(type(stamp) in (int, float) and math.isfinite(stamp) and start <= stamp <= end,
             'voice_recovery_proof_stale')
        need(sample['device'] == profile.device
             and type(sample['idle_lines']) is list
             and sorted(sample['idle_lines']) == sorted(profile.lines)
             and sample['phone_or_related_channels'] == [], 'voice_recovery_not_quiet')
        streams = sample['streams']
        need(type(streams) is list and len(streams) == 5, 'voice_recovery_proof_invalid')
        indexed = {}
        for stream in streams:
            need(type(stream) is dict and set(stream) == {
                'slot', 'status', 'tx_packets', 'rx_packets', 'tx_octets', 'rx_octets'},
                'voice_recovery_proof_invalid')
            slot = stream['slot']
            need(type(slot) is int and 0 <= slot < 5 and slot not in indexed
                 and stream['status'] == 'Not Ready', 'voice_recovery_not_quiet')
            counts = tuple(stream[k] for k in ('tx_packets', 'rx_packets', 'tx_octets', 'rx_octets'))
            need(all(type(n) is int and 0 <= n < 2**64 for n in counts),
                 'voice_recovery_proof_invalid')
            indexed[slot] = counts
        counters.append(indexed)
        times.append(stamp)
    need(times[1] - times[0] >= .25 and counters[0] == counters[1],
         'voice_recovery_not_quiet')


def _backup(directory, name, raw):
    fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _encoded(value):
    return (json.dumps(value, sort_keys=True) + '\n').encode()


def _replace(path, value, mode):
    raw = _encoded(value)
    fd, temporary = tempfile.mkstemp(prefix='.voice-recovery-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(mode))
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync(path.parent)
        return _digest(raw)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def status(manager, target, *, context, control):
    profile = manager._profile(target, context, 'recover')
    result = {'schema_version': 1, 'phone': profile.phone_id, 'line': target,
              'session_id': None, 'journal_sha256': None, 'eligible': False,
              'reason': 'voice_recovery_unavailable', 'minimum_age_remaining_seconds': 0.0,
              'quiescence_verified': False}
    try:
        with manager._lock:
            path = manager._paths(profile)[1]
            raw, value, info = _read(path, control)
            sid = value.get('session_id')
            result.update(session_id=sid if type(sid) is str and re.fullmatch('[0-9a-f]{32}', sid) else None,
                          journal_sha256=_digest(raw))
            _finite(value, profile, sid, control)
            current = _owner(manager, profile, sid, control)
            _finite_cache(manager, current, value, control)
            control._require(profile.media_transport == 'sip_audiosocket', 'voice_recovery_sip_required')
            remaining = max(0.0, min(control.EXTERNAL_OWNER_GRACE_SECONDS,
                info.st_mtime + control.EXTERNAL_OWNER_GRACE_SECONDS - time.time()))
            result['minimum_age_remaining_seconds'] = remaining
            control._require(remaining == 0, 'voice_recovery_grace_pending')
            supervisor = manager.hands_free
            control._require(supervisor is not None, 'hands_free_disable_required')
            entry = supervisor._entries[profile.phone_id]
            _, hands, _ = _read(supervisor._paths(entry)[1], control)
            probe = type(entry)(entry.profile, entry.line)
            supervisor._load(probe)
            control._require(_desired(entry) == _desired(probe), 'voice_recovery_session_changed')
            _hands(hands, profile, control)
            control._require(not entry.enabled and not entry.launching and not entry.suspended
                             and profile.phone_id not in manager._recovering, 'voice_phone_busy')
            result.update(eligible=True, reason=None)
    except control.ControlError as error:
        result['reason'] = error.reason
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return result


def recover(manager, target, sid, expected, *, context, verify_cleanup, control):
    profile = manager._profile(target, context, 'recover')
    need = control._require
    need(type(sid) is str and re.fullmatch('[0-9a-f]{32}', sid)
         and type(expected) is str and re.fullmatch('[0-9a-f]{64}', expected),
         'voice_recovery_request_invalid')
    need(callable(verify_cleanup) and profile.media_transport == 'sip_audiosocket',
         'voice_recovery_sip_required')
    fd = hands_fd = None
    registered = False
    commit_started = completed = False
    try:
        with manager._lock:
            need(profile.phone_id not in manager._recovering, 'voice_phone_busy')
            current = _owner(manager, profile, sid, control)
            lock, path = manager._paths(profile)
            base_info = path.parent.lstat()
            need(stat.S_ISDIR(base_info.st_mode) and base_info.st_uid == os.getuid()
                 and not base_info.st_mode & 0o077, 'voice_recovery_file_invalid')
            fd = _flock(lock, control)
            raw, value, info = _read(path, control)
            need(_digest(raw) == expected, 'voice_recovery_session_changed')
            _finite(value, profile, sid, control)
            _finite_cache(manager, current, value, control)
            need(time.time() >= info.st_mtime + control.EXTERNAL_OWNER_GRACE_SECONDS,
                 'voice_recovery_grace_pending')
            supervisor = manager.hands_free
            need(supervisor is not None and not supervisor._closed, 'hands_free_disable_required')
            entry = supervisor._entries[profile.phone_id]
            need(not entry.enabled and not entry.launching and not entry.suspended
                 and entry.control_pending is None, 'hands_free_disable_required')
            hands_lock, hands_path = supervisor._paths(entry)
            hands_fd = os.dup(entry.fd) if entry.fd is not None else _flock(hands_lock, control)
            _lock_current(hands_lock, hands_fd, control)
            hands_raw, hands_value, hands_info = _read(hands_path, control)
            # Existing strict identity/history/Bloom parser remains authoritative.
            probe = type(entry)(entry.profile, entry.line)
            supervisor._load(probe)
            _hands(hands_value, profile, control)
            need(_desired(entry) == _desired(probe), 'voice_recovery_session_changed')
            need(entry.session is None or entry.session is current, 'voice_recovery_session_changed')
            cached_hands = _cache(entry)
            cached_finite = copy.deepcopy(vars(current)) if current is not None else None
            manager._recovering.add(profile.phone_id)
            registered = True
            started = manager.clock()
            deadline = started + PROOF_SECONDS
            challenge = secrets.token_hex(32)
        proof = verify_cleanup(profile=profile, session_id=sid, challenge=challenge, deadline=deadline)
        with manager._lock:
            finished = manager.clock()
            need(started <= finished <= deadline, 'voice_recovery_proof_stale')
            _proof(proof, profile, sid, challenge, started, finished, control)
            need(_owner(manager, profile, sid, control) is current, 'voice_recovery_session_changed')
            need(_cache(entry) == cached_hands and
                 (vars(current) if current is not None else None) == cached_finite,
                 'voice_recovery_session_changed')
            need(not supervisor._closed and not entry.enabled and not entry.launching
                 and not entry.suspended and entry.control_pending is None,
                 'hands_free_disable_required')
            need(_read(path, control)[0] == raw and _read(hands_path, control)[0] == hands_raw,
                 'voice_recovery_session_changed')
            need(_identity(path.lstat()) == _identity(info)
                 and _identity(hands_path.lstat()) == _identity(hands_info),
                 'voice_recovery_file_changed')
            _lock_current(lock, fd, control)
            _lock_current(hands_lock, hands_fd, control)
            actual_base = path.parent.lstat()
            need((actual_base.st_dev, actual_base.st_ino, actual_base.st_mode, actual_base.st_uid) ==
                 (base_info.st_dev, base_info.st_ino, base_info.st_mode, base_info.st_uid),
                 'voice_recovery_file_changed')
            archive = path.parent / ('.orphan-recovery-' + sid + '-' + challenge[:16])
            archive.mkdir(mode=0o700)
            updated = dict(value, state='failed', active=False, reserved=False, remaining_seconds=0)
            updated_hands = dict(hands_value, pending=False, error=None)
            _backup(archive, 'finite.before.json', raw)
            _backup(archive, 'hands-free.before.json', hands_raw)
            _backup(archive, 'proof.json', _encoded(proof))
            _backup(archive, 'commit.json', _encoded({
                'version': 1, 'session_id': sid, 'kind': 'administrative_unreservation',
                'historical_stop_confirmed': False,
                'before': {'finite': _digest(raw), 'hands_free': _digest(hands_raw)},
                'after': {'finite': _digest(_encoded(updated)),
                          'hands_free': _digest(_encoded(updated_hands))}}))
            _sync(archive)
            _sync(path.parent)
            need(_read(path, control)[0] == raw and _read(hands_path, control)[0] == hands_raw,
                 'voice_recovery_session_changed')
            _lock_current(lock, fd, control)
            _lock_current(hands_lock, hands_fd, control)
            need(manager.clock() <= deadline, 'voice_recovery_proof_stale')
            # Finite first: a crash cannot re-enable capture. The disabled HF
            # journal is untouched until positive proof is durably archived.
            # Two replacements are not atomic. An I/O failure from this point
            # retains the in-process fence until shutdown, and the archive
            # identifies exact before/after bytes. Never auto-replay or rollback.
            commit_started = True
            digest = _replace(path, updated, info.st_mode)
            if current is not None:
                current.state = 'failed'
                current.cleanup_verified = True
                # Keep error, Stop flag, request ID, diagnostics and run directory.
            _replace(hands_path, updated_hands, hands_info.st_mode)
            supervisor._load(entry)
            entry.session = None
            entry.blocked = False
            entry.state = 'disabled'
            completed = True
            return {'schema_version': 1, 'status': 'ORPHAN_RECOVERY_COMPLETE', 'state': 'recovered',
                    'phone': profile.phone_id, 'line': target, 'session_id': sid,
                    'historical_stop_confirmed': value['stop_confirmed'],
                    'stop_confirmed': value['stop_confirmed'], 'prior_journal_sha256': expected,
                    'journal_sha256': digest, 'archive': archive.name,
                    'proof_kind': 'phone_reported_transport_quiescence', 'enabled': False}
    except control.ControlError:
        raise
    except Exception:
        raise control.ControlError('voice_recovery_unverified', 409) from None
    finally:
        with manager._lock:
            if registered and (not commit_started or completed):
                manager._recovering.discard(profile.phone_id)
            if hands_fd is not None:
                os.close(hands_fd)
            if fd is not None:
                os.close(fd)
