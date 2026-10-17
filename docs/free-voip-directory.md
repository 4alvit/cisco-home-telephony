# Free VoIP services and practice directory

Provider information checked on **2026-10-17**. This directory distinguishes a
published service address from a call that has actually passed an audio test.
The current deployment evidence and configuration procedure are in
[External SIP](external-sip.md). Account names, SIP identities, credentials,
assigned numbers and room assignments belong in private configuration.

## Short codes and useful public destinations

- **802 — SIP2SIP echo:** `4444@sip2sip.info`. This is the provider's documented
  microphone test. Speak briefly and listen for your own voice. A previous
  synthetic call answered and exchanged RTP in both directions, but its
  receive-loss estimate increased; that pilot did not establish clean audio.
- **803 — The Test Call through Linphone:** `thetestcall@sip.linphone.org`.
  This independent operator publishes an interactive phone-test menu. It is
  a verified published peer, not a Linphone-operated echo service. Configure
  the Linphone profile's fixed `target` as `thetestcall`; provisioning alone
  does not establish reachability or acceptable audio.
- **801 — Callcentric test, currently withheld:** `17771234567` is the fixed
  test destination for an eligible Callcentric account. The reviewed account
  still has an outbound risk-review block, so leave its `target` as `null` and
  do not advertise 801 as usable.

SIP2SIP also documents **`3333@sip2sip.info`** for audio/video testing. The current
provider profile has one fixed target, so selecting `3333` would replace the
destination of 802. It does not create another short code. No additional short
code is assigned here, and this project does not claim Cisco video support.

