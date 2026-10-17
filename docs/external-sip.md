# Optional external SIP accounts

The provisioner supports three **disabled-by-default, SIP-network-only** account
profiles. Generated configuration alone does not prove provider registration or
a successful external call. As of **2026-10-17**, SIP2SIP, Callcentric and Linphone
all reached **Registered** on the deployed Asterisk. No external test call was
placed: the SIP2SIP 802 audio pilot was deferred at the user's request to keep
the phones quiet. Two-way external audio remains unverified. A Callcentric IP Freedom account
was created at $0, but its provider risk review currently blocks outbound calls
and account changes. No support ticket was automatically opened and no support
message has been sent. Linphone email activation is complete; no verified fixed
test peer has been selected. Both are deployed for registration only, without
801/803 call routes; these numbers reach the existing reject fallback. A relaunched FWD account was created,
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
  `sip.linphone.org`. There is no invented echo-test number. A free individual
  account and a real peer are required; registration may require email/phone
  verification. Third-party SIP devices are supported by the service.
  [Official FAQ](https://www.linphone.org/en/faq/).

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
extension and rings **one** explicitly chosen, inventoried local 200–299 line.
Every other destination is rejected. Its separate context cannot include or jump
to `home`, so it cannot access another provider, Assist, radio or arbitrary local
extensions. PBX-only generation cannot enable inbound calls without an inventory.
When the local phone gateway is enabled, its PBX DND setting is respected. This
does not synchronize native Cisco DND.

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
