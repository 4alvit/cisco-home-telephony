"""Bounded, pure-stdlib codec and packet helpers for an isolated L16 lab.

These helpers open no sockets. Packet timing describes observed delivery;
known-pattern comparisons describe synthetic PCM, never acoustic quality.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import math
import re
import struct
from collections.abc import Iterable, Sequence


SAMPLE_RATE = 16000
PACKET_MS = 20
SAMPLES_PER_PACKET = 320
PCM_BYTES_PER_PACKET = 640
MAX_PAYLOAD = 4096
MAX_PACKETS = 1000
MAX_SDP_BYTES = 16384
MAX_CAPTURE_SECONDS = 60
FAULTS = ("clean", "byteorder", "payload-type", "clock", "pacing", "drop", "reorder")


class ProtocolError(ValueError):
    """A bounded lab input does not satisfy the supported protocol subset."""


def _integer(value, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolError(f"invalid {label}")
    return value


@dataclass(frozen=True)
class CodecMapping:
    payload_type: int
    encoding: str
    clock_rate: int
    channels: int = 1

    def __post_init__(self):
        _integer(self.payload_type, 0, 127, "RTP payload type")
        _integer(self.clock_rate, 1, 192000, "RTP clock rate")
        _integer(self.channels, 1, 2, "channel count")
        if not isinstance(self.encoding, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,32}", self.encoding):
            raise ProtocolError("invalid codec name")
        object.__setattr__(self, "encoding", self.encoding.upper())
        if self.encoding == "G722" and (self.clock_rate != 8000 or self.channels != 1):
            raise ProtocolError("G722 RTP uses an 8000 Hz mono clock")

    @property
    def sample_rate(self) -> int:
        return 16000 if self.encoding == "G722" else self.clock_rate

    def timestamp_step(self, packet_ms: int = PACKET_MS) -> int:
        _integer(packet_ms, 1, 60, "packet interval")
        if self.clock_rate * packet_ms % 1000:
            raise ProtocolError("packet interval is not an integer RTP tick count")
        return self.clock_rate * packet_ms // 1000


@dataclass(frozen=True)
class AudioDescription:
    port: int
    address: str
    direction: str
    codecs: tuple[CodecMapping, ...]


_STATIC = {
    0: ("PCMU", 8000, 1), 8: ("PCMA", 8000, 1), 9: ("G722", 8000, 1),
    10: ("L16", 44100, 2), 11: ("L16", 44100, 1), 18: ("G729", 8000, 1),
}


def parse_audio_sdp(text: str | bytes) -> AudioDescription:
    """Read one RTP/AVP audio section; preserve its advertised payload order.

    IPv4 literals only, with no DNS lookup. This is an SDP parser, not network
    authorization: a harness must separately enforce its loopback boundary.
    Unrelated session/media attributes are ignored; conflicting mappings,
    multicast, extra media sections and malformed relevant fields are rejected.
    """
    if isinstance(text, bytes):
        try:
            text = text.decode("ascii")
        except UnicodeDecodeError:
            raise ProtocolError("SDP must be ASCII") from None
    if (not isinstance(text, str) or not text or len(text) > MAX_SDP_BYTES
            or not text.isascii() or "\x00" in text):
        raise ProtocolError("invalid bounded SDP text")
    lines = text.replace("\r\n", "\n").splitlines()
    if len(lines) > 256 or any(len(line) > 1024 for line in lines):
        raise ProtocolError("SDP line limit exceeded")
    session_address = media_address = None
    session_direction, media_direction = "sendrecv", None
    direction_seen = set()
    media = False
    port = None
    offered: list[int] = []
    mappings: dict[int, CodecMapping] = {}
    for line in lines:
        if not line:
            continue
        if not re.match(r"^[a-z]=", line) or any(ord(char) < 32 for char in line):
            raise ProtocolError("malformed SDP line")
        if line.startswith("m="):
            if media:
                raise ProtocolError("exactly one audio media section is supported")
            match = re.fullmatch(r"m=audio ([0-9]{1,5}) RTP/AVP ([0-9 ]+)", line)
            if not match:
                raise ProtocolError("one RTP/AVP audio section is required")
            port = _integer(int(match[1]), 0, 65535, "SDP media port")
            values = match[2].split()
            if not 1 <= len(values) <= 32 or any(len(item) > 3 for item in values):
                raise ProtocolError("invalid SDP payload list")
            offered = [_integer(int(item), 0, 127, "SDP payload type") for item in values]
            if len(set(offered)) != len(offered):
                raise ProtocolError("duplicate SDP payload type")
            media = True
        elif line.startswith("c="):
            match = re.fullmatch(r"c=IN IP4 ([0-9.]{7,15})", line)
            if not match:
                raise ProtocolError("SDP requires an IPv4 literal connection")
            try:
                address = ipaddress.IPv4Address(match[1])
            except ipaddress.AddressValueError:
                raise ProtocolError("invalid SDP connection address") from None
            if address.is_multicast or address == ipaddress.IPv4Address("255.255.255.255"):
                raise ProtocolError("multicast or broadcast SDP is unsupported")
            if media:
                if media_address is not None:
                    raise ProtocolError("duplicate SDP media connection")
                media_address = str(address)
            else:
                if session_address is not None:
                    raise ProtocolError("duplicate SDP session connection")
                session_address = str(address)
        elif line.startswith("a=rtpmap:"):
            if not media:
                raise ProtocolError("rtpmap must belong to the audio section")
            match = re.fullmatch(r"a=rtpmap:([0-9]{1,3}) ([A-Za-z0-9-]{1,32})/([0-9]{1,6})(?:/([12]))?", line)
            if not match:
                raise ProtocolError("malformed SDP rtpmap")
            payload_type = int(match[1])
            if payload_type not in offered or payload_type in mappings:
                raise ProtocolError("unoffered or duplicate SDP rtpmap")
            mapping = CodecMapping(payload_type, match[2], int(match[3]), int(match[4] or 1))
            if payload_type in _STATIC and (mapping.encoding, mapping.clock_rate, mapping.channels) != _STATIC[payload_type]:
                raise ProtocolError("SDP conflicts with a static RTP mapping")
            mappings[payload_type] = mapping
        elif line in ("a=sendrecv", "a=sendonly", "a=recvonly", "a=inactive"):
            if media in direction_seen:
                raise ProtocolError("conflicting SDP direction attributes")
            direction_seen.add(media)
            if media:
                media_direction = line[2:]
            else:
                session_direction = line[2:]
    if port is None or (media_address or session_address) is None:
        raise ProtocolError("SDP audio section or connection is missing")
    codecs = []
    for payload_type in offered:
        if payload_type in mappings:
            codecs.append(mappings[payload_type])
        elif payload_type in _STATIC:
            codecs.append(CodecMapping(payload_type, *_STATIC[payload_type]))
        elif payload_type >= 96:
            raise ProtocolError("dynamic RTP payload requires an explicit rtpmap")
        else:
            raise ProtocolError("unsupported unassigned static RTP mapping")
    return AudioDescription(port, media_address or session_address,
                            media_direction or session_direction, tuple(codecs))


def select_l16(description: AudioDescription) -> CodecMapping:
    """Select the first explicitly advertised dynamic 16 kHz mono L16 mapping."""
    if not isinstance(description, AudioDescription):
        raise ProtocolError("parsed audio description required")
    for mapping in description.codecs:
        if (96 <= mapping.payload_type <= 127 and mapping.encoding in ("L16", "L16-256")
                and mapping.clock_rate == SAMPLE_RATE and mapping.channels == 1):
            return mapping
    raise ProtocolError("SDP did not negotiate dynamic L16/16000 mono")


def pack_pcm16(samples: Sequence[int], byteorder: str = "big") -> bytes:
    if byteorder not in ("big", "little") or not isinstance(samples, Sequence):
        raise ProtocolError("PCM requires a bounded sample sequence and explicit byte order")
    if not 1 <= len(samples) <= MAX_PAYLOAD // 2:
        raise ProtocolError("PCM sample limit exceeded")
    values = [_integer(item, -32768, 32767, "signed PCM16 sample") for item in samples]
    return struct.pack((">" if byteorder == "big" else "<") + f"{len(values)}h", *values)


def unpack_pcm16(payload: bytes, byteorder: str = "big") -> tuple[int, ...]:
    if (byteorder not in ("big", "little") or not isinstance(payload, bytes)
            or not 2 <= len(payload) <= MAX_PAYLOAD or len(payload) % 2):
        raise ProtocolError("PCM must contain bounded complete 16-bit samples")
    return struct.unpack((">" if byteorder == "big" else "<") + f"{len(payload) // 2}h", payload)


@dataclass(frozen=True)
class AudioSocketFrame:
    message_type: int
    payload: bytes


def audiosocket_frame(pcm_le: bytes) -> bytes:
    """Frame already-little-endian mono PCM16/16000; do not swap it again."""
    unpack_pcm16(pcm_le, "little")
    return b"\x12" + struct.pack("!H", len(pcm_le)) + pcm_le


def parse_audiosocket_frame(wire: bytes) -> AudioSocketFrame:
    if not isinstance(wire, bytes) or not 3 <= len(wire) <= MAX_PAYLOAD + 3:
        raise ProtocolError("invalid bounded AudioSocket frame")
    message_type, size = struct.unpack("!BH", wire[:3])
    payload = wire[3:]
    if size != len(payload):
        raise ProtocolError("AudioSocket length does not match one complete frame")
    if message_type == 0x12:
        unpack_pcm16(payload, "little")
    elif message_type == 0x01:
        if size != 16:
            raise ProtocolError("AudioSocket UUID requires 16 bytes")
    elif message_type == 0x00:
        if size:
            raise ProtocolError("AudioSocket termination must be empty")
    elif message_type == 0x03:
        if size != 1 or payload not in b"0123456789*#ABCDabcd":
            raise ProtocolError("invalid AudioSocket DTMF frame")
    elif message_type == 0xFF:
        if size != 1:
            raise ProtocolError("AudioSocket error requires one code byte")
    else:
        raise ProtocolError("unsupported AudioSocket frame type")
    return AudioSocketFrame(message_type, payload)


@dataclass(frozen=True)
class RTPPacket:
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    marker: bool
    payload: bytes


def make_rtp(payload: bytes, payload_type: int, sequence: int, timestamp: int,
             ssrc: int, marker: bool = False) -> bytes:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_PAYLOAD:
        raise ProtocolError("invalid bounded RTP payload")
    _integer(payload_type, 0, 127, "RTP payload type")
    _integer(sequence, 0, 65535, "RTP sequence")
    _integer(timestamp, 0, 0xFFFFFFFF, "RTP timestamp")
    _integer(ssrc, 0, 0xFFFFFFFF, "RTP SSRC")
    if type(marker) is not bool:
        raise ProtocolError("RTP marker must be boolean")
    return struct.pack("!BBHII", 0x80, payload_type | (0x80 if marker else 0),
                       sequence, timestamp, ssrc) + payload


def parse_rtp(wire: bytes) -> RTPPacket:
    if not isinstance(wire, bytes) or not 13 <= len(wire) <= MAX_PAYLOAD + 12 + 60 + 260 + 255:
        raise ProtocolError("invalid bounded RTP packet")
    first, second, sequence, timestamp, ssrc = struct.unpack_from("!BBHII", wire)
    if first >> 6 != 2:
        raise ProtocolError("RTP version 2 required")
    offset = 12 + 4 * (first & 15)
    if offset >= len(wire):
        raise ProtocolError("truncated RTP CSRC list")
    if first & 16:
        if offset + 4 > len(wire):
            raise ProtocolError("truncated RTP extension header")
        words = struct.unpack_from("!H", wire, offset + 2)[0]
        if words > 64:
            raise ProtocolError("RTP extension exceeds limit")
        offset += 4 + words * 4
    end = len(wire)
    if first & 32:
        padding = wire[-1]
        if padding == 0 or padding > end - offset:
            raise ProtocolError("invalid RTP padding")
        end -= padding
    if not offset < end or end - offset > MAX_PAYLOAD:
        raise ProtocolError("invalid RTP payload boundary")
    return RTPPacket(second & 127, sequence, timestamp, ssrc, bool(second & 128), wire[offset:end])


def analyze_rtp(observations: Iterable[tuple[float, bytes]], codec: CodecMapping,
                expected_samples: Sequence[int] | None = None, *, packet_ms: int = PACKET_MS,
                pacing_tolerance_ms: float = 10.0) -> dict:
    """Diagnose one finite fixed-packetization stream, without guessing its PCM.

    ``expected_samples`` is an optional known repeating L16 frame. Only an
    exact synthetic match can demonstrate byte order. Without that reference,
    arbitrary PCM byte order is explicitly not evaluated.
    """
    if not isinstance(codec, CodecMapping):
        raise ProtocolError("codec mapping required")
    step = codec.timestamp_step(packet_ms)
    if (isinstance(pacing_tolerance_ms, bool) or not isinstance(pacing_tolerance_ms, (int, float))
            or not math.isfinite(pacing_tolerance_ms) or not 0 <= pacing_tolerance_ms <= 100):
        raise ProtocolError("invalid pacing tolerance")
    l16 = codec.encoding in ("L16", "L16-256") and codec.clock_rate == SAMPLE_RATE and codec.channels == 1
    g722 = codec.encoding == "G722"
    if not (l16 or g722):
        raise ProtocolError("analysis supports L16/16000 mono or G722 only")
    sample_count = SAMPLE_RATE * packet_ms // 1000
    frame_bytes = sample_count * 2 if l16 else 8000 * packet_ms // 1000
    expected_be = expected_le = None
    if expected_samples is not None:
        if not l16 or not isinstance(expected_samples, Sequence) or len(expected_samples) != sample_count:
            raise ProtocolError("known PCM reference must be one complete L16 frame")
        expected_be = pack_pcm16(expected_samples, "big")
        expected_le = pack_pcm16(expected_samples, "little")
    report = {
        "ok": False, "scope": "packet_diagnostics_only",
        "content_reference_supplied": expected_samples is not None,
        "packets": 0, "valid_packets": 0, "malformed_packets": 0,
        "payload_type_mismatches": 0, "ssrc_changes": 0, "frame_size_mismatches": 0,
        "clock_mismatches": 0, "missing_packets": 0, "reordered_packets": 0,
        "duplicate_packets": 0, "gap_events": 0, "pacing_outliers": 0,
        "clock_checks": 0, "pacing_intervals": 0,
        "pacing_max_deviation_ms": 0.0, "duration_seconds": 0.0,
        "negotiated_payload_type": codec.payload_type, "encoding": codec.encoding,
        "sample_rate": codec.sample_rate, "rtp_clock_rate": codec.clock_rate,
        "expected_timestamp_step": step, "expected_frame_bytes": frame_bytes,
        "byte_order": {"status": "not_checked", "big_endian_matches": 0,
                       "little_endian_matches": 0, "ambiguous_matches": 0, "unmatched": 0},
        "findings": [],
    }
    first_arrival = last_arrival = base_timestamp = ssrc = None
    high_sequence = high_extended = high_arrival = None
    seen: set[int] = set()
    byte_order = report["byte_order"]
    for observation in observations:
        report["packets"] += 1
        if report["packets"] > MAX_PACKETS:
            raise ProtocolError("capture packet limit exceeded")
        if not isinstance(observation, (tuple, list)) or len(observation) != 2:
            raise ProtocolError("observation must contain arrival time and packet bytes")
        arrival, wire = observation
        if (isinstance(arrival, bool) or not isinstance(arrival, (int, float))
                or not math.isfinite(arrival) or not 0 <= arrival <= 1e12):
            raise ProtocolError("invalid monotonic arrival time")
        if first_arrival is None:
            first_arrival = arrival
        if last_arrival is not None and arrival < last_arrival:
            raise ProtocolError("arrival observations must be in monotonic order")
        if arrival - first_arrival > MAX_CAPTURE_SECONDS:
            raise ProtocolError("capture duration exceeds limit")
        last_arrival = arrival
        try:
            packet = parse_rtp(wire)
        except ProtocolError:
            report["malformed_packets"] += 1
            continue
        report["valid_packets"] += 1
        if packet.payload_type != codec.payload_type:
            report["payload_type_mismatches"] += 1
            continue
        if ssrc is None:
            ssrc, base_timestamp = packet.ssrc, packet.timestamp
        elif ssrc != packet.ssrc:
            report["ssrc_changes"] += 1
            continue
        if len(packet.payload) != frame_bytes:
            report["frame_size_mismatches"] += 1
        if high_sequence is None:
            high_sequence, high_extended, high_arrival = packet.sequence, 0, arrival
            extended = 0
        else:
            delta = (packet.sequence - high_sequence) & 0xFFFF
            if delta >= 0x8000:
                delta -= 0x10000
            extended = high_extended + delta
            if extended in seen:
                report["duplicate_packets"] += 1
                continue
            if delta < 0:
                report["reordered_packets"] += 1
            elif delta > 0:
                if delta > 1:
                    report["gap_events"] += 1
                deviation = abs((arrival - high_arrival) * 1000 - delta * packet_ms)
                report["pacing_intervals"] += 1
                report["pacing_max_deviation_ms"] = max(report["pacing_max_deviation_ms"], deviation)
                if deviation > pacing_tolerance_ms + 1e-7:
                    report["pacing_outliers"] += 1
                high_sequence, high_extended, high_arrival = packet.sequence, extended, arrival
        seen.add(extended)
        if extended != 0:
            report["clock_checks"] += 1
        if packet.timestamp != (base_timestamp + extended * step) & 0xFFFFFFFF:
            report["clock_mismatches"] += 1
        if expected_be is not None:
            matches_be, matches_le = packet.payload == expected_be, packet.payload == expected_le
            key = ("ambiguous_matches" if matches_be and matches_le else
                   "big_endian_matches" if matches_be else
                   "little_endian_matches" if matches_le else "unmatched")
            byte_order[key] += 1
    if seen:
        report["missing_packets"] = max(seen) - min(seen) + 1 - len(seen)
    if first_arrival is not None:
        report["duration_seconds"] = round(last_arrival - first_arrival, 9)
    report["pacing_max_deviation_ms"] = round(report["pacing_max_deviation_ms"], 6)
    if expected_be is not None:
        if byte_order["big_endian_matches"] and byte_order["little_endian_matches"]:
            byte_order["status"] = "mixed"
        elif byte_order["little_endian_matches"]:
            byte_order["status"] = "matched_little_endian"
        elif byte_order["big_endian_matches"]:
            byte_order["status"] = "matched_big_endian"
        elif byte_order["ambiguous_matches"]:
            byte_order["status"] = "ambiguous"
        else:
            byte_order["status"] = "no_known_match"
    fields = {
        "malformed_packets": "malformed_rtp", "payload_type_mismatches": "payload_type",
        "ssrc_changes": "ssrc_change", "frame_size_mismatches": "frame_size",
        "clock_mismatches": "rtp_clock", "missing_packets": "packet_loss",
        "reordered_packets": "packet_reordering", "duplicate_packets": "duplicate_packet",
        "pacing_outliers": "packet_pacing",
    }
    for field, code in fields.items():
        if report[field]:
            report["findings"].append({"code": code, "count": report[field]})
    if byte_order["little_endian_matches"]:
        report["findings"].append({"code": "little_endian_on_l16_wire", "count": byte_order["little_endian_matches"]})
    if byte_order["unmatched"]:
        report["findings"].append({"code": "known_pattern_mismatch", "count": byte_order["unmatched"]})
    if not seen:
        report["findings"].append({"code": "no_negotiated_media", "count": 1})
    report["ok"] = not report["findings"]
    return report


@dataclass(frozen=True)
class RTPFixture:
    codec: CodecMapping
    observations: tuple[tuple[float, bytes], ...]
    expected_samples: tuple[int, ...] | None


def fixture_packets(fault: str = "clean", *, payload_type: int = 102,
                    frames: int = 6, encoding: str = "L16") -> RTPFixture:
    """Deterministic packet diagnostics fixture, with no network or audio device.

    G722 payload bytes are opaque geometry fixtures, not encoded test audio.
    L16 uses a known asymmetric signed pattern so byte swaps are falsifiable.
    """
    if fault not in FAULTS or encoding not in ("L16", "G722"):
        raise ProtocolError("unknown offline fixture")
    if not (encoding == "G722" and type(payload_type) is int and payload_type == 9):
        _integer(payload_type, 96, 127, "dynamic fixture payload type")
    _integer(frames, 4, 100, "fixture frame count")
    if encoding == "G722" and fault == "byteorder":
        raise ProtocolError("G722 payload byte order is not an L16 PCM test")
    codec = CodecMapping(payload_type, encoding, 16000 if encoding == "L16" else 8000)
    samples = tuple([-32768, -257, -1, 0, 1, 258, 32767]
                    + [(index * 811) % 65536 - 32768 for index in range(313)]) if encoding == "L16" else None
    payload = (pack_pcm16(samples, "little" if fault == "byteorder" else "big")
               if samples is not None else bytes(range(160)))
    selected_pt = 96 + (payload_type - 96 + 1) % 32 if fault == "payload-type" else payload_type
    step = (160 if encoding == "L16" else 320) if fault == "clock" else codec.timestamp_step()
    packets = [make_rtp(payload, selected_pt, (65533 + index) & 0xFFFF,
                        (0xFFFFFE00 + index * step) & 0xFFFFFFFF, 0x10203040, index == 0)
               for index in range(frames)]
    indices = list(range(frames))
    if fault == "drop":
        indices.remove(2)
    elif fault == "reorder":
        indices[2], indices[3] = indices[3], indices[2]
    interval = 0.005 if fault == "pacing" else 0.02
    observations = tuple(((index if fault == "drop" else position) * interval, packets[index])
                         for position, index in enumerate(indices))
    return RTPFixture(codec, observations, samples)
