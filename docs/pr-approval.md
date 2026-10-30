# Trusted pull-request approval

The [approval caller](../.github/workflows/auto-approve.yml) delegates to the
[shared approval workflow at an immutable revision](https://github.com/victron-venus/venus-os-ci-toolkit/blob/368320994ed55ae68935dcadb4062000a4648b92/.github/workflows/auto-approve-reusable.yml).
It performs GitHub metadata operations only: neither the caller nor the pinned
workflow checks out or runs pull-request code. The shared workflow verifies the
reviewer's identity, refuses self-approval, re-reads the head and base before
writing a review, and attaches the approval to that exact head commit.

## Staged activation

Keep the bootstrap PR in draft while reviewing this caller and connecting its
credential. The existing shared `!draft` condition skips automatic approval
without adding a project-specific activation variable. A skipped job on a draft
PR is **not** evidence that bot approval works. Once a trusted PR is ready, a
missing credential produces a configuration failure rather than silent approval.

The read-only audit on 30 October 2026 found no repository Actions secrets,
`can_approve_pull_request_reviews: false`, and default workflow permissions of
`read`. The existing automation collaborator has repository write access.
The caller alone therefore does not complete activation. Secret delivery,
the Actions approval setting and a successful real review remain prerequisites.
Track the coordinated rollout in
[the governance issue](https://github.com/4alvit/terraform-github-4alvit/issues/101).

## Eligibility and execution boundary

- Only ready PRs by `4alvit`, `californiantiramisu`, `dependabot[bot]` or
  `renovate[bot]`, targeting the default branch, are eligible.
- The shared caller convention uses `pull_request` for same-repository PRs
  other than Dependabot. Fork PRs and Dependabot use `pull_request_target`
  against the trusted base workflow. Unknown authors are skipped.
- Title/body edits do not start approval work or cancel a pending meaningful
  run. Base changes are re-evaluated. Concurrency separates both event types.
- Only `BOT_PAT` and optional `APPROVAL_PAT` are forwarded explicitly. There
  is no `secrets: inherit`, shell step, checkout or PR artifact download.
- Same-repository branch writers can edit the caller itself. They are within
  this trust boundary; this is not a sandbox for untrusted branch writers.
  Re-review any caller change before enabling credentials for additional writers.

Approval does not require the `automerge` label. In the shared toolkit that
label is an additional requirement of the separate **merge** workflow; it does
not make an unknown PR author trusted. This repository's caller only approves:
it does not add a merge workflow, enable native auto-merge or override branch
protection. `validate`, `images` and `Wake gate` remain required independently.

## Connect the existing credential

This is a personal-account repository, so it cannot inherit an organization
secret. The exact destination is the repository Actions secret `BOT_PAT` in
`4alvit/cisco-home-telephony`, using the existing automation account's credential.
The credential must cover this repository and permit pull-request reviews;
repository collaborator access alone does not prove the token has that scope.
Do not substitute the PR author's token, generate a new token, put a value in
Terraform, or print it in a command, log, issue or artifact.

Follow the shared
[secret control plane](https://github.com/victron-venus/venus-os-ci-toolkit/blob/368320994ed55ae68935dcadb4062000a4648b92/docs/SECRETS.md)
and [GitHub delivery runbook](https://github.com/victron-venus/venus-os-ci-toolkit/blob/368320994ed55ae68935dcadb4062000a4648b92/docs/SECRETS_GITHUB.md):

1. The credential custodian confirms the existing encrypted catalog entry,
   issuer identity, authorized repository scope and recovery custody without
   exporting its value to this checkout.
2. **Bootstrap is separate:** `github-publish` refuses a missing destination.
   It cannot create the first `BOT_PAT` secret or copy a value out of another
   GitHub repository. A separately authorized operator must first create this
   destination from the existing protected source through the approved secret
   delivery process. Do not bypass the publisher's refusal or broaden another
   organization's secret visibility.
3. Record the exact repository target in the encrypted schema-2 catalog:
   type `github`, scope `repository`, name `BOT_PAT`, owner type `User`, owner
   login `4alvit`, repository `4alvit/cisco-home-telephony`. Resolve current
   numeric IDs through the API. Keep catalog generations, revisions, manifests,
   source digests and publication receipts in private operator storage.
4. For subsequent managed delivery, review the manifest independently, use
   the publisher's zero-write preflight, then its explicit apply and journal
   workflow. Preserve partial/uncertain results instead of retrying blindly.
   API acceptance and secret metadata do not prove a usable credential.

`APPROVAL_PAT` is optional and is not a request to mint another token. For a
normal maintainer PR, `BOT_PAT` supplies the independent automation reviewer.
For a same-repository PR authored by that automation account, the pinned shared
workflow can use the independent `github-actions[bot]` reviewer when Actions
approval is enabled. Dependabot also uses `GITHUB_TOKEN`, not a PAT fallback.
Other self-approval or missing-credential cases fail explicitly.

### One-time destination bootstrap

There is no `github-publish --create` mode. The supported primitives are
`catalog-materialize` for one independently pinned existing entry and GitHub
CLI's repository secret creation. Run these only in the credential custodian's
private environment, with exclusive writer ownership and the separately
authorized repository administrator session. They are not CI steps.

Use an existing owned directory with mode `0700`, set `umask 077`, and retain a
private operation record containing the actor, target IDs, source revision,
timestamp and outcomes. Confirm through GitHub that the exact destination is
absent: a permission/network error is not proof of absence. If it exists, stop
and use the reviewed publisher path instead of overwriting it.

From the reviewed toolkit checkout, select the existing catalog entry (the
generation, revision, entry ID and ciphertext digest below are operator inputs,
not values to invent or derive from an unreviewed source):

```sh
umask 077
python -m tools.secret_control catalog-materialize \
  --catalog /private/operator/catalog.age \
  --identity /private/operator/operator.agekey \
  --expected-sha256 "$APPROVED_CIPHERTEXT_SHA256" \
  --generation "$APPROVED_GENERATION" \
  --entry "$APPROVED_ENTRY" --revision "$APPROVED_REVISION" \
  --output /private/operator/cisco-bootstrap/BOT_PAT
```

Materialization is local, preserves the entry's bytes and refuses a different
existing output. The file is mode `0600`. Use the publisher's administrator
session for delivery, not the payload token as `GH_TOKEN`.

GitHub CLI supports a zero-write encryption preview. Redirect even the sealed
output to private storage; do not print either representation of the credential:

```sh
gh secret set BOT_PAT --app actions --repo 4alvit/cisco-home-telephony \
  --no-store < /private/operator/cisco-bootstrap/BOT_PAT \
  > /private/operator/cisco-bootstrap/sealed-preview.txt
```

This preview checks encryption and public-key access; it does not prove secret
write permission and is not a create-only transaction. After reviewing the
operation record, recheck the exact target, its absence and sole writer
ownership, then perform the separately authorized one-time write:

```sh
gh secret set BOT_PAT --app actions --repo 4alvit/cisco-home-telephony \
  < /private/operator/cisco-bootstrap/BOT_PAT
gh api repos/4alvit/cisco-home-telephony/actions/secrets/BOT_PAT \
  > /private/operator/cisco-bootstrap/accepted-metadata.json
```

The returned metadata is a bootstrap receipt, not the managed publisher's
journal or proof of stored-value equality. Record failures or ambiguous
outcomes privately and reconcile before retrying; GitHub's API is an upsert,
so there is no atomic absent-only guarantee. Remove materialized and preview
files under the custodian's retention policy after consumer verification.
Keep the encrypted recovery source. Adopt the exact destination in the catalog
and use the manifest/preflight/journal path for subsequent updates.

## Enable and prove the chain

After the caller is reviewed and secret delivery is confirmed,
the repository administrator can enable Actions review capability while
retaining read-only default permissions:

```sh
gh api --method PUT repos/4alvit/cisco-home-telephony/actions/permissions/workflow \
  -f default_workflow_permissions=read \
  -F can_approve_pull_request_reviews=true
```

These commands are rollout instructions, not steps performed by the caller.
Before running them, coordinate with the governance owner. The workflow grants
only `contents: read` and `pull-requests: write` to its job. No branch-protection
setting is changed by activation.

Check configuration using metadata only:

```sh
gh secret list --repo 4alvit/cisco-home-telephony --json name,updatedAt
gh api repos/4alvit/cisco-home-telephony/actions/permissions/workflow
```

Mark the reviewed same-repository bootstrap PR ready, or use a fresh reviewed
same-repository maintainer PR targeting `main`. Fork and Dependabot target-event
coverage becomes available after the caller reaches the default branch. Verify
the approval job succeeds, the review author differs from the PR author, and
the review's `commit_id` equals the PR's current `headRefOid`. A new push must
receive an approval for its new head; an old approval is insufficient. Also
exercise the automation-authored fallback before depending on it. Preserve
only redacted run/review URLs and commit IDs as acceptance evidence.

Only after this consumer proof should the separate Terraform rollout restore
one required independent approval, preserving all three CI contexts, strict
base checks and the authorized owner emergency path. Neither a skipped caller
nor a green ordinary CI run proves this approval chain.

To stop new automatic approvals, disable this Actions workflow and cancel any
approval run already in progress. This does not revoke a review already written;
inspect and handle existing reviews separately. Do not lower the required review
count as a workaround for a broken credential.
