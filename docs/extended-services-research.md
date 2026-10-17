# External calling and extended Cisco phone services

Checked on **2026-10-17** for **CP-7945G running SIP45.9-4-2SR4-3S** and
**Asterisk 22.11.0 with PJSIP**. This is a support and implementation reference,
not a claim that every feature below has passed a physical-phone test. Deployment
acceptance belongs in [server-validation.md](server-validation.md); the existing
authenticated CGI and SSH procedures are in [phone-control.md](phone-control.md).

The disabled provider profiles and exact deployment boundary are documented in
[external-sip.md](external-sip.md). The historical providers supplied by the
operator were rechecked on **2026-10-17** below; a familiar old name does not
establish that its old free plan or login still works.

## Historical account list: current status

### SIPNET — active site, but not a verified free signup

The [free-calling FAQ](https://www.sipnet.ru/faq/oplata/besplatnye-zvonki) still
states that calls between SIPNET SIP IDs are unmetered. That is a statement about
in-network calls, not the total account cost. The current
[registration page and homepage](https://www.sipnet.ru/) show paid plans, identity
verification, and a requirement to acquire a SIPNET number for individual
customers. The page also showed a restoration-work notice warning that
registration and calls may be unavailable. Its footer says the Russian operator
provides service only to subscribers located in the Russian Federation.

Consequently SIPNET is **not included as a ready free profile**. An existing
account's actual plan, eligibility and continuing charges must be checked before
reuse. No signup, payment, identity verification or network workaround was
attempted. Technical settings are documented in the operator's
[connection recommendations](https://wikijs.sipnet.ru/ru/home/Connection_recommendations):
`sipnet.ru`, the full SIP ID, account password, DNS service discovery, and SIP on
5060 UDP/TCP or 5061 TLS. Do not pin a current proxy IP. The
[SIP ID specification](https://wikijs.sipnet.ru/ru/home/%D0%9B%D0%B8%D1%87%D0%BD%D1%8B%D0%B9_%D0%BA%D0%B0%D0%B1%D0%B8%D0%BD%D0%B5%D1%82/SIPID)
describes seven- or ten-digit IDs with significant leading zeroes. A future
profile should allow only a separately verified SIP ID, not unrestricted numeric
destinations. It would still require an account-specific test.

### Free World Dialup — a new 2026 service, different signup

The old `account2.freeworlddialup.com` did not resolve during this check. However,
FWD has announced a return: see founder Jeff Pulver's
[2026-04-23 announcement](https://thejeffpulver.substack.com/p/free-world-dialup-returns)
and the current [FWD site](https://www.fwdialup.com/). Do not infer that an old
account or password was migrated.

The new [signup form](https://www.fwdialup.com/sign-up) requests a full name and
email, then an emailed one-time code. Its [onboarding description](https://www.fwdialup.com/how-it-works)
adds identity verification and number assignment. The
[FAQ](https://www.fwdialup.com/faq) says that SIP credentials, server settings and
codec guidance are available after login. Public pages describe a non-commercial
experimental network, but do not establish a complete permanent $0 price/usage
contract or a free PSTN service. The [mission statement](https://www.fwdialup.com/mission)
requires account verification before calls can originate.

A user-authorized signup was completed on 2026-10-17 through name/email and an
emailed code. The account dashboard issued SIP settings, but its identity
verification action opened a **Verification Fee** checkout; no amount was
visible in the observed checkout. No payment, card details or identity document
were supplied. The account is therefore recorded as created but not verified
for calling. A genuinely free usable calling path has not been demonstrated;
no FWD registration, outbound call or PBX profile was enabled. This observed
activation boundary takes precedence over assuming that the service name or
experimental description guarantees free activation.

FWD advertises AMR-WB with narrowband fallback and
[no server-side wideband/narrowband transcoding](https://www.fwdialup.com/hd-voice).
The 7945G does not gain AMR-WB from this project. Confirm a mutually supported
narrowband codec in the new account's settings; the current Asterisk image does
not claim an AMR-WB transcoder. No FWD profile is generated while the required
verification fee and usable free-call terms remain unresolved. Historical FWD
echo numbers are not assumed to exist on the relaunched network.

### IPKall — discontinued

IPKall's own [preserved homepage from 2016-03-04](https://web.archive.org/web/20160304001939id_/http://www.ipkall.com/)
announces discontinuation effective **2016-05-01**. This archived operator notice
was retrieved and read; the current `phone.ipkall.com/login.asp` hostname did not
resolve. The historical free Washington inbound number is not a current signup
or a reusable trunk. No replacement login was invented.

### MySIPSwitch and SIPSorcery — predecessor and existing-account service

The developer's [2009 migration-era explanation](https://sipsorcery.wordpress.com/2009/07/14/incoming-call-processing/)
describes MySIPSwitch as the predecessor of SIPSorcery. The old MySIPSwitch site
did not provide a usable current signup during this check. Use the successor's
current account process rather than an old tutorial.

SIPSorcery's [current pricing page](https://www.sipsorcery.com/mainsite/Home/Pricing)
says it is **not accepting new users**. Its
[operator announcement](https://www.sipsorcery.com/mainsite) schedules migration
of existing accounts for **2026-11-01 at 14:00 UTC**. Accounts, SIP credentials,
providers and dialplans were copied, but provider registrations on the new
platform start disabled. The operator identifies `do.sipsorcery.com` for testing
the new platform before the cutover; retain DNS names rather than hardcoding
either server IP. Suspended and some duplicate-email accounts were not migrated.

The new dialplan engine does not support the old Google Voice bridge or the
listed legacy database/web helpers. Its
[FAQ](https://www.sipsorcery.com/mainsite/Home/FAQ) explicitly says it does not
provide PSTN origination/termination, a DID, free calls to countries, or Google
Voice support. An existing account may still be useful for SIP routing, but its
actual plan must be reviewed. It cannot supply the missing free PSTN carrier.

### SIP Broker — available directory/interconnect, no account needed to call

The [operator wiki](https://faq.sipbroker.com/tiki-index.php) describes a free SIP
interconnect requiring no signup. Its
[member form](https://www.sipbroker.com/sipbroker/action/memberRegister) is still
available, but requires an **existing SIP URI**, a separate SIP Broker password,
and an email confirmation. Membership does not create a standalone SIP account
or private incoming telephone number.

The [operator's Asterisk example](https://faq.sipbroker.com/Asterisk+Configuration)
uses `sipbroker.com`, leaves registration blank, and lists `*011188888` as its
test announcement. Those examples use obsolete chan_sip syntax and broad
fallback routes; do not copy them into this PJSIP system. A future implementation
can expose one fixed test route on a separate outbound-only endpoint, retaining
the same no-relay restrictions as the current profiles. It must not import the
example's automatic fallback to a paid carrier. No live test was made, so an
available page is not proof of current media reachability. Public PSTN access
numbers are shared dial-in gateways; the caller's telephone operator may charge.

### FreeDigits — no verified current service

`freedigits.com` did not resolve during the bounded check, and no current
operator signup, support contract or authoritative shutdown notice was found.
Classify the historical Iowa-number service as **unavailable/unverified**, not
as a working free option. No exact closure date or successor is claimed. Do not
send old credentials to a similarly named service.

### sipgate Germany — legacy free base plan, paid new service

The official [starter pricing explanation](https://help.sipgate.de/cloud-telefonanlage/accountverwaltung/vertrage-rechnungen-und-preise/was-sind-die-anrufkosten-fuer-sipgate-starter)
states that new starter signups ended on **2022-09-08**. Existing starter/basic
customers can retain a plan without a monthly base fee, but calls and additional
services may be charged. The current
[service description](https://www.sipgate.de/leistungsbeschreibung) explicitly
marks starter as a legacy contract. Inspect an old account's actual contract
before considering its existing number; do not create a paid replacement.

New customers are offered a
[30-day trial](https://help.sipgate.de/cloud-telefonanlage/erste-schritte/readme/wie-funktioniert-die-kostenlose-testphase)
with temporary numbers and limited included minutes. It ends automatically and
its configuration is deleted on expiry. This is not a permanent free household
trunk. New paid plans and identity/location checks must not be represented as
the historical free basic service.

## What can be implemented locally

- **Text notifications:** a Cisco XML service using `CiscoIPPhoneText`, opened
  through authenticated `CGI/Execute`. This uses the LAN, not an SMS network.
- **Pictures:** a `CiscoIPPhoneImageFile` page referencing a PNG. The 7945G
  Services pane is **298 × 156 pixels**, with a 16-bit-color display. This is
  different from the background-image dimensions. PNG can be palettized or RGB;
  serve it as `image/png`. See Cisco's [XML objects and model-specific image
  sizes](https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cuipph/all_models/xsi/7_0/english/programming/guide/70xsi/xsi70obj.html).
- **Dial:** Cisco documents a `Dial:` URI. A selected telephone line must be
  handled separately; the suffix after the number is **not a line selector**.
- **Answer and speaker control:** emulate a documented physical key only after
  confirming the relevant call and UI state. These are button presses, not an
  idempotent “set speaker on” API.
- **Hang up:** target the exact Asterisk channel and its previously observed
  unique ID. A server-side AMI hangup is preferable to guessing a softkey.
- **DND:** implement an explicit **PBX DND** preference in local routing. Native
  phone DND is a separate feature and must not be reported as synchronized unless
  separately observed.
- **Automatic answering:** use an explicit opt-in local controller, or provision
  a line's documented auto-answer settings. A controller pressing a ringing line
  key is UI automation; it is not the firmware's persistent auto-answer setting.

The command syntax and limits are detailed below. These capabilities need no
external calling account. Account registration is required only for external
SIP/PSTN services.

## Free SIP accounts, with the actual cost boundary

The countries below identify the provider/operator, not a promise of a free
national telephone number. A SIP address is not a PSTN number. No ongoing,
general-purpose **free PSTN trunk for this Asterisk deployment** was verified.
Published offers were checked, then user-authorized SIP2SIP, Callcentric and
Linphone accounts were created. Signup is separate from SIP registration and
voice acceptance; current account restrictions are recorded below. No paid plan,
card, telephone number or PSTN credit was ordered.

### United States: Callcentric IP Freedom

The [IP Freedom plan](https://www.callcentric.com/rate/plans/ip_freedom/) is
$0 setup and $0/month, requires no credit card, and supports free calls within
Callcentric and compatible SIP networks. It explicitly excludes calls to normal
landline/mobile numbers. It is a concrete option for an external SIP pilot.

The account created on 2026-10-17 shows IP Freedom at $0 and no payment card.
Its dashboard also reports a risk-review restriction: outbound calling and
account changes are blocked, and inbound/outbound SMS is disabled. It requests
additional account details through support; no existing ticket or specific
document request was present. No support message was sent. Stage registration
only with `target: null`; keep route 801 absent until the provider clears the
restriction. The [signup FAQ](https://www.callcentric.com/faq/29/213) identifies
the initial signup password as the initial SIP password. The deployed account
subsequently reached Registered without changing that credential. This does not
remove the provider's outbound restriction or establish working audio.

Callcentric's [PJSIP configuration guide](https://www.callcentric.com/support/device/asterisk/17_pjsip)
supplies an Asterisk-compatible contract: registration server
`sip:sip.callcentric.net`, account ID beginning `1777`, the account extension's
**SIP password**, UDP transport and G.711 µ-law. A non-default provider extension
uses the account ID plus that extension, not the extension alone. The guide is
for Asterisk 17; apply the equivalent objects to 22, rather than replacing this
repository's entire configuration. Validate its current inbound source ranges
and route inbound calls to an isolated context without an outbound trunk path.

For PSTN, [Pay Per Call](https://www.callcentric.com/products/) has no plan
monthly/setup fee but charges per minute; additional E911 fees apply to US/Canada
customers. The same page lists North America Basic at $1.95/month plus $1.50
setup, including E911 and 120 minutes to the US, Canada and Puerto Rico. Taxes
are additional. The residential Dollar Unlimited inbound number is $1/month
in selected US states; this is not a free DID. Check destination-specific rates
before enabling external dialing.

### Netherlands: SIP2SIP / AG Projects

[SIP2SIP](https://sip2sip.info/) offers free SIP accounts and Internet audio,
video and messaging. Its [settings contract](https://sip2sip.info/settings/)
uses the chosen username/password and realm `sip2sip.info`. Registration is
needed for inbound calls. Use RFC 3263 NAPTR/SRV discovery; the provider explicitly
warns against hardcoding its current proxy IP or transport. Confirm the built
Asterisk resolver supports the selected discovery path before deployment.

The [help page](https://sip2sip.info/help/) lists `3333@sip2sip.info` for media
testing and `4444@sip2sip.info` for echo. PSTN requires paid credit; its published
funding process requires a bank transfer **greater than 20 USD/EUR**. Free signup
therefore does not provide free arbitrary PSTN minutes. No payment or support
email was sent.

The account was created and its settings email received on 2026-10-17. The
deployed Asterisk subsequently reached Registered using the issued bare username
and domain. After the user resumed testing, a bounded synthetic 802 call to
`4444@sip2sip.info` answered and showed increasing RTP counts in both directions,
with complete channel cleanup afterward. No physical phone was called and no
audio was recorded. The local receive-loss estimate (`lp`) increased from 42 to
91, so connectivity passed but clean audio quality has **not** been established.
The final RTT was 0.15451 seconds (154.51 ms), a delay measurement rather than a
loss percentage. See the [pilot evidence and Asterisk source interpretation](external-sip.md#observed-sip2sip-pilot-and-quality-limit)
before treating a successful call as media-quality acceptance.

### France: Linphone / Belledonne Communications

The [French operator](https://www.linphone.org/en/belledonne-communications-company-overview/)
offers an individual free community service at `sip.linphone.org`. Its
[FAQ](https://www.linphone.org/en/faq/) confirms SIP addresses, independent SIP
passwords, compatibility with third-party SIP hardware/software, and email or
phone validation at signup. It explicitly says the free account cannot be
associated with a virtual telephone number. This is another SIP-to-SIP option,
not a free PSTN trunk. Use the current account settings supplied after signup and
verify PJSIP registration and two-way audio before enabling a call route.
Email activation completed on 2026-10-17 and the account dashboard displays the
assigned SIP address. The deployed account reached Registered with a null
target. No Linphone-operated echo/test URI was verified. The independent
[The Test Call operator's page](https://thetestcall.blogspot.com/), updated
2026-07-20, publishes `sip:thetestcall@sip.linphone.org`, supports PCMU, PCMA and
G.722, and lists real-time echo on menu option 3. This provides a concrete
candidate for a later explicitly reviewed test, not a Linphone-operated service
or a verified live result. No call to that peer was made and it has not been
configured; route 803 remains absent. Do not substitute a guessed echo address
or assume that a published test address guarantees availability or audio quality.

### How to connect a provider without changing the phones

Keep the Cisco lines registered to local Asterisk. Add a provider-specific
PJSIP registration/auth/endpoint/AOR and an isolated inbound context, then expose
only chosen outbound destinations through the local dialplan. Store provider
credentials in private deployment input/Kubernetes Secrets. Preserve the current
LAN phone/firewall rules; the selected provider may require an additional,
separately scoped SIP/RTP policy.

Start with one free SIP account and an explicit test destination. Verify
registration, caller identity, incoming routing, two-way RTP and hangup. Only
enable PSTN after selecting a paid plan and destination allowlist. A successful
registration alone proves neither media nor free calling. Do not copy a vendor's
example unrestricted `_X.` route into an Internet-reachable inbound context.

## Google Voice: what is and is not a supported route

Google's [personal Voice setup guide](https://support.google.com/voice/answer/115061?hl=en)
still describes calls through its browser/mobile applications. Almost all calls
from the US to US/Canadian numbers are free; some destinations cost $0.01/minute,
and international calls use published rates. Account and number verification
are part of the signup flow.

No documented, supported consumer Google Voice SIP credentials or call-control
API suitable for registering this Asterisk server were found. The official
[desk-phone list](https://knowledge.workspace.google.com/admin/voice/desk-phone-requirements-for-voice)
contains supported Poly devices, not Cisco 7945G or generic Asterisk. Do not
present an old XMPP, scraped-web-session or OBi tutorial as a supported new
installation. HP's [OBiTALK end-of-service notice](https://support.hp.com/in-en/document/ish_10969583-11049883-16)
documents closure of the consumer portal on October 31, 2024: new onboarding and
Google Voice configuration through that portal are unavailable. An existing
legacy device is not a reproducible new bridge.

The [current Google subscription comparison](https://knowledge.workspace.google.com/admin/voice/compare-voice-subscriptions)
was updated October 13, 2026. Paid standalone Gmail-account plans now include
Starter at $10/month and Standard at $20/month. Do not confuse these with the
free personal service. Workspace add-on Standard is $20/user/month and Premier
is $30/user/month; **SIP Link is available on these Workspace tiers**, not the
standalone plans. [SIP Link requirements](https://knowledge.workspace.google.com/admin/voice/sip-link-requirements)
include a certified SBC, existing carrier service, TLS signaling and encrypted
media. Stock Asterisk is not listed as a certified SBC. This is a paid enterprise
integration path, not free outbound calling for the home PBX.

Google's [linked-number guide](https://support.google.com/voice/answer/165221?co=GENIE.Platform%3DDesktop&hl=en-GB)
also says forwarding to automated systems is unsupported. A forwarding scheme
through a purchased DID should not be advertised as an officially supported
Google Voice-to-Asterisk IVR integration.

## Cisco XML: implementable, model-specific contracts

### Open a short-lived text or image page

Serve XML on the private phone service endpoint and POST a URL-encoded `XML`
form field to the phone's `/CGI/Execute`, authenticated through its configured
callback. Use the existing [remote-control authentication](phone-control.md)
rather than exposing an unauthenticated service. A minimal text page is:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<CiscoIPPhoneText>
  <Title>Home notification</Title>
  <Prompt>Press Exit to return</Prompt>
  <Text>The garage door is open.</Text>
  <SoftKeyItem>
    <Name>Exit</Name><URL>SoftKey:Exit</URL><Position>1</Position>
  </SoftKeyItem>
</CiscoIPPhoneText>
```

Build XML with an XML library; never interpolate unescaped message text. Start
with English/ASCII. Arbitrary Unicode rendering depends on the installed phone
locale/fonts and is not established by an XML UTF-8 declaration. Custom softkeys
replace defaults, so retain an explicit Exit action.

The corresponding image page uses a server-generated, bounded PNG:

```xml
<CiscoIPPhoneImageFile>
  <Title>Home status</Title>
  <Prompt>Press Exit to return</Prompt>
  <LocationX>-1</LocationX><LocationY>-1</LocationY>
  <URL>http://192.168.50.10:18080/content/SHORT_LIVED_NONCE.png</URL>
  <SoftKeyItem>
    <Name>Exit</Name><URL>SoftKey:Exit</URL><Position>1</Position>
  </SoftKeyItem>
</CiscoIPPhoneImageFile>
```

The address is synthetic. `-1,-1` centers the image. Resize to fit **298 × 156**,
remove transparency/interlacing, and serve only to the intended phone. A PNG's
encoded RGB depth need not be “16 bits per channel”; 16 bits describes the phone's
display. The model-specific limits and centering are in Cisco's
[9.1 developer guide](https://www.cisco.com/en/US/docs/voice_ip_comm/cuipph/all_models/xsi/9_1_1/xsidevguide911.pdf),
chapters 3 and 5. Cisco's [image-format note](https://community.cisco.com/t5/collaboration-knowledge-base/image-format-for-7945-7965-phones/ta-p/3633019)
also discusses simpler PNG encoding for these models.

Request an idle-only display with:

```xml
<CiscoIPPhoneExecute>
  <ExecuteItem Priority="2" URL="http://192.168.50.10:18080/content/SHORT_LIVED_NONCE.xml"/>
</CiscoIPPhoneExecute>
```

**Priority applies only to HTTP URLs. Internal `Key:`, `Dial:` and `Play:` URIs
execute immediately regardless of Priority.** Priority 2 can discard a busy-phone
request; a successful CGI response is not proof that a person saw the message.
Keep one ExecuteItem per request and check the response's item status, then use
an observed screenshot for attended acceptance.

### Pictures, sprites and SMS are different features

The phone can display a rendered picture; no native SVG, APNG or sprite-sheet
animation contract was verified for this firmware. For a changing dashboard,
compose a PNG on the server and use a bounded sequence of XML pages. Cisco's
[HTTP header guide](https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cuipph/all_models/xsi/7_0/english/programming/guide/70xsi/xsi70htp.html)
supports `Refresh: 3; url=...` in seconds. This supports a slideshow/status refresh,
not a promise of smooth video or a particular frame rate. Stop refreshes after a
short TTL and provide Exit. A wallpaper remains a separate TFTP/menu operation.

No native cellular SMS/MMS client was verified on the 7945G. A useful bridge is:
provider SMS webhook/API → validated local event → Cisco text/image page. For
example, [Twilio documents inbound message webhooks](https://www.twilio.com/docs/messaging/guides/webhook-request)
with `MessageSid`, `From`, `To`, `Body` and media metadata, and
[signature validation](https://www.twilio.com/docs/usage/webhooks/webhooks-security).
That optional bridge needs its own provider account, telephone number, pricing
selection and credential. Do not publish the private phone control listener as
the provider webhook. Deduplicate provider message IDs and constrain any media
fetching before converting images. No provider SMS bridge is claimed deployed
by this document.

For a concrete cost comparison, Callcentric's [current SMS plans](https://www.callcentric.com/products/)
list $1/month plus $0.010/message for Basic, or $2/month plus $0.008/message for
Pro. A phone's SIP registration does not automatically create SMS support.

### Dial, answer, hangup and DND

The 9.1 guide's exact syntax is:

```text
Dial:{dialSequence}[:{useAppUI}:{applicationId}[:audibleFeedback]]
```

`useAppUI` is Boolean 0/1. **`Dial:600:2` does not select line 2.** To request a
specific line, use a separately verified line-selection workflow or Asterisk
Originate targeting that line. Originate rings the phone first, then connects the
destination after answer; describe this behavior explicitly. A direct `Dial:600`
is an immediate phone action.

Cisco's [internal URI reference](https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cuipph/all_models/xsi/7_0/english/programming/guide/70xsi/xsi70uri.html)
documents `Key:Line1`, `Key:Line2`, `Key:Speaker` and `Key:Soft1` through the
physical softkeys. Use a fresh ringing-state observation before a line/Answer
key. `Key:Speaker` toggles state; no generic `Key:Answer` or `Key:DND` appears in
this model's documented key set. `SoftKey:Exit` is valid in a page's softkey
binding but **SoftKey URIs are not allowed inside Execute**.

For Asterisk PJSIP, distinguish `Ring` from `Ringing`: an incoming INVITE **from
the phone** creates state `Ring`, while a received 180 response **from the called
phone** sets `Ringing`. An answer controller must not interpret the user's
outgoing-call `Ring` state as a call waiting to be answered. See the
[22.11.0 channel source](https://github.com/asterisk/asterisk/blob/22.11.0/channels/chan_pjsip.c).
Revalidate the exact channel/unique ID immediately before a call action and avoid
blind retries after an ambiguous timeout.

Native DND is a configurable phone softkey: the
[7900 softkey definitions](https://usecallmanager.nz/soft-keys.html) include DND
in on-hook and ringing sets. Its position depends on the template. Pressing a
known visible softkey is possible UI automation; setting an Asterisk database
flag is **PBX DND**, which should affect local call routing but does not prove the
phone displays DND. CUCM UDS/AXL examples are CUCM-server APIs, not APIs supplied
by this Asterisk installation.

### Speakerphone auto-answer

The maintained [SEP configuration reference](https://usecallmanager.nz/sepmac-cnf-xml.html)
documents the following inside the selected `sipLines/line`:

```xml
<autoAnswer>
  <autoAnswerEnabled>1</autoAnswerEnabled>
  <autoAnswerMode>Auto Answer with Speakerphone</autoAnswerMode>
</autoAnswer>
```

`autoAnswerEnabled=0` disables it. `sipProfile/autoAnswerTimer` sets the delay in
seconds; `autoAnswerAltBehavior=true` prevents automatic answering while any
line already has a call. Treat these as provisioned settings requiring phone
acceptance, not an already implemented runtime toggle. Auto-answer enables the
room microphone as part of a call; keep it explicitly opt-in and visibly named.

For selective per-call auto-answer, the
[usecallmanager dialplan reference](https://usecallmanager.nz/dialplan-extensions.html#cisco_autoanswer)
uses `CISCO_AUTOANSWER`. Its [actual Asterisk patch](https://raw.githubusercontent.com/usecallmanagernz/patches/master/asterisk/cisco-usecallmanager-22.11.0.patch)
sends a proprietary REFER after ringing, with MIME type
`application/x-cisco-remotecc-request+xml` and an
`x-cisco-remotecc-request/answercallreq/dialogid` body containing the call ID and
both dialog tags. This is not a generic `Alert-Info` or `answer-after=0` header.
The [patch release page](https://usecallmanager.nz/patching-asterisk.html) labels
its Asterisk 22 port “ready for testing” and reintroduces a modified `chan_sip`.
Setting that variable on stock PJSIP does not implement the feature. Do not
replace the working PJSIP stack merely to claim support for this method.

## Acceptance before claiming an extended feature works

Use one idle phone first. Confirm a text page and bounded PNG visually, including
Exit; confirm a notification requested during an active call does not interrupt
it. For call control, verify the selected line in fresh AMI state, answer only a
call ringing toward that phone, and hang up only the same channel/unique ID.
Check DND by observing the intended routing outcome, and label PBX versus native
phone state correctly. Test automatic answering only after explicitly enabling
it, then confirm disabling it restores ordinary ringing. Test both lines and
busy-call behavior before applying the feature to every phone.
