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
import re

# Exact Debian 13 packages from snapshot 20261101T000000Z, with the
# DSA-6613-1 zlib repair from security snapshot 20261107T160000Z. Epochs
# are separate Trivy fields; checking only Version/Release would miss them.
EXACT_PACKAGES = {'libbrotli1': ('1.1.0', '2+deb13u1'),
                  'libexpat1': ('2.9.0', '1~deb13u1'),
                  'libssh-4': ('0.11.5', '0+deb13u1'),
                  'libssl3t64': ('3.5.7', '1~deb13u3'),
                  'libmbedcrypto16': ('3.6.6', '0.1~deb13u1'),
                  'ffmpeg': ('7.1.5', '0+deb13u1'),
                  'zlib1g': ('1.3.dfsg+really1.3.1', '1+deb13u1')}
EXACT_EPOCHS = {'ffmpeg': 7, 'zlib1g': 1}
PROFILES = ('voice', 'asterisk')
PBX_PILLOW_VERSION = '13.0.0'
HIGH_SEVERITIES = {"HIGH", "CRITICAL"}
SEVERITIES = {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"}


def verify(report, *, profile="voice"):
    if profile not in PROFILES:
        raise ValueError("Unknown image security profile")
    if (not isinstance(report, dict) or report.get("SchemaVersion") != 2
            or report.get("ArtifactType") != "container_image"):
        raise ValueError("Expected a complete Trivy v2 container-image report")
    metadata = report.get("Metadata")
    operating_system = metadata.get("OS") if isinstance(metadata, dict) else None
    if (not isinstance(operating_system, dict) or operating_system.get("Family") != "debian"
            or not isinstance(operating_system.get("Name"), str)
            or not re.fullmatch(r"13(?:\.[0-9]+)*", operating_system["Name"])
            or operating_system.get("EOSL") is True):
        raise ValueError("Reviewed Debian 13 runtime required; EOL/unknown OS evidence is rejected")
    results = report.get("Results")
    if not isinstance(results, list) or not results:
        raise ValueError("Missing image package results")
    inventory = set()
    package_counts = Counter()
    libraries = {name: [] for name in EXACT_PACKAGES}
    blocked = []
    pillow_packages = []
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
        package_counts[kind] += len(packages)
        for package in packages:
            if (not isinstance(package, dict) or not isinstance(package.get("Name"), str)
                    or not package["Name"] or not isinstance(package.get("Version"), str)
                    or not package["Version"]):
                raise ValueError("Malformed package inventory")
            if kind == "os-pkgs" and (package["Name"].startswith("libmbed")
                    or package.get("SrcName") == "mbedtls") and package["Version"].startswith("2."):
                blocked.append("EOL Mbed TLS 2.x must not be present: " + package["Name"])
            if profile == "asterisk":
                if kind == "lang-pkgs" and package["Name"].lower() == "pillow":
                    pillow_packages.append(package)
                if kind == "os-pkgs" and (package["Name"] in {"python3-pil", "python3-pil.imagetk"}
                        or package.get("SrcName") == "pillow"):
                    blocked.append("Unreviewed Debian Pillow copy must not be present: " + package["Name"])
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
    required = {("os-pkgs", "debian"), ("lang-pkgs", "python-pkg")}
    if not required <= inventory:
        raise ValueError("Missing required package inventories for " + profile)
    if profile == "asterisk" and (len(pillow_packages) != 1
            or pillow_packages[0]["Version"] != PBX_PILLOW_VERSION):
        blocked.append("Expected exactly one upstream Pillow " + PBX_PILLOW_VERSION)
    for name, expected in EXACT_PACKAGES.items():
        packages = libraries[name]
        if (len(packages) != 1 or
                (packages[0]["Version"], packages[0].get("Release")) != expected
                or packages[0].get("Epoch", 0) != EXACT_EPOCHS.get(name, 0)):
            blocked.append(f"Expected exactly {name} {expected[0]}-{expected[1]}")
    return {
        "passed": not blocked,
        "profile": profile,
        "operating_system": operating_system["Name"],
        "package_counts": dict(sorted(package_counts.items())),
        "blocking_findings": sorted(blocked),
        "all_severity_counts": dict(sorted(severity_counts.items())),
        "unfixed_os_high_critical": sorted(unfixed_os),
        "scope": "All language HIGH/CRITICAL; fixed OS HIGH/CRITICAL; exact Debian 13 security package versions; no EOL Mbed TLS 2.x",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--profile", choices=PROFILES, default="voice")
    args = parser.parse_args()
    try:
        result = verify(json.loads(args.report.read_text()), profile=args.profile)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Invalid container security evidence: {error}\n")
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
