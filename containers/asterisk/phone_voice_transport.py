#!/usr/bin/env python3
"""Opt-in Cisco legacy RTP transport; importing this module starts no media.

The integration boundary is mono PCM16 little-endian at 8 kHz. Legacy Cisco
streams use RTP PT 0 (G.711 mu-law), 160 samples per packet. See
docs/voice-transport.md for the attended pilot and physical acceptance gates.
"""
import argparse
from collections import Counter
from functools import wraps
import importlib.util
import ipaddress
import json
import math
import multiprocessing
from pathlib import Path
import secrets
import signal
import socket
import struct
import sys
import threading
import time
from typing import NamedTuple

SAMPLE_RATE = 8000
FRAME_SAMPLES = 160
FRAME_SECONDS = 0.020
MAX_SESSION_SECONDS = 300
MAX_RTP_PACKETS = MAX_SESSION_SECONDS * (SAMPLE_RATE // FRAME_SAMPLES) + 100
MAX_DATAGRAM = 2048
SILENCE = b'\xff' * FRAME_SAMPLES
_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8',
    '192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24'))


class VoiceTransportError(ValueError):
    """A bounded public error code, never a private address or credential."""


class CommandRejected(VoiceTransportError):
    """Confirmed no-start rejection: this command did not acquire a stream."""


def _require(condition, reason):
    if not condition:
        raise VoiceTransportError(reason)


def _ipv4(value):
    _require(isinstance(value, str), 'invalid_ipv4')
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise VoiceTransportError('invalid_ipv4') from None
    _require(str(address) == value and any(address in net for net in _NETWORKS),
             'local_unicast_ipv4_required')
    # Reject obvious network/broadcast targets. Subnet-specific directed
    # broadcasts also require the operator's LAN routing/firewall policy.
    _require(int(address) & 255 not in (0, 255), 'host_ipv4_required')
    return str(address)


def _port(value, cisco=True):
    _require(type(value) is int and 1 <= value <= 65535, 'invalid_port')
    if cisco:
        _require(20480 <= value <= 32768 and value % 2 == 0, 'invalid_cisco_rtp_port')
    return value


def _duration(value, maximum=MAX_SESSION_SECONDS):
    _require(type(value) in (int, float) and 0 < value <= maximum and math.isfinite(value),
             'invalid_duration')
    return float(value)


def packet_budget(duration):
    """Finite 20-ms packet allowance with two seconds of scheduling margin."""
    return math.ceil(_duration(duration) / FRAME_SECONDS) + 100


def tx_uri(ip, port):
    return f'RTPTx:{_ipv4(ip)}:{_port(port)}'


def rx_uri(ip, port, volume=20):
    uri = f'RTPRx:{_ipv4(ip)}:{_port(port)}'
    if volume is not None:
        _require(type(volume) is int and 0 <= volume <= 100, 'invalid_volume')
        uri += f':{volume}'
    return uri


def stop_uris():
    return ('RTPTx:Stop', 'RTPRx:Stop')


def _decode_sample(value):
    value = ~value & 255
    magnitude = (((value & 15) << 3) + 132) << ((value >> 4) & 7)
    return 132 - magnitude if value & 128 else magnitude - 132


_DECODE = tuple(_decode_sample(value) for value in range(256))


def decode_ulaw(payload):
    _require(isinstance(payload, bytes) and len(payload) <= 8000, 'invalid_ulaw')
    return struct.pack('<' + 'h' * len(payload), *(_DECODE[value] for value in payload))


def encode_ulaw(pcm):
    """ITU G.711 mu-law quantization, without the removed audioop module."""
    _require(isinstance(pcm, bytes) and len(pcm) % 2 == 0 and len(pcm) <= 16000,
             'invalid_pcm16')
    result = bytearray()
    for (sample,) in struct.iter_unpack('<h', pcm):
        # Quantize to 14 bits before companding, including negative rounding.
        sample >>= 2
        mask = 0x7f if sample < 0 else 0xff
        if sample < 0:
            sample = -sample
        sample = min(sample, 8159) + 33
        segment = max(0, sample.bit_length() - 6)
        code = 0x7f if segment >= 8 else (segment << 4) | ((sample >> (segment + 1)) & 15)
        result.append(code ^ mask)
    return bytes(result)


