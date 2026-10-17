# Home Assistant and Cisco 7945G

The full HA toolkit is in [ha-homelab/asterisk-telephony](https://github.com/ha-homelab/asterisk-telephony):
installation, package/dashboard generator, automations, SVG diagrams and acceptance.
This document covers Asterisk/k3s. The [version and API research](home-assistant-research.md)
records limitations of the existing integration.

## Features and scope

This project uses the existing **TECH7Fox Asterisk integration** without copying
its source into our repository. It exposes PJSIP devices, line state, connected
line and DTMF, and provides `asterisk.send_action`. The HA package supports calls
between internal phones, Assist calls and hanging up a specific channel. The
DTMF example creates a notification; it does not unlock doors or control heating.

Upstream has one global service, so this setup supports **one PBX per HA instance**.
The registered sensor reflects DeviceState, not independent proof of SIP
registration; also inspect `pjsip show contacts`. DTMF arrives through sensor
state/attributes, not a dedicated HA event-bus event. Available configuration
does not establish two-way audio on a physical phone.

## 1. Enable AMI in local configuration

Add these fields to `.local/site.json`, replacing the example addresses:

```json
"home_assistant": {
  "enabled": true,
  "ami_client_ipv4": "192.168.50.30",
  "allow_originate": true,
  "assist": {
    "enabled": true,
    "host_ipv4": "192.168.50.30",
    "sip_port": 5060
  }
}
```

Complete example: [`site.ha.example.json`](../deploy/site.ha.example.json).
`ami_client_ipv4` is **the source address observed by the PBX after routing/NAT**,
not necessarily the Pod IP or HA web-interface address. `host_ipv4` is the reachable
address of HA's VoIP listener. Use an existing trusted VPN for remote HA. Management
and Assist then use that VPN; ordinary phone-to-phone calls stay in the LAN.
Do not expose SIP, AMI or TFTP on the WAN.

```bash
python3 scripts/provision.py --site .local/site.json --inventory .local/phones.json
bash scripts/deploy.sh
```

The generator creates `.local/ha-ami-secret.json` with username `homeassistant`
and a separate random password. This Git-ignored file uses mode0600 and is reused
on subsequent runs. It is not included in Cisco XML. Read it privately when
configuring HA. `.local/secrets.json` continues to hold only phone passwords.
Back up both files.

AMI listens on **the selected PBX node's IP:5038/TCP**, permits exactly one client
/32 and disables the HTTP manager. Permissions are `read=call,dtmf` and
`write=system,originate`. `allow_originate:false` removes only `originate`.
**This is not a read-only account:** the `system` class needed for PJSIP discovery
also permits Hangup and other administrative AMI actions. Protect HA and this
account as PBX management access. `command`, `config` and `all` are not granted;
AMI CLI access is denied.

The integration uses plain AMI TCP without TLS. Restrict the firewall to the exact
source IP, as in the [template](../deploy/firewall.nft.example); an ACL does not
encrypt the connection.

## 2. Prepare Assist and networking

Add **Voice over IP** in HA, check its SIP UDP port and configure an Assist
pipeline with working STT, conversation agent and TTS. Cloud LLM/STT is optional;
local services can be used. Account for their resources and recognition language.

HA must be reachable on **UDP5060** or the selected port. For HA in k3s, verify
actual Pod networking and routability of the address advertised in SDP. An HTTP
Ingress does not carry SIP/RTP. Typical options are hostNetwork or a separate
routable network; do not automatically change an existing HA installation's network.

HA VoIP chooses a dynamic UDP RTP port and the adjacent RTCP port; the reviewed
version has no configurable fixed RTP range. Check the HA host kernel's ephemeral
range and allow it **only from the PBX IP**. On the PBX, allow HA UDP5060 and
RTP10000–10199. SIP OPTIONS qualify is not used here because HA VoIP does not
answer it like a conventional SIP PBX. No incoming routes from HA to phones are
configured; call control uses AMI.

## 3. Build and deploy the updated PBX

Follow the [main runbook](k3s-deployment.md): build both images, run smoke tests,
push to your registry and update immutable digests in site.json. An old image
without Opus cannot provide Assist transcoding.

```bash
bash scripts/build-images.sh YOUR_REGISTRY/cisco-home
bash scripts/smoke-containers.sh YOUR_REGISTRY/cisco-home
# After pushing, recording digests and reviewing firewall/maintenance timing:
bash scripts/deploy.sh --apply --firewall-reviewed
```

Opus is built from Wazo GPL-2.0 source pinned by commit and SHA256. The source
archive, LICENSE and README are included under
`/usr/share/doc/asterisk-opus-open-source/` in the image. This provides G.711↔Opus
transcoding, not just an SDP codec advertisement. With Assist enabled, readiness
requires the module to be running.

Generation adds **630** to the Cisco dialplan. Update TFTP profiles and let phones
load the new dialplan during a separate provisioning window. Changing an
extension does not require reinstalling firmware.

## 4. Connect the integration and test

Install the reviewed upstream version using the companion repository, restart HA
in an agreed maintenance window, then add Asterisk through the UI: PBX IP,
TCP5038, `homeassistant`, and the locally stored password. After device discovery,
enter **actual entity IDs** into the HA package/dashboard generator's local
inventory. Automatically generated IDs may differ from examples.

1. Check `pjsip show contacts`, HA device states and absence of AMI errors.
2. Make an internal call between two phones and verify state changes.
3. Place a call through the HA script; hang up only its actual Channel.
4. Test DTMF during a test call and verify the harmless HA notification.
5. Dial 630. After the first device discovery in HA, enable **Allow calls** and
   select a pipeline, then call again. SIP From preserves the phone's extension,
   so different phones should have distinct device identities.
6. Verify STT, TTS response, two-way audio and hangup. Repeat after PBX/HA restart
   and phone cold boot. Start with one phone.

## Rollback

Remove/disable the HA package, set `home_assistant.enabled:false`, regenerate and
perform the normal deployment. This disables AMI in Asterisk and removes route
630 from newly generated configuration. Remove HA firewall allowances and update
the phone dialplan in the next TFTP window. For full restoration, use the previous
immutable image/config Secret following the [rollback runbook](operations.md).
Do not delete existing HA `.storage`, HACS, secrets, device entries or databases.
