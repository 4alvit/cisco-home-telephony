"""Bounded, read-only observations of firmware bytes; never a firmware loader.

``bounded`` means that a standard-format component has complete, consistent
file-backed ranges inside the supplied buffer. It does not mean authentic,
deployable, executable on the host, or compatible with a particular runtime.
SBN is deliberately opaque: no undocumented length is trusted as an envelope
boundary and no signature verification is claimed.
"""

from __future__ import annotations

import hashlib
import struct
import zlib


MAX_INPUT_SIZE = 64 * 1024 * 1024
MAX_TABLE_ENTRIES = 4096
MAX_REPORTED_ENTRIES = 128
MAX_SYMBOL_ENTRIES = 4096


class BinaryFormatError(ValueError):
    """A truncated or inconsistent component."""


class BinaryLimitError(BinaryFormatError):
    """A configured resource limit, not evidence of an invalid component."""


class UnsupportedELF(BinaryFormatError):
    """An ELF feature this conservative parser does not implement."""


def _check_input(data: bytes) -> None:
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if len(data) > MAX_INPUT_SIZE:
        raise BinaryLimitError("input exceeds 64 MiB")


def _positive(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _range(size: int, start: int, length: int, label: str) -> int:
    if start < 0 or length < 0 or start > size or length > size - start:
        raise BinaryFormatError(f"{label} exceeds available bytes")
    return start + length


def _string(data: bytes, start: int, length: int, index: int) -> str:
    if index < 0 or index >= length:
        raise BinaryFormatError("string offset exceeds its string table")
    stop = min(start + length, start + index + 256)
    end = data.find(b"\0", start + index, stop)
    truncated = end < 0
    if truncated:
        end = stop
    raw = data[start + index:end]
    # Escape controls so metadata cannot inject terminal control sequences.
    return "".join(chr(c) if 32 <= c <= 126 else f"\\x{c:02x}" for c in raw) + (
        "..." if truncated else ""
    )


def _mips_flags(flags: int) -> dict:
    arch = {
        0x00000000: "MIPS I", 0x10000000: "MIPS II", 0x20000000: "MIPS III",
        0x30000000: "MIPS IV", 0x40000000: "MIPS V", 0x50000000: "MIPS32",
        0x60000000: "MIPS64", 0x70000000: "MIPS32r2", 0x80000000: "MIPS64r2",
        0x90000000: "MIPS32r6", 0xA0000000: "MIPS64r6",
    }.get(flags & 0xF0000000, "unrecognized")
    abi = {0x1000: "o32", 0x2000: "o64", 0x3000: "eabi32", 0x4000: "eabi64"}.get(
        flags & 0xF000, "not explicitly tagged"
    )
    return {"isa_flag": arch, "abi_flag": abi, "abi2_flag": bool(flags & 0x20),
            "pic_flag": bool(flags & 2), "cpic_flag": bool(flags & 4),
            "interpretation": "ELF flags only; calling convention and runtime ABI unproven"}


def parse_elf(data: bytes, offset: int = 0) -> dict:
    """Describe one ELF candidate, returning JSON-safe metadata even on failure.

    ELF32/ELF64 and both byte orders are supported. Extended numbering is
    explicitly unsupported. ``span_size`` is only supplied for bounded files;
    trailing padding or an enclosing archive are not included in that span.
    Symbol names are hints from validated tables, not an SDK or driver API.
    """
    _check_input(data)
    record = {"offset": offset, "status": "invalid", "errors": []}
    try:
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise BinaryFormatError("offset must be an integer")
        _range(len(data), offset, 16, "ELF identification")
        ident = data[offset:offset + 16]
        if ident[:4] != b"\x7fELF":
            raise BinaryFormatError("ELF magic missing")
        if ident[4] not in (1, 2) or ident[5] not in (1, 2):
            raise UnsupportedELF("unsupported ELF class or byte order")
        if ident[6] != 1:
            raise UnsupportedELF("unsupported ELF identification version")
        is64 = ident[4] == 2
        endian = ">" if ident[5] == 2 else "<"
        fmt = endian + ("HHIQQQIHHHHHH" if is64 else "HHIIIIIHHHHHH")
        header_size = 64 if is64 else 52
        available = len(data) - offset
        _range(available, 0, header_size, "ELF header")
        (kind, machine, version, entry, phoff, shoff, flags, ehsize, phentsize,
         phnum, shentsize, shnum, shstrndx) = struct.unpack_from(fmt, data, offset + 16)
        record["header"] = {
            "class_bits": 64 if is64 else 32, "byte_order": "big" if ident[5] == 2 else "little",
            "machine": machine, "machine_name": {8: "MIPS", 3: "x86", 62: "x86-64", 40: "ARM", 183: "AArch64"}.get(machine, "unrecognized"),
            "type": kind, "entry": entry, "flags": flags, "flags_hex": f"0x{flags:08x}",
            "osabi": ident[7], "abi_version": ident[8], "program_count": phnum,
            "section_count": shnum, "section_names_index": shstrndx,
        }
        if machine == 8:
            record["mips"] = _mips_flags(flags)
        if version != 1 or ehsize != header_size:
            raise BinaryFormatError("ELF version or header size is inconsistent")
        if phnum == 0xFFFF or shstrndx == 0xFFFF or (shnum == 0 and shoff != 0):
            raise UnsupportedELF("extended ELF numbering is not implemented")
        if phnum + shnum > MAX_TABLE_ENTRIES:
            raise BinaryLimitError("ELF table entry limit exceeded")
        phsize, shsize = (56, 64) if is64 else (32, 40)
        span = header_size
        for count, start, actual_size, expected_size, label in (
            (phnum, phoff, phentsize, phsize, "program table"),
            (shnum, shoff, shentsize, shsize, "section table"),
        ):
            if count:
                if actual_size != expected_size or start < header_size:
                    raise BinaryFormatError(f"ELF {label} layout is inconsistent")
                span = max(span, _range(available, start, count * actual_size, label))
            elif start:
                raise BinaryFormatError(f"ELF empty {label} has an offset")
        programs = []
        for i in range(phnum):
            values = struct.unpack_from(endian + ("IIQQQQQQ" if is64 else "IIIIIIII"),
                                        data, offset + phoff + i * phentsize)
            if is64:
                ptype, pflags, poff, vaddr, _paddr, filesz, memsz, align = values
            else:
                ptype, poff, vaddr, _paddr, filesz, memsz, pflags, align = values
            if ptype != 0:
                span = max(span, _range(available, poff, filesz, "program file range"))
                if ptype == 1 and filesz > memsz:
                    raise BinaryFormatError("load segment file size exceeds memory size")
                if align not in (0, 1) and (align & (align - 1) or poff % align != vaddr % align):
                    raise BinaryFormatError("program alignment is inconsistent")
            programs.append({"type": ptype, "offset": poff, "file_size": filesz,
                             "memory_size": memsz, "virtual_address": vaddr, "flags": pflags})
        sections = []
        for i in range(shnum):
            values = struct.unpack_from(endian + ("IIQQQQIIQQ" if is64 else "IIIIIIIIII"),
                                        data, offset + shoff + i * shentsize)
            name, stype, sflags, addr, soff, size, link, info, align, entsize = values
            if stype not in (0, 8):  # SHT_NULL and SHT_NOBITS have no file payload.
                span = max(span, _range(available, soff, size, "section file range"))
            if align not in (0, 1) and align & (align - 1):
                raise BinaryFormatError("section alignment is not a power of two")
            if link >= shnum and link != 0:
                raise BinaryFormatError("section link index exceeds section table")
            if entsize and size % entsize:
                raise BinaryFormatError("section size is not a multiple of its entry size")
            sections.append({"index": i, "name_offset": name, "type": stype, "flags": sflags,
                             "offset": soff, "size": size, "link": link, "entry_size": entsize})
        if shstrndx:
            if shstrndx >= shnum or sections[shstrndx]["type"] != 3:
                raise BinaryFormatError("section names do not reference a string table")
            names = sections[shstrndx]
            for section in sections:
                section["name"] = _string(data, offset + names["offset"], names["size"], section["name_offset"])
        imports, exports, libraries = set(), set(), set()
        total_symbols = scanned_symbols = 0
        metadata_notes = []
        for section in sections:
            stype = section["type"]
            if stype not in (2, 11, 6):
                continue
            expected = (24 if is64 else 16) if stype in (2, 11) else (16 if is64 else 8)
            if section["entry_size"] != expected or section["size"] % expected:
                raise BinaryFormatError("symbol/dynamic table entry size is inconsistent")
            strings = sections[section["link"]]
            if strings["type"] != 3:
                raise BinaryFormatError("symbol/dynamic table does not link to a string table")
            count = section["size"] // expected
            scan_count = min(count, max(0, MAX_SYMBOL_ENTRIES - scanned_symbols))
            total_symbols += count
            scanned_symbols += scan_count
            for i in range(scan_count):
                here = offset + section["offset"] + i * expected
                if stype == 6:
                    tag, value = struct.unpack_from(endian + ("qQ" if is64 else "iI"), data, here)
                    if tag == 0:
                        break
                    if tag == 1:
                        libraries.add(_string(data, offset + strings["offset"], strings["size"], value))
                else:
                    if is64:
                        name, info, _other, ndx, _value, _size = struct.unpack_from(endian + "IBBHQQ", data, here)
                    else:
                        name, _value, _size, info, _other, ndx = struct.unpack_from(endian + "IIIBBH", data, here)
                    if name and info >> 4 in (1, 2):
                        text = _string(data, offset + strings["offset"], strings["size"], name)
                        (imports if ndx == 0 else exports).add(text)
            if scan_count < count:
                metadata_notes.append("symbol/dynamic entry scan limit reached")
        record.update({
            "status": "bounded", "span_size": span,
            "sha256": hashlib.sha256(memoryview(data)[offset:offset + span]).hexdigest(),
            "programs": programs[:MAX_REPORTED_ENTRIES], "sections": sections[:MAX_REPORTED_ENTRIES],
            "tables_truncated": phnum > MAX_REPORTED_ENTRIES or shnum > MAX_REPORTED_ENTRIES,
            "symbols": {"undefined_global": sorted(imports)[:MAX_REPORTED_ENTRIES],
                        "defined_global": sorted(exports)[:MAX_REPORTED_ENTRIES],
                        "needed_libraries": sorted(libraries)[:MAX_REPORTED_ENTRIES],
                        "entries_total": total_symbols, "entries_scanned": scanned_symbols,
                        "names_truncated": any(len(x) > MAX_REPORTED_ENTRIES for x in (imports, exports, libraries)),
                        "notes": sorted(set(metadata_notes))},
            "interpretation": "bounded standard ELF file ranges; loading, signatures and runtime compatibility unproven",
        })
    except BinaryFormatError as exc:
        record["status"] = "limit" if isinstance(exc, BinaryLimitError) else "unsupported" if isinstance(exc, UnsupportedELF) else "invalid"
        record["errors"].append(str(exc))
    return record


def scan_elfs(data: bytes, *, max_candidates: int = 1024) -> list[dict]:
    """Find ELF magic without promoting an invalid header to a file."""
    _check_input(data)
    _positive(max_candidates, "max_candidates")
    records = []
    cursor = 0
    while (position := data.find(b"\x7fELF", cursor)) >= 0:
        if len(records) >= max_candidates:
            records.append({"offset": position, "status": "limit", "errors": ["ELF candidate limit reached"]})
            break
        records.append(parse_elf(data, position))
        cursor = position + 4
    return records


def decompress_gzip_at(data: bytes, offset: int = 0, *,
                       max_output_size: int = MAX_INPUT_SIZE) -> tuple[bytes, dict]:
    """Decompress exactly one gzip member, checking its trailer via zlib.

    Embedded member names are not interpreted. Concatenated members are not
    joined; ``compressed_size`` lets callers distinguish them. No partial data
    is returned on corruption, truncation, or a limit. Allocation is bounded
    by the output limit plus a one-byte overflow sentinel and input chunks.
    """
    _check_input(data)
    _positive(max_output_size, "max_output_size")
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise BinaryFormatError("offset must be an integer")
    _range(len(data), offset, 3, "gzip header")
    if data[offset:offset + 3] != b"\x1f\x8b\x08":
        raise BinaryFormatError("gzip magic missing")
    stream = zlib.decompressobj(16 + zlib.MAX_WBITS)
    output = bytearray()
    cursor = offset
    try:
        while cursor < len(data):
            chunk = data[cursor:min(len(data), cursor + 64 * 1024)]
            cursor += len(chunk)
            output.extend(stream.decompress(chunk, max_output_size - len(output) + 1))
            if len(output) > max_output_size:
                raise BinaryLimitError("gzip output limit exceeded")
            if stream.eof:
                consumed = cursor - offset - len(stream.unused_data)
                blob = bytes(output)
                return blob, {"offset": offset, "status": "bounded", "compressed_size": consumed,
                              "uncompressed_size": len(blob), "sha256": hashlib.sha256(blob).hexdigest(),
                              "checksum_verified": True}
            if stream.unconsumed_tail:
                raise BinaryLimitError("gzip output limit exceeded")
    except zlib.error as exc:
        raise BinaryFormatError("gzip data or trailer checksum is invalid") from exc
    raise BinaryFormatError("gzip member is truncated")


def scan_gzip(data: bytes, *, max_candidates: int = 1024,
              max_uncompressed_size: int = MAX_INPUT_SIZE,
              max_total_uncompressed_size: int = 2 * MAX_INPUT_SIZE) -> list[dict]:
    """Verify members with both individual and aggregate expansion budgets.

    Failed members conservatively consume their entire attempted output
    allowance. This also bounds work on overlapping corrupt/bomb candidates.
    Magic inside a successfully verified member is not rescanned as a member.
    """
    _check_input(data)
    for value, name in ((max_candidates, "max_candidates"),
                        (max_uncompressed_size, "max_uncompressed_size"),
                        (max_total_uncompressed_size, "max_total_uncompressed_size")):
        _positive(value, name)
    records = []
    cursor = 0
    budget = max_total_uncompressed_size
    while (position := data.find(b"\x1f\x8b\x08", cursor)) >= 0:
        if len(records) >= max_candidates or budget <= 0:
            records.append({"offset": position, "status": "limit", "errors": ["gzip candidate or aggregate expansion limit reached"]})
            break
        allowance = min(budget, max_uncompressed_size)
        try:
            blob, record = decompress_gzip_at(data, position, max_output_size=allowance)
            budget -= len(blob)
            cursor = position + record["compressed_size"]
        except BinaryFormatError as exc:
            budget -= allowance
            record = {"offset": position, "status": "limit" if isinstance(exc, BinaryLimitError) else "invalid",
                      "errors": [str(exc)], "expansion_budget_charged": allowance}
            cursor = position + 3
        records.append(record)
    return records


def inspect_binary(data: bytes, *, max_candidates: int = 1024,
                   max_uncompressed_size: int = MAX_INPUT_SIZE,
                   max_total_uncompressed_size: int = 2 * MAX_INPUT_SIZE,
                   include_gzip: bool = True) -> dict:
    """Reproducible metadata without clocks, paths, writes, or execution.

    Set ``include_gzip=False`` after reaching the caller's recursion depth or
    aggregate expansion budget. This skips decompression entirely and records
    that an empty gzip result does not establish absence of gzip members.
    """
    _check_input(data)
    if not isinstance(include_gzip, bool):
        raise ValueError("include_gzip must be a boolean")
    return {
        "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "envelope": {"format": "opaque", "prefix_16_hex": data[:16].hex(),
                     "vendor_signature_verified": False,
                     "interpretation": "prefix is an observation; SBN fields and authenticated boundaries are not decoded"},
        "elf": scan_elfs(data, max_candidates=max_candidates),
        "gzip_scan": "performed" if include_gzip else "not_requested",
        "gzip": scan_gzip(data, max_candidates=max_candidates,
                          max_uncompressed_size=max_uncompressed_size,
                          max_total_uncompressed_size=max_total_uncompressed_size) if include_gzip else [],
    }
