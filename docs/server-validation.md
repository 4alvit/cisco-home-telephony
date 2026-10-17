# Live server validation — October 17, 2026

The server is installed in the existing home k3s cluster. One physical 7945G
powered through PoE has successfully converted from SCCP to SIP and registered
as extension **201**. The operator confirmed a working handset echo test on
**600** and Russian spoken replies from Assist on **630**. Live checks also
verified calls initiated and ended from Home Assistant. Speakerphone, DTMF and calls between two phones
still require separate acceptance testing.

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
  actual HA source can reach AMI; access from the workstation was rejected.
  Other firewall rules were preserved. TFTP is closed during ordinary operation;
  the separately controlled first-phone commissioning window is described below.
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

Images were built on a standard GitHub `ubuntu-24.04` runner and passed
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

## Two additional physical phones: commissioning in progress

Two more CP-7945G units were identified independently through DHCP, HTTP,
LLDP and switch forwarding records. Their hardware revisions are **12.0** and
**13.0**. Both report the SCCP SR4-3 application file; an older HTTP Version
field disagrees with that application/LLDP observation and is not used as a
reason to downgrade. The existing verified SIP bundle is staged for both.

The PBX now contains six endpoints: **201/202** on the existing phone,
**203/204** on the second, and **205/206** on the third. Existing credentials
and the first phone's profile were preserved. The new profiles have numeric
labels, distinct line and management credentials, and the same wallpaper
catalogue. HA discovered all twenty endpoint entities for the four new lines
after a scoped integration reload; discovery alone is not registration.

Per-device DHCP reservations and option 150, exact-source firewall additions,
and a bounded TFTP Job were applied. Served-file hashes matched the prepared
profiles, firmware and artwork. Each new phone received one independently
verified PoE cycle; power was restored and all switch-port settings preserved.
Both phones then reported the intended TFTP server, but requested only old
trust files and signed configuration, with **Trust List Update Failed** in
their status logs. Their old profiles still disable SSH. A targeted handset
ITL erase is the outstanding step before they can accept the replacement
profile; no factory reset or signature bypass was attempted.

At this checkpoint, **201/202 remain registered**. The new phones are **not
yet converted or accepted**: their four contacts, wallpaper selection, remote
management, HA call controls and spoken Assist still need live verification.
See [the repeatable expansion runbook](adding-phones.md) for the complete
procedure and cleanup requirements.

## Remaining acceptance

1. Confirm audible echo on **202**, then verify speakerphone audio and DTMF
   separately from the successful handset echo test on **201 → 600**.
2. Verify dashboard confirmation behavior by clicking its controls; the
   underlying HA scripts have passed live Originate/Hangup tests.
3. Finish commissioning the two newly inventoried phones, then test calls in both directions,
   two-way audio, DTMF, hold/transfer and group ringing.
4. Repeat relevant tests after controlled phone/PBX restarts and AMI
   reconnection, then commission the remaining phones in small groups.

The registered pilot and service endpoint `ha-assist` do not establish that the
whole batch is ready. A working transcoder and listening socket do not prove
network quality on other phones. The observed voice results apply to this
pilot and the selected pipeline; they are not a load or endurance test.
