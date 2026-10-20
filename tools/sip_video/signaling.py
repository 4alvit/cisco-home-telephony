"""A bounded, loopback-only SIP UAS for the synthetic H.264 experiment.

This deliberately narrow RFC 3261 / RFC 8866 / RFC 6184 subset is not a PBX,
registrar, general SIP stack, or production endpoint. It accepts one dialog,
never follows routing headers, and sends responses only to a validated local
request source. SIP transactions and the server lifetime have fixed bounds.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
from dataclasses import dataclass
import math
import re
import secrets
import socket
import threading
import time
from typing import Callable

MAX_MESSAGE = 8192
MAX_BODY = 4096
MAX_HEADERS = 32
MAX_CACHE = 32
LOOPBACK = "127.0.0.1"
_TOKEN = r"[A-Za-z0-9_.!%*+`'~-]+"
_URI = re.compile(r"sip:" + _TOKEN + r"@127\.0\.0\.1(?::([0-9]{1,5}))?\Z")
_TAG = re.compile(r"(?:^|;)\s*tag=(" + _TOKEN + r")(?=;|$)")


class SipError(ValueError):
    """A message is outside this experiment's strict supported subset."""


@dataclass(frozen=True)
class MediaSection:
    kind: str
    formats: tuple[str, ...]


@dataclass(frozen=True)
class VideoOffer:
    payload_type: int
    source_port: int
    packetization_mode: int
    sprop_parameter_sets: str | None = None
    profile_level_id: str | None = None
    media_sections: tuple[MediaSection, ...] = ()


@dataclass(frozen=True)
class SipRequest:
    method: str
    uri: str
    headers: dict[str, str]
    body: bytes
    cseq: int
    branch: str

    @property
    def key(self) -> tuple[str, str, int, str]:
        return (self.headers["call-id"], self.branch, self.cseq, self.method)


def _port(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,5}", value) or not 1024 <= int(value) <= 65535:
        raise SipError("only unprivileged loopback ports are supported")
    return int(value)


def _uri(value: str) -> None:
    match = _URI.fullmatch(value)
    if not match:
        raise SipError("only numeric IPv4 loopback SIP URIs are supported")
    if match[1]:
        _port(match[1])


def _address(value: str) -> str:
    match = re.fullmatch(r"<([^<>]+)>((?:;[A-Za-z0-9_.=~-]+)*)", value)
    if not match:
        raise SipError("a simple bracketed SIP address is required")
    _uri(match[1])
    if len(_TAG.findall(match[2])) > 1:
        raise SipError("duplicate address tag")
    return match[2]


def _tag(value: str) -> str | None:
    match = _TAG.search(value)
    return match[1] if match else None


def parse_request(data: bytes, source: tuple[str, int]) -> SipRequest:
    """Validate before responding: malformed and reflection-shaped input is dropped."""
    if source[0] != LOOPBACK or not 1024 <= source[1] <= 65535:
        raise SipError("request source is not an unprivileged loopback endpoint")
    if not data or len(data) > MAX_MESSAGE or b"\x00" in data:
        raise SipError("invalid SIP message size")
    head, sep, body = data.partition(b"\r\n\r\n")
    if not sep or len(body) > MAX_BODY:
        raise SipError("missing header terminator or oversized body")
    try:
        lines = head.decode("ascii").split("\r\n")
    except UnicodeDecodeError:
        raise SipError("SIP headers must be ASCII") from None
    if len(lines) > MAX_HEADERS + 1 or any(len(line) > 1024 for line in lines):
        raise SipError("SIP header count or line limit exceeded")
    if any(any(ord(c) < 32 or ord(c) == 127 for c in line) for line in lines):
        raise SipError("SIP header control character")
    first = re.fullmatch(r"(INVITE|ACK|BYE|CANCEL|OPTIONS) ([^ ]+) SIP/2\.0", lines[0])
    if not first:
        raise SipError("unsupported SIP request line")
    method, uri = first.groups()
    _uri(uri)
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if not colon or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", name):
            raise SipError("invalid or folded SIP header")
        name = name.lower()
        if name in headers:
            raise SipError("duplicate SIP header")
        headers[name] = value.strip()
    for name in ("via", "from", "to", "call-id", "cseq", "max-forwards", "content-length"):
        if not headers.get(name):
            raise SipError("missing required SIP header")
    if any(name in headers for name in ("route", "record-route", "require", "proxy-require")):
        raise SipError("routing and mandatory extensions are unsupported")
    if not re.fullmatch(r"[0-9]{1,4}", headers["content-length"]) or int(headers["content-length"]) != len(body):
        raise SipError("Content-Length does not match the datagram")
    if not re.fullmatch(r"[0-9]{1,2}", headers["max-forwards"]) or not 1 <= int(headers["max-forwards"]) <= 70:
        raise SipError("invalid Max-Forwards")
    if not re.fullmatch(r"[A-Za-z0-9_.@~-]{1,128}", headers["call-id"]):
        raise SipError("invalid Call-ID")
    seq = re.fullmatch(r"([0-9]{1,10}) (INVITE|ACK|BYE|CANCEL|OPTIONS)", headers["cseq"])
    if not seq or seq[2] != method or not 1 <= int(seq[1]) < 2**31:
        raise SipError("invalid CSeq")
    via = re.fullmatch(r"SIP/2\.0/UDP 127\.0\.0\.1:([0-9]{1,5});branch=(z9hG4bK" + _TOKEN + r")(;rport)?", headers["via"])
    if not via or _port(via[1]) != source[1]:
        raise SipError("Via must match the local request socket")
    _address(headers["from"])
    _address(headers["to"])
    if not _tag(headers["from"]):
        raise SipError("From tag is required")
    if "contact" in headers:
        _address(headers["contact"])
        if ";" in headers["contact"]:
            raise SipError("Contact parameters are unsupported")
    if method == "INVITE" and "contact" not in headers:
        raise SipError("INVITE requires a local Contact")
    if method != "INVITE" and body:
        raise SipError("only INVITE may carry a body")
    return SipRequest(method, uri, headers, body, int(seq[1]), via[2])


