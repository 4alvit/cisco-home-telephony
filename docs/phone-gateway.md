# Persistent phone control from Home Assistant

![Private phone control architecture](diagrams/phone-control.svg)

The optional gateway connects HA to the existing local Cisco phones. It supports
numeric dialing, answering a selected ringing line, exact-channel hangup,
explicit speaker-button toggling, PBX Do Not Disturb, opt-in auto-answer, text
pages and photographs. The separate `cisco_phone_control` HA integration supplies
entities and recurring alarms/timers. A button press is not evidence of successful
audio; see the dated [validation record](server-validation.md).

This service is an explicit change from the short-lived commissioning callback.
It keeps the previously provisioned callback URLs and credentials, so the phones
do not need a new profile or another restart. It runs only on the private network.
Never publish its API or callback ports on the Internet.

## Prepare private configuration

Enable the dedicated AMI account in the private site input:

```json
"gateway": {"enabled": true, "ami_client_ipv4": "192.168.50.10"}
```

The address is the PBX/gateway node, not an arbitrary client network. Provisioning
creates `gateway-ami-secret.json` separately from the HA AMI account. Preserve all
existing secrets when generating into the deployment directory.

After all phones have verified profiles and working commissioning callbacks:

```bash
umask 077
python3 scripts/configure_gateway.py \
  --site .local/site.json --inventory .local/phones.json \
  --ami-secret .local/gateway-ami-secret.json \
  --callback .local/phone-a/control.json \
  --callback .local/phone-b/control.json \
  --ha-source-ip 192.168.50.11 --enable-persistent \
  --output .local/gateway.json
python3 scripts/render_gateway.py --site .local/site.json \
  --config .local/gateway.json --output .local/gateway.yaml
```

Include exactly one callback for every inventoried phone. The generator verifies
each device, exact source ACL and previously provisioned authentication URL. It
does not overwrite an existing gateway file. Preserve its bearer token and state
across upgrades. Add only reviewed internal radio extensions to `dial_allowlist`
and, if wanted, `ring_allowlist`; neither is an unrestricted dialplan.

## Deploy and hand over the listeners

Build the Asterisk image containing `phone_gateway.py` and Pillow and use its
immutable digest. On the selected node, create `/var/lib/cisco-phone-gateway`
owned by UID/GID `10001:10001`, mode `0700`. Back up this directory with the
private gateway input; it contains preferences and operation idempotency state.

Review host firewall changes before applying. The API port (default TCP18084)
admits the actual HA source. The node itself may complete TCP health probes;
HTTP API requests still require the HA source and bearer token. Each callback
port admits only its exact phone IP. Keep the dedicated AMI account restricted to the node's
source address. Also admit that exact node `/32` to **TCP 5038 in the host's raw
telephony guard**, alongside the existing HA exception. A connection from the
node to its own private address can traverse local/loopback routing and still
be blocked by raw PREROUTING; host networking does not bypass that rule. Match
the gateway's actual source address rather than opening AMI to a subnet or all
loopback traffic. Asterisk's AMI user ACL must independently permit the same
source. A healthy gateway TCP listener does not prove its AMI connection works;
verify authenticated inventory/state requests too. Ordinary Kubernetes network
policy alone does not provide this boundary.

`scripts/gateway_firewall.py` manages only its own `CISCO_PHONE_MGMT` INPUT
chain in IPv4/IPv6. It returns allowed traffic to the existing host policy;
it never broadly accepts traffic or flushes Kubernetes chains. Its JSON input
contains `bind_ipv4`, `api_port`, `api_clients` and `phones` (each with `ipv4`
and `port`), without credentials. Copy the script and reviewed input to the node,
run `check`, then `apply`; `status` verifies the exact owned rules. Run `apply`
from a root-owned systemd oneshot unit before the gateway starts, and retain
that unit across node reboots. IPv6 is blocked on all reserved ports. Changed
owned rules are refused unless an explicit `--previous-config` matches them.
`remove` deletes only the owned hook and chain. Keep predecessor callback
source restrictions until the replacement has been verified.

With no active commissioning requests or phone calls, stop the old temporary
callback services, cancel their stale cleanup timers, and apply the reviewed
gateway manifest. Never run two listeners on one callback port. Check readiness,
authenticated inventory, every phone's callback and a harmless notification.
Then add the HA integration using the private API URL and token. Do not expose
the token in screenshots, public YAML, Git, terminal output or URLs.

Rollback: remove the gateway Deployment, restore the previous exact-source
firewall rules, and explicitly renew/start the original bounded callbacks if
needed. Existing SIP registrations do not depend on the gateway. Alarm and
auto-answer control stop when it is unavailable; failed alarms are not replayed
automatically after recovery.

## API contract

All API requests require `Authorization: Bearer <private-token>` from a configured
HA source. `GET /v1/phones` returns stable primary IDs, assigned lines, live AMI
registration/call state, PBX DND, auto-answer and capability names. It does not
return credentials. Every mutation is a JSON POST to
`/v1/phones/<primary-line>/<action>` with a unique `idempotency_key`.

- `dial`: `line`, `number`; only explicit configured numbers are accepted.
- `answer` / `hangup`: `line`, `channel`, `uniqueid` copied from a fresh inventory.
- `speaker`: `mode: "toggle"`; only an idle phone is accepted. This emulates a
  physical button, so there is no invented boolean speaker state.
- `dnd` / `autoanswer`: `enabled: true` or `false`. Both default to false.
- `ring`: `line`, `destination` (default640); ring up to20seconds, then run the
  allowlisted local destination after answer. Alarm640 ends after a finite tone.
- `message`: `title`, `text` (up to2000characters).
- `image`: `title`, `image_base64`; PNG/JPEG up to2MiB and16million source pixels.
  EXIF orientation is applied, metadata is removed, and the picture is converted
  to a bounded RGB PNG fitting the7945G's **298×156** Services pane.

Successful requests return `status: "accepted"` and `action_id`. HTTP409 means
busy, DND or stale/ambiguous ownership; an unavailable dependency returns503.
An ambiguous network outcome is never retried automatically. Reusing a completed
idempotency key returns its recorded result; a pending key returns a conflict.
Retain keys for one logical timer/alarm occurrence, not across different events.

## Behavioral limits

The controller distinguishes Asterisk **Ringing** (the phone is being called)
from **Ring** (the phone is originating a call). Auto-answer is opt-in per phone
and attempts one answer only for a single freshly observed ringing channel.
It must not press a line key during an outgoing call or another active line.

PBX DND blocks local direct/group routing and gateway alarm/dial requests. It does
not claim to synchronize the phone's native DND softkey/icon, or protect routes
which bypass this gateway and the guarded dialplan. Preferences are restored to
Asterisk after a PBX restart. Partial two-line updates retain the desired state
and are retried by reconciliation; inventory exposes `dnd_pending` until the
update completes. A short recovery interval can precede restoration.

Text/photo delivery is a Cisco XML notification, **not cellular SMS/MMS**.
The gateway serves each resource only to the intended phone for120seconds and
retains no message/image in its SQLite state. Expiring the URL does not clear a
page already on the display; press its Exit softkey. It is neither a video player
nor animated-GIF support. Still sprites or a composed sprite sheet can be sent
as a normal image. Font/locale coverage limits arbitrary Unicode.

The exact Cisco syntax, model limits and external calling options are documented
with primary sources in [extended services research](extended-services-research.md).
