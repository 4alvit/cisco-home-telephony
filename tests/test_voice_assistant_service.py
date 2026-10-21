import asyncio
from dataclasses import asdict
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
import wave

from voice_assistant.config import NeverWake, build_service, load_config, private_path, validate_config
from voice_assistant.audio import UtteranceEndpoint, pcm_rms
from voice_assistant.ha import AudioPayload, HAClient
from voice_assistant.playback import FFmpegDecoder, PCMOutput, WavePlayback, decode_wave
from voice_assistant.service import AssistantService, ServiceSettings
from voice_assistant.simulation import SimulatedHASocket


FRAME = b"\x01\x00" * 320
SPEECH = b"\x00\x20" * 320


class Trigger:
    def __init__(self):
        self.resets = 0

    def feed(self, pcm):
        return pcm == FRAME

    def reset(self):
        self.resets += 1


class WarmingTrigger(Trigger):
    """Synthetic 1.28-second model history at the service's 20 ms cadence."""

    def __init__(self):
        super().__init__()
        self.frames = 0

    @property
    def ready(self):
        return self.frames >= 64

    def feed(self, pcm):
        self.frames += 1
        return self.ready and super().feed(pcm)

    def reset(self):
        super().reset()
        self.frames = 0


class Assist:
    def __init__(self):
        self.calls, self.frames = [], []
        self.started = asyncio.Event()

    async def run(self, audio, conversation_id=None, on_event=None):
        self.calls.append(conversation_id)
        self.started.set()
        self.frames.append([frame async for frame in audio])
        await on_event({"type": "stt-end"})
        return SimpleNamespace(conversation_id="conversation-" + str(len(self.calls)),
                               text="Synthetic response", tts_url=None)


