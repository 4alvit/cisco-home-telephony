# Cisco 7945G → SIP → Asterisk

The purchased batch contains ten 7945G phones, but start with **one**. This is
Enterprise SIP, not MPP. Firmware binaries are not included: verify archive
usage rights and hardware compatibility before writing flash.

## 1. Record the inventory

For each phone, record its label model (`CP-7945G`), MAC, hardware revision/VID,
App Load, Boot Load, any `Must use … or above` label, and current IP/VLAN/TFTP
settings. Check the handset, display, keys and Ethernet. Save photographs and
available old configuration outside Git.

The MAC is on the label or under `Settings → Network Configuration`; versions
are in Model Information. If web access is enabled, Device Information provides
read-only status. See [Cisco Remote Monitoring](https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cuipph/7975G_7971g-ge_7970g_7965g_7945g/9_0/english/admin_guide/P747_BK_W06BD6D9_00_adminguide_7945-7965-7970-7971-7975/P747_BK_W06BD6D9_00_adminguide_7945-7965-7970-7971-7975_chapter_01000.html).

Do not set `verified: true` based only on the eBay listing. Check each label,
then assign extensions 201–210 and room labels.

## 2. Select the firmware path

The latest package found in the reviewed official material is **7945_7965 SIP
9.4(2)SR4-3**. See the [September 2025 Cisco device pack](https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cucm/device_packs/1401-devpack-for-sep-2025.html).
The expected load file is `SIP45.9-4-2SR4-3S.loads`; verify the exact name against
the archive actually obtained. Linux distinguishes `SIP45` from `sip45`.

Choose the path from the phone's actual state:

