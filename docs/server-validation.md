# Live server validation — October 17, 2026

The server is installed in the existing home k3s cluster. Four physical 7945G
phones powered through PoE have converted from SCCP to SIP and registered
**201–208**, with two lines per phone. The operator confirmed handset echo on
**201 → 600**, Russian spoken Assist replies on **201 → 630**, and that the
second and third phones work well. Active call checks also verified the other
lines, while native VoIP discovery and Assist pipeline selection were checked
for all four primary identities, **201/203/205/207**.

The records below distinguish human audio confirmation, observed live calls,
configuration checks and isolated tests. A registration or successful control
request alone does not prove that someone heard audio. Subsequent service
expansion and acceptance results are recorded separately from initial
commissioning.

## Verified on the live system

- Dedicated namespace and one Asterisk **22.11.0** instance on a home Linux AMD64
  node. The container runs as UID10001, without capabilities, with a read-only
  root filesystem and a verified immutable image digest.
- SIP TCP/UDP, endpoint `ha-assist`, extension **630** and the Opus module checked
  in the running container. Both G.711 μ-law → Opus and Opus → G.711 translation
  paths are available.
- Readiness checks both SIP transports, AMI LISTEN on the configured IPv4:5038,
  Opus, and the Assist endpoint/route. Successful rollout does not replace a call.
- Persistent firewall restrictions on PBX and HA nodes, including IPv6. Only the
  actual HA source and the explicitly admitted local phone-gateway source can
  reach AMI; access from the workstation was rejected.
  Other firewall rules were preserved. TFTP is closed during ordinary operation;
  the separately controlled commissioning windows are described below.
- Pinned TECH7Fox integration **1.0.5** installed in existing **Home Assistant
  2026.10.0**. Asterisk and native VoIP entries are `loaded`; VoIP listens on
  UDP5060. `AMI Connected` is on.
- AMI Login, CoreSettings, PJSIPShowEndpoints and Logoff succeeded from the HA
  container itself. The version matched and the endpoint was found; CLI Command
  received the expected denial. HA service `asterisk.send_action` accepted Ping;
  that service response alone does not prove calling works.
- A dedicated administrative telephony dashboard contains the actual AMI sensor,
  five entities for extension 201, native VoIP permission/pipeline controls,
  Assist-call and guarded Hangup buttons, first-phone instructions and
  documentation links. Its English configuration was verified by reading it
  back through the API. Registration was checked independently on Asterisk
  and HA, as described below.
- HA configuration and `.storage` were backed up locally before installation.
  Following HA's internal restart, it returned to `RUNNING` with safe mode off;
  all **120 automations and 94 scripts** were preserved. A later restart loaded
  two phone-control scripts and one disabled DTMF test automation; all 214 prior
  entities and the prior automation on/off states remained intact. The Recorder
  configuration retained its 43 existing entity exclusions and added exactly
  the three mapped Connected Line/DTMF sensors. The original main configuration
  and Recorder include files were unchanged.

