#!/usr/bin/env python3
"""Real Asterisk alert regression in a disposable --network none container.

Only generated synthetic credentials and loopback SIP/RTP are used. A separate
container is mandatory: this script never accepts an existing PBX/container.

Contracts: Dial without a timeout waits until answer/hangup/failure; option i
ignores forwarding; U(...)/GOSUB_RESULT=ABORT ends both legs before bridging.
WaitForSignal without its optional timeout waits indefinitely. The Originate
5000ms timeout covers only the immediately answered Local channel.
https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/Dialplan_Applications/Dial/
https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/Dialplan_Applications/WaitForSignal/
https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/AMI_Actions/Originate/
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid


def need(value, message):
    if not value:
        raise RuntimeError(message)


class AMI:
    def __init__(self, secret):
        self.sock = socket.create_connection(('127.0.0.1', 5038), timeout=3)
        self.file = self.sock.makefile('rb')
        need(self.file.readline(1024).startswith(b'Asterisk Call Manager/'), 'invalid fixture AMI banner')
        self.action('Login', Username=secret['username'], Secret=secret['secret'], Events='off')

    def close(self):
        self.file.close()
        self.sock.close()

    def action(self, action, complete=None, **fields):
        identity = uuid.uuid4().hex
        fields = {'Action':action, 'ActionID':identity, **fields}
        need(not any(re.search(r'[\r\n\x00]', str(v)) for v in fields.values()), 'invalid AMI field')
        self.sock.sendall(('\r\n'.join(f'{key}: {value}' for key,value in fields.items()) + '\r\n\r\n').encode())
        result, size, deadline = [], 0, time.monotonic() + 5
        while time.monotonic() < deadline:
            frame = {}
            while True:
                raw = self.file.readline(16385)
                size += len(raw)
                need(raw and len(raw) <= 16384 and size < 1_000_000, 'invalid AMI response')
                if raw == b'\r\n':
                    break
                key, sep, value = raw.decode().rstrip('\r\n').partition(':')
                if sep:
                    frame[key] = value.strip()
            if frame.get('ActionID') != identity:
                continue
            need(frame.get('Response') != 'Error', 'fixture AMI action rejected: ' + action)
            result.append(frame)
            if (complete and frame.get('Event') == complete) or (not complete and 'Response' in frame):
                return result
        raise RuntimeError('fixture AMI timeout')

    def channels(self):
        return [frame for frame in self.action('CoreShowChannels', complete='CoreShowChannelsComplete')
                if frame.get('Event') == 'CoreShowChannel']

    def value(self, channel, variable):
        return self.action('Getvar', Channel=channel, Variable=variable)[0].get('Value', '')


def headers(message):
    return {key.lower():value.strip() for line in message.split('\r\n')[1:]
            if (parts := line.partition(':'))[1] for key,value in [(parts[0],parts[2])]}


class Phone:
    """Loopback SIP UAS. It sends 180 on INVITE; never autoanswers."""
    def __init__(self):
        self.sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sip.bind(('127.0.0.2', 5090))
        self.audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.audio.bind(('127.0.0.2', 6000))
        self.forward = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.forward.bind(('127.0.0.2', 5091))
        self.calls, self.audio_packets, self.forward_packets = {}, 0, 0
        self.lock = threading.RLock()
        self.stopped, self.error = threading.Event(), None
        self.thread = threading.Thread(target=self.listen, daemon=True)
        self.thread.start()

    def reply(self, message, address, status, body='', contact=None):
        h = headers(message)
        to = h['to'] if ';tag=' in h['to'] else h['to'] + ';tag=alert-fixture'
        rows = ['SIP/2.0 ' + status, 'Via: ' + h['via'], 'From: ' + h['from'],
                'To: ' + to, 'Call-ID: ' + h['call-id'], 'CSeq: ' + h['cseq']]
        if contact:
            rows.append('Contact: <' + contact + '>')
        if body:
            rows.append('Content-Type: application/sdp')
        rows += ['Content-Length: ' + str(len(body)), '', body]
        self.sip.sendto('\r\n'.join(rows).encode(), address)

    def listen(self):
        try:
            while not self.stopped.is_set():
                ready, _, _ = select.select([self.sip, self.audio, self.forward], [], [], .1)
                for sock in ready:
                    raw, address = sock.recvfrom(65535)
                    if sock is self.audio:
                        self.audio_packets += 1
                        continue
                    if sock is self.forward:
                        self.forward_packets += 1
                        continue
                    message = raw.decode('ascii')
                    h, method = headers(message), message.split(' ', 1)[0]
                    need(address[0] == '127.0.0.1', 'unexpected non-loopback SIP source')
                    with self.lock:
                        if method == 'INVITE':
                            call = self.calls.setdefault(h['call-id'], {'request':message, 'address':address,
                                'first_invite':time.monotonic(), 'invites':0, 'cancel':None, 'bye':None,
                                'answer':None, 'reject':None, 'ack':None})
                            call['invites'] += 1
                            self.reply(message, address, '180 Ringing')
                        elif method == 'CANCEL':
                            call = self.calls[h['call-id']]
                            call['cancel'] = time.monotonic()
                            self.reply(message, address, '200 OK')
                            self.reply(call['request'], call['address'], '487 Request Terminated')
                        elif method == 'BYE':
                            self.calls[h['call-id']]['bye'] = time.monotonic()
                            self.reply(message, address, '200 OK')
                        elif method == 'ACK':
                            self.calls[h['call-id']]['ack'] = time.monotonic()
                        elif method == 'OPTIONS':
                            self.reply(message, address, '200 OK')
        except BaseException as exc:
            self.error = type(exc).__name__ + ': ' + str(exc)

    def respond(self, call, mode):
        with self.lock:
            if mode == 'answer':
                call['answer'] = time.monotonic()
                body = ('v=0\r\no=fixture 1 1 IN IP4 127.0.0.2\r\ns=alert-fixture\r\n'
                        'c=IN IP4 127.0.0.2\r\nt=0 0\r\nm=audio 6000 RTP/AVP 0\r\n'
                        'a=rtpmap:0 PCMU/8000\r\na=sendrecv\r\n')
                self.reply(call['request'], call['address'], '200 OK', body,
                           'sip:201@127.0.0.2:5090')
            elif mode == 'reject':
                call['reject'] = time.monotonic()
                self.reply(call['request'], call['address'], '603 Decline')
            elif mode == 'forward':
                self.reply(call['request'], call['address'], '302 Moved Temporarily',
                           contact='sip:forward-target@127.0.0.2:5091')
            else:
                raise RuntimeError('unknown fixture response')

    def close(self):
        self.stopped.set()
        self.thread.join(2)
        for sock in (self.sip, self.audio, self.forward):
            sock.close()
        need(not self.thread.is_alive(), 'fixture SIP worker not stopped')


def await_condition(predicate, seconds, description):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise RuntimeError('timeout: ' + description)


def fixture_pjsip(content):
    """Give the isolated peer a static contact and bounded qualification retry."""
    needle = '[201]\ntype=aor\n'
    need(content.count(needle) == 1, 'generated fixture AOR missing')
    need(content.count('qualify_frequency=60\n') == 1, 'unexpected fixture qualification settings')
    # The first OPTIONS can precede Phone.bind(). Only this disposable fixture
    # retries quickly; never change production qualification to mask readiness.
    return content.replace(needle, needle + 'contact=sip:201@127.0.0.2:5090\n').replace(
        'qualify_frequency=60\n', 'qualify_frequency=1\n')


def fixture_ready(docker, container):
    """Application registration alone can precede dialplan/PJSIP loading."""
    checks = (
        ('core show application WaitForSignal', r'WaitForSignal\('),
        ('dialplan show 201@cisco-alert', r'Dial\(PJSIP/201,,iU\(cisco-alert-answered\)\)'),
        ('pjsip show endpoints', r'^\s*Endpoint:\s+201/'),
    )
    for command, pattern in checks:
        response = subprocess.run(docker + ['exec', container, 'asterisk', '-rx', command],
                                  capture_output=True, text=True, timeout=5)
        if response.returncode != 0 or not re.search(pattern, response.stdout, re.M):
            return False
    return True


def endpoint_ready(ami, phone):
    need(phone.error is None, 'SIP fixture worker failed: ' + str(phone.error))
    endpoints = ami.action('PJSIPShowEndpoints', complete='EndpointListComplete')
    return any(frame.get('Event') == 'EndpointList' and frame.get('ObjectName') == '201'
               and frame.get('DeviceState') == 'Not in use' for frame in endpoints)


def failure_logs(docker, container):
    """Keep bounded diagnostics from this fixture, without masking its failure."""
    try:
        result = subprocess.run(docker + ['logs', '--tail', '80', container],
                                capture_output=True, text=True, timeout=5)
        print('Isolated alert fixture log tail:\n' + (result.stdout + result.stderr)[-12000:],
              file=sys.stderr)
    except (OSError, subprocess.SubprocessError):
        print('Isolated alert fixture logs unavailable.', file=sys.stderr)


def test_inside():
    need({name for _,name in socket.if_nameindex()} == {'lo'}, 'fixture requires network-none namespace')
    secret = json.loads(Path('/etc/asterisk/alert-fixture-secret.json').read_text())
    ami, phone, owned_ids = AMI(secret), Phone(), set()
    result = {'network':'none', 'synthetic_only':True, 'physical_phones_called':False, 'cases':{}}
    try:
        version = ami.action('CoreSettings')[0].get('AsteriskVersion', '')
        need(version == '22.11.0', 'fixture requires Asterisk22.11.0')
        result['asterisk_version'] = version
        need(not ami.channels(), 'fixture must start idle')
        # Phone is now listening. Wait for its OPTIONS response to make the
        # static contact available before the first asynchronous Originate.
        await_condition(lambda: endpoint_ready(ami, phone), 8, 'synthetic alert endpoint ready')

        def start():
            alert = uuid.uuid4().hex
            local_a, local_b = 'cisco-alert-' + alert + '-a', 'cisco-alert-' + alert + '-b'
            owned_ids.update((local_a, local_b))
            before = set(phone.calls)
            ami.action('Originate', Channel='Local/201@cisco-alert/n', Application='WaitForSignal',
                       Data='cisco-alert-' + alert, Timeout='5000', Async='true',
                       ChannelId=local_a, OtherChannelId=local_b, Variable='__CISCO_ALERT_ID=' + alert,
                       CallerID='Fixture timer <640>')
            def new_call():
                with phone.lock:
                    new = set(phone.calls) - before
                    need(len(new) <= 1, 'one alert created more than one SIP dialog')
                    return phone.calls[next(iter(new))] if new else None
            call = await_condition(new_call, 5, 'alert INVITE')
            def channel():
                candidates = [c for c in ami.channels() if c.get('Linkedid') == local_a
                              and c.get('Channel','').startswith('PJSIP/201-')]
                need(len(candidates) <= 1, 'multiple physical alert legs')
                return candidates[0] if candidates else None
            physical = await_condition(channel, 3, 'physical alert channel')
            need(physical['ChannelStateDesc'] == 'Ringing', 'fixture unexpectedly answered')
            need(ami.value(physical['Channel'], 'CISCO_ALERT_ID') == alert, 'alert ID not inherited')
            roots = [c for c in ami.channels() if c.get('Uniqueid') in (local_a, local_b)]
            need({c['Uniqueid'] for c in roots} == {local_a, local_b}, 'Local roots not preserved')
            need(all(re.fullmatch(r'Local/201@cisco-alert-[0-9a-f]+;[12]', c['Channel'])
                     and c.get('Linkedid') == local_a for c in roots), 'Local ownership contract differs')
            owned_ids.add(physical['Uniqueid'])
            return call, physical, local_a

        call, physical, linked = start()
        # A 5s Originate timeout must expire on the answered Local leg only.
        # Holding >35s also catches an accidental 20/30s phone dial timeout.
        end = call['first_invite'] + 36
        while time.monotonic() < end:
            current = ami.channels()
            match = [c for c in current if c.get('Uniqueid') == physical['Uniqueid']]
            need(len(match) == 1 and match[0]['ChannelStateDesc'] == 'Ringing', 'alert stopped ringing before36s')
            need(all(c.get('Linkedid') == linked for c in current), 'unexpected fixture channel')
            need(call['cancel'] is None and call['answer'] is None and phone.error is None,
                 'alert cancelled or answered automatically')
            time.sleep(.5)
        held = time.monotonic() - call['first_invite']
        fresh = [c for c in ami.channels() if c['Channel'] == physical['Channel']
                 and c.get('Uniqueid') == physical['Uniqueid'] and c.get('Linkedid') == linked]
        need(len(fresh) == 1, 'exact stop identity changed')
        ami.action('Hangup', Channel=fresh[0]['Channel'], Cause='16')
        await_condition(lambda:call['cancel'], 5, 'CANCEL after exact stop')
        await_condition(lambda:not ami.channels(), 5, 'stop cleanup')
        need(call['answer'] is None and phone.audio_packets == 0, 'stop test sent audio or answered')
        result['cases']['ring_until_stop'] = {'ring_seconds':round(held, 3), 'cancel':True,
                                             'answered':False, 'channels_after':0}

        call, _, linked = start()
        roots = [c for c in ami.channels() if c.get('Uniqueid') == linked
                 and c.get('Linkedid') == linked
                 and re.fullmatch(r'Local/201@cisco-alert-[0-9a-f]+;[12]', c['Channel'])]
        need(len(roots) == 1, 'exact Local root changed before stop')
        ami.action('Hangup', Channel=roots[0]['Channel'], Cause='16')
        await_condition(lambda:call['cancel'], 5, 'CANCEL after Local root stop')
        await_condition(lambda:not ami.channels(), 5, 'Local root stop cleanup')
        need(call['answer'] is None and phone.audio_packets == 0, 'Local stop sent audio or answered')
        result['cases']['local_root_stop'] = {'cancel':True, 'answered':False, 'channels_after':0}

        call, _, _ = start()
        phone.respond(call, 'answer')
        await_condition(lambda:call['bye'], 5, 'immediate BYE after manual answer')
        await_condition(lambda:not ami.channels(), 5, 'answer cleanup')
        answer_to_bye = call['bye'] - call['answer']
        need(answer_to_bye < 3, 'answer did not immediately terminate alert')
        need(phone.audio_packets == 0, 'manual answer received unexpected RTP/audio')
        result['cases']['manual_answer'] = {'answer_to_bye_seconds':round(answer_to_bye, 3),
                                           'rtp_packets':0, 'channels_after':0}

        call, _, _ = start()
        phone.respond(call, 'reject')
        await_condition(lambda:not ami.channels(), 5, 'reject cleanup')
        count = len(phone.calls)
        time.sleep(3)
        need(len(phone.calls) == count and not ami.channels(), 'declined alert retried')
        result['cases']['manual_reject'] = {'no_retry_observed_seconds':3, 'channels_after':0}

        call, _, _ = start()
        phone.respond(call, 'forward')
        await_condition(lambda:not ami.channels(), 5, 'forward refusal cleanup')
        time.sleep(.5)
        need(phone.forward_packets == 0 and phone.audio_packets == 0, 'alert followed SIP redirect or sent RTP')
        need(phone.error is None, 'SIP fixture worker failed')
        result['cases']['redirect'] = {'followed':False, 'channels_after':0}
        result['status'] = 'PASS'
        return result
    finally:
        # Only exact synthetic identities are eligible even inside this isolated PBX.
        try:
            for channel in ami.channels():
                if channel.get('Uniqueid') in owned_ids or channel.get('Linkedid') in owned_ids:
                    ami.action('Hangup', Channel=channel['Channel'], Cause='16')
        finally:
            ami.close()
            phone.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default=os.environ.get('ASTERISK_TEST_IMAGE'))
    parser.add_argument('--docker-context', default=os.environ.get('ASTERISK_DOCKER_CONTEXT'))
    parser.add_argument('--inside', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inside:
        print(json.dumps(test_inside(), sort_keys=True), flush=True)
        return
    need(args.image, '--image or ASTERISK_TEST_IMAGE is required')
    docker = ['docker'] + (['--context',args.docker_context] if args.docker_context else [])
    spec = importlib.util.spec_from_file_location('alert_provision', Path(__file__).with_name('provision.py'))
    provision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(provision)
    container = None
    with tempfile.TemporaryDirectory(prefix='cisco-alert-fixture-') as temporary:
        root = Path(temporary)
        site = {'node_ipv4':'127.0.0.1', 'lan_cidr':'127.0.0.0/8', 'ntp_ipv4':'127.0.0.1',
                'phone_cidrs':['127.0.0.0/8'], 'gateway':{'enabled':True,'ami_client_ipv4':'127.0.0.1'}}
        inventory = {'phones':[{'model':'CP-7945G','verified':True,'mac':'020000000001',
                               'extension':'201','label':'Fixture','transport':'udp',
                               'firmware_load':'SIP45.9-4-2SR4-3S'}]}
        provision.generate(site, inventory, root)
        folder = root/'asterisk'
        pjsip = folder/'pjsip.conf'
        pjsip.write_text(fixture_pjsip(pjsip.read_text()))
        (folder/'alert-fixture-secret.json').write_bytes((root/'gateway-ami-secret.json').read_bytes())
        (folder/'alert-fixture.py').write_bytes(Path(__file__).read_bytes())
        try:
            create = subprocess.run(docker + ['create','--pull','never','--network','none','--user','0:0',args.image],
                                    capture_output=True, text=True, check=True, timeout=15)
            container = create.stdout.strip()
            inspected = subprocess.run(docker + ['inspect','--format','{{.HostConfig.NetworkMode}}',container],
                                       capture_output=True, text=True, check=True, timeout=10)
            need(inspected.stdout.strip() == 'none', 'fixture isolation not applied')
            subprocess.run(docker + ['cp',str(folder)+'/.',container+':/etc/asterisk/'],
                           capture_output=True, check=True, timeout=15)
            subprocess.run(docker + ['start',container],capture_output=True,check=True,timeout=15)
            await_condition(lambda: fixture_ready(docker, container), 25,
                            'generated alert dialplan, application and endpoint loaded')
            completed = subprocess.run(docker + ['exec',container,'python3','/etc/asterisk/alert-fixture.py','--inside'],
                                       capture_output=True,text=True,timeout=85)
            need(completed.returncode == 0, 'isolated alert regression failed: ' + completed.stdout[-3000:] + completed.stderr[-3000:])
            result = json.loads(completed.stdout)
            need(result.get('status') == 'PASS', 'isolated alert regression did not pass')
            print(json.dumps(result, sort_keys=True))
        except Exception:
            if container:
                failure_logs(docker, container)
            raise
        finally:
            if container:
                subprocess.run(docker + ['rm','-f',container],capture_output=True,check=True,timeout=15)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status':'FAIL','error':str(error)}), flush=True)
        raise SystemExit(1)
