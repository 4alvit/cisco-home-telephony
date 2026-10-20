"""Bounded, read-only inspection of embedded classic ZIP/JAR archives.

No member is executed or written to disk. Names and manifest values are not
returned: metadata contains counts and fixed, allowlisted capability labels.
Validation means ZIP structure, sizes, deflate termination and CRC integrity;
it does not mean a Cisco signature is present, trusted, or installable.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import re
import struct
import zlib


_EOCD = struct.Struct("<4s4H2IH")
_CENTRAL = struct.Struct("<4s6H3I5H2I")
_LOCAL = struct.Struct("<4s5H3I2H")
_NAMESPACES = ("java/", "javax/microedition/", "javax/", "cip/", "com/cisco/", "com/sun/", "sun/")
_CAPABILITIES = {
    "javax/microedition/midlet/": "midlet_api_classes",
    "javax/microedition/lcdui/": "midp_ui_classes",
    "javax/microedition/media/": "mobile_media_api_classes",
    "javax/microedition/io/": "microedition_io_classes",
    "java/net/": "java_network_classes",
    "cip/midp/midletsuite/": "vendor_midlet_installer_classes",
}
_MANIFEST_KEYS = {
    "manifest-version", "microedition-profile", "microedition-configuration",
    "midlet-name", "midlet-version", "midlet-vendor", "midlet-jar-url",
    "midlet-jar-size", "midlet-permissions", "midlet-permissions-opt",
}
_PROFILES = {"MIDP-1.0", "MIDP-2.0", "MIDP-2.1", "MIDP-3.0"}
_CONFIGURATIONS = {"CLDC-1.0", "CLDC-1.1", "CLDC-1.8", "CDC-1.0", "CDC-1.1"}


class _InvalidArchive(ValueError):
    """Internal rejection carrying a fixed reason, never firmware text."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise _InvalidArchive(reason)


def _manifest_hints(payload: bytes) -> dict:
    # Only the main section is relevant. Ignore free text, identifiers, URLs,
    # certificate bodies, signature values, and unknown attributes entirely.
    lines = payload.decode("utf-8", errors="replace").replace("\r\n", "\n").split("\n")
    unfolded: list[str] = []
    for line in lines:
        if not line:
            break
        if line.startswith(" ") and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    fields: dict[str, str] = {}
    for line in unfolded:
        key, sep, value = line.partition(":")
        if sep:
            fields[key.lower()] = value.strip()
    profiles = sorted(_PROFILES.intersection(fields.get("microedition-profile", "").split()))
    configurations = sorted(_CONFIGURATIONS.intersection(fields.get("microedition-configuration", "").split()))
    return {
        "present": True,
        "recognized_attributes": sorted(_MANIFEST_KEYS.intersection(fields)),
        "profiles": profiles,
        "configurations": configurations,
        "midlet_declaration_count": sum(bool(re.fullmatch(r"midlet-[1-9][0-9]*", key)) for key in fields),
        "signature_attribute_present": any(key.startswith(("midlet-certificate-", "midlet-jar-rsa-")) for key in fields),
    }


