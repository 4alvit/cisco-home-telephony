#!/usr/bin/env python3
"""Private-LAN Cisco 7945G control gateway. Never expose this service publicly.

The API is bearer-authenticated and source-restricted. Phone authentication and
short-lived XML/image resources are served on separate, per-phone listeners.
Key/Dial URIs are immediate: all control decisions use fresh AMI observations.
"""
import argparse
import base64
import hashlib
import hmac
import io
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import parse, request
import xml.etree.ElementTree as ET

MAX_BODY = 3 * 1024 * 1024


def app_module(name):
    """Load packaged siblings also when the gateway is imported by provisioners."""
    module_name = '_cisco_gateway_' + name
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(module_name, Path(__file__).with_name(name + '.py'))
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
    return sys.modules[module_name]


class Refused(Exception):
    def __init__(self, reason, status=409):
        self.reason, self.status = reason, status


class PhoneCommandRejected(Refused):
    """A validated device rejection, distinct from an ambiguous network result."""
    def __init__(self, response_code):
        self.response_code = response_code
        super().__init__('phone_rejected_action', 502)


def require(value, reason, status=409):
    if not value:
        raise Refused(reason, status)


def constant(left, right):
    return hmac.compare_digest(hashlib.sha256(left.encode()).digest(),
                               hashlib.sha256(right.encode()).digest())


