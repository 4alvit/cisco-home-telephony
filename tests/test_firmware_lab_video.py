import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zlib


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("firmware_lab_video", ROOT / "tools/firmware_lab/video.py")
video = importlib.util.module_from_spec(spec)
spec.loader.exec_module(video)


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(width=298, height=156):
    return (video.PNG_SIGNATURE
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + b"\x10\x18\x20" * width) * height))
            + chunk(b"IEND", b""))


class OfflineVideoTest(unittest.TestCase):
    def test_plan_has_no_network_file_or_device_input(self):
        command = video.command_plan(4, 2)
        self.assertEqual(command[command.index("-i") + 1], "pipe:0")
        self.assertEqual(command[command.index("-protocol_whitelist") + 1], "pipe")
        self.assertEqual(command[command.index("-format_whitelist") + 1], "matroska,webm,mov,avi")
        self.assertNotIn("lavfi", command)
        self.assertEqual(command[-1], "pipe:1")
        self.assertIn("-an", command)
        self.assertIn("-nostdin", command)

    def test_bounds_reject_unbounded_or_nonfinite_work(self):
        for count, interval in ((0, 2), (31, 2), (True, 2), (2.0, 2),
                                (3, float("nan")), (3, float("inf")),
                                (3, .5), (3, 6), (30, 5), (1, True)):
            with self.subTest(count=count, interval=interval), self.assertRaises(video.VideoError):
                video.command_plan(count, interval)

    def test_regular_file_restrictions_preserve_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mkv"
            source.write_bytes(b"test bytes")
            link = root / "link.mkv"
            link.symlink_to(source)
            fifo = root / "fifo.mkv"
            os.mkfifo(fifo)
            for invalid in (root, link, fifo, "https://example.invalid/video.mkv"):
                with self.subTest(source=str(invalid)), self.assertRaises(video.VideoError):
                    video._read_source(invalid)
            self.assertEqual(video._read_source(source), b"test bytes")
            self.assertEqual(source.read_bytes(), b"test bytes")
            with patch.object(video, "MAX_INPUT_BYTES", 4), self.assertRaises(video.VideoError):
                video._read_source(source)

    def test_output_refuses_escape_reuse_and_symlink_ancestors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("../escape", "/absolute", ".", "", "a/b"):
                with self.subTest(name=name), self.assertRaises(video.VideoError):
                    video._fresh_directory(root, name)
            output = video._fresh_directory(root, "safe")
            self.assertEqual(output, root.resolve() / ".local/firmware-lab/video/safe")
            (output / "existing").write_bytes(b"untouched")
            with self.assertRaises(video.VideoError):
                video._fresh_directory(root, "safe")
            self.assertEqual((output / "existing").read_bytes(), b"untouched")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "target").mkdir()
            (root / ".local").symlink_to(root / "target", target_is_directory=True)
            with self.assertRaisesRegex(video.VideoError, "symlinks"):
                video._fresh_directory(root, "blocked")

    def test_png_checks_dimensions_checksums_and_frame_count(self):
        picture = png()
        self.assertEqual(video.split_pngs(picture * 2, 2), [picture, picture])
        for invalid, count in ((b"", 2), (picture[:-1], 2), (picture + b"extra", 2),
                               (picture * 2, 1), (png(1, 1), 1),
                               (picture[:50] + bytes([picture[50] ^ 1]) + picture[51:], 1)):
            with self.subTest(length=len(invalid), count=count), self.assertRaises(video.VideoError):
                video.split_pngs(invalid, count)

    def test_bad_dimensions_and_nonsquare_pixels_are_rejected(self):
        for stream in ({"width": 5000, "height": 50}, {"width": 0, "height": 40},
                       {"width": 4000, "height": 4000},
                       {"width": 400, "height": 300, "sample_aspect_ratio": "4:3"}):
            with patch.object(video, "_run", return_value=json.dumps({"streams": [stream]}).encode()):
                with self.assertRaises(video.VideoError):
                    video._probe_media(b"fixture")

    def test_subprocess_errors_are_redacted_and_time_bounded(self):
        result = subprocess.CompletedProcess(["ffmpeg"], 1, stdout=b"", stderr=b"private metadata")
        with patch.object(video.subprocess, "run", return_value=result) as run:
            with self.assertRaisesRegex(video.VideoError, "exit 1") as error:
                video._run(["ffmpeg"], b"fixture")
            self.assertNotIn("private", str(error.exception))
            self.assertEqual(run.call_args.kwargs["timeout"], 30)
            self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
            self.assertNotIn("shell", run.call_args.kwargs)
        with patch.object(video.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffmpeg", 30)):
            with self.assertRaisesRegex(video.VideoError, "time limit"):
                video._run(["ffmpeg"])

    def test_manifest_distinguishes_offline_success_from_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "private-source-name.mkv"
            source.write_bytes(b"synthetic fixture")
            with patch.object(video, "_probe_media", return_value={"codec": "h264"}), \
                    patch.object(video, "_run", return_value=png() * 2):
                report = video.extract_stills(source, name="demo", repository=root, frame_count=2)
            output = Path(report["output_directory"])
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["acceptance"]["offline_conversion"], "passed")
            self.assertEqual(manifest["acceptance"]["sip_negotiation"], "not tested")
            self.assertEqual(manifest["acceptance"]["phone_display"], "not tested")
            self.assertEqual(manifest["source"]["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertNotIn("private-source-name", json.dumps(manifest))
            self.assertEqual([frame["nominal_seconds"] for frame in manifest["frames"]], [0, 2])
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((output / "frame-000.png").stat().st_mode), 0o600)

    def test_missing_dependency_probe_does_not_launch_devices(self):
        with patch.object(video.shutil, "which", return_value=None), \
                patch.object(video.importlib.util, "find_spec", return_value=None), \
                patch.object(video.subprocess, "run") as run:
            result = video.probe_dependencies()
        run.assert_not_called()
        self.assertFalse(result["tools"]["pjsua"]["available"])
        self.assertFalse(result["python"]["PIL"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and ffprobe required")
    def test_real_h264_or_lossless_fixture_to_phone_size_pngs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "synthetic.mkv"
            encoders = subprocess.check_output(["ffmpeg", "-hide_banner", "-encoders"], stderr=subprocess.DEVNULL)
            encoder = "libx264" if b"libx264 " in encoders else "ffv1"
            # Synthetic pixels only; this command has no device or network input.
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
                            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=10:duration=6",
                            "-an", "-c:v", encoder, "-threads", "1", str(source)],
                           check=True, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            before = source.read_bytes()
            report = video.extract_stills(source, name="real", repository=root, frame_count=3)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(len(report["frames"]), 3)
            self.assertEqual(report["source"]["codec"], "h264" if encoder == "libx264" else "ffv1")
            self.assertEqual(report["dimensions"], [298, 156])
            self.assertGreater(len({frame["sha256"] for frame in report["frames"]}), 1)
            for frame in report["frames"]:
                path = Path(report["output_directory"]) / frame["file"]
                self.assertEqual(len(video.split_pngs(path.read_bytes(), 1)), 1)
            with self.assertRaisesRegex(video.VideoError, "already exists"):
                video.extract_stills(source, name="real", repository=root, frame_count=3)


if __name__ == "__main__":
    unittest.main()
