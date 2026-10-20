# Local voice adapter prototype

The `voice_assistant` package provides a runnable server-side wake and Home
Assistant Assist adapter. It opens no microphone, phone, listening socket or
audio device. The parent controller owns authentication, Cisco media commands,
RTP, both-line call priority, resampling and physical Stop. This is a software
prototype, not a passed physical-phone acceptance test.

As of October 19, 2026, synthetic tests cover the complete coordinator → HA
JSON/binary protocol → TTS WAV → PCM-sink path, conversation isolation,
cancellation, limits and the frozen wake frontend. A synthetic headless replay
also passed against live HA with cloud STT, local conversation and local TTS.
Physical full-turn behavior and a fully offline deployment remain separate gates.

## Run without hardware or network

Use Python 3.11 or newer for these dependency-free commands from the repository
root. On Python 3.10, first create the private environment shown below and install
`requirements-python310.txt`, then run the commands with its Python executable:

```sh
python3 -m voice_assistant demo
python3 -m voice_assistant validate --config examples/local-voice-assistant.example.json
python3 -m unittest discover -s tests -p 'test_voice_assistant*.py' -v
```

The demo uses the real HA protocol client with an in-memory protocol peer. It
reports `network_connections: 0`, 640 input PCM bytes, stage transitions and a
success flag. Importing the package and validating configuration have no network
side effects. NumPy-dependent tests skip if that optional package is absent;
the optional compressed-audio test skips if FFmpeg is absent.

The core coordinator, WAV decoder, configuration loader and synthetic protocol
demo use only the standard library on Python 3.11+. Python 3.10 additionally
needs the optional `requirements-python310.txt` extra (`async_timeout`) for HA
deadline handling, including the protocol demo. Live WebSocket access requires the
`requirements-websocket.txt` extra. ONNX wake detection additionally requires
`requirements-onnx.txt`. These files are inside `voice_assistant/`; nothing is
installed into the shared runtime automatically. For an isolated environment:

```sh
python3 -m venv .local/voice-venv
.local/voice-venv/bin/python -m pip install -r voice_assistant/requirements-python310.txt
.local/voice-venv/bin/python -m pip install -r voice_assistant/requirements-websocket.txt
# Only for an explicitly configured private ONNX bundle:
.local/voice-venv/bin/python -m pip install -r voice_assistant/requirements-onnx.txt
```

No `openwakeword` package or legacy TFLite runtime is required. The adapter
executes the saved openWakeWord-family ONNX assets with the exact EchoMuse
frontend. Optional `FFmpegDecoder` handles compressed TTS using an existing
local FFmpeg executable; it permits only pipe input and selected audio demuxers.
The Python 3.10 extra is a no-op on newer Python versions. The WebSocket extra
includes both `websockets` and `aiohttp`; live TTS fetch uses aiohttp so
cancellation closes the actual response read.
Hosts using only aiohttp can use `requirements-aiohttp.txt` and
pass `connect=voice_assistant.ha.aiohttp_connect` to `build_service`, or select
`--transport aiohttp` for replay. No websockets package is needed for that path.

## Integration contract

The stable entrypoint is
`voice_assistant.config.build_service(config, workspace, sink, on_event=callback,
compressed_decoder=None)`. Pass the output of `load_config`; `workspace` is a
`pathlib.Path`. The caller runs the returned `AssistantService` on one asyncio
event loop. The package is importable from the checkout or through `PYTHONPATH`;
it does not require a daemon or package installation.

The sink implements:

```python
async def write_pcm(phone_id: str, data: bytes, sample_rate: int) -> None: ...
async def stop(phone_id: str) -> None: ...
```

Output is signed little-endian 16-bit **mono** PCM, with the rate supplied on
every call. WAV rates are preserved; decoded compressed output is 16 kHz. The
sink owns bounded buffering, pacing, resampling to 8 kHz, G.711 encoding and RTP.
Its `stop` must flush unsent audio. Output blocks are up to 20 ms; the final block
may be shorter. `write_pcm` must await actual paced delivery; a sink that only
enqueues blocks must also provide `async drain(phone_id)`, which the adapter
awaits before completing playback. Do not assume the WAV rate is 16 kHz.

Input to the coordinator is exactly **640 bytes per call**: 20 ms of signed
little-endian 16-bit mono PCM at **16 kHz**, without WAV or RTP headers. The RTP
track supplies 8 kHz decoded PCM; the parent performs continuous 8 → 16 kHz
resampling and reblocking. Upsampling does not restore telephone bandwidth.
Validate source identity/SSRC, apply jitter/loss policy, and reject stale media
before invoking this interface. On discontinuity that invalidates frontend
history, stop and deliberately start a new session rather than mixing streams.

