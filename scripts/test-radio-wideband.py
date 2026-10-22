#!/usr/bin/env python3
"""Verify incoming/outgoing radio bandwidth and G.711 fallback in an isolated PBX.

Only synthetic local tones, credentials and loopback SIP/RTP are used. Never
accepts an existing container or accesses a real phone or radio station.
"""
import argparse
from contextlib import contextmanager
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


helper = module("radio_fixture_helpers", Path(__file__).with_name("test-alert-originate.py"))
need, await_condition = helper.need, helper.await_condition


OWNER_LABEL = "org.cisco-home-telephony.radio-wideband-owner"
CREATE_RECOVERY_SECONDS = 10


class OwnedContainer:
    """Recover only this invocation's named, labelled disposable container."""

    def __init__(self, docker):
        self.docker = docker
        self.nonce = secrets.token_hex(16)
        self.name = "cisco-radio-wideband-" + self.nonce
        self.container = None
        self.attempted = False
        self.created = False

    def _named_ids(self):
        result = subprocess.run(
            self.docker + ["container", "ls", "--all", "--no-trunc", "--filter",
                           "name=^/" + self.name + "$", "--format", "{{.ID}}"],
            capture_output=True, text=True, check=True, timeout=5)
        identities = result.stdout.split()
        need(len(identities) <= 1 and
             all(re.fullmatch(r"[0-9a-f]{64}", value) for value in identities),
             "could not verify exact fixture name")
        return identities

    def _inspect_owned(self):
        # Avoid returning image environment variables or unrelated metadata.
        template = ('[{{json .Id}},{{json .Name}},{{json .Config.Labels}},'
                    '{{json .HostConfig.NetworkMode}}]')
        result = subprocess.run(
            self.docker + ["inspect", "--type", "container", "--format", template, self.name],
            capture_output=True, text=True, timeout=5)
        if result.returncode:
            need(not self._named_ids(), "could not inspect named fixture")
            return None
        try:
            identity, name, labels, network = json.loads(result.stdout)
        except (ValueError, TypeError):
            raise RuntimeError("invalid fixture ownership metadata") from None
        need(isinstance(identity, str) and re.fullmatch(r"[0-9a-f]{64}", identity)
             and name == "/" + self.name and isinstance(labels, dict)
             and labels.get(OWNER_LABEL) == self.nonce,
             "fixture ownership mismatch; refusing removal")
        need(self.container is None or self.container == identity,
             "fixture identity changed; refusing removal")
        return identity, network

    def create(self, image):
        self.attempted = True  # The daemon can finish after the CLI times out.
        result = subprocess.run(
            self.docker + ["create", "--pull", "never", "--name", self.name,
                           "--label", OWNER_LABEL + "=" + self.nonce,
                           "--network", "none", "--cpus", "1", "--memory", "512m",
                           "--pids-limit", "128", "--user", "0:0", image],
            capture_output=True, text=True, check=True, timeout=15)
        identity = result.stdout.strip()
        need(re.fullmatch(r"[0-9a-f]{64}", identity), "invalid created fixture ID")
        self.container = identity
        owned = self._inspect_owned()
        need(owned is not None and owned[1] == "none", "fixture isolation not applied")
        self.created = True
        return identity

    def cleanup(self):
        if not self.attempted:
            return
        deadline = time.monotonic() + CREATE_RECOVERY_SECONDS
        while True:
            owned = self._inspect_owned()
            if owned is not None:
                break
            if self.created:
                return
            # A timed-out create may still be executing in the daemon. Absence
            # now is not proof that it cannot appear later; fail explicitly.
            remaining = deadline - time.monotonic()
            need(remaining > 0,
                 "fixture cleanup unconfirmed after uncertain create: " + self.name)
            time.sleep(min(.25, remaining))
        identity, _network = owned
        subprocess.run(self.docker + ["rm", "-f", identity],
                       capture_output=True, text=True, check=True, timeout=10)
        need(not self._named_ids(), "fixture removal not confirmed: " + self.name)


