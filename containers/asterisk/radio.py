#!/usr/bin/env python3
"""Per-call AGI radio: curated HTTP audio -> FFmpeg pipe -> local AudioSocket.

No daemon, recording, arbitrary URL argument, shell command or idle fetching.
Only the AudioSocket UUID for this AGI call can start a network fetch.
"""
import contextlib
import fcntl
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from urllib.parse import urljoin, urlsplit
import uuid

CATALOG = Path(__file__).with_name("radio-catalog.json")
IO_TIMEOUT = 8
START_TIMEOUT = 15
MAX_REDIRECTS = 3
FRAME_BYTES = 320  # 20 ms, 8 kHz, signed 16-bit little-endian mono.
USER_AGENT = "HomeTelephony/1.0"
GPM_CHANNELS = {327, 95, 93}
GPM_API_LIMIT = 65536


def catalog():
    return json.loads(CATALOG.read_text())["stations"]


def settings(site):
    """No user-supplied station URLs; only immutable image catalog identifiers."""
    cfg = site.get("radio", {})
    if not isinstance(cfg, dict) or set(cfg) - {"enabled", "stations", "max_seconds", "max_calls"}:
        raise ValueError("radio accepts only enabled, stations, max_seconds and max_calls")
    if type(cfg.get("enabled", False)) is not bool:
        raise ValueError("radio.enabled must be boolean")
    if not cfg.get("enabled", False):
        return None
    names = cfg.get("stations", list(catalog()))
    if (not isinstance(names, list) or not names or any(not isinstance(x, str) for x in names)
            or len(set(names)) != len(names) or set(names) - catalog().keys()):
        raise ValueError("radio.stations must contain unique catalog IDs")
    seconds, calls = cfg.get("max_seconds", 1800), cfg.get("max_calls", 2)
    if type(seconds) is not int or not 30 <= seconds <= 7200:
        raise ValueError("radio.max_seconds must be 30..7200")
    if type(calls) is not int or not 1 <= calls <= 4:
        raise ValueError("radio.max_calls must be 1..4")
    return {"enabled": True, "stations": names, "max_seconds": seconds, "max_calls": calls}


def checked_url(url, station, purpose="stream"):
    if not isinstance(url, str) or len(url) > 4096 or not url.isascii() or re.search(r"[\x00-\x20\x7f]", url):
        raise ValueError("invalid stream URL")
    p = urlsplit(url)
    schemes = {"https", "http"} if station.get("allow_http") else {"https"}
    known_host = p.hostname in station["hosts"] or (station.get("redirect_host_regex")
        and p.hostname is not None and re.fullmatch(station["redirect_host_regex"], p.hostname))
    if (p.scheme not in schemes or not known_host or p.username is not None
            or p.password is not None or p.fragment or p.port not in (None, 80 if p.scheme == "http" else 443)):
        raise ValueError("stream URL is outside station policy")
    if station.get("resolver") == "gpm-channel":
        channel = station.get("channel_id")
        if type(channel) is not int or channel not in GPM_CHANNELS or p.scheme != "https":
            raise ValueError("invalid broadcaster channel policy")
        if purpose == "api":
            valid = (p.hostname == "101.ru" and p.path == f"/api/channel/getListServersChannel/{channel}/channel/"
                     and p.query == "dataFormat=json")
        else:
            valid = (purpose == "stream" and p.hostname == "srv01.gpmradio.ru"
                     and p.path == f"/stream/pro/aac/64/{channel}"
                     and re.fullmatch(r"token=[A-Za-z0-9._~-]{16,3072}", p.query))
        if not valid:
            # Never include the rejected URL: its query may contain a short-lived token.
            raise ValueError("broadcaster URL is outside channel policy")
    return p


def public_addresses(host, port):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global
                            or ipaddress.ip_address(a[4][0]).is_multicast for a in addresses):
        raise ValueError("stream DNS must resolve only to public addresses")
    return addresses


