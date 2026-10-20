#!/usr/bin/env python3
"""Explicit, bounded voice-session ownership for authenticated operator adapters.

Importing, configuring or querying this module never starts microphone capture.
The existing reviewed voice_session.py remains the only live runner. Adapters
must provide authorization; this module does not expose a new network listener.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
from dataclasses import dataclass, field
import fcntl
import hashlib
import hmac
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
MAX_CONFIG = 256 * 1024
MAX_REPORT = 16 * 1024
STOP_GRACE_SECONDS = 45.0
REPORT_GRACE_SECONDS = 2.0
STATES = frozenset(('idle', 'starting', 'running', 'stopping', 'stopped',
                    'cancelled', 'failed', 'recovery_required', 'external_owner'))
PUBLIC_ERRORS = frozenset(('voice_state_unavailable', 'voice_owner_unavailable',
                          'voice_start_failed', 'voice_start_uncertain', 'voice_signal_failed',
                          'voice_session_failed', 'voice_stop_unconfirmed'))


def _safe_status(value):
    """Validate, then copy only the public wire fields, including on client reads."""
    _require(isinstance(value, dict) and type(value.get('schema_version')) is int
             and value['schema_version'] == 1 and value.get('state') in STATES,
             'invalid_voice_response', 502)
    for key in ('phone', 'line'):
        _require(isinstance(value.get(key), str) and re.fullmatch(r'2[0-9]{2}', value[key]),
                 'invalid_voice_response', 502)
    session = value.get('session_id')
    _require(session is None or isinstance(session, str) and re.fullmatch(r'[0-9a-f]{32}', session),
             'invalid_voice_response', 502)
    for key in ('active', 'stop_requested', 'stop_confirmed'):
        _require(type(value.get(key)) is bool, 'invalid_voice_response', 502)
    remaining = value.get('remaining_seconds')
    _require(type(remaining) in (int, float) and math.isfinite(remaining) and 0 <= remaining <= 30,
             'invalid_voice_response', 502)
    result = {key: value[key] for key in ('schema_version', 'phone', 'line', 'session_id', 'state',
              'active', 'remaining_seconds', 'stop_requested', 'stop_confirmed')}
    error = value.get('error')
    result['error'] = error if error is None or error in PUBLIC_ERRORS else 'voice_state_unavailable'
    return result


class ControlError(Exception):
    """Fixed public refusal category, never an underlying exception string."""
    def __init__(self, reason, status=409):
        self.reason, self.status = reason, status
        super().__init__(reason)


def _require(condition, reason='invalid_voice_control_config', status=400):
    if not condition:
        raise ControlError(reason, status)


def _private_path(value, workspace):
    root = Path(workspace).resolve()
    path = Path(value)
    _require('..' not in path.parts)
    if not path.is_absolute():
        path = root / path
    base = root / '.local'
    _require(path != base and path.is_relative_to(base), 'private_path_required')
    current = path
    while current != root:
        _require(not current.is_symlink(), 'private_symlink_refused')
        current = current.parent
    return path


def _read(path, limit=MAX_CONFIG):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            _require(stat.S_ISREG(info.st_mode) and info.st_size <= limit
                     and not info.st_mode & 0o077, 'private_file_required')
            data = source.read(limit + 1)
        _require(len(data) <= limit, 'private_file_required')
        return data
    except OSError:
        raise ControlError('private_file_unavailable', 400) from None


def _json(path):
    try:
        value = json.loads(_read(path))
        _require(isinstance(value, dict))
        return value
    except (ValueError, UnicodeError):
        raise ControlError('invalid_voice_control_config', 400) from None


def _libraries():
    # Lazy: status for an already configured manager does not import providers.
    name = '_voice_control_transport'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name('phone_voice_transport.py'))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from voice_assistant.config import validate_config
    from voice_assistant.service import ServiceSettings
    return sys.modules[name], validate_config, ServiceSettings


@dataclass(frozen=True)
class Profile:
    phone_id: str
    lines: tuple[str, ...]
    device: str = field(repr=False)
    ipv4: str = field(repr=False)
    transport_config: Path = field(repr=False)
    voice_config: Path = field(repr=False)
    duration: float
    bind_ipv4: str = field(repr=False)
    bind_port: int
    display_port: int | None = None


@dataclass(frozen=True)
class ControlConfig:
    workspace: Path = field(repr=False)
    state_directory: Path = field(repr=False)
    python_executable: str = field(repr=False)
    ffmpeg_executable: str = field(repr=False)
    profiles: tuple[Profile, ...]
    enabled: bool = False


def _inputs(transport_path, voice_path, workspace):
    transport, validate_voice, settings_type = _libraries()
    config = _json(_private_path(transport_path, workspace))
    allowed = {'gateway_config', 'line', 'bind_ipv4', 'bind_port', 'phone_rtp_port',
               'duration_seconds', 'receive_idle_seconds', 'volume', 'expected_source_port'}
    _require(not set(config) - allowed)
    gateway = _json(_private_path(config['gateway_config'], workspace))
    transport._gateway_module().validate(gateway)
    transport.tx_uri(config['bind_ipv4'], config['bind_port'])
    transport.rx_uri(config['bind_ipv4'], config['phone_rtp_port'], config.get('volume', 20))
    transport._duration(config['duration_seconds'])
    transport._duration(config.get('receive_idle_seconds', 3), 10)
    if config.get('expected_source_port') is not None:
        transport._port(config['expected_source_port'], False)
    selected = [phone for phone in gateway['phones'] if config.get('line') in phone['lines']]
    _require(len(selected) == 1)
    phone = selected[0]
    voice = validate_voice(_json(_private_path(voice_path, workspace)))
    settings = settings_type(**voice['service'])
    _require(settings.enabled and not settings.rearm_after_turn, 'single_voice_turn_required')
    _require(voice['phones'] == {phone['id']: True}, 'one_selected_phone_required')
    _require(voice['wake']['mode'] == 'manual', 'manual_voice_mode_required')
    token = _read(_private_path(voice['ha']['token_file'], workspace), 8192).decode('ascii').strip()
    _require(token and all(33 <= ord(c) <= 126 for c in token), 'invalid_voice_token')
    return config, gateway, phone, voice, token


def load_control_config(path, workspace=ROOT):
    """Read owner-only private configuration without sockets, writes or starts."""
    try:
        workspace = Path(workspace).resolve()
        data = _json(_private_path(path, workspace))
        _require(set(data) == {'version', 'enabled', 'state_directory', 'python_executable',
                               'ffmpeg_executable', 'profiles'})
        _require(type(data['version']) is int and data['version'] == 1 and type(data['enabled']) is bool)
        state = _private_path(data['state_directory'], workspace)
        python = data['python_executable']
        _require(isinstance(python, str) and Path(python).is_absolute()
                 and Path(python).is_file() and os.access(python, os.X_OK), 'invalid_voice_python')
        ffmpeg = data['ffmpeg_executable']
        _require(isinstance(ffmpeg, str) and Path(ffmpeg).is_absolute()
                 and Path(ffmpeg).is_file() and os.access(ffmpeg, os.X_OK), 'invalid_voice_ffmpeg')
        _require(isinstance(data['profiles'], list) and 1 <= len(data['profiles']) <= 32)
        profiles, identities, addresses, lines = [], set(), set(), set()
        rtp_endpoints, display_endpoints = set(), set()
        for entry in data['profiles']:
            _require(isinstance(entry, dict) and set(entry) <= {'transport_config', 'voice_config', 'display_port'})
            transport_path = _private_path(entry['transport_config'], workspace)
            voice_path = _private_path(entry['voice_config'], workspace)
            config, gateway, phone, _voice, _token = _inputs(transport_path, voice_path, workspace)
            _require(phone['device'] not in identities and phone['ipv4'] not in addresses
                     and not lines.intersection(phone['lines']), 'duplicate_physical_phone')
            endpoint = (config['bind_ipv4'], config['bind_port'])
            _require(endpoint not in rtp_endpoints, 'voice_rtp_endpoint_collision')
            port = entry.get('display_port')
            if port is not None:
                _require(type(port) is int and 1024 <= port <= 65535, 'invalid_voice_display_port')
                _require(port not in {config['bind_port'], config['phone_rtp_port'], gateway['api_port'],
                                      gateway['ami']['port'], *(p['port'] for p in gateway['phones'])},
                         'voice_display_port_collision')
                _require((config['bind_ipv4'], port) not in display_endpoints,
                         'voice_display_port_collision')
                display_endpoints.add((config['bind_ipv4'], port))
            profiles.append(Profile(phone['id'], tuple(phone['lines']), phone['device'], phone['ipv4'],
                                    transport_path, voice_path, float(config['duration_seconds']),
                                    config['bind_ipv4'], config['bind_port'], port))
            rtp_endpoints.add(endpoint)
            identities.add(phone['device']); addresses.add(phone['ipv4']); lines.update(phone['lines'])
        return ControlConfig(workspace, state, python, ffmpeg, tuple(profiles), data['enabled'])
    except ControlError:
        raise
    except Exception:
        raise ControlError('invalid_voice_control_config', 400) from None


class TokenSourceAuthorizer:
    """Adapter for a trusted HTTP handler; pass the socket peer, never forwarded headers."""
    def __init__(self, token, sources):
        _require(isinstance(token, str) and 32 <= len(token) <= 128 and sources)
        self._token, self._sources = token, frozenset(sources)

    def __call__(self, context, action, phone_id):
        if not isinstance(context, dict) or context.get('source') not in self._sources:
            return False
        headers = context.get('authorization')
        return (isinstance(headers, (list, tuple)) and len(headers) == 1
                and isinstance(headers[0], str)
                and hmac.compare_digest(hashlib.sha256(headers[0].encode()).digest(),
                                        hashlib.sha256(('Bearer ' + self._token).encode()).digest()))


class _SubprocessHandle:
    def __init__(self, argv, *, cwd, env, lease_fd):
        self.process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        shell=False, close_fds=True, pass_fds=(lease_fd,),
                                        start_new_session=True)
        self.overflow = False
        self._data, self._lock, self._done = bytearray(), threading.Lock(), threading.Event()
        threading.Thread(target=self._drain, name='voice-result', daemon=True).start()

    def _drain(self):
        try:
            while True:
                block = os.read(self.process.stdout.fileno(), 4096)
                if not block:
                    break
                with self._lock:
                    available = MAX_REPORT - len(self._data)
                    self._data.extend(block[:available])
                    self.overflow |= len(block) > available
        except OSError:
            self.overflow = True
        finally:
            self.process.stdout.close()
            self._done.set()

    def poll(self):
        return self.process.poll()

    def terminate(self):
        self.process.terminate()  # Only this runner. Its watchdog must survive.

    def kill(self):
        self.process.kill()

    def read_result(self):
        if not self._done.is_set():
            return None
        with self._lock:
            return bytes(self._data)


@dataclass
class _Session:
    profile: Profile
    session_id: str
    request_id: str
    line: str
    lease_fd: int
    state: str = 'starting'
    started: float | None = None
    stop_at: float | None = None
    exit_at: float | None = None
    handle: object = None
    directory: Path | None = None
    stop_requested: bool = False
    stop_confirmed: bool = False
    killed: bool = False
    spawn_attempted: bool = False
    error: str | None = None


class VoiceControl:
    """One manager per host; a shared private state directory fences other managers.

    A crashed/ambiguous owner leaves a durable recovery_required reservation.
    Only verified cleanup permits another start. Never remove a live lock file.
    """
    def __init__(self, config, *, authorize, runner_factory=_SubprocessHandle,
                 clock=time.monotonic, monitor=True):
        _require(isinstance(config, ControlConfig) and callable(authorize))
        _require(1 <= len(config.profiles) <= 32 and type(config.enabled) is bool)
        _require(all(isinstance(p, Profile) and type(p.duration) in (int, float)
                     and math.isfinite(p.duration) and 0 < p.duration <= 30
                     and p.lines and p.phone_id == p.lines[0] for p in config.profiles))
        all_lines = [line for p in config.profiles for line in p.lines]
        _require(len(all_lines) == len(set(all_lines))
                 and len({p.device for p in config.profiles}) == len(config.profiles)
                 and len({p.ipv4 for p in config.profiles}) == len(config.profiles),
                 'duplicate_physical_phone')
        _require(len({(p.bind_ipv4, p.bind_port) for p in config.profiles}) == len(config.profiles),
                 'voice_rtp_endpoint_collision')
        display_endpoints = [(p.bind_ipv4, p.display_port) for p in config.profiles
                             if p.display_port is not None]
        _require(len(display_endpoints) == len(set(display_endpoints)), 'voice_display_port_collision')
        self.config, self.authorize, self.runner_factory, self.clock = config, authorize, runner_factory, clock
        self._profiles = {line: p for p in config.profiles for line in p.lines}
        self._sessions, self._requests = OrderedDict(), {}
        self._current, self._lock, self._wake = {}, threading.RLock(), threading.Event()
        self._closed = False
        self._thread = None
        if monitor:
            self._thread = threading.Thread(target=self._monitor, name='voice-control', daemon=True)
            self._thread.start()

    def _profile(self, target, context, action):
        # Invalid targets reveal no registry/configuration before authorization.
        p = self._profiles.get(target) if isinstance(target, str) else None
        try:
            allowed = self.authorize(context, action, p.phone_id if p else None) is True
        except Exception:
            allowed = False
        _require(allowed, 'forbidden', 403)
        _require(p is not None, 'unknown_voice_phone', 404)
        return p

    def _paths(self, p):
        key = hashlib.sha256(p.device.encode()).hexdigest()
        base = _private_path(self.config.state_directory, self.config.workspace)
        return base / (key + '.lock'), base / (key + '.json')

    def _mkdir(self):
        path = _private_path(self.config.state_directory, self.config.workspace)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        _require(path.is_dir() and path.stat().st_uid == os.getuid()
                 and not path.stat().st_mode & 0o077, 'private_state_directory_required')

    def _journal(self, p):
        path = self._paths(p)[1]
        if not path.exists():
            return None
        value = _json(path)
        _require(value.get('schema_version') == 1 and value.get('phone') == p.phone_id
                 and value.get('state') in STATES, 'voice_recovery_required', 409)
        value.update(_safe_status(value))
        return value

    def _write(self, s):
        path = self._paths(s.profile)[1]
        value = self._view(s)
        value['request_id'] = s.request_id
        # Unresolved ownership survives any process crash between transitions.
        value['reserved'] = s.lease_fd >= 0 and s.state not in ('stopped', 'cancelled', 'failed')
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix='.voice-state-', dir=path.parent)
            with os.fdopen(fd, 'w') as stream:
                json.dump(value, stream, sort_keys=True)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            raise ControlError('voice_state_unavailable', 503) from None
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)

    def _acquire(self, p):
        self._mkdir()
        lock_path, _ = self._paths(p)
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            info = os.fstat(fd)
            _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                     and not info.st_mode & 0o077, 'private_state_file_required')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ControlError('voice_phone_busy', 409) from None
            prior = self._journal(p)
            _require(not prior or prior.get('reserved') is False, 'voice_recovery_required', 409)
            return fd
        except Exception:
            if 'fd' in locals():
                os.close(fd)
            raise

    def _view(self, s):
        remaining = 0 if s.started is None else max(0, s.profile.duration - (self.clock() - s.started))
        return {'schema_version': 1, 'phone': s.profile.phone_id, 'line': s.line,
                'session_id': s.session_id, 'state': s.state,
                'active': s.state in ('starting', 'running', 'stopping'),
                'remaining_seconds': round(remaining, 1), 'stop_requested': s.stop_requested,
                'stop_confirmed': s.stop_confirmed, 'error': s.error}

    @staticmethod
    def _check_profile(p, config, phone):
        _require((phone['id'], tuple(phone['lines']), phone['device'], phone['ipv4'],
                  float(config['duration_seconds']), config['bind_ipv4'], config['bind_port'])
                 == (p.phone_id, p.lines, p.device, p.ipv4, p.duration, p.bind_ipv4, p.bind_port),
                 'voice_profile_changed')

    def _prepare(self, p, s):
        config, gateway, phone, voice, token = _inputs(p.transport_config, p.voice_config, self.config.workspace)
        self._check_profile(p, config, phone)
        directory = self.config.state_directory / ('run-' + s.session_id)
        directory.mkdir(mode=0o700)
        s.directory = directory
        # Minimize credentials to the selected phone and its existing AMI account.
        gateway = {'persistent_enabled': True, 'bind_ipv4': gateway['bind_ipv4'],
                   'api_port': gateway['api_port'], 'api_clients': gateway['api_clients'],
                   'api_token': 'unused-voice-control-api-token-00000000',
                   'ami': gateway['ami'], 'dial_allowlist': [], 'ring_allowlist': [],
                   'phones': [{key: phone[key] for key in
                               ('id', 'lines', 'device', 'ipv4', 'port', 'auth_path', 'username', 'password')}]}
        config['gateway_config'] = str(directory / 'gateway.json')
        voice['ha']['token_file'] = str(directory / 'ha-token.txt')
        for name, data in (('transport.json', config), ('gateway.json', gateway), ('voice.json', voice)):
            fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as output:
                json.dump(data, output)
        fd = os.open(directory / 'ha-token.txt', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as output:
            output.write(token + '\n')
        argv = [self.config.python_executable, str(ROOT / 'scripts/voice_session.py'),
                '--transport-config', str(directory / 'transport.json'),
                '--voice-config', str(directory / 'voice.json'), '--workspace', str(self.config.workspace),
                '--ffmpeg', self.config.ffmpeg_executable,
                '--run', '--attended', '--exclusive-media', '--enable-microphone']
        if p.display_port is not None:
            argv += ['--display-port', str(p.display_port)]
        return argv

    def start(self, target, request_id, *, context, attended=False, exclusive_media=False,
              enable_microphone=False, dry_run=False):
        p = self._profile(target, context, 'start')
        _require(isinstance(request_id, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', request_id),
                 'voice_request_id_required')
        _require(attended is True and exclusive_media is True and enable_microphone is True,
                 'explicit_voice_start_required')
        _require(type(dry_run) is bool)
        with self._lock:
            _require(not self._closed, 'voice_control_closed', 503)
            _require(self.config.enabled, 'voice_control_disabled', 403)
            # Validation never reserves/replays an operation. In particular a
            # key previously used by a real attempt must still return the
            # no-actions schema expected by every dry-run client.
            if dry_run:
                latest = _inputs(p.transport_config, p.voice_config, self.config.workspace)
                self._check_profile(p, latest[0], latest[2])
                return {'schema_version': 1, 'phone': p.phone_id, 'line': target,
                        'valid': True, 'network_actions': 0, 'microphone_started': False,
                        'duration_seconds': p.duration}
            previous = self._requests.get(request_id)
            if previous:
                _require(previous.profile.phone_id == p.phone_id, 'voice_request_id_conflict')
                return self._view(previous)
            # Preserve the last completed request across manager restarts.
            prior = self._journal(p)
            if prior and prior.get('request_id') == request_id and prior.get('reserved') is False:
                return _safe_status(prior)
            current = self._current.get(p.phone_id)
            _require(not current or current.lease_fd < 0, 'voice_phone_busy')
            try:
                fd = self._acquire(p)
            except OSError:
                raise ControlError('voice_state_unavailable', 503) from None
            s = _Session(p, secrets.token_hex(16), request_id, target, fd)
            self._sessions[s.session_id] = s; self._requests[request_id] = s; self._current[p.phone_id] = s
            try:
                self._write(s)
            except Exception:
                os.close(fd); s.lease_fd = -1
                s.state, s.error = 'failed', 'voice_state_unavailable'
                self._trim()
                raise ControlError('voice_state_unavailable', 503) from None
        try:
            argv = self._prepare(p, s)
            with self._lock:
                if s.stop_requested or self._closed:
                    self._no_launch(s, 'cancelled', None)
                else:
                    # Pass the locked descriptor to the runner, so supervisor
                    # death cannot release live ownership. The durable journal
                    # additionally covers surviving watchdog descendants.
                    env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1',
                           'PYTHONUNBUFFERED': '1', 'TMPDIR': '/tmp', 'LANG': 'C.UTF-8'}
                    s.started = self.clock()
                    s.spawn_attempted = True
                    s.handle = self.runner_factory(argv, cwd=str(ROOT), env=env, lease_fd=s.lease_fd)
                    s.state = 'running'
                    self._write(s)
                    self._wake.set()
                self._trim()
                return self._view(s)
        except Exception:
            with self._lock:
                if s.handle is None and not s.spawn_attempted:
                    self._no_launch(s, 'failed', 'voice_start_failed')
                elif s.handle is None:
                    s.state, s.error = 'recovery_required', 'voice_start_uncertain'
                    try:
                        self._write(s)
                    finally:
                        if s.lease_fd >= 0:
                            os.close(s.lease_fd); s.lease_fd = -1
                else:
                    s.error = 'voice_state_unavailable'
                    self._request_stop(s)
                self._trim()
            raise ControlError('voice_start_failed', 503) from None

    def _no_launch(self, s, state, error):
        s.state, s.error = state, error
        self._write(s)
        self._release(s)

    def _release(self, s):
        if s.lease_fd >= 0:
            os.close(s.lease_fd); s.lease_fd = -1
        if s.directory:
            shutil.rmtree(s.directory, ignore_errors=True)
            s.directory = None

    def _trim(self):
        for key, s in list(self._sessions.items()):
            if len(self._sessions) <= 128:
                break
            if s.lease_fd < 0:
                self._sessions.pop(key); self._requests.pop(s.request_id, None)

    def status(self, target, *, context, session_id=None):
        p = self._profile(target, context, 'status')
        with self._lock:
            s = self._sessions.get(session_id) if session_id else self._current.get(p.phone_id)
            if s:
                _require(s.profile.phone_id == p.phone_id, 'unknown_voice_session', 404)
                if session_id is not None or s.lease_fd >= 0:
                    return self._view(s)
            _require(session_id is None, 'unknown_voice_session', 404)
            prior = self._journal(p)
            if s and (not prior or prior.get('session_id') == s.session_id):
                return self._view(s)
            if prior:
                # Another process's journal never proves a live process or
                # current audio state. Only this manager can signal its child.
                if prior.get('reserved') is not False:
                    prior.update(state='recovery_required', active=False,
                                 stop_confirmed=False, error='voice_owner_unavailable')
                return {key: prior[key] for key in ('schema_version', 'phone', 'line', 'session_id',
                        'state', 'active', 'remaining_seconds', 'stop_requested', 'stop_confirmed', 'error')}
            return {'schema_version': 1, 'phone': p.phone_id, 'line': target, 'session_id': None,
                    'state': 'idle', 'active': False, 'remaining_seconds': 0,
                    'stop_requested': False, 'stop_confirmed': False, 'error': None}

    def _request_stop(self, s):
        if s.state not in ('starting', 'running', 'stopping'):
            return
        s.stop_requested = True
        if s.stop_at is None:
            s.stop_at = self.clock()
            s.state = 'stopping'
            if s.handle is not None:
                try:
                    s.handle.terminate()
                except ProcessLookupError:
                    pass
                except OSError:
                    s.error = 'voice_signal_failed'
            self._write(s)
        self._wake.set()

    def stop(self, target, session_id, *, context):
        p = self._profile(target, context, 'stop')
        with self._lock:
            s = self._sessions.get(session_id) if isinstance(session_id, str) else None
            _require(s is not None and s.profile.phone_id == p.phone_id, 'unknown_voice_session', 404)
            self._request_stop(s)
            return self._view(s)

    def _finish(self, s, raw):
        confirmed, clean, reason = False, False, 'voice_result_unavailable'
        try:
            _require(type(raw) is bytes and len(raw) <= MAX_REPORT and not s.handle.overflow)
            report = json.loads(raw)
            result = report.get('result')
            confirmed = (report.get('watchdog_armed') is True and report.get('watchdog_finished') is True
                         and type(report.get('watchdog_stop_errors')) is int
                         and report['watchdog_stop_errors'] == 0
                         and (report.get('watchdog_stop_confirmed') is True
                              or isinstance(result, dict) and result.get('stop_confirmed') is True))
            clean = report.get('cleanup_errors') == []
            completed = (isinstance(result, dict)
                         and result.get('reason') in ('turn_complete', 'explicit_stop',
                                                     'display_stop', 'shutdown'))
            reason = 'voice_session_failed' if report.get('error') or not completed else None
        except Exception:
            pass
        s.stop_confirmed = bool(confirmed and not s.killed)
        if s.stop_confirmed and clean:
            s.state, s.error = ('failed', reason) if reason else ('stopped', None)
            self._write(s)
            self._release(s)
        else:
            s.state, s.error = 'recovery_required', 'voice_stop_unconfirmed'
            self._write(s)
            # The durable reservation now blocks every manager, even after
            # this process exits. Keep snapshots for private investigation.
            if s.lease_fd >= 0:
                os.close(s.lease_fd); s.lease_fd = -1
        s.handle = None

    def tick(self):
        """One nonblocking supervisor pass; deterministic with injected test handles."""
        with self._lock:
            now = self.clock()
            for s in list(self._sessions.values()):
                if s.handle is None:
                    continue
                code = s.handle.poll()
                if code is None:
                    if (s.started is not None and now - s.started >= s.profile.duration) or s.handle.overflow:
                        self._request_stop(s)
                    if s.stop_at is not None and now - s.stop_at >= STOP_GRACE_SECONDS and not s.killed:
                        s.handle.kill(); s.killed = True
                    continue
                if s.exit_at is None:
                    s.exit_at = now
                raw = s.handle.read_result()
                if raw is not None or now - s.exit_at >= REPORT_GRACE_SECONDS:
                    self._finish(s, raw)

    def _monitor(self):
        while True:
            self._wake.wait(.05); self._wake.clear()
            try:
                self.tick()
            except Exception:
                # Leave durable reservations intact if storage/OS operations
                # fail. Existing runner/watchdog bounds remain independent.
                pass
            with self._lock:
                if self._closed and all(s.handle is None for s in self._sessions.values()):
                    return

    def close(self):
        """Request graceful cleanup without waiting under an adapter's request lock."""
        failed = False
        with self._lock:
            self._closed = True
            for s in self._sessions.values():
                try:
                    self._request_stop(s)
                except Exception:
                    # A journal/OS failure for one phone must not prevent
                    # sending Stop to the other owned runners.
                    failed = True
        self._wake.set()
        if failed:
            raise ControlError('voice_state_unavailable', 503) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    validate = sub.add_parser('validate', help='read private profiles without starting audio')
    validate.add_argument('--config', required=True)
    validate.add_argument('--workspace', default=str(ROOT))
    for action in ('status', 'start', 'stop'):
        command = sub.add_parser(action, help='call the authenticated gateway voice adapter')
        command.add_argument('--client-config', required=True)
        command.add_argument('--line', required=True)
        if action == 'start':
            command.add_argument('--request-id', required=True)
            for flag in ('attended', 'exclusive-media', 'enable-microphone', 'dry-run'):
                command.add_argument('--' + flag, action='store_true')
        if action == 'stop':
            command.add_argument('--session-id', required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == 'validate':
            config = load_control_config(args.config, args.workspace)
            result = {'valid': True, 'enabled': config.enabled, 'profiles': len(config.profiles),
                      'network_actions': 0, 'microphone_started': False}
        else:
            _require(re.fullmatch(r'2[0-9]{2}', args.line), 'unknown_voice_phone')
            if str(ROOT / 'scripts') not in sys.path:
                sys.path.insert(0, str(ROOT / 'scripts'))
            import cisco
            body = None
            if args.action == 'start':
                _require(args.attended and args.exclusive_media and args.enable_microphone,
                         'explicit_voice_start_required')
                _require(re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', args.request_id),
                         'voice_request_id_required')
                body = {'idempotency_key': args.request_id, 'attended': True, 'exclusive_media': True,
                        'enable_microphone': True, 'dry_run': args.dry_run}
            elif args.action == 'stop':
                _require(re.fullmatch(r'[0-9a-f]{32}', args.session_id), 'unknown_voice_session')
                body = {'idempotency_key': 'voice-stop-' + args.session_id, 'session_id': args.session_id}
            # No inventory/AMI request: the server resolves either configured line.
            client = cisco.Gateway(cisco.load_config(args.client_config), timeout=10)
            response = client.request('/v1/phones/' + args.line + '/voice_' + args.action, body)
            _require(isinstance(response, dict), 'invalid_voice_response', 502)
            if body is not None:
                _require(response.get('status') == 'accepted'
                         and response.get('action_id') == body['idempotency_key'],
                         'invalid_voice_response', 502)
            value = response.get('voice')
            _require(isinstance(value, dict), 'invalid_voice_response', 502)
            if 'valid' in value:
                duration = value.get('duration_seconds')
                _require(args.action == 'start' and args.dry_run
                         and type(value.get('schema_version')) is int and value['schema_version'] == 1
                         and value.get('valid') is True and value.get('microphone_started') is False
                         and type(value.get('network_actions')) is int and value['network_actions'] == 0
                         and type(duration) in (int, float) and math.isfinite(duration) and 0 < duration <= 30
                         and isinstance(value.get('phone'), str)
                         and re.fullmatch(r'2[0-9]{2}', value['phone']) and value.get('line') == args.line,
                         'invalid_voice_response', 502)
                result = {'valid': True, 'network_actions': 0, 'microphone_started': False}
            else:
                result = _safe_status(value)
                if args.action != 'status':
                    _require(result['session_id'] is not None, 'invalid_voice_response', 502)
                if args.action == 'stop':
                    _require(result['session_id'] == args.session_id, 'invalid_voice_response', 502)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({'error': 'voice_control_request_failed', 'automatic_retry': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
