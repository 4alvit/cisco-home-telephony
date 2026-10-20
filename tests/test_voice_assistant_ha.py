"""Synthetic HA protocol and bounded audio transport checks; never contacts HA."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from io import BytesIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from voice_assistant.ha import AssistError, AssistTimeout, HAClient, _timeout, aiohttp_connect


def event(kind, data=None):
    result = {"id": 1, "type": "event", "event": {"type": kind}}
    if data is not None:
        result["event"]["data"] = data
    return result


def answer_events(conversation="new-conversation"):
    return [
        event("stt-end", {"stt_output": {"text": "synthetic request"}}),
        event("intent-start", {"intent_input": "synthetic request"}),
        event("intent-end", {"intent_output": {
            "conversation_id": conversation, "continue_conversation": True,
            "response": {"speech": {"plain": {"speech": "Synthetic answer."}}},
        }}),
        event("tts-start", {"tts_input": "Synthetic answer."}),
        event("tts-end", {"tts_output": {"url": "/api/tts_proxy/synthetic.wav", "mime_type": "audio/wav"}}),
        event("run-end"),
    ]


class FakeWebSocket:
    def __init__(self, *, handler=7, auth_ok=True, completion=None, run_messages=None):
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait(json.dumps({"type": "auth_required"}))
        self.sent = []
        self.closed = False
        self.handler = handler
        self.auth_ok = auth_ok
        self.completion = answer_events() if completion is None else completion
        self.run_messages = run_messages
        self.first_binary = asyncio.Event()

    def enqueue(self, messages):
        for message in messages:
            self.incoming.put_nowait(json.dumps(message))

    async def send(self, message):
        self.sent.append(message)
        if isinstance(message, bytes):
            self.first_binary.set()
            if message == bytes([self.handler]):
                self.enqueue(self.completion)
            return
        data = json.loads(message)
        if data["type"] == "auth":
            self.enqueue([{"type": "auth_ok" if self.auth_ok else "auth_invalid", "message": "private detail"}])
        elif data["type"] == "assist_pipeline/run":
            self.enqueue(self.run_messages if self.run_messages is not None else [
                {"id": 1, "type": "result", "success": True, "result": None},
                event("run-start", {"conversation_id": "initial-conversation", "runner_data": {"stt_binary_handler_id": self.handler}}),
                event("stt-start", {"metadata": {"sample_rate": 16000}}),
            ])
        elif data["type"] == "ping":
            self.enqueue([{"id": data["id"], "type": "pong"}])

    async def recv(self):
        return await self.incoming.get()

    async def close(self):
        self.closed = True

    @property
    def binary(self):
        return [item for item in self.sent if isinstance(item, bytes)]

    @property
    def commands(self):
        return [json.loads(item) for item in self.sent if isinstance(item, str)]


async def samples(*chunks):
    for chunk in chunks:
        yield chunk


def client_for(socket, **kwargs):
    async def connect(url):
        socket.connected_url = url
        return socket
    return HAClient("https://ha.example", "synthetic-token", connect=connect, **kwargs)


class HAProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_pcm_pipeline_context_events_and_tts(self):
        socket = FakeWebSocket(handler=201)
        events = []

        async def on_event(item):
            events.append(item["type"])

        client = client_for(socket, pipeline_id="local-pipeline", device_id="synthetic-device")
        pcm = b"\x01\x00\xff\xff" * 200
        result = await client.run(samples(pcm, b"", pcm), "previous-conversation", on_event)
        self.assertEqual(socket.connected_url, "wss://ha.example/api/websocket")
        self.assertEqual(socket.commands[0], {"type": "auth", "access_token": "synthetic-token"})
        self.assertEqual(socket.commands[1], {
            "id": 1, "type": "assist_pipeline/run", "start_stage": "stt", "end_stage": "tts",
            "input": {"sample_rate": 16000}, "timeout": 45.0, "pipeline": "local-pipeline",
            "device_id": "synthetic-device", "conversation_id": "previous-conversation",
        })
        self.assertEqual(socket.binary, [b"\xc9" + pcm, b"\xc9" + pcm, b"\xc9"])
        self.assertEqual(result.conversation_id, "new-conversation")
        self.assertEqual(result.text, "Synthetic answer.")
        self.assertEqual(result.transcript, "synthetic request")
        self.assertEqual(result.tts_url, "/api/tts_proxy/synthetic.wav")
        self.assertEqual(result.tts_mime_type, "audio/wav")
        self.assertTrue(result.continue_conversation)
        self.assertIn("stt-start", events)
        self.assertEqual(events[-1], "run-end")
        self.assertEqual(socket.commands[-1], {"id": 2, "type": "ping"})
        self.assertTrue(socket.closed)

    async def test_auth_error_does_not_send_pipeline_or_audio(self):
        socket = FakeWebSocket(auth_ok=False)
        with self.assertRaises(AssistError) as raised:
            await client_for(socket).run(samples(b"\x00\x00"))
        self.assertEqual(raised.exception.code, "authentication-rejected")
        self.assertNotIn("private detail", str(raised.exception))
        self.assertEqual(len(socket.commands), 1)
        self.assertFalse(socket.binary)
        self.assertTrue(socket.closed)

    async def test_invalid_handler_cannot_send_audio(self):
        for handler in (None, True, -1, 256, "1"):
            with self.subTest(handler=handler):
                socket = FakeWebSocket(handler=handler)
                with self.assertRaisesRegex(AssistError, "invalid-binary-handler"):
                    await client_for(socket).run(samples(b"\x00\x00"))
                self.assertFalse(socket.binary)
                self.assertTrue(socket.closed)

    async def test_server_command_rejection(self):
        socket = FakeWebSocket(run_messages=[{"id": 1, "type": "result", "success": False, "error": {"code": "pipeline-not-found", "message": "private pipeline"}}])
        with self.assertRaisesRegex(AssistError, "pipeline-not-found"):
            await client_for(socket).run(samples())
        self.assertTrue(socket.closed)

    async def test_server_timeout_after_run_end_is_not_success(self):
        socket = FakeWebSocket(completion=[event("run-end"), event("error", {"code": "timeout", "message": "private detail"})])
        with self.assertRaises(AssistTimeout):
            await client_for(socket).run(samples(b"\x00\x00"))
        self.assertTrue(socket.closed)

    async def test_truncated_pipeline_not_accepted_even_with_pong(self):
        socket = FakeWebSocket(completion=[event("run-end")])
        with self.assertRaisesRegex(AssistError, "incomplete-run"):
            await client_for(socket).run(samples())

    async def test_pipeline_error_and_untrusted_message_redaction(self):
        socket = FakeWebSocket(completion=[event("error", {"code": "stt-no-text-recognized", "message": "private transcript"})])
        with self.assertRaises(AssistError) as raised:
            await client_for(socket).run(samples())
        self.assertEqual(raised.exception.code, "stt-no-text-recognized")
        self.assertNotIn("private transcript", str(raised.exception))
        socket = FakeWebSocket(completion=[event("error", {"code": "https://private.example/token", "message": "private transcript"})])
        with self.assertRaises(AssistError) as raised:
            await client_for(socket).run(samples())
        self.assertEqual(raised.exception.code, "server-error")

    async def test_stt_end_stops_waiting_sender_without_waiting_for_input(self):
        socket = FakeWebSocket(completion=[])
        input_closed = asyncio.Event()

        async def live_audio():
            try:
                yield b"\x00\x00"
                await asyncio.Event().wait()
            finally:
                input_closed.set()

        task = asyncio.create_task(client_for(socket).run(live_audio()))
        await asyncio.wait_for(socket.first_binary.wait(), 1)
        socket.enqueue(answer_events())
        result = await asyncio.wait_for(task, 1)
        self.assertEqual(result.text, "Synthetic answer.")
        self.assertTrue(input_closed.is_set())
        self.assertEqual(socket.binary, [b"\x07\x00\x00"])

    async def test_task_cancel_closes_connection_and_audio_generator(self):
        socket = FakeWebSocket()
        input_closed = asyncio.Event()

        async def live_audio():
            try:
                yield b"\x00\x00"
                await asyncio.Event().wait()
            finally:
                input_closed.set()

        task = asyncio.create_task(client_for(socket).run(live_audio()))
        await asyncio.wait_for(socket.first_binary.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(socket.closed)
        self.assertTrue(input_closed.is_set())
        self.assertFalse(any(cmd["type"].endswith("cancel") for cmd in socket.commands))

    async def test_timeout_includes_auth_handshake(self):
        socket = FakeWebSocket()
        socket.incoming.get_nowait()
        with self.assertRaises(AssistTimeout):
            await client_for(socket, timeout=0.03).run(samples())
        self.assertTrue(socket.closed)

    async def test_timeout_includes_stalled_audio(self):
        socket = FakeWebSocket()

        async def live_audio():
            await asyncio.Event().wait()
            yield b""

        with self.assertRaises(AssistTimeout):
            await client_for(socket, timeout=0.03).run(live_audio())
        self.assertTrue(socket.closed)

    async def test_invalid_or_oversized_audio_is_rejected_promptly(self):
        for chunk, code in ((b"\x00", "invalid-pcm16-chunk"), ("pcm", "invalid-pcm16-chunk"), (b"\x00" * 20, "input-audio-limit")):
            with self.subTest(code=code):
                socket = FakeWebSocket()
                with self.assertRaisesRegex(AssistError, code):
                    await client_for(socket, max_input_bytes=10).run(samples(chunk))
                self.assertTrue(socket.closed)
                self.assertFalse(socket.binary)

    async def test_large_chunk_split_without_changing_pcm(self):
        socket = FakeWebSocket()
        pcm = b"\x01\x80" * 12000
        await client_for(socket).run(samples(pcm))
        self.assertEqual(b"".join(chunk[1:] for chunk in socket.binary), pcm)
        self.assertTrue(all(len(chunk) <= 16385 for chunk in socket.binary))
        self.assertEqual(socket.binary[-1], b"\x07")

    async def test_no_speech_response_can_complete_without_tts(self):
        socket = FakeWebSocket(completion=[
            event("stt-end", {"stt_output": {"text": "synthetic request"}}),
            event("intent-end", {"intent_output": {"conversation_id": "new", "response": {"speech": {}}}}),
            event("run-end"),
        ])
        result = await client_for(socket).run(samples())
        self.assertEqual(result.text, "")
        self.assertIsNone(result.tts_url)

    async def test_shared_client_keeps_runs_independent(self):
        sockets = []

        async def connect(url):
            socket = FakeWebSocket(completion=answer_events(f"result-{len(sockets)}"))
            sockets.append(socket)
            return socket

        client = HAClient("https://ha.example", "synthetic-token", connect=connect)
        results = await asyncio.gather(client.run(samples(), "one"), client.run(samples(), "two"))
        self.assertEqual([result.conversation_id for result in results], ["result-0", "result-1"])
        self.assertEqual([socket.commands[1]["conversation_id"] for socket in sockets], ["one", "two"])
        self.assertTrue(all(socket.closed for socket in sockets))

    async def test_unrelated_event_is_ignored(self):
        socket = FakeWebSocket(completion=[{"id": 9, "type": "event", "event": {"type": "error"}}] + answer_events())
        result = await client_for(socket).run(samples())
        self.assertEqual(result.text, "Synthetic answer.")


class FakeHTTPResponse:
    def __init__(self, body=b"RIFFsynthetic", headers=None, status=200, url="https://ha.example/api/tts_proxy/synthetic.wav", history=()):
        self.body = BytesIO(body)
        self.status = status
        self.headers = headers if headers is not None else {"Content-Type": "audio/wav"}
        self.url = url
        self.history = history
        self.content = SimpleNamespace(read=AsyncMock(side_effect=self.body.read))
        self.exited = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.exited = True
        self.body.close()


def mocked_http(response):
    session = SimpleNamespace(get=Mock(return_value=response), close=AsyncMock())
    module = SimpleNamespace(
        ClientSession=Mock(return_value=session),
        ClientTimeout=Mock(side_effect=lambda **values: SimpleNamespace(**values)),
    )
    return module, session


class TrickleHTTPResponse(FakeHTTPResponse):
    """An endless body that supplies bytes often enough to avoid idle timeout."""

    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.active_reads = 0
        self.read_calls = 0
        self.read_cancelled = False
        self.content.read.side_effect = self.trickle

    async def trickle(self, size):
        self.active_reads += 1
        self.read_calls += 1
        self.entered.set()
        try:
            await asyncio.sleep(.005)
            return b"x"
        except asyncio.CancelledError:
            self.read_cancelled = True
            raise
        finally:
            self.active_reads -= 1


class AudioFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_origin_fetch_disables_proxies_and_redirects(self):
        response = FakeHTTPResponse()
        module, session = mocked_http(response)
        with patch.dict("sys.modules", {"aiohttp": module}):
            result = await HAClient("https://ha.example", "synthetic-token").fetch_audio("/api/tts_proxy/synthetic.wav")
        self.assertEqual(result.data, b"RIFFsynthetic")
        self.assertEqual(result.content_type, "audio/wav")
        self.assertIs(module.ClientSession.call_args.kwargs["trust_env"], False)
        self.assertIs(module.ClientSession.call_args.kwargs["auto_decompress"], False)
        self.assertEqual(module.ClientSession.call_args.kwargs["timeout"].total, 15.0)
        self.assertIs(session.get.call_args.kwargs["allow_redirects"], False)
        self.assertIs(session.get.call_args.kwargs["ssl"], True)
        self.assertEqual(session.get.call_args.args[0], "https://ha.example/api/tts_proxy/synthetic.wav")
        self.assertEqual(session.get.call_args.kwargs["headers"]["Authorization"], "Bearer synthetic-token")
        self.assertEqual(session.get.call_args.kwargs["headers"]["Accept-Encoding"], "identity")
        self.assertTrue(response.exited)
        session.close.assert_awaited_once()

    async def test_canonicalized_host_and_path_are_not_redirects(self):
        for base, path, canonical in (
            ("http://LOCALHOST:8123", "/audio", "http://localhost:8123/audio"),
            ("http://localhost:8123", "/%61udio", "http://localhost:8123/audio"),
            ("http://ha.example:80", "/audio", "http://ha.example/audio"),
        ):
            with self.subTest(base=base, path=path):
                response = FakeHTTPResponse(url=canonical)
                module, session = mocked_http(response)
                with patch.dict("sys.modules", {"aiohttp": module}):
                    result = await HAClient(base, "synthetic-token").fetch_audio(path)
                self.assertEqual(result.data, b"RIFFsynthetic")
                self.assertEqual(session.get.call_args.args[0], base + path)
                self.assertIs(session.get.call_args.kwargs["allow_redirects"], False)
                self.assertEqual(session.get.call_args.kwargs["headers"]["Authorization"], "Bearer synthetic-token")
                self.assertTrue(response.exited)
                session.close.assert_awaited_once()

    async def test_rejects_other_origin_userinfo_and_bad_url_before_fetch(self):
        client = HAClient("https://ha.example", "synthetic-token")
        for url in ("https://other.example/audio", "//other.example/audio", "http://ha.example/audio", "https://ha.example:444/audio", "https://ha.example:0/audio", "https://user@ha.example/audio", "file:///audio", "https://ha.example:99999/audio", "/audio#fragment", "/audio\n", "\thttps://ha.example/audio", ""):
            with self.subTest(url=url), patch.object(client, "_fetch_audio_async") as fetch:
                with self.assertRaisesRegex(AssistError, "unsafe-tts-url"):
                    await client.fetch_audio(url)
                fetch.assert_not_awaited()

    async def test_response_size_type_and_redirect_checks(self):
        cases = [
            (dict(body=b"12345", headers={"Content-Length": "5"}), "audio-size-limit"),
            (dict(body=b"12345"), "audio-size-limit"),
            (dict(body=b""), "empty-audio"),
            (dict(headers={"Content-Type": "text/html"}), "audio-content-type"),
            (dict(headers={"Content-Encoding": "gzip"}), "audio-content-encoding"),
            (dict(status=302), "audio-redirect-rejected"),
            (dict(history=(object(),)), "audio-redirect-rejected"),
            (dict(status=500), "audio-http-status"),
            (dict(url="https://other.example/audio"), "audio-redirect-rejected"),
        ]
        for kwargs, code in cases:
            response = FakeHTTPResponse(**kwargs)
            module, session = mocked_http(response)
            with self.subTest(code=code), patch.dict("sys.modules", {"aiohttp": module}):
                with self.assertRaisesRegex(AssistError, code):
                    await HAClient("https://ha.example", "synthetic-token", max_audio_bytes=4).fetch_audio("/api/tts_proxy/synthetic.wav")
                self.assertTrue(response.exited)
                session.close.assert_awaited_once()

    async def test_fetch_exception_does_not_disclose_url_or_token(self):
        module, session = mocked_http(FakeHTTPResponse())
        session.get.side_effect = OSError("secret URL or token")
        with patch.dict("sys.modules", {"aiohttp": module}):
            with self.assertRaises(AssistError) as raised:
                await HAClient("https://ha.example", "synthetic-token").fetch_audio("/audio")
        self.assertEqual(raised.exception.code, "audio-fetch-failed")
        self.assertNotIn("secret", str(raised.exception))
        session.close.assert_awaited_once()

    async def test_missing_aiohttp_for_fetch_has_actionable_error(self):
        with patch.dict("sys.modules", {"aiohttp": None}):
            with self.assertRaises(AssistError) as raised:
                await HAClient("https://ha.example", "synthetic-token").fetch_audio("/audio")
        self.assertEqual(raised.exception.code, "aiohttp-extra-required")
        self.assertIn("Live TTS audio fetching requires aiohttp", str(raised.exception))

    async def test_trickle_deadline_terminates_read_and_closes_resources_without_worker(self):
        response = TrickleHTTPResponse()
        module, session = mocked_http(response)
        loop = asyncio.get_running_loop()
        with patch.dict("sys.modules", {"aiohttp": module}), patch.object(loop, "run_in_executor", side_effect=AssertionError("fetch must not launch a worker")) as executor:
            with self.assertRaises(AssistTimeout):
                await HAClient("https://ha.example", "synthetic-token", fetch_timeout=.045).fetch_audio("/api/tts_proxy/synthetic.wav")
            self.assertGreater(response.read_calls, 1)
            self.assertTrue(response.read_cancelled)
            self.assertEqual(response.active_reads, 0)
            self.assertTrue(response.exited)
            session.close.assert_awaited_once()
            executor.assert_not_called()
            calls_at_timeout = response.read_calls
            await asyncio.sleep(.02)
            self.assertEqual(response.read_calls, calls_at_timeout)

    async def test_cancelling_trickle_terminates_read_before_fetch_returns(self):
        response = TrickleHTTPResponse()
        module, session = mocked_http(response)
        with patch.dict("sys.modules", {"aiohttp": module}):
            task = asyncio.create_task(HAClient("https://ha.example", "synthetic-token").fetch_audio("/api/tts_proxy/synthetic.wav"))
            await asyncio.wait_for(response.entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(response.read_cancelled)
        self.assertEqual(response.active_reads, 0)
        self.assertTrue(response.exited)
        session.close.assert_awaited_once()


class HAConfigurationTests(unittest.TestCase):
    def test_invalid_configuration_rejected_without_network(self):
        for kwargs in ({"timeout": 0}, {"timeout": float("nan")}, {"connection_timeout": -1}, {"max_audio_bytes": True}, {"max_input_bytes": 0}, {"pipeline_id": ""}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                HAClient("https://ha.example", "synthetic-token", **kwargs)
        for url in ("ftp://ha.example", "https://user:pass@ha.example", "https://ha.example#secret", "https://ha.example?token=secret", "https://ha.example\n"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                HAClient(url, "synthetic-token")


def mocked_aiohttp(socket=None):
    """Model only the documented aiohttp APIs; no installed package is needed."""
    socket = socket or FakeWebSocket()
    message_types = SimpleNamespace(TEXT=1, BINARY=2, CLOSE=8, CLOSED=257, ERROR=258)

    async def receive():
        return SimpleNamespace(type=message_types.TEXT, data=await socket.recv())

    websocket = SimpleNamespace(
        send_str=AsyncMock(side_effect=socket.send),
        send_bytes=AsyncMock(side_effect=socket.send),
        receive=AsyncMock(side_effect=receive),
        close=AsyncMock(side_effect=socket.close),
    )
    session = SimpleNamespace(
        ws_connect=AsyncMock(return_value=websocket), close=AsyncMock(),
    )
    module = SimpleNamespace(
        WSMsgType=message_types,
        ClientSession=Mock(return_value=session),
        ClientTimeout=Mock(side_effect=lambda **values: SimpleNamespace(**values)),
        TraceConfig=Mock(side_effect=lambda: SimpleNamespace(on_request_redirect=[])),
    )
    return module, session, websocket, socket


class AiohttpTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_assist_exchange_uses_aiohttp_and_closes_both_resources(self):
        module, session, websocket, socket = mocked_aiohttp()
        with patch.dict("sys.modules", {"aiohttp": module}):
            client = HAClient("https://ha.example", "synthetic-token", connect=aiohttp_connect)
            result = await client.run(samples(b"\x01\x00"))
        self.assertEqual(result.text, "Synthetic answer.")
        self.assertEqual(socket.binary, [b"\x07\x01\x00", b"\x07"])
        self.assertEqual(session.ws_connect.call_args.args, ("wss://ha.example/api/websocket",))
        options = session.ws_connect.call_args.kwargs
        self.assertIs(options["ssl"], True)
        self.assertIsNone(options["proxy"])
        self.assertEqual(options["max_msg_size"], 1024 * 1024)
        self.assertEqual(options["compress"], 0)
        options = module.ClientSession.call_args.kwargs
        self.assertIs(options["trust_env"], False)
        self.assertEqual(options["timeout"].total, 10.0)
        self.assertEqual(len(options["trace_configs"][0].on_request_redirect), 1)
        session.close.assert_awaited_once()
        websocket.close.assert_awaited_once()

    async def test_handshake_failure_closes_owning_session(self):
        module, session, websocket, _ = mocked_aiohttp()
        session.ws_connect.side_effect = OSError("private endpoint detail")
        with patch.dict("sys.modules", {"aiohttp": module}):
            with self.assertRaises(AssistError) as raised:
                await HAClient("https://ha.example", "synthetic-token", connect=aiohttp_connect).run(samples())
        self.assertEqual(raised.exception.code, "transport-or-callback-failed")
        self.assertNotIn("private", str(raised.exception))
        session.close.assert_awaited_once()
        websocket.send_str.assert_not_awaited()

    async def test_redirect_trace_aborts_before_auth_and_closes_session(self):
        module, session, websocket, _ = mocked_aiohttp()

        async def redirected_handshake(*args, **kwargs):
            trace = module.ClientSession.call_args.kwargs["trace_configs"][0]
            for callback in trace.on_request_redirect:
                await callback(session, None, SimpleNamespace(url="https://other.example"))
            self.fail("redirect callback must abort the handshake")

        session.ws_connect.side_effect = redirected_handshake
        with patch.dict("sys.modules", {"aiohttp": module}):
            with self.assertRaisesRegex(AssistError, "websocket-redirect-rejected"):
                await HAClient("https://ha.example", "synthetic-token", connect=aiohttp_connect).run(samples())
        session.close.assert_awaited_once()
        websocket.send_str.assert_not_awaited()

    async def test_cancel_during_handshake_closes_session(self):
        module, session, websocket, _ = mocked_aiohttp()
        entered = asyncio.Event()

        async def blocked_handshake(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        session.ws_connect.side_effect = blocked_handshake
        with patch.dict("sys.modules", {"aiohttp": module}):
            task = asyncio.create_task(aiohttp_connect("wss://ha.example/api/websocket"))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        session.close.assert_awaited_once()
        websocket.send_str.assert_not_awaited()

    async def test_handshake_deadline_closes_session(self):
        module, session, _, _ = mocked_aiohttp()

        async def blocked_handshake(*args, **kwargs):
            await asyncio.Event().wait()

        session.ws_connect.side_effect = blocked_handshake
        with patch.dict("sys.modules", {"aiohttp": module}):
            with self.assertRaises((TimeoutError, asyncio.TimeoutError)):
                await aiohttp_connect("wss://ha.example/api/websocket", connection_timeout=.01)
        session.close.assert_awaited_once()

    async def test_cancel_during_close_still_closes_session(self):
        module, session, websocket, _ = mocked_aiohttp()
        entered = asyncio.Event()

        async def blocked_close():
            entered.set()
            await asyncio.Event().wait()

        websocket.close.side_effect = blocked_close
        with patch.dict("sys.modules", {"aiohttp": module}):
            connection = await aiohttp_connect("wss://ha.example/api/websocket")
            task = asyncio.create_task(connection.close())
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        session.close.assert_awaited_once()

    async def test_close_deadline_still_closes_session(self):
        module, session, websocket, _ = mocked_aiohttp()

        async def blocked_close():
            await asyncio.Event().wait()

        websocket.close.side_effect = blocked_close
        with patch.dict("sys.modules", {"aiohttp": module}):
            connection = await aiohttp_connect("wss://ha.example/api/websocket", close_timeout=.01)
            await asyncio.wait_for(connection.close(), 1)
        session.close.assert_awaited_once()

    async def test_closed_error_binary_and_oversized_text_messages_are_rejected(self):
        for kind, data, code in (
            (8, "private close reason", "websocket-closed-or-failed"),
            (257, None, "websocket-closed-or-failed"),
            (258, OSError("private URL"), "websocket-closed-or-failed"),
            (2, b"unexpected binary", "invalid-json-message"),
            (1, "\u0401\u0401\u0401", "invalid-json-message"),
        ):
            with self.subTest(kind=kind):
                module, session, websocket, _ = mocked_aiohttp()
                websocket.receive.side_effect = None
                websocket.receive.return_value = SimpleNamespace(type=kind, data=data)
                with patch.dict("sys.modules", {"aiohttp": module}):
                    connection = await aiohttp_connect("wss://ha.example/api/websocket", max_message_bytes=4)
                    try:
                        with self.assertRaisesRegex(AssistError, code):
                            await connection.recv()
                    finally:
                        await connection.close()
                session.close.assert_awaited_once()

    async def test_missing_optional_dependency_has_clear_error(self):
        with patch.dict("sys.modules", {"aiohttp": None}):
            with self.assertRaisesRegex(AssistError, "aiohttp-extra-required"):
                await aiohttp_connect("wss://ha.example/api/websocket")


@contextmanager
def without_stdlib_timeout():
    """Simulate Python 3.10 without relying on an installed older interpreter."""
    missing = object()
    original = getattr(asyncio, "timeout", missing)
    if original is not missing:
        del asyncio.timeout
    try:
        yield
    finally:
        if original is not missing:
            asyncio.timeout = original


@asynccontextmanager
async def passive_timeout(seconds):
    """A stand-in for dispatch tests; these exchanges do not block."""
    yield


class LegacyAsyncioTimeoutError(Exception):
    """Python 3.10's asyncio error was distinct from built-in TimeoutError."""


