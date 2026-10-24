"""Replay committed protocol regressions without starting timed mutation."""
from pathlib import Path
import unittest

from fuzz.contracts import fuzz_protocol, fuzz_signaling


class FuzzContractTests(unittest.TestCase):
    def test_committed_protocol_inputs(self):
        root = Path(__file__).resolve().parents[1] / "fuzz" / "corpus"
        for name, target in (("protocol", fuzz_protocol), ("signaling", fuzz_signaling)):
            seeds = sorted((root / name).iterdir())
            self.assertTrue(seeds)
            for seed in seeds:
                with self.subTest(target=name, seed=seed.name):
                    target(seed.read_bytes())
            for value in range(256):
                with self.subTest(target=name, byte=value):
                    target(bytes([value]) * (value % 48))
