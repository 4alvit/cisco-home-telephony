"""Real public synthetic media through the installed shared FFmpeg adapters.

Run this file on stdin inside each final image; fixture generation uses only
local CPU and temporary files. The same test also runs in the host suite.
"""
import importlib.util
import math
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest


def installed_media_runtime():
    candidates = (
        Path(__file__).resolve().parent.parent / 'containers/asterisk/media_runtime.py',
        Path('/workspace/containers/asterisk/media_runtime.py'),
        Path('/usr/local/lib/media_runtime.py'),
    )
    source = next((path for path in candidates if path.is_file()), None)
    if source is None:
        raise RuntimeError('shared media runtime is absent from the final image')
    spec = importlib.util.spec_from_file_location('contract_media_runtime', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),
                     'FFmpeg and ffprobe are required for real media fixtures')
class FFmpegRuntimeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media = installed_media_runtime()
        cls.temporary = tempfile.TemporaryDirectory(prefix='ffmpeg-contract-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        cls.pcm = b''.join(struct.pack('<h', round(8000 * math.sin(i * 2 * math.pi * 440 / 16000)))
                           for i in range(4000))
        cls.samples = []
        for name, fmt, encoder, extra in (
            ('mp3', 'mp3', 'libmp3lame', []),
            ('aac', 'adts', 'aac', []),
            ('vorbis', 'ogg', 'vorbis', ['-strict', 'experimental', '-ac', '2']),
            ('opus', 'ogg', 'libopus', []),
            ('flac', 'flac', 'flac', []),
            ('wav16', 'wav', 'pcm_s16le', []),
            ('wav24', 'wav', 'pcm_s24le', []),
            ('wav32', 'wav', 'pcm_s32le', []),
            ('wavfloat', 'wav', 'pcm_f32le', []),
            ('wav8', 'wav', 'pcm_u8', []),
        ):
            path = cls.directory / name
            subprocess.run([
                'ffmpeg', '-nostdin', '-v', 'error', '-f', 's16le', '-ar', '16000',
                '-ac', '1', '-i', 'pipe:0', '-threads', '1', '-c:a', encoder, *extra,
                '-metadata', 'title=Contract title', '-metadata', 'artist=Contract artist',
                '-f', fmt, str(path),
            ], input=cls.pcm, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=15, check=True)
            cls.samples.append((name, 'aac' if fmt == 'adts' else fmt, path))

    def decode(self, fmt, path, position=0):
        result = subprocess.run(self.media.decoder_args(fmt, position), input=path.read_bytes(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=15, check=True)
        return result.stdout

    def test_every_supported_audio_family_decodes_to_audible_pcm(self):
        for name, fmt, path in self.samples:
            with self.subTest(codec=name):
                decoded = self.decode(fmt, path)
                self.assertEqual(len(decoded) % 2, 0)
                self.assertGreaterEqual(len(decoded), 6400)
                self.assertLess(len(decoded), 16000)
                values = struct.unpack('<' + 'h' * (len(decoded) // 2), decoded)
                self.assertGreater(max(abs(value) for value in values), 1000)
                if name in ('flac', 'wav16', 'wav24', 'wav32', 'wavfloat'):
                    self.assertEqual(decoded, self.pcm)

    def test_ffprobe_reads_finite_duration_and_metadata(self):
        for name, fmt, path in self.samples:
            with self.subTest(codec=name), path.open('rb') as source:
                result = self.media.probe_file(source, fmt)
                self.assertGreater(result['duration'], .1)
                self.assertLess(result['duration'], .5)
                self.assertEqual(source.tell(), 0)
                if name != 'aac':
                    self.assertEqual(result['title'], 'Contract title')
                    self.assertEqual(result['artist'], 'Contract artist')

    def test_seek_and_radio_gain_filters_work(self):
        path = next(path for name, _, path in self.samples if name == 'flac')
        decoded = self.decode('flac', path, position=.1)
        self.assertEqual(decoded, self.pcm[3200:])
        radio_path = next(path for name, _, path in self.samples if name == 'mp3')
        reference = self.decode('mp3', radio_path)
        result = subprocess.run(self.media.radio.ffmpeg_args({'format': 'mp3'}),
                                input=radio_path.read_bytes(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=15, check=True)
        self.assertEqual(len(result.stdout), len(reference))
        shape = '<' + 'h' * (len(reference) // 2)
        source = struct.unpack(shape, reference)
        quieter = struct.unpack(shape, result.stdout)
        self.assertTrue(all(abs(actual - expected / 2) <= 1
                            for actual, expected in zip(quieter, source)))


if __name__ == '__main__':
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise SystemExit('Final-image media contract requires ffmpeg and ffprobe; refusing to skip')
    unittest.main()
