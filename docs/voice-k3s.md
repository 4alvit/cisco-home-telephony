# Suspended voice-session pilot on k3s

For persistent CLI, phone-menu and Home Assistant controls, use the
[voice gateway deployment guide](voice-gateway-deployment.md). This page covers
the separate, finite attended-session Job and its image packaging.

`scripts/render_voice_job.py` prepares a private Kubernetes manifest for one
attended, manually activated voice session. Rendering is offline: it reads
local inputs and writes a manifest without contacting Kubernetes, Home
Assistant or a phone. The rendered Job starts **suspended**. Rendering or
reviewing it does not activate a microphone.

This is a packaging prototype for the [bounded session runner](../scripts/voice_session.py),
[RTP transport](voice-transport.md) and [Assist adapter](local-voice-assistant.md).
It is not an unattended voice service or a replacement for the existing
Asterisk workload. The operator owns all cluster deployment and live
phone actions.

## What the manifest contains

The renderer captures an explicit allowlist of 13 public Python files in a
ConfigMap. It can also include a locally supplied pure-Python `websockets`
15.0.1 wheel in `binaryData`. The private transport configuration, minimized
gateway configuration, voice configuration and HA token go into a Secret,
mounted as `/run/voice/.local/transport.json`, `gateway.json`, `voice.json`
and `ha-token.txt`. The gateway copy retains the selected physical phone with
both lines, its execution credentials, AMI credentials and required validation
fields. Unused API fields contain inert values; the pilot does not launch a
gateway server. Unrelated gateway API tokens, HA application credentials, room
labels and other metadata are excluded. Model weights are not packaged; the
initial runner requires manual activation and does not wait for a wake phrase.

Public source files are projected under `/workspace`. The Secret has its own
mount at `/run/voice/.local`, and the session runner uses `/run/voice` as its
private workspace. This keeps Kubernetes' projected-file links inside the
private directory accepted by the runtime path guard; the guard is not relaxed
to permit private files elsewhere.

The Job can use a dedicated immutable voice-runtime image through
`--voice-image`. This overrides only the pilot's image; it does not modify the
site configuration or the running PBX. Omitting the override retains the
site's `asterisk_image` as a compatibility fallback for a runtime whose
dependencies have been verified.

The voice runtime must provide Python 3.11 or later, `aiohttp`, `websockets`
15.x, Pillow and FFmpeg. The current TTS fetch path requires `aiohttp` even when
the Assist WebSocket uses `websockets`. A read-only inspection of the existing
PBX image found Python 3.11.2, Pillow 9.4 and FFmpeg 5.1.9, but neither
`aiohttp` nor `websockets`. That image is not a runnable current voice runtime.
Adding the optional WebSockets wheel alone does not supply the missing TTS
dependency. Keep Python packages and their transitive dependencies in the
runtime image rather than expanding the ConfigMap into a dependency archive.
The pilot performs no runtime package installation.

The workload uses these constraints:

| Setting | Effect |
| --- | --- |
| `suspend: true` | The operator must explicitly release the reviewed Job. |
| One completion and one parallel worker | Intended for one bounded manual session. |
| `restartPolicy: Never`, `backoffLimit: 0` | No configured container restart or failed-Job retry. |
| `activeDeadlineSeconds: 90` | Kubernetes bounds the active Job lifetime separately from the media deadline. |
| `terminationGracePeriodSeconds: 45` | Allows graceful shutdown before Kubernetes can force termination. |
| `ttlSecondsAfterFinished: 300` | Finished Jobs become eligible for cleanup after five minutes. |
| Node pinning and `hostNetwork: true` | RTP uses the selected node's LAN address and ports. |
| No service-account token mount | The voice process receives no Kubernetes API token. |
| User/group 10001, read-only root filesystem, all capabilities dropped, `RuntimeDefault` seccomp | Limits container privileges and writes. |
| Requests: 100m CPU, 128 MiB memory, 64 MiB ephemeral storage | Reserves resources for the pilot. |
| Limits: 1000m CPU, 512 MiB memory, 256 MiB ephemeral storage | Sets the pilot's resource limits. |
| `/tmp` backed by a 64 MiB `emptyDir` | Provides bounded writable temporary storage. |

