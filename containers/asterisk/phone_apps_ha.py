"""Small, server-side Home Assistant adapter for Cisco XML services.

Only an operator-configured private IPv4 origin and explicit entity allowlists
are reachable. Neither tokens, HA URLs nor entity IDs are sent to the phone.
"""
import copy
import http.client
import io
import ipaddress
import json
import math
import re
import time
from urllib import error, parse, request


SCHEDULE_PATH = '/api/services/cisco_phone_control/schedule?return_response'
MAX_JSON = 128 * 1024
MAX_IMAGE = 3 * 1024 * 1024
OPERATIONS = {'status', 'timer_start', 'timer_cancel', 'alarm_set', 'alarm_enable',
              'alarm_disable', 'toggle_saved_alarm', 'alert_stop'}
HA_ERRORS = {
    'The timer belongs to the other line': 'ha_timer_owned_by_other_line',
    'The alarm belongs to the other line': 'ha_alarm_owned_by_other_line',
    'The active alert belongs to the other line': 'ha_alert_owned_by_other_line',
    'A timer is already pending; explicit replace is required': 'ha_timer_replace_required',
    'The alarm belongs to the other line; explicit replace is required': 'ha_alarm_replace_required',
    'Stop the active timer alert before starting another countdown': 'ha_stop_active_timer_first',
    'Stop the active alarm before changing its schedule': 'ha_stop_active_alarm_first',
    'Gateway unavailable; schedule change refused': 'ha_gateway_unavailable_for_change',
    'Schedule storage failed; no operation was sent': 'ha_storage_failed_no_operation_sent',
    'request_id was already used for different schedule inputs': 'ha_request_id_conflict',
}


