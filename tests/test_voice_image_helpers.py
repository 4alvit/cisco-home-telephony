"""Exercise image helpers offline through a recording Docker executable."""
import ast
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
IMAGE_FILES = (
    "containers/voice/Dockerfile",
    "containers/voice/requirements.lock",
    "containers/voice/smoke.py",
    "containers/voice/stage_private.py",
)
CONTROL_SOURCE = "containers/asterisk/phone_voice_control.py"
HANDS_FREE_SOURCE = "containers/asterisk/phone_voice_hands_free.py"
SIP_SOURCES = ("scripts/voice_session_sip.py", "containers/asterisk/phone_voice_sip.py",
               "containers/asterisk/phone_voice_sip_ami.py")
BASE_DIGEST = "sha256:" + "a" * 64
IMAGE_ID = "sha256:" + "b" * 64


def renderer_sources():
    tree = ast.parse((ROOT / "scripts/render_voice_job.py").read_text())
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "SOURCE_FILES"
            for target in statement.targets
        ):
            return ast.literal_eval(statement.value)
    raise AssertionError("Renderer has no literal SOURCE_FILES allowlist")


FAKE_DOCKER = r'''
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

args = sys.argv[1:]
records = Path(os.environ["VOICE_FAKE_RECORDS"])
with (records / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps(args) + "\n")
if args == ["info"]:
    pass
elif args[:3] == ["buildx", "imagetools", "inspect"]:
    print(os.environ["VOICE_FAKE_BASE_DIGEST"])
elif args[:1] == ["build"]:
    context = Path(args[-1])
    files = {}
    for path in context.rglob("*"):
        if path.is_file():
            files[path.relative_to(context).as_posix()] = {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "mode": stat.S_IMODE(path.stat().st_mode),
                "symlink": path.is_symlink(),
            }
    (records / "context.json").write_text(json.dumps({"path": str(context), "files": files}))
    if os.environ.get("VOICE_FAKE_FAIL") == "build":
        sys.exit(7)
elif args[:2] == ["image", "inspect"]:
    print(os.environ["VOICE_FAKE_IMAGE_ID"])
elif args[:1] == ["run"]:
    if os.environ.get("VOICE_FAKE_FAIL") == "run":
        sys.exit(8)
else:
    # Never forward unknown commands, including pushes, to a real Docker binary.
    sys.exit(98)
'''


class VoiceImageHelperTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.repo = self.root / "synthetic-repository"
        self.bin = self.root / "bin"
        self.records = self.root / "records"
        self.tmp = self.root / "tmp"
        for path in (self.repo, self.bin, self.records, self.tmp):
            path.mkdir()
        self.sources = renderer_sources()
        for relative in (*self.sources, CONTROL_SOURCE, HANDS_FREE_SOURCE, *SIP_SOURCES):
            self.write(relative, "# Synthetic source: " + relative + "\n")
        for relative in (*IMAGE_FILES, "scripts/build-voice-image.sh", "scripts/smoke-voice-image.sh"):
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        self.write(".local/private-token.txt", "synthetic-private-canary\n")
        self.write("unlisted.py", "# synthetic-unlisted-public-canary\n")
        self.write("containers/voice/unlisted.txt", "synthetic-image-directory-canary\n")
        fake = self.bin / "docker"
        fake.write_text("#!" + sys.executable + "\n" + FAKE_DOCKER)
        fake.chmod(0o755)
        self.environment = dict(os.environ)
        self.environment.pop("PYTHON_IMAGE", None)
        self.environment.pop("BUILD_PLATFORM", None)
        self.environment.pop("VOICE_FAKE_FAIL", None)
        self.environment.update(
            PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            TMPDIR=str(self.tmp), VOICE_FAKE_RECORDS=str(self.records),
            VOICE_FAKE_BASE_DIGEST=BASE_DIGEST, VOICE_FAKE_IMAGE_ID=IMAGE_ID,
        )

    def write(self, relative, text):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def run_helper(self, script, *arguments, **environment):
        for record in self.records.iterdir():
            record.unlink()
        return subprocess.run(
            ["bash", str(self.repo / "scripts" / script), *arguments],
            cwd=self.root, env={**self.environment, **environment},
            capture_output=True, text=True, timeout=15,
        )

    def build(self, **environment):
        return self.run_helper("build-voice-image.sh", "example.invalid/synthetic", **environment)

    def calls(self):
        log = self.records / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def only_call(self, command):
        calls = [args for args in self.calls() if args[0] == command]
        self.assertEqual(len(calls), 1, self.calls())
        return calls[0]

    def assert_no_build_or_run(self):
        self.assertFalse(any(args[0] in {"build", "run"} for args in self.calls()), self.calls())
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_build_snapshots_only_runner_control_and_image_files(self):
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = json.loads((self.records / "context.json").read_text())
        self.assertNotIn(CONTROL_SOURCE, self.sources,
                         "One-shot Job snapshots must not include the gateway control adapter")
        expected = set(self.sources) | {CONTROL_SOURCE, HANDS_FREE_SOURCE} | set(SIP_SOURCES) | set(IMAGE_FILES)
        self.assertEqual(set(snapshot["files"]), expected)
        dockerfile = (self.repo / "containers/voice/Dockerfile").read_text().replace("\\\n", " ")
        copied = {}
        for line in dockerfile.splitlines():
            fields = shlex.split(line, comments=True)
            if fields and fields[0] == "COPY":
                for source in fields[1:-1]:
                    copied[source] = fields[-1]
        self.assertEqual(set(copied), expected - {"containers/voice/Dockerfile"})
        self.assertEqual(copied[CONTROL_SOURCE], "/workspace/containers/asterisk/")
        self.assertEqual(copied[HANDS_FREE_SOURCE], "/workspace/containers/asterisk/")
        self.assertEqual(copied["containers/asterisk/phone_camera_preview.py"],
                         "/workspace/containers/asterisk/")
        self.assertEqual(copied["containers/voice/stage_private.py"], "/opt/voice/stage_private.py")
        for relative, info in snapshot["files"].items():
            self.assertEqual(info["sha256"], hashlib.sha256((self.repo / relative).read_bytes()).hexdigest())
            self.assertEqual(info["mode"], 0o644)
            self.assertFalse(info["symlink"])
        context = Path(snapshot["path"])
        self.assertNotEqual(context, self.repo)
        self.assertEqual(context.parent, self.tmp)
        self.assertFalse(context.exists(), "Temporary source snapshot must be removed")
        build = self.only_call("build")
        self.assertEqual(build[build.index("--platform") + 1], "linux/amd64")
        self.assertEqual(build[build.index("--build-arg") + 1],
                         "PYTHON_IMAGE=python:3.12-slim-bookworm@" + BASE_DIGEST)
        self.assertEqual(build[build.index("-f") + 1], str(context / "containers/voice/Dockerfile"))
        self.assertEqual(build[build.index("-t") + 1], "example.invalid/synthetic/voice:pilot")
        self.assertEqual(build[-1], str(context))
        self.assertEqual(self.only_call("image")[2], "example.invalid/synthetic/voice:pilot")
        self.assertIn(IMAGE_ID, self.only_call("run"))
        self.assertFalse(any(args[0] in {"push", "login", "pull"} for args in self.calls()))

    def test_explicit_official_digest_and_arm64_are_used_without_registry_lookup(self):
        for version in ("3.11", "3.12"):
            with self.subTest(version=version):
                base = "python:" + version + "-slim-bookworm@" + BASE_DIGEST
                result = self.build(PYTHON_IMAGE=base, BUILD_PLATFORM="linux/arm64")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(any(args[0] == "buildx" for args in self.calls()))
                build = self.only_call("build")
                self.assertEqual(build[build.index("--platform") + 1], "linux/arm64")
                self.assertEqual(build[build.index("--build-arg") + 1], "PYTHON_IMAGE=" + base)

    def test_mutable_or_unapproved_base_image_never_builds(self):
        for base in ("python:3.12-slim-bookworm", "example.invalid/python@" + BASE_DIGEST,
                     "python:3.13-slim-bookworm@" + BASE_DIGEST,
                     "python:3.12-slim-bookworm@sha256:" + "A" * 64,
                     "python:3.12-slim-bookworm@sha256:1234"):
            with self.subTest(base=base):
                result = self.build(PYTHON_IMAGE=base)
                self.assertNotEqual(result.returncode, 0)
                self.assert_no_build_or_run()

    def test_invalid_registry_digest_never_builds(self):
        result = self.build(VOICE_FAKE_BASE_DIGEST="sha256:not-a-digest")
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_build_or_run()

    def test_invalid_platform_or_prefix_is_rejected_before_docker(self):
        result = self.build(BUILD_PLATFORM="linux/386")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])
        for prefix in ("--push", "example.invalid/space in name", ""):
            with self.subTest(prefix=prefix):
                result = self.run_helper("build-voice-image.sh", prefix)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls(), [])

    def test_source_file_symlink_into_private_directory_is_rejected(self):
        source = self.repo / self.sources[0]
        source.unlink()
        source.symlink_to(self.repo / ".local/private-token.txt")
        result = self.build()
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_build_or_run()

    def test_source_parent_symlink_is_rejected(self):
        source_parent = self.repo / "voice_assistant"
        private_parent = self.repo / ".local/private-package"
        source_parent.rename(private_parent)
        source_parent.symlink_to(private_parent, target_is_directory=True)
        result = self.build()
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_build_or_run()

    def test_failed_build_removes_context_and_does_not_smoke(self):
        result = self.build(VOICE_FAKE_FAIL="build")
        self.assertEqual(result.returncode, 7, result.stderr)
        snapshot = json.loads((self.records / "context.json").read_text())
        self.assertFalse(Path(snapshot["path"]).exists())
        self.assertFalse(any(args[0] in {"run", "image"} for args in self.calls()))

    def test_smoke_uses_local_image_id_and_restricts_network_filesystem_and_privileges(self):
        result = self.run_helper("smoke-voice-image.sh", "example.invalid/synthetic/voice:pilot")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([args[0] for args in self.calls()], ["image", "run"])
        run = self.only_call("run")
        for flag in ("--rm", "--read-only"):
            self.assertIn(flag, run)
        for flag, value in {
            "--pull": "never", "--network": "none",
            "--tmpfs": "/tmp:rw,nosuid,nodev,noexec,size=64m",
            "--cap-drop": "ALL", "--security-opt": "no-new-privileges",
            "--user": "10001:10001", "--pids-limit": "64", "--memory": "512m",
            "--cpus": "1", "--entrypoint": "python3",
        }.items():
            self.assertEqual(run[run.index(flag) + 1], value)
        self.assertEqual(run[-2:], [IMAGE_ID, "/opt/voice/smoke.py"])
        for flag in ("--privileged", "--device", "--volume", "-v", "--mount", "--cap-add"):
            self.assertNotIn(flag, run)

    def test_smoke_rejects_invalid_local_image_id_before_run(self):
        result = self.run_helper("smoke-voice-image.sh", "synthetic:pilot",
                                 VOICE_FAKE_IMAGE_ID="synthetic:mutable")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(args[0] == "run" for args in self.calls()))

    def test_smoke_failure_is_not_reported_as_successful_build(self):
        result = self.build(VOICE_FAKE_FAIL="run")
        self.assertEqual(result.returncode, 8, result.stderr)
        self.assertNotIn("Built and smoke-tested locally:", result.stdout)
        self.assertEqual(list(self.tmp.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
