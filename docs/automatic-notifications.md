# Automatic spoken notifications

Selected Home Assistant text notifications can be spoken on Cisco phones and
converted Echo devices using the configured HA voice. Cisco screen copies are
optional. The existing phone, browser or mobile notification runs
first. The optional device copy has its own event consumer, so an unavailable
phone cannot prevent the original notification.

![Selected notifications, private routing and guarded device delivery](diagrams/automatic-notifications.svg)

The gateway exposes `POST /v1/notifications`. Home Assistant supplies the text
and the original event identity; the gateway chooses destinations from private
configuration. This uses the existing phone gateway and converted-Echo
announcement adapters. It does not install another Home Assistant integration,
capture every notification, or assign another SIP number to an Echo.

## What each device receives

- **Cisco speech:** the existing announcement service synthesizes the text with
  the configured HA voice and calls an eligible idle handset. The gateway
  answers only that announcement's verified incoming call on speakerphone.
  The message plays once and Asterisk ends the call. Ordinary incoming calls
  retain their existing auto-answer settings. A brief ring may occur while the
  incoming call is verified.
- **Optional Cisco screen:** a plain-text notification on an eligible idle handset.
  Both line numbers resolve to one physical handset. The notification page has
  an idle timeout, and ordinary calls keep priority. Existing motion-image
  notifications remain a separate path.
- **Converted Echo / Echo Dot / Echo Show:** an optional spoken copy through
  the existing direct announcement adapter and configured HA voice. These are
  converted devices with the required local adapter, not stock Amazon speakers.
  The adapter checks that the device is idle immediately before playback.
- **Other endpoints:** do not infer support from a SIP registration or an HA
  media player alone. The forwarding route requires the matching capability.
  In particular, a device without a text screen does not receive a Cisco XML
  page.

The public sample keeps forwarding and speech disabled until configured.
`speech_enabled` controls speech on both Cisco and Echo devices. Screen
delivery is a separate choice; it is not a substitute for spoken delivery.
Speech destinations may be configured while speech remains disabled, allowing
installation and review without sound.

## Configure the existing gateway

Deploy the updated gateway and its sibling `phone_notifications.py` from the
same reviewed source revision before enabling this configuration. The normal
Asterisk and voice image builds include both files. Follow the existing
[gateway deployment procedure](phone-gateway.md); an older gateway does not
implement the new endpoint.

Add a `notifications` object to the gateway's existing private configuration.
Keep that configuration in the existing Kubernetes Secret or another
owner-only runtime file. Start with
[the configuration sample](../examples/automatic-notifications.example.json).
The following example contains fictional destinations:

```json
{
  "notifications": {
    "enabled": false,
    "speech_enabled": false,
    "sources": [
      "general",
      "doorbell"
    ],
    "screen_destinations": [],
    "speech_destinations": [
      "*"
    ],
    "echo_destinations": [],
    "cooldown_seconds": 60,
    "max_event_age_seconds": 90,
    "quiet_hours": {
      "start": "22:00",
      "end": "08:00",
      "timezone": "Etc/UTC"
    },
    "announcement": {
      "base_url": "http://127.0.0.1:18085",
      "token": "REPLACE_WITH_EXISTING_PRIVATE_ANNOUNCEMENT_TOKEN"
    }
  }
}
```

Use your local IANA timezone for quiet hours. Set `enabled` to `true` only after
the destination and source lists are reviewed. Keep `speech_enabled: false`
when no audible delivery is wanted.

Configured quiet hours suppress **both** screens and speech. They may cross
midnight; their start and end must differ. Omit the optional `quiet_hours`
object when all-day screen delivery is wanted, keeping `speech_enabled: false`
to prevent audio. Quiet hours use the specified timezone rather than the HA
server's assumed local time. These are admission checks, not a hard mute for
an announcement already playing.

`screen_destinations: ["*"]` explicitly includes every configured compatible
Cisco handset, including those currently powered off. An offline device cannot
receive a notification; it becomes eligible for a later new event when it
returns. Missed notifications are not replayed.
`speech_destinations: ["*"]` includes every configured compatible Cisco
handset for spoken delivery, including those currently powered off. Explicit
lines such as `["201", "203"]` select a subset. Sibling line numbers resolve to
one physical phone and do not create two announcements. Set
`screen_destinations: []` when the intended result is speech only. A physical
phone must not appear in both screen and speech destination sets, even under
different line numbers: overlapping modes are rejected instead of allowing a
screen update and a call to compete. `speech_enabled: false` suppresses both
Cisco and Echo speech without turning speech recipients into screen recipients.