def open_stream(station, url=None, content_types=None, purpose="stream"):
    """Validate every redirect and connect to the checked IP, avoiding DNS rebinding."""
    if url is None and station.get("resolver") == "gpm-channel":
        url = gpm_stream_url(station)
    url = url or station["url"]
    content_types = content_types or {"audio/mpeg", "audio/mp3", "audio/aac", "audio/aacp", "application/octet-stream"}
    for redirect in range(MAX_REDIRECTS + 1):
        p = checked_url(url, station, purpose)
        port = 443 if p.scheme == "https" else 80
        addresses = public_addresses(p.hostname, port)
        conn = http.client.HTTPConnection(p.hostname, port, timeout=IO_TIMEOUT)
        try:
            last_error = None
            for family, kind, protocol, _, address in addresses:
                sock = socket.socket(family, kind, protocol)
                sock.settimeout(IO_TIMEOUT)
                try:
                    sock.connect(address)
                    if p.scheme == "https":
                        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=p.hostname)
                    conn.sock = sock
                    break
                except OSError as exc:
                    sock.close()
                    last_error = exc
            else:
                raise OSError("stream connection failed") from last_error
            target = p.path or "/"
            if p.query:
                target += "?" + p.query
            conn.request("GET", target, headers={"User-Agent": USER_AGENT, "Icy-MetaData": "0", "Accept-Encoding": "identity"})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location or redirect == MAX_REDIRECTS:
                    raise ValueError("stream redirect limit")
                url = urljoin(url, location)
                conn.close()
                continue
            if response.status != 200:
                raise ValueError("stream HTTP status rejected")
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].lower()
            if content_type not in content_types:
                raise ValueError("stream is not supported direct audio")
            if response.getheader("icy-metaint") not in (None, "0"):
                raise ValueError("server returned unsupported ICY metadata")
            return conn, response
        except BaseException:
            conn.close()
            raise
    raise ValueError("stream redirect limit")


def gpm_stream_url(station):
    """Resolve a fresh official AAC URL per call; never persist its bearer token.

    detifm.ru embeds this 101.ru endpoint in its channel player. The API and
    audio fetch use the same User-Agent and egress host; tokens are short-lived.
    Only the HTTPS/443 AAC64 entry for the exact configured channel is accepted.
    API redirects cannot enter the audio policy, nor audio redirects the API.
    """
    api = station["url"]
    checked_url(api, station, "api")
    conn, response = open_stream(station, api, {"application/json"}, purpose="api")
    try:
        body = response.read(GPM_API_LIMIT + 1)
    finally:
        conn.close()
    if len(body) > GPM_API_LIMIT:
        raise ValueError("broadcaster response size rejected")
    try:
        document = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid broadcaster response") from None
    if (not isinstance(document, dict) or type(document.get("status")) is not int
            or document["status"] != 1 or not isinstance(document.get("result"), list)
            or not 1 <= len(document["result"]) <= 16):
        raise ValueError("broadcaster response schema rejected")
    candidates = []
    for item in document["result"]:
        if not isinstance(item, dict) or item.get("format") != "AAC":
            continue
        candidate = item.get("urlStream")
        try:
            checked_url(candidate, station, "stream")
        except ValueError:
            continue
        candidates.append(candidate)
    if len(candidates) != 1:
        raise ValueError("broadcaster has no unique approved AAC stream")
    return candidates[0]


def hls_playlist(text, base):
    """Small unencrypted audio HLS subset; no keys, byte ranges or external maps."""
    if len(text) > 65536 or not text.startswith("#EXTM3U"):
        raise ValueError("invalid HLS playlist")
    if any(tag in text for tag in ("#EXT-X-KEY", "#EXT-X-MAP", "#EXT-X-BYTERANGE", "#EXT-X-I-FRAME")):
        raise ValueError("unsupported HLS feature")
    urls = [urljoin(base, line.strip()) for line in text.splitlines() if line.strip() and not line.startswith("#")]
    if not 1 <= len(urls) <= 64:
        raise ValueError("invalid HLS entry count")
    return "#EXT-X-STREAM-INF:" in text, urls


