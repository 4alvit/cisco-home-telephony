# Freeze a media candidate before an attended test

`scripts/media_candidate.py` records the exact, declared inputs to a scoped
candidate. It runs locally with the Python standard library and local Git. It
does not contact Home Assistant, Kubernetes, a registry, or a phone, and it does
not start playback, change configuration, or promote a release.

A successful verification means **the supplied local inputs still match the
frozen candidate**. It is not media acceptance, an RC decision, proof of physical
audibility, or proof that local captures represent the current live system.

## Prepare private inputs

Use a private directory outside Git, or an ignored `.local/` directory. Keep the
spec, captures, manifest, and test receipts private. The manifest omits local
paths and configuration values, but still contains component identities, image
names, file names, hashes, and sizes. Secret hashes are not anonymization.

The collector must gather every component used by the declared test path:

- The gateway source commit and subtree, deployed image digest, stable controller
  specification, configuration, and relevant model/configuration inputs.
- The HA integration source commit and subtree, **actual installed source-file
  bytes**, and the integration configuration inputs used by the test.
- Runtime dependencies such as HA core, Asterisk, Music Assistant, or TFTP when
  that dependency is in the test path: deployed image digests, controller specs,
  and relevant configuration inputs. Do not invent source commits for images.
- Raw secret/config/model bytes locally, or fresh SHA-256 and byte-length
  fingerprints computed by the collector from the actual remote file bytes.
  A filename, ConfigMap name, Kubernetes resource version, or model name alone
  is not a content fingerprint.

Collect into a new snapshot directory while inputs are stable. For an installed
integration, copy the actual deployed files; do not substitute a Git archive and
call it an installed capture. This tool compares the captured tree to its Git
subtree, including every relative file name and byte. Missing, extra, modified,
or symlinked files are refused. If the declared scope is *installed source files*,
the collector may leave out runtime `__pycache__` files, but must explicitly list
that exclusion in `excluded_scope`. The tool itself has no implicit exclusions.

Use full Git commit IDs and immutable SHA-256 image digests. The source checkout
may contain unrelated worktree changes: the tool reads the specified committed
tree, not the working tree. Image observations must come from the deployed
container identity, rather than merely its desired tag. For a multi-platform
image, pin the digest reported for the actual deployed platform; an index digest
and its platform manifest digest are not treated as equal.

## Spec format

The following is a structural example. Replace the all-zero commit and digest
placeholders with the exact identities, and add every dependency and input used
by the declared path. Relative input paths are relative to the spec file. Absolute
paths are allowed. Parent traversal (`..`) and symlinked paths are refused.

```json
{
  "schema_version": 1,
  "scope": "attended-radio",
  "excluded_scope": ["wake-word", "video", "installed-python-cache"],
  "required_components": ["gateway", "ha_integration", "ha_core"],
  "components": {
    "gateway": {
      "role": "source",
      "source": {
        "repo": "/srv/checkouts/cisco-home-telephony",
        "commit": "0000000000000000000000000000000000000000",
        "subdir": "."
      },
      "image": {
        "expected": "registry.example.invalid/gateway@sha256:0000000000000000000000000000000000000000000000000000000000000000",
        "observation": {"file": "gateway-pod.json", "pointer": "/status/containerStatuses/0/imageID"}
      },
      "controller": {"file": "gateway-deployment.json", "pointer": "/spec"},
      "inputs": {
        "config": {"kind": "config", "file": "gateway-config.json"},
        "credential": {
          "kind": "secret",
          "fingerprint": {"file": "remote-fingerprints.json", "pointer": "/gateway_credential"}
        }
      }
    },
    "ha_integration": {
      "role": "source",
      "source": {
        "repo": "/srv/checkouts/asterisk-telephony",
        "commit": "0000000000000000000000000000000000000000",
        "subdir": "custom_components/cisco_phone_control"
      },
      "installed_tree": "installed-cisco-phone-control",
      "inputs": {
        "entry": {
          "kind": "config",
          "fingerprint": {"file": "remote-fingerprints.json", "pointer": "/integration_entry"}
        }
      }
    },
    "ha_core": {
      "role": "runtime_dependency",
      "image": {
        "expected": "registry.example.invalid/ha-core@sha256:0000000000000000000000000000000000000000000000000000000000000000",
        "observation": {"file": "ha-pod.json", "pointer": "/status/containerStatuses/0/imageID"}
      },
      "controller": {"file": "ha-controller.json", "pointer": "/spec"},
      "inputs": {
        "configuration": {"kind": "config", "file": "configuration.yaml"}
      }
    }
  }
}
```

For each referenced fingerprint, the selected JSON object has **exactly** these
fields (the digest below is a placeholder):

```json
{
  "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "bytes": 123
}
```

`role: source` requires source identity and inputs, plus an installed tree or an
image/controller pair. `role: runtime_dependency` requires the image/controller
pair and inputs, and does not accept a source claim. The declared scope must
contain at least two source components, at least one source component with an
image, and at least one installed tree. This supports a gateway and an integration
installed inside a separate HA core image without claiming that the integration
has its own container.

