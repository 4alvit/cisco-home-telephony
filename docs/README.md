# Documentation

Running Cisco 7945G desk phones on a local Asterisk PBX, with Home Assistant
control, hands-free voice, and the dated records of what was actually verified.

## Start here

- [Code and infrastructure map](code-and-infrastructure-map.md) — where the PBX, gateway and voice code live.
- [Bootstrap the PBX before connecting phones](pbx-bootstrap.md) — what to prepare before MAC addresses and firmware are verified.
- [Cisco 7945G → SIP → Asterisk](sip-conversion.md) — converting one verified phone before expanding.
- [Inventory and configuration generation](provisioning.md) — generating the configuration from a verified inventory.
- [Add more Cisco 7945G phones](adding-phones.md) — extending to further physical phones and their ports.
- [Stable phone addresses with the existing DHCP service](static-phone-addresses.md) — reserving one IPv4 address per phone.
- [Daily phone boot and TFTP availability](daily-phone-boot.md) — the profile-only TFTP Deployment and why it persists.
- [Powering ten Cisco 7945G phones](poe.md) — built-in PoE without modification.
- [Verified facts about the Cisco 7945G](cisco-research.md) — the October 2026 review of model, condition and firmware.
- [Preserved Cisco 7945/7965 SIP firmware](firmware-archive.md) — the preserved firmware copy downloaded in October 2026.

## PBX and network

- [Install, connect and troubleshoot the PBX](asterisk-operations.md) — the path from a fresh checkout to registered phones.
- [Cisco 7945G → Asterisk in a home k3s cluster](k3s-deployment.md) — deploying the internal PBX for ten phones.
- [Optional external SIP accounts](external-sip.md) — the three disabled-by-default, SIP-only accounts.
- [Outbound SIP and profile TFTP firewall](external-firewall.md) — the narrow return path the firewall script adds.
- [Add existing speakers and displays to Asterisk](existing-endpoints.md) — serving a converted speaker, a display and a conventional endpoint.
- [A second line and a personal wallpaper on the 7945G](second-line-and-wallpaper.md) — using both programmable line buttons.
- [A separate SIP line on a Mac](macos-softphone.md) — a softphone as its own extension, not a Cisco phone.
- [LifeSize Phone in the Cisco CLI](cli-lifesize.md) — registering a first-generation LifeSize Phone over SIP.
- [Using the Cisco phones away from home](remote-phones-vpn.md) — the October 2026 research on remote use.
- [Free VoIP services and practice directory](free-voip-directory.md) — providers checked in October 2026, with what was and was not tested.
- [External calling and extended Cisco phone services](extended-services-research.md) — the October 2026 check of external services.

## Command line

- [Cisco command line](cli.md) — the entry point to the `cisco` command.
- [Install, connect and troubleshoot the Cisco CLI](cli-installation.md) — controlling the phone that owns a selected line.
- [Spoken announcements from the CLI](cli-announcements.md) — turning text into a bounded announcement.
- [Play a music library from the Cisco CLI](cli-music.md) — playing a library selection on the phone speaker.
- [Install and run the foreground voice CLI](voice-cli-install.md) — the foreground voice path for an already deployed gateway.

## Phone features

- [Remote management of the Cisco 7945G](phone-control.md) — web access, SSH diagnostics and authenticated control.
- [Persistent phone control from Home Assistant](phone-gateway.md) — the optional gateway between HA and the local phones.
- [Phone media players](phone-media-player.md) — one media player per physical phone.
- [Dialled alarm control and incoming ring group](phone-services.md) — the optional services on the existing Asterisk instance.
- [Applications on the Cisco 7945G](phone-applications.md) — the English Services menu apps.
- [Phone and service directory](phone-directory.md) — the directory, with fictional room and client names.
- [Handset hotline](handset-hotline.md) — a dedicated phone calling a private household group on pickup.
- [Physical phone volume](hardware-volume.md) — handling volume key presses on the 7945G.
- [Automatic spoken notifications](automatic-notifications.md) — speaking selected Home Assistant text notifications.
- [Motion snapshots on Cisco phones](motion-notifications.md) — showing one still image with a motion notification.
- [Camera preview on Cisco 7945G](camera-preview.md) — refreshing stills from an allowlisted Home Assistant camera.
- [Radio, alarms and do-not-disturb](radio-and-alarms.md) — the optional services on the existing Asterisk instance.
- [Dial-in time and Home Assistant music](dial-audio-services.md) — answering a local call with time or a configured playlist.
- [Home Assistant controls for existing endpoints](endpoint-home-assistant.md) — the offline renderer for endpoint controls.
- [Explicit phone commands in Home Assistant Assist](assist-phone.md) — the optional generator for Assist sentences.

## Voice assistant

