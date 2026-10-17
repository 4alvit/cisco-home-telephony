import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("render_k3s", ROOT / "scripts/render_k3s.py")
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)


def fixture():
    return {"kube_context": "test-context", "namespace": "telephony", "node_name": "test-node",
            "node_ipv4": "192.168.50.58", "lan_cidr": "192.168.50.0/24",
            "asterisk_image": "example.invalid/asterisk@sha256:" + "a" * 64,
            "tftp_image": "example.invalid/tftp@sha256:" + "b" * 64}


def configs():
    result = {name + ".conf": "; unit test fixture\n" for name in ("asterisk", "pjsip", "extensions", "rtp", "modules", "logger")}
    result["pjsip.conf"] = "".join(
        f"[transport-{protocol}]\ntype=transport\nprotocol={protocol}\n"
        "bind=192.168.50.58:5060\nlocal_net=192.168.50.0/24\n\n"
        for protocol in ("udp", "tcp"))
    return result


class K3sSafetyTests(unittest.TestCase):
    def test_mutable_image_public_address_and_shared_namespace_rejected(self):
        for changes in ({"asterisk_image": "image:latest"}, {"node_ipv4": "8.8.8.8", "lan_cidr": "8.8.8.0/24"},
                        {"node_ipv4": "192.168.161.4"}, {"namespace": "default"}, {"kube_context": ""}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                render.validate_site(dict(fixture(), **changes))

    def test_default_has_no_provisioning_or_public_exposure_resources(self):
        manifest = render.render_core(fixture(), configs())
        self.assertEqual([i["kind"] for i in manifest["items"]], ["Namespace", "Secret", "Deployment"])
        deployment = manifest["items"][-1]["spec"]
        self.assertEqual(deployment["replicas"], 1)
        self.assertEqual(deployment["strategy"]["type"], "Recreate")
        pod = deployment["template"]["spec"]
        self.assertTrue(pod["hostNetwork"])
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchFields"][0]["values"], ["test-node"])

    def test_credentials_change_secret_and_old_revision_is_restorable(self):
        before = render.render_core(fixture(), configs())
        changed = dict(configs(), **{"pjsip.conf": "password=new-random-test-value\n"})
        after = render.render_core(fixture(), changed)
        old_name = before["items"][1]["metadata"]["name"]
        new_name = after["items"][1]["metadata"]["name"]
        self.assertNotEqual(old_name, new_name)
        self.assertTrue(after["items"][1]["immutable"])
        self.assertEqual(after["items"][2]["spec"]["template"]["spec"]["volumes"][0]["secret"]["secretName"], new_name)

    def test_tftp_is_bounded_and_readonly(self):
        manifest = render.render_tftp(fixture(), {"SEP000000000001.cnf.xml": "<device/>"})
        job = manifest["items"][1]["spec"]
        self.assertEqual(job["activeDeadlineSeconds"], 3600)
        self.assertEqual(job["backoffLimit"], 0)
        daemon = job["template"]["spec"]["containers"][0]
        self.assertIn("20000:20031", daemon["args"])
        self.assertNotIn("--create", daemon["args"])
        self.assertTrue(daemon["volumeMounts"][0]["readOnly"])

    def test_readiness_requires_both_transports_not_just_running_process(self):
        probe = render.render_core(fixture(), configs())["items"][2]["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["exec"]["command"]
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "asterisk"
            binary.write_text("#!/bin/sh\nprintf '%s\\n' \"$TEST_TRANSPORTS\"\n")
            binary.chmod(0o700)
            env = dict(os.environ, PATH=directory + os.pathsep + os.environ["PATH"])
            for output, success in (("Transport: header only", False),
                                    ("Transport: transport-udp udp 0 0 192.168.50.58:5060", False),
                                    ("Transport: transport-udp udp 0 0 192.168.50.58:5060\nTransport: transport-tcp tcp 0 0 192.168.50.58:5060", True)):
                env["TEST_TRANSPORTS"] = output
                result = subprocess.run(probe, env=env, capture_output=True)
                self.assertEqual(result.returncode == 0, success)

    def run_ha_probe(self, tcp_row, *, assist=True,
                     codec="codec_opus_open_source.so Opus transcoder 0 Running core",
                     endpoint="  Endpoint:  ha-assist  Not in use  0 of inf",
                     dialplan="  '630' => 1. Dial(PJSIP/ha-assist/sip:630@192.168.50.30:5060,60)"):
        site = dict(fixture(), home_assistant={"enabled": True, "assist": {"enabled": assist}})
        probe = render.render_core(site, configs())["items"][2]["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
        self.assertEqual(probe["timeoutSeconds"], 5)
        command = list(probe["exec"]["command"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "asterisk"
            binary.write_text("""#!/bin/sh
case "$2" in
  'pjsip show transports') printf '%s\\n' "$TEST_TRANSPORTS" ;;
  'module show like codec_opus_open_source') printf '%s\\n' "$TEST_CODEC" ;;
  'pjsip show endpoint ha-assist') printf '%s\\n' "$TEST_ENDPOINT" ;;
  'dialplan show 630@home') printf '%s\\n' "$TEST_DIALPLAN" ;;
  *) exit 98 ;;
esac
""")
            binary.chmod(0o700)
            tcp = root / "proc-net-tcp"
            tcp.write_text("  sl  local_address rem_address   st tx_queue rx_queue\n" + tcp_row + "\n")
            # Run the actual generated shell and real grep, substituting only
            # the read-only kernel data source so the test also runs on macOS.
            self.assertEqual(command[2].count("/proc/net/tcp"), 1)
            command[2] = command[2].replace("/proc/net/tcp", shlex.quote(str(tcp)))
            env = dict(os.environ, PATH=directory + os.pathsep + os.environ["PATH"],
                       TEST_TRANSPORTS="Transport: transport-udp udp 0 0 192.168.50.58:5060\nTransport: transport-tcp tcp 0 0 192.168.50.58:5060",
                       TEST_CODEC=codec, TEST_ENDPOINT=endpoint, TEST_DIALPLAN=dialplan)
            return subprocess.run(command, env=env, capture_output=True, text=True, timeout=5)

    def test_ha_readiness_requires_ami_listening_on_configured_local_address(self):
        # 3A32A8C0 is 192.168.50.58 in /proc/net/tcp. Both SIP transports work
        # throughout these cases, so only a genuine local LISTEN should pass.
        cases = (
            ("", False),
            (" 0: 3A32A8C0:13AE 00000000:0000 01 00000000:00000000", False),
            (" 0: 3A32A8C0:C001 1E32A8C0:13AE 01 00000000:00000000", False),
            (" 0: 3B32A8C0:13AE 00000000:0000 0A 00000000:00000000", False),
            (" 0: 3A32A8C0:13AE 00000000:0000 0A 00000000:00000000", True),
        )
        for row, expected in cases:
            with self.subTest(row=row):
                result = self.run_ha_probe(row, assist=False, codec="", endpoint="", dialplan="")
                self.assertEqual(result.returncode == 0, expected, result.stderr)

    def test_assist_readiness_requires_real_endpoint_route_and_running_codec(self):
        listening = " 0: 3A32A8C0:13AE 00000000:0000 0A 00000000:00000000"
        result = self.run_ha_probe(listening)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.run_ha_probe("").returncode, 0)
        for missing in ({"endpoint": "Unable to find object ha-assist."},
                        {"endpoint": "Endpoint: ha-assist-other Not in use"},
                        {"dialplan": "There is no existence of 630@home extension"},
                        {"dialplan": "'630' => 1. Hangup()"},
                        {"codec": "codec_opus_open_source.so Opus transcoder 0 Not Running"}):
            with self.subTest(missing=missing):
                result = self.run_ha_probe(listening, **missing)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_preflight_enforces_firmware_and_generated_profile_match(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory)
            (local / "site.json").write_text(json.dumps(fixture()))
            (local / "asterisk").mkdir()
            for name, text in configs().items():
                (local / "asterisk" / name).write_text(text)
            (local / "firmware").mkdir()
            load = "SIP45.test"
            files = {load + ".loads": b"synthetic manifest", "test.sbn": b"synthetic binary"}
            for name, content in files.items():
                (local / "firmware" / name).write_bytes(content)
            (local / "firmware-manifest.json").write_text(json.dumps({"model": "CP-7945G", "reviewed": True, "load_id": load,
                "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}))
            (local / "phones.json").write_text(json.dumps({"phones": [{"model":"CP-7945G", "verified":True, "mac":"020000000001", "firmware_load":load}]}))
            (local / "tftp").mkdir()
            xml = local / "tftp/SEP020000000001.cnf.xml"
            xml.write_text("<device><loadInformation>" + load + "</loadInformation></device>")
            cmd = ["python3", str(ROOT / "scripts/preflight.py"), "--site", str(local / "site.json"), "--with-tftp"]
            self.assertEqual(subprocess.run(cmd, capture_output=True).returncode, 0)
            xml.write_text("<device><loadInformation>SIP45.wrong</loadInformation></device>")
            self.assertNotEqual(subprocess.run(cmd, capture_output=True).returncode, 0)
            xml.write_text("<device><loadInformation>" + load + "</loadInformation></device>")
            (local / "firmware/test.sbn").write_bytes(b"tampered")
            self.assertNotEqual(subprocess.run(cmd, capture_output=True).returncode, 0)

    def test_default_deploy_cannot_contact_kubernetes_or_print_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory)
            (local / "site.json").write_text(json.dumps(fixture()))
            (local / "asterisk").mkdir()
            for name, text in configs().items():
                (local / "asterisk" / name).write_text(text + "; secret-marker-must-not-print\n")
            bin_dir = local / "bin"
            bin_dir.mkdir()
            kube = bin_dir / "kubectl"
            kube.write_text("#!/bin/sh\necho 'kubectl must not execute' >&2\nexit 98\n")
            kube.chmod(0o700)
            env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
            result = subprocess.run(["bash", str(ROOT / "scripts/deploy.sh"), "--site", str(local / "site.json")], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("secret-marker", result.stdout + result.stderr)
            self.assertNotIn("kubectl must not", result.stdout + result.stderr)
            rendered = local / "rendered/core.yaml"
            self.assertEqual(rendered.stat().st_mode & 0o777, 0o600)
            self.assertFalse((local / "rendered/tftp.yaml").exists())

    def run_delayed_tftp_deploy(self, failure=""):
        """Execute the real deployment control flow with a delayed Job controller."""
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory)
            scripts = local / "scripts"
            scripts.mkdir()
            (scripts / "deploy.sh").write_text((ROOT / "scripts/deploy.sh").read_text())
            # Config generation is covered separately. Isolate this test from all
            # cluster/network access while retaining the real shell control flow.
            for name in ("preflight.py", "render_k3s.py"):
                (scripts / name).write_text("pass\n")
            (local / "site.json").write_text(json.dumps(fixture()))
            bin_dir = local / "bin"
            bin_dir.mkdir()
            (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
            (bin_dir / "sleep").chmod(0o700)
            kube = bin_dir / "kubectl"
            kube.write_text(f"#!{sys.executable}\n" + '''import json, os, pathlib, sys
args = sys.argv[1:]
root = pathlib.Path(os.environ["TEST_ROOT"])
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\\n")
counter = root / "pod-queries"
count = int(counter.read_text()) if counter.exists() else 0
if "get" in args and "pod" in args:
    if os.environ.get("TEST_FAILURE") == "discovery":
        print("synthetic API error", file=sys.stderr)
        sys.exit(17)
    count += 1
    counter.write_text(str(count))
    if count >= 3:
        print("pod/cisco-tftp-test")
elif "wait" in args:
    if count < 3:
        print("error: no matching resources found", file=sys.stderr)
        sys.exit(44)
    if os.environ.get("TEST_FAILURE") == "readiness":
        print("synthetic readiness timeout", file=sys.stderr)
        sys.exit(23)
''')
            kube.chmod(0o700)
            env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
                       TEST_ROOT=str(local), TEST_FAILURE=failure)
            result = subprocess.run(["bash", str(scripts / "deploy.sh"), "--site", str(local / "site.json"),
                                     "--apply", "--firewall-reviewed", "--with-tftp"],
                                    env=env, capture_output=True, text=True, timeout=10)
            calls = [json.loads(line) for line in (local / "calls.jsonl").read_text().splitlines()]
            return result, calls

    def test_tftp_waits_for_pod_creation_before_existing_readiness_wait(self):
        result, calls = self.run_delayed_tftp_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        queries = [call for call in calls if "get" in call and "pod" in call]
        waits = [call for call in calls if "wait" in call]
        self.assertEqual(len(queries), 3)
        self.assertEqual(len(waits), 1)
        self.assertIn("--timeout=120s", waits[0])
        self.assertIn("--for=condition=Ready", waits[0])
        for call in queries + waits:
            self.assertEqual(call[call.index("--context") + 1], "test-context")
            self.assertEqual(call[call.index("-n") + 1], "telephony")
            self.assertEqual(call[call.index("-l") + 1], "app=cisco-tftp")
        for call in queries:
            self.assertTrue(any(call_arg.startswith("--request-timeout=") and
                                1 <= int(call_arg.split("=")[1][:-1]) <= 5 for call_arg in call))

    def test_tftp_discovery_and_readiness_errors_remain_failures(self):
        for failure, expected_code in (("discovery", 17), ("readiness", 23)):
            with self.subTest(failure=failure):
                result, calls = self.run_delayed_tftp_deploy(failure)
                self.assertEqual(result.returncode, expected_code, result.stderr)
                self.assertNotIn("TFTP active", result.stdout)
                if failure == "discovery":
                    self.assertFalse(any("wait" in call for call in calls))

if __name__ == "__main__":
    unittest.main()
