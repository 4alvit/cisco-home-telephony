# Images from a native GitHub runner

Normal push/PR runs build and smoke-test on standard `ubuntu-24.04`, without
publishing containers or uploading artifacts. If your workstation is ARM and
your k3s node is AMD64, a manual workflow can export the verified AMD64 images
without local emulation.

```bash
gh workflow run validate.yml --repo 4alvit/cisco-home-telephony \
  --ref main -f export_images=true
gh run list --repo 4alvit/cisco-home-telephony --workflow validate.yml --limit 5
```

Select the run you created. Inspect `headSha`, `event`, `headBranch` and
`conclusion` with `gh run view RUN_ID --json headSha,event,headBranch,conclusion`.
Require the expected commit, `workflow_dispatch`, `main` and `success`.
Do not use binaries from an unreviewed PR or another fork.

```bash
umask 077
mkdir -p .local/ci-images
gh run download RUN_ID --repo 4alvit/cisco-home-telephony \
  --name verified-amd64-images-COMMIT_SHA --dir .local/ci-images
cd .local/ci-images
# Linux; on macOS, use shasum -a 256 instead of sha256sum.
sha256sum -c SHA256SUMS
cat source.json
docker load --input asterisk-amd64.tar.gz
docker load --input tftp-amd64.tar.gz
docker image inspect cisco-home-ci/asterisk:22.11.0 cisco-home-ci/tftp:bookworm
```

Match `source.json` to the run/commit and `images.json` to the imported image IDs
and `linux/amd64` architecture. Artifacts contain no home configuration, firmware
or secrets. The `cisco-home-ci/*` tags are local: retag them for your registry
before pushing, following the [deployment runbook](k3s-deployment.md). Use the
resulting registry digests in k3s, not image IDs or mutable tags.

## TFTP-only export

For the permanent profile service, select `export_target=tftp`. The workflow
still runs the complete validation suite, including real TFTP transfers and
isolated firewall checks, but exports only the TFTP image:

```sh
gh workflow run validate.yml --repo 4alvit/cisco-home-telephony \
  --ref main -f export_images=true -f export_target=tftp
# Verify the successful run and exact source commit as described above.
mkdir -p .local/ci-tftp
gh run download RUN_ID --repo 4alvit/cisco-home-telephony \
  --name verified-tftp-amd64-COMMIT_SHA --dir .local/ci-tftp
cd .local/ci-tftp
shasum -a 256 -c SHA256SUMS
cat source.json
docker load --input tftp-amd64.tar.gz
docker image inspect cisco-home-ci/tftp:bookworm
cd ../..
```

Compare the loaded configuration ID and `linux/amd64` platform with
`image.json`. Publish to the reviewed registry and use its immutable manifest
digest in the private site's `tftp_image`; a Docker configuration ID is not a
deployment digest. The TFTP archive is limited to 150 MiB and retained for one
day. It contains the server and fixed access policy, with no phone profiles,
credentials, wallpapers or Cisco firmware.

Follow the [daily profile service runbook](daily-phone-boot.md) to render the
private inventory, migrate the firewall and apply `--profiles-only`. This mode
does not deploy the Asterisk or voice images built during validation.

## Voice runtime export

Select `export_target=voice` when dispatching a voice update. The default
`export_target=pbx` exports Asterisk and TFTP; a run exports only one family
while still building and checking all three images. The voice artifact is
`verified-voice-amd64-COMMIT_SHA`. Download it to a different private directory
so its provenance and checksums cannot overwrite the PBX export:

```sh
gh workflow run validate.yml --repo 4alvit/cisco-home-telephony \
  --ref main -f export_images=true -f export_target=voice
mkdir -p .local/ci-voice
gh run download RUN_ID --repo 4alvit/cisco-home-telephony \
  --name verified-voice-amd64-COMMIT_SHA --dir .local/ci-voice
cd .local/ci-voice
shasum -a 256 -c SHA256SUMS
cat source.json
docker load --input voice-amd64.tar.gz
docker image inspect cisco-home-ci/voice:pilot
```

