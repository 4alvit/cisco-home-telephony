"""CLI entry point: python3 -m tools.firmware_lab."""

import argparse
import json
import sys

from .inspect import inspect_directory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, help="local flat .sbn/.loads directory")
    parser.add_argument("--output", required=True, help="fresh directory below repository .local/")
    parser.add_argument("--extract", action="store_true", help="also carve validated extents into private non-executable files")
    args = parser.parse_args(argv)
    try:
        report = inspect_directory(args.directory, args.output, extract=args.extract)
    except (OSError, ValueError) as exc:
        print(f"Firmware inspection stopped: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "report": str(args.output) + "/report.json",
        "components": len(report["components"]),
        "artifacts": len(report["artifacts"]),
        "complete_reference_set": report["complete_reference_set"],
        "inputs_unchanged": report["inputs_unchanged"],
        "vendor_signature_verified": False,
        "native_execution_proven": False,
        "midlet_deployment_proven": False,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
