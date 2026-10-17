# Editable diagrams

- [architecture.svg](architecture.svg): existing DHCP router, phones, PoE switch,
  Asterisk on a fixed LAN node, temporary TFTP and Home Assistant. HA initiates
  the AMI connection; Assist audio uses SIP/RTP.
- [assist.svg](assist.svg): extension 630, bidirectional G.711 ↔ Opus conversion,
  native HA VoIP and pipeline acceptance on a physical phone.
- [commissioning.svg](commissioning.svg): inventory, firmware-path selection,
  TFTP readiness, pilot phone, call checks and rollout to the remaining batch.
- [power.svg](power.svg): built-in 802.3af support, powered by a PoE switch or an
  active injector connected to an ordinary switch.

Each file is a standalone SVG containing editable text and vector objects.
There are no MAC addresses, passwords or delivery details. Diagram sources and
rebuild instructions are in the [presentation guide](../slides/README.md).
The parent documentation contains full procedures, limitations and sources.
Project ports are listed in the [k3s runbook](../k3s-deployment.md) and
[HA guide](../home-assistant.md). Diagrams describe the architecture; they do not
establish that home deployment or physical acceptance has been completed.
