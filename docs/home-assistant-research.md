# Home Assistant: verified capabilities and integration contract

Reviewed October 16, 2026 (Pacific). This record distinguishes source review,
container tests and physical-phone acceptance, which remains incomplete.

## Component selection

PBX management uses the existing
[TECH7Fox/asterisk-hass-integration](https://github.com/TECH7Fox/asterisk-hass-integration);
Assist conversations use native
[Home Assistant Voice over IP](https://www.home-assistant.io/integrations/voip/).
These are separate connections: HA connects to Asterisk AMI over TCP, while
Asterisk sends Assist calls to HA over SIP/UDP. Cisco phones register only with
Asterisk and continue using SIP/TCP and G.711. These baseline features do not
require a new custom HA integration.

The reviewed TECH7Fox version is `v1.0.5`, commit
`65ea9d637a58d6edb3b2bc9e8a6f90de896a9b3e`. The tag points to that commit; its
[release](https://github.com/TECH7Fox/asterisk-hass-integration/releases/tag/v1.0.5)
was published April 15, 2026. Endpoint discovery, sensor handlers and the service
were checked in source, not only against a website feature list.

Archive for reproducible installation:

```text
https://github.com/TECH7Fox/asterisk-hass-integration/archive/65ea9d637a58d6edb3b2bc9e8a6f90de896a9b3e.zip
SHA-256: 697792a522eb90db62672ff57bc0b03bec4caba45c0748583bdd32768e6dcad1
```

Normal installation is documented through HACS. The reviewed upstream tree has
no LICENSE; do not copy its source into our public companion repository as our
own redistributable integration. The installer can retrieve the chosen archive
directly from upstream. Upstream's `asterisk-ami>=0.1.7` dependency is not pinned
to an exact version; pinning integration source does not pin transitive Python
dependencies.

## What TECH7Fox provides

- Automatic PJSIP endpoint discovery using `PJSIPShowEndpoints`. Missing legacy
  `chan_sip` is supported because `SIPpeers` errors are handled.
- A server binary sensor, `AMI Connected`, for the AMI connection.
- Per-endpoint `Registered`, `State`, `Connected Line`, `DTMF Sent` and `DTMF Received`.
- `Registered` derived from DeviceState, not SIP ContactStatus or a separate
  registration confirmation. `State` represents line use, ringing, hold and unavailability.
- `Connected Line` includes a number and attributes `Channel`, `ChannelState`,
  `ChannelStateDesc`, `CallerIDNum`, `CallerIDName`, `ConnectedLineNum`,
  `ConnectedLineName`, `Exten` and `Context`; hangup adds cause and code. This is
  the last processed channel, not a complete call log or count.
- `asterisk.send_action` sends an arbitrary AMI action with parameters. There are
  no separate `asterisk.originate` or `asterisk.hangup` services.

Sources: [setup and send_action](https://github.com/TECH7Fox/asterisk-hass-integration/blob/65ea9d637a58d6edb3b2bc9e8a6f90de896a9b3e/custom_components/asterisk/__init__.py),
[sensors](https://github.com/TECH7Fox/asterisk-hass-integration/blob/65ea9d637a58d6edb3b2bc9e8a6f90de896a9b3e/custom_components/asterisk/sensor.py),
[binary sensors](https://github.com/TECH7Fox/asterisk-hass-integration/blob/65ea9d637a58d6edb3b2bc9e8a6f90de896a9b3e/custom_components/asterisk/binary_sensor.py).

This design supports **one AMI server**. The globally registered service has no
config-entry selector and may target the last registered client if multiple
servers exist. Reload the integration after adding Asterisk endpoints to rerun
discovery. After an AMI outage, a retained endpoint value is not evidence of its
current state; also check `AMI Connected`. Upstream does not reconstruct every
event missed during a disconnect. Verify compatibility with the installed HA
version locally; upstream runs synchronous AMI operations from async setup.

## AMI permission classes for these features

Config-flow fields are `host`, `port` (default `5038`), `username` and `password`.
This version has no TLS field. Use a dedicated account, the HA process's actual
source IP, a private network, host firewall and AMI ACL. Do not expose AMI to the
whole phone LAN or the Internet.

Minimum classes for discovery, sensors, Originate and Hangup on Asterisk 22.11.0:

```ini
[general]
enabled = yes
webenabled = no
bindaddr = PBX_LAN_IP
port = 5038

[homeassistant]
secret = GENERATED_PRIVATE_AMI_SECRET
deny = 0.0.0.0/0.0.0.0
permit = HA_SOURCE_IP/255.255.255.255
read = call,dtmf
write = system,originate
```

Replace both IPs and the secret; do not publish the resulting file.
`PJSIPShowEndpoints` requires `system`; `CoreSettings` allows `system` or
`reporting`; `Hangup` allows `system` or `call`; `Originate` requires `originate`.
AMI checks for at least one allowed bit, so these operations do not require adding
`reporting` and `call` to write permissions. `DeviceStateChange`, channel and hangup
events belong to read `call`; `DTMFBegin` belongs to `dtmf`.
`res_manager_devicestate.so` must be loaded.

`system` is broad: beyond discovery it permits some module operations and
configuration reads. These are minimum **classes** for unchanged upstream, not
an allowlist of four harmless commands. `read=all`, `write=all`, `command` and
`config` are unnecessary here. Redirect or Setvar would additionally need
`write=call`; this project does not use them.

Verified against [AMI action registration and checks](https://github.com/asterisk/asterisk/blob/22.11.0/main/manager.c),
[PJSIP registration](https://github.com/asterisk/asterisk/blob/22.11.0/res/res_pjsip/pjsip_configuration.c),
[DeviceStateChange](https://github.com/asterisk/asterisk/blob/22.11.0/main/devicestate.c),
and [DTMF/channel events](https://github.com/asterisk/asterisk/blob/22.11.0/main/manager_channels.c).

## Exact HA action schema

Call phone 201 and, after answer, connect it to existing echo extension 600:

```yaml
action: asterisk.send_action
data:
  action: Originate
  parameters:
    Channel: PJSIP/201
    Context: home
    Exten: "600"
    Priority: "1"
    CallerID: "Home Assistant <600>"
    Timeout: "20000"
    Async: "true"
```

Hang up a specific existing channel obtained from current PBX state:

```yaml
action: asterisk.send_action
data:
  action: Hangup
  parameters:
    Channel: "PJSIP/201-00000001"
```

The second example illustrates the format; channel names are dynamic. Do not
hardcode the suffix or pass an empty/stale Channel. The HA script must first check
that AMI is connected, the Channel matches the intended endpoint and its current
state indicates an active call. `send_action` does not return structured results
in an HA response variable: service success does not prove the phone answered.
Verify outcomes through events/sensors and the actual call.

Sources: [services.yaml](https://github.com/TECH7Fox/asterisk-hass-integration/blob/65ea9d637a58d6edb3b2bc9e8a6f90de896a9b3e/custom_components/asterisk/services.yaml),
[official Originate reference](https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/AMI_Actions/Originate/).

## DTMF automation through sensor changes

A DTMF sensor's state is the latest event timestamp; the digit is in its `Digit`
attribute. Other attributes are `Channel`, `ConnectedLineNum`, `ConnectedLineName`
and `Context`; Sent also has `CallerIDNum`/`CallerIDName`. Upstream does not publish
a native HA event such as `asterisk_dtmf`. Trigger on timestamp changes to handle
repeated identical digits, rather than watching only `Digit`.

Both handlers filter AMI `DTMFBegin` by `ConnectedLineNum == extension`.
Sent/Received describe directions from Asterisk's perspective. Do not assume
`201 DTMF Received` necessarily means the keypad on phone 201. During initial
testing, inspect both call legs and Channel. HA-generated entity IDs may gain
suffixes or be renamed. Map every `sensor.201_...` example to the actual registry,
then test digits 1/1/# during a call. For RFC4733, Asterisk must receive the media:
keep `direct_media=no`.

## Assist on extension 630

HA Core `2026.10.2` and dependency `voip-utils==0.5.0` were reviewed. Native VoIP
listens on **UDP 5060**; options field `sip_port` changes it. It is not a phone
registration server. Configure outbound PJSIP endpoint `ha-assist` with UDP
transport, `disallow=all`, `allow=opus`, `direct_media=no`, an explicit HA LAN
address, and no outbound authentication or registration. Route from `home`:

```asterisk
exten => 630,1,Dial(PJSIP/ha-assist/sip:630@HA_LAN_IP:5060,30)
 same => n,Hangup()
```

Use `qualify_frequency=0` for this AOR. The reviewed `voip-utils` has no OPTIONS
handler, so SIP qualify is not a reliable HA health check and can disable an
otherwise working route. Do not set a shared `from_user`: HA identifies devices
by SIP From URI. Preserving each extension's CallerID produces distinct HA
devices even though traffic comes from one Asterisk IP.

After the first call, a new device appears in HA. Enable **Allow calls**, choose
an Assist pipeline with working STT/TTS, then call again. Without permission, HA
plays an unconfigured-device message. Without an STT/TTS pipeline, it reports a
configuration issue. Native HA outgoing announcements require a separately
configured reverse SIP route; an outbound Assist endpoint does not provide them
automatically.

Sources: [HA setup/UDP](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/voip/__init__.py),
[pipeline checks](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/voip/voip.py),
[device identity and Allow calls](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/voip/devices.py),
[SIP implementation](https://github.com/home-assistant-libs/voip-utils/blob/a6d93d0c50c8d005d030bd11ce99a57934b4e5b2/voip_utils/sip.py).

## Audio and firewall

HA VoIP returns SDP `opus/48000/2`, `ptime:20`. Conversion to mono 16 kHz happens
inside HA before STT. Asterisk Opus passthrough alone is insufficient: the 7945G
needs **G.711 ↔ Opus transcoding**. The Dockerfile therefore builds
`codec_opus_open_source.so` from
[Wazo commit 591b6cd](https://github.com/wazo-platform/wazo-codec-opus-open-source/tree/591b6cd9b75e2b9b6d815ef364a385fccee595a7)
with `libopus-dev`, retaining Asterisk's native `res_format_attr_opus`. This is an
open GPL-2.0 module; no proprietary codec_opus binary is downloaded. Its license,
README and corresponding source archive are in the container at
`/usr/share/doc/asterisk-opus-open-source/`.

Asterisk retains RTP range `10000–10199`. On HA, `voip-utils` allocates a dynamic
port using `bind(..., 0)` and the next port for RTCP. RTP is not guaranteed to be
even-numbered, and the integration has no fixed-range setting. HA firewall rules
must account for its OS's actual ephemeral range and adjacent RTCP port, **only
from the PBX IP**. Provide direct routing to HA; publishing only Docker 5060/UDP
does not publish all dynamic RTP ports. Permit SIP/RTP and return traffic between
PBX and HA; allow AMI only from HA to PBX.

Audio/port-selection source:
[voip-utils 0.5.0 RTP server](https://github.com/home-assistant-libs/voip-utils/blob/a6d93d0c50c8d005d030bd11ce99a57934b4e5b2/voip_utils/voip.py).

## Completed checks and remaining acceptance

An isolated ARM64 Docker container running Asterisk 22.11.0 with synthetic
configuration verified that the open Opus module builds, loads as Running,
completes `core show translation recalc 1`, and leaves the PBX running.
`core show translation paths ulaw` and `... paths opus` report:

```text
(ulaw@8000)->(slin@8000)->(opus@48000)
(opus@48000)->(slin@8000)->(ulaw@8000)
```

This tests a real transcoder in a container, not a Cisco or a live HA call. Before
family use, verify upstream installation in actual HA, AMI discovery/reconnect,
one phone's registration/disconnection, incoming/outgoing calls, hold/hangup,
repeated DTMF, then a two-way Assist conversation through 630. Check Russian STT,
TTS, a permitted harmless command, and release of all channels after hangup.
Separately verify ordinary calls after Asterisk, HA and phone restarts. Later
home deployment results are recorded in [live validation](server-validation.md).