@contextmanager
def isolated_container(docker):
    """180 seconds for work, then at most 30 seconds for exact-owner cleanup."""
    owner = OwnedContainer(docker)
    watched = (signal.SIGTERM, signal.SIGINT, signal.SIGALRM)
    previous = {sig: signal.getsignal(sig) for sig in watched}
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    primary_error = None

    def interrupted(signum, _frame):
        raise RuntimeError("radio fixture interrupted by signal " + str(signum))

    def expired(_signum, _frame):
        raise TimeoutError("radio fixture exceeded its bounded deadline")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 180)
    try:
        yield owner
    except BaseException as error:
        primary_error = error
        raise
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        # Once stopping, repeated operator signals must not skip cleanup.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.setitimer(signal.ITIMER_REAL, 30)
        try:
            owner.cleanup()
        except BaseException as cleanup_error:
            if primary_error is None:
                raise
            note = "Exact-owner fixture cleanup failed: " + str(cleanup_error)
            if hasattr(primary_error, "add_note"):
                primary_error.add_note(note)
            print(note, file=sys.stderr)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            if previous_timer[0]:
                remaining = max(.001, previous_timer[0] - (time.monotonic() - started))
                signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])


def incoming_fixture_pjsip(content):
    """Retain generated codec policy; identify only the isolated loopback peer."""
    content = helper.fixture_pjsip(content)
    pattern = r"(?ms)^\[201\]\ntype=endpoint\n.*?(?=^\[|\Z)"
    matches = list(re.finditer(pattern, content))
    need(len(matches) == 1, "generated fixture endpoint missing")
    match = matches[0]
    before = match[0]
    need(len(re.findall(r"(?m)^auth=auth-201$", before)) == 1
         and len(re.findall(r"(?m)^context=home$", before)) == 1
         and len(re.findall(r"(?m)^identify_by=username$", before)) == 1,
         "unexpected generated fixture identification")
    after = re.sub(r"(?m)^auth=auth-201\n", "", before)
    after = after.replace("context=home\n", "context=radio-smoke\n", 1)
    after = after.replace("identify_by=username\n", "identify_by=ip\n", 1)
    return (content[:match.start()] + after + content[match.end():]
            + "\n[radio-fixture-loopback]\ntype=identify\nendpoint=201\nmatch=127.0.0.2/32\n")


def media_fixture_dialplan(content):
    """Replace only the network source, preserving generated radio ownership."""
    need(content.count("AGI(radio.py,echo)") == 1, "generated radio route missing")
    content = content.replace("AGI(radio.py,echo)",
                              "AGI(/etc/asterisk/radio_fixture_agi.py,echo)")
    return content + "\n[radio-smoke]\nexten => 799,1,Goto(home,702,1)\n"


def selected_answer(body, payload, codec):
    """A sorted broad answer does not prove a single negotiated voice codec."""
    rows = body.splitlines()
    media = [line.split() for line in rows if line.startswith("m=")]
    need(len(media) == 1 and len(media[0]) >= 4 and media[0][0] == "m=audio"
         and media[0][1].isdigit() and 1 <= int(media[0][1]) <= 65535
         and media[0][2] == "RTP/AVP", "invalid incoming SDP audio answer")
    need(all(value.isdigit() for value in media[0][3:]), "invalid SDP payload")
    accepted = [int(value) for value in media[0][3:]]
    need(len(accepted) == len(set(accepted)), "duplicate SDP payload")
    mappings = {}
    for line in rows:
        match = re.fullmatch(r"a=rtpmap:(\d+) ([^ ]+)", line)
        if match:
            number = int(match[1])
            need(number not in mappings, "duplicate SDP codec mapping")
            mappings[number] = match[2].lower()
    need(mappings.get(payload) == codec.lower() + "/8000",
         "selected incoming codec mapping missing")
    voice = [value for value in accepted
             if not mappings.get(value, "").startswith("telephone-event/")]
    need(voice == [payload], "incoming answer must select exactly one voice codec")
    need(all(value == payload or (value == 101 and mappings.get(value) == "telephone-event/8000")
             for value in accepted), "incoming answer contains an unoffered payload")
    return accepted