def _validate(data: bytes, eocd: int, limits: dict, budget: list[int]) -> dict:
    _require(eocd + _EOCD.size <= len(data), "truncated_end_record")
    _, disk, cd_disk, on_disk, count, cd_size, cd_offset, comment_size = _EOCD.unpack_from(data, eocd)
    _require(not disk and not cd_disk and on_disk == count, "multidisk_unsupported")
    _require(count != 0xffff and cd_size != 0xffffffff and cd_offset != 0xffffffff, "zip64_unsupported")
    _require(count <= limits["max_entries"], "entry_limit")
    end = eocd + _EOCD.size + comment_size
    start = eocd - cd_size - cd_offset
    cd_start = eocd - cd_size
    _require(0 <= start <= cd_start <= eocd and end <= len(data), "invalid_archive_extent")
    if count:
        _require(data[start:start + 4] == b"PK\x03\x04", "missing_initial_local_header")
    else:
        _require(cd_size == cd_offset == 0, "invalid_empty_archive")

    entries = []
    names = set()
    position = cd_start
    declared_total = 0
    for _ in range(count):
        _require(position + _CENTRAL.size <= eocd, "truncated_central_directory")
        entry = _CENTRAL.unpack_from(data, position)
        (magic, made_by, needed, flags, method, mtime, mdate, crc, compressed_size,
         size, name_size, extra_size, note_size, entry_disk, internal, external, local_offset) = entry
        _require(magic == b"PK\x01\x02", "invalid_central_directory")
        _require(entry_disk == 0, "multidisk_unsupported")
        _require(0xffffffff not in (compressed_size, size, local_offset), "zip64_unsupported")
        _require(not flags & (1 | 0x40 | 0x2000), "encrypted_unsupported")
        _require(method in (0, 8), "compression_unsupported")
        _require(size <= limits["max_member_bytes"], "member_size_limit")
        declared_total += size
        _require(declared_total <= budget[0], "total_uncompressed_limit")
        name_start = position + _CENTRAL.size
        position = name_start + name_size + extra_size + note_size
        _require(position <= eocd, "truncated_central_directory")
        name = data[name_start:name_start + name_size]
        _require(name and b"\0" not in name, "invalid_member_name")
        _require(name not in names, "duplicate_member_name")
        names.add(name)
        # Avoid even metadata extraction from an archive that encourages unsafe
        # filesystem extraction by a later consumer of the validated bytes.
        _require(not name.startswith((b"/", b"\\")) and b"\\" not in name
                 and b".." not in name.split(b"/") and b":" not in name, "unsafe_member_path")
        _require((external >> 16) & 0xf000 != 0xa000, "symlink_member_unsupported")
        entries.append((name, flags, method, crc, compressed_size, size, start + local_offset))
    _require(position == eocd, "central_directory_size_mismatch")

    spans = []
    payloads = []
    for name, flags, method, crc, compressed_size, size, local in entries:
        _require(start <= local and local + _LOCAL.size <= cd_start, "invalid_local_header_extent")
        (magic, needed, local_flags, local_method, mtime, mdate, local_crc,
         local_compressed_size, local_size, name_size, extra_size) = _LOCAL.unpack_from(data, local)
        _require(magic == b"PK\x03\x04" and flags == local_flags and method == local_method, "local_header_mismatch")
        payload_start = local + _LOCAL.size + name_size + extra_size
        payload_end = payload_start + compressed_size
        _require(payload_end <= cd_start, "invalid_member_extent")
        _require(data[local + _LOCAL.size:local + _LOCAL.size + name_size] == name, "local_name_mismatch")
        if flags & 8:
            # Both signed and unsigned classic 32-bit descriptors are valid.
            descriptor = payload_end
            if data[descriptor:descriptor + 4] == b"PK\x07\x08":
                descriptor += 4
            _require(descriptor + 12 <= cd_start, "truncated_data_descriptor")
            _require(struct.unpack_from("<3I", data, descriptor) == (crc, compressed_size, size), "data_descriptor_mismatch")
            span_end = descriptor + 12
        else:
            _require((local_crc, local_compressed_size, local_size) == (crc, compressed_size, size), "local_size_crc_mismatch")
            span_end = payload_end
        spans.append((local, span_end))
        payloads.append((name, method, crc, size, payload_start, payload_end))
    spans.sort()
    for previous, following in zip(spans, spans[1:]):
        _require(previous[1] <= following[0], "overlapping_members")

    classes = Counter()
    namespaces = Counter()
    packages = set()
    capabilities = set()
    class_entries = invalid_class_headers = signature_entries = 0
    manifest = {"present": False}
    for name_bytes, method, crc, size, payload_start, payload_end in payloads:
        budget[0] -= size  # Count all attempted output, including failed CRCs.
        compressed = data[payload_start:payload_end]
        if method == 0:
            _require(len(compressed) == size, "stored_size_mismatch")
            payload = compressed
        else:
            decoder = zlib.decompressobj(-15)
            try:
                payload = decoder.decompress(compressed, size + 1)
            except zlib.error:
                raise _InvalidArchive("deflate_error") from None
            _require(len(payload) == size and decoder.eof and not decoder.unused_data
                     and not decoder.unconsumed_tail, "deflate_size_or_termination_mismatch")
        _require(zlib.crc32(payload) & 0xffffffff == crc, "member_crc_mismatch")
        name = name_bytes.decode("utf-8", errors="replace")
        upper_name = name.upper()
        if upper_name == "META-INF/MANIFEST.MF":
            if len(payload) <= 65536:
                manifest = _manifest_hints(payload)
            else:
                manifest = {"present": True, "metadata_skipped": "manifest_size_limit"}
        if upper_name.startswith("META-INF/") and upper_name.endswith((".SF", ".RSA", ".DSA", ".EC")):
            signature_entries += 1
        if name.endswith(".class"):
            class_entries += 1
            packages.add(name.rpartition("/")[0])
            for prefix in _NAMESPACES:
                if name.startswith(prefix):
                    namespaces[prefix.rstrip("/").replace("/", ".")] += 1
                    break
            else:
                namespaces["other"] += 1
            for prefix, capability in _CAPABILITIES.items():
                if name.startswith(prefix):
                    capabilities.add(capability)
            if len(payload) >= 8 and payload[:4] == b"\xca\xfe\xba\xbe":
                minor, major = struct.unpack_from(">HH", payload, 4)
                classes[f"{major}.{minor}"] += 1
            else:
                invalid_class_headers += 1

    return {
        "offset": start, "end_offset": end, "size": end - start,
        "sha256": hashlib.sha256(data[start:end]).hexdigest(),
        "entry_count": count, "uncompressed_bytes": declared_total,
        "class_entry_count": class_entries, "class_versions": dict(sorted(classes.items())),
        "invalid_class_header_count": invalid_class_headers, "package_count": len(packages),
        "namespace_class_counts": dict(sorted(namespaces.items())),
        "capability_hints": sorted(capabilities), "manifest": manifest,
        "signature_entry_count": signature_entries,
        "integrity": "zip_structure_sizes_and_crc_verified",
        "signature_verification": "not_performed",
        "installability": "not_established",
    }


