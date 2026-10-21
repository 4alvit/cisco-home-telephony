# Voice sessions and hands-free controls

For normal use, start with the [phone and CLI quickstart](voice-quickstart.md).
This page describes configuration, ownership and recovery.

![Finite voice session and explicit Stop](diagrams/voice-operator-flow.svg)

`phone_voice_control.py` manages one attended voice session per physical phone.
It launches the runner selected by the private transport profile, retains
ownership across separate status and Stop requests, and accepts only private,
operator-configured profiles. Manual mode handles one request; wake mode can
return to listening after a successful reply within the same finite session.
Finite Start does not enable boot activation or change the phone's SIP setup.
An explicitly enabled hands-free policy can supervise successive finite SIP
sessions and survive a clean gateway restart. It is off by default.

The controller runs inside the existing authenticated phone gateway. The
gateway, `cisco LINE voice` command, phone menu and companion Home Assistant
service share this session owner. Voice remains disabled unless the gateway
is explicitly started with a private voice registry. Updating client code alone
does not enable a deployed gateway or start a microphone.

The [foreground runner](voice-cli-install.md), XML/RTP port and display examples,
and watchdog-specific recovery details on this page describe the stock
`xml_rtp` transport. For opt-in `sip_audiosocket`, follow the
[SIP speakerphone setup and cleanup guide](sip-voice-contingency.md): use the
same gateway owner, an ONNX wake profile and no `display_port`. Start and Stop
can be remote; no handset button is required. The shared status and exact-session
control API below still applies, and physical qualification remains separate.

## Hands-free policy and status

Hands-free requires a SIP AudioSocket profile with ONNX wake detection and
`rearm_after_turn: true`, with a configured session length of 30–300 seconds.
Registry capability enablement and persistent operator
intent are separate. Loading a capable registry alone does not authorize capture.
The deliberate Enable operation grants persistent microphone operation through
the same physical-phone owner, shared by both line aliases. The gateway runs
one supervisor; no recurring CLI process is needed.

```sh
cisco --json 201 voice enable --idempotency-key handsfree-example-001
cisco --json 201 voice status
cisco --json 201 voice disable --idempotency-key handsfree-disable-001
cisco --json 201 voice status
```

Replace the example line and keys. Enable and Disable generate a fresh key once
when none is supplied. `voice enable` supplies the required consent booleans;
it does not use `--attended`, `--exclusive-media` or `--enable-microphone` flags.
Enable supports `--dry-run`, which validates eligibility without changing intent
or starting media. Disable has no dry run and needs no session ID.

Disable stores disabled intent before requesting Stop, including between
sessions and during startup/backoff. A valid finite `voice stop` of the **current
supervised session** also disables that intent. Repeating a historical Stop
cannot disable a newer persistent generation; `voice disable` is phone-wide.
Finite Start is refused while hands-free is enabled; use
Disable first. Replaying an old Enable cannot undo a newer Disable. After a
lost acknowledgement, inspect status before considering another mutation.

The CLI's `voice status` reads finite status and hands-free status. The two API
objects remain separate; `hands_free` is not added inside the strict `voice`
object. Hands-free status contains only:

- `schema_version`, `phone`, `line`, `enabled` (durable desired state);
- `state`, `session_id` (owned inner session or null), `session_seconds`;
- `consecutive_failures`, `retry_after_seconds`, and fixed-category `error`.

`phone` is the canonical handset and `line` is the effective line selected by
Enable. Querying a sibling alias does not change that selection. Ordinary status
also works for an ineligible profile; Enable and its dry run refuse that profile.

Its states mean:

- `disabled`: automatic starts are disabled; check any former session's cleanup
  separately before claiming its microphone is off.
- `waiting_idle`: waiting for all lines to be registered and idle, without phone
  channels. It is not proof of a particular cause such as an ordinary call.
- `starting`: admission/preparation is in progress.
- `running`: an inner session is running, not necessarily ready for a wake phrase.
- `stopping`: owned-session shutdown is in progress.
- `backoff`: waiting before a bounded retry after a clean transient failure.
- `faulted`: no automatic retry; resolve the cause and explicitly Enable again.