class Phone:
    """One isolated outgoing-answer or incoming-offer SIP/RTP peer."""

    reply = helper.Phone.reply

    def __init__(self, codec, payload, *, incoming=False, mixed_offer=False):
        self.codec, self.payload = codec, payload
        self.incoming, self.mixed_offer = incoming, mixed_offer
        self.call_id = secrets.token_hex(16) + "@radio-fixture"
        self.local_from = "<sip:201@127.0.0.2>;tag=" + secrets.token_hex(8)
        self.remote_to = None
        self.answer_payloads = None
        self.sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sip.bind(("127.0.0.2", 5090))
        self.audio.bind(("127.0.0.2", 6000))
        self.stopped = threading.Event()
        self.packets, self.error, self.bye, self.offered = [], None, False, ""
        self.thread = threading.Thread(target=self.listen, daemon=True)
        self.thread.start()

    def send_request(self, method, uri, to, body=""):
        rows = [f"{method} {uri} SIP/2.0",
                "Via: SIP/2.0/UDP 127.0.0.2:5090;rport;branch=z9hG4bK" + secrets.token_hex(12),
                "Max-Forwards: 70", "From: " + self.local_from, "To: " + to,
                "Call-ID: " + self.call_id, "CSeq: 1 " + method,
                "Contact: <sip:201@127.0.0.2:5090>",
                "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS"]
        if body:
            rows.append("Content-Type: application/sdp")
        rows += ["Content-Length: " + str(len(body.encode("ascii"))), "", body]
        self.sip.sendto("\r\n".join(rows).encode("ascii"), ("127.0.0.1", 5060))

    def dial(self):
        need(self.incoming, "only incoming fixture peer can dial")
        mappings = ([(0, "PCMU/8000"), (8, "PCMA/8000"), (18, "G729/8000"),
                     (102, "L16/16000"), (9, "G722/8000"), (116, "iLBC/8000"),
                     (101, "telephone-event/8000")] if self.mixed_offer
                    else [(0, "PCMU/8000")])
        self.offered = ("v=0\r\no=fixture 1 1 IN IP4 127.0.0.2\r\ns=radio-fixture\r\n"
                        "c=IN IP4 127.0.0.2\r\nt=0 0\r\nm=audio 6000 RTP/AVP "
                        + " ".join(str(number) for number, _ in mappings) + "\r\n"
                        + "".join(f"a=rtpmap:{number} {name}\r\n" for number, name in mappings)
                        + ("a=fmtp:18 annexb=no\r\na=fmtp:116 mode=20\r\na=fmtp:101 0-15\r\n"
                           if self.mixed_offer else "")
                        + "a=sendrecv\r\na=ptime:20\r\n")
        self.send_request("INVITE", "sip:799@127.0.0.1", "<sip:799@127.0.0.1>", self.offered)

    def incoming_message(self, message, address):
        h = helper.headers(message)
        method = message.split(" ", 1)[0]
        if method == "OPTIONS":
            self.reply(message, address, "200 OK")
            return
        need(h.get("call-id") == self.call_id, "incoming SIP dialog changed")
        if method == "SIP/2.0":
            need(h.get("cseq") == "1 INVITE", "unexpected incoming SIP transaction")
            status = int(message.split(" ", 2)[1])
            if 100 <= status < 200:
                return
            need(status == 200 and h.get("from") == self.local_from
                 and ";tag=" in h.get("to", ""), "incoming INVITE was not accepted")
            contact = re.fullmatch(r"<(sip:[^<> ]+)>", h.get("contact", ""))
            need(contact is not None
                 and re.fullmatch(r"sip:(?:[^@;: ]+@)?127\.0\.0\.1(?::5060)?(?:;transport=udp)?",
                                  contact[1], re.I),
                 "incoming response has an unexpected remote target")
            need(self.remote_to is None or self.remote_to == h["to"], "incoming dialog tag changed")
            self.remote_to = h["to"]
            # Complete the transaction even when its codec answer fails the oracle.
            self.send_request("ACK", contact[1], self.remote_to)
            self.answer_payloads = selected_answer(
                message.partition("\r\n\r\n")[2], self.payload, self.codec)
        elif method == "BYE":
            need(self.remote_to is not None and h.get("from") == self.remote_to
                 and h.get("to") == self.local_from, "incoming BYE dialog changed")
            self.reply(message, address, "200 OK")
            self.bye = True
        else:
            raise RuntimeError("unexpected incoming SIP method")

    def listen(self):
        try:
            while not self.stopped.is_set():
                for sock in select.select([self.sip, self.audio], [], [], .1)[0]:
                    raw, address = sock.recvfrom(65535)
                    need(address[0] == "127.0.0.1", "non-loopback fixture traffic")
                    if sock is self.audio:
                        need(len(raw) >= 12 and raw[0] >> 6 == 2, "invalid RTP")
                        need(len(self.packets) < 1000, "unbounded fixture RTP")
                        # The isolated Asterisk sender needs no CSRCs/extensions.
                        need(raw[0] & 0x3f == 0, "unexpected RTP options")
                        need(raw[1] & 0x7f == self.payload, "wrong RTP codec")
                        self.packets.append(raw[12:])
                        continue
                    message = raw.decode("ascii")
                    if self.incoming:
                        need(address == ("127.0.0.1", 5060), "unexpected incoming SIP peer")
                        self.incoming_message(message, address)
                        continue
                    method = message.split(" ", 1)[0]
                    if method == "INVITE":
                        self.offered = message.partition("\r\n\r\n")[2]
                        need(f"a=rtpmap:{self.payload} {self.codec}/8000" in self.offered,
                             "requested codec not offered")
                        body = ("v=0\r\no=fixture 1 1 IN IP4 127.0.0.2\r\ns=radio-fixture\r\n"
                                "c=IN IP4 127.0.0.2\r\nt=0 0\r\n"
                                f"m=audio 6000 RTP/AVP {self.payload}\r\n"
                                f"a=rtpmap:{self.payload} {self.codec}/8000\r\na=sendrecv\r\n")
                        self.reply(message, address, "200 OK", body,
                                   "sip:201@127.0.0.2:5090")
                    elif method == "BYE":
                        self.reply(message, address, "200 OK")
                        self.bye = True
                    elif method == "OPTIONS":
                        self.reply(message, address, "200 OK")
        except BaseException as exc:
            self.error = str(exc)

    def close(self):
        self.stopped.set()
        self.thread.join(2)
        self.sip.close()
        self.audio.close()
        need(not self.thread.is_alive(), "fixture peer did not stop")