`hostNetwork` shares the node's network namespace. Before a live start, the
operator must reserve the configured RTP and optional display ports and verify
that the transport bind address belongs to that node. This manifest does not
change the existing PBX, HA pipeline, phone provisioning or node firewall.
The renderer requires the transport bind address, gateway bind address and AMI
host to equal `site.node_ipv4`, with the gateway enabled and its AMI client set
to that node. Its port checks detect configured collisions, not other live
processes already using a node port.

## Build and check the voice runtime

The dedicated [Dockerfile](../containers/voice/Dockerfile) and
[dependency lock](../containers/voice/requirements.lock) package the runtime
dependencies independently of the PBX. From the repository root, on a host
with Docker available:

```sh
bash scripts/build-voice-image.sh cisco-home-ci
```

The build script tags `PREFIX/voice:pilot`, runs the smoke check automatically,
and never pushes it. Its default base is `python:3.12-slim-bookworm`, resolved
to an immutable digest before building. `PYTHON_IMAGE` can select a reviewed
official Python 3.11 or 3.12 `slim-bookworm@sha256:...` reference. The locked
runtime supports those two Python versions on Linux amd64 and arm64;
`BUILD_PLATFORM` defaults to `linux/amd64` and can select `linux/arm64`.
The build context contains only approved public source and runtime files.
Building can fetch base images, OS packages and locked Python packages;
that build-time activity is separate from the offline renderer and the
runtime's no-install policy.

To smoke-test an already built local image separately:

```sh
bash scripts/smoke-voice-image.sh cisco-home-ci/voice:pilot
```

