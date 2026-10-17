# Outbound SIP return-path firewall

[`external_firewall.py`](../scripts/external_firewall.py) adds a narrow return
path for an explicitly configured, outbound-only provider. It does not create
accounts, start calls, modify the router, or install a public port forward.
The provider endpoint must already follow the fixed-destination restrictions in
[external-sip.md](external-sip.md). Incoming provider profiles remain disabled.

The existing host guard drops unfamiliar RTP sources in raw PREROUTING, before
connection tracking. Simply adding a stateful INPUT rule cannot override that
drop. This tool first installs and verifies both IPv4 and IPv6 INPUT guards, then
inserts **one** IPv4 raw RETURN for UDP 10000–10199 addressed to the configured
private PBX address. The original raw RTP DROP remains immediately after it. All
other raw rules, including local SIP, AMI and TFTP, retain their exact contents
and order. [Netfilter hook priorities](https://wiki.nftables.org/wiki-nftables/index.php/Netfilter_hooks)
explain why the order matters.

## Traffic policy

- Existing IPv4 RTP source exceptions are copied exactly from a private capture
  of the old raw guard. There is no independent source list to accidentally widen.
  Existing LAN and Home Assistant traffic continues through the original policy.
- Other IPv4 RTP is returned to the existing INPUT policy only with conntrack
  state `ESTABLISHED` and direction `REPLY`, addressed to the PBX on UDP
  10000–10199. All other packets to that local RTP destination are dropped.
- The selected external SIP port, normally UDP **5070**, uses the same
  `ESTABLISHED` plus `REPLY` requirement and then a DROP. The supported configurable
  range is 5070–5099; use exactly the port generated for the provider transport.
- IPv6 gets no external exception. The original IPv6 raw rules are unchanged;
  the new INPUT guard drops the external SIP port and RTP range for IPv6.
- Rules use `RETURN`, never a broad `ACCEPT`. Existing later host policy can still
  reject traffic. Other local destination addresses and forwarded traffic are
  outside the IPv4 exception. The PJSIP transport must bind the selected address.

There is no `RELATED`, SIP connection-tracking helper, provider subnet guess,
OUTPUT rule, DNS snapshot allowlist or daemon. Provider destinations come from
validated PJSIP URIs and the runtime DNS resolver. An old DNS address does not
remain on a firewall allowlist because no such list exists. This policy does
not implement an expiring outbound DNS lease or restrict other software's
outbound traffic. It relies on the controlled PJSIP configuration to initiate
only the selected provider traffic. See the
[conntrack match reference](https://man7.org/linux/man-pages/man8/iptables-extensions.8.html)
for state and direction semantics.

Asterisk must send first to the actual remote IP/port. A response from a different
SIP proxy or RTP tuple is dropped, even if it belongs to the same provider. A
changed-source BYE/re-INVITE, early media, a silent caller, asymmetric RTP or NAT
port remapping can therefore fail closed. `rtp_symmetric` cannot learn an address
from packets the firewall dropped. Asterisk's
[`rtp_keepalive`](https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/Module_Configuration/res_pjsip/#rtp_keepalive)
can send comfort noise, but does not prove that the provider returns a matching
tuple. Test registration refresh, two-way audio, and hangup in both directions;
registration by itself is insufficient. Do not widen to all Internet sources to
work around a failed pilot.

## Capture and review on the PBX node

Finish all phone TFTP/dialplan refreshes first. Restore the ordinary phone policy
with TFTP closed, verify no calls are active, and coordinate other firewall
writers. The tool intentionally refuses unknown rules or a changed raw guard.
Its lock serializes only other invocations of this tool, not Kubernetes,
firewalld, other scripts, or manual commands.

Install the script without changing rules:

```bash
sudo install -m 0755 scripts/external_firewall.py /usr/local/sbin/cisco-external-sip-guard
```

Run capture on the actual PBX node using the **existing** raw chain name and
ownership comment. Values below are synthetic; determine the real ones from the
installed guard. Capture reads both `iptables-save -t raw` and
`ip6tables-save -t raw`; it creates a new, root-owned **0600** file and refuses
an existing output path. It never prints the captured source addresses.

```bash
sudo /usr/local/sbin/cisco-external-sip-guard capture \
  --bind-ipv4 192.168.50.10 --signaling-port 5070 \
  --raw-chain TEST_TELEPHONY --raw-owner synthetic-telephony/node \
  --config /etc/cisco-external-sip-guard.json
sudo /usr/local/sbin/cisco-external-sip-guard check \
  --config /etc/cisco-external-sip-guard.json
sudo /usr/local/sbin/cisco-external-sip-guard status \
  --config /etc/cisco-external-sip-guard.json
```

Review the private JSON locally before applying. The baseline accepts only the
existing owned source-RETURN/port-DROP pattern, a final owned RETURN, canonical
RFC1918 source networks and the exact first PREROUTING hook. It rejects a capture
of an already relaxed raw rule, foreign comments, additional references, missing
RTP protection, temporary TFTP source exceptions and unrecognized tokens. Do not edit the capture to make a drift
error disappear; reconcile the intended old policy first. Configuration must
remain a regular, non-symlink file owned by the invoking user with mode 0600.

`check` uses the kernel's `iptables-restore --test --noflush` for both address
families and the raw change. It does not apply rules. `status` reports recognized
states and hashes, not source addresses or raw rules. Unknown drift is an error;
no replacement is attempted. The script requires Python 3, iptables/ip6tables
save/restore, iproute2 `ss`, and privileges to manage this node's firewall.

## Apply and persist in a controlled window

Apply performs these stages in order:

1. Recognize exact current raw rules and absent, open or closed owned filter
   chains. Kernel-validate the complete target before changing it.
2. Install IPv6 and then IPv4 INPUT protection as the first rules in INPUT.
   Independently read and verify both before relaxing any raw RTP protection.
3. Insert the destination-limited IPv4 raw RETURN, retaining all other raw rules.
4. Read back every owned chain and compare hashes of unrelated chains/rules.
   A failed transition attempts to restore raw protection first, then retain a
   closed INPUT guard. A failed recovery is reported explicitly.

```bash
sudo /usr/local/sbin/cisco-external-sip-guard apply \
  --config /etc/cisco-external-sip-guard.json
```

The [systemd unit](../deploy/cisco-external-sip-guard.service) runs after and
requires the existing `cisco-telephony-guard.service`, and runs before
`k3s-agent.service`. It also runs after `cisco-phone-gateway-guard.service` when
that optional service is started, without requiring or starting it. This
serializes the two INPUT writers during boot and avoids their unrelated-rule
hash checks racing. Install it only after the initial check/apply is reviewed:

```bash
sudo install -m 0644 deploy/cisco-external-sip-guard.service \
  /etc/systemd/system/cisco-external-sip-guard.service
sudo systemctl daemon-reload
sudo systemctl enable --now cisco-external-sip-guard.service
```

The legacy guard's persistent configuration remains the ordinary closed phone
policy. On boot, it establishes the baseline first; the new service then installs
INPUT protection and enables the return path. Ordering does not make k3s depend
on successful external-SIP setup. If the new guard fails, leave external provider
profiles disabled until reviewed; the raw baseline still blocks outside RTP.
For a deployment that requires external-SIP readiness before k3s may start, add
an operator-reviewed `Requires=cisco-external-sip-guard.service` and matching
`After=` drop-in to k3s rather than assuming `Before=` expresses that dependency.

Recheck `status` after k3s/network startup and after any other firewall operation.
Both external-guard filters (IPv4 and IPv6) must report `first: true`, IPv4 raw
must include `open`, and IPv6 raw must remain recognized. This is not a continuous rule reconciler. Another writer
can move the hook or rewrite raw rules. If the legacy guard restores its exact
closed baseline, outside RTP stops; a fresh explicit `apply` can safely re-enable
it. Other drift is rejected. Some older guard implementations instead reject the
new raw RETURN; restore this tool's baseline before asking them to change policy.
Never run both writers concurrently.

The external and phone-management guards each request the first INPUT position.
With this startup order, the external guard is first and the management guard
follows it. The management status can therefore report `first: false` while its
rules remain effective: their protected ports are disjoint, and the external
chain returns unrelated traffic to the next INPUT rule. This is an understood
ordering limitation, not a reason to make both services repeatedly reapply.
Do not run periodic or concurrent reconciliation of these guards. When both need
an explicit update, apply the management guard first and the external guard
second, then verify the exact owned bodies and their order. An unknown ACCEPT or
other foreign rule ahead of the guards still requires investigation.

Before a later TFTP window or phone-inventory change, disable the external
provider transport and remove this guard using the current baseline and the
listener-stopped procedure below. Then change the legacy policy, finish TFTP,
restore its ordinary phone policy, and capture/review a **new** baseline before
reinstalling external protection. Remove the old filter **before** changing the
legacy raw policy; otherwise its exact-baseline check will reject removal. The
tool will not silently adopt a changed filter source list.

## Rollback and removal

`rollback` always restores and verifies the exact original IPv4 raw body **before**
closing the INPUT exceptions. It leaves both owned INPUT chains installed and
blocks external SIP replies too. This is safe while the UDP transport still
listens, although any external call or registration will stop working.

```bash
sudo /usr/local/sbin/cisco-external-sip-guard rollback \
  --config /etc/cisco-external-sip-guard.json
```

To remove the guard entirely, first disable external profiles and deploy the
configuration that removes their UDP transport. Stopping the systemd unit runs
rollback. Then `remove` restores the raw baseline again, verifies through `ss`
that **no UDP socket in either address family** listens on the configured SIP
port, and deletes only this tool's exact hooks and chains. It rechecks the
listener immediately before each deletion. Keep the external transport stopped
throughout removal; do not restart it after deleting protection.

```bash
sudo systemctl disable --now cisco-external-sip-guard.service
sudo /usr/local/sbin/cisco-external-sip-guard remove \
  --config /etc/cisco-external-sip-guard.json
```

A present listener makes removal fail with the closed guard retained. No foreign
chains, full tables, router settings or connection-tracking entries are flushed.
The private configuration is deliberately retained for audit and recovery.

## Tests and limits of evidence

```bash
python3 -m unittest discover -s tests -p test_external_firewall.py -v
EXTERNAL_FIREWALL_TEST_IMAGE=your-image-with-python3-iptables-iproute2 \
python3 -m unittest discover -s tests -p test_external_firewall.py -v
```

Set `EXTERNAL_FIREWALL_DOCKER_CONTEXT` for a non-default Docker context. The optional
runtime test starts a disposable `--network none` container with `NET_ADMIN`,
without host networking, mounted private files or provider connectivity. It
checks real iptables serialization and UDP connection tracking, unsolicited and
changed-tuple rejection, IPv6 rejection, retained local RTP sources, rollback,
and removal with a real listening socket. Its synthetic addresses exist only
inside that container. The test does not establish a provider's NAT behavior,
DNS availability or real audio quality. The normal suite skips this test unless
an image is explicitly selected.