def parse_offer(body: bytes) -> VideoOffer:
    """Accept one sending H.264 stream; preserve other audio m-lines as disabled."""
    if not body or len(body) > MAX_BODY:
        raise SipError("SDP is missing or oversized")
    try:
        lines = body.decode("ascii").splitlines()
    except UnicodeDecodeError:
        raise SipError("SDP must be ASCII") from None
    if len(lines) > 64 or any(len(line) > 1024 or not re.fullmatch(r"[a-z]=[\x20-\x7e]*", line) for line in lines):
        raise SipError("invalid or oversized SDP line")
    if lines[:1] != ["v=0"] or sum(line.startswith("v=") for line in lines) != 1:
        raise SipError("unsupported SDP version")
    for prefix in ("o=", "s=", "t="):
        if sum(line.startswith(prefix) for line in lines) != 1:
            raise SipError("missing or duplicated SDP session field")
    origin = next(line[2:] for line in lines if line.startswith("o="))
    if not re.fullmatch(r"[^ ]+ [0-9]+ [0-9]+ IN IP4 127\.0\.0\.1", origin):
        raise SipError("SDP origin must be numeric IPv4 loopback")
    if "t=0 0" not in lines:
        raise SipError("scheduled SDP sessions are unsupported")
    sections: list[dict] = []
    session_connection = None
    session_direction = "sendrecv"
    for line in lines:
        if line.startswith("m="):
            bits = line[2:].split()
            if len(bits) < 4 or len(bits) > 19 or bits[0] not in ("video", "audio") or bits[2] != "RTP/AVP":
                raise SipError("only audio/video RTP/AVP media sections are supported")
            if any(not re.fullmatch(r"[0-9]{1,3}", item) or int(item) > 127 for item in bits[3:]):
                raise SipError("invalid RTP payload type")
            if len(set(bits[3:])) != len(bits[3:]):
                raise SipError("duplicate RTP payload type")
            sections.append({"kind": bits[0], "port": bits[1], "formats": bits[3:], "lines": [], "connection": None})
            if len(sections) > 4:
                raise SipError("too many SDP media sections")
        elif line.startswith("c="):
            if line != "c=IN IP4 127.0.0.1":
                raise SipError("SDP media connection must be numeric IPv4 loopback")
            if sections:
                if sections[-1]["connection"] is not None:
                    raise SipError("duplicate media connection")
                sections[-1]["connection"] = LOOPBACK
            else:
                if session_connection is not None:
                    raise SipError("duplicate session connection")
                session_connection = LOOPBACK
        elif line in ("a=sendrecv", "a=sendonly", "a=recvonly", "a=inactive") and not sections:
            session_direction = line[2:]
        elif line.startswith("a="):
            if line == "a=rtcp-mux" or line.startswith("a=rtcp-mux:"):
                raise SipError("multiplexed RTP/RTCP is unsupported")
            if line.startswith("a=rtcp:") and not re.fullmatch(r"a=rtcp:[0-9]{1,5}(?: IN IP4 127\.0\.0\.1)?", line):
                raise SipError("nonloopback RTCP is unsupported")
            if line.startswith(("a=candidate:", "a=ice-", "a=fingerprint:", "a=crypto:", "a=setup:", "a=group:")):
                raise SipError("ICE, bundled media and encrypted media are unsupported")
            if sections:
                sections[-1]["lines"].append(line[2:])
    videos = [item for item in sections if item["kind"] == "video"]
    if len(videos) != 1:
        raise SipError("exactly one video media section is required")
    for item in sections:
        if not (item["connection"] or session_connection):
            raise SipError("missing SDP media connection")
        if item["port"] != "0":
            _port(item["port"])
    video = videos[0]
    source_port = _port(video["port"])
    directions = [item for item in video["lines"] if item in ("sendrecv", "sendonly", "recvonly", "inactive")]
    if len(directions) > 1 or (directions[0] if directions else session_direction) not in ("sendrecv", "sendonly"):
        raise SipError("video offer must send media")
    for fmt in video["formats"]:
        payload = int(fmt)
        maps = [item for item in video["lines"] if item.startswith(f"rtpmap:{fmt} ")]
        if not 96 <= payload <= 127 or maps != [f"rtpmap:{fmt} H264/90000"]:
            continue
        fmtps = [item for item in video["lines"] if item.startswith(f"fmtp:{fmt} ")]
        if len(fmtps) > 1:
            raise SipError("duplicate H.264 format parameters")
        params: dict[str, str] = {}
        if fmtps:
            for part in fmtps[0].split(" ", 1)[1].split(";"):
                name, sep, value = part.strip().partition("=")
                if not sep or not name or name in params:
                    raise SipError("invalid or duplicate H.264 format parameter")
                params[name] = value
        mode = params.get("packetization-mode", "0")
        if mode not in ("0", "1"):
            continue
        profile = params.get("profile-level-id")
        if profile is not None and not re.fullmatch(r"[0-9a-fA-F]{6}", profile):
            raise SipError("invalid H.264 profile-level-id")
        sprop = params.get("sprop-parameter-sets")
        if sprop is not None:
            entries = sprop.split(",")
            if len(entries) != 2 or len(sprop) > 1024:
                raise SipError("bounded SPS and PPS parameter sets required")
            try:
                decoded = [base64.b64decode(entry, validate=True) for entry in entries]
            except ValueError:
                raise SipError("invalid H.264 parameter sets") from None
            if any(not entry or len(entry) > 512 for entry in decoded) or [entry[0] & 31 for entry in decoded] != [7, 8]:
                raise SipError("invalid H.264 SPS/PPS NAL types")
        return VideoOffer(payload, source_port, int(mode), sprop, profile,
                          tuple(MediaSection(item["kind"], tuple(item["formats"])) for item in sections))
    raise SipError("no supported dynamic H.264/90000 payload with packetization-mode 0 or 1")