```python
from pathlib import Path
from voice_assistant.config import build_service, load_config

service = build_service(
    load_config(".local/voice/config.json"), Path.cwd(), sink,
    on_event=handle_status,
)

# Run only after the controller authorizes a deliberate user activation.
service.ensure_allowed(phone_id)  # synchronous, before any hardware session.start
stream_id = await service.activate(phone_id)  # manual request, no wake required
# Or: stream_id = await service.listen(phone_id)  # wait for local ONNX detection
accepted = service.feed_pcm(phone_id, stream_id, pcm_16k_20ms)
service.end_utterance(phone_id, stream_id)  # optional upstream VAD/end button
await service.wait_idle(phone_id)

# Either line becoming busy, ringing, radio, mute, lost receiver or Stop:
await service.preempt(phone_id, reason="line_busy")
# Controller also stops physical RTPTx/RTPRx and flushes its media queues.
await service.close()  # shutdown
```

`listen` and `activate` return an opaque stream ID. `feed_pcm` returns false for
a stale ID or non-capturing state; it raises for a malformed current PCM frame.
Stop/preemption invalidates the ID. A reconnect cannot resume a stopped stream.
Use a fresh explicit activation and obtain a new ID. `end_utterance` ends input
without cancelling the reply; `stop`/`preempt` cancel the complete turn.

Call the public synchronous `ensure_allowed(phone_id)` **before** the controller
starts physical media. It raises `PermissionError` for a disabled service,
unpermitted phone or unknown phone, and has no state or media side effects.
`listen` repeats this check; `activate` reaches the same gate through `listen`.

Status callbacks receive `VoiceEvent(phone_id, state, reason)`, containing no
transcript, token or model name. States are `stopped`, `armed`, `listening`,
`thinking`, `speaking` and `error`. Callbacks are synchronous, must return quickly,
and should queue gateway/UI work. On `stopped` or `error`, the controller must
close physical media as well. Dropping PCM in this process is **not** evidence
that the phone microphone is closed. The parent's hardware watchdog and both
phone lines must remain authoritative, including the possible stock-RTP
interaction with an active call. The package never sends a Cisco control URI.

## Enablement and bounded behavior

Copy the example into `.local/voice/config.json`, replace the placeholders, and
keep all tokens, addresses, identifiers, model files and response audio private.
Both `service.enabled` and the chosen `phones` value must be true before an
explicit activation succeeds. The example sets both false and selects `manual`
mode. Construction never starts listening; there is no always-listen default.

Default limits are 320 ms of in-memory pre-roll, 100 queued 20 ms frames, a
12-second utterance, a two-second missing-input timeout and a 35-second total
turn deadline. Queues reserve an end marker slot. Overflow fails closed rather
than dropping arbitrary speech and continuing. There is no persistent audio or
transcript log. The CLI saves a response only when `--output` is supplied.

Default completion stops the stream. Explicit `rearm_after_turn: true` allows
continuing wake listening within that authorized session, subject to a duplicate
window. Stops and failures never automatically rearm. Do not bind this setting
to DND or auto-answer.

One turn at a time wins across phones in this coordinator. Other wake detections
are discarded during its turn and the following duplicate window; stale
pre-roll/frontends are reset. This is deterministic first-arrival arbitration,
not acoustic ranking. Other processes and existing voice devices are outside
this coordinator; household-wide arbitration still needs integration. Playback
does not listen for barge-in. Cue playback, screen status mapping and physical
media scheduling belong to the parent controller.

Each phone owns an independent frontend and conversation ID. Normal subsequent
explicit activations can reuse that phone's conversation; Stop or preemption
clears it. Client errors are exposed as bounded reason codes, not raw provider
exceptions. Cancellation closes the per-turn HA connection and stops playback;
it cannot undo a Home Assistant action that already executed. There is no
automatic retry of a failed intent.

## Exact private ONNX bundle

Set `wake.mode` to `onnx` only after placing a reviewed private bundle under
`.local`. `bundle_file` points to its JSON manifest. Its format is:

```json
{
  "format_version": 1,
  "frontend": "echomuse-oww-16k-v1",
  "threshold": 0.5,
  "models": {
    "mel": {"path": "mel.onnx", "sha256": "REPLACE_WITH_64_LOWERCASE_HEX"},
    "embedding": {"path": "embedding.onnx", "sha256": "REPLACE_WITH_64_LOWERCASE_HEX"},
    "classifier": {"path": "classifier.onnx", "sha256": "REPLACE_WITH_64_LOWERCASE_HEX"}
  }
}
```

