"""Optional LifeSize Phone driver using its authenticated automation SSH CLI."""
from contextlib import contextmanager
import csv
import fcntl
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess


class LifeSizeError(Exception):
    def __init__(self, reason):
        self.reason = reason


def require(value, reason):
    if not value:
        raise LifeSizeError(reason)


def validate_config(value, config_path):
    require(isinstance(value, dict) and len(value) <= 32, 'invalid_lifesize_config')
    result, hosts = {}, set()
    for line, entry in value.items():
        require(isinstance(line, str) and re.fullmatch(r'2[0-9]{2}', line)
                and isinstance(entry, dict), 'invalid_lifesize_mapping')
        require(set(entry) <= {'host', 'identity_file', 'known_hosts_file', 'host_key_alias',
                              'legacy_rsa', 'label'}, 'invalid_lifesize_config')
        host = entry.get('host')
        try:
            address = ipaddress.IPv4Address(host)
        except (ValueError, TypeError):
            raise LifeSizeError('lifesize_requires_private_ipv4') from None
        require(isinstance(host, str) and any(address in ipaddress.ip_network(net)
                for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')),
                'lifesize_requires_private_ipv4')
        require(host not in hosts, 'duplicate_lifesize_host')
        hosts.add(host)
        item = dict(entry)
        item.setdefault('host_key_alias', host)
        item.setdefault('legacy_rsa', False)
        item.setdefault('label', 'LifeSize Phone')
        require(isinstance(item['host_key_alias'], str) and re.fullmatch(
            r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', item['host_key_alias']), 'invalid_lifesize_host_alias')
        require(type(item['legacy_rsa']) is bool, 'invalid_lifesize_legacy_rsa')
        require(isinstance(item['label'], str) and re.fullmatch(
            r'[A-Za-z0-9][ -~]{0,79}', item['label']), 'invalid_lifesize_label')
        for field in ('identity_file', 'known_hosts_file'):
            path = item.get(field)
            require(isinstance(path, str) and 0 < len(path) <= 4096 and not any(
                ord(c) < 32 for c in path), 'invalid_lifesize_ssh_file')
            path = Path(path).expanduser()
            if not path.is_absolute():
                path = Path(config_path).expanduser().absolute().parent / path
            item[field] = str(path)
        result[line] = item
    return result


class Phone:
    def __init__(self, config, timeout):
        self.config, self.timeout = config, timeout

    def request(self, command):
        c = self.config
        for field in ('identity_file', 'known_hosts_file'):
            try:
                info = os.lstat(c[field])
            except OSError:
                raise LifeSizeError('lifesize_ssh_file_unavailable') from None
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and not info.st_mode & (0o077 if field == 'identity_file' else 0o022),
                    'lifesize_ssh_file_permissions')
        argv = ['ssh', '-F', '/dev/null', '-o', 'BatchMode=yes',
                '-o', 'ConnectTimeout=' + str(min(5, self.timeout)),
                '-o', 'ConnectionAttempts=1', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'UserKnownHostsFile=' + c['known_hosts_file'],
                '-o', 'GlobalKnownHostsFile=/dev/null',
                '-o', 'HostKeyAlias=' + c['host_key_alias'],
                '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
                '-i', c['identity_file']]
        if c['legacy_rsa']:
            argv += ['-o', 'HostKeyAlgorithms=+ssh-rsa', '-o', 'PubkeyAcceptedAlgorithms=+ssh-rsa']
        argv += ['auto@' + c['host'], command]
        try:
            response = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                      timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise LifeSizeError('lifesize_ssh_outcome_unknown') from None
        require(response.returncode == 0, 'lifesize_ssh_or_command_failed')
        require(len(response.stdout) <= 65536, 'lifesize_response_too_large')
        try:
            rows = [line.strip() for line in response.stdout.decode('utf-8').splitlines() if line.strip()]
        except UnicodeError:
            raise LifeSizeError('invalid_lifesize_response') from None
        require('ok,00' in rows and not any(row.startswith('error,') for row in rows),
                'lifesize_command_unconfirmed')
        return [row for row in rows if row != 'ok,00' and not row.startswith('CS,')]

    def snapshot(self, line):
        require(self.request('get sip username') == [line], 'lifesize_sip_line_mismatch')
        registration = self.request('get sip register')
        require(len(registration) == 1 and re.fullmatch(r'[A-Za-z -]{1,48}', registration[0]),
                'invalid_lifesize_registration')
        return {'registered': registration[0] == 'registered', 'registration': registration[0],
                'calls': parse_calls(self.request('status call active'))}


def parse_calls(rows):
    require(len(rows) <= 32, 'invalid_lifesize_calls')
    calls, seen = [], set()
    for row in rows:
        fields = next(csv.reader([row]))
        # The original API has seven columns; Phone 4.5.2 appends four.
        require(len(fields) in (7, 11), 'invalid_lifesize_calls')
        if len(fields) == 11:
            require(fields[7] in ('Yes', 'No') and fields[8] in
                    ('sip', 'h323', 'h323gw', 'isdn', 'pstn')
                    and fields[9] in ('Yes', 'No') and re.fullmatch(
                        r'[0-9]{1,6}:[0-5][0-9]:[0-5][0-9]', fields[10]),
                    'invalid_lifesize_calls')
        call_id, conference, state, incoming, media_type, number, _name = fields[:7]
        require(re.fullmatch(r'[1-9][0-9]{0,9}', call_id) and call_id not in seen
                and conference.isdigit() and state in
                ('Dialing', 'Ringing', 'Connected', 'Terminating', 'Ringback')
                and incoming in ('Yes', 'No') and media_type in ('Audio', 'Video', 'Unknown'),
                'invalid_lifesize_calls')
        seen.add(call_id)
        calls.append({'id': call_id, 'state': state, 'incoming': incoming == 'Yes',
                      'number': number if re.fullmatch(r'[A-Za-z0-9@+_.:\[\]-]{0,128}', number)
                      else '(unavailable)'})
    return calls


@contextmanager
def device_lock(host):
    directory = Path.home() / '.local/state/cisco-home/lifesize'
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = directory.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077, 'lifesize_lock_permissions')
    path = directory / (hashlib.sha256(host.encode()).hexdigest() + '.lock')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and not info.st_mode & 0o077, 'lifesize_lock_permissions')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LifeSizeError('lifesize_command_already_running') from None
        yield
    finally:
        os.close(fd)


