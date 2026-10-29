# Home Assistant controls for existing endpoints

The offline [renderer](../scripts/render_endpoint_ha.py) creates an optional HA
package and dashboard for existing SIP clients, native phone integrations and
Cast speakers. It does not install an integration, register a phone, contact HA,
make calls or play audio. Keep existing Cisco controls and native phone
integrations in place; this package routes only the capabilities you configure.

| Endpoint | Controls | Source of current state |
| --- | --- | --- |
| Generic SIP client | Request a call, hang up one exact call, PBX DND | Fresh gateway AMI snapshot; dashboard polls every 30 seconds |
| Existing native phone integration | Only explicitly mapped dial, answer or hangup actions | The configured native readiness and call-state entities |
| Existing Cast speaker | Speak an announcement, stop current media | Existing Cast media player and speech provider |

Generic SIP clients are not HA media players and have no generated remote
answer, speaker, volume or auto-answer control. **Request call first rings the
selected SIP client**. After someone answers it, Asterisk dials the destination.
Native `dial` retains the semantics of its existing integration.

## Prepare an inventory

Copy [endpoint-ha.example.json](../examples/endpoint-ha.example.json) to a private
file and replace or remove its synthetic entries. `id` is a stable local key;
renaming it creates different HA scripts/helpers. `destinations` is a local
allowlist of existing dialplan numbers containing 3–8 digits, not a mechanism
to add PBX routes. The gateway independently enforces its own `dial_allowlist`.

For `kind: sip`, the gateway must already contain that softphone with
`controls_enabled: true`. Preserve the existing gateway bearer token and its
source-address allowlist. HA must be an allowed API client. A disabled endpoint,
an old gateway without generic endpoints, or unavailable AMI produces an explicit
script failure. The optional TECH7Fox integration is not required for these
controls; they use the gateway's `/v1/endpoints/<extension>` API.

For `kind: native`, use actual entity and action IDs from the installed phone
integration. This supports separate Dot `phone_*` and Show-client integrations
without duplicating their call stacks. An action adapter contains:

```json
{
  "action": "script.example_phone_dial",
  "destination_field": "number",
  "data": {"device_id": "example_device"}
}
```

`action` is an existing `domain.service`; `destination_field` is required only
for dial and receives the selected extension. Optional `data` contains fixed
scalar values; optional `target_entity` supplies `target.entity_id`. Choose an
endpoint-specific action or pass its explicit device identity. Only add `answer`
when that integration genuinely supports it. Native adapters own call identity,
busy-state and operation validation. For complex argument shapes, provide an
existing HA wrapper script and map that script here. A readiness entity other
than `on` or an unavailable explicit target stops the script with an error in
its trace. HA's action dispatcher rejects missing actions. Service existence
alone does not establish that the device implements the action.

For `kind: cast`, use an existing `media_player` from the Cast integration and an
existing `tts` entity. Runtime checks require media playback support and an
available player/provider; the Stop action separately checks stop support. An
unused TTS provider may have state `unknown`, which is allowed if it exists and
is not `unavailable`. No SIP extension is assigned to a Cast speaker.

## Render and install in the next HA maintenance window

```sh
python3 scripts/render_endpoint_ha.py \
  --inventory .local/endpoint-ha.json --out .local/endpoint-ha
```

Both files are generated with mode `0600`. They use JSON formatting, which is
valid YAML, plus a normal `!secret` reference in the package. No token is read
or written by the renderer. Add the referenced key to HA's existing
`secrets.yaml`, with the **complete** `Bearer <existing-token>` header value.
Keep that value private. The inventory field `authorization_secret` contains
only the key name.

Copy `endpoint-package.yaml` into HA's existing packages directory, after
checking how that installation includes packages. Do not replace its
`configuration.yaml`. Use HA's configuration check before loading the package.
Follow the active HA upgrade/maintenance plan for any restart; rendering does
not authorize one. Import `endpoint-dashboard.yaml` into a separate dashboard
using its raw configuration editor. Verify actual entity IDs after the first
load; prior entity-registry names or collisions can change generated IDs.

The dashboard includes destination, announcement text and DND helpers. Choose
their values before running the corresponding script. Scripts also accept
`destination`, `message` or `enabled` directly from automations; explicit inputs
override the helpers. The dashboard text helper supports 255 characters; a
direct speech action supports up to 500. There are no automatic call or speech
triggers, and changing a helper alone does not perform an action.

## Call identity and results

Every SIP mutation gets a fresh status response rather than trusting the
dashboard's polling sensor. Hangup requires exactly one current call and passes
both its channel and unique ID. The gateway revalidates that pair before hanging
up. With zero calls or multiple calls the script stops; use the client or the
gateway's exact-call control to choose the intended call. Call setup, hangup and
DND use a new operation key once per script run. The package performs no retries.
After a timeout or unknown outcome, inspect current state before starting a new
run. Restarting the script represents a new logical operation.

`available` reports a fresh AMI device state, not a new network registration
probe. A successful action response means the gateway accepted/completed its
control operation; it does not establish that a person answered or heard audio.
Confirm actual call transitions and two-way audio during attended acceptance.

## Cast announcement behavior

Speech uses HA's documented [`tts.speak`](https://www.home-assistant.io/integrations/tts/)
action and the existing player. A speaker must be able to fetch the generated
HA audio URL. Verify its network route and HA's internal URL if synthesis succeeds
but playback does not. Playback capability comes from the entity's
[`supported_features`](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/media_player/const.py).

Speech can interrupt existing media, and Stop stops whatever that speaker is
currently playing. Automatic restoration of previous media is not promised.
The reviewed [Cast implementation](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/cast/media_player.py)
does not advertise `MEDIA_ANNOUNCE`; the package does not pretend it does. This
is one-way audio output, not a SIP call or a two-way microphone session.

## Verify without disturbing other endpoints

First render and review the candidate files. Check a disabled endpoint and a
missing native action fail without a mutation. When the maintenance window is
complete, inspect one configured endpoint's status, then make an attended echo
call and verify exact-call hangup. Test DND on that endpoint and restore the
previous setting. Play a short Cast announcement only with someone present,
then inspect the existing player. None of these live steps are performed by
generation or the repository's unit tests.

The standard suite includes 11 renderer tests. The optional runtime suite adds
seven tests using HA's actual package merger, schemas, template engine and script runner with
in-process fake services. It covers missing actions, stale/ambiguous calls,
Cast feature refusal, destination `555`, leading-zero numbers and exact
trailing-zero channel IDs. To reproduce the runtime checks in an isolated
Python 3.14 environment, install `homeassistant==2026.10.4` and `xmltodict`, then
run:

```sh
python -m unittest discover -s tests -p 'test_endpoint_ha*.py' -v
```

The optional runtime tests skip when HA is absent; ordinary renderer tests have
no third-party dependencies. These checks validate the package without starting
a Home Assistant server or contacting existing endpoints.
