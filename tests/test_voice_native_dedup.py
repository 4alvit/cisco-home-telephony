"""Offline native HA wake deduplication protocol and lifecycle coverage."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from voice_assistant.ha import HAClient, AssistError
from voice_assistant.service import AssistantService, ServiceSettings
from voice_assistant.config import validate_config

FRAME = b"\x01\x00" * 320
PHRASE = "Привет, Тест"

class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait(json.dumps({"type": "auth_required"}))
        self.sent = []
        self.closed = False
    async def send(self, item):
        self.sent.append(item)
        if isinstance(item, bytes):
            return
        message = json.loads(item)
        if message["type"] == "auth":
            answer = {"type": "auth_ok"}
        else:
            answer = {"type": "event", "id": 1, "event": {
                "type": "error", "data": {"code": "duplicate_wake_up_detected", "message": "private"}}}
        self.incoming.put_nowait(json.dumps(answer))
    async def recv(self):
        return await self.incoming.get()
    async def close(self):
        self.closed = True

class Trigger:
    def feed(self, pcm):
        return pcm == FRAME
    def reset(self):
        pass

class Playback:
    def __init__(self):
        self.plays = []
    async def play(self, phone, result):
        self.plays.append(result)
    async def stop(self, phone):
        pass

class Assist:
    def __init__(self, error="duplicate_wake_up_detected"):
        self.calls = []
        self.error = error
    async def run(self, audio, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise AssistError(self.error)
        frames = [frame async for frame in audio]
        return SimpleNamespace(conversation_id="next", text="Synthetic answer", tts_url=None)

class NativeDedupTests(unittest.IsolatedAsyncioTestCase):
    def service(self, *, rearm=True, assist=None, phrase=PHRASE):
        self.now = 100.0
        self.assist = assist or Assist()
        self.playback = Playback()
        self.replies = []
        service = AssistantService(ServiceSettings(enabled=True, rearm_after_turn=rearm,
            duplicate_window_seconds=0), {"test": True}, lambda _: Trigger(), self.assist,
            self.playback, clock=lambda: self.now, wake_word_phrase=phrase,
            on_reply=lambda *args: self.replies.append(args))
        self.addAsyncCleanup(service.close)
        return service
    async def wake(self, service):
        stream = await service.listen("test")
        service.feed_pcm("test", stream, FRAME)
        await service.wait_idle("test")
        return stream
    async def test_protocol_supplies_exact_phrase_and_retains_typed_error(self):
        for phrase in (None, PHRASE):
            socket = Socket()
            async def connect(url):
                return socket
            client = HAClient("https://ha.example", "synthetic", connect=connect)
            async def audio():
                yield FRAME
            with self.assertRaises(AssistError) as error:
                await client.run(audio(), wake_word_phrase=phrase)
            self.assertEqual(error.exception.code, "duplicate_wake_up_detected")
            command = json.loads(socket.sent[1])
            expected = {"sample_rate": 16000}
            if phrase is not None:
                expected["wake_word_phrase"] = phrase
            self.assertEqual(command["input"], expected)
            self.assertTrue(socket.closed)
    async def test_invalid_phrase_refuses_before_connect(self):
        async def connect(url):
            self.fail("invalid config must never connect")
        client = HAClient("https://ha.example", "synthetic", connect=connect)
        for phrase in ("", " x", "x ", "x\nq", 1, True, "x" * 129):
            with self.subTest(phrase=repr(phrase)):
                with self.assertRaises(ValueError):
                    await client.run(None, wake_word_phrase=phrase)
    async def test_duplicate_discards_audio_and_rearms_with_cooldown(self):
        service = self.service()
        stream = await self.wake(service)
        item = service._sessions["test"]
        self.assertEqual(service.status("test").state, "armed")
        self.assertEqual(item.stream_id, stream)
        self.assertIsNone(item.queue)
        self.assertEqual(len(item.preroll), 0)
        self.assertIsNone(item.conversation_id)
        self.assertEqual(self.assist.calls[0]["wake_word_phrase"], PHRASE)
        self.assertEqual(self.playback.plays, [])
        self.assertEqual(self.replies, [])
        service.feed_pcm("test", stream, FRAME)
        await asyncio.sleep(0)
        self.assertEqual(len(self.assist.calls), 1)
        self.now += 2.1
        self.assist.error = None
        service.feed_pcm("test", stream, FRAME)
        service.end_utterance("test", stream)
        await service.wait_idle("test")
        self.assertEqual(len(self.assist.calls), 2)
        self.assertEqual(len(self.playback.plays), 1)
        self.assertEqual(item.conversation_id, "next")
    async def test_manual_never_supplies_phrase_or_masks_duplicate(self):
        service = self.service()
        await service.activate("test")
        await service.wait_idle("test")
        self.assertNotIn("wake_word_phrase", self.assist.calls[0])
        self.assertEqual(service.status("test").state, "error")
    async def test_no_configured_phrase_preserves_failure_behavior(self):
        service = self.service(phrase=None)
        await self.wake(service)
        self.assertNotIn("wake_word_phrase", self.assist.calls[0])
        self.assertEqual(service.status("test").state, "error")
    async def test_rearm_still_requires_explicit_setting(self):
        service = self.service(rearm=False)
        await self.wake(service)
        self.assertEqual(service.status("test").state, "stopped")
    async def test_unrelated_typed_failure_remains_error(self):
        service = self.service(assist=Assist("intent-failed"))
        await self.wake(service)
        self.assertEqual(service.status("test").state, "error")
        self.assertEqual(service.status("test").reason, "turn_failed")
    async def test_playback_error_cannot_be_classified_as_competing_wake(self):
        service = self.service(assist=Assist(error=None))
        async def failed_playback(phone, result):
            raise AssistError("duplicate_wake_up_detected")
        self.playback.play = failed_playback
        stream = await service.listen("test")
        service.feed_pcm("test", stream, FRAME)
        service.end_utterance("test", stream)
        await service.wait_idle("test")
        self.assertEqual(service.status("test").state, "error")
    async def test_untyped_error_cannot_be_classified_as_competing_wake(self):
        class Lookalike(Assist):
            async def run(self, audio, **kwargs):
                error = RuntimeError("duplicate_wake_up_detected")
                error.code = "duplicate_wake_up_detected"
                raise error
        service = self.service(assist=Lookalike())
        await self.wake(service)
        self.assertEqual(service.status("test").state, "error")
    async def test_stop_wins_over_racing_duplicate_response(self):
        class DelayedAssist(Assist):
            def __init__(self):
                super().__init__()
                self.started = asyncio.Event()
            async def run(self, audio, **kwargs):
                self.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    raise AssistError("duplicate_wake_up_detected")
        assist = DelayedAssist()
        service = self.service(assist=assist)
        stream = await service.listen("test")
        service.feed_pcm("test", stream, FRAME)
        await assist.started.wait()
        await service.stop("test")
        self.assertEqual(service.status("test").state, "stopped")
        self.assertIsNone(service._sessions["test"].stream_id)
        self.assertFalse(service.feed_pcm("test", stream, FRAME))
    def test_config_is_additive_and_manual_phrase_is_refused(self):
        config = {"service": {}, "phones": {"test": True},
            "ha": {"base_url": "https://ha.example", "token_file": ".local/token", "pipeline_id": "test"},
            "wake": {"mode": "onnx", "bundle_file": ".local/test.json"}}
        validate_config(config)
        config["wake"]["phrase"] = PHRASE
        validate_config(config)
        config["wake"]["mode"] = "manual"
        with self.assertRaises(ValueError):
            validate_config(config)

if __name__ == "__main__":
    unittest.main()