def run(args, devices, data, on_dispatch):
    line, command = args.extension, args.command
    require(command in (None, 'status', 'destinations', 'dial', 'call', 'hangup'),
            'command_not_supported_by_lifesize')
    require(not any(line in phone['lines'] for phone in data['phones']),
            'ambiguous_lifesize_cisco_mapping')
    mutation = command in ('dial', 'call', 'hangup')
    if mutation:
        require(args.idempotency_key is None, 'lifesize_idempotency_keys_not_supported')
    if command in ('dial', 'call'):
        require(re.fullmatch(r'[0-9]{3,8}', args.number)
                and args.number in data['dial_allowlist'], 'destination_not_allowlisted')
        require(args.number != line, 'lifesize_cannot_dial_itself')
    config = devices[line]
    phone = Phone(config, args.timeout)
    with device_lock(config['host']):
        observed = phone.snapshot(line)
        base = {'selected_line': line, 'phone_id': line, 'driver': 'lifesize_ssh',
                'label': config['label']}
        if not mutation:
            return dict(base, status='ok', **observed, dial_allowlist=data['dial_allowlist'],
                        available_commands=['status', 'directory', 'destinations', 'dial NUMBER',
                                            'call NUMBER', 'hangup'])
        if command in ('dial', 'call'):
            require(observed['registered'], 'lifesize_not_registered')
            require(not observed['calls'], 'phone_busy')
            action, request = 'dial', 'control call dial -p sip ' + args.number
            details = {'number': args.number}
        else:
            require(len(observed['calls']) == 1, 'lifesize_hangup_requires_one_call')
            call_id = observed['calls'][0]['id']
            action, request = 'hangup', 'control call hangup ' + call_id
            details = {'call_id': call_id}
        if args.dry_run:
            return dict(base, status='dry_run', action=action, **details)
        action_id = secrets.token_hex(16)
        on_dispatch(action_id)
        phone.request(request)
        return dict(base, status='accepted', action=action, action_id=action_id, **details,
                    note='Phone acknowledged the request. Run status to observe the effect. No automatic retry.')


def add_directory(result, devices):
    for line, config in devices.items():
        matches = [p for p in result['directory'] if line in p['lines']]
        require(not any(p['type'] != 'softphone' for p in matches), 'ambiguous_lifesize_cisco_mapping')
        result['directory'] = [p for p in result['directory'] if p not in matches]
        result['directory'].append({'type': 'lifesize', 'id': line, 'lines': [line],
                                    'room': config['label'], 'online': None,
                                    'selected': result['selected_line'] == line})
    result['directory'].sort(key=lambda p: int(p['id']))
    return result
