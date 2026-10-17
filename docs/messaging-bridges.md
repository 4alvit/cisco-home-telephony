# Telegram, WhatsApp and Viber voice bridges

Research checked on **2026-10-17**. None of these bridges is deployed or logged
into an account. A messaging bot, an audio-file message, and a live voice-call
bridge are different capabilities.

## Telegram: a feasible experimental bridge

Telegram's [call method](https://core.telegram.org/method/phone.requestCall) is
available to user accounts. The [Bot API](https://core.telegram.org/bots/api)
does not provide an equivalent private-call media bridge. The design needs a
user session, Telegram call signaling and media, and a SIP/RTP endpoint.
The official [call flow](https://core.telegram.org/api/calls) describes the
signaling and encryption boundary.

The concrete candidate is
[`foobar26/tg2sip` at `fab2613`](https://github.com/foobar26/tg2sip/tree/fab26138aabe2208f7fc2cc97dc245debb522a68).
It implements both directions with PJSUA2, Pyrogram and NTgCalls, including a
mapping from numeric Telegram caller IDs to SIP destinations. Its author's
compatibility reports are upstream claims, not tests on this home system.
The [example configuration](https://github.com/foobar26/tg2sip/blob/fab26138aabe2208f7fc2cc97dc245debb522a68/config.example.yaml)
is a starting point for review, not a configuration to copy into production.

The candidate pins Pyrogram 2.0.106, PJSIP 2.14.1 and NTgCalls 3.0.0.
[Pyrogram is archived](https://github.com/pyrogram/pyrogram), while
[NTgCalls continues development](https://github.com/pytgcalls/ntgcalls).
The older [Infactum/tg2sip](https://github.com/Infactum/tg2sip) is archived and
uses the legacy call stack. Neither project should be treated as a supported
drop-in telephone provider.

For a pilot, use a dedicated account and the operator's own
[Telegram application credentials](https://core.telegram.org/api/obtaining_api_id).
Keep its session and second-factor setup private. Explicitly select the account,
permitted contacts and call directions before login. Disable automatic Telegram
notification messages and contact import in the candidate.

Proposed routing:

- An internal alias maps to one approved numeric Telegram user ID.
- An approved incoming Telegram caller maps to one configured internal line.
- Unknown callers and arbitrary destinations are rejected; the bridge has no
  PSTN access and does not inherit the whole household dialplan.
- The bridge runs separately from Asterisk and HA, with one replica, one call
  initially, private session storage and a pinned image. Its host-network
  assumption and negotiated media traffic require an isolated network test.

The bridge decodes Telegram audio before sending it over SIP. Consequently,
Telegram encryption ends at the bridge; the complete mixed call must not be
described as end-to-end encrypted. No recording is needed for operation.

Acceptance must cover both directions with current Telegram clients: speech,
answer, reject, busy, caller cancellation, hangup, reconnect, revoked sessions
and zero abandoned PBX channels. DTMF needs a separate test.

## WhatsApp: official business SIP integration

An official business route exists. Infobip documents
[incoming WhatsApp calls to SIP and outgoing SIP calls to WhatsApp](https://www.infobip.com/docs/whatsapp/whatsapp-business-calling/supported-calling-scenarios).
It requires an eligible business sender and permission from the called user.
Its [outbound availability page](https://www.infobip.com/docs/whatsapp/whatsapp-business-calling/business-initiated-calling)
currently excludes US-based business senders; check eligibility for the exact
provider and account before planning a deployment.

[SIPhon's implementation example](https://siphon-sip.org/cookbook/whatsapp-calling/)
uses SIP over TLS and Opus over SRTP, with an internal SIP leg. A possible home
design would place a separate SBC at that boundary and transcode for the Cisco
handsets. It would use authenticated signaling, a trusted certificate and a
restricted internal destination. WhatsApp business calls must not break out to
the PSTN under the documented service restrictions.

This is not a SIP login for a personal WhatsApp account. No fully free home
solution, outbound tariff or account eligibility was verified. Direct Meta
documentation returned HTTP 429 during this review, so the implementation and
provider documentation above are identified explicitly. Recheck
[Meta's calling documentation](https://developers.facebook.com/documentation/business-messaging/whatsapp/calling)
before implementation. No business subscription or account migration was made.

## Viber: business voice exists; the bot API is messaging

The [public Viber Bot API](https://developers.viber.com/docs/api/rest-bot-api/)
documents messages and callbacks, not a personal live-call audio interface.
New bots have required commercial terms since February 5, 2024.

Viber also offers a separate Business Calls product. Its
[official brochure](https://www.forbusiness.viber.com/business-calls.pdf)
describes integrating a business's VoIP infrastructure and a call button in its
business account. The [partner directory](https://www.forbusiness.viber.com/en/messaging-partners/)
lists that product separately. Infobip's
[Calls API](https://www.infobip.com/docs/voice-and-video/calls) and
[routing documentation](https://www.infobip.com/docs/voice-and-video/call-routing)
provide concrete Viber voice integration references.

This is a business-provider option, not a verified free personal-account bridge.
Confirm eligibility, supported call directions and terms for the selected
account first. [Viber Out](https://help.viber.com/hc/en-us/articles/9209069652637-Manage-your-Viber-Out-plans-and-credit)
requires purchased credit or a plan; it does not establish an inbound SIP
service for this PBX.

## Desktop audio-loop alternatives

Routing a desktop messenger's microphone and speaker through virtual audio
devices is technically conceivable. It does not supply a reliable call-control
API, caller mapping or recovery contract. No such arrangement was installed or
accepted here. Keep the normal Mac SIP client independent of these experiments.
