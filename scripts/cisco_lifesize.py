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
import tempfile
import time


# Values are native LifeSize settings, not Cisco volume-key presses or music gain.
VOLUME_SETTINGS = {'ringer': ('volume ring-tone', 10), 'call': ('volume speaker', 100)}
SETTING_COMMANDS = {'autoanswer': 'autoanswer', 'automute': 'autoanswer_mic_muted',
                    'dnd': 'dnd', 'mic': 'mic_muted'}
SETTINGS = {'ringer_volume': VOLUME_SETTINGS['ringer'], 'call_volume': VOLUME_SETTINGS['call'],
            'autoanswer': ('call auto-answer', None),
            'autoanswer_mic_muted': ('call auto-mute', None),
            'dnd': ('call do-not-disturb', None), 'mic_muted': ('audio mute', None)}
AVAILABLE_COMMANDS = (
    'status', 'directory', 'destinations', 'dial NUMBER', 'call NUMBER',
    'answer', 'pickup', 'reject', 'hangup', 'autoanswer [on|off]', 'automute [on|off]',
    'dnd [on|off|status]',
    'volume ringer|call status', 'volume ringer|call up|down [--steps 1..5]',
    'volume ringer set 0..10', 'volume call set 0..100', 'mic [mute|unmute|status]',
    'message TEXT [--title TITLE]', 'message --text-file FILE [--title TITLE]',
)
CAPABILITIES = ('dial', 'answer', 'reject', 'hangup', 'autoanswer', 'automute', 'dnd',
                'hardware_volume', 'microphone', 'message')


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
        self.control_socket = None

    def ssh_arguments(self):
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
        return argv

    @contextmanager
    def connection(self):
        """Keep one authenticated transport for a bounded foreground operation."""
        require(self.control_socket is None, 'lifesize_connection_already_open')
        # A short private path also fits macOS's Unix socket path limit.
        with tempfile.TemporaryDirectory(prefix='cisco-ssh-', dir='/tmp') as directory:
            socket = str(Path(directory) / 'control')
            master = None
            try:
                master = subprocess.Popen(self.ssh_arguments() + [
                    '-M', '-S', socket, '-N', 'auto@' + self.config['host']],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                deadline = time.monotonic() + self.timeout
                while not Path(socket).exists() and master.poll() is None and time.monotonic() < deadline:
                    time.sleep(.025)
                require(master.poll() is None and Path(socket).exists(), 'lifesize_connection_unavailable')
                self.control_socket = socket
                yield self
            except OSError:
                raise LifeSizeError('lifesize_connection_unavailable') from None
            finally:
                self.control_socket = None
                if master is not None and master.poll() is None:
                    master.terminate()
                    try:
                        master.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        master.kill()
                        master.wait()

    def request(self, command):
        c = self.config
        argv = self.ssh_arguments()
        if self.control_socket is not None:
            # If the prepared transport disappears, fail instead of silently
            # reconnecting and delaying a call-scoped action.
            argv += ['-S', self.control_socket, '-o', 'ControlMaster=no',
                     '-o', 'ProxyCommand=false']
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
        return [row for row in rows if row != 'ok,00' and not asynchronous_row(row)]

    def snapshot(self, line):
        require(self.request('get sip username') == [line], 'lifesize_sip_line_mismatch')
        registration = self.request('get sip register')
        require(len(registration) == 1 and re.fullmatch(r'[A-Za-z -]{1,48}', registration[0]),
                'invalid_lifesize_registration')
        return {'registered': registration[0] == 'registered', 'registration': registration[0],
                'calls': parse_calls(self.request('status call active'))}

    def read_setting(self, name, maximum=None):
        rows = self.request('get ' + name)
        require(len(rows) == 1, 'invalid_lifesize_setting')
        if maximum is None:
            require(rows[0] in ('on', 'off'), 'invalid_lifesize_setting')
            return rows[0] == 'on'
        require(re.fullmatch(r'0|[1-9][0-9]{0,2}', rows[0]) is not None,
                'invalid_lifesize_setting')
        value = int(rows[0])
        require(value <= maximum, 'invalid_lifesize_setting')
        return value

    def settings(self, keys=None):
        values = {}
        for key in SETTINGS if keys is None else keys:
            name, maximum = SETTINGS[key]
            value = self.read_setting(name, maximum)
            values[key] = value if maximum is None else {
                'value': value, 'min': 0, 'max': maximum, 'unit': 'native',
            }
        return values


def asynchronous_row(row):
    """Notifications are not the scalar reply; malformed new forms remain visible."""
    if row.startswith('CS,') or row in ('MS,true', 'MS,false'):
        return True
    if row.startswith('IC,'):
        fields = next(csv.reader([row]))
        return (len(fields) == 7 and fields[1].isdigit() and fields[2].isdigit()
                and fields[3] in ('Dialing', 'Ringing', 'Ring Incoming', 'Connected', 'Terminating', 'Ringback')
                and fields[4] in ('Audio', 'Video', 'Unknown'))
    return False


def prepare_message(args):
    require((args.text is not None) != (args.text_file is not None), 'choose_text_or_text_file')
    value = args.text
    if args.text_file is not None:
        fd = os.open(Path(args.text_file).expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_size <= 1024, 'lifesize_message_file_invalid')
            raw = source.read(1025)
        require(len(raw) <= 1024, 'lifesize_message_file_invalid')
        try:
            value = raw.decode('ascii').rstrip('\r\n')
        except UnicodeError:
            raise LifeSizeError('lifesize_message_requires_simple_ascii') from None
    # The firmware does not document quoting/escaping. Only unambiguous text is
    # accepted; none of these characters can escape the native quoted argument.
    alphabet = r"[A-Za-z0-9 .,!?():_+/@'-]+"
    require(isinstance(args.title, str) and 1 <= len(args.title) <= 32
            and re.fullmatch(alphabet, args.title), 'lifesize_message_title_invalid')
    require(isinstance(value, str) and value.strip() and re.fullmatch(alphabet, value),
            'lifesize_message_requires_simple_ascii')
    text = args.title + ': ' + value
    require(len(text) <= 200, 'lifesize_message_maximum_200_characters')
    return text


def parse_calls(rows):
    require(len(rows) <= 32, 'invalid_lifesize_calls')
    calls, seen = [], set()
    for row in rows:
        fields = next(csv.reader([row]))
        # The original API has seven columns. Phone 4.5.2 appends three,
        # followed by an elapsed duration once the call is established.
        require(len(fields) in (7, 10, 11), 'invalid_lifesize_calls')
        if len(fields) >= 10:
            require(fields[7] in ('Yes', 'No') and fields[8] in
                    ('sip', 'h323', 'h323gw', 'isdn', 'pstn')
                    and fields[9] in ('Yes', 'No'),
                    'invalid_lifesize_calls')
        if len(fields) == 11:
            require(re.fullmatch(r'[0-9]{1,6}:[0-5][0-9]:[0-5][0-9]', fields[10]),
                    'invalid_lifesize_calls')
        call_id, conference, state, incoming, media_type, number, _name = fields[:7]
        require(re.fullmatch(r'[1-9][0-9]{0,9}', call_id) and call_id not in seen
                and conference.isdigit() and state in
                ('Dialing', 'Ringing', 'Ring Incoming', 'Connected', 'Terminating', 'Ringback')
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


def validate_action(args):
    """Validate the command before any phone connection."""
    command = args.command
    require(command in (None, 'status', 'destinations', 'dial', 'call', 'hangup',
                        'answer', 'pickup', 'reject', 'autoanswer', 'automute', 'dnd', 'volume', 'mic',
                        'message'),
            'command_not_supported_by_lifesize')
    if command == 'message':
        args.lifesize_message = prepare_message(args)
        return True
    if command in ('autoanswer', 'automute', 'dnd'):
        choices = (None, 'on', 'off', 'status') if command == 'dnd' else (None, 'on', 'off')
        require(args.enabled in choices, 'invalid_lifesize_setting_action')
        return args.enabled in ('on', 'off')
    if command == 'mic':
        require(args.mode in (None, 'status', 'mute', 'unmute'), 'invalid_lifesize_setting_action')
        return args.mode in ('mute', 'unmute')
    if command == 'volume':
        require(args.context in VOLUME_SETTINGS and args.direction in ('status', 'up', 'down', 'set'),
                'invalid_lifesize_volume_action')
        require(type(args.steps) is int and 1 <= args.steps <= 5, 'invalid_lifesize_volume_steps')
        if args.direction == 'set':
            require(type(args.value) is int and 0 <= args.value <= VOLUME_SETTINGS[args.context][1],
                    'invalid_lifesize_volume_value')
        else:
            require(args.value is None, 'invalid_lifesize_volume_value')
        require(args.direction in ('up', 'down') or args.steps == 1,
                'lifesize_steps_require_volume_up_or_down')
        return args.direction != 'status'
    return command in ('dial', 'call', 'hangup', 'answer', 'pickup', 'reject')


def run(args, devices, data, on_dispatch):
    line, command = args.extension, args.command
    mutation = validate_action(args)
    require(not any(line in phone['lines'] for phone in data['phones']),
            'ambiguous_lifesize_cisco_mapping')
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
        # Each request opens an SSH session. Keep call control independent of
        # unrelated settings and query only the requested setting for its control.
        if command in (None, 'status', 'destinations'):
            settings = phone.settings()
        else:
            key = args.context + '_volume' if command == 'volume' else SETTING_COMMANDS.get(command)
            settings = phone.settings((key,)) if key is not None else {}
        base = {'selected_line': line, 'phone_id': line, 'driver': 'lifesize_ssh', 'label': config['label']}
        status = dict(base, status='ok', **observed, settings=settings,
                      capabilities=list(CAPABILITIES), dial_allowlist=data['dial_allowlist'],
                      available_commands=list(AVAILABLE_COMMANDS),
                      note='Ringer, auto-answer and auto-mute changes require an idle phone. '
                           'Speaker, microphone and DND changes require one connected call. '
                           'Native DND protects that call from interruptions; it does not block idle incoming calls.')
        if not mutation:
            return status
        return execute_action(args, phone, observed, settings, base, status, on_dispatch)


def execute_action(args, phone, observed, settings, base, status, on_dispatch):
    command = args.command
    setting_name, setting_key, maximum = None, None, None
    details = {}
    if command in ('autoanswer', 'automute', 'dnd', 'mic'):
        setting_name, setting_key = {
            'autoanswer': ('call auto-answer', 'autoanswer'),
            'automute': ('call auto-mute', 'autoanswer_mic_muted'),
            'dnd': ('call do-not-disturb', 'dnd'),
            'mic': ('audio mute', 'mic_muted'),
        }[command]
        previous = settings[setting_key]
        desired = args.mode == 'mute' if command == 'mic' else args.enabled == 'on'
        action = command
    elif command == 'volume':
        setting_name, maximum = VOLUME_SETTINGS[args.context]
        setting_key = args.context + '_volume'
        previous = settings[setting_key]['value']
        desired = args.value if args.direction == 'set' else max(0, min(maximum,
            previous + args.steps * (1 if args.direction == 'up' else -1)))
        action = 'hardware_volume'
        details.update(context=args.context, direction=args.direction, steps=args.steps)

    if setting_name is not None:
        details.update(previous_value=previous, value=desired, changed=desired != previous)
        if desired == previous:
            if args.dry_run:
                return dict(base, status='dry_run', action=action, **details)
            return dict(status, action=action, confirmed=True, **details)
        if command in ('mic', 'dnd') or (command == 'volume' and args.context == 'call'):
            require(len(observed['calls']) == 1 and observed['calls'][0]['state'] == 'Connected',
                    'lifesize_requires_one_connected_call')
            details['call_id'] = observed['calls'][0]['id']
        else:
            require(not observed['calls'], 'lifesize_setting_requires_idle_phone')
        value = ('on' if desired else 'off') if maximum is None else str(desired)
        request = 'set ' + setting_name + ' ' + value
    elif command in ('dial', 'call'):
        require(observed['registered'], 'lifesize_not_registered')
        require(not observed['calls'], 'phone_busy')
        action, request = 'dial', 'control call dial -p sip ' + args.number
        details = {'number': args.number}
    elif command == 'message':
        require(not observed['calls'], 'lifesize_message_requires_idle_phone')
        action = 'message'
        request = 'set system message -t 15 "' + args.lifesize_message + '"'
        details = {'characters': len(args.lifesize_message), 'duration_seconds': 15}
    else:
        require(len(observed['calls']) == 1, 'lifesize_requires_one_call')
        call = observed['calls'][0]
        if command in ('answer', 'pickup', 'reject'):
            require(call['state'] in ('Ringing', 'Ringback', 'Ring Incoming') and call['incoming'],
                    'lifesize_requires_one_incoming_ringing_call')
            action = 'reject' if command == 'reject' else 'answer'
            flag = '-r' if command == 'reject' else '-t audio'
            request = 'control call answer ' + flag + ' ' + call['id']
        else:
            action, request = 'hangup', 'control call hangup ' + call['id']
        details = {'call_id': call['id']}
    if args.dry_run:
        return dict(base, status='dry_run', action=action, **details)

    # Local locking cannot prevent a person or another controller using the phone.
    # Read the target setting before the final call-state check to keep the latter
    # as close to the mutation as possible.
    if setting_name is not None:
        require(phone.read_setting(setting_name, maximum) == previous, 'lifesize_setting_changed')
    require(parse_calls(phone.request('status call active')) == observed['calls'],
            'lifesize_call_context_changed')
    action_id = secrets.token_hex(16)
    on_dispatch(action_id)
    phone.request(request)
    if setting_name is not None:
        actual = phone.read_setting(setting_name, maximum)
        require(actual == desired, 'lifesize_setting_not_confirmed')
        if 'call_id' in details:
            require(parse_calls(phone.request('status call active')) == observed['calls'],
                    'lifesize_call_context_changed_after_setting')
        if maximum is None:
            settings[setting_key] = actual
        else:
            settings[setting_key] = dict(settings[setting_key], value=actual)
        return dict(base, status='accepted', action=action, action_id=action_id,
                    confirmed=True, settings=settings, **details,
                    note='Phone setting read back successfully. No automatic retry.')
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
