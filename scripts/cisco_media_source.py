#!/usr/bin/env python3
"""Resolve HA media sources without starting playback or copying local audio.

This module is also the fixed worker sent to the configured Kubernetes pod.
Credentials and media identifiers arrive on stdin, never in command arguments.
"""
import asyncio
import ipaddress
import json
import math
from pathlib import Path
import re
import sys
from urllib.parse import urljoin, urlsplit

MAX_INPUT = 256 * 1024
MAX_RESPONSE = 256 * 1024
ERRORS = frozenset((
    'invalid_media_source_request', 'media_source_dependency_required',
    'media_source_authentication_failed', 'media_source_resolution_failed',
    'media_source_response_invalid', 'media_source_timeout',
    'media_source_connection_failed', 'media_source_audio_required',
))


class MediaSourceError(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def require(value, reason='media_source_response_invalid'):
    if not value:
        raise MediaSourceError(reason)


def unique_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value)
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=pairs)


def validate_payload(payload):
    reason = 'invalid_media_source_request'
    require(isinstance(payload, dict) and set(payload) == {'url', 'token', 'timeout', 'media_ids'}, reason)
    base, token, timeout, identifiers = (payload[key] for key in ('url', 'token', 'timeout', 'media_ids'))
    require(isinstance(base, str) and len(base) <= 256 and not any(c.isspace() for c in base), reason)
    url = urlsplit(base)
    require(url.scheme in ('http', 'https') and not url.username and not url.password
            and url.path in ('', '/') and not url.query and not url.fragment, reason)
    address = ipaddress.IPv4Address(url.hostname)
    require(any(address in ipaddress.IPv4Network(net) for net in
                ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')), reason)
    require(url.port is None or 1 <= url.port <= 65535, reason)
    require(isinstance(token, str) and re.fullmatch(r'[A-Za-z0-9_.-]{32,4096}', token), reason)
    require(type(timeout) in (int, float) and 1 <= timeout <= 60 and math.isfinite(timeout), reason)
    require(isinstance(identifiers, list) and 1 <= len(identifiers) <= 50, reason)
    for value in identifiers:
        require(isinstance(value, str) and 1 <= len(value) <= 4096 and value.isascii()
                and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value), reason)
        parsed = urlsplit(value)
        require(parsed.scheme == 'media-source' and parsed.netloc and not parsed.username
                and not parsed.password and not parsed.fragment, reason)
    return base.rstrip('/')


async def receive(ws):
    raw = await ws.receive_str()
    require(len(raw.encode()) <= MAX_RESPONSE)
    value = unique_json(raw)
    require(isinstance(value, dict))
    return value


def audio_url(base, result):
    require(isinstance(result, dict))
    value, mime = result.get('url'), result.get('mime_type')
    require(isinstance(mime, str) and (mime.lower().startswith('audio/')
            or mime.lower() == 'application/ogg'), 'media_source_audio_required')
    require(isinstance(value, str) and 1 <= len(value) <= 4096 and value.isascii()
            and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
            and '\\' not in value and not value.startswith('//'))
    if value.startswith('/'):
        value = urljoin(base + '/', value)
    require(len(value) <= 4096)  # Match the gateway after expanding relative URLs.
    url = urlsplit(value)
    require(url.scheme in ('http', 'https') and url.hostname and not url.username
            and not url.password and not url.fragment and (url.port is None or 1 <= url.port <= 65535))
    return value


async def exchange(ws, payload, base):
    require((await receive(ws)).get('type') == 'auth_required')
    await ws.send_json({'type': 'auth', 'access_token': payload['token']})
    auth = await receive(ws)
    require(auth.get('type') == 'auth_ok', 'media_source_authentication_failed')
    urls = []
    for number, identifier in enumerate(payload['media_ids'], 1):
        await ws.send_json({'id': number, 'type': 'media_source/resolve_media',
                            'media_content_id': identifier})
        response = await receive(ws)
        require(type(response.get('id')) is int and response['id'] == number
                and response.get('type') == 'result' and type(response.get('success')) is bool)
        require(response['success'], 'media_source_resolution_failed')
        urls.append(audio_url(base, response.get('result')))
    return urls


async def connect(payload, base):
    try:
        import aiohttp
    except ImportError:
        raise MediaSourceError('media_source_dependency_required') from None
    async def refuse_redirect(*_args):
        # Do not authenticate a WebSocket reached through an HTTP redirect.
        raise MediaSourceError('media_source_response_invalid')

    trace = aiohttp.TraceConfig()
    trace.on_request_redirect.append(refuse_redirect)
    timeout = aiohttp.ClientTimeout(total=payload['timeout'])
    async with aiohttp.ClientSession(timeout=timeout, trust_env=False, trace_configs=[trace]) as session:
        async with session.ws_connect(base.replace('http', 'ws', 1) + '/api/websocket',
                                      max_msg_size=MAX_RESPONSE, autoclose=True, autoping=True) as ws:
            return await exchange(ws, payload, base)


def perform(payload):
    try:
        base = validate_payload(payload)
        urls = asyncio.run(asyncio.wait_for(connect(payload, base), timeout=payload['timeout']))
        return {'ok': True, 'urls': urls}
    except MediaSourceError as error:
        return {'ok': False, 'reason': error.reason}
    except (asyncio.TimeoutError, TimeoutError):
        return {'ok': False, 'reason': 'media_source_timeout'}
    except (ValueError, TypeError, KeyError, OverflowError, UnicodeError):
        return {'ok': False, 'reason': 'invalid_media_source_request'}
    except Exception:
        return {'ok': False, 'reason': 'media_source_connection_failed'}


def resolve_media(client, identifiers):
    """Use an existing validated HA Gateway transport; return private URLs."""
    payload = {'url': client.config['gateway_url'], 'token': client.config['token'],
               'timeout': client.timeout, 'media_ids': identifiers}
    validate_payload(payload)
    if client.config['transport'] == 'direct':
        result = perform(payload)
    else:
        require(client.config['transport'] == 'kubectl', 'invalid_media_source_request')
        if client.pod is None:
            client.pod = client.resolve_pod()
        source = Path(__file__).read_text()
        command = client.kube_args() + ['exec', '-i', client.pod, '-c',
                  client.config['kubectl']['container'], '--', 'python3', '-c', source]
        result = client.run(command, input_data=json.dumps(payload).encode())
    require(isinstance(result, dict) and type(result.get('ok')) is bool)
    if not result['ok']:
        reason = result.get('reason')
        require(isinstance(reason, str) and reason in ERRORS)
        raise MediaSourceError(reason)
    urls = result.get('urls')
    require(isinstance(urls, list) and len(urls) == len(identifiers))
    # Validate the remote worker result as well; never return raw response data.
    return [audio_url(payload['url'], {'url': value, 'mime_type': 'audio/unknown'}) for value in urls]


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        require(len(raw) <= MAX_INPUT, 'invalid_media_source_request')
        result = perform(unique_json(raw))
    except Exception:
        result = {'ok': False, 'reason': 'invalid_media_source_request'}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
