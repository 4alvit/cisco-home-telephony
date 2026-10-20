"""Offline safety and compatibility checks for the optional wake image."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load("wake_image_builder", "containers/voice-wake/build.py")
smoke = load("wake_image_smoke", "containers/voice-wake/smoke.py")
BASE = "registry.example/voice@sha256:" + "a" * 64
IMAGE = "sha256:" + "b" * 64


class WakeImageBuildTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.manifest = {"version": 1, "base_source_commit": "c" * 40,
                         "sha256": {"/workspace/synthetic.py": "d" * 64}}
        for name in builder.CONTEXT_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Synthetic public source: " + name + "\n")
        (self.root / ".local").mkdir()
        (self.root / ".local/private.onnx").write_bytes(b"private-model-canary")
        (self.root / "public-unlisted.py").write_text("unlisted-canary")
        self.args = SimpleNamespace(base_image=BASE, base_source_ref="c" * 40,
                                    tag="registry.example/voice-wake:pilot",
                                    platform="linux/amd64")
        self.calls = []
        self.captured = None
        self.context = None

    def fake_command(self, args, **_kwargs):
        self.calls.append(args)
        if args[:2] == ["docker", "build"]:
            self.context = Path(args[-1])
            self.captured = {
                p.relative_to(self.context).as_posix(): p.read_bytes()
                for p in self.context.rglob("*") if p.is_file()
            }
        return SimpleNamespace(stdout=("linux/amd64" if args[-1] ==
                                       "{{.Os}}/{{.Architecture}}" else IMAGE) + "\n")

    def build(self):
        with patch.object(builder, "base_manifest", return_value=self.manifest), \
                patch.object(builder, "run", side_effect=self.fake_command):
            return builder.build(self.args, root=self.root)

    def test_context_contains_only_allowlisted_public_files_and_manifest(self):
        result = self.build()
        self.assertEqual(set(self.captured), set(builder.CONTEXT_FILES) | {"base-source.json"})
        self.assertFalse(any(b"canary" in value for value in self.captured.values()))
        self.assertEqual(json.loads(self.captured["base-source.json"]), self.manifest)
        self.assertFalse(self.context.exists())
        self.assertEqual(result["image_id"], IMAGE)
        self.assertFalse(result["private_models_included"])
        self.assertFalse(result["pushed"])
        self.assertEqual(set(result["context_sha256"]), set(builder.CONTEXT_FILES))

    def test_smokes_use_built_image_id_without_network_or_writable_root(self):
        self.build()
        builds = [a for a in self.calls if a[:2] == ["docker", "build"]]
        self.assertEqual(len(builds), 1)
        self.assertIn("VOICE_IMAGE=" + BASE, builds[0])
        runs = [a for a in self.calls if a[:2] == ["docker", "run"]]
        self.assertEqual(len(runs), 2)
        for command in runs:
            self.assertEqual(command[command.index("--network") + 1], "none")
            self.assertIn("--read-only", command)
            self.assertIn("ALL", command)
            self.assertEqual(command[command.index("--user") + 1], "10001:10001")
            self.assertEqual(command[-2], IMAGE)
            self.assertNotIn(self.args.tag, command)
        self.assertEqual([a[-1] for a in runs],
                         ["/opt/voice/smoke.py", "/opt/voice-wake/smoke.py"])
        self.assertFalse(any(a[1] in ("push", "login") for a in self.calls))

    def test_local_image_id_is_checked_tagged_without_pull_and_alias_removed(self):
        self.args.base_image = None
        self.args.base_image_id = IMAGE
        result = self.build()
        tag = next(a for a in self.calls if a[:2] == ["docker", "tag"])
        alias = tag[-1]
        self.assertEqual(tag[2], IMAGE)
        self.assertTrue(alias.startswith("cisco-voice-wake-base:"))
        checks = [a for a in self.calls if a[:3] == ["docker", "image", "inspect"]
                  and a[3] == alias]
        self.assertEqual(len(checks), 2, "Alias must be verified before and after building")
        self.assertIn(["docker", "image", "rm", "--no-prune", alias], self.calls)
        self.assertFalse(any(a[:2] == ["docker", "pull"] for a in self.calls))
        self.assertIn("VOICE_IMAGE=" + alias, next(a for a in self.calls
                                                  if a[:2] == ["docker", "build"]))
        self.assertEqual(result["base_image_id"], IMAGE)
        self.assertIsNone(result["base_image"])

    def test_platform_mismatch_stops_before_build_for_local_and_registry_bases(self):
        for local in (False, True):
            self.calls.clear()
            self.args.base_image = None if local else BASE
            self.args.base_image_id = IMAGE if local else None
            def wrong_platform(args, **kwargs):
                result = self.fake_command(args, **kwargs)
                if args[-1] == "{{.Os}}/{{.Architecture}}":
                    result.stdout = "linux/arm64\n"
                return result
            with patch.object(builder, "base_manifest", return_value=self.manifest), \
                    patch.object(builder, "run", side_effect=wrong_platform):
                with self.subTest(local=local), self.assertRaisesRegex(ValueError, "platform"):
                    builder.build(self.args, root=self.root)
            self.assertFalse(any(a[:2] == ["docker", "build"] for a in self.calls))

    def test_local_alias_changed_after_build_fails_and_removes_alias(self):
        self.args.base_image = None
        self.args.base_image_id = IMAGE
        alias_checks = 0
        def changed_alias(args, **kwargs):
            nonlocal alias_checks
            result = self.fake_command(args, **kwargs)
            if (args[:3] == ["docker", "image", "inspect"]
                    and args[3].startswith("cisco-voice-wake-base:")):
                alias_checks += 1
                if alias_checks == 2:
                    result.stdout = "sha256:" + "e" * 64 + "\n"
            return result
        with patch.object(builder, "base_manifest", return_value=self.manifest), \
                patch.object(builder, "run", side_effect=changed_alias):
            with self.assertRaisesRegex(ValueError, "ID changed"):
                builder.build(self.args, root=self.root)
        alias = next(a[-1] for a in self.calls if a[:2] == ["docker", "tag"])
        self.assertIn(["docker", "image", "rm", "--no-prune", alias], self.calls)
        self.assertFalse(any(a[:2] == ["docker", "run"] for a in self.calls))

    def test_rejects_mutable_base_bad_platform_and_option_tags_before_commands(self):
        for field, value in (("base_image", "registry.example/voice:latest"),
                             ("base_image", "--pull"), ("platform", "linux/386"),
                             ("tag", "--push"), ("tag", "bad tag")):
            previous = getattr(self.args, field)
            setattr(self.args, field, value)
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.build()
            setattr(self.args, field, previous)
        self.assertEqual(self.calls, [])

    def test_rejects_source_symlink_to_private_model(self):
        source = self.root / builder.CONTEXT_FILES[-1]
        source.unlink()
        source.symlink_to(self.root / ".local/private.onnx")
        with self.assertRaisesRegex(ValueError, "symlinks"):
            self.build()
        self.assertFalse(any(a[:2] == ["docker", "build"] for a in self.calls))

    def test_rejects_source_parent_symlink(self):
        source = self.root / "voice_assistant"
        target = self.root / ".local/package"
        source.rename(target)
        source.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            self.build()

    def test_failed_smoke_never_returns_success_receipt(self):
        def fail(args, **kwargs):
            if args[:2] == ["docker", "run"]:
                raise subprocess.CalledProcessError(1, args)
            return self.fake_command(args, **kwargs)
        with patch.object(builder, "base_manifest", return_value=self.manifest), \
                patch.object(builder, "run", side_effect=fail):
            with self.assertRaises(subprocess.CalledProcessError):
                builder.build(self.args, root=self.root)

    def test_dockerfile_copies_every_context_file_except_itself_and_no_models(self):
        copied = []
        for line in (ROOT / "containers/voice-wake/Dockerfile").read_text().splitlines():
            if not line.startswith("COPY "):
                continue
            fields = shlex.split(line, comments=True)
            if fields and fields[0] == "COPY":
                copied.extend(fields[1:-1])
        self.assertEqual(set(copied), set(builder.CONTEXT_FILES)
                         - {"containers/voice-wake/Dockerfile"} | {"base-source.json"})
        self.assertFalse(any(".onnx" in p or ".local" in p for p in copied))


class BaseCompatibilityTests(unittest.TestCase):
    def test_base_manifest_covers_every_file_copied_into_manual_image(self):
        copied = {}
        for line in (ROOT / "containers/voice/Dockerfile").read_text().splitlines():
            if not line.startswith("COPY "):
                continue
            fields = shlex.split(line, comments=True)
            destination = fields[-1]
            for source in fields[1:-1]:
                installed = (destination + Path(source).name
                             if destination.endswith("/") else destination)
                self.assertNotIn(source, copied)
                copied[source] = installed
        self.assertEqual(builder.BASE_FILES, copied)

    def test_manifest_reads_explicit_commit_not_current_dirty_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in builder.BASE_FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("original " + name)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.name=Synthetic Test",
                            "-c", "user.email=test@example.invalid",
                            "commit", "-qm", "synthetic base"], cwd=root, check=True)
            original = (root / "voice_assistant/audio.py").read_bytes()
            (root / "voice_assistant/audio.py").write_text("unrelated working change")
            manifest = builder.base_manifest(root, "HEAD")
            self.assertEqual(manifest["sha256"]["/workspace/voice_assistant/audio.py"],
                             hashlib.sha256(original).hexdigest())
            self.assertEqual(len(manifest["sha256"]), len(builder.BASE_FILES))

    def test_smoke_rejects_wrong_base_source_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "file.py"
            source.write_bytes(b"expected")
            path = root / "manifest.json"
            path.write_text(json.dumps({"version": 1, "base_source_commit": "c" * 40,
                "sha256": {str(source): hashlib.sha256(b"expected").hexdigest()}}))
            self.assertEqual(smoke.check_base(path), "c" * 40)
            source.write_bytes(b"changed")
            with self.assertRaisesRegex(RuntimeError, "base_source_revision_mismatch"):
                smoke.check_base(path)

    @unittest.skipUnless(importlib.util.find_spec("onnxruntime")
                         and importlib.util.find_spec("numpy"),
                         "Optional ONNX Runtime and NumPy are unavailable")
    def test_real_cpu_inference_on_weightless_synthetic_model(self):
        self.assertTrue(smoke.check_onnx())


if __name__ == "__main__":
    unittest.main()
