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
2. Export only the required image family using [the existing workflow](docs/ci-images.md).
   Check successful run ID, event, source commit, image configuration ID,
   architecture and every `SHA256SUMS` entry after download. A configuration ID
   is not a registry manifest digest.
3. Keep private inventories, profiles, credentials, recordings, models and
   incident receipts out of source archives, image layers and release assets.
4. Publish a unique `v0.1.0-beta.N` with matching source, verified assets,
   installation instructions, known limitations and the precise test scope.
   Never overwrite an existing tag or silently replace accepted release bytes.

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
