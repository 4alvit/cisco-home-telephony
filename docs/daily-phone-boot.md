# Daily phone boot and TFTP availability

**Current operation:** reopen the reviewed, source-restricted TFTP window before
an authorized phone reboot. **Proposed improvement:** a persistent service for
profiles only, with firmware confined to a separate maintenance window. That
service is not implemented or deployed by this document.

![Current boot requirements and proposed separation of profiles from firmware](diagrams/daily-phone-boot.svg)

## What is stored, and what is requested

The 7945G stores firmware images and user preferences in nonvolatile flash. Its
normal startup loads the stored image, obtains network settings, contacts TFTP,
requests configuration, and then connects to its call-control server. Cisco
documents configuration requests on reset/registration and a load-file request
when the configured image differs from the running image. These are different
operations: fetching a profile is not reinstalling firmware. See Cisco's
[configuration and startup chapter](https://www.cisco.com/c/ja_jp/td/docs/vauc/iptelep/ipphone7900/ag/029/P747_BK_W06BD6D9_00_adminguide_7945-7965-7970-7971-7975/P747_BK_W06BD6D9_00_adminguide_7945-7965-7970-7971-7975_chapter_010.html)
(official Japanese edition) and the
[English Cisco guide, pages 34–37](https://www.th-wildau.de/files/HRZ/dokumente/voip/Cisco-Unified-IP-Telefone-7975G-7971G-GE-7970G-7965G-und-7945G-2018.pdf#page=54)
(Cisco-authored PDF mirrored by TH Wildau).

This does not establish that every SIP load can register to Asterisk using only
cached configuration. Nor does a `.loads` request prove that firmware components
were downloaded or written. Distinguish requested filenames, completed transfers,
the resulting running load, and fresh SIP registration. Do not assume either
“nothing is cached” or “TFTP is unnecessary after conversion.”

## What the beta10 recovery showed

One configured phone failed to restore registration after an authorized power
cycle while TFTP was closed. Its web interface returned, but later PBX checks
did not confirm either fresh registration. Earlier retained contacts were not
evidence of successful boot. A version-display discrepancy was also observed;
it did not establish that the firmware had been replaced.

After the restricted TFTP window opened, both lines registered without another
reboot and the clock corrected. The same SIP application component was reported,
while the older web version label remained. The window made the accepted profile and firmware set available; actual
requests and completed transfers were not logged.
This supports a TFTP dependency but does not isolate its cause, prove a
profile-only boot, or establish behavior across all ten phones. The window was
then closed and its firewall baseline restored. Keep raw receipts private.

## Before a reboot today

1. Identify the exact phone and its DHCP reservation/TFTP destination. Reuse the
   accepted profile, dial plan, manifest and firmware bundle from its last
   successful deployment; compare their private hashes.
2. Reopen that reviewed TFTP maintenance window for the intended source address,
   including UDP 69 and the configured UDP 20000–20031 transfer range. Retain
   default drops for other sources and IPv6. Verify the listener, exact served
   files, bounded lifetime and cleanup ownership before rebooting.
3. Restart only the authorized phone, after confirming no firmware write is in
   progress. Do not rerun provisioning just to restore file availability.
   `scripts/deploy.sh` also reapplies the core PBX configuration and can restart
   Asterisk; it is not a TFTP-only reboot helper. Follow the existing
   [deployment runbook](k3s-deployment.md).
4. Verify boot completion, intended load and fresh registrations for both lines.
   A web response, old contact, RRQ or running TFTP Pod alone is insufficient.
   After transfers finish, close the exact window and verify removal of its
   listener and temporary firewall exceptions. Retain the ordinary DROP rules.

Do not use a factory reset or repeated power cycles to diagnose a missing file.
Firmware recovery remains a separate operation with the reviewed signed bundle;
Cisco's [7945/7965 SIP firmware instructions](https://www.cisco.com/web/software/282074288/128566/cmterm-7945_7965-sip.9-4-2SR2-2-readme.html)
describe signed-image authentication and manual TFTP installation.

## Proposed profile-only service

The implementation plan is a separate, single-replica k3s Deployment on the
fixed TFTP node/address. It would serve an explicit, read-only allowlist of
generated `SEP<MAC>.cnf.xml` profiles, `dialplan.xml`, and reviewed optional
non-firmware assets. It must not mount the firmware directory or expose `.loads`,
`.sbn`, arbitrary paths, uploads, or symlink targets. Private profiles contain
credentials and must stay outside Git and public logs.

Only one process may own the TFTP address/ports. Switching to bounded firmware
maintenance must quiesce the daily listener, confirm its absence, then start
the maintenance owner; restore the daily owner only after confirmed cleanup.
Deployment strategy alone does not serialize an independent Job or host daemon.

Network restrictions must use explicit inventoried handset IPv4 `/32` sources,
matched to the fixed TFTP destination, and retain default DROP rules. Do not
inherit broad `phone_cidrs`, softphone or management-client ranges. An allowed
source IP is not device authentication. With a shared
TFTP root, an allowed phone can request another phone's profile; do not claim
per-file authorization or confidentiality between allowed devices.

The current [external SIP firewall](external-firewall.md) deliberately accepts
only a baseline with TFTP closed. Persistent exceptions conflict with that
contract. Do not remove the validation, edit captured evidence, bypass drift
checks, or add broad accepts. A reviewed policy/version transition, writer
ownership and rollback design are prerequisites to a daily service.

## Gates before permanent operation

First test the renderer, strict file selection, path/symlink rejection and port
ownership offline. Then use isolated network tests for allowed/denied sources,
missing files, transfer ports, IPv6, firewall drift and interrupted owner changes.
Finally, in an attended window, prove warm and cold boot with profiles available
and **no firmware served**, fresh registrations, and recovery when TFTP is
unavailable. Record whether firmware requests are harmless probes or a real
dependency. If that gate fails, retain bounded maintenance; do not quietly add
firmware to the daily root. Roll out further phones only after that evidence.
