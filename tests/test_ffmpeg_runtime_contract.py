"""Real synthetic audio and image media through the installed shared adapters.

Run this file on stdin inside each final image; fixture generation uses only
local CPU and temporary files. The same test also runs in the host suite.
"""
import base64
import importlib.metadata
import importlib.util
import io
import math
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


def installed_module(name, *, runtime_name=None):
    candidates = (
        Path(__file__).resolve().parent.parent / 'containers/asterisk' / (name + '.py'),
        Path('/workspace/containers/asterisk') / (name + '.py'),
        Path('/usr/local/lib') / ((runtime_name or name) + '.py'),
    )
    source = next((path for path in candidates if path.is_file()), None)
    if source is None:
        raise RuntimeError('shared runtime module is absent from the final image: ' + name)
    spec = importlib.util.spec_from_file_location('contract_' + name, source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),
                     'FFmpeg and ffprobe are required for real media fixtures')
class FFmpegRuntimeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media = installed_module('media_runtime')
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


@unittest.skipUnless(importlib.util.find_spec('PIL'), 'Pillow is required for image fixtures')
class PillowRuntimeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PIL import Image
        cls.image = Image
        cls.display = installed_module('phone_voice_display')
        cls.gateway = installed_module('phone_gateway', runtime_name='cisco-phone-gateway')

    def encoded_image(self, fmt):
        image = self.image.new('RGB', (64, 32), (40, 120, 220))
        exif = self.image.Exif()
        exif[274] = 6  # A clockwise rotation must produce a portrait image.
        exif[270] = 'Synthetic private metadata'
        output = io.BytesIO()
        image.save(output, format=fmt, exif=exif, icc_profile=b'synthetic ICC metadata')
        return output.getvalue()

    def assert_clean_png(self, data, expected_size):
        with self.image.open(io.BytesIO(data)) as image:
            image.load()
            self.assertEqual(image.format, 'PNG')
            self.assertEqual(image.mode, 'RGB')
            self.assertEqual(image.size, expected_size)
            self.assertEqual(dict(image.getexif()), {})
            self.assertNotIn('icc_profile', image.info)
            pixel = image.getpixel((image.width // 2, image.height // 2))
            self.assertTrue(all(abs(actual - expected) <= 3
                                for actual, expected in zip(pixel, (40, 120, 220))))

    def test_phone_display_decodes_png_and_jpeg_orients_and_strips_metadata(self):
        for fmt in ('PNG', 'JPEG'):
            with self.subTest(format=fmt):
                rendered = self.display.phone_image(self.encoded_image(fmt))
                self.assert_clean_png(rendered, (298, 156))
                with self.image.open(io.BytesIO(rendered)) as image:
                    # The rotated portrait is centered in the wide screen canvas.
                    self.assertEqual(image.getpixel((20, 78)), self.display.BACKGROUND)

    def test_gateway_image_notification_converts_without_a_device_transport(self):
        gateway = object.__new__(self.gateway.Gateway)
        for fmt in ('PNG', 'JPEG'):
            with self.subTest(format=fmt):
                resources = []
                def capture(phone, data, content_type):
                    resources.append((data, content_type))
                    return 'http://127.0.0.1/synthetic/' + str(len(resources))
                gateway.resource = capture
                with patch.object(self.gateway, 'execute') as execute:
                    gateway.notify({'id': '201'}, 'image', {
                        'title': 'Synthetic fixture',
                        'image_base64': base64.b64encode(self.encoded_image(fmt)).decode(),
                    })
                execute.assert_called_once_with({'id': '201'}, 'http://127.0.0.1/synthetic/2', priority=2)
                self.assertEqual(len(resources), 2)
                self.assertEqual(resources[0][1], 'image/png')
                self.assert_clean_png(resources[0][0], (78, 156))
                self.assertIn(b'CiscoIPPhoneImageFile', resources[1][0])

    def test_unsupported_format_is_rejected_without_publishing(self):
        output = io.BytesIO()
        self.image.new('RGB', (8, 8)).save(output, format='GIF')
        with self.assertRaises(self.display.DisplayError):
            self.display.phone_image(output.getvalue())
        gateway = object.__new__(self.gateway.Gateway)
        gateway.resource = Mock()
        with patch.object(self.gateway, 'execute') as execute:
            with self.assertRaises(self.gateway.Refused):
                gateway.notify({'id': '201'}, 'image', {
                    'image_base64': base64.b64encode(output.getvalue()).decode(),
                })
        gateway.resource.assert_not_called()
        execute.assert_not_called()

    @unittest.skipUnless(__name__ == '__main__', 'exact dependency policy is for the final image')
    def test_final_image_has_auditable_pillow_without_runtime_installers(self):
        import PIL
        self.assertEqual(PIL.__version__, '13.0.0')
        self.assertEqual(importlib.metadata.version('Pillow'), PIL.__version__)
        self.assertIsNone(importlib.util.find_spec('pip'))
        self.assertIsNone(importlib.util.find_spec('ensurepip'))
        self.assertTrue(any(str(path).endswith('.dist-info/METADATA')
                            for path in importlib.metadata.files('Pillow')))


if __name__ == '__main__':
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise SystemExit('Final-image media contract requires ffmpeg and ffprobe; refusing to skip')
    if importlib.util.find_spec('PIL') is None:
        raise SystemExit('Final-image media contract requires Pillow; refusing to skip')
    unittest.main()
