"""Phone admission must be independent from the PBX interface subnet."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("provision_networks", ROOT / "scripts/provision.py")
provision = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provision)


class PhoneNetworkTests(unittest.TestCase):
    def setUp(self):
        self.site = {"node_ipv4": "192.168.50.10", "lan_cidr": "192.168.50.0/24",
                     "ntp_ipv4": "192.168.50.1", "namespace": "telephony", "node_name": "test",
                     "kube_context": "test", "asterisk_image": "example.invalid/pbx@sha256:" + "a" * 64}
        self.inventory = {"phones": [{"model": "CP-7945G", "verified": True, "mac": "020000000001",
                                      "extension": "201", "label": "Test", "firmware_load": "SIP45.9-4-2SR4-3S"}]}

    def test_explicit_phone_host_replaces_lan_acl_but_keeps_transport_lan(self):
        self.site["phone_cidrs"] = ["192.168.60.25/32", "10.20.30.40/32"]
        with tempfile.TemporaryDirectory() as tmp:
            provision.generate(self.site, self.inventory, tmp)
            text = (Path(tmp) / "asterisk/pjsip.conf").read_text()
            self.assertIn("deny=0.0.0.0/0\npermit=192.168.60.25/32\npermit=10.20.30.40/32", text)
            self.assertNotIn("permit=192.168.50.0/24", text)
            self.assertEqual(text.count("local_net=192.168.50.0/24"), 2)
            self.assertEqual(text.count("local_net=192.168.60.25/32"), 2)
            self.assertIn("bind=192.168.50.10:5060", text)
            self.assertIn("media_address=192.168.50.10", text)

    def test_default_preserves_previous_lan_admission(self):
        self.assertEqual(provision.phone_networks(self.site), ["192.168.50.0/24"])
        self.assertEqual(provision.local_networks(self.site), ["192.168.50.0/24"])

    def test_invalid_allowlists_fail_before_output_is_created(self):
        invalid = (None, [], "192.168.60.25/32", [False], ["192.168.60.25"],
                   ["192.168.60.25/24"], ["0.0.0.0/0"], ["8.8.8.8/32"], ["::1/128"],
                   ["127.0.0.1/32"], ["192.168.60.25/32", "192.168.60.25/32"],
                   ["192.168.60.0/24", "192.168.60.25/32"])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "not-created"
            for value in invalid:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    provision.generate(dict(self.site, phone_cidrs=value), self.inventory, out)
                self.assertFalse(out.exists())

    def test_preflight_detects_acl_drift_and_regeneration_repairs_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "site.json"
            provision.generate(self.site, self.inventory, root)
            self.site["phone_cidrs"] = ["192.168.60.25/32"]
            path.write_text(json.dumps(self.site))
            command = [sys.executable, str(ROOT / "scripts/preflight.py"), "--site", str(path)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("rerun provision.py", result.stderr)
            provision.generate(self.site, self.inventory, root)
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            # A broadened phone permit is rejected even when transports are current.
            config = root / "asterisk/pjsip.conf"
            config.write_text(config.read_text().replace("permit=192.168.60.25/32", "permit=192.168.50.0/24"))
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Phone source ACL", result.stderr)

    def test_phone_network_does_not_change_ami_or_assist_acl(self):
        self.site["phone_cidrs"] = ["192.168.60.25/32"]
        self.site["home_assistant"] = {"enabled": True, "ami_client_ipv4": "10.0.0.30",
                                        "assist": {"enabled": True, "host_ipv4": "10.0.0.30"}}
        with tempfile.TemporaryDirectory() as tmp:
            provision.generate(self.site, self.inventory, tmp)
            root = Path(tmp) / "asterisk"
            self.assertIn("permit=10.0.0.30/255.255.255.255", (root / "manager.conf").read_text())
            self.assertNotIn("192.168.60.25", (root / "manager.conf").read_text())
            text = (root / "pjsip.conf").read_text()
            assist = text.split("[ha-assist]", 1)[1].split("\n[", 1)[0]
            self.assertIn("permit=10.0.0.30/32", assist)
            self.assertNotIn("permit=192.168.60.25", assist)

    def test_bootstrap_accepts_acl_without_creating_fake_phone(self):
        self.site["phone_cidrs"] = ["192.168.60.25/32"]
        self.site.pop("ntp_ipv4")
        with tempfile.TemporaryDirectory() as tmp:
            provision.generate(self.site, None, tmp, pbx_only=True)
            root = Path(tmp)
            self.assertFalse((root / "tftp").exists())
            self.assertFalse((root / "secrets.json").exists())
            self.assertNotIn("type=endpoint", (root / "asterisk/pjsip.conf").read_text())


if __name__ == "__main__":
    unittest.main()
