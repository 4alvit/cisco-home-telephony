#!/usr/bin/env python3
"""Build-only, hash-pinned package installation without a runtime pip copy."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import urllib.request

PIP_URL = ('https://files.pythonhosted.org/packages/3f/06/'
           '3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/'
           'pip-26.2.2-py3-none-any.whl')
PIP_SHA256 = 'fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70'
MAX_WHEEL_BYTES = 3 * 1024 * 1024
PIP_MAIN = ("import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); "
            "runpy.run_module('pip',run_name='__main__')")


def download_installer(destination: Path) -> None:
    with urllib.request.urlopen(PIP_URL, timeout=30) as response:
        data = response.read(MAX_WHEEL_BYTES + 1)
    if len(data) > MAX_WHEEL_BYTES or hashlib.sha256(data).hexdigest() != PIP_SHA256:
        raise ValueError('Official installer wheel size/hash mismatch')
    with destination.open('xb') as stream:
        stream.write(data)


def install_locked(requirements: Path) -> None:
    if requirements.is_symlink() or not requirements.is_file():
        raise ValueError('A regular locked requirements file is required')
    env = {name: value for name, value in os.environ.items()
           if name not in ('PYTHONPATH', 'PYTHONHOME')}

    def run(arguments):
        subprocess.run([sys.executable, '-I', *arguments], env=env, check=True, timeout=300)

    with tempfile.TemporaryDirectory(prefix='voice-installer-') as directory:
        wheel = Path(directory) / 'pip.whl'
        download_installer(wheel)
        # Use the existing distribution to remove itself BEFORE the temporary
        # wheel enters sys.path. Otherwise pip can select the wheel's metadata.
        if importlib.util.find_spec('pip') is not None:
            run(['-m', 'pip', '--isolated', '--disable-pip-version-check',
                 'uninstall', '--yes', 'pip'])
            importlib.invalidate_caches()
            if importlib.util.find_spec('pip') is not None:
                raise RuntimeError('The global installer was not removed')
        entry = ['-c', PIP_MAIN, str(wheel), '--isolated', '--disable-pip-version-check']
        run([*entry, 'install', '--no-cache-dir', '--only-binary=:all:',
             '--require-hashes', '-r', str(requirements)])
        run([*entry, 'check'])
    ensurepip = Path(sysconfig.get_path('stdlib')) / 'ensurepip'
    if ensurepip.exists():
        shutil.rmtree(ensurepip)
    importlib.invalidate_caches()
    if any(importlib.util.find_spec(name) is not None for name in ('pip', 'ensurepip')):
        raise RuntimeError('The final runtime still contains an installer')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('usage: install_locked.py /absolute/requirements.lock')
    if (Path(__file__).resolve() != Path('/tmp/install_locked.py')
            or sys.argv[1] not in ('/opt/voice/requirements.lock', '/opt/voice-wake/requirements.lock')
            or os.geteuid() != 0):
        raise SystemExit('This helper is only for the controlled Docker build stage')
    install_locked(Path(sys.argv[1]))
