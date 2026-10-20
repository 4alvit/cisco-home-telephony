# Voice status and camera stills on Cisco 7945G

This is a local prototype for a bounded companion screen, implemented in
[`phone_voice_display.py`](../containers/asterisk/phone_voice_display.py).
It renders the English states **Listening**, **Thinking**, **Speaking**,
**Stopped** and **Error**, with an optional avatar or camera frame. The service
does not start a microphone, make a call, send a phone command, receive SIP
video, or select a Home Assistant pipeline. A state label reports the caller's
supplied state; it is not evidence that the phone microphone or speaker has
actually entered that state.

The code and synthetic tests are not physical acceptance. No new phone,
firmware, gateway or Home Assistant deployment is included. Read
[`native-app-feasibility.md`](native-app-feasibility.md) for the wider voice
project and its acoustic and call-control gates.

## Display contract

Each physical phone has one active session and one active image. A new start
is refused while that phone already has an active session. Replacing a frame
discards the previous frame rather than adding another gateway resource.
Sessions and frames are memory-only and expire. A page fetch must not extend
their lifetime indefinitely. The integration must run cleanup on a bounded
timer even if no phone makes another request.

**Server expiry does not dismiss an already-visible phone page.** The library
does not schedule a final refresh beyond the session lifetime, and a phone
that stops fetching can retain the last page indefinitely. HTTP `Expires`
controls history/cache behavior, not an on-screen dismissal timer. The visible
timestamp helps identify old content but cannot update itself after refresh
ends. The user may need to press Exit; an automatic dismissal would require a
separately reviewed controller behavior and physical testing.

Images are encoded as a real **298 x 156 RGB PNG**, not merely resized to fit
inside that rectangle. Aspect ratio is preserved and unused space is filled
with a background color. Camera metadata must not be exposed in the output.
The capture timestamp belongs to the supplied frame: fetching an old image
again must not make it appear newly captured. A failed or expired camera
refresh must stop presenting an old frame as current.

Active voice states have a small, deterministic avatar unless the caller
supplies an image. This is a static illustration, not an animation. The library
uses `CiscoIPPhoneImageFile` with an `image/png` response for an image and a
`CiscoIPPhoneText` page when no image is available. XML text and URL values must
be escaped by a serializer, with invalid XML control characters removed. The
298 x 156 limit is the Services image area, not the full physical panel. Cisco
documents both the format and the 7945G/7965G image dimensions in its
[XML object reference](https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cuipph/all_models/xsi/7_0/english/programming/guide/70xsi/xsi70obj.html).

Timed refresh belongs on the XML response, for example
`Refresh: 3; url=http://display.example.invalid:18080/voice/session/TOKEN`.
The URL must stay within the configured, phone-specific display route. Start a
single-phone pilot between **2 and 5 seconds**; this is a conservative test
interval, not a measured frame rate, latency guarantee or smooth-animation
claim. A slow source must not accumulate parallel frame requests. Use no
automatic refresh for terminal, blocked or expired pages.

Cisco documents the `Refresh` time in seconds and optional URL, including
manual refresh when the time is zero. Its HTTP `Expires` behavior also affects
the phone's navigation history; already-expired pages are omitted from that
history. The adapter should preserve `Cache-Control: no-store` and a past
`Expires` value so Back/Exit does not replay a stale session. Large XML and PNG
objects should be fetched: the documented phone POST object limit is 512 bytes.
[Cisco HTTP reference](https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cuipph/all_models/xsi/7_0/english/programming/guide/70xsi/xsi70htp.html).

## Stop and request authorization

The phone-specific listener, not a query parameter, establishes physical
phone identity. Keep the existing source-IP restriction and authenticate the
controller that starts or updates a display session. An untrusted request must
never choose an arbitrary phone, camera URL, Home Assistant entity or callback.
All advertised URLs come from configured server routes and session tokens.

