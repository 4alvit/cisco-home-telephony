# Bounded voice session controls

`phone_voice_control.py` manages one attended voice session per physical phone.
It launches the reviewed [foreground runner](voice-cli-install.md), retains
ownership across separate status and Stop requests, and accepts only private,
operator-configured profiles. The initial version supports manual activation.
It does not create a persistent wake listener or change the phone's SIP setup.

The controller runs inside the existing authenticated phone gateway. The
gateway, `cisco LINE voice` command, phone menu and companion Home Assistant
service share this session owner. Voice remains disabled unless the gateway
is explicitly started with a private voice registry. Updating client code alone
does not enable a deployed gateway or start a microphone.

## Prepare an explicit private registry

Use the same stable checkout and selected Python environment described in the
[CLI installation guide](voice-cli-install.md). The interpreter needs the live
HA transport and TTS dependencies, including `aiohttp`, plus FFmpeg and Pillow
when using the display. Python 3.11 or later is recommended; a supported Python
3.10 environment also needs the conditional `async_timeout` dependency.

Each profile points to the existing private transport and voice configuration.
The transport configuration selects a line; its gateway configuration resolves
that line to the physical phone. Both lines share one voice owner. The voice
configuration must enable only that physical phone, use manual mode, and disable
rearming. Duration must be at most 30 seconds. A profile cannot supply a command,
shell fragment, remote URL, or arbitrary process arguments.

The registry has this shape; all paths and identifiers below are examples:

```json
{
  "version": 1,
  "enabled": false,
  "state_directory": ".local/voice-control",
  "python_executable": "/absolute/stable/checkout/.local/voice-venv/bin/python",
  "ffmpeg_executable": "/absolute/path/to/ffmpeg",
  "profiles": [
    {
      "transport_config": ".local/voice-phone-a-transport.json",
      "voice_config": ".local/voice-phone-a-assistant.json",
      "display_port": 18090
    }
  ]
}
```

Keep the registry and referenced private files under the selected workspace's
`.local` directory with owner-only permissions. Choose unused RTP and display
ports. Omit `display_port` when the optional display is not configured. Keep
`enabled` false while preparing and validating the registry; enabling it does
not start a session by itself.

Set `ffmpeg_executable` to the installed executable's absolute path. For example,
a Homebrew installation may use `/opt/homebrew/bin/ffmpeg`, while a Linux image
may use `/usr/bin/ffmpeg`. The controller validates and passes this path explicitly;
the child does not search the operator's shell PATH.

Profiles must use distinct local RTP receive endpoints: the same
`(bind_ipv4, bind_port)` pair cannot appear twice. Optional display listeners
must likewise use distinct `(bind_ipv4, display_port)` pairs. The loader rejects
these collisions across profiles, including profiles for different phones.
It also checks the display port against the profile's configured service and
media ports. These checks do not establish that another running process has
left a port free; verify actual port availability before enabling the adapter.
The loaded local RTP bind address and port are fixed for that profile and are
checked again during Start preparation and dry-run validation. Editing them
under a running controller does not redirect the next session silently.

Validate the registry locally, without contacting the gateway or phone:

```sh
python3 containers/asterisk/phone_voice_control.py validate \
  --config .local/voice-control.json --workspace "$PWD"
```

Validation reads the referenced private files, including the HA token. It does
not start a child, reserve media, check interpreter imports, or prove that the
configured network addresses are reachable. The result reports `valid`,
`enabled`, the profile count, and no microphone or network actions.

To attach the registry, supply both voice options when starting the existing
gateway from a checkout with its runtime dependencies installed:

```sh
python3 containers/asterisk/phone_gateway.py \
  --config .local/gateway.json --state-directory .local/gateway-state \
  --voice-control-config .local/voice-control.json --voice-workspace "$PWD"
```

The registry's physical phone identities must match the gateway inventory.
Without these options the ordinary gateway continues without voice controls.
An invalid registry prevents startup; it is not partially enabled.

The source layout must include `containers/asterisk`, `scripts/voice_session.py`
and `voice_assistant` under the same root. Copying the controller next to a
standalone renamed gateway script is insufficient. A container can keep these
sources under `/workspace` and use a separate writable private workspace.
All referenced private paths must then resolve under that workspace's `.local`
directory. Kubernetes projected Secrets use symlinks: copy their contents into
ordinary owner-only files before loading the registry rather than pointing
the controller at the projection. Keep state persistent across replacements,
with the same runtime user and a shutdown grace longer than 47 seconds.

The selected interpreter and source checkout must remain at their configured
paths. The controller creates immutable private snapshots for its child runner
and bounds captured output. Do not copy these snapshots into public artifacts
or delete active state to work around an ownership refusal.

## Start, status and Stop

A Start request must explicitly confirm all three conditions:

- An attendant is physically present at the selected phone.
- The phone's XML media path is reserved for this session.
- Microphone capture is authorized for this bounded attempt.

The controller does not infer these confirmations from a previous request,
opening a menu, or loading a configuration. The runner independently checks
the phone before starting media. A busy, ringing, unavailable or uncertain
phone is not an invitation to retry until it happens to start.