At most ten physical Cisco handsets and sixteen speech recipients in total
are supported per notification. For example, a spoken group can contain ten
Cisco phones and six Echo devices, or sixteen Echo devices alone. Configure
`echo_destinations` with the existing mapped Echo extensions. Both speech
routes need the private `announcement` connection to
the existing local announcement service. Reuse that service's actual address
and bearer token:

```json
{
  "announcement": {
    "base_url": "http://127.0.0.1:18085",
    "token": "REPLACE_WITH_EXISTING_PRIVATE_ANNOUNCEMENT_TOKEN"
  }
}
```

This object belongs inside `notifications`. The announcement token and gateway
API token have separate purposes. Do not publish either token. Do not expose
the announcement service to the LAN just to support HA: HA talks to the
gateway, and the gateway reaches the existing local service. Requests pin
Cisco recipients to the `sip` route and Echo recipients to `ha_satellite`. An
Echo cannot silently fall back to a SIP call. The existing announcement service
must already map each configured extension to its physical device.

For the sample above, replace the token, choose your timezone, and set both
`enabled` and `speech_enabled` to `true` to speak selected notifications on all
configured Cisco phones. Add existing Echo extensions to `echo_destinations`
when those speakers should receive the same text. No CLI process must remain
running: the gateway follows its own announcement job and issues one answer
only after matching the incoming line, channel and unique call identity.
Asterisk owns playback completion and hangup. See
[announcement routing](cli-announcements.md) for the service and adapter setup.

## Install the Home Assistant consumer

Copy [the example package](../examples/cisco_notifications.yaml) into HA's
existing package directory. Preserve the installation's package include and
existing configuration; do not replace `configuration.yaml` or an existing
notification dispatcher with this example.

Add these keys to HA's private `secrets.yaml`, substituting the gateway address
and its existing token:

```yaml
cisco_notifications_url: "http://192.0.2.10:8080/v1/notifications"
cisco_gateway_authorization: "Bearer REPLACE_WITH_EXISTING_GATEWAY_TOKEN"
```

Use the actual trusted gateway URL and restrict gateway access to its approved
API clients. If using HTTPS, configure a valid trusted certificate. The
`rest_command` sends JSON using HA's `to_json` filter, preserving quotation
marks, newlines and non-English text without shell interpolation.

The consumer listens only for `cisco_notification_requested`. It keeps no
automation traces, runs at most four copies concurrently, and drops excess
work instead of building a queue of stale announcements. The gateway still
validates every request and owns durable duplicate prevention. The consumer
performs no automatic retries. Missing, malformed, partial or refused responses
produce a fixed warning in HA's log without including message text, source
names, response bodies or tokens.

Validate HA configuration, then reload the affected REST commands and
automations using the actions supported by the installed HA version. If a
restart is required by that installation, use its normal maintenance process.
HA's [RESTful Command documentation](https://www.home-assistant.io/integrations/rest_command/)
describes configuration and response handling.

## Forward one copy of each selected event

The existing `script.notify_optional` can send one event after its normal
delivery loop. Extend its fields with `also_devices` (default `false`),
optional `device_event_id`, and optional `device_event_at`. Keep `also_devices` independent of
`also_chrome`: choosing a browser copy must not silently enable a speaker.
Retain the installation's existing mute, category and provider guards.

Merge these entries into the script's existing `fields` mapping; none is
required for existing callers:

```yaml
also_devices:
  name: Also notify local devices
  default: false
  selector:
    boolean:
device_event_id:
  name: Original event ID
  selector:
    text:
device_event_at:
  name: Original event Unix timestamp
  selector:
    number:
      min: 0
      max: 4102444800
      mode: box
```

Append this block after the original notification deliveries:

```yaml
- if:
    - condition: template
      value_template: >-
        {{ also_devices | default(false) | bool }}
  then:
    - event: cisco_notification_requested
      event_data:
        notification: >-
          {% set payload = {
            'source': notification_category | default('general', true),
            'title': notify_data.get('title', 'Home Assistant'),
            'message': notify_data['message']
          } %}
          {% if device_event_id is defined %}
            {% set payload = dict(payload, event_id=device_event_id) %}
          {% endif %}
          {% if device_event_at is defined %}
            {% set payload = dict(payload, event_at=device_event_at) %}
          {% endif %}
          {{ payload }}
      continue_on_error: true
```

