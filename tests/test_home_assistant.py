import copy
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("provision_ha", ROOT / "scripts/provision.py")
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class HomeAssistantTests(unittest.TestCase):
    def setUp(self):
        self.site = {"node_ipv4": "192.168.50.10", "lan_cidr": "192.168.50.0/24", "ntp_ipv4": "192.168.50.1"}
        self.ha = {"enabled": True, "ami_client_ipv4": "192.168.50.30", "allow_originate": True,
                   "assist": {"enabled": True, "host_ipv4": "192.168.50.30", "sip_port": 5060}}
        self.inventory = {"phones": [{"model": "CP-7945G", "verified": True, "mac": "020000000001",
                                     "extension": "201", "label": "Office", "firmware_load": "SIP45.9-4-2SR4-3S"}]}

    def test_disabled_has_no_management_listener_or_assist_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            p.generate(self.site, self.inventory, tmp)
            root = Path(tmp)
            self.assertIn("enabled=no", (root / "asterisk/manager.conf").read_text())
            self.assertNotIn("630", (root / "asterisk/extensions.conf").read_text())
            self.assertFalse((root / "ha-ami-secret.json").exists())

    def test_enabled_persists_separate_password_and_limits_network_and_actions(self):
        self.site["home_assistant"] = self.ha
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p.generate(self.site, self.inventory, tmp)
            secret = json.loads((root / "ha-ami-secret.json").read_text())
            manager = (root / "asterisk/manager.conf").read_text()
            self.assertIn("bindaddr=192.168.50.10", manager)
            self.assertIn("permit=192.168.50.30/255.255.255.255", manager)
            self.assertIn("read=call,dtmf\nwrite=system,originate\n", manager)
            self.assertIn("secret=" + secret["secret"], manager)
            self.assertEqual(stat.S_IMODE((root / "ha-ami-secret.json").stat().st_mode), 0o600)
            self.assertNotIn(secret["secret"], (root / "tftp/SEP020000000001.cnf.xml").read_text())
            p.generate(self.site, self.inventory, tmp)
            self.assertEqual(secret, json.loads((root / "ha-ami-secret.json").read_text()))
            self.ha["allow_originate"] = False
            p.generate(self.site, self.inventory, tmp)
            self.assertIn("write=system\n", (root / "asterisk/manager.conf").read_text())
            self.assertEqual(secret, json.loads((root / "ha-ami-secret.json").read_text()))

    def test_assist_uses_opus_keeps_caller_and_rejects_inbound_trunk_calls(self):
        self.site["home_assistant"] = self.ha
        with tempfile.TemporaryDirectory() as tmp:
            p.generate(self.site, self.inventory, tmp)
            root = Path(tmp)
            sip = (root / "asterisk/pjsip.conf").read_text()
            dial = (root / "asterisk/extensions.conf").read_text()
            self.assertIn("allow=opus", sip)
            self.assertNotIn("from_user=", sip)
            self.assertIn("context=ha-deny-inbound", sip)
            self.assertIn("Dial(PJSIP/ha-assist/sip:630@192.168.50.30:5060,60)", dial)
            self.assertIn("[ha-deny-inbound]\nexten => _.,1,Hangup(21)", dial)
            self.assertIn('MATCH="630"', (root / "tftp/dialplan.xml").read_text())
            self.ha["enabled"] = False
            p.generate(self.site, self.inventory, tmp)
            self.assertNotIn("ha-assist", (root / "asterisk/pjsip.conf").read_text())
            self.assertNotIn('MATCH="630"', (root / "tftp/dialplan.xml").read_text())

    def test_rejects_public_sources_invalid_flags_and_sip_bind_collision(self):
        for patch in ({"ami_client_ipv4": "0.0.0.0"}, {"ami_client_ipv4": "8.8.8.8"},
                      {"enabled": "yes"}, {"allow_originate": "yes"},
                      {"assist": {"enabled": True, "host_ipv4": "192.168.50.10", "sip_port": 5060}},
                      {"assist": {"enabled": True, "host_ipv4": "192.168.50.30", "sip_port": "5060\n"}}):
            site = dict(self.site, home_assistant={**copy.deepcopy(self.ha), **patch})
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                p.generate(site, self.inventory, "/unused")

    def test_existing_secret_symlink_is_rejected(self):
        self.site["home_assistant"] = self.ha
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "ha-ami-secret.json").symlink_to(Path(tmp) / "missing.json")
            with self.assertRaises(ValueError):
                p.generate(self.site, self.inventory, tmp)

    def test_preflight_rejects_stale_management_and_assist_configuration(self):
        self.site.update({"home_assistant": self.ha, "namespace": "telephony", "node_name": "test",
                          "kube_context": "test", "asterisk_image": "example.invalid/pbx@sha256:" + "a" * 64})
        with tempfile.TemporaryDirectory() as tmp:
            site_path = Path(tmp) / "site.json"
            p.generate(self.site, self.inventory, tmp)
            site_path.write_text(json.dumps(self.site))
            command = [sys.executable, str(ROOT / "scripts/preflight.py"), "--site", str(site_path)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            for patch in ({"enabled": False}, {"ami_client_ipv4": "192.168.50.31"}, {"allow_originate": False},
                          {"assist": {"enabled": True, "host_ipv4": "192.168.50.31"}}):
                changed = dict(self.site, home_assistant={**self.ha, **patch})
                site_path.write_text(json.dumps(changed))
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("rerun provision.py", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
