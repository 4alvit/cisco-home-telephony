# Explicit phone commands in Home Assistant Assist

[render_assist_phone.py](../scripts/render_assist_phone.py) generates optional
English and Russian custom sentences and an `intent_script` package for **one
explicitly configured native TECHO5 phone**. Generation is offline; installing
the files does not initiate a call. This adds an HA conversation route, not a
new native Dot contact matcher or wake-word model.

Copy [the example](../examples/assist-phone.example.json) into private storage.
Replace the two sensor IDs and three action names with the target's verified
native entities and actions. The sensors must be its **Phone** and **Phone call**
entities. Use actual entity registry IDs; renaming a device does not reliably
change them. No credentials belong in this inventory or its output.

Choose unique `id` and English/Russian `namespaces` for each target. Every phrase
must include its namespace. All matching phrases control that fixed target,
regardless of which Assist device heard them; this is not source-device routing
or voice authentication. Do not reuse a namespace in another sentence file.
Use HA's built-in conversation agent for deterministic local recognition. A
pipeline using an unrelated LLM agent may bypass these sentences unless its
local-intent preference routes them to Home Assistant first. Installing files
does not change the device's Assist pipeline or prove it handles these phrases.

List only explicitly approved destinations. Numbers are strings of 3–8 digits;
leading zeros are retained. Numeric syntax alone does not make a destination
local or safe: verify the PBX routes and enforce its own dialing restrictions.
Each destination has literal spoken names in both languages. The renderer
rejects duplicate names within a language, grammar operators and wildcards.
There is no free-form number or contact slot. The example recognizes:

- `example phone call time service` → configured `555`
- `example phone answer`
- `example phone hang up`
- `телефон пример позвони службе времени` → configured `555`
- `телефон пример ответь на звонок`
- `телефон пример заверши звонок`

Recognition normalizes capitalization and punctuation. Additional synonyms are
accepted only when explicitly configured as destination names; namespaced
answer and hang-up wording is fixed. Speech recognition must support the
language of the selected Assist pipeline. Installing the Russian file does not
change the pipeline language or STT model.

## Native state guards and action adapters

The renderer uses the TECHO5 v1.0.40 native Phone sensor states: call requires
`Ready` and an empty Phone call peer; answer requires `Ringing`; hang up permits
`Ringing`, `Calling`, or `In call`. Unknown, unavailable, unconfigured, login
failure and other states do not pass. These values come from the
[native implementation](https://github.com/HuskerMinion/techo5/blob/2ae3c75776eb04f614295ceba7de3cfcf60e531f/echod/internal/feature/phone/phone.go).
This state model is not a generic SIP adapter: do not point it at PBX device
states or an unrelated client's sensor.

Each action is a fixed `esphome` action, named `script` adapter, or (answer/hang-up
only) `button.press` with one explicit `target_entity`. Call additionally declares
its `destination_field`, normally `number`. No service name or target is derived
from speech. An explicit target entity must exist, be available and not be a
restored placeholder; unused buttons with `unknown` state are allowed.
A named adapter must operate the selected phone, preserve the
destination allowlist and propagate errors; do not use a script that silently
ignores failures or starts background work. Verify the action exists in HA
before installation. Missing actions raise HA errors rather than reporting
success. The renderer cannot inspect service availability offline.

The state guard reads HA's current native sensor immediately before dispatch;
it is not an atomic device lock or a fresh PBX query. The native phone still
enforces its own call state. A successful spoken response says only that the
command was sent. It does not claim the call connected, was answered, or ended.
Blocked states return an explicit refusal, with no native action. There is no
automatic retry. This native call action starts an outbound call on the phone;
it does not use the generic gateway's ring-then-connect callback.

## Generate and load

From the repository root:

```sh
python3 scripts/render_assist_phone.py \
  --config /private/path/assist-phone.json \
  --output /private/path/assist-phone-rendered
```

The command writes three files, each mode `0600`, using JSON syntax that is
valid YAML. Review them, then copy the two `custom_sentences/{en,ru}/` files into
the matching directories beneath HA's configuration directory. Copy the
`packages/` file into the existing HA packages directory. A typical package
setup is `homeassistant: {packages: !include_dir_named packages}`; preserve the
installation's existing configuration structure. Use one filename per target;
when removing or changing an ID, remove its old sentence and package files too.

Run HA's configuration check. When the `intent_script` integration is already
loaded, call `intent_script.reload`, then `conversation.reload`. A first-time
installation may need one HA restart to load the integration; do not assume a
reload service exists before the integration is loaded. Those reloads load
definitions only. Typing a matching phrase into Assist **does execute the
command** when its guard passes. Select the built-in Home Assistant conversation
agent for a deterministic text test, in an approved call window with a known
destination. Use the physical/native hang-up control as the fallback.
[HA custom sentences](https://www.home-assistant.io/voice_control/custom_sentences/)
and [intent scripts](https://www.home-assistant.io/integrations/intent_script/)
describe the underlying integrations.

## Microphone limitation

The Dot's native English contact matching does not add Russian call/answer/end
commands. With **Wake word during calls** off, the target Dot may not open an
Assist turn while ringing or in a call. Use another existing Assist device or
HA's Assist text interface to issue the namespaced command. This renderer does
not alter that switch, configure a local hang-up word, or promise the Dot hears
commands during a call. Even when wake during calls is enabled, the native
implementation blocks activation during far-end speech and outgoing dialing.

## Offline checks

```sh
python3 -m unittest discover -s tests -p 'test_assist_phone.py' -v
```

The optional runtime tests additionally require `homeassistant==2026.10.4`,
`xmltodict`, and `hassil==3.12.1` in an isolated compatible Python environment.
They use real HA package merging, intent handling and sentence recognition,
with fake native actions. They never connect to HA, the PBX, or a phone.
