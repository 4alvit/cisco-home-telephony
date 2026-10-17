#!/usr/bin/env python3
"""List the curated stations, or explicitly test bounded public audio playback."""
import argparse
import importlib.util
import json
from pathlib import Path
import socket
import struct
import threading
import uuid

spec = importlib.util.spec_from_file_location("radio_probe", Path(__file__).resolve().parents[1] / "containers/asterisk/radio.py")
radio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(radio)


def probe(name):
    token = uuid.uuid4()
    call = radio.Call(radio.catalog()[name], 4, token)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0)); listener.listen(1); listener.settimeout(5)
    thread = threading.Thread(target=call.serve, args=(listener,), daemon=True)
    thread.start()
    frames = peak = 0
    try:
        with socket.create_connection(listener.getsockname(), timeout=20) as conn:
            conn.sendall(b"\x01\x00\x10" + token.bytes)
            while True:
                kind, data = radio.receive_frame(conn)
                if kind == 0:
                    break
                if kind != 0x10 or len(data) != 320:
                    raise ValueError("unexpected audio frame")
                frames += 1
                peak = max(peak, max(abs(x) for x in struct.unpack("<160h", data)))
    except (OSError, EOFError, ValueError):
        pass
    finally:
        call.close(); listener.close(); thread.join(timeout=3)
    decoder_reaped = call.proc is not None and call.proc.poll() is not None
    worker_stopped = not thread.is_alive()
    return {"station": name, "decoded_frames": frames, "peak": peak,
            "decoder_reaped": decoder_reaped, "worker_stopped": worker_stopped,
            "passed": frames >= 25 and peak > 0 and decoder_reaped and worker_stopped}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--all", action="store_true", help="test every station; contacts public broadcasters")
    mode.add_argument("--station", choices=radio.catalog(), help="test one public stream")
    args = parser.parse_args()
    if not args.all and not args.station:
        for name, station in radio.catalog().items():
            print(f"{station['extension']} {name}: {station['name']}")
        return 0
    results = [probe(name) for name in (radio.catalog() if args.all else [args.station])]
    print(json.dumps(results, indent=2))
    return 0 if all(x["passed"] for x in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
