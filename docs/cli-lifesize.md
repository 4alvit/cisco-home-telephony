# LifeSize Phone in the Cisco CLI

A first-generation LifeSize Phone can register directly with Asterisk over SIP.
It uses its own automation SSH interface, not Cisco XML/SEP profiles. Registration
alone does not add it to the Cisco gateway's inventory. Without the optional native
driver, selecting its number returns `line_not_in_cisco_gateway_inventory`.

## Private configuration

Install all seven CLI modules from the same revision, using the instructions below.
Merge the `lifesize` object from
[`cisco-cli.lifesize.example.json`](../deploy/cisco-cli.lifesize.example.json) into
your existing private `~/.config/cisco-home/cli.json`; this example is a fragment,
not a replacement for the gateway/token settings. Keep the file mode at `0600`.
Use your phone's registered extension as the dictionary key and its private IPv4
address as `host`. Do not put it in the Cisco SEP/MAC provisioning list.

The phone's `auto` account must already accept the configured SSH public key.
Use a dedicated `known_hosts_file` containing its verified host key. Relative
key paths are resolved beside the CLI configuration. The private key must be
owned by your user and have no group/other permissions. The known-hosts file
must be owned by your user and not writable by other users. Encrypted keys that
require a prompt or agent are not supported by this unattended driver.

For legacy firmware that requires RSA/SHA-1 SSH signatures, explicitly set
`legacy_rsa` to `true`. This changes only these phone connections, not global SSH
settings. After an address change, optional `host_key_alias` can retain the old
verified known-hosts label. No password is stored in the CLI configuration.

## Commands and capability boundaries

The native driver controls the LifeSize conference speakerphone. Its settings
apply to the physical phone, even when a command starts with one SIP extension.

| Function | Command | LifeSize behavior |
| --- | --- | --- |
| Live state | `cisco 222` or `cisco 222 status` | Read registration, calls and native settings. |
| Directory | `cisco 222 directory` | List configured endpoints without probing every phone. |
| Outgoing call | `cisco 222 555` or `cisco 222 call 777` | Dial an allowed destination from the conference speakerphone. |
| Answer | `cisco 222 answer` or `cisco 222 pickup` | Answer one observed incoming alerting call in audio mode (`Ring Incoming`, `Ringing` or `Ringback`). |
| Reject | `cisco 222 reject` | Reject one observed incoming alerting call (`Ring Incoming`, `Ringing` or `Ringback`). |
| Hang up | `cisco 222 hangup` | End one observed call using its exact handle. |
| Ringer level | `cisco 222 volume ringer status` | Read the native ring-tone level, on a 0–10 scale. |
| Set ringer | `cisco 222 volume ringer set 5` | Set the absolute native ring-tone level. |
| Speaker level | `cisco 222 volume call status` | Read the native conference-speaker level, on a 0–100 scale. |
| Set speaker | `cisco 222 volume call set 60` | Set the absolute conference-speaker level. |
| Relative level | `cisco 222 volume call down --steps 2` | Adjust the selected native level by two scale units. |
| Microphone | `cisco 222 mic mute`, `mic unmute`, `mic status` | Control local outgoing audio; this does not mute the speaker. |
| Auto-answer | `cisco 222 autoanswer`, `autoanswer on`, `autoanswer off` | Read or change the phone's native incoming-call answer preference. |
| Auto-mute | `cisco 222 automute`, `automute on`, `automute off` | Read or change whether answered incoming calls start with the microphone muted. |
| Do Not Disturb | `cisco 222 dnd`, `dnd on`, `dnd off` | Read or change protection against interruptions during one connected call. |
| Text popup | `cisco 222 message 'Dinner is ready' --title Notice` | Request a 15-second native popup while the phone is idle. |

`volume call` addresses the conference speaker. It does not select a handset or
headset, and does not claim that those audio paths exist. These native levels
are separate from the Cisco gateway's digital music gain. Likewise, `mic mute`
mutes outgoing microphone audio; the existing `mute`/`unmute` music commands are
not microphone controls and are not exposed by this LifeSize driver.

Read-only setting queries work without an active call. Changing speaker volume,
the microphone or DND requires exactly one connected call. Changes to the ringer,
auto-answer and auto-mute require an idle phone. Relative volume steps use
native integer units and stop at the scale's minimum or maximum.

On the verified first-generation Phone firmware (`LS_PH1_4.5.2`), native DND
protects an **existing call** from interruptions. It is not a global block on
incoming calls while the phone is idle. An idle DND write can be acknowledged
without changing the reported
value; the driver therefore permits a changed value only during one connected
call and verifies it by reading it back. To silence the idle ringer, use
`cisco 222 volume ringer set 0`; this does not reject incoming calls or disable
auto-answer.