class RtpPacket(NamedTuple):
    sequence: int
    timestamp: int
    ssrc: int
    payload: bytes
    marker: bool


def parse_rtp(data):
    _require(isinstance(data, bytes) and 12 <= len(data) <= MAX_DATAGRAM, 'invalid_rtp_size')
    flags, payload_type, sequence, timestamp, ssrc = struct.unpack('!BBHII', data[:12])
    _require(flags >> 6 == 2 and payload_type & 127 == 0, 'unsupported_rtp')
    offset = 12 + (flags & 15) * 4
    _require(offset <= len(data), 'truncated_csrc')
    if flags & 16:
        _require(offset + 4 <= len(data), 'truncated_extension')
        words = struct.unpack('!H', data[offset + 2:offset + 4])[0]
        offset += 4 + words * 4
        _require(offset <= len(data), 'truncated_extension')
    end = len(data)
    if flags & 32:
        padding = data[-1]
        _require(padding > 0 and padding <= end - offset, 'invalid_padding')
        end -= padding
    payload = data[offset:end]
    _require(len(payload) == FRAME_SAMPLES, 'invalid_g711_frame')
    return RtpPacket(sequence, timestamp, ssrc, payload, bool(payload_type & 128))


class RtpSender:
    def __init__(self, sequence=None, timestamp=None, ssrc=None):
        self.sequence = secrets.randbits(16) if sequence is None else sequence
        self.timestamp = secrets.randbits(32) if timestamp is None else timestamp
        self.ssrc = secrets.randbits(32) if ssrc is None else ssrc
        for value, bits in ((self.sequence, 16), (self.timestamp, 32), (self.ssrc, 32)):
            _require(type(value) is int and 0 <= value < 1 << bits, 'invalid_rtp_counter')
        self.first = True

    def packet(self, payload):
        _require(isinstance(payload, bytes) and len(payload) == FRAME_SAMPLES,
                 'invalid_g711_frame')
        result = struct.pack('!BBHII', 0x80, 0x80 if self.first else 0,
                             self.sequence, self.timestamp, self.ssrc) + payload
        self.first = False
        self.sequence = (self.sequence + 1) & 0xffff
        self.timestamp = (self.timestamp + FRAME_SAMPLES) & 0xffffffff
        return result


class RtpReceiver:
    """Small reorder window; no audio history, automatic relatching or resync.

    ingest/flush return PCMU frames. UdpMedia.receive exposes decoded PCM16.
    Source IP is mandatory. An unspecified source port is pinned together with
    SSRC only after the first syntactically valid 20 ms PT 0 packet.
    """
    def __init__(self, expected_ip, expected_port=None, reorder_window=5, max_dropout=50):
        self.expected_ip = _ipv4(expected_ip)
        self.source_port = None if expected_port is None else _port(expected_port, False)
        _require(type(reorder_window) is int and 1 <= reorder_window <= 10, 'invalid_reorder_window')
        _require(type(max_dropout) is int and reorder_window <= max_dropout <= 50, 'invalid_max_dropout')
        self.reorder_window, self.max_dropout = reorder_window, max_dropout
        self.ssrc = self.sequence = self.timestamp = None
        self.pending = {}
        self.counters = Counter()

    def ingest(self, data, source):
        if (not isinstance(source, tuple) or len(source) != 2 or source[0] != self.expected_ip
                or type(source[1]) is not int or not 1 <= source[1] <= 65535
                or self.source_port is not None and source[1] != self.source_port):
            self.counters['source_rejected'] += 1
            return []
        try:
            packet = parse_rtp(data)
        except VoiceTransportError:
            self.counters['malformed'] += 1
            return []
        if self.ssrc is not None and packet.ssrc != self.ssrc:
            self.counters['ssrc_rejected'] += 1
            return []
        if self.sequence is None:
            self.sequence, self.timestamp = packet.sequence, packet.timestamp
            self.ssrc, self.source_port = packet.ssrc, source[1]
        delta = (packet.sequence - self.sequence) & 0xffff
        if delta >= 0x8000:
            self.counters['late'] += 1
            return []
        if delta > self.max_dropout:
            self.counters['discontinuity'] += 1
            return []
        if packet.timestamp != (self.timestamp + delta * FRAME_SAMPLES) & 0xffffffff:
            self.counters['timestamp_rejected'] += 1
            return []
        if packet.sequence in self.pending:
            self.counters['duplicates'] += 1
            return []
        self.counters['received'] += 1
        self.pending[packet.sequence] = packet.payload
        return self._drain(force=False)

    def _drain(self, force):
        frames = []
        while self.pending:
            payload = self.pending.pop(self.sequence, None)
            if payload is None:
                ahead = max((seq - self.sequence) & 0xffff for seq in self.pending)
                if not force and ahead < self.reorder_window:
                    break
                payload = SILENCE
                self.counters['lost'] += 1
            frames.append(payload)
            self.counters['emitted'] += 1
            self.sequence = (self.sequence + 1) & 0xffff
            self.timestamp = (self.timestamp + FRAME_SAMPLES) & 0xffffffff
        return frames

    def flush(self):
        return self._drain(force=True)


