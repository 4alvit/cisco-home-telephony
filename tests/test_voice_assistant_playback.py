"""Offline playback outcome contracts; no HA, network, or audio devices."""

import asyncio
from contextlib import ExitStack
from dataclasses import FrozenInstanceError
import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import wave

from voice_assistant.ha import AudioPayload
from voice_assistant.playback import PCMOutput, PlaybackResult, WavePlayback


def make_wave(pcm, rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(pcm)
    return buffer.getvalue()


class Sink:
    def __init__(self):
        self.writes = []

    async def write_pcm(self, phone, data, rate):
        self.writes.append((phone, data, rate))


class PlaybackOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # The test loop already exists, so its internal self-pipe is unaffected.
        guards = ExitStack()
        self.addCleanup(guards.close)
        for target in ("socket.socket", "socket.create_connection", "subprocess.Popen"):
            guards.enter_context(patch(target, side_effect=AssertionError("external I/O forbidden")))
        self.reply = SimpleNamespace(tts_url="/api/tts_proxy/synthetic.wav")

    def playback(self, sink, pcm, rate=16000):
        return WavePlayback(AsyncMock(return_value=AudioPayload(make_wave(pcm, rate), "audio/wav")), sink)

    async def test_missing_url_returns_immutable_zero_without_fetch_or_sink_calls(self):
        fetch = AsyncMock(side_effect=AssertionError("fetch forbidden"))
        sink = Sink()
        sink.drain = AsyncMock(side_effect=AssertionError("drain forbidden"))
        outcome = await WavePlayback(fetch, sink).play("synthetic", SimpleNamespace(tts_url=None))
        self.assertEqual(outcome, PlaybackResult(0, 0))
        with self.assertRaises(FrozenInstanceError):
            outcome.frames = 1
        fetch.assert_not_awaited()
        sink.drain.assert_not_awaited()
        self.assertEqual(sink.writes, [])

    async def test_empty_decoded_pcm_returns_zero_without_writes_or_drain(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                sink = Sink()
                sink.drain = AsyncMock(side_effect=AssertionError("drain forbidden"))
                if compressed:
                    decoder = AsyncMock(return_value=PCMOutput(b"", 16000))
                    fetch = AsyncMock(return_value=AudioPayload(b"synthetic-compressed", "audio/mpeg"))
                    player = WavePlayback(fetch, sink, compressed_decoder=decoder)
                else:
                    player = self.playback(sink, b"")
                self.assertEqual(await player.play("synthetic", self.reply), PlaybackResult(0, 0))
                self.assertEqual(sink.writes, [])
                sink.drain.assert_not_awaited()

    async def test_counts_source_blocks_and_exact_bytes_without_optional_drain(self):
        # At 8 kHz a 20-ms source block is 320 bytes, not a 640-byte wire frame.
        pcm = b"\x02\x00" * 325
        sink = Sink()
        outcome = await self.playback(sink, pcm, 8000).play("synthetic", self.reply)
        self.assertEqual(outcome, PlaybackResult(3, 650))
        self.assertEqual(sink.writes, [
            ("synthetic", pcm[:320], 8000),
            ("synthetic", pcm[320:640], 8000),
            ("synthetic", pcm[640:], 8000),
        ])

    async def test_positive_outcome_waits_for_all_writes_and_drain(self):
        pcm = b"\x03\x00" * 645
        sink = Sink()
        entered, release = asyncio.Event(), asyncio.Event()

        async def drain(phone):
            self.assertEqual(phone, "synthetic")
            self.assertEqual(b"".join(row[1] for row in sink.writes), pcm)
            entered.set()
            await release.wait()

        sink.drain = drain
        task = asyncio.create_task(self.playback(sink, pcm).play("synthetic", self.reply))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(task.done())
            release.set()
            outcome = await asyncio.wait_for(task, 1)
            self.assertEqual(outcome, PlaybackResult(3, 1290))
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_partial_write_failure_preserves_exception_and_has_no_outcome(self):
        sink = Sink()
        failure = RuntimeError("synthetic write failure")
        original = sink.write_pcm

        async def write(phone, data, rate):
            if sink.writes:
                raise failure
            await original(phone, data, rate)

        sink.write_pcm = write
        sink.drain = AsyncMock()
        completed = []
        with self.assertRaises(RuntimeError) as raised:
            completed.append(await self.playback(sink, b"\x01\x00" * 640).play("synthetic", self.reply))
        self.assertIs(raised.exception, failure)
        self.assertEqual(len(sink.writes), 1)
        self.assertEqual(completed, [])
        sink.drain.assert_not_awaited()

    async def test_drain_failure_preserves_exception_and_has_no_outcome(self):
        sink = Sink()
        failure = RuntimeError("synthetic drain failure")
        sink.drain = AsyncMock(side_effect=failure)
        completed = []
        with self.assertRaises(RuntimeError) as raised:
            completed.append(await self.playback(sink, b"\x01\x00" * 320).play("synthetic", self.reply))
        self.assertIs(raised.exception, failure)
        self.assertEqual(len(sink.writes), 1)
        self.assertEqual(completed, [])
        sink.drain.assert_awaited_once_with("synthetic")

    async def test_cancel_during_write_or_drain_has_no_outcome(self):
        for phase in ("write", "drain"):
            with self.subTest(phase=phase):
                sink = Sink()
                entered, hold = asyncio.Event(), asyncio.Event()
                completed = []

                async def block(*args):
                    entered.set()
                    await hold.wait()

                if phase == "write":
                    sink.write_pcm = block
                    sink.drain = AsyncMock()
                else:
                    sink.drain = block

                async def play():
                    completed.append(await self.playback(sink, b"\x01\x00" * 320).play("synthetic", self.reply))

                task = asyncio.create_task(play())
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertEqual(completed, [])
                    if phase == "write":
                        sink.drain.assert_not_awaited()
                    else:
                        self.assertEqual(len(sink.writes), 1)
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_stop_during_decode_or_drain_invalidates_normal_return(self):
        for phase in ("decode", "drain"):
            with self.subTest(phase=phase):
                sink = Sink()
                sink.stop = AsyncMock()
                entered, release = asyncio.Event(), asyncio.Event()
                pcm = b"\x01\x00" * 320

                async def pause(*args):
                    entered.set()
                    await release.wait()
                    return PCMOutput(pcm, 16000)

                if phase == "decode":
                    fetch = AsyncMock(return_value=AudioPayload(b"synthetic-compressed", "audio/mpeg"))
                    player = WavePlayback(fetch, sink, compressed_decoder=pause)
                else:
                    sink.drain = pause
                    player = self.playback(sink, pcm)
                completed = []

                async def play():
                    completed.append(await player.play("synthetic", self.reply))

                task = asyncio.create_task(play())
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    await player.stop("synthetic")
                    sink.stop.assert_awaited_once_with("synthetic")
                    release.set()  # The dependency returns normally, not via task cancellation.
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 1)
                    self.assertEqual(completed, [])
                    self.assertEqual(len(sink.writes), 0 if phase == "decode" else 1)
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_stopping_another_phone_does_not_cancel_pending_playback(self):
        sink = Sink()
        sink.stop = AsyncMock()
        entered, release = asyncio.Event(), asyncio.Event()
        pcm = b"\x01\x00" * 320

        async def fetch(url):
            entered.set()
            await release.wait()
            return AudioPayload(make_wave(pcm), "audio/wav")

        player = WavePlayback(fetch, sink)
        task = asyncio.create_task(player.play("synthetic", self.reply))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await player.stop("other-phone")
            release.set()
            self.assertEqual(await asyncio.wait_for(task, 1), PlaybackResult(1, 640))
            self.assertEqual(sink.writes, [("synthetic", pcm, 16000)])
            sink.stop.assert_awaited_once_with("other-phone")
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
