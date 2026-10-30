"""Exact-session music commands for the Cisco CLI; no playback at import time."""
import math
from pathlib import PurePosixPath
import re
import secrets
import time
from urllib.parse import urlsplit, urlunsplit


COMMANDS = frozenset(('play', 'search', 'music', 'queue', 'pause', 'resume', 'stop',
                      'next', 'previous', 'clear-queue', 'seek', 'gain', 'mute', 'unmute'))
EXAMPLES = ['play FILE_OR_QUERY_OR_URL', 'search QUERY', 'music', 'queue',
            'pause', 'resume', 'stop', 'next', 'previous', 'clear-queue',
            'seek SECONDS', 'gain PERCENT', 'mute', 'unmute']
SESSION = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
STATES = frozenset(('idle', 'buffering', 'playing', 'paused', 'error', 'blocked'))
REASONS = frozenset(('cleanup_pending', 'voice', 'call', 'offline', 'dnd', 'playback_failed'))


class PlayerError(Exception):
    def __init__(self, reason):
        self.reason = reason


def require(value, reason):
    if not value:
        raise PlayerError(reason)


def number(value, low, high):
    return type(value) in (int, float) and low <= value <= high and math.isfinite(value)


def source_url(value):
    require(isinstance(value, str) and 1 <= len(value) <= 4096 and value.isascii()
            and not re.search(r'[\x00-\x20\x7f\\]', value), 'invalid_music_url')
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in ('http', 'https') and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and not parsed.fragment and '%' not in parsed.hostname
                 and (parsed.port is None or 1 <= parsed.port <= 65535))
    except ValueError:
        valid = False
    require(valid, 'invalid_music_url')
    return value


def public_source(value):
    if value is None:
        return None
    if isinstance(value, str) and re.fullmatch(r'[0-9]{3,8}', value):
        return value
    parsed = urlsplit(source_url(value))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))


def player_status(phone):
    value = phone.get('player')
    require(isinstance(value, dict), 'controlled_player_unavailable')
    require(type(value.get('v')) is int and value['v'] == 1, 'invalid_player_status')
    capabilities = value.get('capabilities')
    require(isinstance(capabilities, list) and len(capabilities) <= 32
            and all(isinstance(item, str) and re.fullmatch(r'[a-z_]{1,40}', item)
                    for item in capabilities) and len(set(capabilities)) == len(capabilities),
            'invalid_player_status')
    require(isinstance(value.get('state'), str) and value['state'] in STATES,
            'invalid_player_status')
    session, line = value.get('session_id'), value.get('line')
    require('session_id' in value and 'line' in value
            and (session is None and line is None and value['state'] in ('idle', 'blocked')
                 or isinstance(session, str) and SESSION.fullmatch(session)
                 and line in phone['lines'] and value['state'] != 'blocked'),
            'invalid_player_identity')
    length, index = value.get('queue_length'), value.get('queue_index')
    require(type(length) is int and 0 <= length <= 50 and type(index) is int
            and (length == 0 and index == 0 and session is None
                 or 0 <= index < length and session is not None), 'invalid_player_queue')
    require(number(value.get('position'), 0, 86400)
            and number(value.get('position_updated_at'), 0, 10**12)
            and number(value.get('volume_level'), 0, 1)
            and type(value.get('is_volume_muted')) is bool
            and type(value.get('seekable')) is bool, 'invalid_player_status')
    duration = value.get('duration')
    require('duration' in value and (duration is None or number(duration, 0, 86400))
            and (not value['seekable'] or duration is not None and duration > 0),
            'invalid_player_status')
    reason = value.get('reason')
    require('reason' in value and (reason is None or isinstance(reason, str) and reason in REASONS),
            'invalid_player_status')
    for field in ('title', 'artist', 'album'):
        item = value.get(field)
        require(field in value and (item is None or isinstance(item, str) and len(item) <= 256
                and not re.search(r'[\x00-\x1f\x7f]', item)), 'invalid_player_status')
    fields = ('v', 'state', 'session_id', 'line', 'title', 'artist', 'album', 'position',
              'position_updated_at', 'duration', 'seekable', 'queue_index', 'queue_length',
              'volume_level', 'is_volume_muted', 'reason')
    result = {key: value[key] for key in fields}
    result.update(capabilities=list(capabilities), source_id=public_source(value.get('source_id')))
    return result


def library_tracks(config, query):
    require('music' in config, 'music_library_config_required')
    from cisco_library import select_tracks
    return select_tracks(config['music'], query)