class UdpMedia:
    """One exclusive local socket, exact peer, finite budgets, 20 ms output.

    No recording or callbacks retain received audio. A consumer owns any PCM
    it receives. Set a monotonic deadline before open when used standalone.
    """
    def __init__(self, bind_ip, bind_port, phone_ip, phone_port=None,
                 expected_source_port=None, max_packets=MAX_RTP_PACKETS, clock=time.monotonic):
        self.bind_ip, self.bind_port = _ipv4(bind_ip), _port(bind_port)
        self.phone_ip = _ipv4(phone_ip)
        self.phone_port = None if phone_port is None else _port(phone_port)
        _require(type(max_packets) is int and 1 <= max_packets <= MAX_RTP_PACKETS, 'invalid_packet_budget')
        self.receiver = RtpReceiver(phone_ip, expected_source_port)
        self.sender = RtpSender()
        self.max_packets, self.clock = max_packets, clock
        self.counters = Counter()
        self.sock = None
        self.deadline = None
        self.last_received = None
        self._next_send = 0.0
        self._pending_since = None

    def open(self):
        _require(self.sock is None, 'media_already_open')
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
            sock.bind((self.bind_ip, self.bind_port))
            sock.settimeout(0.01)
        except BaseException:
            sock.close()
            raise
        self.sock = sock

    def close(self):
        sock, self.sock = self.sock, None
        if sock is not None:
            sock.close()
        self.receiver.pending.clear()

    def _available(self):
        _require(self.sock is not None, 'media_closed')
        _require(self.deadline is None or self.clock() < self.deadline, 'duration_expired')

    def receive(self):
        self._available()
        _require(self.counters['datagrams'] < self.max_packets, 'receive_budget_exhausted')
        try:
            data, source = self.sock.recvfrom(MAX_DATAGRAM + 1)
        except socket.timeout:
            data = None
        frames = []
        if data is not None:
            self.counters['datagrams'] += 1
            before = self.receiver.counters['received']
            frames = self.receiver.ingest(data, source)
            if self.receiver.counters['received'] != before:
                self.last_received = self.clock()
        if self.receiver.pending:
            if self._pending_since is None:
                self._pending_since = self.clock()
            elif self.clock() - self._pending_since >= self.receiver.reorder_window * FRAME_SECONDS:
                frames += self.receiver.flush()
                self._pending_since = None
        else:
            self._pending_since = None
        return [decode_ulaw(frame) for frame in frames]

    def send_frame(self, payload):
        self._available()
        _require(self.phone_port is not None, 'playback_port_required')
        _require(isinstance(payload, bytes) and len(payload) == FRAME_SAMPLES, 'invalid_g711_frame')
        _require(self.counters['sent'] < self.max_packets, 'send_budget_exhausted')
        now = self.clock()
        if now < self._next_send:
            return False
        # Never send a catch-up burst following a slow consumer or scheduler.
        self.sock.sendto(self.sender.packet(payload), (self.phone_ip, self.phone_port))
        self._next_send = now + FRAME_SECONDS
        self.counters['sent'] += 1
        return True

    def send_pcm(self, pcm):
        _require(isinstance(pcm, bytes) and len(pcm) == FRAME_SAMPLES * 2, 'invalid_pcm16_frame')
        return self.send_frame(encode_ulaw(pcm))