def hls_chunks(station, stop):
    """Fetch the official playlist and fresh AAC segments, with bounded buffers."""
    playlist = station["url"]
    seen = []
    master_count = 0
    while not stop.is_set():
        conn, response = open_stream(station, playlist, {"application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl"})
        try:
            body = response.read(65537).decode("utf-8")
        finally:
            conn.close()
        master, urls = hls_playlist(body, playlist)
        for url in urls:
            checked_url(url, station)
        if master:
            master_count += 1
            if master_count > 2:
                raise ValueError("HLS master nesting limit")
            playlist = urls[0]
            continue
        for url in urls:
            if stop.is_set():
                return
            if url in seen:
                continue
            conn, response = open_stream(station, url)
            try:
                data = response.read(2_000_001)
            finally:
                conn.close()
            if not data or len(data) > 2_000_000:
                raise ValueError("HLS segment size rejected")
            seen = (seen + [url])[-256:]
            yield data
        if "#EXT-X-ENDLIST" in body:
            return
        stop.wait(1.5)


def ffmpeg_args(station):
    if station["format"] not in ("mp3", "aac"):
        raise ValueError("unsupported stream encoding")
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-protocol_whitelist", "pipe", "-threads", "1", "-filter_threads", "1",
            "-probesize", "32768", "-analyzeduration", "0", "-f", station["format"], "-i", "pipe:0", "-vn", "-ac", "1", "-ar", "8000",
            "-af", "volume=0.5", "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"]


def recv_exact(conn, size):
    data = bytearray()
    while len(data) < size:
        part = conn.recv(size - len(data))
        if not part:
            raise EOFError("AudioSocket closed")
        data.extend(part)
    return bytes(data)


def receive_frame(conn):
    kind, length = struct.unpack("!BH", recv_exact(conn, 3))
    if length > 4096:
        raise ValueError("AudioSocket frame too large")
    return kind, recv_exact(conn, length)


def send_audio(conn, data):
    conn.sendall(struct.pack("!BH", 0x10, len(data)) + data)