Keep the `notification` wrapper: HA requires `event_data` to be a mapping.
Its nested template returns the complete notification mapping, preserving
numeric-looking text such as `"123"` as text. Rendering each message field as
a separate native template can accidentally turn it into a number. The
consumer accepts this wrapped shape and an already structured flat event;
the REST API receives a flat JSON object.

Where available, the caller supplies the original event ID and timestamp.
In a state-triggered automation, a call can look like this; replace the
fictional notifier:

```yaml
- action: script.notify_optional
  data:
    notify_service: notify.example_mobile
    notification_category: general
    also_devices: true
    device_event_id: "{{ trigger.to_state.context.id }}_example"
    device_event_at: "{{ as_timestamp(trigger.to_state.last_changed) }}"
    notify_data:
      title: Laundry
      message: The washing machine has finished.
```

If one automation sends the same notification to a phone, browser and TV,
set `also_devices: true` on **one** of those calls. Use the same event ID only
for the same logical event and payload. A second event requires a new ID.
Never generate a new timestamp merely to make delayed work appear fresh.
For non-state triggers, use their original stable event identity and time.

If the producer omits an event ID, the consumer combines the forwarding
event's context ID with its `time_fired` timestamp. An omitted `event_at`
also uses `time_fired`. This describes
the notification boundary, not the original sensor event or camera exposure.
It cannot deduplicate independently emitted events with new contexts or times.
Including the time distinguishes intentional notifications emitted under one
shared context. Reprocessing the same event keeps its identity. Reusing an
explicit ID with different content is refused.
Preserve explicit original metadata when forwarding delayed source events so
the gateway can reject them as stale.

The event can also be emitted directly after an original notification action.
Only sources explicitly listed in the private gateway configuration are
allowed. Avoid enrolling noisy motion or switch-state categories by default;
select the categories whose text is useful on shared screens or speakers.

The request accepts a lowercase source key of up to 48 characters, starting
with a letter and containing only letters, digits and underscores. Event IDs
contain 8–128 letters, digits, underscores, periods, colons or hyphens.
`event_at` is a finite Unix timestamp; dates more than five seconds in the
future or older than the configured maximum age are refused. The default
maximum age is 90 seconds and the allowed range is 5–120 seconds.

Titles contain 1–128 characters and messages 1–2000 characters; blank messages
are refused. A Cisco screen uses the first 32 title characters and the full
message. Cisco and Echo speech use the first 1000 message characters, without
speaking the title. Put the essential information first. The Cisco page
requests a
45-second **idle** timeout; interacting with the phone can affect dismissal,
so this is not an exact wall-clock screen-clear guarantee.

## Delivery guarantees and privacy

The gateway validates freshness and configuration before delivery. A persistent
claim is recorded before sending to a physical destination. Repeated events,
duplicate handset lines, cooldowns and uncertain network outcomes must not
produce repeated notifications. Do not remove the gateway's persistent state
to force a replay.

The ledger holds hashes and delivery metadata for 24 hours, with a maximum of
4096 events. It does not provide unlimited replay history. Reusing an event ID
with changed content is a conflict. Target cooldown defaults to 60 seconds;
it may be increased up to one day. Skipped/offline events are consumed rather
than queued for later delivery.

An accepted request means work was accepted; it does not prove that someone
saw the screen or heard a message. Speech uses the announcement service's job
state: Asterisk reports Cisco playback and the direct adapter reports Echo
playback. Offline, busy, DND, active-alert and incompatible devices are skipped.
Automatic forwarding never interrupts an existing call or waits for a busy
device to become idle later. A Cisco announcement deliberately opens a short
speakerphone call; its microphone may be active during that call. This is
one-shot answering, not a change to persistent auto-answer or automute
preferences. Echo speech uses direct playback without a SIP call.

The API reports accepted, skipped and unknown recipients separately, with an
overall `ok`, `partial` or `refused` status. Its `display_verified` and
`playback_verified` fields remain `false`: transport acceptance is not
physical observation. Work has a 45-second
admission deadline; an already-started device operation may finish later.
Cisco answering runs in a bounded background worker, so HA does not wait for
the whole spoken message. Another event is refused while that worker is active;
there is no backlog of announcements. This refusal is reported as
`notification_speech_busy`, including for a dry run during an active worker.
The worker is bounded to 180 seconds and never repeats an uncertain answer
command or answers an unrelated ringing call. If the original event becomes
stale before the call is answered, the owned announcement job is cancelled;
it is not played later merely because the handset became available.