def _locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class VoiceSession:
    """Single-use session. status() must fail closed and cover both lines.

    status returns idle/call/ringing/radio/disconnected/unknown. The coordinator
    never resumes: an explicit new session and renewed ownership are required.
    Poll/check regularly; a production host must provide independent watchdog
    coverage via arm(deadline), as the attended CLI does.
    """
    def __init__(self, execute, media, status, *, duration=10, enable_microphone=False,
                 enable_playback=False, volume=20, media_reserved=False,
                 clock=time.monotonic, arm=None):
        self.execute, self.media, self.status = execute, media, status
        self._lock = threading.RLock()
        self.duration = _duration(duration)
        _require(type(enable_microphone) is bool and type(enable_playback) is bool
                 and type(media_reserved) is bool, 'explicit_boolean_required')
        rx_uri(media.bind_ip, media.phone_port or media.bind_port, volume)
        self.enable_microphone, self.enable_playback = enable_microphone, enable_playback
        self.volume, self.media_reserved, self.clock, self.arm = volume, media_reserved, clock, arm
        self.state, self.reason = 'new', None
        self.deadline = None
        self.stop_errors = []
        self.stop_confirmed = False
        self._start_attempted = False
        self._directions = set()

    def _status(self):
        try:
            status = self.status()
        except Exception:
            return 'unknown'
        return status if status in ('idle', 'call', 'ringing', 'radio', 'disconnected') else 'unknown'

    def _guard(self):
        _require(self.state in ('starting', 'active'), 'session_stopped')
        _require(self.clock() < self.deadline, 'duration_expired')
        status = self._status()
        _require(self.state in ('starting', 'active'), 'session_stopped')
        _require(status == 'idle', 'preempted_' + status)
        # A slow state provider must never permit a start beyond the deadline.
        _require(self.clock() < self.deadline, 'duration_expired')

    @_locked
    def start(self):
        _require(self.state == 'new', 'session_not_new')
        _require(self.enable_microphone or self.enable_playback, 'media_not_enabled')
        _require(self.media_reserved, 'exclusive_media_required')
        _require(not self.enable_playback or self.media.phone_port is not None, 'playback_port_required')
        self.state = 'starting'
        self.deadline = self.clock() + self.duration
        self.media.deadline = self.deadline
        try:
            self._guard()
            self.media.open()
            self._guard()
            if self.arm is not None:
                self.arm(self.deadline)
            for uri in ([tx_uri(self.media.bind_ip, self.media.bind_port)] if self.enable_microphone else []) + (
                    [rx_uri(self.media.bind_ip, self.media.phone_port, self.volume)] if self.enable_playback else []):
                self._guard()
                direction = uri.split(':', 1)[0]
                self._directions.add(direction)
                self._start_attempted = True
                try:
                    self.execute(uri)
                except CommandRejected:
                    self._directions.discard(direction)
                    raise
                self._guard()
            self.state = 'active'
            return self
        except BaseException:
            if self.state == 'stopped':
                # A start that completed after a concurrent stop must not
                # revive media. Retry only the directions this session owns.
                self._stop_remote()
            else:
                self.stop('start_failed')
            raise

    @_locked
    def check(self):
        if self.state != 'active':
            return False
        try:
            self._guard()
        except VoiceTransportError as error:
            self.stop(str(error))
            return False
        return True

    @_locked
    def poll(self):
        """Return zero or more 320-byte PCM16LE frames; never retain audio."""
        if not self.check() or not self.enable_microphone:
            return []
        try:
            return self.media.receive()
        except (VoiceTransportError, OSError):
            self.stop('media_receive_failed')
            return []

    @_locked
    def play(self, pcm, *, allowed=None):
        """Send one PCM16LE frame if its optional cancellation guard permits.

        The bounded, synchronous allowed() callback runs under the session
        lock after focus checks, so a cancelled frame queued on that lock is
        discarded. It must return True to permit sending. False or an error
        returns False without stopping the session; omitted guards preserve
        the existing paced-send behavior. The caller must stop retrying when
        its generation is cancelled.
        """
        if not self.check() or not self.enable_playback:
            return False
        if allowed is not None:
            try:
                if allowed() is not True:
                    return False
            except Exception:
                return False
            if self.state != 'active':
                return False
        try:
            return self.media.send_pcm(pcm)
        except (VoiceTransportError, OSError):
            self.stop('media_send_failed')
            return False

    @_locked
    def stop(self, reason='explicit_stop'):
        if self.state == 'stopped':
            return
        self.state, self.reason = 'stopped', reason
        try:
            self.media.close()
        except Exception:
            self.stop_errors.append('local_close_failed')
        finally:
            self._stop_remote()

    def _stop_remote(self):
        remote_errors = []
        if self._directions:
            for uri in stop_uris():
                if uri.split(':', 1)[0] not in self._directions:
                    continue
                try:
                    self.execute(uri)
                except Exception:
                    remote_errors.append(uri.split(':')[0] + '_stop_failed')
            self.stop_errors.extend(remote_errors)
            self.stop_confirmed = not remote_errors

    @_locked
    def no_media_started(self, watchdog):
        """Seal a never-started lifecycle; empty counters are not ownership proof.

        A queued start cannot run after this check: stopped is terminal and
        start() uses the same lock. Even a failed watchdog arm is excluded.
        """
        return (self.state == 'stopped' and self._start_attempted is False
                and self.media.sock is None and self.stop_errors == []
                and self.stop_confirmed is False
                and getattr(watchdog, '_arm_attempted', None) is False
                and getattr(watchdog, 'process', object()) is None)

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()


