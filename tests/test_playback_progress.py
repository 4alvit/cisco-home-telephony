"""Offline playback-to-progress checks; no phone, provider or process is used."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import io
import json
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import wave


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "containers/asterisk"))
import cisco
import phone_voice_control as control
import voice_session as runner
from phone_voice_controller import RtpPCMSink
from voice_assistant.playback import PlaybackResult, WavePlayback


PRIVATE = "synthetic-private-reply-and-token"
URL = "https://speech.example.invalid/" + PRIVATE


def wav(pcm):
    data = io.BytesIO()
    with wave.open(data, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(pcm)
    return SimpleNamespace(data=data.getvalue(), content_type="audio/wav")


def reply(url=URL):
    return SimpleNamespace(tts_url=url, text=PRIVATE, transcript=PRIVATE)


class PlaybackProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="playback-progress-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        (self.workspace / ".local").mkdir(mode=0o700)
        self.directory = self.workspace / ".local" / "session"
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / "progress.json"
        self.progress = runner.SessionProgress(self.workspace, self.path)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        # Host scheduling delays are not application stderr or playback failures.
        # Keep asyncio debug and leak checks; disable only its timing diagnostic.
        self.stack.enter_context(mock.patch.object(
            asyncio.get_running_loop(), "slow_callback_duration", float("inf")))
        # IsolatedAsyncioTestCase has already created its loop/self-pipe.
        for target, name in ((socket, "socket"), (socket, "create_connection"),
                             (subprocess, "Popen")):
            self.stack.enter_context(mock.patch.object(
                target, name, side_effect=AssertionError("external I/O forbidden")))
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.stdout))
        self.stack.enter_context(contextlib.redirect_stderr(self.stderr))

    async def asyncTearDown(self):
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertEqual(self.stderr.getvalue(), "")
        raw = self.path.read_bytes()
        self.assertLess(len(raw), 2048)
        self.assertNotIn(PRIVATE.encode(), raw)
        self.assertNotIn(b"speech.example.invalid", raw)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def playback(self, pcm, *, sink=None):
        sink = sink or SimpleNamespace(write_pcm=mock.AsyncMock(),
                                       drain=mock.AsyncMock(), stop=mock.AsyncMock())
        fetch = mock.AsyncMock(return_value=wav(pcm))
        player = runner.ProgressPlayback(WavePlayback(fetch, sink), self.progress)
        return player, fetch, sink

    def manager_view(self):
        # Exercise the production progress reader/view without constructing a
        # supervisor, launching a child, claiming media or reading configuration.
        manager = object.__new__(control.VoiceControl)
        manager.clock = lambda: 1.0
        session = SimpleNamespace(
            directory=self.directory, state="running", phase="preparing",
            phase_reason=None, turns_completed=0, playback=None,
            profile=SimpleNamespace(phone_id="201", mode="wake", duration=30),
            started=0.0, line="201", session_id="a" * 32,
            stop_requested=False, stop_confirmed=False, error=None)
        return manager, session, manager._view(session)

    async def test_real_missing_and_empty_tts_never_complete_a_turn(self):
        player, fetch, sink = self.playback(b"")
        self.assertEqual(await player.play("201", reply(None)), PlaybackResult(0, 0))
        fetch.assert_not_awaited()
        self.assertEqual(await player.play("201", reply()), PlaybackResult(0, 0))
        fetch.assert_awaited_once_with(URL)
        sink.write_pcm.assert_not_awaited()
        sink.drain.assert_not_awaited()
        self.assertEqual(self.progress.turns_completed, 0)
        self.assertEqual(self.progress.playback, {
            "playback_requests": 2, "tts_offers": 1, "empty_playbacks": 2,
            "incomplete_playbacks": 0, "completed_pcm_bytes": 0})
        _, _, value = self.manager_view()
        self.assertEqual(cisco.voice_status(control._safe_status(value))["playback"],
                         self.progress.playback)

    async def test_two_real_replies_count_only_after_drain_and_reach_cli(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def drain(_phone):
            started.set()
            await release.wait()

        sink = SimpleNamespace(write_pcm=mock.AsyncMock(),
                               drain=mock.AsyncMock(side_effect=drain), stop=mock.AsyncMock())
        pcm = b"\x01\x00" * 325  # One full block and one short tail.
        player, _, _ = self.playback(pcm, sink=sink)
        task = asyncio.create_task(player.play("201", reply()))
        try:
            await asyncio.wait_for(started.wait(), 1)
            self.assertEqual(self.progress.turns_completed, 0)
            self.assertEqual(self.progress.playback["completed_pcm_bytes"], 0)
            release.set()
            self.assertEqual(await task, PlaybackResult(2, len(pcm)))
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(await player.play("201", reply()), PlaybackResult(2, len(pcm)))
        self.assertEqual(sink.write_pcm.await_count, 4)
        self.assertEqual(sink.drain.await_count, 2)
        self.assertEqual(self.progress.turns_completed, 2)
        self.assertEqual(self.progress.playback, {
            "playback_requests": 2, "tts_offers": 2, "empty_playbacks": 0,
            "incomplete_playbacks": 0, "completed_pcm_bytes": 2 * len(pcm)})
        _, _, value = self.manager_view()
        public = cisco.voice_status(control._safe_status(value))
        self.assertEqual(public["turns_completed"], 2)
        self.assertEqual(public["playback"], self.progress.playback)
        await player.stop("201")
        sink.stop.assert_awaited_once_with("201")

    async def test_partial_write_failure_and_cancellation_remain_incomplete(self):
        for error in (RuntimeError(PRIVATE), asyncio.CancelledError(PRIVATE)):
            with self.subTest(kind=type(error).__name__):
                sink = SimpleNamespace(
                    write_pcm=mock.AsyncMock(side_effect=[None, error]),
                    drain=mock.AsyncMock(), stop=mock.AsyncMock())
                player, _, _ = self.playback(b"\x01\x00" * 325, sink=sink)
                with self.assertRaises(type(error)) as raised:
                    await player.play("201", reply())
                self.assertIs(raised.exception, error)
                self.assertEqual(sink.write_pcm.await_count, 2)
                sink.drain.assert_not_awaited()
                self.assertEqual(self.progress.turns_completed, 0)
                self.assertEqual(self.progress.playback["completed_pcm_bytes"], 0)
        self.assertEqual(self.progress.playback["incomplete_playbacks"], 2)
        self.assertEqual(self.progress.playback["playback_requests"], 2)
        self.assertEqual(self.progress.playback["empty_playbacks"], 0)

    async def test_drain_failure_preserves_only_prior_completed_bytes(self):
        pcm = b"\x01\x00" * 325
        player, _, sink = self.playback(pcm)
        await player.play("201", reply())
        sink.drain.side_effect = RuntimeError(PRIVATE)
        with self.assertRaises(RuntimeError):
            await player.play("201", reply())
        self.assertEqual(sink.write_pcm.await_count, 4)
        self.assertEqual(self.progress.turns_completed, 1)
        self.assertEqual(self.progress.playback["completed_pcm_bytes"], len(pcm))
        self.assertEqual(self.progress.playback["incomplete_playbacks"], 1)
        self.assertEqual(self.progress.playback["empty_playbacks"], 0)

    async def assert_real_sink_cancellation(self, sent_before_stop):
        queued, release, returned = (threading.Event() for _ in range(3))

        class Transport:
            state = "active"

            def __init__(self):
                self.lock = threading.Lock()
                self.calls = self.sent = 0

            def play(self, frame, *, allowed):
                if len(frame) != 320:
                    raise AssertionError("unexpected synthetic PCM frame size")
                with self.lock:
                    self.calls += 1
                    blocked = self.calls == sent_before_stop + 1
                try:
                    if blocked:
                        queued.set()
                        if not release.wait(2):
                            raise AssertionError("synthetic transport barrier timed out")
                    with self.lock:
                        if not allowed():
                            return False
                        self.sent += 1
                        return True
                finally:
                    if blocked:
                        returned.set()

        transport = Transport()
        sink = RtpPCMSink(transport, "201")
        player, _, _ = self.playback(b"\x01\x00" * 320 * (sent_before_stop + 1), sink=sink)
        task = asyncio.create_task(player.play("201", reply()))
        try:
            self.assertTrue(await asyncio.to_thread(queued.wait, 1))
            self.assertEqual(sink.frames_sent, sent_before_stop)
            self.assertEqual(transport.sent, sent_before_stop)
            await player.stop("201")
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            self.assertTrue(await asyncio.to_thread(returned.wait, 1))
            self.assertEqual(transport.calls, sent_before_stop + 1)
            self.assertEqual(transport.sent, sent_before_stop)
            self.assertEqual(sink.frames_sent, sent_before_stop)
            self.assertEqual(self.progress.turns_completed, 0)
            self.assertEqual(self.progress.playback, {
                "playback_requests": 1, "tts_offers": 1, "empty_playbacks": 0,
                "incomplete_playbacks": 1, "completed_pcm_bytes": 0})
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if queued.is_set():
                self.assertTrue(await asyncio.to_thread(returned.wait, 2))

    async def test_real_rtp_sink_queued_cancellation_does_not_complete(self):
        await self.assert_real_sink_cancellation(0)

    async def test_real_rtp_sink_partial_cancellation_preserves_actual_send_count(self):
        await self.assert_real_sink_cancellation(1)

    async def test_stop_during_real_wave_fetch_cancels_reply_but_allows_next_play(self):
        entered, release = asyncio.Event(), asyncio.Event()
        pcm = b"\x01\x00" * 320

        async def fetch(_url):
            entered.set()
            await release.wait()
            return wav(pcm)

        transport = SimpleNamespace(state="active", play=mock.Mock(
            side_effect=lambda _frame, *, allowed: allowed()))
        sink = RtpPCMSink(transport, "201")
        player = runner.ProgressPlayback(WavePlayback(fetch, sink), self.progress)
        task = asyncio.create_task(player.play("201", reply()))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await player.stop("201")
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            transport.play.assert_not_called()
            self.assertEqual(sink.frames_sent, 0)
            self.assertEqual(self.progress.turns_completed, 0)
            self.assertEqual(self.progress.playback, {
                "playback_requests": 1, "tts_offers": 1, "empty_playbacks": 0,
                "incomplete_playbacks": 1, "completed_pcm_bytes": 0})
            self.assertEqual(await player.play("201", reply()), PlaybackResult(1, len(pcm)))
            transport.play.assert_called_once()
            self.assertEqual(sink.frames_sent, 1)
            self.assertEqual(self.progress.turns_completed, 1)
            self.assertEqual(self.progress.playback, {
                "playback_requests": 2, "tts_offers": 2, "empty_playbacks": 0,
                "incomplete_playbacks": 1, "completed_pcm_bytes": len(pcm)})
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_stop_after_rtp_send_before_await_resumes_cancels_remaining_reply(self):
        sent, release, returned = (threading.Event() for _ in range(3))

        class Transport:
            state = "active"

            def __init__(self):
                self.lock = threading.Lock()
                self.frames_sent = 0

            def play(self, _frame, *, allowed):
                with self.lock:
                    if not allowed():
                        return False
                    self.frames_sent += 1
                    first = self.frames_sent == 1
                if first:
                    sent.set()
                    try:
                        if not release.wait(2):
                            raise AssertionError("synthetic sent-frame barrier timed out")
                    finally:
                        returned.set()
                return True  # The frame was sent before Stop, so preserve True.

        transport = Transport()
        sink = RtpPCMSink(transport, "201")
        pcm = b"\x01\x00" * 640  # Two blocks; Stop must prevent the second.
        player, fetch, _ = self.playback(pcm, sink=sink)
        task = asyncio.create_task(player.play("201", reply()))
        try:
            self.assertTrue(await asyncio.to_thread(sent.wait, 1))
            self.assertEqual(transport.frames_sent, 1)
            self.assertEqual(sink.frames_sent, 0)  # The await has not resumed yet.
            await player.stop("201")
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            self.assertEqual(transport.frames_sent, 1)
            self.assertEqual(sink.frames_sent, 1)
            self.assertEqual(self.progress.turns_completed, 0)
            self.assertEqual(self.progress.playback, {
                "playback_requests": 1, "tts_offers": 1, "empty_playbacks": 0,
                "incomplete_playbacks": 1, "completed_pcm_bytes": 0})
            fetch.return_value = wav(pcm[:640])
            self.assertEqual(await player.play("201", reply()), PlaybackResult(1, 640))
            self.assertEqual(transport.frames_sent, 2)
            self.assertEqual(sink.frames_sent, 2)
            self.assertEqual(self.progress.turns_completed, 1)
            self.assertEqual(self.progress.playback, {
                "playback_requests": 2, "tts_offers": 2, "empty_playbacks": 0,
                "incomplete_playbacks": 1, "completed_pcm_bytes": 640})
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if sent.is_set():
                self.assertTrue(await asyncio.to_thread(returned.wait, 2))

    async def test_legacy_and_invalid_results_do_not_certify_output(self):
        values = (None, "legacy", PlaybackResult(0, 2), PlaybackResult(1, 3),
                  PlaybackResult(True, 2), PlaybackResult(1, 64 * 1024 * 1024 + 2))
        for value in values:
            with self.subTest(kind=type(value).__name__):
                inner = SimpleNamespace(play=mock.AsyncMock(return_value=value))
                player = runner.ProgressPlayback(inner, self.progress)
                self.assertIs(await player.play("201", reply()), value)
                self.assertEqual(self.progress.turns_completed, 0)
                self.assertEqual(self.progress.playback["completed_pcm_bytes"], 0)
        self.assertEqual(self.progress.playback["incomplete_playbacks"], len(values))
        self.assertEqual(self.progress.playback["empty_playbacks"], 0)

    async def test_counter_limits_refuse_without_mutating_or_starting_playback(self):
        self.progress.playback["playback_requests"] = 10000
        before = copy.deepcopy(self.progress.playback)
        inner = SimpleNamespace(play=mock.AsyncMock(return_value=PlaybackResult(1, 2)))
        with self.assertRaises(ValueError):
            await runner.ProgressPlayback(inner, self.progress).play("201", reply())
        inner.play.assert_not_awaited()
        self.assertEqual(self.progress.playback, before)
        self.progress.playback["completed_pcm_bytes"] = 64 * 1024 * 1024
        before = copy.deepcopy(self.progress.playback)
        with self.assertRaises(ValueError):
            self.progress.playback_update(completed=True, completed_pcm_bytes=2)
        self.assertEqual(self.progress.playback, before)
        self.assertEqual(self.progress.turns_completed, 0)
        for bad in (True, -1, 1.0):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                self.progress.playback_update(empty_playbacks=bad)
        self.assertEqual(self.progress.playback, before)

    async def test_legacy_progress_and_status_without_playback_remain_valid(self):
        legacy = json.loads(self.path.read_text())
        del legacy["playback"]
        legacy["turns_completed"] = 3
        self.path.write_text(json.dumps(legacy))
        _, _, value = self.manager_view()
        public = cisco.voice_status(control._safe_status(value))
        self.assertEqual(public["turns_completed"], 3)
        self.assertNotIn("playback", public)

    async def test_invalid_metadata_cannot_replace_last_good_progress_or_leak(self):
        player, _, _ = self.playback(b"\x01\x00" * 10)
        await player.play("201", reply())
        manager, session, value = self.manager_view()
        good = copy.deepcopy(value["playback"])
        bad_values = (dict(good, token=PRIVATE), dict(good, completed_pcm_bytes=10**400),
                      dict(good, playback_requests=True))
        for bad in bad_values:
            with self.subTest(kind=list(bad)[-1]):
                changed = dict(value, playback=bad)
                with self.assertRaises(control.ControlError):
                    control._safe_status(changed)
                with self.assertRaises(cisco.Refused):
                    cisco.voice_status(changed)
                payload = json.loads(self.path.read_text())
                payload.update(playback=bad, turns_completed=9)
                self.path.write_text(json.dumps(payload))
                manager._progress(session)
                self.assertEqual(session.playback, good)
                self.assertEqual(session.turns_completed, 1)
        # Restore the valid private snapshot for the shared size/privacy check.
        self.progress.publish(self.progress.phase, self.progress.reason)


if __name__ == "__main__":
    unittest.main()