1. If the required SIP version is already installed, keep it and proceed to provisioning.
2. **Hardware 13.0+ must not be downgraded to 8.x.** The Hardware Updates section
   of the [Cisco 9.3(1)SR4 release notes](https://www.cisco.com/en/US/docs/voice_ip_comm/cuipph/7900_series/firmware/9_3_1SR4/english/release_notes/P790_BK_RF3660D6_00_rn-9_3_1sr4-7900-series_chapter_00.pdf)
   sets the minimum to **9.3(1)SR3**. Also check the label and target release notes.
3. An older hardware revision with App Load **below 8.3(3)** needs an intermediate
   **8.3(3)–8.5(2)** before 9.3(1)+, according to the same document. A documented
   path for old SCCP is **SCCP 8.5(2)** followed by a compatible SIP release.
   See [Cisco CCA 3.0(1), Resolving Cisco Model 79xx Phone Upgrade Issues](https://www.cisco.com/c/en/us/td/docs/net_mgmt/cisco_configuration_assistant/version3_0_1/release/Notes/cca_3_0_1_relnotes.html).
4. For older hardware already running 8.3(3)+, check the target package requirements;
   do not automatically add an intermediate downgrade.
5. If the version is unknown or labels disagree, resolve the discrepancy first.
   Do not guess a load ID or bypass `Load Rejected HC`.

A hardware minimum is not a recommendation to downgrade to it. Intermediate
firmware is needed only for genuinely old loads. If the required authorized
archive is unavailable, that upgrade path is not ready to run.

## 3. Obtain the complete package

An inspected third-party copy of the target release is documented in the
[firmware archive guide](firmware-archive.md), including a working download,
complete component hashes, safe extraction and explicit provenance limits.

Use Cisco Software Download or an existing authorized archive with known
provenance. Check the model, SIP/SCCP protocol and published checksum. Record
source, date and SHA-256 privately. Your own SHA-256 identifies the file's bytes
but does not prove their origin.

```bash
unzip -l /absolute/path/to/cisco-firmware.zip
shasum -a 256 /absolute/path/to/cisco-firmware.zip
```

Cisco explicitly documents ZIP distribution for TFTP installation without CUCM.
Do not replace signed components with homemade images.
See the [official README](https://www.cisco.com/web/software/282074288/128566/cmterm-7945_7965-sip.9-4-2SR2-2-readme.html).

Keep each release separately. The active TFTP directory must contain one complete
bundle, including `term45.default.loads` if supplied. `term75.default.loads` is
for the 7975, not the 7945. Do not mix components from different releases. Follow
the firmware import procedure in the main README; never commit `.local/tftp`
or files containing passwords.

## 4. Prepare infrastructure before restarting

Deploy Asterisk/TFTP using the [k3s guide](../deploy/README.md). You need a fixed
node LAN IPv4 address, DHCP, reachable NTP and routing between phone and PBX.
Do not place NAT between them for the pilot.

Set DHCP option 150 in the voice scope to the **TFTP node's IPv4 address**, not a
URL, ClusterIP or Pod IP. Alternatively, set `Alternate TFTP = Yes` and
`TFTP Server 1 = <node LAN IPv4>` on the phone. Unlock protected settings by
opening Settings and entering `**#`, then save changes. Factory recovery needs
DHCP option 150/66 because a full reset removes manual settings.

Allow UDP 69 and the server's TFTP transfer range, TCP 5060 for SIP, and UDP
10000–10199 on Asterisk. UDP 16384–16482 is the generator's **phone-side** RTP
range, at the other end of the connection. Account for both directions across
VLANs. Do not start a second DHCP server on the shared home network.

## 5. Generate a SIP profile

Set verified IP/CIDR values and an existing NTP server in `.local/site.json`.
For the pilot, keep one entry in `.local/phones.json`: actual MAC, extension,
`model: "CP-7945G"`, `verified: true`, `transport: "tcp"`, and the exact
`firmware_load` without `.loads`.

```bash
python3 scripts/provision.py \
  --site .local/site.json \
  --inventory .local/phones.json \
  --out .local
```

The generator creates a per-phone `SEP<MAC>.cnf.xml`, `dialplan.xml`, Asterisk
configuration and local secrets. Rerunning preserves passwords. Password changes
must reach both the PBX and phone together.

Key excerpts from generated XML:

```xml
<deviceProtocol>SIP</deviceProtocol>
<!-- Inside callManager: -->
<processNodeName>LAN_IP_PBX</processNodeName>
<!-- Inside sipLines/line: -->
<proxy>USECALLMANAGER</proxy>
<!-- At device level: -->
<loadInformation>SIP45.9-4-2SR4-3S</loadInformation>
<deviceSecurityMode>1</deviceSecurityMode>
<transportLayerProtocol>1</transportLayerProtocol>
```

These excerpts are explanatory, not a complete profile. `1` selects TCP; omit
`.loads` from `loadInformation`. `USECALLMANAGER` uses `callManagerGroup`'s address.
The phone timezone is `Pacific Standard/Daylight Time`; the server may use IANA
`America/Los_Angeles`. NTP uses `unicast`. XML follows the
[USECALLMANAGER author's documentation](https://usecallmanager.nz/sepmac-cnf-xml.html),
not a Cisco guarantee of Asterisk compatibility.

After boot, verify the clock and daylight saving behavior. G.711 is the baseline
codec; TLS/SRTP are not configured. Use network isolation and ACLs initially.

## 6. Pilot without a factory reset

1. Check Asterisk/TFTP, the complete bundle, correct MAC in the SEP filename and
   sufficient power.
2. Connect one phone, configure TFTP and save.
3. Perform a normal restart with `Settings → **#**`. Reconnecting power once is
   acceptable only if a firmware update has not started.
4. Observe TFTP requests for the individual `SEP…cnf.xml`, then the load file and
   components if firmware changes. Do not automatically fabricate files from
   the previous CUCM environment.
5. Wait for a complete boot without interrupting power during flash writes.
   Several automatic restarts may occur.
6. Verify App Load, extension and time, then registration and calling.

### Old CUCM trust blocks the new configuration

If the phone repeatedly requests only `CTLSEP…`, `ITLSEP…`, `ITLFile.tlv`,
`SEP<MAC>.cnf.xml.sgn` or `XMLDefault.cnf.xml.sgn`, without requesting plain
`SEP<MAC>.cnf.xml` or firmware, inspect its old CUCM trust list. Those requests
alone do not prove a trust fault. Check the actual TFTP Server 1 address and a
short private capture first. An RRQ followed by a TFTP error response confirms
that exchange reached both ends; it does not establish that every file or
large transfer can pass. Missing requests or unanswered RRQs instead require
checking DHCP/VLAN, routing, the Job and both TFTP UDP ranges.

With the correct TFTP target and complete firmware/profile set ready, inspect
the phone's **Settings → Security Configuration → Trust List → ITL File**.
For a stale ITL from the previous CUCM installation:

1. If settings are locked, enter `**#` once to unlock them.
2. Select **Unlock** for the ITL file and confirm its lock icon opens.
3. Select **more → Erase**. The phone may restart and fetch configuration again.
4. Inspect **CTL File** separately if present. ITL and CTL are distinct lists;
   do not assume erasing one clears the other or impose a universal deletion
   order. Resolve any remaining stale list using the procedure for that phone
   and load, then recheck requests.

The ITL steps are illustrated in a
[Cisco employee's 79xx guide](https://community.cisco.com/t5/documentos-colaboraci%C3%B3n-voz-y-video/c%C3%B3mo-eliminar-el-archivo-itl-de-los-tel%C3%A9fonos-ip/ta-p/3134134).
This targeted deletion does **not** require a factory reset. Save the existing
settings first and keep power and TFTP available through any resulting update.
If the menu remains locked or deletion fails, record that result rather than
repeatedly resetting the phone.

Never rename unsigned XML to `.sgn`, fabricate a CTL/ITL file, or bypass firmware
signature verification. Removing obsolete configuration trust is separate from
validating Cisco's signed firmware. Afterward, verify that the phone requests
the intended plain XML, load and components, and confirm the resulting App Load.

### Separate intermediate SCCP stage

If step 2 established that intermediate firmware is required, use a separate
pilot TFTP directory. Point the saved SCCP profile at the intermediate bundle's
**exact `SCCP45.…` load ID** and verify App Load after restart. The main generator
intentionally accepts only SIP45; do not weaken validation or rename SCCP to SIP.

If no old profile is available, prepare the standard recovery bundle with
`term45.default.loads` and its components on an isolated test network, then use
the factory recovery procedure below. This is not an automatic recommendation
to erase the phone. Once the intermediate version is verified, switch the pilot
to the prepared SIP directory and restart. Do not expose intermediate firmware
to the entire batch.

## 7. Recovery only when necessary

A normal restart preserves configuration. Factory reset removes settings,
certificates, history and the application; recovery depends on TFTP. First rule
out an incorrect MAC, VLAN, IP, firewall, incomplete package or incompatible
hardware. Follow the targeted old-CUCM trust checks above before considering a
full reset.

Before a factory reset, verify DHCP, option 150/66, `term45.default.loads` and all
its components. Power on while holding `#` until line keys flash amber in sequence,
release it and enter `123456789*0#`. Wait for the main screen without interrupting
power. See [Cisco Administration Guide, Basic Reset / Factory Reset, printed
pages 248–249, preserved copy](https://www.vikaskumar.org/files/voip/manuals/P747_BK_W06BD6D9_00_adminguide_7945-7965-7970-7971-7975.pdf#page=268)
and the [official Japanese guide](https://www.cisco.com/c/ja_jp/td/docs/vauc/iptelep/ipphone7900/ag/029/P747_BK_W06BD6D9_00_adminguide_7945-7965-7970-7971-7975.pdf).

For `Auth Fail`, check the version path and integrity. `Load Rejected HC` indicates
a hardware minimum. Repeated resets do not fix the cause. Do not use unknown
deep-reset sequences or bypass signature checks.

## 8. Acceptance and rollout to ten phones

For each phone, verify:

1. `pjsip show contacts` shows the expected extension and LAN address.
2. Start a new call and dial `600` explicitly; it returns your speech through
   echo. If it only rings, confirm the actual dialed extension and Asterisk's
   `Answer()`/`Echo()` execution using the [troubleshooting guide](operations.md).
3. `601` plays the test tone; use moderate volume.
4. A call between two phones has two-way speech, DTMF, hold/resume, transfer,
   speakerphone and handset operation.
5. `620` rings the group; answering one phone stops the others ringing.
6. Restarting the phone preserves its extension; registration recovers after PBX restart.
7. Time is correct, no secrets are in Git, and unintended networks have no access.

After a successful pilot, add phones in small groups and record the final App
Load. Ten configuration entries do not equal ten flashed phones. External numbers
and emergency calling are not connected in the baseline configuration.

After transfers and flash writes finish, delete the TFTP Job and close its
temporary firewall allowances using the [operations guide](operations.md).
The Job deadline and cleanup TTL do not remove host firewall rules.