def private_ip(value):
    address = ipaddress.IPv4Address(value)
    require(any(address in ipaddress.ip_network(net) for net in
                ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')), 'private_ipv4_required', 400)
    return str(address)


def room_name(value):
    """Validate optional public metadata, never credentials or configuration text."""
    require(value is None or isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _'()-]{0,47}", value)
            and value == value.strip(), 'bad_room', 400)
    return value


def validate(config):
    require(config.get('persistent_enabled') is True, 'explicit_persistent_mode_required', 400)
    private_ip(config['bind_ipv4'])
    require(type(config['api_port']) is int and 1024 <= config['api_port'] <= 65535, 'bad_port', 400)
    require(re.fullmatch(r'[A-Za-z0-9_-]{32,128}', config['api_token']), 'bad_api_token', 400)
    require(config['api_clients'] and len(config['api_clients']) <= 8, 'bad_api_clients', 400)
    for value in config['api_clients']:
        private_ip(value)
    private_ip(config['ami']['host'])
    require(type(config['ami']['port']) is int and 1 <= config['ami']['port'] <= 65535, 'bad_ami_port', 400)
    for key in ('username', 'secret'):
        require(isinstance(config['ami'][key], str) and 1 <= len(config['ami'][key]) <= 128
                and not re.search(r'[\r\n\x00]', config['ami'][key]), 'bad_ami_credentials', 400)
    ports, addresses, lines = {config['api_port']}, set(), set()
    require(1 <= len(config['phones']) <= 32, 'bad_phone_count', 400)
    for phone in config['phones']:
        room_name(phone.get('room'))
        require(phone['id'] == phone['lines'][0] and 1 <= len(phone['lines']) <= 2, 'bad_line_mapping', 400)
        private_ip(phone['ipv4'])
        require(phone['ipv4'] not in addresses, 'duplicate_phone_ip', 400)
        addresses.add(phone['ipv4'])
        require(re.fullmatch(r'SEP[0-9A-F]{12}', phone['device']), 'bad_device', 400)
        require(type(phone['port']) is int and 1024 <= phone['port'] <= 65535
                and phone['port'] not in ports, 'duplicate_or_bad_port', 400)
        ports.add(phone['port'])
        require(re.fullmatch(r'/auth/[a-f0-9]{64}', phone['auth_path']), 'bad_auth_path', 400)
        for key in ('username', 'password'):
            require(re.fullmatch(r'[A-Za-z0-9_-]{24,96}', phone[key]), 'bad_phone_credentials', 400)
        require(len(phone['username']) + len(phone['password']) + 1 <= 64, 'phone_credentials_too_long', 400)
        for line in phone['lines']:
            require(re.fullmatch(r'2[0-9]{2}', line) and line not in lines, 'duplicate_or_bad_line', 400)
            lines.add(line)
    softphones = config.get('softphones', [])
    require(isinstance(softphones, list) and len(softphones) <= 50, 'bad_softphones', 400)
    for client in softphones:
        require(isinstance(client, dict) and 'extension' in client
                and set(client) <= {'extension', 'room'}, 'bad_softphone_metadata', 400)
        extension = client['extension']
        require(isinstance(extension, str) and re.fullmatch(r'2[0-9]{2}', extension)
                and extension not in lines, 'duplicate_or_bad_softphone_extension', 400)
        lines.add(extension)
        room_name(client.get('room'))
    require(all(isinstance(number, str) and re.fullmatch(r'[0-9]{3,8}', number)
                for number in config['dial_allowlist']), 'bad_dial_allowlist', 400)
    require(isinstance(config.get('ring_allowlist', ['640']), list)
            and all(number == '640' or number in config['dial_allowlist']
                    for number in config.get('ring_allowlist', ['640'])), 'bad_ring_allowlist', 400)
    services = config.get('services', [])
    require(isinstance(services, list) and len(services) <= 128, 'bad_services', 400)
    service_numbers = set(lines)
    for entry in services:
        require(isinstance(entry, dict) and set(entry) == {'number', 'label', 'kind'},
                'bad_service_metadata', 400)
        number, label = entry['number'], entry['label']
        require(isinstance(number, str) and re.fullmatch(r'[0-9]{3,8}', number)
                and number not in service_numbers and number in config['dial_allowlist'],
                'duplicate_or_bad_service_number', 400)
        require(isinstance(label, str) and label == label.strip()
                and re.fullmatch(r'[A-Za-z0-9][ -~]{0,79}', label), 'bad_service_label', 400)
        require(isinstance(entry['kind'], str) and entry['kind'] in {
            'diagnostic', 'group', 'assistant', 'alert', 'radio', 'external', 'automation'},
            'bad_service_kind', 400)
        service_numbers.add(number)
    applications = config.get('applications', {})
    require(isinstance(applications, dict) and not set(applications) - {'enabled', 'home_assistant'}
            and type(applications.get('enabled', False)) is bool, 'bad_applications', 400)
    if 'home_assistant' in applications:
        require(applications.get('enabled') is True, 'applications_disabled', 400)
        app_module('phone_apps_ha').validate_config(applications['home_assistant'])
    paths = set()
    for phone in config['phones']:
        path = phone.get('apps_path')
        if applications.get('enabled') or path is not None:
            require(isinstance(path, str) and re.fullmatch(r'/apps/[a-f0-9]{64}', path)
                    and path not in paths, 'bad_apps_path', 400)
            paths.add(path)
    return config


class AMI:
    """One bounded, event-filtered AMI session per fresh operation."""
    def __init__(self, config):
        self.config = config

    def __enter__(self):
        self.sock = socket.create_connection((self.config['host'], self.config['port']), timeout=5)
        self.file = self.sock.makefile('rb')
        try:
            require(self.file.readline(1024).startswith(b'Asterisk Call Manager/'), 'bad_ami_banner', 503)
            self.action('Login', Username=self.config['username'], Secret=self.config['secret'], Events='off')
        except Exception:
            self.file.close()
            self.sock.close()
            raise
        return self

    def __exit__(self, *_):
        self.file.close()
        self.sock.close()

    def action(self, name, complete=None, **headers):
        action_id = secrets.token_hex(12)
        data = {'Action': name, 'ActionID': action_id, **headers}
        require(all(not re.search(r'[\r\n\x00]', str(value)) for value in data.values()), 'invalid_ami_header', 400)
        self.sock.sendall(('\r\n'.join(f'{key}: {value}' for key, value in data.items()) + '\r\n\r\n').encode())
        results, total, deadline = [], 0, time.monotonic() + 8
        while time.monotonic() < deadline:
            fields = {}
            while True:
                line = self.file.readline(16385)
                total += len(line)
                require(line and len(line) <= 16384 and total <= 1024 * 1024, 'invalid_ami_response', 503)
                if line in (b'\r\n', b'\n'):
                    break
                key, separator, value = line.decode('utf-8', 'replace').partition(':')
                if separator:
                    fields[key] = value.strip()
            if fields.get('ActionID') != action_id:
                continue
            if fields.get('Response') == 'Error':
                # Preserve the public refusal code while exposing only bounded,
                # non-secret diagnostics to isolated runtime tests. Never copy
                # raw AMI messages, response fields, or request headers.
                error = Refused('ami_action_rejected', 503)
                error.ami_action = name if re.fullmatch(r'[A-Za-z0-9]{1,64}', name) else 'unknown'
                message = fields.get('Message', '').strip().lower()
                error.ami_error = {
                    'authentication failed': 'authentication_failed',
                    'login already in use': 'login_in_use',
                    'permission denied': 'permission_denied',
                    'no such channel': 'no_such_channel',
                    'no endpoints found': 'no_endpoints',
                    'could not get endpoints': 'endpoints_unavailable',
                }.get(message, 'unknown_action' if message.startswith('invalid/unknown command:') else 'other')
                raise error
            results.append(fields)
            if complete and fields.get('Event') == complete:
                return results
            if not complete and fields.get('Response') in ('Success', 'Follows'):
                return results
        raise Refused('ami_timeout', 503)

    def snapshot(self):
        endpoints = self.action('PJSIPShowEndpoints', complete='EndpointListComplete')
        channels = self.action('CoreShowChannels', complete='CoreShowChannelsComplete')
        return ({item['ObjectName']: item.get('DeviceState', 'Unavailable') for item in endpoints
                 if item.get('Event') == 'EndpointList'},
                [item for item in channels if item.get('Event') == 'CoreShowChannel'])


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise Refused('phone_redirect_refused', 502)


def execute(phone, uri, priority=0):
    root = ET.Element('CiscoIPPhoneExecute')
    ET.SubElement(root, 'ExecuteItem', {'Priority': str(priority), 'URL': uri})
    payload = parse.urlencode({'XML': ET.tostring(root, encoding='unicode')}).encode()
    auth = base64.b64encode((phone['username'] + ':' + phone['password']).encode()).decode()
    req = request.Request('http://' + phone['ipv4'] + '/CGI/Execute', data=payload,
                          headers={'Authorization': 'Basic ' + auth,
                                   'Content-Type': 'application/x-www-form-urlencoded'})
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=10) as response:
        data = response.read(65537)
    require(len(data) <= 65536 and b'<!' not in data, 'invalid_phone_response', 502)
    document = ET.fromstring(data)
    items = document.findall('ResponseItem')
    code = None
    if document.tag == 'CiscoIPPhoneError' and not len(document):
        code = document.get('Number')
    elif (document.tag == 'CiscoIPPhoneResponse' and len(items) == 1
          and len(document) == 1 and items[0].get('URL') == uri):
        code = items[0].get('Status')
    if code in {'1', '2', '3', '4', '5', '6'}:
        raise PhoneCommandRejected(int(code))
    require(document.tag == 'CiscoIPPhoneResponse' and len(items) == 1
            and items[0].get('Status') == '0' and items[0].get('URL') == uri,
            'phone_did_not_confirm_action', 502)


def own_channels(phone, channels):
    return [item for item in channels if any(re.fullmatch(r'PJSIP/' + re.escape(line) + r'-[0-9a-f]+',
                                                        item.get('Channel', '')) for line in phone['lines'])]


def text(value, limit):
    require(isinstance(value, str) and 1 <= len(value) <= limit
            and not any(ord(char) < 32 and char not in '\n\t' for char in value), 'bad_text', 400)
    return value


class Gateway:
    def __init__(self, config, state_directory):
        self.config = validate(config)
        self.phones = {phone['id']: phone for phone in config['phones']}
        self.lock = threading.RLock()
        self.resources = {}
        self.stopped = threading.Event()
        self.auto_attempts = set()
        self.last_pbx_start = None
        self.dnd_pending = set(self.phones)
        directory = Path(state_directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / 'gateway.sqlite3', check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS preferences (phone TEXT PRIMARY KEY, dnd INTEGER, autoanswer INTEGER)')
        self.db.execute('CREATE TABLE IF NOT EXISTS operations (key TEXT PRIMARY KEY, digest TEXT, response TEXT, created REAL)')
        self.db.execute('''CREATE TABLE IF NOT EXISTS alerts (
            id TEXT PRIMARY KEY, phone TEXT, line TEXT, kind TEXT, state TEXT,
            reason TEXT, created REAL, seen INTEGER, stop_requested INTEGER)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS intercoms (
            root TEXT PRIMARY KEY, state TEXT, created REAL)''')
        # Never resume answering a partially established intercom after restart.
        self.db.execute("UPDATE intercoms SET state='stopping' WHERE state='starting'")
        for phone in self.phones:
            self.db.execute('INSERT OR IGNORE INTO preferences VALUES (?, 0, 0)', (phone,))
        self.db.commit()

        self.apps = None
        if config.get('applications', {}).get('enabled'):
            backend_config = config['applications'].get('home_assistant')
            backend = (app_module('phone_apps_ha').HomeAssistantApps(backend_config)
                       if backend_config else None)
            self.apps = app_module('phone_apps').PhoneApps(self, backend)

    def preference(self, phone_id):
        row = self.db.execute('SELECT dnd,autoanswer FROM preferences WHERE phone=?', (phone_id,)).fetchone()
        return {'dnd': bool(row[0]), 'autoanswer': bool(row[1])}

    def sync_dnd(self, ami):
        # Restore after an Asterisk restart; never rely on ephemeral AstDB alone.
        for phone in self.phones.values():
            value = '1' if self.preference(phone['id'])['dnd'] else '0'
            for line in phone['lines']:
                ami.action('DBPut', Family='cisco-dnd', Key=line, Val=value)

    def alerts(self):
        columns = ('id', 'phone', 'line', 'kind', 'state', 'reason', 'created', 'seen', 'stop_requested')
        return [dict(zip(columns, row)) for row in self.db.execute(
            'SELECT id,phone,line,kind,state,reason,created,seen,stop_requested FROM alerts ORDER BY created')]

    @staticmethod
    def alert_channels(alert, channels):
        roots = {'cisco-alert-' + alert['id'] + suffix for suffix in ('-a', '-b')}
        related = [item for item in channels if item.get('Uniqueid') in roots or item.get('Linkedid') in roots]
        for item in related:
            name = item.get('Channel', '')
            require(re.fullmatch(r'Local/' + re.escape(alert['line']) + r'@cisco-alert-[0-9a-f]+;[12]', name)
                    or re.fullmatch(r'PJSIP/' + re.escape(alert['line']) + r'-[0-9a-f]+', name),
                    'unexpected_alert_channel')
            require(item.get('Uniqueid'), 'alert_channel_identity_missing')
        return related

    def is_alert_channel(self, channel):
        # Retained records also suppress auto-answer for a late-arriving channel
        # after an ambiguous originate or a gateway restart.
        identities = {channel.get('Uniqueid'), channel.get('Linkedid')}
        return any('cisco-alert-' + alert['id'] + suffix in identities
                   for alert in self.alerts() for suffix in ('-a', '-b'))

    def stop_alert_channels(self, ami, alert):
        _, channels = ami.snapshot()
        for item in self.alert_channels(alert, channels):
            # Hanging up one Local leg can remove all its peers. Re-read before
            # each action and never act on a recycled name or another call.
            _, current = ami.snapshot()
            if not any(candidate.get('Uniqueid') == item['Uniqueid']
                       and candidate.get('Channel') == item['Channel']
                       for candidate in self.alert_channels(alert, current)):
                continue
            try:
                ami.action('Hangup', Channel=item['Channel'], Cause='16')
            except Refused:
                _, current = ami.snapshot()
                require(not any(candidate.get('Uniqueid') == item['Uniqueid']
                                for candidate in self.alert_channels(alert, current)),
                        'alert_hangup_not_confirmed', 503)

    def reconcile_alerts(self, ami, channels):
        for alert in self.alerts():
            related = self.alert_channels(alert, channels)
            if related:
                if alert['stop_requested'] or alert['state'] == 'ended':
                    # A delayed originate must never revive a terminal alert.
                    # Persist cancellation before I/O so an ambiguous Hangup or
                    # gateway restart continues stopping the same owned legs.
                    if not alert['stop_requested']:
                        self.db.execute('UPDATE alerts SET state=?,stop_requested=1 WHERE id=?',
                                        ('stopping', alert['id']))
                        self.db.commit()
                    self.stop_alert_channels(ami, alert)
                    state = 'stopping'
                else:
                    state = 'ringing' if any(item.get('ChannelStateDesc') == 'Ringing'
                                             for item in related) else 'starting'
                self.db.execute('UPDATE alerts SET state=?,seen=1 WHERE id=?', (state, alert['id']))
            elif alert['state'] != 'ended' and (alert['seen'] or time.time() - alert['created'] > 15):
                reason = 'stopped' if alert['stop_requested'] else ('call_ended' if alert['seen'] else 'not_observed')
                self.db.execute('UPDATE alerts SET state=?,reason=? WHERE id=?', ('ended', reason, alert['id']))
        self.db.commit()

    def latest_alert(self, phone_id):
        found = [alert for alert in self.alerts() if alert['phone'] == phone_id]
        return ({key: found[-1][key] for key in ('id', 'line', 'kind', 'state', 'reason')} if found else None)

    def listing(self):
        with self.lock, AMI(self.config['ami']) as ami:
            endpoints, channels = ami.snapshot()
            self.reconcile_alerts(ami, channels)
            self.reconcile_intercoms(ami, channels)
            return {'ami_fresh': True, 'observed_at': time.time(),
                    'dial_allowlist': self.config['dial_allowlist'],
                    'ring_allowlist': self.config.get('ring_allowlist', ['640']),
                    'services': [{key: entry[key] for key in ('number', 'label', 'kind')}
                                 for entry in self.config.get('services', [])],
                    'softphones': [
                        {'extension': client['extension'], 'room': client.get('room'),
                         'online': endpoints.get(client['extension']) not in (None, 'Unavailable', 'Invalid')}
                        for client in self.config.get('softphones', [])],
                    'phones': [
                {'id': phone['id'], 'lines': phone['lines'], 'room': phone.get('room'),
                 **self.preference(phone['id']),
                 'dnd_pending': phone['id'] in self.dnd_pending,
                 'alert': self.latest_alert(phone['id']),
                 'online': all(endpoints.get(line) not in (None, 'Unavailable', 'Invalid') for line in phone['lines']),
                 'channels': [{'name': item['Channel'], 'uniqueid': item.get('Uniqueid'),
                               'state': item.get('ChannelStateDesc'), 'line': item['Channel'].split('/')[1].split('-')[0]}
                              for item in own_channels(phone, channels)],
                 'capabilities': ['dial', 'intercom', 'answer', 'hangup', 'speaker_toggle', 'message', 'image', 'dnd', 'autoanswer', 'ring', 'alert', 'alert_stop']}
                for phone in self.phones.values()]}

    @staticmethod
    def intercom_channels(root, channels):
        roots = {root, root + '-b'}
        return [item for item in channels if roots.intersection((item.get('Uniqueid'), item.get('Linkedid')))]

    def is_intercom_channel(self, channel):
        return any(self.db.execute('SELECT 1 FROM intercoms WHERE root=? OR root || ? = ?',
                                   (identity, '-b', identity)).fetchone()
                   for identity in (channel.get('Uniqueid'), channel.get('Linkedid')) if identity)

    def stop_intercom(self, ami, root):
        _, channels = ami.snapshot()
        for candidate in self.intercom_channels(root, channels):
            _, current = ami.snapshot()
            if any(item.get('Channel') == candidate.get('Channel')
                   and item.get('Uniqueid') == candidate.get('Uniqueid')
                   for item in self.intercom_channels(root, current)):
                try:
                    ami.action('Hangup', Channel=candidate['Channel'], Cause='16')
                except Refused:
                    # Stopping one bridged leg can end the other after its
                    # fresh ownership check. CoreShowChannels can briefly
                    # retain a terminated channel while its cache catches up.
                    # Only poll for that identity's absence; never repeat
                    # Hangup against a potentially recycled channel name.
                    deadline = time.monotonic() + 2
                    while True:
                        _, current = ami.snapshot()
                        if not any(item.get('Uniqueid') == candidate.get('Uniqueid')
                                   for item in current):
                            break
                        require(time.monotonic() < deadline,
                                'intercom_hangup_not_confirmed', 503)
                        time.sleep(.05)

    def reconcile_intercoms(self, ami, channels):
        for root, state, created in self.db.execute('SELECT root,state,created FROM intercoms').fetchall():
            if state == 'starting' and time.time() - created > 35:
                self.db.execute("UPDATE intercoms SET state='stopping' WHERE root=?", (root,))
                self.db.commit()
                state = 'stopping'
            if state == 'stopping' and self.intercom_channels(root, channels):
                self.stop_intercom(ami, root)
            # Retain failed identities to catch a delayed asynchronous originate.
            if time.time() - created > 30 * 86400 and not self.intercom_channels(root, channels):
                self.db.execute('DELETE FROM intercoms WHERE root=?', (root,))
                self.db.commit()

    def intercom(self, ami, phone, line, destination, endpoints, channels):
        require(isinstance(destination, str) and destination in self.config['dial_allowlist'], 'number_not_allowed')
        targets = [item for item in self.phones.values() if destination in item['lines']]
        require(len(targets) == 1, 'intercom_requires_cisco_destination')
        target = targets[0]
        require(target['id'] != phone['id'], 'intercom_requires_two_phones')
        for item in (phone, target):
            require(all(endpoints.get(number) not in (None, 'Unavailable', 'Invalid') for number in item['lines']),
                    'phone_unavailable', 503)
            require(not own_channels(item, channels) and
                    all(endpoints.get(number) == 'Not in use' for number in item['lines']), 'phone_busy')
            require(not self.preference(item['id'])['dnd'], 'do_not_disturb')
            require(not any(row['phone'] == item['id'] and row['state'] != 'ended' for row in self.alerts()),
                    'alert_already_active')
        root = 'cisco-intercom-' + secrets.token_hex(16)
        self.db.execute('INSERT INTO intercoms VALUES (?, ?, ?)', (root, 'starting', time.time()))
        self.db.commit()
        try:
            # Both source and destination use Dial(...,i). A direct PJSIP
            # Originate can follow a source redirect and discard its assigned ID.
            # Allow Local optimization so the final physical legs share a bridge;
            # their Linkedid retains one of these explicit operation identities.
            ami.action('Originate', Channel='Local/' + line + '@cisco-intercom',
                       ChannelId=root, OtherChannelId=root + '-b',
                       Application='Dial', Data='PJSIP/' + destination + ',8,i',
                       CallerID=(phone.get('room') or line) + ' <' + line + '>',
                       Timeout='8000', Async='true')
            deadline, attempted, seen = time.monotonic() + 12, set(), set()
            while time.monotonic() < deadline:
                _, current = ami.snapshot()
                related = self.intercom_channels(root, current)
                legs = []
                for item, number in ((phone, line), (target, destination)):
                    owned = own_channels(item, current)
                    require(len(owned) <= 1 and all(ch in related for ch in owned), 'intercom_call_changed', 503)
                    if not owned:
                        require(number not in seen, 'intercom_call_ended', 503)
                        continue
                    ch = owned[0]
                    require(ch['Channel'].startswith('PJSIP/' + number + '-') and ch.get('Uniqueid'),
                            'intercom_call_changed', 503)
                    seen.add(number)
                    legs.append(ch)
                    if ch.get('ChannelStateDesc') == 'Ringing' and number not in attempted:
                        # One speaker-key press on this isolated incoming call.
                        # Never toggle a speaker on an already answered call.
                        attempted.add(number)
                        execute(item, 'Key:Speaker')
                        break  # Re-read both phones before another key press.
                else:
                    if (len(legs) == 2 and all(ch.get('ChannelStateDesc') == 'Up' for ch in legs)
                            and legs[0].get('BridgeId') and legs[0]['BridgeId'] == legs[1].get('BridgeId')):
                        self.db.execute("UPDATE intercoms SET state='connected' WHERE root=?", (root,))
                        self.db.commit()
                        return {'line': line, 'number': destination, 'connection_state': 'connected',
                                'intercom_id': root, 'speaker_requested': len(attempted) == 2}
                time.sleep(0.2)
            raise Refused('intercom_setup_timeout', 503)
        except Exception as error:
            self.db.execute("UPDATE intercoms SET state='stopping' WHERE root=?", (root,))
            self.db.commit()
            try:
                self.stop_intercom(ami, root)
            except Exception:
                pass  # The background reconciler retains and retries cleanup.
            refused = Refused('intercom_not_connected_check_status', 503)
            for key in ('ami_action', 'ami_error'):
                if hasattr(error, key):
                    setattr(refused, key, getattr(error, key))
            raise refused from None

    def resource(self, phone, data, content_type):
        with self.lock:
            now = time.time()
            self.resources = {key: value for key, value in self.resources.items() if value[0] > now}
            require(len(self.resources) < 64, 'too_many_pending_messages', 429)
            path = '/content/' + secrets.token_urlsafe(24)
            self.resources[(phone['id'], path)] = (now + 120, data, content_type)
        return f'http://{self.config["bind_ipv4"]}:{phone["port"]}{path}'

    def notify(self, phone, action, body):
        title = text(body.get('title', 'Home phone'), 48)
        if action == 'message':
            root = ET.Element('CiscoIPPhoneText')
            ET.SubElement(root, 'Title').text = title
            ET.SubElement(root, 'Prompt').text = 'Press Exit to return'
            ET.SubElement(root, 'Text').text = text(body.get('text'), 2000)
        else:
            from PIL import Image, ImageOps
            try:
                encoded = body['image_base64']
                require(isinstance(encoded, str) and len(encoded) <= MAX_BODY - 1024, 'image_too_large', 413)
                raw = base64.b64decode(encoded, validate=True)
                require(len(raw) <= 2 * 1024 * 1024, 'image_too_large', 413)
                image = Image.open(io.BytesIO(raw))
                require(image.format in ('PNG', 'JPEG') and image.width * image.height <= 16_000_000,
                        'unsupported_image', 400)
                image = ImageOps.exif_transpose(image).convert('RGB')
                image = ImageOps.contain(image, (298, 156))
                output = io.BytesIO()
                image.info.clear()
                image.save(output, format='PNG')
            except (ValueError, OSError, KeyError):
                raise Refused('invalid_image', 400) from None
            image_url = self.resource(phone, output.getvalue(), 'image/png')
            root = ET.Element('CiscoIPPhoneImageFile')
            ET.SubElement(root, 'Title').text = title
            ET.SubElement(root, 'Prompt').text = 'Press Exit to return'
            ET.SubElement(root, 'LocationX').text = '-1'
            ET.SubElement(root, 'LocationY').text = '-1'
            ET.SubElement(root, 'URL').text = image_url
        key = ET.SubElement(root, 'SoftKeyItem')
        ET.SubElement(key, 'Name').text = 'Exit'
        ET.SubElement(key, 'URL').text = 'SoftKey:Exit'
        ET.SubElement(key, 'Position').text = '1'
        uri = self.resource(phone, ET.tostring(root, encoding='utf-8', xml_declaration=True), 'text/xml; charset=utf-8')
        # Unlike Key:/Dial:, an HTTP URI obeys Priority=2 (idle-only).
        execute(phone, uri, priority=2)

    def mutate(self, phone_id, action, body):
        require(phone_id in self.phones, 'unknown_phone', 404)
        require(action in ('dial', 'intercom', 'answer', 'hangup', 'speaker', 'message', 'image', 'dnd', 'autoanswer', 'ring', 'alert', 'alert_stop'),
                'unknown_action', 404)
        require(isinstance(body, dict), 'json_object_required', 400)
        key = body.get('idempotency_key')
        require(isinstance(key, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', key), 'idempotency_key_required', 400)
        digest = hashlib.sha256(json.dumps([phone_id, action, body], sort_keys=True).encode()).hexdigest()
        with self.lock:
            previous = self.db.execute('SELECT digest,response FROM operations WHERE key=?', (key,)).fetchone()
            if previous:
                require(previous[0] == digest, 'idempotency_key_conflict')
                require(previous[1] is not None, 'previous_action_outcome_unknown')
                return json.loads(previous[1])
            self.db.execute('DELETE FROM operations WHERE created < ?', (time.time() - 30 * 86400,))
            self.db.execute('INSERT INTO operations VALUES (?, ?, NULL, ?)', (key, digest, time.time()))
            self.db.commit()  # Persist intent BEFORE any phone or AMI mutation.
            try:
                result = self._mutate(self.phones[phone_id], action, body)
                result.update(status='accepted', action_id=key)
            except Refused as error:
                # Keep ambiguous I/O outcomes claimed; a retry must not dial again.
                if error.status < 500:
                    self.db.execute('DELETE FROM operations WHERE key=?', (key,))
                    self.db.commit()
                raise
            self.db.execute('UPDATE operations SET response=? WHERE key=?', (json.dumps(result), key))
            self.db.commit()
            return result

    def _mutate(self, phone, action, body):
        line = body.get('line', phone['id'])
        require(line in phone['lines'], 'line_does_not_belong_to_phone', 400)
        if action in ('dnd', 'autoanswer'):
            require(type(body.get('enabled')) is bool, 'enabled_boolean_required', 400)
            # Persist desired state before AMI writes. A partial two-line update
            # remains pending and is reconciled without requiring a PBX restart.
            self.db.execute('UPDATE preferences SET ' + action + '=? WHERE phone=?', (int(body['enabled']), phone['id']))
            self.db.commit()
            if action == 'dnd':
                self.dnd_pending.add(phone['id'])
                with AMI(self.config['ami']) as ami:
                    for endpoint in phone['lines']:
                        ami.action('DBPut', Family='cisco-dnd', Key=endpoint, Val='1' if body['enabled'] else '0')
                self.dnd_pending.discard(phone['id'])
            return {action: body['enabled']}
        with AMI(self.config['ami']) as ami:
            endpoints, channels = ami.snapshot()
            self.reconcile_alerts(ami, channels)
            if action == 'intercom':
                return self.intercom(ami, phone, line, body.get('number'), endpoints, channels)
            if action == 'alert_stop':
                alert_id = body.get('alert_id')
                require(isinstance(alert_id, str) and re.fullmatch(r'[0-9a-f]{32}', alert_id), 'bad_alert_id', 400)
                matches = [alert for alert in self.alerts() if alert['id'] == alert_id and alert['phone'] == phone['id']]
                require(len(matches) == 1, 'alert_not_found', 404)
                alert = matches[0]
                if alert['state'] != 'ended':
                    self.db.execute('UPDATE alerts SET state=?,stop_requested=1 WHERE id=?', ('stopping', alert_id))
                    self.db.commit()
                    self.stop_alert_channels(ami, alert)
                    _, current = ami.snapshot()
                    self.reconcile_alerts(ami, current)
                return {'alert_id': alert_id, 'state': next(row['state'] for row in self.alerts() if row['id'] == alert_id)}
            require(all(endpoints.get(endpoint) not in (None, 'Unavailable', 'Invalid') for endpoint in phone['lines']),
                    'phone_unavailable', 503)
            owned = own_channels(phone, channels)
            if action in ('message', 'image'):
                require(not owned, 'phone_busy')
                self.notify(phone, action, body)
                return {'resource_ttl_seconds': 120}
            if action in ('dial', 'ring', 'alert'):
                require(not owned and all(endpoints.get(endpoint) == 'Not in use' for endpoint in phone['lines']), 'phone_busy')
                require(not self.preference(phone['id'])['dnd'], 'do_not_disturb')
                require(not any(row['phone'] == phone['id'] and row['state'] != 'ended' for row in self.alerts()),
                        'alert_already_active')
                if action == 'alert':
                    kind = body.get('kind')
                    require(kind in ('timer', 'alarm'), 'bad_alert_kind', 400)
                    alert_id = secrets.token_hex(16)
                    self.db.execute('INSERT INTO alerts VALUES (?, ?, ?, ?, ?, NULL, ?, 0, 0)',
                                    (alert_id, phone['id'], line, kind, 'starting', time.time()))
                    self.db.commit()  # Durable identity exists before the async originate.
                    try:
                        ami.action('Originate', Channel='Local/' + line + '@cisco-alert/n',
                                   Application='WaitForSignal', Data='cisco-alert-' + alert_id,
                                   Timeout='5000', Async='true', CallerID=kind.title() + ' <640>',
                                   ChannelId='cisco-alert-' + alert_id + '-a',
                                   OtherChannelId='cisco-alert-' + alert_id + '-b',
                                   Variable='__CISCO_ALERT_ID=' + alert_id)
                    except Exception:
                        self.db.execute('UPDATE alerts SET state=? WHERE id=?', ('unknown', alert_id))
                        self.db.commit()
                        raise
                    return {'alert_id': alert_id, 'line': line, 'kind': kind, 'ring_timeout_seconds': None}
                if action == 'ring':
                    destination = body.get('destination', '640')
                    require(destination in self.config.get('ring_allowlist', ['640']), 'ring_destination_not_allowed', 400)
                    ami.action('Originate', Channel='PJSIP/' + line, Context='home', Exten=destination, Priority='1',
                               Timeout='20000', Async='true', CallerID='Home alarm <640>')
                    return {'ring_timeout_seconds': 20, 'destination': destination}
                number = body.get('number')
                require(number in self.config['dial_allowlist'], 'number_not_allowed', 400)
                index = phone['lines'].index(line) + 1
                # The Dial URI has NO line-selector suffix. Select the real
                # line key first, then submit only a numeric dial sequence.
                execute(phone, 'Key:Line' + str(index))
                execute(phone, 'Dial:' + number)
                return {'line': line, 'number': number}
            if action == 'speaker':
                require(body.get('mode') == 'toggle', 'only_explicit_toggle_supported', 400)
                require(not owned, 'speaker_toggle_requires_idle_phone')
                execute(phone, 'Key:Speaker')
                return {'speaker_state': 'unknown', 'operation': 'toggle'}
            selected = [item for item in owned if item['Channel'].startswith('PJSIP/' + line + '-')]
            require(len(selected) == 1 and len(owned) == 1, 'exactly_one_selected_phone_channel_required')
            channel = selected[0]
            require(channel.get('Uniqueid'), 'channel_identity_missing')
            require(body.get('channel') == channel['Channel'] and body.get('uniqueid') == channel['Uniqueid'],
                    'channel_identity_changed')
            if action == 'answer':
                require(not self.is_alert_channel(channel), 'alert_requires_manual_answer_or_stop')
                require(channel.get('ChannelStateDesc') == 'Ringing', 'phone_not_ringing')
                execute(phone, 'Key:Line' + str(phone['lines'].index(line) + 1))
            else:
                for alert in self.alerts():
                    if channel in self.alert_channels(alert, channels):
                        self.db.execute('UPDATE alerts SET stop_requested=1 WHERE id=?', (alert['id'],))
                        self.db.commit()
                ami.action('Hangup', Channel=channel['Channel'], Cause='16')
            return {'line': line, 'channel': channel['Channel'], 'uniqueid': channel['Uniqueid']}

    def background(self):
        while not self.stopped.wait(2):
            try:
                with self.lock, AMI(self.config['ami']) as ami:
                    status = ami.action('CoreStatus')[0]
                    started = (status.get('CoreStartupDate'), status.get('CoreStartupTime'))
                    require(all(started), 'pbx_start_identity_missing', 503)
                    if started != self.last_pbx_start or self.dnd_pending:
                        self.sync_dnd(ami)
                        self.dnd_pending.clear()
                        self.last_pbx_start = started
                    endpoints, channels = ami.snapshot()
                    self.reconcile_alerts(ami, channels)
                    self.reconcile_intercoms(ami, channels)
                    active = {item.get('Uniqueid') for item in channels}
                    self.auto_attempts.intersection_update(active)
                    for phone in self.phones.values():
                        pref = self.preference(phone['id'])
                        owned = own_channels(phone, channels)
                        if not pref['autoanswer'] or pref['dnd'] or len(owned) != 1:
                            continue
                        item = owned[0]
                        if self.is_alert_channel(item) or self.is_intercom_channel(item):
                            continue
                        identity = item.get('Uniqueid')
                        if not identity or identity in self.auto_attempts or item.get('ChannelStateDesc') != 'Ringing':
                            continue
                        self.auto_attempts.add(identity)  # One attempt, including ambiguous timeout.
                        line = item['Channel'].split('/')[1].split('-')[0]
                        self.mutate(phone['id'], 'answer', {'line': line, 'channel': item['Channel'],
                                    'uniqueid': identity, 'idempotency_key': 'autoanswer:' + identity})
            except Exception:
                # No credentials, text, image data or raw remote responses in logs.
                pass


class Handler(BaseHTTPRequestHandler):
    server_version = 'CiscoHomeGateway'
    sys_version = ''
    protocol_version = 'HTTP/1.0'

    def log_message(self, *_):
        pass  # Authentication callback URLs contain credentials.

    def setup(self):
        super().setup()
        self.connection.settimeout(12)

    def reply(self, status, data, content_type='application/json'):
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def body(self, limit):
        require('Transfer-Encoding' not in self.headers and len(self.headers.get_all('Content-Length', [])) <= 1,
                'invalid_framing', 400)
        length = self.headers.get('Content-Length', '0')
        require(length.isdigit() and int(length) <= limit, 'request_too_large', 413)
        data = self.rfile.read(int(length))
        require(len(data) == int(length), 'truncated_request', 400)
        return data

    def handle_request(self):
        gateway, phone = self.server.gateway, self.server.phone
        source = self.client_address[0]
        if phone:
            require(source == phone['ipv4'], 'forbidden', 403)
            target = parse.urlsplit(self.path)
            require(not target.scheme and not target.netloc and not target.fragment, 'bad_target', 400)
            if target.path == phone['auth_path']:
                if self.command == 'GET':
                    require(not self.body(0), 'bad_auth_body', 400)
                    encoded = target.query
                else:
                    require(not target.query and self.headers.get('Content-Type') == 'application/x-www-form-urlencoded',
                            'bad_auth_content_type', 400)
                    encoded = self.body(4096).decode('ascii')
                pairs = parse.parse_qsl(encoded, keep_blank_values=True, strict_parsing=True)
                values = dict(pairs)
                require(len(pairs) == 3 and set(values) == {'UserID', 'Password', 'devicename'}, 'bad_auth_fields', 403)
                allowed = (constant(values['UserID'], phone['username']) and constant(values['Password'], phone['password'])
                           and constant(values['devicename'], phone['device']))
                return self.reply(200, b'AUTHORIZED' if allowed else b'UNAUTHORIZED', 'text/plain')
            apps_path = phone.get('apps_path')
            if gateway.apps and apps_path and (target.path == apps_path or target.path.startswith(apps_path + '/')):
                require(self.command == 'GET' and len(self.path) <= 4096 and not self.body(0), 'bad_app_request', 400)
                # HA schedule calls read gateway state in a separate request.
                # Do not hold the gateway lock while invoking the application.
                try:
                    data, content_type = gateway.apps.handle(phone['id'], target.path[len(apps_path):] or '/', target.query)
                except Exception as error:
                    # Return a native error page, without disclosing exceptions,
                    # HA response bodies, credentials or callback URLs.
                    data, content_type = gateway.apps.error(getattr(error, 'reason', 'operation_failed'))
                return self.reply(200, data, content_type)
            require(self.command == 'GET' and not target.query and not self.body(0), 'not_found', 404)
            # Separate resource lock would deadlock an Execute call awaiting its
            # phone callback; dict lookups are atomic and resources immutable.
            item = gateway.resources.get((phone['id'], target.path))
            require(item and item[0] > time.time(), 'expired_or_unknown_resource', 404)
            return self.reply(200, item[1], item[2])
        require(source in gateway.config['api_clients'], 'forbidden', 403)
        require(len(self.headers.get_all('Authorization', [])) == 1
                and constant(self.headers.get('Authorization', ''), 'Bearer ' + gateway.config['api_token']), 'forbidden', 403)
        if self.command == 'GET':
            require(self.path == '/v1/phones' and not self.body(0), 'not_found', 404)
            return self.reply(200, gateway.listing())
        match = re.fullmatch(r'/v1/phones/(2[0-9]{2})/([a-z]+(?:_[a-z]+)*)', self.path)
        require(match, 'not_found', 404)
        require(self.headers.get('Content-Type', '').split(';')[0] == 'application/json', 'json_required', 400)
        try:
            data = json.loads(self.body(MAX_BODY))
        except (ValueError, UnicodeError):
            raise Refused('invalid_json', 400) from None
        return self.reply(200, gateway.mutate(match[1], match[2], data))

    def do_GET(self):
        try:
            self.handle_request()
        except Refused as error:
            self.reply(error.status, {'status': 'refused', 'reason': error.reason})
        except Exception:
            self.reply(503, {'status': 'unknown', 'reason': 'operation_failed_do_not_retry_blindly'})

    do_POST = do_GET


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--state-directory', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    config_path = Path(args.config)
    # Kubernetes projects Secrets using symlinks. The read-only Secret volume,
    # restricted pod identity and fixed operator-supplied path are the boundary.
    require(config_path.is_file() and config_path.stat().st_size <= 128 * 1024, 'invalid_config_file', 400)
    gateway = Gateway(json.loads(config_path.read_text()), args.state_directory)
    servers = []
    for phone in [None, *gateway.phones.values()]:
        server = ThreadingHTTPServer((gateway.config['bind_ipv4'], phone['port'] if phone else gateway.config['api_port']), Handler)
        server.daemon_threads = True
        server.gateway, server.phone = gateway, phone
        servers.append(server)
    threading.Thread(target=gateway.background, daemon=True).start()
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        gateway.stopped.set()
        for server in servers:
            server.shutdown()


if __name__ == '__main__':
    main()
