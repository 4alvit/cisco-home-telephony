"""Native Cisco XML services for a source-restricted, per-phone HTTP listener.

This module never accepts a phone identity, URL, entity ID or service name from
the handset. The listener supplies the physical phone identity. Start and edit
actions require a short-lived, single-use confirmation; voice Stop targets the
exact session shown on an explicitly selected Stop link.
"""
import copy
import math
import re
import secrets
import threading
import time
from urllib.parse import parse_qsl, urlencode
import xml.etree.ElementTree as ET


MIME = 'text/xml; charset=utf-8'
PAGE_SIZE = 20
TOKEN_TTL = 90
MAX_TOKENS_PER_PHONE = 24
DAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
ALERT_STATES = frozenset(('starting', 'ringing', 'stopping', 'ended', 'unknown'))


class AppError(Exception):
    def __init__(self, reason, status=400):
        self.reason, self.status = reason, status
        super().__init__(reason)


def require(value, reason, status=400):
    if not value:
        raise AppError(reason, status)


def safe(value, limit=80):
    """Bound display strings and strip XML 1.0 control/surrogate characters."""
    return ''.join(char for char in str(value) if char in '\n\t' or
                   32 <= ord(char) <= 0xD7FF or 0xE000 <= ord(char) <= 0xFFFD)[:limit]


def xml(root):
    return ET.tostring(root, encoding='utf-8', xml_declaration=True), MIME


def child(root, name, value):
    ET.SubElement(root, name).text = safe(value, 2048)


