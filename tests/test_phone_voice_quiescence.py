"""Synthetic-only quiescence proofs; no phone, AMI or other network I/O."""
import copy
import io
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'containers/asterisk'))
import phone_voice_quiescence as q

SID = '1' * 32
CHALLENGE = '2' * 64
DEVICE = 'SEP020000000001'
PHONE = {'id': '201', 'lines': ['201', '202'], 'device': DEVICE,
         # An uncontacted synthetic RFC1918 fixture address.
         'ipv4': '192.168.254.10', 'username': 'synthetic-user', 'password': 'synthetic-password'}
PRIVATE = {'ami': {'host': '127.0.0.1', 'port': 5038,
                   'username': 'synthetic-ami', 'secret': 'synthetic-secret'}}
PROFILE = SimpleNamespace(phone_id='201', lines=('201', '202'), device=DEVICE, ipv4=PHONE['ipv4'])


def payload():
    return q._inputs(PROFILE, SID, CHALLENGE, 100.0, PRIVATE, PHONE)


def page(**changes):
    fields = {'Host Name': DEVICE, 'Stream Status': 'Not Ready',
              'Start Time': '11:39:11a', 'Local Address': '192.168.254.10/16432',
              'Remote Address': '192.168.254.20/10004',
              'Sender Packets': '358', 'Rcvr Packets': '429',
              'Sender Octets': '57280', 'Rcvr Octets': '68640'}
    fields.update(changes)
    return ('<html><table><tr><td>Navigation</td><td><table>' + ''.join(
        '<tr><td>' + key + '</td><td>&nbsp;</td><td><b>' + value + '</b></td></tr>'
        for key, value in fields.items()) + '</table></td></tr></table></html>').encode()


def proof():
    return {'version': 1, 'session_id': SID, 'challenge': CHALLENGE, 'samples': [
        {'observed_at': stamp, 'device': DEVICE, 'idle_lines': ['201', '202'],
         'phone_or_related_channels': [],
         'streams': [q._parse_page(page(), DEVICE, slot)[0] for slot in range(5)]}
        for stamp in (10.2, 11.2)]}


class Clock:
    value = 10.0
    def now(self):
        return self.value
    def sleep(self, seconds):
        self.value += seconds


class FakeProcess:
    pid = 32123
    def __init__(self, output=None):
        self.stdin, self.stdout = io.BytesIO(), io.BytesIO()
        self.returncode = None
        self.output = json.dumps(proof()).encode() if output is None else output
        self.waits = []
    def communicate(self, data, timeout):
        self.input, self.timeout = data, timeout
        self.returncode = 0
        return self.output, None
    def poll(self):
        return self.returncode
    def wait(self, timeout):
        self.waits.append(timeout)
        self.returncode = 0
        return 0


