#!/usr/bin/env python3
"""Control one Cisco line and its hosting phone through the private gateway."""
import argparse
import base64
import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import subprocess
import sys
import time

from cisco_http import ERROR_REASONS, SCHEDULE_PATH, gateway_url, json_loads_unique, perform

COMMANDS = {
    'dial': 'dial NUMBER', 'intercom': 'intercom NUMBER', 'answer': 'answer', 'hangup': 'hangup',
    'speaker_toggle': 'speaker toggle', 'dnd': 'dnd [on|off]',
    'hardware_volume': 'volume ringer|call up|down [--steps 1..5]',
    'autoanswer': 'autoanswer [on|off]', 'message': 'message TEXT [--title TITLE]',
    'image': 'image FILE [--title TITLE]', 'ring': 'ring [DESTINATION]',
    'alert': 'alert timer|alarm', 'alert_stop': 'stop-alert [--alert-id ID]',
}
VOICE_COMMANDS = ['voice status', 'voice start --idempotency-key KEY --attended --exclusive-media --enable-microphone',
                  'voice stop [--session-id ID]', 'voice enable', 'voice disable',
                  'voice recovery-status', 'voice recover --session-id ID --journal-sha256 SHA']
HANDS_FREE_STATES = frozenset(('disabled', 'waiting_idle', 'starting', 'running',
                             'stopping', 'backoff', 'faulted'))
HANDS_FREE_ERRORS = frozenset(('hands_free_state_unavailable', 'hands_free_owner_unavailable',
    'hands_free_recovery_required', 'hands_free_observation_failed',
    'hands_free_retry_exhausted', 'hands_free_start_failed'))
VOICE_PHASES = frozenset(('preparing', 'armed', 'listening', 'thinking', 'speaking', 'stopped', 'error'))
VOICE_PHASE_REASONS = frozenset((
    'model_warmup', 'wake_ready', 'new_session', 'turn_complete', 'user_stop',
    'explicit_stop', 'display_stop', 'shutdown', 'lease_expired', 'session_finished',
    'transport_stopped', 'receive_idle_timeout', 'voice_session_failed', 'turn_timeout',
    'turn_failed', 'audio_backpressure', 'wake_inference_failed', 'playback_stop_failed',
    'speech_start', 'end_of_speech', 'no_speech', 'utterance_limit',
    'audio_not_received', 'turn_cancelled', 'duplicate_activation', 'call',
    'start_failed', 'assistant_error'))
VOICE_PUBLIC_ERRORS = frozenset((
    'voice_state_unavailable', 'voice_owner_unavailable', 'voice_start_failed',
    'voice_start_uncertain', 'voice_signal_failed', 'voice_session_failed',
    'voice_stop_unconfirmed'))
VOICE_EXTERNAL_OWNER_NOTE = (
    "The gateway cannot stop this session. For a wake Job, use Stop on its live wake screen, "
    "or have the operator shut down the exact owning Job with its normal grace period. "
    "Confirm cleanup before starting again.")
MAX_FILE = 2 * 1024 * 1024
SCHEDULE_COMMANDS = ['schedule', 'timer [status]', 'timer start MINUTES [--replace]',
    'timer cancel', 'alarm [status]', 'alarm set HH:MM --days DAYS [--enable] [--replace]',
    'alarm on', 'alarm off']


class Refused(Exception):
    def __init__(self, reason, *, http_status=None):
        self.reason, self.http_status = reason, http_status


def require(value, reason):
    if not value:
        raise Refused(reason)


def read_file(path, limit, *, private=False):
    try:
        fd = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_size <= limit, 'file_type_or_size_invalid')
            if private:
                require(info.st_uid == os.getuid() and not info.st_mode & 0o077,
                        'private_file_requires_owner_only_permissions')
            data = source.read(limit + 1)
            require(len(data) <= limit, 'file_too_large')
            return data
    except OSError:
        raise Refused('file_unavailable_or_symlink') from None


def load_config(path):
    try:
        config = json_loads_unique(read_file(path, 16384, private=True))
        require(isinstance(config, dict), 'config_requires_object')
        allowed = {'gateway_url', 'token', 'token_file', 'transport', 'kubectl', 'home_assistant'}
        require(set(config) <= allowed, 'unknown_config_field')
        config['gateway_url'] = gateway_url(config['gateway_url'])
        require(('token' in config) != ('token_file' in config), 'choose_token_or_token_file')
        if 'token_file' in config:
            token_path = Path(config['token_file']).expanduser()
            if not token_path.is_absolute():
                token_path = Path(path).expanduser().parent / token_path
            config['token'] = read_file(token_path, 512, private=True).decode().strip()
        require(isinstance(config['token'], str) and
                re.fullmatch(r'[A-Za-z0-9_-]{32,128}', config['token']), 'invalid_token')
        config.setdefault('transport', 'direct')
        require(config['transport'] in ('direct', 'kubectl'), 'invalid_transport')
        if config['transport'] == 'kubectl':
            kube = config.get('kubectl')
            require(isinstance(kube, dict) and set(kube) ==
                    {'context', 'namespace', 'selector', 'container'}, 'invalid_kubectl_config')
            for field in ('context', 'namespace', 'container'):
                require(isinstance(kube[field], str) and
                        re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}', kube[field]),
                        'invalid_kubectl_target')
            require(isinstance(kube['selector'], str) and len(kube['selector']) <= 256 and
                    re.fullmatch(r'[A-Za-z0-9_./-]+=[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_./-]+=[A-Za-z0-9_.-]+)*',
                                 kube['selector']), 'invalid_kubectl_selector')
        else:
            require('kubectl' not in config, 'kubectl_requires_explicit_transport')
        if 'home_assistant' in config:
            config['home_assistant'] = load_ha_config(config['home_assistant'], path)
        return config
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise Refused('invalid_private_config') from None


def load_ha_config(config, path):
    require(isinstance(config, dict) and set(config) <=
            {'url', 'token', 'token_file', 'transport', 'kubectl', 'entry_id'}, 'invalid_ha_config')
    config = dict(config)
    config['gateway_url'] = gateway_url(config.pop('url'))
    require(isinstance(config.get('entry_id'), str) and
            re.fullmatch(r'[A-Za-z0-9_-]{8,128}', config['entry_id']), 'invalid_ha_entry_id')
    require(('token' in config) != ('token_file' in config), 'choose_ha_token_or_token_file')
    if 'token_file' in config:
        token_path = Path(config['token_file']).expanduser()
        if not token_path.is_absolute():
            token_path = Path(path).expanduser().parent / token_path
        config['token'] = read_file(token_path, 4097, private=True).decode().strip()
    require(isinstance(config['token'], str) and re.fullmatch(r'[A-Za-z0-9_.-]{32,4096}', config['token']),
            'invalid_ha_token')
    require(config.get('transport') in ('direct', 'kubectl'), 'ha_requires_explicit_transport')
    if config['transport'] == 'kubectl':
        kube = config.get('kubectl')
        require(isinstance(kube, dict) and set(kube) == {'context', 'namespace', 'selector', 'container'},
                'invalid_ha_kubectl_config')
        for field in ('context', 'namespace', 'container'):
            require(isinstance(kube[field], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}', kube[field]),
                    'invalid_ha_kubectl_target')
        require(isinstance(kube['selector'], str) and len(kube['selector']) <= 256 and re.fullmatch(
            r'[A-Za-z0-9_./-]+=[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_./-]+=[A-Za-z0-9_.-]+)*', kube['selector']),
            'invalid_ha_kubectl_selector')
    else:
        require('kubectl' not in config, 'ha_kubectl_requires_explicit_transport')
    config['api'] = 'home_assistant'
    return config


