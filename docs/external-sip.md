# Optional external SIP accounts

The provisioner supports three **disabled-by-default, SIP-network-only** account
profiles. Generated configuration alone does not prove provider registration or
a successful external call. As of **2026-10-17**, SIP2SIP, Callcentric and Linphone
all reached **Registered** on the deployed Asterisk. After the user resumed
testing, a bounded synthetic SIP2SIP 802 call answered and showed increasing RTP
packet counts in both directions, then cleaned up completely. Its receive-loss
counter increased, so this establishes connectivity, **not clean audio quality**;
see the pilot evidence below. A Callcentric IP Freedom account
was created at $0, but its provider risk review currently blocks outbound calls
and account changes. No support ticket was automatically opened and no support
message has been sent. Linphone email activation is complete. The latest
deployment selects the independent **The Test Call** peer for Linphone and
includes **803**, together with the optional **621** incoming group. The latest
synthetic 803 call answered, exchanged RTP in both directions with zero reported
receive loss, and cleaned up completely. Menu/DTMF and human listening checks
remain pending. Callcentric's target
remains null, so **801 remains withheld**. A relaunched FWD account was created,
but calling requires identity verification whose next step presented a
**Verification Fee** checkout; no payment or identity document was supplied, and
no FWD profile is enabled. Account identifiers and credentials remain private.
See the [research and current cost boundaries](extended-services-research.md)
for provider terms and limitations.

SIP2SIP initially showed Rejected/Unregistered around the closed-firewall startup
and retry window. Its issued settings and official instructions confirmed the
configured bare username and separate domain. It subsequently registered
without an authentication or configuration change. Do not treat a transient
registration state alone as evidence of a wrong password, and do not change
credentials or broaden firewall rules without diagnostics.

## Latest deployed-route pilots

Two bounded synthetic calls exercised the final deployed configuration using
Asterisk's `Milliwatt` audio source. Neither called a household handset or
recorded anyone's voice:

- **802 / SIP2SIP:** answered; transmitted packets increased **260 → 510** and
  received packets **214 → 416**. The local receive-loss estimate rose
  **44 → 91**, so the media-quality concern remains open.
- **803 / The Test Call through Linphone:** answered; transmitted packets
  increased **252 → 503** and received packets **222 → 429**. The sampled local
  receive-loss estimate stayed at **zero**.

Each pilot ended with **zero owned channels and zero total channels**. These
measurements confirm answer, bidirectional RTP and cleanup. No person listened,
and the pilots did not navigate the remote menu or verify DTMF responses.
Zero sampled loss is not a guarantee of future call quality.

## Earlier SIP2SIP pilot and quality limit

The synthetic 802 call to the provider's documented `4444@sip2sip.info` echo
service ran on **2026-10-17, 20:49:48–20:50:02 UTC**. Both samples reported the
external channel as `Up`. Over approximately five seconds, transmitted RTP
packets increased **255 → 505** and received packets **213 → 414**. Cleanup left
zero owned channels and zero total channels, and all eight local lines were
available afterward. This pilot called no physical phone and recorded no audio.
The reported channel read/write formats were `alaw`/`ulaw`; these fields are not
a listening test or a complete record of SDP codec negotiation.

