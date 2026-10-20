# Offline firmware and video feasibility gate

Assessment date: **19 October 2026**. Target: **SIP45.9-4-2SR4-3S** on the
Cisco 7945G. This is the bounded implementation follow-up to
[the native application feasibility study](native-app-feasibility.md).

**20 October follow-up:** the [native static map](native-static-map.md) adds a
bounded segmented-image recognizer and corroborated startup, display and DSP
interfaces. The installation/signing gate below remains unresolved.

## Decision

**No-go for installing our own native application or MIDlet on the current
release.** The missing proof is an available, authorized build/sign/deploy
route that actually accepts a newly built application on this firmware.
This is a project gate decision, not proof that such a route could never exist.
Offline extraction does not satisfy it.

**No-go for promising native SIP video on the handset.** Finding executable
code, Java classes or media-related names does not demonstrate a handset video
decoder, a usable display API, sufficient resources, or a supported loader.

**Go for offline inspection and a server-side video-to-stills prototype.**
The tools in `tools/firmware_lab/` read local files and create private evidence.
They do not activate a microphone, call a phone, contact a device, alter a
phone configuration, install code, flash firmware or restart infrastructure.

## Reproduce the firmware inspection

Use Python 3.9 or later from the repository root. Supply the existing private
directory holding only the eight `.sbn`/`.loads` files; this tool does not
download or stage firmware. Choose a new output directory each time:

```bash
python3 -m tools.firmware_lab \
  --directory .local/pbx-bootstrap/firmware \
  --output .local/firmware-lab/sr4-3-review \
  --extract
```

Omit `--extract` for metadata only. `report.json` contains input hashes,
integrity comparisons with [the recorded public hash list](firmware-archive.md),
per-component observations, offsets, declared ELF extents, gzip checks and JAR
metadata. With extraction enabled, generated numbered files contain bounded
ELF extents, validated ZIP archives and gzip output. Artifact records map every
file back to its source component/node/offset and record its size and SHA-256.
No archive member name becomes an output path.

The complete reference match only identifies this previously recorded copy.
It is **not vendor signature verification**. The JSON explicitly keeps vendor
authentication, native execution and MIDlet deployment proofs false. It checks
the input hashes again after inspection to establish that the source files
remained unchanged during the run.

### Parser and filesystem boundaries

- Input is a flat set of at most 32 regular files, 64 MiB per file and 128 MiB
  total. Unexpected files, input file symlinks and special files are rejected.
- Output must be a fresh directory below this checkout's `.local/`; symlinked
  output parents are rejected. New directories are private and artifacts use
  mode `0600`, without executable bits. A failed run can leave partial private
  evidence; choose a fresh directory for the next run.
- ELF magic alone is insufficient. The parser validates supported ELF headers
  and bounded table/data extents, preserving raw flags and inferred ABI hints.
  Even a bounded extent is only a candidate embedded ELF file; no relocation,
  linkage, loader compatibility or execution is attempted.
- Gzip requires a complete checked stream. Expansion and artifact bytes are
  bounded; recursion stops after one expansion level, with the nested gzip
  scan explicitly marked not requested. Expanded payloads total at most
  64 MiB; gzip and JAR decompression each have a separate 128 MiB aggregate
  work budget across all sources. Failed gzip attempts consume their reserved
  allowance, and successful gzip validation plus retrieval are both charged.
- ZIP recovery requires a consistent central directory and local records,
  member size/CRC checks and bounded decompression. Malformed, unsupported or
  over-budget candidates remain diagnostics, not accepted archives.
- The unknown SBN envelope is described as observations. No cryptographic
  verification, reconstruction, repacking or signature operation is claimed.

## Observed component and ABI map

The completed private run matched **all eight input sizes and SHA-256 hashes**
from the reference manifest. It produced **70 artifacts, totaling 10,849,304
bytes**, and rechecked all original hashes successfully. These are measurements
of the preserved files, not a new observation of a running phone.

