"""Private wake-volume staging with synthetic bytes; no inference or network."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "voice_wake_staging_test", ROOT / "containers/voice-wake/stage_models.py")
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)


class WakeModelStagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "private-volume"
        self.source.mkdir()
        (self.source / "nested").mkdir()
        self.destination = self.root / "runtime/.local/models"
        self.destination.mkdir(mode=0o700, parents=True)
        self.destination.parent.chmod(0o700)
        self.payload = {role: ("private-model-" + role).encode() for role in stage.ROLES}
        self.bundle = {"format_version": 1, "frontend": stage.FRONTEND_ID,
                       "threshold": 0.5, "models": {}}
        for role, value in self.payload.items():
            path = self.source / "nested" / (role + ".onnx")
            path.write_bytes(value)
            self.bundle["models"][role] = {
                "path": "nested/" + role + ".onnx",
                "sha256": hashlib.sha256(value).hexdigest(),
            }
        self.source_before = {path.relative_to(self.source): path.read_bytes()
                              for path in self.source.rglob("*") if path.is_file()}

    def run_stage(self, *, source=None, destination=None, bundle=None):
        return stage.stage_models(source or self.source, destination or self.destination,
                                  self.bundle if bundle is None else bundle)

    def assert_empty_failure(self, **kwargs):
        with self.assertRaises(stage.StageError) as caught:
            self.run_stage(**kwargs)
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertNotIn(str(self.root), str(caught.exception))

    def test_verified_bytes_and_normalized_bundle_are_private_without_source_changes(self):
        original = copy.deepcopy(self.bundle)
        with mock.patch("socket.socket", side_effect=AssertionError("network")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("process")):
            result = self.run_stage()
        self.assertEqual(result, {"staged": True, "models": 3,
                                  "total_bytes": sum(map(len, self.payload.values()))})
        self.assertEqual(self.bundle, original)
        expected = copy.deepcopy(original)
        for role, data in self.payload.items():
            self.assertEqual((self.destination / (role + ".onnx")).read_bytes(), data)
            expected["models"][role]["path"] = role + ".onnx"
        self.assertEqual(json.loads((self.destination / "bundle.json").read_text()), expected)
        for path in self.destination.iterdir():
            self.assertFalse(path.is_symlink())
            self.assertEqual(path.stat().st_uid, os.geteuid())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        for path, data in self.source_before.items():
            self.assertEqual((self.source / path).read_bytes(), data)

    def test_malformed_bundles_fail_before_creating_files(self):
        invalid = []
        for field, value in (("format_version", True), ("frontend", "other"),
                             ("threshold", False), ("threshold", 0), ("threshold", 2),
                             ("threshold", 10 ** 1000), ("threshold", float("nan")),
                             ("threshold", float("inf")), ("models", {})):
            invalid.append({**self.bundle, field: value})
        invalid.append({**self.bundle, "extra": "private"})
        for name in ("/tmp/secret.onnx", "../secret.onnx", "nested/../secret.onnx",
                     "nested//mel.onnx", "./mel.onnx", "nested/mel.txt",
                     "nested\\mel.onnx", "nested/\nmel.onnx"):
            value = copy.deepcopy(self.bundle)
            value["models"]["mel"]["path"] = name
            invalid.append(value)
        for digest in ("A" * 64, "a" * 63, None, []):
            value = copy.deepcopy(self.bundle)
            value["models"]["mel"]["sha256"] = digest
            invalid.append(value)
        for value in invalid:
            with self.subTest(bundle=value):
                self.assert_empty_failure(bundle=value)

    def test_checksum_mismatch_is_not_published(self):
        (self.source / "nested/mel.onnx").write_bytes(b"changed-under-pinned-manifest")
        self.assert_empty_failure()

    def test_file_directory_and_root_symlinks_are_never_followed(self):
        path = self.source / "nested/mel.onnx"
        data = path.read_bytes()
        path.unlink()
        replacement = self.source / "real.onnx"
        replacement.write_bytes(data)
        path.symlink_to(replacement)
        self.assert_empty_failure()
        path.unlink()
        path.write_bytes(data)
        nested = self.source / "nested"
        nested.rename(self.source / "real-directory")
        nested.symlink_to("real-directory", target_is_directory=True)
        self.assert_empty_failure()
        alias = self.root / "volume-alias"
        alias.symlink_to(self.source, target_is_directory=True)
        self.assert_empty_failure(source=alias)

    def test_nonregular_file_does_not_block(self):
        path = self.source / "nested/mel.onnx"
        path.unlink()
        os.mkfifo(path)
        self.assert_empty_failure()

    def test_empty_oversized_and_aggregate_size_are_bounded(self):
        path = self.source / "nested/mel.onnx"
        value = path.read_bytes()
        path.write_bytes(b"")
        self.assert_empty_failure()
        path.write_bytes(value)
        with mock.patch.object(stage, "MAX_MODEL_BYTES", len(value) - 1):
            self.assert_empty_failure()
        with mock.patch.object(stage, "MAX_TOTAL_BYTES", sum(map(len, self.payload.values())) - 1):
            self.assert_empty_failure()

    def test_existing_content_and_wrong_modes_are_preserved(self):
        old = self.destination / "existing.onnx"
        old.write_bytes(b"keep")
        with self.assertRaisesRegex(stage.StageError, "destination_must_be_empty"):
            self.run_stage()
        self.assertEqual(old.read_bytes(), b"keep")
        old.unlink()
        self.destination.chmod(0o755)
        self.assert_empty_failure()
        self.destination.chmod(0o700)
        with mock.patch.object(stage.os, "geteuid", return_value=os.geteuid() + 1):
            self.assert_empty_failure()

    def test_mid_copy_failure_rolls_back_only_created_files(self):
        original = stage._create
        calls = []
        def fail(directory, name, data, created):
            calls.append(name)
            original(directory, name, data, created)
            if name == "embedding.onnx":
                raise OSError("private failure detail")
        with mock.patch.object(stage, "_create", side_effect=fail):
            self.assert_empty_failure()
        self.assertIn("mel.onnx", calls)
        self.assertIn("embedding.onnx", calls)

    def test_competing_file_and_replaced_owned_inode_survive_rollback(self):
        original = stage._create
        def conflict(directory, name, data, created):
            original(directory, name, data, created)
            if name == "mel.onnx":
                path = self.destination / name
                # Preserve the old inode under another name to prevent inode
                # reuse from making this a false replacement observation.
                path.rename(self.destination / "held-original")
                path.write_bytes(b"new-owner")
                (self.destination / "competitor").write_bytes(b"other")
                raise OSError("fail")
        with mock.patch.object(stage, "_create", side_effect=conflict):
            with self.assertRaises(stage.StageError):
                self.run_stage()
        self.assertEqual((self.destination / "mel.onnx").read_bytes(), b"new-owner")
        self.assertEqual((self.destination / "competitor").read_bytes(), b"other")
        self.assertFalse((self.destination / stage.MARKER).exists())

    def test_repeat_staging_and_path_aliases_are_refused(self):
        self.run_stage()
        with self.assertRaisesRegex(stage.StageError, "destination_must_be_empty"):
            self.run_stage()
        for source, destination in (
                (Path("relative"), self.destination),
                (self.source, self.root / "runtime/.local/other"),
                (self.destination, self.destination),
                (self.destination.parent, self.destination)):
            with self.assertRaises(stage.StageError):
                self.run_stage(source=source, destination=destination)

    def test_changed_source_after_verification_cannot_replace_copied_bytes(self):
        original = stage._create
        def swap(directory, name, data, created):
            if name == stage.MARKER:
                (self.source / "nested/mel.onnx").write_bytes(b"replacement-model")
            original(directory, name, data, created)
        with mock.patch.object(stage, "_create", side_effect=swap):
            self.run_stage()
        self.assertEqual((self.destination / "mel.onnx").read_bytes(), self.payload["mel"])
        bundle = json.loads((self.destination / "bundle.json").read_text())
        self.assertEqual(hashlib.sha256((self.destination / "mel.onnx").read_bytes()).hexdigest(),
                         bundle["models"]["mel"]["sha256"])

    def test_concurrent_initializers_have_one_owner(self):
        barrier = threading.Barrier(2, timeout=5)
        original = stage._create
        results, failures = [], []
        def synchronized(directory, name, data, created):
            if name == stage.MARKER:
                barrier.wait()
            original(directory, name, data, created)
        def initialize():
            try:
                results.append(self.run_stage())
            except stage.StageError:
                failures.append(True)
        with mock.patch.object(stage, "_create", side_effect=synchronized):
            workers = [threading.Thread(target=initialize) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual({path.name for path in self.destination.iterdir()},
                         {"bundle.json", "mel.onnx", "embedding.onnx", "classifier.onnx"})


if __name__ == "__main__":
    unittest.main()
