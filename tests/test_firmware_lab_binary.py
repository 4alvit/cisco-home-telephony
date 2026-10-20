"""Synthetic binary fixtures only; no vendor firmware is distributed."""

import gzip
import hashlib
import importlib.util
from pathlib import Path
import struct
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "firmware_lab_binary", Path(__file__).parents[1] / "tools/firmware_lab/binary.py"
)
binary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(binary)


def elf32(*, endian=">", machine=8, nobits=False, symbols=False):
    """A complete synthetic ELF executable with one loadable segment."""
    ident = b"\x7fELF" + bytes((1, 2 if endian == ">" else 1, 1, 0, 0)) + bytes(7)
    text = b"\0\0\0\0"
    names = b"\0.text\0.bss\0.shstrtab\0.symtab\0.strtab\0"
    sections = [(0,) * 10, (1, 1, 6, 0x1000, 84, len(text), 0, 0, 4, 0)]
    body = bytearray(text + names)
    if nobits:
        sections.append((7, 8, 3, 0x2000, 0xFFFFFF00, 0x1000, 0, 0, 4, 0))
    nameindex = len(sections)
    sections.append((12, 3, 0, 0, 88, len(names), 0, 0, 1, 0))
    if symbols:
        strtab = b"\0service_open\0module_start\0"
        syms = bytes(16) + struct.pack(endian + "IIIBBH", 1, 0, 0, 0x12, 0, 0) + struct.pack(endian + "IIIBBH", 14, 0x1000, 4, 0x12, 0, 1)
        symoffset = 84 + len(body)
        sections.append((22, 2, 0, 0, symoffset, len(syms), len(sections) + 1, 1, 4, 16))
        body.extend(syms)
        sections.append((30, 3, 0, 0, 84 + len(body), len(strtab), 0, 0, 1, 0))
        body.extend(strtab)
    shoff = 84 + len(body)
    header = ident + struct.pack(endian + "HHIIIIIHHHHHH", 2, machine, 1, 0x1000, 52, shoff, 0x10000000, 52, 32, 1, 40, len(sections), nameindex)
    program = struct.pack(endian + "IIIIIIII", 1, 84, 0x1000, 0, 4, 4, 5, 4)
    return header + program + body + b"".join(struct.pack(endian + "IIIIIIIIII", *s) for s in sections)


def patch32(blob, offset, value, endian=">"):
    copy = bytearray(blob)
    struct.pack_into(endian + "I", copy, offset, value)
    return bytes(copy)


class ELFTests(unittest.TestCase):
    def test_complete_embedded_mips_file_and_span(self):
        elf = elf32()
        source = b"wrapper" + elf + b"not part of ELF"
        record = binary.parse_elf(source, 7)
        self.assertEqual(record["status"], "bounded")
        self.assertEqual(record["span_size"], len(elf))
        self.assertEqual(record["sha256"], hashlib.sha256(elf).hexdigest())
        self.assertEqual(record["header"]["byte_order"], "big")
        self.assertEqual(record["mips"]["isa_flag"], "MIPS II")
        self.assertEqual(record["mips"]["abi_flag"], "not explicitly tagged")

    def test_little_endian_and_elf64(self):
        self.assertEqual(binary.parse_elf(elf32(endian="<"))["status"], "bounded")
        ident = b"\x7fELF\x02\x01\x01" + bytes(9)
        header = ident + struct.pack("<HHIQQQIHHHHHH", 1, 62, 1, 0, 0, 0, 0, 64, 0, 0, 0, 0, 0)
        self.assertEqual(binary.parse_elf(header)["header"]["class_bits"], 64)
        self.assertEqual(binary.parse_elf(header)["span_size"], 64)

    def test_nobits_does_not_extend_file(self):
        blob = elf32(nobits=True)
        self.assertEqual(binary.parse_elf(blob)["span_size"], len(blob))

    def test_validated_symbol_hints(self):
        record = binary.parse_elf(elf32(symbols=True))
        self.assertEqual(record["status"], "bounded", record)
        self.assertEqual(record["symbols"]["undefined_global"], ["service_open"])
        self.assertEqual(record["symbols"]["defined_global"], ["module_start"])

    def test_every_truncation_is_not_extractable(self):
        blob = elf32()
        for end in range(len(blob)):
            with self.subTest(end=end):
                record = binary.parse_elf(blob[:end])
                self.assertNotEqual(record["status"], "bounded")
                self.assertNotIn("span_size", record)

    def test_file_ranges_and_segment_memory_are_validated(self):
        for offset, value in ((28, 0xFFFFFFF0), (32, 0xFFFFFFF0), (56, 0xFFFFFFF0), (68, 100000), (72, 1), (80, 3)):
            with self.subTest(offset=offset):
                self.assertEqual(binary.parse_elf(patch32(elf32(), offset, value))["status"], "invalid")

    def test_section_range_link_and_name_validated(self):
        blob = elf32()
        shoff = struct.unpack_from(">I", blob, 32)[0]
        for rel, value in ((40, 10000), (40 + 16, len(blob)), (40 + 24, 10000)):
            self.assertEqual(binary.parse_elf(patch32(blob, shoff + rel, value))["status"], "invalid")

    def test_extended_numbering_and_limits(self):
        blob = bytearray(elf32())
        struct.pack_into(">H", blob, 44, 0xFFFF)
        self.assertEqual(binary.parse_elf(bytes(blob))["status"], "unsupported")
        struct.pack_into(">H", blob, 44, 5000)
        self.assertEqual(binary.parse_elf(bytes(blob))["status"], "limit")

    def test_candidate_limit_and_random_header(self):
        records = binary.scan_elfs(b"\x7fELF" * 5, max_candidates=2)
        self.assertEqual([r["status"] for r in records], ["unsupported", "unsupported", "limit"])
        self.assertEqual(binary.parse_elf(b"not ELF" * 4)["status"], "invalid")

    def test_symbol_link_cannot_select_arbitrary_section(self):
        blob = elf32(symbols=True)
        shoff = struct.unpack_from(">I", blob, 32)[0]
        malformed = patch32(blob, shoff + 3 * 40 + 24, 1)
        self.assertEqual(binary.parse_elf(malformed)["status"], "invalid")


