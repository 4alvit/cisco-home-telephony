#!/usr/bin/env python3
"""CI-only AGI fixture: real worker/AudioSocket, synthetic MP3, no Internet."""
import importlib.util
import io
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location("real_radio", "/var/lib/asterisk/agi-bin/radio.py")
radio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(radio)
instances = []

class Source:
    def close(self):
        pass

class FixtureCall(radio.Call):
    def __init__(self, *args, **kwargs):
        data = Path("/var/lib/asterisk/radio-fixture.mp3").read_bytes()
        super().__init__(*args, **kwargs, opener=lambda _station: (Source(), io.BytesIO(data)))
        instances.append(self)

radio.Call = FixtureCall
try:
    radio.main()
finally:
    Path("/tmp/radio-fixture.json").write_text(json.dumps({
        "calls": len(instances),
        "frames": sum(x.frames for x in instances),
        "decoder_reaped": all(x.proc is not None and x.proc.poll() is not None for x in instances),
    }))
