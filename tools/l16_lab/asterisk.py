"""Disposable, network-none Asterisk L16 interoperability experiment.

This module never targets an existing container, host PBX, or physical phone.
Its internal SIP and AudioSocket peers run only inside the owned container.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import signal
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import time
import uuid

from . import protocol

OWNER_LABEL = "org.cisco-home-telephony.l16-lab-owner"
CASE_SECONDS = 8
FRAME_COUNT = 80
FRAME_SAMPLES = 320
AUDIO_PORT = 9019
TOKEN = uuid.UUID("11111111-2222-4333-8444-555555555555")
CASES = (
    ("incoming_l16_102", True, "L16", 102),
    ("incoming_l16_110", True, "L16", 110),
    ("outgoing_l16", False, "L16", None),
    ("incoming_g722_fallback", True, "G722", 9),
    ("outgoing_g722_fallback", False, "G722", 9),
)


class LabError(RuntimeError):
    """A bounded lab assertion failed, without exposing private configuration."""


def need(condition, message):
    if not condition:
        raise LabError(message)


def describe_plan():
    """Pure description: does not invoke Docker or inspect the host network."""
    return {"status": "PLAN", "network": "none", "published_ports": [],
            "physical_phone_access": False, "existing_container_access": False,
            "case_timeout_seconds": CASE_SECONDS, "work_timeout_seconds": 180,
            "cases": [row[0] for row in CASES],
            "requires": ["explicit local Docker context or Unix socket", "local Asterisk 22.11 image",
                         "PJSIP, AudioSocket, slin16, G.722, Python 3, FFmpeg in image"],
            "claim": "Synthetic PBX interoperability only; physical Cisco compatibility remains untested."}


def fixture_configs():
    """Only synthetic loopback configuration; absolute timeout precedes Answer."""
    return {
        "asterisk.conf": """[directories]
astetcdir => /etc/asterisk
astmoddir => /usr/lib/asterisk/modules
astvarlibdir => /var/lib/asterisk
astdbdir => /var/lib/asterisk/db
astkeydir => /var/lib/asterisk/keys
astdatadir => /var/lib/asterisk
astagidir => /var/lib/asterisk/agi-bin
astspooldir => /var/spool/asterisk
astrundir => /run/asterisk
astlogdir => /var/log/asterisk
[options]
verbose=0
debug=0
""",
        "modules.conf": "[modules]\nautoload=yes\nnoload=chan_sip.so\nnoload=chan_iax2.so\nnoload=chan_mgcp.so\nnoload=chan_skinny.so\nnoload=res_http_websocket.so\nnoload=res_ari.so\n",
        "pjsip.conf": """[global]
type=global
user_agent=L16-Isolated-Lab
[transport]
type=transport
protocol=udp
bind=127.0.0.1:5060
[lab]
type=endpoint
transport=transport
context=l16-lab
disallow=all
allow=slin16,g722
aors=lab
identify_by=ip
direct_media=no
rtp_symmetric=no
force_rport=yes
rewrite_contact=no
ice_support=no
rtp_timeout=5
[lab]
type=aor
contact=sip:lab@127.0.0.2:5090
qualify_frequency=0
[loopback]
type=identify
endpoint=lab
match=127.0.0.2/32
""",
        "extensions.conf": f"""[general]
static=yes
writeprotect=yes
[l16-lab]
exten => 9001,1,Set(TIMEOUT(absolute)={CASE_SECONDS})
 same => n,Answer()
 same => n,Dial(AudioSocket/127.0.0.1:{AUDIO_PORT}/{TOKEN}/c(slin16),3,g)
 same => n,Hangup()
exten => 9002,1,Set(TIMEOUT(absolute)=3)
 same => n,Answer()
 same => n,Dial(AudioSocket/127.0.0.1:{AUDIO_PORT}/{TOKEN}/c(slin16),3,g)
 same => n,Hangup()
exten => _X!,1,Hangup(1)
[l16-sentinel]
exten => s,1,Set(TIMEOUT(absolute)=15)
 same => n,Answer()
 same => n,Wait(12)
 same => n,Hangup()