| Component | Validated offline observations |
| --- | --- |
| `apps45.9-4-2SR4-3.sbn` | 61 bounded ELF extents; 2,127,112 extracted bytes |
| `jar45sip.9-4-2SR4-3.sbn` | 6 bounded ELF extents; 47,328 extracted bytes; two validated ZIP archives |
| `cvm45sip.9-4-2SR4-3.sbn` | Gzip at offset 1,111; 2,692,865 compressed bytes; 6,815,252 expanded bytes; stream checksum verified |
| `cnu45.9-4-2SR4-3.sbn`, `dsp45.9-4-2SR4-3.sbn`, three `.loads` files | Hashes match; no bounded ELF, gzip or ZIP components found by these scanners |

All **67 ELF extents** are 32-bit, big-endian, machine MIPS, type `ET_EXEC`.
Raw flags are `0x10000000`, interpreted as the MIPS II ISA flag; the ABI field is
**not explicitly tagged**. There are no overlaps between the accepted ELF
extents within either source. The report records entry addresses, section and
program tables, and bounded symbol/import hints. Some symbols suggest runtime
allocation, module registration and device/process interfaces; names alone do
not establish parameter layouts, calling conventions, supported APIs or a
linkable SDK. ISA flags do not identify the actual processor model or speed.

The expanded CVM data does not contain an ELF/gzip candidate under this scan.
The tool leaves that payload opaque instead of calling it a recovered native
program. Likewise, no marker found is not evidence that a firmware component
contains no code; this tool does not decode every packaging format.

The two ZIP extents in the JAR component are:

| Source byte interval, end exclusive | Size | Entries | Class headers |
| --- | ---: | ---: | ---: |
| `[225528, 539023)` | 313,495 | 172 | 0 |
| `[539023, 2085140)` | 1,546,117 | 1,317 | 1,242 |

All recovered class headers report version **48.0**, across **55 packages**.
The sanitized namespace counts are 770 `java`, 418 `sun`, 37 `com.sun`, 16
`javax.microedition` and one other `javax` class. This resembles runtime/library
content, not proof of a user-installable application. Neither archive's
manifest declares a MIDlet; no JAR signature entries were found. Enclosing SBN
authentication remains separate and unverified. Only the first eight bytes of
each class are used for the version count; bytecode validity is not tested.

## Java signing and the legitimate build route

Cisco's enhanced-services documentation explicitly lists the 7945G with SIP
and SCCP firmware 8.4(1) or later. It describes **Cisco-signed JAD/JAR MIDlets**,
512 KB maximum per JAR and 2 MB total MIDlet flash. Its CUCM provisioning model
requires Internal/Both service provisioning, a service vendor matching the
JAD, and a matching service version when specified. These are historical
documented constraints, not successful deployment on this standalone SIP
installation.
[Cisco enhanced services and phone considerations](https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cucmbe/rel_notes/7_1_3/cucmbe-rel_notes-713.html).