The initial commissioning images were built on a standard GitHub `ubuntu-24.04` runner and passed
[native AMD64 build and isolated smoke tests](https://github.com/4alvit/cisco-home-telephony/actions/runs/39029793014)
from commit `f5cb1546ed93c53d4792817d0bb1ba0f3a929821`. Archive checksums,
architecture and registry manifest/config digests were checked before deployment.
The temporary GitHub artifact was deleted after transfer. Registry addresses,
full deployment receipts, firewall configuration, entity maps and secrets remain
local. Later readiness changes did not modify these images' Dockerfiles.

The subsequent [CI run for the current implementation](https://github.com/4alvit/cisco-home-telephony/actions/runs/39051675792)
passed on `main` at commit `e33b9996e728576ff55ef8c622520fcfeb8b9d1e`, including
the image builds and isolated smoke tests. The subsequent documentation-only
[run at `2a5cc3a`](https://github.com/4alvit/cisco-home-telephony/actions/runs/39052869779)
also passed. These checks complement the live results; they do not establish
handset audio quality.

## First physical phone

A **CP-7945G**, hardware revision **13.0**, was found on the LAN after being
connected through PoE. It initially ran **SCCP45.9-4-2SR4-3S** with boot load
**tnp45.9-3-1-CR17.bin**. Device and network status were read from its web
interface and saved privately. MAC, serial number and network details are not
published. These observations apply to one phone, not the remaining batch.

The [preserved SIP45 SR4-3 bundle](firmware-archive.md) was subsequently downloaded,
its eight component hashes checked, and the same files staged on the PBX node.
A verified private profile for extension **201** was deployed; Asterisk is Ready
and Home Assistant discovered its five phone entities after an integration
reload. Its source is admitted by a single-host `/32` rule, and the existing NTP
server was verified. The existing AMI password was preserved and
`AMI Connected` remains on.

The first commissioning attempt then enabled a bounded TFTP Job and a
single-phone firewall exception. A DHCP reservation and option 150 were applied
only to that phone. After a normal restart, its web interface confirmed the new
TFTP address, and packet capture showed requests reaching the server with
file-not-found replies returning to the phone. The requested files were CTL/ITL
trust files and signed `SEP…cnf.xml.sgn` / `XMLDefault.cnf.xml.sgn` configuration.
The ordinary XML profile and firmware components had not been requested.

The phone's console logs also showed signed-configuration lookup and trust-list
errors. The user erased the old ITL at the handset using the targeted
[trust-list procedure](sip-conversion.md#old-cucm-trust-blocks-the-new-configuration).
Subsequent network capture showed requests for the intended `.loads` file and
all five `.sbn` components. After the update, the phone's web interface reported:

- Version **SIP45.9-4-2SR4-3S**.
- App Load **jar45sip.9-4-2SR4-3.sbn**.
- Directory number **201**.
- Unchanged boot load **tnp45.9-3-1-CR17.bin**.

Asterisk then showed extension **201** with a **TCP contact in `Avail` state**.
Home Assistant showed its registered sensor on and state **`Not in use`**,
while **`AMI Connected`** remained on. This verifies SIP boot and registration
for the pilot phone. A subsequent call to **600** executed `Answer()` and
`Echo()` on Asterisk, and the operator confirmed hearing their voice returned
through the handset. This verifies that call's two-way audio path. Speakerphone,
DTMF and calls between phones remain separate acceptance checks.

The first call to **630** discovered the native Home Assistant VoIP device.
Calls were then enabled for that device and the existing Russian voice pipeline
selected. A subsequent call reached the satellite's `listening`, `processing`
and `responding` states. The operator confirmed an audible spoken answer in
Russian, both when dialing **630** and after answering a call initiated from HA.

The generated Assist-call script was invoked through HA's service API. The
phone rang, answered and connected to Assist. A separate call then verified
the generated guarded Hangup script: the current `PJSIP/201-…` channel was
checked immediately before invoking the script, the phone returned to
`Not in use`, Connected Line became `None`, and Asterisk showed no active
channels. These are live service/script checks; the dashboard was verified
by API readback, rather than by clicking its controls in a browser.

The phone also logged `Error Updating Locale` after unsuccessful requests for
`/be-sip.jar` and `/g3-tones.xml`, which are not in the staged firmware bundle.
SIP registration succeeded despite those locale errors. Locale provisioning
remains a separate follow-up; no placeholder files were created to hide them.

No firmware signature checks were bypassed. Successful boot does not change
the [third-party archive's provenance limits](firmware-archive.md); its vendor
signature was not independently verified offline. Raw captures, phone logs
and device identifiers stay private. After confirming that the transfer and
flash had finished, the TFTP Job was deleted and its Pod termination verified.
The temporary IPv4 TFTP allowances were removed; IPv6 protection and unrelated
rules remained unchanged, and the fallback timer was canceled. The
per-phone DHCP reservation and TFTP option remain for future controlled
provisioning windows. Job expiry alone would not have closed the firewall.

## Second line, wallpaper and remote management

The same physical phone now independently registers **201 and 202** over TCP,
using distinct credentials. The original 201 and AMI credentials were preserved.
An outgoing call from line 202 and an incoming call to it reached `Echo()` on
600. Both directions of G.711 RTP were observed, with zero reported packet loss
in these short samples. In the final **202 → 600** test, Connected Line remained
`None` while Device State was `In use`. The corrected HA Hangup script ended
the current 202 channel: HA returned to `Not in use`, registration stayed on,
and the PBX reported zero active channels. This verifies RTP flow and HA call
control; audible echo on 202 has not been confirmed by the operator. The earlier
human audio confirmation applies to 201.

The phone's LLDP neighbour, switch forwarding table and LLDP remote-port record
identified its exact port on a Netgear GS110TP. One controlled five-second PoE
cycle loaded the configuration. Readback confirmed restored power and unchanged
configuration of all other switch ports. A separate wallpaper Secret supplied
the 320 × 212 image and 80 × 53 thumbnail. Authenticated remote keys selected and
saved the generated Lake dawn artwork; screenshots confirmed both line labels
and the new background.

Web access, SSH and personalization are enabled. The authenticated CGI callback
uses separate random credentials, an exact phone/source/nonce match and a
renewable, bounded maintenance window. Tests exposed and corrected the firmware's
64-byte Basic credential-pair limit and a callback socket-restart issue. SSH
accepted its separate generated credentials and the diagnostic login; the
minimal algorithm lists in [the management guide](phone-control.md) avoid an
embedded-server buffer error. A minimal legacy TLS handshake also succeeded,
but the phone's expired MIC certificate does not provide trusted HTTPS.

The image selection persists after leaving Settings. The temporary TFTP Job
was removed after delivery and the firewall returned to its ordinary phone
policy. The callback URL remains in the private profile so later remote sessions
can explicitly renew their window without provisioning the phone again. The
temporary callback service was stopped, its dedicated firewall rule removed,
and its listening port confirmed closed after the final call-control test.

## Second and third phones: SIP, wallpaper and call control verified

Two more CP-7945G units were identified independently through DHCP, HTTP,
LLDP and switch forwarding records. Their hardware revisions are **12.0** and
**13.0**. Both initially reported the SCCP SR4-3 application file; an older HTTP
Version field disagreed with that application/LLDP observation and was not used
as a reason to downgrade. The existing verified SIP bundle was staged for both.

This expansion brought the total to six phone endpoints: **201/202** on the
existing phone, **203/204** on the second, and **205/206** on the third. Existing credentials
and the first phone's profile were preserved. The new profiles have numeric
labels, distinct line and management credentials, and the same wallpaper
catalogue. HA discovered all twenty endpoint entities for the four new lines
after a scoped integration reload; discovery alone is not registration.

HA endpoint controls were subsequently installed while the new phones remained
offline: four guarded Hangup scripts, eight dashboard cards, and twelve
Recorder exclusions (**49 → 61**). Configuration validation passed before one
HA restart. HA returned to `RUNNING`, all **218** pre-existing script/automation
entities were preserved, and dashboard readback matched. The existing 201
native VoIP settings remained unchanged. This checkpoint verified configuration
deployment; call-control acceptance followed after the phones registered.

Per-device DHCP reservations and option 150, exact-source firewall additions,
and a bounded TFTP Job were applied. Served-file hashes matched the prepared
profiles, firmware and artwork. Each new phone received one independently
verified PoE cycle; power was restored and all switch-port settings preserved.
Both phones then reported the intended TFTP server, but requested only old
trust files and signed configuration, with **Trust List Update Failed** in
their status logs. Their old profiles disabled SSH. The user then erased the
old ITL on each handset using the targeted procedure. Both phones requested
the intended signed firmware components and booted **SIP45.9-4-2SR4-3S**;
all four independent TCP contacts, **203, 204, 205 and 206**, registered.
No factory reset or firmware-signature bypass was used.

Authenticated CGI navigation selected and saved **LakeDawn** on both phones.
Fresh screenshots confirmed the saved image and numeric **203/204** and
**205/206** labels. Both phones also passed two-stage SSH authentication using
their separate device credentials, followed by the diagnostic login.

Each of the four new lines was tested separately against **600**. Asterisk
showed the exact originating channel **Up** in **Echo**, RTP flowed in both
directions with **zero reported packet loss** in the observed samples, and the
corresponding HA Hangup script ended that channel. These results verify all
four call-control paths; short RTP samples do not establish endurance or
human-audible quality for every mode. The operator subsequently reported that
both new phones work well. No specific human-audio result for **203/205 → 630**
is claimed.

Native VoIP discovery, configuration checks, application and readback then
passed for **203 and 205**. **Allow calls** is enabled for each, with exactly
the existing 201 Russian pipeline and voice activity detection (VAD) settings.
The original 201 settings were unchanged. A strict HA verification at
**19:39:43 UTC** confirmed all six registered lines idle. These checks establish
native VoIP readiness; spoken requests and audible replies on each new caller
identity still need human acceptance.

The original fallback timer was canceled before extending that provisioning
window and renewing its callbacks. After firmware and artwork delivery
completed, that TFTP Job was deleted. Any later window needs its own exact
phone scope and cleanup plan; old timers must not close a newer session.
See [the repeatable expansion runbook](adding-phones.md) for the full procedure.

## Fourth phone: SIP, wallpaper and remote management verified

The fourth CP-7945G, hardware revision13.0, now runs
**SIP45.9-4-2SR4-3S** and registers **207/208**. Its old ITL was erased by the
operator; DHCP/TFTP then delivered the prepared profile and signed firmware.
No additional PoE cycle or factory reset was performed. All16 served-file
hashes matched the prepared files. The temporary TFTP Job was removed after
transfer/flash completion and the normal closed-TFTP firewall policy restored.

Both lines passed separate **600/Echo** tests with the correct originating
identity, bidirectional RTP, zero reported loss in the observed samples and
exact-channel HA Hangup. Authenticated screenshots confirmed the saved
LakeDawn wallpaper and numeric line labels. Two-stage SSH authentication and
independently pinned host-key checks passed.

HA now includes all eight line mappings and12 telephony scripts while preserving
224 pre-existing script/automation entities and67 Recorder exclusions. A first
630 call discovered the real native207 identity, which was explicitly enabled.
The captured old selection for phone 201 was **FCC Russian Backup**, using
`tts.fcc_cloud_speech` with `Chatterbox-Multilingual.ru-RU.Male`. All four primary
identities **201/203/205/207** now select the existing **Local Russian Home**
pipeline: `tts.local_russian_speech`, profile `silero-ru-default`, with the
running TTS backend set to **silero-ru-baya**. The Echo devices and global
pipeline definitions were preserved. Selecting the voice is distinct from
human-audible acceptance of a new call. HA-originated Assist calls on
**203/205/207** subsequently passed: actual incoming Ringing state, one guarded
answer, the630 route, native satellite listening, bidirectional RTP with zero
reported loss, and exact-channel HA Hangup. The last check ended at
**19:54:22 UTC**, with all eight lines idle and zero active PBX calls.

The [four-phone diagram](diagrams/four-phones.svg) shows the installed topology.
The extended radio/control deployment is documented separately and must be
verified after its new container image is installed.

## Phone-control and radio service expansion

The expanded Asterisk and gateway services run the verified Linux AMD64 image
built from `60da33d295208d23ee61cee42b67acf087f24447` in
[the successful native build and smoke run](https://github.com/4alvit/cisco-home-telephony/actions/runs/39064408398).
The local registry manifest and imported image architecture were verified before
rollout; the temporary export artifact was then deleted. Firmware, phone
credentials, numeric line labels and LakeDawn artwork were preserved.

The persistent gateway uses a separate AMI identity, one exact-source callback
listener per phone and a source-restricted HA API. A required local-node AMI
exception was added to the raw firewall without broadening it to a subnet.
The old temporary callback processes and their stale cleanup timers were
retired after replacement verification. All four authenticated screenshot
callbacks passed.

The new HA **Cisco phone controls** integration adds **120 entities** across four
physical-phone devices and four dashboard tabs. Its installation preserved all
6,468 existing registry identities, 16 native VoIP entities and existing
scripts/automations. Recorder exclusions increased from 67 to 83, adding only the
12 volatile text inputs and four countdown sensors. HA returned to `RUNNING`,
with the new entities available and the expected Recorder filters active.

One pre-existing script was executing when the baseline was captured and was
idle after restart. That exact runtime transition was recorded separately;
script activity was not treated as an automation enable/disable flag. Existing
automation enabled states remained unchanged.

All four phones fetched the new dial template. Two selected PoE ports were
cycled independently and restored with all eight switch-port configurations
preserved; the other two phones completed a verified automatic refresh. New
TFTP requests, HTTP identities, fresh registrations and screenshots of both
numeric labels established completion. The temporary TFTP Job was deleted,
UDP69 had no listener, and the ordinary closed-TFTP policy was restored.

The first phone then passed these live gateway checks:

- Selected line **202** dialed **601**, reached `Up`, and sent/received increasing
  RTP counters with zero reported loss in the observed samples. The screenshot
  showed line202 connected to601. Exact-channel remote Hangup returned it to idle.
- PBX DND rejected a gateway-originated call. Opt-in auto-answer changed a real
  incoming **201 → 640** channel from `Ringing` to `Up`; remote Hangup completed.
  Both preferences were restored to their original disabled state.
- An English text notification and a PNG photograph appeared on the physical
  phone's Services screen. Their Exit softkey was visually verified and pressed
  using `Key:Soft1`. `SoftKey:Exit` is valid as the page binding, not as an Execute
  URI; an initial private test-helper mistake was corrected without changing
  the gateway's valid page XML.
- **701/Matryoshka** reached `Up` through the production AGI/AudioSocket route,
  with increasing bidirectional RTP counters, then ended by exact-channel Hangup.
  Human confirmation of radio audibility is deferred.

All seven broadcaster streams also decoded to nonzero PCM in the running AMD64
pod, with each test's FFmpeg child reaped. These short samples prove current
reachability and decoder operation, not long-session reliability or a person's
hearing of every station. See [radio validation](radio-and-alarms.md#live-network-verification).

### External SIP and deferred acceptance

The outbound-only external SIP guard is installed and enabled as a persistent
service. Its verified policy admits established reply traffic for the configured
SIP/RTP paths, retains the existing local sources and IPv6 restrictions, and
creates no router port forward. TFTP remains closed.

**SIP2SIP, Callcentric and Linphone each reached `Registered`.** Account
identifiers and credentials are stored only in private local files. Callcentric
still blocks outbound calls pending its account review, so 801 has no provider
route. Linphone has no verified peer selected, so 803 also remains unrouted.
SIP2SIP **802** is configured for its fixed **4444** echo destination. After the
operator resumed testing, a bounded synthetic call reached `Up` and exchanged
RTP in both directions. Over a five-second sample, sent packets increased from
255 to 505 and received packets from 213 to 414. Asterisk's local loss counter
also increased from 42 to 91, so this establishes connectivity, not clean audio
quality. Read/write codecs were G.711 A-law/mu-law. No handset was called and no
audio was recorded. Exact-channel cleanup left zero active channels, with all
eight local lines still available. Human listening and external-media quality
remain to be checked.

The operator then requested a pause in phone testing. The pending test timer was
canceled, its alarm was disabled, original UI settings were restored and the
helper exited. A direct PBX check confirmed zero active channels. Further
physical calls, music, timer/alarm acceptance, HA content-service acceptance and
the external echo pilot were paused. The operator subsequently resumed testing;
the external echo result above was obtained after that resumption. Schedule and
content-service results are recorded separately below when completed.

## Persistent timer and alarm correction

After testing resumed, the operator heard the original timer ring and then play
a tone after it was answered. That confirmed the old finite call path, but did
not meet the requested behavior: both timers and weekday alarms must keep
ringing until manually dismissed, without automatically answering the handset.

The corrected implementation uses a private Local-channel helper with an
unlimited phone `Dial`, ignores forwarding, and aborts the call immediately on
manual answer. A disposable Asterisk **22.11.0** container with networking
disabled and a synthetic SIP handset verified ringing for more than 35 seconds,
exact cancellation, immediate hangup on manual answer with zero RTP packets,
no retry after rejection, ignored forwarding, and zero remaining channels.
Gateway tests additionally cover persistent alert identity, cancellation after
restart, delayed originate completion and exclusion from automatic answering.

The HA update adds **Stop alert**, makes timer cancellation and alarm disabling
stop their own active alert, and removes the obsolete sound selectors. Its
expected inventory is **29 controls per phone, 116 across four phones**.
Deployment and physical acceptance of this corrected behavior are pending;
the earlier finite-call test must not be treated as their acceptance.

## Remaining acceptance

1. Confirm audible echo on **202**, then verify speakerphone audio and DTMF
   separately from the successful handset echo test on **201 → 600**.
2. Verify dashboard confirmation behavior by clicking its controls; the
   underlying HA scripts have passed live Originate/Hangup tests.
3. Complete human-audible Assist tests for **203/205** using their verified
   native VoIP permissions and pipeline settings.
4. Confirm a spoken request and audible Silero Baya reply from the fourth phone.
5. Test calls between physical phones in both directions, two-way audio, DTMF,
   hold/transfer and group ringing. Echo calls do not replace these checks.
6. Repeat relevant tests after controlled phone/PBX restarts and AMI
   reconnection, then commission the remaining phones in small groups.

The eight registered lines and service endpoint `ha-assist` do not establish that
the whole batch is ready. A working transcoder and listening socket do not prove
network quality on another phone. Human-confirmed Russian Assist applies to
the pilot and its selected pipeline; the per-line RTP and call-control results
above are not a load or endurance test.
