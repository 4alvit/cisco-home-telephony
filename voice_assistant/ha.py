"""Bounded Home Assistant Assist exchange over a per-turn WebSocket.

The wire contract follows Home Assistant Core 2026.10.2:
https://developers.home-assistant.io/docs/voice/pipelines/
https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/assist_pipeline/websocket_api.py

No connection is made until ``run`` or ``fetch_audio`` is explicitly called.
Input is headerless signed 16-bit little-endian mono PCM at 16,000 Hz. The
caller owns wake detection, microphone permission and per-phone conversation
state. Cancelling ``run`` closes its connection; there is no private cancel
command and no automatic retry that could execute an intent twice.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
import inspect
import json
import logging
import math
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit


class AssistError(RuntimeError):
    """A safe-to-display error code; raw server messages may contain secrets."""

    def __init__(self, code: str, *, hint: str | None = None):
        self.code = code
        message = f"Home Assistant Assist failed ({code})"
        if hint:
            message += f". {hint}"
        super().__init__(message)


class AssistTimeout(AssistError):
    def __init__(self):
        super().__init__("timeout")


def _timeout(seconds: float):
    """Use stdlib timeouts on 3.11+, loading the 3.10 extra only when needed."""
    factory = getattr(asyncio, "timeout", None)
    if factory is not None:
        return factory(seconds)
    try:
        from async_timeout import timeout
    except ImportError:
        raise AssistError(
            "async-timeout-extra-required",
            hint="Python 3.10 requires async_timeout>=4 in the private runtime environment",
        ) from None
    return timeout(seconds)


@dataclass(frozen=True)
class AssistResult:
    conversation_id: str | None = None
    text: str = ""
    tts_url: str | None = None
    tts_mime_type: str | None = None
    transcript: str = ""
    continue_conversation: bool = False


@dataclass(frozen=True)
class AudioPayload:
    data: bytes
    content_type: str


class WebSocket(Protocol):
    async def send(self, message: str | bytes) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


Connect = Callable[[str], Awaitable[WebSocket]]
EventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class _AiohttpWebSocket:
    """Own both aiohttp resources while exposing the small transport contract."""

    def __init__(self, session, websocket, message_types, close_timeout, max_message_bytes):
        self._session = session
        self._websocket = websocket
        self._message_types = message_types
        self._close_timeout = close_timeout
        self._max_message_bytes = max_message_bytes

    async def send(self, message: str | bytes) -> None:
        if isinstance(message, str):
            await self._websocket.send_str(message)
        elif isinstance(message, bytes):
            await self._websocket.send_bytes(message)
        else:
            raise TypeError("WebSocket messages must be str or bytes")

    async def recv(self) -> str:
        message = await self._websocket.receive()
        if message.type == self._message_types.TEXT:
            if not isinstance(message.data, str) or len(message.data.encode("utf-8")) > self._max_message_bytes:
                raise AssistError("invalid-json-message")
            return message.data
        if message.type == self._message_types.BINARY:
            raise AssistError("invalid-json-message")
        # aiohttp represents peer close, protocol errors and transport failure
        # as messages. Do not stringify their possibly sensitive payloads.
        raise AssistError("websocket-closed-or-failed")

    async def close(self) -> None:
        try:
            async with _timeout(self._close_timeout):
                await self._websocket.close()
        except (TimeoutError, asyncio.TimeoutError):
            pass
        finally:
            # Also runs when a caller cancels the WebSocket close handshake.
            await self._session.close()


async def aiohttp_connect(
    url: str,
    *,
    connection_timeout: float = 10.0,
    close_timeout: float = 2.0,
    max_message_bytes: int = 1024 * 1024,
) -> WebSocket:
    """Optional aiohttp transport: ``HAClient(..., connect=aiohttp_connect)``.

    A fresh session belongs to each returned connection. The session bypasses
    environment proxies and netrc credentials. TLS validation remains enabled.
    The redirect trace raises before aiohttp follows a redirect, preventing a
    later HA auth message from being sent to a redirected endpoint. Only the
    optional ``aiohttp`` dependency is needed; no ``websockets`` import occurs.
    Python 3.10 also needs ``async_timeout>=4`` for timeout contexts.
    Compatible with the current aiohttp 3.x client and tracing APIs.
    """
    connection_timeout = _positive(connection_timeout, "connection_timeout")
    close_timeout = _positive(close_timeout, "close_timeout")
    if type(max_message_bytes) is not int or max_message_bytes <= 0:
        raise ValueError("max_message_bytes must be a positive integer")
    try:
        import aiohttp
    except ImportError:
        raise AssistError("aiohttp-extra-required") from None

    async def reject_redirect(session, context, params):
        raise AssistError("websocket-redirect-rejected")

    trace = aiohttp.TraceConfig()
    trace.on_request_redirect.append(reject_redirect)
    session = aiohttp.ClientSession(
        trust_env=False,
        timeout=aiohttp.ClientTimeout(
            total=connection_timeout, connect=connection_timeout,
            sock_connect=connection_timeout,
        ),
        trace_configs=[trace],
    )
    try:
        # The outer deadline also covers aiohttp's rounded HTTP timeouts.
        async with _timeout(connection_timeout):
            websocket = await session.ws_connect(
                url, ssl=True, proxy=None, max_msg_size=max_message_bytes,
                autoclose=True, autoping=True, compress=0,
            )
        return _AiohttpWebSocket(
            session, websocket, aiohttp.WSMsgType, close_timeout, max_message_bytes,
        )
    except BaseException:
        # Includes task cancellation while acquiring the connection. Closing
        # the owning session also closes any partially acquired connection.
        await session.close()
        raise


def _positive(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{name} must be a positive finite number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return float(value)


def _url_parts(url: str):
    if not isinstance(url, str) or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise ValueError("URL contains whitespace or control characters")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Home Assistant URL must use http or https")
    if parts.username is not None or parts.password is not None or parts.fragment:
        raise ValueError("URL credentials and fragments are not allowed")
    # Accessing port also validates malformed/out-of-range ports.
    _ = parts.port
    return parts


def _origin(parts) -> tuple[str, str, int]:
    port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
    return parts.scheme, parts.hostname.lower(), port


class HAClient:
    """One independent connection per request; safe to share between phones.

    ``connect`` is an async ``url -> WebSocket`` factory for tests/transports.
    The default transport needs the optional ``websockets>=15`` package. Its
    proxy support, redirects and payload logging are disabled. Inject the
    optional ``aiohttp_connect`` to reuse an aiohttp installation instead.
    HTTP TTS fetches require optional aiohttp, bypass proxies, reject every
    redirect and read without executor threads so cancellation aborts the I/O.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        pipeline_id: str | None = None,
        device_id: str | None = None,
        timeout: float = 45.0,
        connection_timeout: float = 10.0,
        close_timeout: float = 2.0,
        max_audio_bytes: int = 8 * 1024 * 1024,
        max_input_bytes: int = 16000 * 2 * 30,
        fetch_timeout: float = 15.0,
        connect: Connect | None = None,
    ):
        parts = _url_parts(base_url)
        if parts.query:
            raise ValueError("Home Assistant base URL must not have a query")
        if not isinstance(token, str) or not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError("A nonempty ASCII access token without whitespace is required")
        for name, value in (("pipeline_id", pipeline_id), ("device_id", device_id)):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name} must be a nonempty string or None")
        for name, value in (("max_audio_bytes", max_audio_bytes), ("max_input_bytes", max_input_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.base_url = base_url.rstrip("/")
        self._origin = _origin(parts)
        self._token = token
        self.pipeline_id = pipeline_id
        self.device_id = device_id
        self.timeout = _positive(timeout, "timeout")
        self.connection_timeout = _positive(connection_timeout, "connection_timeout")
        self.close_timeout = _positive(close_timeout, "close_timeout")
        self.fetch_timeout = _positive(fetch_timeout, "fetch_timeout")
        self.max_audio_bytes = max_audio_bytes
        self.max_input_bytes = max_input_bytes
        self._connect = connect or self._default_connect
        self.websocket_url = urlunsplit((
            "wss" if parts.scheme == "https" else "ws", parts.netloc,
            parts.path.rstrip("/") + "/api/websocket", "", "",
        ))

    async def _default_connect(self, url: str) -> WebSocket:
        try:
            from websockets.asyncio.client import connect
        except ImportError:
            raise AssistError("websockets-extra-required") from None

        class DirectConnect(connect):
            # A redirect must not move the subsequent auth message to another
            # endpoint. Returning the original handshake exception aborts it.
            def process_redirect(self, exc):
                return exc

        quiet = logging.Logger("voice_assistant.transport", level=logging.CRITICAL + 1)
        quiet.addHandler(logging.NullHandler())
        return await DirectConnect(
            url, proxy=None, compression=None, logger=quiet,
            open_timeout=self.connection_timeout, close_timeout=self.close_timeout,
            max_size=1024 * 1024, max_queue=16,
        )

    async def run(
        self,
        audio: AsyncIterator[bytes],
        conversation_id: str | None = None,
        on_event: EventCallback | None = None,
    ) -> AssistResult:
        """Send a bounded turn and return its answer; task cancellation aborts it.

        Callbacks receive the inner event dictionary, including ``type`` and
        ``data``. They may be synchronous or asynchronous. They must be quick;
        the same total timeout bounds callbacks, auth, audio and server work.
        Text and TTS URLs are private data and are never logged here.
        """
        if conversation_id is not None and not isinstance(conversation_id, str):
            raise ValueError("conversation_id must be a string or None")
        ws = None
        try:
            async with _timeout(self.timeout):
                async with _timeout(self.connection_timeout):
                    ws = await self._connect(self.websocket_url)
                    auth = await self._receive(ws)
                    if auth.get("type") != "auth_required":
                        raise AssistError("invalid-auth-handshake")
                    await self._send_json(ws, {"type": "auth", "access_token": self._token})
                    auth = await self._receive(ws)
                    if auth.get("type") != "auth_ok":
                        raise AssistError("authentication-rejected")
                return await self._exchange(ws, audio, conversation_id, on_event)
        except (TimeoutError, asyncio.TimeoutError):
            raise AssistTimeout() from None
        except AssistError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            # Transport exceptions can contain full URLs, tokens or transcripts.
            raise AssistError("transport-or-callback-failed") from None
        finally:
            if ws is not None:
                try:
                    async with _timeout(self.close_timeout):
                        await ws.close()
                except Exception:
                    pass

    async def inspect_pipeline(self):
        """Read the explicitly selected pipeline and its two speech entities.

        This does not start a pipeline, send audio, synthesize speech or execute
        an intent. The temporary filtered entity subscription ends when this
        connection closes, including on timeout or cancellation. The result's
        ``require`` method can enforce exact private provider/voice expectations.
        """
        from .pipeline import inspect_exchange

        if not self.pipeline_id or self.pipeline_id.startswith("conversation."):
            raise AssistError("explicit-pipeline-required")
        ws = None
        try:
            async with _timeout(self.timeout):
                async with _timeout(self.connection_timeout):
                    ws = await self._connect(self.websocket_url)
                    auth = await self._receive(ws)
                    if auth.get("type") != "auth_required":
                        raise AssistError("invalid-auth-handshake")
                    await self._send_json(ws, {"type": "auth", "access_token": self._token})
                    auth = await self._receive(ws)
                    if auth.get("type") != "auth_ok":
                        raise AssistError("authentication-rejected")
                return await inspect_exchange(self, ws)
        except (TimeoutError, asyncio.TimeoutError):
            raise AssistTimeout() from None
        except (AssistError, asyncio.CancelledError):
            raise
        except Exception:
            raise AssistError("pipeline-inspection-failed") from None
        finally:
            if ws is not None:
                try:
                    async with _timeout(self.close_timeout):
                        await ws.close()
                except Exception:
                    pass

    @staticmethod
    async def _send_json(ws: WebSocket, message: dict[str, Any]) -> None:
        await ws.send(json.dumps(message, separators=(",", ":")))

    @staticmethod
    async def _receive(ws: WebSocket) -> dict[str, Any]:
        raw = await ws.recv()
        if not isinstance(raw, str) or len(raw) > 1024 * 1024:
            raise AssistError("invalid-json-message")
        try:
            message = json.loads(raw)
        except (ValueError, TypeError):
            raise AssistError("invalid-json-message") from None
        if not isinstance(message, dict):
            raise AssistError("invalid-json-message")
        return message

    async def _send_audio(self, ws: WebSocket, audio: AsyncIterator[bytes], handler: int) -> None:
        prefix = bytes([handler])
        total = 0
        async for chunk in audio:
            if not isinstance(chunk, bytes) or len(chunk) % 2:
                raise AssistError("invalid-pcm16-chunk")
            # Empty upstream chunks mean no audio; only exhaustion means EOS.
            if not chunk:
                continue
            total += len(chunk)
            if total > self.max_input_bytes:
                raise AssistError("input-audio-limit")
            # Bound each message even if the upstream iterator batches audio.
            for offset in range(0, len(chunk), 16384):
                await ws.send(prefix + chunk[offset:offset + 16384])
        await ws.send(prefix)

    async def _exchange(self, ws, audio, conversation_id, on_event) -> AssistResult:
        command = {
            "id": 1, "type": "assist_pipeline/run", "start_stage": "stt",
            "end_stage": "tts", "input": {"sample_rate": 16000},
            "timeout": self.timeout,
        }
        for key, value in (("pipeline", self.pipeline_id), ("device_id", self.device_id), ("conversation_id", conversation_id)):
            if value is not None:
                command[key] = value
        await self._send_json(ws, command)
        sender = receiver = None
        started = ended = intent_done = tts_done = False
        text = transcript = ""
        tts_url = mime_type = None
        continue_conversation = False
        event_count = 0
        try:
            while True:
                if receiver is None:
                    receiver = asyncio.create_task(self._receive(ws))
                waiting = {receiver}
                if sender is not None:
                    waiting.add(sender)
                done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                if sender is not None and sender in done:
                    sender.result()
                    sender = None
                if receiver not in done:
                    continue
                message = receiver.result()
                receiver = None
                if ended and message.get("type") == "pong" and message.get("id") == 2:
                    # A server timeout sends run-end before error. Reading to a
                    # protocol pong drains queued events, and the stage check
                    # also refuses a truncated run even if no error arrived.
                    if not intent_done or (text and not tts_done):
                        raise AssistError("incomplete-run")
                    return AssistResult(conversation_id, text, tts_url, mime_type, transcript, continue_conversation)
                if message.get("id") != 1:
                    continue
                if message.get("type") == "result":
                    if message.get("success") is not True:
                        self._raise_server_error(message.get("error"))
                    continue
                if message.get("type") != "event":
                    raise AssistError("unexpected-message")
                event = message.get("event")
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    raise AssistError("invalid-event")
                event_count += 1
                if event_count > 10000:
                    raise AssistError("event-limit")
                kind = event["type"]
                data = event.get("data") or {}
                if not isinstance(data, dict):
                    raise AssistError("invalid-event-data")
                if kind == "error":
                    self._raise_server_error(data)
                if kind == "run-start":
                    if started:
                        raise AssistError("duplicate-run-start")
                    started = True
                    conversation_id = data.get("conversation_id") or conversation_id
                    runner = data.get("runner_data") or {}
                    handler = runner.get("stt_binary_handler_id")
                    if type(handler) is not int or not 0 <= handler <= 255:
                        raise AssistError("invalid-binary-handler")
                    sender = asyncio.create_task(self._send_audio(ws, audio, handler))
                elif kind == "stt-end":
                    transcript = (data.get("stt_output") or {}).get("text", "")
                    if sender is not None:
                        sender.cancel()
                        await asyncio.gather(sender, return_exceptions=True)
                        sender = None
                elif kind == "intent-end":
                    intent_done = True
                    output = data.get("intent_output") or {}
                    conversation_id = output.get("conversation_id") or conversation_id
                    continue_conversation = output.get("continue_conversation") is True
                    speech = (output.get("response") or {}).get("speech") or {}
                    text = (speech.get("plain") or speech.get("ssml") or {}).get("speech", "")
                elif kind == "tts-start":
                    text = data.get("tts_input") or text
                elif kind == "tts-end":
                    tts_done = True
                    output = data.get("tts_output") or {}
                    tts_url = output.get("url")
                    mime_type = output.get("mime_type")
                    if not isinstance(tts_url, str) or not tts_url:
                        raise AssistError("missing-tts-url")
                elif kind == "run-end":
                    if ended or not started:
                        raise AssistError("unexpected-run-end")
                    ended = True
                    if sender is not None:
                        sender.cancel()
                        await asyncio.gather(sender, return_exceptions=True)
                        sender = None
                    await self._send_json(ws, {"id": 2, "type": "ping"})
                if on_event is not None:
                    pending = on_event(event)
                    if inspect.isawaitable(pending):
                        await pending
        finally:
            tasks = [task for task in (sender, receiver) if task is not None]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _raise_server_error(data):
        code = data.get("code") if isinstance(data, dict) else None
        # Error text is deliberately discarded; accept only short token codes.
        if not isinstance(code, str) or len(code) > 80 or not all(c.isascii() and (c.isalnum() or c in "_-") for c in code):
            code = "server-error"
        if code == "timeout":
            raise AssistTimeout()
        raise AssistError(code or "server-error")

    async def fetch_audio(self, url: str) -> AudioPayload:
        """Fetch a same-origin TTS result with byte, time and redirect limits.

        TLS verification remains enabled. This method does not decode audio;
        the playback adapter must validate the actual format. Live fetching
        requires optional aiohttp, including when using the websockets turn
        transport. Reads run directly in the event loop: timeout/cancellation
        closes the response and its session before this method exits. No
        executor worker is left reading a trickling body after the deadline.
        """
        try:
            # urljoin/urlsplit normalize some controls; reject those before
            # normalization instead of silently accepting a different URL.
            if not isinstance(url, str) or not url or any(ord(c) < 33 or ord(c) == 127 for c in url):
                raise ValueError("invalid TTS URL")
            target = urljoin(self.base_url + "/", url)
            if _origin(_url_parts(target)) != self._origin:
                raise ValueError("cross-origin URL")
        except (TypeError, ValueError):
            raise AssistError("unsafe-tts-url") from None
        try:
            async with _timeout(self.fetch_timeout):
                return await self._fetch_audio_async(target)
        except (TimeoutError, asyncio.TimeoutError):
            raise AssistTimeout() from None
        except AssistError:
            raise
        except Exception:
            raise AssistError("audio-fetch-failed") from None

    async def _fetch_audio_async(self, target: str) -> AudioPayload:
        try:
            import aiohttp
        except ImportError:
            raise AssistError(
                "aiohttp-extra-required",
                hint="Live TTS audio fetching requires aiohttp in the private runtime environment",
            ) from None
        session = aiohttp.ClientSession(
            trust_env=False,
            # Compressed HTTP bodies must not inflate before the byte check.
            auto_decompress=False,
            timeout=aiohttp.ClientTimeout(
                total=self.fetch_timeout,
                connect=min(self.connection_timeout, self.fetch_timeout),
                sock_connect=min(self.connection_timeout, self.fetch_timeout),
            ),
        )
        try:
            async with session.get(
                target, allow_redirects=False, ssl=True,
                headers={
                    "Authorization": "Bearer " + self._token,
                    "Accept": "audio/*, application/octet-stream",
                    "Accept-Encoding": "identity",
                },
            ) as response:
                # aiohttp canonicalizes host casing and percent escapes even
                # without a redirect. Redirect status/history are authoritative;
                # keep the independent origin check without comparing paths.
                if 300 <= response.status < 400 or response.history:
                    raise AssistError("audio-redirect-rejected")
                try:
                    response_origin = _origin(_url_parts(str(response.url)))
                except (ValueError, TypeError):
                    raise AssistError("audio-redirect-rejected") from None
                if response_origin != self._origin:
                    raise AssistError("audio-redirect-rejected")
                if response.status != 200:
                    raise AssistError("audio-http-status")
                size = response.headers.get("Content-Length")
                if size is not None and (not size.isdigit() or int(size) > self.max_audio_bytes):
                    raise AssistError("audio-size-limit")
                encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
                if encoding != "identity":
                    raise AssistError("audio-content-encoding")
                content_type = response.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0].strip().lower()
                if not (content_type.startswith("audio/") or content_type == "application/octet-stream"):
                    raise AssistError("audio-content-type")
                chunks = []
                total = 0
                while True:
                    chunk = await response.content.read(min(65536, self.max_audio_bytes - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self.max_audio_bytes:
                        raise AssistError("audio-size-limit")
                    chunks.append(chunk)
                if not total:
                    raise AssistError("empty-audio")
                return AudioPayload(b"".join(chunks), content_type)
        finally:
            await session.close()
