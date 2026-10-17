# Radio, alarms and do-not-disturb

These optional services use the existing Asterisk instance. They add no paid
service, public listener, or continuous radio downloader. Radio starts only
after an authenticated phone calls an enabled station number. Existing lines
201–208, Echo 600, test tone 601, first-line group 620 and Assist 630 retain their
numbers. All features on this page are disabled unless explicitly enabled.

![Per-call radio and finite alarms](diagrams/radio-services.svg)

## Station numbers

The versioned [catalog](../containers/asterisk/radio-catalog.json) contains the
actual stream URLs and the exact host policies. The broadcaster pages below
are the sources, not third-party stream aggregators. Each station was decoded
to nonzero 8 kHz PCM by the real worker on **2026-10-17**; availability can change.

- **701 — Matryoshka Radio London** (`matryoshka`), Russian-language music and
  programmes. [Official station](https://matryoshka.fm/). Its HTTPS entry point
  redirects to rotating `nNN-euN.rcs.revma.com` HTTP nodes. This is the only
  catalog entry that permits HTTP; transport integrity is consequently weaker.
  The catalog restricts that hostname pattern and checks every resolved IP.
- **702 — Echo online** (`echo`), Russian-language talk/news.
  [Official Echo](https://echofm.online/). This is the current online service,
  not a claim that the former Echo of Moscow FM station is still broadcasting.
- **703 — RADIO BOB! Rock** (`bob-rock`), the main rock stream.
- **704 — RADIO BOB! Metal** (`bob-metal`).
- **705 — RADIO BOB! Gothic** (`bob-gothic`).
  The three streams come from the [official RADIO BOB! stream directory](https://streams.radiobob.de/).
- **706 — SomaFM PopTron** (`soma-poptron`), electronic/indie pop.
  [Official direct-stream page](https://somafm.com/poptron/directstreamlinks.html).
  SomaFM specifies personal, individual listening; this implementation is for
  private home listening and does not authorize public redistribution.
- **707 — Radyo Viva** (`radyo-viva`), Turkish music.
  [Official station](https://www.radyoviva.com.tr/). Its embedded live player
  supplies an HLS playlist with AAC segments; the worker supports that bounded,
  unencrypted audio format.

There is no station audio in this repository, archive, proxy endpoint, or
download library. Use the original broadcasts under each provider's terms.
Programmes, adverts and availability remain controlled by the broadcaster.

## Enable and verify

Merge the following into the existing private `site.json`; do not replace its
network, image or HA fields with an example:

```json
{
  "radio": {
    "enabled": true,
    "stations": ["matryoshka", "echo", "bob-rock", "bob-metal", "bob-gothic", "soma-poptron", "radyo-viva"],
    "max_seconds": 1800,
    "max_calls": 2
  },
  "alarms": {"enabled": true, "duration_seconds": 15}
}
```

The [complete synthetic example](../deploy/site.radio.example.json) requires
real local settings and verified image digests before use. An enabled `radio`
object without `stations` uses the whole catalog. `max_seconds` accepts
30–7200 seconds; `max_calls` accepts 1–4 concurrent radio calls across the PBX.
The default two-call limit keeps this small-node workload bounded.

Build the image containing Python, FFmpeg, `app_audiosocket`, `res_audiosocket`
and `res_agi`. Reusing an old image while changing only `site.json` is not
sufficient. Deploy its verified architecture-specific digest using the
[k3s runbook](k3s-deployment.md), after checking that all calls are idle.
Readiness checks require the radio worker, FFmpeg and running AudioSocket
application when radio is enabled.

Use the same private output directory that contains the existing secrets:

```bash
python3 scripts/provision.py --site .local/site.json \
  --inventory .local/phones.json --out .local
python3 scripts/preflight.py --site .local/site.json
python3 scripts/check_radio_streams.py
python3 scripts/check_radio_streams.py --station echo
```

The last command contacts one broadcaster and decodes a few seconds without
placing a call or writing audio. `--all` explicitly probes all seven stations.
The command without an option lists the catalog and performs no network
fetch. Diagnostics contain station IDs, PCM frame counts, signal peaks and
success state, not redirect tokens, caller details, or raw stream content.
The local probe requires Python 3 and FFmpeg; the container includes both.

The generator preserves existing SIP and management passwords. It writes
`asterisk/radio.conf` as a private runtime catalog selection and extends the
shared Cisco `dialplan.xml` with the enabled exact numbers. Phones need to
retrieve the updated dial template through a reviewed temporary TFTP window
for immediate number recognition. That step need not change their firmware
or credentials. Keep the per-phone source guards and normal
[provisioning cleanup](adding-phones.md) procedure.

Call one enabled station from a single idle phone. Confirm intelligible audio,
then hang up and verify that the channel and its decoder disappear. Repeat
with a second station. Pressing `#` also ends the radio session when Asterisk
receives the negotiated RFC4733 digit. A third simultaneous call at the default
limit receives the finite failure tone. A failed connection, invalid redirect,
unsupported payload or stalled stream also produces a two-second local tone
and hangs up; there is no automatic retry loop. Silence from an actual silent
broadcast is not classified as a network failure.

To disable radio, set `radio.enabled` to `false`, regenerate and redeploy at an
idle window. The generator removes stale runtime radio configuration and
routes. Do not remove or regenerate unrelated secret files.

## Live network verification

On **2026-10-17**, all seven catalog stations also passed a passive probe inside
our running Asterisk pod, using its installed worker and FFmpeg 5.1.9. Each
station produced 251 nonzero-signal PCM frames, about five seconds of audio,
through a synthetic loopback AudioSocket session. Every probe released its
concurrency slot, stopped its worker thread and reaped its exact FFmpeg child.
The installed worker and catalog hashes matched the repository sources.

This verifies stream reachability and decoding from the deployed network.
The probes created no Asterisk calls and saved no audio. They do not establish
AGI dialplan execution, SIP/RTP delivery, speaker output, or human audibility on
a physical phone. Those still require the separate handset checks above.

## Audio and network lifecycle

An AGI process starts for one station ID. It acquires a local concurrency slot,
opens an ephemeral **loopback-only** TCP socket, and asks Asterisk to connect
using a random per-call UUID. Only that matching connection can start the
downloader and decoder. No new host firewall rule or Kubernetes Service is
needed for AudioSocket.

Python resolves the approved hostname and rejects non-public or mixed
public/private address results. It connects to the checked address, verifies
TLS hostname/certificate where HTTPS is used, and validates up to three
redirects against the station's immutable host policy. Site configuration
cannot add URLs or hosts. The HTTP client does not use proxy environment
variables. Outbound DNS and TCP 443 are needed; Matryoshka additionally needs
TCP 80 to its approved CDN. Do not open inbound internet ports.

FFmpeg receives audio bytes on standard input with its protocol whitelist set
to `pipe`, a fixed MP3/AAC demuxer and no shell. It cannot follow a playlist URL
or open another network/file source. The HLS adapter independently validates
each playlist and segment URL; playlists are capped at 64 KiB, segments at
2 MB, nesting at two master playlists, and encryption/key/map/byte-range
features are rejected. Both adapters impose a total byte ceiling. There is
one decoder per call, with bounded threading and no unbounded audio queue.

PCM is mono, signed 16-bit little-endian, 8 kHz, paced in 20 ms frames with a
0.5 volume multiplier. Asterisk converts this to the phone's negotiated
G.711 codec. Incoming microphone frames are discarded; they are never sent
to the station or recorded. Network operations time out after eight seconds;
initial audio has a 15-second startup bound. A channel-bound watchdog provides
a final bound if an operation stalls. Hangup, `#`, time limit or failure closes
the AudioSocket and terminates/reaps FFmpeg. There is no background fetch
between calls.

This uses Asterisk's documented [AudioSocket application](https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/Dialplan_Applications/AudioSocket/),
[AudioSocket framing](https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/)
and [AGI application](https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/Dialplan_Applications/AGI/),
plus FFmpeg's [protocol whitelist and pipe protocol](https://ffmpeg.org/ffmpeg-protocols.html).

## Finite alarms and recurring schedules

**640** answers and plays a local, intermittent 880 Hz signal for the configured
1–30 seconds, then stops and hangs up. The default is 15 seconds; an absolute
35-second channel limit is a second bound. This is an attention signal, not
an emergency alarm, wake-up guarantee or unattended monitoring service.

Home Assistant owns timers, clock times, weekday recurrence and enable/disable
state. The [companion repository](https://github.com/ha-homelab/asterisk-telephony)
documents that scheduler. The PBX contract after a phone answers is:

```text
Action: Originate
Channel: PJSIP/<verified-primary-extension>
Context: home
Exten: 640
Priority: 1
Timeout: 20000
Async: true
```

By default, the phone rings normally for at most 20 seconds and must be
answered. Optional per-phone auto-answer can be enabled explicitly through the
gateway; it is off by default. The scheduler/gateway must check a fresh
registered, idle endpoint and DND state immediately before originating. Busy,
unregistered, DND or missed alarms must be reported rather than interrupting
another call or retrying indefinitely. Test one immediate alarm before enabling
a recurring schedule and verify the phone returns to idle after the tone.

## Optional gateway DND

The [phone gateway](phone-gateway.md) uses a separate AMI account when this
private site option is enabled:

```json
{"gateway": {"enabled": true, "ami_client_ipv4": "192.168.50.10"}}
```

Use the actual exact private source address of the gateway, not a subnet. The
generator stores and preserves its independent credentials in
`gateway-ami-secret.json`. `cisco_gateway` receives `read=system,call` and
`write=system,call,originate`, an exact `/32` permit, and no AMI CLI-command
permission. Asterisk's [DBPut](https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/AMI_Actions/DBPut/)
and [DBGet](https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/AMI_Actions/DBGet/)
operate the `cisco-dnd` family, keyed by extension.

When that key is `1`, a direct home-context call to the line receives Busy.
Group 620 includes only each physical phone's primary line and skips a primary
with DND set, so a dual-line phone is never rung twice by the group. Secondary
lines have independent DND for direct calls. An empty group returns Busy.

AstDB lives in an `emptyDir`; the gateway's private persistent state is the
source of truth and must restore DND after PBX reconnect/recreation. A short
reconnect gap can otherwise lose DND. **AMI Originate directly to `PJSIP/...`
bypasses the home dialplan DND check**, so alarm and call-button guards must
consult the gateway's DND state themselves. DND is not a security boundary or
a substitute for fresh call-state checks. Disabling the gateway option removes
its account and DND dialplan additions while retaining the private secret file.

## Validation and limits

Unit tests exercise default compatibility, all eight preserved SIP passwords,
invalid configuration/URL/DNS rejection, HLS bounds, concurrency locks,
unauthorized AudioSocket UUID refusal, and real FFmpeg decoding with hangup
cleanup. `scripts/smoke-containers.sh` also runs a synthetic local radio call
through actual Asterisk AGI/AudioSocket, checks nonzero recorded PCM and reaped
FFmpeg, and exercises real gateway AMI startup fingerprints, DBPut/Get/Del, direct/group DND Busy,
channel access and refused CLI permission. Its test-only audio fixture is not
installed in the production image and uses no broadcaster connection.

These checks passed locally with Asterisk 22.11.0 on Linux ARM64. The separate
broadcaster probes validate current network playback; they do not establish
physical-handset audibility, home egress behavior, long-session reliability or
AMD64 deployment acceptance. Record those results only after testing the
deployed image and actual phones. Broadcast endpoints can change without
notice: update the reviewed catalog, rerun the bounded probes and rebuild the
image rather than allowing arbitrary URLs at runtime.
