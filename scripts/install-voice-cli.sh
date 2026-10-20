#!/usr/bin/env bash
# Install a local launcher without altering the checkout or Python environment.
set -euo pipefail

usage() {
    cat <<'HELP'
Usage: install-voice-cli.sh [--repo PATH] [--python PATH_OR_COMMAND]
                          [--prefix PATH | --bin-dir PATH]

Install cisco-voice, a launcher for this checkout's scripts/voice_session.py.

  --repo PATH        Stable Git checkout root (default: this installer's repo)
  --python COMMAND   Python executable, version 3.10+ (default: python3)
  --prefix PATH      Install PATH/bin/cisco-voice
  --bin-dir PATH     Install PATH/cisco-voice (default: $HOME/.local/bin)
  --help             Show this help without installing anything

The launcher preserves the caller's working directory and arguments. Keep the
checkout and selected Python environment at their original paths. Dependencies
must be installed separately; this installer downloads nothing and uses no sudo.
Live voice sessions need websockets and aiohttp (including for TTS retrieval);
install voice_assistant/requirements-websocket.txt in the selected environment.
Python 3.11+ is recommended. Python 3.10 also requires async_timeout>=4,<6 in
the selected environment; installation is refused if that backport is missing.
An identical executable wrapper is left unchanged. Any differing existing file
or destination symlink is refused; there is no overwrite or update option.
HELP
}

fail() {
    printf 'install-voice-cli: %s\n' "$1" >&2
    exit "${2:-1}"
}

repo=''
python_command='python3'
destination=''
destination_kind=''
while (($#)); do
    case "$1" in
        --help)
            usage
            exit 0
            ;;
        --repo|--python|--prefix|--bin-dir)
            option=$1
            (($# >= 2)) || fail "Missing value for $option." 2
            [[ -n "$2" && "$2" != --* ]] || fail "Missing value for $option." 2
            case "$option" in
                --repo) repo=$2 ;;
                --python) python_command=$2 ;;
                --prefix|--bin-dir)
                    [[ -z "$destination_kind" ]] || fail '--prefix and --bin-dir may be specified only once and are mutually exclusive.' 2
                    destination_kind=$option
                    destination=$2
                    ;;
            esac
            shift 2
            ;;
        *) fail "Unknown argument: $1. Use --help for usage." 2 ;;
    esac
done

if [[ -z "$repo" ]]; then
    repo=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd) || fail 'Cannot locate the installer checkout.'
fi
if [[ -z "$destination_kind" ]]; then
    [[ -n "${HOME:-}" ]] || fail 'HOME is unset; specify --bin-dir or --prefix.'
    destination=$HOME/.local/bin
elif [[ "$destination_kind" == --prefix ]]; then
    destination=$destination/bin
fi