class GzipTests(unittest.TestCase):
    def test_complete_member_with_wrapper_and_tail(self):
        compressed = gzip.compress(b"synthetic payload", mtime=0)
        output, metadata = binary.decompress_gzip_at(b"prefix" + compressed + b"tail", 6)
        self.assertEqual(output, b"synthetic payload")
        self.assertEqual(metadata["compressed_size"], len(compressed))
        self.assertTrue(metadata["checksum_verified"])

    def test_truncation_checksum_and_exact_size_limit(self):
        compressed = gzip.compress(b"x" * 100, mtime=0)
        self.assertEqual(len(binary.decompress_gzip_at(compressed, max_output_size=100)[0]), 100)
        with self.assertRaises(binary.BinaryLimitError):
            binary.decompress_gzip_at(compressed, max_output_size=99)
        for blob in (compressed[:-1], compressed[:-8] + bytes(8)):
            with self.assertRaises(binary.BinaryFormatError):
                binary.decompress_gzip_at(blob)

    def test_concatenation_keeps_members_separate(self):
        first = gzip.compress(b"one", mtime=0)
        second = gzip.compress(b"two", mtime=0)
        output, metadata = binary.decompress_gzip_at(first + second)
        self.assertEqual(output, b"one")
        self.assertEqual(metadata["compressed_size"], len(first))
        self.assertEqual([x["offset"] for x in binary.scan_gzip(first + second)], [0, len(first)])

    def test_total_budget_and_failed_member_budget(self):
        member = gzip.compress(b"a" * 100, mtime=0)
        records = binary.scan_gzip(member * 3, max_total_uncompressed_size=150)
        self.assertEqual([r["status"] for r in records], ["bounded", "limit", "limit"])
        corrupt = b"\x1f\x8b\x08" + bytes(7) + b"bad deflate"
        records = binary.scan_gzip(corrupt + member, max_uncompressed_size=10, max_total_uncompressed_size=10)
        self.assertEqual(records[0]["expansion_budget_charged"], 10)
        self.assertEqual(records[-1]["status"], "limit")

    def test_empty_and_multichunk_member(self):
        for payload in (b"", bytes(range(256)) * 1024):
            compressed = gzip.compress(payload, mtime=0)
            self.assertEqual(binary.decompress_gzip_at(compressed)[0], payload)

    def test_inspection_reproducible_and_no_signature_claim(self):
        blob = b"opaque" + elf32() + gzip.compress(b"data", mtime=0)
        report = binary.inspect_binary(blob)
        self.assertEqual(report, binary.inspect_binary(blob))
        self.assertFalse(report["envelope"]["vendor_signature_verified"])
        self.assertEqual(len(report["elf"]), 1)
        self.assertEqual(len(report["gzip"]), 1)

    def test_gzip_scan_can_be_skipped_without_decompression(self):
        blob = elf32() + gzip.compress(b"large expansion" * 1024, mtime=0)
        with patch.object(binary, "decompress_gzip_at", side_effect=AssertionError("must not decompress")):
            report = binary.inspect_binary(blob, include_gzip=False, max_uncompressed_size=1)
        self.assertEqual(report["gzip_scan"], "not_requested")
        self.assertEqual(report["gzip"], [])
        self.assertEqual(report["elf"][0]["status"], "bounded")
        self.assertEqual(binary.inspect_binary(b"")["gzip_scan"], "performed")

    def test_gzip_scan_flag_must_be_boolean(self):
        with self.assertRaises(ValueError):
            binary.inspect_binary(b"", include_gzip="false")

    def test_invalid_parameters(self):
        for kwargs in ({"max_candidates": 0}, {"max_uncompressed_size": -1}, {"max_total_uncompressed_size": True}):
            with self.assertRaises(ValueError):
                binary.inspect_binary(b"", **kwargs)
        with self.assertRaises(binary.BinaryFormatError):
            binary.decompress_gzip_at(b"data", -1)


if __name__ == "__main__":
    unittest.main()
