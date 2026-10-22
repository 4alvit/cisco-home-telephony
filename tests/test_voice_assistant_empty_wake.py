"""Local/HA empty turn recovery; all HA and audio transports are in-memory."""

import asyncio
import json
from types import SimpleNamespace
import unittest

from voice_assistant.ha import AssistError, EmptyWakeTurn, HAClient
from voice_assistant.service import AssistantService, ServiceSettings
from voice_assistant.simulation import SimulatedHASocket
from tests.test_voice_assistant_service import FRAME, SPEECH, Assist, Playback, Trigger


class TrackedSocket(SimulatedHASocket):
    def __init__(self, close_mode=None):
        super().__init__()
        self.close_mode = close_mode
        self.audio_seen = asyncio.Event()
        self.closing = asyncio.Event()
        self.release_close = asyncio.Event()
        self.eos = 0

    async def send(self, message):
        await super().send(message)
        if isinstance(message, bytes):
            self.eos += int(len(message) == 1)
            self.audio_seen.set()

    async def close(self):
        self.closing.set()
        if self.close_mode == "failure":
            raise RuntimeError("synthetic private transport detail")
        if self.close_mode == "block":
            await self.release_close.wait()
        self.closed = True


class STTErrorSocket(TrackedSocket):
    """HA VAD followed by a provider error, without a synthetic success reply."""

    async def send(self, message):
        if isinstance(message, bytes) and len(message) == 1:
            self.eos += 1
            return
        await super().send(message)

    def finish_stt(self, code="stt-no-text-recognized", *, late=False):
        self._event("stt-vad-start")
        self._event("stt-vad-end")
        if late:
            self._event("intent-start")
        self._event("error", {"code": code, "message": "synthetic private provider detail"})
        self._event("run-end")


