#!/usr/bin/env python3
"""Short-lived, single-device Cisco authentication callback and keypad control."""
import argparse
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import socketserver
import stat
import sys
import time
from urllib import error, parse, request
import xml.etree.ElementTree as ET

MAX_REQUEST = 8192
MAX_HEADERS = 4096
MAX_RESPONSE = 2 * 1024 * 1024
READ_SECONDS = 3
MAX_TTL = 2700
KEYS = frozenset(["Settings", "NavDwn", "NavUp", "NavLeft", "NavRight", "NavSelect",
                  "Line1", "Line2", "Speaker", "Release"]
                 + [f"Soft{i}" for i in range(1, 5)]
                 + [f"KeyPad{i}" for i in range(10)] + ["KeyPadStar", "KeyPadPound"])
FIELDS = {"phone_ipv4", "auth_bind_ipv4", "auth_port", "auth_path", "device",
          "username", "password", "created_at", "expires_at", "ttl_seconds"}


def private_ipv4(value):
    address = ipaddress.IPv4Address(value)
    if not any(address in ipaddress.IPv4Network(net) for net in
               ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
        raise ValueError("management addresses must be RFC1918 IPv4 literals")
    return str(address)


def validate_config(config, now=None, *, allow_expired=False):
    now = time.time() if now is None else now
    if not isinstance(config, dict) or set(config) != FIELDS:
        raise ValueError("invalid management configuration fields")
    for field in ("phone_ipv4", "auth_bind_ipv4"):
        if not isinstance(config[field], str) or private_ipv4(config[field]) != config[field]:
            raise ValueError("invalid management IPv4 address")
    if type(config["auth_port"]) is not int or not 1024 <= config["auth_port"] <= 65535:
        raise ValueError("auth_port must be 1024..65535")
    for field, pattern in (("auth_path", r"/auth/[a-f0-9]{64}"),
                           ("device", r"SEP[0-9A-F]{12}"),
                           ("username", r"[A-Za-z0-9_-]{24,96}"),
                           ("password", r"[A-Za-z0-9_-]{24,96}")):
        if not isinstance(config[field], str) or not re.fullmatch(pattern, config[field]):
            raise ValueError("invalid management credential, device or nonce format")
    if len(config["username"]) + 1 + len(config["password"]) > 64:
        raise ValueError("management Basic credentials exceed the phone's 64-byte limit")
    mac = config["device"][3:]
    if int(mac[:2], 16) & 1 or int(mac, 16) == 0:
        raise ValueError("device must identify a unicast MAC")
    if any(type(config[key]) is not int for key in ("created_at", "expires_at", "ttl_seconds")):
        raise ValueError("management timestamps and TTL must be integers")
    if (not 1 <= config["ttl_seconds"] <= MAX_TTL
            or config["expires_at"] != config["created_at"] + config["ttl_seconds"]
            or config["created_at"] > now + 5
            or (not allow_expired and config["expires_at"] <= now)):
        raise ValueError("management window expired or invalid")
    return config


def load_config(path, *, allow_expired=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 16384:
            raise ValueError("management config must be a small regular file with mode 0600")
        config = json.load(source)
    return validate_config(config, allow_expired=allow_expired)


def write_new(path, data):
    """Never overwrite an existing config, screenshot or symbolic link."""
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as target:
        target.write(data)


def create_config(phone, bind, port, device, ttl):
    now = int(time.time())
    config = dict(phone_ipv4=phone, auth_bind_ipv4=bind, auth_port=port, device=device,
                  # Each value has 144 random bits while the Basic pair stays
                  # below the 7945G firmware's observed credential-size limit.
                  username=secrets.token_urlsafe(18), password=secrets.token_urlsafe(18),
                  auth_path="/auth/" + secrets.token_hex(32), created_at=now,
                  expires_at=now + ttl, ttl_seconds=ttl)
    return validate_config(config)


def renew_config(path, ttl):
    """An explicit local operator action preserves phone URL and credentials."""
    config = load_config(path, allow_expired=True)
    config.update(created_at=int(time.time()), ttl_seconds=ttl)
    config["expires_at"] = config["created_at"] + ttl
    validate_config(config)
    path = Path(path)
    temporary = path.with_name(path.name + '.renew-' + secrets.token_hex(8))
    try:
        write_new(temporary, (json.dumps(config, indent=2) + '\n').encode())
        if path.is_symlink():
            raise ValueError("refusing management config symlink")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def equal(left, right):
    # Fixed-size digests also avoid revealing supplied string lengths in comparison.
    return hmac.compare_digest(hashlib.sha256(left.encode()).digest(),
                               hashlib.sha256(right.encode()).digest())


def authorized(config, source, method, target, headers, body, now=None):
    """Only Cisco's exact documented three-field GET or POST schema is accepted."""
    now = time.time() if now is None else now
    if now >= config["expires_at"] or source != config["phone_ipv4"]:
        return False
    if method not in ("GET", "POST") or "transfer-encoding" in headers:
        return False
    try:
        if not target.startswith("/") or target.startswith("//"):
            return False
        url = parse.urlsplit(target)
        if url.scheme or url.netloc or url.fragment or "#" in target:
            return False
        path_ok = equal(url.path, config["auth_path"])
        if method == "GET":
            if body or headers.get("content-length", "0") != "0":
                return False
            encoded = url.query
        else:
            if "?" in target or headers.get("content-type", "").lower() != "application/x-www-form-urlencoded":
                return False
            encoded = body.decode("ascii")
        if len(encoded) > 4096 or re.search(r"%(?![0-9a-fA-F]{2})", encoded):
            return False
        values = parse.parse_qsl(encoded, keep_blank_values=True, strict_parsing=True,
                                 encoding="utf-8", errors="strict", max_num_fields=3)
        if len(values) != 3 or {key for key, _ in values} != {"UserID", "Password", "devicename"}:
            return False
        values = dict(values)
        # Evaluate all credential comparisons even when a previous value differs.
        return bool(path_ok & equal(values["UserID"], config["username"])
                    & equal(values["Password"], config["password"])
                    & equal(values["devicename"], config["device"]))
    except (ValueError, UnicodeError):
        return False


def read_request(connection, deadline):
    """Read one bounded HTTP request, with an absolute deadline and no keep-alive."""
    data = b""
    def receive(limit):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("request deadline exceeded")
        connection.settimeout(remaining)
        block = connection.recv(limit)
        if not block:
            raise ValueError("incomplete request")
        return block
    while b"\r\n\r\n" not in data:
        data += receive(MAX_HEADERS + 1 - len(data))
        if b"\r\n\r\n" not in data and len(data) > MAX_HEADERS:
            raise ValueError("request headers too large")
    head, body = data.split(b"\r\n\r\n", 1)
    if len(head) + 4 > MAX_HEADERS:
        raise ValueError("request headers too large")
    lines = head.decode("ascii").split("\r\n")
    method, target, version = lines[0].split(" ")
    if version not in ("HTTP/1.0", "HTTP/1.1") or len(target) > 4096:
        raise ValueError("unsupported request")
    headers = {}
    for line in lines[1:]:
        name, value = line.split(":", 1)
        name = name.lower()
        if not re.fullmatch(r"[a-z0-9-]+", name) or name in headers:
            raise ValueError("invalid or duplicate HTTP header")
        headers[name] = value.strip(" \t")
    length = headers.get("content-length", "0")
    if not re.fullmatch(r"0|[1-9][0-9]{0,3}", length) or "transfer-encoding" in headers:
        raise ValueError("invalid request framing")
    length = int(length)
    if len(head) + 4 + length > MAX_REQUEST or length > 4096 or len(body) > length:
        raise ValueError("request size limit exceeded")
    if method == "POST" and "content-length" not in headers:
        raise ValueError("POST requires content length")
    while len(body) < length:
        body += receive(length - len(body))
    return method, target, headers, body


class AuthHandler(socketserver.BaseRequestHandler):
    def handle(self):
        allowed = False
        try:
            if self.client_address[0] == self.server.config["phone_ipv4"]:
                args = read_request(self.request, min(time.monotonic() + READ_SECONDS,
                                                       self.server.deadline))
                allowed = time.monotonic() < self.server.deadline and authorized(
                    self.server.config, self.client_address[0], *args)
        except (ValueError, UnicodeError, OSError):
            pass
        payload = b"AUTHORIZED" if allowed else b"UNAUTHORIZED"
        response = (b"HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\nCache-Control: no-store\r\n"
                    b"Connection: close\r\nContent-Length: " + str(len(payload)).encode()
                    + b"\r\n\r\n" + payload)
        try:
            self.request.settimeout(1)
            self.request.sendall(response)
        except OSError:
            pass


class AuthServer(socketserver.TCPServer):
    # Explicit renewal restarts on the same address while accepted connections
    # may remain in TIME_WAIT. This does not permit a second live listener.
    allow_reuse_address = True
    request_queue_size = 5

    def handle_error(self, request_socket, client_address):
        # No traceback: exception text could contain a URL, body or credential.
        pass


def serve(config):
    validate_config(config)
    with AuthServer((config["auth_bind_ipv4"], config["auth_port"]), AuthHandler) as server:
        server.config = config
        server.deadline = time.monotonic() + max(0, config["expires_at"] - time.time())
        server.timeout = 0.25
        print("Temporary phone authentication callback started", flush=True)
        while time.monotonic() < server.deadline and time.time() < config["expires_at"]:
            server.handle_request()
    print("Temporary phone authentication callback stopped", flush=True)


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("phone redirect refused")


def phone_request(config, *, key=None, allow_active=False):
    validate_config(config)
    if type(allow_active) is not bool or (allow_active and key is None):
        raise ValueError("active-call control requires an explicit key request")
    data = None
    endpoint = "Screenshot"
    if key is not None:
        if key not in KEYS:
            raise ValueError("key is not in the permitted single-key set")
        endpoint = "Execute"
        priority = 0 if allow_active else 2
        xml = f'<CiscoIPPhoneExecute><ExecuteItem Priority="{priority}" URL="Key:{key}"/></CiscoIPPhoneExecute>'
        data = parse.urlencode({"XML": xml}).encode("ascii")
    token = base64.b64encode((config["username"] + ":" + config["password"]).encode()).decode()
    req = request.Request(f'http://{config["phone_ipv4"]}/CGI/{endpoint}', data=data,
                          headers={"Authorization": "Basic " + token,
                                   "Content-Type": "application/x-www-form-urlencoded",
                                   "Connection": "close"})
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=20 if key is not None else 5) as response:
        if response.status != 200:
            raise ValueError("phone returned a non-success HTTP status")
        data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise ValueError("phone response too large")
        return data


def screenshot_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"BM"):
        return ".bmp"
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("unsupported screenshot XML")
    try:
        root = ET.fromstring(data)
        if root.tag in ("CiscoIPPhoneImage", "CiscoIPPhoneImageFile"):
            return ".xml"
    except ET.ParseError:
        pass
    raise ValueError("phone did not return a supported screenshot")


