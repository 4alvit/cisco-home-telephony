"""Run the finite loopback SIP video laboratory: no external bind option."""
import argparse
import json
from pathlib import Path

from tools.firmware_lab.storage import create_private_output
from .demo import SCENARIOS, dependencies, run_demo


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe", help="check local FFmpeg encoder/decoder availability")
    demo = sub.add_parser("demo", help="exercise actual loopback SIP and H264 RTP")
    demo.add_argument("--output", required=True, help="fresh directory below repository .local/")
    demo.add_argument("--scenario", choices=(*SCENARIOS, "all"), default="all")
    args = parser.parse_args(argv)
    try:
        if args.command == "probe":
            result = dependencies()
            print(json.dumps(result, sort_keys=True))
            return 0 if all(result.values()) else 2
        output = create_private_output(Path(args.output))
        scenarios = SCENARIOS if args.scenario == "all" else (args.scenario,)
        outcomes = []
        for scenario in scenarios:
            directory = output / scenario
            directory.mkdir(mode=0o700)
            result = run_demo(directory, scenario)
            outcomes.append({"scenario": scenario, "success": result["success"],
                             "decoded_frames": (result["media"] or {}).get("decoded_frames", 0)})
        print(json.dumps({"success": True, "results": outcomes}, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError):
        # Keep input payloads and native-tool diagnostics out of public logs.
        print(json.dumps({"success": False, "error": "sip_video_lab_failed",
                          "automatic_retry": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