def answer_sdp(offer: VideoOffer, port: int) -> bytes:
    _port(str(port))
    lines = ["v=0", "o=- 1 1 IN IP4 127.0.0.1", "s=Loopback H264 proof", "c=IN IP4 127.0.0.1", "t=0 0"]
    for media in offer.media_sections or (MediaSection("video", (str(offer.payload_type),)),):
        if media.kind == "audio":
            lines.extend(["m=audio 0 RTP/AVP " + " ".join(media.formats), "a=inactive"])
        else:
            lines.extend([f"m=video {port} RTP/AVP {offer.payload_type}", "a=recvonly", f"a=rtpmap:{offer.payload_type} H264/90000"])
            params = f"packetization-mode={offer.packetization_mode}"
            if offer.profile_level_id:
                params += f";profile-level-id={offer.profile_level_id}"
            lines.append(f"a=fmtp:{offer.payload_type} {params}")
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


class SipVideoServer:
    """One finite local dialog, with no background work after close().

    Closed dialogs retain their bounded transaction cache until close() or the
    server deadline, allowing UDP retransmissions. Their media is already closed.
    The media adapter must finish close() within 10 seconds, including decoding.
    """

    def __init__(self, media_factory: Callable, *, answer_delay: float = 0.05,
                 call_timeout: float = 10, ack_timeout: float = 2,
                 server_timeout: float = 30):
        for name, value, low, high in (("answer_delay", answer_delay, 0, 5),
                                      ("call_timeout", call_timeout, .05, 60),
                                      ("ack_timeout", ack_timeout, .05, 10),
                                      ("server_timeout", server_timeout, .1, 120)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{name} must be finite and between {low} and {high}")
        self.media_factory = media_factory
        self.answer_delay, self.call_timeout = answer_delay, call_timeout
        self.ack_timeout, self.server_timeout = ack_timeout, server_timeout
        self.port = 0
        self._socket = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._state = "new"
        self._reason = None
        self._invite = None
        self._peer = None
        self._offer = None
        self._media = None
        self._media_snapshot = None
        self._local_tag = secrets.token_hex(8)
        self._cache = OrderedDict()
        self._accepted = False
        self._last_cseq = 0
        self._dropped = 0
        self._responses = 0
        self._answer_at = self._ack_deadline = self._call_deadline = 0
        self._next_final_retry = 0
        self._retry_interval = .5
        self._final_response = None
        self._invite_response = None

    def start(self):
        owns_start = False
        try:
            with self._lock:
                if self._state != "new":
                    raise RuntimeError("server may only be started once")
                owns_start = True
                self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._socket.bind((LOOPBACK, 0))
                self._socket.settimeout(.02)
                self.port = self._socket.getsockname()[1]
                self._state = "listening"
                self._thread = threading.Thread(target=self._run, name="sip-video-loopback", daemon=True)
                self._thread.start()
        except BaseException:
            if owns_start:
                with self._lock:
                    self._stop.set()
                    self._state, self._reason = "closed", "start_failed"
                    if self._socket is not None:
                        self._socket.close()
                # A start implementation may have launched its worker before
                # raising. Join outside the lock used by the worker's finalizer.
                self.close()
            raise
        return self

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._stop.set()
        if (self._thread and self._thread.ident is not None
                and self._thread is not threading.current_thread()):
            self._thread.join(timeout=15)
            if self._thread.is_alive():
                raise RuntimeError("media close exceeded the server shutdown bound")
        with self._lock:
            if self._state == "new":
                self._state, self._reason = "closed", "server_closed"

    def snapshot(self) -> dict:
        with self._lock:
            media = self._media.snapshot() if self._media is not None else self._media_snapshot
            return {"state": self._state, "close_reason": self._reason,
                    "port": self.port, "accepted_dialogs": int(self._accepted),
                    "dropped_messages": self._dropped, "responses": self._responses,
                    "worker_alive": bool(self._thread and self._thread.is_alive()),
                    "socket_closed": self._socket is None or self._socket.fileno() < 0,
                    "media": media}

    def _send(self, data: bytes, peer):
        try:
            self._socket.sendto(data, peer)
            self._responses += 1
        except OSError:
            self._stop.set()

    def _response(self, request, peer, code, phrase, *, body=b"", cache=True):
        to = request.headers["to"]
        if code != 100 and not _tag(to):
            to += ";tag=" + self._local_tag
        via = request.headers["via"]
        if via.endswith(";rport"):
            via += "=" + str(peer[1])
        headers = [f"SIP/2.0 {code} {phrase}", "Via: " + via,
                   "From: " + request.headers["from"], "To: " + to,
                   "Call-ID: " + request.headers["call-id"], "CSeq: " + request.headers["cseq"],
                   f"Contact: <sip:video@127.0.0.1:{self.port}>"]
        if body:
            headers.append("Content-Type: application/sdp")
        headers.append(f"Content-Length: {len(body)}")
        data = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body
        if cache and request is self._invite:
            self._invite_response = data
        if cache:
            self._cache[(peer, request.key)] = data
            self._cache.move_to_end((peer, request.key))
            while len(self._cache) > MAX_CACHE:
                self._cache.popitem(last=False)
        self._send(data, peer)
        return data

    def _terminate(self, reason):
        if self._media is not None:
            media, self._media = self._media, None
            try:
                media.close(decode=self._state == "established" and reason in ("bye", "call_timeout"))
                self._media_snapshot = media.snapshot()
            except Exception:
                self._media_snapshot = {"state": "cleanup_failed"}
                reason = "media_cleanup_failed"
        self._state, self._reason = "closed", reason

    def _dialog_matches(self, request, peer):
        return (self._invite is not None and peer == self._peer
                and request.headers["call-id"] == self._invite.headers["call-id"]
                and _tag(request.headers["from"]) == _tag(self._invite.headers["from"])
                and _tag(request.headers["to"]) == self._local_tag)

    def _handle(self, request, peer, now):
        if (self._invite is not None and request.key == self._invite.key
                and peer == self._peer and self._invite_response is not None):
            self._send(self._invite_response, peer)
            return
        cached = self._cache.get((peer, request.key))
        if cached is not None:
            self._send(cached, peer)
            return
        if request.method == "OPTIONS":
            self._response(request, peer, 200, "OK")
        elif request.method == "INVITE":
            if self._accepted:
                self._response(request, peer, 486, "Busy Here")
                return
            if _tag(request.headers["to"]):
                self._response(request, peer, 481, "Call/Transaction Does Not Exist")
                return
            if request.headers.get("content-type", "").lower() != "application/sdp":
                self._response(request, peer, 415, "Unsupported Media Type")
                return
            try:
                self._offer = parse_offer(request.body)
            except SipError:
                self._response(request, peer, 488, "Not Acceptable Here")
                return
            self._accepted, self._invite, self._peer = True, request, peer
            self._last_cseq = request.cseq
            self._state = "ringing"
            self._answer_at = now + self.answer_delay
            self._call_deadline = now + self.call_timeout
            self._response(request, peer, 100, "Trying", cache=False)
            self._response(request, peer, 180, "Ringing")
        elif request.method == "CANCEL":
            invite = self._invite
            matches = (invite and peer == self._peer and request.cseq == invite.cseq
                       and request.branch == invite.branch and request.uri == invite.uri
                       and request.headers["call-id"] == invite.headers["call-id"]
                       and request.headers["from"] == invite.headers["from"]
                       and request.headers["to"] == invite.headers["to"])
            if not matches:
                self._response(request, peer, 481, "Call/Transaction Does Not Exist")
            else:
                self._response(request, peer, 200, "OK")
                if self._state == "ringing":
                    self._response(invite, peer, 487, "Request Terminated")
                    self._terminate("cancelled")
        elif request.method == "ACK":
            if (self._dialog_matches(request, peer) and request.cseq == self._invite.cseq
                    and self._state == "awaiting_ack"):
                self._state = "established"
        elif request.method == "BYE":
            if not self._dialog_matches(request, peer) or self._state not in ("awaiting_ack", "established"):
                self._response(request, peer, 481, "Call/Transaction Does Not Exist")
            elif request.cseq <= self._last_cseq:
                self._response(request, peer, 500, "Server Internal Error")
            else:
                self._last_cseq = request.cseq
                self._response(request, peer, 200, "OK")
                self._terminate("bye")

    def _tick(self, now):
        if self._state in ("ringing", "awaiting_ack", "established") and now >= self._call_deadline:
            if self._state == "ringing":
                self._response(self._invite, self._peer, 480, "Temporarily Unavailable")
            self._terminate("call_timeout")
        elif self._state == "ringing" and now >= self._answer_at:
            try:
                self._media = self.media_factory(self._offer)
                body = answer_sdp(self._offer, self._media.port)
            except Exception:
                self._response(self._invite, self._peer, 500, "Server Internal Error")
                self._terminate("media_start_failed")
                return
            self._final_response = self._response(self._invite, self._peer, 200, "OK", body=body)
            self._state = "awaiting_ack"
            self._ack_deadline = now + self.ack_timeout
            self._next_final_retry = now + self._retry_interval
        elif self._state == "awaiting_ack":
            if now >= self._ack_deadline:
                self._terminate("ack_timeout")
            elif now >= self._next_final_retry:
                self._send(self._final_response, self._peer)
                self._retry_interval = min(self._retry_interval * 2, 2)
                self._next_final_retry = now + self._retry_interval

    def _run(self):
        deadline = time.monotonic() + self.server_timeout
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= deadline:
                    with self._lock:
                        if self._state != "closed":
                            self._terminate("server_timeout")
                    break
                try:
                    data, peer = self._socket.recvfrom(MAX_MESSAGE + 1)
                except socket.timeout:
                    data = None
                except OSError:
                    break
                with self._lock:
                    if data is not None:
                        try:
                            request = parse_request(data, peer)
                        except SipError:
                            self._dropped += 1
                        else:
                            self._handle(request, peer, time.monotonic())
                    self._tick(time.monotonic())
        finally:
            with self._lock:
                if self._state != "closed":
                    self._terminate("server_closed")
                self._socket.close()