def acquire_slot(directory, count):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for index in range(count):
        fd = os.open(directory / f"slot-{index}", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            os.close(fd)
    raise ValueError("radio concurrent-call limit reached")


class Call:
    def __init__(self, station, seconds, token, opener=open_stream):
        self.station, self.seconds, self.token, self.opener = station, seconds, token, opener
        self.stop = threading.Event()
        self.proc = self.http = self.peer = None
        self.failed = False
        self.frames = 0

    def close(self):
        self.stop.set()
        if self.peer:
            with contextlib.suppress(OSError):
                self.peer.shutdown(socket.SHUT_RDWR)
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=1)
            for pipe in (self.proc.stdin, self.proc.stdout):
                with contextlib.suppress(OSError):
                    pipe.close()
        if self.http:
            self.http.close()

    def feed(self):
        try:
            if self.station.get("hls"):
                chunks = hls_chunks(self.station, self.stop)
            else:
                self.http, response = self.opener(self.station)
                chunks = iter(lambda: response.read1(8192), b"")
            # A hard byte ceiling complements timeouts even for fast/malicious sources.
            budget = self.seconds * 128000 + 2_000_000
            for data in chunks:
                if self.stop.is_set():
                    break
                budget -= len(data)
                if budget <= 0:
                    raise ValueError("stream byte limit")
                while data and not self.stop.is_set():
                    written = self.proc.stdin.write(data)
                    if not written:
                        raise OSError("decoder input closed")
                    data = data[written:]
                self.proc.stdin.flush()
        except (OSError, ValueError, http.client.HTTPException):
            self.failed = True
        finally:
            if self.proc:
                with contextlib.suppress(OSError):
                    self.proc.stdin.close()
            if self.http:
                self.http.close()

    def discard_input(self, conn):
        try:
            while not self.stop.is_set():
                if not select.select([conn], [], [], .2)[0]:
                    continue
                kind, data = receive_frame(conn)
                if kind == 0 or kind == 0xff or (kind == 3 and data == b"#"):
                    break
                if kind not in (0x10, 3):
                    raise ValueError("unexpected AudioSocket message")
                # Caller audio is deliberately discarded, never stored or transmitted.
        except (OSError, EOFError, ValueError):
            pass
        self.stop.set()

    def serve(self, listener):
        try:
            conn, address = listener.accept()
            with conn:
                self.peer = conn
                conn.settimeout(3)
                kind, data = receive_frame(conn)
                if address[0] != "127.0.0.1" or kind != 1 or data != self.token.bytes:
                    raise ValueError("AudioSocket call identity mismatch")
                conn.settimeout(IO_TIMEOUT)
                self.proc = subprocess.Popen(ffmpeg_args(self.station), stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
                threading.Thread(target=self.feed, daemon=True).start()
                threading.Thread(target=self.discard_input, args=(conn,), daemon=True).start()
                last_data = time.monotonic()
                audio_start = None
                next_frame = last_data
                pending = bytearray()
                while not self.stop.is_set() and (audio_start is None or time.monotonic() - audio_start < self.seconds):
                    if select.select([self.proc.stdout], [], [], 0.1)[0]:
                        data = os.read(self.proc.stdout.fileno(), 8192)
                        if not data:
                            self.failed = True
                            break
                        pending.extend(data)
                        last_data = time.monotonic()
                    if time.monotonic() - last_data > (START_TIMEOUT if not self.frames else IO_TIMEOUT):
                        self.failed = True
                        break
                    while len(pending) >= FRAME_BYTES and not self.stop.is_set():
                        now = time.monotonic()
                        if audio_start is None:
                            audio_start = next_frame = now
                        if now - audio_start >= self.seconds:
                            break
                        if self.stop.wait(max(0, next_frame - now)):
                            break
                        send_audio(conn, pending[:FRAME_BYTES])
                        del pending[:FRAME_BYTES]
                        self.frames += 1
                        next_frame = max(next_frame + .02, time.monotonic() - .02)
                # The AGI caller plays a finite local failure tone after AudioSocket returns.
                with contextlib.suppress(OSError):
                    conn.sendall(b"\x00\x00\x00")
        except (OSError, EOFError, ValueError):
            self.failed = True
        finally:
            self.close()


def command(line):
    print(line, flush=True)
    response = sys.stdin.readline(4097)
    if not response or len(response) > 4096 or not response.startswith("200 result="):
        raise EOFError("AGI channel ended")
    return response


def main():
    call = listener = None
    slot = None
    def interrupted(_signum, _frame):
        raise EOFError("AGI interrupted")
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGALRM):
        signal.signal(sig, interrupted)
    signal.alarm(10)
    try:
        for _ in range(128):
            line = sys.stdin.readline(4097)
            if line in ("\n", "\r\n"):
                break
            if not line or len(line) > 4096:
                raise ValueError("invalid AGI environment")
        else:
            raise ValueError("AGI environment too large")
        cfg = settings({"radio": json.loads(Path("/etc/asterisk/radio.conf").read_text())})
        if len(sys.argv) != 2 or cfg is None or sys.argv[1] not in cfg["stations"]:
            raise ValueError("station is not enabled")
        slot = acquire_slot("/run/asterisk/radio", cfg["max_calls"])
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)
        token = uuid.uuid4()
        call = Call(catalog()[sys.argv[1]], cfg["max_seconds"], token)
        thread = threading.Thread(target=call.serve, args=(listener,), daemon=True)
        thread.start()
        signal.alarm(cfg["max_seconds"] + START_TIMEOUT + 10)
        command(f"EXEC AudioSocket {token},127.0.0.1:{listener.getsockname()[1]}")
        call.close()
        thread.join(timeout=3)
        command('SET VARIABLE RADIOSTATUS "' + ("failed" if call.failed else "ok") + '"')
    except (OSError, ValueError, EOFError, KeyError, json.JSONDecodeError):
        # Never log request URLs, AGI environment, caller data or redirect query tokens.
        with contextlib.suppress(OSError):
            print('SET VARIABLE RADIOSTATUS "failed"', flush=True)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if call:
            call.close()
        if listener:
            listener.close()
        if slot is not None:
            os.close(slot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
