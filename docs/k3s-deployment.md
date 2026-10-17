# Cisco 7945G → Asterisk in a home k3s cluster

This guide deploys an internal PBX for ten Cisco CP-7945G phones. Convert the
phones to suitable **SIP firmware** using the separate conversion guide first.
External SIP trunks, PSTN/emergency calling, voicemail and public access are
outside this setup. Two phones, echo `600`, tone `601` and ring group `620` let
you verify the complete local call path.

## 1. Choose the home node

Public examples use an illustrative network: node `voice-node`, IP
`192.168.50.10`, LAN `192.168.50.0/24`, context `home-k3s` and registry
`192.168.50.20:5050`. These are **not actual infrastructure values or the result
of a live cluster check**. Keep your own values in `.local/site.json`.

Choose a wired Linux worker inside the home: voice traffic should use the LAN,
not a cloud control plane or VPN. A fixed node means the node selected for this
Deployment; it need not be an otherwise unused physical computer. Check its
resources, ports and CPU architecture. Do not reinstall an existing k3s cluster
for this PBX.

The registry example uses HTTP on a trusted home network. It requires separate
configuration for **Docker builder pushes** and **k3s/containerd pulls**. The
[HTTP registry prerequisite](../deploy/README.md#plain-http-registry-prerequisite)
describes the scoped configuration; an existing k3s mirror does not configure
Docker. Prefer a registry with TLS. An image digest is required in either case.

Asterisk runs in one Pod with `hostNetwork: true`, node selection by
`metadata.name`, `replicas: 1` and the `Recreate` strategy. SIP and RTP use the
node IP. There is no Kubernetes Service/Ingress/NodePort/LoadBalancer, DNS tunnel,
router port forwarding or DHCP workload. Do not scale to two replicas: both
would compete for the same ports.

## 2. Prepare local settings and firmware

You need Python 3.9+, kubectl with existing cluster access, Docker/BuildKit on a
suitable builder, SSH access to the selected node and licensed Cisco firmware.
The image architecture must match the node. The build script defaults to
`linux/amd64`; set `BUILD_PLATFORM=linux/arm64` for an ARM node. For amd64, use a
native amd64 builder or correctly configured BuildKit emulation.

```bash
umask 077
mkdir -p .local/firmware
cp deploy/site.example.json .local/site.json
# Edit .local/site.json: verify node/IP/CIDR/context and the phone source ACL.
# Set ntp_ipv4 to an existing reachable NTP server; the node need not provide NTP.
# Use the inventory example in README with physically verified MAC addresses.
python3 scripts/provision.py --site .local/site.json --inventory .local/phones.json
python3 scripts/verify_firmware.py --directory .local/firmware --manifest .local/firmware-manifest.json
```

The final command must pass before copying firmware to the node. Do not download
unverified firmware from forums. Keep vendor firmware, MAC addresses/inventory,
passwords, kubeconfig, generated XML and Secret manifests out of Git.

## 3. Build, test and publish images

If the node is AMD64 and your workstation is ARM, use the documented
[native build and one-time export through GitHub Actions](ci-images.md).
It uses the same Dockerfiles and smoke test; ordinary push/PR runs retain no
image artifacts.

Asterisk is pinned to **22.11.0 LTS**, announced by the official site; the source
archive SHA-256 is pinned in the Dockerfile. This checks that the reviewed HTTPS
archive has not changed; it is not a claim that a separate GPG signature was
verified. The Debian `bookworm-slim` base is resolved to a digest before the
build. APT dependencies come from the current Debian repository, so builds are
not bit-for-bit reproducible. Final deployment image references must include
digests.

```bash
# On an amd64 builder for voice-node. This command does not push.
bash scripts/build-images.sh 192.168.50.20:5050/cisco-home
# Publish to your reachable registry:
docker push 192.168.50.20:5050/cisco-home/asterisk:22.11.0
docker push 192.168.50.20:5050/cisco-home/tftp:bookworm
docker image inspect --format '{{join .RepoDigests "\n"}}' \
  192.168.50.20:5050/cisco-home/asterisk:22.11.0
docker image inspect --format '{{join .RepoDigests "\n"}}' \
  192.168.50.20:5050/cisco-home/tftp:bookworm
```

For an HTTP registry, `registries.yaml` on the k3s node does not configure the
Docker builder. The builder separately needs a scoped `insecure-registries`
entry for your `HOST:PORT`: see the
[procedure and limits](../deploy/README.md#plain-http-registry-prerequisite).
Do not overwrite an existing daemon.json or restart a shared builder without a
maintenance window.

Put the corresponding `repository@sha256:…` references into `asterisk_image` and
`tftp_image` in `.local/site.json`. If the registry requires authentication,
create a pull Secret in namespace `telephony` from a separate Docker config file
and add its **name** to `image_pull_secrets`. Do not put a PAT/password into
site.json or shell history.

Test the images with the real configuration generator, a synthetic MAC, a
random temporary password and a loopback IP. The containers cannot access the
LAN:

```bash
bash scripts/smoke-containers.sh 192.168.50.20:5050/cisco-home
```

The smoke test checks `chan_pjsip`, endpoint 201, UDP/TCP transports and dialplan
600/601/620, then performs a real TFTP RRQ and checks the init script as UID10001
on a root-owned volume accessible through fsGroup. Asterisk has no network;
TFTP uses a disposable Docker internal network with no published ports. To
support remote Docker contexts, synthetic configs are copied into disposable
fixture images, which are removed along with the test containers and network.
GitHub Actions runs these same checks after building both images. Test physical
Cisco registration and two-way audio separately on the LAN.

## 4. Check the node, firewall and DHCP

```bash
kubectl --context home-k3s get nodes -o wide
python3 scripts/preflight.py --site .local/site.json --cluster --with-tftp
# Read-only checks on the selected node:
ssh voice-node 'ip -br addr; sudo ss -lntup; df -h /var/lib; free -h'
ssh voice-node 'sudo nft list ruleset'
```

First check that nothing else owns TCP/UDP `5060`, UDP `69`, UDP `10000–10199` or
UDP `20000–20031`. `ss` alone cannot prove a range is not used dynamically;
compare it with other hostNetwork workloads and their configurations. Allow at
least 512 MiB RAM and a 1 CPU limit for Asterisk. Short LAN calls usually need
less, but verify resource usage and audio under real load.

Allow the trusted phone sources in `phone_cidrs` (default: `lan_cidr`) to reach
the selected node on:

- TCP **and** UDP `5060`: SIP signaling;
- UDP `10000–10199`: Asterisk RTP;
- during provisioning only, UDP `69` **and** `20000–20031`: TFTP RRQ and transfers;
- DNS/NTP/DHCP at the existing network servers, not new Pods in this setup.

An explicit phone `/32` can be outside the PBX's `lan_cidr` if routing already
works in both directions. Match that source in the host firewall rather than
widening the allowed subnet. Changing `phone_cidrs` requires regeneration;
preflight rejects stale generated ACLs. It does not install firewall rules.

Replies must reach the phones' dynamic client UDP ports. `69/udp` alone is not
enough for TFTP. Ports `20000–20031` bound the server transfer sockets; flash one
phone first, then small batches. The RTP range belongs to the server; the phone
chooses its own RTP source port.

Adapt `deploy/firewall.nft.example` to the node's existing firewall. First use
`sudo nft -c -f …` to check syntax, then have the firewall owner install and
persist the rules through the existing mechanism. **Do not run
`nft flush ruleset`**: it would remove k3s rules. The template allows LAN traffic,
but other rules can still block it. NetworkPolicy support for hostNetwork
depends on the CNI, so ordinary Pod NetworkPolicy is not considered protection
here. Do not expose SIP/TFTP to WAN, ZeroTier/WireGuard peer networks or a home
Cloudflare tunnel.

DHCP stays on the existing router. Within the phone scope, set Cisco option
**150 = 192.168.50.10** (the TFTP IP), after confirming the selected address.
For dnsmasq this is usually `dhcp-option=tag:phones,150,192.168.50.10`, but the
`phones` tag must actually be assigned to the intended MAC addresses/range: do
not blindly paste this line into global configuration. For one test phone you
can set Alternate TFTP manually. Do not run a second DHCP server on the LAN.

## 5. Deliver firmware to the selected node

Only the firmware directory is created on the node; it contains no password
configurations. Kubernetes Secrets deliver the XML. Verify the SSH host key
through your normal trusted process first.

```bash
rsync -av --checksum .local/firmware/ voice-node:/tmp/cisco-firmware/
ssh voice-node 'sudo install -d -m 0755 /var/lib/cisco-tftp'
# Only after local manifest verification; all inputs must be regular files in one directory.
ssh voice-node 'sudo find /tmp/cisco-firmware -maxdepth 1 -type f -exec cp {} /var/lib/cisco-tftp/ \;'
ssh voice-node 'sudo find /var/lib/cisco-tftp -maxdepth 1 -type f -exec chmod 0644 {} \;'
```

Compare the node's SHA-256 values with `.local/firmware-manifest.json`; a matching
filename does not prove a matching version. For example, run
`ssh voice-node 'sha256sum /var/lib/cisco-tftp/*'`. These commands omit
`rsync --delete`, so existing files are not silently deleted. Before installing
a new firmware set, explicitly remove or archive obsolete files to avoid
serving the wrong load. Subdirectories and symlinks are prohibited in the set.
Images contain no Cisco firmware; you retain your local licensed copy when
provisioning is complete.

## 6. Render and apply

```bash
python3 -m unittest discover -s tests -v
bash scripts/deploy.sh --with-tftp
# The default is local generation ONLY, with no kubectl calls.
```

Review `.local/rendered/core.yaml` and `tftp.yaml` privately: they contain
Secrets, and base64 is not encryption. The first server dry run requires an
existing namespace; dry-running a Namespace does not save it for validation of
its children.

```bash
# These commands change only the dedicated namespace and start no workloads.
kubectl --context home-k3s create namespace telephony
kubectl --context home-k3s label namespace telephony \
  pod-security.kubernetes.io/enforce=privileged \
  pod-security.kubernetes.io/audit=restricted \
  pod-security.kubernetes.io/warn=restricted --overwrite
bash scripts/deploy.sh --server-dry-run --with-tftp
# After firewall/socket/firmware review, during an agreed maintenance window:
bash scripts/deploy.sh --apply --firewall-reviewed --with-tftp
```

`hostNetwork` needs an exception from Pod Security baseline/restricted. The
privileged label permits this at the **dedicated namespace** level, while
Asterisk itself is unprivileged: UID10001, no capabilities and a read-only root
filesystem. TFTP starts as root only for bind/chroot/setuid, with the four
required capabilities, then `tftpd-hpa` switches to user tftp. Do not give
unrelated users permission to create Pods in this namespace. Kubernetes Secrets
need appropriate RBAC and encryption of the existing cluster datastore/backups.

The TFTP Job lasts at most **60 minutes**, then terminates; the TTL controller
removes it ten minutes later. Begin the first flash as soon as the Job is ready.
Do not start another flash near the end of the hour. For another window, delete
the old Job and apply again with `--with-tftp`. Do not delete the Job or
interrupt power during an active flash.

## 7. Acceptance and closing provisioning

```bash
kubectl --context home-k3s -n telephony get pods -o wide
kubectl --context home-k3s -n telephony logs job/cisco-tftp
kubectl --context home-k3s -n telephony exec deploy/asterisk -- asterisk -rx 'pjsip show endpoints'
kubectl --context home-k3s -n telephony exec deploy/asterisk -- asterisk -rx 'pjsip show contacts'
```

`tftpd-hpa` sends transfer logs to syslog. The minimal image has no syslog daemon,
so `kubectl logs job/cisco-tftp` is not a complete RRQ/transfer log. For a short
network check, use `tcpdump` on the selected node's LAN interface with a filter
for UDP69/20000–20031, without `-A`/`-X` or full payload capture, then stop the
capture. Also watch the phone's display.

Check one 7945G: correct SIP load, DHCP address, clock and registration. Call
`600` (echo), `601` (test tone), then call between two phones in both directions.
Check off-hook/on-hook behavior, speakerphone, DTMF, hold/resume and ring group
`620`; keep a call active for several minutes. Add the remaining phones only
after these checks. Do not publish MAC addresses, passwords or SIP packets from
logs.

```bash
# Only after all current flashes/transfers have completed:
kubectl --context home-k3s -n telephony delete job cisco-tftp --ignore-not-found
# Then remove temporary UDP69/20000–20031 allows from the existing firewall.
```

A 7945G may request TFTP configuration again at its next reboot. Closing TFTP
prevents updates; it does not guarantee autonomous boot with every firmware
version. Test a cold boot of one phone after closing the window. If the selected
load needs TFTP at boot, open a restricted reboot window. Permanent TFTP requires
a separate deliberate decision and LAN protection.

## Sources

- [Asterisk downloads / LTS](https://www.asterisk.org/downloads/).
- [Asterisk 22.11.0 source archive](https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-22.11.0.tar.gz).
- [Asterisk release support](https://docs.asterisk.org/About-the-Project/Asterisk-Versions/).
- [Kubernetes hostNetwork and NetworkPolicy](https://kubernetes.io/docs/concepts/services-networking/network-policies/#networkpolicy-and-hostnetwork-pods).
- [Kubernetes Jobs: deadline, TTL](https://kubernetes.io/docs/concepts/workloads/controllers/job/).
- [tftpd-hpa: chroot, port-range, blocksize](https://manpages.debian.org/bookworm/tftpd-hpa/in.tftpd.8.en.html).
- [k3s networking](https://docs.k3s.io/networking/basic-network-options).
