# Re-download installation files and remove local staging copies

Downloaded installers, container export archives, duplicate unpacked images,
phone dumps and old deployment snapshots are not permanent runtime storage.
After installation and verification, remove obsolete workstation copies
instead of making another backup of them. Keep the source and verification
instructions below so a future maintenance operation can obtain its own files.

## Preserve the running installation

Keep current private site/inventory configuration, canonical secrets, rendered
configuration still used for deployment, service state, and required local
models or other working assets. Keep the small source/hash records needed to
identify an installed version. Do not publish these private records.

The permanent [daily TFTP service](daily-phone-boot.md) serves profiles, the dial
plan and reviewed artwork from its own managed root. A converted phone keeps
its firmware in flash. Removing an unused firmware download on a workstation
is different from deleting a live TFTP root, a node's container image, a
persistent volume, or an installed Mac application. Check actual mounts and
file references before removing any directory used by a running process.

Do not remove `.local/` wholesale: it contains both disposable staging data and
working configuration. This guide does not require retaining raw phone dumps,
old full-directory snapshots or extra copies of installed firmware.

## Cisco 7945G/7965G SIP firmware

The reviewed target is **SIP 9.4(2)SR4-3**, load **`SIP45.9-4-2SR4-3S`**.
The preserved download is **`7945-7965.tar.gz`**, 6,816,140 bytes:

- [Archive download](https://archive.org/download/20250911_20250911_1148/7945-7965.tar.gz)
- [Archive item and provenance](https://archive.org/details/20250911_20250911_1148)
- [Metadata](https://archive.org/metadata/20250911_20250911_1148)

Expected archive SHA-256:

```text
6747037770284d06d0b2d9c0986c15aaf7f89378e676dc4f6964d412487a2138
```

Use the [complete firmware download procedure](firmware-archive.md), which
contains the exact eight component filenames, sizes, hashes, bounded extraction
and verifier commands. It also explains the third-party provenance and
licensing limits. Do not substitute an older package based only on its model
number. The optional SR3 fallback is a different complete release and is not
needed to reinstall the reviewed SR4-3 target.

Retrieve and verify firmware before a future maintenance window. Then follow
[provisioning](provisioning.md) and [SIP conversion](sip-conversion.md).
Deleting an obsolete local copy does not call for a phone reset or reflash.

## Project container exports

Release assets were checked on **October 25, 2026**. Use the exact release that
matches the intended deployment; do not replace an installed version merely
because a newer download exists.

| Component | Published source | Files to download together |
| --- | --- | --- |
| Wake-capable phone gateway | [Beta 25](https://github.com/4alvit/cisco-home-telephony/releases/tag/v0.1.0-beta.25), source `213454385b7e75057f6b4d5db5a7b6a79922ccdd` | `voice-wake-amd64.tar.gz`, `SHA256SUMS`, `image.json`, `source.json`, `receipt.json`, `container-sbom.cdx.json`, `container-trivy.json` |
| Managed TFTP service | [Beta 20](https://github.com/4alvit/cisco-home-telephony/releases/tag/v0.1.0-beta.20), release source `5e4b8b9682fa133612037ac80cdeb7477fb8f9cc` | `tftp-amd64.tar.gz`, `SHA256SUMS`, `image.json`, `source.json` |

For example, download the gateway export into a fresh private directory:

```sh
umask 077
mkdir -p .local
mkdir .local/gateway-redownload
gh release download v0.1.0-beta.25 --repo 4alvit/cisco-home-telephony \
  --dir .local/gateway-redownload \
  --pattern voice-wake-amd64.tar.gz --pattern SHA256SUMS \
  --pattern image.json --pattern source.json --pattern receipt.json \
  --pattern container-sbom.cdx.json --pattern container-trivy.json
cd .local/gateway-redownload
shasum -a 256 -c SHA256SUMS
```

Follow [gateway image verification and installation](voice-gateway-deployment.md)
or [TFTP installation](daily-phone-boot.md#obtain-the-managed-image) after download.
The Beta 20 image source is `eba086718b8e1b3b58fbc1b928bf6941f99133a3`;
its release includes later deployment-only fixes. Preserve that distinction when
checking `source.json`. An image configuration ID is not a registry manifest
digest. A downloaded or unpacked archive is not the installed runtime image.

Old CI exports and candidate-only images may no longer be downloadable:
GitHub Actions artifacts have short retention. For a replacement **Asterisk
22.11.0 / TFTP bookworm** export, use the [CI image procedure](ci-images.md) with
`export_target=pbx`; its archives are `asterisk-amd64.tar.gz` and
`tftp-amd64.tar.gz`, with `images.json`, `source.json` and `SHA256SUMS`.
The same guide covers the lean voice export `voice-amd64.tar.gz`; it is not the
wake-capable gateway archive above. Alternatively, use `scripts/build-images.sh`
from the selected, reviewed source checkout. Verify and qualify a new build;
rebuilding source does not promise the identical old image digest.

After verified import, obsolete `.tar`, `.tar.gz`, OCI layouts, downloaded ZIP
wrappers, partial downloads and duplicate image trees can be removed from the
workstation. Keep the small source/digest mapping, not extra image copies.

## Home Assistant component package

[Asterisk Telephony 0.8.0b2](https://github.com/ha-homelab/asterisk-telephony/releases/tag/v0.8.0b2)
records source `1ac0f3052d8c5a9ebc4b446498f804493a52ce70`. Its package is
`cisco_phone_control-0.8.0b2-1ac0f30.tar.gz`; the accompanying files are
`SHA256SUMS`, `files.json`, `files.sha256`, `provenance.json` and `build_package.py`.
Download matching files from that release, check their hashes, and follow that
repository's installation guide. Deleting the old package does not remove the
installed component, HA configuration or persistent state. Source changes newer
than that release do not make the older package a newer version.

## Mac softphone installers

The [Mac softphone guide](macos-softphone.md) contains account setup and
signature checks. Obtain installers only through the publisher:

- **Zoiper5 5.6.15 Community Edition:** the downloaded installer was
  `Zoiper5_5.6.15.pkg`; use the [official macOS download page](https://www.zoiper.com/en/voip-softphone/download/zoiper5/for/mac).
  That page can move to a newer release and is not a guarantee of an older build.
- **Linphone 6.2.3:** [official `Linphone-6.2.3-mac.dmg`](https://download.linphone.org/releases/macosx/app/Linphone-6.2.3-mac.dmg).
- **Telephone 1.6:** the [official Mac App Store listing](https://apps.apple.com/us/app/telephone/id406825478);
  no retained standalone installer is required. Store availability/version may change.

Unused PKG/DMG files and temporary extracted installer trees can be removed
after installation. Preserve `/Applications` bundles that are still used,
current application preferences, Keychain entries and the canonical SIP
credentials. Removing a cached installer does not require re-registering a
working softphone.
