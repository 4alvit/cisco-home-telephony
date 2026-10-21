# Stock Cisco 7945G voice transport

`containers/asterisk/phone_voice_transport.py` provides a bounded RTP transport
library and an attended compatibility pilot. This is a **prototype**, not an
accepted always-listening service. It does not start with the gateway, enable a
Home Assistant pipeline, or install anything on a phone. Importing the module
and running the default CLI do not activate audio.

The [feasibility study](native-app-feasibility.md) defines the larger plan. This
stage tests the existing XML control path and G.711 media path. Wake detection,
speech recognition, conversation, production media arbitration and acoustic
acceptance remain separate work.

## Documented phone contract

Cisco's Release 7.0(1) model matrix includes the 7945G for legacy unicast RTP.
The relevant commands are:

| Command | Meaning |
| --- | --- |
| `RTPTx:receiver_ipv4:receiver_port` | Send phone audio to the receiver. |
| `RTPRx:sender_ipv4:phone_port[:volume]` | Receive audio from the sender. |
| `RTPTx:Stop` | Stop channel-one transmit streaming. |
| `RTPRx:Stop` | Stop channel-one receive streaming. |

Ports must be even, from 20480 through 32768 inclusive. The format is G.711
mu-law in 20 ms packets. Receive volume is 0–100; omission preserves the phone's
current volume. Calls starting or resuming terminate existing streams. During
an active call, new transmit is rejected but new receive can play as a whisper,
ignoring volume. A held call permits transmit until resumed. Another stream in
the same direction produces a busy response. Ringing alone has no stated RTP
preemption guarantee. The receive-only Mute lamp is not proof of microphone
closure. These are documentation claims, not measurements on the installed
firmware. [Cisco legacy RTP reference](https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cuipph/all_models/xsi/7_0/english/programming/guide/70xsi/xsi70uri.html).

Commands use the gateway's existing `phone_gateway.execute(phone, uri)`
abstraction, which submits one authenticated `CiscoIPPhoneExecute` item and
requires a matching successful response. Internal URIs execute immediately;
`Priority="2"` does **not** make RTP idle-only. Multiple ExecuteItems have no
guaranteed execution order, so dependent operations use separate calls.
[Cisco Execute and Response objects](https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cuipph/all_models/xsi/7_0/english/programming/guide/70xsi/xsi70obj.html).

The Component RTP API is intentionally outside this pilot. It adds stream IDs,
notifications and a single `sendReceive` request, while legacy transmit and
receive are separate. A legacy Stop targeting either direction of a Component
duplex stream can stop the entire stream. A Component `stopMedia` without an ID
stops all service streams. Mixing independent controllers is therefore unsafe;
the schema's codec list also does not establish codec support on a particular
firmware release. [Cisco Component RTP API](https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cuipph/all_models/xsi/7_0/english/programming/guide/70xsi/xsi70api.html).

## Session policy

The coordinator requires explicit microphone permission. Playback is a separate
choice. Before starting and during the session, the pilot reads fresh AMI state
for **every line on the selected physical phone**. Any associated channel,
non-idle endpoint or unavailable state prevents or terminates the pilot; held
calls are not treated as idle. AMI failure also ends the session. The selected
line identifies the phone and does not select a microphone or start a call.

An explicit Stop, call preemption, receive timeout, controller failure or session
deadline is terminal. There is no automatic reconnect or return to listening.
Starting again requires another deliberate invocation. The pilot does not press
line, speaker or headset keys, and does not answer, originate or hang up calls.
AMI observation and phone execution are separate operations; they cannot make
the check/start boundary atomic. Call coexistence must still pass the physical
acceptance gate.

Legacy Stop has no application-owned stream ID. Reserve this phone's XML audio
for the pilot and stop other media controllers before acknowledging
`--exclusive-media`. Do not run alongside radio or another XML/Component RTP
application. A busy response is not permission to replace another stream.
Exclusive ownership must remain true through watchdog cleanup.

The local receiver is bound to a specific owned IPv4 address and port. Incoming
audio is checked against the selected phone's address; an optional known source
port can further restrict it. Otherwise, the first valid packet pins both the
source port and SSRC for the whole session. Packet parsing and media queues are bounded.
Only the expected G.711 media format enters the audio path. Sequence numbers,
timestamps and source identity are checked so malformed, unexpected or stale
traffic is not treated as fresh audio. The prototype is not a complete RTP/RTCP
endpoint or a production jitter buffer.

## Library boundary