The Stop softkey cancels the display session through a callback with an
authorization hook. Bind authorization to the physical phone and active
session, reject expired or replaced tokens, and claim a Stop operation before
calling external code so duplicate requests do not cancel a later session.
Do not hold a display lock while performing network I/O. Do not put credentials
or model details in page text, URLs, exceptions or public logs.

In an integrated voice service, the callback must request cancellation of the
matching voice turn and its owned audio stream through the voice controller.
It must never invoke a generic hangup, speaker-key toggle or stop-all-streams
operation. **Stopped** means the display has stopped; hardware silence is a
separate result that the audio controller must establish. A callback failure
must be surfaced without assuming that audio stopped. Session expiry and
server restart do not prove that a remote phone closed an audio stream.

## Call, audio and screen focus

The application is subordinate to ordinary phone use. Before starting a
session, responding to an automatic refresh, or sending any optional initial
page push, obtain current focus for the physical phone across both lines.
Unknown or stale focus is a refusal, not idle permission.

Apply these rules in the integration:

1. Incoming calls, ringing alerts, dialing, active calls and held calls retain
   the native phone screen and controls. Stop refreshing and expire the
   companion session. Do not send `Key:`, `Dial:`, `Init:`, RTP or other phone
   control URIs from the display service.
2. Radio playback also owns audio focus. If it uses a SIP channel, it is
   already covered by the busy-call gate. If it uses a separate RTP stream,
   the radio controller must supply that focus explicitly; an empty AMI
   channel list is not enough. Do not steal audio focus or resume a stopped
   assistant because a screen request arrives.
3. The controller may use Listening/Thinking/Speaking only for the current
   assistant turn after it has acquired audio focus. A camera preview has no
   audio rights. A doorbell can associate a still with an event in controller
   state, but this prototype cannot overlay it on a native incoming-call UI.
4. Following preemption, require a fresh authorized session and focus check.
   Do not queue a page that can unexpectedly appear when the user finishes a
   call. Preserve explicit user Stop across reconnects in the voice controller.

If an initial page push is added later, an HTTP URL with Execute `Priority=2`
is the appropriate additional device-side safeguard: Cisco describes it as
execute-if-idle with no deferred execution. Priority zero is immediate;
priority one waits for idle. This does not replace server-side arbitration,
and the priority attribute does not protect internal phone URIs.
[Cisco Execute priority reference](https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cuipph/all_models/xsi/7_0/english/programming/guide/70xsi/xsi70obj.html).

## Integration boundary

### Library API and offline example

[`phone-voice-display.example.json`](../examples/phone-voice-display.example.json)
contains synthetic identifiers and a reserved, nonresolving host. It is input
for `PhoneVoiceDisplay(**config, ...)`, not an addition that the existing
gateway configuration validator already accepts. `PhoneVoiceDisplay` opens no
network listener and starts no background worker. The optional
`VoiceDisplayServer` adapter below opens a listener only when explicitly
started. The JSON has no `enabled` setting. A deployment copies the example
into `.local/` and injects real controller hooks in code.

The constructor requires these hooks:

- `authorize(phone_id, action, context) -> bool` handles `start`, `update`,
  `read` and `stop`. It must return exactly `True` to grant access. The adapter
  creates trusted context after authenticating the request; client-supplied
  JSON is not trusted context.
- `focus(phone_id, context) -> str` allows `idle` or `assistant`. A call,
  ringing, radio, unknown state or a hook failure prevents presentation and
  permanently preempts the session. The controller remains responsible for
  the actual audio cancellation and focus transition.
- `on_stop(phone_id, session_id, context) -> bool` is invoked at most once for
  an authorized Stop of that session. Exactly `True` produces **Stopped**;
  other results or failure produce **Error**. The refresh loop is stopped
  before calling it. The library does not retry the callback.

All hooks must finish within the integration's request budget. The library
does not impose a network timeout on arbitrary injected Python code. Avoid
blocking requests under the gateway lock. Use a bounded controller operation
and surface uncertain cancellation rather than claiming success.

