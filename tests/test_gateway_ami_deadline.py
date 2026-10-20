"""Offline AMI response deadlines, framing, and resource ownership."""

from contextlib import contextmanager
import io
import socket
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_phone_gateway import gateway


ACTION_ID = 'synthetic-action'
SUCCESS = b'Response: Success\r\nActionID: synthetic-action\r\n\r\n'


class ShortDeadlineClock:
    """Shorten only deadline construction; peer and socket clocks stay real."""

    def __init__(self, budget=0.18, original_budget=8):
        self.budget = budget
        self.original_budget = original_budget
        self.first = True

    def monotonic(self):
        now = time.monotonic()
        if self.first:
            self.first = False
            return now - self.original_budget + self.budget
        return now


class LateStream(io.BytesIO):
    """Deliver bytes at a chosen logical time through either read interface."""

    def __init__(self, data, clock, arrival):
        super().__init__(data)
        self.clock = clock
        self.arrival = arrival

    def readline(self, size=-1):
        result = super().readline(size)
        self.clock.now = self.arrival
        return result

    def read1(self, size=-1):
        result = super().read1(size)
        self.clock.now = self.arrival
        return result


@contextmanager
def connected_ami():
    """Use an actual bounded local stream without DNS or a listening port."""
    client, peer = socket.socketpair()
    client.settimeout(0.6)
    peer.settimeout(0.6)
    ami = gateway.AMI({})
    ami.sock = client
    ami.file = client.makefile('rb')
    try:
        yield ami, peer
    finally:
        ami.file.close()
        client.close()
        peer.close()


def memory_ami(data):
    ami = gateway.AMI({})
    ami.sock = Mock()
    ami.file = io.BytesIO(data)
    return ami


class AMIDeadlineTests(unittest.TestCase):
    def assert_safe_timeout(self, error):
        self.assertEqual(error.reason, 'ami_timeout')
        self.assertEqual(error.status, 503)
        self.assertEqual(vars(error), {'reason': 'ami_timeout', 'status': 503})
        self.assertNotIn('synthetic-private-value', str(error))

    def assert_short_timeout(self, ami):
        clock = ShortDeadlineClock()
        started = time.monotonic()
        with patch.object(gateway, 'time', clock), patch.object(
                gateway.secrets, 'token_hex', return_value=ACTION_ID):
            with self.assertRaises(gateway.Refused) as caught:
                ami.action('Ping', Secret='synthetic-private-value')
        elapsed = time.monotonic() - started
        self.assert_safe_timeout(caught.exception)
        self.assertGreaterEqual(elapsed, 0.12)
        self.assertLess(elapsed, 0.55)

    def test_silent_peer_times_out_with_sanitized_refusal(self):
        with connected_ami() as (ami, _peer):
            self.assert_short_timeout(ami)

    def test_partial_line_times_out_without_waiting_for_newline(self):
        with connected_ami() as (ami, peer):
            peer.sendall(b'Response: Success\r\nMessage: synthetic-private-value')
            self.assert_short_timeout(ami)

    def test_continuous_byte_trickle_cannot_extend_absolute_deadline(self):
        with connected_ami() as (ami, peer):
            stop = threading.Event()

            def trickle():
                # Every byte arrives well within the socket inactivity timeout.
                end = time.monotonic() + 0.7
                try:
                    while time.monotonic() < end and not stop.is_set():
                        peer.sendall(b'x')
                        stop.wait(0.02)
                    peer.shutdown(socket.SHUT_WR)
                except OSError:
                    pass  # The reader may have already closed after timeout.

            worker = threading.Thread(target=trickle, name='ami-test-trickle')
            worker.start()
            try:
                self.assert_short_timeout(ami)
            finally:
                stop.set()
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive(), 'local peer did not stop')

    def test_complete_success_at_or_after_deadline_is_rejected(self):
        for arrival in (8.0, 9.0):
            with self.subTest(arrival=arrival):
                clock = SimpleNamespace(now=0.0)
                clock.monotonic = lambda: clock.now
                ami = memory_ami(b'')
                ami.file = LateStream(SUCCESS, clock, arrival)
                with patch.object(gateway, 'time', clock), patch.object(
                        gateway.secrets, 'token_hex', return_value=ACTION_ID):
                    with self.assertRaises(gateway.Refused) as caught:
                        ami.action('Ping')
                self.assert_safe_timeout(caught.exception)


