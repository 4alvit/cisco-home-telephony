# Release and acceptance

A GitHub prerelease is an installable, source-bound candidate. It does not imply
that every image family, phone or optional voice service has passed live
acceptance. Keep release notes explicit about the component being changed.

## Define the release unit

- **PBX:** Asterisk, dialplan, radio and call-control runtime. Preserve existing
  private configuration and prove the affected call paths and rollback.
- **Daily profiles:** TFTP image and matching deployment scripts. Verify source,
  image, serving-file hashes, permissions, fixed network policy, one owner and
  listener, complete transfers and a real phone boot/registration.
- **CLI + announcement service:** the seven CLI modules, guarded hybrid
  announcement runtime, renderer, examples and their source manifest. Echo HA
  adapters and device builds are separate release units with explicit compatible
  versions. Verify routing, job ownership, expiry, duplicate handling, DND and
  failure reporting; record quiet checks separately from audible acceptance.
- **Voice gateway:** base voice image, optional wake layer and matching CLI.
  Verify their source relationship, private model compatibility, complete
  request/reply cycles, renewal, call preemption, explicit Disable and cleanup.
  Private startup diagnostics do not count as successful call acceptance.

Beta 20 is a daily-profile update; it is not a replacement for the Beta 19
voice gateway. Compare the source files actually included in each image when
reusing an older build. Record both the image build commit and release commit,
with the byte comparison that justifies any difference.

## Prepare a candidate

1. Review the source and require both applicable GitHub workflows to succeed on
   that exact commit. Run the full Python suite, isolated image smoke checks and
   optional wake tests. Preserve failures and list skipped tests with reasons.
2. For image releases, export only the required image family using [the existing workflow](docs/ci-images.md).
   Check successful run ID, event, source commit, image configuration ID,
   architecture and every `SHA256SUMS` entry after download. A configuration ID
   is not a registry manifest digest.
3. Keep private inventories, profiles, credentials, recordings, models and
   incident receipts out of source archives, image layers and release assets.
4. Publish a unique `v0.1.0-beta.N` with matching source, verified assets,
   installation instructions, known limitations and the precise test scope.
   Never overwrite an existing tag or silently replace accepted release bytes.

## Package the CLI and announcement service

Commit the reviewed changes first. The packager reads only committed blobs
from an explicit allowlist, compiles the Python sources, and produces stable
archive metadata. It does not package local edits, private inventories,
configuration secrets, device firmware or an Asterisk image.

```sh
revision=$(git rev-parse HEAD)
python3 scripts/package-announcements.py --commit "$revision" \
  --output ".local/announcement-release-$revision"
(cd ".local/announcement-release-$revision" && shasum -a 256 -c SHA256SUMS)
```

The output directory must not already exist. Publish these four assets together:

- `cisco-cli.tar.gz`: seven matching CLI modules, examples and documentation.
- `announcement-service.tar.gz`: runtime sources, renderer, examples and
  documentation; deployment reuses the saved Asterisk image.
- `SOURCE.json`: the full source commit and combined file hashes.
- `SHA256SUMS`: hashes of both archives and the standalone source manifest.

Each archive also contains its own `SOURCE.json` with only that bundle's file
hashes. Confirm every extracted file against its bundle manifest and compare
`source_commit` with the exact commit recorded in the release. Keep the
existing Asterisk image identity in deployment evidence: packaging source
files does not rebuild or establish acceptance of that image. Installation
instructions are in [CLI announcements](docs/cli-announcements.md#install-from-the-release-packages).

Release notes must list the compatible `echomuse_home` and `vaca` adapters and
associated device builds, or state that direct Echo delivery remains disabled
until their required capabilities are present. The runtime must continue to
reject incomplete guards; never weaken them to make a release demonstration
pass. Include any change in private configuration shape without publishing
actual credentials, endpoint identities or room assignments.

## Announcements: beta, release candidate and stable scope

A beta may include the new direct delivery route after offline tests, source
verification and a quiet installation. Clearly label devices whose audio path
has not yet been exercised. Readiness, capability queries and `--dry-run`
confirm configuration and software state; they do not prove voice quality,
no-ringing behavior, playback completion or acoustic audibility.

A release candidate freezes the CLI/service bytes and its dependency manifest.
If only the CLI/API package is being qualified, state that narrow scope in the
release title and notes, including which device acceptance checks remain
pending. Do not describe a source-only RC as qualification of the complete
household voice system.

Stable promotion is scoped to the accepted component set and reuses its RC
bytes. A stable CLI/API package must not imply that untested Echo firmware or
an untested end-to-end announcement path is stable. Promotion of the complete
announcement feature requires its applicable device acceptance results,
including busy/DND admission, real-call preemption, owned cancellation and
playback completion. When sound is prohibited, publish the tested scope with
those results explicitly pending; do not manufacture beta, RC and stable
labels as substitutes for the missing evidence.

No release task may send a demonstration message, place a call or play audio
as an implicit installation check. Follow the operator's current quiet-hours
restriction even when a new release is otherwise ready.

## Promote only accepted scope

A release candidate needs a frozen manifest of its complete component set and
exact bytes, plus a reviewed acceptance plan for that same set. An earlier
phone test can only be reused for unchanged relevant source and configuration;
explain and verify that comparison. Otherwise test the new candidate in an
operator-approved idle window with rollback available.

Stable promotion reuses the accepted RC bytes; it does not rebuild them from
new dependencies or a moving base image. Required live results must refer to
the candidate's manifest. Keep source tests, packet/media checks, observed
hardware behavior and human listening as distinct evidence. Explicitly list
unsupported features and untested devices.

The whole stack remains pre-stable until that scope has been accepted. Passing
TFTP boot tests alone cannot qualify voice. Earlier synthetic voice responses
cannot establish human audibility or general question-answering quality.
Do not operate phones, restart the PBX or change a household deployment as a
side effect of publishing a candidate.

## Failure and recovery

Preserve the rejected candidate and its bounded private evidence. Fix the cause
or improve diagnostic capture, publish a new candidate, and rerun the checks
that cover the change. Never turn a failed or missing observation into success.
For startup `call_changed`, use the [private rejection diagnostic](docs/voice-control.md#rejected-sip-setup-diagnostics).
The receipt explains an admission failure and cannot release a reservation or
prove microphone closure. Follow the existing recovery procedure separately.
