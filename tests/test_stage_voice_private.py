"""Offline staging of synthetic AtomicWriter inputs into private control files."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def module_from_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


stage = module_from_file("stage_voice_private_test", ROOT / "containers/voice/stage_private.py")
control = module_from_file("staged_voice_control_test", ROOT / "containers/asterisk/phone_voice_control.py")


class StageVoicePrivateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.source = self.root / "projected-inputs"
        self.version = self.source / "..synthetic-version"
        self.version.mkdir(parents=True)
        (self.source / "..data").symlink_to(self.version.name, target_is_directory=True)
        self.workspace = self.root / "runtime"
        self.destination = self.workspace / ".local/config"
        self.destination.mkdir(parents=True, mode=0o700)
        self.destination.parent.chmod(0o700)
        self.state = self.workspace / ".local/state"
        self.state.mkdir(mode=0o700)
        self.state_file = self.state / "existing-session.json"
        self.state_file.write_bytes(b'{"synthetic-existing-state": true}\n')
        self.state_before = self.state_file.read_bytes()
        gateway = {
            "persistent_enabled": True, "bind_ipv4": "192.168.50.10", "api_port": 18084,
            "api_token": "a" * 48, "api_clients": ["192.168.50.11"],
            "ami": {"host": "192.168.50.10", "port": 5038,
                    "username": "synthetic-gateway", "secret": "synthetic-ami-secret"},
            "dial_allowlist": [], "ring_allowlist": [],
            "phones": [{"id": "201", "lines": ["201", "202"], "ipv4": "192.168.50.20",
                        "device": "SEP020000000001", "port": 18080,
                        "auth_path": "/auth/" + "c" * 64,
                        "username": "u" * 24, "password": "p" * 24}],
        }
        transport = {
            "gateway_config": ".local/config/gateway.json", "line": "201",
            "bind_ipv4": "192.168.50.10", "bind_port": 24576, "phone_rtp_port": 24578,
            "duration_seconds": 10, "receive_idle_seconds": 3, "volume": 20,
        }
        voice = {
            "service": {"enabled": True, "rearm_after_turn": False}, "phones": {"201": True},
            "ha": {"base_url": "http://192.168.50.30:8123", "token_file": ".local/config/token.txt",
                   "pipeline_id": "synthetic-pipeline"},
            "wake": {"mode": "manual", "bundle_file": ".local/config/unused-bundle.json"},
        }
        configuration = {
            "version": 1, "enabled": True, "state_directory": ".local/state",
            "python_executable": sys.executable,
            "ffmpeg_executable": sys.executable,  # Validation only; no process is started.
            "profiles": [{"transport_config": ".local/config/transport.json",
                          "voice_config": ".local/config/voice.json"}],
        }
        self.payload = {name + ".json": json.dumps(data).encode()
                        for name, data in (("gateway", gateway), ("transport", transport),
                                           ("voice", voice), ("control", configuration))}
        self.payload["token.txt"] = b"synthetic-staged-token\n"
        for name, data in self.payload.items():
            self.project(name, data)
        self.manifest = self.source / "staging.json"
        self.write_manifest({"version": 1, "files": list(self.payload)})

    def project(self, name, data):
        target = self.version / name
        if target.exists():
            target.chmod(0o600)
        target.write_bytes(data)
        target.chmod(0o440)
        link = self.source / name
        if not link.is_symlink():
            link.symlink_to(Path("..data") / name)

    def write_manifest(self, data):
        self.project("staging.json", json.dumps(data).encode())

    def stage(self, **kwargs):
        return stage.stage_private(kwargs.get("source", self.source),
                                   kwargs.get("destination", self.destination),
                                   kwargs.get("manifest", self.manifest))

    def assert_state_preserved(self):
        self.assertEqual(self.state_file.read_bytes(), self.state_before)
        self.assertEqual(list(self.state.iterdir()), [self.state_file])

    def assert_rejected_without_outputs(self, **kwargs):
        with self.assertRaises(stage.StageError):
            self.stage(**kwargs)
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assert_state_preserved()

    def test_atomic_writer_inputs_become_real_owner_only_control_files(self):
        self.assertTrue(self.manifest.is_symlink())
        self.assertTrue((self.source / "token.txt").is_symlink())
        count = self.stage()
        self.assertEqual(count, len(self.payload))
        self.assertEqual({path.name for path in self.destination.iterdir()}, set(self.payload))
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o700)
        for name, content in self.payload.items():
            path = self.destination / name
            self.assertFalse(path.is_symlink())
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.stat().st_uid, os.getuid())
        with mock.patch("socket.socket", side_effect=AssertionError("staging opened network")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("staging started process")):
            config = control.load_control_config(self.destination / "control.json", self.workspace)
        self.assertTrue(config.enabled)
        self.assertEqual(config.state_directory, self.state)
        self.assertEqual(config.profiles[0].phone_id, "201")
        self.assertEqual(config.profiles[0].lines, ("201", "202"))
        self.assertEqual(config.profiles[0].transport_config, self.destination / "transport.json")
        self.assert_state_preserved()

    def test_cli_stages_without_disclosing_payload_or_private_paths(self):
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "containers/voice/stage_private.py"),
             "--source", str(self.source), "--destination", str(self.destination),
             "--manifest", str(self.manifest)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        for marker in ("synthetic-staged-token", "synthetic-ami-secret", str(self.source), str(self.destination)):
            self.assertNotIn(marker, result.stdout + result.stderr)
        self.assertEqual((self.destination / "token.txt").read_bytes(), self.payload["token.txt"])
        self.assert_state_preserved()

    def test_invalid_manifest_schema_names_and_duplicates_leave_destination_empty(self):
        invalid = [
            {"version": True, "files": ["token.txt"]},
            {"version": 2, "files": ["token.txt"]},
            {"version": 1, "files": ["token.txt"], "extra": True},
            {"version": 1, "files": "token.txt"},
            {"version": 1, "files": []},
            {"version": 1, "files": ["token.txt", "token.txt"]},
        ]
        for name in ("../token.txt", "/token.txt", "nested/token.txt", ".token.txt",
                     "token..txt", "token.py", "staging.json", 123):
            invalid.append({"version": 1, "files": [name]})
        for manifest in invalid:
            with self.subTest(manifest=manifest):
                self.write_manifest(manifest)
                self.assert_rejected_without_outputs()

    def test_missing_last_input_does_not_publish_earlier_valid_files(self):
        self.write_manifest({"version": 1, "files": ["token.txt", "missing.json"]})
        self.assert_rejected_without_outputs()

    def test_source_file_link_cannot_escape_projected_root(self):
        outside = self.root / "outside-secret.txt"
        outside.write_text("synthetic-outside-secret")
        target = self.source / "token.txt"
        target.unlink()
        target.symlink_to(outside)
        self.assert_rejected_without_outputs()
        self.assertEqual(outside.read_text(), "synthetic-outside-secret")

    def test_manifest_must_resolve_inside_source_root(self):
        outside = self.root / "outside-manifest.json"
        outside.write_bytes(self.manifest.read_bytes())
        self.manifest.unlink()
        self.manifest.symlink_to(outside)
        self.assert_rejected_without_outputs()

    def test_source_root_symlink_is_not_accepted(self):
        linked = self.root / "linked-inputs"
        linked.symlink_to(self.source, target_is_directory=True)
        self.assert_rejected_without_outputs(source=linked, manifest=linked / "staging.json")

    def test_per_file_size_is_bounded_before_destination_changes(self):
        self.project("large.txt", b"x" * (stage.MAX_FILE_BYTES + 1))
        self.write_manifest({"version": 1, "files": ["token.txt", "large.txt"]})
        self.assert_rejected_without_outputs()

    def test_total_size_is_bounded_before_destination_changes(self):
        names = []
        for number in range(stage.MAX_TOTAL_BYTES // stage.MAX_FILE_BYTES + 1):
            name = "large-" + str(number) + ".txt"
            self.project(name, b"x" * stage.MAX_FILE_BYTES)
            names.append(name)
        self.write_manifest({"version": 1, "files": names})
        self.assert_rejected_without_outputs()

    def test_manifest_file_count_is_bounded(self):
        self.write_manifest({"version": 1, "files": ["f" + str(i) + ".txt"
                                                     for i in range(stage.MAX_FILES + 1)]})
        self.assert_rejected_without_outputs()

    def test_manifest_bytes_and_duplicate_keys_are_rejected(self):
        for content in (b" " * (stage.MAX_MANIFEST_BYTES + 1),
                        b'{"version":1,"version":1,"files":["token.txt"]}'):
            with self.subTest(size=len(content)):
                self.project("staging.json", content)
                self.assert_rejected_without_outputs()

    def test_special_source_file_is_rejected_without_blocking(self):
        name = "pipe.txt"
        os.mkfifo(self.source / name)
        self.write_manifest({"version": 1, "files": [name]})
        self.assert_rejected_without_outputs()

    def test_destination_permissions_must_be_owner_only(self):
        for mode in (0o750, 0o770, 0o755, 0o777):
            with self.subTest(mode=oct(mode)):
                self.destination.chmod(mode)
                self.assert_rejected_without_outputs()
        self.destination.chmod(0o700)

    def test_destination_must_belong_to_current_user(self):
        with mock.patch.object(stage.os, "geteuid", return_value=os.geteuid() + 1):
            self.assert_rejected_without_outputs()

    def test_destination_symlink_cannot_redirect_writes(self):
        original = self.destination.with_name("original-config")
        self.destination.rename(original)
        self.destination.symlink_to(original, target_is_directory=True)
        self.assert_rejected_without_outputs()
        self.assertEqual(list(original.iterdir()), [])

    def test_destination_parent_symlink_cannot_redirect_writes(self):
        local = self.destination.parent
        original = local.with_name("original-local")
        local.rename(original)
        local.symlink_to(original, target_is_directory=True)
        self.assert_rejected_without_outputs()

    def test_destination_outside_local_config_is_rejected(self):
        outside = self.workspace / "public-config"
        outside.mkdir(mode=0o700)
        self.assert_rejected_without_outputs(destination=outside)
        self.assertEqual(list(outside.iterdir()), [])

    def test_existing_configuration_is_not_partially_updated(self):
        self.stage()
        before = {path.name: path.read_bytes() for path in self.destination.iterdir()}
        self.project("token.txt", b"synthetic-new-token\n")
        with self.assertRaises(stage.StageError):
            self.stage()
        self.assertEqual({path.name: path.read_bytes() for path in self.destination.iterdir()}, before)
        self.assert_state_preserved()

    def test_failure_after_second_file_write_rolls_back_all_owned_files(self):
        real_fsync = os.fsync
        calls = 0

        def fail_second_file(descriptor):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.assertEqual({p.name for p in self.destination.iterdir()},
                                 {stage.MARKER, "gateway.json", "transport.json"})
                raise OSError("synthetic disk failure")
            return real_fsync(descriptor)

        with mock.patch.object(stage.os, "fsync", side_effect=fail_second_file):
            self.assert_rejected_without_outputs()
        self.assertEqual(calls, 2)

    def test_rollback_preserves_a_foreign_replacement_and_sibling_state(self):
        real_fsync = os.fsync
        calls = 0
        replacement = self.destination / "transport.json"

        def replace_second_file_then_fail(descriptor):
            nonlocal calls
            calls += 1
            if calls == 2:
                # The original inode remains open, so this replacement has a
                # distinct identity even on filesystems which recycle inodes.
                replacement.unlink()
                replacement.write_bytes(b"synthetic-foreign-replacement")
                raise OSError("synthetic concurrent replacement")
            return real_fsync(descriptor)

        with mock.patch.object(stage.os, "fsync", side_effect=replace_second_file_then_fail):
            with self.assertRaises(stage.StageError):
                self.stage()
        self.assertEqual(list(self.destination.iterdir()), [replacement])
        self.assertEqual(replacement.read_bytes(), b"synthetic-foreign-replacement")
        self.assert_state_preserved()

    def test_competing_initializer_marker_is_never_overwritten_or_removed(self):
        real_open = os.open
        injected = False

        def claim_marker_first(path, flags, mode=0o777, **kwargs):
            nonlocal injected
            if path == stage.MARKER and flags & os.O_CREAT and not injected:
                injected = True
                descriptor = real_open(path, flags, mode, **kwargs)
                os.write(descriptor, b"synthetic-other-initializer")
                os.close(descriptor)
            return real_open(path, flags, mode, **kwargs)

        with mock.patch.object(stage.os, "open", side_effect=claim_marker_first):
            with self.assertRaises(stage.StageError):
                self.stage()
        self.assertTrue(injected)
        marker = self.destination / stage.MARKER
        self.assertEqual(list(self.destination.iterdir()), [marker])
        self.assertEqual(marker.read_bytes(), b"synthetic-other-initializer")
        self.assert_state_preserved()


if __name__ == "__main__":
    unittest.main()
