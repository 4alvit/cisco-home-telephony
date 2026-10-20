"""Strict, finite loopback RTP/H.264 capture followed by pipe-only still decode.

This is a lab receiver, not a jitter buffer or a production video endpoint.
Loss, reordering, stream changes and unsupported H.264 syntax abort the capture.
No decoder is launched until the finite capture has ended and every accepted
NAL has passed the elementary-stream checks below.
"""

from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import math
import os
from pathlib import Path
import re
import selectors
import socket
import struct
import subprocess
import threading
import time

from tools.firmware_lab.video import VideoError, split_pngs


MAX_INPUT = 4 * 1024 * 1024
MAX_PACKET = 2048
MAX_PACKETS = 8192
MAX_NALS = 512
MAX_NAL = 256 * 1024
MAX_FRAMES = 8
MAX_SECONDS = 10.0
MAX_OUTPUT = MAX_FRAMES * (298 * 156 * 4 + 4096)
DECODE_SECONDS = 10.0
START_CODE = b"\x00\x00\x00\x01"


class MediaError(ValueError):
    """A finite lab capture could not be accepted."""


class _Bits:
    def __init__(self, data: bytes):
        self.data, self.position = data, 0

    def read(self, count: int) -> int:
        if not 0 <= count <= 32 or self.position + count > len(self.data) * 8:
            raise MediaError("truncated H.264 parameter set")
        result = 0
        for _ in range(count):
            result = (result << 1) | ((self.data[self.position // 8]
                                      >> (7 - self.position % 8)) & 1)
            self.position += 1
        return result

    def ue(self, maximum: int = 65535) -> int:
        zeros = 0
        while self.read(1) == 0:
            zeros += 1
            if zeros > 24:
                raise MediaError("H.264 Exp-Golomb value exceeds limit")
        value = (1 << zeros) - 1 + self.read(zeros)
        if value > maximum:
            raise MediaError("H.264 parameter value exceeds limit")
        return value

    def se(self, maximum: int = 65535) -> int:
        value = self.ue(maximum * 2)
        return (value + 1) // 2 if value & 1 else -(value // 2)


def _rbsp(data: bytes) -> bytes:
    """Remove valid prevention bytes; reject hidden Annex-B delimiters.

    RTP NAL bytes are later joined with start codes. Without this check, an
    embedded delimiter could smuggle an unchecked second SPS to the decoder.
    """
    result = bytearray()
    zeros = 0
    for index, value in enumerate(data):
        if zeros >= 2:
            if value < 3:
                raise MediaError("unescaped H.264 start-code sequence")
            if value == 3:
                if index + 1 == len(data) or data[index + 1] > 3:
                    raise MediaError("invalid H.264 prevention byte")
                zeros = 0
                continue
        result.append(value)
        zeros = zeros + 1 if value == 0 else 0
    return bytes(result)


def validate_sps(nal: bytes) -> dict:
    """Bound coded geometry and buffering before exposing bytes to FFmpeg."""
    if not 4 <= len(nal) <= 2048 or nal[0] & 0x9F != 7:
        raise MediaError("invalid SPS NAL")
    bits = _Bits(_rbsp(nal[1:]))
    profile, constraints, level = bits.read(8), bits.read(8), bits.read(8)
    if constraints & 3 or profile not in (66, 77, 100) or level > 31:
        raise MediaError("unsupported H.264 profile, constraints or level")
    sps_id = bits.ue(31)
    if profile == 100:
        if bits.ue(3) != 1 or bits.ue(6) != 0 or bits.ue(6) != 0:
            raise MediaError("only 8-bit 4:2:0 H.264 is supported")
        bits.read(1)  # qpprime_y_zero_transform_bypass_flag
        if bits.read(1):
            for index in range(8):
                if bits.read(1):
                    last, next_value = 8, 8
                    for _ in range(16 if index < 6 else 64):
                        if next_value:
                            next_value = (last + bits.se(128) + 256) % 256
                        last = next_value or last
    bits.ue(12)  # log2_max_frame_num_minus4
    order = bits.ue(2)
    if order == 0:
        bits.ue(12)
    elif order == 1:
        bits.read(1)
        bits.se()
        bits.se()
        for _ in range(bits.ue(255)):
            bits.se()
    refs = bits.ue(4)
    bits.read(1)  # gaps_in_frame_num_value_allowed_flag
    width = (bits.ue(63) + 1) * 16
    height = (bits.ue(47) + 1) * 16
    if bits.read(1) != 1:
        raise MediaError("interlaced H.264 is not supported")
    bits.read(1)
    left = right = top = bottom = 0
    if bits.read(1):
        left, right, top, bottom = (bits.ue(1024) for _ in range(4))
    visible_width = width - 2 * (left + right)
    visible_height = height - 2 * (top + bottom)
    if not (0 < visible_width <= width <= 1024
            and 0 < visible_height <= height <= 768):
        raise MediaError("H.264 coded or cropped dimensions exceed limit")
    # VUI and all remaining bits remain bounded by the SPS NAL length. We use
    # coded dimensions, rather than trusting display/cropping metadata.
    return {"id": sps_id, "width": visible_width, "height": visible_height,
            "coded_width": width, "coded_height": height, "reference_frames": refs}


def _pps(nal: bytes) -> tuple[int, int]:
    if not 2 <= len(nal) <= 2048:
        raise MediaError("invalid PPS NAL")
    bits = _Bits(_rbsp(nal[1:]))
    pps_id, sps_id = bits.ue(255), bits.ue(31)
    bits.read(1)
    bits.read(1)
    if bits.ue(7):
        raise MediaError("H.264 slice groups are not supported")
    return pps_id, sps_id


def validate_sprop(value: str | None) -> tuple[bytes, ...]:
    """Decode only small SPS/PPS sprop-parameter-sets from the negotiated SDP."""
    if value is None:
        return ()
    if not isinstance(value, str) or not 1 <= len(value) <= 8192:
        raise MediaError("invalid SDP parameter sets")
    parts = value.split(",")
    if len(parts) != 2:
        raise MediaError("SDP must contain exactly one SPS and one PPS")
    try:
        nals = tuple(base64.b64decode(part, validate=True) for part in parts)
    except (ValueError, binascii.Error):
        raise MediaError("invalid SDP parameter-set encoding") from None
    if any(not nal or nal[0] & 0x80 for nal in nals):
        raise MediaError("invalid SDP parameter-set NAL")
    if (nals[0][0] & 31, nals[1][0] & 31) != (7, 8):
        raise MediaError("SDP parameter sets must be SPS then PPS")
    metadata = validate_sps(nals[0])
    _, sps_id = _pps(nals[1])
    if sps_id != metadata["id"]:
        raise MediaError("PPS refers to an unavailable SPS")
    return nals


class RTPAssembler:
    """A small RFC 6184 subset: single NAL, STAP-A and FU-A, without recovery."""

    def __init__(self, payload_type: int, packetization_mode: int,
                 sprop_parameter_sets: str | None = None, *, max_frames: int = MAX_FRAMES,
                 profile_level_id: str | None = None):
        if type(payload_type) is not int or not 96 <= payload_type <= 127:
            raise MediaError("dynamic H.264 payload type required")
        if type(packetization_mode) is not int or packetization_mode not in (0, 1):
            raise MediaError("unsupported H.264 packetization mode")
        if type(max_frames) is not int or not 1 <= max_frames <= MAX_FRAMES:
            raise MediaError("frame limit must be between 1 and 8")
        if profile_level_id is not None and (not isinstance(profile_level_id, str)
                or re.fullmatch("[0-9A-Fa-f]{6}", profile_level_id) is None):
            raise MediaError("invalid H.264 profile-level-id")
        self.profile_level_id = bytes.fromhex(profile_level_id) if profile_level_id else None
        self.payload_type, self.packetization_mode = payload_type, packetization_mode
        self.max_frames = max_frames
        self.sequence = self.ssrc = self.timestamp = None
        self.marked = False
        self.fragment: bytearray | None = None
        self.fragment_header = None
        self.stream = bytearray()
        self.sps: tuple[bytes, dict] | None = None
        self.pps: tuple[bytes, int] | None = None
        self.nals = self.nal_bytes = self.access_units = self.input_bytes = self.packets = 0
        self.accepted_packets = 0
        self.has_vcl = self.unit_has_vcl = False
        for nal in validate_sprop(sprop_parameter_sets):
            self._nal(nal)

    @property
    def complete(self) -> bool:
        return self.access_units >= self.max_frames

    def _nal(self, nal: bytes) -> None:
        if not 1 <= len(nal) <= MAX_NAL or nal[0] & 0x80:
            raise MediaError("invalid H.264 NAL size or header")
        kind = nal[0] & 31
        if kind not in (1, 5, 6, 7, 8, 9, 10, 11, 12):
            raise MediaError("unsupported H.264 NAL type")
        rbsp = _rbsp(nal[1:])
        if kind in (6, 12):  # Metadata and filler are unnecessary for stills.
            return
        if kind == 7:
            metadata = validate_sps(nal)
            if self.profile_level_id is not None and nal[1:4] != self.profile_level_id:
                raise MediaError("SPS differs from negotiated profile-level-id")
            if self.sps is not None and self.sps[0] != nal:
                raise MediaError("SPS changes are not supported")
            self.sps = (nal, metadata)
        elif kind == 8:
            pps_id, sps_id = _pps(nal)
            if self.sps is None or self.sps[1]["id"] != sps_id:
                raise MediaError("PPS refers to an unavailable SPS")
            if self.pps is not None and self.pps[0] != nal:
                raise MediaError("PPS changes are not supported")
            self.pps = (nal, pps_id)
        elif kind in (1, 5):
            if self.sps is None or self.pps is None:
                raise MediaError("video slice arrived before SPS/PPS")
            bits = _Bits(rbsp)
            macroblocks = self.sps[1]["coded_width"] * self.sps[1]["coded_height"] // 256
            bits.ue(macroblocks - 1)
            bits.ue(9)
            if bits.ue(255) != self.pps[1]:
                raise MediaError("video slice refers to an unavailable PPS")
            self.has_vcl = self.unit_has_vcl = True
        if len(self.stream) + len(nal) + 4 > MAX_INPUT:
            raise MediaError("H.264 elementary stream exceeds byte limit")
        if self.nals >= MAX_NALS:
            raise MediaError("H.264 NAL count exceeds limit")
        self.stream.extend(START_CODE + nal)
        self.nals += 1
        self.nal_bytes += len(nal)

    def feed(self, packet: bytes) -> None:
        if self.complete:
            raise MediaError("capture frame limit reached")
        self.packets += 1
        self.input_bytes += len(packet)
        if self.packets > MAX_PACKETS or self.input_bytes > MAX_INPUT:
            raise MediaError("RTP capture exceeds packet or byte limit")
        if not 13 <= len(packet) <= MAX_PACKET:
            raise MediaError("RTP packet size exceeds limit")
        first, second, sequence, timestamp, ssrc = struct.unpack_from("!BBHII", packet)
        if first != 0x80:
            raise MediaError("RTP extensions, padding and CSRCs are unsupported")
        if second & 127 != self.payload_type:
            raise MediaError("RTP payload type differs from negotiated H.264")
        if self.sequence is not None and sequence != (self.sequence + 1) & 0xFFFF:
            raise MediaError("RTP sequence gap or reordering")
        if self.ssrc is not None and ssrc != self.ssrc:
            raise MediaError("RTP SSRC changed")
        if self.timestamp is not None:
            if timestamp == self.timestamp and self.marked:
                raise MediaError("RTP timestamp reused after marker")
            if timestamp != self.timestamp:
                if not self.marked or not 0 < (timestamp - self.timestamp) & 0xFFFFFFFF < 0x80000000:
                    raise MediaError("RTP timestamp changed before marker or moved backwards")
                self.unit_has_vcl = False
        self.sequence, self.timestamp, self.ssrc = sequence, timestamp, ssrc
        self.marked = bool(second & 128)
        payload = packet[12:]
        kind = payload[0] & 31
        if payload[0] & 0x80:
            raise MediaError("forbidden H.264 payload bit")
        if 1 <= kind <= 23:
            if self.fragment is not None:
                raise MediaError("NAL interrupted an unfinished FU-A")
            self._nal(payload)
        elif kind == 24 and self.packetization_mode == 1:
            if self.fragment is not None:
                raise MediaError("STAP-A interrupted an unfinished FU-A")
            offset, members = 1, 0
            while offset < len(payload):
                if offset + 2 > len(payload):
                    raise MediaError("truncated STAP-A length")
                length = struct.unpack_from("!H", payload, offset)[0]
                offset += 2
                if not length or offset + length > len(payload):
                    raise MediaError("truncated or empty STAP-A NAL")
                self._nal(payload[offset:offset + length])
                offset += length
                members += 1
                if members > 32:
                    raise MediaError("STAP-A member limit exceeded")
            if not members:
                raise MediaError("empty STAP-A")
        elif kind == 28 and self.packetization_mode == 1:
            if len(payload) < 3:
                raise MediaError("empty FU-A fragment")
            flags = payload[1]
            start, end = bool(flags & 128), bool(flags & 64)
            if flags & 32 or not 1 <= flags & 31 <= 23 or (start and end):
                raise MediaError("invalid FU-A flags or type")
            header = (payload[0] & 0xE0) | (flags & 31)
            if start:
                if self.fragment is not None:
                    raise MediaError("overlapping FU-A fragments")
                self.fragment = bytearray([header])
                self.fragment_header = header
            elif self.fragment is None or self.fragment_header != header:
                raise MediaError("FU-A continuation has no matching start")
            self.fragment.extend(payload[2:])
            if len(self.fragment) > MAX_NAL:
                raise MediaError("FU-A NAL exceeds byte limit")
            if end:
                nal = bytes(self.fragment)
                self.fragment = None
                self._nal(nal)
        else:
            raise MediaError("unsupported RTP H.264 aggregation or fragmentation")
        if self.marked:
            if self.fragment is not None or not self.unit_has_vcl:
                raise MediaError("RTP marker does not end a complete video access unit")
            self.access_units += 1
        self.accepted_packets += 1

    def finish(self) -> bytes:
        if self.fragment is not None or (self.packets and not self.marked):
            raise MediaError("capture ended with incomplete RTP access unit")
        return bytes(self.stream) if self.has_vcl else b""


def decoder_command(frame_count: int) -> list[str]:
    if type(frame_count) is not int or not 1 <= frame_count <= MAX_FRAMES:
        raise MediaError("invalid decode frame count")
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-max_alloc", "16777216", "-threads", "1", "-filter_threads", "1",
        "-protocol_whitelist", "pipe", "-format_whitelist", "h264",
        "-probesize", "1048576", "-analyzeduration", "1000000", "-f", "h264",
        "-i", "pipe:0", "-map", "0:v:0", "-an", "-sn", "-dn", "-map_metadata", "-1",
        "-vf", "scale=298:156:force_original_aspect_ratio=decrease,"
               "pad=298:156:(ow-iw)/2:(oh-ih)/2:color=0x101820,setsar=1",
        "-frames:v", str(frame_count), "-threads", "1", "-c:v", "png",
        "-pix_fmt", "rgb24", "-fs", str(MAX_OUTPUT), "-f", "image2pipe", "pipe:1",
    ]


