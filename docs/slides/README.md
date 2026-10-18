# Presentation: Cisco 7945G, Asterisk and Home Assistant

[Open PDF](cisco-home-telephony.pdf) · [Download editable PowerPoint](cisco-home-telephony.pptx)

![Overview of all 19 slides](preview.webp)

19 English slides cover Cisco 7945G model capabilities, verified inventory, home network
architecture, PoE, SCCP/SIP conversion, TFTP, Asterisk PJSIP, Home Assistant over
AMI, Assist on extension 630 with G.711 ↔ Opus, deployment, call acceptance,
backups and batch rollout. The final five slides cover twenty lines across ten configured Cisco identities,
the installed 290-control Home Assistant integration, alarms and radio,
free SIP account status, and Mac line 209 and children's radio 708–710.
PPTX labels and diagrams are editable objects.
Speaker notes link to primary sources. The PDF is suitable for reading and printing.

The presentation accompanies the [k3s runbook](../k3s-deployment.md),
[phone provisioning guide](../provisioning.md) and [Home Assistant guide](../home-assistant.md).
A separate [PBX bootstrap procedure](../pbx-bootstrap.md) uses `--pbx-only` to
prepare the server before verified phone profiles are available. Detailed HA
packages, dashboard, installation and acceptance procedures live in the
[companion repository](https://github.com/ha-homelab/asterisk-telephony).
Before flashing, check each phone's App Load and hardware revision. HW 13.0+
must not run firmware below 9.3(1)SR3. Follow the chosen release's upgrade notes.

The slides distinguish implemented features, verified local call results and
remaining live acceptance. The status snapshot reflects October 17, 2026.
Saved pilot evidence covers gateway call control, DND, opt-in auto-answer,
displayed text/images and radio 701 RTP. The revised timer/alarm contract keeps
ringing until manual dismissal, without auto-answer or after-answer audio.
The alert update is deployed. Two physical timer checks ended by manual answer.
A scheduled weekday alarm rang beyond 90 seconds, and the corrected HA Stop alert
route passed live acceptance. Earlier finite-tone tests do not validate the new behavior.
Ten Cisco identities are configured, with Mac 209 kept separate. Phones retain
their numbers and credentials when unplugged; online status depends on the
connected devices. The tenth phone completed its automatic SIP45.9-4-2SR4-3S
upgrade without a PoE cycle. Both lines registered; its numeric labels, saved
LakeDawn wallpaper, authenticated CGI and independently pinned SSH login passed.
Earlier added devices passed the same physical checks. The temporary TFTP job,
listener, selected-source window and timer were closed after completion.

The expanded Home Assistant installation passed its post-restart checks and
dashboard readback with 290 controls and 143 Recorder exclusions. The tenth-device expansion preserved
6,818 existing registry identities. Per-line call
and native Assist acceptance remains a separate result. Native Assist on primary
218 is still pending because that handset was unplugged before onboarding.
Each phone needs its first 630 call, Allow calls and its Russian Baya pipeline.

The dedicated Mac 209 account uses TCP/IPv4. **Zoiper5 is the current compact
client**, replacing the earlier Telephone and Linphone trials. Its registration
and an echo call to 600 passed, with the user confirming audible echo. Earlier
Linphone tests also reached Up/Echo with PCMU and increasing RTP in both
directions; those historical results do not describe the current app's interface.
Official Deti FM channels 708–710
are deployed and produced PCM through the installed worker. A listener confirmed
music on 708; this is not an audible-playback result for every station. To dial
these new numbers without refreshing a Cisco profile, leave the handset down
and speaker/headset off, enter all digits, then press Dial rather than a line key.
Account activation does not establish SIP registration or working external audio.
All three configured providers reached `Registered`. The latest bounded SIP2SIP
802 echo call answered and exchanged RTP in both directions: transmitted packets
increased from 260 to 510 and received packets from 214 to 416, while the local
receive-loss counter increased from 44 to 91.

The deployed Linphone 803 route uses the independently published **The Test
Call** peer. Its bounded live pilot also answered: transmitted packets increased
from 252 to 503 and received packets from 222 to 429, with locally reported
receive loss staying at zero in both samples. Both tests ended with zero active
channels after exact cleanup. These observations verify answered calls and
packet exchange. They do not establish voice quality, an overall loss percentage
or working menu interaction. **Human listening and DTMF menu tests remain
pending.** Callcentric remains blocked for outbound calls pending provider
review, with 801 withheld.
Successful container/runtime checks do not replace human-audible phone tests.
See the [dated validation record](../server-validation.md) for detailed results.

## Sources

- [tools/build_slides.mjs](../../tools/build_slides.mjs) contains text, colors,
  object layout, source-linked speaker notes and the four SVG definitions.
- [SVG diagrams](../diagrams/) can be edited in a text editor, Inkscape or another
  SVG editor. They contain no external images or fonts.
- Presentation font: Arial. Format: 16:9, 1280 × 720.

## Rebuild SVG files without special packages

From the repository root:

```bash
node tools/build_slides.mjs --svg-only
```

This updates `docs/diagrams/architecture.svg`, `assist.svg`, `commissioning.svg`
and `power.svg`. It needs a modern Node.js runtime and makes no network requests.

## Rebuild PPTX and PDF

Authoring uses `@oai/artifact-tool` from Codex's bundled workspace runtime.
This is **not a public npm dependency**; do not install an unrelated package with
the same name. In Codex, obtain paths with `load_workspace_dependencies`, then
set these variables to the returned paths:

```bash
export RUNTIME_NODE=/path/to/bundled/node
export RUNTIME_NODE_MODULES=/path/to/bundled/node_modules
export ARTIFACT_TOOL_ROOT="$RUNTIME_NODE_MODULES/@oai/artifact-tool"
export SOFFICE=/path/to/bundled/soffice
"$RUNTIME_NODE" tools/build_slides.mjs
```

For the built-in package check before delivery, also set:

```bash
export PRESENTATION_SKILL_DIR=/path/to/presentations/skills/presentations
export RUNTIME_PYTHON=/path/to/bundled/python3
"$RUNTIME_NODE" tools/build_slides.mjs
```

The build writes PPTX, PDF and `preview.webp` to `docs/slides/`. Visual-review
images, the candidate PPTX and validation reports go to Git-ignored
`.slides-build/`. If native PDF export is unavailable, the script uses LibreOffice
via `SOFFICE`. Review every slide after edits: structural validation does not
establish that labels and arrows are legible.

Editing the resulting PPTX in PowerPoint or LibreOffice does not require this runtime.