Status and Stop use the configured phone mapping, so they remain available
when a fresh AMI inventory cannot be obtained. They still require authentication
and authorization. A Stop request includes the exact session ID returned by
Start or status. A stale Stop must not affect a later session, even when it names
the same line. Repeating Stop for the same session is safe; it does not start
another session or repeat a Start. An old session that is no longer retained
can be reported as unknown; that response does not authorize stopping another
session.

Replay suppression is bounded. The controller retains up to 128 request/session
history entries in memory, and only the latest per-phone journal survives a
restart. An older completed request ID can therefore outlive its replay record.
Use a new unique request ID only for a deliberately new attempt. Never schedule
a delayed automatic retry, even with the original ID, or reuse an old ID for a
different attempt. Inspect current status and resolve uncertainty first.

After the updated gateway is installed and enabled, the ordinary `cisco` command
uses the existing private [Cisco client configuration](cli-installation.md).
The following line and request ID are synthetic; substitute the configured line
and choose a new request ID for each deliberately new attempt:

```sh
cisco --config .local/cisco-cli.json 201 voice status

cisco --config .local/cisco-cli.json 201 voice start \
  --idempotency-key voice-pilot-example-001 \
  --attended --exclusive-media --enable-microphone --dry-run
```

The Start dry run contacts the authenticated control API but does not launch a
runner or reserve a session. It requires the registry to be enabled and the
same explicit gates as a real Start. Its no-network-actions result describes
downstream session activity, not the client HTTP request. It does not prove
that the phone is idle or that a subsequent Start will be accepted.

For an explicitly attended real attempt, use the same Start command without
`--dry-run`. Record the returned session ID. Stop targets that exact ID:

```sh
cisco --config .local/cisco-cli.json 201 voice stop \
  --session-id 0123456789abcdef0123456789abcdef
```

The displayed ID is an example, not a valid local session. Run status again to
observe cleanup. Exiting the client or pressing Ctrl+C in the client does not
stop the gateway-owned session; send its explicit Stop request.

`voice stop --dry-run` only reads status and checks the supplied session ID;
it does not request Stop. `--json` returns sanitized structured metadata.
The standalone `phone_voice_control.py` client remains available with
`status|start|stop --client-config PATH --line LINE`; its Start request ID
flag is `--request-id`.

Treat an accepted Start as a control-plane result, not proof that capture or
speech recognition succeeded. The runner currently reports its detailed result
at completion; process status alone cannot truthfully identify Listening,
Thinking or Speaking. The optional [phone display](voice-display.md) has its own
session state and physical acceptance requirements.

Every response exposes a bounded status object with `schema_version`, `phone`,
`line`, `session_id`, `state`, `active`, `remaining_seconds`, `stop_requested`,
`stop_confirmed` and `error`. `phone` is the canonical physical identity; `line`
is the original session's Start alias, or the queried alias when idle. A status
request through the sibling line does not change an existing session's alias.
The profile's configured transport line still selects the runner's media
configuration. Responses exclude credentials, private paths,
process arguments, transcripts, audio and raw child output. Error values are
fixed codes, not exception text.

States are `idle`, `starting`, `running`, `stopping`, `stopped`, `cancelled`,
`failed` and `recovery_required`; `external_owner` is reserved. `cancelled` means
the attempt was cancelled before its runner launched. `failed` is not a
successful conversation. Read `stop_confirmed` separately: even `active: false`
does not prove physical microphone closure when recovery is required.
Assistant errors, input timeouts, transport failures and lease expiry remain
`failed` even when their media cleanup is confirmed. Deliberate Stop and shutdown
can report `stopped`; that state alone does not claim a completed conversation.

## Deadlines and uncertain cleanup

The controller applies an external lease of at most 30 seconds and requests
runner shutdown with SIGTERM when that lease expires. It allows a further
45 seconds for cleanup. If forced termination is necessary, it targets only
its own runner process; it does not kill the watchdog's process group.
This is additional supervision, not a hardware-enforced microphone lease.

Stop acknowledgement, process exit and physical microphone closure are different
observations. The controller checks the runner's structured cleanup and watchdog
evidence. A nonzero exit code can follow an explicit user Stop; it is not by
itself evidence that Stop failed. Conversely, process exit alone does not prove
that the phone accepted Stop.

Missing or unconfirmed evidence leaves persistent `recovery_required` state.
There is no automatic retry or automatic restart into capture. A new Start must
not replace an active, stopping or unresolved session. State survives separate
requests and controller restarts so a restart cannot silently forget an
uncertain microphone session.

Current status also checks for a newer per-phone journal written by another
controller. An existing process can report that newer session instead of its
old completed cache, but it cannot signal the other controller's child.
A journal indicating unresolved ownership is conservatively shown as
`recovery_required`, with no confirmed Stop; it is not proof of current audio
activity. An explicit query for a retained older session still returns that
session's history. Send Stop through the controller that owns the session;
if that owner is unavailable, use the physical recovery procedure rather than
adopting or signalling a process by PID.

