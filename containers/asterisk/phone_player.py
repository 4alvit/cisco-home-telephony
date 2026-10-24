"""Exact-call media sessions for the authenticated phone gateway.

The HTTP controller never passes a URL to Asterisk or to a shell. It reserves
one short-lived AudioSocket destination in AstDB; the dialplan consumes it once.
All subsequent controls refer to the opaque session and the observed call UID.
"""
import math
import re
import time
import uuid
from urllib.parse import urlsplit, urlunsplit


CAPABILITIES = ['play', 'url', 'pause', 'resume', 'seek', 'volume', 'mute',
                'queue', 'next', 'previous']
ACTIONS = {'player_' + name for name in (
    'play', 'pause', 'resume', 'stop', 'seek', 'volume', 'mute', 'next',
    'previous', 'clear_queue')}


def options(value):
    if not isinstance(value, dict) or set(value) - {'enabled', 'allowed_origins', 'max_calls', 'max_seconds', 'max_bytes'}:
        raise ValueError('invalid player configuration')
    result = dict(enabled=False, allowed_origins=[], max_calls=4, max_seconds=7200,
                  max_bytes=64 * 1024 * 1024)
    result.update(value)
    if type(result['enabled']) is not bool:
        raise ValueError('invalid player enabled flag')
    for key, low, high in [('max_calls', 1, 10), ('max_seconds', 30, 7200),
                            ('max_bytes', 1024 * 1024, 64 * 1024 * 1024)]:
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('invalid player limit')
    origins = result['allowed_origins']
    if not isinstance(origins, list) or len(origins) > 16:
        raise ValueError('invalid player origins')
    for origin in origins:
        if not isinstance(origin, str) or len(origin) > 256:
            raise ValueError('invalid player origin')
        p = urlsplit(origin)
        if (p.scheme not in ('http', 'https') or not p.hostname or p.username
                or p.password or p.path not in ('', '/') or p.query or p.fragment):
            raise ValueError('invalid player origin')
        _ = p.port
    return result


def public_source(value):
    """Signed query strings belong to the worker, never its status record."""
    if not isinstance(value, str) or not value.startswith(('http://', 'https://')):
        return value
    p = urlsplit(value)
    return urlunsplit((p.scheme, p.netloc, p.path, '', ''))


