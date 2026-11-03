"""Actual optional WebSocket transport against bounded loopback-only peers."""
import asyncio
from contextlib import asynccontextmanager
import importlib.util
import json
import logging
import os
import sys
import unittest
from unittest.mock import patch

from voice_assistant.ha import AssistError, HAClient
from tests.test_voice_assistant_ha import answer_events, event, samples


class DefaultTransportVersionTests(unittest.IsolatedAsyncioTestCase):
    async def test_python310_rejected_before_import_or_connection(self):
        client = HAClient("http://127.0.0.1:1", "synthetic-token")
        with patch("voice_assistant.ha.sys.version_info", (3, 10)), \
             patch.dict("sys.modules", {"websockets.asyncio.client": None}):
            with self.assertRaises(AssistError) as raised:
                await client._default_connect(client.websocket_url)
        self.assertEqual(raised.exception.code, "websockets-python-version")
        self.assertIn("Python 3.11+", str(raised.exception))
        self.assertIn("websockets>=17.4,<18", str(raised.exception))


@unittest.skipUnless(sys.version_info >= (3, 11) and importlib.util.find_spec("websockets"),
                     "optional websockets transport requires Python 3.11+ and its dependency")
class WebsocketsTransportTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def peer(self, handler):
        from websockets.asyncio.server import serve
        errors = []
        done = asyncio.Event()

        async def tracked(ws):
            try:
                await handler(ws)
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        quiet = logging.Logger("synthetic-websocket-peer", level=logging.CRITICAL + 1)
        quiet.addHandler(logging.NullHandler())
        server = await serve(tracked, "127.0.0.1", 0, compression=None,
                             close_timeout=.5, logger=quiet)
        try:
            yield server.sockets[0].getsockname()[1], done
        finally:
            server.close()
            await asyncio.wait_for(server.wait_closed(), 2)
        self.assertEqual(errors, [])

    def client(self, port, **kwargs):
        return HAClient(f"http://127.0.0.1:{port}", "synthetic-token", timeout=5,
                        connection_timeout=3, close_timeout=.5, **kwargs)

    async def test_real_fragmented_auth_pcm_answer_and_close(self):
        commands, audio = [], []

        async def handler(ws):
            # Ordinary fragmented JSON remains supported below the new limit.
            await ws.send(['{"type":', '"auth_required"}'])
            commands.append(json.loads(await ws.recv()))
            await ws.send(json.dumps({"type": "auth_ok"}))
            commands.append(json.loads(await ws.recv()))
            for message in ({"id": 1, "type": "result", "success": True, "result": None},
                            event("run-start", {"runner_data": {"stt_binary_handler_id": 7}}),
                            event("stt-start")):
                await ws.send(json.dumps(message))
            audio.extend([await ws.recv(), await ws.recv()])
            for message in answer_events():
                await ws.send(json.dumps(message))
            ping = json.loads(await ws.recv())
            commands.append(ping)
            await ws.send(json.dumps({"id": ping["id"], "type": "pong"}))
            await ws.wait_closed()

        async with self.peer(handler) as (port, done):
            # The default transport must not inherit a machine's proxy setting.
            with patch.dict(os.environ, {"ws_proxy": "http://127.0.0.1:1",
                                         "http_proxy": "http://127.0.0.1:1", "no_proxy": ""}):
                result = await asyncio.wait_for(self.client(port).run(samples(b"\x01\x00")), 6)
            await asyncio.wait_for(done.wait(), 2)
        self.assertEqual(commands[0], {"type": "auth", "access_token": "synthetic-token"})
        self.assertEqual(commands[1]["type"], "assist_pipeline/run")
        self.assertEqual(commands[2]["type"], "ping")
        self.assertEqual(audio, [b"\x07\x01\x00", b"\x07"])
        self.assertEqual(result.text, "Synthetic answer.")
        self.assertEqual(result.conversation_id, "new-conversation")

    async def test_cancel_during_auth_receive_closes_real_connection(self):
        authenticated = asyncio.Event()

        async def handler(ws):
            await ws.send('{"type":"auth_required"}')
            self.assertEqual(json.loads(await ws.recv())["type"], "auth")
            authenticated.set()
            await ws.wait_closed()

        async with self.peer(handler) as (port, done):
            task = asyncio.create_task(self.client(port).run(samples()))
            try:
                await asyncio.wait_for(authenticated.wait(), 3)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                await asyncio.wait_for(done.wait(), 2)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_empty_fragment_flood_is_rejected_before_auth(self):
        from websockets.exceptions import ConnectionClosed
        received_close = []

        async def handler(ws):
            try:
                # Exceed the upstream default of 4096 without changing its cap.
                # Finite 8-KiB fixture: all fragments carry zero payload.
                await ws.send([""] * 4097)
                await ws.recv()
                self.fail("fragment flood must close without an auth message")
            except ConnectionClosed as exc:
                received_close.append(exc.rcvd.code if exc.rcvd else None)

        async with self.peer(handler) as (port, done):
            with self.assertRaises(AssistError) as raised:
                await asyncio.wait_for(self.client(port).run(samples()), 6)
            self.assertEqual(raised.exception.code, "transport-or-callback-failed")
            self.assertNotIn("synthetic-token", str(raised.exception))
            await asyncio.wait_for(done.wait(), 2)
        self.assertEqual(received_close, [1009])

    async def test_redirect_is_rejected_before_auth_or_second_connection(self):
        from websockets.asyncio.server import serve
        from websockets.http11 import Response
        from websockets.datastructures import Headers
        reached = []

        async def destination(ws):
            reached.append(True)

        async with self.peer(destination) as (destination_port, _done):
            def redirect(_connection, _request):
                return Response(302, "Found", Headers({
                    "Location": f"ws://127.0.0.1:{destination_port}/api/websocket",
                    "Content-Length": "0"}), b"")

            server = await serve(destination, "127.0.0.1", 0, process_request=redirect,
                                 close_timeout=.5)
            try:
                port = server.sockets[0].getsockname()[1]
                with self.assertRaises(AssistError) as raised:
                    await asyncio.wait_for(self.client(port).run(samples()), 6)
                self.assertEqual(raised.exception.code, "transport-or-callback-failed")
            finally:
                server.close()
                await asyncio.wait_for(server.wait_closed(), 2)
        self.assertEqual(reached, [])
