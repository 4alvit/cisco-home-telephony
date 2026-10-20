"""A synthetic caller exercising real SIP UDP, RTP and FFmpeg decoding."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import time

from .media import MediaReceiver
from .signaling import SipVideoServer


LOOPBACK = "127.0.0.1"
SCENARIOS = ("complete", "cancel", "ack-timeout", "malformed-rtp")


class DemoError(RuntimeError):
    """A fixed diagnostic, without input payloads or tool stderr."""


def dependencies() -> dict:
    tool = shutil.which("ffmpeg")
    if tool is None:
        return {"ffmpeg": False, "libx264": False, "h264": False, "png": False}
    result = {"ffmpeg": True}
    for flag, names in (("-encoders", ("libx264", "png")), ("-decoders", ("h264",))):
        try:
            process = subprocess.run([tool, "-hide_banner", flag], capture_output=True,
                                     timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise DemoError("ffmpeg_dependency_probe_failed") from None
        if process.returncode or len(process.stdout) > 1024 * 1024:
            raise DemoError("ffmpeg_dependency_probe_failed")
        text = process.stdout.decode("utf-8", "replace")
        for name in names:
            result[name] = bool(re.search(r"^\s*\S+\s+" + re.escape(name) + r"\s", text, re.M))
    return result


def synthetic_h264() -> bytes:
    """Generate three distinct baseline frames; never open a media device."""
    tool = shutil.which("ffmpeg")
    if tool is None:
        raise DemoError("ffmpeg_required")
    command = [
        tool, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=2",
        "-frames:v", "3", "-an", "-threads", "1", "-c:v", "libx264",
        "-profile:v", "baseline", "-level:v", "3.0", "-pix_fmt", "yuv420p",
        "-x264-params", "keyint=1:repeat-headers=1:aud=1:slices=1",
        "-f", "h264", "pipe:1",
    ]
    try:
        process = subprocess.run(command, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise DemoError("synthetic_encoder_failed") from None
    if process.returncode or not 0 < len(process.stdout) <= 1024 * 1024:
        raise DemoError("synthetic_encoder_failed")
    return process.stdout


def packetize(data: bytes, *, payload_type: int = 96, mtu: int = 1000) -> list[bytes]:
    """Synthetic RFC 6184 mode-1 sender: single NAL and FU-A, 2 fps timestamps."""
    if (not isinstance(data, bytes) or len(data) > 1024 * 1024
            or type(payload_type) is not int or not 96 <= payload_type <= 127
            or type(mtu) is not int or not 64 <= mtu <= 1200):
        raise DemoError("invalid_synthetic_packetizer_input")
    nals = [nal for nal in re.split(b"\x00\x00\x00?\x01", data) if nal]
    if not nals or any(not 1 <= (nal[0] & 31) <= 23 for nal in nals):
        raise DemoError("invalid_annex_b")
    units: list[list[bytes]] = []
    for nal in nals:
        if nal[0] & 31 == 9 or not units:
            units.append([])
        units[-1].append(nal)
    packets = []
    sequence = 1000
    for index, unit in enumerate(units):
        timestamp = index * 45000
        for nal_index, nal in enumerate(unit):
            if len(nal) <= mtu:
                payloads = [nal]
            else:
                body = nal[1:]
                chunks = [body[i:i + mtu - 2] for i in range(0, len(body), mtu - 2)]
                payloads = [
                    bytes(((nal[0] & 0xE0) | 28,
                           (nal[0] & 31) | (0x80 if i == 0 else 0)
                           | (0x40 if i == len(chunks) - 1 else 0))) + chunk
                    for i, chunk in enumerate(chunks)
                ]
            for part, payload in enumerate(payloads):
                marker = nal_index == len(unit) - 1 and part == len(payloads) - 1
                header = struct.pack("!BBHII", 0x80, payload_type | (0x80 if marker else 0),
                                     sequence & 0xFFFF, timestamp, 0x11223344)
                packets.append(header + payload)
                sequence += 1
    if len(packets) > 2048:
        raise DemoError("synthetic_packet_limit")
    return packets


class SyntheticCaller:
    """One bounded local SIP dialog; no registration, routing or external target."""

    def __init__(self, server_port: int):
        if type(server_port) is not int or not 1 <= server_port <= 65535:
            raise DemoError("invalid_local_server_port")
        self.target = (LOOPBACK, server_port)
        self.sip = None
        self.rtp = None
        try:
            self.sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sip.bind((LOOPBACK, 0))
            self.rtp.bind((LOOPBACK, 0))
            self.sip.settimeout(.2)
            self.port = self.sip.getsockname()[1]
            self.video_port = self.rtp.getsockname()[1]
        except BaseException:
            self.close()
            raise
        self.to = "<sip:receiver@127.0.0.1>"
        self.invite_final = None
        self.statuses: list[dict] = []

    def close(self):
        try:
            if self.sip is not None:
                self.sip.close()
        finally:
            if self.rtp is not None:
                self.rtp.close()

    def request(self, method: str, *, body: bytes = b"") -> bytes:
        if method not in ("INVITE", "ACK", "BYE", "CANCEL"):
            raise DemoError("unsupported_synthetic_method")
        cseq = 2 if method == "BYE" else 1
        branch = "z9hG4bK-demo-bye" if method == "BYE" else "z9hG4bK-demo-invite"
        if method == "ACK" and self.invite_final == 200:
            branch = "z9hG4bK-demo-ack"
        headers = [
            f"{method} sip:receiver@127.0.0.1:{self.target[1]} SIP/2.0",
            f"Via: SIP/2.0/UDP 127.0.0.1:{self.port};branch={branch};rport",
            "Max-Forwards: 1",
            f"From: <sip:caller@127.0.0.1>;tag=synthetic-caller",
            "To: " + (self.to if method in ("ACK", "BYE") else "<sip:receiver@127.0.0.1>"),
            "Call-ID: synthetic-video@127.0.0.1",
            f"CSeq: {cseq} {method}",
            f"Contact: <sip:caller@127.0.0.1:{self.port}>",
        ]
        if body:
            headers.append("Content-Type: application/sdp")
        headers.append("Content-Length: " + str(len(body)))
        wire = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body
        self.sip.sendto(wire, self.target)
        return wire

    def offer(self, profile: str = "42c01e") -> bytes:
        if not re.fullmatch("[0-9a-f]{6}", profile):
            raise DemoError("invalid_synthetic_profile")
        return ("\r\n".join([
            "v=0", "o=caller 1 1 IN IP4 127.0.0.1", "s=Synthetic video",
            "c=IN IP4 127.0.0.1", "t=0 0",
            "m=audio 40000 RTP/AVP 0", "a=sendrecv",
            f"m=video {self.video_port} RTP/AVP 96",
            "a=rtpmap:96 H264/90000",
            f"a=fmtp:96 packetization-mode=1;profile-level-id={profile}",
            "a=sendonly", "",
        ])).encode("ascii")

    def response(self, status: int, method: str, *, timeout: float = 5) -> tuple[dict, bytes]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, source = self.sip.recvfrom(8193)
            except socket.timeout:
                continue
            if source != self.target or len(data) > 8192:
                raise DemoError("unexpected_sip_response")
            try:
                head, body = data.split(b"\r\n\r\n", 1)
                lines = head.decode("ascii").split("\r\n")
                code = int(lines[0].split()[1])
                headers = dict((k.lower(), v.strip()) for k, v in
                               (line.split(":", 1) for line in lines[1:]))
                response_method = headers["cseq"].split()[1]
                if len(body) != int(headers["content-length"]):
                    raise ValueError
            except (ValueError, IndexError, KeyError, UnicodeError):
                raise DemoError("malformed_sip_response") from None
            self.statuses.append({"status": code, "method": response_method})
            if len(self.statuses) > 32:
                raise DemoError("too_many_sip_responses")
            if code == status and response_method == method:
                if method == "INVITE" and code >= 200:
                    self.to = headers["to"]
                    self.invite_final = code
                return headers, body
            if code >= 300:
                raise DemoError("sip_call_rejected")
        raise DemoError("sip_response_timeout")


def _wait(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise DemoError("lab_condition_timeout")


def run_demo(output: Path, scenario: str = "complete") -> dict:
    """Run actual local sockets and decoder; the supplied output is a fresh directory."""
    if scenario not in SCENARIOS:
        raise DemoError("unknown_scenario")
    output = Path(output)
    if not output.is_dir() or output.is_symlink() or any(output.iterdir()):
        raise DemoError("fresh_output_directory_required")
    source = synthetic_h264() if scenario == "complete" else None
    profile = "42c01e"
    if source is not None:
        sps = next((nal for nal in re.split(b"\x00\x00\x00?\x01", source)
                    if nal and nal[0] & 31 == 7), None)
        if sps is None or len(sps) < 4:
            raise DemoError("synthetic_sps_missing")
        profile = sps[1:4].hex()
    receivers = []

    def factory(offer):
        receiver = MediaReceiver(offer, output / "frames", max_frames=3, max_seconds=3).start()
        receivers.append(receiver)
        return receiver

    server = SipVideoServer(factory, answer_delay=.4 if scenario == "cancel" else .02,
                           ack_timeout=.35, call_timeout=5, server_timeout=15).start()
    caller = None
    before_cleanup = None
    packets_sent = 0
    try:
        caller = SyntheticCaller(server.port)
        caller.request("INVITE", body=caller.offer(profile))
        if scenario == "cancel":
            caller.response(180, "INVITE")
            caller.request("CANCEL")
            caller.response(200, "CANCEL")
            caller.response(487, "INVITE")
            caller.request("ACK")
            _wait(lambda: server.snapshot()["state"] == "closed")
            if receivers:
                raise DemoError("cancel_started_media")
        else:
            _, answer = caller.response(200, "INVITE")
            answer_text = answer.decode("ascii")
            if ("c=IN IP4 127.0.0.1" not in answer_text or
                    "m=audio 0 RTP/AVP 0" not in answer_text or
                    "a=recvonly" not in answer_text):
                raise DemoError("unexpected_sdp_answer")
            match = re.search(r"^m=video (\d+) RTP/AVP 96\r?$", answer_text, re.M)
            if not match:
                raise DemoError("missing_video_answer")
            media_port = int(match[1])
            if scenario == "ack-timeout":
                _wait(lambda: server.snapshot()["state"] == "closed")
            else:
                caller.request("ACK")
                _wait(lambda: server.snapshot()["state"] == "established")
                if scenario == "malformed-rtp":
                    caller.rtp.sendto(b"not an RTP packet", (LOOPBACK, media_port))
                    packets_sent = 1
                    _wait(lambda: receivers[0].snapshot()["error"] is not None)
                else:
                    packets = packetize(source)
                    for packet in packets:
                        caller.rtp.sendto(packet, (LOOPBACK, media_port))
                        packets_sent += 1
                        time.sleep(.001)
                    _wait(lambda: receivers[0].snapshot()["state"] in ("captured", "failed"))
                caller.request("BYE")
                caller.response(200, "BYE", timeout=8)
                _wait(lambda: server.snapshot()["state"] == "closed")
        before_cleanup = server.snapshot()
        media = receivers[0].snapshot() if receivers else None
        if media and (not media["socket_closed"] or media["worker_alive"] or media["decoder_running"]):
            raise DemoError("protocol_did_not_release_media")
        if scenario == "complete":
            if not media or media["error"] or media["decoded_frames"] != 3:
                raise DemoError("video_frames_not_decoded")
            if len({f["sha256"] for f in media["frames"]}) != 3:
                raise DemoError("synthetic_frames_not_distinct")
        elif media and (media["decoded_frames"] or (scenario == "malformed-rtp" and not media["error"])):
            raise DemoError("negative_scenario_produced_frames")
    finally:
        # The assertions above run BEFORE this last-resort fixture cleanup.
        try:
            if caller is not None:
                caller.close()
        finally:
            try:
                server.close()
            finally:
                for receiver in receivers:
                    receiver.close()
    result = {
        "schema_version": 1, "scenario": scenario, "success": True,
        "scope": "synthetic loopback SIP/RTP; decode after capture; no phone or audio leg",
        "sip_responses": caller.statuses, "rtp_packets_sent": packets_sent,
        "synthetic_source_sha256": hashlib.sha256(source).hexdigest() if source else None,
        "before_fixture_cleanup": before_cleanup, "after_cleanup": server.snapshot(),
        "media": receivers[0].snapshot() if receivers else None,
    }
    data = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode()
    from tools.firmware_lab.storage import write_private
    write_private(output, "result.json", data)
    return result
