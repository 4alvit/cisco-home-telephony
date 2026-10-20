"""Plan or explicitly run the isolated L16 interoperability laboratory."""
import argparse
import json
from pathlib import Path

from tools.firmware_lab.storage import create_private_output, write_private


FAULTS = ("clean", "byteorder", "payload-type", "clock", "pacing", "drop", "reorder")


def plan():
    return {
        "mode": "plan", "executed": False,
        "scope": "synthetic data; disposable network-none Asterisk; no physical phones",
        "interfaces": {"rtp": "L16/16000/1, negotiated dynamic payload, big-endian PCM16",
                       "audiosocket": "type 0x12, little-endian PCM16 at 16000 Hz",
                       "g722": "16000 Hz audio with an 8000 Hz RTP clock"},
        "cases": ["incoming L16 PT102", "incoming L16 alternate dynamic PT",
                  "outgoing L16 using offered PT", "incoming G722 fallback",
                  "outgoing G722 fallback"],
        "requirements": ["Python 3.10+", "explicit local Docker context or Unix socket",
                         "pre-existing compatible Asterisk image; no automatic pulls"],
        "examples": ["python3 -m tools.l16_lab offline --output .local/l16/offline-1",
                     "python3 -m tools.l16_lab asterisk --image IMAGE --docker-context LAB_CONTEXT",
                     "python3 -m tools.l16_lab asterisk --image IMAGE --docker-context LAB_CONTEXT --execute --output .local/l16/asterisk-1"],
    }


def offline():
    from .protocol import analyze_rtp, fixture_packets
    results = {}
    for fault in FAULTS:
        fixture = fixture_packets(fault=fault)
        report = analyze_rtp(fixture.observations, fixture.codec,
                             expected_samples=fixture.expected_samples)
        expected_ok = fault == "clean"
        if report["ok"] != expected_ok:
            raise RuntimeError("offline fault fixture did not produce its expected result")
        results[fault] = report
    fixture = fixture_packets(encoding="G722", payload_type=9)
    g722 = analyze_rtp(fixture.observations, fixture.codec)
    if not g722["ok"]:
        raise RuntimeError("G722 clock fixture failed")
    return {"mode": "offline", "status": "PASS", "network_used": False,
            "scope": "synthetic protocol diagnostics; no Asterisk or codec decoding",
            "faults": results, "g722_geometry": g722}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("plan", help="show scope and examples; never contact Docker")
    off = sub.add_parser("offline", help="check deterministic synthetic faults without networking")
    off.add_argument("--output", required=True)
    pbx = sub.add_parser("asterisk", help="plan a disposable PBX test; add --execute to run")
    pbx.add_argument("--image")
    engine = pbx.add_mutually_exclusive_group()
    engine.add_argument("--docker-context")
    engine.add_argument("--docker-socket")
    pbx.add_argument("--execute", action="store_true")
    pbx.add_argument("--output")
    args = parser.parse_args(argv)
    if args.command in (None, "plan") or (args.command == "asterisk" and not args.execute):
        print(json.dumps(plan(), indent=2, sort_keys=True))
        return 0
    if args.command == "asterisk":
        if not args.image or not (args.docker_context or args.docker_socket) or not args.output:
            parser.error("--execute requires --image, one explicit local Docker engine, and --output")
    try:
        destination = create_private_output(Path(args.output))
        if args.command == "offline":
            report = offline()
        else:
            from .asterisk import run_isolated
            report = run_isolated(args.image, docker_context=args.docker_context,
                                  docker_socket=args.docker_socket)
        write_private(destination, "report.json", (json.dumps(report, indent=2, sort_keys=True) + "\n").encode())
        print(json.dumps({"status": report["status"], "mode": args.command,
                          "report": str(destination / "report.json")}, sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(json.dumps({"status": "FAIL", "error": str(error), "automatic_retry": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