def amplitude(samples, rate, frequency):
    return 2 * abs(sum(value * complex(math.cos(2 * math.pi * frequency * n / rate),
                                      math.sin(2 * math.pi * frequency * n / rate))
                       for n, value in enumerate(samples))) / len(samples)


@contextmanager
def media_gateway():
    """Use the production reader/Stop path with this fixture's loopback AMI.

    No HTTP listener or phone command is started. Production configuration still
    rejects loopback; the override below is confined to a network-none fixture.
    """
    gateway = module("radio_gateway_fixture", Path(__file__).with_name("phone_gateway.py"))
    secret = json.loads(Path("/etc/asterisk/gateway-ami-secret.json").read_text())
    config = {
        "persistent_enabled": True, "bind_ipv4": "192.168.50.10", "api_port": 18084,
        "api_token": "a" * 48, "api_clients": ["192.168.50.11"],
        "ami": {"host": "192.168.50.10", "port": 5038, **secret},
        "dial_allowlist": ["702"], "services": [
            {"number": "702", "label": "Synthetic radio", "kind": "radio"}],
        "phones": [{"id": "201", "lines": ["201"], "ipv4": "192.168.50.21",
                    "device": "SEP020000000001", "port": 18080,
                    "auth_path": "/auth/" + "c" * 64,
                    "username": "u" * 24, "password": "p" * 24}],
    }
    with tempfile.TemporaryDirectory(prefix="radio-gateway-state-") as state:
        server = gateway.Gateway(config, state)
        server.config["ami"]["host"] = "127.0.0.1"
        server.dnd_pending.clear()
        try:
            yield server, gateway.Refused
        finally:
            server.db.close()


def test_inside():
    need({name for _, name in socket.if_nameindex()} == {"lo"}, "network-none required")
    with media_gateway() as (server, refused):
        return radio_cases(server, refused)


