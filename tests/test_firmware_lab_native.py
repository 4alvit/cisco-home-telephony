"""Synthetic layout tests only; no vendor bytes or native-code execution."""

import copy
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from tools.firmware_lab.native import (
    NativeLayoutError, address_to_offset, main, map_image, offset_to_address,
    read_input, write_report,
)


def specimen(prefix=b"", suffix=b""):
    header = bytearray(100)
    header[:4] = bytes.fromhex("deadbeef")
    struct.pack_into(">III", header, 52, 0x12345678, 0x1010, 3)
    struct.pack_into(">9I", header, 64, 1, 0x1000, 32, 2, 0x8000, 16, 3, 0x8010, 32)
    text = struct.pack(">4I", 0x1010, 0x1020, 0x8010, 0x8030) + b"\0" * 16
    return prefix + header + text + b"\0" * 16 + suffix


def changed(data, offset, value):
    result = bytearray(data)
    struct.pack_into(">I", result, offset, value)
    return bytes(result)


class NativeMapTest(unittest.TestCase):
    def test_contained_candidate_and_roundtrip(self):
        data = specimen(b"odd", b"tail")
        result = map_image(data, 3)
        self.assertEqual(result["span_size"], 148)
        self.assertEqual(result["trailing_bytes"], 4)
        self.assertEqual(result["entry_file_offset"], 119)
        self.assertTrue(all(result["cross_checks"].values()))
        self.assertFalse(result["vendor_signature_verified"])
        self.assertFalse(result["execution_compatible"])
        for address, offset in [(0x1000, 103), (0x101f, 134), (0x8000, 135), (0x800f, 150)]:
            self.assertEqual(address_to_offset(result, address), offset)
            self.assertEqual(offset_to_address(result, offset), address)

    def test_memory_only_and_cross_region_ranges(self):
        result = map_image(specimen())
        self.assertIsNone(address_to_offset(result, 0x8010))
        self.assertIsNone(address_to_offset(result, 0x800f, 2))
        self.assertIsNone(address_to_offset(result, 0x9999))
        self.assertIsNone(offset_to_address(result, 99))
        self.assertIsNone(offset_to_address(result, 131, 2))
        self.assertIsNone(offset_to_address(result, 148))

    def test_cross_checks_are_evidence_not_authentication(self):
        result = map_image(changed(specimen(), 100, 0))
        self.assertFalse(result["cross_checks"]["entry"])
        self.assertFalse(result["vendor_signature_verified"])

    def test_header_and_region_truncations(self):
        for size in [0, 3, 63, 64, 99, 100, 131, 132, 147]:
            with self.subTest(size=size), self.assertRaises(NativeLayoutError):
                map_image(specimen()[:size])

    def test_invalid_header_and_regions(self):
        variants = {
            "magic": (0, 0), "zero count": (60, 0), "large count": (60, 65),
            "kind": (64, 4), "zero size": (72, 0), "unaligned address": (68, 0x1001),
            "unaligned size": (72, 31), "overlap": (80, 0x1010),
            "entry outside": (56, 0x9000), "entry unaligned": (56, 0x1011),
            "memory overflow": (92, 0xfffffff0), "memory limit": (96, 0x10000000),
            "kind order": (88, 1), "two text regions": (76, 1),
        }
        for name, (offset, value) in variants.items():
            with self.subTest(name=name), self.assertRaises(NativeLayoutError):
                map_image(changed(specimen(), offset, value))

    def test_argument_types_and_limits(self):
        for offset in [True, -1, 1.0, len(specimen())]:
            with self.subTest(offset=offset), self.assertRaises(NativeLayoutError):
                map_image(specimen(), offset)
        with self.assertRaises(NativeLayoutError):
            map_image(bytearray(specimen()))
        with patch("tools.firmware_lab.native.MAX_INPUT", 64), self.assertRaises(NativeLayoutError):
            map_image(specimen())
        result = map_image(specimen())
        for method in (address_to_offset, offset_to_address):
            for address, size in [(True, 1), (-1, 1), (0, 0), (0, True)]:
                with self.subTest(method=method.__name__, address=address, size=size):
                    with self.assertRaises(NativeLayoutError):
                        method(result, address, size)
        with self.assertRaises(NativeLayoutError):
            address_to_offset(result, 0xffffffff, 2)

    def test_no_mutation(self):
        data = specimen()
        result = map_image(data)
        snapshot = copy.deepcopy(result)
        address_to_offset(result, 0x1000)
        offset_to_address(result, 100)
        self.assertEqual(result, snapshot)
        self.assertEqual(data, specimen())


class NativeFileTest(unittest.TestCase):
    def test_cli_metadata_only_no_clobber(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            private = root / ".local"
            private.mkdir()
            source = private / "synthetic.bin"
            source.write_bytes(specimen())
            target = private / "map.json"
            self.assertEqual(main(["--input", str(source), "--output", str(target)]), 0)
            self.assertEqual(source.read_bytes(), specimen())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(target.read_text())["span_size"], 148)
            with self.assertRaises(FileExistsError):
                write_report(target, {})

    def test_reject_output_outside_private_or_symlink_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with self.assertRaises(NativeLayoutError):
                write_report(root / "public.json", {})
            with self.assertRaises(NativeLayoutError):
                write_report(root / ".local", {})
            private = root / ".local"
            private.mkdir()
            with self.assertRaises(NativeLayoutError):
                write_report(private / ".." / "escape.json", {})
            link = private / "alias"
            link.symlink_to(private, target_is_directory=True)
            with self.assertRaises(NativeLayoutError):
                write_report(link / "report.json", {})

    def test_reject_symlink_input_and_fifo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "source"
            source.write_bytes(specimen())
            link = root / "link"
            link.symlink_to(source)
            with self.assertRaises(OSError):
                read_input(link)
            fifo = root / "fifo"
            os.mkfifo(fifo)
            with self.assertRaises(NativeLayoutError):
                read_input(fifo)


if __name__ == "__main__":
    unittest.main()