class AMIFramingTests(unittest.TestCase):
    def action(self, data, name='Ping', **kwargs):
        ami = memory_ami(data)
        with patch.object(gateway.secrets, 'token_hex', return_value=ACTION_ID):
            return ami.action(name, **kwargs)

    def assert_invalid_response(self, data):
        with self.assertRaises(gateway.Refused) as caught:
            self.action(data)
        self.assertEqual(vars(caught.exception), {
            'reason': 'invalid_ami_response', 'status': 503})
        self.assertNotIn('synthetic-private-value', str(caught.exception))

    def test_event_list_ignores_other_action_errors_and_completion(self):
        data = (
            b'Response: Error\r\nActionID: other\r\nMessage: Permission denied\r\n\r\n'
            b'Response: Success\r\nActionID: synthetic-action\r\n\r\n'
            b'Event: EndpointListComplete\r\nActionID: other\r\n\r\n'
            b'Event: EndpointList\r\nActionID: synthetic-action\r\n'
            b'ObjectName: synthetic-endpoint\r\n\r\n'
            b'Event: EndpointListComplete\r\nActionID: synthetic-action\r\n\r\n'
        )
        result = self.action(data, 'PJSIPShowEndpoints', complete='EndpointListComplete')
        self.assertEqual(result, [
            {'Response': 'Success', 'ActionID': ACTION_ID},
            {'Event': 'EndpointList', 'ActionID': ACTION_ID,
             'ObjectName': 'synthetic-endpoint'},
            {'Event': 'EndpointListComplete', 'ActionID': ACTION_ID},
        ])

    def test_follows_and_lf_framing_are_preserved(self):
        result = self.action(
            b'Response: Follows\nActionID: synthetic-action\nOutput: synthetic\n\n')
        self.assertEqual(result, [{
            'Response': 'Follows', 'ActionID': ACTION_ID, 'Output': 'synthetic'}])

    def test_prefetched_bytes_remain_available_to_next_action(self):
        first = SUCCESS.replace(b'synthetic-action', b'first')
        second = SUCCESS.replace(b'synthetic-action', b'second')
        ami = memory_ami(first + second)
        with patch.object(gateway.secrets, 'token_hex', side_effect=['first', 'second']):
            self.assertEqual(ami.action('Ping')[0]['ActionID'], 'first')
            self.assertEqual(ami.action('Ping')[0]['ActionID'], 'second')
        self.assertEqual(ami.sock.sendall.call_count, 2)

    def test_line_limit_is_inclusive_and_counts_line_terminator(self):
        prefix = b'Response: Success\r\nActionID: synthetic-action\r\n'
        for length in (16384, 16385):
            with self.subTest(length=length):
                line = b'Message: ' + b'x' * (length - len(b'Message: \r\n')) + b'\r\n'
                self.assertEqual(len(line), length)
                data = prefix + line + b'\r\n'
                if length == 16384:
                    self.assertEqual(len(self.action(data)[0]['Message']), length - 11)
                else:
                    self.assert_invalid_response(data)

    def test_total_limit_counts_ignored_action_ids_and_all_frames(self):
        def ignored_frame(length):
            prefix, suffix = b'ActionID: other\r\nPadding: ', b'\r\n\r\n'
            return prefix + b'x' * (length - len(prefix) - len(suffix)) + suffix

        for total in (1024 * 1024, 1024 * 1024 + 1):
            with self.subTest(total=total):
                ignored_bytes = total - len(SUCCESS)
                count = (ignored_bytes - 32) // 4096
                data = (ignored_frame(4096) * count
                        + ignored_frame(ignored_bytes - 4096 * count) + SUCCESS)
                self.assertEqual(len(data), total)
                if total == 1024 * 1024:
                    self.assertEqual(self.action(data), [
                        {'Response': 'Success', 'ActionID': ACTION_ID}])
                else:
                    self.assert_invalid_response(data)

    def test_eof_and_unterminated_frames_do_not_return_success(self):
        for data in (
            b'',
            b'Response: Success\r\nActionID: synthetic-action\r\n',
            b'Message: synthetic-private-value',
        ):
            with self.subTest(data_length=len(data)):
                self.assert_invalid_response(data)

    def test_invalid_request_header_is_rejected_before_any_write(self):
        ami = memory_ami(SUCCESS)
        with self.assertRaises(gateway.Refused) as caught:
            ami.action('Ping', Secret='synthetic-private-value\r\nInjected: yes')
        self.assertEqual(vars(caught.exception), {
            'reason': 'invalid_ami_header', 'status': 400})
        ami.sock.sendall.assert_not_called()


