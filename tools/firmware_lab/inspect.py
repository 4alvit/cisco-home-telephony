"""Deterministic metadata and optional private carving of local firmware bytes.

The signed SBN container is not authenticated here. Embedded byte structures
are examined without loading them as code. No original files are written.
"""

import hashlib
from itertools import islice
import json
from pathlib import Path
import re

from . import SCHEMA_VERSION
from . import binary, java
from .reference import COMPONENTS, LOAD_ID
from .storage import REPOSITORY, create_private_output, read_regular, write_private


MAX_INPUT_FILES = 32
MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_GZIP_DECOMPRESSION_BYTES = 128 * 1024 * 1024
MAX_JAR_DECOMPRESSION_BYTES = 128 * 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def inspect_directory(directory, output, *, extract=False, repository=REPOSITORY):
    """Inspect a flat firmware set and write a fresh private report directory.

    Embedded ELF extents are carved only when their declared tables fit. ZIPs
    must pass the Java parser's complete validation. Gzip is expanded one level
    with a global budget. Parsed/carved does not mean runnable or authenticated.
    """
    source = Path(directory)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("input must be a regular directory, not a symlink")
    paths = sorted(islice(source.iterdir(), MAX_INPUT_FILES + 1))
    if not paths or len(paths) > MAX_INPUT_FILES:
        raise ValueError("input must contain between 1 and 32 firmware files")
    inputs = []
    total_input = 0
    for path in paths:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.(sbn|loads)", path.name):
            raise ValueError("input directory must contain only flat .sbn/.loads files")
        data = read_regular(path)
        total_input += len(data)
        if total_input > MAX_INPUT_BYTES:
            raise ValueError("firmware set exceeds total input byte limit")
        inputs.append((path, data))

    target = create_private_output(output, repository)
    report = {
        "schema_version": SCHEMA_VERSION,
        "scope": "offline byte inspection only",
        "reference_load_id": LOAD_ID,
        "vendor_signature_verified": False,
        "native_execution_proven": False,
        "midlet_deployment_proven": False,
        "limits": {
            "max_input_files": MAX_INPUT_FILES,
            "max_input_bytes": MAX_INPUT_BYTES,
            "max_expanded_bytes": MAX_EXPANDED_BYTES,
            "max_artifact_bytes": MAX_ARTIFACT_BYTES,
            "max_gzip_decompression_bytes": MAX_GZIP_DECOMPRESSION_BYTES,
            "max_jar_decompression_bytes": MAX_JAR_DECOMPRESSION_BYTES,
            "gzip_recursion_depth": 1,
        },
        "components": [],
        "artifacts": [],
    }
    expanded_remaining = MAX_EXPANDED_BYTES
    artifact_remaining = MAX_ARTIFACT_BYTES
    gzip_work_remaining = MAX_GZIP_DECOMPRESSION_BYTES
    jar_work_remaining = MAX_JAR_DECOMPRESSION_BYTES

    def artifact(payload, kind, component, node, offset):
        nonlocal artifact_remaining
        if not extract:
            return
        if len(payload) > artifact_remaining:
            raise ValueError("total extracted artifact byte limit exceeded")
        artifact_remaining -= len(payload)
        name = f"{len(report['artifacts']):04d}.{kind}"
        write_private(target, name, payload)
        report["artifacts"].append({
            "file": name, "kind": kind, "component": component,
            "node": node, "offset": offset, "size": len(payload),
            "sha256": digest(payload),
        })

    def scan(data, component, node, *, expand):
        nonlocal expanded_remaining, gzip_work_remaining, jar_work_remaining
        # Both parsers return bounded observations; raw firmware strings stay
        # inside the private report. Reserve enough work for the second pass
        # which retrieves accepted gzip payloads, rather than scanning twice
        # under an uncharged budget. Nested gzip is not scanned or recursed.
        gzip_budget = min(expanded_remaining, gzip_work_remaining // 2)
        include_gzip = expand and gzip_budget > 0
        native = binary.inspect_binary(
            data, max_uncompressed_size=max(1, gzip_budget),
            max_total_uncompressed_size=max(1, gzip_budget),
            include_gzip=include_gzip,
        )
        gzip_work_remaining -= sum(
            item.get("uncompressed_size", item.get("expansion_budget_charged", 0))
            for item in native["gzip"]
        )
        if not include_gzip:
            native["gzip_skip_reason"] = "recursion_limit" if not expand else "global_budget_limit"
        if jar_work_remaining > 0:
            java_report = java.inspect_java(data, max_total_uncompressed_bytes=jar_work_remaining)
            jar_work_remaining -= java_report.get("decompression_budget_used", 0)
        else:
            java_report = {"archives": [], "scan_complete": False,
                           "stop_reason": "global_decompression_limit"}
        result = {"node": node, "size": len(data), "sha256": digest(data),
                  "binary": native, "java": java_report, "expanded": []}
        for elf in native["elf"]:
            if elf.get("status") == "bounded":
                start = elf["offset"]
                artifact(data[start:start + elf["span_size"]], "elf", component, node, start)
        if extract:
            for meta in java_report["archives"]:
                artifact(data[meta["offset"]:meta["end_offset"]], "zip", component, node, meta["offset"])
        if expand:
            for gz in native["gzip"]:
                if gz.get("status") != "bounded":
                    continue
                if gz["uncompressed_size"] > min(expanded_remaining, gzip_work_remaining):
                    result["expanded"].append({"status": "limit", "offset": gz["offset"]})
                    continue
                payload, _ = binary.decompress_gzip_at(
                    data, gz["offset"], max_output_size=max(1, min(expanded_remaining, gzip_work_remaining)))
                expanded_remaining -= len(payload)
                gzip_work_remaining -= len(payload)
                artifact(payload, "bin", component, node, gz["offset"])
                child = scan(payload, component, f"gzip@{gz['offset']}", expand=False)
                child["parent_offset"] = gz["offset"]
                result["expanded"].append(child)
        return result

    for path, data in inputs:
        expected = COMPONENTS.get(path.name)
        report["components"].append({
            "name": path.name,
            "matches_reference": expected == (len(data), digest(data)),
            "inspection": scan(data, path.name, "source", expand=True),
        })
    report["complete_reference_set"] = (
        {path.name for path, _ in inputs} == set(COMPONENTS)
        and all(item["matches_reference"] for item in report["components"])
    )
    # This re-read detects source changes during the run without opening any
    # input for writing; a failed run never reports unchanged inputs.
    for path, data in inputs:
        if digest(read_regular(path)) != digest(data):
            raise ValueError("source changed during inspection")
    report["inputs_unchanged"] = True
    report["budgets_used"] = {
        "expanded_bytes": MAX_EXPANDED_BYTES - expanded_remaining,
        "gzip_decompression_bytes": MAX_GZIP_DECOMPRESSION_BYTES - gzip_work_remaining,
        "jar_decompression_bytes": MAX_JAR_DECOMPRESSION_BYTES - jar_work_remaining,
        "artifact_bytes": MAX_ARTIFACT_BYTES - artifact_remaining,
    }
    write_private(target, "report.json", (json.dumps(report, indent=2, sort_keys=True) + "\n").encode())
    return report