`refresh_seconds` must be an integer from 2 through 5, `ttl_seconds` from 5
through 120, and `max_sessions` from 1 through 64. The defaults are 3, 120 and
16 respectively. Session TTL uses a monotonic clock and is a hard lifetime:
read, frame refresh and state update do not extend it. UTC wall-clock capture
times are for display, not lease enforcement.

Run this example from the repository root. It creates only local objects and
uses a synthetic authorization context. The example hooks are not production
authentication or a real focus observer.

```python
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path("containers/asterisk").resolve()))
from phone_voice_display import PhoneVoiceDisplay

config = json.loads(Path("examples/phone-voice-display.example.json").read_text())
offline_context = object()
display = PhoneVoiceDisplay(
    **config,
    authorize=lambda phone, action, context: context is offline_context,
    focus=lambda phone, context: "idle",
    on_stop=lambda phone, session, context: True,
)
ref = display.start(
    "synthetic-phone-a", state="Thinking", text="Preparing a local response.",
    context=offline_context,
)
page = display.handle(
    "synthetic-phone-a", "/session/" + ref.session_id,
    context=offline_context,
)
assert page.status == 200 and page.allow_present
print(page.body.decode("utf-8"))
stopped = display.stop("synthetic-phone-a", ref.session_id, context=offline_context)
assert stopped.status == 200
```

`start()` returns `SessionRef(session_id, page_url, stop_url)`. Its keyword
arguments include `mode="voice"` or `mode="camera"`, `state`, `text`, optional
`image` bytes, optional `captured_at`, and optional `frame_source`.
`update(phone_id, session_id, ...)` replaces supplied state, text or image
within the original lifetime. Once the previous session is terminal or
expired, a new `start()` invalidates its URLs. A Stop callback still in progress
also prevents replacement, including after its session TTL expires.
`should_present(phone_id, session_id, context=...)` rechecks
presentation permission before an optional controller-owned page push. It
returns false while Stop is pending. An explicit GET before expiry can still
show **Thinking** with "Stop requested; awaiting confirmation." and no timed
refresh. The independent per-phone Stop reservation survives session cleanup
until the callback finishes; TTL expiry cannot open a new session underneath
that callback.

A camera source has the signature `frame_source(phone_id) -> Frame`, where
`Frame(data, captured_at)` contains PNG/JPEG bytes and a UTC Unix timestamp.
The callback can run on page fetches, at most once per refresh interval, with
only one callback in progress per physical phone. Camera images require an
explicit capture timestamp; a supplied voice image defaults to update time.
The displayed camera timestamp is prefixed with **Stale frame** when it is
more than `max(10, 2 * refresh_seconds)` seconds old. This is an age warning,
not an upstream freshness check. A frame-source failure clears the frame,
shows **Error** and stops automatic refresh. The
controller must supply the actual source capture time if it knows it; an
acquisition time is not proof that an upstream cached image is fresh. Keep
camera credentials and network acquisition in the controller and return only
the bounded image and timestamp. This callback is also subject to the
controller's timeout and concurrency budget.

`handle(phone_id, relative_path, query="", context=..., method="GET")` returns
`DisplayResponse(status, body, content_type, headers, allow_present)`. Strip
only the configured base path before passing it a route. Routes under
`/session/{session_id}` serve the main page, `/image?v={revision}`, `/text` and
`/stop`. Forward the returned HTTP status and headers, especially `Refresh`.
A suppressed result has status 204, no display body, and
`allow_present=False`; do not wrap it in an error page that takes screen focus.
An explicit Stop or page GET may return a terminal **Stopped** or **Error**
page when focus allows it. `should_present()` returns false for terminal
sessions so the controller does not proactively push those pages. An explicit
Stop may still invoke cancellation on a preempted session, but its response
remains suppressed to protect phone focus.
Only the latest frame revision is available. The separate `/text` page allows
text detail without requiring it to fit inside the image area.

