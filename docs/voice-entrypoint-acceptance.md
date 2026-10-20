# Offline wake entrypoint acceptance

The [audio pipeline check](voice-pipeline-acceptance.md) exercises the real
detector, codec, controller and HA protocol with service objects constructed
directly. It does not test whether the installed runner reads its configuration
and selects wake mode correctly.

`scripts/test-voice-entrypoint.py` covers that missing connection. It invokes
`voice_session.main` with disposable private configuration, through the real
transport and voice configuration loaders, `build_service` and the controller.
It uses the same private model bundle and qualified recording as the pipeline
check. The phone, datagram socket and HA peer remain in-memory substitutes.

This is an offline acceptance test. It must not be pointed at a household
configuration, real HA credentials, a phone or an audio device.

## Run the two scenarios

Use the qualified NumPy and ONNX Runtime environment described in the
[pipeline guide](voice-pipeline-acceptance.md). Run from the repository root:

```sh
umask 077
.local/voice-check-venv/bin/python scripts/test-voice-entrypoint.py \
  --private-root "$PWD/.local" \
  --bundle "$PWD/.local/wake/bundle.json" \
  --positive-wav "$PWD/.local/wake-fixtures/positive.wav" \
  --scenario all \
  --duration-seconds 8 \
  --timeout 120 \
  --json > .local/voice-entrypoint-report.json
```

Use a previously selected positive recording long enough for detector warm-up
and the following captured utterance. Preserve its provenance and the frozen
model threshold; do not tune either to make this test pass. Inputs and detailed
reports belong under ignored `.local`, with access restricted to their owner.

The positive scenario must load the model through `build_service`, select wake
mode, detect the phrase, pass the expected post-trigger PCM to the HA client and
produce the expected RTP response. The matched no-wake scenario replaces the
input with silence and must produce neither a HA conversation nor output audio.

The runner normally returns a nonzero exit status for an expired session without
a completed answer. That is the expected runner result for the no-wake control;
the acceptance tool distinguishes that outcome from a failed assertion.

## Use the shipped image

A successful run in a host virtual environment does not establish image
compatibility. For image acceptance, use the exact retained immutable wake
image and its installed Python, libraries and production source files.

Only these two test files need to be supplied at their usual paths if they are
absent from the image:

- `/workspace/scripts/test-voice-entrypoint.py`
- `/workspace/scripts/test-voice-pipeline.py`

Mount the test files and a disposable copy of the private inputs read-only.
Keep the production modules baked into the image: mounting the repository over
`/workspace` would test a different installation. The private input directory
must end in `.local`, and its files must be owned by the image's runtime user
with owner-only permissions. Change ownership only on disposable copies.

Run with networking disabled, no audio devices, no host credentials, a
read-only root filesystem and a bounded writable temporary filesystem. Retain
the image's unprivileged runtime user. Apply memory, process and CPU limits as
well as an outer container deadline; do not start the live wake Job.

Record the container runtime's inspected image identity and launch arguments
alongside the report. The report's platform, interpreter and source hashes
identify what the test observed, but cannot independently prove an immutable
container image identity.

## Interpret the result

Check the scenario assertions, runner exit status, audio lineage and cleanup
together. A model load or a nonzero byte count alone is insufficient.
The production entrypoint must release its resources before the fixture's
rescue cleanup. Recovery performed by the real runner is part of the production
path; recovery performed only by the test cannot make a failed run pass.

This check uses a generated synthetic configuration and a scripted HA response.
It does not validate the deployed household profile, real STT or intent
processing, TTS voice quality, phone loudspeakers, room acoustics, Kubernetes
startup or the reservation wrapper. Those remain separate acceptance steps.