def resolve_tracks(args, config, gateway_factory):
    if args.source.startswith(('http://', 'https://')):
        return [{'source': source_url(args.source), 'metadata': {}}]
    tracks = library_tracks(config, args.source)
    require(1 <= len(tracks) <= 50, 'invalid_music_selection')
    require('home_assistant' in config, 'home_assistant_media_config_required')
    from cisco_media_source import resolve_media
    urls = resolve_media(gateway_factory(config['home_assistant'], args.timeout),
                         [track['media_content_id'] for track in tracks])
    require(isinstance(urls, list) and len(urls) == len(tracks), 'invalid_resolved_music_sources')
    # Validate the complete selection before the first phone mutation.
    result = []
    for track, url in zip(tracks, urls):
        title = re.sub(r'[\x00-\x1f\x7f]', ' ', PurePosixPath(track['relative_path']).stem)[:256]
        result.append({'source': source_url(url), 'metadata': {'title': title}})
    return result


def enqueue_plan(tracks, mode, active):
    indexed = list(enumerate(tracks, 1))
    if not active:
        return [(index, track, mode if index == 1 else 'add') for index, track in indexed]
    if mode == 'next':
        return [(index, track, 'next') for index, track in reversed(indexed)]
    if mode == 'play':
        return [(1, tracks[0], 'play')] + [(index, track, 'next') for index, track in reversed(indexed[1:])]
    return [(index, track, mode if index == 1 or mode == 'add' else 'add')
            for index, track in indexed]


def receipt(value, action, body):
    require(isinstance(value, dict) and value.get('status') == 'accepted'
            and value.get('action_id') == body['idempotency_key'], 'unconfirmed_player_response')
    if action in ('volume', 'mute'):
        require(number(value.get('volume_level'), 0, 1)
                and type(value.get('is_volume_muted')) is bool, 'unconfirmed_player_response')
        field = 'volume_level' if action == 'volume' else 'is_volume_muted'
        require(value[field] == body[field], 'unconfirmed_player_response')
        return body['session_id']
    session = value.get('session_id')
    require(isinstance(session, str) and SESSION.fullmatch(session), 'unconfirmed_player_response')
    require(body['session_id'] is None or session == body['session_id'], 'unconfirmed_player_response')
    if body['session_id'] is None:
        require(action == 'play' and value.get('line') == body['line'], 'unconfirmed_player_response')
    elif 'line' in value:
        require(value['line'] == body['line'], 'unconfirmed_player_response')
    return session


def wait_ready(args, client, phone_id, session, inventory_validator):
    deadline = time.monotonic() + min(45, args.timeout)
    for _ in range(90):
        remaining = deadline - time.monotonic()
        require(remaining >= 1, 'player_readiness_timeout')
        previous_timeout = client.timeout
        try:
            client.timeout = min(previous_timeout, remaining)
            phone = inventory_validator(client.request('/v1/phones'), args.extension)
        finally:
            client.timeout = previous_timeout
        require(phone['id'] == phone_id, 'player_phone_mapping_changed')
        state = player_status(phone)
        require(state['session_id'] == session and state['line'] == args.extension, 'player_session_changed')
        if state['state'] in ('playing', 'paused'):
            return
        require(state['state'] == 'buffering', 'player_not_ready')
        time.sleep(min(.5, max(0, deadline - time.monotonic())))
    raise PlayerError('player_readiness_timeout')


