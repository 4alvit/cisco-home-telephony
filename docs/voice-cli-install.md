# Install and run the foreground voice CLI

`cisco-voice` runs [`scripts/voice_session.py`](../scripts/voice_session.py)
from a stable checkout using a selected Python interpreter. It validates
configuration by default. A live invocation runs one attended, bounded voice
session on one selected physical phone; it does not install an always-on
listener, configure Home Assistant, change firmware or enable startup services.

The coordinating operator has established stock-phone RTP capture, a private
wake-detection pilot on a Python 3.10 target, and audible reply playback. These
are bounded observations, not acceptance of one acoustic HA conversation from
speech through response, duplex acoustics, call coexistence or unattended listening. See
[the recorded transport scope](voice-transport.md#observed-transport-pilots).

All addresses, phone identities, room names, credentials, wake phrases, model
weights and run reports remain private under `.local/`. Examples below contain
paths and placeholders only; they do not select a real phone.

## Prepare a stable checkout and interpreter

Use the existing repository in a location you intend to keep. Do not install
from a disposable worktree or move the checkout or virtual environment after
installation. The wrapper stores their absolute paths and preserves the
virtual environment's Python executable path, including its final symlink.
It does not activate a virtual environment or copy the project.

The installer uses Bash and Git. Python **3.11 or newer is recommended** on a
Unix host; Python 3.10 requires the timeout compatibility extra below. From
the repository root, prepare an isolated interpreter if you do not already
have a suitable one:

```sh
python3 --version
python3 -m venv .local/voice-venv
.local/voice-venv/bin/python -m pip install -r voice_assistant/requirements-websocket.txt
```

For a Python 3.10 environment, install the conditional compatibility
[requirement](../voice_assistant/requirements-python310.txt) using that same
selected interpreter before running the wrapper installer:

```sh
.local/voice-venv/bin/python -m pip install -r voice_assistant/requirements-python310.txt
```

The file specifies `async_timeout>=4,<6; python_version < "3.11"`. On Python
3.10, the installer requires an importable, callable `async_timeout.timeout`
and an installed distribution with major version 4 or 5. Python 3.11+ uses
the standard-library timeout and needs no compatibility extra. Installer
tests exercise the 3.10 branch with a simulated version, and the controller
suite also runs under native Python 3.10. A separate private target-host pilot
verified capture, wake detection and playback on Python 3.10. Those observations
do not establish the complete acoustic HA conversation path or unattended use.

The foreground CLI uses the default WebSocket transport. Its current
[requirement file](../voice_assistant/requirements-websocket.txt) specifies
both `websockets>=15,<17` and `aiohttp>=3.12,<4`. **aiohttp is required for live
TTS audio fetching even when the turn uses websockets.** It supplies the
asynchronous HTTP path that can be cancelled while fetching the reply. Install
both through the requirement file into the interpreter the wrapper will use,
not a different system Python.

The separate `aiohttp_connect` adapter can also carry the WebSocket turn in
other integrations. That is a different choice from the mandatory aiohttp TTS
dependency, and it is not a `cisco-voice` transport flag.

Check imports and display installed versions using the same interpreter. This
opens no network connection and does not establish target-host acceptance:

```sh
.local/voice-venv/bin/python - <<'PY'
import sys
from importlib.metadata import version
import aiohttp
from websockets.asyncio.client import connect

print("Python", sys.version.split()[0])
for package in ("websockets", "aiohttp"):
    print(package, version(package))
if sys.version_info < (3, 11):
    from async_timeout import timeout
    assert callable(timeout)
    print("async_timeout", version("async-timeout"))
PY
```

For the optional phone status display, install Pillow into that same
environment:

```sh
.local/voice-venv/bin/python -m pip install Pillow
.local/voice-venv/bin/python -c 'from PIL import Image'
```

The project does not pin a standalone Pillow version in these requirement
files. Pillow is needed for the display's PNG conversion and default avatars;
image tests skip without it, so a passing suite with skips does not validate
the display. See [display validation](voice-display.md#local-validation-and-dependency-coverage).

An existing FFmpeg executable is needed when HA returns compressed TTS such
as MP3, Ogg or FLAC. PCM WAV playback uses the built-in decoder. Check the
executable you plan to use with `ffmpeg -version`, or supply its absolute path
through `--ffmpeg`. The repository specifies no minimum FFmpeg version for
this CLI. Neither the wrapper installer nor the voice command installs it.

For a reviewed private ONNX wake bundle only, also install
[the ONNX requirements](../voice_assistant/requirements-onnx.txt):

```sh
.local/voice-venv/bin/python -m pip install -r voice_assistant/requirements-onnx.txt
```

Those requirements are `numpy>=1.26,<3` and `onnxruntime>=1.23,<2`.
Manual mode needs neither. No `openwakeword` package, legacy TFLite runtime or
automatic model download is required. Use the requirement files as the source
of truth when updating the checkout.

## Check the selected Home Assistant pipeline

Once the private configuration exists, run this metadata-only check before
opening a physical voice session:

```sh
.local/voice-venv/bin/python -m voice_assistant.pipeline \
  --workspace . --config .local/voice.json
```

It checks the explicitly selected pipeline and the reported availability of
its speech providers. It sends no audio and runs no intent. Exact provider and
voice expectations, JSON output, exit codes and the limits of this check are
documented in [pipeline preflight](voice-pipeline-preflight.md). Its output
contains private deployment identifiers and should remain outside public logs.

## Install the command as your normal user

The installer creates only a small `cisco-voice` wrapper. It downloads nothing,
does not install Python packages, uses no `sudo`, and includes no credentials
or private configuration inside the wrapper. It leaves the existing `cisco`
command and `scripts/cisco.py` unchanged.

From the chosen checkout:

```sh
bash scripts/install-voice-cli.sh --help
bash scripts/install-voice-cli.sh \
  --repo "$PWD" \
  --python "$PWD/.local/voice-venv/bin/python"
export PATH="$HOME/.local/bin:$PATH"
cisco-voice --help
```

`--repo PATH` defaults to the checkout containing the installer, and
`--python PATH_OR_COMMAND` defaults to `python3`. The default destination is
`$HOME/.local/bin/cisco-voice`. Add that bin directory to your shell's PATH in
your usual startup file if you want it in future terminals; the installer does
not edit shell startup files.

Choose either destination override, never both:

```sh
# Installs PREFIX/bin/cisco-voice.
bash scripts/install-voice-cli.sh \
  --python "$PWD/.local/voice-venv/bin/python" \
  --prefix "$HOME/.local/cisco-voice"

# Installs directly into this bin directory.
bash scripts/install-voice-cli.sh \
  --python "$PWD/.local/voice-venv/bin/python" \
  --bin-dir "$HOME/bin"
```

Add the corresponding bin directory to PATH, or invoke the wrapper by its
full path. The selected repository must be a Git checkout root with the
regular `scripts/voice_session.py` file, and the selected interpreter must
report Python 3.11 or newer, or Python 3.10 with the validated timeout extra.

The installer reports `Installed cisco-voice: ...`, or
`Already installed cisco-voice: ...` when an existing executable regular wrapper
is byte-for-byte identical. It refuses any different existing command, a
destination symlink, and a bin directory that is itself a symlink. There is no
overwrite option. Inspect the destination
before deciding what to keep; choose another bin directory, or manually remove
only an old wrapper you have confirmed is yours and intentionally want to
replace. Moving the checkout or interpreter requires that same deliberate
reinstallation. The wrapper reports missing source/interpreter paths instead
of silently selecting another runtime.

The wrapper preserves the caller's working directory and forwards arguments.
It does not change into the checkout. The examples below run from the
repository root so relative private configuration paths resolve consistently.

## Prepare private configuration

Copy the public templates only if the destination files do not already exist:

```sh
umask 077
mkdir -p .local/voice
if [ ! -e .local/phone-voice-transport.json ]; then
  cp examples/phone-voice-transport.example.json .local/phone-voice-transport.json
fi
if [ ! -e .local/voice/config.json ]; then
  cp examples/local-voice-assistant.example.json .local/voice/config.json
fi
```

Edit these private copies. Do not put a token or wake phrase on the command
line or copy private values back into the examples.

In `.local/phone-voice-transport.json`:

- Point `gateway_config` to the existing private gateway JSON, which contains
  the selected phone inventory, AMI access and phone HTTP credentials. This
  is not the small `cisco` client configuration or its API token file.
- Select a configured `line`. It identifies its owning physical phone; it
  does not choose the microphone or place a SIP call.
- Set `bind_ipv4` to an address owned by the machine running this command and
  reachable from the phone. Configure free even RTP ports from 20480 through
  32768 for `bind_port` and `phone_rtp_port`. Do not reuse a documentation
  address or guess the phone's transmit source port.
- Keep `duration_seconds` short for a first trial; the controller permits at
  most 30 seconds including setup. `receive_idle_seconds` permits at most
  10 seconds without accepted microphone audio. Start playback volume low.
  Leave `expected_source_port` null unless a specific source port has been
  established for this phone.

Relative `gateway_config` paths resolve from the caller's current directory,
not from the transport JSON's directory. Running elsewhere therefore requires
an absolute private gateway path as well as explicit config paths. The host
must already have a permitted route to AMI, phone HTTP and the selected media
ports; the installer does not change networking or firewall rules.

In `.local/voice/config.json`:

- Replace `phones` with exactly one key: the selected physical phone's `id`
  from the gateway inventory, with value `true`. This can differ from the
  selected `line` when that line is the phone's second line.
- Set `service.enabled` to `true` only when preparing an authorized trial.
  Keep `rearm_after_turn` false for this foreground workflow. Retain bounded
  utterance and turn settings; a longer HA timeout does not extend the
  transport's 30-second limit.
- Set the private HA `base_url`, an explicitly reviewed Assist `pipeline_id`,
  and a `token_file` path. Put the existing HA token in that private file using
  your editor or secret tooling, with access restricted to your user. A
  gateway token or SIP password cannot substitute for an HA token.
- Start with `wake.mode` set to `manual`. Keep the required `bundle_file` field
  present; it is not loaded in manual mode.

`--workspace` selects the root whose `.local/` contains the HA token and any
ONNX bundle. Their paths are resolved against that root and must remain inside
its `.local/` after resolution, including symlinks. Use `--workspace "$PWD"`
from the repository root to make that choice explicit.

The chosen HA pipeline may use cloud STT or other remote providers. Local
capture and local wake detection do not make that pipeline fully offline, and
a successful conversation can execute an HA intent. Review the pipeline and
use a harmless informational request for the first attended trial.

## Validate first without starting audio

From the repository root:

```sh
cisco-voice \
  --transport-config "$PWD/.local/phone-voice-transport.json" \
  --voice-config "$PWD/.local/voice/config.json" \
  --workspace "$PWD"
```

Omitting `--run` is the dry-run mode; there is no `--dry-run` flag. Success
reports `valid: true`, `network_actions: 0` and `microphone_started: false`.
It reads and validates the transport, gateway and voice JSON, checks the
single permitted phone mapping and checks any display-port argument. It
opens no media socket and makes no phone, AMI or HA request.

This check does not load the HA token, validate an ONNX bundle, import every
optional dependency, test FFmpeg, confirm `service.enabled` is true, or verify
network reachability and firmware behavior. Those runtime gates can still
fail after a valid result. Check dependencies locally with the selected
interpreter and review private assets before authorizing audio.

## Run one attended foreground session

Have an attendant at the chosen phone, confirm both lines are idle, and reserve
the phone's XML audio for this invocation. Do not run radio or another
XML/Component RTP controller concurrently. `--exclusive-media` records that
operator commitment; it does not acquire a cross-process lock or stop other
controllers for you.

After the dry run and preparation, all four flags are required:

```sh
cisco-voice \
  --transport-config "$PWD/.local/phone-voice-transport.json" \
  --voice-config "$PWD/.local/voice/config.json" \
  --workspace "$PWD" \
  --ffmpeg ffmpeg \
  --run --attended --exclusive-media --enable-microphone
```

Manual mode starts the bounded Assist turn after the transport starts; there
is no wake-word gate. This invocation enables capture and TTS playback. Stay
at the phone and speak the prepared request. This CLI does not provide a
playback-only flag; use the separate [transport pilot](voice-transport.md#attended-pilot)
when testing capture and playback independently.

To request the optional status/Stop screen, add `--display-port` with an unused
permitted TCP port from 1024 through 65535. The display binds the configured
`bind_ipv4`, serves only the selected phone's exact source address, and the
controller sends an idle-only page push. Setup or status-update failure
requests capture shutdown. No shared gateway restart or deployment is
performed. See [the display guide](voice-display.md) for screen, refresh and
expiry limits; a page still visible on the phone does not prove audio is live.

For private wake mode, change only the reviewed private configuration to
`wake.mode: "onnx"` and provide its frozen bundle under `.local/`. Follow the
[bundle contract](local-voice-assistant.md#exact-private-onnx-bundle), including
all three models, digests and matching frontend. ONNX mode waits for local
detection within this same short attended session. It is not a background
service, an infinite listening mode or proof of telephony wake accuracy.
There is no automatic rearm after a stop or failure.

## Stop, inspect and keep the result private

Press **Ctrl+C** in the foreground terminal to request cancellation. The
controller attempts remote capture/playback Stop and cancels the Assist turn;
the independent watchdog also handles owned or uncertain streams. Allow
cleanup to finish. A second terminal close or process kill is not a substitute
for confirming that the phone stopped.

The media deadline is not a hardware microphone lease. Cleanup can outlast the
configured session, and the watchdog may need up to 23 seconds for its bounded
passes. A network partition, host failure or unreachable phone can prevent a
confirmed remote Stop. The attendant must verify the phone's state and use
its local controls if needed. Do not start another media controller while
ownership or cleanup is uncertain.

The final JSON contains stage states, input counters, output-frame counts,
the controller result and watchdog fields; it reports `recorded_audio: false`.
`capture_observed` means accepted RTP arrived, and `output_frames` counts sent
audio frames. Neither proves speech recognition quality, audible playback or
physical microphone closure. Inspect `result.reason`, `result.stop_confirmed`
and any `watchdog_stop_confirmed`, `watchdog_finished` and stop-error fields.
No recording or transcript log is created by this command, but the configured
HA providers have their own data handling.

Exit zero requires a completed turn and a confirmed Stop path in the current
CLI. A deliberate Ctrl+C can return nonzero even after successful cleanup;
an error or nonzero result is not permission for an automatic retry. Keep run
output under `.local/` if retaining it and record physical observations
separately. The complete acoustic HA conversation path, sustained wake accuracy,
Mute/Stop and both-line call coexistence remain acceptance work beyond the
bounded capture, wake and playback pilots.