The threshold above is a schema example, not a calibration recommendation. Copy
the threshold and all three digests from the selected frozen release and feature
provenance. Never silently select a newer experimental classifier. Model names
are deliberately generic; do not add the household phrase or model weights to
public files. Manifest/assets must resolve within `.local`; escapes, digest
mismatches, incompatible outputs and legacy formats are rejected. The loader
creates ONNX sessions from verified bytes and does not download anything.

The implementation preserves raw int16-scale float32 input, 1280-sample hops,
480-sample left context after the first hop, 76 initial mel frames of ones,
`raw_mel / 10 + 2`, embedding input `[1, 76, 32, 1]`, and classifier input
`[1, 16, 96]` after 16 real embeddings. It adds no normalization, extra sigmoid
or score smoothing. The refractory period is 19 hops. The reference is the
current `ha-echomuse/training/oww_features.py`, not the old EchoLocal TFLite path.

After starting or resetting an ONNX session, the frontend requires 1.28 seconds
of audio history. The coordinator reports `armed` with reason `model_warmup`
until that history is ready, then reports `armed` with reason `wake_ready` once.
The controller should display preparation until readiness and only then invite
the wake phrase. Fresh streams and automatic rearming require fresh history;
manual activation bypasses wake inference. Detectors without a `ready` property
retain the existing `armed` event with no readiness reason.

A read-only comparison using the existing private training environment verified
the saved assets' digests and compared three four-second synthetic inputs
(silence, noise and tone): **150 embeddings and 105 classifier windows matched
the reference with maximum absolute difference 0.0**. This proves synthetic
numerical parity. It does not measure wake recall, false wakes, far-field speech
or transfer through G.711. Independent room-distance acceptance of the model
and then telephony-specific evaluation remain necessary.

## HA protocol and headless replay

`HAClient.run(audio, conversation_id=None, on_event=None)` opens one WebSocket
per turn, authenticates, and sends `assist_pipeline/run` from `stt` to `tts` with
16 kHz input. It prefixes binary PCM with the one-byte handler received in
`run-start`; the handler byte alone ends audio. STT completion cancels a waiting
audio sender. Results include conversation ID, text, transcript and TTS URL/MIME.
Those are private application data and are not logged by this package.