def _gateway_module():
    name = '_cisco_voice_gateway'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name('phone_gateway.py'))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def gateway_phone_status(gateway, config, phone):
    """Read-only AMI snapshot; a channel on either line, including held, wins."""
    with gateway.AMI(config['ami']) as ami:
        endpoints, channels = ami.snapshot()
    if gateway.own_channels(phone, channels):
        return 'call'
    states = [endpoints.get(line) for line in phone['lines']]
    if any(state in (None, 'Unavailable', 'Invalid') for state in states):
        return 'disconnected'
    return 'idle' if all(state == 'Not in use' for state in states) else 'call'


class _GatewayObserver:
    """Slow AMI cannot block RTP/control ticks; stale snapshots fail closed."""
    def __init__(self, gateway, config, phone):
        self.gateway, self.config, self.phone = gateway, config, phone
        self.observation = ('unknown', 0.0)
        self.done = threading.Event()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.done.is_set():
            started = time.monotonic()
            try:
                status = gateway_phone_status(self.gateway, self.config, self.phone)
            except Exception:
                status = 'disconnected'
            # Age from request start, not response receipt: a slow response is
            # not fresh evidence that the phone remains idle.
            self.observation = (status, started)
            self.ready.set()
            self.done.wait(0.25)

    def status(self):
        status, at = self.observation
        return status if time.monotonic() - at <= 1.0 else 'unknown'


def _execute_worker(pipe, phone, uri, deadline):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        if not uri.endswith(':Stop') and time.monotonic() >= deadline:
            raise CommandRejected('duration_expired')
        _gateway_module().execute(phone, uri)
        result = 'ok'
    except Exception as error:
        # Only parse/busy (1) and unauthorized (4) prove no start. Cisco error
        # 2 is failure to frame the RESPONSE, so media may already be active.
        # Other internal/unknown statuses conservatively remain ambiguous.
        status = getattr(error, 'response_code', None)
        result = ('rejected' if isinstance(error, CommandRejected)
                  or type(status) is int and status in (1, 4) else 'error')
    try:
        pipe.send(result)
    except (EOFError, OSError):
        pass
    pipe.close()


