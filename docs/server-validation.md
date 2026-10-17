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

The Asterisk and gateway update from commit `5fd8ea4` passed
[native AMD64 CI and isolated tests](https://github.com/4alvit/cisco-home-telephony/actions/runs/39068853623)
and was deployed by its verified registry digest. Both containers became Ready
without restarts; all eight physical-phone contacts and all three provider
registrations recovered. The new helper and `WaitForSignal` were present, 802
remained routed, and registration-only 801/803 remained unrouted. No phone call,
TFTP session or firewall change was part of that rollout. The original private
configuration was backed up before promoting the verified candidate.

Home Assistant **0.2.0**, including destination labels from commit `9b79eed`,
then passed its configuration check and one controlled restart. Its verified
inventory contains **116 controls**, four new Stop alert buttons, and no
obsolete owned sound selectors. The **6,585** other existing registry
identities were preserved; the 83 Recorder exclusions and existing settings
were retained. The four owned
dashboard tabs were saved and read back. There were eight available phone
contacts, zero active PBX channels and zero pending schedules at verification.
Two physical timer occurrences rang and ended when the operator manually
answered or pressed a button on the phone. Asterisk recorded a phone answer,
ran the abort-on-answer handler and immediately ended each call; the gateway
recorded `call_ended`. Cleanup confirmed zero channels and pending schedules,
with the original preferences restored. General auto-answer was enabled during
these tests, but the test harness sent no Answer command.

Both calls were manually dismissed before the planned 40-second observation.
They validate physical manual dismissal, not uninterrupted physical ringing
beyond the old timeout. The longer ringing result above belongs to the isolated
SIP-handset test. A subsequent alarm selected **Saturday at 14:25
America/Los_Angeles** and reached ringing at 14:25:02. The physical call remained
ringing for more than 90 seconds before the phone answered and the abort handler
ended it. This verifies the weekday/time trigger and physical ringing beyond
the old limit.

That test also exposed an HTTP routing defect: the gateway's path expression
rejected the underscore in `alert_stop`, returning 404 before the stop handler.
The correction admits underscore-separated action names while retaining the
explicit action allowlist. A real loopback HTTP regression checks alert
creation, exact-channel stop, idempotent replay and preservation of an unrelated
call. Deployment and HA-button acceptance of this correction are recorded
separately below. The earlier finite-call test does not validate the revised
behavior.

The fix in commit `052c374` passed the
[native build and isolated smoke tests](https://github.com/4alvit/cisco-home-telephony/actions/runs/39071068234).
Its verified image was deployed to Asterisk and the phone gateway, changing only
the image references. All configuration and credentials remained unchanged;
the three connected Cisco phones, Mac 209 and three providers recovered.
The operator had deliberately disconnected 205/206, whose configuration was
retained.

A subsequent physical test created one owned timer alert on **201**, observed
`ringing` for three seconds, then invoked the actual discovered HA **Stop alert**
button. The same alert reached `ended` with reason `stopped`, and Asterisk had
zero active channels. No fallback termination was used and no schedule or
preference was changed. This verifies the corrected HTTP path and HA stop
control in addition to the separate manual-dismissal and long-ring tests.

## Mac client and children's radio expansion

The official Mac App Store build of **Telephone 1.6 (143)** was installed and
passed strict code-signature and Gatekeeper verification; its binary includes
arm64. Extension **209** has a separately generated SIP identity and source ACL.
Telephone then showed **Available**, while Asterisk reported one reachable 209
contact at approximately 16 ms RTT. The running app had an established TCP
connection to the PBX; IPv4 and TCP were selected and the outbound proxy was
disabled. The app stored its secret in Keychain. Its first numeric dial attempt
then stalled in macOS Contacts authorization, before any call reached Asterisk.
Telephone was quit with its saved account preserved; Contacts access was not
granted.

The replacement **Linphone 6.2.3** passed Developer ID and notarization checks
and contained both arm64 and x86_64 code. Its documented local XML provisioning
method configured the existing 209 identity over TCP/IPv4. Linphone showed one
green account, and Asterisk confirmed one reachable 209 contact. Call/audio
acceptance then passed on **209 → 600**: the channel was Up in Echo, negotiated
PCMU, and sent/received RTP increased from 63 to 594 packets with zero reported
loss in the sample. The 20-second test ended cleanly, and the operator confirmed
hearing their returned voice in Linphone. Its transient import file was removed
after successful provisioning; the private client configuration remains mode
`0600`.

The three candidate radio channels **708 (toddler music), 709 (lullabies), and
710 (children's songs)** passed bounded decoding from the PBX network. Each
produced 251 PCM frames, approximately five seconds, with a nonzero signal.
Their temporary signed URLs were resolved through the official player's API.
No calls or audio files were created, and all decoders and temporary probe
files were cleaned up.

The combined image from commit `38b7bb1` passed
[native AMD64 build and isolated smoke checks](https://github.com/4alvit/cisco-home-telephony/actions/runs/39069535250).
Its registry digest and configuration were verified before deployment, and
the temporary GitHub artifact was deleted. Both workloads became Ready;
nine local endpoints, eight physical-phone contacts, three provider
registrations and the 708–710 AGI routes were verified. The alert helper and
802 route remained present, while 801/803 remained unrouted. All three new
streams passed another five-second probe using the installed worker and
catalog; their hashes matched that exact commit.

A subsequent physical call from **207 to 708** executed the toddler-music AGI
route, and the operator confirmed audible music. The call ended normally and
left no active channels. Stations 709 and 710 have passed the installed-worker
PCM probes but do not yet have separate human listening confirmation. The
digit-entry method for the 708 call was not recorded.

The Mac firewall addition was kernel-checked and applied to the existing two
guards. It added only the Mac's single source to SIP/TCP and RTP in the raw
guard and to RTP in the external guard's INPUT chain. Existing ordered rules,
IPv6, hook positions and external `ESTABLISHED`/`REPLY` restrictions were
preserved. A connection from the Mac to the PBX's TCP5060 then succeeded.
That connectivity check is not SIP registration or an audio test.
The Mac's current Wi-Fi private address was verified as Fixed, and a single
DHCP reservation retained its current IP. Other DHCP sections and effective
options were unchanged. The DNS service reloaded successfully without a
Wi-Fi restart; router, DNS and PBX connectivity remained available.

## Fifth handset and preserved disconnected devices

The next independently identified **CP-7945G**, hardware revision **10.0**,
started on **SCCP45.9-3-1SR2-1S**, with application
**jar45sccp.9-3-1ES13.sbn** and boot load **tnp45.8-3-1-21a.bin**. Its initial
status explicitly reported no installed trust list and it requested ordinary
SEP configuration. No routine ITL erase or factory reset was requested.

The new device receives **210/211**. The operator disconnected **205/206**,
whose inventory, credentials and entities remain reserved; **209** remains
the separate Mac client. The server candidate retains every previous phone
profile and credential and adds only the new device, its two lines and exact
network source. During commissioning, the original **201/202** and **207/208**
also ceased to match the live switch inventory: their previous ports showed
different MACs, and one port had no link or power load. The original identities
remain configured. A separate uncommissioned SCCP phone was identified on one
of those ports; it is not counted as the new 210/211 device. The five-phone
checkpoint used group members **201/203/205/207/210**; secondary lines and Mac209
were excluded. That checkpoint is illustrated in the
[five-phone and Mac schematic](diagrams/five-phones-and-mac.svg).

The firewall migration retained the Mac rules, previous phone sources,
external SIP reply restrictions, unrelated hooks and IPv6 rules. One scoped
DHCP reservation and option150 tag were added. The temporary TFTP Job serves
17 hash-verified files, with access restricted to the new phone and a separate
fallback cleanup timer. One five-second PoE cycle targeted only the new
phone's verified switch port; independent readback confirmed restored power
and unchanged configuration on all eight ports.

After downloading the SIP components, the phone rebooted without another
power cycle. Its HTTP device page reported **jar45sip.9-4-2SR4-3.sbn** as the
actual App Load and SSH access enabled. At **21:49 UTC**, both **210 and 211**
were independently available on Asterisk. The five-phone server reconciliation
verified all 11 local endpoint definitions, the expected alert/group routes,
three registered providers, the original connected 203/204 and Mac 209, and
zero active channels. The verified candidate was promoted to the canonical
private configuration with the previous complete directory preserved.
The completed phone then showed the expected numeric **210/211** labels and
saved **LakeDawn** wallpaper. Authenticated CGI and screenshots worked. An
independently observed SSH host key was pinned before using the phone's generated
management credentials; login and the read-only debug/help checks passed.

## Additional connected batch and the tenth phone

The operator replaced completed phones with unconverted units and confirmed
that ITL had already been erased on the connected batch. Four further
**CP-7945G** identities were independently matched across their HTTP device
pages, DHCP leases, LLDP where available, and switch forwarding entries. One is hardware
revision **10.0** and three are **13.0**. The assigned pairs are **212/213,
214/215, 216/217 and 218/219**; all use the same verified SR4-3 firmware path.
The private source inventory retains the earlier five physical identities.

At this checkpoint the inventory contained nine physical phones, with the Mac
remaining on **209**. The original **201/202, 205/206 and 207/208** phones were
deliberately disconnected and retained their profiles, credentials and HA
identities. The tenth physical unit was initially waiting for a free PoE port;
no profile was generated before its actual identity could be verified.

The combined server rollout passed at **21:51 UTC**. Both deployments were
Ready on the same verified image, with **19 local endpoints** (18 Cisco lines
and the Mac), all 18 Cisco alert routes, the nine primary group members, and
three registered providers. The connected **203/204, 209 and 210/211** contacts
recovered, with zero active channels. Only the generated configuration Secret
references changed. The previous five-phone canonical directory was backed up
before promoting the nine-phone configuration. The four new phones were still
pending physical conversion at this server checkpoint.

A scoped HA integration reload discovered **261 actual phone controls** and
ten additional Asterisk endpoint families. The candidate adds **15 scripts**,
five control tabs and ten endpoint cards, and extends the exact Recorder
exclusion set from **83 to 133**. Its **27 scripts and one automation** passed
validation using the installed HA 2026.10.0 schema. This checkpoint establishes
discovery and configuration validity; package installation and phone acceptance
are recorded separately below. See the [fleet schematic](diagrams/fleet-and-mac.svg).

The four additional phones subsequently booted **SIP45.9-4-2SR4-3S** and
registered all eight lines **212–219**. Two phones began downloading on their
own; the other two received one independently verified PoE cycle each. No
second cycle interrupted an update. One phone briefly retained a stale SCCP
Version field after its App Load already reported SIP; the Version field later
caught up without intervention.

All four passed authenticated CGI, numeric line-label screenshots, saved
**LakeDawn** wallpaper and SSH login/read-only help checks after independent
host-key verification. The temporary TFTP Job and its Pod were removed, the
four-source TFTP exception was closed, and its fallback timer was stopped.
The external SIP reply policy, unrelated firewall rules and IPv6 policy were
unchanged. The completed fifth-phone window had been closed separately before
this batch began.

Home Assistant then passed its full configuration check and completed one
restart. Its final live verification found **261 phone controls**, **133 exact
Recorder exclusions**, 50 new Asterisk endpoint entities and 12 online physical
lines. All **6,599 baseline registry identities** were preserved. The five new
control tabs and the added endpoint cards were saved and read back through the
HA API. All 15 newly added text inputs passed their live privacy guards.
Existing phone preferences, native voice settings and automation enablement
were retained; new timers and alarms started disabled.

## Tenth handset

At **22:08 UTC**, a fresh switch, DHCP and HTTP inventory found the tenth
**CP-7945G**, hardware revision **12.0**, replacing the previously connected
203/204 phone. Its actual application was **jar45sccp.9-4-2SR4-3.sbn**, even
though the Version field still showed an older release. The operator confirmed
that the last phone was connected and its trust protection had been removed.
Only then were its independent lines **220/221** allocated. The disconnected
203/204 and 218/219 devices retained their own profiles; neither device's
identity or credentials were reassigned to the new phone.

The ten-phone server rollout passed at **22:12 UTC**, with **21 local endpoint
definitions**, 20 Cisco alert entries, ten primary group members and three
registered providers. The same container image was retained; only the two
configuration Secret references changed. Previously connected **209 and
210–217** recovered after the restart, and the nine-phone canonical directory
was retained before promotion. Live user calls resumed, so this checkpoint was
not an idle-call snapshot.

Linphone briefly showed a registration error during that restart. Its TCP
connection and registration recovered without changing its account, password
or network policy. Subsequent user-originated calls **209 → 210** and
**209 → 213** reached the selected Cisco line's Ringing state. These observations
establish call routing, not answered two-way audio acceptance.

The tenth handset downloaded automatically, with **no PoE cycle**. At
**22:19 UTC**, both **220 and 221** were available on Asterisk; its actual App
Load was verified as **jar45sip.9-4-2SR4-3.sbn**. Authenticated CGI, numeric
220/221 labels and SSH login with an independently verified host key passed.

The final HA expansion passed its configuration check and one planned restart.
Live verification found **290 phone controls**, **143 exact Recorder exclusions**,
ten additional endpoint entities for 220/221 and three additional guarded
scripts. All **6,818 identities** in the preceding baseline were retained.
The new text inputs passed their privacy guards, and the saved dashboard
matched the intended result. Both new lines and ten connected physical lines
in total were online at this checkpoint; unplugged devices kept their settings.

## Remaining acceptance

1. Confirm audible echo on **202**, then verify speakerphone audio and DTMF
   separately from the successful handset echo test on **201 → 600**.
2. Verify dashboard confirmation behavior by clicking its controls; the
   underlying HA scripts have passed live Originate/Hangup tests.
3. Complete human-audible Assist tests for **203/205** using their verified
   native VoIP permissions and pipeline settings.
4. Confirm a spoken request and audible Silero Baya reply on **207** and the
   later primary lines **210, 212, 214, 216, 218 and 220**. Automated RTP and satellite
   state checks do not establish what a person heard.
5. Test calls between physical phones in both directions, two-way audio, DTMF,
   hold/transfer and group ringing. Echo calls do not replace these checks.
6. Repeat relevant tests after controlled phone/PBX restarts and AMI
   reconnection. All ten physical units have completed SIP conversion; complete
   any remaining per-device audio checks when those phones are connected.

A count of registered lines and the service endpoint `ha-assist` does not
establish that the whole batch is ready. A working transcoder and listening socket do not prove
network quality on another phone. Human-confirmed Russian Assist applies to
the pilot and its selected pipeline; the per-line RTP and call-control results
above are not a load or endurance test.


### Compact Mac client and completed tenth-phone cleanup

After SIP conversion, the tenth phone saved its LakeDawn wallpaper and passed
CGI/screenshot and independently pinned SSH login checks. Its owned TFTP Job
was deleted by UID, the Pod/listener were absent, its exact source window was
closed, and the fallback timer stopped. Existing reply and unrelated firewall
rules were preserved. No PoE cycle was needed for this phone.

With explicit approval for its free noncommercial licence, Zoiper5 **5.6.15**
Community Edition was installed from the signed, notarized official package.
Linphone was quit before the existing **209** identity was entered into Zoiper;
no account secret or PBX configuration changed. The automatic protocol probe
reported “Not found”, but explicit SIP/TCP setup registered successfully.
Compact mode and collapse-on-hangup were enabled, avatars/animations and the
startup Pro screen disabled, and video left off. The local configuration and
backup were protected with mode 0600; unnecessary STUN and video codecs were
disabled for this local audio endpoint.

The operator confirmed audible echo on **209 → 600**. Asterisk observed PCMU
and **880 received / 879 transmitted** RTP packets with zero reported loss at
the 18-second sample. The call ended from the Zoiper UI. After restarting the
client to verify saved settings, **209 → 220** reached the physical phone's
`Ringing` state and was ended from Zoiper. That final test verifies routing and
ringing, not an answered two-way conversation.


### Extension-oriented CLI acceptance

The local `cisco` command was installed with an owner-only configuration and
separate token file outside the checkout. It uses the existing Kubernetes/HA
network path; no gateway firewall or RBAC expansion was needed.

- `cisco 221` resolved the second line of the 220/221 phone and displayed its
  status and command examples.
- `cisco 221 600` created an answered Echo call from **221**, with PCMU/RTP in
  both directions. `cisco 221 hangup` ended the exact observed call.
- `cisco 212 220` produced a call from **212** and a ringing channel on **220**.
  An unrelated incoming user call arrived on 212 during this check. The CLI's
  conservative multi-channel Hangup rule was not bypassed; the test harness
  ended only its own channel after checking its unique ID.
- `cisco 221 209` reached Zoiper with caller **221**. After an audio answer in
  Zoiper, both legs exchanged PCMU/RTP with zero reported loss in the sample.
  `cisco 221 hangup` ended that call. This is an automated media check, not a
  separate human voice-quality confirmation.
- `cisco 221 dnd on` changed the hosting phone's DND state, visible when reading
  line **220**. DND was restored to off and the phone had no test calls left.
- During a fresh **216 → 221** call, `cisco 221 pickup` changed the selected
  ringing channel to `Up`, and `cisco 221 hangup` ended it. A second call
  answered automatically with `cisco 221 autoanswer on`; reading line 220
  confirmed that the preference applies to the hosting phone. Auto-answer
  was restored to off. The test correlated both call legs by channel name,
  unique ID and linked ID, and cleanup targeted only those owned calls.

The CLI distinguishes a gateway's accepted request from a verified ringing,
answered or audible result. The tests above used live follow-up observations.

### Native Assist profiles on the later phones

The connected primary lines **210, 212, 214, 216 and 220** completed native
VoIP discovery through 630. Their Allow calls controls and existing Silero Baya
pipeline selections were verified separately. HA-originated Assist tests on
**210, 212, 214, 216 and 220** reached the listening state, exchanged RTP in both
directions and ended through the exact HA Hangup control.

Phone **218/219** was disconnected at this checkpoint. Its SIP conversion and
managed controls are complete, but its native Assist discovery and media test
remain pending until it is connected. Profile configuration and packet flow
do not replace the human listening checks listed above.

### Atomic scheduling service deployment

Cisco Phone Control **0.3.0** passed its local tests and was installed from a
frozen, hash-checked component snapshot. The full HA configuration check exited
successfully, followed by one planned restart. Verification preserved all
**6,877 registry identities** in that later baseline, all **290 phone controls**,
the **143 exact Recorder exclusions**, the dashboard and the native voice
profiles. The registry baseline is larger than the earlier tenth-phone
expansion baseline because subsequent native VoIP discovery added entities.

All ten physical-phone mappings returned valid responses from the actual
`cisco_phone_control.schedule` service, including default-only profiles with
no saved user changes. Existing timer deadlines and alarm settings were
preserved. The new API keeps each timer's and alarm's target line separate from
the general Line selector and exposes them through the CLI's schedule status.

The live CLI scheduling test then passed on **220/221**. A one-minute timer
captured line 221; replaying the same request ID retained its exact timer ID and
deadline. Canceling it from 220 was refused, as was starting another timer
without explicit replacement. A separate Monday/Wednesday/Friday alarm on 220
preserved the timer's 221 target, passed enable/disable checks, and refused a
disable request from 221.

With ordinary auto-answer enabled, that exact timer alert rang on **221 for
more than 27 seconds** without being answered automatically. Its identity was
correlated with the gateway's durable operation record. `cisco 221 timer cancel`
stopped the alert. Final readback showed no pending timer, no enabled alarm,
auto-answer off and no remaining channels on the tested phone. Original alarm
time, weekdays and owner, and the original five-minute duration were restored
through normal APIs. The last timer owner/ID, request history and alarm's
configured flag remained as truthful history; no HA storage file was edited.
