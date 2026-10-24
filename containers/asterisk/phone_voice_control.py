#!/usr/bin/env python3
"""Explicit, bounded voice-session ownership for authenticated operator adapters.

Importing, configuring or querying this module never starts microphone capture.
The existing reviewed voice_session.py remains the only live runner. Adapters
must provide authorization; this module does not expose a new network listener.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
from contextlib import contextmanager
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
MAX_FAILURE_RECEIPT = 4096
MAX_SESSION_SECONDS = 300
MAX_TURNS = 10000
PLAYBACK_LIMITS = {'playback_requests': 10000, 'tts_offers': 10000,
                   'empty_playbacks': 10000, 'incomplete_playbacks': 10000,
                   'completed_pcm_bytes': 64 * 1024 * 1024}
STOP_GRACE_SECONDS = 45.0
RUNNER_COMPLETION_MARGIN_SECONDS = 7.0
UNCERTAIN_CLEANUP_SECONDS = 55.0
EXTERNAL_OWNER_GRACE_SECONDS = (
    MAX_SESSION_SECONDS + STOP_GRACE_SECONDS + RUNNER_COMPLETION_MARGIN_SECONDS + UNCERTAIN_CLEANUP_SECONDS + 8)
REPORT_GRACE_SECONDS = 2.0
STATES = frozenset(('idle', 'starting', 'running', 'stopping', 'stopped',
                    'cancelled', 'failed', 'recovery_required', 'external_owner'))
PUBLIC_ERRORS = frozenset(('voice_state_unavailable', 'voice_owner_unavailable',
                          'voice_start_failed', 'voice_start_uncertain', 'voice_signal_failed',
                          'voice_session_failed', 'voice_stop_unconfirmed'))
PHASES = frozenset(('preparing', 'armed', 'listening', 'thinking', 'speaking', 'stopped', 'error'))
PHASE_REASONS = frozenset(('model_warmup', 'wake_ready', 'new_session', 'turn_complete',
                          'user_stop', 'explicit_stop', 'display_stop', 'shutdown',
                          'lease_expired', 'session_finished', 'transport_stopped',
                          'receive_idle_timeout', 'voice_session_failed', 'turn_timeout',
                          'turn_failed', 'audio_backpressure', 'wake_inference_failed',
                          'playback_stop_failed', 'speech_start', 'end_of_speech',
                          'no_speech', 'utterance_limit', 'audio_not_received', 'turn_cancelled',
                          'duplicate_activation', 'call', 'start_failed', 'assistant_error'))
SIP_CLEAN_REASONS = frozenset(('explicit_stop', 'lease_expired', 'shutdown', 'dtmf_stop',
                             'peer_ended', 'call_changed', 'call_preempted'))
SIP_COMPLETION_REASONS = SIP_CLEAN_REASONS | frozenset((
    'peer_closed', 'peer_error', 'oversized_packet', 'single_use_wire',
    'stream_identity_mismatch', 'wire_not_admitted', 'unsupported_audio_packet',
    'wire_not_ready', 'start_failed', 'admission_unverified', 'assistant_stopped',
    'silence_sender_stopped', 'receive_idle_timeout', 'session_failed',
    'observation_unavailable', 'stale_observation', 'voice_session_failed'))

# Diagnostic vocabulary is independent of the cleanup proof and public status.
# Never retain exception text, arbitrary AMI messages, or launch credentials.
STARTUP_DIAGNOSTIC_REASONS = frozenset((
    'lease_identity_changed', 'lease_not_held', 'lease_journal_invalid',
    'lease_journal_changed', 'explicit_stop', 'lease_expired', 'snapshot_stale',
    'pbx_proof_unavailable', 'call_identity_changed', 'call_admission_mismatch',
    'pbx_deadline_unverified', 'phone_not_idle', 'do_not_disturb_or_unknown',
    'call_changed', 'call_setup_timeout', 'single_use_call', 'ami_action_rejected',
    'stopped_before_start', 'stopped_during_start', 'stopped_before_session',
    'accept_timeout', 'ami_timeout', 'bad_ami_banner', 'invalid_ami_header',
    'invalid_ami_response', 'phone_redirect_refused', 'invalid_phone_response',
    'phone_rejected_action', 'phone_did_not_confirm_action', 'other'))
STARTUP_DIAGNOSTIC_ERROR_TYPES = frozenset((
    'BoundaryError', 'Refused', 'TimeoutError', 'OSError', 'RuntimeError', 'other'))
STARTUP_DIAGNOSTIC_STAGES = frozenset((
    'inputs_or_initialization', 'service_build', 'authorize_phone', 'listener_bind',
    'sip_start', 'peer_accept', 'wire_session', 'runner_unhandled', 'other'))
STARTUP_DIAGNOSTIC_AMI_STAGES = frozenset((
    'not_started', 'guard', 'login', 'idle_snapshot', 'dnd', 'originate',
    'setup_snapshot', 'verify_identity', 'verify_session', 'verify_deadline',
    'speaker_snapshot', 'speaker', 'grant', 'complete', 'other'))
STARTUP_DIAGNOSTIC_AMI_ACTIONS = frozenset((
    'Login', 'PJSIPShowEndpoints', 'CoreShowChannels', 'DBGet', 'Originate',
    'Getvar', 'Hangup', 'other'))
STARTUP_DIAGNOSTIC_AMI_ERRORS = frozenset((
    'authentication_failed', 'login_in_use', 'permission_denied', 'no_such_channel',
    'no_endpoints', 'endpoints_unavailable', 'unknown_action', 'other'))
_STARTUP_DIAGNOSTIC_FIELDS = {
    'reason': STARTUP_DIAGNOSTIC_REASONS, 'error_type': STARTUP_DIAGNOSTIC_ERROR_TYPES,
    'stage': STARTUP_DIAGNOSTIC_STAGES, 'ami_stage': STARTUP_DIAGNOSTIC_AMI_STAGES,
    'ami_action': STARTUP_DIAGNOSTIC_AMI_ACTIONS, 'ami_error': STARTUP_DIAGNOSTIC_AMI_ERRORS,
}
_DIAGNOSTIC_CLEANUP_ERRORS = frozenset((
    'connection_abort_failed', 'connection_close_failed', 'listener_cleanup_failed',
    'connection_cleanup_failed', 'assistant_cleanup_failed', 'pbx_cleanup_failed',
    'status_cleanup_failed', 'wire_close_failed', 'assistant_stop_failed', 'other'))
_DIAGNOSTIC_RESULT_REASONS = SIP_COMPLETION_REASONS | frozenset(('unknown', 'disconnected'))


SETUP_DIAGNOSTIC_MAX_ROWS = 2
SETUP_DIAGNOSTIC_MAX_COUNT = 65535
SETUP_DIAGNOSTIC_MAX_ID = 256
SETUP_DIAGNOSTIC_MAX_BYTES = 1024
_SETUP_STATES = frozenset(('Down', 'Rsrvd', 'OffHook', 'Dialing', 'Ring', 'Ringing',
                    'Up', 'Busy', 'Dialing Offhook', 'Pre-ring', 'Unknown'))
_SETUP_RELATIONS = frozenset(('root', 'root_b', 'unrelated', 'missing', 'invalid'))
_SETUP_FIELDS = {'version', 'predicate', 'channel_count', 'channel_count_capped',
          'own_count', 'own_count_capped', 'rows_truncated', 'rows'}
_SETUP_ROW_FIELDS = {'channel_sha256', 'uniqueid_sha256', 'linkedid_sha256',
              'uniqueid_root', 'linkedid_root', 'state'}


def _setup_text(value):
    return (type(value) is str and 0 < len(value) <= SETUP_DIAGNOSTIC_MAX_ID
            and value.isascii() and all(32 <= ord(c) < 127 for c in value))


def _setup_digest(value):
    return hashlib.sha256(value.encode('ascii')).hexdigest() if _setup_text(value) else None


def _setup_relation(value, root):
    if value is None or type(value) is str and not value:
        return 'missing'
    if not _setup_text(value):
        return 'invalid'
    return 'root' if value == root else 'root_b' if value == root + '-b' else 'unrelated'


def project_setup_rejection(root, channels, own):
    """Call only inside the existing rejected branch, on those same lists.

    Predicate names express the first true branch in Python's existing OR.
    Only two already-selected phone rows are visited; no extra snapshot or scan.
    """
    if not re.fullmatch(r'cisco-voice-[0-9a-f]{32}', root):
        raise ValueError('invalid_root')
    if type(channels) is not list or type(own) is not list or not own:
        raise ValueError('invalid_snapshot')
    rows = []
    for item in own[:SETUP_DIAGNOSTIC_MAX_ROWS]:
        if type(item) is not dict:
            raise ValueError('invalid_row')
        state = item.get('ChannelStateDesc')
        rows.append({
            'channel_sha256': _setup_digest(item.get('Channel')),
            'uniqueid_sha256': _setup_digest(item.get('Uniqueid')),
            'linkedid_sha256': _setup_digest(item.get('Linkedid')),
            'uniqueid_root': _setup_relation(item.get('Uniqueid'), root),
            'linkedid_root': _setup_relation(item.get('Linkedid'), root),
            'state': state if type(state) is str and state in _SETUP_STATES else 'Unknown',
        })
    result = {
        'version': 1,
        'predicate': 'multiple_phone_channels' if len(own) > 1 else 'unrelated_phone_channel',
        'channel_count': min(len(channels), SETUP_DIAGNOSTIC_MAX_COUNT),
        'channel_count_capped': len(channels) > SETUP_DIAGNOSTIC_MAX_COUNT,
        'own_count': min(len(own), SETUP_DIAGNOSTIC_MAX_COUNT),
        'own_count_capped': len(own) > SETUP_DIAGNOSTIC_MAX_COUNT,
        'rows_truncated': len(own) > SETUP_DIAGNOSTIC_MAX_ROWS,
        'rows': rows,
    }
    if not valid_setup_rejection(result):
        raise ValueError('invalid_setup_rejection')
    return result


def valid_setup_rejection(value):
    """Validate bounded private metadata without granting cleanup authority."""
    if type(value) is not dict or set(value) != _SETUP_FIELDS:
        return False
    if type(value['version']) is not int or value['version'] != 1:
        return False
    if type(value['predicate']) is not str or value['predicate'] not in (
            'multiple_phone_channels', 'unrelated_phone_channel'):
        return False
    for key in ('channel_count', 'own_count'):
        if type(value[key]) is not int or not 1 <= value[key] <= SETUP_DIAGNOSTIC_MAX_COUNT:
            return False
        if type(value[key + '_capped']) is not bool:
            return False
        if value[key + '_capped'] and value[key] != SETUP_DIAGNOSTIC_MAX_COUNT:
            return False
    if type(value['rows_truncated']) is not bool:
        return False
    count = value['own_count']
    if value['channel_count'] < count or value['rows_truncated'] != (count > SETUP_DIAGNOSTIC_MAX_ROWS):
        return False
    if (value['predicate'] == 'multiple_phone_channels') != (count > 1):
        return False
    rows = value['rows']
    if type(rows) is not list or len(rows) != min(count, SETUP_DIAGNOSTIC_MAX_ROWS):
        return False
    for row in rows:
        if type(row) is not dict or set(row) != _SETUP_ROW_FIELDS:
            return False
        for key in ('channel_sha256', 'uniqueid_sha256', 'linkedid_sha256'):
            if row[key] is not None and (type(row[key]) is not str
                    or not re.fullmatch('[0-9a-f]{64}', row[key])):
                return False
        for key in ('uniqueid_root', 'linkedid_root'):
            if type(row[key]) is not str or row[key] not in _SETUP_RELATIONS:
                return False
        if type(row['state']) is not str or row['state'] not in _SETUP_STATES:
            return False
    return len(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()) <= SETUP_DIAGNOSTIC_MAX_BYTES


def valid_startup_diagnostic(value):
    """Accept fixed categories and a bounded, context-specific private projection."""
    if type(value) is not dict or type(value.get("version")) is not int:
        return False
    fields = {"version", *_STARTUP_DIAGNOSTIC_FIELDS}
    if value["version"] == 2:
        fields.add("rejected_setup")
    elif value["version"] != 1:
        return False
    if set(value) != fields or not all(
            type(value[key]) is str and value[key] in allowed
            for key, allowed in _STARTUP_DIAGNOSTIC_FIELDS.items()):
        return False
    return value["version"] == 1 or (
        value["reason"] == "call_changed" and value["error_type"] == "BoundaryError"
        and value["stage"] == "sip_start"
        and value["ami_stage"] == "setup_snapshot"
        and valid_setup_rejection(value["rejected_setup"]))


def _safe_playback(value):
    _require(type(value) is dict and set(value) == set(PLAYBACK_LIMITS),
             'invalid_voice_response', 502)
    _require(all(type(value[key]) is int and 0 <= value[key] <= maximum
                 for key, maximum in PLAYBACK_LIMITS.items()), 'invalid_voice_response', 502)
    return {key: value[key] for key in PLAYBACK_LIMITS}


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
    _require(type(remaining) in (int, float) and 0 <= remaining <= MAX_SESSION_SECONDS
             and math.isfinite(remaining),
             'invalid_voice_response', 502)
    result = {key: value[key] for key in ('schema_version', 'phone', 'line', 'session_id', 'state',
              'active', 'remaining_seconds', 'stop_requested', 'stop_confirmed')}
    error = value.get('error')
    result['error'] = error if error is None or error in PUBLIC_ERRORS else 'voice_state_unavailable'
    if 'phase' in value:
        phase = value['phase']
        _require(phase is None or type(phase) is str and phase in PHASES,
                 'invalid_voice_response', 502)
        result['phase'] = phase
    if 'phase_reason' in value:
        reason = value['phase_reason']
        result['phase_reason'] = reason if type(reason) is str and reason in PHASE_REASONS else None
    if 'turns_completed' in value:
        count = value['turns_completed']
        _require(type(count) is int and 0 <= count <= MAX_TURNS, 'invalid_voice_response', 502)
        result['turns_completed'] = count
    if 'mode' in value:
        _require(value['mode'] in ('manual', 'wake'), 'invalid_voice_response', 502)
        result['mode'] = value['mode']
    if 'session_seconds' in value:
        duration = value['session_seconds']
        _require(type(duration) in (int, float) and 0 < duration <= MAX_SESSION_SECONDS
                 and math.isfinite(duration), 'invalid_voice_response', 502)
        result['session_seconds'] = duration
    if 'playback' in value:
        result['playback'] = _safe_playback(value['playback'])
    return result


class ControlError(Exception):
    """Fixed public refusal category, never an underlying exception string."""
    def __init__(self, reason, status=409):
        self.reason, self.status = reason, status
        super().__init__(reason)


def _require(condition, reason='invalid_voice_control_config', status=400):
    if not condition:
        raise ControlError(reason, status)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result


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
class ControlPhone:
    """Trusted physical identity for fencing; it does not enable voice Start."""
    phone_id: str
    lines: tuple[str, ...]
    device: str = field(repr=False)


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
    mode: str = 'manual'
    rearm_after_turn: bool = False
    media_transport: str = 'xml_rtp'


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
               'duration_seconds', 'receive_idle_seconds', 'volume', 'expected_source_port', 'media_transport'}
    _require(not set(config) - allowed)
    _require(config.get('media_transport', 'xml_rtp') in ('xml_rtp', 'sip_audiosocket'))
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
    _require(settings.enabled, 'voice_control_disabled')
    _require(config.get('media_transport', 'xml_rtp') != 'sip_audiosocket'
             or voice['wake']['mode'] == 'onnx', 'sip_requires_wake_mode')
    _require(voice['phones'] == {phone['id']: True}, 'one_selected_phone_required')
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
            endpoint = ('127.0.0.1' if config.get('media_transport') == 'sip_audiosocket'
                        else config['bind_ipv4'], config['bind_port'])
            _require(endpoint not in rtp_endpoints, 'voice_rtp_endpoint_collision')
            port = entry.get('display_port')
            _require(config.get('media_transport', 'xml_rtp') != 'sip_audiosocket' or port is None,
                     'sip_display_not_supported')
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
                                    config['bind_ipv4'], config['bind_port'], port,
                                    'wake' if _voice['wake']['mode'] == 'onnx' else 'manual',
                                    _voice['service'].get('rearm_after_turn', False),
                                    config.get('media_transport', 'xml_rtp')))
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
    launch_challenge: str | None = field(default=None, repr=False)
    cleanup_verified: bool = False
    error: str | None = None
    phase: str | None = 'preparing'
    phase_reason: str | None = None
    turns_completed: int = 0
    playback: dict | None = None
    # Internal lifecycle evidence; deliberately absent from finite wire status.
    requested_reason: str | None = None
    completion_reason: str | None = None
    supervised: bool = False


class VoiceControl:
    """One manager per host; a shared private state directory fences other managers.

    A crashed/ambiguous owner leaves a durable recovery_required reservation.
    Only verified cleanup permits another start. Never remove a live lock file.
    """
    def __init__(self, config, *, authorize, runner_factory=_SubprocessHandle,
                 clock=time.monotonic, monitor=True, control_phones=None):
        _require(isinstance(config, ControlConfig) and callable(authorize))
        _require(1 <= len(config.profiles) <= 32 and type(config.enabled) is bool)
        _require(all(isinstance(p, Profile) and type(p.duration) in (int, float)
                     and math.isfinite(p.duration) and 0 < p.duration <= MAX_SESSION_SECONDS
                     and p.mode in ('manual', 'wake') and type(p.rearm_after_turn) is bool
                     and p.media_transport in ('xml_rtp', 'sip_audiosocket')
                     and (p.media_transport != 'sip_audiosocket' or p.mode == 'wake' and p.display_port is None)
                     and p.lines and p.phone_id == p.lines[0] for p in config.profiles))
        all_lines = [line for p in config.profiles for line in p.lines]
        _require(len(all_lines) == len(set(all_lines))
                 and len({p.device for p in config.profiles}) == len(config.profiles)
                 and len({p.ipv4 for p in config.profiles}) == len(config.profiles),
                 'duplicate_physical_phone')
        _require(len({('127.0.0.1' if p.media_transport == 'sip_audiosocket' else p.bind_ipv4, p.bind_port)
                      for p in config.profiles}) == len(config.profiles),
                 'voice_rtp_endpoint_collision')
        display_endpoints = [(p.bind_ipv4, p.display_port) for p in config.profiles
                             if p.display_port is not None]
        _require(len(display_endpoints) == len(set(display_endpoints)), 'voice_display_port_collision')
        if control_phones is None:
            control_phones = tuple(ControlPhone(p.phone_id, p.lines, p.device)
                                   for p in config.profiles)
        _require(isinstance(control_phones, tuple) and 1 <= len(control_phones) <= 100,
                 'invalid_control_phone_inventory')
        for phone in control_phones:
            _require(isinstance(phone, ControlPhone)
                     and isinstance(phone.phone_id, str)
                     and isinstance(phone.lines, tuple) and 1 <= len(phone.lines) <= 2
                     and phone.phone_id == phone.lines[0]
                     and all(isinstance(line, str) and re.fullmatch(r'2[0-9]{2}', line)
                             for line in phone.lines)
                     and isinstance(phone.device, str) and re.fullmatch(r'SEP[0-9A-F]{12}', phone.device),
                     'invalid_control_phone_inventory')
        control_lines = [line for phone in control_phones for line in phone.lines]
        _require(len(control_lines) == len(set(control_lines))
                 and len({phone.device for phone in control_phones}) == len(control_phones),
                 'duplicate_control_phone')
        by_id = {phone.phone_id: phone for phone in control_phones}
        _require(all(by_id.get(p.phone_id) == ControlPhone(p.phone_id, p.lines, p.device)
                     for p in config.profiles), 'control_phone_inventory_mismatch')
        self.config, self.authorize, self.runner_factory, self.clock = config, authorize, runner_factory, clock
        self._profiles = {line: p for p in config.profiles for line in p.lines}
        self._control_phones = {line: phone for phone in control_phones for line in phone.lines}
        self._sessions, self._requests = OrderedDict(), {}
        self._current, self._lock, self._wake = {}, threading.RLock(), threading.Event()
        self._closed = False
        self.hands_free = None
        self._thread = None
        if monitor:
            self._thread = threading.Thread(target=self._monitor, name='voice-control', daemon=True)
            self._thread.start()

    def _profile(self, target, context, action):
        # Invalid targets reveal no registry/configuration before authorization.
        profiles = self._control_phones if action == 'control' else self._profiles
        p = profiles.get(target) if isinstance(target, str) else None
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

    def reserve_control(self, target, *, context):
        """Fence direct phone I/O against every manager using this state directory.

        This temporary reservation never changes a voice journal or signals a
        runner. Hold only the file descriptor across caller I/O, not the manager
        lock: status and Stop must remain available. The locked current journal,
        rather than a stale local session, determines external ownership.
        """
        p = self._profile(target, context, 'control')
        return self._reserve_phone(p, shutdown=False)

    def reserve_shutdown(self, target, *, context):
        """Retain a quiescent phone's fence until authentication listeners close."""
        p = self._profile(target, context, 'control')
        return self._reserve_phone(p, shutdown=True)

    @contextmanager
    def _reserve_phone(self, p, *, shutdown):
        fd = None
        try:
            with self._lock:
                if shutdown:
                    _require(self._closed, 'voice_shutdown_not_started', 503)
                else:
                    _require(not self._closed, 'voice_control_closed', 503)
                try:
                    fd = self._acquire(p)
                    prior = self._journal(p)
                except OSError:
                    raise ControlError('voice_state_unavailable', 503) from None
                _require(not prior or not prior['active'] and prior['state'] not in {
                    'starting', 'running', 'stopping', 'recovery_required', 'external_owner'},
                    'voice_phone_busy', 409)
            yield
        finally:
            if fd is not None:
                os.close(fd)

    def _progress(self, s):
        if s.directory is None or s.state not in ('starting', 'running', 'stopping'):
            return
        try:
            value = json.loads(_read(s.directory / 'progress.json', 2048))
            _require(isinstance(value, dict) and type(value.get('schema_version')) is int
                     and value['schema_version'] == 1)
            phase, count = value.get('phase'), value.get('turns_completed')
            _require(type(phase) is str and phase in PHASES
                     and type(count) is int and 0 <= count <= MAX_TURNS)
            stamp = value.get('updated_at')
            _require(type(stamp) in (int, float) and math.isfinite(stamp) and stamp > 0)
            reason = value.get('reason')
            playback = _safe_playback(value['playback']) if 'playback' in value else None
            s.phase, s.phase_reason = phase, reason if type(reason) is str and reason in PHASE_REASONS else None
            s.turns_completed = max(s.turns_completed, count)
            if playback is not None:
                s.playback = playback
        except (ControlError, ValueError, TypeError, OverflowError, RecursionError):
            # Progress is advisory; a missing update cannot block exact-session Stop.
            pass

    def _view(self, s):
        self._progress(s)
        remaining = 0 if s.started is None else max(0, s.profile.duration - (self.clock() - s.started))
        return {'schema_version': 1, 'phone': s.profile.phone_id, 'line': s.line,
                'session_id': s.session_id, 'state': s.state,
                'active': s.state in ('starting', 'running', 'stopping'),
                'remaining_seconds': round(remaining, 1), 'stop_requested': s.stop_requested,
                'stop_confirmed': s.stop_confirmed, 'error': s.error,
                'phase': s.phase, 'phase_reason': s.phase_reason,
                'turns_completed': s.turns_completed, 'mode': s.profile.mode,
                'session_seconds': s.profile.duration,
                **({'playback': dict(s.playback)} if s.playback is not None else {})}

    @staticmethod
    def _check_profile(p, config, phone, voice):
        _require((phone['id'], tuple(phone['lines']), phone['device'], phone['ipv4'],
                  float(config['duration_seconds']), config['bind_ipv4'], config['bind_port'])
                 == (p.phone_id, p.lines, p.device, p.ipv4, p.duration, p.bind_ipv4, p.bind_port),
                 'voice_profile_changed')
        _require(('wake' if voice['wake']['mode'] == 'onnx' else 'manual') == p.mode
                 and voice['service'].get('rearm_after_turn', False) == p.rearm_after_turn,
                 'voice_profile_changed')
        _require(config.get('media_transport', 'xml_rtp') == p.media_transport, 'voice_profile_changed')

    def _prepare(self, p, s):
        config, gateway, phone, voice, token = _inputs(p.transport_config, p.voice_config, self.config.workspace)
        self._check_profile(p, config, phone, voice)
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
        runner = 'voice_session_sip.py' if p.media_transport == 'sip_audiosocket' else 'voice_session.py'
        argv = [self.config.python_executable, str(ROOT / 'scripts' / runner),
                '--transport-config', str(directory / 'transport.json'),
                '--voice-config', str(directory / 'voice.json'), '--workspace', str(self.config.workspace),
                '--status-file', str(directory / 'progress.json'),
                '--ffmpeg', self.config.ffmpeg_executable,
                '--run', '--attended', '--exclusive-media', '--enable-microphone']
        if p.display_port is not None:
            argv += ['--display-port', str(p.display_port)]
        if p.media_transport == 'sip_audiosocket':
            argv += ['--lease-fd', str(s.lease_fd), '--session-id', s.session_id,
                     '--state-directory', str(self.config.state_directory),
                     '--lease-expires-unix', str(time.time() + p.duration)]
        return argv

    def start(self, target, request_id, *, context, attended=False, exclusive_media=False,
              enable_microphone=False, dry_run=False, _hands_free_generation=None):
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
                self._check_profile(p, latest[0], latest[2], latest[3])
                return {'schema_version': 1, 'phone': p.phone_id, 'line': target,
                        'valid': True, 'network_actions': 0, 'microphone_started': False,
                        'duration_seconds': p.duration}
            if self.hands_free is not None:
                self.hands_free.admit(p.phone_id, _hands_free_generation)
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
            s = _Session(p, secrets.token_hex(16), request_id, target, fd,
                         supervised=_hands_free_generation is not None)
            self._sessions[s.session_id] = s; self._requests[request_id] = s; self._current[p.phone_id] = s
            if self.hands_free is not None:
                self.hands_free.started(s, _hands_free_generation)
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
                    s.launch_challenge = secrets.token_hex(32)
                    env.update(CISCO_VOICE_SESSION_ID=s.session_id,
                               CISCO_VOICE_LAUNCH_CHALLENGE=s.launch_challenge)
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
        s.phase = 'error' if error else 'stopped'
        s.completion_reason = 'start_failed' if error else s.requested_reason
        self._write(s)
        s.cleanup_verified = True
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
                                 stop_confirmed=False, error='voice_owner_unavailable',
                                 phase=None, phase_reason=None)
                return _safe_status(prior)
            return {'schema_version': 1, 'phone': p.phone_id, 'line': target, 'session_id': None,
                    'state': 'idle', 'active': False, 'remaining_seconds': 0,
                    'stop_requested': False, 'stop_confirmed': False, 'error': None,
                    'phase': None, 'phase_reason': None, 'turns_completed': 0,
                    'mode': p.mode, 'session_seconds': p.duration}

    def _request_stop(self, s, reason='explicit_stop'):
        _require(reason in ('explicit_stop', 'lease_expired', 'call_preempted', 'shutdown'),
                 'invalid_voice_stop_reason')
        if s.state not in ('starting', 'running', 'stopping'):
            return
        s.stop_requested = True
        if s.stop_at is None:
            s.requested_reason = reason
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
            if self.hands_free is not None and self._current.get(p.phone_id) is s:
                self.hands_free.manual_stop(p.phone_id, session_id=s.session_id)
            self._request_stop(s)
            return self._view(s)

    def _proof_owner(self, s):
        # Called under the manager lock. Do not overwrite another session's
        # journal even when a mismatched child claims it never started media.
        _require(self._sessions.get(s.session_id) is s
                 and self._requests.get(s.request_id) is s
                 and self._current.get(s.profile.phone_id) is s and s.lease_fd >= 0,
                 'voice_owner_unavailable')
        journal = json.loads(_read(self._paths(s.profile)[1]), object_pairs_hook=_unique_object)
        _require(isinstance(journal, dict) and journal.get('reserved') is True
                 and journal.get('session_id') == s.session_id
                 and journal.get('request_id') == s.request_id
                 and journal.get('phone') == s.profile.phone_id,
                 'voice_owner_unavailable')

    @staticmethod
    def _completed_child(s, raw, exit_codes):
        code = s.handle.poll()
        _require(type(code) is int and code in exit_codes and not s.killed
                 and s.spawn_attempted is True and s.handle.read_result() == raw)

    def _launch_proof(self, s, proof, raw):
        self._completed_child(s, raw, (1,))
        _require(not s.stop_requested and s.stop_at is None)
        _require(type(proof['version']) is int and proof['version'] == 1
                 and proof['session_id'] == s.session_id
                 and type(proof['challenge']) is str
                 and re.fullmatch(r'[0-9a-f]{64}', proof['challenge'])
                 and type(s.launch_challenge) is str
                 and hmac.compare_digest(proof['challenge'], s.launch_challenge)
                 and proof['cleanup_complete'] is True)

    def _no_media_proof(self, s, report, raw):
        _require(s.profile.media_transport == 'xml_rtp')
        proof = report['cleanup_proof']
        _require(type(proof) is dict and set(proof) == {
            'version', 'kind', 'session_id', 'challenge', 'rtp_start_attempted',
            'watchdog_arm_attempted', 'watchdog_process_created', 'cleanup_complete'})
        self._launch_proof(s, proof, raw)
        _require(proof['kind'] == 'safe_no_media_started'
                 and proof['rtp_start_attempted'] is False
                 and proof['watchdog_arm_attempted'] is False
                 and proof['watchdog_process_created'] is False)
        _require(set(report) == {'error', 'result', 'states', 'turns_completed', 'capture_observed',
                                'input', 'output_frames', 'recorded_audio', 'cleanup_errors',
                                'watchdog_armed', 'cleanup_proof'}
                 and report['error'] == 'voice_session_failed' and report['result'] is None
                 and report['watchdog_armed'] is False
                 and type(report['cleanup_errors']) is list and report['cleanup_errors'] == []
                 and type(report['turns_completed']) is int and report['turns_completed'] == 0
                 and s.turns_completed == 0 and report['capture_observed'] is False
                 and type(report['input']) is dict and report['input'] == {}
                 and type(report['output_frames']) is int and report['output_frames'] == 0
                 and report['recorded_audio'] is False and type(report['states']) is list
                 and all(state in ('stopped', 'error') for state in report['states']))
        return True

    def _no_sip_proof(self, s, report, raw):
        _require(s.profile.media_transport == 'sip_audiosocket')
        if 'startup_diagnostic' in report:
            _require(valid_startup_diagnostic(report['startup_diagnostic'])
                     # A captured setup rejection follows an Originate attempt.
                     and report['startup_diagnostic']['version'] == 1)
        proof = report['cleanup_proof']
        _require(type(proof) is dict and set(proof) == {
            'version', 'kind', 'transport', 'session_id', 'challenge',
            'originate_attempted', 'cleanup_complete'})
        self._launch_proof(s, proof, raw)
        _require(proof['kind'] == 'safe_no_sip_originate'
                 and proof['transport'] == 'sip_audiosocket'
                 and proof['originate_attempted'] is False)
        _require(set(report) - {'startup_diagnostic'} == {
            'transport', 'session_id', 'recorded_audio', 'error', 'result', 'cleanup_errors',
            'turns_completed', 'originate_attempted', 'speaker_attempted', 'pbx_deadline_armed',
            'pbx_call_absent', 'safe_no_media_started', 'input_frames', 'output_frames', 'cleanup_proof'}
                 and report['transport'] == 'sip_audiosocket' and report['session_id'] == s.session_id
                 and report['error'] == 'voice_session_failed' and report['result'] is None
                 and report['originate_attempted'] is False and report['speaker_attempted'] is False
                 and report['pbx_deadline_armed'] is False and report['pbx_call_absent'] is True
                 and report['safe_no_media_started'] is True and report['recorded_audio'] is False
                 and type(report['cleanup_errors']) is list and report['cleanup_errors'] == []
                 and s.turns_completed == 0
                 and all(type(report[key]) is int and report[key] == 0
                         for key in ('turns_completed', 'input_frames', 'output_frames')))
        return True

    def _sip_startup_stop_proof(self, s, report, raw):
        """Validate a stopped, observed startup without inventing a media result."""
        _require(s.profile.media_transport == 'sip_audiosocket')
        self._completed_child(s, raw, (1,))
        _require(s.stop_requested is True and s.stop_at is not None
                 and s.requested_reason in ('explicit_stop', 'lease_expired', 'call_preempted', 'shutdown'))
        proof = report['cleanup_proof']
        _require(type(proof) is dict and set(proof) == {
            'version', 'kind', 'transport', 'session_id', 'challenge', 'originate_attempted',
            'startup_drained', 'startup_sealed', 'phone_seen', 'cleanup_complete'})
        _require(type(proof['version']) is int and proof['version'] == 1
                 and proof['kind'] == 'safe_sip_startup_stop'
                 and proof['transport'] == 'sip_audiosocket' and proof['session_id'] == s.session_id
                 and type(proof['challenge']) is str
                 and re.fullmatch(r'[0-9a-f]{64}', proof['challenge'])
                 and type(s.launch_challenge) is str
                 and hmac.compare_digest(proof['challenge'], s.launch_challenge)
                 and all(proof[key] is True for key in ('originate_attempted', 'startup_drained',
                     'startup_sealed', 'phone_seen', 'cleanup_complete')))
        diagnostic = report.get('startup_diagnostic')
        _require(valid_startup_diagnostic(diagnostic)
                 and diagnostic['reason'] == 'explicit_stop'
                 and diagnostic['stage'] == 'sip_start' and diagnostic['error_type'] == 'BoundaryError')
        _require(set(report) == {
            'transport', 'session_id', 'recorded_audio', 'error', 'result', 'cleanup_errors',
            'turns_completed', 'originate_attempted', 'speaker_attempted', 'pbx_deadline_armed',
            'pbx_call_absent', 'safe_no_media_started', 'input_frames', 'output_frames',
            'cleanup_proof', 'startup_diagnostic'}
                 and report['transport'] == 'sip_audiosocket' and report['session_id'] == s.session_id
                 and report['error'] == 'voice_session_failed' and report['result'] is None
                 and report['originate_attempted'] is True and type(report['speaker_attempted']) is bool
                 and report['pbx_deadline_armed'] is True and report['pbx_call_absent'] is True
                 and report['safe_no_media_started'] is False and report['recorded_audio'] is False
                 and type(report['cleanup_errors']) is list and report['cleanup_errors'] == []
                 and s.turns_completed == 0
                 and all(type(report[key]) is int and report[key] == 0
                         for key in ('turns_completed', 'input_frames', 'output_frames')))
        return True

    def _save_failure(self, s, raw, report, *, cleanup_evidence_verified):
        """Best-effort, bounded diagnostic evidence; never a recovery authority."""
        if (s.profile.media_transport != 'sip_audiosocket'
                or s.state not in ('failed', 'recovery_required')):
            return
        temporary = None
        try:
            # Refuse stale sessions, foreign journals and changed private paths.
            # This runs before the terminal journal write or lease release.
            self._proof_owner(s)
            self._mkdir()
            code = s.handle.poll()
            status = 'unavailable'
            if type(raw) is bytes:
                if len(raw) > MAX_REPORT or s.handle.overflow:
                    status = 'overflow'
                elif report is None:
                    status = 'invalid_json'
                elif type(report) is not dict:
                    status = 'not_object'
                else:
                    try:
                        self._completed_child(s, raw, (0, 1))
                        status = ('matched' if report.get('transport') == s.profile.media_transport
                                  and report.get('session_id') == s.session_id else 'identity_mismatch')
                    except Exception:
                        status = 'child_unverified'
            receipt = {
                'schema_version': 1, 'session_id': s.session_id, 'phone': s.profile.phone_id,
                'transport': s.profile.media_transport, 'state': s.state,
                'error': s.error if s.error in PUBLIC_ERRORS else 'other',
                'returncode': code if type(code) is int and -255 <= code <= 255 else None,
                'report_status': status,
                'report_bytes': min(len(raw), MAX_REPORT + 1) if type(raw) is bytes else None,
                'report_overflow': s.handle.overflow is True,
                'stop_requested': s.stop_requested is True, 'killed': s.killed is True,
                'stop_confirmed': s.stop_confirmed is True,
                'cleanup_evidence_verified': cleanup_evidence_verified is True,
            }
            if status == 'matched':
                def category(value, allowed):
                    return value if type(value) is str and value in allowed else 'other'

                def errors(value):
                    if type(value) is not list:
                        return None
                    return sorted({category(item, _DIAGNOSTIC_CLEANUP_ERRORS)
                                   for item in value[:32]})

                claims = {
                    'error': None if report.get('error') is None else
                             category(report['error'], PUBLIC_ERRORS),
                    'cleanup_errors': errors(report.get('cleanup_errors')),
                }
                for key in ('originate_attempted', 'speaker_attempted', 'pbx_deadline_armed',
                            'pbx_call_absent', 'safe_no_media_started', 'recorded_audio'):
                    value = report.get(key)
                    claims[key] = value if type(value) is bool else None
                for key, limit in (('turns_completed', MAX_TURNS), ('input_frames', 100000000),
                                   ('output_frames', 100000000), ('silence_frames', 100000000)):
                    value = report.get(key)
                    claims[key] = value if type(value) is int and 0 <= value <= limit else None
                result = report.get('result')
                claims['result_present'] = type(result) is dict
                if type(result) is dict:
                    claims['result_reason'] = category(result.get('reason'), _DIAGNOSTIC_RESULT_REASONS)
                    value = result.get('stop_confirmed')
                    claims['result_stop_confirmed'] = value if type(value) is bool else None
                    claims['result_cleanup_errors'] = errors(result.get('cleanup_errors'))
                diagnostic = report.get('startup_diagnostic')
                claims['startup_diagnostic_status'] = (
                    'absent' if 'startup_diagnostic' not in report else
                    'valid' if valid_startup_diagnostic(diagnostic) else 'invalid')
                if valid_startup_diagnostic(diagnostic):
                    claims['startup_diagnostic'] = dict(diagnostic)
                receipt['child_claims'] = claims
            encoded = (json.dumps(receipt, sort_keys=True, separators=(',', ':')) + '\n').encode()
            _require(len(encoded) <= MAX_FAILURE_RECEIPT)
            path = self._paths(s.profile)[1].with_suffix('.last-failure.json')
            fd, temporary = tempfile.mkstemp(prefix='.voice-failure-', dir=path.parent)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except Exception:
            # Missing diagnostics must not change the ownership or cleanup decision.
            pass
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _finish(self, s, raw):
        with self._lock:
            self._finish_locked(s, raw)

    def _finish_locked(self, s, raw):
        confirmed, clean, never_started, reason = False, False, False, 'voice_result_unavailable'
        s.cleanup_verified = False
        if s.profile.media_transport == 'sip_audiosocket':
            self._proof_owner(s)
        self._progress(s)
        report = None
        try:
            _require(type(raw) is bytes and len(raw) <= MAX_REPORT and not s.handle.overflow)
            report = json.loads(raw, object_pairs_hook=_unique_object)
        except Exception:
            pass
        if (s.profile.media_transport == 'xml_rtp'
                and isinstance(report, dict) and 'cleanup_proof' in report):
            self._proof_owner(s)
        try:
            _require(isinstance(report, dict))
            if 'cleanup_proof' in report:
                proof = report['cleanup_proof']
                if (s.profile.media_transport == 'sip_audiosocket' and type(proof) is dict
                        and proof.get('kind') == 'safe_sip_startup_stop'):
                    confirmed = self._sip_startup_stop_proof(s, report, raw)
                    clean, reason = True, None
                    s.completion_reason = s.requested_reason
                    s.phase_reason = 'call' if s.requested_reason == 'call_preempted' else s.requested_reason
                else:
                    validate = self._no_sip_proof if s.profile.media_transport == 'sip_audiosocket' else self._no_media_proof
                    never_started = validate(s, report, raw)
                    clean, reason = True, 'voice_session_failed'
                    s.phase_reason = 'start_failed'
                    s.completion_reason = 'start_failed'
            elif s.profile.media_transport == 'sip_audiosocket':
                confirmed, clean, reason = self._sip_stop_report(s, report, raw)
            else:
                confirmed, clean, reason = self._stop_report(s, report)
        except Exception:
            # A malformed proof cannot fall back to contradictory Stop fields.
            confirmed, clean, never_started = False, False, False
        s.stop_confirmed = bool(confirmed and not s.killed)
        if s.stop_confirmed and clean or never_started:
            s.state, s.error = ('failed', reason) if reason else ('stopped', None)
            s.phase = 'error' if reason else 'stopped'
            self._save_failure(s, raw, report, cleanup_evidence_verified=True)
            self._write(s)
            s.cleanup_verified = True
            self._release(s)
        else:
            s.state, s.error = 'recovery_required', 'voice_stop_unconfirmed'
            s.phase = 'error'
            self._save_failure(s, raw, report, cleanup_evidence_verified=False)
            self._write(s)
            # The durable reservation now blocks every manager, even after
            # this process exits. Keep snapshots for private investigation.
            if s.lease_fd >= 0:
                os.close(s.lease_fd); s.lease_fd = -1
        s.handle = None

    def _sip_stop_report(self, s, report, raw):
        self._completed_child(s, raw, (0, 1))
        _require(report.get('transport') == 'sip_audiosocket'
                 and report.get('session_id') == s.session_id
                 and report.get('safe_no_media_started') is False)
        result = report.get('result')
        confirmed = (report.get('originate_attempted') is True
                     and report.get('pbx_deadline_armed') is True
                     and report.get('pbx_call_absent') is True
                     and isinstance(result, dict) and result.get('stop_confirmed') is True)
        clean = (type(report.get('cleanup_errors')) is list and report['cleanup_errors'] == []
                 and isinstance(result, dict) and type(result.get('cleanup_errors')) is list
                 and result['cleanup_errors'] == [])
        terminal = result.get('reason') if isinstance(result, dict) else None
        completed = isinstance(terminal, str) and terminal in SIP_CLEAN_REASONS
        s.completion_reason = (terminal if isinstance(terminal, str)
                               and terminal in SIP_COMPLETION_REASONS else 'voice_session_failed')
        # SIGTERM intentionally has no public reason payload. Recover only the
        # manager's own first stop cause; independent peer/DTMF evidence wins.
        if terminal == 'explicit_stop' and s.requested_reason in (
                'lease_expired', 'call_preempted', 'shutdown'):
            s.completion_reason = s.requested_reason
        public_reason = {'call_changed': 'call', 'call_preempted': 'call',
                         'peer_ended': 'transport_stopped', 'peer_closed': 'transport_stopped',
                         'dtmf_stop': 'explicit_stop'}.get(s.completion_reason, s.completion_reason)
        s.phase_reason = public_reason if public_reason in PHASE_REASONS else 'voice_session_failed'
        count = report.get('turns_completed')
        if type(count) is int and 0 <= count <= MAX_TURNS:
            s.turns_completed = max(s.turns_completed, count)
        return confirmed, clean, 'voice_session_failed' if not completed or report.get('error') is not None else None

    @staticmethod
    def _stop_report(s, report):
        result = report.get('result')
        count = report.get('turns_completed')
        if type(count) is int and 0 <= count <= MAX_TURNS:
            s.turns_completed = max(s.turns_completed, count)
        confirmed = (report.get('watchdog_armed') is True and report.get('watchdog_finished') is True
                     and type(report.get('watchdog_stop_errors')) is int
                     and report['watchdog_stop_errors'] == 0
                     and (report.get('watchdog_stop_confirmed') is True
                          or isinstance(result, dict) and result.get('stop_confirmed') is True))
        clean = report.get('cleanup_errors') == []
        completed = (isinstance(result, dict)
                     and result.get('reason') in ('turn_complete', 'explicit_stop',
                                                 'display_stop', 'shutdown', 'lease_expired'))
        if isinstance(result, dict) and result.get('reason') in PHASE_REASONS:
            s.phase_reason = result['reason']
        reason = 'voice_session_failed' if report.get('error') or not completed else None
        return confirmed, clean, reason

    def tick(self):
        """One nonblocking supervisor pass; deterministic with injected test handles."""
        with self._lock:
            now = self.clock()
            for s in list(self._sessions.values()):
                if s.handle is None:
                    continue
                code = s.handle.poll()
                if code is None:
                    if s.handle.overflow:
                        self._request_stop(s)
                    # The independent owner monitor enforces early rotation;
                    # slow observations for another phone cannot delay it.
                    elif s.started is not None and now - s.started >= (
                            s.profile.duration - (5 if s.supervised else 0)):
                        self._request_stop(s, reason='lease_expired')
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
            if self.hands_free is not None:
                try:
                    self.hands_free.close()
                except Exception:
                    failed = True
            for s in self._sessions.values():
                try:
                    self._request_stop(s, reason='shutdown')
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
                         and type(duration) in (int, float) and math.isfinite(duration)
                         and 0 < duration <= MAX_SESSION_SECONDS
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