The [official public SDK listing](https://developer.cisco.com/site/ip-phone-services/downloads/)
offers IP Phone Services SDK versions 7.1(2) and 4.1(1). Its
[documented scope](https://www.cisco.com/c/en/us/products/collateral/collaboration-endpoints/unified-ip-phone-7900-series/product_data_sheet09186a00800925a8.html)
is XML/HTTP phone services and server-side samples. That is the usable public
development route for this project's current screen services; it is not a CNU
firmware kit or demonstrated MIDlet signing entitlement. A historical Cisco
developer answer describes J2ME/MIDP 2.0 and selected third-party applications,
but does not establish a presently available public phone-side SDK.
[Cisco developer explanation](https://community.cisco.com/t5/collaboration-knowledge-base/java-apps-on-cisco-ip-phones/ta-p/3630193).

Oracle documents the legitimate generic Java ME sequence: compile against the
appropriate profile libraries, preverify, and package JAR/JAD. This can produce
a generic Java ME application; it does not establish that the result matches
the phone's runtime. Signing additionally requires a certificate trusted by
the target device. A self-generated key or generic JAR signature does not
satisfy Cisco's documented signing requirement. No Java toolchain was installed
or sample compiled for the phone in this gate.
[Oracle build sequence](https://docs.oracle.com/javame/dev-tools/jme-sdk-3.0-mac/UserGuide-html/z400007747176.html),
[Oracle signing requirements](https://docs.oracle.com/javame/dev-tools/jme-sdk-3.4/ecl/html/security.htm).

The term `signedMIDlet` is treated here as the signing requirement, not as a
newly verified XML configuration element. The bounded public-source review
did not establish that literal element or any switch that permits unsigned
applications. No attempt was made to defeat authentication or exploit an older
firmware release.

## What would reopen the native execution gate

The first two items are prerequisites to any device experiment:

1. **A legitimate deployment route:** vendor documentation and an available
   signing/service arrangement that covers our own application on this exact
   model/release, or another supported loader with explicit installation rights.
2. **A reproducible matching build:** the CNU headers, runtime/import libraries,
   linker/startup definitions and ABI contract for native code; or the matching
   Java profile, phone API libraries, packaging and signing workflow for a MIDlet.
3. **A harmless deployment demonstration on a designated test phone:** newly
   built code starts through that route, displays a recognizable result,
   survives the documented restart policy and can be removed/recovered.
4. **Separate media/API acceptance:** documented and measured access to audio,
   display and networking while ordinary calls retain priority. Native video
   additionally needs actual codec negotiation, decoding, display throughput,
   resource and call-coexistence measurements.

A generic MIPS cross-compiler does not supply the CNU runtime contract.
A decompiler's pseudocode is not a reproducible application build. Changing a
JAR inside an SBN is not a signing workflow. Enabling SSH does not supply a
general-purpose program loader. Cisco documents signed image authentication
for these models and the removal of the interactive Unix-like shell; its
advisory identifies 9.4(2) as the fixed release. These facts rule out treating
older demonstrations as current installation evidence.
[Image authentication](https://www.cisco.com/web/software/282074288/128566/cmterm-7945_7965-sip.9-4-2SR2-2-readme.html),
[Cisco advisory and shell hardening](https://www.cisco.com/c/dam/en/us/support/docs/csa/cisco-sa-20130109-uipphone.html).

## Runnable offline video-to-stills alternative

The executable part of this gate is a **local file decoder**, implemented in
`tools/firmware_lab/video.py`:

```bash
python3 -m tools.firmware_lab.video probe
python3 -m tools.firmware_lab.video extract .local/sample.mkv \
  --name sample-stills --frames 3 --interval 2
```

It reads a regular local file into memory and passes bytes to FFmpeg/ffprobe
over stdin. The only allowed input protocol is `pipe`; permitted containers
are Matroska/WebM, AVI and streamable MP4/MOV. The original is never opened for
writing. Some MP4 layouts need backward seeking and therefore fail with this
nonseekable input; use Matroska or a separately prepared fast-start copy.

Input is capped at 64 MiB and 4096 pixels per dimension / 8 million pixels,
with square pixels. Output is at most 30 frames, sampled every 1–5 seconds,
covering at most 120 seconds. FFprobe has a 10-second timeout and FFmpeg a
30-second timeout. The decoder restricts allocations, threads and output bytes;
these are bounded experiment controls, not an OS sandbox or total-memory quota.
PNG structure, dimensions and chunk CRCs are checked before writing. New
`0600` PNGs and a manifest go into a fresh `.local/firmware-lab/video/NAME/`
directory. The manifest records **nominal sample times**, not camera capture
timestamps or measured live latency.

The local dependency probe found FFmpeg/ffprobe **9.0.1**, an H.264 decoder,
PNG encoder and Pillow. PJSUA, baresip, GStreamer and Python `pjsua2` were not
available. The existing Asterisk image definition includes FFmpeg and Pillow;
that source-level dependency finding does not attest to a running container.
The real synthetic test decoded a six-second, 640×360 H.264 Matroska clip into
three distinct **298×156 RGB PNGs**, with sizes 16,379, 17,928 and 17,123 bytes.
This validates offline decode/resize output only. The nominal two-second
sampling interval is not a measured phone refresh rate.

### Concrete SIP bridge design and remaining work

The [SIP video lab](sip-video-bridge.md) now implements a finite synthetic
loopback SIP/RTP-to-PNG path, including real H.264 decoding after capture.
It does not implement the two-leg bridge below or deliver pictures to a phone.

A separate **server-side PJSIP application** is the concrete path for a source
that exposes only SIP video:

1. Terminate the source SIP dialog with audio plus receive-only H.264 video.
   Create a separate audio-only SIP leg through Asterisk to the 7945G. Two legs
   avoid assuming that an audio-only handset will negotiate and retain video.
2. Bridge audio between the legs in that application. Propagate ringing,
   answer, cancellation and hangup in both directions, including failures and
   multiple simultaneous calls.
3. Add a custom `pjmedia_port` sink with `pjsua_vid_conf_add_port()`, obtain the
   incoming decoded video port with `pjsua_call_get_vid_conf_port()` and connect
   it through `pjsua_vid_conf_connect()`. Keep only the latest frame; perform
   conversion and publication in a worker so the decoder callback cannot block.
4. Convert to the established 298×156 PNG contract, associate the frame with
   the correct call/phone, and use the existing authenticated gateway and
   refresh UI. Include acquisition time, stale-frame rejection, Stop, expiry,
   bounded resource replacement and call-screen priority.

PJSIP documents these APIs and video build dependencies. A suitable build needs
video enabled, the selected H.264 decoder and pixel converter; the bundled
PJPROJECT in an Asterisk build does not itself provide this application or
Python bindings. This bridge is **designed, not implemented or accepted here**.
[PJSUA video bridge APIs](https://docs.pjsip.org/en/2.14/api/generated/pjsip/group/group__PJSUA__LIB__VIDEO.html),
[call media direction and ports](https://docs.pjsip.org/en/2.15.1/api/generated/pjsip/group/group__PJSUA__LIB__CALL.html),
[PJSIP build dependencies](https://docs.pjsip.org/en/2.15.1/get-started/posix/build_instructions.html).

FFmpeg alone is not the SIP signaling endpoint. An alternative media worker
could use GStreamer's RTP jitter buffer, H.264 depayloader, decoder, scaler and
bounded appsink, but still needs a separate SIP/SDP controller. Asterisk can
transport compatible video; it does not transcode video into this image
service.
[FFmpeg protocols](https://ffmpeg.org/ffmpeg-protocols.html),
[GStreamer RTP buffering](https://gstreamer.freedesktop.org/documentation/rtpmanager/rtpjitterbuffer.html),
[H.264 depayloading](https://gstreamer.freedesktop.org/documentation/rtp/rtph264depay.html),
[appsink](https://gstreamer.freedesktop.org/documentation/app/appsink.html),
[Asterisk media limits](https://docs.asterisk.org/Operation/Asterisk-Audio-and-Video-Capabilities/).

The next acceptance gate needs a built endpoint and controlled peer to prove
SDP/H.264 profile and packetization compatibility, RTP decoding, SRTP/NAT as
applicable, two-leg call lifecycle, frame freshness and cleanup. Actual handset
tests must then establish image fetch/refresh behavior and call coexistence.
None was exercised here. Even if all pass, the result is refreshed stills on
the phone, without native video or an audio/video synchronization guarantee.

## Verification

Run the firmware lab tests independently of any private input:

```bash
python3 -m unittest discover -s tests -p 'test_firmware_lab*.py' -v
```

Fixtures are synthetic. Tests cover malformed/bounded binary parsing,
decompression limits, archive validation, private output boundaries, stable
reports and local media conversion where dependencies are present. Firmware
bytes, recovered classes and detailed local evidence must remain under
`.local/`; do not commit them. A successful local test is not a phone acceptance
test or a production deployment.

The completed run passed **55 tests**, including the real synthetic media
conversion. Two complete final extractions produced byte-identical reports
and all 70 artifacts, with the original hashes unchanged in both runs.
The public changes are confined to this document,
`tools/firmware_lab/`, and `tests/test_firmware_lab*.py`. Detailed firmware and
video manifests and recovered vendor bytes remain in ignored local output.