class Playback:
    def __init__(self):
        self.plays, self.stops = [], []

    async def play(self, phone, result):
        self.plays.append((phone, result))

    async def stop(self, phone):
        self.stops.append(phone)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def make(self, *, settings=None, assist=None, playback=None, factory=None, phones=None,
             on_reply=None):
        self.assist, self.playback = assist or Assist(), playback or Playback()
        self.events = []
        service = AssistantService(settings or ServiceSettings(enabled=True, duplicate_window_seconds=0),
                                   phones or {"alpha": True, "beta": True},
                                   factory or (lambda _: Trigger()), self.assist, self.playback,
                                   self.events.append, on_reply=on_reply)
        self.addAsyncCleanup(service.close)
        return service

    async def test_disabled_and_per_phone_permission_do_not_start_anything(self):
        service = self.make(settings=ServiceSettings(), phones={"alpha": True})
        self.assertFalse(service.feed_pcm("alpha", "stale", FRAME))
        with self.assertRaises(PermissionError):
            await service.listen("alpha")
        service = self.make(phones={"alpha": False})
        with self.assertRaises(PermissionError):
            await service.activate("alpha")
        self.assertEqual(self.assist.calls, [])

    async def test_synchronous_permission_gate_has_no_media_or_state_side_effects(self):
        service = self.make(phones={"alpha": True, "beta": False})
        self.assertIsNone(service.ensure_allowed("alpha"))
        for phone in ("beta", "unknown"):
            with self.assertRaises(PermissionError):
                service.ensure_allowed(phone)
            with self.assertRaises(PermissionError):
                await service.listen(phone)
        self.assertEqual(service.status("alpha").state, "stopped")
        self.assertEqual(service.status("beta").state, "stopped")
        self.assertEqual(self.assist.calls, [])
        self.assertEqual(self.playback.plays, [])
        self.assertEqual(self.playback.stops, [])
        self.assertEqual(self.events, [])

        disabled = self.make(settings=ServiceSettings(), phones={"alpha": True})
        with self.assertRaises(PermissionError):
            disabled.ensure_allowed("alpha")
        self.assertEqual(self.playback.stops, [])
        self.assertEqual(self.events, [])

    async def test_wake_preroll_turn_and_default_no_rearm(self):
        service = self.make()
        stream = await service.listen("alpha")
        silence = bytes(640)
        service.feed_pcm("alpha", stream, silence)
        service.feed_pcm("alpha", stream, FRAME)
        service.feed_pcm("alpha", stream, silence)
        service.end_utterance("alpha", stream)
        await service.wait_idle("alpha")
        self.assertEqual(self.assist.frames, [[silence, FRAME, silence]])
        self.assertEqual(service.status("alpha").state, "stopped")
        self.assertFalse(service.feed_pcm("alpha", stream, FRAME))
        self.assertEqual([e.state for e in self.events][-4:], ["listening", "thinking", "speaking", "stopped"])

    async def test_model_warmup_announces_ready_once_after_full_history(self):
        now = [100.0]
        service = self.make(factory=lambda _: WarmingTrigger(), settings=ServiceSettings(
            enabled=True, duplicate_window_seconds=2))
        service._clock = lambda: now[0]
        stream = await service.listen("alpha")
        self.assertEqual(service.status("alpha").state, "armed")
        self.assertEqual(service.status("alpha").reason, "model_warmup")
        for index in range(63):
            now[0] = 100.0 + (index + 1) * .02
            self.assertTrue(service.feed_pcm("alpha", stream, bytes(640)))
        self.assertEqual(service.status("alpha").reason, "model_warmup")
        now[0] = 101.28
        self.assertTrue(service.feed_pcm("alpha", stream, bytes(640)))
        # Fresh listening has no previous-turn cooldown to wait out.
        self.assertEqual(service.status("alpha").reason, "wake_ready")
        for _ in range(12):
            self.assertTrue(service.feed_pcm("alpha", stream, bytes(640)))
        self.assertEqual([event.reason for event in self.events if event.state == "armed"],
                         ["model_warmup", "wake_ready"])
        self.assertEqual(self.assist.calls, [])

    async def test_rearm_wake_ready_waits_for_duplicate_window(self):
        now = [100.0]
        service = self.make(factory=lambda _: WarmingTrigger(), settings=ServiceSettings(
            enabled=True, rearm_after_turn=True, duplicate_window_seconds=2))
        service._clock = lambda: now[0]
        stream = await service.listen("alpha")
        for _ in range(64):
            service.feed_pcm("alpha", stream, bytes(640))
        service.feed_pcm("alpha", stream, FRAME)
        service.end_utterance("alpha", stream)
        await service.wait_idle("alpha")
        self.assertEqual(len(self.assist.calls), 1)
        self.assertEqual(service._quiet_until, 102.0)
        rearmed_at = len(self.events)

        for index in range(64):
            now[0] = 100.0 + (index + 1) * .02
            service.feed_pcm("alpha", stream, bytes(640))
        self.assertTrue(service._sessions["alpha"].detector.ready)
        self.assertEqual(service.status("alpha").reason, "model_warmup")
        now[0] = 101.999
        service.feed_pcm("alpha", stream, bytes(640))
        self.assertFalse(any(event.reason == "wake_ready" for event in self.events[rearmed_at:]))

        now[0] = 102.0
        service.feed_pcm("alpha", stream, bytes(640))
        self.assertEqual(service.status("alpha").reason, "wake_ready")
        self.assertEqual([event.reason for event in self.events[rearmed_at:]], ["wake_ready"])
        service.feed_pcm("alpha", stream, FRAME)
        self.assertEqual(service.status("alpha").state, "listening")
        await service.stop("alpha")
        self.assertEqual(service.status("alpha").state, "stopped")
        self.assertEqual(service.status("alpha").reason, "user_stop")
        self.assertFalse(service.feed_pcm("alpha", stream, FRAME))

    async def test_previous_owner_cooldown_suppresses_ready_and_trigger(self):
        now = [100.0]
        service = self.make(factory=lambda _: WarmingTrigger(), settings=ServiceSettings(
            enabled=True, duplicate_window_seconds=2))
        service._clock = lambda: now[0]
        previous = await service.activate("alpha")
        service.end_utterance("alpha", previous)
        await service.wait_idle("alpha")
        stream = await service.listen("beta")
        for index in range(64):
            now[0] = 100.0 + (index + 1) * .02
            service.feed_pcm("beta", stream, bytes(640))
        self.assertTrue(service._sessions["beta"].detector.ready)
        self.assertEqual(service.status("beta").reason, "model_warmup")
        service.feed_pcm("beta", stream, FRAME)
        self.assertEqual(service.status("beta").state, "armed")
        self.assertFalse(service._sessions["beta"].detector.ready)
        self.assertEqual(len(self.assist.calls), 1)
        self.assertFalse(any(event.phone_id == "beta" and event.reason == "wake_ready"
                             for event in self.events))

    async def test_active_owner_suppresses_other_wake_ready(self):
        service = self.make(factory=lambda _: WarmingTrigger())
        await service.activate("alpha")
        stream = await service.listen("beta")
        for _ in range(64):
            service.feed_pcm("beta", stream, bytes(640))
        self.assertTrue(service._sessions["beta"].detector.ready)
        self.assertEqual(service.status("beta").reason, "model_warmup")
        service.feed_pcm("beta", stream, FRAME)
        self.assertEqual(service.status("beta").state, "armed")
        self.assertEqual(service._owner, "alpha")
        self.assertFalse(any(event.phone_id == "beta" and event.reason == "wake_ready"
                             for event in self.events))

    async def test_new_owner_immediately_refreshes_other_ready_streams(self):
        for mode in ("manual", "wake"):
            with self.subTest(mode=mode):
                service = self.make(factory=lambda _: WarmingTrigger(),
                                    phones={"alpha": True, "beta": True, "inactive": True})
                events = self.events
                beta_stream = await service.listen("beta")
                beta = service._sessions["beta"]
                for _ in range(64):
                    service.feed_pcm("beta", beta_stream, bytes(640))
                self.assertEqual(service.status("beta").reason, "wake_ready")
                resets = beta.detector.resets

                if mode == "manual":
                    await service.activate("alpha")
                else:
                    alpha_stream = await service.listen("alpha")
                    for _ in range(64):
                        service.feed_pcm("alpha", alpha_stream, bytes(640))
                    service.feed_pcm("alpha", alpha_stream, FRAME)

                # No beta audio arrives between owner acquisition and this
                # observation: its previously published Ready must be revoked.
                self.assertEqual(service._owner, "alpha")
                self.assertEqual(service.status("beta").reason, "model_warmup")
                self.assertEqual([(event.phone_id, event.state) for event in events[-2:]],
                                 [("beta", "armed"), ("alpha", "listening")])
                self.assertEqual(beta.stream_id, beta_stream)
                self.assertTrue(beta.detector.ready)
                self.assertEqual(beta.detector.frames, 64)
                self.assertEqual(beta.detector.resets, resets)
                self.assertEqual([event.reason for event in events
                                  if event.phone_id == "beta" and event.state == "armed"],
                                 ["model_warmup", "wake_ready", "model_warmup"])
                count = len(events)
                service.feed_pcm("beta", beta_stream, bytes(640))
                self.assertEqual(len(events), count)
                self.assertEqual(beta.detector.frames, 65)
                self.assertEqual(service.status("inactive").state, "stopped")
                self.assertFalse(any(event.phone_id == "inactive" for event in events))

    async def test_new_stream_restarts_warmup_and_rejects_stale_progress(self):
        service = self.make(factory=lambda _: WarmingTrigger())
        old_stream = await service.listen("alpha")
        for _ in range(64):
            service.feed_pcm("alpha", old_stream, bytes(640))
        await service.preempt("alpha")
        new_stream = await service.listen("alpha")
        self.assertEqual(service.status("alpha").reason, "model_warmup")
        for _ in range(64):
            self.assertFalse(service.feed_pcm("alpha", old_stream, bytes(640)))
        self.assertEqual(service.status("alpha").reason, "model_warmup")
        for _ in range(63):
            service.feed_pcm("alpha", new_stream, bytes(640))
        self.assertEqual(service.status("alpha").reason, "model_warmup")
        service.feed_pcm("alpha", new_stream, bytes(640))
        self.assertEqual(service.status("alpha").reason, "wake_ready")
        self.assertEqual([event.reason for event in self.events if event.state == "armed"],
                         ["model_warmup", "wake_ready", "model_warmup", "wake_ready"])

    async def test_rearm_and_other_phone_reset_announce_fresh_warmup(self):
        service = self.make(factory=lambda _: WarmingTrigger(), settings=ServiceSettings(
            enabled=True, rearm_after_turn=True, duplicate_window_seconds=0))
        streams = {phone: await service.listen(phone) for phone in ("alpha", "beta")}
        for phone, stream in streams.items():
            for _ in range(64):
                service.feed_pcm(phone, stream, bytes(640))
            self.assertEqual(service.status(phone).reason, "wake_ready")
        service.feed_pcm("alpha", streams["alpha"], FRAME)
        service.end_utterance("alpha", streams["alpha"])
        await service.wait_idle("alpha")
        for phone, stream in streams.items():
            self.assertEqual(service.status(phone).state, "armed")
            self.assertEqual(service.status(phone).reason, "model_warmup")
            for _ in range(63):
                service.feed_pcm(phone, stream, bytes(640))
            self.assertEqual(service.status(phone).reason, "model_warmup")
            service.feed_pcm(phone, stream, bytes(640))
            self.assertEqual(service.status(phone).reason, "wake_ready")
            self.assertEqual([event.reason for event in self.events
                              if event.phone_id == phone and event.state == "armed"],
                             ["model_warmup", "wake_ready", "model_warmup", "wake_ready"])

    async def test_suppressed_wake_reset_updates_readiness_without_duplicate_events(self):
        service = self.make(factory=lambda _: WarmingTrigger())
        streams = {phone: await service.listen(phone) for phone in ("alpha", "beta")}
        for phone, stream in streams.items():
            for _ in range(64):
                service.feed_pcm(phone, stream, bytes(640))
        service.feed_pcm("alpha", streams["alpha"], FRAME)
        service.feed_pcm("beta", streams["beta"], FRAME)
        self.assertEqual(service.status("alpha").state, "listening")
        self.assertEqual(service.status("beta").state, "armed")
        self.assertEqual(service.status("beta").reason, "model_warmup")
        service.end_utterance("alpha", streams["alpha"])
        await service.wait_idle("alpha")
        self.assertEqual([event.reason for event in self.events
                          if event.phone_id == "beta" and event.state == "armed"],
                         ["model_warmup", "wake_ready", "model_warmup"])

    async def test_manual_activation_skips_wake_readiness_and_inference(self):
        service = self.make(factory=lambda _: WarmingTrigger())
        stream = await service.activate("alpha")
        self.assertEqual(service.status("alpha").state, "listening")
        self.assertTrue(service.feed_pcm("alpha", stream, FRAME))
        self.assertEqual(service._sessions["alpha"].detector.frames, 0)
        self.assertFalse(any(event.reason in ("model_warmup", "wake_ready") for event in self.events))
        service.end_utterance("alpha", stream)
        await service.wait_idle("alpha")
        self.assertEqual(len(self.assist.calls), 1)

    async def test_detectors_without_readiness_keep_existing_armed_events(self):
        for detector_type in (Trigger, NeverWake):
            with self.subTest(detector=detector_type.__name__):
                service = self.make(factory=lambda _: detector_type())
                stream = await service.listen("alpha")
                service.feed_pcm("alpha", stream, bytes(640))
                self.assertIsNone(service.status("alpha").reason)
                self.assertEqual([event.reason for event in self.events if event.state == "armed"], [None])

    async def test_manual_activation_and_per_phone_conversation_isolation(self):
        service = self.make(factory=lambda _: NeverWake())
        for phone in ("alpha", "alpha", "beta"):
            stream = await service.activate(phone)
            service.feed_pcm(phone, stream, FRAME)
            service.end_utterance(phone, stream)
            await service.wait_idle(phone)
        self.assertEqual(self.assist.calls, [None, "conversation-1", None])
        await service.stop("alpha")
        stream = await service.activate("alpha")
        service.end_utterance("alpha", stream)
        await service.wait_idle("alpha")
        self.assertIsNone(self.assist.calls[-1])

    async def test_reply_callback_is_current_speaking_stream_before_playback(self):
        delivered = []
        def on_reply(phone, stream, text):
            self.assertEqual(service.status(phone).state, "speaking")
            self.assertTrue(service.owns_reply(phone, stream))
            self.assertEqual(self.playback.plays, [])
            delivered.append((phone, stream, text))
        service = self.make(on_reply=on_reply)
        stream = await service.activate("alpha")
        self.assertFalse(service.owns_reply("alpha", stream))
        service.end_utterance("alpha", stream)
        await service.wait_idle("alpha")
        self.assertEqual(delivered, [("alpha", stream, "Synthetic response")])
        self.assertFalse(service.owns_reply("alpha", stream))
        self.assertFalse(service.owns_reply("unknown", stream))
        self.assertNotIn("Synthetic response",
                         json.dumps([asdict(event) for event in self.events]))
        self.assertEqual(set(asdict(service.status("alpha"))), {"phone_id", "state", "reason"})

    async def test_reply_text_is_xml_safe_bounded_and_never_coerces_objects(self):
        class TextAssist(Assist):
            async def run(self, *args, **kwargs):
                result = await super().run(*args, **kwargs)
                result.text = self.text
                return result
        class DangerousText:
            def __str__(self):
                raise AssertionError("untrusted response was coerced")
        class TextSubclass(str):
            pass
        prefix = 'A < B & "C"\t\n\r'
        for text, expected in (
                (prefix + "\x00\x01\x1f\ud800\ufffe\uffff" + "😀" * 2000,
                 (prefix + "😀" * 2000)[:1000]),
                ("\x00" * 4096 + "do not scan this suffix", None),
                ("", None), (None, None), (123, None),
                (DangerousText(), None), (TextSubclass("not exact str"), None)):
            with self.subTest(kind=type(text).__name__):
                assist, delivered = TextAssist(), []
                assist.text = text
                service = self.make(assist=assist, on_reply=lambda *args: delivered.append(args))
                stream = await service.activate("alpha")
                service.end_utterance("alpha", stream)
                await service.wait_idle("alpha")
                self.assertEqual(delivered, [] if expected is None else
                                 [("alpha", stream, expected)])
                self.assertEqual(service.status("alpha").reason, "turn_complete")
                self.assertEqual(len(self.playback.plays), 1)

    async def test_reply_callback_failure_cannot_break_playback_or_cleanup(self):
        def broken(*args):
            raise RuntimeError("synthetic private response in UI failure")
        service = self.make(on_reply=broken)
        stream = await service.activate("alpha")
        service.end_utterance("alpha", stream)
        await service.wait_idle("alpha")
        self.assertEqual(len(self.playback.plays), 1)
        self.assertIn("alpha", self.playback.stops)
        self.assertEqual(service.status("alpha").reason, "turn_complete")
        self.assertNotIn("synthetic private", json.dumps([asdict(e) for e in self.events]))
        await service.activate("beta")
        self.assertEqual(service.status("beta").state, "listening")

    async def test_late_provider_reply_after_preemption_or_replacement_is_suppressed(self):
        class LateAssist(Assist):
            async def run(self, audio, **kwargs):
                self.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    # A provider can finish despite cancellation. Its old token
                    # still must not regain display ownership.
                    return SimpleNamespace(conversation_id="old", text="Late private reply",
                                           tts_url=None)
        for action in ("preempt", "replace"):
            with self.subTest(action=action):
                replies, assist = [], LateAssist()
                service = self.make(assist=assist, on_reply=lambda *args: replies.append(args))
                old = await service.activate("alpha")
                await asyncio.wait_for(assist.started.wait(), 1)
                if action == "preempt":
                    await service.preempt("alpha", "call")
                else:
                    new = await service.listen("alpha")
                    self.assertNotEqual(old, new)
                    self.assertEqual(service.status("alpha").state, "armed")
                self.assertEqual(replies, [])
                self.assertEqual(self.playback.plays, [])
                self.assertFalse(service.owns_reply("alpha", old))

    async def test_preemption_during_playback_revokes_reply_ownership(self):
        class HeldPlayback(Playback):
            def __init__(self):
                super().__init__()
                self.started = asyncio.Event()
            async def play(self, phone, result):
                self.started.set()
                await asyncio.Event().wait()
        playback, replies = HeldPlayback(), []
        service = self.make(playback=playback, on_reply=lambda *args: replies.append(args))
        stream = await service.activate("alpha")
        service.end_utterance("alpha", stream)
        await asyncio.wait_for(playback.started.wait(), 1)
        self.assertTrue(service.owns_reply("alpha", stream))
        self.assertFalse(service.owns_reply("alpha", "stale-stream"))
        self.assertFalse(service.owns_reply("beta", stream))
        await service.preempt("alpha", "call")
        self.assertEqual(len(replies), 1)
        self.assertFalse(service.owns_reply("alpha", stream))
        self.assertEqual(service.status("alpha").state, "stopped")

    async def test_cancellation_queued_at_speaking_suppresses_reply_and_playback(self):
        for target in ("current", "owner"):
            with self.subTest(target=target):
                replies = []
                service = self.make(on_reply=lambda *args: replies.append(args))
                def cancel_when_speaking(event):
                    self.events.append(event)
                    if event.state == "speaking":
                        task = (asyncio.current_task() if target == "current" else
                                service._sessions[event.phone_id].task)
                        task.cancel()
                service._on_event = cancel_when_speaking
                stream = await service.activate("alpha")
                service.end_utterance("alpha", stream)
                await service.wait_idle("alpha")
                self.assertEqual(replies, [])
                self.assertEqual(self.playback.plays, [])
                self.assertIn("alpha", self.playback.stops)
                self.assertFalse(service.owns_reply("alpha", stream))

    async def test_cancellation_queued_by_reply_callback_prevents_playback(self):
        replies = []
        def on_reply(*args):
            replies.append(args)
            asyncio.current_task().cancel()
        service = self.make(on_reply=on_reply)
        stream = await service.activate("alpha")
        service.end_utterance("alpha", stream)
        await service.wait_idle("alpha")
        self.assertEqual(replies, [("alpha", stream, "Synthetic response")])
        self.assertEqual(self.playback.plays, [])
        self.assertIn("alpha", self.playback.stops)
        self.assertFalse(service.owns_reply("alpha", stream))

    async def test_builder_reply_callback_requires_explicit_config_opt_in(self):
        for option in ("missing", False, True):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as tmp:
                config = load_config(Path(__file__).parents[1] /
                                     "examples/local-voice-assistant.example.json")
                config.pop("display_reply_text", None)
                if option != "missing":
                    config["display_reply_text"] = option
                config["service"].update(enabled=True, duplicate_window_seconds=0)
                config["phones"] = {"alpha": True}
                root = Path(tmp)
                (root / ".local/voice").mkdir(parents=True)
                (root / ".local/voice/ha-token.txt").write_text("synthetic-token")
                replies = []
                class Sink:
                    async def stop(self, phone):
                        pass
                service = build_service(config, root, Sink(),
                                        on_reply=lambda *args: replies.append(args))
                service.assist = Assist()
                try:
                    stream = await service.activate("alpha")
                    service.end_utterance("alpha", stream)
                    await service.wait_idle("alpha")
                    self.assertEqual(replies, [("alpha", stream, "Synthetic response")]
                                     if option is True else [])
                finally:
                    await service.close()

    async def test_preempt_cancels_pending_turn_and_invalidates_old_frames(self):
        service = self.make()
        stream = await service.activate("alpha")
        service.feed_pcm("alpha", stream, FRAME)
        await self.assist.started.wait()
        await service.preempt("alpha", "line_busy")
        self.assertEqual(service.status("alpha").state, "stopped")
        self.assertEqual(service.status("alpha").reason, "line_busy")
        self.assertFalse(service.feed_pcm("alpha", stream, FRAME))
        self.assertEqual(self.playback.plays, [])

    async def test_cancel_before_task_first_instruction_releases_arbitration(self):
        service = self.make()
        await service.activate("alpha")
        await service.stop("alpha")
        await service.activate("beta")
        self.assertEqual(service.status("beta").state, "listening")

    async def test_later_preemption_wins_over_suspended_activation(self):
        class DelayedStop(Playback):
            def __init__(self):
                super().__init__()
                self.started, self.release = asyncio.Event(), asyncio.Event()
                self.count = 0
            async def stop(self, phone):
                self.count += 1
                if self.count == 1:
                    self.started.set()
                    await self.release.wait()
        playback = DelayedStop()
        service = self.make(playback=playback)
        arming = asyncio.create_task(service.listen("alpha"))
        await playback.started.wait()
        preempting = asyncio.create_task(service.preempt("alpha", "call"))
        await asyncio.sleep(0)
        playback.release.set()
        with self.assertRaisesRegex(RuntimeError, "superseded"):
            await arming
        await preempting
        self.assertEqual(service.status("alpha").state, "stopped")
        self.assertFalse(any(event.state == "armed" for event in self.events))

    async def test_other_phone_completion_blocks_activation_suspended_before_cooldown(self):
        class DelayedStop(Playback):
            def __init__(self):
                super().__init__()
                self.started, self.release = asyncio.Event(), asyncio.Event()
                self.delay_alpha = True

            async def stop(self, phone):
                self.stops.append(phone)
                if phone == "alpha" and self.delay_alpha:
                    self.delay_alpha = False
                    self.started.set()
                    await self.release.wait()

        playback, assist, events = DelayedStop(), Assist(), []
        now = [100.0]
        service = AssistantService(
            ServiceSettings(enabled=True, duplicate_window_seconds=2),
            {"alpha": True, "beta": True}, lambda _: NeverWake(),
            assist, playback, events.append, clock=lambda: now[0],
        )
        self.addAsyncCleanup(service.close)
        arming = asyncio.create_task(service.activate("alpha"))
        try:
            await asyncio.wait_for(playback.started.wait(), 1)
            beta = await service.activate("beta")
            service.feed_pcm("beta", beta, FRAME)
            service.end_utterance("beta", beta)
            await service.wait_idle("beta")
        finally:
            playback.release.set()

        with self.assertRaisesRegex(RuntimeError, "cooling down"):
            await arming
        self.assertEqual(service.status("alpha").state, "stopped")
        self.assertEqual(service.status("alpha").reason, "duplicate_activation")
        self.assertEqual(len(assist.calls), 1)
        self.assertEqual([event.phone_id for event in events if event.state == "listening"],
                         ["beta"])

        # The rejection retains the other phone's original duplicate deadline.
        now[0] = 101.999
        with self.assertRaisesRegex(RuntimeError, "cooling down"):
            await service.activate("alpha")
        now[0] = 102.0
        await service.activate("alpha")
        self.assertEqual(service.status("alpha").state, "listening")

    async def test_simultaneous_wakes_have_one_owner_and_fresh_audio(self):
        service = self.make()
        a, b = await service.listen("alpha"), await service.listen("beta")
        service.feed_pcm("alpha", a, FRAME)
        service.feed_pcm("beta", b, FRAME)
        service.end_utterance("alpha", a)
        await service.wait_idle("alpha")
        self.assertEqual(len(self.assist.calls), 1)
        self.assertEqual(service.status("beta").state, "armed")
        service.feed_pcm("beta", b, FRAME)
        service.end_utterance("beta", b)
        await service.wait_idle("beta")
        self.assertEqual(self.assist.frames[-1], [FRAME])

    async def test_bounded_backpressure_fails_closed_before_turn_starts(self):
        service = self.make(settings=ServiceSettings(enabled=True, preroll_ms=0, queue_frames=2,
                                                     duplicate_window_seconds=0))
        stream = await service.activate("alpha")
        self.assertTrue(service.feed_pcm("alpha", stream, FRAME))
        self.assertFalse(service.feed_pcm("alpha", stream, FRAME))
        await service.wait_idle("alpha")
        self.assertEqual(service.status("alpha").reason, "audio_backpressure")
        self.assertEqual(service.status("alpha").state, "error")
        self.assertFalse(service.feed_pcm("alpha", stream, FRAME))
        await service.activate("beta")

    async def test_turn_timeout_stops_output_and_input(self):
        service = self.make(settings=ServiceSettings(enabled=True, turn_timeout_seconds=.1))
        stream = await service.activate("alpha")
        await service.wait_idle("alpha")
        self.assertEqual(service.status("alpha").reason, "turn_timeout")
        self.assertFalse(service.feed_pcm("alpha", stream, FRAME))
        self.assertIn("alpha", self.playback.stops)

    async def test_length_limit_ends_stream_without_waiting_for_idle(self):
        service = self.make(settings=ServiceSettings(enabled=True, preroll_ms=0,
                                                     max_utterance_seconds=.1))
        stream = await service.activate("alpha")
        for _ in range(5):
            service.feed_pcm("alpha", stream, FRAME)
        self.assertFalse(service.feed_pcm("alpha", stream, FRAME))
        await service.wait_idle("alpha")
        self.assertEqual(len(self.assist.frames[0]), 5)

    async def test_pcm_contract_and_independent_detectors(self):
        service = self.make()
        stream = await service.listen("alpha")
        for malformed in (b"a", bytearray(640), bytes(1280)):
            with self.assertRaises(ValueError):
                service.feed_pcm("alpha", stream, malformed)
        detector = Trigger()
        with self.assertRaises(ValueError):
            self.make(factory=lambda _: detector)

    async def test_inference_failure_rejects_current_and_future_media(self):
        class BrokenDetector(Trigger):
            def feed(self, pcm):
                raise RuntimeError("sensitive provider details")
        service = self.make(factory=lambda _: BrokenDetector())
        stream = await service.listen("alpha")
        self.assertFalse(service.feed_pcm("alpha", stream, FRAME))
        self.assertFalse(service.feed_pcm("alpha", stream, FRAME))
        self.assertEqual(service.status("alpha").reason, "wake_inference_failed")
        self.assertEqual(self.assist.calls, [])

    async def test_preemption_during_playback_cancels_and_flushes(self):
        class SlowPlayback(Playback):
            def __init__(self):
                super().__init__()
                self.started, self.cancelled = asyncio.Event(), False
            async def play(self, phone, result):
                self.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
        playback = SlowPlayback()
        service = self.make(playback=playback)
        stream = await service.activate("alpha")
        service.end_utterance("alpha", stream)
        await playback.started.wait()
        self.assertFalse(service.feed_pcm("alpha", stream, FRAME))
        await service.preempt("alpha", "radio")
        self.assertTrue(playback.cancelled)
        self.assertEqual(service.status("alpha").state, "stopped")
        self.assertIn("alpha", playback.stops)

    async def test_rearm_is_explicit_and_retains_session(self):
        service = self.make(settings=ServiceSettings(enabled=True, rearm_after_turn=True,
                                                     duplicate_window_seconds=0))
        stream = await service.listen("alpha")
        service.feed_pcm("alpha", stream, FRAME)
        service.end_utterance("alpha", stream)
        await service.wait_idle("alpha")
        self.assertEqual(service.status("alpha").state, "armed")
        self.assertTrue(service.feed_pcm("alpha", stream, bytes(640)))

    async def test_local_endpoint_keeps_pcm_and_waits_for_both_echo_clocks(self):
        now = [100.0]
        service = self.make(settings=ServiceSettings(
            enabled=True, local_endpointing=True, preroll_ms=0, duplicate_window_seconds=0))
        service._clock = lambda: now[0]
        stream = await service.activate("alpha")
        service.feed_pcm("alpha", stream, SPEECH)
        now[0] = 102.49
        service.feed_pcm("alpha", stream, bytes(640))
        self.assertEqual(service.status("alpha").state, "listening")
        now[0] = 102.5
        service.feed_pcm("alpha", stream, bytes(640))
        self.assertEqual(service.status("alpha").state, "thinking")
        self.assertFalse(service.feed_pcm("alpha", stream, SPEECH))
        await service.wait_idle("alpha")
        self.assertEqual(self.assist.frames, [[SPEECH, bytes(640), bytes(640)]])
        self.assertEqual(len(self.playback.plays), 1)
        self.assertEqual(service.status("alpha").reason, "turn_complete")

    async def test_local_no_speech_starts_at_live_frame_not_wake_preroll(self):
        now = [100.0]
        service = self.make(settings=ServiceSettings(
            enabled=True, local_endpointing=True, rearm_after_turn=True))
        service._clock = lambda: now[0]
        stream = await service.listen("alpha")
        service.feed_pcm("alpha", stream, FRAME)
        now[0] = 104.9
        service.feed_pcm("alpha", stream, bytes(640))
        now[0] = 105.1
        service.feed_pcm("alpha", stream, bytes(640))
        self.assertEqual(service.status("alpha").state, "listening")
        now[0] = 110.0
        service.feed_pcm("alpha", stream, bytes(640))
        await service.wait_idle("alpha")
        self.assertEqual(service.status("alpha").reason, "no_speech")
        self.assertEqual(service.status("alpha").state, "error")
        self.assertEqual(self.playback.plays, [])
        self.assertFalse(service.feed_pcm("alpha", stream, FRAME))

    async def test_first_audio_timeout_cancels_ha_and_does_not_rearm(self):
        now = [100.0]
        service = self.make(settings=ServiceSettings(
            enabled=True, local_endpointing=True, rearm_after_turn=True))
        service._clock = lambda: now[0]
        stream = await service.activate("alpha")
        await self.assist.started.wait()
        now[0] = 105.01
        await asyncio.wait_for(service.wait_idle("alpha"), .5)
        self.assertEqual(service.status("alpha").reason, "audio_not_received")
        self.assertEqual(self.playback.plays, [])
        self.assertFalse(service.feed_pcm("alpha", stream, SPEECH))

    async def test_ha_vad_overrides_local_fallback_and_end_closes_capture(self):
        class EventAssist(Assist):
            async def run(self, audio, conversation_id=None, on_event=None):
                self.event = on_event
                return await super().run(audio, conversation_id, on_event)
        now = [100.0]
        assist = EventAssist()
        service = self.make(assist=assist, settings=ServiceSettings(
            enabled=True, local_endpointing=True, duplicate_window_seconds=0))
        service._clock = lambda: now[0]
        stream = await service.activate("alpha")
        await assist.started.wait()
        await assist.event({"type": "stt-vad-start"})
        now[0] = 110.0
        service.feed_pcm("alpha", stream, bytes(640))
        self.assertEqual(service.status("alpha").state, "listening")
        await assist.event({"type": "stt-vad-end"})
        self.assertEqual(service.status("alpha").state, "thinking")
        self.assertFalse(service.feed_pcm("alpha", stream, SPEECH))
        await service.wait_idle("alpha")
        self.assertEqual(len(self.playback.plays), 1)

    async def test_wake_local_endpoint_rearms_successfully_but_manual_is_one_shot(self):
        now = [100.0]
        service = self.make(settings=ServiceSettings(
            enabled=True, local_endpointing=True, rearm_after_turn=True,
            duplicate_window_seconds=0))
        service._clock = lambda: now[0]
        stream = await service.listen("alpha")
        for _ in range(2):
            service.feed_pcm("alpha", stream, FRAME)
            service.feed_pcm("alpha", stream, SPEECH)
            now[0] += 2.5
            service.feed_pcm("alpha", stream, bytes(640))
            await service.wait_idle("alpha")
            self.assertEqual(service.status("alpha").state, "armed")
        self.assertEqual(self.assist.calls, [None, "conversation-1"])
        manual = await service.activate("alpha")
        service.end_utterance("alpha", manual)
        await service.wait_idle("alpha")
        self.assertEqual(service.status("alpha").state, "stopped")
        self.assertFalse(service.feed_pcm("alpha", manual, FRAME))
        await service.activate("alpha")
        self.assertEqual(service.status("alpha").state, "listening")

    async def test_cancelled_wake_turn_never_rearms(self):
        service = self.make(settings=ServiceSettings(
            enabled=True, rearm_after_turn=True, local_endpointing=True,
            duplicate_window_seconds=0))
        stream = await service.listen("alpha")
        service.feed_pcm("alpha", stream, FRAME)
        await self.assist.started.wait()
        service._sessions["alpha"].task.cancel()
        await service.wait_idle("alpha")
        self.assertEqual(service.status("alpha").state, "stopped")
        self.assertEqual(service.status("alpha").reason, "turn_cancelled")
        self.assertFalse(service.feed_pcm("alpha", stream, SPEECH))
        await service.activate("beta")

    async def test_ha_vad_end_cancels_sender_before_stt_end(self):
        peer = SimulatedHASocket()
        sent = asyncio.Event()
        closed = asyncio.Event()
        async def connect(_url):
            return peer
        async def audio():
            try:
                yield SPEECH
                sent.set()
                await asyncio.Event().wait()
            finally:
                closed.set()
        async def event(message):
            if message["type"] == "stt-vad-end":
                self.assertTrue(closed.is_set())
                # Complete the synthetic server's later STT/intent/TTS stages.
                await peer.send(b"\x07")
        client = HAClient("http://example.invalid", "synthetic-token", connect=connect)
        run = asyncio.create_task(client.run(audio(), on_event=event))
        try:
            await asyncio.wait_for(sent.wait(), 1)
            peer._event("stt-vad-end")
            await asyncio.wait_for(run, 1)
        finally:
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
        self.assertTrue(peer.closed)
        self.assertEqual(peer.audio_bytes, len(SPEECH))

    async def test_builder_forwards_exact_cisco_originating_device_id(self):
        config = load_config(Path(__file__).parents[1] / "examples/local-voice-assistant.example.json")
        config["service"]["enabled"] = True
        config["ha"]["device_id"] = "synthetic-cisco-device-212"
        peer = SimulatedHASocket()
        async def connect(_url):
            return peer
        async def audio():
            yield FRAME
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".local/voice").mkdir(parents=True)
            (root / ".local/voice/ha-token.txt").write_text("synthetic-token")
            service = build_service(config, root, object(), connect=connect)
            await service.assist.run(audio())
        self.assertEqual(peer.command["device_id"], "synthetic-cisco-device-212")

    async def test_shutdown_stops_other_phones_even_if_one_sink_fails(self):
        class FailingStop(Playback):
            broken = False
            async def stop(self, phone):
                self.stops.append(phone)
                if self.broken and phone == "alpha":
                    raise RuntimeError("synthetic failure")
        playback = FailingStop()
        service = self.make(playback=playback)
        a, b = await service.listen("alpha"), await service.listen("beta")
        playback.broken = True
        with self.assertRaises(RuntimeError):
            await service.close()
        playback.broken = False
        self.assertEqual(service.status("alpha").state, "stopped")
        self.assertEqual(service.status("beta").state, "stopped")
        self.assertFalse(service.feed_pcm("alpha", a, FRAME))
        self.assertFalse(service.feed_pcm("beta", b, FRAME))

    async def test_full_protocol_to_pcm_output_and_conversation_reuse(self):
        peers, output = [], []
        async def connect(_url):
            peer = SimulatedHASocket()
            peers.append(peer)
            return peer
        async def fetch(_url):
            return AudioPayload(make_wave(FRAME, 16000), "audio/wav")
        class Sink:
            async def write_pcm(self, phone, data, rate):
                output.append((phone, data, rate))
            async def stop(self, phone):
                pass
        client = HAClient("http://example.invalid", "synthetic-token", connect=connect)
        service = self.make(assist=client, playback=WavePlayback(fetch, Sink()))
        for _ in range(2):
            stream = await service.activate("alpha")
            service.feed_pcm("alpha", stream, FRAME)
            service.end_utterance("alpha", stream)
            await service.wait_idle("alpha")
        self.assertEqual(output, [("alpha", FRAME, 16000)] * 2)
        self.assertTrue(all(peer.closed for peer in peers))
        self.assertNotIn("conversation_id", peers[0].command)
        self.assertEqual(peers[1].command["conversation_id"], "synthetic-conversation")

    async def test_queued_playback_drains_before_service_flush(self):
        operations = []
        async def fetch(_url):
            return AudioPayload(make_wave(FRAME), "audio/wav")
        class Sink:
            async def write_pcm(self, phone, data, rate):
                operations.append("enqueue")
            async def drain(self, phone):
                operations.append("drain")
            async def stop(self, phone):
                operations.append("flush")
        output = WavePlayback(fetch, Sink())
        await output.play("alpha", SimpleNamespace(tts_url="/api/tts_proxy/synthetic.wav"))
        await output.stop("alpha")
        self.assertEqual(operations, ["enqueue", "drain", "flush"])

    @unittest.skipUnless(shutil.which("ffmpeg"), "optional local FFmpeg is not installed")
    async def test_optional_ffmpeg_decodes_synthetic_mp3_without_device(self):
        generated = await asyncio.to_thread(subprocess.run, [
            shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "0.1", "-f", "mp3", "pipe:1"],
            capture_output=True, check=True)
        encoded = generated.stdout
        pcm = await FFmpegDecoder(shutil.which("ffmpeg"))(encoded, "audio/mpeg")
        self.assertEqual(pcm.sample_rate, 16000)
        self.assertGreaterEqual(len(pcm.data), 3200)
        self.assertLess(len(pcm.data), 16000)


