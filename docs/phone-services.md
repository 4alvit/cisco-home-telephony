# Dialled alarm control and incoming ring group

These optional services extend the existing local Asterisk deployment. They do not enable a provider, buy a telephone number, or change the timer/alarm ringing behavior. Printed documentation is English; the four spoken alarm confirmations use the configured Russian Silero/Baya voice.

- **641 — Toggle saved alarm.** From either line of a configured Cisco handset, enable or disable that physical handset's existing recurring alarm. Preserve its time, weekdays and owner line. If no alarm has been saved, announce that it is not configured and change no alarm settings.
- **621 — Incoming calls group.** Ring the explicit private list of local recipients simultaneously for up to 25 seconds. Skip recipients with gateway DND enabled. Ring each Cisco once through its primary line, and optionally include registered softphones.

The ordinary 620 group, 630 Assist call, radio stations, timer actions and recurring scheduler continue to work as before. Existing timers and alarms keep ringing until stopped or answered; 641 does not enable speakerphone auto-answer.

## Install and configure 641

Start with the [Asterisk operations guide](asterisk-operations.md), [Home Assistant integration](home-assistant.md) and [CLI installation guide](cli-installation.md). The Cisco Phone Control companion must include `toggle_saved_alarm` (version **0.4.0** or newer). Update that integration and reload/restart Home Assistant before enabling this service in Asterisk. Asterisk's image must contain `/var/lib/asterisk/agi-bin/phone_services.py`.

Create the initial alarm through Home Assistant or the CLI, choosing its owner line, time and weekdays. For example, after configuring CLI access:

```sh
cisco 201 alarm set 07:30 --days mon,tue,wed,thu,fri
```

Check `cisco 201 alarm --help` for the installed CLI's schedule syntax. The explicit `alarm_set` service saves an alarm even when disabled. In Home Assistant, explicitly editing Alarm time or a weekday also saves the displayed schedule without enabling it and captures the currently selected owner line once. Later time/day edits preserve that owner. Merely seeing a default time in an untouched entity does not mean an alarm has been saved.

Use a private directory outside Git (the ignored `.local` directory is suitable). Store an existing HA administrator long-lived access token and the **Cisco Phone Control** config entry ID in `ha-phone-services.json`, with this structure:

```json
{
  "token": "REPLACE_WITH_PRIVATE_HA_ADMIN_TOKEN",
  "entry_id": "REPLACE_WITH_CISCO_PHONE_CONTROL_ENTRY_ID"
}
```

Set mode `0600` and keep the containing directory private. The HA schedule service is administrator-only: this token has broader HA privileges than just alarms. It is read from a file and copied only into the PBX's private generated configuration/Secret; it is never placed in a command argument, environment variable, phone XML, directory response or public repository. Use a dedicated HA administrator identity if operationally appropriate, and revoke its token if exposed. Do not reuse a SIP password, gateway bearer token, or AMI password here.

Generate the four fixed prompts from your already configured TTS engine. `ffmpeg` must be installed on the machine running this preparation command:

```sh
umask 077
chmod 600 /absolute/private/site/ha-phone-services.json
python3 scripts/generate_alarm_prompts.py \
  --ha-url http://192.168.50.30:8123 \
  --credentials-file /absolute/private/site/ha-phone-services.json \
  --engine-id tts.local_russian_speech \
  --out /absolute/private/site/alarm-prompts
```

The example addresses and paths are synthetic. Use your own private HA address. The generator requests Russian speech from the selected HA entity; configure that entity's Wyoming/Silero backend to Baya beforehand. It cannot change the backend's voice. It accepts only the same HA origin's `/api/tts_proxy/` path, converts audio to mono 8 kHz signed 16-bit PCM WAV, and creates mode-0600 files. The output directory must not exist. Generation makes no phone calls. Prompts are prepared once, so a later 641 call does not wait for live speech synthesis.

Merge the `phone_services` object from [the example configuration](../examples/phone-services.example.json) into private `site.json`, and retain the already enabled gateway configuration. All input paths must be absolute. Each prompt must last 0.1–8 seconds and be no larger than 128 KiB; all four together must fit within 384 KiB. The existing 700,000-byte generated configuration limit also applies.

Regenerate and check the complete private deployment, preserving its existing SIP secrets:

```sh
python3 scripts/provision.py --site .local/site.json \
  --inventory .local/phones.json --out .local
python3 scripts/preflight.py --site .local/site.json
python3 scripts/render_k3s.py --site .local/site.json
```