Require the same successful-run checks above. Compare the loaded image's
configuration ID, operating system and architecture with `image.json`. The
configuration ID is **not** the registry manifest digest required by
`render_voice_job.py --voice-image`. Obtain that digest from the destination
registry, or from the verified image manifest imported into the node's
container runtime. Keep the source commit, artifact checksum and resulting
manifest digest together in private deployment records.

The voice archive uses gzip level 9 and has a 250 MiB export limit. The PBX
pair has a 400 MiB export limit. Exporting one family keeps a single run below
400 MiB; old artifacts from other runs still consume storage. These are workflow
guards, not additional GitHub storage allowances. A successful image smoke
checks dependencies and synthetic media with networking disabled; it does not
prove that a live phone or Home Assistant pipeline works. Follow the separate
[voice pilot runbook](voice-k3s.md) for runtime and device acceptance.

## Optional wake image export

The wake runtime has its own workflow because it layers private-model staging
over the manual voice image. Dispatch its single `export_image` input:

```sh
gh workflow run voice-wake.yml --repo 4alvit/cisco-home-telephony \
  --ref main -f export_image=true
gh run list --repo 4alvit/cisco-home-telephony --workflow voice-wake.yml --limit 5
```

Download the `verified-voice-wake-amd64-COMMIT_SHA` artifact into another fresh
private directory and verify it like the voice export: `SHA256SUMS`,
`source.json`, the configuration ID in `image.json`, `linux/amd64`, and a receipt
whose `smoke_passed` is true with `private_models_included` false. The archive is
`voice-wake-amd64.tar.gz`, and the workflow refuses a total at or above 350 MiB.
It contains public code only; wake models stay in the private volume described
in [the wake runtime guide](voice-wake-runtime.md). Exporting a wake image does
not replace the gateway or TFTP artifacts already installed.

For an internal HTTP registry, avoid changing the Docker daemon by extracting
the `.tar.gz` and using `skopeo copy --dest-tls-verify=false
docker-archive:/absolute/path/asterisk-amd64.tar
docker://YOUR_REGISTRY/cisco-home/asterisk:22.11.0`. Apply this TLS exception only
to the verified internal registry. Check registry manifest/config digests and
architecture after transfer.

Export is disabled by default, limited to 400 MiB with one-day retention.
Artifact storage has a separate shared quota: free runner minutes do not imply
unlimited storage. After downloading and verifying, delete only your run's
artifact in GitHub Actions. The first home deployment artifact was deleted after
both images had been transferred and verified.


## Voice image security qualification

Both the manual voice build and its optional wake child scan the **finished
image**, including all OS and language packages, with pinned Trivy 0.74.0.
`scripts/verify_voice_security.py` rejects every HIGH/CRITICAL language finding,
every HIGH/CRITICAL OS finding with a distributor fix, incomplete inventories,
and a remaining runtime `pip` installation. It also requires the exact reviewed
Debian security updates below. Full JSON and CycloneDX SBOMs accompany exported
voice images and remain available as CI artifacts even when the gate fails.
Passing this gate does **not** mean an image has no reported vulnerabilities.

The 2026-10-24 review of `v0.1.0-beta.21` examined wake image configuration
`sha256:e7509a4e8a039f13a1010658c0dd918c154215922cb5bd9582315e3a50b1f87c`
from source `2f64ffbb2b74d89a0256b7742a5df3c39438aea5`. Its complete scan
reported seven fixable HIGH OS package findings. The next build uses the
2026-10-24 Debian snapshot and explicitly installs:

- `libbrotli1=1.0.9-2+deb12u1`: CVE-2026-111497.
- `libexpat1=2.5.0-1+deb12u5`: CVE-2025-59375 and CVE-2026-77214.
- `libssh-gcrypt-4=0.10.6-0+deb12u3`: CVE-2026-0966, CVE-2026-3731,
  CVE-2026-59847 and CVE-2026-59850.