def _bounded_execute(phone, uri, deadline):
    """Hard process deadline around the existing gateway's socket timeout."""
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe(duplex=False)
    worker = context.Process(target=_execute_worker, args=(child, phone, uri, deadline))
    try:
        worker.start()
        child.close()
        if not parent.poll(2.0):
            raise VoiceTransportError('execute_timeout')
        result = parent.recv()
        if result == 'rejected':
            raise CommandRejected('phone_rejected_command')
        _require(result == 'ok', 'execute_failed')
    finally:
        parent.close()
        child.close()
        if worker.pid is not None:
            worker.join(0.05)
            if worker.is_alive():
                worker.terminate()
                worker.join(0.2)
            if worker.is_alive():
                worker.kill()
                worker.join(0.2)


def _watchdog_loop(pipe, execute, deadline, allowed_starts, *, clock=time.monotonic,
                   sleep=time.sleep, retry_delay=10.5):
    """Supervisor owns command ordering and stream leases, even after EOF.

    execute must have a hard time bound. A confirmed rejection never obtains
    a lease; an ambiguous start retains one through a delayed cleanup sweep.
    No main-process acknowledgment is needed to preserve that distinction.
    """
    owned, uncertain = set(), set()
    pipe.send({'ready': True})
    try:
        while clock() < deadline:
            if not pipe.poll(max(0, deadline - clock())):
                break
            message = pipe.recv()
            if message == 'finish':
                break
            if not isinstance(message, tuple) or len(message) != 3 or message[0] != 'execute':
                break
            request_id, uri = message[1:]
            if uri not in allowed_starts and uri not in stop_uris():
                pipe.send({'request_id': request_id, 'result': 'rejected'})
                continue
            direction = uri.split(':', 1)[0]
            stopping = uri.endswith(':Stop')
            if not stopping and (clock() >= deadline or direction in owned):
                pipe.send({'request_id': request_id, 'result': 'rejected'})
                continue
            if stopping and direction not in owned:
                pipe.send({'request_id': request_id, 'result': 'ok'})
                continue
            if not stopping:
                owned.add(direction)
            try:
                execute(uri)
                if stopping:
                    owned.discard(direction)
                result = 'ok'
            except CommandRejected:
                if not stopping:
                    owned.discard(direction)
                result = 'rejected'
            except Exception:
                if not stopping:
                    uncertain.add(direction)
                result = 'error'
            pipe.send({'request_id': request_id, 'result': result})
    except (EOFError, OSError):
        pass
    targets = owned | uncertain
    errors = 0
    if targets:
        for attempt in range(2):
            if attempt:
                sleep(retry_delay)
            for uri in stop_uris():
                if uri.split(':', 1)[0] not in targets:
                    continue
                try:
                    execute(uri)
                except Exception:
                    errors += 1
    result = {'watchdog_triggered': bool(targets), 'watchdog_stop_errors': errors,
              'watchdog_stop_confirmed': bool(targets) and errors == 0,
              'watchdog_stop_directions': sorted(targets)}
    try:
        pipe.send(result)
    except (EOFError, OSError):
        pass
    pipe.close()
    return result


def _watchdog_main(pipe, phone, deadline, allowed_starts):
    # Survive a terminal interrupt or parent exit; workers remain killable.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if hasattr(signal, 'SIGHUP'):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    _watchdog_loop(pipe, lambda uri: _bounded_execute(phone, uri, deadline),
                   deadline, allowed_starts)