class Gateway:
    def __init__(self, config, timeout=20):
        self.config, self.timeout, self.pod = config, timeout, None
        self.on_dispatch = None

    def run(self, args, *, input_data=None):
        try:
            result = subprocess.run(args, input=input_data, capture_output=True,
                                    timeout=self.timeout + 10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise Refused('kubectl_outcome_unknown') from None
        require(result.returncode == 0, 'kubectl_failed_no_automatic_retry')
        require(len(result.stdout) <= 1024 * 1024, 'kubectl_response_too_large')
        try:
            return json_loads_unique(result.stdout)
        except (ValueError, UnicodeError):
            raise Refused('invalid_kubectl_response') from None

    def kube_args(self):
        k = self.config['kubectl']
        return ['kubectl', '--context', k['context'], '--namespace', k['namespace']]

    def resolve_pod(self):
        k = self.config['kubectl']
        found = self.run(self.kube_args() + ['get', 'pods', '--selector', k['selector'],
                         '-o', 'json', '--request-timeout', str(self.timeout) + 's'])
        require(isinstance(found, dict) and isinstance(found.get('items'), list), 'invalid_pod_list')
        candidates = []
        for pod in found['items']:
            if (pod.get('metadata', {}).get('deletionTimestamp') or
                    pod.get('status', {}).get('phase') != 'Running'):
                continue
            if not any(c.get('type') == 'Ready' and c.get('status') == 'True'
                       for c in pod.get('status', {}).get('conditions', [])):
                continue
            if any(c.get('name') == k['container'] and c.get('ready') is True
                   for c in pod.get('status', {}).get('containerStatuses', [])):
                candidates.append(pod.get('metadata', {}).get('name'))
        require(len(candidates) == 1 and isinstance(candidates[0], str) and
                re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}', candidates[0]), 'require_one_ready_target_pod')
        return candidates[0]

    def request(self, path, body=None):
        payload = {'url': self.config['gateway_url'], 'token': self.config['token'],
                   'timeout': self.timeout, 'path': path, 'body': body,
                   'api': self.config.get('api', 'gateway')}
        if self.config['transport'] == 'direct':
            if self.on_dispatch is not None:
                self.on_dispatch()
            result = perform(payload)
        else:
            if self.pod is None:
                self.pod = self.resolve_pod()
            # The fixed repository worker is code; all credentials and user data
            # travel only on stdin, never in argv, shell strings or environment.
            source = Path(__file__).resolve().with_name('cisco_http.py').read_text()
            args = self.kube_args() + ['exec', '-i', self.pod, '-c',
                   self.config['kubectl']['container'], '--', 'python3', '-c', source]
            input_data = json.dumps(payload).encode()
            # Discovery and local preparation cannot have submitted a request.
            # Once exec starts, a lost response cannot prove it did not run.
            if self.on_dispatch is not None:
                self.on_dispatch()
            result = self.run(args, input_data=input_data)
        require(isinstance(result, dict) and type(result.get('ok')) is bool, 'invalid_transport_response')
        if not result['ok']:
            # Worker errors are fixed strings, never raw remote exceptions.
            reason = result.get('reason', '')
            require(isinstance(reason, str) and reason in ERROR_REASONS,
                    'invalid_transport_error')
            status = result.get('http_status')
            require(status is None or type(status) is int and 300 <= status <= 599,
                    'invalid_transport_error')
            raise Refused(reason, http_status=status)
        require(isinstance(result.get('data'), dict), 'invalid_gateway_response')
        return result['data']


def validate_inventory(data, line=None):
    require(data.get('ami_fresh') is True, 'gateway_ami_not_fresh')
    observed = data.get('observed_at')
    try:
        fresh = (type(observed) in (int, float) and math.isfinite(observed)
                 and -5 <= time.time() - observed <= 90)
    except OverflowError:
        fresh = False
    require(fresh, 'gateway_inventory_stale')
    phones = data.get('phones')
    require(isinstance(phones, list) and 1 <= len(phones) <= 32, 'invalid_phone_inventory')
    seen, selected = set(), None
    for phone in phones:
        require(isinstance(phone, dict), 'invalid_phone_inventory')
        lines = phone.get('lines')
        require(isinstance(lines, list) and 1 <= len(lines) <= 2 and phone.get('id') == lines[0],
                'invalid_phone_mapping')
        for number in lines:
            require(isinstance(number, str) and re.fullmatch(r'2[0-9]{2}', number)
                    and number not in seen, 'ambiguous_or_invalid_line_mapping')
            seen.add(number)
        validate_phone_status(phone)
        if line in lines:
            selected = phone
    for name in ('dial_allowlist', 'ring_allowlist'):
        require(isinstance(data.get(name), list) and all(isinstance(n, str) and
                re.fullmatch(r'[0-9]{3,8}', n) for n in data[name]), 'invalid_destination_inventory')
    softphones = validate_softphones(data, seen)
    validate_services(data, seen | {phone['extension'] for phone in softphones})
    if line is None:
        return phones
    require(selected is not None, 'line_not_in_cisco_gateway_inventory')
    return selected


def validate_phone_status(phone):
    room = phone.get('room')
    require(room is None or valid_room(room), 'invalid_phone_room')
    for key in ('online', 'dnd', 'autoanswer', 'dnd_pending'):
        require(type(phone.get(key)) is bool, 'invalid_phone_status')
    capabilities = phone.get('capabilities')
    require(isinstance(capabilities, list) and all(isinstance(c, str) for c in capabilities),
            'invalid_phone_capabilities')
    channels = phone.get('channels')
    require(isinstance(channels, list) and len(channels) <= 32, 'invalid_channel_inventory')
    for ch in channels:
        require(isinstance(ch, dict) and ch.get('line') in phone['lines'] and
                isinstance(ch.get('name'), str) and re.fullmatch(
                    r'PJSIP/' + ch['line'] + r'-[A-Za-z0-9]+', ch['name']) and
                isinstance(ch.get('uniqueid'), str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', ch['uniqueid']) and
                isinstance(ch.get('state'), str) and re.fullmatch(r'[A-Za-z ]{1,40}', ch['state']),
                'invalid_channel_identity')
    alert = phone.get('alert')
    if alert is not None:
        require(isinstance(alert, dict) and isinstance(alert.get('id'), str) and
                re.fullmatch(r'[0-9a-f]{32}', alert['id']) and alert.get('line') in phone['lines'] and
                alert.get('kind') in ('timer', 'alarm') and alert.get('state') in
                ('starting', 'ringing', 'stopping', 'ended', 'unknown'), 'invalid_alert_inventory')


def valid_room(room):
    return isinstance(room, str) and room == room.strip() and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _'()-]{0,47}", room))


def validate_softphones(data, used_lines):
    softphones = data.get('softphones', [])
    require(isinstance(softphones, list) and len(softphones) <= 50, 'invalid_softphone_inventory')
    seen = set(used_lines)
    for phone in softphones:
        require(isinstance(phone, dict), 'invalid_softphone_inventory')
        line = phone.get('extension')
        require(isinstance(line, str) and re.fullmatch(r'2[0-9]{2}', line) and line not in seen,
                'ambiguous_or_invalid_line_mapping')
        seen.add(line)
        require(phone.get('room') is None or valid_room(phone['room']), 'invalid_phone_room')
        require(type(phone.get('online')) is bool, 'invalid_phone_status')
    return softphones


def validate_services(data, used_lines):
    services = data.get('services', [])
    require(isinstance(services, list) and len(services) <= 128, 'invalid_service_inventory')
    seen = set(used_lines)
    for item in services:
        require(isinstance(item, dict) and set(item) == {'number', 'label', 'kind'},
                'invalid_service_metadata')
        number, label = item['number'], item['label']
        require(isinstance(number, str) and re.fullmatch(r'[0-9]{3,8}', number)
                and number not in seen and number in data['dial_allowlist'],
                'ambiguous_or_unallowed_service_number')
        require(isinstance(label, str) and label == label.strip()
                and re.fullmatch(r'[A-Za-z0-9][ -~]{0,79}', label), 'invalid_service_label')
        require(isinstance(item['kind'], str) and item['kind'] in
                ('diagnostic', 'group', 'assistant', 'alert', 'radio', 'external', 'automation'),
                'invalid_service_kind')
        seen.add(number)
    return services


def commands(phone):
    result = ['status', 'help', 'directory', 'destinations'] + [value for key, value in COMMANDS.items()
              if key in phone['capabilities']]
    if 'answer' in phone['capabilities']:
        result.append('pickup')
    if 'dial' in phone['capabilities']:
        result.append('call NUMBER')
    if 'intercom' in phone['capabilities']:
        result.append('drop-in NUMBER')
    if 'voice' in phone['capabilities']:
        result.extend(VOICE_COMMANDS)
    return result


def status_result(data, phone, line):
    # Deliberately exclude any additional, unexpected server fields.
    filtered = {k: phone[k] for k in ('id', 'lines', 'online', 'dnd', 'dnd_pending', 'autoanswer')}
    filtered['room'] = phone.get('room')
    filtered['channels'] = [{k: c[k] for k in ('line', 'name', 'uniqueid', 'state')}
                            for c in phone['channels']]
    alert = phone.get('alert')
    filtered['alert'] = None if alert is None else {k: alert[k] for k in ('id', 'line', 'kind', 'state')}
    return {'status': 'ok', 'selected_line': line, 'line_button': phone['lines'].index(line) + 1,
            'phone': filtered, 'observed_at': data['observed_at'], 'ami_fresh': True,
            'available_commands': commands(phone), 'dial_allowlist': data['dial_allowlist'],
            'ring_allowlist': data['ring_allowlist']}


def directory_result(data, selected_line=None):
    phones = validate_inventory(data)
    softphones = validate_softphones(data, {line for phone in phones for line in phone['lines']})
    if selected_line is not None:
        require(any(selected_line in p['lines'] for p in phones) or
                any(selected_line == p['extension'] for p in softphones), 'line_not_in_phone_directory')
    entries = []
    for phone in phones:
        entry = {key: phone[key] for key in ('id', 'lines', 'online', 'dnd', 'dnd_pending', 'autoanswer')}
        entry.update(type='cisco', room=phone.get('room'), channel_count=len(phone['channels']),
                     selected=selected_line in phone['lines'])
        entries.append(entry)
    for phone in softphones:
        entries.append({'type': 'softphone', 'id': phone['extension'], 'lines': [phone['extension']],
                        'room': phone.get('room'), 'online': phone['online'],
                        'selected': selected_line == phone['extension']})
    entries.sort(key=lambda phone: int(phone['id']))
    return {'status': 'ok', 'directory': entries, 'selected_line': selected_line,
            'services': sorted(({key: entry[key] for key in ('number', 'label', 'kind')}
                                for entry in data.get('services', [])), key=lambda entry: int(entry['number'])),
            'observed_at': data['observed_at'], 'ami_fresh': True}


def make_action(args, data, phone):
    command, line = args.command, args.extension
    action = {'stop-alert': 'alert_stop', 'pickup': 'answer', 'call': 'dial',
              'drop-in': 'intercom', 'volume': 'hardware_volume'}.get(command, command)
    capability = 'speaker_toggle' if action == 'speaker' else action
    require(capability in phone['capabilities'], 'command_not_supported_by_gateway')
    body = {'line': line}
    if action in ('dial', 'ring', 'intercom'):
        number = args.number
        require(number in data['ring_allowlist' if action == 'ring' else 'dial_allowlist'],
                'destination_not_allowlisted')
        body['destination' if action == 'ring' else 'number'] = number
        if action == 'intercom':
            target = validate_inventory(data, number)
            require(target['id'] != phone['id'], 'intercom_requires_two_phones')
            for item in (phone, target):
                require(item['online'], 'phone_unavailable')
                require(not item['channels'], 'phone_busy')
                require(not item['dnd'], 'do_not_disturb')
                require(not item.get('alert') or item['alert']['state'] == 'ended', 'alert_already_active')
    elif action == 'hardware_volume':
        require(phone['online'], 'phone_unavailable')
        require(not phone.get('alert') or phone['alert']['state'] == 'ended', 'alert_already_active')
        body.update(context=args.context, direction=args.direction, steps=args.steps)
        if args.context == 'ringer':
            require(not phone['channels'], 'ringer_volume_requires_idle_phone')
        else:
            selected = [c for c in phone['channels'] if c['line'] == line and c['state'] == 'Up']
            require(len(selected) == 1 and len(phone['channels']) == 1,
                    'call_volume_requires_one_active_call_on_selected_line')
            body.update(channel=selected[0]['name'], uniqueid=selected[0]['uniqueid'])
    elif action in ('answer', 'hangup'):
        selected = [c for c in phone['channels'] if c['line'] == line]
        if command == 'pickup' and not selected:
            raise Refused('pickup_requires_ringing_call_use_speaker_toggle_explicitly_when_idle')
        require(len(selected) == 1 and len(phone['channels']) == 1,
                'require_one_channel_on_selected_line_and_no_sibling_call')
        if action == 'answer':
            require(selected[0]['state'] == 'Ringing', 'selected_line_not_ringing')
        body.update(channel=selected[0]['name'], uniqueid=selected[0]['uniqueid'])
    elif action in ('dnd', 'autoanswer'):
        body['enabled'] = args.enabled == 'on'
    elif action == 'speaker':
        body['mode'] = 'toggle'
    elif action == 'alert':
        body['kind'] = args.kind
    elif action == 'alert_stop':
        alert = phone.get('alert')
        require(alert is not None and alert['line'] == line, 'no_alert_on_selected_line')
        require(args.alert_id is None or args.alert_id == alert['id'], 'alert_identity_changed')
        body['alert_id'] = alert['id']
    elif action in ('message', 'image'):
        require(1 <= len(args.title) <= 48 and not any(ord(c) < 32 for c in args.title), 'invalid_title')
        body['title'] = args.title
        if action == 'message':
            require((args.text is not None) != (args.text_file is not None), 'choose_text_or_text_file')
            value = args.text if args.text is not None else read_file(args.text_file, 8000).decode('utf-8')
            require(1 <= len(value) <= 2000 and not any(ord(c) < 32 and c not in '\n\t' for c in value),
                    'message_requires_1_to_2000_characters')
            body['text'] = value
        else:
            image = read_file(args.file, MAX_FILE)
            require(image.startswith(b'\x89PNG\r\n\x1a\n') or image.startswith(b'\xff\xd8\xff'),
                    'image_requires_png_or_jpeg')
            body['image_base64'] = base64.b64encode(image).decode('ascii')
    key = args.idempotency_key or 'cli-' + secrets.token_hex(16)
    require(re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', key), 'invalid_idempotency_key')
    body['idempotency_key'] = key
    return action, body


def weekdays(value):
    names = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
    aliases = {'weekdays': list(range(5)), 'weekend': [5, 6], 'daily': list(range(7))}
    if value in aliases:
        return aliases[value]
    values = value.lower().split(',')
    require(values and len(set(values)) == len(values) and all(day in names for day in values),
            'days_require_mon_to_sun_or_weekdays_weekend_daily')
    return sorted(names.index(day) for day in values)


def schedule_request(args, phone, config):
    operation = 'status'
    body = {'entry_id': config['entry_id'], 'line': args.extension}
    if phone is not None:
        body['phone'] = phone['id']
    if args.command == 'timer':
        if args.schedule_action == 'start':
            require(1 <= args.minutes <= 1440, 'timer_minutes_require_1_to_1440')
            operation = 'timer_start'
            body.update(minutes=args.minutes, replace=args.replace)
        elif args.schedule_action == 'cancel':
            operation = 'timer_cancel'
    elif args.command == 'alarm':
        if args.schedule_action == 'set':
            require(re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', args.at), 'alarm_time_requires_HH_MM')
            operation = 'alarm_set'
            body.update(time=args.at, weekdays=weekdays(args.days), enabled=args.enable, replace=args.replace)
        elif args.schedule_action in ('on', 'off'):
            operation = 'alarm_enable' if args.schedule_action == 'on' else 'alarm_disable'
    body['operation'] = operation
    if operation != 'status':
        key = args.idempotency_key or 'cli-' + secrets.token_hex(16)
        require(re.fullmatch(r'[A-Za-z0-9_.:-]{8,80}', key), 'schedule_request_id_requires_8_to_80_characters')
        body['request_id'] = key
    return body


def schedule_result(value, phone, line):
    require(isinstance(value, dict), 'invalid_ha_schedule')
    lines = value.get('lines')
    require(isinstance(lines, list) and 1 <= len(lines) <= 2 and
            all(isinstance(number, str) and re.fullmatch(r'2[0-9]{2}', number) for number in lines) and
            len(set(lines)) == len(lines) and value.get('phone') == lines[0] and line in lines,
            'invalid_ha_line_mapping')
    if phone is None:
        phone = {'id': value['phone'], 'lines': lines}
    else:
        require(phone['lines'] == lines, 'ha_gateway_line_mapping_mismatch')
    require(type(value.get('schema_version')) is int and value['schema_version'] == 1 and
            value.get('phone') == phone['id'] and value.get('line') == line,
            'ha_schedule_identity_or_version_mismatch')
    require(type(value.get('gateway_fresh')) is bool, 'invalid_ha_gateway_freshness')
    timezone = value.get('timezone')
    require(isinstance(timezone, str) and re.fullmatch(r'[A-Za-z0-9_+./-]{1,100}', timezone),
            'invalid_ha_timezone')
    timer, alarm = value.get('timer'), value.get('alarm')
    require(isinstance(timer, dict) and isinstance(alarm, dict), 'invalid_ha_schedule')
    require(timer.get('line') in phone['lines'] and type(timer.get('active')) is bool, 'invalid_ha_timer')
    require(timer.get('id') is None or isinstance(timer['id'], str) and re.fullmatch(r'[0-9a-f]{32}', timer['id']),
            'invalid_ha_timer_id')
    deadline, remaining = timer.get('deadline'), timer.get('remaining_seconds')
    require(deadline is None or type(deadline) in (float, int) and 0 <= deadline <= sys.float_info.max,
            'invalid_ha_timer_deadline')
    require(remaining is None or type(remaining) is int and remaining >= 0, 'invalid_ha_timer_remaining')
    require(alarm.get('line') in phone['lines'] and type(alarm.get('enabled')) is bool and
            type(alarm.get('configured')) is bool and
            isinstance(alarm.get('time'), str) and re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', alarm['time']) and
            isinstance(alarm.get('weekdays'), list) and all(type(day) is int and 0 <= day <= 6 for day in alarm['weekdays']),
            'invalid_ha_alarm')
    alert = value.get('alert')
    if alert is not None:
        require(isinstance(alert, dict) and isinstance(alert.get('id'), str) and
                re.fullmatch(r'[0-9a-f]{32}', alert['id']) and alert.get('line') in phone['lines'] and
                alert.get('kind') in ('timer', 'alarm') and alert.get('state') in
                ('starting', 'ringing', 'stopping', 'ended', 'unknown'), 'invalid_ha_alert')
        alert = {key: alert[key] for key in ('id', 'line', 'kind', 'state')}
    operation = value.get('operation_result')
    require(operation in ('applied', 'replayed', 'outcome_unknown', 'not_active', 'status'),
            'invalid_ha_schedule_result')
    return {'schema_version': 1, 'phone': phone['id'], 'lines': lines, 'line': line, 'timezone': timezone,
            'gateway_fresh': value['gateway_fresh'],
            'timer': {key: timer[key] for key in ('line', 'id', 'active', 'deadline', 'remaining_seconds')},
            'alarm': {key: alarm[key] for key in ('line', 'time', 'weekdays', 'enabled', 'configured')},
            'alert': alert, 'operation_result': operation}


def voice_status(value):
    """Validate the public voice schema and discard all untrusted extra fields."""
    states = {'idle', 'starting', 'running', 'stopping', 'stopped', 'cancelled',
              'failed', 'recovery_required', 'external_owner'}
    require(isinstance(value, dict) and type(value.get('schema_version')) is int
            and value['schema_version'] == 1 and isinstance(value.get('state'), str)
            and value['state'] in states,
            'invalid_voice_response')
    require(all(type(value.get(key)) is bool
                for key in ('active', 'stop_requested', 'stop_confirmed')), 'invalid_voice_response')
    require(value['active'] == (value['state'] in ('starting', 'running', 'stopping')),
            'invalid_voice_response')
    require(all(isinstance(value.get(key), str) and re.fullmatch(r'2[0-9]{2}', value[key])
                for key in ('phone', 'line')), 'invalid_voice_response')
    session = value.get('session_id')
    require((session is None and value['state'] == 'idle')
            or (isinstance(session, str) and re.fullmatch(r'[0-9a-f]{32}', session)
                and value['state'] != 'idle'),
            'invalid_voice_response')
    remaining = value.get('remaining_seconds')
    # Bound integers before float conversion inside isfinite: a hostile large
    # JSON integer must produce the same safe refusal as any malformed status.
    require(type(remaining) in (int, float) and 0 <= remaining <= 300 and math.isfinite(remaining),
            'invalid_voice_response')
    result = {key: value[key] for key in ('schema_version', 'phone', 'line', 'session_id', 'state',
              'active', 'remaining_seconds', 'stop_requested', 'stop_confirmed')}
    if isinstance(value.get('error'), str) and value['error'] in VOICE_PUBLIC_ERRORS:
        result['error'] = value['error']
    for field in ('phase', 'phase_reason', 'turns_completed', 'mode', 'session_seconds'):
        if field not in value:
            continue
        item = value[field]
        if field == 'phase':
            valid = item is None or isinstance(item, str) and item in VOICE_PHASES
        elif field == 'phase_reason':
            item = item if isinstance(item, str) and item in VOICE_PHASE_REASONS else None
            valid = True
        elif field == 'turns_completed':
            valid = type(item) is int and 0 <= item <= 10000
        elif field == 'mode':
            valid = isinstance(item, str) and item in ('manual', 'wake')
        else:
            valid = type(item) in (int, float) and 0 < item <= 300 and math.isfinite(item)
        require(valid, 'invalid_voice_response')
        result[field] = item
    if 'playback' in value:
        limits = {'playback_requests': 10000, 'tts_offers': 10000,
                  'empty_playbacks': 10000, 'incomplete_playbacks': 10000,
                  'completed_pcm_bytes': 64 * 1024 * 1024}
        playback = value['playback']
        require(type(playback) is dict and set(playback) == set(limits), 'invalid_voice_response')
        require(all(type(playback[key]) is int and 0 <= playback[key] <= maximum
                    for key, maximum in limits.items()), 'invalid_voice_response')
        result['playback'] = {key: playback[key] for key in limits}
    return result


def hands_free_status(value):
    """Validate supervisor intent separately from the finite session schema."""
    require(type(value) is dict and type(value.get('schema_version')) is int
            and value['schema_version'] == 1, 'invalid_hands_free_response')
    require(all(isinstance(value.get(key), str) and re.fullmatch(r'2[0-9]{2}', value[key])
                for key in ('phone', 'line')),
            'invalid_hands_free_response')
    require(type(value.get('enabled')) is bool and isinstance(value.get('state'), str)
            and value['state'] in HANDS_FREE_STATES, 'invalid_hands_free_response')
    session = value.get('session_id')
    require('session_id' in value and (session is None or isinstance(session, str)
            and re.fullmatch(r'[0-9a-f]{32}', session)), 'invalid_hands_free_response')
    duration, failures, retry = (value.get(key) for key in
                                ('session_seconds', 'consecutive_failures', 'retry_after_seconds'))
    require(type(duration) in (int, float) and 0 < duration <= 300 and math.isfinite(duration)
            and type(failures) is int and 0 <= failures <= 3
            and type(retry) in (int, float) and 0 <= retry <= 10 and math.isfinite(retry),
            'invalid_hands_free_response')
    require(value['state'] == 'backoff' or retry == 0, 'invalid_hands_free_response')
    require('error' in value and (value['error'] is None or isinstance(value['error'], str)
            and value['error'] in HANDS_FREE_ERRORS), 'invalid_hands_free_response')
    return {key: value[key] for key in ('schema_version', 'phone', 'line', 'enabled', 'state',
            'session_id', 'session_seconds', 'consecutive_failures', 'retry_after_seconds', 'error')}


def voice_wake_ready(value):
    return (value.get('state') == 'running' and value.get('active') is True
            and value.get('stop_requested') is False and value.get('stop_confirmed') is False
            and value.get('error') is None and value.get('remaining_seconds', 0) > 0
            and value.get('phase') == 'armed' and value.get('phase_reason') == 'wake_ready')


def hands_free_command(args, client, path, *, on_dispatch=None):
    operation = args.voice_action
    require(not (operation == 'disable' and args.dry_run), 'hands_free_disable_dry_run_unsupported')
    key = args.idempotency_key or 'cli-' + secrets.token_hex(16)
    require(isinstance(key, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', key),
            'voice_request_id_required')
    body = {'idempotency_key': key}
    if operation == 'enable':
        body.update(enable_microphone=True, exclusive_media=True, dry_run=args.dry_run)
    if not args.dry_run and on_dispatch is not None:
        client.on_dispatch = lambda: on_dispatch(key)
    response = client.request(path + 'hands_free_' + operation, body)
    require(type(response) is dict and response.get('status') == 'accepted'
            and response.get('action_id') == key, 'unconfirmed_gateway_response')
    value = response.get('hands_free')
    if args.dry_run:
        require(type(value) is dict and type(value.get('schema_version')) is int
                and value['schema_version'] == 1 and value.get('valid') is True
                and value.get('line') == args.extension
                and isinstance(value.get('phone'), str) and re.fullmatch(r'2[0-9]{2}', value['phone'])
                and value.get('microphone_started') is False
                and type(value.get('network_actions')) is int and value['network_actions'] == 0
                and type(value.get('duration_seconds')) in (int, float)
                and 30 <= value['duration_seconds'] <= 300 and math.isfinite(value['duration_seconds']),
                'invalid_hands_free_response')
        return {'status': 'dry_run', 'action': 'voice_enable', 'action_id': key,
                'selected_line': args.extension, 'microphone_started': False}
    return {'status': 'accepted', 'action': 'voice_' + operation, 'action_id': key,
            'hands_free': hands_free_status(value), 'selected_line': args.extension}


def voice_recovery_command(args, client, path, *, on_dispatch=None):
    """Separate administrative receipts from ordinary Voice/HA status schemas."""
    operation = args.voice_action
    require(not (operation == 'recover' and args.dry_run), 'voice_recover_dry_run_unsupported')
    key = None
    if operation == 'recover':
        require(isinstance(args.session_id, str) and re.fullmatch(r'[0-9a-f]{32}', args.session_id)
                and isinstance(args.journal_sha256, str)
                and re.fullmatch(r'[0-9a-f]{64}', args.journal_sha256), 'voice_recovery_request_invalid')
        key = args.idempotency_key or 'cli-' + secrets.token_hex(16)
        require(isinstance(key, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', key),
                'voice_request_id_required')
        if on_dispatch is not None:
            client.on_dispatch = lambda: on_dispatch(key)
        response = client.request(path + 'recover', {
            'idempotency_key': key, 'session_id': args.session_id, 'journal_sha256': args.journal_sha256})
        require(type(response) is dict and response.get('status') == 'accepted'
                and response.get('action_id') == key, 'unconfirmed_gateway_response')
    else:
        response = client.request(path + 'recovery_status')
        require(type(response) is dict and response.get('status') == 'ok', 'invalid_voice_recovery_response')
    value = response.get('recovery')
    require(type(value) is dict and type(value.get('schema_version')) is int
            and value['schema_version'] == 1 and value.get('line') == args.extension
            and isinstance(value.get('phone'), str) and re.fullmatch(r'2[0-9]{2}', value['phone']),
            'invalid_voice_recovery_response')
    result = {name: value[name] for name in ('schema_version', 'phone', 'line')}
    for name, length in (('session_id', 32), ('journal_sha256', 64)):
        token = value.get(name)
        require((operation != 'recover' and token is None)
                or isinstance(token, str) and re.fullmatch('[0-9a-f]{' + str(length) + '}', token),
                'invalid_voice_recovery_response')
        result[name] = token
    if operation == 'recover':
        require(value.get('status') == 'ORPHAN_RECOVERY_COMPLETE' and value.get('state') == 'recovered'
                and value['session_id'] == args.session_id
                and value.get('prior_journal_sha256') == args.journal_sha256
                and value.get('stop_confirmed') is False
                and value.get('historical_stop_confirmed') is False
                and value.get('enabled') is False
                and value.get('proof_kind') == 'phone_reported_transport_quiescence',
                'invalid_voice_recovery_response')
        result.update(status=value['status'], state='recovered', prior_journal_sha256=args.journal_sha256,
                      stop_confirmed=False, historical_stop_confirmed=False, enabled=False,
                      proof_kind=value['proof_kind'])
    else:
        remaining = value.get('minimum_age_remaining_seconds')
        require(type(value.get('eligible')) is bool and value.get('quiescence_verified') is False
                and type(remaining) in (int, float) and 0 <= remaining <= 3600 and math.isfinite(remaining)
                and (value.get('reason') is None or value.get('reason') in ERROR_REASONS),
                'invalid_voice_recovery_response')
        result.update(eligible=value['eligible'], quiescence_verified=False, reason=value.get('reason'),
                      minimum_age_remaining_seconds=remaining)
    return {'status': response['status'], 'action': 'voice_' + operation.replace('-', '_'),
            'selected_line': args.extension, 'recovery': result, **({'action_id': key} if key else {})}


def voice_command(args, config, *, on_dispatch=None):
    operation = args.voice_action
    client = Gateway(config, max(args.timeout, 45) if operation == 'recover' else args.timeout)
    path = '/v1/phones/' + args.extension + '/voice_'
    if operation in ('recovery-status', 'recover'):
        return voice_recovery_command(args, client, path, on_dispatch=on_dispatch)
    if operation in ('enable', 'disable'):
        return hands_free_command(args, client, path, on_dispatch=on_dispatch)
    key = None
    session_id = getattr(args, 'session_id', None)
    response = None
    if operation == 'stop':
        if session_id is None:
            response = client.request(path + 'status')
            require(isinstance(response, dict) and response.get('status') == 'ok', 'invalid_voice_response')
            current = voice_status(response.get('voice'))
            if (current['state'] == 'external_owner'
                    or current.get('error') == 'voice_owner_unavailable'):
                return {'status': 'unavailable', 'action': 'voice_stop', 'voice': current,
                        'selected_line': args.extension, 'session_id': current['session_id'],
                        'gateway_stop_available': False, 'reason': 'voice_owner_unavailable',
                        'note': VOICE_EXTERNAL_OWNER_NOTE}
            session_id = current['session_id']
        require(isinstance(session_id, str) and re.fullmatch(r'[0-9a-f]{32}', session_id),
                'unknown_voice_session')
    read_only = operation == 'status' or operation == 'stop' and args.dry_run
    if read_only:
        if response is None:
            query = '?include_playback=1' if getattr(args, 'playback', False) else ''
            try:
                response = client.request(path + 'status' + query)
            except Refused as exc:
                if (query and exc.http_status == 404
                        and exc.reason in ('not_found', 'gateway_http_error')):
                    raise Refused('playback_metadata_unavailable', http_status=404) from None
                raise
        require(isinstance(response, dict) and response.get('status') == 'ok', 'invalid_voice_response')
        if getattr(args, 'playback', False):
            require(isinstance(response.get('voice'), dict), 'invalid_voice_response')
            require('playback' in response['voice'], 'playback_metadata_unavailable')
    else:
        key = args.idempotency_key
        if operation == 'stop' and key is None:
            key = 'voice-stop-' + session_id
        require(isinstance(key, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', key),
                'voice_request_id_required')
        body = {'idempotency_key': key}
        if operation == 'start':
            require(args.attended and args.exclusive_media and args.enable_microphone,
                    'explicit_voice_start_required')
            body.update(attended=True, exclusive_media=True, enable_microphone=True, dry_run=args.dry_run)
        else:
            body['session_id'] = session_id
        if not args.dry_run and on_dispatch is not None:
            client.on_dispatch = lambda: on_dispatch(key)
        response = client.request(path + operation, body)
        require(isinstance(response, dict) and response.get('status') == 'accepted'
                and response.get('action_id') == key, 'unconfirmed_gateway_response')
    value = response.get('voice')
    require(isinstance(value, dict), 'invalid_voice_response')
    if operation == 'start' and args.dry_run:
        duration = value.get('duration_seconds')
        require(type(value.get('schema_version')) is int and value['schema_version'] == 1
                and value.get('valid') is True and value.get('microphone_started') is False
                and type(value.get('network_actions')) is int and value['network_actions'] == 0
                and type(duration) in (int, float) and 0 < duration <= 300 and math.isfinite(duration)
                and isinstance(value.get('phone'), str) and re.fullmatch(r'2[0-9]{2}', value['phone'])
                and value.get('line') == args.extension, 'invalid_voice_response')
        return {'status': 'dry_run', 'action': 'voice_start', 'action_id': key,
                'selected_line': args.extension, 'microphone_started': False}
    require('valid' not in value, 'invalid_voice_response')
    raw_voice = value
    value = voice_status(value)
    # Use only validated public fields; a foreign journal is not a local child
    # that this gateway can signal. Recovery alone does not imply foreign ownership.
    external_owner = (value['state'] == 'external_owner'
                      or value.get('error') == 'voice_owner_unavailable')
    guidance = ({'gateway_stop_available': False, 'reason': 'voice_owner_unavailable',
                 'note': VOICE_EXTERNAL_OWNER_NOTE} if external_owner else {})
    if operation == 'stop':
        require(value['session_id'] == session_id, 'voice_session_changed')
        if args.dry_run:
            if external_owner:
                return {'status': 'unavailable', 'action': 'voice_stop',
                        'session_id': session_id, 'selected_line': args.extension,
                        'voice': value, **guidance}
            return {'status': 'dry_run', 'action': 'voice_stop',
                    'session_id': session_id, 'selected_line': args.extension}
    elif operation == 'start':
        require(value['session_id'] is not None, 'invalid_voice_response')
    result = {'status': 'ok' if operation == 'status' else 'accepted', 'voice': value,
              'voice_wake_ready': 'error' in raw_voice and voice_wake_ready(raw_voice)}
    if operation == 'status':
        result.update(guidance)
        try:
            response = client.request(path + 'hands_free_status')
        except Refused as exc:
            legacy = (exc.http_status == 404 and exc.reason in
                      ('gateway_http_error', 'unknown_action', 'not_found'))
            result.update(hands_free=None, hands_free_available=False,
                          hands_free_error='hands_free_unavailable' if legacy else exc.reason,
                          hands_free_status_failed=not legacy, hands_free_listening=False)
        else:
            require(type(response) is dict and response.get('status') == 'ok',
                    'invalid_hands_free_response')
            intent = hands_free_status(response.get('hands_free'))
            require(intent['phone'] == value['phone'], 'invalid_hands_free_response')
            listening = (intent['enabled'] and intent['state'] == 'running'
                         and intent['error'] is None and intent['session_id'] is not None
                         and intent['session_id'] == value['session_id']
                         and 'error' in raw_voice
                         and voice_wake_ready(raw_voice))
            result.update(hands_free=intent, hands_free_available=True,
                          hands_free_listening=listening)
    if key is not None:
        result['action_id'] = key
    return result


def voice_readiness(value, *, wake_ready=False):
    if value['stop_confirmed']:
        return 'Session ended; cleanup confirmed.'
    if value['stop_requested'] or value['state'] == 'stopping':
        return 'Stop requested; waiting for cleanup confirmation.'
    if not value['active']:
        return ('No active session.' if value['state'] == 'idle'
                else 'Session is not running; cleanup is unconfirmed.')
    return {
        'preparing': 'Preparing; wait before speaking.',
        'armed': ('Wake listener ready; say the wake phrase.' if wake_ready
                  else 'Wake readiness not confirmed; inspect the matching session status.'),
        'listening': 'Listening; speak now.',
        'thinking': 'Processing your request; please wait.',
        'speaking': 'Playing the reply.',
        'stopped': 'Session ended; waiting for cleanup confirmation.',
        'error': 'Voice processing failed; check Stop confirmation.',
    }.get(value.get('phase'), 'Readiness not reported; use the live assistant screen.')


def display_hands_free(result, args):
    value = result.get('hands_free')
    if value is None:
        print('Hands-free: unavailable on this gateway; desired state is unknown.')
        if result.get('hands_free_status_failed'):
            print('Hands-free status error: ' + result['hands_free_error'])
        return
    print('Hands-free phone: ' + value['phone'] + '; enabled line: ' + value['line'])
    print('Hands-free desired: ' + ('enabled' if value['enabled'] else 'disabled'))
    print('Hands-free state: ' + value['state'])
    guidance = {
        'disabled': 'Automatic listening disabled; check finite session status for cleanup.',
        'waiting_idle': 'Waiting for verified idle lines and the quiet period; not listening yet.',
        'starting': 'Preparing a session; wake readiness is not confirmed.',
        'running': 'Session running; wake readiness is not confirmed.',
        'stopping': 'Waiting for owned session cleanup; not confirmed stopped yet.',
        'backoff': 'Automatic retry pending; not listening.',
        'faulted': 'Failed; resolve the reported problem before explicitly enabling again.',
    }[value['state']]
    if result.get('hands_free_listening') is True:
        guidance = 'Wake listener ready; say the wake phrase.'
    print('Hands-free readiness: ' + guidance)
    print('Inner session limit: ' + str(value['session_seconds']) + ' seconds')
    print('Consecutive failures: ' + str(value['consecutive_failures']))
    if value['state'] == 'backoff':
        print('Retry after: ' + str(value['retry_after_seconds']) + ' seconds')
    if value['error']:
        print('Hands-free reason: ' + value['error'])
    if 'action_id' in result:
        print('Action: ' + result['action_id'])
    if value['enabled']:
        print('Disable automatic listening: ' + shlex.join(
            ['cisco', '--config', args.config, '--timeout', str(args.timeout),
             args.extension, 'voice', 'disable']))


def display_voice(result, args):
    if 'hands_free' in result:
        display_hands_free(result, args)
    if 'voice' not in result:
        return
    value = result['voice']
    print('Voice: ' + value['state'])
    print('Phone: ' + value['phone'] + '; session line: ' + value['line'])
    print('Session: ' + (value['session_id'] or 'none'))
    print('Remaining: ' + str(value['remaining_seconds']) + ' seconds')
    if (result.get('hands_free') or {}).get('enabled'):
        print('Finite session phase: ' + str(value.get('phase') or 'unreported'))
    else:
        print('Readiness: ' + voice_readiness(value, wake_ready=result.get('voice_wake_ready', False)))
    if 'mode' in value:
        print('Mode: ' + value['mode'])
    if 'session_seconds' in value:
        print('Session limit: ' + str(value['session_seconds']) + ' seconds')
    if 'turns_completed' in value:
        print('Turns completed: ' + str(value['turns_completed']))
    if 'playback' in value:
        for key, label in (('playback_requests', 'Replies reaching playback'),
                           ('tts_offers', 'Replies offering TTS'),
                           ('empty_playbacks', 'Playbacks with no PCM'),
                           ('incomplete_playbacks', 'Incomplete or unverified playbacks'),
                           ('completed_pcm_bytes', 'PCM bytes in completed playbacks')):
            print(label + ': ' + str(value['playback'][key]))
        print('PCM completion does not confirm audibility; incomplete playback may be partial.')
    print('Stop requested: ' + str(value['stop_requested']))
    print('Stop confirmed: ' + str(value['stop_confirmed']))
    for field, label in (('error', 'Reason'), ('phase_reason', 'Phase reason')):
        if value.get(field):
            print(label + ': ' + value[field])
    if 'action_id' in result:
        print('Action: ' + result['action_id'])
    if result.get('gateway_stop_available') is False:
        print('Gateway Stop available: False')
        print(result['note'])
    elif value['session_id'] and not value['stop_confirmed']:
        prefix = ['cisco', '--config', args.config, '--timeout', str(args.timeout), args.extension, 'voice']
        print('Stop this session: ' + shlex.join([*prefix, 'stop', '--session-id', value['session_id']]))
        print('Refresh status: ' + shlex.join([*prefix, 'status']))


def global_options(p, *, inherited=False):
    def default(value):
        return argparse.SUPPRESS if inherited else value
    p.add_argument('-c', '--config', default=default(os.environ.get('CISCO_CONFIG', '~/.config/cisco-home/cli.json')))
    p.add_argument('-j', '--json', action='store_true', default=default(False), help='machine-readable JSON only')
    p.add_argument('-t', '--timeout', type=int, default=default(20), help='HTTP timeout seconds, 1..60 (default 20)')
    p.add_argument('--idempotency-key', default=default(None), help='reuse only for the same logical operation and exact request')
    p.add_argument('--dry-run', action='store_true', default=default(False), help='validate without changing the phone; voice Start asks the server for dry validation')


def normalize_shorthand(argv):
    """Insert `dial` for a numeric second positional token, preserving options."""
    takes_value = {'-c', '--config', '-t', '--timeout', '--idempotency-key'}
    flags = {'-j', '--json', '--dry-run'}
    positions, index = [], 0
    while index < len(argv):
        token = argv[index]
        if token in takes_value:
            index += 2
            continue
        if token in flags or any(token.startswith(flag + '=') for flag in takes_value if flag.startswith('--')):
            index += 1
            continue
        if token.startswith('-'):
            break  # Leave unknown options and `--` to argparse.
        positions.append(index)
        if len(positions) == 2:
            if re.fullmatch(r'[0-9]+', token):
                return [*argv[:index], 'dial', *argv[index:]]
            break
        index += 1
    return argv


def parser():
    p = argparse.ArgumentParser(description=__doc__, prog='cisco', epilog=
        'Examples: cisco directory; cisco 213; cisco 213 600. Calls use the selected line.')
    global_options(p)
    p.add_argument('extension', help='exact configured Cisco line, or directory for the whole phone registry')
    sub = p.add_subparsers(dest='command')
    descriptions = {
        'status': 'Read current phone status and optional HA schedules.',
        'help': 'Show local commands without accessing private configuration or the network.',
        'destinations': 'Read allowed dial and finite-ring destinations.',
        'directory': 'List Cisco phones, softphones and configured services, highlighting this selected line.',
        'answer': 'Answer one exact ringing call on this line; never answer a timer/alarm alert.',
        'pickup': 'Alias for Answer of one ringing call. Idle use requires explicit speaker toggle.',
        'hangup': 'Hang up one exact observed call; refuse multiple channels or a sibling call.',
    }
    for name in ('status', 'help', 'directory', 'destinations', 'answer', 'pickup', 'hangup'):
        sub.add_parser(name, help=descriptions[name], description=descriptions[name])
    sub.add_parser('dial', aliases=['call'], help='Call a number; the destination rings normally.').add_argument('number')
    sub.add_parser('intercom', aliases=['drop-in'], help='Connect two idle Cisco phones with automatic speaker answer.',
                   description='One-time speaker answer on both phones. Requires two distinct, idle Cisco phones; '
                   'respects DND and does not change persistent auto-answer.').add_argument('number')
    sub.add_parser('ring').add_argument('number', nargs='?', default='640')
    sub.add_parser('speaker', description='Explicit idle speaker-key toggle for the whole phone. '
                   'Final speaker state is unknown; this is not an idempotent on/off command.').add_argument('mode', choices=['toggle'])
    volume = sub.add_parser('volume', help='Step physical ringer or current call volume.',
        description='Send hardware volume-key steps. Ringer requires an idle, physically on-hook phone; '
                    'call requires one active call on the selected line. The phone chooses the current '
                    'handset, headset or speaker output. Absolute level and local audio path are unknown. '
                    'This is separate from digital music gain. No automatic output switching or retry.')
    volume.add_argument('context', choices=['ringer', 'call'])
    volume.add_argument('direction', choices=['up', 'down'])
    volume.add_argument('--steps', type=int, choices=range(1, 6), default=1,
                        help='Number of key presses (default: 1; maximum: 5)')
    for name in ('dnd', 'autoanswer'):
        description = ('Set or read whole-phone Do Not Disturb.' if name == 'dnd' else
                       'Set or read whole-phone gateway auto-answer. Timer/alarm alerts always remain manual.')
        sub.add_parser(name, description=description).add_argument('enabled', nargs='?', choices=['on', 'off'])
    msg = sub.add_parser('message'); msg.add_argument('text', nargs='?')
    msg.add_argument('--text-file'); msg.add_argument('--title', default='Notice')
    img = sub.add_parser('image'); img.add_argument('file'); img.add_argument('--title', default='Notice')
    sub.add_parser('alert').add_argument('kind', choices=['timer', 'alarm'])
    sub.add_parser('stop-alert').add_argument('--alert-id')
    sub.add_parser('schedule')
    timer_sub = sub.add_parser('timer').add_subparsers(dest='schedule_action')
    timer_sub.add_parser('status')
    start = timer_sub.add_parser('start')
    start.add_argument('minutes', type=int)
    start.add_argument('--replace', action='store_true')
    timer_sub.add_parser('cancel')
    alarm_sub = sub.add_parser('alarm').add_subparsers(dest='schedule_action')
    for action in ('status', 'on', 'off'):
        alarm_sub.add_parser(action)
    alarm = alarm_sub.add_parser('set')
    alarm.add_argument('at', metavar='HH:MM')
    alarm.add_argument('--days', required=True, help='mon,tue,... or weekdays, weekend, daily')
    alarm.add_argument('--enable', action='store_true', help='enable this alarm; otherwise save it disabled')
    alarm.add_argument('--replace', action='store_true', help='explicitly replace an alarm owned by the sibling line')
    voice_sub = sub.add_parser('voice', help='Finite sessions and opt-in automatic hands-free controls').add_subparsers(
        dest='voice_action', required=True)
    voice_sub.add_parser('status', help='Read voice ownership without an AMI inventory request').add_argument(
        '--playback', action='store_true', help='Request optional private PCM completion counters')
    voice_start = voice_sub.add_parser('start', help='Requires --idempotency-key and all explicit microphone gates')
    for flag in ('attended', 'exclusive-media', 'enable-microphone'):
        voice_start.add_argument('--' + flag, action='store_true')
    voice_sub.add_parser('stop', help='Stop the current session, or an exact supplied session ID').add_argument('--session-id')
    voice_sub.add_parser('enable', help='Authorize automatic microphone sessions; optional --idempotency-key and --dry-run')
    voice_sub.add_parser('disable', help='Disable automatic sessions durably and request owned-session cleanup')
    voice_sub.add_parser('recovery-status', help='Read exact orphan identity and local gates; does not probe the phone')
    recover = voice_sub.add_parser('recover', help='Administratively release an exact orphan after a bounded quiescence proof')
    recover.add_argument('--session-id', required=True)
    recover.add_argument('--journal-sha256', required=True)
    for command_parser in dict.fromkeys(sub.choices.values()):
        global_options(command_parser, inherited=True)
    for command_parser in (*timer_sub.choices.values(), *alarm_sub.choices.values(), *voice_sub.choices.values()):
        global_options(command_parser, inherited=True)
    return p


def display_schedule(schedule):
    timer, alarm = schedule['timer'], schedule['alarm']
    print(f"Timezone: {schedule['timezone']}; schedule result: {schedule['operation_result']}")
    if not schedule['gateway_fresh']:
        print('Gateway state is unavailable; the active ringing state is unknown.')
    print(f"Timer: line {timer['line']}, active {timer['active']}, remaining {timer['remaining_seconds']} seconds")
    day_names = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
    days = ', '.join(day_names[day] for day in alarm['weekdays']) or 'no days'
    print(f"Alarm: line {alarm['line']}, {alarm['time']} on {days}, enabled {alarm['enabled']}, configured {alarm['configured']}")
    if schedule['alert']:
        alert = schedule['alert']
        print(f"Alert: {alert['kind']} on {alert['line']}, {alert['state']}, id {alert['id']}")
    print('One timer and one weekly alarm slot are shared by this physical phone.')


def display(result):
    if 'directory' in result:
        print('Phone directory:')
        for phone in result['directory']:
            marker = '*' if phone['selected'] else ' '
            room = phone['room'] if phone['room'] is not None else 'Unassigned'
            text = f"{marker} {' / '.join(phone['lines'])}: {room} — online {phone['online']}"
            if phone['type'] == 'cisco':
                text += f"; DND {phone['dnd']}; auto-answer {phone['autoanswer']}; channels {phone['channel_count']}"
            else:
                text += '; softphone (directory only; no Cisco controls)'
            print(text)
        if result['selected_line'] is not None:
            print(f"* Hosting phone for selected line {result['selected_line']}.")
        print('Online means all assigned lines are available in the fresh gateway snapshot.')
        if result.get('services'):
            print('\nServices and shortcuts:')
            for item in result['services']:
                print(f"  {item['number']}: {item['label']} ({item['kind']})")
            print('Configured destinations only; service availability and audio are not monitored here.')
        return
    if 'schedule' in result and 'phone_id' in result:
        print(f"Line {result['selected_line']} / phone {result['phone_id']} — {result['status']}")
        if 'gateway_error' in result:
            print('Live gateway status unavailable: ' + result['gateway_error']['reason'])
        display_schedule(result['schedule'])
        return
    if result['status'] not in ('ok', 'partial'):
        print(json.dumps(result, indent=2))
        return
    phone, line = result['phone'], result['selected_line']
    print(f"Line {line} / button {result['line_button']} — phone {phone['id']} ({', '.join(phone['lines'])})")
    print('Room: ' + (phone.get('room') or 'Unassigned'))
    print(f"Phone online (all assigned lines): {phone['online']}; DND: {phone['dnd']}"
          f"; pending DND restore: {phone['dnd_pending']}; auto-answer: {phone['autoanswer']}")
    for ch in phone['channels']:
        print(f"Call on {ch['line']}: {ch['state']} — {ch['name']} / {ch['uniqueid']}")
    if not phone['channels']:
        print('No active phone channels observed.')
    if phone['alert']:
        a = phone['alert']; print(f"Alert: {a['kind']} on {a['line']}, {a['state']}, id {a['id']}")
    if 'schedule' in result:
        display_schedule(result['schedule'])
    elif 'schedule_error' in result:
        print('Schedule status unavailable: ' + result['schedule_error']['reason'] + ' (partial status).')
    print('Commands:')
    for command in result['available_commands']:
        print(f'  cisco {line} {command}')
    print('Allowed dial numbers: ' + ', '.join(result['dial_allowlist']))
    print('Examples (these change the phone only when you run them):')
    destinations = [n for n in result['dial_allowlist'] if n.startswith('2') and n not in phone['lines']]
    if destinations and 'dial NUMBER' in result['available_commands']:
        target = '220' if '220' in destinations else destinations[0]
        print(f'  cisco {line} {target}')
        if 'intercom NUMBER' in result['available_commands']:
            print(f'  cisco {line} intercom {target}  # Cisco phones only; answers both on speaker')
    if '600' in result['dial_allowlist'] and 'dial NUMBER' in result['available_commands']:
        print(f'  cisco {line} dial 600')
    if 'message TEXT [--title TITLE]' in result['available_commands']:
        print(f'  cisco {line} message "Dinner is ready"')
    if 'dnd [on|off]' in result['available_commands']:
        print(f'  cisco {line} dnd on')
        print(f'  cisco {line} dnd off')
    if 'schedule' in result['available_commands']:
        print(f'  cisco {line} timer start 5')
        print(f'  cisco {line} alarm set 07:30 --days weekdays --enable')
    if 'volume ringer|call up|down [--steps 1..5]' in result['available_commands']:
        print(f'  cisco {line} volume ringer up  # Idle and physically on-hook only')
        print(f'  cisco {line} volume call down --steps 2  # Current output; active call on this line')
    print('Calls use the selected line; DND, auto-answer, speaker, hardware volume and screen content affect its whole phone.')


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    p = parser()
    if not argv or argv in (['--json'], ['-j']):
        if argv:
            print(json.dumps({'status': 'help', 'commands': ['status', 'directory', 'destinations', 'pickup', *COMMANDS.values(), *SCHEDULE_COMMANDS, *VOICE_COMMANDS]}))
        else:
            p.print_help()
        return 0
    args = p.parse_args(normalize_shorthand(argv))
    if args.command == 'help':
        if args.json:
            print(json.dumps({'status': 'help', 'commands': ['status', 'directory', 'destinations', 'pickup', *COMMANDS.values(), *SCHEDULE_COMMANDS, *VOICE_COMMANDS]}))
        else:
            p.print_help()
            print('\nDevice commands: ' + '; '.join(COMMANDS.values()))
            print('pickup: answer one exact ringing call; idle speaker use requires explicit speaker toggle.')
            print('Schedules (optional Home Assistant config): ' + '; '.join(SCHEDULE_COMMANDS))
            print('Voice (requires configured gateway supervisor): ' + '; '.join(VOICE_COMMANDS))
        return 0
    action_key = None
    exit_code = 0

    def remember_voice_dispatch(key):
        nonlocal action_key
        action_key = key

    try:
        if args.extension == 'directory':
            require(args.command is None, 'global_directory_takes_no_subcommand')
            args.extension, args.command = None, 'directory'
        else:
            require(re.fullmatch(r'2[0-9]{2}', args.extension), 'extension_requires_three_digit_cisco_line')
        require(1 <= args.timeout <= 60, 'timeout_requires_1_to_60_seconds')
        config = load_config(args.config)
        if args.command == 'voice':
            result = voice_command(args, config, on_dispatch=remember_voice_dispatch)
            if args.json or not ('voice' in result or 'hands_free' in result):
                print(json.dumps(result, sort_keys=True))
            else:
                display_voice(result, args)
            return int(result.get('gateway_stop_available') is False
                       or result.get('hands_free_status_failed') is True
                       or (result.get('hands_free') or {}).get('state') in {'faulted', 'backoff'}
                       or result.get('voice', {}).get('state') in {
                'failed', 'recovery_required', 'external_owner'})
        phone = None
        if args.command not in ('schedule', 'timer', 'alarm'):
            try:
                gateway = Gateway(config, args.timeout)
                data = gateway.request('/v1/phones')
                if args.command != 'directory':
                    phone = validate_inventory(data, args.extension)
            except (Refused, OSError, ValueError, UnicodeError, TypeError, KeyError, AttributeError, RecursionError) as exc:
                if args.command not in (None, 'status') or 'home_assistant' not in config:
                    raise
                ha_config = config['home_assistant']
                value = Gateway(ha_config, args.timeout).request(SCHEDULE_PATH, {
                    'entry_id': ha_config['entry_id'], 'line': args.extension, 'operation': 'status'})
                value = schedule_result(value, None, args.extension)
                result = {'status': 'partial', 'selected_line': args.extension, 'phone_id': value['phone'],
                          'schedule': value, 'gateway_error': {'reason': exc.reason if isinstance(exc, Refused)
                                                             else 'gateway_read_failed'},
                          'available_commands': ['status', 'help', *SCHEDULE_COMMANDS]}
                if args.json:
                    print(json.dumps(result))
                else:
                    display(result)
                    print('Schedule commands: ' + '; '.join(SCHEDULE_COMMANDS))
                return 1
        if args.command == 'directory':
            result = directory_result(data, args.extension)
        elif args.command in ('schedule', 'timer', 'alarm'):
            require('home_assistant' in config, 'home_assistant_schedule_config_required')
            ha_config = config['home_assistant']
            body = schedule_request(args, phone, ha_config)
            ha = Gateway(ha_config, args.timeout)
            saved_alarm_change = body['operation'] in ('alarm_enable', 'alarm_disable')
            dry_run = args.dry_run and 'request_id' in body
            if saved_alarm_change or dry_run:
                current = ha.request(SCHEDULE_PATH, {key: body[key] for key in ('entry_id', 'line')} |
                                     {'operation': 'status'})
                current = schedule_result(current, None, args.extension)
                if saved_alarm_change:
                    require(current['operation_result'] in ('applied', 'status'),
                            'ha_schedule_state_unconfirmed')
                    require(current['alarm']['configured'], 'ha_alarm_not_configured')
                    require(current['alarm']['line'] == args.extension, 'ha_alarm_owned_by_other_line')
                    phone = {'id': current['phone'], 'lines': current['lines']}
                    body['phone'] = phone['id']
            if dry_run:
                result = {'status': 'dry_run', 'selected_line': args.extension, 'phone_id': current['phone'],
                          'action': body['operation'], 'request': {key: value for key, value in body.items()
                                                                 if key != 'entry_id'}}
            else:
                action_key = body.get('request_id')
                value = schedule_result(ha.request(SCHEDULE_PATH, body), phone, args.extension)
                if saved_alarm_change and value['operation_result'] != 'outcome_unknown':
                    alarm = value['alarm']
                    require(value['operation_result'] in ('applied', 'replayed')
                            and alarm['configured'] and alarm['line'] == args.extension
                            and alarm['enabled'] == (body['operation'] == 'alarm_enable'),
                            'ha_alarm_change_unconfirmed')
                result = {'status': 'ok' if action_key is None else 'accepted', 'selected_line': args.extension,
                          'phone_id': value['phone'], 'schedule': value}
                if action_key is not None:
                    result['action_id'] = action_key
                    result['note'] = 'Schedule update accepted; future ringing is not guaranteed. No automatic retry.'
                if value['operation_result'] == 'outcome_unknown':
                    exit_code = 1
                    result.update(status='not_confirmed', reason='ha_schedule_outcome_unknown',
                                  note='Local schedule state is shown; ringing outcome is unknown. Do not retry blindly.')
        elif args.command in (None, 'status', 'destinations') or (
                args.command in ('dnd', 'autoanswer') and args.enabled is None):
            result = status_result(data, phone, args.extension)
            if 'home_assistant' in config:
                result['available_commands'].extend(SCHEDULE_COMMANDS)
                if args.command in (None, 'status'):
                    try:
                        ha_config = config['home_assistant']
                        value = Gateway(ha_config, args.timeout).request(SCHEDULE_PATH, {
                            'entry_id': ha_config['entry_id'], 'phone': phone['id'],
                            'line': args.extension, 'operation': 'status'})
                        result['schedule'] = schedule_result(value, phone, args.extension)
                        if not result['schedule']['gateway_fresh']:
                            result['status'], exit_code = 'partial', 1
                    except (Refused, OSError, ValueError, UnicodeError, TypeError, KeyError, AttributeError, RecursionError) as exc:
                        result['status'], exit_code = 'partial', 1
                        result['schedule_error'] = {'reason': exc.reason if isinstance(exc, Refused)
                                                    else 'ha_schedule_read_failed'}
                        if isinstance(exc, Refused) and exc.http_status is not None:
                            result['schedule_error']['http_status'] = exc.http_status
        else:
            action, body = make_action(args, data, phone)
            action_key = body['idempotency_key']
            path = f"/v1/phones/{phone['id']}/{action}"
            if args.dry_run:
                fields = {k: v for k, v in body.items() if k not in ('text', 'title', 'image_base64')}
                result = {'status': 'dry_run', 'selected_line': args.extension, 'phone_id': phone['id'],
                          'action': action, 'request': fields, 'content_omitted': action in ('message', 'image')}
            else:
                response = gateway.request(path, body)
                require(response.get('status') == 'accepted' and response.get('action_id') == action_key,
                        'unconfirmed_gateway_response')
                result = {'status': 'accepted', 'selected_line': args.extension, 'phone_id': phone['id'],
                          'action': action, 'action_id': action_key,
                          'note': 'Request accepted; run status to observe the effect. No automatic retry.'}
                if action == 'hardware_volume':
                    require(response.get('context') == body['context']
                            and response.get('direction') == body['direction']
                            and type(response.get('requested_steps')) is int
                            and type(response.get('acknowledged_steps')) is int
                            and response['requested_steps'] == response['acknowledged_steps'] == body['steps']
                            and response.get('level_known') is False
                            and 'volume_level' in response and response['volume_level'] is None
                            and response.get('physical_context_verified') is False
                            and response.get('audio_output') == 'contextual',
                            'unconfirmed_hardware_volume_response')
                    result.update({k: response[k] for k in (
                        'context', 'direction', 'requested_steps', 'acknowledged_steps',
                        'level_known', 'volume_level', 'audio_output', 'physical_context_verified')})
                    result['note'] = ('Phone acknowledged volume key presses. Absolute level and physical '
                                      'audio path are unknown; no automatic Save or retry. The currently '
                                      'active handset, headset or speaker determines call volume.')
                if action == 'intercom':
                    require(response.get('connection_state') == 'connected'
                            and isinstance(response.get('intercom_id'), str)
                            and re.fullmatch(r'cisco-intercom-[0-9a-f]{32}', response['intercom_id'])
                            and type(response.get('speaker_requested')) is bool, 'unconfirmed_intercom_response')
                    result.update(connection_state='connected', number=body['number'],
                                  intercom_id=response['intercom_id'], speaker_requested=response['speaker_requested'],
                                  note='Both lines were Up in the same PBX bridge. Speaker state and audibility '
                                       'cannot be read back. Use hangup to end the call.')
                if action in ('alert', 'alert_stop') or 'alert_id' in response:
                    require(isinstance(response.get('alert_id'), str) and re.fullmatch(r'[0-9a-f]{32}', response['alert_id']),
                            'invalid_alert_response')
                    if action == 'alert_stop':
                        require(response['alert_id'] == body['alert_id'], 'invalid_alert_response')
                    elif action == 'alert':
                        require(response.get('line') == body['line'] and response.get('kind') == body['kind'],
                                'invalid_alert_response')
                    result['alert_id'] = response['alert_id']
        if args.json:
            print(json.dumps(result, ensure_ascii=True))
        else:
            display(result)
        return exit_code
    except (Refused, OSError, ValueError, UnicodeError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        result = {'status': 'refused' if action_key is None else 'not_confirmed',
                  'reason': exc.reason if isinstance(exc, Refused) else 'local_or_transport_error'}
        if isinstance(exc, Refused) and exc.http_status is not None:
            result['http_status'] = exc.http_status
        if action_key is not None:
            result.update(action_id=action_key, note='Do not retry blindly; inspect status first.')
            if args.command == 'voice':
                result.update(
                    outcome='unknown',
                    recovery_command=shlex.join(
                        ['cisco', '--config', args.config, '--timeout', str(args.timeout)]
                        + (['--json'] if args.json else [])
                        + [args.extension, 'voice', 'recovery-status' if args.voice_action == 'recover' else 'status']),
                    note='The request may have been applied. Do not retry blindly. '
                         'Restore the intended transport and KUBECONFIG, then run '
                         'the read-only recovery command with the same private configuration.')
        elif args.command is None:
            result['offline_commands'] = ['status', 'help', 'directory', 'destinations', 'pickup', *COMMANDS.values(), *SCHEDULE_COMMANDS, *VOICE_COMMANDS]
        print(json.dumps(result), file=sys.stdout if args.json else sys.stderr)
        if not args.json and args.command is None:
            print('Offline command help (live availability unknown):', file=sys.stderr)
            for command in result['offline_commands']:
                print(f'  cisco {args.extension} {command}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
