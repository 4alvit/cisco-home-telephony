"""Failure-path integration checks; all phone, network and process I/O is fake."""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import ExitStack
import importlib.util
import io
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "voice_session_lifecycle_subject", ROOT / "scripts/voice_session.py")
subject = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = subject
SPEC.loader.exec_module(subject)
import phone_voice_display


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
            mock.call("Listening", "Say your wake phrase."),
        ])
        self.assertEqual(result["result"]["reason"], "turn_complete")
        self.assertTrue(self.session.stop_confirmed)

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

    def invoke(self, result):
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
            ])
        run.assert_awaited_once()
        self.assertEqual(json.loads(output.getvalue()), result)
        return exit_code

    def test_completed_turn_succeeds_with_either_stop_confirmation(self):
        for confirmed_by in ("main", "supervisor"):
            with self.subTest(confirmed_by=confirmed_by):
                result = self.completed_result()
                result["result"]["stop_confirmed"] = confirmed_by == "main"
                result["watchdog_stop_confirmed"] = confirmed_by == "supervisor"
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
                    result["result"]["reason"] = "display_stop"
                else:
                    result["result"] = None
                self.assertEqual(self.invoke(result), 1)


if __name__ == "__main__":
    unittest.main()