Call `cleanup()` on the host's bounded housekeeping tick and use `stats()` for
the `sessions`, `frames` and `frame_bytes` counts. Lazy request cleanup alone cannot reclaim an
unvisited expired session promptly. The library creates no cleanup thread.

Inputs are bounded: images must be nonempty PNG/JPEG bytes, at most 2 MiB and
16 million decoded pixels, with a single frame. Output is an exact-size RGB
PNG with EXIF orientation applied and metadata stripped. Text is limited to
1,000 characters after XML control-character filtering. Capture timestamps
must be finite, nonnegative Unix times, no more than five seconds in the
future. The library validates configured base URLs and never follows them.
The host must ensure those URLs actually route to the matching phone-specific
listener; syntactic validation is not a network access policy.

The adapter must also bound concurrent `start()` and `update()` image-decoding
work. The 16-million-pixel limit applies to each input, while session/frame
limits bound retained state. The library does not provide a global decoding
queue or a cap on simultaneous direct conversions; many concurrent inputs can
consume more temporary memory than the retained frame count suggests. Use a
bounded worker pool or semaphore and reject excess work before decoding.

Invalid calls raise `DisplayError(reason, status)` rather than always returning
a `DisplayResponse`. Relevant statuses are 400 for malformed input, 403 for
failed authorization, 404 for an expired/unknown session or replaced frame,
405 for a non-GET route, 409 for active/stopped/changed session or unavailable
focus, 413 for invalid image byte size, 429 for session capacity, and 503 when
image support is unavailable. A stale image URL can legitimately return 404
after a frame replacement. Adapters should preserve the status and avoid
repeated automatic retry or disclosure of callback exceptions.

### Optional foreground HTTP adapter

`VoiceDisplayServer` supplies a bounded HTTP listener for a controller such as
`scripts/voice_session.py`. It is optional: callers integrating directly with
the existing gateway can continue using `PhoneVoiceDisplay` alone. Adding the
adapter does not deploy a service or change the shared gateway. Its threads
run inside the foreground controller process; it is not a separate daemon.

The constructor is:

```python
VoiceDisplayServer(
    bind_ip, port, phone_id, phone_ip,
    focus=observe_focus, on_stop=cancel_current_turn,
    refresh_seconds=3, ttl_seconds=120,
    max_workers=4, request_timeout=3.0,
)
```

Construction does not open a socket. `start()` binds the listener, starts its
bounded accept/request threads and returns the server. A context manager
calls `start()` on entry and `close()` on exit. Port zero is supported for
local tests; `.port` reports the selected port after binding. `.base_url`
contains the listener URL, `.display` exposes the underlying
`PhoneVoiceDisplay`, and `.session_ref` holds the current `SessionRef`.

Both address arguments must be literal unicast IPv4 addresses. The adapter
accepts phone requests only from the exact `phone_ip` TCP peer, ignoring
forwarding/proxy headers. It rejects other peers before allocating a request
worker or waiting for HTTP headers, so they cannot reserve the phone's Stop
request capacity with incomplete headers. This is a peer-address restriction, not TLS or
cryptographic device authentication; retain the private trusted-network
boundary. The controller creates and updates sessions with local method
calls. There is no remote create/update API. HTTP routes live under
`/voice/session/{token}` and retain the library's session, frame and Stop
checks.

`start_status(state, text="")` creates a session and returns its `SessionRef`.
It accepts lowercase or titlecase forms of the five English states.
`update_status(state, text="")` updates the existing reference and does not
rearm a stopped or expired session. The helper's `focus` and `on_stop` hooks
take no arguments: `focus()` returns the current focus, and `on_stop()` must
finish synchronously and return exactly `True` to confirm Stop. The usual
focus/preemption and uncertain-cancellation behavior still applies.

`max_workers` bounds simultaneous HTTP workers, and `request_timeout` bounds
client request handling. These limits do not make arbitrary Python callbacks
interruptible. Keep both hooks short and bound any synchronous I/O they
perform. `close()` releases the listener, client connections and display store,
with a bounded best-effort wait for threads; it cannot kill a callback blocked
inside Python. It does not stop microphone capture, playback or other media.
The controller must own that shutdown explicitly.