if [[ "$python_command" == */* ]]; then
    python_path=$python_command
else
    python_path=$(type -P -- "$python_command") || fail 'The selected Python executable was not found.'
fi
[[ -f "$python_path" && -x "$python_path" ]] || fail 'The selected Python path must be an executable file.'
version=$("$python_path" -c 'import sys; print("cisco-voice-python %d.%d" % sys.version_info[:2])' 2>/dev/null) || fail 'The selected executable must be Python 3.10 or newer.'
if [[ "$version" =~ ^cisco-voice-python\ ([0-9]+)\.([0-9]+)$ ]]; then
    major=${BASH_REMATCH[1]}
    minor=${BASH_REMATCH[2]}
    ((10#$major > 3 || (10#$major == 3 && 10#$minor >= 10))) || fail 'The selected executable must be Python 3.10 or newer.'
else
    fail 'The selected executable must be Python 3.10 or newer.'
fi
if ((10#$major == 3 && 10#$minor == 10)); then
    "$python_path" -c '
import importlib.metadata
import re
import async_timeout
version = importlib.metadata.version("async-timeout")
if not re.fullmatch(r"[45]\.\d+(?:\.\d+)?(?:\.post\d+)?", version) or not callable(getattr(async_timeout, "timeout", None)):
    raise SystemExit(1)
' 2>/dev/null || fail 'Python 3.10 requires async_timeout>=4,<6. Install voice_assistant/requirements-python310.txt into the selected Python environment first.'
fi

# Arguments are passed separately, never interpolated into executable code.
"$python_path" - "$repo" "$python_path" "$destination" <<'PYTHON'
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile


def fail(message):
    print('install-voice-cli: ' + message, file=sys.stderr)
    raise SystemExit(1)


if sys.version_info[:2] < (3, 10):
    fail('The selected executable must be Python 3.10 or newer.')

try:
    repo = Path(sys.argv[1]).resolve(strict=True)
    # abspath deliberately preserves the final venv executable symlink.
    python = Path(os.path.abspath(sys.argv[2]))
    bin_dir = Path(os.path.abspath(sys.argv[3]))
    if not repo.is_dir():
        fail('The source repository must be a stable Git checkout directory.')
    result = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--show-toplevel'],
                            check=False, capture_output=True, text=True)
    if result.returncode or Path(result.stdout.rstrip('\n')).resolve() != repo:
        fail('The source repository must be the root of a stable Git checkout.')
    source = repo / 'scripts' / 'voice_session.py'
    if (not source.is_file() or source.is_symlink() or not os.access(source, os.R_OK) or
            source.parent.resolve() != repo / 'scripts'):
        fail('The checkout must contain a readable, regular scripts/voice_session.py.')
    if not python.is_file() or not os.access(python, os.X_OK):
        fail('The selected Python path must remain an executable file.')
    if bin_dir.is_symlink():
        fail('The destination directory must not be a symlink.')

    wrapper = '''#!/bin/sh
# Managed by cisco-home-telephony install-voice-cli.sh v1.
# Keep the original checkout and selected Python environment in place.
python={python}
source={source}
if [ ! -f "$source" ] || [ ! -r "$source" ]; then
    printf '%s\\n' 'cisco-voice: source missing; restore the original stable checkout or install into a new destination.' >&2
    exit 127
fi
if [ ! -f "$python" ] || [ ! -x "$python" ]; then
    printf '%s\\n' 'cisco-voice: Python unavailable; restore the selected environment or install into a new destination.' >&2
    exit 127
fi
exec "$python" "$source" "$@"
'''.format(python=shlex.quote(str(python)), source=shlex.quote(str(source))).encode('utf-8')
    target = bin_dir / 'cisco-voice'

    def identical_existing():
        try:
            info = target.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
            fail('Refusing an existing non-executable file, directory, or symlink at cisco-voice.')
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(target, flags), 'rb') as existing:
            opened = os.fstat(existing.fileno())
            if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or
                    not stat.S_ISREG(opened.st_mode) or existing.read(len(wrapper) + 1) != wrapper):
                fail('Refusing to overwrite a different existing cisco-voice command. Choose another destination.')
        return True

    if identical_existing():
        print('Already installed cisco-voice: ' + str(target))
        raise SystemExit(0)
    bin_dir.mkdir(parents=True, exist_ok=True)
    # Refuse a newly encountered directory symlink before creating any file.
    if bin_dir.is_symlink() or not bin_dir.is_dir():
        fail('The destination must be a directory, not a symlink.')
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix='.cisco-voice-', dir=bin_dir)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(wrapper)
            output.flush()
            os.fchmod(output.fileno(), 0o755)
            os.fsync(output.fileno())
        try:
            # link() is exclusive even if another installer creates the target
            # after the earlier check; it never follows or replaces that target.
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            if identical_existing():
                print('Already installed cisco-voice: ' + str(target))
            else:
                fail('The destination changed during installation; retry safely.')
        else:
            print('Installed cisco-voice: ' + str(target))
    finally:
        if temporary is not None:
            os.unlink(temporary)
except (OSError, ValueError, subprocess.SubprocessError):
    fail('Installation failed; check the checkout, Python executable, and destination permissions. Existing commands were not replaced.')
PYTHON