def inspect_java(data: bytes, *, max_archives: int = 64, max_entries: int = 8192,
                 max_member_bytes: int = 8 * 1024 * 1024,
                 max_total_uncompressed_bytes: int = 64 * 1024 * 1024,
                 max_input_bytes: int = 128 * 1024 * 1024,
                 max_candidates: int = 512) -> dict:
    """Return JSON-safe metadata for validated embedded classic ZIP archives.

    Limits apply to one call; decompression output budget is shared across all
    candidates, including failed attempts. ZIP64, encryption, unusual methods,
    symlinks and traversal paths are deliberately outside this bounded gate.
    Class versions inspect only the 8-byte header, not bytecode validity.
    Invalid candidates are summarized with fixed reason codes and offsets.
    """
    limits = {"max_archives": max_archives, "max_entries": max_entries,
              "max_member_bytes": max_member_bytes,
              "max_total_uncompressed_bytes": max_total_uncompressed_bytes,
              "max_input_bytes": max_input_bytes, "max_candidates": max_candidates}
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in limits.values()):
        raise ValueError("all limits must be positive integers")
    report = {"archives": [], "rejected_candidates": [], "candidates_examined": 0,
              "limits": limits, "scan_complete": True}
    if len(data) > max_input_bytes:
        report.update(scan_complete=False, stop_reason="input_size_limit")
        return report
    budget = [max_total_uncompressed_bytes]
    search_at = 0
    while (eocd := data.find(b"PK\x05\x06", search_at)) != -1:
        if report["candidates_examined"] >= max_candidates or len(report["archives"]) >= max_archives:
            report.update(scan_complete=False, stop_reason="candidate_or_archive_limit")
            break
        search_at = eocd + 4
        report["candidates_examined"] += 1
        try:
            archive = _validate(data, eocd, limits, budget)
            # Reject ambiguous overlapping enclosing archives. Disjoint archives
            # are the expected firmware packaging case; nesting is not recursed.
            _require(all(archive["end_offset"] <= prior["offset"] or archive["offset"] >= prior["end_offset"]
                         for prior in report["archives"]), "overlapping_archives")
            report["archives"].append(archive)
        except _InvalidArchive as error:
            report["rejected_candidates"].append({"end_record_offset": eocd, "reason": str(error)})
    report["decompression_budget_used"] = max_total_uncompressed_bytes - budget[0]
    return report


def iter_validated_archives(data: bytes, **limits):
    """Yield ``(metadata, exact_archive_bytes)`` without writing or executing."""
    for archive in inspect_java(data, **limits)["archives"]:
        yield archive, data[archive["offset"]:archive["end_offset"]]