The old image also contained pip 25.0.1 with outdated vendored packages that
its scan did not enumerate separately. Image inspection found certifi
2024.8.30, msgpack 1.1.0 and setuptools 70.3.0. Updating application locks alone
would leave these copies behind. The build now downloads the official pip
26.2.2 wheel using a fixed URL and SHA-256, removes any existing global pip
**before** loading that wheel, installs hash-locked binary dependencies, runs
`pip check`, and removes the temporary wheel and `ensurepip`. The helper uses an
isolated Python interpreter and does not inherit `PYTHONPATH` or `PYTHONHOME`.
It supports both the original Python base with pip and the pip-free manual
image inherited by the wake child. No installer is required to run the shipped
application; rebuilding the image is the supported dependency-update path.
The existing network-disabled, non-root image smokes verify dependency versions,
application imports, media decoding and removal of all build-only installers.

### Remaining critical OS findings in the reviewed beta.21 image

These eight findings are retained in the full report. The following disposition
is specific to the examined image and current voice call paths; it is neither a
scanner suppression nor a claim that arbitrary use of its bundled tools is safe.
Recheck the new image and distributor advisories before each release:

- [CVE-2023-6879](https://security-tracker.debian.org/tracker/CVE-2023-6879),
  `libaom3`: multithreaded AV1 encoding with a resolution change. The voice
  pipeline decodes audio and does not encode AV1. Debian lists the Bookworm
  update as a minor issue without a DSA.
- [CVE-2026-58016](https://security-tracker.debian.org/tracker/CVE-2026-58016),
  `libglib2.0-0`: malformed GDBus introspection XML. The voice application does
  not use GDBus; Debian postpones the Bookworm update as a minor issue.
- [CVE-2025-47917](https://security-tracker.debian.org/tracker/CVE-2025-47917),
  `libmbedcrypto7`: certificate-writing name conversion. The application uses
  Python OpenSSL for TLS and does not call the Mbed TLS certificate writer.
  Debian marks this source package end-of-life in Bookworm: its continued
  presence is a maintenance risk that the security gate cannot fix by upgrading
  to an unavailable distributor package. The supported replacement is tracked
  separately in [issue #48](https://github.com/4alvit/cisco-home-telephony/issues/48).
- [CVE-2026-34873](https://security-tracker.debian.org/tracker/CVE-2026-34873)
  and [CVE-2026-34875](https://security-tracker.debian.org/tracker/CVE-2026-34875),
  `libmbedcrypto7`: upstream describes affected newer Mbed TLS series for
  [TLS 1.3 resumption](https://mbed-tls.readthedocs.io/en/latest/security-advisories/mbedtls-security-advisory-2026-03-client-impersonation-while-resuming-tls13-session/)
  and [FFDH export](https://mbed-tls.readthedocs.io/en/latest/security-advisories/mbedtls-security-advisory-2026-03-ffdh-buffer-overflow/).
  The examined Debian package is 2.28.3, outside those stated version ranges;
  retain the distro findings and this version discrepancy rather than
  silently overriding the scanner. Neither operation is an application path.
- [CVE-2025-7458](https://security-tracker.debian.org/tracker/CVE-2025-7458),
  `libsqlite3-0`: crafted SQL with an overflowing ORDER BY clause. The voice
  application neither executes SQL nor exposes an SQL input. Debian lists the
  Bookworm issue as minor without a DSA.
- [CVE-2026-6653](https://security-tracker.debian.org/tracker/CVE-2026-6653),
  `libxml2`: internal DTD/entity processing. Voice playback explicitly selects
  MP3, Ogg or FLAC demuxing, permits only the FFmpeg `pipe` protocol and does
  not auto-detect XML playlists. Debian postpones the Bookworm update.
- [CVE-2023-45853](https://security-tracker.debian.org/tracker/CVE-2023-45853),
  `zlib1g`: Debian states that the affected MiniZip contribution is not built
  from its zlib source package. The raw package finding remains visible.

The reviewed image also has unfixed HIGH OS findings, retained without severity
filtering. A future reachable vulnerability, newly available security update or
expanded application use of these libraries requires another review; this dated
list is not a permanent exception. These software checks do not replace the
separate phone, audio, restart and rollback acceptance in
[release hold #46](https://github.com/4alvit/cisco-home-telephony/issues/46).


### Speech decoder boundary

Compressed speech decoding admits only MP3, Vorbis, Opus and FLAC decoders and
explicitly selects the first audio stream. Video, subtitles and data are not
mapped to output. The input codec allowlist also rejects opening an attached
PNG video decoder; an actual synthetic MP3 with valid ID3 artwork tests both the
unrestricted positive control and restricted refusal while preserving speech
samples. Real MP3/Vorbis/Opus/FLAC fixtures run on the host and inside the final
voice image with networking disabled, a read-only root and no capabilities.

This narrows decoder use; it does not remove the multimedia libraries or prevent
all container-header parsing. The complete vulnerability inventory and the
supported-library replacement follow-up in #48 remain necessary. Expanding the
accepted codec set requires new fixtures and a fresh security review.

## Required pull-request checks

The required contexts are `validate`, `images` and `Wake gate`, all emitted by
GitHub Actions. `validate` holds the fast checks and usually finishes in about
three minutes; `images` builds the voice and PBX containers, scans the voice
image, rejects actionable vulnerabilities and runs the isolated container
smokes, which takes about nine minutes. The two jobs are independent and both
are required, so a failing test is reported long before the builds finish while
a build failure still blocks the merge. An image build does still start when
the fast job fails; the next push cancels it through the workflow concurrency
group. Both jobs run for every pull request, and both workflows run on pushes
to the default branch. A pull-request branch is covered by the
`pull_request` event alone, so a commit pushed to an open pull request is
built once instead of once per trigger; a branch push without a pull request
starts nothing, and an explicit workflow dispatch covers that case. The test
suite holds this: `tests/test_workflow_triggers.py` fails when a workflow
triggers an unfiltered `push` next to a pull request, and it rejects a `push`
narrowed by `paths` alone for the same reason. Before the test suite,
`scripts/verify_docs_links.py` resolves every repository-local link and heading
anchor in the tracked Markdown files, so a moved file or reworded heading fails
without waiting for a build. External URLs are out of scope. The wake workflow
also always runs: its lightweight scope job compares the exact event commits
with complete Git history, then builds the optional image only when an input
changed.

For pull requests, scope uses the unique merge base of the event's exact base
and head, and verifies that the checked-out test merge contains both. For push
updates it compares the event's before/after commits; a new branch checks the
whole tree. Renames include both removed and added paths. An explicit workflow
dispatch always builds. Missing commits, a mismatched checkout, ambiguous
history, invalid paths or an unknown event fail scope; none are treated as an
unrelated documentation update.

`Wake gate` runs after scope and build even when a dependency fails. It passes
only for successful scope plus either a successful required image build or a
proven irrelevant diff with a skipped image build. Failed/cancelled scope,
missing output, a required build that was skipped, and failed/cancelled builds
cannot produce a green gate. Documentation-only changes therefore receive a
real gate result without requiring a redundant image build.

Branch policy is managed separately in the GitHub infrastructure configuration.
Verify its live presence before enabling automatic merge: `gh pr merge --auto`
can merge immediately in an unprotected repository and is not itself a CI wait.
The policy must require these two exact contexts with strict base updates. It
does not require an approving review: this repository has one maintainer, who
cannot approve their own pull request, so such a rule could only ever be
bypassed and would make the gate look stronger than it is. A second person's
review is still expected when one is available. The maintainer keeps an
explicit bypass for recovery when Actions cannot run, so a bypassed merge is a
deliberate, auditable exception and never the normal path. Release publication
still checks the complete exact-source image run and independently verifies
exported bytes.