To display **Ready for wake phrase**, require enabled intent, policy
`state: running` with no error, the same non-null `session_id` in both objects,
and a finite session with `state: running`,
`active: true`, `error: null`, both Stop flags false, positive `remaining_seconds`,
`phase: armed` and `phase_reason: wake_ready`. Do not combine a newer hands-free
session with a stale finite phase. Missing fields or a changed ID are not ready.
Absence of readiness does not prove microphone closure: capture can already be
active during preparation, while uncertain cleanup needs separate recovery.

That tuple applies to raw Gateway responses. In the output of
`cisco --json LINE voice status`, use top-level `hands_free_listening: true`:
the CLI evaluates the full tuple using the original Gateway fields.
Its finite `voice` object omits null `error` values, so do not interpret an
omitted error field as independent readiness evidence. The separate
`voice_wake_ready` field covers only the finite session and does not establish
that the hands-free policy is enabled and owns that same session.

Each inner session retains its configured limit of at most 300 seconds and its
existing native deadline, reservation and cleanup proof. Intentional renewal
requests exact Stop of the owned session five seconds before its deadline, then
requires confirmed terminal cleanup and two fresh idle observations separated
by a quiet period; SIP admission checks races again. There is no overlap or forced lease
clearing. Call setup and warmup can leave gaps in capture; no fixed renewal
latency or memory across new session IDs is promised.

Only positively observed competing-call preemption permits resumption after
idle. Peer hangup, DTMF Stop, explicit Stop of the current supervised session
and ambiguous call changes disable intent. A generic `call_changed` outcome is not sufficient
proof of an ordinary call and must not authorize automatic renewal.

Transient clean failures use bounded exponential backoff and a consecutive-
failure limit. Unconfirmed cleanup, uncertain ownership, invalid configuration
or exhausted retries fault without further automatic attempts. Unknown or
unavailable observations do not start capture; repeated observation failures
are bounded. A short successful turn does not reset failure limits; clean full
lease/renewal completion does.

Shutdown prevents new starts before stopping owned sessions. Enabled intent
can survive a clean gateway restart, but an unresolved journal fails closed
and is never erased automatically. A confirmed phone hangup, DTMF Stop or
ambiguous call ending disables intent even if gateway shutdown was requested
at the same time; restarting the service must not undo that stop.
A disabled intent and an unconfirmed Stop
must be shown together; neither an empty channel list nor process exit proves
physical microphone closure.

### Unconfirmed ordinary calls

Before dispatching an ordinary call, the gateway saves a durable pending-call
record and pauses hands-free renewal. A successful dispatch acknowledgement
does not prove that the call has appeared or finished. If no positive call
observation arrives within 30 seconds, the policy faults with
`hands_free_recovery_required` and retains the record. A lost response after
dispatch can also leave this uncertainty; restarting the gateway preserves it.

Disable still removes hands-free intent, but it does not cancel or prove the
absence of a previously queued ordinary call. A fresh Enable remains refused
while that pending record is unresolved. Investigate the original operation
using gateway and PBX evidence; do not repeatedly place calls, delete the record
or clear ownership to force renewal. The supervisor can reconcile the record
after positively observing a call followed by two fresh idle observations
separated by the quiet period. If the policy already faulted, reconciliation
does not re-enable it: check status and deliberately Enable again only after
the pending operation is resolved.

### Hands-free API

The authenticated endpoints are:

- `GET /v1/phones/<line>/voice_hands_free_status`: no body; reads policy status
  without phone or network activity initiated by the status handler.
- `POST /v1/phones/<line>/voice_hands_free_enable`: `idempotency_key`, literal
  `enable_microphone: true`, literal `exclusive_media: true`; optional `dry_run`.
- `POST /v1/phones/<line>/voice_hands_free_disable`: `idempotency_key` only.

