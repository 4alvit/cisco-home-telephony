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
- [remote-management.svg](remote-management.svg): authenticated CGI, SSH,
  a bounded authentication callback, temporary TFTP and a verified PoE port.
- [four-phones.svg](four-phones.svg): four phones with independent lines
  201–208, first-line group 620 and per-identity Assist 630. The
  [expansion runbook](../adding-phones.md) separates the intended architecture
  from current acceptance results.
- [three-phones.svg](three-phones.svg): the earlier three-phone architectural
  example with independent lines 201–206.
- [radio-services.svg](radio-services.svg): per-call radio workers, approved
  broadcaster streams, and alarms that ring until manually stopped.
  See the [radio and alarms guide](../radio-and-alarms.md) for limits and tests.
- [directory-data-flow.svg](directory-data-flow.svg): architecture only, from
  private inventory through validated server configuration and the authenticated
  gateway API to the CLI. It contains no directory entries, names or numbers.
- [native-app-feasibility.svg](native-app-feasibility.svg): existing screen APIs,
  the proposed local voice path, native-code gates and video limitations.
  This is an assessment, not a deployed-feature diagram.

Each file is a standalone SVG containing editable text and vector objects.
There are no MAC addresses, passwords or delivery details. Diagram sources and
rebuild instructions for the presentation figures are in the
[presentation guide](../slides/README.md). The remote-management and multi-phone
figures are edited directly as standalone SVG.
The parent documentation contains full procedures, limitations and sources.
Project ports are listed in the [k3s runbook](../k3s-deployment.md) and
[HA guide](../home-assistant.md). Diagrams describe the architecture; they do not
establish that home deployment or physical acceptance has been completed.

## Phone controls and alarms

[phone-control.svg](phone-control.svg) shows the HA entities, private API, fresh AMI guards and per-phone callback boundaries. [radio-services.svg](radio-services.svg) shows radio decoding and manually dismissed timer/alarm calls.
