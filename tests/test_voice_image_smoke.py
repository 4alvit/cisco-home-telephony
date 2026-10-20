"""Run the image's actual synthetic control smoke without a container."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class VoiceImageControlSmokeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg required for voice smoke')
    def test_actual_control_smoke_preserves_journal_and_releases_shutdown_fence(self):
        code = """
import runpy, sys
smoke = runpy.run_path(sys.argv[1])
sys.addaudithook(smoke['reject_network'])
smoke['check_cli'](python_executable=sys.executable,
                   ffmpeg_executable=sys.argv[2], cli_script=sys.argv[3])
print('synthetic_control_smoke_passed')
"""
        env = dict(os.environ)
        env['PYTHONPATH'] = os.pathsep.join(str(path) for path in (
            ROOT, ROOT / 'containers/asterisk', ROOT / 'containers/voice'))
        result = subprocess.run(
            [sys.executable, '-B', '-c', code, str(ROOT / 'containers/voice/smoke.py'),
             shutil.which('ffmpeg'), str(ROOT / 'scripts/voice_session.py')],
            env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'synthetic_control_smoke_passed')


if __name__ == '__main__':
    unittest.main()
