# Stock-phone voice assistant: implementation and acceptance

This implementation uses the **unmodified SIP45 firmware** on a Cisco 7945G.
The phone supplies microphone/speaker RTP and renders a small XML interface;
the local host runs the wake detector and Home Assistant connection. It is a
bounded, foreground pilot, not an unattended always-listening service.

![Stock-phone voice architecture and acceptance boundaries](diagrams/voice-rollout.svg)

## What is implemented

- A source-pinned G.711 transport with sequence/timestamp checks, bounded
  reordering, explicit stream ownership and a separate cleanup supervisor.
- A controller connecting 8 kHz phone PCM to 16 kHz wake/STT input, with a
  per-phone permission check before hardware activation, call preemption,
  input inactivity timeout, explicit Stop and an absolute 30-second ceiling.
- Manual activation and the exact frozen EchoMuse ONNX frontend. The private
  classifier, model files, phrase and manifest remain outside Git.
- Home Assistant Assist WebSocket input, conversation continuity within an
  explicitly active session, local decoding of returned speech and paced RTP
  playback. The selected HA pipeline determines whether STT/LLM/TTS is local.
- An English status display with a generated avatar, one replaceable frame,
  source-restricted HTTP routes, expiring session URLs and a Stop softkey.
- An offline firmware inspector and video-to-still converter. Neither changes
  phone firmware nor supplies a missing native SIP video decoder.

The layers have separate tests and clear contracts: [transport](voice-transport.md),
[voice coordinator](local-voice-assistant.md), [display](voice-display.md),
[firmware lab](firmware-lab.md) and [local STT](local-stt.md).

## Physical evidence, 19 October 2026

One configured 7945G passed an initial stock-RTP test:

- A 10-second capture lease received **475 valid G.711 packets**, with no
  parser/source rejection reported. Samples were discarded, not recorded.
- The phone confirmed the transmit Stop. A new socket on the same destination
  port then observed **zero packets over two seconds**.
- A 15-second playback lease sent **689 packets**. The operator confirmed
  clearly audible sound; the phone acknowledged the receive Stop.

The first short, quiet playback trial was masked by TV/music. A louder,
longer, explicitly requested reference made the acoustic check useful.
Packet counts are not a speech-quality score or proof of room-distance wake
recognition. These results cover one phone, not every hardware revision.
Physical Mute, call coexistence and long-running acoustic behavior need their
own acceptance checks.

The exact wake frontend was also compared with its original implementation:
four deterministic PCM inputs, 32 seconds, 400 embedding rows and 340
classifier scores matched exactly. This establishes implementation parity,
not wake phrase detection in a real room.

Four existing synthetic positive clips also triggered the frozen classifier
after 8 kHz downsampling, G.711 encoding/decoding and the production 16 kHz
input conversion; peak scores were 0.9975–0.9986 against the unchanged 0.5
threshold. A subsequent 30-second physical capture delivered 1,479 frames
but produced no wake detection; the operator later confirmed missing that
window. In an attended retry, the phone detected the private phrase after
320 valid frames (65 scored windows), with a peak score of **0.8497**. Remote
Stop was acknowledged and the supervisor finished without errors. No audio
was recorded. This establishes a successful single-room wake trial, not a
measured false-accept/reject rate or acceptance across all phones.

The display now distinguishes initial model preparation from readiness. The
frozen frontend needs 1.28 seconds of incoming history before it can classify;
showing “Listening” immediately could lose a short phrase spoken at startup.

A separate compact HA conversation profile completed two synthetic requests
in **23.98 seconds** and **15.80 seconds**, including reply retrieval and
decoding. It preserves the original preferred profile and combines cloud STT
with a local conversation model and local Silero Baya speech. Its generated
1.2-second reply was sent to the physical phone in **60 RTP frames**;
the operator confirmed hearing the reply, and receive Stop and supervisor
completion were confirmed. That playback test did
not activate the microphone. These are separate wake, pipeline and output
checks; a complete spoken-command-to-answer room test remains unverified.

## Install and prepare

Use the [voice CLI installation guide](voice-cli-install.md) for a local
`cisco-voice` wrapper. For a one-shot cluster execution, use the
[suspended k3s Job guide](voice-k3s.md). The ordinary `cisco` call-control CLI
continues to work independently.

Keep two private configuration files. The transport file selects a provisioned
line, its hosting phone, the local bind address/ports and a short lease. The
assistant file selects the exact HA pipeline and explicit per-phone permission.
Both example files are intentionally inert and contain synthetic values:

- [`phone-voice-transport.example.json`](../examples/phone-voice-transport.example.json)
- [`local-voice-assistant.example.json`](../examples/local-voice-assistant.example.json)

Use the **first-line phone ID** in `phones` in the assistant file. Either line
can select the same physical device in the transport file. Never infer the
mapping from odd/even numbering. HA tokens and model bundles belong under the
workspace's ignored `.local/`, with owner-only permissions.

Set the selected phone's permission to `true` in the private assistant file,
then validate without network or hardware actions. Validation itself does not
activate the microphone, even when the file permits a later live session:

```sh
cisco-voice \
  --transport-config .local/voice/transport.json \
  --voice-config .local/voice/assistant.json
```

After reviewing the selected phone and verifying that both lines are idle,
an attended session is:

```sh
cisco-voice \
  --transport-config .local/voice/transport.json \
  --voice-config .local/voice/assistant.json \
  --display-port 18110 \
  --run --attended --exclusive-media --enable-microphone
```

The host must own the transport bind address. Do not run this on a laptop
while selecting a server address. Ensure the selected phone can reach the
chosen UDP receive port and display TCP port. Permit only the intended local
source; do not publish either port to the Internet. The existing gateway
phone authentication callback must remain reachable.

The prototype uses internal transport helpers in the same repository revision.
Deploy these files together; independently upgrading one layer is unsupported.

## Stop, ownership and failure behavior

Use the on-screen **Stop** control or Ctrl-C in the foreground command.
The controller also stops for either line becoming busy, a stale/failed AMI
observation, input inactivity, a provider error or lease expiry. A fresh
explicit command is required after Stop. No restart policy or reconnect path
may silently re-enable listening.

The supervisor preserves cleanup after a controller crash and distinguishes a
definite device rejection from an ambiguous transport result. It cannot issue
Stop if the whole host is dead or the network is disconnected. The phone has
no demonstrated hardware-enforced RTP lease. A successful HTTP response is
also not a physical audio measurement. Do not promote this pilot to unattended
microphone operation without testing those failure cases and an independent
recovery mechanism.

The display's own TTL bounds server memory and stale URLs. It does not prove
that the phone has dismissed its last rendered page or stopped audio. Stop
confirmation must come from the media controller, and a failed confirmation
must remain visible as an error.

## What remains conditional

A fully local experience needs a **working local STT provider**, a local
conversation agent, local TTS and the actual private wake classifier. A label
such as “Local” in a saved pipeline is not evidence of offline operation.
The [STT repair record](local-stt.md) separates the previously unavailable
Whisper provider from the cloud STT used by the existing profile.

Custom Java/native installation is currently blocked by the lack of a verified
compatible build and signed deployment route. The firmware lab recovered
bounded executable/archive material without executing it, changing hashes or
publishing Cisco binaries. Smooth/native incoming SIP video remains unsupported;
the separate frame converter proves offline still generation only.
