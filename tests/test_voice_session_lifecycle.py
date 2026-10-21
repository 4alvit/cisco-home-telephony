"""Failure-path integration checks; all phone, network and process I/O is fake."""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import ExitStack
import importlib.util
import http.client
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "voice_session_lifecycle_subject", ROOT / "scripts/voice_session.py")
subject = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = subject
SPEC.loader.exec_module(subject)
import phone_voice_display
from voice_assistant.config import NeverWake
from voice_assistant.service import AssistantService, ServiceSettings


REAL_DISPLAY_SERVER = phone_voice_display.VoiceDisplayServer
REAL_VOICE_SESSION = subject.transport.VoiceSession
REAL_WATCHDOG = subject.transport._Watchdog


class FakeSession:
    def __init__(self, events):
        self.events = events
        self.state = "new"
        self.stop_confirmed = False
        self.starts = 0
        self.start_hook = None

    def start(self):
        self.events.append("media.start")
        self.starts += 1
        self.state = "active"
        if self.start_hook:
            self.start_hook()

    def poll(self):
        return []

    def stop(self, reason):
        self.events.append("media.stop")
        self.state = "stopped"
        self.stop_confirmed = True


class FakeService:
    def __init__(self, events):
        self.events = events
        self.activations = 0
        self.close_error = None
        self.playback = SimpleNamespace(play=mock.AsyncMock(return_value=subject.PlaybackResult(1, 640)),
                                        stop=mock.AsyncMock())

    def ensure_allowed(self, phone_id):
        pass

    async def activate(self, phone_id):
        self.activations += 1
        self.events.append("assistant.activate")
        return "synthetic-stream"

    listen = activate

    def status(self, phone_id):
        # End immediately unless the controller is already handling a stop.
        return SimpleNamespace(state="stopped")

    async def stop(self, phone_id, reason):
        self.events.append("assistant.stop")

    async def close(self):
        self.events.append("assistant.close")
        if self.close_error:
            raise self.close_error


class SessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.events = []
        self.loop = asyncio.get_running_loop()
        self.add_signal = self.stack.enter_context(
            mock.patch.object(self.loop, "add_signal_handler"))
        self.remove_signal = self.stack.enter_context(
            mock.patch.object(self.loop, "remove_signal_handler"))
        self.gateway = SimpleNamespace(execute=mock.Mock())
        self.observer = SimpleNamespace(
            thread=SimpleNamespace(start=mock.Mock()), ready=threading.Event(),
            done=threading.Event(), status=mock.Mock(return_value="idle"))
        self.observer.ready.set()
        self.media = SimpleNamespace(
            bind_ip="192.0.2.10", bind_port=24576, phone_port=24578,
            receiver=SimpleNamespace(counters=Counter()))
        self.watchdog = SimpleNamespace(
            execute=mock.Mock(), arm=mock.Mock(),
            finish=mock.Mock(return_value={"watchdog_finished": True,
                                           "watchdog_stop_confirmed": True}))
        self.session = FakeSession(self.events)
        self.service = FakeService(self.events)
        self.display = SimpleNamespace(
            start=mock.Mock(),
            start_status=mock.Mock(return_value=SimpleNamespace(
                page_url="http://192.0.2.10:18085/voice/synthetic-page")),
            update_status=mock.Mock(), close=mock.Mock())
        replacements = {
            "_gateway_module": lambda: self.gateway,
            "_GatewayObserver": mock.Mock(return_value=self.observer),
            "UdpMedia": mock.Mock(return_value=self.media),
            "_Watchdog": mock.Mock(return_value=self.watchdog),
            "VoiceSession": mock.Mock(return_value=self.session),
        }
        for name, replacement in replacements.items():
            self.stack.enter_context(mock.patch.object(subject.transport, name, replacement))
        self.media_factory = replacements["UdpMedia"]
        self.build_service = self.stack.enter_context(
            mock.patch.object(subject, "build_service", return_value=self.service))
        self.stack.enter_context(mock.patch.object(
            phone_voice_display, "VoiceDisplayServer", return_value=self.display))
        self.args = SimpleNamespace(workspace=str(ROOT), ffmpeg="unused-ffmpeg",
                                    display_port=18085)
        self.config = {"bind_ipv4": "192.0.2.10", "bind_port": 24576,
                       "phone_rtp_port": 24578, "duration_seconds": .2,
                       "receive_idle_seconds": .1}
        self.phone = {"id": "synthetic-phone", "ipv4": "192.0.2.20"}
        self.voice = {"service": {"enabled": True}, "wake": {"mode": "manual"}}

    async def invoke(self):
        """Permit either propagated or redacted errors; assert cleanup separately."""
        try:
            result = await asyncio.wait_for(subject.run_session(
                self.args, self.config, {}, self.phone, self.voice), 2)
            return result, None
        except Exception as error:
            return None, error

    def assert_observer_stopped(self):
        if self.observer.thread.start.called:
            self.assertTrue(self.observer.done.is_set(),
                            "a started AMI observer must be stopped on every exit")

    def enable_progress(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        directory = root / '.local' / 'run'
        directory.mkdir(parents=True, mode=0o700)
        self.args.workspace = str(root)
        self.args.status_file = str(directory / 'progress.json')
        return Path(self.args.status_file)

    async def test_progress_tracks_readiness_and_completed_replies_without_private_text(self):
        path = self.enable_progress()
        snapshots = []

        async def activate(phone_id):
            callback = self.build_service.call_args.kwargs['on_event']
            snapshots.append(json.loads(path.read_text()))
            for state, reason in (('stopped', 'new_session'), ('armed', 'model_warmup'),
                                  ('armed', 'wake_ready'), ('listening', None),
                                  ('thinking', 'synthetic-private-token'), ('speaking', None)):
                callback(SimpleNamespace(state=state, reason=reason))
                snapshots.append(json.loads(path.read_text()))
            await self.service.playback.play(phone_id, 'synthetic-private-reply')
            callback(SimpleNamespace(state='stopped', reason='turn_complete'))
            snapshots.append(json.loads(path.read_text()))
            return 'synthetic-stream'

        self.service.activate = activate
        result, error = await self.invoke()
        self.assertIsNone(error)
        self.assertIsNone(result['error'])
        self.assertEqual([value['phase'] for value in snapshots], [
            'preparing', 'preparing', 'preparing', 'armed', 'listening', 'thinking', 'speaking', 'stopped'])
        self.assertEqual([value['turns_completed'] for value in snapshots], [0] * 7 + [1])
        self.assertEqual(result['turns_completed'], 1)
        self.assertEqual(json.loads(path.read_text())['phase'], 'stopped')
        self.assertNotIn('synthetic-private', json.dumps(snapshots) + json.dumps(result))
        self.assertEqual(self.media_factory.call_args.kwargs['max_packets'],
                         subject.transport.packet_budget(self.config['duration_seconds']))

    async def test_status_failure_stops_capture_and_preserves_cleanup(self):
        path = self.enable_progress()
        publish = subject.SessionProgress.publish

        def fail_listening(progress, phase, reason=None):
            if phase == 'listening':
                raise OSError('synthetic-private-path')
            return publish(progress, phase, reason)

        async def activate(phone_id):
            self.build_service.call_args.kwargs['on_event'](
                SimpleNamespace(state='listening', reason=None))
            return 'synthetic-stream'

        self.service.activate = activate
        with mock.patch.object(subject.SessionProgress, 'publish', fail_listening):
            result, error = await self.invoke()
        self.assertIsNone(error)
        self.assertEqual(result['error'], 'voice_session_failed')
        self.assertEqual(json.loads(path.read_text())['phase'], 'error')
        self.assertTrue(self.session.stop_confirmed)
        self.watchdog.finish.assert_called_once()
        self.assertNotIn('synthetic-private-path', json.dumps(result))

    async def test_prestart_failure_leaves_error_snapshot(self):
        path = self.enable_progress()
        self.build_service.side_effect = ValueError('synthetic-model-details')
        result, error = await self.invoke()
        self.assertIsNotNone(error)
        value = json.loads(path.read_text())
        self.assertEqual((value['phase'], value['reason'], value['turns_completed']), ('error', None, 0))
        self.assertEqual(self.session.starts, 0)

    async def test_provider_error_is_not_hidden_by_shutdown_event(self):
        path = self.enable_progress()

        async def activate(phone_id):
            callback = self.build_service.call_args.kwargs['on_event']
            callback(SimpleNamespace(state='error', reason='turn_failed'))
            callback(SimpleNamespace(state='stopped', reason='shutdown'))
            self.assertEqual(json.loads(path.read_text())['phase'], 'error')
            return 'synthetic-stream'

        self.service.activate = activate
        result, error = await self.invoke()
        self.assertIsNone(error)
        self.assertEqual(result['error'], 'voice_session_failed')
        self.assertEqual(json.loads(path.read_text())['reason'], 'turn_failed')
        self.assertEqual(json.loads(path.read_text())['phase'], 'error')

    async def test_long_session_display_remains_bounded_through_cleanup(self):
        self.config['duration_seconds'] = 300
        with mock.patch.object(phone_voice_display, 'VoiceDisplayServer', return_value=self.display) as factory:
            result, error = await self.invoke()
        self.assertIsNone(error)
        self.assertEqual(factory.call_args.kwargs['ttl_seconds'], 330)

    async def test_service_construction_failure_does_not_leave_observer_running(self):
        self.build_service.side_effect = ValueError("synthetic-invalid-model")
        result, error = await self.invoke()
        self.assertTrue(error is not None or result["error"])
        self.assertEqual(self.session.starts, 0)
        self.gateway.execute.assert_not_called()
        self.assert_observer_stopped()

    async def test_disabled_service_has_no_audio_and_no_background_observer(self):
        # Exercise the real builder's gate, before it could read any token/model.
        from voice_assistant.config import build_service
        self.build_service.side_effect = build_service
        self.voice = {
            "service": {"enabled": False}, "phones": {"synthetic-phone": True},
            "ha": {"base_url": "http://192.0.2.30:8123",
                   "token_file": ".local/nonexistent-synthetic-token",
                   "pipeline_id": "synthetic-pipeline"},
            "wake": {"mode": "manual", "bundle_file": ".local/unused"},
        }
        result, error = await self.invoke()
        self.assertTrue(error is not None or result["error"])
        self.assertEqual(self.session.starts, 0)
        self.gateway.execute.assert_not_called()
        self.watchdog.arm.assert_not_called()
        self.assert_observer_stopped()

    async def test_media_constructor_failure_stops_observer(self):
        self.media_factory.side_effect = ValueError("synthetic-media-configuration")
        result, error = await self.invoke()
        self.assertTrue(error is not None or result["error"])
        self.assertEqual(self.session.starts, 0)
        self.assert_observer_stopped()

    async def test_display_push_failure_never_starts_capture_and_cleans_everything(self):
        self.gateway.execute.side_effect = RuntimeError("synthetic-phone-rejection")
        result, error = await self.invoke()
        self.assertIsNone(error)
        self.assertTrue(result["error"])
        self.assertEqual(self.session.starts, 0)
        self.assertEqual(self.service.activations, 0)
        self.assertIn("media.stop", self.events)
        self.assertIn("assistant.close", self.events)
        self.assert_observer_stopped()
        self.watchdog.finish.assert_called_once()
        self.display.close.assert_called_once()

    async def real_prestart_failure(self, mutate=None, identity=True, failure='display'):
        self.media.sock = None
        self.media.close = mock.Mock()
        self.media.open = mock.Mock(side_effect=OSError('synthetic bind failure'))
        watchdog = REAL_WATCHDOG(self.phone, ())
        session = REAL_VOICE_SESSION(watchdog.execute, self.media, self.observer.status,
                                    enable_microphone=True, media_reserved=True, arm=watchdog.arm)
        self.gateway.execute.side_effect = RuntimeError('synthetic display rejection')
        if failure == 'observer-not-ready':
            self.observer.ready = mock.Mock(wait=mock.Mock(return_value=False))
        elif failure == 'observer-status':
            self.observer.status.return_value = 'unknown'
        elif failure == 'bind':
            self.gateway.execute.side_effect = None
        if mutate:
            mutate(session, watchdog)
        env = ({'CISCO_VOICE_SESSION_ID': 'a' * 32,
                'CISCO_VOICE_LAUNCH_CHALLENGE': 'b' * 64} if identity else {})
        if isinstance(identity, dict):
            env = identity
        with mock.patch.object(subject.transport, '_Watchdog', return_value=watchdog), \
                mock.patch.object(subject.transport, 'VoiceSession', return_value=session), \
                mock.patch.dict(os.environ, env, clear=True):
            result, error = await self.invoke()
        self.assertIsNone(error)
        self.assertEqual(session.state, 'stopped')
        self.assertFalse(session.stop_confirmed)
        return result

    async def test_real_never_armed_failure_emits_bound_proof_only_after_cleanup(self):
        result = await self.real_prestart_failure()
        self.assertEqual(result['error'], 'voice_session_failed')
        self.assertIsNone(result['result'])
        self.assertEqual(result['cleanup_errors'], [])
        self.assertIs(result['watchdog_armed'], False)
        self.assertEqual(result['cleanup_proof'], {
            'version': 1, 'kind': 'safe_no_media_started', 'session_id': 'a' * 32,
            'challenge': 'b' * 64, 'rtp_start_attempted': False,
            'watchdog_arm_attempted': False, 'watchdog_process_created': False,
            'cleanup_complete': True,
        })
        self.assertIn('assistant.close', self.events)
        self.display.close.assert_called_once()
        self.media.close.assert_called_once()

    async def test_observer_and_prearm_bind_failures_emit_only_never_started_proof(self):
        for failure in ('observer-not-ready', 'observer-status', 'bind'):
            with self.subTest(failure=failure):
                self.observer.ready = threading.Event()
                self.observer.ready.set()
                self.observer.status.return_value = 'idle'
                result = await self.real_prestart_failure(failure=failure)
                self.assertEqual(result['cleanup_proof']['kind'], 'safe_no_media_started')
                self.assertIsNone(result['result'])
                self.assertIs(result['watchdog_armed'], False)
                self.assertEqual(self.media.open.call_count, int(failure == 'bind'))

    async def test_never_started_failure_without_launch_binding_has_no_proof(self):
        valid = {'CISCO_VOICE_SESSION_ID': 'a' * 32, 'CISCO_VOICE_LAUNCH_CHALLENGE': 'b' * 64}
        bindings = [{}, {'CISCO_VOICE_SESSION_ID': 'a' * 32},
                    {'CISCO_VOICE_LAUNCH_CHALLENGE': 'b' * 64},
                    dict(valid, CISCO_VOICE_SESSION_ID='a' * 31),
                    dict(valid, CISCO_VOICE_SESSION_ID='A' * 32),
                    dict(valid, CISCO_VOICE_LAUNCH_CHALLENGE='b' * 63),
                    dict(valid, CISCO_VOICE_LAUNCH_CHALLENGE='not-a-launch-challenge')]
        for binding in bindings:
            with self.subTest(binding=binding):
                result = await self.real_prestart_failure(identity=binding)
                self.assertNotIn('cleanup_proof', result)

    async def test_cleanup_failure_and_attempted_start_do_not_emit_proof(self):
        for field in ('start', 'arm', 'close', 'assistant-close', 'output', 'capture'):
            with self.subTest(field=field):
                def mutate(session, watchdog):
                    if field == 'start':
                        session._start_attempted = True
                    elif field == 'arm':
                        watchdog._arm_attempted = True
                    elif field == 'close':
                        self.media.close.side_effect = OSError('synthetic close failure')
                    elif field == 'assistant-close':
                        self.service.close_error = RuntimeError('synthetic assistant close failure')
                    elif field == 'output':
                        def builder(config, root, sink, **kwargs):
                            sink.frames_sent = 1
                            return self.service
                        self.build_service.side_effect = builder
                    else:
                        self.media.receiver.counters['received'] = 1
                try:
                    result = await self.real_prestart_failure(mutate)
                    self.assertNotIn('cleanup_proof', result)
                    if field == 'output':
                        self.assertEqual(result['output_frames'], 1)
                finally:
                    self.media.close.side_effect = None
                    self.service.close_error = None
                    self.build_service.side_effect = None
                    self.media.receiver.counters.clear()

    async def test_display_update_failure_stops_capture_without_restart(self):
        self.display.update_status.side_effect = RuntimeError("synthetic-display-failure")
        async def activate(phone_id):
            self.service.activations += 1
            callback = self.build_service.call_args.kwargs["on_event"]
            callback(SimpleNamespace(state="listening", reason=None))
            return "synthetic-stream"
        self.service.activate = activate
        result, error = await self.invoke()
        self.assertIsNone(error)
        self.assertEqual(result["result"]["reason"], "explicit_stop")
        self.assertEqual(self.session.starts, 1)
        self.assertEqual(self.service.activations, 1)
        self.assertTrue(self.session.stop_confirmed)
        self.assert_observer_stopped()
        self.watchdog.finish.assert_called_once()
        self.display.close.assert_called_once()

    async def test_wake_display_waits_for_model_before_showing_listening(self):
        self.voice["wake"]["mode"] = "onnx"

        async def listen(phone_id):
            self.assertEqual(phone_id, self.phone["id"])
            callback = self.build_service.call_args.kwargs["on_event"]
            callback(SimpleNamespace(state="armed", reason="model_warmup"))
            callback(SimpleNamespace(state="armed", reason="wake_ready"))
            return "synthetic-stream"

        self.service.listen = listen
        result, error = await self.invoke()
        self.assertIsNone(error)
        self.assertIsNone(result["error"])
        self.assertEqual(self.display.update_status.call_args_list, [
            mock.call("Thinking", "Preparing wake detector. Please wait for Listening."),
            mock.call("Listening", "Wake ready. Say your wake phrase."),
        ])
        self.assertEqual(result["result"]["reason"], "turn_complete")
        self.assertTrue(self.session.stop_confirmed)

    async def test_reply_callback_is_absent_without_explicit_opt_in_and_display(self):
        for enabled, port in ((None, 18085), (False, 18085), (True, None)):
            with self.subTest(enabled=enabled, port=port):
                self.voice.pop("display_reply_text", None)
                if enabled is not None:
                    self.voice["display_reply_text"] = enabled
                self.args.display_port = port
                self.build_service.reset_mock()
                result, error = await self.invoke()
                self.assertIsNone(error)
                self.assertIsNone(result["error"])
                self.assertIsNone(self.build_service.call_args.kwargs.get("on_reply"))

    async def test_reply_callback_rejects_wrong_phone_stream_and_replaced_display(self):
        self.voice["display_reply_text"] = True
        current_ref = self.display.start_status.return_value
        self.display.session_ref = current_ref
        self.service.owns_reply = mock.Mock(
            side_effect=lambda phone, stream: stream == "synthetic-stream")
        async def activate(phone_id):
            callback = self.build_service.call_args.kwargs["on_reply"]
            callback("other-phone", "synthetic-stream", "wrong phone")
            callback(phone_id, "expired-stream", "wrong stream")
            self.display.session_ref = object()
            callback(phone_id, "synthetic-stream", "replaced display")
            self.display.session_ref = current_ref
            callback(phone_id, "synthetic-stream", "Synthetic visible reply")
            self.build_service.call_args.kwargs["on_event"](
                SimpleNamespace(state="stopped", reason="call"))
            callback(phone_id, "synthetic-stream", "late terminal reply")
            return "synthetic-stream"
        self.service.activate = activate
        result, error = await self.invoke()
        self.assertIsNone(error)
        self.assertIsNone(result["error"])
        self.assertEqual(self.display.update_status.call_args_list, [
            mock.call("Speaking", "Synthetic visible reply"),
            mock.call("Stopped", "call"),
        ])
        self.assertNotIn("Synthetic visible reply", json.dumps(result))

    async def test_reply_display_failure_stops_without_exposing_text_in_result(self):
        self.voice["display_reply_text"] = True
        self.display.session_ref = self.display.start_status.return_value
        self.service.owns_reply = mock.Mock(return_value=True)
        self.display.update_status.side_effect = RuntimeError("Synthetic private reply")
        async def activate(phone_id):
            self.build_service.call_args.kwargs["on_reply"](
                phone_id, "synthetic-stream", "Synthetic private reply")
            return "synthetic-stream"
        self.service.activate = activate
        result, error = await self.invoke()
        self.assertIsNone(error)
        self.assertEqual(result["result"]["reason"], "explicit_stop")
        self.assertTrue(self.session.stop_confirmed)
        self.assertNotIn("Synthetic private reply", json.dumps(result))
        self.watchdog.finish.assert_called_once()
        self.display.close.assert_called_once()

    async def test_stop_during_start_does_not_launch_assist(self):
        original = subject.PhoneVoiceController
        controller = []
        def create(*args, **kwargs):
            item = original(*args, **kwargs)
            controller.append(item)
            return item
        self.stack.enter_context(mock.patch.object(subject, "PhoneVoiceController", side_effect=create))
        acknowledged = threading.Event()
        def start_hook():
            def request():
                controller[0].request_stop("display_stop")
                acknowledged.set()
            self.loop.call_soon_threadsafe(request)
            if not acknowledged.wait(1):
                raise AssertionError("event loop did not handle synthetic Stop")
        self.session.start_hook = start_hook
        result, error = await self.invoke()
        self.assertIsNone(error)
        self.assertEqual(result["result"]["reason"], "display_stop")
        self.assertEqual(self.service.activations, 0)
        self.assertEqual(self.session.starts, 1)
        self.assertTrue(self.session.stop_confirmed)
        self.assert_observer_stopped()
        self.watchdog.finish.assert_called_once()

    async def test_partial_media_start_failure_stops_before_provider_cleanup(self):
        def fail_after_start():
            raise RuntimeError("synthetic-second-direction-failure")
        self.session.start_hook = fail_after_start
        result, error = await self.invoke()
        self.assertIsNone(error)
        self.assertTrue(result["error"])
        self.assertEqual(self.service.activations, 0)
        self.assertEqual(self.session.state, "stopped")
        self.assertLess(self.events.index("media.stop"), self.events.index("assistant.stop"))
        self.assert_observer_stopped()
        self.watchdog.finish.assert_called_once()
        self.display.close.assert_called_once()

    async def test_provider_close_exception_cannot_skip_independent_cleanup(self):
        self.service.close_error = RuntimeError("synthetic-provider-close-failure")
        result, error = await self.invoke()
        self.assertIsNone(error)
        self.assertEqual(result["cleanup_errors"], ["assistant_cleanup_failed"])
        self.assertTrue(self.session.stop_confirmed)
        self.assert_observer_stopped()
        self.watchdog.finish.assert_called_once()
        self.display.close.assert_called_once()
        self.assertEqual(self.remove_signal.call_count, self.add_signal.call_count)

    async def test_cancellation_during_service_close_drains_ordered_cleanup(self):
        progress_path = self.enable_progress()
        entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def close():
            entered.set()
            await release.wait()
            completed.set()

        self.service.close = close
        finish = self.watchdog.finish.return_value

        def finish_after_service():
            self.assertTrue(completed.is_set())
            return finish

        self.watchdog.finish.side_effect = finish_after_service
        task = asyncio.create_task(subject.run_session(
            self.args, self.config, {}, self.phone, self.voice))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertFalse(completed.is_set())
                self.watchdog.finish.assert_not_called()
                self.display.close.assert_not_called()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            self.assertTrue(completed.is_set())
            self.assertTrue(self.session.stop_confirmed)
            self.assert_observer_stopped()
            self.watchdog.finish.assert_called_once()
            self.display.close.assert_called_once()
            self.assertEqual(self.remove_signal.call_count, self.add_signal.call_count)
            self.assertEqual(json.loads(progress_path.read_text())['phase'], 'stopped')
            self.assertEqual(json.loads(progress_path.read_text())['reason'], 'shutdown')
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_cancellation_during_watchdog_finish_drains_worker(self):
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        finish = self.watchdog.finish.return_value

        def blocked_finish():
            entered.set()
            if not release.wait(2):
                raise AssertionError("test did not release watchdog worker")
            completed.set()
            return finish

        self.watchdog.finish.side_effect = blocked_finish
        task = asyncio.create_task(subject.run_session(
            self.args, self.config, {}, self.phone, self.voice))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertFalse(completed.is_set())
                self.display.close.assert_not_called()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            self.assertTrue(completed.is_set())
            self.watchdog.finish.assert_called_once()
            self.display.close.assert_called_once()
            self.assertEqual(self.remove_signal.call_count, self.add_signal.call_count)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_service_close_cancellation_cannot_skip_independent_cleanup(self):
        self.service.close_error = asyncio.CancelledError("synthetic-provider-cancellation")
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(subject.run_session(
                self.args, self.config, {}, self.phone, self.voice), 2)
        self.assertTrue(self.session.stop_confirmed)
        self.assert_observer_stopped()
        self.watchdog.finish.assert_called_once()
        self.display.close.assert_called_once()
        self.assertEqual(self.remove_signal.call_count, self.add_signal.call_count)

    async def test_transport_stop_exception_still_runs_supervisor_and_closes_display(self):
        self.session.stop = mock.Mock(side_effect=RuntimeError("synthetic-stop-failure"))
        result, error = await self.invoke()
        self.assertIsNone(error)
        self.assertIn("transport_cleanup_failed", result["cleanup_errors"])
        self.assertIn("assistant.close", self.events)
        self.assert_observer_stopped()
        self.watchdog.finish.assert_called_once()
        self.display.close.assert_called_once()
        self.assertEqual(self.remove_signal.call_count, self.add_signal.call_count)

    async def test_supervisor_failure_still_closes_display_and_removes_handlers(self):
        self.watchdog.finish.side_effect = RuntimeError("synthetic-supervisor-failure")
        result, error = await self.invoke()
        self.assertIsNone(error)
        self.assertEqual(result["cleanup_errors"], ["supervisor_cleanup_failed"])
        self.assertTrue(self.session.stop_confirmed)
        self.assert_observer_stopped()
        self.assertIn("assistant.close", self.events)
        self.display.close.assert_called_once()
        self.assertEqual(self.remove_signal.call_count, self.add_signal.call_count)

    async def test_display_close_failure_still_removes_handlers(self):
        self.display.close.side_effect = RuntimeError("synthetic-display-close-failure")
        result, error = await self.invoke()
        self.assertIsNone(error)
        self.assertEqual(result["cleanup_errors"], ["display_cleanup_failed"])
        self.assertTrue(self.session.stop_confirmed)
        self.assert_observer_stopped()
        self.watchdog.finish.assert_called_once()
        self.assertEqual(self.remove_signal.call_count, self.add_signal.call_count)


@unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is required for real display rendering")
class ReplyDisplayLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Real loopback XML GETs, synthetic Assist and no phone/media network."""

    asyncSetUp = SessionLifecycleTests.asyncSetUp
    assert_observer_stopped = SessionLifecycleTests.assert_observer_stopped

    @staticmethod
    def get_page(url):
        parsed = urlsplit(url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=1)
        try:
            connection.request("GET", parsed.path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    async def check_reply_lifecycle(self, *, preempt):
        self.voice["display_reply_text"] = True
        self.config.update(duration_seconds=3, receive_idle_seconds=3)
        self.config["bind_ipv4"] = "127.0.0.1"
        self.phone["ipv4"] = "127.0.0.1"
        answer = 'Synthetic private answer: A < B & "C".'
        started, release = asyncio.Event(), asyncio.Event()
        servers, terminal_pages, services, voice_events = [], [], [], []

        class Assist:
            async def run(self, audio, *, conversation_id=None, on_event=None):
                await on_event({"type": "stt-end"})
                return SimpleNamespace(conversation_id="synthetic-conversation",
                                       text=answer, tts_url=None)

        class Playback:
            async def play(self, phone, result):
                started.set()
                await release.wait()
            async def stop(self, phone):
                pass

        def builder(config, workspace, sink, **kwargs):
            self.assertTrue(callable(kwargs.get("on_reply")))
            on_event = kwargs["on_event"]
            def record(event):
                voice_events.append(event)
                on_event(event)
            service = AssistantService(
                ServiceSettings(enabled=True, duplicate_window_seconds=0),
                {self.phone["id"]: True}, lambda _: NeverWake(), Assist(), Playback(),
                on_event=record, on_reply=kwargs["on_reply"])
            services.append(service)
            return service
        self.build_service.side_effect = builder

        def display_factory(bind_ip, port, phone_id, phone_ip, **kwargs):
            server = REAL_DISPLAY_SERVER(bind_ip, 0, phone_id, phone_ip, **kwargs)
            original_close = server.close
            def close():
                try:
                    if server.session_ref is not None:
                        terminal_pages.append(self.get_page(server.session_ref.page_url + "/text"))
                finally:
                    original_close()
            server.close = close
            servers.append(server)
            self.addCleanup(original_close)
            return server
        self.stack.enter_context(mock.patch.object(
            phone_voice_display, "VoiceDisplayServer", side_effect=display_factory))
        task = asyncio.create_task(subject.run_session(
            self.args, self.config, {}, self.phone, self.voice))
        try:
            await asyncio.wait_for(started.wait(), 2)
            server = servers[0]
            status, headers, body = await asyncio.to_thread(
                self.get_page, server.session_ref.page_url + "/text")
            self.assertEqual(status, 200)
            self.assertIn("no-store", headers["Cache-Control"])
            root = ET.fromstring(body)
            self.assertEqual(root.tag, "CiscoIPPhoneText")
            self.assertIn(answer, "".join(root.itertext()))
            self.assertIn(b"&lt;", body)
            self.assertIn(b"&amp;", body)
            self.assertEqual(services[0].status(self.phone["id"]).state, "speaking")
            # The existing Details soft key, rather than a new push, exposes
            # the reply while normal playback keeps the session alive.
            status, _, body = await asyncio.to_thread(
                self.get_page, server.session_ref.page_url)
            self.assertEqual(status, 200)
            self.assertIn(server.session_ref.page_url + "/text",
                          "".join(ET.fromstring(body).itertext()))
            self.gateway.execute.assert_called_once()
            if preempt:
                await services[0].preempt(self.phone["id"], "call")
            else:
                release.set()
            result = await asyncio.wait_for(task, 2)
            self.assertIsNone(result["error"])
            self.assertTrue(self.session.stop_confirmed)
            self.assertEqual(len(terminal_pages), 1)
            status, headers, body = terminal_pages[0]
            self.assertEqual(status, 200)
            self.assertNotIn(answer.encode(), body)
            self.assertIn("Stopped", "".join(ET.fromstring(body).itertext()))
            self.assertNotIn("Refresh", headers)
            self.assertEqual(server.display.stats(), {
                "sessions": 0, "frames": 0, "frame_bytes": 0})
            self.assertNotIn(answer, json.dumps(result))
            self.assertNotIn(answer, repr(voice_events))
            self.assert_observer_stopped()
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_details_show_reply_during_playback_and_clear_on_completion(self):
        await self.check_reply_lifecycle(preempt=False)

    async def test_details_clear_reply_on_preemption_without_extra_push(self):
        await self.check_reply_lifecycle(preempt=True)


class SessionCLIExitTests(unittest.TestCase):
    """The CLI must not call an incomplete supervisor cleanup a successful turn."""

    def completed_result(self):
        return {
            "error": None,
            "cleanup_errors": [],
            "result": {"reason": "turn_complete", "stop_confirmed": True},
            "watchdog_armed": True,
            "watchdog_finished": True,
            "watchdog_stop_errors": 0,
            "watchdog_stop_confirmed": False,
        }

    def invoke(self, result, extra_args=()):
        output = io.StringIO()
        with mock.patch.object(subject.transport, "load_pilot_config", return_value=(
                {}, {}, {"id": "synthetic-phone"})), \
             mock.patch.object(subject, "load_config", return_value={
                 "phones": {"synthetic-phone": True}}), \
             mock.patch.object(subject, "run_session", new_callable=mock.AsyncMock,
                               return_value=result) as run, \
             mock.patch("sys.stdout", output):
            exit_code = subject.main([
                "--transport-config", "synthetic-transport.toml",
                "--voice-config", "synthetic-voice.toml",
                "--run", "--attended", "--exclusive-media", "--enable-microphone",
                *extra_args,
            ])
        run.assert_awaited_once()
        if extra_args:
            self.assertEqual(run.call_args.args[0].status_file, '.local/run/progress.json')
        self.assertEqual(json.loads(output.getvalue()), result)
        return exit_code

    def test_status_file_flag_preserves_one_final_stdout_object(self):
        result = self.completed_result()
        result['turns_completed'] = 2
        self.assertEqual(self.invoke(result, ('--status-file', '.local/run/progress.json')), 0)

    def test_completed_turn_succeeds_with_either_stop_confirmation(self):
        for confirmed_by in ("main", "supervisor"):
            with self.subTest(confirmed_by=confirmed_by):
                result = self.completed_result()
                result["result"]["stop_confirmed"] = confirmed_by == "main"
                result["watchdog_stop_confirmed"] = confirmed_by == "supervisor"
                self.assertEqual(self.invoke(result), 0)

    def test_safe_never_started_cleanup_still_exits_nonzero(self):
        result = {'error': 'voice_session_failed', 'result': None, 'cleanup_errors': [],
                  'watchdog_armed': False, 'turns_completed': 0,
                  'cleanup_proof': {'kind': 'safe_no_media_started'}}
        self.assertEqual(self.invoke(result), 1)

    def test_clean_explicit_stop_or_lease_expiry_is_not_a_runtime_failure(self):
        for reason in ('explicit_stop', 'display_stop', 'shutdown', 'lease_expired'):
            with self.subTest(reason=reason):
                result = self.completed_result()
                result['result']['reason'] = reason
                result['turns_completed'] = 0
                self.assertEqual(self.invoke(result), 0)

    def test_completed_turn_requires_armed_watchdog(self):
        for present in (True, False):
            with self.subTest(field_present=present):
                result = self.completed_result()
                if present:
                    result["watchdog_armed"] = False
                else:
                    del result["watchdog_armed"]
                self.assertEqual(self.invoke(result), 1)

    def test_completed_turn_requires_finished_watchdog(self):
        for present in (True, False):
            with self.subTest(field_present=present):
                result = self.completed_result()
                if present:
                    result["watchdog_finished"] = False
                else:
                    del result["watchdog_finished"]
                self.assertEqual(self.invoke(result), 1)

    def test_completed_turn_rejects_watchdog_stop_errors(self):
        for errors in (None, 1, 2):
            with self.subTest(stop_errors=errors):
                result = self.completed_result()
                if errors is None:
                    del result["watchdog_stop_errors"]
                else:
                    result["watchdog_stop_errors"] = errors
                self.assertEqual(self.invoke(result), 1)

    def test_completed_turn_rejects_cleanup_errors(self):
        result = self.completed_result()
        result["cleanup_errors"] = ["assistant_cleanup_failed"]
        self.assertEqual(self.invoke(result), 1)

    def test_completed_turn_requires_stop_confirmation(self):
        result = self.completed_result()
        result["result"]["stop_confirmed"] = False
        self.assertEqual(self.invoke(result), 1)

    def test_clean_watchdog_cannot_turn_unsuccessful_session_into_success(self):
        for failure in ("runtime_error", "incomplete_turn", "no_result"):
            with self.subTest(failure=failure):
                result = self.completed_result()
                if failure == "runtime_error":
                    result["error"] = "voice_session_failed"
                elif failure == "incomplete_turn":
                    result["result"]["reason"] = "transport_stopped"
                else:
                    result["result"] = None
                self.assertEqual(self.invoke(result), 1)


class ProgressFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.directory = self.root / '.local' / 'run'
        self.directory.mkdir(parents=True, mode=0o700)
        self.path = self.directory / 'progress.json'

    def test_atomic_private_json_contains_only_fixed_progress_fields(self):
        with mock.patch.object(subject.time, 'time', return_value=1700000000.25):
            progress = subject.SessionProgress(self.root, self.path)
            with self.path.open() as previous:
                before = os.fstat(previous.fileno()).st_ino
                progress.publish('speaking', 'private transcript or token')
                progress.playback_update(completed=True, completed_pcm_bytes=640)
                self.assertNotEqual(self.path.stat().st_ino, before)
                self.assertEqual(json.load(previous)['phase'], 'preparing')
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(self.path.stat().st_uid, os.getuid())
        self.assertEqual(json.loads(self.path.read_text()), {
            'schema_version': 1, 'phase': 'speaking', 'reason': None,
            'turns_completed': 1, 'updated_at': 1700000000.25,
            'playback': {'playback_requests': 0, 'tts_offers': 0, 'empty_playbacks': 0,
                         'incomplete_playbacks': 0, 'completed_pcm_bytes': 640}})
        self.assertEqual(list(self.directory.iterdir()), [self.path])

    def test_bad_paths_do_not_create_or_follow_any_output(self):
        outside = self.root / 'outside.json'
        outside.write_text('unchanged')
        linked_directory = self.root / '.local' / 'linked'
        linked_directory.symlink_to(self.directory, target_is_directory=True)
        for path in (outside, self.directory / '..' / 'escape.json',
                     self.root / '.local' / 'bare.json', linked_directory / 'progress.json'):
            with self.subTest(path=path), self.assertRaises((OSError, ValueError)):
                subject.SessionProgress(self.root, path)
        self.assertEqual(outside.read_text(), 'unchanged')
        self.assertFalse(self.path.exists())
        self.path.symlink_to(outside)
        with self.assertRaises(ValueError):
            subject.SessionProgress(self.root, self.path)
        self.assertEqual(outside.read_text(), 'unchanged')

    def test_shared_directory_and_nonprivate_or_nonregular_file_are_refused(self):
        self.directory.chmod(0o755)
        with self.assertRaises(ValueError):
            subject.SessionProgress(self.root, self.path)
        self.directory.chmod(0o700)
        self.path.write_text('{}')
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            subject.SessionProgress(self.root, self.path)
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        with self.assertRaises(ValueError):
            subject.SessionProgress(self.root, self.path)

    def test_hard_link_is_refused_and_failed_replace_retains_old_snapshot(self):
        progress = subject.SessionProgress(self.root, self.path)
        other = self.directory / 'other.json'
        os.link(self.path, other)
        with self.assertRaises(ValueError):
            progress.publish('listening')
        other.unlink()
        original = self.path.read_bytes()
        with mock.patch.object(subject.os, 'replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                progress.publish('listening')
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.directory.iterdir()), [self.path])


class ProgressPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_counts_only_completed_playback_and_preserves_stop(self):
        progress = subject.SessionProgress(ROOT)
        outcome = subject.PlaybackResult(1, 640)
        original = SimpleNamespace(play=mock.AsyncMock(return_value=outcome), stop=mock.AsyncMock())
        playback = subject.ProgressPlayback(original, progress)
        self.assertEqual(await playback.play('phone', object()), outcome)
        self.assertEqual(progress.turns_completed, 1)
        for error in (RuntimeError('synthetic provider text'), asyncio.CancelledError()):
            original.play.side_effect = error
            with self.assertRaises(type(error)):
                await playback.play('phone', object())
            self.assertEqual(progress.turns_completed, 1)
        original.play.side_effect = None
        await playback.play('phone', object())
        self.assertEqual(progress.turns_completed, 2)
        await playback.stop('phone')
        original.stop.assert_awaited_once_with('phone')


if __name__ == "__main__":
    unittest.main()