class Python310TimeoutCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdlib_factory_is_preferred_without_importing_extra(self):
        sentinel = object()
        factory = Mock(return_value=sentinel)
        with patch.object(asyncio, "timeout", factory, create=True), patch.dict("sys.modules", {"async_timeout": None}):
            self.assertIs(_timeout(7.0), sentinel)
        factory.assert_called_once_with(7.0)

    async def test_absent_stdlib_factory_uses_lazy_extra_for_complete_exchange(self):
        factory = Mock(side_effect=passive_timeout)
        module = SimpleNamespace(timeout=factory)
        socket = FakeWebSocket()
        with without_stdlib_timeout(), patch.dict("sys.modules", {"async_timeout": module}):
            result = await client_for(socket).run(samples(b"\x00\x00"))
        self.assertEqual(result.text, "Synthetic answer.")
        self.assertEqual([call.args for call in factory.call_args_list], [(45.0,), (10.0,), (2.0,)])
        self.assertTrue(socket.closed)

    async def test_missing_extra_is_actionable_and_does_not_connect(self):
        connect = AsyncMock()
        with without_stdlib_timeout(), patch.dict("sys.modules", {"async_timeout": None}):
            with self.assertRaises(AssistError) as raised:
                await HAClient("https://ha.example", "synthetic-token", connect=connect).run(samples())
        self.assertEqual(raised.exception.code, "async-timeout-extra-required")
        self.assertIn("Python 3.10 requires async_timeout>=4", str(raised.exception))
        self.assertNotIn("synthetic-token", str(raised.exception))
        connect.assert_not_awaited()

    async def test_both_timeout_exception_types_become_assist_timeout(self):
        module = SimpleNamespace(timeout=passive_timeout)
        for error_type in (TimeoutError, LegacyAsyncioTimeoutError):
            with self.subTest(error_type=error_type.__name__):
                connect = AsyncMock(side_effect=error_type("private transport detail"))
                with without_stdlib_timeout(), patch.dict("sys.modules", {"async_timeout": module}), patch.object(asyncio, "TimeoutError", LegacyAsyncioTimeoutError):
                    with self.assertRaises(AssistTimeout):
                        await HAClient("https://ha.example", "synthetic-token", connect=connect).run(samples())

    async def test_both_fetch_timeout_types_become_assist_timeout(self):
        module = SimpleNamespace(timeout=passive_timeout)
        client = HAClient("https://ha.example", "synthetic-token")
        for error_type in (TimeoutError, LegacyAsyncioTimeoutError):
            with self.subTest(error_type=error_type.__name__):
                with without_stdlib_timeout(), patch.dict("sys.modules", {"async_timeout": module}), patch.object(asyncio, "TimeoutError", LegacyAsyncioTimeoutError), patch.object(client, "_fetch_audio_async", side_effect=error_type("private URL")):
                    with self.assertRaises(AssistTimeout):
                        await client.fetch_audio("/api/tts_proxy/synthetic.wav")

    async def test_aiohttp_close_handles_legacy_timeout_and_closes_session(self):
        fallback = SimpleNamespace(timeout=passive_timeout)
        module, session, websocket, _ = mocked_aiohttp()
        websocket.close.side_effect = LegacyAsyncioTimeoutError("private transport detail")
        with without_stdlib_timeout(), patch.dict("sys.modules", {"async_timeout": fallback, "aiohttp": module}), patch.object(asyncio, "TimeoutError", LegacyAsyncioTimeoutError):
            connection = await aiohttp_connect("wss://ha.example/api/websocket")
            await connection.close()
        session.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
