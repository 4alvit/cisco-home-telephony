# A second line and a personal wallpaper on the 7945G

The CP-7945G has two programmable line buttons. This setup can assign **201**
and **202** to one physical phone, with separate SIP credentials. It remains
one handset; it does not complete acceptance between two physical phones.

On the pilot, both lines registered independently over TCP. Outgoing and incoming
202 calls reached Asterisk's echo application with bidirectional G.711 RTP.
The generated Lake dawn image was selected and saved through authenticated remote
keys, then checked with a fresh screenshot. These signaling/media observations
do not add a new human confirmation of speakerphone audio quality.

## Configure two independent lines

Start from [the synthetic two-line inventory](../examples/phones.two-lines.example.json).
Copy it into a private directory and replace its unverified hardware data.
A phone uses either the legacy `extension` field or a `lines` list, never both:

```json
"lines": [
  {"extension": "201", "label": "Home 201"},
  {"extension": "202", "label": "Home 202"}
],
"background_image_access": true
```

The array order selects buttons 1 and 2. MAC, model, firmware, transport and
phone-level label still belong to the physical phone. Extensions must be
unique across every line and phone. Existing `secrets.json` entries are retained;
back up the current private directory before regenerating. Do not start in an
empty directory if you intend to preserve the working 201 and AMI passwords.

The generator writes one `SEP…cnf.xml` per MAC, independent PJSIP endpoints for
the two lines, and direct routes to both numbers. Group **620** rings only each
physical phone's first line, so this handset receives one group-call leg.
It deliberately omits `lineIndex`: Cisco's bulk-registration behavior differs
from independently authenticated lines. See the original implementer discussion
in [ASTERISK-13145](https://issues-archive.asterisk.org/ASTERISK-13145).

After delivery, require **actual contacts for both 201 and 202** in
`pjsip show contacts`. Select each button before dialing **600** to confirm its
outbound identity and echo. Verify incoming calls to each number separately.
A visible label or generated endpoint alone does not prove registration.

## Stage the included wallpaper

The prepared [Lake dawn artwork](../assets/wallpapers/README.md) contains a full
320 × 212 RGB PNG and an 80 × 53 thumbnail. Copy only its three deployment files
into `wallpaper/` alongside the private `site.json`:

```bash
umask 077
# Example: an existing private deployment is kept in .local/pbx-bootstrap.
mkdir .local/pbx-bootstrap/wallpaper
cp assets/wallpapers/cisco-7945g/{List.xml,home.png,home-thumb.png} \
  .local/pbx-bootstrap/wallpaper/
python3 scripts/provision.py --site .local/pbx-bootstrap/site.json \
  --inventory .local/pbx-bootstrap/phones.json --out .local/pbx-bootstrap
bash scripts/deploy.sh --site .local/pbx-bootstrap/site.json --with-tftp
```

The last command performs a local dry run. Review its output, then follow the
[existing k3s deployment procedure](k3s-deployment.md) for server dry-run and apply.
The optional directory must contain exactly those three regular files. The
renderer validates PNG encoding, dimensions and catalogue paths, and stages a
separate immutable Secret. Vendor firmware remains on its existing read-only
mount; wallpaper is not treated as firmware. Without `wallpaper/`, deployment
behavior is unchanged.

Only the init container mounts the wallpaper Secret. It copies the three files
into the temporary served directory; the TFTP container sees that directory
read-only. Updating the source files or Secret does not update an already
running Job's copied files. After confirming that no transfer or flash is in
progress, close the old window and create a fresh TFTP Job through the deployment
procedure. Do not edit the firmware directory to install artwork or attempt to
patch an active Job's immutable Pod template.

Use a controlled TFTP window with the existing phone-only firewall permission.
Cancel any cleanup timer belonging to an older window before opening a new one.
Keep the same working SIP load; adding a line or wallpaper does not require a
firmware downgrade or factory reset. Restart the phone normally to load the
updated line configuration while TFTP is available.

## Select the picture on the handset

The same menu can be operated remotely using the authenticated screenshots and
single-key commands in [remote phone management](phone-control.md). First load
the callback URL through the controlled provisioning process. Inspect the
actual screen after each step; do not send a blind sequence of menu indexes.

Open **Settings → User Preferences → Background Images**, select the new
thumbnail, then **Select → Save**. Use **Preview** first if offered. The
`background_image_access` flag enables the menu; publishing the files does not
choose the picture automatically. No unsupported automatic-selection tag or
idle XML service is substituted for a wallpaper.

The phone requests:

```text
Desktops/320x212x16/List.xml
Desktops/320x212x16/home-thumb.png
Desktops/320x212x16/home.png
```

Confirm that the chosen picture is visible and saved. Then stop the TFTP Job
and close the temporary firewall window, using the same cleanup procedure as
provisioning. Keep the files for a future controlled recovery window.

## Home Assistant

Discover the new endpoint and obtain its real entity IDs. Keep `call_from: "201"`
and `call_to: null` for this single-handset setup. Add the 202 endpoint's sensors,
guarded Hangup and Recorder exclusions while preserving the existing 201
controls. A second native VoIP identity is discovered only if line 202 calls
630; enable its calls and select a pipeline separately when needed.

## Sources

- [Cisco CP-7945G data sheet](https://www.cisco.com/c/ja_jp/products/collateral/collaboration-endpoints/unified-ip-phone-7945g/product_data_sheet0900aecd8069bb80.html)
- [Cisco user guide, printed page 53](https://www.cisco.com/en/US/docs/voice_ip_comm/cuipph/7965g_7945g/6_1/english/user/guide/65enu61.pdf)
- [Model-specific image sizes and List.xml](https://usecallmanager.nz/background-images.html)
- [Cisco customization guide](https://www.cisco.com/c/ja_jp/td/docs/vauc/iptelep/ipphone7900/ag/029/P747_BK_W06BD6D9_00_adminguide_7945-7965-7970-7971-7975/P747_BK_W06BD6D9_00_adminguide_7945-7965-7970-7971-7975_chapter_0110.html)
