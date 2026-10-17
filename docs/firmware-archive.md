# Preserved Cisco 7945/7965 SIP firmware

On **October 17, 2026**, this project downloaded and inspected a preserved copy
of **SIP 9.4(2)SR4-3** for the Cisco 7945G/7965G. The target load is
`SIP45.9-4-2SR4-3S`. Firmware binaries remain private and are not included in
this repository.

## Source and verification limits

The working download is
[7945-7965.tar.gz on Internet Archive](https://archive.org/download/20250911_20250911_1148/7945-7965.tar.gz).
The [item page](https://archive.org/details/20250911_20250911_1148) and
[metadata API](https://archive.org/metadata/20250911_20250911_1148) identify an
upload dated September 11, 2025. This is a third-party preserved archive,
not Cisco's original ZIP distribution or a Cisco-hosted download. The legacy
7945G download page was unavailable during this check, so the reproducible
procedure below uses this preserved copy.

The downloaded archive has these values:

```text
Size:    6816140 bytes
SHA-256: 6747037770284d06d0b2d9c0986c15aaf7f89378e676dc4f6964d412487a2138
MD5:     d9b486319b94767155f06260570eef64
SHA-1:   469eeddefc24b80ae18080b1a5770999e8248396
```

MD5 and SHA-1 match Internet Archive's published metadata. SHA-256 above was
computed locally. All eight staged firmware files match the sizes and SHA-256
values in the pinned
[patchbay manifest](https://github.com/ChristopherJMiller/patchbay/blob/35739744a739cce9eb3929695e23d389863b7aaa/internal/drivers/cisco79x5/firmware.go).
That manifest itself says its hashes came from a preserved copy and had not
been checked against Cisco's download. The two copies may share an origin.

These checks establish reproducible bytes and agreement with another published
manifest. **They do not authenticate the archive's Cisco origin, verify vendor
signatures offline, or establish a software license.** The archive's collection
label does not make Cisco firmware open source. Review provenance and your right
to use the firmware before deployment. Prefer a verified Cisco distribution if
one becomes available. Never modify signed components or disable/bypass the
phone's signature checks.

The archive contains a `7945-7965/` directory with the eight firmware files
listed below and two small metadata files, `load435.txt` and `load436.txt`.
Only the eight firmware files belong in the staged directory for this procedure.
Their total size is **10,541,010 bytes**. The archive, manifest and metadata stay
outside TFTP.

## Hardware and upgrade path

**Hardware revision 13.0 and later requires at least 9.3(1)SR3.** Do not install
8.x or 9.2(1) on those phones. The target here is 9.4(2)SR4-3. A phone already
running SCCP 9.4(2) does not need an automatic downgrade to an 8.x intermediate
release. Check the actual hardware, current App Load and target release notes
using the [conversion guide](sip-conversion.md) before any restart or reset.
The minimum comes from Cisco's
[Hardware Updates / Installation notes](https://www.cisco.com/en/US/docs/voice_ip_comm/cuipph/7900_series/firmware/9_3_1SR4/english/release_notes/P790_BK_RF3660D6_00_rn-9_3_1sr4-7900-series_chapter_00.pdf).
The [September 2025 Cisco device pack](https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cucm/device_packs/1401-devpack-for-sep-2025.html)
lists the target SIP release; it does not authenticate this mirror.

The separate Internet Archive item `cisco-firmware`, file
`cisco_7945_7965.zip`, contains **9.2(1)** SIP/SCCP archives. It is **unsuitable
for HW13.0+** despite having the right model numbers in its filename.

## Download into a fresh private directory

Run from the repository root with Python 3.9+ and curl available. The commands
make no phone, DHCP, firewall or cluster changes. Keep using the same shell so
`CISCO_FW_REVIEW_DIR` remains set. Each run creates a new directory rather than
overwriting a previously reviewed set.

```bash
umask 077
mkdir -p .local/firmware-downloads
export CISCO_FW_REVIEW_DIR="$(mktemp -d .local/firmware-downloads/sr4-3.XXXXXXXX)"
curl --fail --show-error --silent --location --max-redirs 5 \
  --proto '=https' --proto-redir '=https' --tlsv1.2 \
  --connect-timeout 15 --max-time 180 --max-filesize 6816140 \
  --output "$CISCO_FW_REVIEW_DIR/7945-7965.tar.gz" \
  'https://archive.org/download/20250911_20250911_1148/7945-7965.tar.gz'
```

Stop if the download fails. Do not raise the size limit or replace the expected
hash merely to accept a changed file. The next step verifies the exact archive
before reading it and extracts only known regular files. It does not call
`tar.extractall`, execute archive contents, or follow archive links.

## Verify the bytes and create an unreviewed manifest

The `expected` mapping is the complete component size/SHA-256 list. It is also
the extraction allowlist. The code rejects missing, duplicate, unexpected or
nonregular members and checks each firmware component before writing anything
to the fresh `firmware/` directory.

```bash
python3 - <<'PY'
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import tarfile

base = Path(os.environ['CISCO_FW_REVIEW_DIR'])
archive = base / '7945-7965.tar.gz'
archive_sha256 = '6747037770284d06d0b2d9c0986c15aaf7f89378e676dc4f6964d412487a2138'
source = 'https://archive.org/download/20250911_20250911_1148/7945-7965.tar.gz'
expected = {
    'SIP45.9-4-2SR4-3S.loads': (673, 'c269fb8de6551e64ab7b2ad7c7335690599f28100957fdc5bfbcf7c75046493d'),
    'apps45.9-4-2SR4-3.sbn': (4637904, '24087cd5bd54411dd9b9b0dc9de668334cee9829fd2ca140b6050267512d49e2'),
    'cnu45.9-4-2SR4-3.sbn': (582170, 'f2321bdc9388781cd85f36ff6418edb33716b9ea8a48ef4d8f3847f226d78a9b'),
    'cvm45sip.9-4-2SR4-3.sbn': (2693976, '7c4dafb1532f952267b242b531f9405b0957339a18b2e0195e5bc2ea5de42c5f'),
    'dsp45.9-4-2SR4-3.sbn': (364907, '12bdff18f06e68a6ec6ed5661aa15fbe1ca58e8b75f46ff44f19ee7dc8a99d1c'),
    'jar45sip.9-4-2SR4-3.sbn': (2260034, 'ae6391837f8e071ccc9fb9ea91edff128d303e489dd2e0f85fb495fea7e5c22f'),
    'term45.default.loads': (673, '4816646f9c7cd63b0c1f76706241db6087da13665c0069266a82630c48bd4852'),
    'term65.default.loads': (673, '62ce7e642b2ff461b1566aa11722fcbaf7fd16d7030f7a463092a3c309465ebf'),
}

if archive.is_symlink() or not archive.is_file():
    raise SystemExit('Archive must be a regular local file')
if archive.stat().st_size != 6816140:
    raise SystemExit('Archive size mismatch')
if hashlib.sha256(archive.read_bytes()).hexdigest() != archive_sha256:
    raise SystemExit('Archive SHA-256 mismatch')

payloads = {}
seen = set()
with tarfile.open(archive, mode='r:gz') as bundle:
    for member in bundle:
        if member.name in seen:
            raise SystemExit(f'Duplicate member: {member.name}')
        seen.add(member.name)
        if member.name == '7945-7965' and member.isdir():
            continue
        if not member.isfile():
            raise SystemExit(f'Nonregular member: {member.name}')
        if member.name in ('7945-7965/load435.txt', '7945-7965/load436.txt'):
            if member.size != 70:
                raise SystemExit('Unexpected metadata size')
            continue
        prefix = '7945-7965/'
        name = member.name.removeprefix(prefix)
        if member.name != prefix + name or name not in expected:
            raise SystemExit(f'Unexpected member: {member.name}')
        size, digest = expected[name]
        if member.size != size:
            raise SystemExit(f'Component size mismatch: {name}')
        stream = bundle.extractfile(member)
        if stream is None:
            raise SystemExit(f'Cannot read component: {name}')
        with stream:
            data = stream.read(size + 1)
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise SystemExit(f'Component SHA-256 mismatch: {name}')
        payloads[name] = data

if set(payloads) != set(expected):
    raise SystemExit('Incomplete firmware bundle')
target = base / 'firmware'
manifest_path = base / 'firmware-manifest.json'
if manifest_path.exists() or manifest_path.is_symlink():
    raise SystemExit('Manifest already exists; use a fresh review directory')
target.mkdir(mode=0o700)  # Fails if this directory already exists.
for name, data in payloads.items():
    with (target / name).open('xb') as output:
        output.write(data)
    (target / name).chmod(0o600)
manifest = {
    'model': 'CP-7945G',
    'reviewed': False,
    'load_id': 'SIP45.9-4-2SR4-3S',
    'source': source,
    'archive_sha256': archive_sha256,
    'verified_at_utc': datetime.now(timezone.utc).isoformat(),
    'vendor_signature_offline_verified': False,
    'files': {name: digest for name, (_, digest) in expected.items()},
}
with manifest_path.open('x') as output:
    json.dump(manifest, output, indent=2)
    output.write('\n')
manifest_path.chmod(0o600)
print(f'Archive and all {len(payloads)} component hashes match.')
print(f'Operator review is still required: {manifest_path}')
PY
```

The snippet uses `str.removeprefix`, available in Python 3.9 and later.

## Review before provisioning

Review the source/provenance limits above, your firmware usage rights, the
actual phone's model and hardware revision, current App Load, target release
and recovery prerequisites in the [conversion guide](sip-conversion.md).
Check that the output contains exactly the eight listed firmware files.

Only **after that review**, edit the generated private `firmware-manifest.json`
and change `"reviewed": false` to `"reviewed": true`. This is an operator
attestation, not a Cisco signature result. Keep
`"vendor_signature_offline_verified": false`; no offline signature verification
has been performed by this procedure. The repository's verifier intentionally
rejects an unreviewed manifest.

```bash
python3 scripts/verify_firmware.py \
  --directory "$CISCO_FW_REVIEW_DIR/firmware" \
  --manifest "$CISCO_FW_REVIEW_DIR/firmware-manifest.json"
```

After it passes, follow [firmware staging](provisioning.md) and the
[k3s runbook](k3s-deployment.md). Those commands normally read
`.local/firmware/` and `.local/firmware-manifest.json`; deliberately copy this
reviewed set to those deployment locations when ready, without merging it with
another release. Keep the downloaded archive and review record separately.
Set the phone's `firmware_load` to `SIP45.9-4-2SR4-3S`, without `.loads`.
Have the complete TFTP set available before a phone restart. Never interrupt
power during a flash write. A successful hash check is not a successful flash,
registration or audio test.

## Separate SR3 fallback

A preserved [9.4(2)SR3-1 ZIP](https://archive.org/download/cmterm-7945_7965-sip.9-4-2-1SR3-1/cmterm-7945_7965-sip.9-4-2-1SR3-1.zip)
is also available. Its [metadata](https://archive.org/metadata/cmterm-7945_7965-sip.9-4-2-1SR3-1)
lists 6,512,565 bytes and MD5 `d06edfb4dfca2460d10766cb9e66c564`.
The archive listing contains `SIP45.9-4-2SR3-1S.loads`, five
`9-4-2ES26` `.sbn` files and `term45.default.loads`/`term65.default.loads`.
This is a **different complete release**, requiring its own review, hashes,
manifest and load ID. Do not use the SR4-3 commands or component hashes above
for it, and do not mix its components with SR4-3. Its availability alone is not
a reason to downgrade a working phone.
