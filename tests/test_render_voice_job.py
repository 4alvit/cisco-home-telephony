"""Offline safety checks for the suspended one-shot voice Kubernetes Job."""
import base64
import copy
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("render_voice_job_test", ROOT / "scripts/render_voice_job.py")
r = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = r
SPEC.loader.exec_module(r)


def site_fixture():
    return {"kube_context": "synthetic-context", "namespace": "synthetic-voice",
            "node_name": "synthetic-node", "node_ipv4": "192.168.50.10",
            "lan_cidr": "192.168.50.0/24",
            "asterisk_image": "example.invalid/asterisk@sha256:" + "a" * 64,
            "gateway": {"enabled": True, "ami_client_ipv4": "192.168.50.10"}}


def gateway_fixture():
    return {"persistent_enabled": True, "bind_ipv4": "192.168.50.10", "api_port": 18084,
            "api_token": "a" * 48, "api_clients": ["192.168.50.11"],
            "ami": {"host": "192.168.50.10", "port": 5038,
                    "username": "synthetic-gateway", "secret": "synthetic-ami-secret"},
            "dial_allowlist": ["600", "630"], "ring_allowlist": ["640"],
            "phones": [{"id": "201", "lines": ["201", "202"], "ipv4": "192.168.50.20",
                        "device": "SEP020000000001", "port": 18080,
                        "auth_path": "/auth/" + "c" * 64,
                        "username": "u" * 24, "password": "p" * 24}]}


def voice_fixture():
    return {"service": {"enabled": True, "rearm_after_turn": False},
            "phones": {"201": True},
            "ha": {"base_url": "http://192.168.50.30:8123", "token_file": ".local/ha-token.txt",
                   "pipeline_id": "synthetic-pipeline"},
            "wake": {"mode": "manual", "bundle_file": ".local/unused-bundle.json"}}


def wheel_bytes(*, version="15.0.1", pure="true", tag="py3-none-any", extra=None):
    buffer = io.BytesIO()
    metadata = "websockets-" + version + ".dist-info/"
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("websockets/__init__.py", "__version__ = " + repr(version) + "\n")
        archive.writestr("websockets/asyncio/client.py", "# Synthetic wheel; never execute during render.\n")
        archive.writestr(metadata + "METADATA", "Metadata-Version: 2.1\nName: websockets\nVersion: " + version + "\n")
        archive.writestr(metadata + "WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: " + pure + "\nTag: " + tag + "\n")
        for name, value in (extra or {}).items():
            archive.writestr(name, value)
    return buffer.getvalue()


class RenderVoiceJobTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name).resolve()
        self.local = self.workspace / ".local"
        self.local.mkdir()
        self.site = site_fixture()
        self.gateway = gateway_fixture()
        self.voice = voice_fixture()
        self.transport = {"gateway_config": str(self.local / "gateway.json"), "line": "201",
                          "bind_ipv4": "192.168.50.10", "bind_port": 24576,
                          "phone_rtp_port": 24578, "duration_seconds": 10,
                          "receive_idle_seconds": 3, "volume": 20, "expected_source_port": None}
        self.write_inputs()

    def private_write(self, name, data):
        path = self.local / name
        path.write_text(data)
        path.chmod(0o600)
        return path

    def write_inputs(self):
        for name, data in (("site", self.site), ("gateway", self.gateway),
                           ("transport", self.transport), ("voice", self.voice)):
            self.private_write(name + ".json", json.dumps(data))
        self.private_write("ha-token.txt", "synthetic-ha-token\n")

    def render(self, **kwargs):
        return r.render(self.site, self.transport, self.gateway, self.voice,
                        "synthetic-ha-token", **kwargs)

    def objects(self, **kwargs):
        result = self.render(**kwargs)
        self.assertEqual(result["kind"], "List")
        self.assertEqual([item["kind"] for item in result["items"]], ["ConfigMap", "Secret", "Job"])
        return result["items"]

    def secrets(self, secret):
        return {name: base64.b64decode(value).decode() for name, value in secret["data"].items()}

    def test_job_is_suspended_one_shot_with_finite_lifetime_and_exact_node(self):
        _source, _secret, job = self.objects()
        self.assertEqual(job["apiVersion"], "batch/v1")
        self.assertEqual(job["metadata"]["namespace"], self.site["namespace"])
        spec = job["spec"]
        self.assertIs(spec["suspend"], True)
        self.assertEqual(spec.get("completions", 1), 1)
        self.assertEqual(spec.get("parallelism", 1), 1)
        self.assertEqual(spec["backoffLimit"], 0)
        self.assertEqual(spec["activeDeadlineSeconds"], 90)
        self.assertEqual(spec["ttlSecondsAfterFinished"], 300)
        pod = spec["template"]["spec"]
        self.assertEqual(pod["restartPolicy"], "Never")
        self.assertIs(pod["hostNetwork"], True)
        self.assertIs(pod["automountServiceAccountToken"], False)
        terms = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
        self.assertEqual(terms, [{"matchFields": [{"key": "metadata.name", "operator": "In",
                                                  "values": [self.site["node_name"]]}]}])

    def test_nonroot_readonly_container_has_no_host_mounts_or_capabilities(self):
        _source, _secret, job = self.objects()
        pod = job["spec"]["template"]["spec"]
        security = pod["securityContext"]
        self.assertIs(security["runAsNonRoot"], True)
        self.assertEqual(security["runAsUser"], 10001)
        self.assertEqual(security["runAsGroup"], 10001)
        self.assertEqual(security["seccompProfile"], {"type": "RuntimeDefault"})
        self.assertFalse(pod.get("hostPID", False))
        self.assertFalse(pod.get("hostIPC", False))
        self.assertEqual(len(pod["containers"]), 1)
        self.assertFalse(pod.get("initContainers"))
        container = pod["containers"][0]
        self.assertEqual(container["image"], self.site["asterisk_image"])
        self.assertIs(container["securityContext"]["allowPrivilegeEscalation"], False)
        self.assertIs(container["securityContext"]["readOnlyRootFilesystem"], True)
        self.assertEqual(container["securityContext"]["capabilities"], {"drop": ["ALL"]})
        for tier in ("requests", "limits"):
            self.assertTrue(container["resources"][tier]["cpu"])
            self.assertTrue(container["resources"][tier]["memory"])
        for volume in pod["volumes"]:
            self.assertNotIn("hostPath", volume)
        mounts = {mount["mountPath"]: mount for mount in container["volumeMounts"]}
        self.assertIs(mounts["/workspace"]["readOnly"], True)

    def test_runtime_image_defaults_to_site_image_when_omitted_or_none(self):
        default = self.render()
        explicit_none = self.render(voice_image=None)
        self.assertEqual(default, explicit_none)
        container = default["items"][-1]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["image"], self.site["asterisk_image"])

    def test_runtime_image_override_is_exact_immutable_and_changes_job_revision(self):
        before = copy.deepcopy((self.site, self.transport, self.gateway, self.voice))
        default = self.render()
        first_image = "example.invalid/voice-runtime@sha256:" + "b" * 64
        first = self.render(voice_image=first_image)
        container = first["items"][-1]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["image"], first_image)
        self.assertNotEqual(default["items"][-1]["metadata"]["name"], first["items"][-1]["metadata"]["name"])
        second_image = "example.invalid/voice-runtime@sha256:" + "c" * 64
        second = self.render(voice_image=second_image)
        self.assertNotEqual(first["items"][-1]["metadata"]["name"], second["items"][-1]["metadata"]["name"])
        self.assertEqual(before, (self.site, self.transport, self.gateway, self.voice))
        self.assertEqual(first, self.render(voice_image=first_image))

    def test_runtime_image_override_rejects_mutable_malformed_and_nonlowercase_digests(self):
        invalid = ("", "example.invalid/voice-runtime:latest", "image@sha256:" + "a" * 63,
                   "image@sha256:" + "a" * 65, "image@sha256:" + "A" * 64,
                   "image@sha256:" + "g" * 64, "image@SHA256:" + "a" * 64,
                   "image with space@sha256:" + "a" * 64,
                   "image@sha256:" + "a" * 64 + "\n", True, 42)
        for image in invalid:
            with self.subTest(image=image), self.assertRaises(ValueError):
                self.render(voice_image=image)

    def test_valid_runtime_override_does_not_bypass_site_validation(self):
        image = "example.invalid/voice-runtime@sha256:" + "b" * 64
        original = copy.deepcopy(self.site)
        for changes in ({"asterisk_image": "example.invalid/asterisk:latest"},
                        {"namespace": "default"}, {"node_ipv4": "8.8.8.8"}):
            self.site = dict(original, **changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.render(voice_image=image)
        self.site = original

    def test_job_uses_explicit_bounded_session_command_without_install_or_shell(self):
        _source, _secret, job = self.objects()
        container = job["spec"]["template"]["spec"]["containers"][0]
        command = container.get("command", []) + container.get("args", [])
        self.assertIn("/workspace/scripts/voice_session.py", command)
        for flag in ("--run", "--attended", "--exclusive-media", "--enable-microphone"):
            self.assertIn(flag, command)
        for flag, value in (("--workspace", "/run/voice"),
                            ("--transport-config", "/run/voice/.local/transport.json"),
                            ("--voice-config", "/run/voice/.local/voice.json")):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertNotIn("/bin/sh", command)
        self.assertNotIn("pip", command)
        self.assertNotIn("curl", command)

    def test_secrets_rewrite_private_paths_and_never_appear_in_pod_or_source(self):
        self.voice["ha"]["device_id"] = "synthetic-originating-cisco-device"
        source, secret, job = self.objects()
        files = self.secrets(secret)
        self.assertEqual(set(files), {"transport.json", "gateway.json", "voice.json", "ha-token.txt"})
        self.assertEqual(files["ha-token.txt"].strip(), "synthetic-ha-token")
        transport = json.loads(files["transport.json"])
        voice = json.loads(files["voice.json"])
        self.assertEqual(transport["gateway_config"], "/run/voice/.local/gateway.json")
        self.assertEqual(voice["ha"]["token_file"], ".local/ha-token.txt")
        self.assertEqual(voice["ha"]["device_id"], "synthetic-originating-cisco-device")
        self.assertEqual(voice["wake"]["bundle_file"], ".local/unused-manual-bundle.json")
        public = json.dumps([source, job])
        for marker in ("synthetic-ha-token", "synthetic-ami-secret", "u" * 24, "p" * 24,
                       str(self.workspace), "192.168.50.20", "synthetic-pipeline",
                       "synthetic-originating-cisco-device"):
            self.assertNotIn(marker, public)
        self.assertTrue(secret["immutable"])
        self.assertTrue(source["immutable"])

    def test_gateway_secret_contains_only_selected_phone_and_keeps_both_lines(self):
        extra = copy.deepcopy(self.gateway["phones"][0])
        extra.update(id="203", lines=["203", "204"], ipv4="192.168.50.21",
                     device="SEP020000000002", port=18081)
        self.gateway["phones"].append(extra)
        self.transport["line"] = "202"
        _source, secret, _job = self.objects()
        gateway = json.loads(self.secrets(secret)["gateway.json"])
        self.assertEqual(gateway["phones"], [self.gateway["phones"][0]])
        self.assertEqual(gateway["phones"][0]["lines"], ["201", "202"])
        self.assertEqual(len(self.gateway["phones"]), 2)

    def add_unused_private_metadata(self):
        self.gateway["api_token"] = "private-unused-api-token-" + "z" * 24
        self.gateway["applications"] = {"enabled": False}
        self.gateway["other_private"] = {"token": "unrelated-private-token"}
        self.gateway["ami"]["unused_metadata"] = "unrelated-ami-metadata"
        self.gateway["phones"][0].update(room="Synthetic Room", apps_path="/apps/" + "e" * 64,
                                          other_private="unrelated-phone-metadata")

    def test_gateway_secret_excludes_unused_credentials_room_and_application_metadata(self):
        self.add_unused_private_metadata()
        _source, secret, _job = self.objects()
        encoded = self.secrets(secret)["gateway.json"]
        gateway = json.loads(encoded)
        for marker in (self.gateway["api_token"], "unrelated-private-token", "unrelated-ami-metadata",
                       "unrelated-phone-metadata", "Synthetic Room", "/apps/" + "e" * 64):
            self.assertNotIn(marker, encoded)
        self.assertNotIn("applications", gateway)
        self.assertNotIn("other_private", gateway)
        self.assertEqual(set(gateway["ami"]), {"host", "port", "username", "secret"})
        self.assertEqual(set(gateway["phones"][0]),
                         {"id", "lines", "ipv4", "device", "port", "auth_path", "username", "password"})
        self.assertEqual(gateway["dial_allowlist"], [])
        self.assertEqual(gateway["ring_allowlist"], [])
        r.transport._gateway_module().validate(gateway)

    def test_render_is_deterministic_and_does_not_modify_input_dicts(self):
        before = copy.deepcopy((self.site, self.transport, self.gateway, self.voice))
        first = self.render()
        self.assertEqual(first, self.render())
        self.assertEqual(before, (self.site, self.transport, self.gateway, self.voice))
        self.gateway["ami"]["secret"] = "changed-synthetic-ami-secret"
        changed = self.render()
        self.assertNotEqual(first["items"][1]["metadata"]["name"], changed["items"][1]["metadata"]["name"])

    def test_mutable_image_bad_namespace_and_disabled_gateway_are_rejected(self):
        cases = ({"asterisk_image": "example.invalid/asterisk:latest"}, {"namespace": "default"},
                 {"gateway": {"enabled": False}},
                 {"gateway": {"enabled": True, "ami_client_ipv4": "192.168.50.11"}})
        original = self.site
        for changes in cases:
            self.site = dict(original, **changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.render()
        self.site = original

    def test_transport_gateway_and_ami_bind_must_match_selected_node(self):
        for mapping, key in ((self.transport, "bind_ipv4"), (self.gateway, "bind_ipv4"),
                             (self.gateway["ami"], "host")):
            old = mapping[key]
            mapping[key] = "192.168.50.11"
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.render()
            mapping[key] = old

    def test_only_enabled_manual_one_phone_without_rearm_can_render(self):
        cases = (("service", "enabled", False), ("service", "rearm_after_turn", True),
                 ("wake", "mode", "onnx"), ("service", "max_utterance_seconds", 61),
                 ("service", "turn_timeout_seconds", 181))
        original = copy.deepcopy(self.voice)
        for section, key, value in cases:
            self.voice = copy.deepcopy(original)
            self.voice[section][key] = value
            with self.subTest(section=section, key=key, value=value), self.assertRaises(ValueError):
                self.render()
        for phones in ({"201": False}, {"203": True}, {"201": True, "203": False},
                       {"201": True, "203": True}):
            self.voice = dict(original, phones=phones)
            with self.subTest(phones=phones), self.assertRaises(ValueError):
                self.render()
        self.voice = original

    def test_unknown_line_and_excess_duration_are_rejected(self):
        for key, value in (("line", "299"), ("duration_seconds", 301)):
            old = self.transport[key]
            self.transport[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.render()
            self.transport[key] = old

    def test_long_manual_lease_changes_deadline_without_extending_stop_grace(self):
        names = []
        for duration, expected in ((30, 90), (30.25, 91), (300, 360)):
            with self.subTest(duration=duration):
                self.transport["duration_seconds"] = duration
                _source, secret, job = self.objects()
                spec = job["spec"]
                self.assertEqual(spec["activeDeadlineSeconds"], expected)
                self.assertEqual(spec["template"]["spec"]["terminationGracePeriodSeconds"], 45)
                self.assertIs(spec["suspend"], True)
                self.assertEqual(spec["backoffLimit"], 0)
                private = self.secrets(secret)
                self.assertEqual(json.loads(private["transport.json"])["duration_seconds"], duration)
                voice = json.loads(private["voice.json"])
                self.assertEqual(voice["wake"]["mode"], "manual")
                self.assertIs(voice["service"]["rearm_after_turn"], False)
                names.append(job["metadata"]["name"])
        self.assertEqual(len(set(names)), 3)

    def test_service_time_budgets_are_preserved_within_the_finite_session(self):
        self.transport["duration_seconds"] = 300
        self.voice["service"].update(max_utterance_seconds=60, turn_timeout_seconds=120)
        _source, secret, job = self.objects()
        voice = json.loads(self.secrets(secret)["voice.json"])
        self.assertEqual(voice["service"]["max_utterance_seconds"], 60)
        self.assertEqual(voice["service"]["turn_timeout_seconds"], 120)
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 360)

    def test_private_output_is_exclusive_mode_0600_and_rejects_symlink(self):
        manifest = self.render()
        output = self.local / "job.json"
        r.write_private_manifest(manifest, output, workspace=self.workspace)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(output.read_text()), manifest)
        saved = output.read_bytes()
        with self.assertRaises((FileExistsError, ValueError)):
            r.write_private_manifest(manifest, output, workspace=self.workspace)
        self.assertEqual(output.read_bytes(), saved)
        link = self.local / "link.json"
        link.symlink_to(output)
        with self.assertRaises((OSError, ValueError)):
            r.write_private_manifest(manifest, link, workspace=self.workspace)
        self.assertEqual(output.read_bytes(), saved)

    def test_output_cannot_escape_workspace_private_directory(self):
        manifest = self.render()
        (self.local / "escape").symlink_to(self.workspace)
        for path in (self.workspace / "public.json", self.local / "../public.json",
                     self.local / "escape/job.json"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                r.write_private_manifest(manifest, path, workspace=self.workspace)
        self.assertFalse((self.workspace / "public.json").exists())

    def test_display_port_is_explicit_bounded_and_never_a_freeform_argument(self):
        _source, _secret, job = self.objects(display_port=18086)
        container = job["spec"]["template"]["spec"]["containers"][0]
        command = container.get("command", []) + container.get("args", [])
        self.assertEqual(command[command.index("--display-port") + 1], "18086")
        for port in (True, "18086", 1023, 65536):
            with self.subTest(port=port), self.assertRaises(ValueError):
                self.render(display_port=port)

    def test_projected_source_and_separate_private_mount_use_only_reviewed_files(self):
        source, secret, job = self.objects()
        volumes = job["spec"]["template"]["spec"]["volumes"]
        projected = next(volume["projected"] for volume in volumes if "projected" in volume)
        code_source = next(item["configMap"] for item in projected["sources"] if "configMap" in item)
        self.assertEqual(len(projected["sources"]), 1)
        private_volume = next(volume for volume in volumes if "secret" in volume)
        secret_source = private_volume["secret"]
        self.assertEqual(code_source["name"], source["metadata"]["name"])
        self.assertEqual(secret_source["secretName"], secret["metadata"]["name"])
        self.assertEqual({item["path"] for item in code_source["items"]}, set(r.SOURCE_FILES))
        for item in code_source["items"]:
            self.assertTrue(item["path"].endswith(".py"))
            self.assertNotIn("..", Path(item["path"]).parts)
            self.assertNotIn(".local", Path(item["path"]).parts)
            self.assertEqual(source["data"][item["key"]], (ROOT / item["path"]).read_text())
        self.assertEqual({item["path"] for item in secret_source["items"]},
                         {"transport.json", "gateway.json", "voice.json", "ha-token.txt"})
        mounts = job["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
        private_mount = next(mount for mount in mounts if mount["name"] == private_volume["name"])
        self.assertEqual(private_mount["mountPath"], "/run/voice/.local")
        self.assertTrue(private_mount["readOnly"])
        self.assertNotIn("subPath", private_mount)

    def test_source_snapshot_ignores_unlisted_files_and_changes_revision_with_reviewed_code(self):
        source_root = self.workspace / "source"
        for relative in r.SOURCE_FILES:
            target = source_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# synthetic reviewed source\n")
        (source_root / ".local").mkdir()
        (source_root / ".local/private-token.txt").write_text("must-never-enter-configmap")
        (source_root / "unlisted.py").write_text("must-never-enter-configmap")
        before = self.render(source_root=source_root)
        encoded = json.dumps(before["items"][0])
        self.assertNotIn("must-never-enter-configmap", encoded)
        selected = source_root / next(iter(r.SOURCE_FILES))
        selected.write_text("# updated synthetic reviewed source\n")
        after = self.render(source_root=source_root)
        self.assertNotEqual(before["items"][0]["metadata"]["name"], after["items"][0]["metadata"]["name"])

    def make_sources(self, content="# synthetic reviewed source\n"):
        root = self.workspace / "source"
        for relative in r.SOURCE_FILES:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return root

    def test_source_symlink_and_invalid_python_are_rejected_without_execution(self):
        source_root = self.make_sources()
        target = source_root / r.SOURCE_FILES[0]
        target.unlink()
        target.symlink_to(self.local / "ha-token.txt")
        with self.assertRaises((OSError, ValueError)):
            self.render(source_root=source_root)
        target.unlink()
        target.write_text("raise RuntimeError('render must not execute source')\n")
        self.render(source_root=source_root)
        target.write_text("def invalid syntax\n")
        with self.assertRaises(SyntaxError):
            self.render(source_root=source_root)

    def test_configmap_bound_counts_json_escaping_not_just_raw_source_bytes(self):
        source_root = self.make_sources("#" + "\\" * 30000 + "\n")
        raw_size = sum((source_root / path).stat().st_size for path in r.SOURCE_FILES)
        self.assertLess(raw_size, r.MAX_CONFIGMAP_BYTES)
        with self.assertRaisesRegex(ValueError, "source_configmap_too_large"):
            self.render(source_root=source_root)

    def write_wheel(self, data):
        path = self.local / r.WHEEL_NAME
        path.write_bytes(data)
        return path

    def test_optional_pinned_pure_wheel_is_projected_for_zipimport(self):
        data = wheel_bytes()
        path = self.write_wheel(data)
        with mock.patch.object(r, "WHEEL_SHA256", hashlib.sha256(data).hexdigest()):
            source, _secret, job = self.objects(websockets_wheel=path)
        self.assertEqual(base64.b64decode(source["binaryData"][r.WHEEL_NAME]), data)
        pod = job["spec"]["template"]["spec"]
        environment = {item["name"]: item["value"] for item in pod["containers"][0]["env"]}
        self.assertEqual(environment["PYTHONPATH"].split(":")[0], "/workspace/.deps/" + r.WHEEL_NAME)
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
        projected = next(volume["projected"] for volume in pod["volumes"] if "projected" in volume)
        configmap = next(item["configMap"] for item in projected["sources"] if "configMap" in item)
        self.assertIn({"key": r.WHEEL_NAME, "path": ".deps/" + r.WHEEL_NAME}, configmap["items"])

    def test_wheel_checksum_name_and_pure_metadata_are_required(self):
        path = self.write_wheel(wheel_bytes())
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.render(websockets_wheel=path)
        renamed = path.with_name("arbitrary-package.whl")
        path.rename(renamed)
        with self.assertRaisesRegex(ValueError, "unsupported_websockets_wheel"):
            self.render(websockets_wheel=renamed)
        for changes in ({"version": "15.0.2"}, {"pure": "false"}, {"tag": "cp312-cp312-linux_x86_64"}):
            data = wheel_bytes(**changes)
            path = self.write_wheel(data)
            with self.subTest(changes=changes), \
                 mock.patch.object(r, "WHEEL_SHA256", hashlib.sha256(data).hexdigest()), \
                 self.assertRaises((ValueError, KeyError)):
                self.render(websockets_wheel=path)

    def test_wheel_traversal_native_and_path_hook_members_are_rejected(self):
        for name in ("../escape.py", "/absolute.py", "websockets/native.so", "autoload.pth",
                     "C:/absolute.py", "websockets\\escape.py"):
            data = wheel_bytes(extra={name: b"synthetic content"})
            path = self.write_wheel(data)
            with self.subTest(name=name), \
                 mock.patch.object(r, "WHEEL_SHA256", hashlib.sha256(data).hexdigest()), \
                 self.assertRaisesRegex(ValueError, "unsafe_wheel_member"):
                self.render(websockets_wheel=path)

    def test_compressed_wheel_still_has_bounded_expanded_contents(self):
        data = wheel_bytes(extra={"websockets/large.txt": b"x" * (2 * 1024 * 1024 + 1)})
        self.assertLess(len(data), r.MAX_WHEEL_BYTES)
        path = self.write_wheel(data)
        with mock.patch.object(r, "WHEEL_SHA256", hashlib.sha256(data).hexdigest()), \
             self.assertRaisesRegex(ValueError, "oversized_wheel_contents"):
            self.render(websockets_wheel=path)

    @unittest.skipUnless(os.environ.get("VOICE_TEST_WEBSOCKETS_WHEEL"), "optional reviewed local wheel not supplied")
    def test_optional_real_pinned_wheel_imports_directly_without_installation(self):
        wheel = Path(os.environ["VOICE_TEST_WEBSOCKETS_WHEEL"]).resolve()
        r.read_websockets_wheel(wheel)
        code = ("import websockets; import websockets.asyncio.client; "
                "assert websockets.__version__ == '15.0.1'; "
                "assert '.whl' in websockets.__file__")
        result = subprocess.run([sys.executable, "-S", "-c", code],
                                env=dict(os.environ, PYTHONPATH=str(wheel)), cwd=self.workspace,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def main(self, **overrides):
        values = {"site": ".local/site.json", "transport-config": ".local/transport.json",
                  "voice-config": ".local/voice.json", "workspace": str(self.workspace),
                  "output": ".local/cli-job.json"}
        values.update(overrides)
        args = [part for name, value in values.items() for part in ("--" + name, value)]
        with contextlib.redirect_stdout(io.StringIO()) as stdout, \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = r.main(args)
        return result, stdout.getvalue() + stderr.getvalue()

    def test_cli_renders_without_network_or_cluster_calls_and_never_prints_private_data(self):
        for relative in r.SOURCE_FILES:
            destination = self.workspace / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text((ROOT / relative).read_text())
        with mock.patch("socket.socket", side_effect=AssertionError("offline renderer opened a socket")), \
             mock.patch("subprocess.run", side_effect=AssertionError("offline renderer ran a command")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("offline renderer spawned a command")):
            result, output = self.main()
        self.assertEqual(result, 0, output)
        manifest = json.loads((self.local / "cli-job.json").read_text())
        self.assertTrue(manifest["items"][-1]["spec"]["suspend"])
        self.assertEqual((self.local / "cli-job.json").stat().st_mode & 0o777, 0o600)
        for marker in ("synthetic-ha-token", "synthetic-ami-secret", "192.168.50.", str(self.workspace)):
            self.assertNotIn(marker, output)
        original = (self.local / "cli-job.json").read_bytes()
        result, output = self.main()
        self.assertEqual(result, 2)
        self.assertEqual((self.local / "cli-job.json").read_bytes(), original)

    def test_cli_runtime_image_override_preserves_inputs_and_rejects_mutable_image(self):
        for relative in r.SOURCE_FILES:
            destination = self.workspace / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text((ROOT / relative).read_text())
        original_site = (self.local / "site.json").read_bytes()
        image = "example.invalid/voice-runtime@sha256:" + "b" * 64
        result, output = self.main(**{"voice-image": image})
        self.assertEqual(result, 0, output)
        manifest = json.loads((self.local / "cli-job.json").read_text())
        container = manifest["items"][-1]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["image"], image)
        self.assertEqual((self.local / "site.json").read_bytes(), original_site)
        result, output = self.main(**{"voice-image": "example.invalid/voice-runtime:latest",
                                      "output": ".local/rejected-job.json"})
        self.assertEqual(result, 2)
        self.assertFalse((self.local / "rejected-job.json").exists())
        self.assertEqual((self.local / "site.json").read_bytes(), original_site)

    def test_cli_private_path_escapes_fail_without_output_or_secret_disclosure(self):
        public = self.workspace / "public.json"
        public.write_text(json.dumps(self.site))
        result, output = self.main(site=str(public))
        self.assertEqual(result, 2)
        self.assertFalse((self.local / "cli-job.json").exists())
        self.assertNotIn(str(public), output)
        self.voice["ha"]["token_file"] = "public-token.txt"
        self.write_inputs()
        (self.workspace / "public-token.txt").write_text("outside-secret-must-not-print")
        result, output = self.main()
        self.assertEqual(result, 2)
        self.assertNotIn("outside-secret", output)
        self.assertFalse((self.local / "cli-job.json").exists())

    def atomic_volume(self, mount, files):
        """Model Kubernetes AtomicWriter: real mount root, linked top-level entries."""
        version = mount / "..synthetic-version"
        version.mkdir(parents=True)
        for name, content in files.items():
            target = version / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (mount / "..data").symlink_to(version.name, target_is_directory=True)
        for name in sorted({Path(name).parts[0] for name in files}):
            (mount / name).symlink_to(Path("..data") / name,
                                     target_is_directory=(version / name).is_dir())

    def test_merged_atomic_writer_private_directory_is_rejected_by_runtime_guard(self):
        from voice_assistant.config import build_service
        merged = self.workspace / "merged-volume"
        self.atomic_volume(merged, {".local/ha-token.txt": b"synthetic-ha-token\n"})
        self.assertTrue((merged / ".local").is_symlink())
        with self.assertRaisesRegex(ValueError, "workspace .local must not be a symlink"):
            build_service(self.voice, merged, object())

    def test_atomic_writer_source_and_private_mount_build_service_with_network_blocked(self):
        self.add_unused_private_metadata()
        source, secret, job = self.objects()
        source_root = self.workspace / "projected-source"
        runtime_root = self.workspace / "runtime"
        volumes = job["spec"]["template"]["spec"]["volumes"]
        projected = next(volume["projected"] for volume in volumes if "projected" in volume)
        private = next(volume["secret"] for volume in volumes if "secret" in volume)
        self.assertEqual(projected["defaultMode"], 0o440)
        self.assertEqual(private["defaultMode"], 0o440)
        self.assertEqual(job["spec"]["template"]["spec"]["securityContext"]["fsGroup"], 10001)
        source_files = {}
        for projection in projected["sources"]:
            self.assertEqual(set(projection), {"configMap"})
            projection = projection["configMap"]
            files = {key: value.encode() for key, value in source["data"].items()}
            files.update({key: base64.b64decode(value) for key, value in source.get("binaryData", {}).items()})
            for item in projection["items"]:
                source_files[item["path"]] = files[item["key"]]
        self.atomic_volume(source_root, source_files)
        secret_files = {item["path"]: base64.b64decode(secret["data"][item["key"]])
                        for item in private["items"]}
        self.atomic_volume(runtime_root / ".local", secret_files)
        self.assertTrue((source_root / "scripts").is_symlink())
        self.assertFalse((runtime_root / ".local").is_symlink())
        self.assertTrue((runtime_root / ".local/ha-token.txt").is_symlink())
        transport_path = runtime_root / ".local/transport.json"
        transport = json.loads(transport_path.read_text())
        transport["gateway_config"] = str(runtime_root / ".local/gateway.json")
        transport_path.write_text(json.dumps(transport))

        guard = self.workspace / "network-guard"
        guard.mkdir()
        (guard / "sitecustomize.py").write_text(
            "import socket\n"
            "def blocked(*args, **kwargs):\n"
            "    raise AssertionError('network forbidden in packaged validation test')\n"
            "socket.create_connection = blocked\n"
            "socket.socket.connect = blocked\n"
            "socket.socket.connect_ex = blocked\n"
            "socket.socket.bind = blocked\n"
            "socket.socket.sendto = blocked\n"
        )
        environment = dict(os.environ, PYTHONPATH=os.pathsep.join((str(guard), str(source_root))),
                           PYTHONNOUSERSITE="1")
        command = [sys.executable, "-B", str(source_root / "scripts/voice_session.py"),
                   "--transport-config", str(transport_path),
                   "--voice-config", str(runtime_root / ".local/voice.json"),
                   "--workspace", str(runtime_root)]
        result = subprocess.run(command, cwd=source_root, env=environment,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {"valid": True, "network_actions": 0, "microphone_started": False})
        self.assertNotIn("synthetic-ha-token", result.stdout + result.stderr)
        build = ("from pathlib import Path; import sys; "
                 "from voice_assistant.config import load_config, build_service, private_path; "
                 "root = Path(sys.argv[1]); "
                 "config = load_config(root / '.local/voice.json'); "
                 "assert not (root / '.local').is_symlink(); "
                 "assert (root / '.local/ha-token.txt').is_symlink(); "
                 "assert private_path(config['ha']['token_file'], root).is_relative_to(root / '.local'); "
                 "service = build_service(config, root, object()); "
                 "service.ensure_allowed('201'); print('service-built')")
        result = subprocess.run([sys.executable, "-B", "-c", build, str(runtime_root)],
                                cwd=source_root, env=environment,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "service-built")


if __name__ == "__main__":
    unittest.main()
