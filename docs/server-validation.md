# Live server validation — October 17, 2026

The server is installed in the existing home k3s cluster. This record does not
constitute physical-phone acceptance. One 7945G has since been powered through
PoE and identified on the LAN; SIP flashing, registration and audio tests remain
pending.

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
  first-phone instructions and documentation links. Its configuration was verified
  by reading it back through the API.
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

## First physical phone

A **CP-7945G**, hardware revision **13.0**, was found on the LAN after being
connected through PoE. Its current application is **SCCP45.9-4-2SR4-3S** and its
boot load is **tnp45.9-3-1-CR17.bin**. Device and network status were read from its
web interface and saved privately. MAC, serial number and network details are
not published. This confirms one powered, reachable phone, not SIP registration
or the condition of the remaining batch.

The [preserved SIP45 SR4-3 bundle](firmware-archive.md) was subsequently downloaded,
its eight component hashes checked, and the same files staged on the PBX node.
A verified private profile for extension **201** was deployed; Asterisk is Ready
and Home Assistant discovered its five phone entities after an integration
reload. The phone remains unregistered on SCCP. Its source is admitted by a
single-host `/32` rule, and the existing NTP server was verified. The existing
AMI password was preserved and `AMI Connected` remains on.

The first commissioning attempt then enabled a bounded TFTP Job and a
single-phone firewall exception. A DHCP reservation and option 150 were applied
only to that phone. After a normal restart, its web interface confirmed the new
TFTP address, and packet capture showed requests reaching the server with
file-not-found replies returning to the phone. The requested files were CTL/ITL
trust files and signed `SEP…cnf.xml.sgn` / `XMLDefault.cnf.xml.sgn` configuration.
The ordinary XML profile and firmware components had not been requested.

The phone's console logs also showed signed-configuration lookup and trust-list
errors. The next step is targeted removal of the previous CUCM trust list at
the handset, following the [trust-list procedure](sip-conversion.md), before
another controlled boot. This is a commissioning observation, not a successful
flash: vendor signature acceptance, SIP registration and audio remain pending.
Raw captures, phone logs and device identifiers stay private. Closing the TFTP
Job must be accompanied by closing its temporary firewall exceptions; the
Job's time limit does not perform that firewall cleanup.

## Remaining physical acceptance

1. Resolve the previous CUCM trust configuration at the handset, then restart
   in an active per-phone TFTP commissioning window. Follow the
   [upgrade path](sip-conversion.md); preserve device signature checks.
   Firmware is not stored in Git.
2. Confirm SIP firmware and registration before accepting the phone for service.
3. Verify registration, echo600, a call between two phones, DTMF and two-way audio.
   For Assist, enable calls for the actual VoIP device after its first call and
   select a working STT/TTS pipeline.
4. Map actual HA entity IDs and install the call-control dashboard from the
   [companion repository](https://github.com/ha-homelab/asterisk-telephony).
5. Inventory and commission the remaining phones in small groups.

The service endpoint `ha-assist` does not establish that any Cisco is registered.
A working transcoder and listening socket do not prove network quality,
Assist pipeline behavior or audio in a handset.