The smoke script runs `/opt/voice/smoke.py` with networking disabled, a read-only
filesystem, a temporary filesystem, dropped capabilities and a non-root user.
It uses the existing local image ID without pulling another image. Checks cover
locked versions and imports, a Pillow round trip, synthetic FFmpeg decoding,
AtomicWriter input staging, control configuration, gateway attach/status and
session CLI validation. The gateway check uses synthetic state without starting
listeners or the background reconciliation loop. A passing report includes `image_smoke: "passed"`,
`network_actions: 0` and `microphone_started: false`. It does not use phone or HA
credentials. A passed smoke test is evidence of runtime dependencies and local
processing, not live HA connectivity, phone compatibility or microphone Stop.
The gateway-capable revision passed the full
[CI build and smoke run](https://github.com/4alvit/cisco-home-telephony/actions/runs/39357347912)
at source commit `5475af4`. Its voice-only export passed the storage guard and
artifact checksum/provenance checks. This establishes that exact image's build
and synthetic behavior, not deployment or live phone acceptance. The operator
must still verify transfer, the imported manifest digest and the actual private
runtime configuration on the selected node.

Before rendering for a live pilot, set `VOICE_IMAGE` to the verified immutable
`repository@sha256:...` reference and ensure that exact image is available on
the selected node. Availability can come from a registry or a reviewed CI
image archive imported into the node's container runtime; registry publication
is not required. A mutable `:pilot` tag is useful for the local smoke command
but is rejected as the Job's image reference.

## Private inputs and source snapshots

Keep all configuration inputs, referenced gateway/token files and generated
output inside the selected workspace's `.local` directory. Relative input and
reference paths resolve from that workspace; absolute paths must still remain
inside its `.local` directory. The transport configuration identifies one phone
through either of its gateway lines. The voice configuration must enable only
that physical phone's primary ID and use an explicit Assist pipeline. The
renderer requires `service.enabled: true`, `wake.mode: "manual"` and
`rearm_after_turn: false`. The rendered session still requires the RTP ownership
and physical attendance conditions described in [voice transport](voice-transport.md).

An HA token authorizes real Assist requests after the Job is released. Running
the client on a local node does not make the selected STT, conversation or TTS
providers local. Confirm the intended pipeline separately; packaging does not
change its providers.

The ConfigMap is a **snapshot**, not a live mount of the checkout. Regenerate the
manifest whenever a captured source file, runtime image, dependency wheel or
private input changes, then review the new resources before release. Render
the deployment snapshot after the final transport, display and voice changes
are in place, and validate that source version with the chosen runtime. Do not
assume editing a local Python file updates a previously rendered or deployed
Job.

Treat the output manifest as a credential-bearing file. Secret values are
encoded for Kubernetes, not encrypted by base64. Keep it out of Git, public
reports and shared logs. Secret access and encryption at rest depend on the
existing cluster configuration. [Kubernetes Secret guidance](https://kubernetes.io/docs/concepts/configuration/secret/).

## Render locally

From the repository root, prepare the private configuration files, set
`VOICE_IMAGE` to the verified runtime digest described above, and run:

```sh
python3 scripts/render_voice_job.py \
  --site .local/site.json \
  --transport-config .local/phone-voice-transport.json \
  --voice-config .local/local-voice-assistant.json \
  --voice-image "$VOICE_IMAGE" \
  --output .local/voice-job.json
```

The output's parent directory must already exist. The renderer creates a new
mode-0600 file and refuses to overwrite it; choose a new private output name
when regenerating a snapshot. Paths containing parent traversal or symlinks
below the workspace are rejected.

This creates Kubernetes JSON, also valid YAML. It does not apply resources,
download dependencies or run a session. There is no renderer `--run` flag;
every rendered Job is suspended. Optional arguments are:

| Argument | Purpose |
| --- | --- |
| `--workspace ROOT` | Selects the repository working-tree root containing the captured sources and private inputs. |
| `--voice-image IMAGE` | Uses a separate immutable `repository@sha256:...` voice runtime for this Job. |
| `--websockets-wheel .local/dependencies/websockets-15.0.1-py3-none-any.whl` | Includes the local wheel after validation against the pinned official hash. |
| `--display-port PORT` | Enables the optional phone status/Stop display server on a validated non-conflicting port. |
| `--name phone-voice-pilot` | Selects a 1–32-character lowercase DNS-label base name beginning with a letter. |

If the wheel is omitted, the image must already provide compatible
`websockets` 15.x. Including the wheel adds it to `PYTHONPATH` for direct ZIP
import; there is no package installation or writable dependency directory.
This option remains compatible with older packaging, but supplies only
`websockets`; the image must still supply `aiohttp`, Pillow and FFmpeg.
The source allowlist and optional wheel are captured from local files at render
time. Generated object names combine the base name and content hashes rather
than private phone identities.

## Review and explicit release

The operator reviews the generated source snapshot, image digest, node,
private configuration references, ports, duration and security settings before
any deployment. Deployment must preserve `spec.suspend: true`. Only after the
attendant is at the phone, both lines are idle, and the XML media path is
reserved should the operator explicitly change that Job's `spec.suspend` to
`false`.

The manifest's startup arguments already contain the live-session, attendance,
exclusive-media and microphone flags. No Pod is started while the new Job is
suspended. Unsuspending is the live-action boundary: it authorizes the entire
bounded microphone, Assist and reply-playback session. Do not put the manifest in a controller
that automatically clears suspension or periodically recreates Jobs. Do not
use suspend/resume as a retry mechanism; resuming restarts the Job deadline,
and suspending a running Job terminates its Pods. A subsequent attempt requires
another deliberate review and physical confirmation that the previous session
has stopped. [Kubernetes Job suspension](https://kubernetes.io/docs/concepts/workloads/controllers/job/#suspending-a-job).

Even with one completion, one parallel worker and no container restart,
Kubernetes does not guarantee that the program starts exactly once. Treat an
unexpected additional Pod or restart as a failed pilot, stop further starts,
and inspect the physical phone before continuing. Do not expand this design
into unattended operation without durable session ownership and duplicate-start
handling. [Kubernetes Job failure semantics](https://kubernetes.io/docs/concepts/workloads/controllers/job/#handling-pod-and-container-failures).

## Stop, evidence and cleanup

The voice runner retains its shorter, at-most-30-second media deadline and
independent watchdog. The 90-second Job deadline is additional process cleanup,
not a promise that the phone stops streaming at that instant. Hard Pod
termination, node failure or resource exhaustion can kill the runner and its
watchdog together. **There is no hardware-enforced microphone lease.** The
attendant must verify physical closure and use local phone controls if remote
cleanup is uncertain.

Keep the bounded runner's state, packet counters and cleanup result as private
evidence. A completed Kubernetes Job does not by itself prove microphone
closure, audible playback, acceptable transcription or successful call
preemption. Conversely, terminating a Pod does not prove the phone has accepted
an RTP Stop. This wrapper does not enable recordings or transcript logging.

The Job TTL is not a complete credential-cleanup mechanism. It applies after
the Job finishes and removes dependents through ownership relationships. The
rendered ConfigMap and Secret have no Job owner reference, so they remain after
Job cleanup. The operator must remove the exact pilot ConfigMap and Secret
after collecting results and confirming no Pod uses them. A suspended Job
which never finishes also does not reach the finished-Job TTL. Avoid broad
namespace deletion or selectors that could match the PBX's resources.
[Kubernetes finished-Job cleanup](https://kubernetes.io/docs/concepts/workloads/controllers/ttlafterfinished/).

## Troubleshoot the control-plane path first

An API timeout from the operator's laptop does not prove the cluster is down.
During rollout, an accepted VPN subnet route intercepted the laptop's API
traffic while a LAN node could still reach the same server. Compare the actual
route and a request from a known local node before changing the deployment.
A temporary loopback-only SSH forward can isolate that routing problem while
retaining the existing client credentials, CA verification and the API server's
original TLS name. Do not disable certificate verification or expose the tunnel
to other hosts. Keep the exact addresses and temporary kubeconfig private.

Check API readiness separately from authenticated reads and running workloads.
In the observed cluster, pod listing worked while the `etcd-readiness` check
failed. Logs identified a Kine/SQLite datastore and CPU contention, so the
check's name alone was insufficient to identify the storage backend. The
control-plane host had a long runnable queue and high CPU pressure while a
backup compression process competed for CPU; disk space was not exhausted.
Inspect process ownership and CPU, memory and I/O pressure before restarting
services. A bounded change to a verified background process's priority is
different from stopping its backup; record the original priority and a guarded
restore procedure. Schedule model preparation and recognition away from a
CPU-starved control-plane node.

## Local validation

Run the renderer's synthetic tests without cluster access:

```sh
python3 -m unittest discover -s tests -p 'test_render_voice_job.py'
```

These checks validate manifest construction and input rejection. Image runtime
dependencies, cluster admission, node reachability, physical audio behavior,
call priority and watchdog behavior under Pod failure remain acceptance gates.

## Existing control gateway: gated runtime replacement

The dedicated image can also package the
[bounded voice control gateway](voice-control.md), which owns sessions across
separate Start, status and Stop requests. The intended deployment replaces the
single existing gateway while preserving its configuration, ports and state.
Asterisk remains unchanged. `render_voice_job.py` still emits only the suspended
manual Job described above; it does not render or deploy a gateway replacement.

The gateway reconciles DND preferences and other phone controls in its
background loop. Another instance with a fresh database could reset preferences
even without a voice Start request. **Do not start a second gateway or replace
the existing preferences database with an empty one.** Audit the current
deployment and volume mapping first, preserving the existing `gateway.sqlite3`
and any voice ownership journals before changing the runtime image.

The first validation gate is a separate preflight Job with audit hooks denying
all network and hardware operations. It should validate imports, staged files,
configuration and state paths without starting listeners or a voice session.
Passing preflight does not authorize replacement or establish live phone
acceptance. This guide does not create or run that Job.

Preserve the image's source layout and use separate runtime storage:

| Path | Gateway role |
| --- | --- |
| `/workspace` | Immutable image sources; do not cover them with the manual Job's source projection. |
| `/run/voice-inputs` | Read-only immutable input Secret, including an explicit staging manifest. |
| `/run/voice/.local/config` | Fresh real `emptyDir` directory, owned by user 10001 with mode 0700; staged files are real mode-0600 files. |
| `/run/voice/.local/state/gateway` | Persistent real child directory containing the preserved gateway database. |
| `/run/voice/.local/state/voice-control` | Persistent real child directory containing voice ownership journals. |

The control loader rejects symlinks throughout its private paths. Mounting a
Kubernetes Secret directly as its configuration is therefore unsuitable. The
staging helper may read Kubernetes AtomicWriter links within the input root,
then writes only explicitly listed, bounded files to the real configuration
directory. It does not relax the control loader's privacy checks. Both state
child directories must be owned by user 10001 with mode 0700. Setting `fsGroup`
alone does not change a mounted volume root's owning UID; verify the actual
storage mapping and provision owned child directories rather than assuming the
PVC root satisfies the loader.

The input Secret's `staging.json` lists flat `.json` and `.txt` filenames, for
example:

```json
{
  "version": 1,
  "files": [
    "gateway.json",
    "voice-control.json",
    "transport.json",
    "voice.json",
    "ha-token.txt"
  ]
}
```

Set `immutable: true` on this input Secret and use a new Secret for each changed
configuration. A read-only mount alone does not prevent kubelet updates. The
helper reads the manifest and payloads before publishing the staged files, but
does not pin one AtomicWriter generation across all reads. An immutable Secret
therefore prevents a mixed configuration snapshot. Staging is a prestart
snapshot, not a configuration watcher or live-update mechanism.

Each additional profile requires its own explicitly listed files. Use a fresh
configuration `emptyDir` for each Pod: the staging helper refuses existing
destination content rather than replacing live configuration. It does not copy
or clear the persistent state directory. Provision the PVC and its ownership
before starting the process; do not delete journals to clear an unresolved
session.

Run staging as user 10001 after provisioning the real, empty, mode-0700
configuration directory:

```sh
/usr/local/bin/python3 /opt/voice/stage_private.py \
  --source /run/voice-inputs \
  --destination /run/voice/.local/config \
  --manifest /run/voice-inputs/staging.json
```

The manifest permits 1–128 unique flat ASCII `.json` or `.txt` names, excluding
the manifest itself. Its size is limited to 32 KiB, each input to 256 KiB and
the total copied payload to 1 MiB. Files with leading dots, `..` or slashes are
rejected. Staging does not start the gateway, contact a phone or modify state.

In the staged registry, set `python_executable` to `/usr/local/bin/python3`,
`ffmpeg_executable` to `/usr/bin/ffmpeg`, `state_directory` to
`.local/state/voice-control`, and profile paths to
`.local/config/transport.json` and `.local/config/voice.json`. The transport's
`gateway_config` references `.local/config/gateway.json`; the voice
configuration's `ha.token_file` references `.local/config/ha-token.txt`. The
private workspace is `/run/voice`, independently of the source root
`/workspace`. Keep the registry disabled while preparing these inputs.

Validate staged profiles offline before considering replacement:

```sh
/usr/local/bin/python3 /workspace/containers/asterisk/phone_voice_control.py validate \
  --config /run/voice/.local/config/voice-control.json \
  --workspace /run/voice
```

This validates configuration without starting audio. It does not exercise the
live network, the existing preferences database or shutdown under load.

Use one replica with the `Recreate` strategy, explicit node pinning and the
existing API and callback/listener ports. Retain the runtime's non-root,
read-only and capability restrictions. Set `terminationGracePeriodSeconds` to
at least 60 seconds; 75 seconds is recommended to allow more than the gateway's
roughly 47-second voice cleanup window before a forced Pod kill. The persistent
volume must preserve the same physical control ownership. Old and replacement
gateways must not run concurrently or compete for the state or the phones.

Only after the replacement is approved, staging and preflight pass, the
existing gateway is stopped, and its preserved state is mapped to the paths
above, the replacement container's daemon command is:

```sh
/usr/local/bin/python3 /workspace/containers/asterisk/phone_gateway.py \
  --config /run/voice/.local/config/gateway.json \
  --state-directory /run/voice/.local/state/gateway \
  --voice-control-config /run/voice/.local/config/voice-control.json \
  --voice-workspace /run/voice
```

This command opens the existing gateway listeners and starts its normal
background reconciliation. It is not an offline preflight command.

Daemon startup does not authorize a microphone session. Each Start must still
supply the attendance, exclusive-media and microphone gates from
[voice control](voice-control.md); existing call priority, bounded duration,
explicit Stop and persistent uncertain-cleanup rules continue to apply. The
short manual Job remains unchanged: its source projection is `/workspace`, its
Secret mount is `/run/voice/.local`, and its runtime workspace is `/run/voice`.
