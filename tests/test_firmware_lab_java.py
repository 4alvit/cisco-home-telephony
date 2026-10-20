"""Synthetic ZIP/JAR fixtures only; no vendor firmware is needed for tests."""

import io
import json
from pathlib import Path
import struct
import sys
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.firmware_lab.java import inspect_java, iter_validated_archives


def archive(entries=None, *, compression=zipfile.ZIP_DEFLATED, comment=b""):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as target:
        for name, payload in entries or []:
            target.writestr(name, payload)
        target.comment = comment
    return buffer.getvalue()


def class_header(major=46, minor=0):
    return b"\xca\xfe\xba\xbe" + struct.pack(">HH", minor, major) + b"synthetic fixture"


class JavaInspectorTests(unittest.TestCase):
    def rejection(self, data, reason, **limits):
        report = inspect_java(data, **limits)
        self.assertEqual(report["archives"], [])
        self.assertIn(reason, [item["reason"] for item in report["rejected_candidates"]])

    def test_embedded_disjoint_archives_have_exact_extents_and_safe_metadata(self):
        first = archive([
            ("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\r\nMIDlet-Name: private fixture\r\nMIDlet-Jar-URL: https://private.invalid/app.jar\r\nMicroEdition-Profile: MIDP-2.0\r\nMicroEdition-Configuration: CLDC-1.1\r\nMIDlet-1: confidential,,a.Main\r\nMIDlet-Certificate-1-1: secret\r\n\r\n"),
            ("javax/microedition/midlet/MIDlet.class", class_header()),
            ("cip/midp/midletsuite/Installer.class", class_header(45, 3)),
            ("private/name/DoNotExpose.class", class_header()),
        ], comment=b"private comment")
        second = archive([("resource.bin", b"data")])
        data = b"firmware" + first + b"gap" + second + b"trailer"
        report = inspect_java(data)
        self.assertTrue(report["scan_complete"])
        self.assertEqual(len(report["archives"]), 2)
        item = report["archives"][0]
        self.assertEqual((item["offset"], item["end_offset"]), (8, 8 + len(first)))
        self.assertEqual(item["class_versions"], {"45.3": 1, "46.0": 2})
        self.assertEqual(item["package_count"], 3)
        self.assertIn("vendor_midlet_installer_classes", item["capability_hints"])
        self.assertEqual(item["manifest"]["profiles"], ["MIDP-2.0"])
        self.assertTrue(item["manifest"]["signature_attribute_present"])
        self.assertEqual(item["signature_verification"], "not_performed")
        self.assertEqual([payload for _, payload in iter_validated_archives(data)], [first, second])
        rendered = json.dumps(report)
        for secret in ("private", "confidential", "secret", "DoNotExpose", "resource.bin"):
            self.assertNotIn(secret, rendered)

    def test_empty_and_stored_archives(self):
        self.assertEqual(inspect_java(archive())["archives"][0]["entry_count"], 0)
        item = inspect_java(archive([("A.class", b"not bytecode")], compression=zipfile.ZIP_STORED))["archives"][0]
        self.assertEqual(item["invalid_class_header_count"], 1)

    def test_crc_and_header_corruption_are_rejected(self):
        data = bytearray(archive([("x", b"payload")], compression=zipfile.ZIP_STORED))
        data[31] ^= 1
        self.rejection(bytes(data), "member_crc_mismatch")
        data = bytearray(archive([("x", b"payload")]))
        struct.pack_into("<I", data, 18, 0)
        self.rejection(bytes(data), "local_size_crc_mismatch")

    def test_truncated_false_markers_and_comment_extent(self):
        self.rejection(b"noisePK\x05\x06", "truncated_end_record")
        self.rejection(archive(comment=b"comment")[:-1], "invalid_archive_extent")
        self.assertEqual(inspect_java(b"PK\x03\x04noise")["archives"], [])

    def test_member_and_total_limits_prevent_decompression(self):
        data = archive([("a", b"x" * 1000), ("b", b"y" * 1000)])
        self.rejection(data, "member_size_limit", max_member_bytes=100)
        self.rejection(data, "total_uncompressed_limit", max_total_uncompressed_bytes=1500)
        self.rejection(data, "entry_limit", max_entries=1)
        report = inspect_java(data, max_input_bytes=4)
        self.assertEqual(report["stop_reason"], "input_size_limit")

    def test_output_budget_is_shared_across_archives(self):
        one = archive([("a", b"x" * 100)])
        report = inspect_java(one + one, max_total_uncompressed_bytes=150)
        self.assertEqual(len(report["archives"]), 1)
        self.assertEqual(report["rejected_candidates"][0]["reason"], "total_uncompressed_limit")

    def test_encrypted_multidisk_and_zip64_are_explicitly_rejected(self):
        base = archive([("a", b"b")])
        data = bytearray(base)
        central = data.find(b"PK\x01\x02")
        struct.pack_into("<H", data, central + 8, 1)
        self.rejection(bytes(data), "encrypted_unsupported")
        data = bytearray(base)
        struct.pack_into("<H", data, len(data) - 18, 1)
        self.rejection(bytes(data), "multidisk_unsupported")
        data = bytearray(base)
        struct.pack_into("<I", data, len(data) - 10, 0xffffffff)
        self.rejection(bytes(data), "zip64_unsupported")

    def test_unsafe_paths_and_symlinks_are_rejected(self):
        for name in ("../secret", "/absolute", "C:/drive", "dir\\file"):
            self.rejection(archive([(name, b"data")]), "unsafe_member_path")
        info = zipfile.ZipInfo("link")
        info.external_attr = 0o120777 << 16
        self.rejection(archive([(info, b"target")]), "symlink_member_unsupported")

    def test_archive_and_candidate_limits_are_visible(self):
        one = archive([("a", b"b")])
        report = inspect_java(one + one, max_archives=1)
        self.assertFalse(report["scan_complete"])
        self.assertEqual(len(report["archives"]), 1)
        report = inspect_java(b"PK\x05\x06" * 20, max_candidates=2)
        self.assertFalse(report["scan_complete"])
        self.assertEqual(report["candidates_examined"], 2)

    def test_data_descriptor_archive_is_supported(self):
        class NonSeeking(io.BytesIO):
            def seek(self, *args):
                raise OSError("stream")
        output = NonSeeking()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
            target.writestr("a.class", class_header())
        self.assertEqual(inspect_java(output.getvalue())["archives"][0]["class_entry_count"], 1)

    def test_false_uncompressed_size_does_not_expand_a_bomb(self):
        data = bytearray(archive([("a", b"x" * 1000000)]))
        central = data.find(b"PK\x01\x02")
        struct.pack_into("<I", data, 22, 1)
        struct.pack_into("<I", data, central + 24, 1)
        self.rejection(bytes(data), "deflate_size_or_termination_mismatch", max_member_bytes=2)
        self.assertEqual(inspect_java(bytes(data))["decompression_budget_used"], 1)

    def test_aliasing_member_headers_are_rejected(self):
        data = bytearray(archive([("a", b"content"), ("b", b"content")]))
        first = data.find(b"PK\x01\x02")
        second = data.find(b"PK\x01\x02", first + 4)
        # Point the second central record at the first member; local name must
        # agree with its own central entry, so this is not a valid second file.
        struct.pack_into("<I", data, second + 42, 0)
        self.rejection(bytes(data), "local_name_mismatch")

    def test_central_directory_size_and_member_extents_are_checked(self):
        original = archive([("a", b"content")])
        data = bytearray(original)
        central = data.find(b"PK\x01\x02")
        struct.pack_into("<I", data, central + 20, len(data) * 2)
        self.rejection(bytes(data), "invalid_member_extent")
        data = bytearray(original)
        struct.pack_into("<H", data, central + 32, 1)
        self.rejection(bytes(data), "truncated_central_directory")

    def test_unsupported_compression_is_explicit(self):
        data = bytearray(archive([("a", b"content")]))
        central = data.find(b"PK\x01\x02")
        struct.pack_into("<H", data, central + 10, 99)
        self.rejection(bytes(data), "compression_unsupported")

    def test_manifest_output_uses_only_known_version_values(self):
        data = archive([("META-INF/MANIFEST.MF", b"MicroEdition-Profile: MIDP-123456789.0\r\nMicroEdition-Configuration: CLDC-1.\r\n 1\r\n\r\nName: private\r\n")])
        item = inspect_java(data)["archives"][0]["manifest"]
        self.assertEqual(item["profiles"], [])
        self.assertEqual(item["configurations"], ["CLDC-1.1"])

    def test_forged_signature_entries_do_not_claim_trust(self):
        data = archive([("META-INF/TEST.RSA", b"not a signature")])
        item = inspect_java(data)["archives"][0]
        self.assertEqual(item["signature_entry_count"], 1)
        self.assertEqual(item["signature_verification"], "not_performed")
        self.assertEqual(item["installability"], "not_established")

    def test_bad_limits_are_caller_errors(self):
        for limit in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                inspect_java(b"", max_entries=limit)


if __name__ == "__main__":
    unittest.main()