class AppsHAError(Exception):
    """A fixed, non-secret error code suitable for a phone error screen."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def require(value, reason='invalid_ha_apps_config'):
    if not value:
        raise AppsHAError(reason)


def _match(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def validate_ha_config(config):
    """Validate without reading environment variables or resolving hostnames."""
    require(isinstance(config, dict) and set(config) <= {
        'url', 'token', 'entry_id', 'entities', 'cameras'})
    require(_match(r'[A-Za-z0-9_.-]{32,4096}', config.get('token')))
    require(_match(r'[A-Za-z0-9_-]{8,128}', config.get('entry_id')))
    try:
        value = config.get('url')
        require(isinstance(value, str) and len(value) <= 256 and not re.search(r'\s', value))
        url = parse.urlsplit(value)
        require(url.scheme in ('http', 'https') and not url.username and not url.password
                and url.path in ('', '/') and not url.query and not url.fragment)
        host = ipaddress.IPv4Address(url.hostname)
        require(any(host in ipaddress.IPv4Network(net) for net in
                    ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')))
        require(url.port is None or 1 <= url.port <= 65535)
    except (TypeError, ValueError):
        raise AppsHAError('invalid_ha_apps_config') from None
    for kind, limit in (('entities', 12), ('cameras', 8)):
        entries = config.get(kind, [])
        require(isinstance(entries, list) and len(entries) <= limit)
        seen = set()
        for item in entries:
            require(isinstance(item, dict) and set(item) <=
                    ({'id', 'label', 'entity_id', 'control'} if kind == 'entities'
                     else {'id', 'label', 'entity_id'}))
            require(_match(r'[a-z][a-z0-9_-]{0,31}', item.get('id')) and item['id'] not in seen)
            seen.add(item['id'])
            require(_match(r'[A-Za-z0-9][ -~]{0,47}', item.get('label'))
                    and item['label'] == item['label'].strip())
            entity = item.get('entity_id')
            require(_match(r'[a-z][a-z0-9_]*\.[a-z][a-z0-9_]{0,127}', entity))
            if kind == 'cameras':
                require(entity.startswith('camera.'))
            else:
                require(type(item.get('control', False)) is bool)
                require(not item.get('control', False) or entity.split('.')[0]
                        in ('light', 'switch', 'input_boolean'))
    return config


def validate_config(config):
    """Return a detached, normalized configuration for the gateway loader."""
    value = copy.deepcopy(validate_ha_config(config))
    value['url'] = value['url'].rstrip('/')
    value.setdefault('entities', [])
    value.setdefault('cameras', [])
    return value


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _text(value, limit=80):
    require(isinstance(value, str) and len(value) <= limit
            and not any(ord(c) < 32 or ord(c) == 127 for c in value), 'invalid_ha_response')
    return value


class HomeAssistantApps:
    def __init__(self, config):
        self.config = validate_config(config)
        self.base = config['url'].rstrip('/')
        self.opener = request.build_opener(request.ProxyHandler({}), NoRedirect())

    def _request(self, path, body=None, *, binary=False, timeout=8):
        """No redirects/proxies/retries; bound response size and body read time."""
        data = None if body is None else json.dumps(body, separators=(',', ':')).encode()
        req = request.Request(self.base + path, data=data, headers={
            'Authorization': 'Bearer ' + self.config['token'],
            'Accept': 'image/jpeg,image/png' if binary else 'application/json',
            'Content-Type': 'application/json'})
        limit = MAX_IMAGE if binary else MAX_JSON
        deadline = time.monotonic() + timeout
        try:
            with self.opener.open(req, timeout=timeout) as response:
                require(response.status == 200, 'ha_request_not_confirmed')
                length = response.headers.get('Content-Length')
                require(length is None or length.isdecimal() and int(length) <= limit,
                        'ha_response_too_large')
                chunks, size = [], 0
                while True:
                    remaining = deadline - time.monotonic()
                    require(remaining > 0, 'ha_request_outcome_unknown')
                    sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                    if sock is not None:
                        sock.settimeout(remaining)
                    chunk = response.read1(min(65536, limit + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    require(size <= limit, 'ha_response_too_large')
                    chunks.append(chunk)
                raw = b''.join(chunks)
            return raw if binary else json.loads(raw)
        except error.HTTPError as exc:
            # Never echo the HA response, origin or credentials into a phone page.
            reason = 'ha_request_rejected' if 400 <= exc.code < 500 else 'ha_request_not_confirmed'
            if exc.code in (400, 500):
                try:
                    message = json.loads(exc.read(4097)).get('message')
                    reason = HA_ERRORS.get(message, reason)
                except (ValueError, TypeError, AttributeError, OSError):
                    pass
            exc.close()
            raise AppsHAError(reason) from None
        except (OSError, error.URLError, http.client.HTTPException):
            raise AppsHAError('ha_request_outcome_unknown') from None
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise AppsHAError('invalid_ha_response') from None

    @staticmethod
    def _phone(phone_id):
        require(_match(r'2[0-9]{2}', phone_id), 'invalid_phone')

    def schedule(self, phone_id, line, operation='status', request_id=None, **options):
        self._phone(phone_id)
        require(_match(r'2[0-9]{2}', line) and operation in OPERATIONS, 'invalid_schedule_request')
        allowed = {'timer_start': {'minutes', 'replace'},
                   'alarm_set': {'time', 'weekdays', 'enabled', 'replace'}}.get(operation, set())
        require(set(options) <= allowed, 'invalid_schedule_request')
        if operation == 'timer_start':
            require(type(options.get('minutes')) is int and 1 <= options['minutes'] <= 1440,
                    'invalid_timer_minutes')
        if operation == 'alarm_set':
            require(_match(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', options.get('time')), 'invalid_alarm_time')
            days = options.get('weekdays')
            require(isinstance(days, list) and 1 <= len(days) <= 7 and
                    all(type(day) is int and 0 <= day <= 6 for day in days), 'invalid_alarm_days')
            options['weekdays'] = sorted(set(days))
        for flag in ('enabled', 'replace'):
            require(flag not in options or type(options[flag]) is bool, 'invalid_schedule_request')
        body = {'entry_id': self.config['entry_id'], 'phone': phone_id, 'line': line,
                'operation': operation, **options}
        if operation != 'status':
            require(_match(r'[A-Za-z0-9_.:-]{8,80}', request_id), 'schedule_request_id_required')
            body['request_id'] = request_id
        if operation in ('alarm_enable', 'alarm_disable'):
            current = self.schedule(phone_id, line)
            if not current['alarm']['configured']:
                current['operation_result'] = 'not_configured'
                return current
        response = self._request(SCHEDULE_PATH, body)
        require(isinstance(response, dict), 'invalid_ha_response')
        result = self._schedule_result(response.get('service_response'), phone_id, line)
        # An accepted HTTP request alone is not a confirmed schedule change.
        if result['operation_result'] == 'applied':
            timer, alarm = result['timer'], result['alarm']
            checks = {
                'timer_start': timer['active'] and timer['line'] == line,
                'timer_cancel': not timer['active'],
                'alarm_enable': alarm['configured'] and alarm['enabled'],
                'alarm_disable': not alarm['enabled'],
            }
            if operation == 'alarm_set':
                checks[operation] = (alarm['configured'] and alarm['line'] == line
                    and alarm['time'] == options['time'] and alarm['weekdays'] == options['weekdays']
                    and alarm['enabled'] == options.get('enabled', False))
            require(checks.get(operation, True), 'ha_change_not_confirmed')
        return result

    @staticmethod
    def _schedule_result(value, phone_id, line):
        require(isinstance(value, dict), 'invalid_ha_response')
        lines = value.get('lines')
        require(isinstance(lines, list) and 1 <= len(lines) <= 2 and
                all(_match(r'2[0-9]{2}', number) for number in lines) and len(set(lines)) == len(lines)
                and lines[0] == phone_id and line in lines and value.get('phone') == phone_id
                and value.get('line') == line and type(value.get('schema_version')) is int
                and value['schema_version'] == 1, 'ha_schedule_identity_mismatch')
        require(type(value.get('gateway_fresh')) is bool and
                _match(r'[A-Za-z0-9_+./-]{1,100}', value.get('timezone')), 'invalid_ha_response')
        timer, alarm = value.get('timer'), value.get('alarm')
        require(isinstance(timer, dict) and isinstance(alarm, dict), 'invalid_ha_response')
        require(timer.get('line') in lines and type(timer.get('active')) is bool and
                (timer.get('id') is None or _match(r'[0-9a-f]{32}', timer['id'])), 'invalid_ha_response')
        for key in ('deadline', 'remaining_seconds'):
            item = timer.get(key)
            require(item is None or type(item) in ((int,) if key == 'remaining_seconds' else (int, float))
                    and math.isfinite(item) and item >= 0, 'invalid_ha_response')
        require(alarm.get('line') in lines and type(alarm.get('configured')) is bool and
                type(alarm.get('enabled')) is bool and
                _match(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', alarm.get('time')) and
                isinstance(alarm.get('weekdays'), list) and len(alarm['weekdays']) <= 7 and
                all(type(day) is int and 0 <= day <= 6 for day in alarm['weekdays']), 'invalid_ha_response')
        alert = value.get('alert')
        if alert is not None:
            require(isinstance(alert, dict) and _match(r'[0-9a-f]{32}', alert.get('id')) and
                    alert.get('line') in lines and alert.get('kind') in ('timer', 'alarm') and
                    alert.get('state') in ('starting', 'ringing', 'stopping', 'ended', 'unknown'),
                    'invalid_ha_response')
            alert = {key: alert[key] for key in ('id', 'line', 'kind', 'state')}
        require(value.get('operation_result') in ('applied', 'replayed', 'outcome_unknown',
                'not_active', 'not_configured', 'status'), 'invalid_ha_response')
        return {'schema_version': 1, 'phone': phone_id, 'line': line, 'lines': list(lines),
                'gateway_fresh': value['gateway_fresh'], 'timezone': value['timezone'],
                'timer': {key: timer.get(key) for key in ('line', 'id', 'active', 'deadline', 'remaining_seconds')},
                'alarm': {key: alarm[key] for key in ('line', 'time', 'weekdays', 'enabled', 'configured')},
                'alert': alert, 'operation_result': value['operation_result']}

    def _entry(self, kind, key):
        require(_match(r'[a-z][a-z0-9_-]{0,31}', key), 'unknown_ha_item')
        found = next((item for item in self.config.get(kind, []) if item['id'] == key), None)
        require(found is not None, 'unknown_ha_item')
        return found

    def _entity(self, entry, timeout=8):
        state = self._request('/api/states/' + entry['entity_id'], timeout=timeout)
        require(isinstance(state, dict) and state.get('entity_id') == entry['entity_id'], 'invalid_ha_response')
        value = _text(state.get('state'))
        attrs = state.get('attributes', {})
        require(isinstance(attrs, dict), 'invalid_ha_response')
        unit = _text(attrs.get('unit_of_measurement', ''), 16)
        return {'id': entry['id'], 'label': entry['label'], 'state': value, 'unit': unit,
                'control': entry.get('control', False) and value not in ('unknown', 'unavailable')}

    def entities(self, phone_id):
        self._phone(phone_id)
        results = []
        deadline = time.monotonic() + 8
        for entry in self.config.get('entities', []):
            try:
                remaining = deadline - time.monotonic()
                require(remaining > 0, 'ha_request_outcome_unknown')
                results.append(self._entity(entry, timeout=remaining))
            except AppsHAError:
                results.append({'id': entry['id'], 'label': entry['label'], 'state': 'unavailable',
                                'unit': '', 'control': False})
        return results

    def entity_action(self, phone_id, key, action):
        self._phone(phone_id)
        entry = self._entry('entities', key)
        require(entry.get('control', False) and action in ('on', 'off'), 'ha_control_not_allowed')
        domain = entry['entity_id'].split('.')[0]
        self._request('/api/services/' + domain + '/turn_' + action, {'entity_id': entry['entity_id']})
        result = self._entity(entry)
        require(result['state'] == action, 'ha_change_not_confirmed')
        return result

    def cameras(self, phone_id):
        self._phone(phone_id)
        return [{key: entry[key] for key in ('id', 'label')} for entry in self.config.get('cameras', [])]

    def camera(self, phone_id, key):
        self._phone(phone_id)
        entry = self._entry('cameras', key)
        data = self._request('/api/camera_proxy/' + entry['entity_id'], binary=True)
        try:
            from PIL import Image, ImageOps
        except ImportError:
            raise AppsHAError('camera_image_support_unavailable') from None
        try:
            with Image.open(io.BytesIO(data)) as source:
                require(source.format in ('JPEG', 'PNG') and source.width <= 4096
                        and source.height <= 4096 and source.width * source.height <= 8_000_000,
                        'invalid_camera_image')
                source.load()
                # 7945G XML image area; preserve aspect ratio without stretching.
                picture = ImageOps.pad(source.convert('RGB'), (298, 156),
                                       method=Image.Resampling.LANCZOS, color=(16, 24, 32))
                output = io.BytesIO()
                picture.save(output, format='PNG')
                return output.getvalue()
        except AppsHAError:
            raise
        except (OSError, ValueError, Image.DecompressionBombError):
            raise AppsHAError('invalid_camera_image') from None
