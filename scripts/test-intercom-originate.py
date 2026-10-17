#!/usr/bin/env python3
"""Exercise real intercom bridges and redirect refusal in a network-none PBX.

Uses only generated synthetic accounts and loopback SIP peers. The gateway's
real intercom state machine runs unchanged; its Cisco Speaker transport is
replaced with a synthetic SIP answer. No existing container can be selected.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


helper = module('intercom_fixture_helpers', Path(__file__).with_name('test-alert-originate.py'))
need, await_condition = helper.need, helper.await_condition


class Phone(helper.Phone):
    """One loopback phone with two SIP identities and a redirect trap."""
    def __init__(self, address):
        self.address, self.redirect = address, False
        self.sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sip.bind((address, 5090))
        self.audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.audio.bind((address, 6000))
        self.forward = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.forward.bind((address, 5091))
        self.calls, self.audio_packets, self.forward_packets = {}, 0, 0
        self.lock = threading.RLock()
        self.stopped, self.error = threading.Event(), None
        self.thread = threading.Thread(target=self.listen, daemon=True)
        self.thread.start()

    def reply(self, message, address, status, body='', contact=None):
        if status == '180 Ringing' and self.redirect:
            self.calls[helper.headers(message)['call-id']]['reject'] = helper.time.monotonic()
            status = '302 Moved Temporarily'
            contact = 'sip:redirect-trap@' + self.address + ':5091'
        return super().reply(message, address, status, body, contact)

    def answer(self):
        with self.lock:
            active = [call for call in self.calls.values()
                      if not any(call[key] for key in ('cancel', 'bye', 'answer', 'reject'))]
            need(len(active) == 1 and not self.redirect, 'Speaker must target one isolated ringing phone')
            call = active[0]
            call['answer'] = helper.time.monotonic()
            line = call['request'].split(' ', 2)[1].split(':', 1)[1].split('@')[0]
            body = (f'v=0\r\no=fixture 1 1 IN IP4 {self.address}\r\ns=intercom-fixture\r\n'
                    f'c=IN IP4 {self.address}\r\nt=0 0\r\nm=audio 6000 RTP/AVP 0\r\n'
                    'a=rtpmap:0 PCMU/8000\r\na=sendrecv\r\n')
            self.reply(call['request'], call['address'], '200 OK', body,
                       f'sip:{line}@{self.address}:5090')


def test_inside():
    need({name for _, name in socket.if_nameindex()} == {'lo'}, 'fixture requires network-none namespace')
    gateway = module('intercom_gateway', Path(__file__).with_name('phone_gateway.py'))
    secret = json.loads(Path('/etc/asterisk/intercom-fixture-secret.json').read_text())
    ami = helper.AMI(secret)
    peers = {'201': Phone('127.0.0.2'), '203': Phone('127.0.0.3')}
    config = {'persistent_enabled': True, 'bind_ipv4': '192.168.50.10', 'api_port': 18084,
              'api_clients': ['192.168.50.11'], 'api_token': 't' * 48,
              'ami': {'host': '192.168.50.10', 'port': 5038, **secret},
              'phones': [], 'dial_allowlist': ['201', '202', '203', '204']}
    for index, (phone_id, lines) in enumerate((('201', ['201', '202']), ('203', ['203', '204']))):
        config['phones'].append({'id': phone_id, 'lines': lines, 'ipv4': f'192.168.50.{21 + index}',
            'device': f'SEP02000000000{index + 1}', 'port': 18080 + index,
            'auth_path': '/auth/' + str(index + 1) * 64, 'username': 'u' * 24, 'password': 'p' * 24})
    server = gateway.Gateway(config, '/tmp/intercom-fixture-state')
    # Only the fixture changes the validated config to loopback after creation.
    # No HTTP listeners or phone CGI requests are created by this test.
    server.config['ami']['host'] = '127.0.0.1'
    presses = []

    def speaker(phone, uri, priority=0):
        need(uri == 'Key:Speaker', 'intercom emitted a non-Speaker control')
        presses.append(phone['id'])
        peer = peers[phone['id']]
        if peer.redirect:
            return  # SIP already rejected the call; never answer a redirected leg.
        peer.answer()

    gateway.execute = speaker
    result = {'network': 'none', 'synthetic_only': True, 'physical_phones_called': False, 'cases': {}}
    try:
        need(ami.action('CoreSettings')[0].get('AsteriskVersion') == '22.11.0', 'unexpected Asterisk version')
        need(not ami.channels(), 'fixture must start idle')

        def endpoints_ready():
            with gateway.AMI(server.config['ami']) as probe:
                endpoints, _ = probe.snapshot()
            return all(endpoints.get(line) == 'Not in use' for line in ('201', '202', '203', '204'))

        await_condition(endpoints_ready, 8, 'synthetic endpoints ready')
        before = [server.preference(phone_id) for phone_id in peers]
        connected = server.mutate('201', 'intercom',
            {'line': '202', 'number': '204', 'idempotency_key': 'fixture-connect-secondary-lines'})
        need(connected['connection_state'] == 'connected' and connected['speaker_requested'], 'intercom not connected')
        root = connected['intercom_id']

        def optimized():
            channels = ami.channels()
            return channels if len(channels) == 2 and all(ch['Channel'].startswith('PJSIP/') for ch in channels) else None

        channels = await_condition(optimized, 5, 'Local optimization into physical bridge')
        need({ch['Channel'].split('/')[1].split('-')[0] for ch in channels} == {'202', '204'}, 'wrong selected lines')
        need(all(ch.get('Linkedid') in (root, root + '-b') for ch in channels), 'optimized legs lost operation identity')
        need(all(server.is_intercom_channel(ch) for ch in channels), 'optimized legs lost auto-answer suppression')
        need(all(ch.get('ChannelStateDesc') == 'Up' for ch in channels)
             and channels[0].get('BridgeId') and channels[0]['BridgeId'] == channels[1].get('BridgeId'),
             'physical legs do not share one live bridge')
        need(presses == ['201', '203'], 'Speaker was repeated or selected the wrong phone')
        need(before == [server.preference(phone_id) for phone_id in peers], 'persistent preferences changed')
        with gateway.AMI(server.config['ami']) as cleanup:
            server.stop_intercom(cleanup, root)
        await_condition(lambda: not ami.channels(), 5, 'optimized bridge cleanup')
        result['cases']['optimized_bridge'] = {'exact_lines': ['202', '204'], 'speaker_presses': 2,
            'one_physical_bridge': True, 'linked_ids_preserved': True, 'channels_after': 0}

        for mode, redirect_phone in (('source_redirect', '201'), ('destination_redirect', '203')):
            await_condition(endpoints_ready, 5, 'endpoints idle before ' + mode)
            presses.clear()
            peers[redirect_phone].redirect = True
            before_invites = {phone_id: len(peer.calls) for phone_id, peer in peers.items()}
            try:
                server.mutate('201', 'intercom', {'line': '202', 'number': '204', 'idempotency_key': 'fixture-' + mode})
            except gateway.Refused as exc:
                need(exc.reason == 'intercom_not_connected_check_status', 'unexpected redirect result')
            else:
                raise RuntimeError('redirected intercom reported success')
            await_condition(lambda: not ami.channels(), 5, mode + ' cleanup')
            need(all(peer.forward_packets == 0 for peer in peers.values()), 'intercom followed a SIP redirect')
            if redirect_phone == '201':
                need(len(peers['203'].calls) == before_invites['203'], 'source redirect reached destination')
            else:
                need(presses.count('201') == 1, 'source Speaker not answered exactly once')
            need(all(presses.count(phone_id) <= 1 for phone_id in peers), 'Speaker repeated during refusal')
            need(all(peer.error is None for peer in peers.values()), 'synthetic SIP worker failed')
            result['cases'][mode] = {'followed': False, 'channels_after': 0}
            peers[redirect_phone].redirect = False
        result['status'] = 'PASS'
        return result
    finally:
        try:
            with gateway.AMI(server.config['ami']) as cleanup:
                for (root,) in server.db.execute('SELECT root FROM intercoms').fetchall():
                    server.stop_intercom(cleanup, root)
        finally:
            server.db.close()
            ami.close()
            for peer in peers.values():
                peer.close()


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
    docker = ['docker'] + (['--context', args.docker_context] if args.docker_context else [])
    provision = module('intercom_provision', Path(__file__).with_name('provision.py'))
    container = None
    with tempfile.TemporaryDirectory(prefix='cisco-intercom-fixture-') as temporary:
        root = Path(temporary)
        site = {'node_ipv4': '127.0.0.1', 'lan_cidr': '127.0.0.0/8', 'ntp_ipv4': '127.0.0.1',
                'phone_cidrs': ['127.0.0.0/8'], 'gateway': {'enabled': True, 'ami_client_ipv4': '127.0.0.1'}}
        inventory = {'phones': []}
        for index, lines in enumerate((['201', '202'], ['203', '204'])):
            inventory['phones'].append({'model': 'CP-7945G', 'verified': True, 'mac': f'02000000000{index + 1}',
                'label': 'Fixture', 'transport': 'udp', 'firmware_load': 'SIP45.9-4-2SR4-3S',
                'lines': [{'extension': line, 'label': line} for line in lines]})
        provision.generate(site, inventory, root)
        folder = root / 'asterisk'
        pjsip = folder / 'pjsip.conf'
        content = pjsip.read_text()
        for line, address in (('201', '127.0.0.2'), ('202', '127.0.0.2'), ('203', '127.0.0.3'), ('204', '127.0.0.3')):
            needle = f'[{line}]\ntype=aor\n'
            need(content.count(needle) == 1, 'missing synthetic AOR')
            content = content.replace(needle, needle + f'contact=sip:{line}@{address}:5090\n')
            # A full-URI redirect has a real loopback trap destination. The
            # default user-only redirect could fail for a missing dialplan
            # extension, falsely making a missing Dial(i) look safe.
            endpoint = f'[{line}]\ntype=endpoint\n'
            need(content.count(endpoint) == 1, 'missing synthetic endpoint')
            content = content.replace(endpoint, endpoint + 'redirect_method=uri_core\n')
        pjsip.write_text(content)
        (folder / 'intercom-fixture-secret.json').write_bytes((root / 'gateway-ami-secret.json').read_bytes())
        for name in ('test-intercom-originate.py', 'test-alert-originate.py'):
            (folder / name).write_bytes(Path(__file__).with_name(name).read_bytes())
        (folder / 'phone_gateway.py').write_bytes(
            (Path(__file__).resolve().parents[1] / 'containers/asterisk/phone_gateway.py').read_bytes())
        try:
            created = subprocess.run(docker + ['create', '--pull', 'never', '--network', 'none', '--user', '0:0', args.image],
                                     capture_output=True, text=True, check=True, timeout=15)
            container = created.stdout.strip()
            checked = subprocess.run(docker + ['inspect', '--format', '{{.HostConfig.NetworkMode}}', container],
                                     capture_output=True, text=True, check=True, timeout=10)
            need(checked.stdout.strip() == 'none', 'fixture isolation not applied')
            subprocess.run(docker + ['cp', str(folder) + '/.', container + ':/etc/asterisk/'],
                           capture_output=True, check=True, timeout=15)
            subprocess.run(docker + ['start', container], capture_output=True, check=True, timeout=15)

            def ready():
                response = subprocess.run(docker + ['exec', container, 'asterisk', '-rx', 'dialplan show 202@cisco-intercom'],
                                          capture_output=True, text=True, timeout=5)
                return 'PJSIP/202,8,i' in response.stdout

            await_condition(ready, 25, 'generated intercom dialplan loaded')
            completed = subprocess.run(docker + ['exec', container, 'python3', '/etc/asterisk/test-intercom-originate.py', '--inside'],
                                       capture_output=True, text=True, timeout=65)
            need(completed.returncode == 0, 'isolated intercom regression failed: ' + completed.stdout[-3000:] + completed.stderr[-3000:])
            result = json.loads(completed.stdout)
            need(result.get('status') == 'PASS', 'isolated intercom regression did not pass')
            print(json.dumps(result, sort_keys=True))
        finally:
            if container:
                subprocess.run(docker + ['rm', '-f', container], capture_output=True, check=True, timeout=15)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'FAIL', 'error': str(error)}), flush=True)
        raise SystemExit(1)
