# Daily use of the hands-free voice assistant

For an already configured Cisco SIP phone with supervised hands-free enabled.
Use the [setup quickstart](voice-quickstart.md) for installation and first-time
configuration. All numbers and request keys below are examples; substitute
your own privately. Both lines of a physical phone share one assistant.

## Ask a question

1. **Wait for readiness.** In Home Assistant, the phone's **Voice status** must
   show **Ready for wake phrase**. In `cisco 201 voice status`, look for
   **Hands-free readiness: Wake listener ready; say the wake phrase.**
   **Hands-free enabled** alone is not readiness.
2. **Leave the handset on its cradle.** The assistant uses the speakerphone;
   no Answer or Speaker key is needed to begin a request.
3. **Say the configured wake phrase, then one short question.** Use the language
   configured for your voice pipeline. There is no acknowledgment chime or
   spoken invitation to wait for between the phrase and your question. The
   request phase is **Listening to request** in Home Assistant, but that display
   can lag the audio; do not wait for a screen refresh before asking.
4. **Finish speaking and wait for the reply.** Status moves through **Thinking**
   and **Speaking**. Talking over a reply is not supported.
5. **Wait for Ready for wake phrase again.** Start the next question with the
   wake phrase again. A reply does not extend the current session's time limit.

Keep questions self-contained across sessions. A renewed session has a new ID;
conversation memory across renewals is not guaranteed. Readiness and software
playback counters do not establish that a reply was audible in your room.

## The connected call on the phone is expected

While hands-free is listening, the stock phone shows an active **Voice Assistant**
SIP call. That call carries microphone and reply audio; this prototype does not
return to the normal idle screen between questions or add a live assistant
screen to the SIP transport.

The gateway renews bounded sessions of at most five minutes. The old call ends,
a new call is established, and the wake model becomes ready again. There is a
gap in listening and the phone may ring briefly. Wait for readiness again;
there is no guaranteed fixed delay or uninterrupted listening.

