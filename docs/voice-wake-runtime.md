# Optional local wake runtime

The optional wake image adds CPU inference to the existing voice image. It uses
the same bounded phone session, G.711 audio conversion, Assist connection and
Stop behavior. It does not install firmware, start a microphone, bundle a wake
phrase or download a model. The lean manual image remains separate.

The exact frozen frontend is `echomuse-oww-16k-v1`: 16 kHz PCM16, 1,280-sample
hops, 480 samples of left context, 76 mel frames, and a `[1,16,96]` classifier
input. It first becomes ready after 1.28 seconds of input. The private bundle
supplies the threshold; the existing detector uses a 19-hop, 1.52-second
refractory period. A new stream, gap or stop must reset its audio and model
history. See [the voice coordinator](local-voice-assistant.md) and
[offline qualification](wake-qualification.md).

## Build a separate image

Requirements: Docker, Git and Python 3 on the build host. The base must be the
verified manual voice image from the corresponding source commit, identified
by an immutable registry digest. The optional layer supports CPython 3.11 and
3.12 on Debian bookworm, `linux/amd64` and `linux/arm64`.

From the repository root:

```sh
bash scripts/build-voice-wake-image.sh \
  --base-image 'registry.example/voice@sha256:REPLACE_WITH_VERIFIED_DIGEST' \
  --base-source-ref 'REPLACE_WITH_MANUAL_IMAGE_SOURCE_COMMIT' \
  --tag registry.example/voice-wake:pilot \
  --platform linux/amd64
```

Use the digest and source commit from the base image's verified build receipt.
The helper records expected base-source hashes from that Git commit, so
unrelated working-tree changes are not silently substituted. The local commit
must be available; fetch that exact revision if necessary.

For an unpushed manual image built by the same CI job, use its complete local
image ID instead of `--base-image`:

```sh
MANUAL_IMAGE_ID=$(docker image inspect cisco-home-ci/voice:pilot --format '{{.Id}}')
bash scripts/build-voice-wake-image.sh \
  --base-image-id "$MANUAL_IMAGE_ID" \
  --base-source-ref "$GITHUB_SHA" \
  --tag cisco-home-ci/voice-wake:pilot \
  --platform linux/amd64
```

The helper checks the local image ID and architecture, creates a unique
temporary alias, and verifies that alias still identifies the same image both
before and after building. It removes only that temporary alias afterwards.
No registry pull is needed for this route. Registry bases are pulled by their
explicit digest and their architecture is also verified before building.

The helper creates a temporary allowlisted build context containing only the
optional image files, the wake implementation and a generated base-source
manifest. It never passes the repository root, `.local/`, model files or
recordings to Docker. It builds a separate tag and then runs both the inherited
manual smoke and the optional smoke against the resulting immutable local
image ID, with networking disabled, a read-only root and non-root UID 10001.
It does not push or deploy.

The optional lock pins NumPy 1.26.4, ONNX Runtime 1.23.2 and every required
transitive dependency with PyPI SHA-256 hashes. Installation accepts binary
wheels only. These two inference versions match the existing frozen-frontend
parity check; dependency changes require a new parity check before release.
The optional image has its own size and build receipt; do not place it into
the lean manual image's existing CI artifact budget.

The smoke checks:

- Exact base-source compatibility with the supplied commit.
- Exact installed dependency versions and CPU inference provider.
- Real ONNX Runtime inference on a tiny generated Identity graph with no
  weights, household data or model downloads.
- Existing manual CLI, private-file staging and playback-decoder checks.

This establishes image integrity and runtime availability. It does not claim
that a particular wake phrase was recognized or that a physical microphone
worked. The private frozen bundle still needs its separate hash and acoustic
acceptance checks.

## Runtime paths and private assets

The runtime retains the manual image's interface:

```text
/workspace/scripts/voice_session.py
/workspace/voice_assistant/wake.py
/workspace/containers/asterisk/
/opt/voice/stage_private.py
/opt/voice-wake/stage_models.py
/opt/voice-wake/run_reserved.py
/opt/voice-wake/smoke.py
/run/voice/.local/config/
/run/voice/.local/state/
```

Mount the private model source separately. Stage the manifest and all three
ONNX assets into ordinary owner-readable files inside the runtime workspace's
`.local/` directory; use the model staging tool supplied with the image.
The bundle contains `format_version`, `frontend`, `threshold`, and
`models.{mel,embedding,classifier}.{path,sha256}`. Model paths are relative to
the manifest. The runtime verifies every digest before creating CPU sessions;
it has no implicit model-discovery or network-download fallback.

Use a private volume for the models. An individual Kubernetes Secret is limited
to 1 MiB, which is smaller than typical frontend/classifier files; a model
bundle must not be put in one oversized Secret. Credentials can remain in the
existing small Secret. [Kubernetes Secret size limit](https://kubernetes.io/docs/concepts/configuration/secret/#size-limit).

Set the selected assistant profile's `wake.mode` to `onnx` and
`wake.bundle_file` to the staged manifest. Keep the model threshold unchanged
from its qualification receipt. For repeatable phone/CLI Start, use the
[gateway renderer with a model PVC](voice-gateway-deployment.md#enable-repeatable-wake-sessions).
The separately owned wake Job remains available for bounded acceptance attempts.

Keep the bounded foreground session and visible Stop control. No background
listening starts merely by installing this image. A configured finite wake session
may rearm after successful playback; Stop, error and expiry do not restart it. A full
speech test must still verify the linked microphone → wake → selected STT →
conversation → speech playback path on the chosen physical phone.

## Diagnose a failed build or start

- **Base source revision mismatch:** the digest and source commit do not
  describe the same manual image. Resolve the correct build receipt; do not
  disable the compatibility check.
- **No compatible wheel:** use an advertised architecture and CPython version.
  Source builds and silently selecting a newer inference version are not a fix.
- **Missing NumPy or ONNX Runtime:** the selected image is the lean manual
  variant, or its optional build failed. Verify the deployed digest.
- **Model checksum or private-path rejection:** repair the staged files or
  manifest; never replace the expected hash without verifying the model source.
- **No detection immediately after Start:** wait for the display to report
  Listening after the 1.28-second model-history warmup. A successful import is
  not proof that meaningful audio has reached the model.
- **Recognition changes over G.711:** test the frozen model with the actual
  narrowband conversion and household conditions. Resampling 8 kHz to 16 kHz
  does not restore the frequencies lost by the telephone audio path.