Follow [the deployment procedure](k3s-deployment.md) for the new immutable image and generated PBX Secret. `phone-services.conf` contains the token, entry ID, inventory ownership and prompt audio. It is mounted only with Asterisk configuration, not TFTP. Regenerate the phone dial-plan XML as well; loading its new 641/621 entries removes digit-collection delays on Cisco phones. Never publish any generated configuration or encoded prompts carrying private metadata.

## Configure 621 without opening provider inbound

Set `inbound_ring_group.enabled` to `true` and supply `members` in private `site.json`:

```json
{
  "inbound_ring_group": {
    "enabled": true,
    "members": ["201", "203", "209"]
  }
}
```

Choose one primary extension per desired physical Cisco plus any desired declared softphone extensions. Omit both lines of a phone that should not ring. Labels and room names belong in private inventory; no room name is hard-coded in this feature. The generator rejects duplicates, unknown extensions, Cisco secondary lines and non-local destinations.

The group reads Asterisk's existing `cisco-dnd/EXTENSION` database keys before assembling the targets. A phone that is offline or unregistered cannot prevent the other targets from ringing. All-DND or empty availability ends without falling through to a provider or other service. A recipient's SIP redirect cannot forward this group outside the selected membership (`Dial` uses option `i`). Softphones may additionally decline a call using their own local DND setting.

Calling 621 locally tests the group. For real provider inbound, see [external SIP](external-sip.md). Only the existing reviewed Callcentric path supports an incoming target here: after obtaining a suitable account/number and verifying source trust, you may explicitly change that account's private `inbound.target` to `"621"`. Its `inbound.enabled` remains a separate opt-in and still requires the reviewed `source_cidrs`. The provider context permits only the exact registered account destination, then jumps to 621; it does not include the general local context. SIP2SIP and Linphone inbound remain unsupported. Group configuration alone creates no internet-facing route.

## Outcomes, retries and troubleshooting

On 641, the caller hears one of four short prompts: alarm enabled, alarm disabled, no alarm configured, or change not confirmed. Disabling a saved alarm also stops an active alert only when its kind, owner line and alert ID match that alarm. It leaves unrelated timers untouched. Enabling during a time already claimed by the scheduler does not replay that occurrence.

The AGI derives the line from the authenticated `PJSIP/EXTENSION-...` channel name, not caller ID. It maps that line to its physical handset using generated inventory. A softphone, provider channel or unknown line cannot toggle someone else's alarm. Each Asterisk call gets one hashed request ID, persisted by HA before mutation/stop actions. Repeating the same request does not toggle twice. The AGI makes one bounded request and does not retry a timeout. A timeout can mean the operation succeeded but its reply was lost: inspect HA or `cisco LINE alarm status` before placing another 641 call, because a new call intentionally makes a new toggle.

Read-only PBX checks:

```sh
asterisk -rx 'dialplan show 641@home'
asterisk -rx 'dialplan show 621@home'
asterisk -rx 'module show like res_agi'
asterisk -rx 'pjsip show contacts'
asterisk -rx 'database show cisco-dnd'
```

Run these inside the Asterisk pod as shown in the operations guide. Do not print `phone-services.conf` or turn on HTTP tracing with bearer headers.

- **No alarm configured:** save the time and weekdays explicitly using `alarm_set`; 641 deliberately does not create a default 07:00 schedule.
- **Change not confirmed:** confirm HA connectivity from the PBX, the integration version, administrator token, config entry ID, a fresh gateway snapshot, and current physical inventory. Check the actual HA alarm state before retrying.
- **No voice:** verify the AGI exists and is executable, `res_agi` is loaded, all four WAVs pass provisioning/preflight, and ordinary audio/echo test 600 works. The caller must remain connected for the prompt.
- **A selected phone does not ring:** confirm its primary extension appears in the private membership, its PJSIP contact is reachable, and DND is off. A secondary extension intentionally cannot be a group member.
- **Stale service after disabling:** regenerate configuration. Preflight rejects stale 621/641 routes or a leftover `phone-services.conf` after disabling the feature.

To roll back, restore the prior image and matching generated PBX Secret, or disable these two feature objects and regenerate. Keep HA's stored schedules and original credential files. Removing 641 removes only the dialled toggle; it does not cancel saved alarms or timers. An older HA integration cannot read the new optional no-op request-journal result, so keep the 0.4.0 integration when rolling back only the PBX, or restore a reviewed matching HA storage backup as part of a full rollback.