def decode_stills(data: bytes, frame_count: int, *, process_hook=None) -> list[bytes]:
    """Pipe-only decoder with finite input, drained-output cap and a wall timeout."""
    if not data or len(data) > MAX_INPUT:
        raise MediaError("invalid bounded elementary stream")
    # Revalidate every NAL at the decoder boundary, including any direct call
    # to this helper. Only our canonical four-byte delimiters are accepted.
    if not data.startswith(START_CODE):
        raise MediaError("invalid elementary-stream delimiter")
    checker = RTPAssembler(96, 1, max_frames=frame_count)
    for nal in data[len(START_CODE):].split(START_CODE):
        checker._nal(nal)
    if not checker.has_vcl:
        raise MediaError("elementary stream contains no video slices")
    process = None
    selector = selectors.DefaultSelector()
    try:
        process = subprocess.Popen(decoder_command(frame_count), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   bufsize=0)
        if process_hook:
            process_hook(process)
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE)
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline, offset, output = time.monotonic() + DECODE_SECONDS, 0, bytearray()
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MediaError("decoder exceeded time limit")
            try:
                ready = selector.select(min(remaining, 0.1))
            except InterruptedError:
                continue
            for key, _ in ready:
                if key.fileobj is process.stdin:
                    try:
                        count = os.write(key.fd, data[offset:offset + 65536])
                    except BrokenPipeError:
                        selector.unregister(process.stdin)
                        process.stdin.close()
                        continue
                    except OSError as exc:
                        # Readiness is advisory. A retry must not consume
                        # input or reset the absolute decoder deadline.
                        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                            continue
                        raise
                    offset += count
                    if offset == len(data):
                        selector.unregister(process.stdin)
                        process.stdin.close()
                else:
                    try:
                        chunk = os.read(key.fd, min(65536, MAX_OUTPUT - len(output) + 1))
                    except OSError as exc:
                        # EAGAIN is not EOF; keep the pipe registered and
                        # retain all output already collected.
                        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                            continue
                        raise
                    if not chunk:
                        selector.unregister(process.stdout)
                        process.stdout.close()
                    output.extend(chunk)
                    if len(output) > MAX_OUTPUT:
                        raise MediaError("decoder output exceeded byte limit")
        try:
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise MediaError("decoder exceeded time limit") from None
        if process.returncode:
            raise MediaError("decoder rejected the bounded H.264 capture")
        try:
            return split_pngs(bytes(output), frame_count)
        except VideoError:
            raise MediaError("decoder did not produce valid 298x156 PNG frames") from None
    except FileNotFoundError:
        raise MediaError("required ffmpeg executable is unavailable") from None
    finally:
        selector.close()
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            for stream in (process.stdin, process.stdout):
                if stream is not None and not stream.closed:
                    stream.close()
        if process_hook:
            process_hook(None)


