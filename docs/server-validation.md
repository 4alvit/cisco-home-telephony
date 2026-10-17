# Live server validation — October 17, 2026

The server is installed in the existing home k3s cluster. One physical 7945G
powered through PoE has successfully converted from SCCP to SIP and registered
as extension **201**. Full phone acceptance remains incomplete: real audio,
echo, Assist and calls between two phones still require testing.

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
  five entities for extension 201, first-phone instructions and documentation
  links. Its English configuration was verified by reading it back through the
  API. Registration was subsequently checked independently on Asterisk and HA,
  as described below.
- HA configuration and `.storage` were backed up locally before installation.
  Following HA's internal restart, it returned to `RUNNING` with safe mode off;
  all **120 automations and 94 scripts** were preserved.

Images were built on a standard GitHub `ubuntu-24.04` runner and passed
[native AMD64 build and isolated smoke tests](https://github.com/4alvit/cisco-home-telephony/actions/runs/39029793014)
from commit `f5cb1546ed93c53d4792817d0bb1ba0f3a929821`. Archive checksums,
architecture and registry manifest/config digests were checked before deployment.
The temporary GitHub artifact was deleted after transfer. Registry addresses,
full deployment receipts, firewall configuration, entity maps and secrets remain
local. Later readiness changes did not modify these images' Dockerfiles.

The subsequent [CI run for the current implementation](https://github.com/4alvit/cisco-home-telephony/actions/runs/39051675792)
passed on `main` at commit `e33b9996e728576ff55ef8c622520fcfeb8b9d1e`, including
the image builds and isolated smoke tests. These checks complement the live
results; they do not establish handset audio quality.

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
for the pilot phone. It does not verify audio, DTMF or Assist.

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

## Remaining physical acceptance

1. Dial **600** on the pilot and verify echo, handset/speakerphone audio and DTMF.
2. For **630** Assist, enable calls for the actual VoIP device after its first
   call, choose a working STT/TTS pipeline and verify speech recognition, spoken
   responses and hangup.
3. Verify HA call control against the pilot's actual active Channel using the
   [companion acceptance guide](https://github.com/ha-homelab/asterisk-telephony/blob/main/docs/acceptance.md).
4. Inventory and commission a second phone, then test calls in both directions,
   two-way audio, DTMF, hold/transfer and group ringing.
5. Repeat relevant tests after controlled phone/PBX/HA restarts, then commission
   the remaining phones in small groups.

The registered pilot and service endpoint `ha-assist` do not establish that the
whole batch is ready. A working transcoder and listening socket do not prove
network quality, Assist pipeline behavior or audio in a handset.