When recovery is required, inspect the physical phone and the private session
evidence. Use the phone's local controls if remote closure is uncertain. Do not
delete ownership files or restart the gateway merely to clear the refusal.
This version does not promise unattended recovery. A displayed status page may
remain visible after the process stops; server TTL does not dismiss it.

## Gateway and interface integration

The gateway retains the existing exact-source-IP and Bearer-token checks.
Every controller operation uses a server-created request context and the
canonical physical phone ID. The controller's authorization callback is
mandatory; request JSON cannot manufacture a trusted context.

The embedding API is `load_control_config(path, workspace)` followed by
`VoiceControl(config, authorize=callback)`. The callback receives
`(context, action, canonical_phone_id)` and must return exactly `True` to allow
an operation. Actions are `start`, `status` and `stop`. Pass a trusted context
to each call; the controller resolves line aliases before authorization.
`start(target, request_id, ...)` accepts the three explicit gate booleans and
an optional `dry_run`; `status(target, ..., session_id=None)` reads state;
`stop(target, session_id, ...)` targets one session.

The default monitor supervises deadlines without starting media by itself.
An embedding that sets `monitor=False` must call `tick()` regularly.
`close()` rejects new Starts and requests cleanup, but returns without waiting
for all children to finish. Keep supervision alive during shutdown; do not
equate a returned `close()` call with confirmed microphone closure.

The existing client transport uses these flat gateway action routes:

- `GET /v1/phones/<line-or-primary>/voice_status` reads controller status.
- `POST /v1/phones/<line-or-primary>/voice_start` requests one bounded session.
- `POST /v1/phones/<line-or-primary>/voice_stop` stops the exact supplied session.

The server resolves either assigned line to one configured physical phone.
It rejects unknown fields and conflicting identities. Start carries a stable
idempotency key and explicit gate booleans. Stop carries a session ID. Mutation
responses preserve the existing `status: "accepted"` and `action_id` envelope
around the sanitized controller result. Status requests do not start media.

These routes bypass the existing call-mutation AMI prerequisite, so loss of
AMI does not prevent Stop from reaching an owned runner. The gateway does not
hold its general lock while waiting for a child or cleanup. The voice ownership
lock covers the complete session lifecycle, including cleanup.

The `cisco` client exposes `voice status`, `voice start` and `voice stop` through
its existing direct or explicit `kubectl` transport and owner-only client
configuration. Its voice path branches before the normal fresh-inventory
request. The controller's standalone client uses that same configuration;
neither client owns the remote session merely because its command exits.

The phone's **Home Phone Apps** menu uses the existing per-phone listener,
exact phone source check and opaque application path. Opening Voice shows status
only. Start uses the existing expiring, single-use confirmation flow and clearly
states that the microphone will be used for a bounded session. Stop captures
the exact session ID shown to the user. Selecting the sibling line never creates
a second physical owner. Use explicit Refresh to update the displayed result;
the menu does not poll or repeat microphone commands automatically.

The companion `cisco_phone_control` component exposes an administrator-only
`voice` response service through its existing authenticated gateway client.
It accepts `entry_id`, `phone`, `line`, and `operation`:
`status`, `start` or `stop`. Start additionally requires a caller-retained
`request_id` and all three explicit gate booleans; Stop requires `session_id`
and its own request ID for response correlation.
Start supports `dry_run`. The service uses the last known phone mapping without
requiring a new AMI snapshot. It returns bounded session metadata, including
uncertain cleanup states. It does not add a schedule or an automatic Start button.
The integration must already be loaded with a known phone mapping. A Home
Assistant restart during an AMI outage can prevent that initial load; use the
gateway CLI or the phone's Voice menu for Stop in that case.

## Validation and acceptance

Registry validation and a Start dry run are preparatory checks. They do not
prove device readiness, audio quality, a working HA pipeline or physical Stop.
Do not run a live Start during installation or automated smoke tests.

Before enabling the integrated feature, verify with synthetic inputs that:

- Both line aliases share one owner; concurrent or repeated Start requests
  cannot launch two runners.
- Unknown sources, invalid tokens, foreign session IDs and missing Start gates
  are rejected without launching a process.
- Duplicate Start keys preserve the original operation; a stale Stop cannot
  terminate a replacement session.
- Stop and status work during AMI failure; lease expiry, child failure,
  controller restart and uncertain cleanup retain the correct ownership state.
- Child output and private files stay bounded; public responses contain no
  private paths, credentials, audio, transcripts or raw exceptions.
- Gateway, CLI, phone menu and HA adapters use the same session identity and
  do not introduce implicit retries or recurring capture.

Then conduct one explicitly attended physical acceptance run: confirm both
lines are idle, start the bounded session, inspect capture and reply playback,
request Stop, and verify physical closure. Separately verify incoming-call
priority, active-call and radio refusal/preemption, stale-screen behavior and
the recovery procedure after lost connectivity. Earlier RTP capture/playback
results are a baseline, not acceptance of this new operator layer or an
unattended voice assistant.
