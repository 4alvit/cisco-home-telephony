"""PBX bootstrap must not pretend that any physical phone was commissioned."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("provision_bootstrap", ROOT / "scripts/provision.py")
p = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p)


class BootstrapTest(unittest.TestCase):
    def setUp(self):
        self.site = {
            "node_ipv4": "192.168.50.10", "lan_cidr": "192.168.50.0/24",
            "node_name": "voice-node", "namespace": "telephony", "kube_context": "test-only",
            "asterisk_image": "example.invalid/asterisk@sha256:" + "a" * 64,
            "home_assistant": {"enabled": True, "ami_client_ipv4": "10.20.0.10",
                               "allow_originate": False, "assist": {"enabled": False}},
        }

    def test_bootstrap_has_services_but_no_physical_phone_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(p.generate(self.site, None, root, pbx_only=True), 0)
            self.assertFalse((root / "tftp").exists())
            self.assertFalse((root / "secrets.json").exists())
            self.assertFalse(list(root.rglob("*.xml")))
            sip = (root / "asterisk/pjsip.conf").read_text()
            self.assertIn("[transport-tcp]", sip)
            self.assertIn("[transport-udp]", sip)
            self.assertNotIn("type=endpoint", sip)
            self.assertNotIn("type=auth", sip)
            dial = (root / "asterisk/extensions.conf").read_text()
            for text in ("exten => 600,1,Answer()", "Echo()", "exten => 601,1,Answer()", "Milliwatt()"):
                self.assertIn(text, dial)
            for text in ("exten => 610", "exten => 620", "exten => 630", "Dial(PJSIP/"):
                self.assertNotIn(text, dial)
            self.assertIn("permit=10.20.0.10/255.255.255.255", (root / "asterisk/manager.conf").read_text())
            self.assertIn("write=system\n", (root / "asterisk/manager.conf").read_text())

    def test_ami_secret_is_private_and_survives_regeneration(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / "ha-ami-secret.json"
            p.generate(self.site, None, tmp, pbx_only=True)
            first = secret.read_bytes()
            self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
            p.generate(self.site, None, tmp, pbx_only=True)
            self.assertEqual(first, secret.read_bytes())

    def test_bootstrap_does_not_enable_ami_implicitly(self):
        site = copy.deepcopy(self.site)
        del site["home_assistant"]
        with tempfile.TemporaryDirectory() as tmp:
            p.generate(site, None, tmp, pbx_only=True)
            root = Path(tmp)
            self.assertIn("enabled=no", (root / "asterisk/manager.conf").read_text())
            self.assertFalse((root / "ha-ami-secret.json").exists())

    def test_assist_remains_explicit_and_has_no_phone_endpoint(self):
        site = copy.deepcopy(self.site)
        site["home_assistant"]["assist"] = {"enabled": True, "host_ipv4": "10.20.0.10", "sip_port": 5060}
        with tempfile.TemporaryDirectory() as tmp:
            p.generate(site, None, tmp, pbx_only=True)
            sip = (Path(tmp) / "asterisk/pjsip.conf").read_text()
            dial = (Path(tmp) / "asterisk/extensions.conf").read_text()
            self.assertEqual(sip.count("type=endpoint"), 1)
            self.assertIn("[ha-assist]", sip)
            self.assertIn("Dial(PJSIP/ha-assist/sip:630@10.20.0.10:5060,60)", dial)
            self.assertFalse((Path(tmp) / "tftp").exists())

    def test_normal_mode_still_rejects_empty_or_unverified_inventory(self):
        site = dict(self.site, ntp_ipv4="192.168.50.1")
        for inventory in ({"phones": []}, {"phones": [{"model": "CP-7945G", "verified": False}]}):
            with self.subTest(inventory=inventory), self.assertRaises(ValueError):
                p.validate(site, inventory)

    def test_bootstrap_rejects_inventory_and_invalid_network_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "new"
            with self.assertRaisesRegex(ValueError, "does not accept"):
                p.generate(self.site, {"phones": []}, root, pbx_only=True)
            with self.assertRaisesRegex(ValueError, "usable address"):
                p.generate(dict(self.site, node_ipv4="10.99.0.1"), None, root, pbx_only=True)
            self.assertFalse(root.exists())

    def test_bootstrap_refuses_stale_phone_artifacts_without_overwriting(self):
        for name in ("tftp", "secrets.json"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                if name == "tftp":
                    (root / name).mkdir()
                    (root / name / "SEP020000000001.cnf.xml").write_text("keep")
                else:
                    (root / name).write_text("keep")
                with self.assertRaisesRegex(ValueError, "without phone artifacts"):
                    p.generate(self.site, None, root, pbx_only=True)
                self.assertFalse((root / "asterisk").exists())
                target = root / name / "SEP020000000001.cnf.xml" if name == "tftp" else root / name
                self.assertEqual(target.read_text(), "keep")

    def test_cli_and_deployment_preflight_need_no_inventory_or_ntp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            site_path = root / "site.json"
            site_path.write_text(json.dumps(self.site))
            result = subprocess.run([sys.executable, str(ROOT / "scripts/provision.py"),
                                     "--pbx-only", "--site", str(site_path), "--out", tmp],
                                    cwd=tmp, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            secret = json.loads((root / "ha-ami-secret.json").read_text())["secret"]
            self.assertNotIn(secret, result.stdout + result.stderr)
            result = subprocess.run([sys.executable, str(ROOT / "scripts/preflight.py"), "--site", str(site_path)],
                                    cwd=tmp, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([sys.executable, str(ROOT / "scripts/preflight.py"),
                                     "--site", str(site_path), "--with-tftp"],
                                    cwd=tmp, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)

    def test_cli_rejects_combined_inventory_and_bootstrap(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/provision.py"),
                                 "--pbx-only", "--inventory", "/not-read.json"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with argument", result.stderr)

    def test_verified_phone_can_be_added_later_without_rotating_ami_secret(self):
        inventory = {"phones": [{"verified": True, "model": "CP-7945G", "mac": "020000000001",
                                 "extension": "201", "label": "Test", "firmware_load": "SIP45.9-4-2SR4-3S"}]}
        with tempfile.TemporaryDirectory() as tmp:
            p.generate(self.site, None, tmp, pbx_only=True)
            root = Path(tmp)
            secret = (root / "ha-ami-secret.json").read_bytes()
            p.generate(dict(self.site, ntp_ipv4="192.168.50.1"), inventory, tmp)
            self.assertTrue((root / "tftp/SEP020000000001.cnf.xml").exists())
            self.assertIn("exten => 620,1,Dial(PJSIP/201,25)", (root / "asterisk/extensions.conf").read_text())
            self.assertEqual(secret, (root / "ha-ami-secret.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