def radio_cases(server, refused):
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                    "aevalsrc=0.2*sin(2*PI*1000*t)+0.2*sin(2*PI*5000*t):s=44100:d=5",
                    "-c:a", "libmp3lame", "-b:a", "192k", "-y",
                    "/tmp/radio-fixture.mp3"], check=True, timeout=15)
    result = {"network": "none", "synthetic_only": True, "physical_phones_called": False,
              "cases": {}, "media": {"marked_playing_cases": 0, "stale_stop_refusals": 0,
                                      "uniqueid_stop": False}}
    previous_media = None
    cases = (("G722", "G722", 9, "g722", 16000, False),
             ("PCMU", "PCMU", 0, "mulaw", 8000, False),
             ("incoming_mixed_G722", "G722", 9, "g722", 16000, True),
             ("incoming_PCMU", "PCMU", 0, "mulaw", 8000, True))
    for case, codec, payload, decoder, rate, incoming in cases:
        receipt_path = Path("/tmp/radio-fixture.json")
        receipt_path.unlink(missing_ok=True)
        peer = Phone(codec, payload, incoming=incoming, mixed_offer=incoming and codec == "G722")
        try:
            def ready():
                need(peer.error is None, str(peer.error))
                status = subprocess.check_output(
                    ["asterisk", "-rx", "pjsip show endpoint 201"], text=True, timeout=5)
                return "Not in use" in status
            await_condition(ready, 8, "synthetic SIP endpoint ready")
            if incoming:
                peer.dial()
            else:
                subprocess.run(["asterisk", "-rx",
                                "channel originate PJSIP/201 extension 799@radio-smoke"],
                               check=True, capture_output=True, timeout=5)
            observed = {}

            def playing():
                state = server.listing()["phones"][0]["media"]
                if state["state"] != "playing":
                    return False
                need(state["source"] == "702" and state["line"] == "201",
                     "radio identity differs from the generated home dialplan")
                observed.update(state)
                return True

            await_condition(playing, 3, "fresh marker and linked AudioSocket bridge")
            result["media"]["marked_playing_cases"] += 1
            if previous_media is not None:
                try:
                    server.mutate("201", "media_stop", {
                        **previous_media, "idempotency_key": "stale-stop-" + case})
                except refused as error:
                    need(error.reason == "media_identity_changed", "unexpected stale Stop refusal")
                else:
                    raise RuntimeError("stale Stop was accepted for a replacement radio call")
                current = server.listing()["phones"][0]["media"]
                need(current["state"] == "playing" and current["uniqueid"] == observed["uniqueid"],
                     "stale Stop affected the replacement call")
                result["media"]["stale_stop_refusals"] += 1
            previous_media = {key: observed[key] for key in ("source", "line", "channel", "uniqueid")}
            if case == "incoming_PCMU":
                await_condition(lambda: len(peer.packets) >= 65, 3, "radio before exact-identity Stop")
                stopped = server.mutate("201", "media_stop", {
                    **previous_media, "idempotency_key": "exact-uniqueid-stop"})
                need(stopped["status"] == "accepted", "uniqueid Stop not accepted")
                result["media"]["uniqueid_stop"] = True
            completed_receipt = {}

            def finished():
                need(peer.error is None, str(peer.error))
                if not peer.bye:
                    return False
                # The fixture creates its receipt before writing the JSON.
                # Do not mistake a partially written file for completion.
                try:
                    receipt = json.loads(receipt_path.read_text())
                except (OSError, ValueError):
                    return False
                need(isinstance(receipt, dict), "invalid radio worker receipt")
                completed_receipt.update(receipt)
                return True

            await_condition(finished, 15, "radio call ended and worker reaped")
            receipt = completed_receipt
            need(receipt["calls"] == 1 and receipt["frames"] >= 50 and receipt["decoder_reaped"],
                 "radio worker did not complete")
            need(receipt["sample_rate"] == 16000 and receipt["frame_bytes"] == 640
                 and receipt["audio_kind"] == 0x12,
                 "radio worker used narrowband PCM")

            def no_channels():
                need(peer.error is None, str(peer.error))
                status = subprocess.check_output(
                    ["asterisk", "-rx", "core show channels count"], text=True, timeout=5)
                rows = {line.strip() for line in status.splitlines()}
                return {"0 active channels", "0 active calls"} <= rows

            await_condition(no_channels, 5, "all synthetic radio channels released")
            need(server.listing()["phones"][0]["media"]["state"] == "idle",
                 "ended radio did not return to idle")
            need(len(peer.packets) >= 50, "missing radio RTP")
            options = ["-ar", "8000", "-ac", "1"] if decoder == "mulaw" else []
            decoded = subprocess.run(
                ["ffmpeg", "-v", "error", "-nostdin", "-f", decoder, *options,
                 "-i", "pipe:0", "-ac", "1", "-ar", str(rate), "-f", "s16le", "pipe:1"],
                input=b"".join(peer.packets), capture_output=True, check=True, timeout=10).stdout
            samples = struct.unpack("<" + "h" * (len(decoded) // 2), decoded)
            need(len(samples) >= rate, "decoded radio too short")
            samples = samples[rate // 4:rate // 4 + rate // 2]
            low = amplitude(samples, rate, 1000)
            need(low > 500, "radio missing 1 kHz reference tone")
            entry = {"rtp_payload": payload, "decoded_rate": rate,
                     "packets": len(peer.packets), "tone_1khz": round(low)}
            if incoming:
                need(peer.answer_payloads is not None, "incoming SDP answer missing")
                entry["answer_payloads"] = peer.answer_payloads
                entry["offer_payloads"] = [0, 8, 18, 102, 9, 116, 101] if peer.mixed_offer else [0]
            if codec == "G722":
                high = amplitude(samples, rate, 5000)
                need(high > 500 and high / low > .25, "5 kHz tone lost in radio path")
                entry["tone_5khz"] = round(high)
            result["cases"][case] = entry
        finally:
            peer.close()
    result["status"] = "PASS"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=os.environ.get("ASTERISK_TEST_IMAGE"))
    parser.add_argument("--docker-context", default=os.environ.get("ASTERISK_DOCKER_CONTEXT"))
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inside:
        print(json.dumps(test_inside(), sort_keys=True))
        return
    need(args.image, "--image or ASTERISK_TEST_IMAGE is required")
    docker = ["docker"] + (["--context", args.docker_context] if args.docker_context else [])
    repo = Path(__file__).resolve().parents[1]
    provision = module("radio_fixture_provision", repo / "scripts/provision.py")
    with isolated_container(docker) as owner, \
            tempfile.TemporaryDirectory(prefix="cisco-radio-wideband-") as temporary:
        root = Path(temporary)
        site = {"node_ipv4": "127.0.0.1", "lan_cidr": "127.0.0.0/8",
                "ntp_ipv4": "127.0.0.1", "phone_cidrs": ["127.0.0.0/8"],
                "gateway": {"enabled": True, "ami_client_ipv4": "127.0.0.1"},
                "radio": {"enabled": True, "stations": ["echo"], "max_seconds": 30}}
        inventory = {"phones": [{"model": "CP-7945G", "verified": True, "mac": "020000000001",
                                 "label": "Fixture", "extension": "201", "transport": "udp",
                                 "firmware_load": "SIP45.9-4-2SR4-3S"}]}
        provision.generate(site, inventory, root)
        folder = root / "asterisk"
        pjsip = folder / "pjsip.conf"
        pjsip.write_text(incoming_fixture_pjsip(pjsip.read_text()))
        dialplan = folder / "extensions.conf"
        # Preserve the real home/702 context, source/UID marker and cleanup.
        dialplan.write_text(media_fixture_dialplan(dialplan.read_text()))
        (folder / "gateway-ami-secret.json").write_bytes((root / "gateway-ami-secret.json").read_bytes())
        (folder / "phone_gateway.py").write_bytes((repo / "containers/asterisk/phone_gateway.py").read_bytes())
        for name in ("test-radio-wideband.py", "test-alert-originate.py"):
            (folder / name).write_bytes((repo / "scripts" / name).read_bytes())
        fixture = folder / "radio_fixture_agi.py"
        fixture.write_bytes((repo / "tests/radio_fixture_agi.py").read_bytes())
        fixture.chmod(0o755)
        try:
            container = owner.create(args.image)
            subprocess.run(docker + ["cp", str(folder) + "/.", container + ":/etc/asterisk/"],
                           capture_output=True, check=True, timeout=15)
            # Exercise this checkout's worker even when using an older base image.
            subprocess.run(docker + ["cp", str(repo / "containers/asterisk/radio.py"),
                                     container + ":/var/lib/asterisk/agi-bin/radio.py"],
                           capture_output=True, check=True, timeout=15)
            subprocess.run(docker + ["start", container], capture_output=True, check=True, timeout=15)
            def loaded():
                output = subprocess.run(docker + ["exec", container, "asterisk", "-rx",
                                                  "dialplan show 702@home"],
                                        capture_output=True, text=True, timeout=5)
                return output.returncode == 0 and "radio_fixture_agi.py,echo" in output.stdout
            await_condition(loaded, 25, "radio fixture dialplan loaded")
            completed = subprocess.run(docker + ["exec", container, "python3",
                                                 "/etc/asterisk/test-radio-wideband.py", "--inside"],
                                       capture_output=True, text=True, timeout=90)
            need(completed.returncode == 0, completed.stdout[-3000:] + completed.stderr[-3000:])
            result = json.loads(completed.stdout)
            need(result.get("status") == "PASS", "radio fixture did not pass")
        except Exception:
            if owner.created:
                helper.failure_logs(docker, owner.container)
            raise
    # Never report PASS before exact-owner removal has been confirmed.
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