- [Use the voice assistant on a Cisco phone](voice-quickstart.md) — the quickstart for an already configured phone.
- [Daily use of the hands-free voice assistant](voice-daily-use.md) — questions and commands in normal use.
- [Voice sessions and hands-free controls](voice-control.md) — session ownership and the hands-free controls.
- [Inspect the selected voice pipeline](voice-pipeline-preflight.md) — the exact pipeline in its private configuration.
- [Deploy or upgrade the voice-capable gateway](voice-gateway-deployment.md) — rendering the voice gateway deployment.
- [Stock-phone voice assistant: implementation and acceptance](voice-rollout.md) — the implementation on unmodified SIP45 firmware.
- [Stock Cisco 7945G voice transport](voice-transport.md) — the bounded RTP transport.
- [Local voice adapter prototype](local-voice-assistant.md) — the server-side wake and Home Assistant adapter.
- [Local voice performance](local-voice-performance.md) — the measured repeated synthetic turns.
- [Local Russian speech recognition repair](local-stt.md) — restoring and evaluating the dedicated recognizer.
- [Suspended voice-session pilot on k3s](voice-k3s.md) — the paused pilot and the persistent alternatives.
- [Finite wake session on k3s](voice-wake-k3s.md) — the opt-in pilot connecting the phone microphone to a frozen wake detector.
- [Optional local wake runtime](voice-wake-runtime.md) — the CPU inference image added to the voice image.
- [Voice status and camera stills on Cisco 7945G](voice-display.md) — the bounded companion screen.
- [Recover an ownerless SIP voice session](voice-orphan-recovery.md) — clearing a reserved session after a failed cleanup.
- [Diagnose the phone's RTP transmit source port](voice-rtp-source-port.md) — what to check on a `receive_idle_timeout`.

## Audio and media

- [Wideband audio on Cisco 7945G](audio-quality.md) — the revision that makes the 7945G sound better than narrowband.
- [Uncompressed audio on Cisco 7945G](uncompressed-audio.md) — the October 2026 documented status of 16-bit 16 kHz audio.
- [L16/16000 interoperability laboratory](l16-interoperability-lab.md) — separating SIP negotiation, byte order, RTP timing and audio.
- [Operate the L16 media and call-lifetime laboratory](l16-serverguard-operator.md) — running the four separate gates.
- [SIP video to phone-sized pictures](sip-video-bridge.md) — the prototype that negotiates a real SIP session and receives H.264.
- [SIP speakerphone voice prototype](sip-voice-contingency.md) — holding one ordinary bounded SIP call open.
- [Synthetic framebuffer boundary model](framebuffer-model.md) — the host-only model of the recovered display arithmetic.

## Research and feasibility

- [Home Assistant: verified capabilities and integration contract](home-assistant-research.md) — the October 2026 record separating source review from testing.
- [Home Assistant and Cisco 7945G](home-assistant.md) — where the full HA toolkit lives.
- [Offline firmware and video feasibility gate](firmware-lab.md) — the October 2026 assessment of the target firmware.
- [Cisco 7945G: custom software, local voice activation and video](native-app-feasibility.md) — a feasibility study and effort estimate.
- [Native firmware: recovered interfaces and the next decision](native-static-map.md) — what the retained firmware files revealed.
- [Telegram, WhatsApp and Viber voice bridges](messaging-bridges.md) — research only; none of these is deployed.

## Acceptance and validation

- [Physical acceptance record — 2 November 2026](physical-acceptance-2026-11-02.md) — the bounded tests of the existing deployment.
- [Live server validation — October 17, 2026](server-validation.md) — the installed server and the four physical phones.
- [Offline voice pipeline acceptance](voice-pipeline-acceptance.md) — the real wake detector and telephone audio path joined offline.
- [Offline integrated voice acceptance](voice-integration-test.md) — the script that joins command, gateway, ownership and cleanup.
- [Offline wake entrypoint acceptance](voice-entrypoint-acceptance.md) — exercising the wake entrypoint without audio hardware.
- [Offline reserved voice acceptance](voice-reserved-acceptance.md) — the outer wake-session launcher and reservation path.
- [Offline wake qualification](wake-qualification.md) — evaluating saved audio through the frozen detector.
- [Freeze a media candidate before an attended test](media-candidate-acceptance.md) — recording the exact declared inputs.
- [RC fuzz testing](fuzzing.md) — the fuzz candidate workflow.
- [Images from a native GitHub runner](ci-images.md) — what the standard `ubuntu-24.04` runner builds and smoke-tests.
- [Trusted pull-request approval](pr-approval.md) — the approval caller and what it delegates to.

## Operations

- [Operations and recovery](operations.md) — the routine operational commands.
- [Recovery after a router or network interruption](node-recovery.md) — keeping the PBX, gateway and profile server working.
- [Re-download installation files and remove local staging copies](local-artifact-retention.md) — what may be deleted from local staging.

## Presentation

- [Editable diagrams](diagrams/README.md) — the SVG sources and what each diagram shows.
- [Presentation: Cisco 7945G, Asterisk and Home Assistant](slides/README.md) — the PDF, the editable PowerPoint and the preview.
