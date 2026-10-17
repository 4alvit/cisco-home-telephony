"""Loopback-only mock SIP peer, run inside a Docker --network none container."""
import json
import re
import socket
import time


def headers(message):
    result = {}
    for line in message.split('\r\n')[1:]:
        if ':' in line:
            key, value = line.split(':', 1)
            result.setdefault(key.lower(), value.strip())
    return result


peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
peer.bind(('127.0.0.2', 5090))
peer.settimeout(8)
print('READY', flush=True)
deadline = time.monotonic() + 8
while True:
    message, address = peer.recvfrom(65535)
    message = message.decode('ascii')
    if message.startswith('INVITE '):
        break
    if time.monotonic() > deadline:
        raise RuntimeError('No loopback INVITE')
request = headers(message)
body = ('v=0\r\no=fixture 1 1 IN IP4 127.0.0.2\r\ns=fixture\r\n'
        'c=IN IP4 127.0.0.2\r\nt=0 0\r\nm=audio 6000 RTP/AVP 0\r\n'
        'a=rtpmap:0 PCMU/8000\r\na=sendrecv\r\n')
response = '\r\n'.join([
    'SIP/2.0 200 OK', 'Via: ' + request['via'], 'From: ' + request['from'],
    'To: ' + request['to'] + ';tag=fixture-peer', 'Call-ID: ' + request['call-id'],
    'CSeq: ' + request['cseq'], 'Contact: <sip:fixture@127.0.0.2:5090>',
    'Content-Type: application/sdp', 'Content-Length: ' + str(len(body)), '', body])
peer.sendto(response.encode('ascii'), address)
while True:
    message, _ = peer.recvfrom(65535)
    if message.startswith(b'ACK '):
        break
    if time.monotonic() > deadline:
        raise RuntimeError('No loopback ACK')
# Asterisk applies endpoint ACLs to in-dialog requests too. A blanket deny on
# an outbound-only endpoint would reject this legitimate remote hangup.
bye = '\r\n'.join([
    'BYE sip:fixture@127.0.0.1:5070 SIP/2.0',
    'Via: SIP/2.0/UDP 127.0.0.2:5090;branch=z9hG4bK-fixture-bye',
    'From: ' + request['to'] + ';tag=fixture-peer', 'To: ' + request['from'],
    'Call-ID: ' + request['call-id'], 'CSeq: 2 BYE', 'Max-Forwards: 70',
    'Content-Length: 0', '', ''])
peer.sendto(bye.encode('ascii'), address)
while True:
    message, _ = peer.recvfrom(65535)
    if b'CSeq: 2 BYE\r\n' in message:
        assert message.startswith(b'SIP/2.0 200 '), message.split(b'\r\n')[0]
        break
    if time.monotonic() > deadline:
        raise RuntimeError('No BYE response')
# A new unsolicited request must not identify as the outbound provider.
invite = '\r\n'.join([
    'INVITE sip:201@127.0.0.1:5070 SIP/2.0',
    'Via: SIP/2.0/UDP 127.0.0.2:5090;branch=z9hG4bK-fixture-unsolicited',
    'From: <sip:external-sip-callcentric@127.0.0.2>;tag=fixture-stranger',
    'To: <sip:201@127.0.0.1>', 'Call-ID: fixture-unsolicited',
    'CSeq: 1 INVITE', 'Contact: <sip:fixture@127.0.0.2:5090>',
    'Max-Forwards: 70', 'Content-Type: application/sdp',
    'Content-Length: ' + str(len(body)), '', body])
peer.sendto(invite.encode('ascii'), address)
while True:
    message, _ = peer.recvfrom(65535)
    if b'Call-ID: fixture-unsolicited\r\n' in message and not message.startswith(b'SIP/2.0 100 '):
        assert message.startswith(b'SIP/2.0 401 '), message.split(b'\r\n')[0]
        break
    if time.monotonic() > deadline:
        raise RuntimeError('No unsolicited request rejection')
print(json.dumps({'loopback_call_answered': True, 'remote_bye_accepted': True,
                  'unsolicited_request_rejected': True}), flush=True)
