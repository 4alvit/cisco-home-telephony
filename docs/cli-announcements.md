# Spoken announcements from the CLI

`say` turns text into a finite telephone announcement using the configured
Home Assistant voice. It calls the selected phone or group, requests one answer
for each owned incoming call, plays the generated audio once, then ends the
call. It does not turn on persistent auto-answer.

`message` remains a screen command:

```sh
cisco 222 message "Dinner is ready"   # Display text
cisco 222 say "Dinner is ready"       # Speak through an announcement call
```

![Spoken announcement flow through Home Assistant and Asterisk](diagrams/cli-announcements.svg)

## Commands

```sh
cisco 222 say "Dinner is ready"
cisco 222 voicemsg Dinner is ready
cisco 210,212,222 say "Please come downstairs"
cisco @downstairs say --text-file "$HOME/announcement.txt"
cisco @downstairs say "This is a rehearsal" --dry-run
```

`announce` and `voicemsg` are aliases for `say`. Quote text that contains shell
punctuation. The selected numbers are **recipients** of the
announcement; unlike `cisco 212 220`, this command does not connect two people.
A comma list is explicit. An `@name` is expanded only from the private local
`announcements.groups` configuration. There is no implicit broadcast to every
registered phone.

Run `say` in the foreground until it returns. The CLI performs the one-shot
answer requests while the server owns playback and finite call cleanup. A
closed terminal is not a reliable Stop command. Use the returned job ID:

```sh
cisco @downstairs say-status JOB_ID
cisco @downstairs say-stop JOB_ID
```

Status and Stop address that exact announcement job. Use the same resolved
recipient set as the original command. Stop may return `stopping` while cleanup
finishes; inspect status again to observe completion. Stop does not target an
unrelated call by phone number. After a network error, inspect the known job
before deciding whether to send a new announcement. A new job can produce a
second audible announcement; do not retry blindly.

For an operation whose key you need to retain explicitly:

```sh
cisco 222 say "Dinner is ready" --idempotency-key dinner-notice-001
```

Reusing that key with identical recipients and text refers to the same job while
its journal remains available. Different content with the same key is rejected.
The service retains bounded journals for 24 hours; a key is not a permanent
deduplication guarantee after its journal expires.

## Recipients and call behavior

Supported recipients are configured Cisco phones and explicitly configured
LifeSize phones with a working native SSH driver. SIP registration alone does
not make another softphone or display remotely answerable.

The server checks availability and existing call state before calling. Busy,
offline or DND-protected recipients are skipped and reported individually.
Unknown targets are rejected. A group must not contain both lines of the same
physical Cisco phone: duplicate physical recipients are rejected instead of
calling that phone twice.

The group shares one synthesized audio asset. Each admitted phone gets its own
finite call, so an unavailable recipient does not become a call that rings
forever. Read the per-recipient result: a partly delivered group is not the same
as delivery to every recipient. Phones may answer at different times; group
playback is not sample-synchronized multiroom audio.

Existing persistent `autoanswer` and `automute` preferences are preserved.
Answering uses the ordinary Cisco or LifeSize control path only for the
announcement's incoming call. Playback completion is managed by Asterisk, not
by a timer in the terminal that originally submitted the text.

## Private client configuration

Install the complete seven-file CLI bundle from one reviewed revision:

```text
cisco.py
cisco_http.py
cisco_player.py
cisco_library.py
cisco_media_source.py
cisco_lifesize.py
cisco_announce.py
```

