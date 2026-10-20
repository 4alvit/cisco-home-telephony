#!/usr/bin/env python3
"""Verify radio bandwidth and G.711 fallback in a disposable, network-none PBX.

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


class Phone:
    """Immediately answer one loopback call with a selected codec."""

    reply = helper.Phone.reply

    def __init__(self, codec, payload):
        self.codec, self.payload = codec, payload
        self.sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sip.bind(("127.0.0.2", 5090))
        self.audio.bind(("127.0.0.2", 6000))
        self.stopped = threading.Event()
        self.packets, self.error, self.bye, self.offered = [], None, False, ""
        self.thread = threading.Thread(target=self.listen, daemon=True)
        self.thread.start()

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


def test_inside():
    need({name for _, name in socket.if_nameindex()} == {"lo"}, "network-none required")
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                    "aevalsrc=0.2*sin(2*PI*1000*t)+0.2*sin(2*PI*5000*t):s=44100:d=5",
                    "-c:a", "libmp3lame", "-b:a", "192k", "-y",
                    "/var/lib/asterisk/radio-fixture.mp3"], check=True, timeout=15)
    result = {"network": "none", "synthetic_only": True, "physical_phones_called": False, "cases": {}}
    for codec, payload, decoder, rate in (("G722", 9, "g722", 16000),
                                         ("PCMU", 0, "mulaw", 8000)):
        receipt_path = Path("/tmp/radio-fixture.json")
        receipt_path.unlink(missing_ok=True)
        peer = Phone(codec, payload)
        try:
            def ready():
                need(peer.error is None, str(peer.error))
                status = subprocess.check_output(
                    ["asterisk", "-rx", "pjsip show endpoint 201"], text=True, timeout=5)
                return "Not in use" in status
            await_condition(ready, 8, "synthetic SIP endpoint ready")
            subprocess.run(["asterisk", "-rx",
                            "channel originate PJSIP/201 extension 799@radio-smoke"],
                           check=True, capture_output=True, timeout=5)
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
            if codec == "G722":
                high = amplitude(samples, rate, 5000)
                need(high > 500 and high / low > .25, "5 kHz tone lost in radio path")
                entry["tone_5khz"] = round(high)
            result["cases"][codec] = entry
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
                "radio": {"enabled": True, "stations": ["echo"], "max_seconds": 30}}
        inventory = {"phones": [{"model": "CP-7945G", "verified": True, "mac": "020000000001",
                                 "label": "Fixture", "extension": "201", "transport": "udp",
                                 "firmware_load": "SIP45.9-4-2SR4-3S"}]}
        provision.generate(site, inventory, root)
        folder = root / "asterisk"
        pjsip = folder / "pjsip.conf"
        pjsip.write_text(helper.fixture_pjsip(pjsip.read_text()))
        with (folder / "extensions.conf").open("a") as stream:
            stream.write("\n[radio-smoke]\nexten => 799,1,Answer()\n"
                         " same => n,AGI(/etc/asterisk/radio_fixture_agi.py,echo)\n"
                         " same => n,Hangup()\n")
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
                                                  "dialplan show 799@radio-smoke"],
                                        capture_output=True, text=True, timeout=5)
                return output.returncode == 0 and "radio_fixture_agi.py,echo" in output.stdout
            await_condition(loaded, 25, "radio fixture dialplan loaded")
            completed = subprocess.run(docker + ["exec", container, "python3",
                                                 "/etc/asterisk/test-radio-wideband.py", "--inside"],
                                       capture_output=True, text=True, timeout=55)
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
