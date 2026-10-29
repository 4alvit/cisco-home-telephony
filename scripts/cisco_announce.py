"""Finite HA-voice announcements with one-shot phone answers."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import math
from pathlib import Path
import re
import secrets
import shlex
import threading
import time
from cisco_http import announcement_url

COMMANDS = frozenset(('say', 'announce', 'voicemsg', 'say-status', 'say-stop'))
HELP = ['say TEXT [--text-file FILE]', 'voicemsg TEXT', 'say-status JOB_ID', 'say-stop JOB_ID']
MAX_LINES, MAX_TEXT, WAIT_SECONDS = 16, 1000, 240
JOB_STATES = frozenset(('accepted', 'running', 'stopping', 'finished', 'stopped', 'failed', 'dry_run'))
RECIPIENT_STATES = frozenset(('queued', 'ringing', 'playing', 'completed', 'skipped', 'failed', 'stopping', 'stopped'))
TERMINAL = frozenset(('finished', 'stopped', 'failed', 'dry_run'))

class AnnouncementError(Exception):
    def __init__(self, reason):
        self.reason = reason

def require(value, reason):
    if not value:
        raise AnnouncementError(reason)


def validate_lines(lines):
    require(isinstance(lines, list) and 1 <= len(lines) <= MAX_LINES
            and all(isinstance(line, str) and re.fullmatch(r'2[0-9]{2}', line) for line in lines)
            and len(set(lines)) == len(lines), 'invalid_announcement_targets')
    return lines


def validate_config(value, path, read_file):
    require(isinstance(value, dict) and set(value) <= {
        'gateway_url', 'token_file', 'transport', 'kubectl', 'groups'}, 'invalid_announcement_config')
    result = dict(value)
    result['gateway_url'] = announcement_url(value['gateway_url'])
    require(value.get('transport') == 'kubectl', 'announcements_require_kubectl_transport')
    kube = value.get('kubectl')
    require(isinstance(kube, dict) and set(kube) == {'context', 'namespace', 'selector', 'container'},
            'invalid_announcement_kubectl_config')
    for key in ('context', 'namespace', 'container'):
        require(isinstance(kube[key], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}', kube[key]),
                'invalid_announcement_kubectl_target')
    require(isinstance(kube['selector'], str) and len(kube['selector']) <= 256 and re.fullmatch(
        r'[A-Za-z0-9_./-]+=[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_./-]+=[A-Za-z0-9_.-]+)*', kube['selector']),
        'invalid_announcement_kubectl_selector')
    token_file = value.get('token_file')
    require(isinstance(token_file, str) and 0 < len(token_file) <= 4096,
            'announcement_token_file_required')
    token_path = Path(token_file).expanduser()
    if not token_path.is_absolute():
        token_path = Path(path).expanduser().parent / token_path
    result['token'] = read_file(token_path, 512, private=True).decode().strip()
    require(re.fullmatch(r'[A-Za-z0-9_-]{32,128}', result['token']), 'invalid_announcement_token')
    groups = value.get('groups', {})
    require(isinstance(groups, dict) and len(groups) <= 32, 'invalid_announcement_groups')
    for name, lines in groups.items():
        require(isinstance(name, str) and re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', name),
                'invalid_announcement_group_name')
        validate_lines(lines)
    result['groups'] = groups
    result['api'] = 'announcements'
    return result


def target_lines(target, config):
    require(isinstance(target, str), 'invalid_announcement_targets')
    if target.startswith('@'):
        require(target[1:] in config.get('groups', {}), 'unknown_announcement_group')
        lines = list(config['groups'][target[1:]])
    else:
        lines = target.split(',')
    return sorted(validate_lines(lines), key=int)


def known_targets(lines, config, data, inventory_validator):
    phones = inventory_validator(data)
    selected, physical = {}, set()
    for line in lines:
        phone = next((p for p in phones if line in p['lines']), None)
        native = config.get('lifesize', {}).get(line)
        require(not (phone and native), 'ambiguous_announcement_target')
        require(phone is not None or native is not None, 'unknown_announcement_target')
        identity = ('cisco', phone['id']) if phone else ('lifesize', native['host'])
        require(identity not in physical, 'duplicate_announcement_phone')
        physical.add(identity)
        selected[line] = {'driver': identity[0], 'phone_id': phone['id'] if phone else line}
    return selected


def read_text(args, read_file):
    words = args.text
    require(bool(words) != (args.text_file is not None), 'choose_text_or_text_file')
    text = ' '.join(words) if words else read_file(args.text_file, 16384).decode('utf-8')
    require(1 <= len(text.strip()) <= MAX_TEXT and len(text) <= MAX_TEXT
            and not any(ord(c) < 32 and c not in '\n\t' for c in text)
            and '\x7f' not in text, 'announcement_text_requires_1_to_1000_characters')
    return text


def job_id(key):
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def validate_job(value, identifier, lines):
    require(isinstance(value, dict) and value.get('id') == identifier
            and value.get('status') in JOB_STATES, 'invalid_announcement_response')
    require(sorted(validate_lines(value.get('lines')), key=int) == lines, 'announcement_targets_changed')
    expires, duration = value.get('expires_at'), value.get('duration_seconds')
    require(type(expires) in (int, float) and math.isfinite(expires) and 0 < expires < 10**12,
            'invalid_announcement_expiry')
    require(duration is None or type(duration) in (int, float) and math.isfinite(duration)
            and 0 <= duration <= 180, 'invalid_announcement_duration')
    recipients = value.get('recipients')
    require(isinstance(recipients, list) and len(recipients) == len(lines), 'invalid_announcement_recipients')
    result, seen = [], set()
    for item in recipients:
        require(isinstance(item, dict) and item.get('line') in lines and item['line'] not in seen
                and item.get('status') in RECIPIENT_STATES, 'invalid_announcement_recipient')
        line = item['line']; seen.add(line)
        require(isinstance(item.get('phone_id'), str) and re.fullmatch(r'2[0-9]{2}', item['phone_id']),
                'invalid_announcement_phone')
        channel, unique = item.get('channel'), item.get('uniqueid')
        require((channel is None and unique is None) or isinstance(channel, str)
                and re.fullmatch(r'PJSIP/' + line + r'-[A-Za-z0-9]+', channel)
                and isinstance(unique, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', unique),
                'invalid_announcement_call')
        caller = item.get('caller_id')
        require(isinstance(caller, str) and re.fullmatch(r'[0-9]{3,32}', caller),
                'invalid_announcement_caller')
        reason = item.get('reason')
        require(reason is None or isinstance(reason, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,95}', reason),
                'invalid_announcement_reason')
        if item['status'] in ('ringing', 'playing'):
            require(channel is not None, 'missing_announcement_call')
        result.append({key: item.get(key) for key in ('line', 'phone_id', 'status', 'channel',
                                                     'uniqueid', 'caller_id', 'reason')})
    return {'id': identifier, 'status': value['status'], 'lines': lines, 'recipients': result,
            'expires_at': expires, 'duration_seconds': duration}


def same_ringing(client, identifier, lines, recipient):
    current = validate_job(client.request('/v1/announcements/' + identifier), identifier, lines)
    require(current['status'] not in TERMINAL and current['status'] != 'stopping'
            and current['expires_at'] > time.time(), 'announcement_no_longer_active')
    item = next(p for p in current['recipients'] if p['line'] == recipient['line'])
    require(item['status'] == 'ringing' and all(item[k] == recipient[k] for k in
            ('channel', 'uniqueid', 'caller_id', 'phone_id')), 'announcement_call_changed')


def marker_matches(number, marker):
    return bool(re.fullmatch(r'(?:sips?:)?' + re.escape(marker) + r'(?:@[A-Za-z0-9.:[\]_-]+)?', number))


def answer_once(config, timeout, identifier, lines, recipient, target, gateway_factory,
                inventory_validator, cancelled):
    attempted = False
    try:
        service = gateway_factory(config['announcements'], timeout)
        line = recipient['line']
        if target['driver'] == 'lifesize':
            import cisco_lifesize as native
            device = config['lifesize'][line]
            with native.device_lock(device['host']):
                phone = native.Phone(device, timeout)
                calls = phone.snapshot(line)['calls']
                require(len(calls) == 1 and calls[0]['incoming'] and
                        calls[0]['state'] in ('Ringing', 'Ringback', 'Ring Incoming')
                        and marker_matches(calls[0]['number'], recipient['caller_id']),
                        'announcement_native_call_mismatch')
                same_ringing(service, identifier, lines, recipient)
                require(native.parse_calls(phone.request('status call active')) == calls,
                        'announcement_native_call_changed')
                require(not cancelled.is_set(), 'announcement_cancelled')
                attempted = True
                phone.request('control call answer -t audio ' + calls[0]['id'])
        else:
            gateway = gateway_factory(config, timeout)
            same_ringing(service, identifier, lines, recipient)
            phone = inventory_validator(gateway.request('/v1/phones'), line)
            require(phone['id'] == target['phone_id'] == recipient['phone_id']
                    and not phone['dnd'] and not phone['dnd_pending']
                    and len(phone['channels']) == 1, 'announcement_phone_changed')
            call = phone['channels'][0]
            require(call['line'] == line and call['state'] == 'Ringing'
                    and call['name'] == recipient['channel'] and call['uniqueid'] == recipient['uniqueid'],
                    'announcement_call_changed')
            require(not phone.get('alert') or phone['alert']['state'] == 'ended',
                    'announcement_alert_active')
            require(not cancelled.is_set(), 'announcement_cancelled')
            key = 'announce-' + identifier + '-' + line
            body = {'line': line, 'channel': call['name'], 'uniqueid': call['uniqueid'], 'idempotency_key': key}
            attempted = True
            result = gateway.request('/v1/phones/' + phone['id'] + '/answer', body)
            require(result.get('status') == 'accepted' and result.get('action_id') == key,
                    'announcement_answer_not_confirmed')
        return {'line': recipient['line'], 'status': 'acknowledged'}
    except Exception as error:
        reason = getattr(error, 'reason', 'announcement_answer_failed')
        if not isinstance(reason, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,95}', reason):
            reason = 'announcement_answer_failed'
        return {'line': recipient['line'], 'status': 'not_confirmed' if attempted else 'refused', 'reason': reason}


def recovery(args, identifier):
    return shlex.join(['cisco', '--config', args.config, '--timeout', str(args.timeout),
                      args.extension, 'say-status', identifier])


def run(args, config, *, gateway_factory, inventory_validator, read_file, clock=time.monotonic,
        sleep=time.sleep):
    identifier, dispatched, lines = None, False, []
    latest, answers = None, {}
    cancelled = threading.Event()
    try:
        require('announcements' in config, 'announcement_config_required')
        private = config['announcements']
        lines = target_lines(args.extension, private)
        service = gateway_factory(private, args.timeout)
        if args.command in ('say-status', 'say-stop'):
            identifier = args.job_id
            require(re.fullmatch(r'[0-9a-f]{32}', identifier), 'invalid_announcement_id')
            latest = validate_job(service.request('/v1/announcements/' + identifier), identifier, lines)
            if args.command == 'say-status':
                return latest, int(latest['status'] == 'failed')
            if args.dry_run:
                return dict(latest, status='dry_run', action='stop'), 0
            if latest['status'] not in TERMINAL:
                dispatched = True
                latest = validate_job(service.request('/v1/announcements/' + identifier + '/stop', {}), identifier, lines)
            return latest, 0
        text = read_text(args, read_file)
        data = gateway_factory(config, args.timeout).request('/v1/phones')
        targets = known_targets(lines, config, data, inventory_validator)
        key = args.idempotency_key or 'say-' + secrets.token_hex(16)
        require(isinstance(key, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', key),
                'invalid_idempotency_key')
        identifier = job_id(key)
        body = {'idempotency_key': key, 'lines': lines, 'text': text, 'dry_run': args.dry_run}
        if not args.dry_run:
            dispatched = True
        latest = validate_job(service.request('/v1/announcements', body), identifier, lines)
        if args.dry_run:
            require(latest['status'] == 'dry_run', 'announcement_dry_run_not_confirmed')
            return latest, 0
        deadline = clock() + WAIT_SECONDS
        futures = {}
        with ThreadPoolExecutor(max_workers=MAX_LINES, thread_name_prefix='announcement-answer') as pool:
            try:
                while latest['status'] not in TERMINAL:
                    require(clock() < deadline, 'announcement_wait_timeout')
                    for recipient in latest['recipients']:
                        line = recipient['line']
                        if recipient['status'] == 'ringing' and line not in futures:
                            futures[line] = pool.submit(answer_once, config, args.timeout, identifier, lines,
                                recipient, targets[line], gateway_factory, inventory_validator, cancelled)
                    for line, future in futures.items():
                        if future.done():
                            answers[line] = future.result()
                    sleep(.5)
                    latest = validate_job(service.request('/v1/announcements/' + identifier), identifier, lines)
            finally:
                cancelled.set()
            for line, future in futures.items():
                answers[line] = future.result()
        latest['answers'] = list(answers.values())
        latest['note'] = 'Playback results come from Asterisk. They do not prove that a person heard the message.'
        failure = latest['status'] in ('failed', 'stopped') or any(
            p['status'] != 'completed' for p in latest['recipients'])
        return latest, int(failure)
    except (Exception, KeyboardInterrupt) as error:
        cancelled.set()
        reason = getattr(error, 'reason', 'announcement_local_or_transport_error')
        if not isinstance(reason, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,95}', reason):
            reason = 'announcement_local_or_transport_error'
        result = {'status': 'not_confirmed' if dispatched else 'refused', 'reason': reason,
                  'lines': lines, 'answers': list(answers.values()), 'content_omitted': True}
        if identifier is not None:
            result.update(id=identifier, recovery_command=recovery(args, identifier))
        if dispatched:
            result['note'] = 'Do not repeat the announcement blindly. Inspect this job; the server owns bounded cleanup.'
        return result, 1


def display(value):
    print('Announcement: ' + value['status'])
    if value.get('id'):
        print('Job: ' + value['id'])
    for item in value.get('recipients', []):
        print(item['line'] + ': ' + item['status'] + (' (' + item['reason'] + ')' if item.get('reason') else ''))
    for item in value.get('answers', []):
        if item['status'] != 'acknowledged':
            print('Answer ' + item['line'] + ': ' + item['status'] + ' (' + item.get('reason', '') + ')')
    for key in ('reason', 'note', 'recovery_command'):
        if value.get(key):
            print(value[key])
