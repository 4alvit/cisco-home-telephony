# Uncompressed audio on Cisco 7945G

**Status, 2026-10-20:** uncompressed 16-bit, 16 kHz audio is a documented
phone capability. Reliable two-way operation between `SIP45.9-4-2SR4-3S`
and Asterisk 22.11.0 remains unverified. This page describes a possible
follow-up to the [G.722 audio path](audio-quality.md), not an enabled feature.

## What Cisco documents

Cisco's CUCM 9.0 administration guide includes the 7945G and states:
“These phones also support uncompressed wideband (16 bits, 16 kHz) audio.”
Its Call Statistics table lists **Lin16k for both received and transmitted
RTP streams**. The guide covers SCCP and SIP together; it does not provide
a tested SIP45/Asterisk configuration or establish a SCCP-only restriction.
[Cisco-authored guide, preserved English PDF, printed page 1](https://www.th-wildau.de/files/HRZ/dokumente/voip/Cisco-Unified-IP-Telefone-7975G-7971G-GE-7970G-7965G-und-7945G-2018.pdf#page=21),
[Call Statistics, printed page 194](https://www.th-wildau.de/files/HRZ/dokumente/voip/Cisco-Unified-IP-Telefone-7975G-7971G-GE-7970G-7965G-und-7945G-2018.pdf#page=214).

The original English Cisco guide URL now redirects to the product-series
page. A Japanese edition remains available on Cisco's domain; the detailed
passages above were verified in the preserved English edition.
[Official Japanese PDF](https://www.cisco.com/c/ja_jp/td/docs/vauc/iptelep/ipphone7900/ag/029/P747_BK_W06BD6D9_00_adminguide_7945-7965-7970-7971-7975.pdf).

## Bandwidth and potential benefit

For mono audio, 16,000 samples/second × 16 bits gives **256 kbit/s** of audio.
With 20 ms packets, ordinary IPv4/UDP/RTP headers bring that to **272 kbit/s**;
Ethernet overhead brings it to approximately **0.29 Mbit/s per direction**,
before RTCP and signaling. A 100 Mbit/s full-duplex LAN has ample capacity.
These values are calculated, not measurements from a phone call.

An uncompressed path could avoid G.722 coding loss. It would not establish
stereo, 48 kHz playback or wider acoustic response from the phone's hardware.
An audible improvement would still require comparison on the handset or
speaker being used.

## Three different audio interfaces

**SIP/RTP L16:** samples are signed 16-bit integers in **big-endian** byte
order. For mono 16 kHz, an SDP mapping can be
`a=rtpmap:118 L16/16000/1`. At 20 ms, each packet contains 320 samples,
640 audio bytes and an RTP timestamp increment of 320.
[RFC 3551 §4.5.11](https://www.rfc-editor.org/rfc/rfc3551.html#section-4.5.11),
[RFC 3555 §4.1.15](https://www.rfc-editor.org/rfc/rfc3555.html#section-4.1.15).

Payload `118` is illustrative: the negotiated SDP determines the actual
mapping. Static payloads 10 and 11 are 44.1 kHz stereo and mono, respectively,
not 16 kHz.
[IANA RTP registry](https://www.iana.org/assignments/rtp-parameters).

Asterisk 22.11 maps internal `slin16` to `L16/16000`, recognizes
`L16-256/16000`, and includes 118 in its default dynamic allocation for
`slin16`. Its signed-linear RTP path performs the required byte-order
conversion.
[Exact RTP mappings](https://github.com/asterisk/asterisk/blob/22.11.0/main/rtp_engine.c#L3684),
[codec flags](https://github.com/asterisk/asterisk/blob/22.11.0/main/codec_builtin.c#L303),
[RTP implementation](https://github.com/asterisk/asterisk/blob/22.11.0/res/res_rtp_asterisk.c#L5351).

**AudioSocket:** type `0x12` carries **little-endian** PCM16 at 16 kHz over
framed TCP. Those payload bytes cannot be copied unchanged into L16 RTP.
The radio worker's `c(slin16)` channel lets Asterisk perform the conversion;
do not add another byte swap to the worker.
[AudioSocket protocol](https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/).

**Legacy XML `RTPRx`/`RTPTx`:** Cisco documents **G.711 mu-law with 20 ms
packets** for this service-controlled streaming interface. SIP codec
configuration does not upgrade it to L16 or G.722. Keep the assistant's XML
audio path on its existing contract; general uncompressed-phone support
does not override this interface-specific restriction.
[Cisco Internal URI Features, RTP Streaming](https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cuipph/all_models/xsi/7_0/english/programming/guide/70xsi/xsi70uri.html).

## What remains to test

A real-phone SDP offer captured on 2026-10-20 advertised
`a=rtpmap:102 L16/16000`. This confirms that the phone advertised L16 at
16 kHz; it does not prove that L16 was selected or that two-way L16 audio
worked. Payload 102 belongs to that offer and is not a fixed codec number.
A prior phone status snapshot showed G.711 mu-law reception, which reports
the codec used for that call rather than the phone's full capabilities.

A firsthand experiment using `SIP45.9-4-2SR4-3S` reported LINEAR 8/16 kHz in
DSP logs and a negotiated call with `slin16` as the only common codec.
It also reported **one-way audio** and unsuccessful attempts to select the
codec through guessed `preferredCodec` strings. This is a useful test lead,
not proof of reliable support or a diagnosed firmware defect.
[Author's experiment](https://goughlui.com/2022/12/18/project-connecting-cisco-ip-phone-7945-cp-7945g-with-freepbx/).

A separate pilot should restrict one endpoint to `slin16`, inspect both SDP
directions, and verify two-way audio, payload mapping, byte order, timestamps,
packetization, hold/resume and clean hangup. Retain the G.722 configuration
for rollback. First test a valid L16 offer without guessing additional phone
XML settings.

An isolated Asterisk fixture can establish the PBX's L16 path, but cannot
establish compatibility with the physical phone. Neither that L16 fixture
nor the physical pilot has been completed as part of this research.
