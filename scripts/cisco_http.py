#!/usr/bin/env python3
"""Bounded gateway HTTP worker, also sent unchanged to an explicit kubectl target."""
import http.client
import ipaddress
import json
import re
import sys
from urllib import error, parse, request

MAX_RESPONSE = 256 * 1024
MAX_INPUT = 3 * 1024 * 1024
SAFE_REASONS = frozenset('''forbidden unknown_phone unknown_action phone_unavailable
phone_busy do_not_disturb alert_already_active number_not_allowed
ring_destination_not_allowed exactly_one_selected_phone_channel_required
channel_identity_missing channel_identity_changed phone_not_ringing
alert_requires_manual_answer_or_stop alert_not_found bad_alert_id
previous_action_outcome_unknown idempotency_key_conflict
speaker_toggle_requires_idle_phone operation_failed_do_not_retry_blindly
too_many_pending_messages intercom_requires_cisco_destination intercom_requires_two_phones
intercom_not_connected_check_status'''.split())
HA_ERRORS = {
    'The selected line does not belong to this handset': 'ha_line_mapping_mismatch',
    'The timer belongs to the other line': 'ha_timer_owned_by_other_line',
    'The alarm belongs to the other line': 'ha_alarm_owned_by_other_line',
    'The active alert belongs to the other line': 'ha_alert_owned_by_other_line',
    'The timer belongs to the other line; explicit replace is required': 'ha_timer_replace_required',
    'A timer is already pending; explicit replace is required': 'ha_timer_replace_required',
    'The alarm belongs to the other line; explicit replace is required': 'ha_alarm_replace_required',
    'Stop the active timer alert before starting another countdown': 'ha_stop_active_timer_first',
    'Stop the active alarm before changing its schedule': 'ha_stop_active_alarm_first',
    'request_id was already used for different schedule inputs': 'ha_request_id_conflict',
    'Gateway unavailable; schedule change refused': 'ha_gateway_unavailable_for_change',
    'Schedule storage failed; no operation was sent': 'ha_storage_failed_no_operation_sent',
    'Cisco phone controls are unloading': 'ha_integration_unloading',
}
VOICE_REASONS = frozenset('''voice_control_unavailable voice_inventory_mismatch
invalid_voice_request bad_voice_method unknown_voice_phone unknown_voice_session
voice_request_id_required explicit_voice_start_required voice_control_closed
voice_control_disabled voice_request_id_conflict voice_phone_busy
voice_recovery_required voice_state_unavailable voice_start_failed
voice_profile_changed voice_rtp_endpoint_collision
voice_display_port_collision invalid_voice_ffmpeg'''.split())
SAFE_REASONS = SAFE_REASONS | VOICE_REASONS
ERROR_REASONS = SAFE_REASONS | frozenset(('invalid_gateway_response', 'gateway_http_error',
    'network_outcome_unknown', 'invalid_gateway_response_or_request', 'invalid_worker_input',
    'ha_service_http_error', 'ha_schedule_service_response_missing')) | frozenset(HA_ERRORS.values())
SCHEDULE_PATH = '/api/services/cisco_phone_control/schedule?return_response'


def gateway_url(value):
    """Plain HTTP is deliberately restricted to a configured private IPv4 host."""
    if (not isinstance(value, str) or len(value) > 256 or any(c.isspace() for c in value)
            or '?' in value or '#' in value):
        raise ValueError('invalid_gateway_url')
    url = parse.urlsplit(value)
    if (url.scheme not in ('http', 'https') or url.username or url.password
            or url.path not in ('', '/') or url.query or url.fragment):
        raise ValueError('invalid_gateway_url')
    ip = ipaddress.IPv4Address(url.hostname)
    if not any(ip in ipaddress.IPv4Network(net) for net in
               ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
        raise ValueError('gateway_requires_private_ipv4')
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError('invalid_gateway_port')
    return value.rstrip('/')


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def perform(payload):
    """Return only JSON data or fixed errors; never include a request or exception."""
    try:
        base = gateway_url(payload['url'])
        token, path, body, timeout = (payload[k] for k in ('token', 'path', 'body', 'timeout'))
        api = payload.get('api', 'gateway')
        if api not in ('gateway', 'home_assistant'):
            raise ValueError('invalid_api')
        token_pattern = r'[A-Za-z0-9_.-]{32,4096}' if api == 'home_assistant' else r'[A-Za-z0-9_-]{32,128}'
        if not isinstance(token, str) or not re.fullmatch(token_pattern, token):
            raise ValueError('invalid_token')
        if api == 'home_assistant' and (path != SCHEDULE_PATH or not isinstance(body, dict)):
            raise ValueError('invalid_ha_request')
        if api == 'gateway':
            playback_status = (isinstance(path, str) and re.fullmatch(
                r'/v1/phones/2[0-9]{2}/voice_status\?include_playback=1', path))
            if playback_status:
                if body is not None:
                    raise ValueError('invalid_request_body')
            elif not re.fullmatch(r'/v1/phones(?:/2[0-9]{2}/[a-z]+(?:_[a-z]+)*)?', path):
                raise ValueError('invalid_request_path')
        if type(timeout) not in (int, float) or not 1 <= timeout <= 60:
            raise ValueError('invalid_timeout')
        if body is not None and not isinstance(body, dict):
            raise ValueError('invalid_request_body')
        data = None if body is None else json.dumps(body).encode()
        if data is not None and len(data) > MAX_INPUT:
            raise ValueError('request_too_large')
        req = request.Request(base + path, data=data, headers={
            'Authorization': 'Bearer ' + token, 'Accept': 'application/json',
            'Content-Type': 'application/json'})
        opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
        with opener.open(req, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE or response.status != 200:
                return {'ok': False, 'reason': 'invalid_gateway_response'}
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError('invalid_response')
            if api == 'home_assistant':
                # Discard changed_states, which can contain unrelated state attributes.
                data = data.get('service_response')
                if not isinstance(data, dict):
                    return {'ok': False, 'reason': 'ha_schedule_service_response_missing'}
            return {'ok': True, 'data': data}
    except error.HTTPError as exc:
        reason = 'ha_service_http_error' if payload.get('api') == 'home_assistant' else 'gateway_http_error'
        try:
            data = json.loads(exc.read(4097))
            if isinstance(data, dict) and data.get('reason') in SAFE_REASONS:
                reason = data['reason']
            if isinstance(data, dict) and payload.get('api') == 'home_assistant':
                message = data.get('message')
                if isinstance(message, str) and message in HA_ERRORS:
                    reason = HA_ERRORS[message]
        except (ValueError, TypeError, OSError, RecursionError, http.client.HTTPException):
            pass
        finally:
            exc.close()
        return {'ok': False, 'reason': reason, 'http_status': exc.code}
    except (OSError, error.URLError, http.client.HTTPException):
        return {'ok': False, 'reason': 'network_outcome_unknown'}
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return {'ok': False, 'reason': 'invalid_gateway_response_or_request'}


if __name__ == '__main__':
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError('too_large')
        result = perform(json.loads(raw))
    except Exception:
        result = {'ok': False, 'reason': 'invalid_worker_input'}
    print(json.dumps(result, separators=(',', ':')))