class QuiescenceTests(unittest.TestCase):
    def setUp(self):
        self.socket_guard = patch.object(socket, 'create_connection', side_effect=AssertionError('network forbidden'))
        self.socket_guard.start()
        self.addCleanup(self.socket_guard.stop)

    def test_nested_table_projection_allows_stale_nonzero_addresses(self):
        record, identity = q._parse_page(page(), DEVICE, 3)
        self.assertEqual(record, {'slot': 3, 'status': 'Not Ready', 'tx_packets': 358,
                                 'rx_packets': 429, 'tx_octets': 57280, 'rx_octets': 68640})
        self.assertEqual(identity[0], '11:39:11a')
        self.assertNotIn('Address', json.dumps(record))

    def test_invalid_critical_fields_refuse(self):
        cases = [page(**{'Stream Status': 'Active'}), page(**{'Host Name': 'SEP020000000002'}),
                 page(**{'Sender Packets': '-1'}), page(**{'Sender Octets': str(2**64)}),
                 page(**{'Local Address': 'not-an-address'}), page(**{'Start Time': ''}),
                 page().replace(b'Rcvr Packets', b'Unknown Packets'),
                 page().replace(b'</table>', b'<tr><td>Host Name</td><td></td><td>' +
                                DEVICE.encode() + b'</td></tr></table>', 1),
                 page().replace(b'<td>&nbsp;</td>', b'<td>unexpected</td>', 1),
                 b'x' * (q.MAX_BODY + 1)]
        for raw in cases:
            with self.subTest(raw=raw[:30]), self.assertRaises((q.QuiescenceError, ValueError)):
                q._parse_page(raw, DEVICE, 0)

    def test_device_unclosed_layout_allowed_but_incomplete_critical_row_refused(self):
        raw = page().replace(b'</table></td></tr></table></html>', b'')
        self.assertEqual(q._parse_page(raw, DEVICE, 0), q._parse_page(page(), DEVICE, 0))
        with self.assertRaises(q.QuiescenceError):
            q._parse_page(raw + b'<tr><td>Sender Packets</td><td></td><td>358', DEVICE, 0)

    def run_collection(self, change=None, endpoint=None, channel=None):
        clock, calls = Clock(), []
        snapshots = 0
        class AMI:
            def __enter__(inner):
                calls.append('open')
                return inner
            def __exit__(inner, *_):
                calls.append('close')
            def snapshot(inner):
                nonlocal snapshots
                snapshots += 1
                calls.append('ami')
                clock.value += .05
                endpoints = {'201': 'Not in use', '202': 'Not in use'}
                if endpoint and snapshots == endpoint[0]:
                    endpoints['202'] = endpoint[1]
                return endpoints, [channel] if channel else []
        reads = 0
        def read(_opener, phone, slot, deadline):
            nonlocal reads
            calls.append(slot)
            clock.value += .01
            reads += 1
            raw = page(**change) if change and reads > 5 else page()
            return q._parse_page(raw, phone['device'], slot)
        with patch.object(q, '_gateway', return_value=SimpleNamespace(AMI=lambda config: AMI())), \
             patch.object(q, '_read_page', side_effect=read), \
             patch.object(q.time, 'monotonic', side_effect=clock.now), \
             patch.object(q.time, 'sleep', side_effect=clock.sleep):
            try:
                return q._collect(payload()), calls
            finally:
                self.assertEqual(calls[-1], 'close')

    def test_two_complete_ami_brackets_and_strict_schema(self):
        result, calls = self.run_collection()
        self.assertEqual(calls, ['open', 'ami', 0, 1, 2, 3, 4, 'ami',
                                 'ami', 0, 1, 2, 3, 4, 'ami', 'close'])
        q._validate_proof(result, payload(), 10, 100)
        self.assertGreaterEqual(result['samples'][1]['observed_at'] - result['samples'][0]['observed_at'], 1)
        rendered = json.dumps(result)
        for private in ('synthetic', PHONE['ipv4'], 'Local Address', 'Start Time'):
            self.assertNotIn(private, rendered)

    def test_counter_or_tuple_change_refuses_and_closes_ami(self):
        for change in ({'Sender Packets': '359'}, {'Rcvr Octets': '68641'},
                       {'Start Time': '11:40:11a'}, {'Remote Address': '192.168.254.20/10005'},
                       {'Local Address': '192.168.254.10/16433'}):
            with self.subTest(change=change), self.assertRaises(q.QuiescenceError):
                self.run_collection(change=change)

    def test_each_ami_bracket_must_be_idle(self):
        for index in range(1, 5):
            for state in ('Unavailable', 'In use', 'Ringing', None):
                with self.subTest(index=index, state=state), self.assertRaises(q.QuiescenceError):
                    self.run_collection(endpoint=(index, state))

    def test_global_related_or_alias_channels_refuse_unrelated_allowed(self):
        base = {'Channel': 'PJSIP/999-00ab', 'Uniqueid': '9.1', 'Linkedid': '9.1'}
        self.run_collection(channel=base)
        for update in ({'Channel': 'PJSIP/201-00ab'}, {'Channel': 'PJSIP/202-00ab'},
                       {'Uniqueid': 'cisco-voice-' + SID},
                       {'Linkedid': 'cisco-voice-' + SID + '-b'}, {'Linkedid': ''}):
            with self.subTest(update=update), self.assertRaises(q.QuiescenceError):
                self.run_collection(channel={**base, **update})

    def test_http_fixed_get_no_proxy_redirect_and_body_limit(self):
        response = SimpleNamespace(status=200, geturl=lambda: url, read=lambda n: page())
        class Response:
            def __enter__(self): return response
            def __exit__(self, *_): pass
        requests = []
        class Opener:
            def open(self, req, timeout):
                requests.append((req, timeout))
                return Response()
        url = 'http://' + PHONE['ipv4'] + '/CGI/Java/Serviceability?adapter=device.statistics.streaming.4'
        with patch.object(q.time, 'monotonic', return_value=10):
            q._read_page(Opener(), PHONE, 4, 100)
            response.read = lambda n: b'x' * n
            with self.assertRaises(q.QuiescenceError):
                q._read_page(Opener(), PHONE, 4, 100)
        req, timeout = requests[0]
        self.assertEqual((req.full_url, req.get_method(), req.data, timeout), (url, 'GET', None, 3))
        self.assertTrue(req.get_header('Authorization').startswith('Basic '))
        self.assertEqual(q.request.ProxyHandler({}).proxies, {})
        with self.assertRaises(q.QuiescenceError):
            q._NoRedirect().redirect_request(None, None, 302, '', {}, 'http://example.invalid')

    def test_input_binding_rejects_mismatches_before_spawn(self):
        with patch.object(q.subprocess, 'Popen', side_effect=AssertionError('must not spawn')):
            for phone in ({**PHONE, 'device': 'SEP020000000002'}, {**PHONE, 'lines': ['201']},
                          {**PHONE, 'ipv4': '127.0.0.1'}, {**PHONE, 'password': 'bad\nsecret'}):
                with self.subTest(phone=phone), self.assertRaises(q.QuiescenceError):
                    q.prove_quiescence(PROFILE, SID, CHALLENGE, time.monotonic() + 20,
                                       private=PRIVATE, phone=phone)
            for sid, challenge, deadline in ((SID, 'invalid', time.monotonic() + 20),
                                              ('invalid', CHALLENGE, time.monotonic() + 20),
                                              (SID, CHALLENGE, time.monotonic() - 1),
                                              (SID, CHALLENGE, float('nan'))):
                with self.assertRaises(q.QuiescenceError):
                    q.prove_quiescence(PROFILE, sid, challenge, deadline, private=PRIVATE, phone=PHONE)

    def test_parent_success_reaps_and_keeps_credentials_off_argv(self):
        process = FakeProcess()
        with patch.object(q.subprocess, 'Popen', return_value=process) as spawn, \
             patch.object(q.os, 'killpg', side_effect=ProcessLookupError), \
             patch.object(q.time, 'monotonic', side_effect=[10, 10.1, 12, 12.1]):
            result = q.prove_quiescence(PROFILE, SID, CHALLENGE, 100, private=PRIVATE, phone=PHONE)
        self.assertEqual(result, proof())
        argv = spawn.call_args.args[0]
        self.assertEqual(argv[1], '-I')
        self.assertNotIn('synthetic', str(spawn.call_args))
        self.assertTrue(spawn.call_args.kwargs['start_new_session'])
        self.assertEqual(json.loads(process.input)['deadline'], 38.5)
        self.assertEqual(process.waits, [.6])
        self.assertTrue(process.stdin.closed and process.stdout.closed)

    def test_timeout_escalates_and_exit_race_still_reaps(self):
        for exit_race in (False, True):
            process = FakeProcess()
            process.communicate = lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired('worker', 1))
            def wait(timeout):
                process.waits.append(timeout)
                if len(process.waits) == 1 and not exit_race:
                    raise subprocess.TimeoutExpired('worker', timeout)
                process.returncode = 0
            process.wait = wait
            signals = []
            def killpg(pid, sig):
                signals.append(sig)
                if exit_race or sig == 0:
                    raise ProcessLookupError
            with patch.object(q.subprocess, 'Popen', return_value=process), \
                 patch.object(q.os, 'killpg', side_effect=killpg), \
                 patch.object(q.time, 'monotonic', return_value=10):
                with self.assertRaises(q.QuiescenceError):
                    q.prove_quiescence(PROFILE, SID, CHALLENGE, 40, private=PRIVATE, phone=PHONE)
            self.assertIn(signal.SIGTERM, signals)
            if not exit_race: self.assertIn(signal.SIGKILL, signals)
            self.assertEqual(process.waits[-1], .6)
            self.assertTrue(process.stdin.closed and process.stdout.closed)

    def test_invalid_proof_and_lingering_group_never_return_success(self):
        for mutation in ({'challenge': 'f' * 64}, {'session_id': 'f' * 32}, {'extra': 'secret'}):
            process = FakeProcess(json.dumps({**proof(), **mutation}).encode())
            with patch.object(q.subprocess, 'Popen', return_value=process), \
                 patch.object(q.os, 'killpg', side_effect=ProcessLookupError), \
                 patch.object(q.time, 'monotonic', side_effect=[10, 10.1, 12]):
                with self.assertRaises(q.QuiescenceError):
                    q.prove_quiescence(PROFILE, SID, CHALLENGE, 40, private=PRIVATE, phone=PHONE)
            self.assertTrue(process.stdout.closed)
        process = FakeProcess()
        process.returncode = 0
        with patch.object(q.os, 'killpg') as kill:
            with self.assertRaisesRegex(q.QuiescenceError, 'worker_unreaped'):
                q._reap(process)
        self.assertEqual(kill.call_args_list[-1].args, (process.pid, signal.SIGKILL))

    @unittest.skipUnless(os.name == 'posix', 'POSIX worker process groups')
    def test_real_local_sleeping_child_is_killed_and_reaped(self):
        # No adapter worker/network: exercise the process boundary with a local
        # child deliberately ignoring TERM, then confirm waitpid sees no child.
        child = subprocess.Popen([sys.executable, '-I', '-c',
            'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); time.sleep(60)'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=2), 'local child did not become ready')
                self.assertEqual(child.stdout.readline(), b'ready\n')
            started = time.monotonic()
            q._reap(child)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(child.returncode, -signal.SIGKILL)
            with self.assertRaises(ChildProcessError):
                os.waitpid(child.pid, os.WNOHANG)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=2)
            child.stdin.close()
            child.stdout.close()


if __name__ == '__main__':
    unittest.main()