class EmptyWakeTests(unittest.IsolatedAsyncioTestCase):
    def make(self, *, assist=None, close_mode=None, playback=None, rearm=True, timeout=2,
             stt_error=False):
        self.now = [100.0]
        self.sockets = []
        self.playback = playback or Playback()
        self.events, self.replies = [], []

        async def connect(_url):
            socket_type = STTErrorSocket if stt_error and not self.sockets else TrackedSocket
            socket = socket_type(close_mode)
            self.sockets.append(socket)
            return socket

        self.service = AssistantService(
            ServiceSettings(enabled=True, local_endpointing=True, rearm_after_turn=rearm,
                            duplicate_window_seconds=0, turn_timeout_seconds=timeout),
            {"alpha": True}, lambda _: Trigger(),
            assist or HAClient("http://example.invalid", "synthetic-token", connect=connect,
                               close_timeout=.05),
            self.playback, self.events.append, clock=lambda: self.now[0],
            on_reply=lambda *args: self.replies.append(args))
        self.addAsyncCleanup(self.service.close)
        return self.service

    async def started(self, *, manual=False):
        service = self.service
        stream = await (service.activate("alpha") if manual else service.listen("alpha"))
        service.feed_pcm("alpha", stream, FRAME)
        for _ in range(100):
            if self.sockets and self.sockets[0].audio_seen.is_set():
                break
            await asyncio.sleep(0)
        self.assertTrue(self.sockets and self.sockets[0].audio_seen.is_set())
        return stream

    def expire(self, stream):
        self.service.feed_pcm("alpha", stream, bytes(640))
        self.now[0] += 5.01
        self.service.feed_pcm("alpha", stream, bytes(640))
        self.assertFalse(self.service.feed_pcm("alpha", stream, SPEECH))

    async def idle(self):
        await asyncio.wait_for(self.service.wait_idle("alpha"), 1)

    async def test_real_ha_empty_abort_closes_without_eos_then_same_stream_succeeds(self):
        service = self.make()
        stream = await self.started()
        revision = service._sessions["alpha"].revision
        self.expire(stream)
        await self.idle()
        self.assertTrue(self.sockets[0].closed)
        self.assertEqual(self.sockets[0].eos, 0)
        self.assertEqual(service.status("alpha").state, "armed")
        self.assertEqual(service._sessions["alpha"].stream_id, stream)
        self.assertEqual(service._sessions["alpha"].revision, revision)
        self.assertIsNone(service._sessions["alpha"].conversation_id)
        self.assertEqual(self.playback.plays, [])
        self.assertEqual(self.replies, [])
        self.assertNotIn("speaking", [event.state for event in self.events])

        service.feed_pcm("alpha", stream, FRAME)
        service.feed_pcm("alpha", stream, SPEECH)
        self.now[0] += 2.5
        service.feed_pcm("alpha", stream, bytes(640))
        await self.idle()
        self.assertEqual(service.status("alpha").state, "armed")
        self.assertEqual(len(self.sockets), 2)
        self.assertEqual(self.sockets[1].audio_bytes, 3 * 640)
        self.assertEqual(self.sockets[1].eos, 1)
        self.assertEqual(len(self.playback.plays), 1)
        self.assertEqual(len(self.replies), 1)

    async def test_empty_capture_before_auth_or_run_start_waits_for_confirmed_close(self):
        for stage in ("auth", "assist_pipeline/run"):
            with self.subTest(stage=stage):
                class GatedSocket(TrackedSocket):
                    def __init__(self):
                        super().__init__()
                        self.waiting = asyncio.Event()
                        self.release = asyncio.Event()

                    async def send(self, message):
                        if isinstance(message, str) and json.loads(message)["type"] == stage:
                            self.waiting.set()
                            await self.release.wait()
                        await super().send(message)

                socket = GatedSocket()
                async def connect(_url):
                    return socket
                service = self.make(assist=HAClient(
                    "http://example.invalid", "synthetic-token", connect=connect))
                stream = await service.listen("alpha")
                service.feed_pcm("alpha", stream, FRAME)
                await asyncio.wait_for(socket.waiting.wait(), 1)
                self.expire(stream)
                # No acknowledged local abort yet: stay fenced, do not rearm.
                self.assertEqual(service.status("alpha").state, "listening")
                self.assertIsNone(service._sessions["alpha"].stream_id)
                self.assertFalse(socket.closed)
                socket.release.set()
                await self.idle()
                self.assertTrue(socket.closed)
                self.assertEqual(socket.audio_bytes, 0)
                self.assertEqual(socket.eos, 0)
                self.assertEqual(service.status("alpha").state, "armed")
                self.assertEqual(service._sessions["alpha"].stream_id, stream)
                await service.close()

    async def test_auth_stall_deadline_after_empty_capture_is_terminal(self):
        class StalledAuth(TrackedSocket):
            def __init__(self):
                super().__init__()
                self.waiting = asyncio.Event()

            async def send(self, message):
                if isinstance(message, str) and json.loads(message)["type"] == "auth":
                    self.waiting.set()
                    await asyncio.Event().wait()
                await super().send(message)

        socket = StalledAuth()
        async def connect(_url):
            return socket
        service = self.make(assist=HAClient(
            "http://example.invalid", "synthetic-token", connect=connect,
            connection_timeout=.02))
        stream = await service.listen("alpha")
        service.feed_pcm("alpha", stream, FRAME)
        await asyncio.wait_for(socket.waiting.wait(), 1)
        self.expire(stream)
        await self.idle()
        self.assertTrue(socket.closed)
        self.assertEqual(socket.audio_bytes, 0)
        self.assertEqual(socket.eos, 0)
        self.assertEqual(service.status("alpha").reason, "turn_failed")
        self.assertIsNone(service._sessions["alpha"].stream_id)

    async def test_no_rearm_and_manual_remain_terminal(self):
        for manual, rearm in ((False, False), (True, True)):
            with self.subTest(manual=manual, rearm=rearm):
                service = self.make(rearm=rearm)
                stream = await self.started(manual=manual)
                self.expire(stream)
                await self.idle()
                self.assertEqual(service.status("alpha").reason, "no_speech")
                self.assertEqual(service.status("alpha").state, "error")
                self.assertIsNone(service._sessions["alpha"].stream_id)
                await service.close()

    async def test_stop_or_replacement_during_ha_close_cannot_restore_old_stream(self):
        for replace in (False, True):
            with self.subTest(replace=replace):
                service = self.make(close_mode="block")
                stream = await self.started()
                self.expire(stream)
                await asyncio.wait_for(self.sockets[0].closing.wait(), 1)
                action = service.listen("alpha") if replace else service.stop("alpha")
                result = await asyncio.wait_for(action, 1)
                self.assertNotEqual(service._sessions["alpha"].stream_id, stream)
                self.assertFalse(service.feed_pcm("alpha", stream, FRAME))
                if replace:
                    self.assertEqual(service._sessions["alpha"].stream_id, result)
                else:
                    self.assertEqual(service.status("alpha").reason, "user_stop")
                self.assertEqual(self.playback.plays, [])
                await service.close()

    async def test_stop_after_ha_close_during_sink_cleanup_cannot_rearm(self):
        class BlockingStop(Playback):
            def __init__(self):
                super().__init__()
                self.cleaning = asyncio.Event()

            async def stop(self, phone):
                await super().stop(phone)
                if len(self.stops) == 2:
                    self.cleaning.set()
                    await asyncio.Event().wait()

        for stt_error in (False, True):
            with self.subTest(stt_error=stt_error):
                playback = BlockingStop()
                service = self.make(playback=playback, stt_error=stt_error)
                stream = await self.started()
                if stt_error:
                    self.sockets[0].finish_stt()
                else:
                    self.expire(stream)
                await asyncio.wait_for(playback.cleaning.wait(), 1)
                self.assertTrue(self.sockets[0].closed)
                await asyncio.wait_for(service.stop("alpha", reason="lease_expired"), 1)
                self.assertEqual(service.status("alpha").reason, "lease_expired")
                self.assertIsNone(service._sessions["alpha"].stream_id)
                self.assertEqual(self.playback.plays, [])
                self.assertEqual([event.state for event in self.events].count("armed"), 1)
                await service.close()

    async def test_ha_close_failure_or_deadline_cannot_rearm(self):
        for mode in ("failure", "block"):
            with self.subTest(mode=mode):
                service = self.make(close_mode=mode)
                stream = await self.started()
                self.expire(stream)
                await self.idle()
                self.assertEqual(service.status("alpha").state, "error")
                self.assertEqual(service.status("alpha").reason, "turn_failed")
                self.assertIsNone(service._sessions["alpha"].stream_id)
                self.assertEqual(self.replies, [])
                self.assertEqual(self.sockets[0].eos, 0)
                await service.close()

    async def test_playback_cleanup_failure_cannot_rearm(self):
        class FailingStop(Playback):
            async def stop(self, phone):
                await super().stop(phone)
                if len(self.stops) == 2:
                    raise RuntimeError("synthetic cleanup failure")
        service = self.make(playback=FailingStop())
        stream = await self.started()
        self.expire(stream)
        await self.idle()
        self.assertEqual(service.status("alpha").reason, "playback_stop_failed")
        self.assertIsNone(service._sessions["alpha"].stream_id)

    async def test_swallowed_local_abort_and_late_events_are_not_success(self):
        class SwallowAbort(Assist):
            async def run(self, audio, conversation_id=None, on_event=None):
                try:
                    return await super().run(audio, conversation_id, on_event)
                except EmptyWakeTurn:
                    await on_event({"type": "stt-end"})
                    return SimpleNamespace(conversation_id="must-not-be-saved", text="late reply")
        assist = SwallowAbort()
        service = self.make(assist=assist)
        stream = await service.listen("alpha")
        service.feed_pcm("alpha", stream, FRAME)
        await assist.started.wait()
        self.expire(stream)
        await self.idle()
        self.assertEqual(service.status("alpha").reason, "empty_turn_not_aborted")
        self.assertIsNone(service._sessions["alpha"].conversation_id)
        self.assertIsNone(service._sessions["alpha"].stream_id)
        self.assertEqual(self.playback.plays, [])
        self.assertEqual(self.replies, [])

    async def test_external_cancellation_after_empty_capture_stays_terminal(self):
        service = self.make()
        stream = await self.started()
        self.expire(stream)
        service._sessions["alpha"].task.cancel()
        await self.idle()
        self.assertEqual(service.status("alpha").reason, "turn_cancelled")
        self.assertIsNone(service._sessions["alpha"].stream_id)

    async def test_nonconsuming_provider_times_out_instead_of_rearming(self):
        class NoConsumption(Assist):
            async def run(self, *args, **kwargs):
                self.started.set()
                await asyncio.Event().wait()
        assist = NoConsumption()
        service = self.make(assist=assist, timeout=.1)
        stream = await service.listen("alpha")
        service.feed_pcm("alpha", stream, FRAME)
        await assist.started.wait()
        self.expire(stream)
        await self.idle()
        self.assertEqual(service.status("alpha").reason, "turn_timeout")
        self.assertIsNone(service._sessions["alpha"].stream_id)

    async def test_unrelated_error_or_forged_empty_exception_cannot_rearm(self):
        for failure in (AssistError("stt-no-text-recognized"),
                        AssistError("stt-stream-failed"), EmptyWakeTurn()):
            with self.subTest(failure=failure.code):
                class FailingAssist(Assist):
                    async def run(self, *args, **kwargs):
                        raise failure
                service = self.make(assist=FailingAssist())
                stream = await service.listen("alpha")
                service.feed_pcm("alpha", stream, FRAME)
                await self.idle()
                self.assertEqual(service.status("alpha").reason, "turn_failed")
                self.assertIsNone(service._sessions["alpha"].stream_id)
                await service.close()

    async def test_ha_empty_stt_waits_for_close_then_same_stream_can_answer(self):
        service = self.make(stt_error=True, close_mode="block")
        stream = await self.started()
        revision = service._sessions["alpha"].revision
        first = self.sockets[0]
        first.finish_stt()
        await asyncio.wait_for(first.closing.wait(), 1)
        self.assertFalse(first.closed)
        self.assertNotEqual(service.status("alpha").state, "armed")
        first.release_close.set()
        await self.idle()
        self.assertTrue(first.closed)
        self.assertEqual(service.status("alpha").state, "armed")
        self.assertEqual(service._sessions["alpha"].stream_id, stream)
        self.assertEqual(service._sessions["alpha"].revision, revision)
        self.assertIsNone(service._sessions["alpha"].conversation_id)
        self.assertEqual(self.playback.plays, [])
        self.assertEqual(self.replies, [])
        self.assertNotIn("speaking", [event.state for event in self.events])

        service.feed_pcm("alpha", stream, FRAME)
        for _ in range(100):
            if len(self.sockets) == 2:
                break
            await asyncio.sleep(0)
        self.assertEqual(len(self.sockets), 2)
        self.sockets[1].release_close.set()
        service.feed_pcm("alpha", stream, SPEECH)
        self.now[0] += 2.5
        service.feed_pcm("alpha", stream, bytes(640))
        await self.idle()
        self.assertEqual(service.status("alpha").state, "armed")
        self.assertEqual(len(self.playback.plays), 1)
        self.assertEqual(len(self.replies), 1)

    async def test_ha_empty_stt_manual_or_no_rearm_remains_terminal(self):
        for manual, rearm in ((True, True), (False, False)):
            with self.subTest(manual=manual, rearm=rearm):
                service = self.make(stt_error=True, rearm=rearm)
                await self.started(manual=manual)
                self.sockets[0].finish_stt()
                await self.idle()
                self.assertTrue(self.sockets[0].closed)
                self.assertEqual(service.status("alpha").reason, "turn_failed")
                self.assertIsNone(service._sessions["alpha"].stream_id)
                self.assertEqual(self.playback.plays, [])
                self.assertEqual(self.replies, [])
                await service.close()

    async def test_ha_empty_stt_close_failure_or_deadline_is_terminal(self):
        for mode in ("failure", "block"):
            with self.subTest(close_mode=mode):
                service = self.make(stt_error=True, close_mode=mode)
                await self.started()
                self.sockets[0].finish_stt()
                await self.idle()
                self.assertEqual(service.status("alpha").reason, "turn_failed")
                self.assertIsNone(service._sessions["alpha"].stream_id)
                self.assertEqual(self.playback.plays, [])
                self.assertEqual(self.replies, [])
                await service.close()

    async def test_stop_lease_or_replacement_during_empty_stt_close_wins(self):
        for action in ("user_stop", "lease_expired", "replace"):
            with self.subTest(action=action):
                service = self.make(stt_error=True, close_mode="block")
                stream = await self.started()
                self.sockets[0].finish_stt()
                await asyncio.wait_for(self.sockets[0].closing.wait(), 1)
                if action == "replace":
                    replacement = await asyncio.wait_for(service.listen("alpha"), 1)
                    self.assertEqual(service._sessions["alpha"].stream_id, replacement)
                else:
                    await asyncio.wait_for(service.stop("alpha", reason=action), 1)
                    self.assertIsNone(service._sessions["alpha"].stream_id)
                    self.assertEqual(service.status("alpha").reason, action)
                self.sockets[0].release_close.set()
                await self.idle()
                self.assertNotEqual(service._sessions["alpha"].stream_id, stream)
                self.assertFalse(service.feed_pcm("alpha", stream, FRAME))
                self.assertEqual(self.playback.plays, [])
                self.assertEqual(self.replies, [])
                await service.close()

    async def test_ha_provider_failure_or_late_empty_stt_cannot_rearm(self):
        for code, late in (("stt-stream-failed", False),
                           ("stt-no-text-recognized", True)):
            with self.subTest(code=code, late=late):
                service = self.make(stt_error=True)
                await self.started()
                self.sockets[0].finish_stt(code, late=late)
                await self.idle()
                self.assertTrue(self.sockets[0].closed)
                self.assertEqual(service.status("alpha").reason, "turn_failed")
                self.assertIsNone(service._sessions["alpha"].stream_id)
                self.assertEqual(self.playback.plays, [])
                self.assertEqual(self.replies, [])
                await service.close()
