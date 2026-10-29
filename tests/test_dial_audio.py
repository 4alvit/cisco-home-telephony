import datetime as dt
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dial_audio', ROOT / 'containers/asterisk/dial_audio.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ClockAndMediaTests(unittest.TestCase):
    def test_russian_number_and_plural(self):
        time = dt.datetime(2026, 1, 1, 21, 21)
        self.assertEqual(m.clock_message(time, 'ru'), 'Сейчас двадцать один час, двадцать одна минута.')
        self.assertEqual(m.clock_message(time.replace(hour=0, minute=12), 'ru'), 'Сейчас ноль часов, двенадцать минут.')
        self.assertEqual(m.russian_number(22, True), 'двадцать две')

    def test_dst_uses_configured_zone(self):
        before = dt.datetime(2026, 11, 1, 8, 59, tzinfo=dt.timezone.utc).astimezone(ZoneInfo('America/Los_Angeles'))
        after = dt.datetime(2026, 11, 1, 9, 0, tzinfo=dt.timezone.utc).astimezone(ZoneInfo('America/Los_Angeles'))
        self.assertEqual(m.clock_message(before, 'en'), 'The local time is 01:59 AM.')
        self.assertEqual(m.clock_message(after, 'en'), 'The local time is 01:00 AM.')
        self.assertNotEqual(before.utcoffset(), after.utcoffset())

    def test_remote_and_traversal_paths_rejected(self):
        for value in ('https://example.org/media/a', '//example.org/a', '/media/../api/a',
                      '/media/%2e%2e/api/a', '/media/a?token=x', '/media/a#x', '/media/a\\b'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                m.check_path(value, '/media/')
        self.assertEqual(m.check_path('/media/music/Some%20Song.mp3', '/media/'), '/media/music/Some%20Song.mp3')

    def test_example_config(self):
        import json
        config = json.loads((ROOT / 'deploy/dial-audio.example.json').read_text())
        self.assertIs(m.validate(config), config)

    def test_minute_rollover_resynthesizes(self):
        from unittest.mock import Mock
        ha = Mock()
        ha.speech.return_value = b'fixture'
        base = dt.datetime(2026, 1, 1, 9, 59, 59)
        times = iter([base, base + dt.timedelta(seconds=2), base + dt.timedelta(seconds=2), base + dt.timedelta(seconds=3)])
        service = {'kind': 'clock', 'timezone': 'UTC', 'language': 'en'}
        with tempfile.TemporaryDirectory() as directory:
            def encode(args, **kwargs):
                Path(args[-1]).write_bytes(b'\0' * 3200)
            with patch.object(m.subprocess, 'run', side_effect=encode):
                m.prepare_audio(ha, service, Path(directory), now=lambda: next(times))
        self.assertEqual(ha.speech.call_count, 2)
        self.assertIn('10:00 AM', ha.speech.call_args.args[1])

    def test_media_transcodes_local_file_to_slin16(self):
        from unittest.mock import Mock
        ha = Mock()
        ha.audio.return_value = b'fixture'
        with tempfile.TemporaryDirectory() as directory:
            def encode(args, **kwargs):
                self.assertEqual(args[args.index('-protocol_whitelist') + 1], 'file')
                self.assertEqual(args[args.index('-ar') + 1], '16000')
                self.assertEqual(args[args.index('-t') + 1], '600')
                Path(args[-1]).write_bytes(b'\0' * 3200)
            with patch.object(m.subprocess, 'run', side_effect=encode):
                path = m.prepare_audio(ha, {'kind':'media', 'path':'/media/music/a.mp3', 'max_seconds':600}, Path(directory))
            self.assertEqual(path.name, 'playback')
        ha.audio.assert_called_once_with('/media/music/a.mp3')


if __name__ == '__main__':
    unittest.main()
