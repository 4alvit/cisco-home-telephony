# Inventory and configuration generation

If phones are not yet verified, use the [explicit PBX-only bootstrap](pbx-bootstrap.md).
It prepares the server and AMI without an inventory. The requirements below
remain mandatory for normal phone provisioning.

## 1. Verify the phones

Check each phone's `CP-7945G` label, SW-port MAC, serial number, hardware revision,
current load, existing CTL/ITL and access to settings. Store serial numbers only
in private `.local/phones.json`.

Copy `examples/phones.example.json`, which has ten entries for extensions 201–210.
Remove entries for missing phones. MACs may use `:` or `-`; the generator converts
them to 12 uppercase hexadecimal characters. Use ASCII labels of up to 24
characters for initial setup. The top-level `phoneLabel` is automatically
shortened to 12 characters, with spaces replaced by `_`. Passwords are limited
to 30 characters by this generation's SEP format.

For two independent numbers on one physical 7945G, use
[`phones.two-lines.example.json`](../examples/phones.two-lines.example.json) and
the [second-line guide](second-line-and-wallpaper.md). Replace the legacy
`extension` field with an ordered `lines` list of one or two extension/label
pairs; do not specify both forms. Extensions must remain unique across the
whole inventory. Keep the existing private secrets when adding a line: the
generator reuses the first line's password and creates one for the new line.
Group 620 rings the first line of each physical phone only.

An optional per-phone `room` assigns one room name to both lines and supplies
their PJSIP caller-ID name without changing numeric handset labels. The
[room directory guide](phone-directory.md) covers the central inventory,
separate Mac entry, validation and full synthetic ten-phone example.

`verified:true` means the model and MAC were checked against the actual phone;
it does not verify firmware compatibility. `firmware_load` is the exact `.loads`
filename **without its extension**, for example `SIP45.9-4-2SR4-3S`, only if that
file exists in your verified bundle. Do not rename firmware to match the example.
An intermediate SCCP stage requires the separate procedure in the
[conversion guide](sip-conversion.md); this generator produces the final SIP profile.

