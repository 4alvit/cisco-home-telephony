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
