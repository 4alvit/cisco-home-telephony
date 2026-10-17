import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).parents[1]
def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result
r = module("radio_test", "containers/asterisk/radio.py")
p = module("provision_radio_test", "scripts/provision.py")


class RadioConfiguration(unittest.TestCase):
    def setUp(self):
        self.site = {"node_ipv4": "192.168.50.10", "lan_cidr": "192.168.50.0/24", "ntp_ipv4": "192.168.50.1"}
        self.inventory = {"phones": [{"model": "CP-7945G", "verified": True, "mac": f"02000000000{n}",
            "label": "Phone", "firmware_load": "SIP45.9-4-2SR4-3S",
            "lines": [{"extension": str(199+n*2), "label": str(199+n*2)},
                      {"extension": str(200+n*2), "label": str(200+n*2)}]} for n in range(1, 5)]}

    def test_defaults_disabled_and_enable_disable_preserves_all_phone_passwords(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p.generate(self.site, self.inventory, root)
            passwords = (root / "secrets.json").read_bytes()
            xml = (root / "tftp/SEP020000000001.cnf.xml").read_bytes()
            baseline = (root / "asterisk/extensions.conf").read_text()
            self.assertNotIn("radio.py", baseline)
            self.site.update(radio={"enabled": True, "stations": ["echo", "bob-metal"], "max_seconds": 90},
                             alarms={"enabled": True, "duration_seconds": 5})
            p.generate(self.site, self.inventory, root)
            dial = (root / "asterisk/extensions.conf").read_text()
            self.assertIn("AGI(radio.py,echo)", dial)
            self.assertIn("AGI(radio.py,bob-metal)", dial)
            self.assertNotIn("exten => 701,", dial)
            self.assertIn("Wait(5)", dial)
            self.assertIn("TIMEOUT(absolute)=120", dial)
            for ext in range(201, 209):
                self.assertIn(f"exten => {ext},1,Dial(PJSIP/{ext},25)", dial)
            self.assertIn("Dial(PJSIP/201&PJSIP/203&PJSIP/205&PJSIP/207,25)", dial)
            self.assertEqual(passwords, (root / "secrets.json").read_bytes())
            self.assertEqual(xml, (root / "tftp/SEP020000000001.cnf.xml").read_bytes())
            template = (root / "tftp/dialplan.xml").read_text()
            for ext in ("640", "702", "704"):
                self.assertIn(f'MATCH="{ext}"', template)
            self.site.pop("radio"); self.site.pop("alarms")
            p.generate(self.site, self.inventory, root)
            self.assertEqual(baseline, (root / "asterisk/extensions.conf").read_text())
            self.assertFalse((root / "asterisk/radio.conf").exists())

    def test_rejects_arbitrary_urls_injection_and_resource_limits(self):
        cases = [{"enabled": "yes"}, {"enabled": True, "url": "http://127.0.0.1/"},
                 {"enabled": True, "stations": ["echo", "echo"]}, {"enabled": True, "stations": ["echo\n"]},
                 {"enabled": True, "stations": []}, {"enabled": True, "max_seconds": 7201},
                 {"enabled": True, "max_calls": True}, {"enabled": True, "max_calls": 5}]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                r.settings({"radio": case})
        for seconds in (0, 31, True, "5"):
            with self.assertRaises(ValueError):
                p.alarm_settings({"alarms": {"enabled": True, "duration_seconds": seconds}})

    def test_gateway_secrets_and_dnd_are_opt_in_and_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.site["home_assistant"] = {"enabled": True, "ami_client_ipv4": "192.168.50.30", "allow_originate": True}
            p.generate(self.site, self.inventory, root)
            existing = (root / "ha-ami-secret.json").read_bytes()
            self.site["gateway"] = {"enabled": True, "ami_client_ipv4": "192.168.50.10"}
            p.generate(self.site, self.inventory, root)
            saved = (root / "gateway-ami-secret.json").read_bytes()
            self.assertNotEqual(existing, saved)
            self.assertEqual(existing, (root / "ha-ami-secret.json").read_bytes())
            manager = (root / "asterisk/manager.conf").read_text()
            self.assertIn("[cisco_gateway]", manager)
            self.assertIn("permit=192.168.50.10/255.255.255.255", manager)
            self.assertIn("read=system,call\nwrite=system,call,originate", manager)
            self.assertNotIn("command", manager)
            dial = (root / "asterisk/extensions.conf").read_text()
            for ext in range(201, 209):
                self.assertIn(f"DB(cisco-dnd/{ext})", dial)
            group = dial.split("exten => 620,", 1)[1].split("; Internal service", 1)[0]
            for ext in (202, 204, 206, 208):
                self.assertNotIn(f"PJSIP/{ext}", group)
            p.generate(self.site, self.inventory, root)
            self.assertEqual(saved, (root / "gateway-ami-secret.json").read_bytes())
            self.site["gateway"]["enabled"] = False
            p.generate(self.site, self.inventory, root)
            self.assertNotIn("[cisco_gateway]", (root / "asterisk/manager.conf").read_text())
            self.assertNotIn("cisco-dnd", (root / "asterisk/extensions.conf").read_text())
            self.assertEqual(saved, (root / "gateway-ami-secret.json").read_bytes())

    def test_gateway_only_can_enable_ami_and_rejects_public_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.site["gateway"] = {"enabled": True, "ami_client_ipv4": "192.168.50.10"}
            p.generate(self.site, self.inventory, tmp)
            manager = (Path(tmp) / "asterisk/manager.conf").read_text()
            self.assertIn("enabled=yes", manager)
            self.assertNotIn("[homeassistant]", manager)
        for address in ("8.8.8.8", "0.0.0.0", "::1"):
            with self.assertRaises(ValueError):
                p.gateway_settings({"node_ipv4": "192.168.50.10", "gateway": {"enabled": True, "ami_client_ipv4": address}})

    def test_preflight_refuses_stale_radio_and_alarm_config(self):
        site = dict(self.site, namespace="telephony", node_name="voice-node", kube_context="test",
                    asterisk_image="example.invalid/pbx@sha256:" + "a" * 64,
                    radio={"enabled": True, "stations": ["echo"]}, alarms={"enabled": True})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "site.json"
            p.generate(site, self.inventory, root)
            path.write_text(json.dumps(site))
            command = [sys.executable, str(ROOT / "scripts/preflight.py"), "--site", str(path)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            runtime = root / "asterisk/radio.conf"
            expected = runtime.read_text()
            runtime.write_text(expected.replace('"echo"', '"bob-metal"'))
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Radio settings differ", result.stderr)
            runtime.write_text(expected)
            site["alarms"]["enabled"] = False
            path.write_text(json.dumps(site))
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Disabled alarms have stale routes", result.stderr)


class RadioIsolation(unittest.TestCase):
    def test_url_policy_rejects_cross_host_local_schemes_credentials_and_downgrades(self):
        station = r.catalog()["echo"]
        for url in ("file:///etc/passwd", "http://127.0.0.1/", "https://example.com/", station["url"].replace("https:", "http:"),
                    station["url"].replace("https://", "https://user:password@"), station["url"] + "#bad", station["url"] + "\n"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                r.checked_url(url, station)
        self.assertEqual(r.checked_url(station["url"], station).scheme, "https")

    def test_private_or_mixed_dns_is_rejected(self):
        def answer(ip):
            return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
        for records in ([], [answer("127.0.0.1")], [answer("192.168.50.10")], [answer("224.0.0.1")],
                        [answer("8.8.8.8"), answer("169.254.169.254")]):
            with patch.object(r.socket, "getaddrinfo", return_value=records), self.assertRaises(ValueError):
                r.public_addresses("station.example", 443)

    def test_rotating_broadcaster_cdn_is_narrowly_matched(self):
        station = r.catalog()["matryoshka"]
        r.checked_url("http://n05-eu1.rcs.revma.com/example", station)
        for host in ("evil.rcs.revma.com", "n05-eu1.rcs.revma.com.example.org", "n05-us1.rcs.revma.com"):
            with self.assertRaises(ValueError):
                r.checked_url(f"http://{host}/example", station)

    def test_hls_parser_bounds_and_rejects_encryption_and_file_references(self):
        station = r.catalog()["radyo-viva"]
        master, urls = r.hls_playlist('#EXTM3U\n#EXTINF:3,\nhttps://chunks.radyotvonline.net/one.aac\n', station['url'])
        self.assertFalse(master)
        r.checked_url(urls[0], station)
        for body in ('not a playlist', '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128\nhttps://x/one.aac',
                     '#EXTM3U\n#EXT-X-BYTERANGE:10\nhttps://x/one.aac', '#EXTM3U\n'+'x'*65536):
            with self.assertRaises(ValueError):
                r.hls_playlist(body, station['url'])
        _, urls = r.hls_playlist('#EXTM3U\nfile:///etc/passwd\n', station['url'])
        with self.assertRaises(ValueError):
            r.checked_url(urls[0], station)

    def test_ffmpeg_has_no_network_input_and_no_shell(self):
        args = r.ffmpeg_args(r.catalog()["echo"])
        self.assertEqual(args[args.index("-i") + 1], "pipe:0")
        self.assertEqual(args[args.index("-protocol_whitelist") + 1], "pipe")
        self.assertNotIn("https", " ".join(args))

    def test_slot_limit_releases_after_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = r.acquire_slot(tmp, 1)
            try:
                with self.assertRaises(ValueError):
                    r.acquire_slot(tmp, 1)
            finally:
                os.close(first)
            os.close(r.acquire_slot(tmp, 1))

    def test_bad_uuid_never_launches_fetch_or_decoder(self):
        listener = socket.socket(); listener.bind(("127.0.0.1", 0)); listener.listen(1); listener.settimeout(1)
        call = r.Call(r.catalog()["echo"], 1, uuid.uuid4())
        with patch.object(r.subprocess, "Popen") as popen:
            thread = threading.Thread(target=call.serve, args=(listener,)); thread.start()
            with socket.create_connection(listener.getsockname()) as conn:
                conn.sendall(b"\x01\x00\x10" + uuid.uuid4().bytes)
            thread.join(3)
            listener.close()
            self.assertFalse(thread.is_alive())
            self.assertTrue(call.failed)
            popen.assert_not_called()

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required for real decoder test")
    def test_real_mp3_decode_is_paced_and_hangup_reaps_process(self):
        audio = subprocess.check_output(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                                         "-f", "mp3", "pipe:1"], stderr=subprocess.DEVNULL, timeout=10)
        class Http:
            def close(self):
                pass
        token = uuid.uuid4()
        call = r.Call(r.catalog()["echo"], 4, token, opener=lambda _s: (Http(), io.BytesIO(audio)))
        listener = socket.socket(); listener.bind(("127.0.0.1", 0)); listener.listen(1); listener.settimeout(1)
        thread = threading.Thread(target=call.serve, args=(listener,)); thread.start()
        with socket.create_connection(listener.getsockname(), timeout=3) as conn:
            conn.sendall(b"\x01\x00\x10" + token.bytes)
            samples = []
            start = time.monotonic()
            for _ in range(25):
                kind, data = r.receive_frame(conn)
                self.assertEqual(kind, 0x10); self.assertEqual(len(data), 320)
                samples.extend(struct.unpack("<160h", data))
            self.assertGreater(time.monotonic() - start, .35)
            self.assertGreater(max(abs(x) for x in samples), 100)
            conn.sendall(b"\x00\x00\x00")
        thread.join(3); listener.close()
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(call.proc.poll())


if __name__ == "__main__":
    unittest.main()