The helper reads `CHANNEL(rtcp,all,audio)`. In Asterisk 22.11.0, its **`lp` field
maps to `rxploss`**, the local receive-loss estimate, and **`rlp` maps to
`txploss`**, the remote party's reported loss of packets sent by Asterisk.
These are packet counts, not percentages or latency measurements. See the
[quality-string mapping](https://github.com/asterisk/asterisk/blob/22.11.0/main/rtp_engine.c#L2528-L2556)
and the [native RTP statistics mapping](https://github.com/asterisk/asterisk/blob/22.11.0/res/res_rtp_asterisk.c#L8780-L8785).

Here `lp` increased **42 → 91**: a net increase of 49 in the cumulative
receive-loss estimate. The native RTP engine calculates that estimate from
expected and received sequence counts saved at the latest local RTCP report;
the immediately read `rxcount` can be newer. Late packets can also affect the
estimate. Do not turn these two samples into an exact loss percentage, equate
loss to `txcount - rxcount`, or assign the cause to a particular network device.
The positive, growing estimate is a quality concern requiring investigation.
[Asterisk's loss calculation](https://github.com/asterisk/asterisk/blob/22.11.0/res/res_rtp_asterisk.c#L4450-L4506).

The final `rtt` was **0.15451 seconds (154.51 ms)**. Asterisk documents RTT in
seconds; it is round-trip delay, not one-way delay or loss. `rlp` remained zero,
which alone does not certify a loss-free send path or a fresh remote report.
[RTCPReceived field definitions](https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/AMI_Events/RTCPReceived/).

The acceptance result is therefore **answer + bidirectional RTP + cleanup
passed; media quality remains unaccepted**. A later authorized quality test
should correlate RTCP report times, sequence/SSRC continuity, jitter and scoped
firewall counters, then assess intelligibility in both directions. Preserve the
existing network restrictions while identifying any loss source.

## Routes and account requirements

- **801 — Callcentric IP Freedom:** calls the fixed Callcentric test account
  `17771234567` by default. The free plan supplies a SIP account, not free calls
  to normal telephone numbers. Supply the `1777…` account ID and its extension's
  SIP password. A non-default provider extension appends its three digits to the
  full account ID. [Official plan](https://www.callcentric.com/rate/plans/ip_freedom/)
  and [PJSIP settings and test number](https://www.callcentric.com/support/device/asterisk/17_pjsip).
- **802 — SIP2SIP:** calls `4444@sip2sip.info` for echo by default. `3333` is
  another documented media test. Supply the chosen account username/password.
  The generated profile uses DNS names, not a pinned infrastructure address.
  [Account settings](https://sip2sip.info/settings/) and
  [test addresses](https://sip2sip.info/help/).
- **803 — Linphone:** calls one explicitly configured SIP peer name at
  `sip.linphone.org`. The deployed profile selects `thetestcall`, labelled
  **The Test Call (English menu)** in the service catalog. A free individual
  account and a real peer are required; registration may require email/phone
  verification. Third-party SIP devices are supported by the service.
  [Official FAQ](https://www.linphone.org/en/faq/).

No Linphone-operated echo service was verified. The independent
[The Test Call operator's page](https://thetestcall.blogspot.com/), updated
2026-07-20, publishes `sip:thetestcall@sip.linphone.org`, lists PCMU/PCMA/G.722,
and provides real-time echo on menu option 3. This is a possible explicitly
reviewed peer, not an official Linphone test service. Its route is deployed,
with the account identity kept in private configuration. The latest synthetic
pilot confirmed reachability and bidirectional RTP with zero sampled receive
loss. Menu behavior, DTMF and subjective audio quality remain unverified. See the
[published menu and test boundaries](free-voip-directory.md#using-the-803-menu).

Only enabled profiles with a non-null target add their number to Asterisk and
the Cisco exact-match `dialplan.xml`. Each call route has one fixed destination. Callcentric destinations
must be `1777…` account identifiers; SIP2SIP allows its documented test numbers
or a letter-first peer name; Linphone allows a letter-first peer name. These
conservative constraints intentionally exclude some numeric SIP usernames.
Arbitrary URIs, dial strings, PSTN numbers and caller-supplied routing expressions
are rejected. Do not select a peer whose forwarding behavior you have not reviewed.

Set an account's `"target": null` to generate its SIP registration and endpoint
without an outbound call route or phone dialplan entry. This supports credential
and registration checks while a provider review or a verified peer is pending.
Omitting `target` preserves the Callcentric and SIP2SIP test defaults; for
Linphone, omitting it also means registration only. Inbound is a separate opt-in
setting and must remain disabled for a registration-only pilot. A missing route
is not a provider-wide calling ban for an administrator with direct AMI/CLI access.

## Prepare private configuration

Merge the `external_sip` object from
[site.external-sip.example.json](../deploy/site.external-sip.example.json) into
the existing private `site.json`; do not replace the site's other settings. The
sample is inert: its global and per-account switches are false. Its public IP is
a documentation placeholder which validation rejects when enabled.

For each selected provider create a separate credential JSON file in a private
directory. It must be an absolute path to a regular file owned by the invoking
user, with mode **0600**. Symlinks, FIFOs and oversized files are rejected. Its
only fields are `username` and `password`. For example, write it through a local
editor; do not put the password in a shell argument, command history or chat:

```json
{
  "username": "17770000001",
  "password": "REPLACE_WITH_YOUR_SIP_PASSWORD"
}
```

The example values are not credentials. The accepted password alphabet is ASCII
letters, digits and `_!@%+.,:=/-`, length 12–128. This excludes characters which
could alter Asterisk configuration. Set/reset a compatible SIP password at the
provider if necessary; do not silently change an existing password before using
it. Use the SIP password, which may differ from the provider website password.

After adding the real account, reviewed target (or explicit null), public IPv4 and network plan,
enable the global switch and only that account. This local preparation command
creates two mode-0600 fragments in a new mode-0700 directory and makes **no
network connections**:

```bash
python3 scripts/external_sip.py \
  --site .local/site.json \
  --out .local/external-sip-review
```

The normal `provision.py` workflow incorporates the same fragments. It validates
credentials before writing output. Generated `pjsip.conf` contains the account
secrets, so retain the normal private-file and Kubernetes Secret handling. Do not
publish it, include it in an image, or paste `pjsip show auth` output into logs.

## Transport, DNS and NAT prerequisites

These initial profiles use **UDP SIP and G.711**, without TLS/SRTP. They create a
separate `external-sip-udp` transport bound to the existing private PBX node on
UDP **5070** by default; 5070–5099 is configurable. The existing phone transports
on 5060 and their source ACLs are unchanged. The generated transport advertises
the operator-supplied public address and the same public signaling port. Its
private `local_net` list includes both the node LAN and the configured phone
networks. A real public unicast IPv4 is required; private, CGNAT, multicast and
documentation addresses are rejected.

Before deployment, verify the public address and whether the router preserves
the selected signaling/RTP ports. The profile assumes the external signaling
port equals its local port. Double NAT, CGNAT or port remapping needs a separately
reviewed solution. It does not discover a public IP, turn on SIP ALG, change
firewall rules, or create a public port forward. RTP uses the existing PBX UDP
10000–10199 range. Successful registration alone does not prove two-way media.
See Asterisk's [NAT configuration guide](https://docs.asterisk.org/Configuration/Channel-Drivers/SIP/Configuring-res_pjsip/Configuring-res_pjsip-to-work-through-NAT/).

Registration and AOR URIs use the provider domain without an explicit port or
transport URI parameter, preserving DNS service discovery. The image includes
`res_resolver_unbound`; deployment readiness requires it when external SIP is
enabled. Verify it with:

```text
module show like res_resolver_unbound
```

Expect the module to be `Running`. DNS UDP/TCP 53 must be reachable through the
site's approved resolver. Asterisk documents [hostname/port transport selection](https://docs.asterisk.org/Configuration/Channel-Drivers/SIP/Configuring-res_pjsip/PJSIP-Transport-Selection/)
and the [unbound resolver module](https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/Module_Configuration/res_resolver_unbound/).

SIP2SIP requests RFC 3263 discovery and warns against pinning its current proxy
address or transport. This implementation preserves domain discovery but restricts
the local profile to UDP; it does not claim automatic UDP/TCP/TLS failover. Verify
current UDP service discovery and a real account pilot before using this profile.
If the provider's advertised transports change, update the profile rather than
hardcoding one of its present proxy IPs. Linphone's profile also needs a real
account/peer pilot. The local parser test cannot establish provider acceptance.

## Incoming-call isolation

Incoming calls are disabled by default. Outbound-only profiles have
`identify_by=ip`, no incoming identify object, no registration `line` matching,
and no dynamic registration contacts. Thus an unsolicited request cannot select
that provider endpoint by username. The endpoint must still accept legitimate
in-dialog requests, such as the remote party's BYE: a blanket endpoint `deny`
would block those too. This distinction follows the
[Asterisk 22 request distributor](https://github.com/asterisk/asterisk/blob/22.11.0/res/res_pjsip/pjsip_distributor.c)
and is covered by the isolated SIP protocol test.

Only Callcentric has an implemented opt-in incoming profile, based on the
published ranges `204.11.192.0/22` and `199.87.144.0/21`. Its `source_cidrs`
must be non-overlapping canonical subnets within those ranges. Both endpoint ACL
and IP identify rules use these reviewed sources. Re-check the provider's
published ranges before deployment. Do not widen the Cisco phone ACL or use
`0.0.0.0/0` to make incoming calls work.

An enabled inbound profile accepts the exact account identifier as the called
extension. Its private `external_sip.accounts[].inbound.target` may select
**one inventoried local 200–299 line**, or **621** when the reviewed incoming
group is enabled. This is separate from the account's outbound `target` field.
Every other called destination is rejected. The provider context does not
include `home`: its only permitted cross-context jump is the literal
`Goto(home,621,1)` for that selected group. It cannot choose another provider,
Assist, radio or an arbitrary local extension. PBX-only generation cannot enable
inbound calls without a verified inventory.

The [621 group configuration](phone-services.md#configure-621-without-opening-provider-inbound)
requires an explicit list of Cisco primary lines and optional declared
softphones. Secondary Cisco lines, duplicate recipients and unknown numbers are
rejected. Omitting a physical handset's primary line excludes that handset from
the group; its second line does not create another recipient. Group calls ring
for up to 25 seconds, respect gateway DND and ignore SIP redirects. They do not
enable speakerphone auto-answer. A direct inbound line also respects gateway
DND when the gateway is enabled. This does not synchronize native Cisco DND.

Enabling 621 creates a local group, not a provider inbound route. The deployed
configuration includes that local group, while all provider inbound switches remain
disabled. Real incoming delivery still needs explicit provider activation,
source-policy verification and a separate acceptance call. Keep the actual
membership and room labels in private configuration.

A bounded local 621 acceptance call reached all five currently online eligible
recipients without targeting either line of the excluded handset. Exact-call
cleanup left zero owned channels and preserved DND/auto-answer settings. This
tests the local group, not an incoming provider or PSTN path.

SIP2SIP and Linphone incoming profiles remain blocked until an appropriate
provider-authentication/source policy has been reviewed. Their absence of fixed
CIDRs must not be worked around by opening the whole Internet. Any router changes
for an actual incoming pilot are a separate, explicit network operation.

## Deployment and acceptance

1. Check account terms, selected free SIP target, credentials, DNS and NAT plan.
   Leave PSTN service, auto-recharge and chargeable forwarding disabled.
2. Regenerate private PBX/phone configuration and run the ordinary preflight.
   Preflight compares the complete external fragments and rejects stale profiles,
   altered ACLs and duplicate managed objects.
3. Follow the existing deployment process. PJSIP transport changes require an
   Asterisk restart; do not claim that `pjsip reload` applied a new bind/NAT value.
   Refresh the phone's TFTP dialplan through the documented controlled restart.
4. Read `pjsip show registrations`, `pjsip show endpoint external-sip-callcentric`
   (or the selected provider) and `dialplan show 801@home` (or 802/803). Verify the
   intended account is registered; avoid publishing account identifiers.
   For a null target, verify that its 801/802/803 route does not exist and stop
   at registration acceptance. Keep the provider review/peer blocker recorded.
5. Place one user-authorized SIP-only test call, verify both audio directions and
   remote/local hangup, then verify the channel count returns to zero. Routes cap
   call duration at 120 seconds and ignore SIP forwarding requests.
6. If incoming is enabled, test its exact target and prove other extension numbers
   are rejected. Keep private receipts, excluding credentials.

For Cisco firmware checks, correlate **App Load ID**, the current **LLDP firmware
advertisement**, and a fresh **PJSIP contact at the verified handset address**.
The web page's separate **Version** field can retain an older SCCP label after a
successful SIP conversion. Verify the physical MAC and these independent
indicators before deciding that another firmware conversion is needed.

A missing LLDP row alone does not prove that a phone is unplugged. Before a
remote PoE operation, repeat the switch forwarding-table, link and power checks
and verify the handset's MAC directly through its web status page. An LLDP
omission may be tolerated only when the same single handset MAC is learned on
the expected powered port in repeated snapshots and its model and SIP load
match; a conflicting neighbor or ambiguous port must stop the operation.

To disable, turn off `external_sip.enabled`, regenerate and deploy. The external
transport, registrations and routes disappear from generated configuration. If
you separately changed a router/firewall, remove only those owned rules. The
script never cleans up unrelated network settings.

No PSTN trunk route or E.164 dial pattern is generated. Using a paid provider plan
later requires explicit cost approval and a separate implementation with a
reviewed destination allowlist, billing limits, emergency-calling limitations and
acceptance tests. A provider's $0 monthly fee does not mean its PSTN calls are free.

## Reproduce the isolated tests

```bash
python3 -m unittest discover -s tests -p test_external_sip.py -v
ASTERISK_TEST_IMAGE=your-built-image/asterisk:22.11.0 \
ASTERISK_REQUIRE_UNBOUND=1 \
python3 -m unittest discover -s tests -p test_external_sip.py -v
```

For a non-default Docker context, also set `ASTERISK_DOCKER_CONTEXT`. The runtime
test copies synthetic fixtures into a disposable `--network none` container; it
does not mount production secrets, contact DNS/providers, register live accounts
or reach phones. It verifies all three profiles and appended routes load, a
loopback SIP call accepts the remote BYE, and a fresh unsolicited INVITE gets a
401 response. It also removes the Callcentric/Linphone targets and reloads the
dialplan: all three registration objects remain, only 802 has a call route,
and the generated phone XML stays unchanged. It is not a test of real DNS discovery, account registration, RTP
audio or provider billing.
