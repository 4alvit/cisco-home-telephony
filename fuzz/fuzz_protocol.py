"""Atheris entry point for bounded SDP, RTP and AudioSocket parsing."""
import sys
from pathlib import Path

import atheris

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
with atheris.instrument_imports():
    from fuzz.contracts import fuzz_protocol

atheris.Setup(sys.argv, fuzz_protocol)
atheris.Fuzz()