class AMISessionCleanupTests(unittest.TestCase):
    CONFIG = {'host': 'synthetic.invalid', 'port': 5038,
              'username': 'synthetic-user', 'secret': 'synthetic-private-value'}

    def test_banner_prefetch_login_and_context_exit(self):
        with connected_ami() as (connection, peer):
            # __enter__ owns its own wrapper; release the helper's first.
            connection.file.close()
            peer.sendall(b'Asterisk Call Manager/5.0\r\n' + SUCCESS)
            with patch.object(gateway.socket, 'create_connection',
                              return_value=connection.sock) as connect, patch.object(
                    gateway.secrets, 'token_hex', return_value=ACTION_ID):
                with gateway.AMI(self.CONFIG) as session:
                    self.assertFalse(session.file.closed)
                    self.assertNotEqual(session.sock.fileno(), -1)
                self.assertTrue(session.file.closed)
                self.assertEqual(session.sock.fileno(), -1)
            connect.assert_called_once_with(('synthetic.invalid', 5038), timeout=5)

    def test_invalid_banner_and_login_failure_close_file_and_socket(self):
        cases = (
            (b'Unexpected synthetic banner\r\n', 'bad_ami_banner'),
            (b'Asterisk Call Manager/5.0\r\n'
             b'Response: Error\r\nActionID: synthetic-action\r\n'
             b'Message: synthetic-private-value\r\n\r\n', 'ami_action_rejected'),
        )
        for data, reason in cases:
            with self.subTest(reason=reason), connected_ami() as (connection, peer):
                connection.file.close()
                peer.sendall(data)
                session = gateway.AMI(self.CONFIG)
                with patch.object(gateway.socket, 'create_connection',
                                  return_value=connection.sock), patch.object(
                        gateway.secrets, 'token_hex', return_value=ACTION_ID):
                    with self.assertRaises(gateway.Refused) as caught:
                        session.__enter__()
                self.assertEqual(caught.exception.reason, reason)
                self.assertNotIn('synthetic-private-value', str(caught.exception))
                self.assertNotIn('synthetic-private-value', repr(vars(caught.exception)))
                self.assertTrue(session.file.closed)
                self.assertEqual(session.sock.fileno(), -1)

    def test_partial_banner_has_absolute_deadline_and_closes_resources(self):
        with connected_ami() as (connection, peer):
            connection.file.close()
            peer.sendall(b'Asterisk Call Manager/')
            session = gateway.AMI(self.CONFIG)
            clock = ShortDeadlineClock(original_budget=5)
            started = time.monotonic()
            with patch.object(gateway.socket, 'create_connection',
                              return_value=connection.sock), patch.object(
                    gateway, 'time', clock):
                with self.assertRaises(gateway.Refused) as caught:
                    session.__enter__()
            self.assertEqual(vars(caught.exception), {'reason': 'ami_timeout', 'status': 503})
            self.assertLess(time.monotonic() - started, 0.55)
            self.assertTrue(session.file.closed)
            self.assertEqual(session.sock.fileno(), -1)

    def test_socket_is_closed_even_when_file_close_fails(self):
        session = memory_ami(b'')
        session.file = Mock()
        session.file.close.side_effect = OSError('synthetic close failure')
        with self.assertRaises(OSError):
            session.__exit__(None, None, None)
        session.sock.close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
