"""Read-only, deadline-bound proof of phone-reported media quiescence.

The caller must hold the recovery/session fences. This proves two observations,
not a SIP Stop acknowledgement, hardware microphone state, or future silence.
Credentials cross the subprocess boundary only through stdin and are never
included in results. The child uses only AMI inventory and five fixed HTTP GETs.
"""
from __future__ import annotations

import base64
from html.parser import HTMLParser
import importlib.util
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from urllib import request

MAX_SECONDS = 30.0
REAP_RESERVE = 1.5
MAX_BODY = 262144
MAX_PROOF = 16384
COUNTERS = {'Sender Packets': 'tx_packets', 'Rcvr Packets': 'rx_packets',
            'Sender Octets': 'tx_octets', 'Rcvr Octets': 'rx_octets'}
FIELDS = {'Host Name', 'Stream Status', 'Start Time', 'Local Address',
          'Remote Address', *COUNTERS}


class QuiescenceError(RuntimeError):
    """Fixed, non-secret public error; raw transport errors never escape."""


def _need(condition):
    if not condition:
        raise QuiescenceError('voice_quiescence_unproven')


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    _need(remaining > 0)
    return remaining


def _ipv4(value):
    _need(type(value) is str)
    address = ipaddress.IPv4Address(value)
    _need(str(address) == value and any(address in network for network in (
        ipaddress.IPv4Network('10.0.0.0/8'), ipaddress.IPv4Network('172.16.0.0/12'),
        ipaddress.IPv4Network('192.168.0.0/16'), ipaddress.IPv4Network('127.0.0.0/8'))))
    return value


def _credential(value):
    _need(type(value) is str and 1 <= len(value) <= 128
          and not any(c in value for c in '\r\n\x00'))
    return value


def _inputs(profile, session_id, challenge, deadline, private, phone):
    _need(type(session_id) is str and re.fullmatch('[a-f0-9]{32}', session_id)
          and type(challenge) is str and re.fullmatch('[a-f0-9]{64}', challenge))
    _need(type(deadline) in (int, float) and math.isfinite(deadline))
    lines = list(profile.lines)
    _need(1 <= len(lines) <= 20 and len(set(lines)) == len(lines)
          and all(type(line) is str and re.fullmatch('[0-9]{1,16}', line) for line in lines))
    _need(type(profile.device) is str and re.fullmatch('SEP[0-9A-F]{12}', profile.device))
    _need(profile.phone_id in lines and phone['id'] == profile.phone_id
          and phone['lines'] == lines and phone['device'] == profile.device
          and phone['ipv4'] == profile.ipv4)
    ami = private['ami']
    _need(type(ami['port']) is int and 1 <= ami['port'] <= 65535)
    return {'session_id': session_id, 'challenge': challenge, 'deadline': deadline,
            'phone': {'id': profile.phone_id, 'lines': lines, 'device': profile.device,
                      'ipv4': _ipv4(profile.ipv4),
                      'username': _credential(phone['username']),
                      'password': _credential(phone['password'])},
            'ami': {'host': _ipv4(ami['host']), 'port': ami['port'],
                    'username': _credential(ami['username']),
                    'secret': _credential(ami['secret'])}}


