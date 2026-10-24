"""Exercise the real decoder with synthetic speech and attached video artwork."""
import asyncio
import math
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from voice_assistant.playback import FFmpegDecoder


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required for real media fixtures')
class SpeechCodecTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = shutil.which('ffmpeg')
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        pcm = b''.join(struct.pack('<h', int(8000 * math.sin(i * 2 * math.pi * 440 / 16000)))
                       for i in range(3200))
        source = cls.root / 'synthetic.pcm'; source.write_bytes(pcm)
        cls.audio = {}
        # A regular file avoids platform pipe-buffer behavior in fixture encoders.
        for name, fmt, encoder, extra in (
                ('mp3', 'mp3', 'libmp3lame', []),
                ('vorbis', 'ogg', 'vorbis', ['-strict', 'experimental', '-ac', '2']),
                ('opus', 'ogg', 'libopus', []),
                ('flac', 'flac', 'flac', [])):
            result = cls.ffmpeg_run(['-f', 's16le', '-ar', '16000', '-ac', '1', '-i', str(source),
                                     '-threads', '1', '-c:a', encoder, *extra, '-f', fmt, 'pipe:1'])
            result.check_returncode()
            cls.audio[name] = (result.stdout, 'audio/' + fmt)
        png = cls.ffmpeg_run(['-f', 'lavfi', '-i', 'color=c=red:s=16x16:r=1', '-frames:v', '1',
                             '-threads', '1', '-c:v', 'png', '-f', 'image2pipe', 'pipe:1'])
        png.check_returncode()
        # A valid ID3v2.3 APIC frame; no external media, malformed payload or exploit.
        picture = b'\0image/png\0\x03\0' + png.stdout
        frame = b'APIC' + struct.pack('>I', len(picture)) + b'\0\0' + picture
        size = len(frame)
        tag = b'ID3\x03\0\0' + bytes((size >> shift & 127 for shift in (21, 14, 7, 0))) + frame
        mp3 = cls.audio['mp3'][0]
        if mp3.startswith(b'ID3'):
            mp3 = mp3[10 + sum(value << shift for value, shift in zip(mp3[6:10], (21, 14, 7, 0))):]
        cls.artwork_audio = tag + mp3

    @classmethod
    def ffmpeg_run(cls, arguments, data=None):
        return subprocess.run([cls.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', *arguments],
                              input=data, stdin=subprocess.DEVNULL if data is None else None,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)

    async def decode_recorded(self, data, mime):
        recorded = []
        original = asyncio.create_subprocess_exec
        async def launch(*args, **kwargs):
            recorded.append(args)
            return await original(*args, **kwargs)
        with patch('voice_assistant.playback.asyncio.create_subprocess_exec', side_effect=launch):
            output = await FFmpegDecoder(self.ffmpeg, max_seconds=2)(data, mime)
        self.assertEqual(len(recorded), 1)
        return output, recorded[0]

    async def test_supported_speech_codecs_remain_decodable(self):
        for name, (data, mime) in self.audio.items():
            with self.subTest(codec=name):
                output, arguments = await self.decode_recorded(data, mime)
                self.assertEqual((output.sample_rate, output.channels, output.sample_width), (16000, 1, 2))
                self.assertGreaterEqual(len(output.data), 3200)
                self.assertLess(len(output.data), 64000)
                self.assertTrue(any(output.data))
                self.assertEqual(arguments[arguments.index('-map') + 1], '0:a:0')
                self.assertEqual(arguments[arguments.index('-protocol_whitelist') + 1], 'pipe')
                self.assertLess(arguments.index('-codec_whitelist'), arguments.index('-i'))
                for flag in ('-vn', '-sn', '-dn'):
                    self.assertIn(flag, arguments)

    async def test_attached_png_cannot_open_video_decoder_but_speech_is_preserved(self):
        original, _ = await self.decode_recorded(*self.audio['mp3'])
        decorated, arguments = await self.decode_recorded(self.artwork_audio, 'audio/mpeg')
        self.assertEqual(decorated.data, original.data)
        whitelist = arguments[arguments.index('-codec_whitelist') + 1]
        video = ['-f', 'mp3', '-i', 'pipe:0', '-map', '0:v:0', '-c:v', 'rawvideo',
                 '-frames:v', '1', '-f', 'rawvideo', 'pipe:1']
        # Positive control proves the fixture really contains decodable video.
        allowed = self.ffmpeg_run(video, self.artwork_audio)
        allowed.check_returncode()
        self.assertEqual(len(allowed.stdout), 16 * 16 * 3)
        rejected = self.ffmpeg_run(['-codec_whitelist', whitelist, *video], self.artwork_audio)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(rejected.stdout, b'')
        self.assertIn(b'Codec (png) not on whitelist', rejected.stderr)

    async def test_non_speech_input_type_is_rejected_before_spawning(self):
        with patch('voice_assistant.playback.asyncio.create_subprocess_exec') as launch:
            with self.assertRaises(ValueError):
                await FFmpegDecoder(self.ffmpeg)(self.artwork_audio, 'video/mp4')
            launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