Each phone may optionally set `"web_access": true` to keep its HTTP status and
diagnostic pages available during the pilot. Omit the field or set it to `false`
to disable the phone's web server. Only JSON booleans are accepted; strings such
as `"true"`, numbers and `null` are rejected before any output is written.
This maps to `vendorConfig/webAccess`: **0 enables the web server; 1 disables it**,
as documented by [USECALLMANAGER](https://usecallmanager.nz/sepmac-cnf-xml.html#webAccess).
After changing this field, regenerate and deploy the profile, then restart the
phone so it retrieves the updated configuration. Check its status pages at
`http://PHONE_IP/` from the management network. The option does not enable SSH
or change authentication settings.

Additional optional phone fields are documented in
[remote management](phone-control.md): `ssh_enabled` creates a separate SSH
credential, `authentication_url` points to the private callback, and
`phone_personalization` requires both that URL and web access.
`background_image_access` controls access to the wallpaper menu. The enable flags
require actual JSON booleans; `authentication_url` is a URL string. Setting a flag does not itself install a
wallpaper or start the callback: follow the corresponding deployment and
verification steps. Keep a configured callback URL in the inventory; omitting
it from a later generation omits its XML element.

## 2. Configure site.json

Use `deploy/site.example.json` as the starting point. `node_ipv4` is the PBX
address and must belong to its `lan_cidr`. `ntp_ipv4` must identify an existing
reachable NTP server; this project does not deploy NTP. The phone timezone is
Pacific Standard/Daylight Time. Verify its clock before provisioning.

Optional `phone_cidrs` defines the source allowlist for phone SIP endpoints.
Omitting it preserves the default `[lan_cidr]`. An explicit list **replaces**
that default. For a PBX at `192.168.50.10` in `192.168.50.0/24`, setting
`"phone_cidrs": ["192.168.60.25/32"]` allows only that phone, without also
allowing every host in the PBX subnet. Supply 1–50 non-overlapping private IPv4
CIDRs, including `/32` entries for individually verified phone addresses.

The generator also adds these networks to both SIP transports' `local_net`
for NAT classification. This does not open a firewall or authorize all hosts
in `lan_cidr`. Review host firewall rules separately: phone sources need
TCP/UDP5060 and UDP10000–10199 on the PBX. Existing routes must support traffic
in both directions. Open TFTP UDP69 and UDP20000–20031 only for the intended
phones during provisioning. The generator changes no firewall, routes or DHCP.
Regenerate configuration after editing `phone_cidrs`; preflight rejects stale
ACLs. Do not widen `lan_cidr` just to admit a single phone.

Each phone's `transport` defaults to `tcp`; `udp` must be selected explicitly.
XML sets `transportLayerProtocol=1` or `2` respectively, and PJSIP uses the
matching transport. This project does not use value `4` from older online
examples. The phone receives the PBX address through `USECALLMANAGER` and
`processNodeName`. SEP schema reference:
[USECALLMANAGER documentation](https://usecallmanager.nz/sepmac-cnf-xml.html).

## 3. Generate locally

```bash
umask 077
python3 scripts/provision.py --site .local/site.json --inventory .local/phones.json
```

Output:

- `.local/secrets.json`: a separate random 120-bit password per extension.
- `.local/asterisk/*.conf`: PJSIP endpoint/auth/AOR, dialplan, RTP and runtime paths.
- `.local/tftp/SEP<MAC>.cnf.xml`: one profile per phone.
- `.local/tftp/dialplan.xml`: extensions 2xx, 600, 601 and 620.

With the corresponding features enabled, output also includes the separate
`phone-management-secrets.json` for SSH and `ha-ami-secret.json` for Home
Assistant; Assist adds route 630. Include these private credential files in
the backup. The controller's `control.json` is created separately by
`phone_control.py init` and is not a SIP or SSH credential file.

Directories use mode 0700 and files 0600. Passwords are not printed. When a phone
is removed from the inventory, the generator removes its old generated SEP
profile from `.local/tftp`; other files are left intact. Removed extensions'
secrets remain in the local store for rollback; delete them according to your
retention policy. After changing profiles, redeploy and provision the affected
phones. Running the generator alone does not change the live PBX.

## 4. Prepare firmware

Extract an authorized Cisco ZIP for **7945/7965 SIP** into a separate directory.
Verify its signature/checksum against a trusted source and read the release notes.
Keep every required `.loads`, `.sbn` and other component; a `.loads` file alone is
insufficient. Copy only the complete verified bundle into `.local/firmware/`,
without configuration files or README files.

```bash
shasum -a 256 .local/firmware/*
```

Add **every filename** and SHA-256 to `files` in the local manifest, following
`examples/firmware-manifest.example.json`. Record provenance and `load_id`, and set
`reviewed:true` only after manual verification.

```bash
python3 scripts/verify_firmware.py --directory .local/firmware --manifest .local/firmware-manifest.json
```

The check rejects missing/extra files, incorrect SHA-256 values, symlinks and
paths outside the directory. It **does not establish** Cisco provenance,
completeness against release notes, hardware compatibility or usage rights.
A locally computed SHA-256 records the current bytes; comparing those bytes to
the same hash does not authenticate them.

## 5. Minimal Asterisk setup

Each extension has one endpoint and AOR, with one active device per AOR. A new
REGISTER replaces the old contact. Profiles require passwords; there is no
anonymous endpoint or trunk. The source ACL is restricted to `phone_cidrs`
(default: `lan_cidr`). All media passes through the PBX (`direct_media=no`), with
RTP bound to its LAN IP (`media_address`, `bind_rtp_to_media_address`). This avoids
selecting a WireGuard/overlay address on a multi-interface node.

The baseline G.711 ulaw/alaw codecs need no transcoding for a typical internal
call. Add G.722, BLF, shared lines, transfer and voicemail only after basic calling
is verified; this delivery does not claim those features have been tested.

PJSIP reference: [official Asterisk examples](https://docs.asterisk.org/Configuration/Channel-Drivers/SIP/Configuring-res_pjsip/res_pjsip-Configuration-Examples/).