class _Rows(HTMLParser):
    """Keep nested layout rows separate from their inner statistics rows."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.rows = [], []

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            _need(len(self.stack) < 16)
            self.stack.append({'cells': [], 'active': None})
        elif tag in ('td', 'th') and self.stack:
            row = self.stack[-1]
            _need(row['active'] is None)
            row['active'] = []
            row['cells'].append(row['active'])

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.stack:
            self.stack[-1]['active'] = None
        elif tag == 'tr' and self.stack:
            row = self.stack.pop()
            _need(len(self.rows) < 4096)
            self.rows.append([' '.join(''.join(cell).split()) for cell in row['cells']])

    def handle_data(self, data):
        for row in self.stack:
            if row['active'] is not None:
                row['active'].append(data)


def _parse_page(raw, device, slot):
    _need(type(raw) is bytes and len(raw) <= MAX_BODY)
    parser = _Rows()
    parser.feed(raw.decode('utf-8', 'strict'))
    parser.close()
    # The device omits closing tags on outer layout rows. Only completed
    # statistics rows are evidence; an incomplete critical row is a refusal.
    _need(all(not row['cells'] or ' '.join(''.join(row['cells'][0]).split()) not in FIELDS
              for row in parser.stack))
    values = {}
    for row in parser.rows:
        if row and row[0] in FIELDS:
            _need(len(row) == 3 and row[1] == '' and row[0] not in values)
            values[row[0]] = row[2]
    _need(set(values) == FIELDS and values['Host Name'] == device
          and values['Stream Status'] == 'Not Ready')
    start = values['Start Time']
    _need(1 <= len(start) <= 64 and all(32 <= ord(c) < 127 for c in start))
    for key in ('Local Address', 'Remote Address'):
        match = re.fullmatch(r'([0-9.]+)/([0-9]{1,5})', values[key])
        _need(match is not None)
        _need(str(ipaddress.IPv4Address(match[1])) == match[1] and int(match[2]) <= 65535)
    public = {'slot': slot, 'status': 'Not Ready'}
    for key, output in COUNTERS.items():
        text = values[key]
        _need(re.fullmatch('[0-9]{1,20}', text) is not None and int(text) < 2**64)
        public[output] = int(text)
    return public, (start, values['Local Address'], values['Remote Address'])


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise QuiescenceError('voice_quiescence_unproven')


def _read_page(opener, phone, slot, deadline):
    url = ('http://' + phone['ipv4'] + '/CGI/Java/Serviceability?'
           'adapter=device.statistics.streaming.' + str(slot))
    auth = base64.b64encode((phone['username'] + ':' + phone['password']).encode()).decode()
    req = request.Request(url, method='GET', headers={
        'Authorization': 'Basic ' + auth, 'Cache-Control': 'no-cache'})
    with opener.open(req, timeout=min(3.0, _remaining(deadline))) as response:
        _need(response.status == 200 and response.geturl() == url)
        raw = response.read(MAX_BODY + 1)
    _remaining(deadline)
    return _parse_page(raw, phone['device'], slot)


def _idle(ami, phone, session_id, deadline):
    _remaining(deadline)
    endpoints, channels = ami.snapshot()
    _remaining(deadline)
    _need(type(endpoints) is dict and type(channels) is list and len(channels) <= 8192)
    _need(all(endpoints.get(line) == 'Not in use' for line in phone['lines']))
    roots = {'cisco-voice-' + session_id, 'cisco-voice-' + session_id + '-b'}
    for channel in channels:
        _need(type(channel) is dict)
        for key in ('Channel', 'Uniqueid', 'Linkedid'):
            value = channel.get(key)
            _need(type(value) is str and 1 <= len(value) <= 512
                  and not any(ord(c) < 32 for c in value))
        _need(not any(channel['Channel'].startswith('PJSIP/' + line + '-')
                      for line in phone['lines']))
        _need(channel['Uniqueid'] not in roots and channel['Linkedid'] not in roots)


def _gateway():
    path = Path(__file__).resolve().with_name('phone_gateway.py')
    spec = importlib.util.spec_from_file_location('_quiescence_gateway', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _collect(payload):
    phone, deadline = payload['phone'], payload['deadline']
    opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
    samples, previous = [], None
    with _gateway().AMI(payload['ami']) as ami:
        for index in range(2):
            if index:
                _need(_remaining(deadline) > 1.0)
                time.sleep(1.0)
            _idle(ami, phone, payload['session_id'], deadline)
            records = [_read_page(opener, phone, slot, deadline) for slot in range(5)]
            _idle(ami, phone, payload['session_id'], deadline)
            _need(previous is None or records == previous)
            previous = records
            samples.append({'observed_at': time.monotonic(), 'device': phone['device'],
                            'idle_lines': phone['lines'], 'phone_or_related_channels': [],
                            'streams': [record[0] for record in records]})
    _remaining(deadline)
    return {'version': 1, 'session_id': payload['session_id'],
            'challenge': payload['challenge'], 'samples': samples}


def _validate_proof(value, payload, start, end):
    _need(type(value) is dict and set(value) == {'version', 'session_id', 'challenge', 'samples'}
          and type(value['version']) is int and value['version'] == 1
          and value['session_id'] == payload['session_id']
          and value['challenge'] == payload['challenge'])
    samples = value['samples']
    _need(type(samples) is list and len(samples) == 2)
    for sample in samples:
        _need(type(sample) is dict and set(sample) == {
            'observed_at', 'device', 'idle_lines', 'phone_or_related_channels', 'streams'})
        stamp = sample['observed_at']
        _need(type(stamp) in (float, int) and math.isfinite(stamp) and start <= stamp <= end)
        _need(sample['device'] == payload['phone']['device']
              and sample['idle_lines'] == payload['phone']['lines']
              and type(sample['idle_lines']) is list
              and sample['phone_or_related_channels'] == [])
        streams = sample['streams']
        _need(type(streams) is list and len(streams) == 5)
        for slot, stream in enumerate(streams):
            _need(type(stream) is dict and set(stream) == {
                'slot', 'status', 'tx_packets', 'rx_packets', 'tx_octets', 'rx_octets'}
                and type(stream['slot']) is int and stream['slot'] == slot
                and stream['status'] == 'Not Ready')
            _need(all(type(stream[key]) is int and 0 <= stream[key] < 2**64
                      for key in COUNTERS.values()))
    _need(samples[1]['observed_at'] - samples[0]['observed_at'] >= .25
          and samples[0]['streams'] == samples[1]['streams'])


def _signal_group(process, sig):
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def _reap(process):
    """Every path waits; leader-exit races never skip pipe cleanup or reaping."""
    try:
        if process.poll() is None:
            _signal_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=.3)
            except subprocess.TimeoutExpired:
                _signal_group(process, signal.SIGKILL)
        process.wait(timeout=.6)
        # This worker never spawns children. Any surviving group is a refusal.
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        _signal_group(process, signal.SIGKILL)
        raise QuiescenceError('voice_quiescence_worker_unreaped')
    except subprocess.TimeoutExpired:
        raise QuiescenceError('voice_quiescence_worker_unreaped') from None
    finally:
        for pipe in (process.stdin, process.stdout):
            if pipe is not None:
                pipe.close()


def prove_quiescence(profile, session_id, challenge, deadline, *, private, phone):
    """Return a strict two-sample proof only after the read-only child is reaped."""
    process = None
    start = time.monotonic()
    try:
        _need(type(deadline) in (int, float) and math.isfinite(deadline))
        end = min(deadline, start + MAX_SECONDS)
        work_end = end - REAP_RESERVE
        _need(work_end - start >= 2.0)
        payload = _inputs(profile, session_id, challenge, work_end, private, phone)
        data = json.dumps(payload, allow_nan=False).encode()
        _need(len(data) < MAX_PROOF)
        process = subprocess.Popen([sys.executable, '-I', str(Path(__file__).resolve()), '--worker'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            raw, _ = process.communicate(data, timeout=_remaining(work_end))
            _need(process.returncode == 0 and len(raw) <= MAX_PROOF)
            value = json.loads(raw)
            _validate_proof(value, payload, start, time.monotonic())
        finally:
            _reap(process)
        _remaining(end)
        return value
    except QuiescenceError:
        raise
    except Exception:
        raise QuiescenceError('voice_quiescence_unproven') from None


def _interrupt(*_args):
    raise QuiescenceError('voice_quiescence_unproven')


def _worker():
    # Also bound orphaned workers whose invoking parent died before cleanup.
    signal.signal(signal.SIGALRM, _interrupt)
    signal.signal(signal.SIGTERM, _interrupt)
    signal.setitimer(signal.ITIMER_REAL, MAX_SECONDS)
    try:
        raw = sys.stdin.buffer.read(MAX_PROOF + 1)
        _need(len(raw) <= MAX_PROOF)
        payload = json.loads(raw)
        remaining = min(MAX_SECONDS, _remaining(payload['deadline']))
        signal.setitimer(signal.ITIMER_REAL, remaining)
        result = _collect(payload)
        output = json.dumps(result, allow_nan=False).encode()
        _need(len(output) <= MAX_PROOF)
        sys.stdout.buffer.write(output + b'\n')
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        # Never print raw AMI/HTTP errors, response bodies or credentials.
        sys.stdout.write('{"error":"voice_quiescence_unproven"}\n')
        return 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == '__main__':
    raise SystemExit(_worker() if sys.argv[1:] == ['--worker'] else 2)
