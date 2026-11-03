"""Terminal TCP errors must not fabricate cleanup failures or PBX proof."""
import asyncio
import errno
import socket
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from tests.test_phone_voice_sip import Assistant, CALL, FRAME, PBX, TOKEN, Writer
from phone_voice_sip import BoundaryError, Grant, Session, Wire, close_tcp_writer


class ClosingWriter:
    def __init__(self, error, *, closes_socket=True, closing=True,
                 drops_socket=False, exposes_socket=True):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.error = error
        self.closes_socket = closes_socket
        self.drops_socket = drops_socket
        self.exposes_socket = exposes_socket
        self.transport = SimpleNamespace(is_closing=lambda: closing)

    def get_extra_info(self, key):
        return self.sock if key == 'socket' and self.exposes_socket else None

    def close(self):
        if self.closes_socket:
            self.sock.close()
        if self.drops_socket:
            self.exposes_socket = False

    async def wait_closed(self):
        await asyncio.sleep(0)
        raise self.error


class DisconnectTests(unittest.IsolatedAsyncioTestCase):
    def writer(self, error, **kwargs):
        writer = ClosingWriter(error, **kwargs)
        self.addCleanup(writer.sock.close)
        return writer

    async def test_terminal_close_error_needs_closed_tcp_descriptor(self):
        for error_type in (ConnectionResetError, BrokenPipeError):
            with self.subTest(error_type=error_type):
                writer = self.writer(error_type('peer disconnected'))
                await close_tcp_writer(writer)
                self.assertEqual(writer.sock.fileno(), -1)
                # The runner may close the same accepted stream after Wire.
                await close_tcp_writer(writer)

    async def test_socket_is_captured_before_close_drops_extra_info(self):
        writer = self.writer(ConnectionResetError(), drops_socket=True)
        await close_tcp_writer(writer)
        self.assertIsNone(writer.get_extra_info('socket'))
        # No retained socket evidence is invented for a later close attempt.
        with self.assertRaises(ConnectionResetError):
            await close_tcp_writer(writer)

    async def test_terminal_error_without_complete_local_proof_is_not_suppressed(self):
        for options in ({'closes_socket': False}, {'closing': False},
                        {'exposes_socket': False}):
            with self.subTest(options=options):
                writer = self.writer(ConnectionResetError(), **options)
                with self.assertRaises(ConnectionResetError):
                    await close_tcp_writer(writer)

    async def test_unknown_error_and_cancellation_survive_closed_socket(self):
        for error in (OSError(errno.EIO, 'local failure'), RuntimeError('unknown'),
                      asyncio.CancelledError()):
            with self.subTest(error=type(error)):
                writer = self.writer(error)
                with self.assertRaises(type(error)):
                    await close_tcp_writer(writer)

    async def test_close_timeout_is_not_converted_to_closed_socket_success(self):
        writer = self.writer(ConnectionResetError())
        writer.wait_closed = asyncio.Event().wait
        with self.assertRaises(TimeoutError):
            await close_tcp_writer(writer)
        self.assertEqual(writer.sock.fileno(), -1)

    async def test_header_and_payload_resets_are_peer_closed(self):
        for error_type in (ConnectionResetError, BrokenPipeError):
            for prefix in ([], [b'\x12\x00\x02']):
                with self.subTest(error_type=error_type, payload=bool(prefix)):
                    error = error_type('private network detail')
                    reader = SimpleNamespace(readexactly=AsyncMock(side_effect=[*prefix, error]))
                    wire = Wire(reader, Writer(), TOKEN)
                    wire.admitted = True
                    with self.assertRaisesRegex(BoundaryError, '^peer_closed$') as caught:
                        await wire.read_pcm()
                    self.assertIs(caught.exception.__cause__, error)
                    self.assertEqual(wire.frames_received, 0)

    async def test_write_and_drain_disconnects_are_peer_closed(self):
        for stage in ('write', 'drain'):
            for error_type in (ConnectionResetError, BrokenPipeError):
                with self.subTest(stage=stage, error_type=error_type):
                    writer = Writer()
                    error = error_type('private network detail')
                    if stage == 'write':
                        def fail(_data):
                            raise error
                        writer.write = fail
                    else:
                        writer.drain = AsyncMock(side_effect=error)
                    wire = Wire(None, writer, TOKEN)
                    wire.admitted = True
                    with self.assertRaisesRegex(BoundaryError, '^peer_closed$'):
                        await wire.write_pcm(FRAME)
                    self.assertEqual(wire.frames_sent, 0)

    async def test_unknown_read_write_errors_and_cancellation_are_preserved(self):
        for error in (OSError(errno.EIO, 'local failure'), asyncio.CancelledError()):
            with self.subTest(error=type(error)):
                wire = Wire(SimpleNamespace(readexactly=AsyncMock(side_effect=error)), Writer(), TOKEN)
                wire.admitted = True
                with self.assertRaises(type(error)):
                    await wire.read_pcm()
                wire.writer.drain = AsyncMock(side_effect=error)
                with self.assertRaises(type(error)):
                    await wire.write_pcm(FRAME)

    async def test_peer_reset_cannot_replace_exact_pbx_absence_proof(self):
        reader = SimpleNamespace(readexactly=AsyncMock(side_effect=[
            b'\x01\x00\x10', TOKEN.bytes, ConnectionResetError()]))
        grant = Grant(TOKEN, 'demo', CALL, time.monotonic(), time.monotonic() + 1)
        pbx = PBX(grant)
        pbx.confirmed = False
        wire = Wire(reader, Writer(), TOKEN)
        result = await Session(wire, grant, pbx).run(Assistant())
        self.assertEqual(result.reason, 'peer_closed')
        self.assertEqual(result.cleanup_errors, ())
        self.assertFalse(result.stop_confirmed)
        self.assertFalse(result.reservation_may_release)
        self.assertEqual(pbx.hangups, [CALL])