class _Watchdog:
    def __init__(self, phone, allowed_starts):
        self.phone = phone
        self.allowed_starts = tuple(allowed_starts)
        self.process = self.pipe = None
        self._arm_attempted = False
        self.report = {}
        self.request_id = 0

    def arm(self, deadline):
        self._arm_attempted = True
        context = multiprocessing.get_context('spawn')
        self.pipe, child = context.Pipe()
        self.process = context.Process(target=_watchdog_main,
                                       args=(child, self.phone, deadline, self.allowed_starts))
        self.process.start()
        child.close()
        _require(self.pipe.poll(min(5.0, max(0, deadline - time.monotonic()))), 'watchdog_not_ready')
        _require(self.pipe.recv().get('ready') is True, 'watchdog_not_ready')

    def execute(self, uri):
        _require(self.pipe is not None, 'watchdog_not_armed')
        self.request_id += 1
        self.pipe.send(('execute', self.request_id, uri))
        deadline = time.monotonic() + 3
        while True:
            _require(self.pipe.poll(max(0, deadline - time.monotonic())), 'watchdog_command_timeout')
            result = self.pipe.recv()
            if 'watchdog_triggered' in result:
                self.report.update(result)
                raise VoiceTransportError('watchdog_cleanup_started')
            if result.get('request_id') == self.request_id:
                break
        if result.get('result') == 'rejected':
            raise CommandRejected('phone_rejected_command')
        _require(result.get('result') == 'ok', 'watchdog_command_failed')

    def finish(self):
        if self.process is None:
            return {'watchdog_armed': False}
        try:
            self.pipe.send('finish')
        except (BrokenPipeError, EOFError, OSError):
            pass
        # At most four hard-bounded Stop workers + delayed retry sweep.
        self.process.join(23)
        result = {'watchdog_armed': True, 'watchdog_finished': not self.process.is_alive()}
        try:
            while self.pipe.poll():
                result.update(self.pipe.recv())
        except (EOFError, OSError):
            pass
        self.pipe.close()
        return {**result, **self.report}


def load_pilot_config(path):
    """Validate without network I/O. Configuration and credentials stay local."""
    with Path(path).open() as source:
        config = json.load(source)
    allowed = {'gateway_config', 'line', 'bind_ipv4', 'bind_port', 'phone_rtp_port',
               'duration_seconds', 'receive_idle_seconds', 'volume', 'expected_source_port',
               'media_transport'}
    _require(isinstance(config, dict) and not set(config) - allowed, 'invalid_pilot_config')
    _require(config.get('media_transport', 'xml_rtp') == 'xml_rtp', 'xml_rtp_transport_required')
    _require(isinstance(config.get('gateway_config'), str), 'gateway_config_required')
    _ipv4(config['bind_ipv4'])
    _port(config['bind_port'])
    _port(config['phone_rtp_port'])
    _duration(config['duration_seconds'])
    _duration(config.get('receive_idle_seconds', 3), 10)
    rx_uri(config['bind_ipv4'], config['phone_rtp_port'], config.get('volume', 20))
    if config.get('expected_source_port') is not None:
        _port(config['expected_source_port'], False)
    gateway = _gateway_module()
    with Path(config['gateway_config']).open() as source:
        private = gateway.validate(json.load(source))
    matches = [phone for phone in private['phones'] if config.get('line') in phone['lines']]
    _require(len(matches) == 1, 'unknown_phone_line')
    return config, private, matches[0]


def _tone_frame():
    # Quiet synthetic 400 Hz reference; never microphone loopback.
    return struct.pack('<' + 'h' * FRAME_SAMPLES,
                       *(int(1000 * math.sin(2 * math.pi * 400 * i / SAMPLE_RATE))
                         for i in range(FRAME_SAMPLES)))


