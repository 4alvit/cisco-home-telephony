"""Hardware-free integration checks for the bounded voice bridge."""
import asyncio
import importlib.util
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

RUNNER_SPEC = importlib.util.spec_from_file_location(
    'controller_progress_subject', ROOT / 'scripts/voice_session.py')
RUNNER = importlib.util.module_from_spec(RUNNER_SPEC)
RUNNER_SPEC.loader.exec_module(RUNNER)


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
    def test_explicit_session_duration_is_bounded_at_five_minutes(self):
        for duration in (30, 180, 300):
            controller = PhoneVoiceController(Session(), None, 'phone', duration=duration)
            self.assertEqual(controller.duration, duration)
        for duration in (0, -1, 301, True, '300', 10 ** 400, float('inf'), float('nan')):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                PhoneVoiceController(Session(), None, 'phone', duration=duration)

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


class UsableLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def scripted_run(self, phases, *, duration=180):
        now = [0.0]
        session = Session()
        phases = iter(phases)
        fed = []
        service = SimpleNamespace(state='armed', ensure_allowed=lambda _: None)

        async def listen(_):
            return 'one-stream'

        async def stop(*_, **__):
            service.state = 'stopped'

        def poll():
            now[0], service.state, audio = next(phases)
            if service.state == 'transport_stopped':
                session.state = 'stopped'
            return [bytes(320)] if audio else []

        session.poll = poll
        service.listen = listen
        service.stop = stop
        service.status = lambda _: SimpleNamespace(state=service.state)
        service.feed_pcm = lambda _phone, stream, _pcm: fed.append((now[0], stream))
        controller = PhoneVoiceController(session, service, 'phone', duration=duration,
                                          receive_idle_seconds=3, clock=lambda: now[0])
        result = await controller.run(mode='wake')
        return result, session, fed

    async def test_processing_and_playback_pause_idle_check_then_resume_with_fresh_grace(self):
        result, session, fed = await self.scripted_run([
            (0, 'armed', True), (20, 'thinking', False), (25, 'speaking', False),
            (26, 'armed', False), (28.99, 'armed', False), (29.01, 'armed', False)])
        self.assertEqual(result.reason, 'receive_idle_timeout')
        self.assertEqual(fed, [(0, 'one-stream')])
        self.assertEqual(session.stops, ['receive_idle_timeout'])
        self.assertTrue(result.stop_confirmed)

    async def test_transport_failure_still_stops_during_processing(self):
        result, session, _ = await self.scripted_run([
            (0, 'thinking', False), (1, 'transport_stopped', False)])
        self.assertEqual(result.reason, 'transport_stopped')
        self.assertEqual(session.stops, ['transport_stopped'])

    async def test_deadline_discovered_inside_transport_poll_is_normal_expiry(self):
        now = [0.0]
        commands = []
        media = SimpleNamespace(bind_ip='192.0.2.10', bind_port=24000, phone_port=24002,
                                open=mock.Mock(), close=mock.Mock(), receive=mock.Mock(return_value=[]))
        session = VoiceSession(commands.append, media, lambda: 'idle', duration=300,
                               enable_microphone=True, media_reserved=True, clock=lambda: now[0])
        poll = session.poll

        def expiry_poll():
            now[0] = 300.0
            return poll()

        async def listen(_):
            return 'one-stream'

        async def stop(*_, **__):
            pass

        session.poll = expiry_poll
        service = SimpleNamespace(ensure_allowed=lambda _: None, listen=listen, stop=stop,
                                  status=lambda _: SimpleNamespace(state='armed'))
        result = await PhoneVoiceController(session, service, 'phone', duration=300,
                                            clock=lambda: now[0]).run(mode='wake')
        self.assertEqual(session.reason, 'duration_expired')
        self.assertEqual(result.reason, 'lease_expired')
        self.assertTrue(result.stop_confirmed)
        self.assertEqual(commands, ['RTPTx:192.0.2.10:24000', 'RTPTx:Stop'])
        media.receive.assert_not_called()

    async def test_wake_rearm_never_renews_deadline_or_feeds_at_expiry(self):
        result, session, fed = await self.scripted_run([
            (0, 'armed', True), (90, 'thinking', False), (179, 'armed', True),
            (180, 'armed', True)])
        self.assertEqual(result.reason, 'lease_expired')
        self.assertEqual(fed, [(0, 'one-stream'), (179, 'one-stream')])
        self.assertEqual(session.started, 1)
        self.assertEqual(session.stops, ['lease_expired'])

    async def test_manual_finishes_one_turn_even_when_service_can_rearm(self):
        session = Session()
        session.poll = lambda: [bytes(320)]
        turns = []

        class Assist:
            async def run(self, audio, **kwargs):
                async for _ in audio:
                    turns.append(True)
                    break
                return SimpleNamespace(conversation_id='one-conversation')

        class NoManualWake:
            def feed(self, _):
                raise AssertionError('manual completion must not feed the rearmed detector')
            def reset(self):
                pass

        async def decode(_):
            return bytes(320), 8000

        service = AssistantService(ServiceSettings(enabled=True, rearm_after_turn=True,
                                                   duplicate_window_seconds=0),
                                   {'phone': True}, lambda _: NoManualWake(), Assist(),
                                   PhonePCMPlayback(session, decode))
        result = await asyncio.wait_for(PhoneVoiceController(session, service, 'phone',
                                                              duration=180).run(), 2)
        self.assertEqual(result.reason, 'turn_complete')
        self.assertEqual(len(turns), 1)
        self.assertEqual(len(session.sent), 1)
        self.assertEqual(session.started, 1)

    async def test_wake_rearms_for_two_successful_turns_with_one_transport_start(self):
        session = Session()
        session.poll = lambda: [bytes(320)]
        turns = []

        class Wake:
            def feed(self, _): return True
            def reset(self): pass

        class Assist:
            async def run(self, audio, **kwargs):
                async for _ in audio:
                    turns.append(kwargs.get('conversation_id'))
                    break
                return SimpleNamespace(conversation_id='same-conversation')

        async def decode(_):
            return bytes(320), 8000

        def on_event(event):
            if event.state == 'armed' and len(turns) == 2:
                controller.request_stop('display_stop')

        service = AssistantService(ServiceSettings(enabled=True, rearm_after_turn=True,
                                                   duplicate_window_seconds=0),
                                   {'phone': True}, lambda _: Wake(), Assist(),
                                   PhonePCMPlayback(session, decode), on_event=on_event)
        controller = PhoneVoiceController(session, service, 'phone', duration=180)
        result = await asyncio.wait_for(controller.run(mode='wake'), 2)
        self.assertEqual(result.reason, 'display_stop')
        self.assertEqual(turns, [None, 'same-conversation'])
        self.assertEqual(len(session.sent), 2)
        self.assertEqual(session.started, 1)
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


class StoppedTransportPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def assert_pending_playback_outcome(self, playback_type, reason, *, failed):
        media = SimpleNamespace(
            bind_ip='192.0.2.10', bind_port=24000, phone_port=24002,
            open=mock.Mock(), close=mock.Mock(), send_pcm=mock.Mock(return_value=True))
        session = VoiceSession(lambda _uri: None, media, lambda: 'idle',
                               enable_playback=True, media_reserved=True, clock=lambda: 0)
        session.start()
        queued, release = threading.Event(), threading.Event()
        real_play = session.play
        play_results = []

        def pending_play(*args, **kwargs):
            queued.set()
            if not release.wait(2):
                raise AssertionError('pending playback barrier was not released')
            result = real_play(*args, **kwargs)
            play_results.append(result)
            return result

        session.play = pending_play
        if playback_type is RtpPCMSink:
            sink = RtpPCMSink(session, 'phone')

            class Playback:
                async def play(self, _phone_id, _result):
                    await sink.write_pcm('phone', bytes(320), 8000)

                async def stop(self, phone_id):
                    await sink.stop(phone_id)

            playback = Playback()
        else:
            async def decode(_result):
                return bytes(320), 8000

            playback = PhonePCMPlayback(session, decode)

        class Assist:
            async def run(self, _audio, **_kwargs):
                return SimpleNamespace(conversation_id=None)

        progress = RUNNER.SessionProgress(ROOT)
        events = []

        def on_event(event):
            events.append((event.state, event.reason))
            if event.reason != 'new_session':
                progress.publish(event.state, event.reason)

        service = AssistantService(ServiceSettings(enabled=True, rearm_after_turn=True),
                                   {'phone': True}, lambda _: NeverWake(), Assist(),
                                   RUNNER.ProgressPlayback(playback, progress), on_event=on_event)
        try:
            await service.activate('phone')
            self.assertTrue(await asyncio.to_thread(queued.wait, 2))
            self.assertEqual(service.status('phone').state, 'speaking')
            # The controller stops transport before awaiting assistant.stop.
            # Hold that exact gap open until the already queued write returns.
            await asyncio.to_thread(session.stop, reason)
            self.assertTrue(session.stop_confirmed)
            release.set()
            await asyncio.wait_for(service.wait_idle('phone'), 2)

            self.assertEqual(play_results, [False])
            media.send_pcm.assert_not_called()
            self.assertEqual(progress.turns_completed, 0)
            self.assertEqual(progress.failed, failed)
            self.assertEqual(service.status('phone').state, 'error' if failed else 'stopped')
            self.assertEqual(service.status('phone').reason,
                             'turn_failed' if failed else 'turn_cancelled')
            self.assertEqual(('error', 'turn_failed') in events, failed)
            await service.stop('phone', reason='shutdown')
            # A later stopped event must preserve genuine failures, while a
            # canceled reply must neither latch an error nor count as complete.
            self.assertEqual(progress.failed, failed)
            self.assertEqual(progress.turns_completed, 0)
        finally:
            release.set()
            await service.close()
            session.stop('synthetic_test_cleanup')

    async def test_orderly_stop_cancels_pending_reply_without_error_or_completion(self):
        for playback_type in (RtpPCMSink, PhonePCMPlayback):
            for reason in ('shutdown', 'explicit_stop', 'display_stop',
                           'lease_expired', 'duration_expired'):
                with self.subTest(sender=playback_type.__name__, reason=reason):
                    await self.assert_pending_playback_outcome(playback_type, reason, failed=False)

    async def test_transport_failure_keeps_pending_reply_error(self):
        for playback_type in (RtpPCMSink, PhonePCMPlayback):
            for reason in ('media_send_failed', 'unknown_transport_failure'):
                with self.subTest(sender=playback_type.__name__, reason=reason):
                    await self.assert_pending_playback_outcome(playback_type, reason, failed=True)