`required_components` must exactly match `components`; this catches omissions
from the prepared spec. It does not discover missing dependencies. Every component
requires at least one named input. Input kinds are `config`, `secret`, `model`, and
`other`. Add the applicable model files or fingerprints if inference is in scope.
Do not add a fabricated input merely to satisfy validation.

Labels contain 1–64 lowercase ASCII letters, digits, `_`, `.`, or `-`, beginning
with a letter or digit. Exclusions must be an explicit nonempty label list.
Unknown schema fields, duplicate JSON keys, non-finite numbers, malformed image
identities, and incomplete fingerprints are refused.

## Freeze and verify

Run from the repository root. Create the private destination directory first;
the tool never creates parent directories or overwrites an existing manifest.

```sh
python3 scripts/media_candidate.py freeze \
  --spec .local/media-candidate/spec.json \
  --manifest .local/media-candidate/manifest.json

python3 scripts/media_candidate.py verify \
  --spec .local/media-candidate/spec.json \
  --manifest .local/media-candidate/manifest.json \
  --manifest-sha256 EXPECTED_SHA256_FROM_FREEZE
```

Both commands use exit status `0` on success and return one JSON object:

```json
{
  "acceptance": false,
  "components": 3,
  "manifest_sha256": "<64 hexadecimal characters>",
  "scope": "attended-radio",
  "status": "candidate_unchanged"
}
```

Freeze uses `status: candidate_frozen`. A refusal has exit status `2`,
`status: refused`, `acceptance: false`, and a static `reason`; it does not print raw
input values or local paths. For example, `candidate_drift` means the frozen
manifest no longer matches current supplied captures;
`installed_tree_does_not_match_source` means the installed bytes differ from the
declared source tree. CLI usage errors are also exit `2`, from argparse.

Keep the expected manifest SHA independently with the test plan. Without that
expected SHA, verify still detects input drift against the file it receives, but
cannot establish that this is the previously approved manifest. Never respond to
drift by silently freezing a replacement under the old test identity: inspect
the change, freeze a new candidate, and bind a new test plan to its digest.

## Hash rules and bounds

- Local raw inputs: SHA-256 of exact file bytes, plus byte length. Whitespace,
  line-ending, secret, or model changes count as drift.
- Reported remote fingerprints: the collector's declared SHA-256 and length of
  exact remote bytes. The manifest marks these as `collector_reported_raw_file`,
  distinct from `local_raw_file`; the tool cannot independently read those hosts.
- Controller: the explicitly selected JSON object, serialized as UTF-8 JSON with
  sorted keys, ASCII escapes, compact separators, and finite numbers. Metadata
  outside that pointer is outside this hash. Use `/spec` for a stable controller
  spec; do not select an empty object or omit security/configuration fields to
  hide drift. Array order remains significant.
- Installed tree: every relative path, content SHA-256, and byte length. Its
  summary hash covers that canonical map. File mode is not an installed-tree
  acceptance claim; only regular files from regular Git blobs are allowed.
- Source: full committed identity and exact subtree object. Images: the declared
  immutable reference and matching observed SHA-256 digest. Git/image association
  remains a deployment provenance assertion supplied by the collector; this tool
  does not reproduce builds or verify image signatures.
- Manifest: canonical JSON with one trailing newline, no timestamps or local
  paths. Identical declared inputs produce identical bytes. New files use mode
  `0600` subject to a more restrictive process umask.

Limits are 16 components, 64 inputs per component, 8 MiB per JSON file, 512 MiB per
local raw input or installed file, 4,096 installed files, 1 GiB total installed
bytes, and 32 nested installed directories. A reported fingerprint may describe
at most 1 GiB. Symlinks and nonregular inputs are refused, including in parent
directories. File metadata is checked before and after reads to catch ordinary
concurrent writes; the captures must still be quiescent because this is not an
atomic snapshot of multiple files or systems. Git calls have a 30-second timeout,
disable lazy fetch/replacement objects, and forbid network transport.

## Bind an attended test without overclaiming

The private live collector must freshly capture/recompute runtime inputs and run
verify immediately before a phone test. Re-verifying old local exports only proves
that those old exports have not changed. The collector can then issue a short-lived
attestation tied to the manifest digest, the exact test-spec digest, and target.
The attended helper consumes `--freeze`, `--freeze-sha256`, and `--attestation`,
checks their binding and freshness, then performs its own phone/queue preflight.
That attestation is a collector trust boundary, not cryptographic live proof.

Keep candidate integrity, attended test results, and release acceptance as separate
facts. A matching manifest never overrides a busy phone, stale state, a failed
cleanup, or a missing audible confirmation. No command here automatically starts
a test, retries a phone action, or promotes a beta to an RC.

Offline regression tests:

```sh
python3 -m unittest discover -s tests -p test_media_candidate.py -v
```