The following construction example stays on loopback and does not start a
listener. Its hooks are synthetic and must be replaced for a real controller:

```python
from phone_voice_display import VoiceDisplayServer

server = VoiceDisplayServer(
    "127.0.0.1", 0, "synthetic-phone-a", "127.0.0.1",
    focus=lambda: "idle",
    on_stop=lambda: True,
)
# When the foreground controller is ready:
# with server:
#     ref = server.start_status("thinking", "Preparing a local response.")
#     server.update_status("speaking", "Response ready.")
#     ...  # Keep the foreground controller alive for its owned session.
```

The helper never pushes a page to a phone. The parent controller owns an
optional initial `execute(phone, ref.page_url, priority=2)` after a fresh focus
check. It must not push on each frame or use the adapter's existence as proof
that call-screen arbitration or microphone shutdown has been accepted.

### Proposed gateway and app changes

The existing [`phone_apps.py`](../containers/asterisk/phone_apps.py) and
[`phone_gateway.py`](../containers/asterisk/phone_gateway.py) remain unchanged
in this track. Their existing temporary-resource pool allows 64 resources for
120 seconds; allocating new XML and PNG entries for every refresh would spend
that shared budget quickly. Keep this service's session/frame store separate
and bounded, and resolve resources through its own phone-specific routes.

`phone_apps_ha.camera()` already produces an exact-size image using
`ImageOps.pad`. The generic gateway image notification uses `ImageOps.contain`,
which can return a smaller image. The new display converter owns its exact
dimensions; it must not depend on callers having used one particular path.
The display layer accepts image bytes from a trusted caller and does not fetch
camera URLs. Camera acquisition, source allowlists, transport timeouts and
frame capture times remain the controller's responsibility.

The following snippets are an integration proposal, not an applied patch.
`voice_config`, `authorize_display`, `observe_display_focus`,
`cancel_display_turn`, `trusted_display_context` and
`trusted_display_context_for_menu` are adapter-supplied values or functions
that still need to be wired to the coordinated voice controller.
Use the physical phone ID already established by the listener.

```python
# Gateway initialization, after validating separate display configuration.
self.voice_module = app_module("phone_voice_display")
self.voice_display = self.voice_module.PhoneVoiceDisplay(
    **voice_config,
    authorize=authorize_display,
    focus=observe_display_focus,
    on_stop=cancel_display_turn,
)
self.voice_display_refs = {}  # At most one reference per configured phone.

# The authenticated controller starts or replaces a session and saves its ref.
# A handset GET does not create a new voice turn or enable a microphone.
```

Add a display-response writer alongside `Handler.reply`. Keep the ordinary
response path unchanged. A fixed header allowlist avoids accepting arbitrary
headers through a future adapter change.

```python
def reply_display(self, result):
    self.send_response(result.status)
    if result.status != 204:
        self.send_header("Content-Type", result.content_type)
        self.send_header("Content-Length", str(len(result.body)))
    for name in ("Cache-Control", "Pragma", "Expires", "Refresh"):
        if name in result.headers:
            self.send_header(name, result.headers[name])
    self.end_headers()
    if result.body:
        self.wfile.write(result.body)
```

Route the display before `PhoneApps.handle`, which currently returns only a
body and MIME type. A private per-phone `voice_path` can be derived from that
phone's configured display base URL. It must be disjoint from authentication
and existing application routes; if nested under `apps_path`, match it first.
Never derive this prefix or phone identity from an untrusted query parameter.

