# Cisco 7945G: home telephony with Asterisk and k3s

A complete guide for a batch of **10 × Cisco 7945G** phones bought on eBay.
The model was confirmed in the delivery email dated **December 1, 2022**.
See [purchase evidence](docs/purchase-evidence.md).

**These phones already support IEEE 802.3af PoE.** No hardware modification is
needed. Use a PoE switch or a compatible active injector, connected to the
**SW** port. Asterisk uses the native **7945/7965 SIP firmware (SIP45)**;
conversion to MPP is not required.

![Home telephony architecture](docs/diagrams/architecture.svg)

## What is included

- Per-phone `SEP<MAC>.cnf.xml`, dialplan and Asterisk PJSIP configuration generator.
- A ten-phone inventory template with required model and MAC verification.
- Separate Asterisk and temporary TFTP containers, with declarative k3s deployment.
- Dry-run defaults, explicit Kubernetes context/node selection, and image digests.
- Local firmware integrity checks, acceptance procedures, backup and rollback.
- [SVG diagrams](docs/diagrams/) and [color slides](docs/slides/), all in English.
- [Home Assistant](docs/home-assistant.md): phone states, DTMF, call control and
  Assist on extension **630**.
- [Remote phone management](docs/phone-control.md): authenticated screenshots
  and keypad control, separate SSH credentials, and renewable access windows.

**The server is deployed in the home k3s cluster:** Asterisk 22.11.0, Home
Assistant AMI and VoIP integrations, and a telephony dashboard are installed.
See [live deployment validation](docs/server-validation.md).
The first physical 7945G now runs **SIP45.9-4-2SR4-3S**, registers to Asterisk
over TCP as **201** and **202** on its two line buttons, and appears registered
in Home Assistant. Handset echo on **600**,
Russian Assist on **630**, and HA-initiated calling and hangup passed live tests.
Speakerphone, DTMF and two-phone acceptance remain. Cisco firmware is not
included: obtain an authorized copy and verify the upgrade path.
The [second-line and wallpaper guide](docs/second-line-and-wallpaper.md) covers
the independent 202 line and original generated background now installed on the
pilot. Authenticated screenshots, remote keys and SSH diagnostics were verified;
see [remote management](docs/phone-control.md) for legacy client compatibility.
The [firmware archive guide](docs/firmware-archive.md) records a working preserved
SR4-3 copy, byte-level checks, extraction steps and provenance limitations.

## Workflow

1. [Check power and PoE](docs/poe.md).
2. [Inventory the phones and select the SCCP → SIP upgrade path](docs/sip-conversion.md).
3. [Generate private configuration](docs/provisioning.md).
4. [Build images and deploy Asterisk/TFTP to k3s](docs/k3s-deployment.md).
5. [Test calls, close TFTP and save a backup](docs/operations.md).

Before physical inventory is complete, [bootstrap the PBX and AMI](docs/pbx-bootstrap.md)
with `provision.py --pbx-only`. This does not create placeholder phones, Cisco XML
or TFTP configuration.

**Before any reset or flash:** 7945G hardware revision **13.0 and later must not
run firmware below 9.3(1)SR3**. The common instruction to install 8.5(2) on every
phone is unsafe for these revisions. Check each phone's hardware and current load.

## Local preparation

```bash
git clone https://github.com/4alvit/cisco-home-telephony.git
cd cisco-home-telephony
umask 077
mkdir -p .local/firmware
cp deploy/site.example.json .local/site.json
cp examples/phones.example.json .local/phones.json
cp examples/firmware-manifest.example.json .local/firmware-manifest.json
```

Fill in IP/NTP settings, verified MACs, hardware revisions and firmware load IDs.
The templates intentionally fail validation until completed. Set `verified` to
`true` only after checking the physical phone.

```bash
python3 scripts/provision.py --site .local/site.json --inventory .local/phones.json
python3 scripts/verify_firmware.py --directory .local/firmware --manifest .local/firmware-manifest.json
python3 -m unittest discover -s tests -v
# After building the images and adding their digests to site.json:
bash scripts/deploy.sh --with-tftp
```

The last command **does not contact the cluster**. Read the [full runbook](docs/k3s-deployment.md)
before applying changes: it covers firewall rules, DHCP option 150, firmware
staging on the node and acceptance checks.

## Extensions and networking

Example extensions: **201–210**. **600** is echo, **601** is a test tone, and
**620** rings the group. With HA integration enabled, **630** reaches Home
Assistant Assist. There are no outgoing PSTN calls, trunks, voicemail or emergency
calls. Use a working external phone service for emergency calls.

Default profile: **SIP TCP 5060**, G.711, and RTP through Asterisk. The server also
listens on UDP 5060 for explicitly configured UDP phones. Asterisk RTP uses UDP
**10000–10199**; phone RTP uses **16384–16482**. Temporary TFTP uses UDP
**69 + 20000–20031**, with a provisioning window of up to one hour. Use a trusted
LAN/VLAN without Internet exposure. SIP and TFTP are unencrypted in this setup.

Public examples use placeholders: `voice-node`, `192.168.50.10`, and context
`home-k3s`. Replace them in `.local/site.json`. Verify node identity and port
availability. Internal calls pass through the local PBX. Assist audio also
travels to HA; a remote HA node uses the existing trusted VPN for this leg.

## Private data

Git ignores `.local/`, which holds passwords, MACs and rendered Kubernetes
Secrets. Rerunning the generator preserves passwords in `.local/secrets.json`.
If this file is lost, restore a backup or reconfigure both phones and PBX with
matching credentials. Do not commit vendor firmware, kubeconfig files, call
captures or raw emails. Real network settings and personal data stay local.

[Verified technical sources](docs/cisco-research.md) explain Cisco limitations
and the scope of Asterisk compatibility.

## Free CI

[GitHub Actions](https://github.com/4alvit/cisco-home-telephony/actions) checks
Python, shell, Asterisk/TFTP builds and container startup on standard
`ubuntu-24.04` runners. Runner minutes are free for this public repository.
The workflow uses no paid larger runners, paid external services or increased
cache quotas. Normal push/PR runs build and test without uploading artifacts.
For deployment, manually run the workflow on `main` with `export_images: true`
(default: `false`). After successful smoke tests it exports verified AMD64 images,
limited to 400 MiB in total and one day of retention. Delete that deployment
artifact after verified download and import. Artifact storage has a separate
quota; free runner minutes do not include unlimited storage.
See [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

## Home Assistant

The [integration guide](docs/home-assistant.md) covers a dedicated AMI account,
a single-IP ACL and an internal Assist route. HA packages, dashboard, pinned
installation of the existing integration and diagrams live in the public
[ha-homelab/asterisk-telephony](https://github.com/ha-homelab/asterisk-telephony)
repository. It is managed through the existing
[organization Terraform](https://github.com/4alvit/terraform-github-ha-homelab/blob/main/asterisk-telephony.tf).

For Assist, the image includes an Opus transcoder built from source: Cisco uses
G.711 while native Home Assistant VoIP uses Opus. AMI is disabled by default;
`deploy/site.ha.example.json` shows how to enable it explicitly.