Sources: [SIP2SIP test services](https://sip2sip.info/help/),
[The Test Call operator's directory](https://thetestcall.blogspot.com/), and
[Callcentric's Asterisk guide](https://www.callcentric.com/support/device/asterisk/17_pjsip).

The numbers above are local aliases chosen by this project. They are not
globally dialable numbers. Each enabled alias reaches one reviewed SIP
destination; it does not enable arbitrary international dialing. A null target
retains registration without creating an outbound alias. Check the generated
dialplan and the latest [deployment acceptance](external-sip.md#deployment-and-acceptance)
before using the alias.

## Using the 803 menu

The operator's directory was updated on **2026-07-20** and publishes support for
PCMU, PCMA and G.722. After the service answers:

- Press **1** to record and replay your speech; press **#** to finish recording.
- Press **2** to enter a sequence of digits, then **#** to hear them read back.
- Press **3** for live echo; **\*** skips its introduction and **#** returns to
  the menu.
- Press **4** for hold-music samples; **#** advances to the next sample.
- Press **7** to hear caller identification, or **8** for immediate digit
  readback.
- Enter **\*1001** for repeating UTC time announcements.

These functions can help with listening to prompts, practicing spoken numbers
or comparing a phrase with its replay. They do **not** evaluate pronunciation,
correct grammar or provide a language teacher. Speech used for replay is sent
to this external service. The operator also exposes a public conference and a
feedback-message option; neither belongs in an unattended connectivity test.
[Published menu and SIP address](https://thetestcall.blogspot.com/).

For a practical self-check, use a short invented phrase, replay it, then compare
normal handset audio with speakerphone audio. Use the local 600 echo service
first if the goal is to diagnose the telephone without an external provider.
An echo test accepts any spoken language; it does not measure language ability.

## What is free, and what number do you receive?

**A SIP identity is an Internet address. A DID is a telephone number assigned by
a carrier for incoming calls from ordinary telephone networks.** Registration
of a SIP account proves neither DID ownership nor successful media delivery.
Likewise, a provider being based in a country does not assign a number from that
country.

- **SIP2SIP:** a free SIP account supports Internet communications. The official
  help page separately requires funded credit for PSTN calling. No assigned
  PSTN DID was verified for the reviewed account.
  [Service](https://sip2sip.info/), [settings](https://sip2sip.info/settings/),
  [PSTN funding boundary](https://sip2sip.info/help/).
- **Linphone's community service:** free SIP accounts interoperate with SIP
  devices. The official FAQ explicitly says these accounts cannot be associated
  with a virtual number; professional or third-party carrier services are
  separate. The reviewed identity is therefore a SIP address, not a French or
  other national telephone number.
  [Linphone account and virtual-number FAQ](https://www.linphone.org/en/faq/).
- **Callcentric IP Freedom:** $0 setup and $0 monthly service for Callcentric
  and compatible Internet SIP calls. Traditional landline/mobile calling and
  incoming-number products are separate. A fresh read-only account review
  showed only IP Freedom and no incoming-number product. Although an extension
  was registered, the risk alert still blocked outbound calls, account changes
  and incoming/outgoing SMS. The provider requests a support review; no support
  message or paid order was sent.
  [IP Freedom](https://www.callcentric.com/rate/plans/ip_freedom/),
  [separate carrier products](https://www.callcentric.com/products/).

**No assigned PSTN DID or DID country has been verified for these three
integrations.** This is an account-audit result, not a claim about every old
account the operator might hold. In particular, a pre-existing Google Voice or
sipgate number would need its own account review. Public test-service numbers
and shared SIP Broker access numbers belong to their operators, not to this
installation.

Incoming SIP calls can be possible without a DID, but accepting them still
requires a reviewed provider-identification policy and an explicit local route.
Registration does not automatically enable incoming calls to the house. The
current incoming configuration remains disabled. Any future ring group should
be generated from the private inventory, with exclusions applied to the whole
physical telephone so its second line does not defeat the exclusion. Do not
publish the real group membership or room labels. Follow the implementation and
source-policy limits in [Incoming-call isolation](external-sip.md#incoming-call-isolation).

## Actual language-learning services

No unlimited, free, public SIP language tutor was verified in this review.
The available phone-test menus offer listening and self-playback exercises,
which are useful but have a different purpose.

- [EnglishCall AI](https://www.englishcall.ai/) advertises actual English
  conversation practice through its telephone and web interfaces. Its free
  allowance is a **five-minute trial**, followed by paid plans. No public SIP
  URI was verified, and calling its PSTN number would also require a suitable
  carrier route. It is not enabled as a free PBX service.
- [ROKITOK](https://rokitok.com/help) describes conversational English lessons
  as free for now, with possible future paid features. No public dial-in number
  or SIP destination suitable for this PBX was verified. A web account and a
  claim of telephone support are insufficient to create a working short code.

For conversations with a real practice partner, agree on a reachable SIP peer
and add only that reviewed destination through private configuration. Do not
copy obsolete language-exchange numbers from an old directory into the public
dialplan or advertise a paid trial as a permanently free service.

## Previously used providers: current position

### SIPNET

SIPNET's [free-calling FAQ](https://www.sipnet.ru/faq/oplata/besplatnye-zvonki)
still says calls between SIPNET SIP IDs are unmetered. It also currently displays
a restoration notice warning that registration and calling may be unavailable.
This does not establish a completely free new account, DID or PSTN plan.
An existing account needs a review of its current contract and eligibility.
No SIPNET profile is enabled here. Use its
[official connection settings](https://wikijs.sipnet.ru/ru/home/Connection_recommendations)
if that review succeeds; preserve the full SIP ID, including leading zeroes.

### Free World Dialup

The [current FWD service](https://www.fwdialup.com/how-it-works) has a signup,
identity-verification and number-assignment process. The legacy
`account2.freeworlddialup.com` hostname failed DNS resolution during this check;
do not assume an old account migrated. In the reviewed new-account flow,
verification opened a **Verification Fee** checkout. No payment or identity
document was supplied, and calling was not activated. A fully free usable path
therefore remains unverified. Its [codec guidance](https://www.fwdialup.com/hd-voice)
also needs review before assuming compatibility with the Cisco handsets.

### IPKall

The operator's [archived discontinuation notice](https://web.archive.org/web/20160304001939id_/http://www.ipkall.com/)
announced closure on **2016-05-01**. The historical login hostname also failed
DNS resolution in this review. The former Washington-number offer is not a
current signup path, and an old number must not be treated as still assigned.

### MySIPSwitch and SIPSorcery

The developer's [historical explanation](https://sipsorcery.wordpress.com/2009/07/14/incoming-call-processing/)
identifies MySIPSwitch as SIPSorcery's predecessor. SIPSorcery's
[current pricing page](https://www.sipsorcery.com/mainsite/Home/Pricing) says it
is not accepting new users. Its [migration notice](https://www.sipsorcery.com/mainsite)
schedules the existing-account cutover for **2026-11-01 at 14:00 UTC**; imported
provider registrations start disabled, and some old dialplan helpers, including
Google Voice, are unsupported. Existing-account owners need to review their
migration. SIPSorcery's [FAQ](https://www.sipsorcery.com/mainsite/Home/FAQ)
explicitly excludes PSTN service, DIDs, free country calling and Google Voice
support. It is a routing option, not the missing free carrier.

### SIP Broker

The [operator wiki](https://faq.sipbroker.com/tiki-index.php) describes a free SIP
interconnect that does not require signup to call. Its
[member registration](https://www.sipbroker.com/sipbroker/action/memberRegister)
expects an existing SIP URI. Registration does not issue a carrier DID. Shared
PSTN access numbers may incur the caller's normal telephone charges. The site
and directory remain available, but individual legacy destinations and test
codes were not call-tested here. No unrestricted SIP Broker dial-through route
is enabled; a future integration should allow only explicitly reviewed peers.

### FreeDigits

The historical `freedigits.com` hostname failed DNS resolution on the review
date. No current authoritative service contract, signup or closure date was
verified. Treat it as unavailable for this deployment rather than claiming an
exact shutdown date or reusing an old Iowa number.

### sipgate Germany

The official [starter-plan notice](https://help.sipgate.de/cloud-telefonanlage/accountverwaltung/vertrage-rechnungen-und-preise/was-sind-die-anrufkosten-fuer-sipgate-starter)
says the free starter plan has not been offered to new customers since
**2022-09-08**. Existing customers retain their plan conditions, with published
charges for ordinary outgoing calls. An old account may still be relevant, but
its assigned number and continuing terms require an authenticated account
review. A [temporary trial](https://help.sipgate.de/cloud-telefonanlage/erste-schritte/readme/wie-funktioniert-die-kostenlose-testphase)
is not a permanent free German DID for a new installation.

### Google Voice

Personal Google Voice offers many free US/Canada calls through its supported
interfaces, with exceptions and international rates in the
[official cost guidance](https://support.google.com/voice/answer/115061?hl=en).
This does not supply SIP credentials for a free Asterisk trunk. The supported
[SIP Link product](https://knowledge.workspace.google.com/admin/voice/sip-link-requirements)
requires a qualifying paid Voice edition and certified session border
controllers, plus an existing carrier arrangement. The
[supported desk-phone requirements](https://knowledge.workspace.google.com/admin/voice/desk-phone-requirements-for-voice)
do not turn these Cisco 7945G phones into supported Google Voice endpoints.
No free direct Google Voice-to-Asterisk integration was verified or enabled.

## Acceptance and maintenance

Keep the following checks separate:

1. **Published:** the operator still lists the exact URI and its purpose.
2. **Configured:** the intended fixed alias exists, the account registers, and
   credentials remain outside Git.
3. **Reachable:** a bounded authorized call answers and exchanges media in both
   directions without calling a physical household phone unexpectedly.
4. **Usable:** a listening test confirms intelligible audio and expected DTMF;
   hangup leaves no owned channels. Increasing loss remains an open issue even
   when the call answered.

For configuration, source ACLs, DNS/NAT and deployment commands, use
[External SIP](external-sip.md). For the local phone command-line client, use
[CLI installation](cli-installation.md) and the [command reference](cli.md).
Keep the latest account restrictions and tests in private audit files, and
update only sanitized outcomes in public documentation. Never publish a full
provider dashboard, authentication output or generated PJSIP secret file.
