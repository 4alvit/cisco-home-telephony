"""Atheris entry point for offline SIP request and video offer validation."""
import sys
from pathlib import Path

import atheris

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
with atheris.instrument_imports():
    from fuzz.contracts import fuzz_signaling

atheris.Setup(sys.argv, fuzz_signaling)
atheris.Fuzz()
