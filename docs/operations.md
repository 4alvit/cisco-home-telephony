# Operations and recovery

## Routine checks

```bash
kubectl --context home-k3s -n telephony get pods -o wide
kubectl --context home-k3s -n telephony logs deploy/asterisk --since=15m
kubectl --context home-k3s -n telephony exec deploy/asterisk -- asterisk -rx 'pjsip show contacts'
kubectl --context home-k3s -n telephony exec deploy/asterisk -- asterisk -rx 'core show channels count'
```

Phones should register to the home node's IP; audio packets travel between that
node and the phones. Check free disk space, memory, restarts and packet loss on
the wired interface. Do not move the Pod automatically to another node: the
phones and option 150 refer to a fixed address.

Readiness confirms that both SIP transports exist in Asterisk; it does not
measure call quality. After every change, make a real two-way call and test
Cisco echo 600. You can later add a SIPp scenario with a dedicated test endpoint
for ongoing audio checks. Monitoring must not write passwords or call
recordings into shared logs.

## What persists

This baseline PBX is **stateless**: `AstDB`, spool and file logs live in
`emptyDir` volumes and disappear when the Pod is recreated. Voicemail,
recordings, persistent CDR and retained AstDB state are not enabled. Phones
restore registrations after a restart. Adding voicemail/recording requires a
separate PVC, retention policy, encryption and a recovery test; the current
backup does not back up a persistent volume that does not exist.

Configuration comes from code in Git plus the **separate** `.local/` directory:
site settings, verified inventory, secrets, XML, Asterisk configs, the manifest
and licensed firmware. Kubernetes Secrets do not replace that backup. Image
versions are pinned by digest; retain the images in a reliable registry or
export them separately with `docker save` so registry garbage collection cannot
remove your rollback option.

## Encrypted backup and recovery

Install `age` and create your public recipient in advance.

```bash
# AGE_RECIPIENT is your age1... public recipient, not a private key.
bash scripts/backup.sh "$AGE_RECIPIENT" "$HOME/backups/cisco-home-2026-10-16.tar.gz.age"
# Check decryption/archive contents without displaying passwords; filenames are private.
age -d -i "$AGE_IDENTITY_FILE" "$HOME/backups/cisco-home-2026-10-16.tar.gz.age" | tar -tzf -
```

The destination directory must exist. Keep at least one encrypted copy outside
the selected k3s node, with the private age key separate from the backup. The
script refuses to overwrite an existing file. Restore into a new private
directory to avoid overwriting your current working configuration:

```bash
umask 077
mkdir -p "$HOME/cisco-restore-check"
age -d -i "$AGE_IDENTITY_FILE" "$HOME/backups/cisco-home-2026-10-16.tar.gz.age" | \
  tar -xzf - -C "$HOME/cisco-restore-check"
python3 scripts/preflight.py --site "$HOME/cisco-restore-check/.local/site.json" --with-tftp
```

After verification, compare the site/node IP, firmware hashes, inventory and
image digests. Choose a maintenance window, move the restored `.local` into the
checkout and follow the normal deployment process. Restoring to a different IP
requires DHCP/TFTP/phone configuration changes; it is not transparent failover.

## Updates and rollback

Before changing anything, use `core show channels count` to confirm that there
are no active calls, make an encrypted backup and record the Git SHA. A new
Asterisk version must pass the build and smoke test, then testing with one real
phone. Do not update all ten phones' firmware at the same time as Asterisk.

Changing any `.conf` creates a new immutable Secret with a hash in its name.
The Pod receives the new Secret reference and is recreated through `Recreate`.
This **interrupts calls**: update during a maintenance window. Old Secrets are
intentionally not deleted automatically.

```bash
kubectl --context home-k3s -n telephony rollout history deployment/asterisk
kubectl --context home-k3s -n telephony rollout undo deployment/asterisk
kubectl --context home-k3s -n telephony rollout status deployment/asterisk --timeout=180s
```

A ReplicaSet rollback restores the previous image digest and Secret reference.
It works only while the old Secret exists and the image remains available.
Do not use this command to roll back firmware: bootloader, signature and
permitted downgrade behavior depend on the current load. After rolling back
Kubernetes, restore the corresponding local configuration from backup/Git;
otherwise the next apply will reintroduce the failed change.

If SIP credentials changed, rolling back Asterisk separately from phone XML can
leave passwords out of sync. Restore the matching Secrets/XML set and update
the phones during a TFTP window. Generate new passwords locally and never send
them in tickets. Remove old immutable Secrets only after a verified backup and
the agreed rollback window; retain Secrets referenced by active ReplicaSets.