The public audio boundary is mono PCM16 little-endian at 8 kHz. One 20 ms frame
is 320 bytes; RTP uses payload type 0 with 160 mu-law bytes.
[RTP audio profile](https://datatracker.ietf.org/doc/html/rfc3551#section-4.5.14).
`RtpSender` advances the sequence by one and the timestamp by 160 per packet,
with modular wrap. [RTP header specification](https://www.rfc-editor.org/rfc/rfc3550#section-5.1).

| API | Contract |
| --- | --- |
| `tx_uri(ip, port)`, `rx_uri(ip, port, volume=20)` | Validate canonical unicast IPv4, Cisco port constraints and optional volume before constructing a command. |
| `RtpReceiver.ingest(data, source)` / `flush()` | Lower-level bounded reordering; returns mu-law frames, inserts silence for bounded gaps and counts rejected traffic. |
| `UdpMedia.receive()` | Returns zero or more decoded 320-byte PCM frames. |
| `UdpMedia.send_pcm(frame)` | Accepts one 320-byte PCM frame; returns `False` when pacing requires the caller to retry. |
| `VoiceSession.start()` / context manager | Explicitly starts requested media after ownership and status checks. |
| `VoiceSession.poll()` / `play(frame, *, allowed=None)` | Wrap media I/O with deadline and preemption checks; an optional playback guard discards cancelled queued frames. |
| `VoiceSession.stop(reason)` | Terminal, idempotent cleanup; no implicit restart. |

`VoiceSession` accepts injected command, media, status, clock and watchdog
functions so tests need no phone. A host integrating it must provide a status
function covering all lines and other media users, poll regularly, and arm an
independent watchdog. The attended CLI supplies those pieces for its limited
pilot. Session lifecycle and media operations serialize through one reentrant
lock, including calls from worker threads. Injected command and status functions
must return within bounded time; the independent watchdog must not depend on
that lock. Direct `UdpMedia` callers must set a monotonic `deadline` before opening
and close their socket. Audio consumers own any PCM they retain; the CLI
discards every received frame. Malformed, wrong-source and stale packets do not
renew its receive timeout.

A playback sink may pass a fast, synchronous generation check as `allowed`.
It runs under the session lock after the state and call checks, immediately
before the media send. Only `True` permits the frame; a false result or callback
error returns `False` without sending or stopping the session. The sink must
stop retrying when its generation is cancelled. Omitting the guard preserves
the existing playback API.

## Configure without starting audio

Copy [the example](../examples/phone-voice-transport.example.json) to
`.local/phone-voice-transport.json`. Keep private addresses, phone identities,
gateway credentials and pilot observations under `.local/`. The public example
contains only synthetic values; `192.0.2.10` is a documentation address and is
not a usable deployment address.

The configuration fields are:

| Field | Purpose |
| --- | --- |
| `gateway_config` | Existing private gateway JSON; relative paths resolve from the current working directory. |
| `line` | A configured line identifying the one physical phone. |
| `bind_ipv4` | Address owned by the computer running the pilot and reachable from the phone. |
| `bind_port` | Local receive port advertised in `RTPTx`. |
| `phone_rtp_port` | Phone receive port advertised in `RTPRx`. |
| `duration_seconds` | Controller media deadline, at most 300 seconds, including setup. |
| `receive_idle_seconds` | Maximum wait without accepted microphone packets. |
| `volume` | Explicit initial playback volume; start low. |
| `expected_source_port` | Optional observed phone transmit source port; `null` pins the first valid packet's source port. |

The phone's transmit source port need not equal `phone_rtp_port`: that field
selects where the phone receives playback. Do not substitute a guessed source
port. Use free, even RTP ports in the documented range. The pilot must run where
its configured local address can be bound and the existing private AMI and phone
HTTP endpoints are reachable. It does not change routing, firewall rules or
phone provisioning.

If startup traffic changes source port, first-packet pinning can reject the
later stream. Follow the [observed source-port diagnostic](voice-rtp-source-port.md)
before changing a private profile; packet capture alone is not accepted audio.

From the repository root, validate the private configuration first:

```sh
python3 containers/asterisk/phone_voice_transport.py \
  --config .local/phone-voice-transport.json
```

The default invocation is a dry run. It loads and validates both JSON files and
checks that the selected line belongs to one configured phone. It makes no
phone or AMI network requests, opens no media socket and grants no microphone
permission. A successful dry run checks configuration, not phone availability
or firmware compatibility. A copied example cannot validate until
`gateway_config` points to an existing private configuration with that line.

## Attended pilot

The operator alone runs live trials. Have an attendant at the chosen
phone, verify every line is idle, and reserve its XML audio path. Start with a
short duration. Capture checks use counters and discard audio; no recordings
are written and there is no recording flag.

For a microphone-only trial:

```sh
python3 containers/asterisk/phone_voice_transport.py \
  --config .local/phone-voice-transport.json \
  --run-pilot --attended --exclusive-media --enable-microphone
```

For synthetic tone playback without microphone capture:

```sh
python3 containers/asterisk/phone_voice_transport.py \
  --config .local/phone-voice-transport.json \
  --run-pilot --attended --exclusive-media --playback-tone
```

Combining `--enable-microphone` and `--playback-tone` requests both paths for an
attended duplex check. Packet delivery does not establish echo cancellation or
usable interruption quality. Observe the actual active audio path, phone
indicators, hardware Mute and the attendant's local stop action. Ctrl-C requests
controller cleanup; do not assume closing a terminal alone closes the
microphone.

The pilot reports terminal `state`/`reason`, `rtp` receive counters and `udp`
I/O counters. `capture_observed` records whether accepted microphone packets
arrived; `playback_packets_sent` counts transmitted packets. Main-session
`stop_confirmed` and `stop_errors` are separate from the watchdog's
`watchdog_stop_confirmed`, completion, error and direction fields.
`cleanup_confirmed` requires either stop path to confirm cleanup, a finished
watchdog, and no watchdog errors.

Keep outcomes distinct: a successful control response means the phone accepted
an action; accepted RTP packets mean media reached the receiver; audible
playback and physical closure require observation. Zero packets can reflect
rejection, routing, firewall, codec or source checks and must not be reported
as a successful capture.

## Cleanup and limits

The controller runs cleanup in `finally`, including after partial startup. It
attempts Stop for each direction whose start succeeded or had an ambiguous
outcome. An explicitly rejected start (`CommandRejected`) does not grant
ownership and must not trigger Stop for that direction. The adapter treats only
code 1 (parse/busy) and code 4 (unauthorized) as definite rejection. Codes 2, 3,
5 and 6, and other response, network or parser failures remain ambiguous.
Failure to frame a response does not prove that the command never executed.
The exclusive-media prerequisite remains essential because a Stop still has no
application-specific identity.

A separate watchdog supervisor is armed before the first start command. All
pilot phone commands run through it, so it records accepted, rejected or
ambiguous results without relying on a later parent acknowledgment. Each
gateway request runs in a killable worker with a two-second result wait. The
underlying gateway's socket timeout is not treated as a total time limit.

At the media deadline or when its parent channel closes, the supervisor stops
owned or uncertain directions. Cleanup can make two passes, separated by
10.5 seconds, to cover a start request still in flight. The parent allows up to
23 seconds for supervisor completion and reports whether it finished. Local
socket cleanup is attempted before remote Stop; local errors do not skip the
remote attempts. Inspect both local and remote cleanup errors.

**The configured finite limit (at most 300 seconds) is a controller deadline, not a hardware microphone
lease.** A phone HTTP request can still be in flight at expiry;
Stop attempts can take additional time or fail. Host failure, loss of power,
network partition, firmware behavior or an unreachable phone can defeat both
controllers. A successful Stop response also does not prove the physical
microphone has closed. The attendant must verify closure and use the phone's
local controls if needed. Do not run another media application until cleanup
has completed and the phone is known idle.

## Observed transport pilots

On 19 October 2026, the coordinating operator reported a successful attended,
capture-only trial on one stock phone, using a local server and a configured
10-second duration. The receiver accepted **475 G.711 RTP packets** and emitted
475 audio frames, with zero parser or source-validation errors.
`stop_confirmed` and `cleanup_confirmed` were both `true`, and the independent
watchdog finished. No audio recordings were made.

After capture Stop, a fresh receiver bound to the same UDP port observed
**zero packets during a two-second probe**. This is evidence that RTP delivery
ceased during that observation window, not proof of physical microphone closure.

A separate playback-only trial with a configured three-second duration sent
**123 RTP packets**, reported no errors, and confirmed Stop. Background
television/music masked this quiet trial, so its packet counters alone did not
establish audibility. A subsequent **15-second playback trial at a higher
level** sent **689 packets** and received explicit user confirmation that the
tone was clearly audible.
The quiet default playback settings remain unchanged.

These observations establish capture-path compatibility, audible receive-stream
playback, and bounded transport behavior for the trials. They do not establish
duplex quality, exact microphone/speaker/headset routing, physical Mute or Stop
controls, call coexistence, or physical microphone closure. Deployment identities
and raw observations remain private.

## Acceptance remains open

Local synthetic tests establish validation, packet handling and controller
policy. They cannot establish installed-firmware behavior. Before expanding
beyond an attended pilot, record these outcomes privately:

1. The actual microphone/speaker or headset path, visible indication, Mute and
   local Stop behavior, including closure after controller Ctrl-C.
2. Capture and playback independently, then together: observed payload type,
   packet size, source port, timing, loss and clear low-volume audio.
3. Both lines' call, ringing and hold/resume behavior, with no assistant audio
   entering a call and no automatic restart after it.
4. Receiver, AMI, controller and network failures, including the independent
   watchdog and physical confirmation after an unconfirmed Stop.
5. Radio/other-service arbitration, acoustic echo and background-media effects,
   before any wake detector or unattended microphone mode is connected.

Use the synthetic test suite without a phone:

```sh
python3 -m unittest discover -s tests -p 'test_phone_voice_transport.py'
```
