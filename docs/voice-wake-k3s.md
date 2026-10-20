# One-shot wake assistant on k3s

This opt-in pilot connects the stock phone's microphone, a private frozen wake
model, the selected Home Assistant pipeline and RTP speech playback. It uses
the [optional wake image](voice-wake-runtime.md). It does not modify firmware.
It is not an always-on assistant: one deliberate start gives one session of
at most 30 seconds, including wake waiting and speech processing.

![Private wake image, model staging and shared session ownership](diagrams/voice-wake-k3s.svg)

## Prepare the runtime

1. Build the optional image from the exact verified manual source/image pair.
   The **Optional wake image** GitHub Actions workflow builds both images,
   runs their offline smoke checks, and optionally exports one amd64 artifact
   for one day. It uses a standard public-repository runner and requires no
   paid marketplace action or external build account.
2. Import that artifact into the selected node's container runtime. Verify its
   archive checksum, source receipt, image configuration and platform. Resolve
   the imported image to an immutable `repository@sha256:...` reference.
3. Prepare an existing private PVC containing the three model files. Preserve
   the paths and SHA-256 values from the qualified bundle. The files must be
   readable by UID/GID `10001`; all path components must be real directories,
   with no symlinks. The tool does not download models or change this volume.
4. Find the **exact existing host directory** used by the gateway's voice
   controller for its `.lock` and `.json` journal files. The last component is
   `voice-control`; the directory must already be owned by UID `10001` with
   mode `0700`. Reuse it. A separate empty directory would defeat cross-process
   microphone ownership.

The renderer cannot prove that an operator-supplied host path is the gateway's
actual mount. Compare the live gateway's mount and private registry before
running a pilot. It deliberately uses `hostPath.type: Directory`, never creates
a missing directory, and does not set `fsGroup` on existing storage.

The model PVC is mounted read-only into a non-root init container. A separate
initializer creates the fresh private runtime directory; only that initializer
runs as root, with `CHOWN` and no access to models, credentials or host state.
Staging checks three digests and limits each model to 16 MiB, with a 32 MiB total.
It copies verified bytes to owner-only files. The running assistant receives
those files read-only and cannot access the source PVC.

## Render without activating a microphone

Keep all actual inputs under the ignored `.local` directory, with mode `0600`.
Use the existing site, transport and assistant schemas. Set `wake.mode` to
`onnx`, enable only the selected physical phone, and keep `rearm_after_turn`
false. Either phone line can select that same physical phone. Use the
[qualified local pipeline](local-stt.md) if offline processing is required.

```sh
python3 scripts/render_voice_wake_job.py \
  --site .local/site.json \
  --transport-config .local/voice/transport.json \
  --voice-config .local/voice/assistant.json \
  --bundle .local/voice/bundle.json \
  --voice-image 'registry.example/voice-wake@sha256:REPLACE_WITH_VERIFIED_DIGEST' \
  --models-claim private-wake-models \
  --state-host-path /srv/telephony/state/voice-control \
  --display-port 18110 \
  --output .local/voice/wake-job.json
```

Names and paths in this example are placeholders, not a household inventory.
The output is an exclusive, owner-only file containing a small Secret and a
**suspended** Job. It contains credentials; never commit it or attach it to a
public issue. Model bytes do not enter the Secret, repository, image or artifact.
Rendering makes no network request and does not contact a phone.

Review the exact node, phone, ports, immutable image, PVC and shared state path.
The output has no automatic retry: `backoffLimit: 0`, `restartPolicy: Never`,
one completion, `podReplacementPolicy: Failed`, and no CronJob. Kubernetes can
still create a replacement Pod in some failure cases. The renderer therefore
passes a stable attempt ID derived from this Job's revision; the runner consumes
that ID durably before starting media. Recreating the same Job or restarting its
Pod cannot replay the attempt.

After an attempt has been consumed, a new attended attempt requires rendering a
new manifest with a deliberately new `--name`, such as `phone-wake-pilot-02`.
First verify that the previous session finished and its media cleanup is
confirmed. This applies even when the previous attempt failed before a useful
answer. Never delete `wake-attempt-*.consumed` files to make an old attempt run
again. Changing configuration also changes the Job identity; it does not
authorize overlapping attempts or bypass an unresolved ownership journal.

