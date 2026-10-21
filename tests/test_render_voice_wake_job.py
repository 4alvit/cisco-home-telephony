"""Offline checks for wake Job privacy, shared ownership and inert delivery."""
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
from test_render_voice_job import site_fixture, gateway_fixture, voice_fixture

SPEC = importlib.util.spec_from_file_location("wake_job_test", ROOT / "scripts/render_voice_wake_job.py")
r = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(r)


class WakeJobTests(unittest.TestCase):
    def setUp(self):
        self.site = site_fixture()
        self.gateway = gateway_fixture()
        self.voice = voice_fixture()
        self.voice["wake"]["mode"] = "onnx"
        self.config = {"gateway_config": ".local/gateway.json", "line": "202",
                       "bind_ipv4": "192.168.50.10", "bind_port": 24576,
                       "phone_rtp_port": 24578, "duration_seconds": 30,
                       "receive_idle_seconds": 3, "volume": 20}
        self.bundle = {
            "format_version": 1, "frontend": "echomuse-oww-16k-v1", "threshold": 0.5,
            "models": {role: {"path": role + ".onnx",
                              "sha256": hashlib.sha256(role.encode()).hexdigest()}
                       for role in ("mel", "embedding", "classifier")},
        }
        self.kwargs = {"voice_image": "example.invalid/voice-wake@sha256:" + "b" * 64,
                       "models_claim": "private-wake-models",
                       "state_host_path": "/srv/gateway/state/voice-control", "display_port": 18110}

    def render(self, **kwargs):
        return r.render(self.site, self.config, self.gateway, self.voice,
                        "synthetic-ha-token", self.bundle, **{**self.kwargs, **kwargs})

    def objects(self):
        return self.render()["items"]

    def test_suspended_single_attempt_no_retry_no_service_token(self):
        secret, job = self.objects()
        self.assertEqual(secret["kind"], "Secret")
        spec = job["spec"]
        self.assertIs(spec["suspend"], True)
        self.assertEqual((spec["parallelism"], spec["completions"], spec["backoffLimit"]), (1, 1, 0))
        self.assertEqual(spec["activeDeadlineSeconds"], 120)
        self.assertEqual(spec["podReplacementPolicy"], "Failed")
        pod = spec["template"]["spec"]
        self.assertEqual(pod["restartPolicy"], "Never")
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["terminationGracePeriodSeconds"], 120)
        self.assertGreaterEqual(
            pod["terminationGracePeriodSeconds"],
            r.control.STOP_GRACE_SECONDS + r.control.REPORT_GRACE_SECONDS
            + r.control.UNCERTAIN_CLEANUP_SECONDS + 10)
        self.assertNotIn("fsGroup", pod["securityContext"])
        self.assertIn("synthetic-node", json.dumps(pod["affinity"]))

    def test_main_uses_reserved_runner_same_gateway_directory(self):
        _, job = self.objects()
        pod = job["spec"]["template"]["spec"]
        main = pod["containers"][0]
        self.assertEqual(main["command"][:2], ["python3", "/opt/voice-wake/run_reserved.py"])
        self.assertEqual(main["command"][main["command"].index("--state-directory") + 1], r.STATE)
        self.assertEqual(main["command"][main["command"].index("--attempt-id") + 1],
                         job["metadata"]["labels"]["wake-pilot-revision"])
        mounts = {v["name"]: v for v in main["volumeMounts"]}
        self.assertTrue(mounts["runtime"]["readOnly"])
        self.assertNotIn("models", mounts)
        self.assertEqual(mounts["state"]["mountPath"], r.STATE)
        volumes = {v["name"]: v for v in pod["volumes"]}
        self.assertEqual(volumes["state"]["hostPath"], {
            "path": "/srv/gateway/state/voice-control", "type": "Directory"})
        self.assertTrue(volumes["models"]["persistentVolumeClaim"]["readOnly"])
        self.assertEqual(pod["securityContext"]["runAsUser"], 10001)

    def test_root_init_cannot_access_host_models_or_credentials(self):
        _, job = self.objects()
        prepare, stage = job["spec"]["template"]["spec"]["initContainers"]
        self.assertEqual(prepare["securityContext"]["runAsUser"], 0)
        self.assertEqual(prepare["securityContext"]["capabilities"], {"drop": ["ALL"], "add": ["CHOWN"]})
        self.assertEqual([m["name"] for m in prepare["volumeMounts"]], ["runtime"])
        mounts = {v["name"]: v for v in stage["volumeMounts"]}
        self.assertTrue(mounts["models"]["readOnly"])
        self.assertTrue(mounts["inputs"]["readOnly"])
        self.assertNotIn("state", mounts)
        self.assertNotIn("runAsUser", stage["securityContext"])

    def test_private_inputs_minimized_and_all_paths_remapped(self):
        self.gateway["phones"][0]["label"] = "PRIVATE ROOM"
        self.gateway["api_token"] = "PRIVATE_API_TOKEN" * 3
        self.voice["wake"]["bundle_file"] = ".local/PRIVATE_USER/model.json"
        self.voice["ha"]["device_id"] = "synthetic-originating-cisco-device"
        secret, job = self.objects()
        files = {k: base64.b64decode(v).decode() for k, v in secret["data"].items()}
        voice = json.loads(files["voice.json"])
        self.assertEqual(voice["wake"]["mode"], "onnx")
        self.assertEqual(voice["wake"]["bundle_file"], "/run/voice/.local/models/bundle.json")
        self.assertEqual(json.loads(files["transport.json"])["line"], "202")
        self.assertEqual(voice["phones"], {"201": True})
        self.assertEqual(voice["ha"]["device_id"], "synthetic-originating-cisco-device")
        self.assertNotIn("PRIVATE_USER", json.dumps(files))
        self.assertNotIn("PRIVATE ROOM", json.dumps(files))
        self.assertNotIn("PRIVATE_API_TOKEN", json.dumps(files))
        self.assertNotIn("synthetic-ha-token", json.dumps(job))
        self.assertNotIn("synthetic-originating-cisco-device", json.dumps(job))
        self.assertNotIn(self.bundle["models"]["mel"]["sha256"], json.dumps(job))
        self.assertEqual(set(json.loads(files["staging.json"])["files"]), set(files) - {"staging.json"})

    def test_changes_to_models_image_claim_or_state_change_job_identity(self):
        original = self.render()["items"][-1]["metadata"]["name"]
        for key, value in (
            ("models_claim", "other-private-models"),
            ("state_host_path", "/srv/other/state/voice-control"),
            ("voice_image", "example.invalid/voice-wake@sha256:" + "c" * 64),
        ):
            self.assertNotEqual(original, self.render(**{key: value})["items"][-1]["metadata"]["name"])
        self.bundle["models"]["classifier"]["sha256"] = "e" * 64
        self.assertNotEqual(original, self.render()["items"][-1]["metadata"]["name"])

    def test_reject_unsafe_model_and_deployment_inputs(self):
        for key, values in {
            "voice_image": ["image:latest", "x@sha256:" + "g" * 64],
            "models_claim": ["", "a/../../b", "UPPER", "a..b"],
            "state_host_path": ["/", "/srv/voice-control", "/srv/../state/voice-control",
                                "/srv/state/other", "relative/voice-control"],
        }.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.render(**{key: value})
        self.bundle["models"]["mel"]["path"] = "../model.onnx"
        with self.assertRaises(ValueError):
            self.render()

    def test_finite_rearming_wake_session_preserves_models_and_fixed_stop_grace(self):
        names = []
        original_bundle = copy.deepcopy(self.bundle)
        self.voice["service"]["turn_timeout_seconds"] = 120
        for rearm in (False, True):
            for duration, expected in ((30, 120), (30.25, 121), (300, 390)):
                with self.subTest(rearm=rearm, duration=duration):
                    self.voice["service"]["rearm_after_turn"] = rearm
                    self.config["duration_seconds"] = duration
                    secret, job = self.objects()
                    spec = job["spec"]
                    self.assertEqual(spec["activeDeadlineSeconds"], expected)
                    self.assertEqual(spec["template"]["spec"]["terminationGracePeriodSeconds"], 120)
                    self.assertIs(spec["suspend"], True)
                    self.assertEqual(spec["backoffLimit"], 0)
                    values = {key: base64.b64decode(value) for key, value in secret["data"].items()}
                    voice = json.loads(values["voice.json"])
                    self.assertEqual(voice["wake"]["mode"], "onnx")
                    self.assertIs(voice["service"]["rearm_after_turn"], rearm)
                    self.assertEqual(voice["service"]["turn_timeout_seconds"], 120)
                    self.assertEqual(json.loads(values["transport.json"])["duration_seconds"], duration)
                    self.assertEqual(json.loads(values["bundle.json"]), original_bundle)
                    names.append(job["metadata"]["name"])
        self.assertEqual(self.bundle, original_bundle)
        self.assertEqual(len(set(names)), 6)

    def test_only_enabled_onnx_with_bounded_lease_can_render(self):
        for duration in (0, 301, True, float("inf"), float("nan")):
            self.config["duration_seconds"] = duration
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                self.render()
        self.config["duration_seconds"] = 30
        self.voice["service"]["enabled"] = False
        with self.assertRaises(ValueError):
            self.render()
        self.voice["service"]["enabled"] = True
        self.voice["wake"]["mode"] = "manual"
        with self.assertRaises(ValueError):
            self.render()

    def test_render_is_deterministic_and_does_not_change_inputs(self):
        before = copy.deepcopy((self.site, self.config, self.gateway, self.voice, self.bundle))
        self.assertEqual(self.render(), self.render())
        self.assertEqual(before, (self.site, self.config, self.gateway, self.voice, self.bundle))

    def test_private_loader_rejects_mode_links_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            private = root / ".local"
            private.mkdir()
            path = private / "input.json"
            path.write_text('{"v":1,"v":2}')
            path.chmod(0o600)
            with self.assertRaises(ValueError):
                json.loads(r.read_private(path, root), object_pairs_hook=r.unique_object)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                r.read_private(path, root)
            path.chmod(0o600)
            link = private / "link.json"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                r.read_private(link, root)


if __name__ == "__main__":
    unittest.main()
