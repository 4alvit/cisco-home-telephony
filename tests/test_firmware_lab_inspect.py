import gzip
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tools.firmware_lab.inspect import inspect_directory
from tools.firmware_lab import binary


def tiny_elf():
    ident = b"\x7fELF" + bytes([1, 2, 1]) + bytes(9)
    return ident + struct.pack(
        ">HHIIIIIHHHHHH", 2, 8, 1, 0, 0, 0, 0x10000000,
        52, 0, 0, 0, 0, 0)


class FirmwareLabInspectTest(unittest.TestCase):
    def test_deterministic_report_carving_and_unchanged_sources(self):
        elf = tiny_elf()
        jar = io.BytesIO()
        with zipfile.ZipFile(jar, "w") as archive:
            archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        payload = b"synthetic-container\0" + gzip.compress(elf, mtime=0) + jar.getvalue()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "synthetic.sbn").write_bytes(payload)
            first_dir = root / ".local/first"
            first = inspect_directory(source, first_dir, extract=True, repository=root)
            second = inspect_directory(source, root / ".local/second", extract=True, repository=root)
            self.assertEqual(first, second)
            self.assertEqual((source / "synthetic.sbn").read_bytes(), payload)
            self.assertTrue(first["inputs_unchanged"])
            self.assertFalse(first["complete_reference_set"])
            self.assertFalse(first["vendor_signature_verified"])
            self.assertFalse(first["native_execution_proven"])
            self.assertFalse(first["midlet_deployment_proven"])
            kinds = {entry["kind"] for entry in first["artifacts"]}
            self.assertEqual(kinds, {"bin", "elf", "zip"})
            for entry in first["artifacts"]:
                data = (first_dir / entry["file"]).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])
                self.assertEqual(len(data), entry["size"])
            self.assertEqual(json.loads((first_dir / "report.json").read_text()), first)
            with self.assertRaises(ValueError):
                inspect_directory(source, first_dir, repository=root)

    def test_metadata_only_does_not_write_binary_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "a.sbn").write_bytes(gzip.compress(tiny_elf(), mtime=0))
            output = root / ".local/run"
            report = inspect_directory(source, output, repository=root)
            self.assertEqual(report["artifacts"], [])
            self.assertEqual([f.name for f in output.iterdir()], ["report.json"])

    def test_unknown_files_and_input_links_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            unknown = source / "notes.txt"
            unknown.write_text("not firmware")
            with self.assertRaises(ValueError):
                inspect_directory(source, root / ".local/run", repository=root)
            unknown.unlink()
            (root / "original").write_bytes(b"firmware")
            (source / "a.sbn").symlink_to(root / "original")
            with self.assertRaises(ValueError):
                inspect_directory(source, root / ".local/run", repository=root)
            self.assertFalse((root / ".local/run").exists())

    def test_gzip_work_budget_counts_both_passes_and_blocks_recursion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "a.sbn").write_bytes(gzip.compress(b"12345", mtime=0))
            (source / "b.sbn").write_bytes(gzip.compress(b"12345", mtime=0))
            with patch("tools.firmware_lab.inspect.MAX_GZIP_DECOMPRESSION_BYTES", 10):
                report = inspect_directory(source, root / ".local/run", repository=root)
            self.assertEqual(report["budgets_used"]["gzip_decompression_bytes"], 10)
            self.assertEqual(report["budgets_used"]["expanded_bytes"], 5)
            first = report["components"][0]["inspection"]
            second = report["components"][1]["inspection"]
            self.assertEqual(first["expanded"][0]["binary"]["gzip_scan"], "not_requested")
            self.assertEqual(second["binary"]["gzip_skip_reason"], "global_budget_limit")

    def test_failed_gzip_work_budget_is_shared_across_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            for name in ("a.sbn", "b.sbn", "c.sbn", "d.sbn"):
                (source / name).write_bytes(gzip.compress(b"x" * 11, mtime=0))
            with patch("tools.firmware_lab.inspect.MAX_GZIP_DECOMPRESSION_BYTES", 10), \
                    patch.object(binary, "decompress_gzip_at", wraps=binary.decompress_gzip_at) as decoder:
                report = inspect_directory(source, root / ".local/run", repository=root)
            charged = sum(call.kwargs["max_output_size"] for call in decoder.call_args_list)
            self.assertLessEqual(charged, 10)
            self.assertEqual(charged, report["budgets_used"]["gzip_decompression_bytes"])
            self.assertEqual(report["budgets_used"]["expanded_bytes"], 0)

    def test_java_budget_shared_and_extraction_does_not_revalidate(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as stream:
            stream.writestr("entry", b"12345")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            for name in ("a.sbn", "b.sbn", "c.sbn"):
                (source / name).write_bytes(archive.getvalue())
            with patch("tools.firmware_lab.inspect.MAX_JAR_DECOMPRESSION_BYTES", 10):
                report = inspect_directory(source, root / ".local/run", extract=True, repository=root)
            self.assertEqual(report["budgets_used"]["jar_decompression_bytes"], 10)
            self.assertEqual(len(report["artifacts"]), 2)
            self.assertEqual(report["components"][2]["inspection"]["java"]["stop_reason"],
                             "global_decompression_limit")


if __name__ == "__main__":
    unittest.main()