Kubernetes can start the same Job program more than once even with one
completion and `restartPolicy: Never`. The durable attempt record is therefore
required in addition to the Job settings.
[Kubernetes Job failure handling](https://kubernetes.io/docs/concepts/workloads/controllers/job/#handling-pod-and-container-failures).

## Start and stop an attended attempt

Apply the reviewed manifest with an explicit kubeconfig and context:

```sh
kubectl --kubeconfig .local/kubeconfig --context YOUR_CONTEXT apply \
  -f .local/voice/wake-job.json
```

The applied Job remains suspended. Read its exact name from the private
manifest; do not select a Job using an unbounded label or wildcard. When ready
at the phone, start that one Job:

```sh
kubectl --kubeconfig .local/kubeconfig --context YOUR_CONTEXT \
  -n YOUR_NAMESPACE patch job EXACT_JOB_NAME --type merge \
  -p '{"spec":{"suspend":false}}'
```

Wait for **Listening** on the phone. The frontend first needs 1.28 seconds of
actual microphone history. Say the private wake phrase, then a short question.
The display progresses through Listening, Thinking and Speaking. Use its
**Stop** softkey to finish. The normal SIP call observer also ends capture when
the phone becomes busy, unavailable or uncertain.

For a remote stop, terminate this exact Job with its normal grace period:

```sh
kubectl --kubeconfig .local/kubeconfig --context YOUR_CONTEXT \
  -n YOUR_NAMESPACE delete job EXACT_JOB_NAME --grace-period=75
```

Do not use force deletion or a zero grace period as a normal stop mechanism.
The process forwards shutdown to the session owner and preserves time for the
media supervisor to confirm closure. A dead host or broken network can still
prevent remote Stop; inspect the handset if confirmation is unavailable.

This standalone process shares the gateway's reservation journal, so a gateway
Start refuses concurrent ownership of the same physical phone, even through
its other line. The existing gateway cannot signal another process's child.
Its status may report `recovery_required` / `voice_owner_unavailable` while the
Job owns the device. Use the phone Stop or the exact Job's termination, not the
ordinary `cisco LINE voice stop` command, for this pilot.

The Job has a 120-second outer deadline for startup and cleanup. This does not
extend the owner's 30-second media session. Keep the last structured result
private and check both conversation outcome and Stop confirmation. A failed or
ambiguous result retains its durable reservation; never delete a journal or
lock file to make a retry pass.

## Collect the result and remove temporary credentials

The Job and its Pods expire five minutes after completion. Capture the main
container's output while the exact Job still exists, using a private file:

```sh
umask 077
kubectl --kubeconfig .local/kubeconfig --context YOUR_CONTEXT \
  -n YOUR_NAMESPACE logs -f job/EXACT_JOB_NAME -c wake-pilot \
  > .local/voice/wake-result.log
```

If startup failed before the main container ran, inspect the same Job's
`prepare-private-runtime` or `stage-pinned-models` container instead. The main
result separates two outcomes:

- `turn_success` and `success` are true only for a completed conversation turn
  with confirmed cleanup and a successful runner exit.
- `cleanup_success` reports successful release of the session's media ownership.
  It can be true after a manual Stop or an expired lease even though the
  conversation did not finish. Inspect `reason` and `voice.stop_confirmed`.

A successful init container only proves preparation. Deleting a Job during a
remote Stop can also remove its log source, so capture output before deletion
when available.

The generated Secret is a separate object and **does not expire with the Job**.
After this Job's Pods have terminated and no other pilot uses the same Secret,
read its exact name from the private manifest and remove that Secret:

```sh
kubectl --kubeconfig .local/kubeconfig --context YOUR_CONTEXT \
  -n YOUR_NAMESPACE delete secret EXACT_SECRET_NAME_FROM_MANIFEST
```

The model PVC and shared gateway reservation directory remain in place.
Deleting temporary Kubernetes objects never clears an unresolved reservation
or proves that the phone stopped sending audio. Preserve uncertain state for
inspection and confirm the physical phone's state before another attempt.

## Acceptance and limits

Offline tests cover private staging, source replacement, concurrent
initialization, malformed configuration, shared gateway/Job exclusion,
configuration changes, duration expiry, and uncertain cleanup. The optional
image smoke executes real ONNX Runtime inference on a generated weightless
graph. These checks do not establish room-distance wake accuracy.

The existing local speech pipeline took roughly 17–27 seconds in synthetic
full-cycle tests. Waiting for a wake phrase and loading the model consume part
of the same short session; a slow reply can hit the limit. A pilot may therefore
demonstrate working components without completing every spoken request.

Before unattended use could be considered, measure actual wake accuracy,
false activations, physical Mute, call preemption and recovery after network
loss. No such unattended mode is enabled here. Stock 7945G firmware still
provides XML images and RTP audio, not native incoming SIP video decoding.
See the [implementation evidence](voice-rollout.md) and
[firmware feasibility](native-app-feasibility.md).