The [CLI installation guide](cli-installation.md) covers the command symlink;
the [LifeSize guide](cli-lifesize.md#install-or-update-the-local-cli) includes an
atomic versioned-bundle update procedure. Keep the existing gateway and native
phone configuration. Merge the `announcements` object from
[`cisco-cli.announcements.example.json`](../deploy/cisco-cli.announcements.example.json)
into the private `~/.config/cisco-home/cli.json`; the example is a fragment, not
a replacement configuration.

Put the announcement service's separate bearer token in the file named by
`token_file`. Relative paths are resolved beside the CLI configuration. Keep
both files owned by your user with mode `0600` or stricter. Real group names,
phone assignments, credentials and private deployment details stay outside
Git. The public example contains fictional groups.

The announcement URL is loopback **inside the selected Asterisk pod**, not a
service listening on the Mac. The example therefore uses explicit Kubernetes
transport and the `announcer` container. The configured context, namespace and
selector must resolve to the intended Ready pod. Existing Kubernetes
authentication and pod-exec access are required; this feature does not create
credentials or widen cluster permissions.

## Home Assistant voice

The service uses the existing Home Assistant TTS engine, language and voice
configuration. Text is synthesized before calls are placed. A synthesis error
therefore does not create silent calls.

For the local Russian voice deployment, the existing clock service configuration
selects `tts.local_russian_speech`, language `ru`, and the
`silero-ru-default` alias. That alias is routed by the speech backend to Baya.
The announcement command does not replace the provider, change voice defaults,
or introduce a cloud speech service. Other installations must configure their
own available Home Assistant engine and voice.

The announcement text is data, not an Assist request. It is spoken as supplied;
it does not ask the assistant to execute household commands.

## Install the announcement service

The service runs as the `announcer` sidecar in the Asterisk pod. It binds to
`127.0.0.1:18085` and requires its own bearer token. Access is through authenticated
Kubernetes execution into that pod; no public ingress or LAN listener is needed.
Both containers share the announcement audio spool. Home Assistant credentials
remain in the existing Asterisk configuration rather than in a spoken message
or a public source file.

Use [announcements.example.json](../deploy/announcements.example.json) as the
runtime configuration template. In the private copy, configure:

- `token`: a separate random announcement API token; put the same value in the
  private client `token_file`.
- `ami`: the existing authorized Asterisk management connection. See the
  [phone gateway](phone-gateway.md) for its AMI configuration and permissions.
- `lines`: an explicit mapping from each accepted extension to its physical
  phone ID. Map both lines of one Cisco phone to the same ID.
- `dial_audio_config` and `voice_service`: the existing dial-audio configuration
  path and its clock/TTS service number, normally `555`.
- `spool` and `agi`: the shared audio directory and mounted announcement AGI
  entry point. Keep the paths from the template unless adapting the deployment.

The service command is:

```sh
python3 /opt/cisco-announcements/phone_announce.py \
  --config /etc/cisco-announcements/config.json
```

The renderer includes `phone_announce.py`, `phone_announce_agi.py` and their
existing `dial_audio.py` / `phone_gateway.py` dependencies from the checked-out
revision. It preserves the saved Asterisk configuration keys and adds the
announcement dialplan include. The sidecar uses the Asterisk deployment's
existing image and audio spool. Container user/group overrides and the pod's
security context are preserved so both processes can use owner-only audio and
journal files. If your Asterisk startup command drops to a different OS user
internally, align the actual Asterisk and sidecar user IDs before deploying.

Save current inputs in an ignored private directory. Replace the example
context and Secret name with the values from your deployment:

```sh
umask 077
mkdir -p private/announcements-input
kubectl --context example-cluster -n telephony get deployment asterisk -o json \
  > private/announcements-input/deployment.json
kubectl --context example-cluster -n telephony get secret CURRENT_ASTERISK_CONFIG_SECRET -o json \
  > private/announcements-input/asterisk-secret.json
cp deploy/announcements.example.json private/announcements-input/config.json
```

Edit the private configuration before rendering. Do not commit the inputs or
rendered resources: they contain credentials and actual endpoint mappings.
Keep the existing token when updating an already installed service.

```sh
python3 scripts/render_announcements.py \
  --deployment private/announcements-input/deployment.json \
  --asterisk-secret private/announcements-input/asterisk-secret.json \
  --config private/announcements-input/config.json \
  --output-directory private/announcements-rendered

kubectl --context example-cluster -n telephony create \
  -f private/announcements-rendered/resources.json
kubectl --context example-cluster -n telephony replace \
  -f private/announcements-rendered/deployment.json
kubectl --context example-cluster -n telephony rollout status deployment/asterisk
```

The output directory must be new. The rendered Deployment retains the saved
`resourceVersion`; if another controller has changed it, fetch fresh inputs and
review/render again rather than forcing a stale replacement. A rollout restarts
the Asterisk pod and can interrupt active calls. Retain the original Deployment
and Secret as private rollback inputs until verification completes.

After rollout, merge the client fragment, check a dry run, then test a short
announcement on one available phone before expanding the group. A dry run
validates the request and configured targets without synthesizing, calling or
checking live availability. Busy, offline and DND checks happen when a real job
is dispatched.

## Results and troubleshooting

The job progresses through `accepted`, `running` and a terminal result such as
`finished`, `failed` or `stopped`; `stopping` means cleanup is still in progress.
`dry_run` is a planning result. Always inspect recipient statuses and reasons:
`finished` alone does not establish that every configured phone received audio.
The CLI's `answers` entries describe control requests, while recipient
`completed` states describe completed Asterisk playback. Neither proves that a
person heard the message. A completed command returns a nonzero exit status if
any recipient was skipped or failed.

| Observation | What to check |
| --- | --- |
| Announcement configuration is missing | Merge the private client fragment and install the matching server. |
| Connection refused on the loopback URL | Check the selected pod/container and sidecar readiness. The URL belongs to the pod. |
| Unauthorized | Verify that the service token and the private client token file match. |
| A group is unknown | Group names are local configuration, not room names from the public directory. |
| Duplicate physical recipient | Remove the second line or repeated number for that same physical phone. |
| Busy, offline or DND recipient | Read that recipient's current state; the announcement does not interrupt an unrelated call. |
| Speech preparation failed | Check the existing HA TTS service, engine/voice configuration and credentials before another call. |
| Phone rings but does not answer | Keep the CLI running; check the relevant Cisco gateway or native LifeSize answer control. |
| A network request had an unknown outcome | Use `say-status` for the existing job ID. Do not create a second job just to discover its state. |
| Playback has ended but cleanup is pending | Read job status and use exact-job Stop if needed; do not hang up an unrelated replacement call. |

Server API routes are `POST /v1/announcements`,
`GET /v1/announcements/JOB_ID` and
`POST /v1/announcements/JOB_ID/stop`. They are private implementation interfaces;
use the CLI so normal target validation, idempotency and answer handling apply.

## Operational limits

This feature is a short announcement, not a conversation, alarm, intercom,
streaming radio player or recording. Physical speaker volume remains the
phone's setting. For LifeSize, use its separate `volume call` control during a
connected call; `volume ringer` changes the ring, not speech loudness.

The bounded limits are 1,000 text characters, up to 16 distinct physical
recipients, 120 seconds of generated audio, 30 seconds to answer a call and
180 seconds overall for a job. The CLI waits at most 240 seconds. A text file is
limited to 16 KiB. The service admits at most four active jobs and retains at
most 512 journal records; completed audio is removed once all recipients are
terminal. These bounds are not a promise of simultaneous group start times.

Screen images and the native `message` popup do not become TTS automatically.
Use `say` when audio is intended, and use the ordinary timer/alarm commands
when a phone should keep ringing until someone dismisses it.