Auto-answer changes the phone's native preference, which remains in effect after
the CLI exits until it is changed again. It is not a one-call intercom request.
The Cisco gateway's rule that timers and alarms always require manual answer
does not apply to this native setting: a call reaching the LifeSize may be
answered automatically. Keep `autoanswer off` when an incoming call must keep
ringing. Reboot persistence is not part of the CLI's confirmation; reread the
setting after a restart.

The phone also has a separate native **auto-mute** preference. When enabled,
an answered incoming call can start with its microphone muted; this preference
is not limited to auto-answer. Changing
`autoanswer` does not change auto-mute. `automute off` explicitly changes this
preference for future calls while the phone is idle. To change only a connected
call, check `cisco 222 mic status` and use `cisco 222 mic unmute` when you intend
to transmit audio. The preference and the live microphone state are distinct.

For example:

```sh
cisco 222                 # SIP registration, active calls and available commands
cisco 222 555             # Dial the local time service from the LifeSize speaker
cisco 222 call 777        # Dial the configured music service
cisco 222 777 --dry-run   # Validate without placing a call
cisco 222 hangup          # End the one observed call by its exact handle
cisco 222 directory      # Include the native phone alongside Cisco phones
cisco 222 volume ringer status
cisco 222 volume ringer set 5
cisco 222 volume call status
cisco 222 mic status
cisco 222 autoanswer off
cisco 222 message 'Dinner is ready' --title Notice
# During one connected call:
cisco 222 volume call down --steps 2
cisco 222 mic unmute
```

Dialing still requires a fresh gateway inventory and a destination in its
`dial_allowlist`. Before controlling the phone, the driver reads its SIP username
and requires it to match the selected extension. It refuses dialing a busy or
unregistered phone, and refuses hangup if zero or multiple calls are present.
Local per-device locking prevents concurrent commands from this Mac. It cannot
lock out a person using the phone or another controller.

Settings are read back after a change; confirmation means that the reported
native value matches the requested value. Setting an already-matching value
does not send another write. It does not prove audible loudness or the behavior
of a future incoming call.

Microphone and speaker changes also recheck the call afterward. If the call
changes during the operation, the result is `not_confirmed`; the CLI does not
retry or roll back a setting against a different call.

`accepted` means the phone acknowledged a call command, not that the other party
answered or audio was heard. On a lost SSH response the CLI reports
`not_confirmed`; inspect `cisco 222` before trying again. There is no automatic
retry or durable idempotency support; mutation commands reject an explicit
`--idempotency-key`. Directory listings do not probe native phones and show
their reachability as `not checked`; selecting the number reads live state.

### Native text popup

Use `message TEXT [--title TITLE]`, or use a private text file to keep the body
out of shell history:

```sh
cisco 222 message --text-file notice.txt --title Notice
```

The popup lasts 15 seconds and requires an idle phone. The title must contain
1–32 characters. The combined `title + ': ' + text` must contain at most 200
characters. These are conservative **client limits**, not a claim about the
firmware's maximum text length.

Both fields accept ASCII letters, digits, spaces and this punctuation:
`. , ! ? ( ) : _ + / @ ' -`. A straight apostrophe is allowed. Double quotes,
backslashes, dollar signs, backticks, semicolons, embedded newlines and other
characters are rejected because the native interface has no verified escaping
contract. A text file must be a regular file of at most 1,024 bytes; only trailing
CR/LF characters are removed, and its remaining content must satisfy the same
rules. The body cannot be empty or contain only spaces.

An accepted message confirms the native command acknowledgement. The CLI has
no screenshot readback and does not prove that the popup was visible. This uses
the LifeSize text-popup command; it does not add Cisco XML services or image
push support.

Cisco image/XML screen pushes, timers, alarms, one-call intercom, voice sessions, arbitrary
speaker-key toggles and controlled `play` queues are not exposed for LifeSize.
Music service numbers can still be dialed normally. For finite spoken calls,
use the separate [announcement service and `say` command](cli-announcements.md).
It can include a configured LifeSize phone without changing its persistent
auto-answer preference. Installing the local native driver alone does not create
a Home Assistant device or install the announcement service.

## Verified behavior

Acceptance on a first-generation Phone running `LS_PH1_4.5.2` covered separate
ringer/speaker level changes with readback, native auto-answer of an Asterisk
call, the incoming microphone-mute preference, live microphone mute/unmute,
in-call DND, manual audio answer, exact-call hangup and rejection of a ringing
call. Tests restored the original preferences afterward. Setting confirmation
is a native-value readback; it is not a calibrated loudness measurement.