Cisco and Echo speech recipients share one announcement job. Busy or offline
recipients discovered before submission are skipped individually. After
submission, a failed Cisco call-ownership check, refused answer or uncertain
response makes the worker request one stop for the entire owned job. This can
cut short playback already in progress on other Cisco or Echo recipients.
The worker does not retry the answer or create a replacement announcement.

The HA REST command has a 60-second timeout. A client timeout cannot establish
that nothing happened, and must not trigger an automatic retry.

Notification text can be visible to anyone near a shared screen or audible
to anyone near an enabled speaker. Forward only the intended categories.
Keep private source-to-device mappings, names, addresses and tokens outside
public Git. Gateway delivery metadata must not contain notification text.
HA event listeners, calling automation traces and debug logging can still
expose action inputs: the example disables consumer traces, but cannot remove
data retained by other integrations or producer automations.
Synthesized speech uses the existing private announcement spool; its audio
cache and journals follow the retention described in the
[announcement guide](cli-announcements.md). Restrict access to that spool as
well as to the HA configuration and gateway state directory.

## Verify without making sound

1. Keep forwarding and speech disabled while preparing configuration. Validate
   the gateway configuration and HA package before enabling either route.
2. Review allowed sources and physical destinations. Select Cisco phones in
   `speech_destinations` and remove them from `screen_destinations`. Confirm
   Echo recipients use direct announcement routes and do not duplicate aliases.
3. Run the notification unit tests, including duplicate events, storage
   failures, stale events, busy devices and partial delivery. These tests use
   simulated transports and do not ring or speak on real devices.
4. Use the dry-run action below to review destinations without a screen, call
   or sound. Production speech requires both `enabled` and `speech_enabled`.
5. An audible test is a separate deliberate action, not part of installing the
   package or reloading HA. Optional screen delivery also changes a real device;
   it is not a read-only health check.

For a read-only target preview, the REST action accepts `dry_run: true`.
The following is a Home Assistant script action using a newly created review
event. Choose an allowed source. Forwarding must be enabled to inspect eligible
targets; a disabled configuration returns `disabled` without probing them.

```yaml
- action: rest_command.cisco_forward_notification
  data:
    notification:
      source: general
      event_id: notification-review-001
      event_at: "{{ as_timestamp(now()) }}"
      title: Configuration review
      message: Read-only delivery preview.
    dry_run: true
  response_variable: notification_preview
```

A dry run may read gateway inventory and existing duplicate history. It does
not send to a device, submit a spoken announcement, consume cooldown or write
delivery history. An eligible Echo in a dry run is only a configured route
candidate: the preview does not contact its announcement adapter and does not
prove that the Echo is online, idle or ready. Remove the temporary review
script when finished. Do not change `dry_run` to false merely to test
connectivity: that becomes a real notification.

## Troubleshooting and rollback

- **No forwarded copy:** verify that one producer sets `also_devices`, the
  event has its original identity/time, the source is allowed, forwarding is
  enabled, and HA can authenticate to `/v1/notifications`.
- **Cisco shows text instead of speaking:** move that handset from
  `screen_destinations` to `speech_destinations`, configure `announcement`, and
  enable `speech_enabled`. A screen-only configuration does not speak.
- **One handset receives nothing:** check fresh gateway capability and state,
  DND, a call/alert, cooldown and the chosen delivery mode. Listing both line
  numbers does not request two copies.
- **Cisco rings without speaking:** inspect the returned announcement job,
  the gateway's exact-call answer result and phone remote-control reachability.
  Do not enable permanent auto-answer as a workaround or repeatedly resend the
  event. An answer can be refused if the call changed or another call appeared.
- **Speech is silent:** verify `speech_enabled`, quiet hours and the existing
  announcement service. Echo additionally needs its direct-route capability.
  Do not switch Echo to a fallback phone call to bypass an idle guard.
- **Timeout or ambiguous result:** inspect recorded state before a new manual
  attempt. Do not regenerate IDs or retry automatically.
- **Repeated source notifications:** ensure only one producer call forwards
  and that the event ID is stable across copies.

For immediate rollback, set `notifications.enabled` to `false` in the gateway
configuration using its normal deployment process. To stop only speech, set
`speech_enabled` to `false`. Remove or disable the HA consumer and the
`also_devices` producer additions when retiring the feature. Leave original
notifiers, motion snapshots and persistent duplicate history intact.
