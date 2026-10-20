"""Bounded speech decoding and transport-neutral PCM playback."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import io
import struct
import wave
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class PCMOutput:
    data: bytes
    sample_rate: int
    channels: int = 1
    sample_width: int = 2


def decode_wave(data: bytes, *, max_seconds: float = 60) -> PCMOutput:
    """Accept uncompressed LE16 WAV, downmix stereo, preserve explicit rate."""
    with wave.open(io.BytesIO(data), "rb") as source:
        rate, channels = source.getframerate(), source.getnchannels()
        if (source.getcomptype() != "NONE" or source.getsampwidth() != 2 or
                channels not in (1, 2) or rate not in (8000, 16000, 22050, 24000, 44100, 48000)):
            raise ValueError("unsupported PCM WAV format")
        frames = source.getnframes()
        if frames > rate * max_seconds:
            raise ValueError("speech exceeds duration limit")
        pcm = source.readframes(frames)
        if len(pcm) != frames * channels * 2:
            raise ValueError("truncated PCM WAV")
    if channels == 2:
        values = struct.unpack("<" + "h" * (len(pcm) // 2), pcm)
        mono = [int((values[i] + values[i + 1]) / 2) for i in range(0, len(values), 2)]
        pcm = struct.pack("<" + "h" * len(mono), *mono)
    return PCMOutput(pcm, rate)


class FFmpegDecoder:
    """Optional local decoder for compressed HA speech; installs nothing.

    Only pipe input is allowed, network protocols and input playlists are disabled.
    Output and execution time are bounded; cancellation kills the child process.
    """

    def __init__(self, executable: str = "ffmpeg", max_seconds: int = 60):
        if type(max_seconds) is not int or not 1 <= max_seconds <= 180:
            raise ValueError("invalid decoder duration")
        self.executable, self.max_seconds = executable, max_seconds

    async def __call__(self, data: bytes, content_type: str) -> PCMOutput:
        formats = {"audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/ogg": "ogg",
                   "application/ogg": "ogg", "audio/flac": "flac", "audio/x-flac": "flac"}
        fmt = formats.get(content_type.split(";", 1)[0].lower())
        if fmt is None:
            raise ValueError("unsupported compressed speech format")
        process = await asyncio.create_subprocess_exec(
            self.executable, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-protocol_whitelist", "pipe", "-f", fmt, "-i", "pipe:0",
            "-t", str(self.max_seconds + 1), "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        try:
            output, _ = await asyncio.wait_for(process.communicate(data), 15)
            if process.returncode or len(output) > self.max_seconds * 32000:
                raise ValueError("speech decoding failed or exceeded duration")
            return PCMOutput(output, 16000)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


class WavePlayback:
    """Fetch TTS and deliver PCM blocks to the parent-owned media sender.

    The sink owns pacing/resampling/RTP and must implement stop by flushing pending
    media. Calls include the explicit sample rate; 20 ms frames are used where
    the source rate permits. write_pcm must await actual delivery, or the sink
    must provide async drain(phone_id) to await all queued playback. No socket
    or physical audio device is opened here.
    """

    def __init__(self, fetch_audio: Callable[[str], Awaitable[Any]], sink: Any, *,
                 compressed_decoder: Callable[[bytes, str], Awaitable[PCMOutput]] | None = None,
                 max_seconds: float = 60):
        self.fetch_audio, self.sink = fetch_audio, sink
        self.compressed_decoder, self.max_seconds = compressed_decoder, max_seconds

    async def play(self, phone_id: str, result: Any) -> None:
        if not result.tts_url:
            return
        payload = await self.fetch_audio(result.tts_url)
        if payload.data.startswith(b"RIFF") and payload.data[8:12] == b"WAVE":
            pcm = decode_wave(payload.data, max_seconds=self.max_seconds)
        elif self.compressed_decoder:
            pcm = await self.compressed_decoder(payload.data, payload.content_type)
        else:
            raise ValueError("compressed TTS requires an explicitly configured local decoder")
        if (pcm.sample_width != 2 or pcm.channels != 1 or pcm.sample_rate <= 0 or
                len(pcm.data) % 2 or len(pcm.data) > pcm.sample_rate * 2 * self.max_seconds):
            raise ValueError("invalid or excessive decoded speech")
        frame_bytes = (pcm.sample_rate // 50) * 2
        for offset in range(0, len(pcm.data), frame_bytes):
            await self.sink.write_pcm(phone_id, pcm.data[offset:offset + frame_bytes], pcm.sample_rate)
        drain = getattr(self.sink, "drain", None)
        if drain is not None:
            await drain(phone_id)

    async def stop(self, phone_id: str) -> None:
        await self.sink.stop(phone_id)