def make_wave(pcm, rate=16000, channels=1):
    result = io.BytesIO()
    with wave.open(result, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(pcm)
    return result.getvalue()


class ConfigAndAudioTests(unittest.TestCase):
    def test_local_endpoint_clocks_and_rms_are_explicit(self):
        self.assertEqual(pcm_rms(bytes(640)), 0)
        self.assertEqual(pcm_rms(SPEECH), .25)
        self.assertEqual(pcm_rms(b"\x00\x80" * 320), 1)
        endpoint = UtteranceEndpoint(10, .01)
        self.assertIsNone(endpoint.decision(15))
        self.assertEqual(endpoint.decision(15.01), "audio_not_received")
        endpoint.observe(bytes(640), 14)
        self.assertIsNone(endpoint.decision(19))
        self.assertEqual(endpoint.decision(19.01), "no_speech")
        endpoint.observe(SPEECH, 18)
        endpoint.observe(SPEECH, 20)
        self.assertIsNone(endpoint.decision(20.5))
        self.assertEqual(endpoint.decision(21), "end_of_speech")
        endpoint.ha_vad_started = True
        self.assertIsNone(endpoint.decision(100))

    def test_optional_originating_device_id_validation(self):
        example = Path(__file__).parents[1] / "examples/local-voice-assistant.example.json"
        config = load_config(example)
        self.assertNotIn("device_id", config["ha"])
        for value in (None, "", "x" * 129, "two words", "line\nbreak", 1, True):
            with self.subTest(value=value):
                config["ha"]["device_id"] = value
                with self.assertRaisesRegex(ValueError, "device_id"):
                    validate_config(config)

    def test_example_is_disabled_and_valid(self):
        config = load_config(Path(__file__).parents[1] / "examples/local-voice-assistant.example.json")
        self.assertFalse(config["service"]["enabled"])
        self.assertFalse(any(config["phones"].values()))

    def test_display_reply_option_accepts_only_exact_booleans_and_rejects_unknown_keys(self):
        example = Path(__file__).parents[1] / "examples/local-voice-assistant.example.json"
        for value in (False, True):
            config = load_config(example)
            config["display_reply_text"] = value
            self.assertIs(validate_config(config)["display_reply_text"], value)
        config = load_config(example)
        config.pop("display_reply_text", None)
        self.assertFalse(validate_config(config).get("display_reply_text", False))
        for value in ("true", "false", 1, 0, None, [], {}):
            with self.subTest(value=value):
                config = load_config(example)
                config["display_reply_text"] = value
                with self.assertRaisesRegex(ValueError, "display_reply_text"):
                    validate_config(config)
        config = load_config(example)
        config["display_reply_text"] = True
        config["display_reply_history"] = True
        with self.assertRaises(ValueError):
            validate_config(config)

    def test_private_paths_reject_escape_including_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".local").mkdir()
            (root / ".local/escape").symlink_to(root)
            for path in ("public.json", ".local/../public.json", ".local/escape/private.json"):
                with self.assertRaises(ValueError):
                    private_path(path, root)

    def test_private_paths_reject_symlinked_private_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "public").mkdir()
            (root / ".local").symlink_to(root / "public", target_is_directory=True)
            for path in (".local/reply.wav", str(root / ".local/reply.wav")):
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, "must not be a symlink"):
                    private_path(path, root)

    def test_ha_byte_limit_matches_configured_utterance_limit(self):
        config = load_config(Path(__file__).parents[1] / "examples/local-voice-assistant.example.json")
        config["service"].update(enabled=True, max_utterance_seconds=60)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".local/voice").mkdir(parents=True)
            (root / ".local/voice/ha-token.txt").write_text("synthetic-token")
            service = build_service(config, root, object())
            self.assertEqual(service.assist.max_input_bytes, 60 * 32000)

    def test_wave_downmix_rate_and_duration_are_explicit(self):
        self.assertEqual(decode_wave(make_wave(b"\x02\x00\x04\x00", 8000, 2)),
                         PCMOutput(b"\x03\x00", 8000))
        with self.assertRaises(ValueError):
            decode_wave(make_wave(FRAME), max_seconds=.001)
        with self.assertRaises(ValueError):
            decode_wave(make_wave(FRAME)[:-1])

    def test_settings_reject_nonfinite_and_truthy_strings(self):
        for args in ({"enabled": "false"}, {"preroll_ms": 3}, {"queue_frames": 2},
                     {"turn_timeout_seconds": float("nan")}, {"rearm_after_turn": 1},
                     {"local_endpointing": "true"}, {"speech_rms_threshold": 0},
                     {"speech_rms_threshold": float("inf")}):
            with self.assertRaises(ValueError):
                ServiceSettings(**args)


if __name__ == "__main__":
    unittest.main()
