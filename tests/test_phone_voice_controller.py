"""Hardware-free integration checks for the bounded voice bridge."""
import asyncio
from pathlib import Path
import struct
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'containers/asterisk'))
sys.path.insert(0, str(ROOT))
from phone_voice_controller import (PhoneVoiceController, PhonePCMPlayback, RtpPCMSink,
                                    playback_frames, upsample_8k_frame)
from phone_voice_transport import VoiceSession
from voice_assistant.service import AssistantService, ServiceSettings


class Session:
    def __init__(self, frames=None):
        self.state, self.stop_confirmed = 'new', False
        self.frames = list(frames or [])
        self.sent, self.stops = [], []
        self.started = 0

    def start(self):
        self.started += 1
        self.state = 'active'

    def poll(self):
        if self.frames:
            return [self.frames.pop(0)]
        return []

    def play(self, pcm, *, allowed=None):
        if allowed is not None and not allowed():
            return False
        if self.state != 'active':
            return False
        self.sent.append(pcm)
        return True

    def stop(self, reason):
        self.stops.append(reason)
        self.state, self.stop_confirmed = 'stopped', True


class NeverWake:
    def feed(self, pcm): return False
    def reset(self): pass


class PcmTests(unittest.TestCase):
    def test_pcm_byte_order_interpolation_and_exact_duration(self):
        raw = struct.pack('<160h', *range(-80, 80))
        result = struct.unpack('<320h', upsample_8k_frame(raw))
        self.assertEqual(result[:4], (-80, -80, -79, -79))
        self.assertEqual(result[-2:], (79, 79))
        with self.assertRaises(ValueError): upsample_8k_frame(raw[:-1])

    def test_downsampling_and_last_frame_padding(self):
        raw = struct.pack('<4h', -100, 100, 1000, 2000)
        frames = list(playback_frames(raw, 16000))
        self.assertEqual(len(frames), 1)
        self.assertEqual(struct.unpack('<2h', frames[0][:4]), (0, 1500))
        self.assertEqual(frames[0][4:], bytes(316))
        for pcm, rate in ((b'x', 8000), (b'', 44100), (bytes(480002), 8000)):
            with self.subTest(rate=rate, length=len(pcm)), self.assertRaises(ValueError):
                list(playback_frames(pcm, rate))


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_pcm_round_trip_with_real_assistant_coordinator(self):
        session = Session([struct.pack('<160h', *([800] * 160))])
        captured = []

        class Assist:
            async def run(self, audio, **kwargs):
                async for frame in audio:
                    captured.append(frame)
                    break
                return SimpleNamespace(conversation_id='synthetic-conversation')

        async def decode(result): return struct.pack('<320h', *([1600] * 320)), 16000
        playback = PhonePCMPlayback(session, decode)
        service = AssistantService(ServiceSettings(enabled=True), {'phone': True},
                                   lambda _: NeverWake(), Assist(), playback)
        controller = PhoneVoiceController(session, service, 'phone', duration=1)
        result = await controller.run()
        self.assertEqual(result.reason, 'turn_complete')
        self.assertTrue(result.stop_confirmed)
        self.assertEqual(captured, [struct.pack('<320h', *([800] * 320))])
        self.assertEqual(session.sent, [struct.pack('<160h', *([1600] * 160))])
        self.assertEqual(service.status('phone').state, 'stopped')
        with self.assertRaises(ValueError): await controller.run()

    async def test_wake_mode_expiry_stops_capture_without_invoking_assist(self):
        session = Session([bytes(320)])

        class Assist:
            async def run(self, *args, **kwargs):
                self.fail('silent stream must not invoke Assist')

        async def decode(result): raise AssertionError('no playback expected')
        service = AssistantService(ServiceSettings(enabled=True), {'phone': True},
                                   lambda _: NeverWake(), Assist(), PhonePCMPlayback(session, decode))
        result = await PhoneVoiceController(session, service, 'phone', duration=.03).run(mode='wake')
        self.assertEqual(result.reason, 'lease_expired')
        self.assertTrue(result.stop_confirmed)
        self.assertEqual(session.sent, [])

    async def test_stop_before_start_never_opens_microphone(self):
        session = Session()
        service = SimpleNamespace(stop=None, status=lambda _: SimpleNamespace(state='stopped'),
                                  ensure_allowed=lambda _: None)
        async def stop(*args, **kwargs): pass
        service.stop = stop
        controller = PhoneVoiceController(session, service, 'phone', duration=1)
        controller.request_stop('display_stop')
        result = await controller.run()
        self.assertEqual(session.started, 0)
        self.assertEqual(result.reason, 'display_stop')

    async def test_provider_failure_stops_transport_and_does_not_leak_exception(self):
        session = Session([bytes(320)])
        class Assist:
            async def run(self, *args, **kwargs): raise RuntimeError('secret URL and transcript')
        async def decode(result): raise AssertionError('no playback expected')
        service = AssistantService(ServiceSettings(enabled=True), {'phone': True},
                                   lambda _: NeverWake(), Assist(), PhonePCMPlayback(session, decode))
        result = await PhoneVoiceController(session, service, 'phone', duration=1).run()
        self.assertEqual(result.reason, 'assistant_error')
        self.assertTrue(result.stop_confirmed)
        self.assertNotIn('secret', repr(result))

    async def test_sender_backpressure_retries_the_same_frame(self):
        session = Session()
        session.start()
        attempts = []
        def send(frame, *, allowed=None):
            attempts.append(frame)
            return len(attempts) > 1
        session.play = send
        async def decode(result): return bytes(320), 8000
        playback = PhonePCMPlayback(session, decode)
        await playback.play('phone', None)
        self.assertEqual(attempts, [bytes(320), bytes(320)])
        self.assertEqual(playback.frames_sent, 1)

class LifecycleRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_service_never_starts_transport(self):
        session = Session()
        service = AssistantService(ServiceSettings(enabled=False), {'phone': True},
                                   lambda _: NeverWake(), None,
                                   PhonePCMPlayback(session, None))
        with self.assertRaises(PermissionError):
            await PhoneVoiceController(session, service, 'phone').run()
        self.assertEqual(session.started, 0)

    async def test_stop_while_transport_starts_does_not_activate_assist(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        session = Session()
        def start():
            entered.set()
            release.wait(1)
            session.state = 'active'
        session.start = start
        activated = []
        async def activate(_): activated.append(True)
        async def stop(*_, **__): pass
        service = SimpleNamespace(ensure_allowed=lambda _: None, activate=activate, stop=stop,
                                  status=lambda _: SimpleNamespace(state='stopped'))
        controller = PhoneVoiceController(session, service, 'phone', duration=1)
        task = asyncio.create_task(controller.run())
        await asyncio.to_thread(entered.wait, 1)
        controller.request_stop()
        release.set()
        result = await task
        self.assertEqual(activated, [])
        self.assertTrue(result.stop_confirmed)

    async def test_silent_rtp_stops_at_input_timeout_before_lease(self):
        session = Session()
        service = AssistantService(ServiceSettings(enabled=True), {'phone': True},
                                   lambda _: NeverWake(), None,
                                   PhonePCMPlayback(session, None))
        result = await PhoneVoiceController(session, service, 'phone', duration=1,
                                            receive_idle_seconds=.02).run(mode='wake')
        self.assertEqual(result.reason, 'receive_idle_timeout')
        self.assertTrue(result.stop_confirmed)


class QueuedPlaybackCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def assert_queued_frame_cancelled(self, playback_type):
        sent = []
        frame = struct.pack('<160h', *([1200] * 160))

        def send_pcm(pcm):
            sent.append(pcm)
            return True

        media = SimpleNamespace(
            bind_ip='192.0.2.10', bind_port=24000, phone_port=24002,
            open=mock.Mock(), close=mock.Mock(), send_pcm=send_pcm)
        commands = []
        session = VoiceSession(commands.append, media, lambda: 'idle',
                               enable_playback=True, media_reserved=True,
                               clock=lambda: 0)
        session.start()
        self.addCleanup(session.stop, 'synthetic_test_cleanup')

        async def decode(_result):
            return frame, 8000

        if playback_type is RtpPCMSink:
            sink = playback_type(session, 'synthetic-phone')

            def write():
                return sink.write_pcm('synthetic-phone', frame, 8000)
        else:
            sink = playback_type(session, decode)

            def write():
                return sink.play('synthetic-phone', None)

        locked, release, queued = (threading.Event() for _ in range(3))

        def hold_session_lock():
            with session._lock:
                locked.set()
                release.wait(3)

        # Observe entry into the worker without replacing the real locked send.
        real_play = session.play

        def observe_play(*args, **kwargs):
            queued.set()
            return real_play(*args, **kwargs)

        session.play = observe_play
        holder = threading.Thread(target=hold_session_lock, daemon=True)
        holder.start()
        task = None
        try:
            self.assertTrue(await asyncio.to_thread(locked.wait, 1))
            task = asyncio.create_task(write())
            self.assertTrue(await asyncio.to_thread(queued.wait, 1))
            self.assertFalse(task.done())

            # Stop returns while the old generation's worker still waits on
            # VoiceSession._lock. Releasing it must not emit the stale frame.
            await asyncio.wait_for(sink.stop('synthetic-phone'), 1)
            self.assertEqual(sent, [])
            self.assertEqual(session.state, 'active')
            release.set()
            await asyncio.wait_for(task, 1)
            self.assertEqual(sent, [])
            self.assertEqual(sink.frames_sent, 0)

            # The fake media is usable, and only the old generation was stopped.
            await asyncio.wait_for(write(), 1)
            self.assertEqual(sent, [frame])
            self.assertEqual(sink.frames_sent, 1)
            self.assertEqual(len(commands), 1)
        finally:
            release.set()
            await asyncio.to_thread(holder.join, 1)
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_rtp_pcm_sink_discards_send_queued_before_stop(self):
        await self.assert_queued_frame_cancelled(RtpPCMSink)

    async def test_phone_pcm_playback_discards_send_queued_before_stop(self):
        await self.assert_queued_frame_cancelled(PhonePCMPlayback)
