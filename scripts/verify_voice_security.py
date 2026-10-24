"""Fail closed on actionable runtime findings in a complete Trivy JSON inventory.

Every HIGH/CRITICAL language-package finding blocks delivery, including vendored
packages found through embedded SBOMs. HIGH/CRITICAL OS findings block delivery
when the distributor provides a fix. Unfixed OS findings remain in the full
report and in the printed summary; passing this gate is not a zero-CVE claim.
"""

import argparse
import json
from collections import Counter
from pathlib import Path

EXACT_PACKAGES = {'libbrotli1': ('1.0.9', '2+deb12u1'),
                  'libexpat1': ('2.5.0', '1+deb12u5'),
                  'libssh-gcrypt-4': ('0.10.6', '0+deb12u3')}
HIGH_SEVERITIES = {"HIGH", "CRITICAL"}
SEVERITIES = {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"}


def verify(report):
    if (not isinstance(report, dict) or report.get("SchemaVersion") != 2
            or report.get("ArtifactType") != "container_image"):
        raise ValueError("Expected a complete Trivy v2 container-image report")
    results = report.get("Results")
    if not isinstance(results, list) or not results:
        raise ValueError("Missing image package results")
    inventory = set()
    libraries = {name: [] for name in EXACT_PACKAGES}
    blocked = []
    severity_counts = Counter()
    unfixed_os = []
    for result in results:
        if not isinstance(result, dict) or result.get("Class") not in {"os-pkgs", "lang-pkgs"}:
            raise ValueError("Unrecognized package result class")
        kind = result["Class"]
        packages = result.get("Packages")
        if not isinstance(packages, list) or not packages:
            raise ValueError("Missing complete package inventory: use --list-all-pkgs")
        inventory.add((kind, result.get("Type")))
        for package in packages:
            if (not isinstance(package, dict) or not package.get("Name")
                    or not package.get("Version")):
                raise ValueError("Malformed package inventory")
            if kind == "os-pkgs" and package["Name"] in EXACT_PACKAGES:
                libraries[package["Name"]].append(package)
            if kind == "lang-pkgs" and package["Name"].lower() == "pip":
                blocked.append("pip must not be present in the final runtime")
        findings = result.get("Vulnerabilities", [])
        if not isinstance(findings, list):
            raise ValueError("Malformed vulnerability list")
        for finding in findings:
            if (not isinstance(finding, dict) or not finding.get("VulnerabilityID")
                    or not finding.get("PkgName") or finding.get("Severity") not in SEVERITIES):
                raise ValueError("Malformed vulnerability finding")
            severity = finding["Severity"]
            severity_counts[severity] += 1
            label = f'{finding["VulnerabilityID"]}: {finding["PkgName"]} ({severity})'
            if severity in HIGH_SEVERITIES:
                if kind == "lang-pkgs" or str(finding.get("FixedVersion") or "").strip():
                    blocked.append(label)
                else:
                    unfixed_os.append(label)
    if not {("os-pkgs", "debian"), ("lang-pkgs", "python-pkg")} <= inventory:
        raise ValueError("Both Debian and Python package inventories are required")
    for name, expected in EXACT_PACKAGES.items():
        packages = libraries[name]
        if (len(packages) != 1 or
                (packages[0]["Version"], packages[0].get("Release")) != expected):
            blocked.append(f"Expected exactly {name} {expected[0]}-{expected[1]}")
    return {
        "passed": not blocked,
        "blocking_findings": sorted(blocked),
        "all_severity_counts": dict(sorted(severity_counts.items())),
        "unfixed_os_high_critical": sorted(unfixed_os),
        "scope": "All language HIGH/CRITICAL; fixed OS HIGH/CRITICAL; exact Debian security package versions",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        result = verify(json.loads(args.report.read_text()))
    except (OSError, ValueError) as error:
        parser.exit(1, f"Invalid container security evidence: {error}\n")
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