class MediaReceiver:
    """One finite exact-peer UDP capture; closing performs deferred still decode."""

    def __init__(self, offer, output_dir: Path, *, max_frames: int = MAX_FRAMES,
                 max_seconds: float = MAX_SECONDS):
        if (isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float))
                or not math.isfinite(max_seconds) or not 0 < max_seconds <= MAX_SECONDS):
            raise MediaError("capture timeout must be positive and at most 10 seconds")
        if type(offer.source_port) is not int or not 1024 <= offer.source_port <= 65535:
            raise MediaError("expected unprivileged loopback source port")
        self.assembler = RTPAssembler(offer.payload_type, offer.packetization_mode,
                                      offer.sprop_parameter_sets, max_frames=max_frames,
                                      profile_level_id=getattr(offer, "profile_level_id", None))
        self.peer = ("127.0.0.1", offer.source_port)
        self.output_dir = Path(output_dir)
        self.max_frames, self.max_seconds = max_frames, max_seconds
        self.port = None
        self._socket = self._thread = self._decoder = None
        self._stop = threading.Event()
        self._close_lock = threading.Lock()
        self._closed = False
        self.state, self.error = "new", None
        self.packets_received = self.ignored_sources = 0
        self.frames: list[dict] = []

    def start(self):
        if self.state != "new":
            raise MediaError("receiver can only be started once")
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._socket.bind(("127.0.0.1", 0))
            self._socket.settimeout(0.05)
            self.port = self._socket.getsockname()[1]
            self.state = "receiving"
            self._thread = threading.Thread(target=self._receive, daemon=True,
                                            name="sip-video-loopback")
            self._thread.start()
        except BaseException:
            self._socket.close()
            raise
        return self

    def _receive(self):
        deadline = time.monotonic() + self.max_seconds
        try:
            while not self._stop.is_set():
                if time.monotonic() >= deadline:
                    if self.assembler.packets:
                        self.assembler.finish()
                    self.state = "captured"
                    return
                try:
                    packet, address = self._socket.recvfrom(MAX_PACKET + 1)
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop.is_set():
                        return
                    raise MediaError("loopback RTP socket failed") from None
                if self._stop.is_set():
                    return
                self.packets_received += 1
                if self.packets_received > MAX_PACKETS:
                    raise MediaError("received packet limit exceeded")
                if address != self.peer:
                    self.ignored_sources += 1
                    continue
                self.assembler.feed(packet)
                if self.assembler.complete:
                    self.state = "captured"
                    return
        except MediaError as exc:
            self.error, self.state = str(exc), "failed"
            self.assembler.stream.clear()
            self.assembler.fragment = None
        finally:
            self._socket.close()

    def _process(self, process):
        self._decoder = process

    def close(self, *, decode: bool = True) -> dict:
        with self._close_lock:
            if self._closed:
                return self.snapshot()
            self._closed = True
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=1.0)
                if self._thread.is_alive():
                    self._socket.close()
                    self._thread.join(timeout=1.0)
                if self._thread.is_alive():
                    self.error = "receiver did not stop within limit"
            elif self._socket is not None:
                self._socket.close()
            created: list[Path] = []
            created_directory = None
            try:
                if decode and not self.error:
                    data = self.assembler.finish()
                    if data:
                        frames = decode_stills(data, self.max_frames,
                                               process_hook=self._process)
                        # The caller supplies a fresh child path. Refuse
                        # symlink ancestry and overwrite; remove only our files.
                        absolute = self.output_dir.absolute()
                        if any(part.is_symlink() for part in (absolute, *absolute.parents)):
                            raise MediaError("output directory must not contain symlinks")
                        absolute.mkdir(mode=0o700)
                        created_directory = absolute
                        for index, frame in enumerate(frames):
                            filename = f"frame-{index:03d}.png"
                            path = absolute / filename
                            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                         | os.O_NOFOLLOW, 0o600)
                            created.append(path)
                            with os.fdopen(fd, "wb") as stream:
                                stream.write(frame)
                            self.frames.append({"file": filename, "bytes": len(frame),
                                                "sha256": hashlib.sha256(frame).hexdigest()})
                self.state = "failed" if self.error else "closed"
            except (MediaError, OSError) as exc:
                self.error = str(exc) if isinstance(exc, MediaError) else "private frame write failed"
                self.state = "failed"
                for path in created:
                    path.unlink(missing_ok=True)
                if created_directory is not None:
                    try:
                        created_directory.rmdir()
                    except OSError:
                        pass
                self.frames.clear()
            finally:
                self.assembler.stream.clear()
                self.assembler.fragment = None
            return self.snapshot()

    def snapshot(self) -> dict:
        return {
            "state": self.state, "error": self.error,
            "packets_received": self.packets_received,
            "packets_accepted": self.assembler.accepted_packets,
            "ignored_sources": self.ignored_sources,
            "input_bytes": self.assembler.input_bytes,
            "nal_bytes": self.assembler.nal_bytes, "nals": self.assembler.nals,
            "access_units": self.assembler.access_units,
            "decoded_frames": len(self.frames), "frames": list(self.frames),
            "socket_closed": self._socket is None or self._socket.fileno() == -1,
            "worker_alive": self._thread is not None and self._thread.is_alive(),
            "decoder_running": self._decoder is not None and self._decoder.poll() is None,
        }