def run_pilot(config, private, phone, *, enable_microphone=False, playback_tone=False):
    gateway = _gateway_module()
    gateway.private_ip(config['bind_ipv4'])
    observer = _GatewayObserver(gateway, private, phone)
    observer.thread.start()
    media = UdpMedia(config['bind_ipv4'], config['bind_port'], phone['ipv4'],
                     config['phone_rtp_port'], config.get('expected_source_port'),
                     max_packets=packet_budget(config['duration_seconds']))
    starts = ([tx_uri(media.bind_ip, media.bind_port)] if enable_microphone else []) + (
        [rx_uri(media.bind_ip, media.phone_port, config.get('volume', 20))] if playback_tone else [])
    watchdog = _Watchdog(phone, starts)
    session = VoiceSession(watchdog.execute, media, observer.status,
                           duration=config['duration_seconds'], enable_microphone=enable_microphone,
                           enable_playback=playback_tone, volume=config.get('volume', 20),
                           media_reserved=True, arm=watchdog.arm)
    error = None
    watchdog_result = {}
    try:
        _require(observer.ready.wait(5) and observer.status() == 'idle', 'phone_not_confirmed_idle')
        session.start()
        tone = _tone_frame()
        while session.check():
            if enable_microphone:
                session.poll()  # Intentionally discard PCM; counters only.
                last = media.last_received or (session.deadline - session.duration)
                if time.monotonic() - last >= config.get('receive_idle_seconds', 3):
                    session.stop('receive_idle_timeout')
                    break
            if playback_tone:
                session.play(tone)
            time.sleep(0.002 if enable_microphone else 0.010)
    except KeyboardInterrupt:
        error = 'operator_interrupt'
    except Exception:
        # Do not print raw gateway/OS exceptions: they may contain credentials,
        # private addresses, HTTP response bodies or file paths.
        error = 'pilot_failed'
    finally:
        saved = {}
        if threading.current_thread() is threading.main_thread():
            for number in (signal.SIGINT, signal.SIGTERM, *([signal.SIGHUP] if hasattr(signal, 'SIGHUP') else [])):
                saved[number] = signal.signal(number, signal.SIG_IGN)
        try:
            session.stop(error or 'pilot_finished')
            observer.done.set()
            watchdog_result = watchdog.finish()
        finally:
            for number, handler in saved.items():
                signal.signal(number, handler)
    cleanup_confirmed = bool(
        (session.stop_confirmed or watchdog_result.get('watchdog_stop_confirmed', False))
        and watchdog_result.get('watchdog_finished')
        and not watchdog_result.get('watchdog_stop_errors'))
    return {'state': session.state, 'reason': session.reason, 'error': error,
            'stop_confirmed': session.stop_confirmed, 'stop_errors': session.stop_errors,
            'cleanup_confirmed': cleanup_confirmed,
            'capture_observed': bool(media.receiver.counters['received']),
            'playback_packets_sent': media.counters['sent'],
            'rtp': dict(media.receiver.counters), 'udp': dict(media.counters),
            'recorded_audio': False, **watchdog_result}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='private pilot JSON, normally under .local/')
    parser.add_argument('--run-pilot', action='store_true', help='perform live bounded media actions')
    parser.add_argument('--attended', action='store_true', help='operator is physically present at the phone')
    parser.add_argument('--exclusive-media', action='store_true', help='phone reserved; no other XML/radio controller')
    parser.add_argument('--enable-microphone', action='store_true', help='explicitly authorize phone microphone RTP')
    parser.add_argument('--playback-tone', action='store_true', help='play a quiet synthetic reference tone')
    args = parser.parse_args(argv)
    live_entered = False
    try:
        if args.run_pilot:
            _require(args.attended and args.exclusive_media, 'attended_exclusive_pilot_required')
            _require(args.enable_microphone or args.playback_tone, 'media_not_enabled')
        config, private, phone = load_pilot_config(args.config)
        if not args.run_pilot:
            print(json.dumps({'mode': 'dry_run', 'valid': True,
                              'duration_seconds': config['duration_seconds'], 'network_actions': 0}))
            return 0
        def interrupt(_number, _frame):
            raise KeyboardInterrupt
        saved_signals = {number: signal.signal(number, interrupt) for number in
                         (signal.SIGTERM, *([signal.SIGHUP] if hasattr(signal, 'SIGHUP') else []))}
        try:
            live_entered = True
            result = run_pilot(config, private, phone, enable_microphone=args.enable_microphone,
                               playback_tone=args.playback_tone)
        finally:
            for number, handler in saved_signals.items():
                signal.signal(number, handler)
        print(json.dumps(result, sort_keys=True))
        return 0 if (not result['error'] and result['cleanup_confirmed']
                     and result.get('watchdog_finished')
                     and not result.get('watchdog_stop_errors')) else 1
    except Exception:
        if live_entered:
            print(json.dumps({'error': 'pilot_failed', 'stop_confirmed': False,
                              'physical_stop_check_required': True}))
        else:
            print(json.dumps({'error': 'invalid_or_unavailable_pilot_configuration', 'network_actions': 0}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
