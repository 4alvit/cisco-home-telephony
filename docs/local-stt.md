# Local Russian speech recognition repair

Assessment date: **19 October 2026**. This work restores and evaluates a dedicated
local STT backend. Home Assistant pipelines and shared services are unchanged.
Deployment-specific manifests, endpoints, model evidence
and synthetic test material remain under
`.local/voice-rollout-20261019/stt-repair/`.

## Confirmed cause

The saved `stt.faster_whisper` entity is unavailable because its dedicated
Wyoming backend deployment has **zero replicas**. The enabled Home Assistant
Wyoming config entry points at the existing Kubernetes Service on TCP 10300,
but that Service has no endpoints. This is a missing running backend, not a
missing entity, disabled integration or mismatched configured Service name.

This was **intentional retirement of an unsuccessful experiment**, not a new
failure in an active STT service. Retained deployment notes record roughly
138 seconds for local recognition on the then-busy host and 221.019 seconds
until completed answer audio in one synthetic run. The notes explicitly kept
the model caches while stopping the workloads. These historical timings are
not a new benchmark and are not attributed to the cloud provider.

The deployment's Kubernetes `Available=True` condition is not a usable STT
health check when its desired replica count is zero. Check a ready backend
endpoint and a Wyoming `Describe`/`Info` exchange instead.

The existing assistant profile still selects a cloud STT provider. Restoring
Whisper will restore an alternative provider; it does not automatically change
that profile or make the entire conversation path local. Verify each stage
independently and use an explicit pipeline ID.

## Repair approach

Preserve the existing Wyoming endpoint where practical so Home Assistant's
normal integration retry can reconnect. A simple scale-up restores a listener
but does not resolve the previously unacceptable latency. Prepare the dedicated STT deployment
with one replica, a digest-pinned image, an explicitly selected multilingual
model and CPU settings, bounded resources, a long startup allowance and a
readiness check. Keep model acquisition separate from recognition serving and
verify the server loads its prepared local model without runtime downloads.
The retained local-path volume is tied to its original node; a different
placement needs a separately prepared cache rather than silently moving it.

The inspected pinned image contains Wyoming Faster Whisper 3.8.1. The candidate
selects the `faster-whisper` backend explicitly; automatic backend selection is
not a reliable way to hold the model constant. It uses Russian, CPU int8, two
threads and beam size one as a test configuration, rather than preserving the
retired workload's float32 setting without measurement.

The model bundle must include **both weights and tokenizer data**. The inspected
`base-int8` model repository lacks `tokenizer.json`; the inference library can
otherwise fall back to loading a tokenizer from a separate repository. The
private model lock therefore records an exact model revision, an exact
compatible multilingual tokenizer revision, lengths and SHA-256 hashes.
`--local-files-only` by itself is not the complete offline guarantee: validate
the bundle, set offline library flags and block serving-time egress. Model
acquisition belongs in an explicit preparation stage.

Upstream documents external Wyoming services, explicit backend selection,
local-only loading and a protocol-level health check. The private candidate
additionally checks these options against the source from the exact image
digest, rather than assuming the latest README matches a deployed image.
[Home Assistant Wyoming integration](https://www.home-assistant.io/integrations/wyoming/),
[Wyoming Faster Whisper](https://github.com/OHF-Voice/wyoming-faster-whisper).

No phone microphone, wake phrase, household recording or cloud speech service
is needed for this gate. Use a generated Russian fixture, retain its expected
transcript privately, and compare normal PCM with the same fixture after a
G.711/8 kHz round trip. Synthetic recognition proves the pipeline can execute;
it does not measure room-distance accuracy, acoustic false wakes or real
speaker recognition quality.

## Acceptance sequence

1. Review the private candidate manifest against the captured deployment,
   image digest, model hashes, node capacity and current endpoint. Save the
   original deployment for rollback, prepare the separate model directory,
   then apply only the dedicated STT workload.
2. Confirm the model loads, the pod becomes ready and the Service has a ready
   endpoint. A successful TCP connection alone is insufficient: request
   Wyoming capabilities and verify a multilingual ASR model supports Russian.
3. Send the synthetic PCM fixture directly to STT. Record returned text,
   recognition duration, model identity and failures. Repeat with telephony
   degradation and test cancellation/concurrency separately.
4. Confirm Home Assistant's existing STT entity becomes available through
   integration retry. Do not restart Home Assistant merely to repair a missing
   external service. If retry does not recover, inspect that entry's error
   before proposing a targeted reload.
5. Create or explicitly select a separate local test pipeline only after its
   STT, conversation and TTS providers are verified. Preserve the existing
   preferred pipeline until a deliberate rollout decision.

For the adapter contract and local pipeline candidate, see
[the local voice assistant guide](local-voice-assistant.md). The test must
distinguish **prepared**, **deployed**, **direct STT verified**, **HA available**
and **phone acceptance**; none implies the next.