This firmware reports a pending call as `Ring Incoming`, with ten status fields
before the elapsed-time field appears. The driver also handles interleaved
incoming-call and mute notifications. If an older CLI returns
`invalid_lifesize_calls` during setup, update the complete seven-module bundle.

## Install or update the local CLI

Use a reviewed checkout containing these controls. The command below installs
the **committed `HEAD`**, so commit or select the intended revision first; it
does not install uncommitted edits. No `pip` dependencies are required for the
native LifeSize driver: it uses Python 3 and the system `ssh` client.
Let local CLI commands finish before updating the entry point.

Run from the repository root:

```sh
git rev-parse HEAD
python3 -m unittest discover -s tests -p 'test_cisco_lifesize.py'
python3 - <<'PY'
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile

names = (
    'cisco.py', 'cisco_http.py', 'cisco_library.py',
    'cisco_lifesize.py', 'cisco_media_source.py', 'cisco_player.py', 'cisco_announce.py',
)
revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
contents = {name: subprocess.check_output(
    ['git', 'show', f'{revision}:scripts/{name}']) for name in names}
releases = Path.home() / '.local/share/cisco-home-cli/releases'
releases.mkdir(parents=True, exist_ok=True)
link = Path.home() / '.local/bin/cisco'
link.parent.mkdir(parents=True, exist_ok=True)

def current_target():
    if link.is_symlink():
        return os.readlink(link)
    if link.exists():
        raise SystemExit('Refusing to replace a regular file at the CLI path.')
    return None

previous = current_target()
release = releases / revision
stage = Path(tempfile.mkdtemp(prefix=f'.{revision}.', dir=releases))
temporary_link = link.with_name(f'.cisco-{secrets.token_hex(8)}')
try:
    for name, data in contents.items():
        compile(data, name, 'exec')
        path = stage / name
        path.write_bytes(data)
        path.chmod(0o755 if name == 'cisco.py' else 0o644)
    subprocess.run([sys.executable, '-B', str(stage / 'cisco.py'), '--help'],
                   check=True, stdout=subprocess.DEVNULL)
    if release.exists():
        if any((release / name).read_bytes() != data for name, data in contents.items()):
            raise SystemExit('Existing release differs; leaving the current CLI unchanged.')
    else:
        os.replace(stage, release)
    if current_target() != previous:
        raise SystemExit('The CLI link changed during installation; inspect it before retrying.')
    temporary_link.symlink_to(release / 'cisco.py')
    os.replace(temporary_link, link)
    print('Installed revision:', revision)
    print('Previous target:', previous or '(none)')
finally:
    if stage.exists():
        shutil.rmtree(stage)
    if temporary_link.is_symlink():
        temporary_link.unlink()
PY
export PATH="$HOME/.local/bin:$PATH"
cisco --help
cisco 222
```

Keep the printed previous target for rollback. Each release contains all seven
modules; the atomic symlink change selects the complete bundle for subsequent
commands without overwriting the previous release. The procedure leaves
`~/.config/cisco-home/cli.json`, SSH keys and known-hosts files
untouched. Do not replace the private configuration with the example fragment.

To roll back, create a temporary symlink beside `~/.local/bin/cisco` pointing to
the recorded previous target, then replace the `cisco` symlink with
`os.replace(temporary_link, link)` as above. Keep both release directories until
verification is complete. A local CLI update does not require restarting
Asterisk, Home Assistant or the phone.

## Troubleshooting

- `lifesize_client_not_installed`: install the full seven-module bundle from one
  revision, including `cisco_lifesize.py`.
- `line_not_in_cisco_gateway_inventory`: check that the native `lifesize` map in
  the private configuration contains the selected registered extension.
- `lifesize_sip_line_mismatch`: the phone reports a different SIP username;
  correct the private mapping rather than disabling the check.
- A setting command is refused or its outcome is unknown: read `cisco 222`
  before retrying. Acknowledging a native command and verifying its final value
  are separate steps; an interrupted readback is not evidence that nothing
  changed.
- `command_not_supported_by_lifesize`: consult the capability table above. A
  command being available for Cisco phones does not make it available for
  LifeSize.

Protocol reference: manufacturer's
[LifeSize Automation Command Line Interface](https://www.dekom.com/fileadmin/pdf/Lifesize_automation_command_line_interface_de.pdf),
including the call-control, volume, microphone-mute and incoming-call settings.