```python
# Inside Handler.handle_request(), after source-IP and request-target checks.
voice_path = gateway.voice_paths[phone["id"]]  # Validated startup mapping.
if target.path == voice_path or target.path.startswith(voice_path + "/"):
    require(len(self.path) <= 4096 and not self.body(0), "bad_display_request", 400)
    try:
        result = gateway.voice_display.handle(
            phone["id"], target.path[len(voice_path):] or "/", target.query,
            context=trusted_display_context(phone, self), method=self.command,
        )
    except gateway.voice_module.DisplayError as error:
        return self.reply(error.status, b"", "text/plain; charset=utf-8")
    return self.reply_display(result)
```

For an app-menu entry, change only the menu construction in
`PhoneApps.handle(..., path="/", ...)` to append an already-created session:

```python
# items is the existing list of Home Phone Apps menu entries.
ref = self.gateway.voice_display_refs.get(phone_id)
context = trusted_display_context_for_menu(phone_id)
try:
    visible = ref and self.gateway.voice_display.should_present(
        phone_id, ref.session_id, context=context,
    )
except self.gateway.voice_module.DisplayError:
    visible = False
if visible:
    items.append(("Voice status", ref.page_url))
return self.menu(phone_id, line, "Home Phone Apps", items,
                 "Choose an app; line " + line)
```

The controller removes stale references when sessions end or are replaced.
Its housekeeping tick calls `voice_display.cleanup()`. The display source
must also be packaged beside the other gateway modules; the current image and
deployment packaging are not changed here. Integration tests must exercise
actual HTTP status/header forwarding, source restriction, cleanup and focus
transitions before a physical pilot. Any optional initial page push remains
owned by the parent controller and uses a fresh `should_present` check plus
the existing `execute(phone, ref.page_url, priority=2)` safeguard. Do not send
one push per camera frame.

## Local validation and dependency coverage

Pillow is required for image conversion, the default voice avatars and the
offline example above. Run the full display suite with Pillow installed:

```sh
python3 -c 'from PIL import Image' && python3 -m unittest discover -s tests -p 'test_phone_voice_display.py' -v
```

Image-dependent tests skip themselves when Pillow is unavailable; the
standard-library control tests can still run. Existing CI without Pillow can
therefore pass while skipping the image suite. Such a result does not validate
the image path or complete this prototype's local acceptance. Require a full
run with Pillow and inspect the skip summary before proceeding to a physical
pilot. Successful local tests still do not establish device rendering,
refresh timing, microphone state or call-screen arbitration.

## Physical acceptance still required

Use one explicitly selected phone after the parent integration is reviewed.
Keep private addresses, device identities, room labels, camera endpoints and
credentials under `.local/`; the example configuration is synthetic.

- Verify all five state labels, title/prompt clipping, Stop and Exit, XML
  escaping, exact PNG size, aspect ratio, color rendering and visible capture
  time on the installed SIP firmware.
- Measure refresh at 5 seconds before trying 3 or 2. Record page latency,
  camera age, failures, phone responsiveness and memory/resource behavior.
  Demonstrate no growing request queue or retained-frame history.
- Exercise Stop, duplicate Stop, replacement, timeout, offline camera, lost
  network, controller restart and callback failure. Confirm stale URLs cannot
  reach a replacement session and terminal pages do not automatically refresh.
- While displaying a page, test incoming, outgoing, active and held calls on
  both lines, ringing alerts and each radio path. Verify the native call UI
  stays usable and no assistant audio enters a call. A successful HTTP result
  alone does not establish that screen focus was preserved.
- After preemption and explicit Stop, verify reconnect and end-of-call do not
  resume a stale page or listening session. Test Exit/navigation behavior with
  a refresh already pending. Observe the last visible page after TTL expiry,
  after refresh stops and after network loss; record whether it remains on
  screen and how the user exits it. Do not treat server cleanup as proof of
  screen dismissal.
- Only after the one-phone gate passes, measure bounded multi-phone load and
  coordinated audio ownership. Record actual observations rather than claiming
  a ten-phone rate from a single synthetic test.

Camera refresh is a sequence of still pictures. It provides neither native
SIP video nor synchronization with call audio. A SIP-only video source would
need a separately reviewed receiver/decoder that supplies bounded stills to
this service.