An active assistant call with **Ready for wake phrase** is normal between
questions. **Thinking** and **Speaking** belong to a request or reply; they are
not the expected idle state. If either remains beyond the configured turn
timeout without a reply or progress, use Disable, check that Stop is confirmed,
and follow the [recovery procedure](voice-control.md#deadlines-and-uncertain-cleanup).
The call timer alone does not indicate a stuck request.

While hands-free is enabled, that phone is intentionally excluded from passive
[motion snapshots](motion-notifications.md), including gaps between renewed
sessions. Disable hands-free and confirm the voice session has fully stopped
before expecting snapshots on that phone. Calls, DND, alerts and uncertain
ownership can still prevent delivery. A motion notification never stops the
assistant to display an image.

## Use the phone for ordinary calls

- **Incoming call:** ordinary calls take priority over the assistant. Answer
  normally. After the call finishes, enabled hands-free can resume automatically
  when the gateway has positively observed the call and verified idle lines.
  Check readiness before the next wake phrase.
- **Dial from the CLI:** `cisco 201 DESTINATION` places a call from the selected
  line to your chosen number; replace `DESTINATION` with that number. The gateway
  handles the assistant handoff. See
  [calling commands](cli.md) for call and intercom controls.
- **Dial manually from a quiet phone:** Disable hands-free using the controls
  below, confirm the assistant call has ended, then dial normally. Re-enable
  hands-free when you want the assistant back.

Ending the assistant call yourself is a manual stop, not an ordinary-call
handoff. While the assistant call is active, **#** sends its DTMF Stop command.
These actions disable automatic listening. Use Enable to bring it back. Finish
other calls or radio playback before expecting wake readiness; both phone lines
must become idle.

## Enable or stop from the CLI

Use your [installed CLI and private connection](cli-installation.md):

```sh
cisco 201 voice enable
cisco 201 voice status

# Stop automatic listening, then check that the owned session has stopped.
cisco 201 voice disable
cisco 201 voice status
```

Enable authorizes ongoing microphone operation through successive bounded
sessions. The gateway runs it, so the CLI can exit. To resume after Disable or
a manual stop, run `voice enable` again and wait for readiness.

Disable clears persistent intent first and requests cleanup. Confirm disabled
intent and, if a session was active, an inactive session with Stop confirmed.
Disabled intent alone does not prove microphone closure. If a command times out,
read status before making another change; do not repeatedly send Enable.

For JSON consumers, `cisco --json 201 voice status` reports readiness as
top-level `hands_free_listening: true`. Do not infer it from a missing error
field, an active SIP call or a running process.

## Enable or stop from Home Assistant

The compatible **Cisco phone controls** companion exposes two read-only entities
for the phone: **Hands-free enabled** and **Voice status**. These are status
indicators, not on/off switches. The ordinary **Call status** sensor only
describes telephony; **In use** is expected during the assistant's SIP call
and is not assistant readiness.

An administrator can run the **Manage hands-free voice** action,
`cisco_phone_control.hands_free`. Select the loaded integration's `entry_id`,
the canonical handset `phone`, and one of its assigned `line` values. The
[service contract](voice-control.md#gateway-and-interface-integration) explains
these fields. A script action for Enable is:

```yaml
action: cisco_phone_control.hands_free
data:
  entry_id: YOUR_CISCO_PHONE_CONTROLS_ENTRY_ID
  phone: "201"
  line: "201"
  operation: enable
  request_id: example-enable-001
  enable_microphone: true
  exclusive_media: true
response_variable: hands_free_result
```

For Disable, use a separate action:

```yaml
action: cisco_phone_control.hands_free
data:
  entry_id: YOUR_CISCO_PHONE_CONTROLS_ENTRY_ID
  phone: "201"
  line: "201"
  operation: disable
  request_id: example-disable-001
response_variable: hands_free_result
```

Replace the example request key with a fresh key for each deliberately new
Enable or Disable. Replaying an old Enable cannot undo a later Disable. After
an uncertain result, inspect status before retrying. An administrator can save
these actions as scripts and expose script buttons on a private dashboard;
they must generate a fresh request key for each new operation.

After Enable, wait for **Ready for wake phrase**. After Disable, check both
disabled intent and session cleanup using `voice status` or the companion's
[finite status response](voice-control.md#gateway-and-interface-integration).
If the integration is unavailable, use the configured CLI.

## If it does not respond

- **Enabled, but not ready:** check **Voice status** or `voice status`.
  Waiting for idle, startup, retry delay and a fault are different states.
  End other calls or radio playback and allow preparation to finish.
- **No response to the wake phrase:** confirm readiness, use the configured
  phrase, and try a short question near the phone in a quiet setting. Room-wide
  pickup is not guaranteed. There is no wake acknowledgment sound to listen for.
- **Wake detected, but no question followed:** with `rearm_after_turn` enabled,
  the same wake session can recover from a local no-speech decision when local
  endpointing is enabled. It can also recover from the exact Home Assistant
  `stt-no-text-recognized` result after a request started, provided no text was
  recognized and no intent or TTS work began. Both paths wait for request
  closure and audio cleanup, and require the current session to remain valid;
  Stop, Disable, replacement or lease expiry cannot be undone by recovery.
  Wait for readiness before repeating the wake phrase and question. Recovery
  does not extend the original time limit or count as a completed reply.
  Manual activation, profiles without rearming, missing input audio and generic
  errors such as `stt-stream-failed` still stop the session. Failed closure or
  cleanup never silently rearms. See the
  [provider compatibility note](voice-gateway-deployment.md#enable-repeatable-wake-sessions)
  if an older provider reports empty recognition as a generic error.
- **Thinking or Speaking:** let the turn finish before asking again. If no sound
  is heard, inspect the [playback diagnostics](voice-quickstart.md#inspect-playback-progress);
  do not treat completed software playback as an audibility test.
- **Wrong language or voice:** the administrator must select the intended Home
  Assistant pipeline and TTS voice in the private profile. Switching from an
  Echo-style device to SIP does not select that pipeline automatically. Local
  wake detection also does not make a cloud recognizer local.
- **Faulted, recovery required, or an uncertain Stop:** use the
  [recovery instructions](voice-control.md#deadlines-and-uncertain-cleanup).
  Do not delete journals, force a restart or loop Enable to clear the condition.

The [release record](voice-rollout.md) describes qualified behavior and limits.
This guide does not imply that every phone or acoustic environment has been tested.