## Troubleshooting

For authenticated screenshots, remote key presses, callback renewal, legacy
SSH/HTTPS failures and single-port PoE recovery, use the
[remote-management runbook](phone-control.md). For the second line and the
required wallpaper sizes/catalogue, use the
[two-line and wallpaper guide](second-line-and-wallpaper.md). The
[live validation record](server-validation.md) distinguishes what worked on the
pilot from acceptance checks still needed for other phones.

**Dark screen with a steady green Display button:** the 7945G uses this light
for screen sleep. The separate Display button has overlapping-rectangle symbols
and sits to the right of the four softkeys below the screen. Press it once to
wake the display; lifting the handset also wakes it. Check this before assuming
a power or firmware failure. See the [Cisco 7945G/7965G user guide, printed
pages 9–11 and 15](https://www.cisco.com/en/US/docs/voice_ip_comm/cuipph/7965g_7945g/6_1/english/user/guide/65enu61.pdf).

**Pod Pending:** check the exact node name, Ready/cordon/taints, requests and free
memory. Strict node selection is intentional. Do not remove unrelated taints
or move the Pod to OCI without changing the architecture.

**ImagePullBackOff:** the digest must exist, the image architecture must match
the node, and the registry must be reachable from that node's containerd. Check
`kubectl describe pod` and the local k3s journal. Do not print registry secrets
into logs. `localhost:5000` on a laptop and on a node are different addresses;
use an explicit registry address.

**CrashLoopBackOff / bind failed:** check configuration and permissions, confirm
that the node actually has the configured IP and that port 5060 is free. A
second Asterisk Pod or another SIP daemon can occupy the same port. With a
read-only root filesystem, only mounted writable directories accept writes.
Do not work around this error by running Asterisk privileged or as root.

**Phone does not request firmware:** check its physical model/MAC, DHCP, VLAN,
TFTP IP via option 150/Alternate TFTP, the `SEP<MAC>.cnf.xml` filename, TFTP Job
status, and the exact `.loads` file plus every required `.sbn`. The provided
TFTP server uses UDP 69 and the separate UDP 20000–20031 range. Unanswered RRQs
can indicate blocked return UDP, a wrong VLAN or an expired Job. Files must be
readable with mode 0644 and served read-only. An RRQ/error response exchange
proves that particular round trip, not a successful file transfer.

**Only trust files and signed XML are requested:** repeated `CTLSEP…`, `ITLSEP…`,
`ITLFile.tlv`, `SEP<MAC>.cnf.xml.sgn` and `XMLDefault.cnf.xml.sgn` requests, with
no plain XML or firmware request, call for inspecting old CUCM trust after
confirming the TFTP target and network exchange. Use the
[targeted ITL/CTL procedure](sip-conversion.md#old-cucm-trust-blocks-the-new-configuration)
before considering a factory reset. A missing signed configuration is not
fixed by renaming plain XML to `.sgn`. Do not fabricate trust files or disable
firmware signature verification.

**TFTP Job Failed/DeadlineExceeded:** this is the expected end of the window
after one hour. Its 600-second cleanup TTL removes the finished Job and dependent
objects, **not host firewall rules**. Confirm that no transfer or flash is active,
delete the old Job and close the temporary TFTP allowances. If another attempt
is needed, review and reopen a narrow firewall window, then repeat
`deploy.sh --apply --firewall-reviewed --with-tftp`. Do not run two provisioning
daemons on the same IP:69. See Kubernetes documentation for
[Job deadlines](https://kubernetes.io/docs/concepts/workloads/controllers/job/)
and [finished-Job cleanup](https://kubernetes.io/docs/concepts/workloads/controllers/ttlafterfinished/).

**No registration:** check SIP firmware, matching TCP/UDP selection in XML and
Asterisk, authName/extension/password consistency, server IP, the permitted
source CIDR, time/NTP, and any previous owner's ITL/CTL as described in the
conversion guide. With an explicit `phone_cidrs` list, check the phone's actual
current IP against that list and the separate host firewall. The initial SIP 401
is usually a normal digest challenge; repeated 401/403 without a successful
REGISTER require a credentials/configuration check. CUCM and MPP provisioning
procedures do not apply to this CP-7945G setup.

**Echo test rings or returns no speech:** end the current call, start a new call
and enter **600** explicitly. Check the dialed number on the display and actual
Asterisk execution before diagnosing RTP. The generated route must execute
`600@home`, then `Answer()` and `Echo()`. A log showing `201@home` and a call to
`PJSIP/201` is a self-call when the caller is also 201; it does not reach the
echo test. Review the recent Asterisk logs and `dialplan show 600@home`; confirm the call reached `Echo()`
before moving to the audio checks below.

**Calls connect, but audio is missing or one-way:** SIP 5060 and RTP are separate
flows. Check UDP 10000–10199 toward Asterisk and return traffic to the phone's
RTP ports. SDP must contain real LAN addresses, not PodCIDR/OCI/WireGuard
addresses. Both ends initially use G.711 µ-law/A-law. This setup has
`direct_media=no`, so audio passes through Asterisk and its host firewall.
Disabling SIP ALG on an edge router makes sense if that router is actually in
the SIP path; do not alter unrelated firewall settings by trial and error.

**Contacts disappear after a restart:** devices must register again. Test a
cold boot after closing TFTP: a particular firmware load may need its TFTP
configuration again. Open a new narrow window for that boot if required.

Limit diagnostic packet capture to a short window: SIP and TFTP can contain
credentials, phone numbers and audio. Keep pcap files private and delete them
when the investigation ends; never commit `.pcap` files.

## Close a TFTP maintenance window

Once transfers and phone flash writes have finished, remove the temporary
server without stopping Asterisk:

```bash
kubectl --context home-k3s -n telephony delete job cisco-tftp --ignore-not-found
```

Then remove the maintenance allowances for **UDP 69 and UDP 20000–20031** from
the host firewall, including any persistent rules that would restore them on
reboot. Preserve SIP/RTP and the separately restricted AMI rules. Verify that
no TFTP server remains and that the firewall has returned to its closed state.
Neither Job expiry nor TTL cleanup performs this firewall step. If the phone
needs TFTP on a later cold boot, open a new reviewed window.

## Stopping and removal

```bash
# Finish active firmware flashes and calls first.
kubectl --context home-k3s -n telephony delete job cisco-tftp --ignore-not-found
kubectl --context home-k3s -n telephony scale deployment/asterisk --replicas=0
```

Then remove temporary firewall rules and option 150 only from the dedicated
phone scope. Scripts do not automatically delete firmware from the node. For
full removal of namespace/Secrets/images, first verify a backup and enumerate
exactly this project's resources; `delete namespace` destroys every object in
that namespace.

## Validation boundaries

On October 16, 2026, local Python unit/integration tests and shell syntax checks
passed. Both Docker images were built on **Linux ARM64** in an existing local
Docker runtime: Asterisk 22.11.0 and tftp-hpa 5.2. The build exposed and resolved a
staging `/var/run` conflict with Debian's `/var/run -> /run` symlink.

The complete `scripts/smoke-containers.sh` passed: synthetic endpoint 201, both
SIP transports, `chan_pjsip` and dialplan 600/601/620 loaded; the TFTP init script
ran as UID 10001 on a root-owned fsGroup volume; and the TFTP daemon, with its
actual flags/capabilities, served a file through RRQ. The network was isolated;
no real MAC addresses, passwords or firmware entered the test. The script
removed its temporary containers, fixture images and test network.

This verifies the ARM64 build/runtime. The `Validate and build` GitHub Actions
workflow checks **Linux AMD64** separately. Check its current Actions status;
do not equate an ARM64 smoke test with a test on a specific home worker. Smoke
tests do not replace Cisco boot, licensed firmware verification, PoE,
DHCP/firewall validation, physical registration or two-way audio. After initial
preparation, the server side was deployed to the home k3s cluster on
October 17, 2026; its [validation results](server-validation.md) are separate from
physical-phone acceptance that has not yet been completed.

With `autoload=yes`, Asterisk also tries to load optional modules and may emit
messages about missing queues/ARI/phoneprov configurations and other unused
features. This project verifies the required PJSIP transports/endpoints and
dialplan; errors for **those** objects must not be ignored. A missing Ethernet
interface for the global EID is expected only in the isolated loopback smoke
test.

## Home Assistant validation

When [HA integration](home-assistant.md) is enabled, the smoke test also opens a
real AMI TCP session in the isolated container. It checks PJSIP discovery,
CoreSettings, internal Local Originate, Hangup permission and rejection of CLI
Command. It also verifies actual G.711↔Opus translation paths and route 630.
This does not replace a call through the live HA pipeline and a physical Cisco:
accept STT, TTS and two-way RTP using the
[complete acceptance procedure](https://github.com/ha-homelab/asterisk-telephony/blob/main/docs/acceptance.md).