def check_key_response(data, key):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("unsupported key response XML")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise ValueError("phone did not confirm the key request") from None
    items = root.findall("ResponseItem")
    if (root.tag != "CiscoIPPhoneResponse" or len(items) != 1
            or items[0].get("Status") != "0" or items[0].get("URL") != "Key:" + key):
        raise ValueError("phone did not confirm the key request")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, help="private mode-0600 JSON configuration")
    commands = ap.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create fresh credentials; never overwrites")
    init.add_argument("--phone-ipv4", required=True)
    init.add_argument("--auth-bind-ipv4", required=True)
    init.add_argument("--auth-port", type=int, default=18080)
    init.add_argument("--device", required=True)
    init.add_argument("--ttl-seconds", type=int, default=MAX_TTL)
    commands.add_parser("serve")
    renew = commands.add_parser("renew", help="explicitly renew the same private URL/credentials")
    renew.add_argument("--ttl-seconds", type=int, default=MAX_TTL)
    key = commands.add_parser("key")
    key.add_argument("key", choices=sorted(KEYS))
    key.add_argument("--allow-active", action="store_true",
                     help="explicitly permit this one key during an operator-controlled call (Priority 0)")
    shot = commands.add_parser("screenshot")
    shot.add_argument("--output", required=True, help="new private output path; suffix must match detected format")
    args = ap.parse_args()
    os.umask(0o077)
    try:
        if args.command == "init":
            config = create_config(args.phone_ipv4, args.auth_bind_ipv4, args.auth_port,
                                   args.device, args.ttl_seconds)
            write_new(args.config, (json.dumps(config, indent=2) + "\n").encode())
            print("Private management configuration created")
            return
        if args.command == "renew":
            renew_config(args.config, args.ttl_seconds)
            print("Private management window explicitly renewed; restart its callback service")
            return
        config = load_config(args.config)
        if args.command == "serve":
            serve(config)
        elif args.command == "key":
            check_key_response(phone_request(config, key=args.key, allow_active=args.allow_active), args.key)
            print("Phone accepted one key request")
        else:
            data = phone_request(config)
            suffix = screenshot_type(data)
            path = Path(args.output)
            if path.suffix and path.suffix.lower() != suffix:
                raise ValueError("output suffix does not match detected screenshot format")
            path = path if path.suffix else path.with_suffix(suffix)
            write_new(path, data)
            print("Private screenshot saved (" + suffix[1:] + ")")
    except KeyboardInterrupt:
        return
    except (ValueError, OSError, error.URLError, http.client.HTTPException, KeyError, TypeError):
        # Never interpolate exceptions: HTTP errors can contain query credentials.
        sys.exit("Phone management failed; request/configuration details withheld")


if __name__ == "__main__":
    main()
