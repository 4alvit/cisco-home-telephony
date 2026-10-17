# A separate SIP line on a Mac

A softphone is a SIP application with its own extension and password. It is not a Cisco inventory entry: it has no fabricated MAC address, firmware requirement, SEP profile or TFTP dependency. Existing phones can dial a new `2xx` extension through the already generated `2..` pattern.

## Choose and install the client

[Telephone](https://www.64characters.com/telephone/) is a small native macOS client. Its free tier supports ordinary calls; Telephone Pro is optional. The official [Mac App Store listing](https://apps.apple.com/us/app/telephone/id406825478) currently supplies version 1.6. The publisher added [Apple Silicon and the modern macOS interface](https://www.64characters.com/blog/) in that release. It supports SIP/TCP and [Opus, G.722, PCMA and PCMU](https://www.64characters.com/telephone/faq/). This configuration deliberately uses PCMU/PCMA to match the existing Cisco call path.

Install from the App Store. If the open-source `mas` command is already available, `mas get 406825478` acquires the free app; it may require the user's App Store session or macOS authorization. Do not enable Pro or change Apple account credentials to complete this setup. After installation, verify the application before entering a SIP secret:

```sh
codesign --verify --deep --strict /Applications/Telephone.app
spctl --assess --verbose --type execute /Applications/Telephone.app
file /Applications/Telephone.app/Contents/MacOS/Telephone
```

The official Store build is signed by Apple Mac OS Application Signing and contains an arm64 architecture. Verify these properties on the actual installed copy. Microphone access is required for speech; Contacts access is optional.

A more feature-rich alternative is [Linphone](https://www.linphone.org/en/download/). Use the stable vendor download and its [Third-Party SIP Account](https://www.linphone.org/en/docs/login-sip-account/) setup. A separate public SIP-provider account is unnecessary for either app when registering directly to this home PBX.

## Declare a generic client

Add a `softphones` list to private `site.json`. This synthetic example uses extension 209 and a reserved example LAN address:

```json
"softphones": [
  {
    "extension": "209",
    "label": "Mac",
    "source_ipv4": "192.168.50.90",
    "transport": "tcp"
  }
]
```

All four fields are required, and unknown fields are rejected. Extensions are strings in `200..299`, unique across all Cisco lines and softphones. Labels use 1–24 ASCII letters, digits, spaces, underscores or hyphens. The source must be an explicit RFC1918 IPv4 literal; public addresses, hostnames, CIDRs, loopback and IPv6 are rejected. Transport is `tcp` or `udp`. Reserve the Mac's address through the existing DHCP system before relying on its exact `/32` permission.

The generator creates a separate `softphone-secrets.json`, mode `0600`, containing a stable random secret for each extension. Generated passwords are 24 URL-safe characters; existing passwords must have 24–30 letters, digits, `_` or `-`. They cannot duplicate another softphone password or a Cisco password. Back up this private file with the other credentials. Removing a client removes its runtime endpoint and route but retains its saved password for an intentional later re-addition.

Regenerate from the current canonical private configuration in a separate candidate directory, preserving all existing secrets:

```sh
python3 scripts/provision.py --site .local/candidate/site.json \
  --inventory .local/candidate/phones.json --out .local/candidate
python3 scripts/render_k3s.py --site .local/candidate/site.json
python3 scripts/preflight.py --site .local/candidate/site.json
```

For an installation with no physical Cisco devices, `--pbx-only` accepts the same optional softphone list and creates no Cisco inventory, TFTP files or `secrets.json`.

Each softphone gets one PJSIP endpoint, authentication object and dynamic AOR with one contact, symmetric RTP and `direct_media=no`. Its source ACL allows only its own `/32`. Both existing local SIP transports include the client `/32` for NAT classification; this does **not** add that address to Cisco endpoint ACLs. Preflight checks the exact generic endpoint, transport, source permission and direct-dial route, rejects stale removed clients, and keeps generic clients out of the private Cisco alert context.

The `home` context provides direct local calls and whichever service numbers are already enabled: echo 600, tone 601, group 620, Assist 630, legacy tone 640, radio and explicitly configured external test routes. A softphone is not added to group 620, Cisco gateway inventory, device management, or persistent Cisco timer/alarm targets. It can still call 620 to reach the physical phones. Existing DND checks also guard calls **to** its direct extension; no new gateway controls are implied.

## Permit the exact client and register

Review the host guard and permit only the reserved Mac `/32` to PBX SIP TCP 5060 (or UDP 5060 if explicitly chosen) and UDP RTP 10000–10199. Preserve existing phone and HA permissions. Do not expose SIP through public ingress or expand Cisco source networks. No TFTP, DHCP option 150, firmware transfer, physical-phone reboot or new image is required. [Review the k3s deployment process](k3s-deployment.md) and use the existing Deployment field ownership and an idle maintenance window.

In Telephone's account setup enter:

- **Full Name:** the chosen display label, for example `Mac`.
- **Domain:** the PBX's private IPv4 address.
- **User Name:** `209`.
- **Password:** the saved secret for 209.

Set the account transport to TCP when the private declaration uses TCP. Use the existing PBX port 5060, leave external STUN/ICE/proxy services disabled for direct LAN operation, and keep media encryption compatible with the current unencrypted LAN endpoint. SIP/TCP alone does not encrypt calls. Telephone's [account implementation](https://github.com/64characters/Telephone/blob/master/Telephone/AccountSetupController.m) saves its password in the macOS Keychain. Do not put credentials in screenshots, shell arguments or public configuration.

First confirm registration with `pjsip show endpoint 209` and `pjsip show contacts`. Registration alone is not an audio test. In an authorized call window, test echo 600, confirm bidirectional audio and clean hangup, then a call to and from a physical extension. Recheck existing contacts and providers after any PBX restart. Keep the Mac awake and the application running when expecting incoming calls; sleep and network changes require fresh registration.