class PhoneApps:
    def __init__(self, gateway, backend=None, clock=time.monotonic):
        self.gateway, self.backend, self.clock = gateway, backend, clock
        self.tokens = {}
        self.token_lock = threading.Lock()

    def url(self, phone_id, route='/', **params):
        phone = self.gateway.phones[phone_id]
        base = f'http://{self.gateway.config["bind_ipv4"]}:{phone["port"]}'
        base += phone['apps_path'].rstrip('/')
        value = base + (route if route != '/' else '')
        return value + ('?' + urlencode(params) if params else '')

    def softkey(self, root, name, url, position):
        key = ET.SubElement(root, 'SoftKeyItem')
        child(key, 'Name', name)
        child(key, 'URL', url)
        child(key, 'Position', position)

    def navigation(self, root, phone_id, line, select=False, submit=False):
        if select:
            self.softkey(root, 'Select', 'SoftKey:Select', 1)
        if submit:
            self.softkey(root, 'Submit', 'SoftKey:Submit', 1)
        self.softkey(root, 'Home', self.url(phone_id, line=line), 3)
        self.softkey(root, 'Exit', 'SoftKey:Exit', 4)

    def menu(self, phone_id, line, title, entries, prompt='Select an item'):
        require(len(entries) <= 32, 'menu_too_large', 503)
        root = ET.Element('CiscoIPPhoneMenu')
        child(root, 'Title', safe(title, 32))
        child(root, 'Prompt', safe(prompt, 80))
        for name, url in entries:
            item = ET.SubElement(root, 'MenuItem')
            child(item, 'Name', safe(name, 64))
            child(item, 'URL', url)
        self.navigation(root, phone_id, line, select=True)
        return xml(root)

    def text(self, phone_id, line, title, value, *, refresh=None):
        root = ET.Element('CiscoIPPhoneText')
        child(root, 'Title', safe(title, 32))
        child(root, 'Prompt', 'Refresh to check current status' if refresh else 'Press Home or Exit')
        child(root, 'Text', safe(value, 1900))
        if refresh:
            self.softkey(root, 'Refresh', self.url(phone_id, refresh, line=line), 1)
        self.navigation(root, phone_id, line)
        return xml(root)

    @staticmethod
    def alert_summary(alert, *, stale=False):
        if not alert:
            return 'Alert: None'
        label = 'Last observed alert' if stale else 'Last alert' if alert['state'] == 'ended' else 'Alert'
        return f'{label}: {alert["kind"]} on line {alert["line"]}, {alert["state"]}'

    @staticmethod
    def error(reason):
        # Never reflect backend errors, URLs, credentials or raw user input.
        messages = {
            'expired_confirmation': 'Confirmation expired or already used. Open the menu and try again.',
            'invalid_line': 'That line does not belong to this phone.',
            'bad_minutes': 'Enter a timer duration from 1 to 1440 minutes.',
            'bad_time': 'Enter time as HHMM, from 0000 to 2359.',
            'bad_days': 'Enter days 1 to 7, Monday to Sunday, with no repeats. Example: 12345.',
            'phone_busy': 'The phone is busy. Finish the current call first.',
            'phone_unavailable': 'The phone is not registered. Try again after it reconnects.',
            'do_not_disturb': 'Do Not Disturb is on. Turn it off before calling.',
            'backend_unavailable': 'Home Assistant is not configured or is unavailable.',
            'camera_preview_unavailable': 'Camera preview is disabled. Use Snapshot instead.',
            'phone_focus_unavailable': 'Camera preview needs both phone lines to be idle and voice cleanup confirmed.',
            'line_does_not_own_schedule': 'This schedule belongs to the other line. Select that line first.',
            'ha_timer_replace_required': 'A timer already exists. Cancel it before starting a new one.',
            'ha_request_outcome_unknown': 'Home Assistant did not confirm the result. Check current status before trying again.',
            'ha_change_not_confirmed': 'Home Assistant did not confirm the change. Check current status before trying again.',
            'alert_stop_not_confirmed': 'The alert Stop result is unconfirmed. Open Phone Status and check the alert before trying again.',
            'voice_control_unavailable': 'Voice control is not configured for this phone.',
            'voice_phone_busy': 'A voice session owns the phone audio, or its cleanup is unconfirmed. Open Voice Assistant for status and Stop guidance before trying this action again.',
            'voice_response_invalid': 'Voice status is unconfirmed. Check status before any new attempt.',
            'voice_request_failed': 'The voice request was not confirmed. Check status; do not start again blindly.',
            'not_configured': 'No alarm has been configured. Set its time and days first.',
        }
        root = ET.Element('CiscoIPPhoneText')
        child(root, 'Title', 'Action not completed')
        child(root, 'Prompt', 'Exit and reopen Services')
        child(root, 'Text', messages.get(reason,
              'The request could not be completed. Check the phone and Home Assistant status. '
              'Do not repeat a call or alert blindly.'))
        key = ET.SubElement(root, 'SoftKeyItem')
        child(key, 'Name', 'Exit'); child(key, 'URL', 'SoftKey:Exit'); child(key, 'Position', '1')
        return xml(root)

    def form(self, phone_id, line, title, operation, fields):
        require(1 <= len(fields) <= 5, 'bad_form', 503)
        root = ET.Element('CiscoIPPhoneInput')
        child(root, 'Title', title)
        child(root, 'Prompt', 'Submit to review; no change yet')
        child(root, 'URL', self.url(phone_id, '/prepare', line=line, op=operation))
        for label, key, default, flags in fields:
            item = ET.SubElement(root, 'InputItem')
            child(item, 'DisplayName', label)
            child(item, 'QueryStringParam', key)
            child(item, 'DefaultValue', default)
            child(item, 'InputFlags', flags)
        self.navigation(root, phone_id, line, submit=True)
        return xml(root)

    def issue(self, phone_id, line, kind, action, payload, description):
        now = self.clock()
        with self.token_lock:
            self.tokens = {key: row for key, row in self.tokens.items() if row['expires'] > now}
            owned = [key for key, row in self.tokens.items() if row['phone'] == phone_id]
            if len(owned) >= MAX_TOKENS_PER_PHONE:
                for key in owned[:len(owned) - MAX_TOKENS_PER_PHONE + 1]:
                    del self.tokens[key]
            token = secrets.token_hex(16)
            self.tokens[token] = {'phone': phone_id, 'line': line, 'kind': kind,
                                  'action': action, 'payload': copy.deepcopy(payload),
                                  'expires': now + TOKEN_TTL, 'description': description}
        return self.menu(phone_id, line, 'Confirm action', [
            ('Confirm: ' + description, self.url(phone_id, '/apply', token=token)),
            ('Cancel', self.url(phone_id, line=line))], f'Line {line}; valid for {TOKEN_TTL} seconds')

    def consume(self, phone_id, token):
        require(re.fullmatch(r'[0-9a-f]{32}', token or ''), 'expired_confirmation', 409)
        with self.token_lock:
            row = self.tokens.get(token)
            require(row and row['phone'] == phone_id and row['expires'] > self.clock(),
                    'expired_confirmation', 409)
            del self.tokens[token]  # Claim before any network I/O, even if it fails.
            return row

    def schedule(self, phone_id, line, operation='status', **params):
        require(self.backend is not None, 'backend_unavailable', 503)
        value = self.backend.schedule(phone_id, line, operation=operation, **params)
        require(value.get('phone') == phone_id and value.get('line') == line and
                value.get('lines') == self.gateway.phones[phone_id]['lines'], 'schedule_identity_changed', 503)
        return value

    def voice_enabled(self, phone_id):
        return (getattr(self.gateway, 'voice', None) is not None
                and any(profile.phone_id == phone_id
                        for profile in getattr(self.gateway, '_voice_profiles', ())))

    def voice(self, phone_id, line, operation='status', **body):
        require(self.voice_enabled(phone_id),
                'voice_control_unavailable', 503)
        try:
            # Only the source-checked handset handler can reach this adapter.
            context = self.gateway._phone_voice_context(phone_id)
            result = self.gateway.voice_request(line, operation, body, context)
        except Exception:
            raise AppError('voice_request_failed', 503) from None
        valid = isinstance(result, dict)
        if operation == 'status':
            valid = valid and result.get('status') == 'ok'
        else:
            valid = (valid and result.get('status') == 'accepted'
                     and result.get('action_id') == body.get('idempotency_key'))
        require(valid, 'voice_response_invalid', 503)
        value = result.get('voice')
        fields = {'schema_version', 'phone', 'line', 'session_id', 'state', 'active',
                  'remaining_seconds', 'stop_requested', 'stop_confirmed', 'error'}
        states = {'idle', 'starting', 'running', 'stopping', 'stopped', 'cancelled',
                  'failed', 'recovery_required', 'external_owner'}
        errors = {'voice_state_unavailable', 'voice_owner_unavailable', 'voice_start_failed',
                  'voice_start_uncertain', 'voice_signal_failed', 'voice_session_failed',
                  'voice_stop_unconfirmed'}
        require(isinstance(value, dict) and set(value) == fields,
                'voice_response_invalid', 503)
        state, session, remaining = value['state'], value['session_id'], value['remaining_seconds']
        require(type(value['schema_version']) is int and value['schema_version'] == 1
                and value['phone'] == phone_id
                and value['line'] in self.gateway.phones[phone_id]['lines']
                and isinstance(state, str) and state in states
                and all(type(value[key]) is bool for key in ('active', 'stop_requested', 'stop_confirmed'))
                and value['active'] == (state in ('starting', 'running', 'stopping'))
                and ((session is None and state == 'idle') or
                     (isinstance(session, str) and re.fullmatch(r'[0-9a-f]{32}', session) and state != 'idle'))
                and type(remaining) in (int, float) and 0 <= remaining <= 30 and math.isfinite(remaining)
                and (value['error'] is None or isinstance(value['error'], str) and value['error'] in errors),
                'voice_response_invalid', 503)
        if operation == 'start':
            require(state != 'idle', 'voice_response_invalid', 503)
        if operation == 'stop':
            require(session == body.get('session_id'), 'voice_response_invalid', 503)
        return {key: value[key] for key in fields}

    def voice_page(self, phone_id, line, value):
        root = ET.Element('CiscoIPPhoneText')
        child(root, 'Title', 'Voice Assistant')
        child(root, 'Prompt', 'Bounded session; microphone up to 30s')
        # A reserved journal from another process is not a locally stoppable
        # session. Local recovery_required sessions still retain their Stop path.
        external_owner = (value['state'] == 'external_owner'
                          or value['error'] == 'voice_owner_unavailable')
        text = ('State: ' + value['state'].replace('_', ' ') + '\nSession line: ' + value['line']
                + '\nRemaining: ' + str(value['remaining_seconds']) + ' seconds'
                + '\nStop requested: ' + ('Yes' if value['stop_requested'] else 'No')
                + '\nStop confirmed: ' + ('Yes' if value['stop_confirmed'] else 'No'))
        if value['session_id']:
            text += '\nSession: ' + value['session_id']
        if external_owner:
            text += ("\nThis menu cannot reach the session's controller. For a wake Job, use Stop"
                     " on its live Listening, Thinking or Speaking screen. If that screen is"
                     " unavailable, ask the operator to stop that exact Job."
                     " Do not restart until cleanup is confirmed.")
        elif value['error'] or value['state'] == 'recovery_required':
            text += '\nCleanup may be unconfirmed. Check the phone; do not restart blindly.'
        elif value['active']:
            text += '\nRunning is not a readiness signal. Use the live assistant screen.'
        child(root, 'Text', text)
        self.softkey(root, 'Refresh', self.url(phone_id, '/voice', line=line), 1)
        if not external_owner and value['state'] in ('idle', 'stopped', 'cancelled', 'failed'):
            self.softkey(root, 'Start', self.url(phone_id, '/voice-start', line=line), 2)
        elif not external_owner and value['session_id'] and not value['stop_confirmed']:
            self.softkey(root, 'Stop', self.url(phone_id, '/voice-stop', line=line,
                                              session=value['session_id']), 2)
        self.navigation(root, phone_id, line)
        return xml(root)

    def current_phone(self, phone_id):
        matches = [phone for phone in self.gateway.listing()['phones'] if phone['id'] == phone_id]
        require(len(matches) == 1, 'phone_unavailable', 503)
        return matches[0]

    def catalog(self, category='all'):
        entries = []
        allowed = set(self.gateway.config['dial_allowlist'])
        if category in ('all', 'phones'):
            for phone in self.gateway.phones.values():
                for line in phone['lines']:
                    if line in allowed:
                        entries.append((line, phone.get('room') or 'Phone', 'phone'))
            for phone in self.gateway.config.get('softphones', []):
                if phone['extension'] in allowed:
                    entries.append((phone['extension'], phone.get('room') or 'Softphone', 'phone'))
        if category in ('all', 'services', 'radio'):
            for service in self.gateway.config.get('services', []):
                if service['number'] in allowed and (category != 'radio' or service['kind'] == 'radio'):
                    entries.append((service['number'], service['label'], service['kind']))
        return sorted(entries, key=lambda row: int(row[0]))

    def paged(self, phone_id, line, title, entries, route, page, **params):
        count = max(1, (len(entries) + PAGE_SIZE - 1) // PAGE_SIZE)
        require(0 <= page < count, 'invalid_page')
        visible = entries[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        if page:
            visible += [('Previous page', self.url(phone_id, route, line=line, page=page - 1, **params))]
        if page + 1 < count:
            visible += [('Next page', self.url(phone_id, route, line=line, page=page + 1, **params))]
        if not visible:
            return self.text(phone_id, line, title, 'No entries configured.')
        return self.menu(phone_id, line, title, visible, f'Line {line}; page {page + 1}/{count}')

    def handle(self, phone_id, path, query):
        require(phone_id in self.gateway.phones, 'invalid_line', 404)
        preview = getattr(self.gateway, 'camera_preview', None)
        if path.startswith('/camera-preview/session/'):
            require(preview is not None, 'camera_preview_unavailable', 503)
            return preview.handle(
                phone_id, path[len('/camera-preview'):], query,
                context=self.gateway._phone_camera_context(phone_id), method='GET')
        require(isinstance(query, str) and len(query) <= 2048, 'invalid_query')
        try:
            pairs = [] if query == '' else parse_qsl(
                query, keep_blank_values=True, strict_parsing=True,
                encoding='utf-8', errors='strict', max_num_fields=8)
        except (ValueError, UnicodeError):
            raise AppError('invalid_query') from None
        params = dict(pairs)
        require(len(pairs) == len(params) and all(len(key) <= 20 and len(value) <= 160
                and not any(ord(char) < 32 for char in key + value) for key, value in pairs), 'invalid_query')
        route_fields = {
            '/': {'line'}, '/lines': {'line'}, '/directory': {'line', 'page', 'category'},
            '/radio': {'line', 'page'}, '/call': {'line', 'number'}, '/status': {'line'},
            '/settings': {'line'}, '/schedules': {'line'}, '/schedule-status': {'line'},
            '/timer': {'line'}, '/alarm': {'line'}, '/prepare': {'line', 'op', 'minutes', 'at', 'days'},
            '/set': {'line', 'setting', 'enabled'}, '/stop-alert': {'line'}, '/apply': {'token'},
            '/home': {'line', 'page'}, '/entity': {'line', 'id'},
            '/entity-set': {'line', 'id', 'enabled'}, '/cameras': {'line', 'page'},
            '/camera': {'line', 'id'},
            '/camera-preview': {'line', 'id'},
            '/voice': {'line'}, '/voice-start': {'line'}, '/voice-stop': {'line', 'session'},
        }
        require(path in route_fields, 'not_found', 404)
        require(set(params) <= route_fields[path], 'invalid_query')
        line = params.get('line', phone_id)
        require(line in self.gateway.phones[phone_id]['lines'], 'invalid_line')
        page_string = params.get('page', '0')
        require(re.fullmatch(r'[0-9]{1,4}', page_string), 'invalid_page')
        page = int(page_string)
        url = lambda route='/', **values: self.url(phone_id, route, line=line, **values)
        if path == '/':
            entries = [
                ('Phone Directory', url('/directory')),
                ('Internet Radio', url('/radio')),
                ('Timers & Alarms', url('/schedules')),
                ('Phone Settings', url('/settings')),
                ('Home Assistant', url('/home')),
                ('Camera Snapshot', url('/cameras')),
                ('Phone Status', url('/status')),
                ('Select line: ' + line, url('/lines'))]
            if self.voice_enabled(phone_id):
                entries.insert(0, ('Voice Assistant', url('/voice')))
            return self.menu(phone_id, line, 'Home Phone Apps', entries, 'Choose an app; line ' + line)
        if path == '/voice':
            return self.voice_page(phone_id, line, self.voice(phone_id, line))
        if path == '/voice-start':
            require(self.voice_enabled(phone_id),
                    'voice_control_unavailable', 503)
            return self.issue(phone_id, line, 'voice', 'start', {},
                              'Microphone <=30s; I am here; audio reserved')
        if path == '/voice-stop':
            session = params.get('session', '')
            require(re.fullmatch(r'[0-9a-f]{32}', session), 'invalid_query')
            value = self.voice(phone_id, line, 'stop', session_id=session,
                               idempotency_key='apps-voice-stop-' + session)
            return self.voice_page(phone_id, line, value)
        if path == '/lines':
            return self.menu(phone_id, line, 'Select source line', [
                ('Line ' + item, self.url(phone_id, line=item))
                for item in self.gateway.phones[phone_id]['lines']])
        if path in ('/directory', '/radio'):
            category = 'radio' if path == '/radio' else params.get('category', 'all')
            require(category in ('all', 'phones', 'services', 'radio'), 'invalid_category')
            entries = [(number + ' - ' + label, url('/call', number=number))
                       for number, label, _kind in self.catalog(category)]
            extra = {'category': category} if path == '/directory' else {}
            return self.paged(phone_id, line, 'Internet Radio' if path == '/radio' else 'Phone Directory',
                              entries, path, page, **extra)
        if path == '/call':
            number = params.get('number')
            matches = [item for item in self.catalog() if item[0] == number]
            require(len(matches) == 1, 'number_not_allowed')
            return self.issue(phone_id, line, 'gateway', 'dial', {'number': number},
                              'Call ' + number + ' from ' + line)
        if path == '/status':
            phone = self.current_phone(phone_id)
            channels = phone.get('channels', [])
            value = f'Line: {line}\nPhone: {phone.get("room") or phone_id}\n'
            value += 'Registered: ' + ('Yes' if phone['online'] else 'No')
            value += '\nDo Not Disturb: ' + ('On' if phone['dnd'] else 'Off')
            value += '\nAuto-answer: ' + ('On' if phone['autoanswer'] else 'Off')
            value += '\nActive calls: ' + str(len(channels))
            value += '\n' + self.alert_summary(phone.get('alert'))
            return self.text(phone_id, line, 'Phone Status', value, refresh='/status')
        if path == '/settings':
            phone = self.current_phone(phone_id)
            entries = []
            for key, label in (('dnd', 'Do Not Disturb'), ('autoanswer', 'Auto-answer')):
                entries.append((label + ': ' + ('On' if phone[key] else 'Off') + ' (change)',
                                url('/set', setting=key, enabled='0' if phone[key] else '1')))
            entries += [('Stop ringing alert', url('/stop-alert')), ('Phone Status', url('/status'))]
            return self.menu(phone_id, line, 'Phone Settings', entries, 'Settings affect both lines')
        if path == '/set':
            setting, enabled = params.get('setting'), params.get('enabled')
            require(setting in ('dnd', 'autoanswer') and enabled in ('0', '1'), 'invalid_setting')
            label = 'Do Not Disturb' if setting == 'dnd' else 'Auto-answer'
            return self.issue(phone_id, line, 'gateway', setting, {'enabled': enabled == '1'},
                              label + ' ' + ('on' if enabled == '1' else 'off'))
        if path == '/stop-alert':
            alert = self.current_phone(phone_id).get('alert')
            if not alert or alert['state'] == 'ended':
                return self.text(phone_id, line, 'Stop Alert', 'There is no active ringing alert.')
            # Use the observed owner line, even when the menu started on its sibling.
            return self.issue(phone_id, alert['line'], 'gateway', 'alert_stop', {'alert_id': alert['id']},
                              'Stop current ringing alert')
        if path == '/schedules':
            if self.backend is None:
                return self.text(phone_id, line, 'Timers & Alarms', 'Home Assistant scheduling is not configured.')
            return self.menu(phone_id, line, 'Timers & Alarms', [
                ('View current schedule', url('/schedule-status')),
                ('Start timer', url('/timer')), ('Cancel timer', url('/prepare', op='timer_cancel')),
                ('Set and enable weekly alarm', url('/alarm')),
                ('Enable saved alarm', url('/prepare', op='alarm_enable')),
                ('Disable saved alarm', url('/prepare', op='alarm_disable')),
                ('Stop ringing alert', url('/stop-alert'))], 'One timer and alarm per phone')
        if path == '/schedule-status':
            value = self.schedule(phone_id, line)
            timer, alarm = value['timer'], value['alarm']
            text = 'Timezone: ' + value['timezone'] + '\n'
            text += f'Timer line {timer["line"]}: ' + ('running' if timer['active'] else 'off')
            if timer['active']:
                text += f' ({timer.get("remaining_seconds", 0)} sec remaining)'
            text += f'\nAlarm line {alarm["line"]}: '
            text += ('enabled' if alarm['enabled'] else 'disabled') if alarm['configured'] else 'not configured'
            if alarm['configured']:
                text += '\n' + alarm['time'] + ' ' + ' '.join(DAYS[day] for day in alarm['weekdays'])
            alert = value.get('alert')
            if value.get('gateway_fresh'):
                text += '\n' + self.alert_summary(alert)
                if alert and alert['kind'] == 'timer' and alert['state'] != 'ended' and not timer['active']:
                    text += '\nThe countdown is off; a timer alert has not ended. Use Stop ringing alert.'
            else:
                text += '\nCurrent alert state is unknown; phone connection status is unavailable.'
                if alert:
                    text += '\n' + self.alert_summary(alert, stale=True)
            return self.text(phone_id, line, 'Schedule Status', text, refresh='/schedule-status')
        if path == '/timer':
            require(self.backend is not None, 'backend_unavailable', 503)
            return self.form(phone_id, line, 'Start Timer', 'timer_start', [('Minutes (1-1440)', 'minutes', '5', 'N')])
        if path == '/alarm':
            require(self.backend is not None, 'backend_unavailable', 503)
            value = self.schedule(phone_id, line)
            return self.form(phone_id, line, 'Set Weekly Alarm', 'alarm_set', [
                ('Time HHMM (' + safe(value['timezone'], 32) + ')', 'at', '0730', 'N'),
                ('Days: 1=Mon ... 7=Sun', 'days', '12345', 'N')])
        if path == '/prepare':
            operation = params.get('op')
            require(operation in ('timer_start', 'timer_cancel', 'alarm_set', 'alarm_enable', 'alarm_disable'),
                    'invalid_operation')
            payload = {}
            extra_keys = set(params) - {'line', 'op'}
            if operation == 'timer_start':
                require(extra_keys == {'minutes'} and re.fullmatch(r'[0-9]{1,4}', params.get('minutes', ''))
                        and 1 <= int(params['minutes']) <= 1440, 'bad_minutes')
                payload = {'minutes': int(params['minutes']), 'replace': False}
                description = 'Start ' + str(payload['minutes']) + ' min timer'
            elif operation == 'alarm_set':
                require(extra_keys == {'at', 'days'}, 'invalid_query')
                at, days = params.get('at', ''), params.get('days', '')
                require(re.fullmatch(r'(?:[01][0-9]|2[0-3])[0-5][0-9]', at), 'bad_time')
                require(re.fullmatch(r'[1-7]{1,7}', days) and len(set(days)) == len(days), 'bad_days')
                weekdays = sorted(int(day) - 1 for day in days)
                payload = {'time': at[:2] + ':' + at[2:], 'weekdays': weekdays, 'enabled': True, 'replace': False}
                description = 'Alarm ' + payload['time'] + ' ' + '/'.join(DAYS[day] for day in weekdays)
            else:
                require(not extra_keys, 'invalid_query')
                description = {'timer_cancel': 'Cancel timer', 'alarm_enable': 'Enable saved alarm',
                               'alarm_disable': 'Disable saved alarm'}[operation]
            value = self.schedule(phone_id, line)
            slot = value['timer'] if operation.startswith('timer_') else value['alarm']
            exists = slot['active'] if operation.startswith('timer_') else slot['configured']
            if operation == 'timer_cancel':
                # Expiry clears the countdown but its alert keeps ringing. A
                # cancellation must reach HA until that alert is terminal.
                alert = value.get('alert')
                active_alert = (alert if alert and alert['kind'] == 'timer'
                                and alert['state'] != 'ended' else None)
                if active_alert:
                    require(active_alert['line'] == line, 'line_does_not_own_schedule', 409)
                # A stale gateway observation cannot prove there is no alert.
                # HA supports persisting cancellation and reporting uncertainty.
                exists = exists or bool(active_alert) or not value.get('gateway_fresh')
            if exists:
                require(slot['line'] == line, 'line_does_not_own_schedule', 409)
            if operation == 'timer_cancel' and not exists:
                return self.text(phone_id, line, 'Timer', 'No timer is running. Nothing was changed.')
            if operation in ('alarm_enable', 'alarm_disable') and not exists:
                return self.text(phone_id, line, 'Alarm', 'No alarm has been configured. Nothing was changed.')
            return self.issue(phone_id, line, 'schedule', operation, payload, description)
        if path == '/home':
            require(self.backend is not None, 'backend_unavailable', 503)
            entries = [(item['label'] + ': ' + str(item['state']), url('/entity', id=item['id']))
                       for item in self.backend.entities(phone_id)]
            return self.paged(phone_id, line, 'Home Assistant', entries, path, page)
        if path in ('/entity', '/entity-set'):
            require(self.backend is not None, 'backend_unavailable', 503)
            entities = [item for item in self.backend.entities(phone_id) if item['id'] == params.get('id')]
            require(len(entities) == 1, 'unknown_entity', 404)
            entity = entities[0]
            if path == '/entity-set':
                enabled = params.get('enabled')
                require(entity['control'] and enabled in ('0', '1'), 'entity_read_only')
                return self.issue(phone_id, line, 'entity', 'on' if enabled == '1' else 'off',
                                  {'id': entity['id']}, entity['label'] + (' on' if enabled == '1' else ' off'))
            if entity['control']:
                return self.menu(phone_id, line, entity['label'], [
                    ('Turn on', url('/entity-set', id=entity['id'], enabled='1')),
                    ('Turn off', url('/entity-set', id=entity['id'], enabled='0')),
                    ('Refresh', url('/entity', id=entity['id']))], 'State: ' + str(entity['state']))
            return self.text(phone_id, line, entity['label'], str(entity['state']) + ' ' + (entity.get('unit') or ''))
        if path in ('/cameras', '/camera', '/camera-preview'):
            require(self.backend is not None, 'backend_unavailable', 503)
            cameras = self.backend.cameras(phone_id)
            if path == '/cameras':
                return self.paged(phone_id, line, 'Camera Snapshot', [
                    (item['label'], url('/camera', id=item['id'])) for item in cameras], path, page)
            found = [item for item in cameras if item['id'] == params.get('id')]
            require(len(found) == 1, 'unknown_camera', 404)
            if path == '/camera-preview':
                require(preview is not None, 'camera_preview_unavailable', 503)
                context = self.gateway._phone_camera_context(phone_id)
                ref = preview.start(phone_id, found[0]['id'], line=line, context=context)
                return preview.handle(phone_id, '/session/' + ref.session_id,
                                      context=context, method='GET')
            image = self.backend.camera(phone_id, found[0]['id'])
            image_url = self.gateway.resource(self.gateway.phones[phone_id], image, 'image/png')
            root = ET.Element('CiscoIPPhoneImageFile')
            child(root, 'Title', found[0]['label']); child(root, 'Prompt', 'Snapshot; not live video')
            child(root, 'LocationX', '-1'); child(root, 'LocationY', '-1'); child(root, 'URL', image_url)
            self.softkey(root, 'Refresh', url('/camera', id=found[0]['id']), 1)
            if preview is not None:
                self.softkey(root, 'Preview', url('/camera-preview', id=found[0]['id']), 2)
            self.navigation(root, phone_id, line)
            return xml(root)
        if path == '/apply':
            token = params.get('token')
            row = self.consume(phone_id, token)
            payload, line = row['payload'], row['line']
            if row['kind'] == 'voice':
                require(row['action'] == 'start', 'invalid_operation')
                value = self.voice(phone_id, line, 'start', idempotency_key='apps-' + token,
                                   attended=True, exclusive_media=True, enable_microphone=True)
                return self.voice_page(phone_id, line, value)
            if row['kind'] == 'gateway':
                result = self.gateway.mutate(phone_id, row['action'],
                        {'line': line, 'idempotency_key': 'apps-' + token, **payload})
                require(isinstance(result, dict) and result.get('status') == 'accepted',
                        'action_not_confirmed', 503)
                if row['action'] == 'alert_stop':
                    state = result.get('state')
                    require(result.get('action_id') == 'apps-' + token
                            and result.get('alert_id') == payload['alert_id']
                            and isinstance(state, str) and state in ALERT_STATES,
                            'alert_stop_not_confirmed', 503)
                    ended = state == 'ended'
                    message = ('Alert stopped.' if ended else 'Stop requested; cleanup is still pending.')
                    message += f'\nLine: {line}\nAlert ID: {payload["alert_id"]}\nState: {state}'
                    if not ended:
                        message += '\nThe alert may continue ringing. Refresh before a new attempt.'
                    return self.text(phone_id, line, 'Alert stopped' if ended else 'Stop requested',
                                     message, refresh='/status')
            elif row['kind'] == 'schedule':
                result = self.schedule(phone_id, line, row['action'], request_id='apps-' + token, **payload)
                if result.get('operation_result') in ('not_active', 'not_configured'):
                    message = ('No timer is running.' if row['action'].startswith('timer_')
                               else 'No alarm has been configured.')
                    return self.text(phone_id, line, 'Nothing changed', message + ' Nothing was changed.')
                require(result.get('operation_result') in ('applied', 'replayed'),
                        'action_not_confirmed', 503)
            else:
                require(row['kind'] == 'entity' and self.backend is not None, 'backend_unavailable', 503)
                self.backend.entity_action(phone_id, payload['id'], row['action'])
            return self.text(phone_id, line, 'Action completed', row['description'] + '.\nLine: ' + line)
        raise AppError('not_found', 404)
