# Bootstrap the PBX before connecting phones

If physical MAC addresses and firmware have not yet been verified, prepare the
server without a placeholder inventory. Explicit `--pbx-only` mode generates only
Asterisk configuration and, when `home_assistant.enabled: true`, AMI configuration
with its own local password. It creates no phone endpoint/auth entries, Cisco XML,
phone `secrets.json` or TFTP directory. Phones need NTP, so `ntp_ipv4` is not
required in this mode.

Use a separate private directory, such as `.local/pbx-bootstrap`. The generator
refuses to run over `tftp` or `secrets.json` left by phone provisioning. It neither
deletes them nor presents old profiles as newly generated configuration.
Rerunning in the same bootstrap directory preserves the AMI password.

1. Create `site.json` with actual cluster and network parameters using the
   [deployment guide](k3s-deployment.md). `asterisk_image` must contain a verified
   image digest for the selected node's architecture. PBX startup does not
   require a TFTP image or Cisco firmware.
2. For HA integration, add `home_assistant` using the [AMI guide](home-assistant.md).
   `ami_client_ipv4` is the source observed by the PBX, not an assumed Pod CIDR
   address. Start with `allow_originate: false` and `assist: {"enabled": false}`;
   enable call control and Assist after checking their prerequisites separately.
3. Generate and validate the configuration:

```bash
umask 077
python3 scripts/provision.py --pbx-only \
  --site .local/pbx-bootstrap/site.json --out .local/pbx-bootstrap
python3 scripts/preflight.py --site .local/pbx-bootstrap/site.json --cluster
./scripts/deploy.sh --site .local/pbx-bootstrap/site.json
# Review private rendered/core.yaml, the firewall and selected context/node.
# Apply only after completing that review:
./scripts/deploy.sh --site .local/pbx-bootstrap/site.json --apply --firewall-reviewed
```

Without `--apply`, deployment only prepares files. **Do not add `--with-tftp`:**
bootstrap has neither phone profiles nor verified firmware. Preserve the existing
host firewall and allow AMI TCP5038 only from HA. Do not expose AMI or SIP/RTP to
the Internet.

Bootstrap provides **600 — Echo** and **601 — Milliwatt**. There is no group 620
or phone extension 2xx. Explicitly enabling Assist adds only route 630 to the
verified HA VoIP listener. A controlled local/AMI call can test these applications
without a registered phone, but cannot prove audio on a physical Cisco. After
startup, verify SIP transports and AMI login; phone entities are not expected
until phones are configured.

With Assist enabled, upstream also discovers service endpoint `ha-assist`.
Its derived `Registered` may be off and its `State` may be `Unavailable`: this
endpoint is dialed through an explicit SIP URI and does not register with HA.
It is not a physical Cisco or an Assist health check. The server's `AMI Connected`
entity and the [AMI probe](https://github.com/ha-homelab/asterisk-telephony/blob/main/docs/ami-verification.md)
check PBX management; audio requires a separate call.

## Add verified phones

After physical verification, create a real `phones.json`, add NTP and run the
normal generator in the **same directory** to preserve the AMI password.

If a phone is outside the PBX `lan_cidr`, add its `/32` to `phone_cidrs` and
review a matching narrow host firewall rule as described in the
[provisioning guide](provisioning.md). You do not need to widen the source ACL
to a common supernet. Preserve the separately verified AMI client address.

```bash
python3 scripts/provision.py --site .local/pbx-bootstrap/site.json \
  --inventory .local/pbx-bootstrap/phones.json --out .local/pbx-bootstrap
```

Normal mode still requires 1–50 physically verified CP-7945G phones. It creates
phone authentication/XML and group 620; echo and the test tone keep extensions
600 and 601. Then validate firmware, run preflight and deploy using the main
runbook. Do not commit `.local`, passwords, MACs or private rendered manifests.