""",
        "rtp.conf": "[general]\nrtpstart=10000\nrtpend=10019\nstrictrtp=yes\nicesupport=no\n",
        "manager.conf": "[general]\nenabled=no\n",
        "http.conf": "[general]\nenabled=no\n",
        "logger.conf": "[general]\n[logfiles]\nconsole => warning,error\n",
    }


def _docker_env():
    return {key: value for key, value in os.environ.items()
            if key not in {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_API_VERSION"}}


class DockerTarget:
    """Explicit local target, with ambient Docker routing overrides removed."""
    def __init__(self, *, docker_context=None, docker_socket=None):
        need(bool(docker_context) != bool(docker_socket), "specify exactly one Docker context or Unix socket")
        self.context, self.socket = docker_context, docker_socket
        if docker_context:
            need(isinstance(docker_context, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", docker_context),
                 "invalid Docker context name")
            self.prefix = ["docker", "--context", docker_context]
        else:
            need(isinstance(docker_socket, str) and docker_socket.startswith("/") and "\x00" not in docker_socket,
                 "Docker socket must be an absolute local path")
            self.prefix = ["docker", "--host", "unix://" + docker_socket]

    def command(self, args, *, timeout=10, check=True):
        result = subprocess.run(self.prefix + list(args), capture_output=True, text=True,
                                env=_docker_env(), timeout=timeout, check=False)
        need(len(result.stdout) + len(result.stderr) <= 2_000_000, "Docker response exceeded limit")
        if check:
            need(result.returncode == 0, "isolated Docker command failed: " + str(args[0]))
        return result

    def verify_local(self):
        if self.context:
            result = self.command(["context", "inspect", self.context, "--format", "{{json .Endpoints.docker.Host}}"])
            try:
                endpoint = json.loads(result.stdout)
            except ValueError:
                raise LabError("invalid Docker context endpoint") from None
            need(isinstance(endpoint, str) and endpoint.startswith("unix:///"), "Docker context must use a local Unix socket")
            path = endpoint[7:]
        else:
            path = self.socket
        try:
            mode = Path(path).stat().st_mode
        except OSError:
            raise LabError("explicit Docker socket is unavailable") from None
        need(stat.S_ISSOCK(mode), "explicit Docker target is not a Unix socket")
        return self


class OwnedContainer:
    def __init__(self, target):
        self.target = target
        self.nonce = secrets.token_hex(16)
        self.name = "cisco-l16-lab-" + self.nonce
        self.identity = None
        self.attempted = False
        self.created = False

    def _named_ids(self):
        result = self.target.command(["container", "ls", "--all", "--no-trunc", "--filter",
                                      "name=^/" + self.name + "$", "--format", "{{.ID}}"], timeout=5)
        ids = result.stdout.split()
        need(len(ids) <= 1 and all(re.fullmatch(r"[0-9a-f]{64}", value) for value in ids), "invalid owned-container lookup")
        return ids

    def inspect_owned(self):
        template = '[{{json .Id}},{{json .Name}},{{json .Config.Labels}},{{json .HostConfig.NetworkMode}},{{json .HostConfig.PortBindings}}]'
        result = self.target.command(["inspect", "--type", "container", "--format", template, self.name], timeout=5, check=False)
        if result.returncode:
            need(not self._named_ids(), "could not inspect owned container")
            return None
        try:
            identity, name, labels, network, ports = json.loads(result.stdout)
        except (ValueError, TypeError):
            raise LabError("invalid owned-container metadata") from None
        need(isinstance(identity, str) and re.fullmatch(r"[0-9a-f]{64}", identity)
             and name == "/" + self.name and isinstance(labels, dict)
             and labels.get(OWNER_LABEL) == self.nonce, "container ownership mismatch; removal refused")
        need(self.identity is None or self.identity == identity, "container identity changed; removal refused")
        need(network == "none" and not ports, "container network isolation not applied")
        return identity

    def create(self, image):
        need(isinstance(image, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,255}", image), "invalid local image reference")
        self.attempted = True
        result = self.target.command(["create", "--pull", "never", "--name", self.name,
                                      "--label", OWNER_LABEL + "=" + self.nonce,
                                      "--network", "none", "--cpus", "1", "--memory", "512m",
                                      "--pids-limit", "128", "--user", "10001:10001", "--cap-drop", "ALL",
                                      "--security-opt", "no-new-privileges", "--env", "L16_LAB_TOKEN=" + self.nonce,
                                      image], timeout=15)
        self.identity = result.stdout.strip()
        need(re.fullmatch(r"[0-9a-f]{64}", self.identity), "invalid created container identity")
        need(self.inspect_owned() == self.identity, "created container ownership missing")
        self.created = True
        return self.identity

    def cleanup(self):
        if not self.attempted:
            return
        deadline = time.monotonic() + 10
        while True:
            identity = self.inspect_owned()
            if identity:
                break
            if self.created:
                return
            need(time.monotonic() < deadline, "container cleanup unconfirmed after uncertain create")
            time.sleep(.1)
        self.target.command(["rm", "-f", identity], timeout=10)
        need(not self._named_ids(), "owned container removal not confirmed")


@contextlib.contextmanager
def isolated_container(target):
    need(threading.current_thread() is threading.main_thread(), "container runner requires the main thread")
    owner = OwnedContainer(target)
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGALRM)
    handlers = {sig: signal.getsignal(sig) for sig in signals}
    timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def expired(_signum, _frame):
        raise LabError("isolated fixture interrupted or exceeded its work deadline")

    for sig in signals:
        signal.signal(sig, expired)
    signal.setitimer(signal.ITIMER_REAL, 180)
    try:
        yield owner
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.setitimer(signal.ITIMER_REAL, 30)
        try:
            owner.cleanup()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
            if timer[0]:
                signal.setitimer(signal.ITIMER_REAL, max(.001, timer[0] - (time.monotonic() - started)), timer[1])


def run_isolated(image, *, docker_context=None, docker_socket=None):
    target = DockerTarget(docker_context=docker_context, docker_socket=docker_socket).verify_local()
    with isolated_container(target) as owner, tempfile.TemporaryDirectory(prefix="cisco-l16-lab-") as temporary:
        root = Path(temporary)
        etc = root / "etc/asterisk"
        package = root / "opt/l16-lab/tools/l16_lab"
        etc.mkdir(parents=True)
        package.mkdir(parents=True)
        # Docker copies these synthetic assets as root while Asterisk runs as
        # UID 10001. Make only the copied tree readable/traversable regardless
        # of inherited umask; keep the host temporary root private.
        for directory in (root / "etc", etc, root / "opt", root / "opt/l16-lab",
                          root / "opt/l16-lab/tools", package):
            directory.chmod(0o755)
        for name, text in fixture_configs().items():
            destination = etc / name
            destination.write_text(text)
            destination.chmod(0o644)
        for name in ("asterisk.py", "protocol.py"):
            destination = package / name
            shutil.copyfile(Path(__file__).with_name(name), destination)
            destination.chmod(0o644)
        identity = owner.create(image)
        target.command(["cp", str(root) + "/.", identity + ":/"], timeout=15)
        target.command(["start", identity], timeout=15)
        deadline = time.monotonic() + 25
        while True:
            result = target.command(["exec", identity, "asterisk", "-rx", "dialplan show 9001@l16-lab"], timeout=5, check=False)
            if result.returncode == 0 and "TIMEOUT(absolute)=" + str(CASE_SECONDS) in result.stdout and "AudioSocket/" in result.stdout:
                break
            need(time.monotonic() < deadline, "isolated Asterisk dialplan did not become ready")
            time.sleep(.2)
        result = target.command(["exec", "--workdir", "/opt/l16-lab", identity, "python3", "-c",
                                 "import json; from tools.l16_lab.asterisk import run_inside; print(json.dumps(run_inside(),sort_keys=True))"], timeout=100, check=False)
        detail = result.stderr.strip().splitlines()[-1:] or ["no diagnostic"]
        need(result.returncode == 0, "isolated case runner failed: " + detail[0][:300])
        try:
            receipt = json.loads(result.stdout)
        except ValueError:
            raise LabError("invalid isolated lab receipt") from None
        need(receipt.get("status") == "PASS", "isolated lab did not pass")
    receipt["owned_container_removed"] = True
    return receipt


def _wait(predicate, seconds, message):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise LabError("timeout: " + message)


def _headers(wire):
    need(len(wire) <= 8192, "oversized SIP message")
    try:
        head, body = wire.decode("ascii").split("\r\n\r\n", 1)
        rows = head.split("\r\n")
        headers = {}
        for row in rows[1:]:
            key, value = row.split(":", 1)
            need(key.lower() not in headers, "duplicate SIP header")
            headers[key.lower()] = value.strip()
        need(int(headers.get("content-length", "-1")) == len(body), "invalid SIP Content-Length")
    except (UnicodeError, ValueError):
        raise LabError("invalid bounded SIP message") from None
    return rows[0], headers, body


def _sdp(payload, codec, port=6000):
    mapping = "L16/16000/1" if codec == "L16" else "G722/8000"
    return ("v=0\r\no=lab 1 1 IN IP4 127.0.0.2\r\ns=L16 isolated fixture\r\n"
            "c=IN IP4 127.0.0.2\r\nt=0 0\r\n"
            f"m=audio {port} RTP/AVP {payload}\r\na=rtpmap:{payload} {mapping}\r\na=sendrecv\r\na=ptime:20\r\n")


class SipPeer:
    """Bounded synthetic caller/callee; all sockets stay in container loopback."""
    def __init__(self, codec, payload, incoming):
        self.codec_name, self.payload, self.incoming = codec, payload, incoming
        self.codec = None
        self.sip = self.rtp = None
        self.stop = threading.Event()
        self.error = None
        self.confirmed, self.bye = False, False
        self.target_rtp = None
        self.packets = []
        self.call_id = secrets.token_hex(12) + "@l16-lab"
        self.local_from = "<sip:lab@127.0.0.2>;tag=" + secrets.token_hex(8)
        self.remote_to = None
        self.offer_sdp, self.answer_sdp = None, None
        try:
            self.sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sip.bind(("127.0.0.2", 5090))
            self.rtp.bind(("127.0.0.2", 6000))
            self.thread = threading.Thread(target=self._listen, daemon=True)
            self.thread.start()
        except BaseException:
            if self.sip is not None:
                self.sip.close()
            if self.rtp is not None:
                self.rtp.close()
            raise

    def _request(self, method, uri, to, body=""):
        rows = [f"{method} {uri} SIP/2.0", "Via: SIP/2.0/UDP 127.0.0.2:5090;branch=z9hG4bK" + secrets.token_hex(10) + ";rport",
                "From: " + self.local_from, "To: " + to, "Call-ID: " + self.call_id,
                "CSeq: 1 " + method, "Max-Forwards: 2", "Contact: <sip:lab@127.0.0.2:5090>"]
        if body:
            rows.append("Content-Type: application/sdp")
        rows.extend(["Content-Length: " + str(len(body)), "", body])
        self.sip.sendto("\r\n".join(rows).encode("ascii"), ("127.0.0.1", 5060))

    def dial(self, extension="9001"):
        need(extension in ("9001", "9002"), "unknown synthetic extension")
        self.offer_sdp = _sdp(self.payload, self.codec_name)
        self._request("INVITE", f"sip:{extension}@127.0.0.1", f"<sip:{extension}@127.0.0.1>", self.offer_sdp)

    def _reply(self, h, code, body=""):
        to = h["to"] if ";tag=" in h["to"] else h["to"] + ";tag=l16-synthetic"
        rows = ["SIP/2.0 " + code, "Via: " + h["via"], "From: " + h["from"], "To: " + to,
                "Call-ID: " + h["call-id"], "CSeq: " + h["cseq"], "Contact: <sip:lab@127.0.0.2:5090>"]
        if body:
            rows.append("Content-Type: application/sdp")
        rows.extend(["Content-Length: " + str(len(body)), "", body])
        self.sip.sendto("\r\n".join(rows).encode("ascii"), ("127.0.0.1", 5060))

    def _select(self, body):
        desc = protocol.parse_audio_sdp(body)
        need(desc.address == "127.0.0.1" and 10000 <= desc.port <= 10019, "unexpected Asterisk RTP endpoint")
        matches = [item for item in desc.codecs if item.encoding.upper() == self.codec_name]
        need(len(matches) == 1, "requested codec not uniquely negotiated")
        codec = matches[0]
        expected_rate = 16000 if self.codec_name == "L16" else 8000
        need(codec.clock_rate == expected_rate and codec.channels == 1 and desc.direction == "sendrecv",
             "negotiated codec rate, channels or duplex direction changed")
        if self.incoming:
            need(codec.payload_type == self.payload, "Asterisk changed offered dynamic payload")
            need(len(desc.codecs) == 1, "Asterisk answer contains an unoffered codec")
        else:
            self.payload = codec.payload_type
        self.codec, self.target_rtp = codec, (desc.address, desc.port)
        return codec

    def _listen(self):
        try:
            deadline = time.monotonic() + CASE_SECONDS + 3
            while not self.stop.is_set() and time.monotonic() < deadline:
                for sock in select.select([self.sip, self.rtp], [], [], .05)[0]:
                    wire, source = sock.recvfrom(8193)
                    if sock is self.rtp:
                        need(self.target_rtp is not None and source == self.target_rtp, "unexpected RTP source")
                        need(len(self.packets) < 600, "RTP packet bound exceeded")
                        self.packets.append((time.monotonic(), wire))
                        continue
                    need(source == ("127.0.0.1", 5060), "unexpected SIP source")
                    first, h, body = _headers(wire)
                    method = first.split()[0]
                    if method == "OPTIONS":
                        self._reply(h, "200 OK")
                    elif method == "SIP/2.0":
                        need(self.incoming and h.get("call-id") == self.call_id, "unexpected SIP response")
                        code = int(first.split()[1])
                        if code < 200:
                            continue
                        need(code == 200 and h.get("cseq") == "1 INVITE", "incoming call rejected")
                        self._select(body)
                        self.answer_sdp = body
                        self.remote_to = h["to"]
                        contact = re.fullmatch(r"<(sip:[^<>]+)>", h.get("contact", ""))
                        need(contact and re.fullmatch(r"sip:(?:[^@;:]+@)?127\.0\.0\.1(?::5060)?(?:;transport=udp)?", contact[1], re.I), "unexpected SIP contact")
                        self._request("ACK", contact[1], self.remote_to)
                        self.confirmed = True
                    elif method == "INVITE":
                        need(not self.incoming, "unexpected inbound INVITE")
                        self._select(body)
                        self.call_id = h["call-id"]
                        self.offer_sdp = body
                        self.answer_sdp = _sdp(self.payload, self.codec_name)
                        self._reply(h, "200 OK", self.answer_sdp)
                    elif method == "ACK":
                        need(h.get("call-id") == self.call_id, "unexpected ACK dialog")
                        self.confirmed = True
                    elif method == "BYE":
                        need(h.get("call-id") == self.call_id, "unexpected BYE dialog")
                        self._reply(h, "200 OK")
                        self.bye = True
                    else:
                        raise LabError("unexpected SIP method")
        except BaseException as error:
            self.error = str(error)

    def close(self):
        self.stop.set()
        self.thread.join(1)
        self.sip.close()
        self.rtp.close()
        need(not self.thread.is_alive(), "SIP peer did not stop")


def pcm_pattern(direction, codec):
    need(direction in ("incoming", "outgoing") and codec in ("L16", "G722"), "invalid synthetic PCM pattern")
    frequency = 1000 if direction == "incoming" else 2200
    if codec == "G722":
        samples = [round(6000 * math.sin(2 * math.pi * frequency * i / 16000)) for i in range(FRAME_SAMPLES * FRAME_COUNT)]
    else:
        # Separate deterministic streams, not phase shifts of one waveform:
        # a reflected signal must never satisfy the other direction's oracle.
        count = FRAME_SAMPLES * FRAME_COUNT
        raw = hashlib.shake_256(("l16-lab/pcm/v1/" + direction).encode("ascii")).digest(count * 2)
        samples = [value // 4 for value in struct.unpack(">" + "h" * count, raw)]
    return struct.pack("<" + "h" * len(samples), *samples)


def _check_l16_duplex(payloads, received):
    expected_le = pcm_pattern("outgoing", "L16")[10 * 640:50 * 640]
    expected_be = bytearray(expected_le)
    expected_be[0::2], expected_be[1::2] = expected_be[1::2], expected_be[0::2]
    need(bytes(expected_be) in payloads, "AudioSocket little-endian to RTP big-endian byte identity failed")
    expected_incoming = pcm_pattern("incoming", "L16")[10 * 640:50 * 640]
    need(expected_incoming in received, "RTP big-endian to AudioSocket little-endian byte identity failed")
    return {"rtp_big_endian_verified": True, "audiosocket_little_endian_verified": True,
            "duplex_exact_samples": 40 * FRAME_SAMPLES}


def _ffmpeg(data, *, encode):
    command = (["ffmpeg", "-v", "error", "-nostdin", "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", "pipe:0",
                "-c:a", "g722", "-f", "g722", "pipe:1"] if encode else
               ["ffmpeg", "-v", "error", "-nostdin", "-f", "g722", "-i", "pipe:0", "-ar", "16000", "-ac", "1", "-f", "s16le", "pipe:1"])
    result = subprocess.run(command, input=data, capture_output=True, timeout=5)
    need(result.returncode == 0 and len(result.stdout) <= 200000, "bounded G.722 fixture conversion failed")
    return result.stdout


class AudioSocketPeer:
    def __init__(self, outgoing_pcm, *, hold_open=False):
        self.output = outgoing_pcm
        self.hold_open = hold_open
        self.received = bytearray()
        self.frames = 0
        self.error = None
        self.connected = False
        self.ended = False
        self.stop = threading.Event()
        self.connection = None
        self.listener = socket.socket()
        try:
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listener.bind(("127.0.0.1", AUDIO_PORT))
            self.listener.listen(1)
            self.listener.settimeout(.1)
            self.thread = threading.Thread(target=self._serve, daemon=True)
            self.thread.start()
        except BaseException:
            self.listener.close()
            raise

    def _serve(self):
        try:
            accept_deadline = time.monotonic() + 3
            while not self.stop.is_set():
                try:
                    connection, source = self.listener.accept()
                    break
                except socket.timeout:
                    need(time.monotonic() < accept_deadline, "AudioSocket accept deadline exceeded")
            else:
                return
            self.connection = connection
            need(source[0] == "127.0.0.1", "unexpected AudioSocket source")
            connection.settimeout(.5)
            buffer, identified, sent = bytearray(), False, 0
            deadline = time.monotonic() + CASE_SECONDS
            next_frame = None
            while not self.stop.is_set() and time.monotonic() < deadline:
                now = time.monotonic()
                if identified and not self.hold_open and sent < FRAME_COUNT and now >= next_frame:
                    part = self.output[sent * 640:(sent + 1) * 640]
                    connection.sendall(protocol.audiosocket_frame(part))
                    sent += 1
                    next_frame += .02
                if identified and not self.hold_open and sent == FRAME_COUNT and now > next_frame + .4:
                    connection.sendall(b"\x00\x00\x00")
                    self.ended = True
                    break
                if not select.select([connection], [], [], .005)[0]:
                    continue
                chunk = connection.recv(8192)
                if not chunk:
                    self.ended = True
                    break
                buffer.extend(chunk)
                need(len(buffer) <= 65536, "AudioSocket receive bound exceeded")
                while len(buffer) >= 3:
                    size = int.from_bytes(buffer[1:3], "big")
                    need(size <= 4096, "AudioSocket frame bound exceeded")
                    if len(buffer) < size + 3:
                        break
                    frame = protocol.parse_audiosocket_frame(bytes(buffer[:size + 3]))
                    del buffer[:size + 3]
                    if not identified:
                        need(frame.message_type == 1 and frame.payload == TOKEN.bytes, "AudioSocket UUID mismatch")
                        identified = self.connected = True
                        next_frame = time.monotonic()
                    elif frame.message_type == 0x12:
                        self.received.extend(frame.payload)
                        self.frames += 1
                        need(len(self.received) <= 500000 and self.frames <= 600, "AudioSocket media bound exceeded")
                    elif frame.message_type == 0:
                        self.ended = True
                        return
                    else:
                        raise LabError("unexpected AudioSocket message type")
            need(self.ended or self.stop.is_set(), "AudioSocket session deadline exceeded")
        except BaseException as error:
            if not self.stop.is_set():
                self.error = str(error)
        finally:
            if self.connection is not None:
                self.connection.close()
            self.listener.close()

    def close(self):
        self.stop.set()
        if self.connection is not None:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.listener.close()
        self.thread.join(1)
        need(not self.thread.is_alive(), "AudioSocket peer did not stop")


def _no_channels():
    result = subprocess.run(["asterisk", "-rx", "core show channels count"], capture_output=True, text=True, timeout=3)
    rows = {line.strip() for line in result.stdout.splitlines()}
    return result.returncode == 0 and {"0 active channels", "0 active calls"} <= rows


def _tone_level(pcm, frequency):
    need(len(pcm) >= 16000, "insufficient decoded duplex audio")
    samples = struct.unpack("<" + "h" * (len(pcm) // 2), pcm)
    samples = samples[2000:6000]
    return 2 * abs(sum(value * complex(math.cos(2 * math.pi * frequency * index / 16000),
                                      math.sin(2 * math.pi * frequency * index / 16000))
                       for index, value in enumerate(samples))) / len(samples)


def run_case(name, incoming, codec_name, payload):
    outgoing_pcm = pcm_pattern("outgoing", codec_name)
    incoming_pcm = pcm_pattern("incoming", codec_name)
    if codec_name == "L16":
        data = bytearray(incoming_pcm)
        data[0::2], data[1::2] = data[1::2], data[0::2]
        encoded, frame_size, increment = bytes(data), 640, 320
    else:
        encoded, frame_size, increment = _ffmpeg(incoming_pcm, encode=True), 160, 160
    need(len(encoded) == FRAME_COUNT * frame_size, "unexpected encoded fixture length")
    audio = AudioSocketPeer(outgoing_pcm)
    peer = None
    try:
        peer = SipPeer(codec_name, payload, incoming)
        if incoming:
            peer.dial()
        else:
            # The only route is a synthetic loopback endpoint. WaitTime bounds
            # pre-answer ringing; the destination dialplan arms absolute time.
            call = Path("/tmp/l16-lab.call")
            call.write_text("Channel: PJSIP/lab\nMaxRetries: 0\nWaitTime: 3\nContext: l16-lab\nExtension: 9001\nPriority: 1\n")
            call.rename("/var/spool/asterisk/outgoing/l16-lab.call")
        def connected():
            need(peer.error is None and audio.error is None, peer.error or audio.error or "peer failed")
            return peer.confirmed and audio.connected
        _wait(connected, 4, "synthetic SIP and AudioSocket connected")
        start = time.monotonic()
        for index in range(FRAME_COUNT):
            remaining = start + .02 * index - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            packet = protocol.make_rtp(encoded[index * frame_size:(index + 1) * frame_size],
                                       peer.payload, (1000 + index) & 65535, 100000 + increment * index, 123456)
            peer.rtp.sendto(packet, peer.target_rtp)
            need(peer.error is None and audio.error is None, peer.error or audio.error or "peer failed")
        _wait(lambda: peer.bye or peer.error or audio.error, 4, "synthetic call hung up")
        need(peer.error is None and audio.error is None, peer.error or audio.error or "peer failed")
        need(peer.bye and audio.ended, "call termination not observed on both transports")
        _wait(_no_channels, 3, "Asterisk released synthetic channels")
        need(len(peer.packets) >= 50 and len(audio.received) >= 32000, "one-way or insufficient duplex media")
        diagnostics = protocol.analyze_rtp(peer.packets, peer.codec)
        need(diagnostics["ok"], "RTP clock, framing, sequence or pacing check failed")
        payloads = b"".join(protocol.parse_rtp(wire).payload for _, wire in peer.packets)
        if codec_name == "L16":
            content = _check_l16_duplex(payloads, audio.received)
        else:
            decoded = _ffmpeg(payloads, encode=False)
            outgoing_level = _tone_level(decoded, 2200)
            incoming_level = _tone_level(bytes(audio.received), 1000)
            need(outgoing_level > 2000 and incoming_level > 2000, "G.722 duplex reference tones missing")
            content = {"outgoing_2200hz_amplitude": round(outgoing_level), "incoming_1000hz_amplitude": round(incoming_level)}
        return {"status": "PASS", "direction": "incoming" if incoming else "outgoing", "codec": codec_name,
                "negotiated_payload_type": peer.payload, "rtp": diagnostics, "audiosocket_frames": audio.frames,
                "native_absolute_timeout_seconds": CASE_SECONDS, "channels_released": True, "clean_bye": peer.bye,
                "content": content, "offer_sdp": peer.offer_sdp, "answer_sdp": peer.answer_sdp}
    finally:
        try:
            audio.close()
        finally:
            if peer is not None:
                peer.close()



def _channel_snapshot():
    listing = subprocess.run(["asterisk", "-rx", "core show channels concise"],
                             capture_output=True, text=True, timeout=3)
    need(listing.returncode == 0, "channel snapshot failed")
    names = [line.split("!", 1)[0] for line in listing.stdout.splitlines() if "!" in line]
    need(len(names) <= 12, "unexpected channel count in isolated fixture")
    result = {}
    for name in names:
        need(re.fullmatch(r"[A-Za-z0-9_@./;:+-]{1,160}", name), "invalid isolated channel name")
        detail = subprocess.run(["asterisk", "-rx", "core show channel " + name],
                                capture_output=True, text=True, timeout=3)
        need(detail.returncode == 0, "channel identity inspection failed")
        match = re.search(r"(?mi)^\s*UniqueID:\s*(\S+)\s*$", detail.stdout)
        need(match is not None, "channel identity missing; absence is not established")
        result[name] = match[1]
    return result


def run_expiry_case():
    """Observe native target expiry while an unrelated synthetic channel stays up."""
    audio = AudioSocketPeer(b"", hold_open=True)
    peer, sentinel = None, {}
    try:
        subprocess.run(["asterisk", "-rx", "channel originate Local/s@l16-sentinel application Wait 12"],
                       capture_output=True, check=True, timeout=3)
        def sentinel_ready():
            current = _channel_snapshot()
            return {name: uid for name, uid in current.items() if name.startswith("Local/s@l16-sentinel-")}
        sentinel = _wait(sentinel_ready, 3, "unrelated synthetic sentinel started")
        peer = SipPeer("L16", 102, True)
        peer.dial("9002")
        def connected():
            need(peer.error is None and audio.error is None, peer.error or audio.error or "guard peer failed")
            return peer.confirmed and audio.connected
        _wait(connected, 3, "guard target connected")
        start = time.monotonic()
        current = _channel_snapshot()
        targets = {name: uid for name, uid in current.items() if name.startswith("PJSIP/lab-")}
        need(len(targets) == 1, "guard target identity is ambiguous")
        # Keep both peers open. No client hangup, socket close, or transport stop
        # is sent before the PBX's independent absolute timer expires.
        _wait(lambda: peer.bye or peer.error or audio.error, 4.5, "native absolute timer terminated target")
        elapsed = time.monotonic() - start
        need(peer.bye and peer.error is None and audio.error is None, peer.error or audio.error or "native expiry not observed")
        need(2 <= elapsed <= 4.5, "target did not expire in the native three-second window")
        current = _channel_snapshot()
        need(not (set(targets.values()) & set(current.values())), "expired target UniqueID remains active")
        need(all(current.get(name) == uid for name, uid in sentinel.items()), "unrelated sentinel changed during target expiry")
        result = {"status": "PASS", "absolute_timeout_seconds": 3,
                  "observed_after_answer_seconds": round(elapsed, 3),
                  "target_uniqueid_reaped": True, "unrelated_sentinel_preserved": True,
                  "client_sent_hangup_before_expiry": False}
    finally:
        try:
            audio.close()
        finally:
            try:
                if peer is not None:
                    peer.close()
            finally:
                current = _channel_snapshot()
                for name, uid in sentinel.items():
                    if current.get(name) == uid:
                        subprocess.run(["asterisk", "-rx", "channel request hangup " + name],
                                       capture_output=True, check=True, timeout=3)
    _wait(_no_channels, 3, "guard and sentinel channels released")
    result["channels_released"] = True
    return result


def run_inside():
    need(Path("/.dockerenv").exists() and re.fullmatch(r"[0-9a-f]{32}", os.environ.get("L16_LAB_TOKEN", "")), "owned container marker required")
    need({name for _, name in socket.if_nameindex()} == {"lo"}, "network-none loopback isolation required")
    version = subprocess.run(["asterisk", "-V"], capture_output=True, text=True, timeout=3)
    need(version.returncode == 0 and "Asterisk 22.11.0" in version.stdout, "Asterisk 22.11.0 fixture required")
    _wait(_no_channels, 3, "initial empty Asterisk")
    results = {}
    for case in CASES:
        results[case[0]] = run_case(*case)
    guard = run_expiry_case()
    need(_no_channels(), "final isolated Asterisk channel teardown failed")
    return {"status": "PASS", "asterisk_version": version.stdout.strip(), "network": "none",
            "synthetic_only": True, "physical_phones_called": False, "cases": results, "native_expiry_guard": guard,
            "channels_released": True}