def run(args, config, *, client=None, phone=None, gateway_factory=None,
        inventory_validator=None, on_dispatch=None):
    """Return a result and exit code; ambiguous mutation outcomes retain every ID."""
    attempted, accepted, accepted_items = [], [], []
    requested = 0
    session = None
    identity = {'selected_line': args.extension}
    if phone is not None:
        identity['phone_id'] = phone['id']
    try:
        if args.command == 'search':
            tracks = library_tracks(config, args.query)
            return {'status': 'ok', 'action': 'search', 'matches': [
                {key: track[key] for key in ('relative_path', 'size_bytes')} for track in tracks]}, 0
        state = player_status(phone)
        session = state['session_id']
        if args.command in ('music', 'queue'):
            return {'status': 'ok', 'action': args.command, **identity, 'player': state}, 0
        require(session is None or state['line'] == args.extension, 'player_owned_by_other_line')
        capability = {'gain': 'volume', 'unmute': 'mute', 'clear-queue': 'queue'}.get(args.command, args.command)
        require(args.command == 'stop' or capability in state['capabilities'], 'player_control_unavailable')
        base = args.idempotency_key or 'cli-' + secrets.token_hex(16)
        require(isinstance(base, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', base), 'invalid_idempotency_key')
        if args.command == 'play':
            tracks = resolve_tracks(args, config, gateway_factory)
            requested = len(tracks)
            if session is not None and args.enqueue != 'replace':
                require(state['queue_length'] + requested <= 50, 'player_queue_full')
            require(requested == 1 or len(base) <= 125, 'batch_idempotency_key_too_long')
            plan = enqueue_plan(tracks, args.enqueue, session is not None)
            operations = [(index, 'play', dict(track, enqueue=mode)) for index, track, mode in plan]
        else:
            requested = 1
            action = {'gain': 'volume', 'unmute': 'mute', 'clear-queue': 'clear_queue'}.get(args.command, args.command)
            extra = {}
            if action == 'volume':
                require(number(args.percent, 0, 100), 'gain_requires_0_to_100')
                extra['volume_level'] = args.percent / 100
            elif action == 'mute':
                extra['is_volume_muted'] = args.command == 'mute'
            else:
                require(session is not None, 'player_not_active')
                if action == 'seek':
                    require(number(args.seconds, 0, 86400), 'invalid_seek_position')
                    require(state['seekable'] and args.seconds <= state['duration'], 'player_seek_unavailable')
                    extra['position'] = args.seconds
            operations = [(1, action, extra)]
        if args.dry_run:
            return {'status': 'dry_run', 'action': args.command, **identity,
                    'session_id': session, 'requested_count': requested, 'content_omitted': True}, 0
        new_session = session is None
        for order, (index, action, extra) in enumerate(operations):
            if order == 1 and new_session:
                wait_ready(args, client, phone['id'], session, inventory_validator)
            key = base if requested == 1 else base + ':' + str(index)
            body = {'line': args.extension, 'session_id': session, 'idempotency_key': key, **extra}
            old_callback = client.on_dispatch
            def dispatched(key=key):
                attempted.append(key)
                if on_dispatch is not None:
                    on_dispatch(key)
            try:
                client.on_dispatch = dispatched
                response = client.request('/v1/phones/' + phone['id'] + '/player_' + action, body)
            finally:
                client.on_dispatch = old_callback
            session = receipt(response, action, body)
            accepted.append(key)
            accepted_items.append(index)
        return {'status': 'accepted', 'action': args.command, **identity, 'session_id': session,
                'requested_count': requested, 'accepted_count': len(accepted), 'remaining_count': 0,
                'action_ids': attempted, 'accepted_action_ids': accepted, 'accepted_item_numbers': accepted_items,
                'note': 'Requests accepted; inspect music status for playback. No automatic retry.'}, 0
    except (Exception, KeyboardInterrupt) as error:
        reason = 'music_interrupted' if isinstance(error, KeyboardInterrupt) else getattr(error, 'reason', None)
        if not isinstance(reason, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,99}', reason):
            reason = 'music_local_or_transport_error'
        unknown = len(attempted) > len(accepted)
        result = {'status': 'not_confirmed' if unknown else 'partial' if accepted else 'refused',
                  'action': args.command, 'reason': reason, **identity,
                  'session_id': session, 'requested_count': requested, 'accepted_count': len(accepted),
                  'remaining_count': requested - len(accepted), 'action_ids': attempted,
                  'accepted_action_ids': accepted, 'accepted_item_numbers': accepted_items,
                  'unconfirmed_count': int(unknown), 'not_attempted_count': requested - len(attempted),
                  'note': 'No automatic retry. Inspect music status before changing the remaining queue.'}
        status = getattr(error, 'http_status', None)
        if type(status) is int and 300 <= status <= 599:
            result['http_status'] = status
        return result, 1


def display(result):
    if result.get('action') == 'search' and result['status'] == 'ok':
        for track in result['matches']:
            print(str(track['size_bytes']) + ' bytes  ' + track['relative_path'])
        print(str(len(result['matches'])) + ' matches. Play the same query to queue all matches.')
    elif 'player' in result:
        player = result['player']
        print('Music on phone ' + result['phone_id'] + ': ' + player['state'])
        print('Selected line: ' + result['selected_line'] + '; playback line: ' + (player['line'] or 'none'))
        print('Track: ' + (player['title'] or player['source_id'] or 'none'))
        print('Queue: ' + str(player['queue_index'] + 1 if player['queue_length'] else 0)
              + '/' + str(player['queue_length']) + ' (current item and count only)')
        print('Position: ' + str(player['position']) + ' seconds; seekable: ' + str(player['seekable']))
        print('Digital gain: ' + str(round(player['volume_level'] * 100, 2))
              + '%; muted: ' + str(player['is_volume_muted']))
        if player['reason']:
            print('Reason: ' + player['reason'])
    else:
        import json
        print(json.dumps(result, ensure_ascii=True, indent=2))