HA 2026.10.2 can emit `run-end` before a timeout error. The client reads through a
WebSocket protocol ping/pong barrier and validates completed stages rather than
treating that first event as unconditional success. Authentication, input,
events, fetch size and overall time are bounded. TTS is fetched only from the
configured HA origin with no redirects or environment proxies; TLS validation
remains enabled. Async HTTP reads have a total deadline; cancellation closes
the response and owning session before returning. No executor read worker is
left running. HTTP content compression is rejected so byte limits
apply directly to the received speech payload.
[Assist API](https://developers.home-assistant.io/docs/voice/pipelines/),
[reviewed HA implementation](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/assist_pipeline/websocket_api.py).

For a deliberate HA test without phone audio, prepare a private 16 kHz PCM WAV
containing a synthetic informational question, review the selected pipeline,
and explicitly enable the service/opaque test-phone key in private config:

```sh
.local/voice-venv/bin/python -m voice_assistant replay \
  --config .local/voice/config.json \
  --input .local/voice/request.wav \
  --phone example-phone --transport aiohttp \
  --output .local/voice/reply.wav
```

This command contacts HA and can execute whatever intent is in that WAV. It
does not connect to a phone or audio device, bypasses wake inference for the
manual turn, paces input and saves the response only to a new `.local` WAV.
The CLI prints stage codes and byte counts, not recognized speech. WAV TTS works
directly; for compressed TTS use the embedding API with `FFmpegDecoder` supplied
to `build_service`. Live headless replay results and synthesized audio are kept
under `.local`; they do not establish physical-phone acceptance.

A separate Ollama integration entry, compact conversation agent and Assist
pipeline were created for the private candidate. Two live synthetic replays
completed in 23.98 and 15.80 seconds from activation through reply decoding into
an unpaced memory sink, both within the 30-second deadline. Each returned the
requested phrase as a 1.2-second reply. This baseline uses a short prompt,
2,048-token context, history of one and no language-model home-control tools;
built-in local intents run first. It retains cloud STT pending local-STT
integration. A 500-frame input queue provides a bounded ten-second startup
buffer because the original two-second queue overflowed before HA accepted
audio on the first attempt. These two results do not establish a general
latency guarantee. All prior integration entries and pipelines, including the
preferred pipeline, were preserved; the candidate config remains disabled.

A subsequent bounded receive-only phone session transmitted the saved reply
as 60 RTP frames, with the microphone disabled. Receive Stop was confirmed and
the watchdog finished with zero errors. The operator subsequently confirmed
hearing this reply. This separately verifies pipeline output and audible phone output; it does not establish a complete acoustic
microphone-to-answer turn or an audible-response latency measurement.

## Saved local-STT audit and candidate

The reviewed October 11–16 saved snapshots contain an enabled local Wyoming
Whisper STT entity, but show it as **unavailable** and its config entry as
**setup_retry**. The saved Russian pipeline still selected cloud STT, followed
by a local conversation agent and local Silero speech. A later speech-settings
snapshot selected the Silero default voice (Baya). These are saved observations,
not current service health or proof that a dedicated `localSTT` service works.
That historical audit was read-only and changed no Home Assistant configuration.
The later compact conversation candidate described above created separate
resources while preserving the existing configuration and preferred pipeline.
The wider saved deployment audit also found cloud-STT preparation and an
uninstalled HACS local-STT listing, but no successful local transcription or
healthy dedicated `localSTT` deployment. Private evidence paths remain outside
this public guide.

The later dedicated local service restored Russian transcription with a pinned
multilingual base model using CPU/int8. A separate Assist pipeline then combined
that verified local STT endpoint with the compact conversation agent and local
Baya speech, preserving all previous pipelines and the preferred selection.
Three synthetic full-cycle trials took 17.07 seconds for clean audio, 16.41 for
a PCMU round trip and 11.85 for a PCMA round trip. These measurements include
streamed input and delivery of the actual decoded reply PCM to a silent sink at
its sample rate; they exclude physical phone transport and acoustic latency.

Only the clean trial passed the transcript and reply checks. Both telephone
codec variants introduced a recognition error that the conversation agent
repeated in its reply. Raising the recognizer beam size from one to three or
five did not fix either fixture. Thus the base model passes this small latency
sample but **does not pass the telephone recognition gate**. A stronger local
model requires separate quality and latency qualification before replacing the
working base service. No general latency or acoustic acceptance claim follows
from these three trials.

The payload below is an unapplied example for isolating local STT/TTS with HA's
built-in conversation engine. It creates a separate pipeline only after each
provider is verified. It is a **WebSocket payload**, not `configuration.yaml`,
and is not either candidate already measured above. For a new installation,
verify the STT entity and benchmark clean and G.711 synthetic speech before
selecting the resulting pipeline for phone use:

```json
{
  "id": 101,
  "type": "assist_pipeline/pipeline/create",
  "name": "Local phone prototype",
  "language": "ru",
  "conversation_engine": "conversation.home_assistant",
  "conversation_language": "ru",
  "stt_engine": "stt.REPLACE_WITH_VERIFIED_LOCAL_WHISPER",
  "stt_language": "ru",
  "tts_engine": "tts.REPLACE_WITH_VERIFIED_LOCAL_SPEECH",
  "tts_language": "ru",
  "tts_voice": "silero-ru-default",
  "wake_word_entity": null,
  "wake_word_id": null,
  "prefer_local_intents": true
}
```

For a service still reporting `setup_retry`, first resolve its cause and
verify the real local endpoint. If a separate STT deployment is needed, qualify
an isolated, pinned Wyoming faster-whisper service with a pre-provisioned
multilingual model. Russian `small`, CPU/int8 is a benchmark candidate, not a
latency promise. Verify local-files-only loading in the chosen pinned version
and forbid runtime model downloads. Confirm Wyoming discovery advertises ASR
and Russian, then transcribe permitted synthetic audio directly.
[Wyoming integration](https://www.home-assistant.io/integrations/wyoming/),
[upstream local model loading](https://github.com/OHF-Voice/wyoming-faster-whisper/blob/master/wyoming_faster_whisper/__main__.py).

Store the returned explicit pipeline ID in private adapter config. Never omit
the ID or use `conversation.*` shorthand: HA can derive default STT/TTS, silently
reintroducing a cloud leg. `prefer_local_intents` and provider names are not
proof of offline operation. Verify actual endpoints and outbound traffic in an
isolated test; compare ordinary and G.711-degraded synthetic Russian speech,
transcript quality, stage latency, cancellation and concurrent requests before
attended phone acceptance.
[HA pipeline schema](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/assist_pipeline/pipeline.py),
[default-provider selection](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/assist_pipeline/default_pipeline.py).

The remaining acceptance gates are physical microphone indication/mute/Stop,
both-line call preemption, disconnect behavior, acoustic false-wake/recall
measurements, speech intelligibility, measured local-STT health/latency and
multi-device arbitration. This adapter's tests do not replace those gates.