class PhonePlayer:
    def __init__(self, gateway, module):
        self.gateway, self.api = gateway, module
        self.options = options(gateway.config.get('media_player', {}))
        self.sessions = {}
        gateway.db.execute('CREATE TABLE IF NOT EXISTS player_preferences '
                           '(phone TEXT PRIMARY KEY, volume REAL, muted INTEGER)')
        for phone in gateway.phones:
            gateway.db.execute('INSERT OR IGNORE INTO player_preferences VALUES (?, .5, 0)', (phone,))
        gateway.db.commit()

    def runtime(self):
        return self.api.app_module('media_runtime')

    def preferences(self, phone):
        volume, muted = self.gateway.db.execute(
            'SELECT volume,muted FROM player_preferences WHERE phone=?', (phone,)).fetchone()
        return {'volume_level': volume, 'is_volume_muted': bool(muted)}

    def source(self, body):
        source = body.get('source')
        metadata = body.get('metadata', {})
        self.api.require(isinstance(source, str) and isinstance(metadata, dict)
                         and not set(metadata) - {'title', 'artist', 'album', 'duration'},
                         'invalid_player_source', 400)
        if source in self.gateway.media_sources():
            catalog = self.api.app_module('radio').catalog()
            station = next((key for key, value in catalog.items() if value['extension'] == source), None)
            self.api.require(station is not None, 'invalid_player_source', 400)
            data = {'station': station, **metadata}
        else:
            data = {'url': source, **metadata}
        try:
            return self.runtime().validate_source(data, self.options['allowed_origins'])
        except (ValueError, TypeError):
            raise self.api.Refused('invalid_player_source', 400) from None

    def validate(self, phone, action, body):
        require = self.api.require
        require(self.options['enabled'], 'player_disabled')
        common = {'line', 'session_id', 'idempotency_key'}
        extra = {'player_play': {'source', 'metadata', 'enqueue'},
                 'player_seek': {'position'}, 'player_volume': {'volume_level'},
                 'player_mute': {'is_volume_muted'}}.get(action, set())
        require(not set(body) - common - extra and common <= set(body), 'invalid_player_request', 400)
        require(body['line'] in phone['lines'], 'line_does_not_belong_to_phone', 400)
        session = body['session_id']
        require(session is None or isinstance(session, str) and re.fullmatch(
            r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', session), 'invalid_player_session', 400)
        if action == 'player_play':
            require(body.get('enqueue', 'replace') in ('replace', 'add', 'next', 'play'), 'invalid_enqueue', 400)
            self.source(body)
        for action_name, key, upper in [('player_seek', 'position', 86400), ('player_volume', 'volume_level', 1)]:
            if action == action_name:
                value = body.get(key)
                require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= upper,
                        'invalid_player_' + key, 400)
        if action == 'player_mute':
            require(type(body.get('is_volume_muted')) is bool, 'invalid_player_mute', 400)

    def supported(self, ami):
        variable = 'DIALPLAN_EXISTS(home,699,cisco-player-v1)'
        response = ami.action('Getvar', Variable=variable)
        return [x.get('Value') for x in response if x.get('Response') == 'Success'
                and x.get('Variable') == variable] == ['1']

    def observe(self, phone, ami, channels):
        """Bind once to the marker, then require the same UID on every read."""
        record = self.sessions.get(phone['id'])
        if not record:
            return None
        session = record['session']
        candidates = self.api.own_channels(phone, channels)
        bound = None
        if len(candidates) == 1:
            candidate = candidates[0]
            uid = candidate.get('Uniqueid')
            if (candidate.get('Context') == 'home' and candidate.get('Exten') == '699'
                    and re.fullmatch(r'[0-9]+\.[0-9]+', uid or '')
                    and candidate.get('Channel', '').startswith('PJSIP/' + record['line'] + '-')):
                try:
                    response = ami.action('Getvar', Channel=candidate['Channel'], Variable='CISCO_PLAYER')
                except self.api.Refused as error:
                    if getattr(error, 'ami_error', None) != 'no_such_channel':
                        raise
                    response = []
                values = [x.get('Value') for x in response if x.get('Response') == 'Success'
                          and x.get('Variable') == 'CISCO_PLAYER']
                if values == [f'1:{session.token}:{uid}'] and record['uid'] in (None, uid):
                    _, fresh = ami.snapshot()
                    if any(x.get('Channel') == candidate['Channel'] and x.get('Uniqueid') == uid for x in fresh):
                        record['uid'] = uid
                        bound = candidate
        if bound is None and (record['uid'] is not None or time.monotonic() - record['created'] > 20):
            session.close()
            self.sessions.pop(phone['id'], None)
            return None
        return record

    def snapshot(self, phone, ami, endpoints, channels):
        record = self.observe(phone, ami, channels)
        value = {'v': 1, 'capabilities': CAPABILITIES, 'state': 'idle', 'reason': None,
                 'session_id': None, 'line': None, 'source_id': None, 'title': None,
                 'artist': None, 'album': None, 'position': 0., 'position_updated_at': time.time(),
                 'duration': None, 'seekable': False, 'queue_index': 0, 'queue_length': 0,
                 **self.preferences(phone['id'])}
        if record:
            state = record['session'].snapshot()
            value.update({k: state[k] for k in value if k in state})
            value.update(line=record['line'], session_id=str(record['session'].token),
                         source_id=public_source(state.get('source_id')), reason=state.get('error'))
            if not record['uid'] and value['state'] in ('playing', 'paused'):
                value['state'] = 'buffering'
        elif not all(endpoints.get(line) == 'Not in use' for line in phone['lines']):
            owned = self.api.own_channels(phone, channels)
            voice = len(owned) == 1 and re.fullmatch(r'cisco-voice-[0-9a-f]{32}', owned[0].get('Linkedid', ''))
            value.update(state='blocked', reason='voice' if voice else 'call' if owned else 'offline')
        elif self.gateway.preference(phone['id'])['dnd']:
            value.update(state='blocked', reason='dnd')
        return value

    def mutate(self, phone, action, body):
        require, gateway = self.api.require, self.gateway
        with self.api.AMI(gateway.config['ami']) as ami:
            endpoints, channels = ami.snapshot()
            record = self.observe(phone, ami, channels)
            current = str(record['session'].token) if record else None
            require(body['session_id'] == current, 'player_session_changed')
            if record:
                require(body['line'] == record['line'], 'player_line_changed')
                require(record['uid'] is not None or action == 'player_stop', 'player_call_not_observed')
            if action in ('player_volume', 'player_mute'):
                if action == 'player_volume':
                    column, value, command, args = 'volume', body['volume_level'], 'volume', {'level': body['volume_level']}
                else:
                    column, value, command, args = 'muted', int(body['is_volume_muted']), 'mute', {'muted': body['is_volume_muted']}
                if record:
                    try:
                        record['session'].control(command, **args)
                    except ValueError:
                        raise self.api.Refused('player_control_not_supported', 400) from None
                gateway.db.execute('UPDATE player_preferences SET ' + column + '=? WHERE phone=?', (value, phone['id']))
                gateway.db.commit()
                return self.preferences(phone['id'])
            if action == 'player_play' and record is None:
                require(self.supported(ami), 'player_dialplan_unavailable')
                require(len(self.sessions) < self.options['max_calls'], 'player_call_limit')
                return self.start(phone, body)
            require(record is not None, 'player_not_active')
            session = record['session']
            try:
                if action == 'player_play':
                    session.control('enqueue', source=self.source(body), mode=body.get('enqueue', 'replace'))
                elif action == 'player_seek':
                    session.control('seek', position=body['position'])
                elif action == 'player_stop':
                    # Closing our socket is safe even if the phone already hung up.
                    session.close()
                    try:
                        if record['uid']:
                            ami.action('Hangup', Channel=record['uid'], Cause='16')
                    except self.api.Refused as error:
                        if getattr(error, 'ami_error', None) != 'no_such_channel':
                            raise
                    finally:
                        self.sessions.pop(phone['id'], None)
                else:
                    session.control(action.removeprefix('player_'))
            except ValueError:
                raise self.api.Refused('player_control_not_supported', 400) from None
            return {'session_id': current}

    def start(self, phone, body):
        gateway, require, line = self.gateway, self.api.require, body['line']
        with gateway.suspend_hands_free(phone, wait_for_call=True):
            with gateway.reserve_phone_controls(phone), self.api.AMI(gateway.config['ami']) as ami:
                endpoints, channels = ami.snapshot()
                require(not self.api.own_channels(phone, channels) and all(
                    endpoints.get(x) == 'Not in use' for x in phone['lines']), 'phone_busy')
                require(not gateway.preference(phone['id'])['dnd'] and phone['id'] not in gateway.dnd_pending,
                        'do_not_disturb')
                gateway.reconcile_alerts(ami, channels)
                require(not any(x['phone'] == phone['id'] and x['state'] != 'ended'
                                for x in gateway.alerts()), 'alert_already_active')
                require(self.supported(ami), 'player_dialplan_unavailable')
                session = self.runtime().MediaSession(uuid.uuid4(), self.source(body),
                    allowed_origins=self.options['allowed_origins'], max_seconds=self.options['max_seconds'],
                    max_bytes=self.options['max_bytes'])
                pref = self.preferences(phone['id'])
                session.control('volume', level=pref['volume_level'])
                session.control('mute', muted=pref['is_volume_muted'])
                self.sessions[phone['id']] = {'session': session, 'line': line, 'uid': None,
                                              'created': time.monotonic()}
                try:
                    session.start()
                    ami.action('DBPut', Family='cisco-media', Key=line, Val=f'{session.port}:{session.token}')
                    gateway._mark_hands_free_call(phone)
                    self.api.execute(phone, 'Key:Line' + str(phone['lines'].index(line) + 1))
                    self.api.execute(phone, 'Dial:699')
                except Exception:
                    session.close()
                    self.sessions.pop(phone['id'], None)
                    raise
                return {'session_id': str(session.token), 'line': line}

    def close(self):
        for record in list(self.sessions.values()):
            record['session'].close()
        self.sessions.clear()