Responses retain the `status`/`action_id` envelope with a separate `hands_free`
object. The underlying finite session API and its exact-session identity checks
remain in place. See the [operator quickstart](voice-quickstart.md#enable-hands-free-mode)
for the shorter workflow.

## Shared wake phrase admission

The optional `wake.phrase` field lets ONNX wake turns use Home Assistant’s native
duplicate-wake filter. Add it to the existing **private** wake configuration,
preserving the bundle and every other field, only after the participating
integrations agree on the transmitted phrase:

```json
{"wake": {"mode": "onnx", "bundle_file": ".local/wake-bundle.json", "phrase": "Hello Example"}}
```

This synthetic example is a configuration fragment, not a complete profile or
a change to the trained wake model. The optional phrase must be nonempty text
of at most 128 characters, without leading/trailing whitespace or ASCII control
characters. It is accepted only in ONNX mode. No case, punctuation or Unicode
normalization is applied.

Home Assistant 2026.10 matches the **exact string** within a two-second window.
The first admitted request wins, regardless of microphone distance. A matching
later detection in that window is quietly discarded; with rearming enabled,
Cisco returns to listening on the same still-valid stream after its cooldown.
It does not fabricate a reply or completed turn. Stop and lease limits still
apply. After the two-second window, another device can start a request while
the first conversation is still active: this is not a whole-conversation lock,
nearest-microphone selection or cross-HA coordination.

A coherent Show/Echo setup requires the VACA integration to forward Wyoming
`Info` so Core resolves model IDs to the same phrase, plus the EchoMuse update
that handles native duplicate rejection quietly. Matching visible labels alone
do not prove matching wire values. Keep real phrases and device mappings
private, and verify those updates are loaded before setting Cisco’s phrase.
These are protocol capabilities, not a claim of deployment or acoustic success.

## Prepare an explicit private registry

Use the same stable checkout and selected Python environment described in the
[CLI installation guide](voice-cli-install.md). The interpreter needs the live
HA transport and TTS dependencies, including `aiohttp`, plus FFmpeg and Pillow
when using the display. Python 3.11 or later is recommended; a supported Python
3.10 environment also needs the conditional `async_timeout` dependency.

Each profile points to the existing private transport and voice configuration.
The transport configuration selects a line; its gateway configuration resolves
that line to the physical phone. Both lines share one voice owner. The voice
configuration must enable only that physical phone. Use `wake.mode: "manual"`
with `rearm_after_turn: false` for one request. A reviewed ONNX wake profile can
set `rearm_after_turn: true` for repeated requests after successful replies.
`duration_seconds` must be positive and at most 300 seconds, including setup,
wake waiting, requests and playback. Each turn keeps its own shorter limits.
A profile cannot supply a command, shell fragment, remote URL, or arbitrary
process arguments.

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
the controller at the projection. Keep state persistent across replacements
with the same runtime user. Preserve the current gateway renderer's calculated
Pod termination grace and callback-drain budget; do not reuse an older value
from a saved manifest. Known local owners receive Stop immediately. An unknown
external owner requires a conservative recovery horizon, not a blind process
signal or an assumption that the longer session has already ended.

The selected interpreter and source checkout must remain at their configured
paths. The controller creates immutable private snapshots for its child runner
and bounds captured output. Do not copy these snapshots into public artifacts
or delete active state to work around an ownership refusal.

## Start, status and Stop

A Start request must explicitly confirm all three conditions:

- An attendant is physically present at the selected phone.
- The phone's media path is reserved for this session.
- Microphone capture is authorized for this bounded attempt.

The controller does not infer these confirmations from a previous request,
opening a menu, or loading a configuration. The runner independently checks
the phone before starting media. A busy, ringing, unavailable or uncertain
phone is not an invitation to retry until it happens to start.

The gateway serializes voice Start with its other phone mutations. While a
finite voice session owns the phone, direct dial, speaker-key, remote-answer
and display changes are refused with `voice_phone_busy`; the same rule covers
both lines and both phones in an intercom request.

In hands-free mode, ordinary call controls first retire the supervised session
and retain the usual reservations and fresh admission checks. Intercom does
this for both phones. Automatic resumption requires a positively observed
ordinary call to finish and fresh idle checks to pass. An explicit speaker-key
command disables hands-free intent instead of scheduling resumption. Display
changes still require an unreserved phone, and unresolved ownership blocks
conflicting operations.

While voice control is attached, each direct command for any gateway handset
holds the same per-device file lock used by the standalone wake runner until
synchronous phone I/O finishes. An intercom holds both devices' locks. Phones
without a voice profile receive control-only identities; this does not
enable their voice Start or expose a voice status endpoint. The wake pilot must
use the same state directory and a handset in this gateway's inventory.

These temporary locks do not rewrite voice journals or block status and Stop.
They close the gap between checking an idle journal and sending a command while
the gateway is running. They do not provide durable fencing after a gateway
crash or prove that a timed-out HTTP command cannot arrive later. Before starting
a wake pilot, resolve any gateway operation with an unknown outcome and confirm
the handset is idle. Do not use a new wake attempt to bypass that uncertainty.

Ordinary incoming SIP calls retain priority. The gateway's `ring` and
timer/alarm `alert` operations remain eligible to originate incoming calls,
which the voice runner's AMI observer uses to stop its media session. Their
existing busy-phone, Do Not Disturb and active-alert checks still apply.
Configured automatic answer waits for voice cleanup before pressing a line
key. A refusal before phone I/O does not consume that call's answer attempt;
an ambiguous phone response still prevents automatic repeated key presses.
Exact-call hangup, alert Stop, voice Stop and preference changes remain
available. The gateway does not send an unrelated RTP Stop merely to force
another command through.

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
`--dry-run`. Record the returned session ID. To stop the current session:

```sh
cisco --config .local/cisco-cli.json 201 voice stop
cisco --config .local/cisco-cli.json 201 voice status
```

With no ID, the CLI reads current status and then sends Stop for that exact
gateway-owned session. It does not send a wildcard Stop or acquire an external
owner. For a previously recorded session, retain explicit targeting:

```sh
cisco --config .local/cisco-cli.json 201 voice stop \
  --session-id 0123456789abcdef0123456789abcdef
```

The displayed ID is an example, not a valid local session. Read status again to
observe cleanup. Exiting the client or pressing Ctrl+C in the client does not
stop the gateway-owned session; send its explicit Stop request.

`voice stop --dry-run` reads status, resolves or checks the exact session ID,
and checks whether the reported ownership permits Stop through this gateway. It does not
send a Stop POST. If the state is `external_owner` or the sanitized error is
`voice_owner_unavailable`, a matching-ID dry run returns `status: "unavailable"`,
`gateway_stop_available: false`, and a nonzero exit code. It retains the
sanitized voice status and session ID, with guidance to use the live wake
screen's **Stop** or the
[exact wake Job's normal shutdown](voice-wake-k3s.md#start-and-stop-an-attended-attempt).
Status also shows this guidance for known external ownership.

`recovery_required` alone does not mean a different controller owns the session;
local recovery sessions retain their gateway Stop and dry-run path. A successful
dry run validates the current ID and known ownership capability, not whether a
later Stop can be delivered or whether capture has closed. Inspect the real
Stop result and `stop_confirmed`. `--json` returns sanitized structured metadata.
The standalone `phone_voice_control.py` client remains available with
`status|start|stop --client-config PATH --line LINE`; its Start request ID
flag is `--request-id`.

Treat an accepted Start as a control-plane result, not proof that capture or
speech recognition succeeded. Updated runners report their current phase while
the session is active; process `state: "running"` alone is not readiness. Wait
for the reported preparation/warmup to finish before speaking. In wake mode,
wait for the explicit wake-ready phase/reason, then say the configured phrase
and request. Manual mode requires no wake phrase. For XML/RTP profiles, the optional
[phone display](voice-display.md) shows the session progress; physical capture,
audible output and microphone closure still require their own acceptance.

Every response exposes a bounded status object with `schema_version`, `phone`,
`line`, `session_id`, `state`, `active`, `remaining_seconds`, `stop_requested`,
`stop_confirmed` and `error`. `phone` is the canonical physical identity; `line`
is the original session's Start alias, or the queried alias when idle. A status
request through the sibling line does not change an existing session's alias.
The profile's configured transport line still selects the runner's media
configuration. Gateway voice-status payloads exclude credentials, private paths,
process arguments, transcripts, audio and raw child output. Error values are
fixed codes, not exception text.

Updated status may additionally include `phase`, `phase_reason`,
`turns_completed`, `mode` and `session_seconds`. These fields are optional for
compatibility; their absence does not mean ready. `phase` can be `preparing`,
`armed`, `listening`, `thinking`, `speaking`, `stopped`, `error` or null; inspect
its fixed `phase_reason` as well. `mode` is `manual` or `wake`;
`session_seconds` and `remaining_seconds` are bounded by 300.
With this revision, `turns_completed` increments only after nonempty decoded
PCM has been passed to the playback sink and any drain has completed. Missing
TTS URLs, empty PCM, failures and cancellation do not qualify. Earlier versions
counted any normal playback return, including a reply with no audio. Neither
counter definition proves that a reply was heard. No transcript or model
identifier is added to status.

Stopping playback invalidates the pending reply across audio fetch, decoding,
frame delivery and drain. A frame already sent cannot be recalled, but later
frames from that reply must not resume after Stop or certify a completed turn.
A new playback request remains possible after the stopped request has ended.

### Playback diagnostics

Optional playback diagnostics use the same authenticated status endpoint:

```bash
cisco --config .local/cisco-cli.json --json 201 voice status --playback
```

This requests `GET /v1/phones/201/voice_status?include_playback=1`. The optional
`voice.playback` object contains five bounded integer counters:

- `playback_requests`: HA results reaching playback after ownership checks;
  this does not count every HA result or attest STT accuracy.
- `tts_offers`: those results containing a TTS URL; no URL is exposed.
- `empty_playbacks`: known successful returns with no PCM to play.
- `incomplete_playbacks`: errors, cancellation or unverified adapter outcomes.
  These may have transmitted partial audio; they do not mean zero output.
- `completed_pcm_bytes`: source PCM bytes in fully completed playbacks only.
  These are not SIP packet counts, a silence measurement or proof of audibility.

The counters accumulate within one session; a positive total can belong to an
earlier reply. During an in-progress playback, completion counters remain
unchanged. A TTS offer or completed PCM does not identify the actual synthesis
backend, prove that Baya ran rather than cached audio being returned, or verify
the speaker.
The existing provider preflight and separate listening acceptance still apply.

Default status, Start and Stop responses omit this object to preserve the
installed Home Assistant companion's strict schema. The diagnostics require
the updated gateway and both CLI files, `scripts/cisco.py` and
`scripts/cisco_http.py`; HA needs no update.
An older server or a session without these counters reports
`playback_metadata_unavailable` rather than inventing zeros or retrying a
mutation. The query is accepted only on GET status, with no body, no duplicate
parameters and the exact value shown above. Old status/progress documents
without the optional object remain valid. Adding hands-free supervision does
not turn playback counters into proof of audible speech.

### Session state and rearming

In wake mode, successful rearming stays inside the original session ID and
deadline. Say the wake phrase again when ready for the next request. Stop,
failure, call preemption and expiry end that inner session. Manual mode ends
after one turn. One-session mode needs another explicit Start; hands-free can
create a new session only under its cleanup, idle and retry rules above. Valid
Stop of the current supervised session also disables persistent intent. Do not expect conversation context to
survive a new session ID.

States are `idle`, `starting`, `running`, `stopping`, `stopped`, `cancelled`,
`failed` and `recovery_required`; `external_owner` is reserved. `cancelled` means
the attempt was cancelled before its runner launched. `failed` reports session
failure even if earlier turns completed. Read `stop_confirmed` separately:
even `active: false` does not prove physical microphone closure when recovery
is required.
Assistant errors, input timeouts and transport failures remain `failed` even
when their media cleanup is confirmed. Clean fixed-deadline expiry reports
`stopped` with `phase_reason: "lease_expired"`; completed replies are counted
separately. Deliberate Stop and shutdown can also report `stopped`; that state
alone does not claim a completed conversation.

## If the CLI loses a Start or Stop acknowledgement

A timeout or broken connection can occur after the gateway receives a request.
The client then cannot establish whether that request ran. An uncertain real
Start or Stop is reported as `status: "not_confirmed"`, with its `action_id`
and a reminder to inspect status before retrying. For example:

```json
{
  "status": "not_confirmed",
  "reason": "network_outcome_unknown",
  "action_id": "voice-pilot-example-001",
  "outcome": "unknown",
  "recovery_command": "cisco --config .local/cisco-cli.json --timeout 20 --json 201 voice status",
  "note": "The request may have been applied. Do not retry blindly. Restore the intended transport and KUBECONFIG, then run the read-only recovery command with the same private configuration."
}
```

`action_id` identifies the request; it is not the `session_id` required for
Stop. Keep the action ID, selected line and any previously returned session ID.
A lost acknowledgement does not prove that Start was refused, that Stop worked,
or that the microphone is off. Local validation failures, failed pod discovery
before dispatch, and dry-run failures do not represent an attempted physical
Start and remain `refused`.

The local `recovery_command` preserves the selected `--config`, `--timeout`,
line and JSON-output choice, with shell quoting. It includes the configuration
path, so keep this recovery output private. Run it from the same working
directory if the configuration path is relative.

Recover in this order:

1. Restore the intended client connection. For Kubernetes transport, check the
   [selected kubeconfig and API endpoint](cli-installation.md#keep-the-kubernetes-connection-usable).
   A closed temporary forwarding port must not be mistaken for a phone failure.
2. Read the selected phone's current voice state:

   ```sh
   cisco --config .local/cisco-cli.json --json 201 voice status
   ```

   This request does not start media. A second transport error leaves the
   outcome unresolved; it is not an idle status.
3. If your session is still present, use its exact `session_id` for an intended
   Stop and read status again. If ownership is unclear or another operator
   owns the session, coordinate with that owner before acting.
4. For a tracked session, confirm the matching ID, `active: false` and
   `stop_confirmed: true`. If status is `idle` with `session_id: null`, there is
   no currently tracked session; this does not prove that audio or a conversation
   occurred, or resolve an outstanding ambiguous dispatch. Confirm no earlier
   worker is still pending, coordinating with the operator when necessary.
   Follow the physical recovery procedure below if closure remains uncertain.
   Do not delete journals or restart the gateway merely to clear status.

Do not replay Start with a new key to test connectivity. Request-key protection
uses bounded retained history; it does not make blind retries a recovery
procedure. The CLI does not retry the mutation automatically. Use a new key only
for a deliberately new attended attempt after the previous ownership and media
state are resolved.

## Deadlines and uncertain cleanup

The controller applies an external lease of at most 300 seconds and requests
runner shutdown with SIGTERM when that lease expires. It allows a further
45 seconds for cleanup. If forced termination is necessary, it targets only
its own runner process; it does not kill the watchdog's process group.
The active-session ceiling does not lengthen these Stop/cleanup budgets.
This is additional supervision, not a hardware-enforced microphone lease.

Stop acknowledgement, process exit and physical microphone closure are different
observations. The controller checks the runner's structured cleanup and watchdog
evidence. A nonzero exit code can follow an explicit user Stop; it is not by
itself evidence that Stop failed. Conversely, process exit alone does not prove
that the phone accepted Stop.

An early startup failure can release ownership without a Stop acknowledgement
only when the trusted runner proves that it attempted neither watchdog arming
nor RTP Start and completed local cleanup. The controller checks the complete
failed child's report against its private launch challenge, exact session and
current reservation before writing the terminal journal and releasing the lock.
That outcome remains `failed`, with `phase: "error"`,
`phase_reason: "start_failed"` and `stop_confirmed: false`. The challenge binds
the report to one launch; it does not authenticate an untrusted Python executable
or imported code. Saved diagnostics cannot authorize this release.

Missing, contradictory or unconfirmed evidence leaves persistent
`recovery_required` state. A watchdog arm attempt, any RTP Start attempt, a
signal, forced termination or incomplete child output excludes the early-failure
release path.
Finite Start does not retry or restart automatically. Hands-free supervision
may renew only after its confirmed-cleanup and idle checks; it never bypasses
an active, stopping or unresolved session. State survives separate
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

The gateway additionally supplies its full trusted inventory as immutable
`ControlPhone` identities through `control_phones=`. Its internal `control`
authorization permits `reserve_control(target, context=...)` to fence a direct
phone command; this context manager releases its file lock in `finally`. These
identities do not add voice profiles, and the gateway does not grant this
internal authorization to a bearer request or expose it as a separate route.
Shutdown uses internal `reserve_shutdown` contexts for every inventory handset.
They check the actual shared file lock as well as the current journal, including
the interval before an external owner writes its first journal. Each quiescent
phone stays fenced until the final authentication listener closes.
Acquiring these fences can create a private `0700` state directory and empty
`0600` lock files without changing a journal. A lock file's existence alone does
not mean a session is active. Embeddings of `Gateway` must call
`release_voice_shutdown()` in `finally` after closing all callback listeners;
do not delete lock files to release ownership.

The default monitor supervises deadlines without starting media by itself.
An embedding that sets `monitor=False` must call `tick()` regularly.
`close()` rejects new Starts and requests cleanup, but returns without waiting
for all children to finish. Keep supervision alive during shutdown; do not
equate a returned `close()` call with confirmed microphone closure.
During gateway shutdown, phone authentication callbacks remain available while
owned or uncertain sessions drain, subject to the current runtime's bounded
shutdown budget. Known local owners receive shutdown immediately; the five-minute
active-session ceiling is not a shutdown sleep. An external owner is not
signalled by this process, so its conservative recovery horizon must account
for a possible remaining lease and cleanup.

Confirmed cleanup and acquired fences permit an earlier exit; a timeout returns
an incomplete result and does not clear unresolved journals. Preserve the
current renderer's calculated Pod termination grace, including listener closure
for the inventory size, rather than copying historical 145/180-second figures.
Do not release new wake Jobs while the gateway is shutting down.

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

**Services → Home Phone Apps → Voice Assistant** uses the gateway's Stop
endpoint. If the firmware shows a service list first, choose **Home Apps**. The
**Stop** softkey on the live **Listening / Thinking / Speaking** screen belongs
to that screen's voice session. Use it for a
[standalone wake Job](voice-wake-k3s.md#start-and-stop-an-attended-attempt).
When status reports `voice_owner_unavailable` or `external_owner`, the
Applications page shows ownership guidance instead of a gateway Stop that
cannot reach that session. This does not prove that its microphone is active
or stopped. Local recovery states still retain gateway Stop where available;
`recovery_required` alone does not establish a different owner.

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

The updated companion also exposes the separate administrator-only
`cisco_phone_control.hands_free` response service. It requires `entry_id`,
canonical `phone`, an owned `line`, and `operation: status|enable|disable`.
Enable additionally requires a retained `request_id`, literal
`enable_microphone: true` and `exclusive_media: true`; it accepts `dry_run`.
Disable requires `request_id` but no session ID or consent flags. Status needs
no request key. Its result has the existing envelope and a separate
`hands_free` object; finite `voice` responses remain unchanged. Updating the
companion is required for this new service, unlike the CLI-only playback query.
Use current matching finite status to determine readiness; policy enablement
alone does not establish active listening.

## Validation and acceptance

The controls above describe implementation capability, not a claim that the
new revision is deployed or acoustically accepted. Confirm the
installed runtime and client together. A usable wake-session acceptance must
include two distinct spoken requests and audible replies under one session ID,
return to wake readiness, explicit Stop, and ordinary-call preemption. The
following records describe earlier pilots; they do not replace that check.
Hands-free additionally needs verified renewal across an inner lease, ordinary-
call pause/resumption, and Disable during preparation or renewal with no later
capture. Check audible effects of call setup/renewal on the stock phone rather
than assuming seamless operation.

The deployed manual pilot passed image/configuration checks, authenticated
directory checks, and status → Start dry-run → status through both line aliases.
Its existing phone preferences survived the gateway replacement; both aliases
remained idle with no microphone activation. The deployed XML renderer also
passed menu and directory checks. Rendering XML does not prove that a handset
fetched or displayed it. An optional administrator-source HTTP rejection check
timed out, so no particular HTTP rejection status was verified by that probe.
These results do not establish physical full-turn or wake-phrase accuracy.

The companion Home Assistant component also passed live upgrade acceptance:
the integration loaded, existing device identities and preferences were
preserved, and status-only requests through both aliases returned the same
idle physical phone with no active session. This check did not start capture.
Keep component backups outside `custom_components`, including hidden backup
directories: Home Assistant can discover their duplicate manifests. Verify
the selected package and version as well as configuration validity before a
restart, then confirm the integration and service actually loaded afterward.

See the [deployment guide](voice-gateway-deployment.md),
[wake evaluation guide](wake-qualification.md) and
[response-stage measurements](local-voice-performance.md) for reproducible
installation and further qualification.

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
