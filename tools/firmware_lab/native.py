"""Read-only map of the observed DEADBEEF segmented-image layout.

This is an empirical layout recognizer, not a vendor format specification,
signature verifier, ELF converter, loader or execution tool. Kinds 1/2 are
treated as consecutive file-backed regions and kind 3 as memory-only regions.
The interpretation must be corroborated for each input; magic alone is weak.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import struct

MAX_INPUT = 64 * 1024 * 1024
MAX_SEGMENTS = 64
MAX_MEMORY = 256 * 1024 * 1024


class NativeLayoutError(ValueError):
    """Input does not fit this bounded, empirically observed layout."""


def _require(value: bool, message: str) -> None:
    if not value:
        raise NativeLayoutError(message)


def _uint(value: int, name: str) -> None:
    _require(type(value) is int and value >= 0, name + " must be a nonnegative integer")


def map_image(data: bytes, offset: int = 0) -> dict:
    """Map one candidate inside bytes without extracting or executing content.

    All file offsets are absolute within the supplied bytes. Memory addresses
    are declared addresses, not observations of an actual running process.
    Trailing bytes are permitted and separately counted; no SBN envelope or
    signature boundary is inferred from the candidate extent.
    """
    _require(type(data) is bytes and len(data) <= MAX_INPUT, "input must be bytes, at most 64 MiB")
    _uint(offset, "offset")
    _require(offset <= len(data) and len(data) - offset >= 64, "truncated candidate header")
    _require(data[offset:offset + 4] == b"\xde\xad\xbe\xef", "candidate magic missing")
    tag, entry, count = struct.unpack_from(">III", data, offset + 52)
    _require(1 <= count <= MAX_SEGMENTS, "segment count outside 1..64")
    header_size = 64 + count * 12
    _require(header_size <= len(data) - offset, "truncated segment table")
    cursor, memory_total, segments = offset + header_size, 0, []
    previous_kind = 0
    for index in range(count):
        kind, address, size = struct.unpack_from(">III", data, offset + 64 + index * 12)
        _require(kind in (1, 2, 3), "unrecognized segment kind")
        _require(kind >= previous_kind, "segment kinds are not ordered")
        previous_kind = kind
        _require(size > 0 and address % 4 == size % 4 == 0, "unaligned or empty region")
        _require(address + size <= 2**32, "32-bit memory range overflow")
        memory_total += size
        _require(memory_total <= MAX_MEMORY, "declared memory exceeds 256 MiB")
        _require(all(address + size <= s["address"] or address >= s["address_end"]
                     for s in segments), "overlapping declared memory regions")
        backed = kind in (1, 2)
        if backed:
            _require(size <= len(data) - cursor, "truncated file-backed region")
        segments.append({
            "index": index, "kind": kind, "address": address,
            "address_end": address + size, "memory_size": size,
            "descriptor_offset": offset + 64 + index * 12,
            "file_offset": cursor if backed else None,
            "file_size": size if backed else 0,
        })
        if backed:
            cursor += size
    text = [s for s in segments if s["kind"] == 1]
    _require(len(text) == 1 and text[0]["memory_size"] >= 16,
             "one kind-1 region of at least 16 bytes required")
    _require(entry % 4 == 0 and text[0]["address"] <= entry < text[0]["address_end"],
             "entry is not aligned within kind-1 region")
    words = list(struct.unpack_from(">IIII", data, text[0]["file_offset"]))
    data_regions = [s for s in segments if s["kind"] == 2]
    words_match = {
        "entry": words[0] == entry,
        "kind_1_end": words[1] == text[0]["address_end"],
        "kind_2_end": bool(data_regions) and words[2] == max(s["address_end"] for s in data_regions),
        "declared_memory_end": words[3] == max(s["address_end"] for s in segments),
    }
    return {
        "format_version": 1, "layout": "observed-deadbeef-segments-v1",
        "candidate_offset": offset, "header_size": header_size,
        "span_size": cursor - offset, "trailing_bytes": len(data) - cursor,
        "input_size": len(data), "input_sha256": hashlib.sha256(data).hexdigest(),
        "candidate_sha256": hashlib.sha256(data[offset:cursor]).hexdigest(),
        "header_word_0x34": tag, "entry_candidate": entry,
        "entry_file_offset": text[0]["file_offset"] + entry - text[0]["address"],
        "declared_memory_bytes": memory_total, "segments": segments,
        "first_kind_1_words": words, "cross_checks": words_match,
        "vendor_signature_verified": False, "execution_compatible": False,
        "interpretation": "Empirical file/address map; region semantics and loader acceptance are not verified.",
    }


def address_to_offset(mapping: dict, address: int, size: int = 1) -> int | None:
    """Translate a wholly contained range; memory-only/unmapped returns None."""
    _uint(address, "address")
    _uint(size, "size")
    _require(size > 0 and address + size <= 2**32, "invalid address range")
    for segment in mapping["segments"]:
        if segment["address"] <= address and address + size <= segment["address_end"]:
            if segment["file_offset"] is None:
                return None
            return segment["file_offset"] + address - segment["address"]
    return None


def offset_to_address(mapping: dict, offset: int, size: int = 1) -> int | None:
    """Translate a wholly contained file-backed range; no cross-region reads."""
    _uint(offset, "offset")
    _uint(size, "size")
    _require(size > 0, "size must be positive")
    for segment in mapping["segments"]:
        start = segment["file_offset"]
        if start is not None and start <= offset and offset + size <= start + segment["file_size"]:
            return segment["address"] + offset - start
    return None


def read_input(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        _require(stat.S_ISREG(info.st_mode) and info.st_size <= MAX_INPUT,
                 "input must be a regular file at most 64 MiB")
        data = stream.read(MAX_INPUT + 1)
    _require(len(data) <= MAX_INPUT, "input grew beyond limit")
    return data


def write_report(path: Path, report: dict) -> None:
    """Owner-only, no-clobber metadata report; no vendor payload is written."""
    path = path.absolute()
    _require(".." not in path.parts, "output must not contain parent traversal")
    _require(".local" in path.parent.parts, "output must be below a .local directory")
    for parent in path.parents:
        _require(not parent.is_symlink(), "output parent is a symlink")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--offset", type=lambda x: int(x, 0), default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        data = read_input(args.input)
        report = map_image(data, args.offset)
        _require(hashlib.sha256(read_input(args.input)).hexdigest() == report["input_sha256"],
                 "input changed during mapping")
        write_report(args.output, report)
    except (NativeLayoutError, OSError) as error:
        parser.exit(1, f"Native image mapping failed: {error}\n")
    print("Private metadata report saved; no firmware extracted or executed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
