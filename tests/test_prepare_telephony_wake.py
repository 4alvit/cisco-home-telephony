"""Offline PCMU corpus preparation: exact samples, annotations, and privacy."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import wave

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prepare_telephony_wake_test", ROOT / "scripts/prepare_telephony_wake.py")
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


def wav_bytes(pcm):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(pcm)
    return stream.getvalue()


class CodecRoundtripTests(unittest.TestCase):
    def test_known_mu_law_levels_and_interpolation(self):
        # All adjacent input pairs are equal, making the intended 8 kHz
        # signal independent of the averaging implementation.
        pcm = struct.pack("<8h", 0, 0, -32124, -32124, 32124, 32124, 0, 0)
        result, wire_hash, padding = prepare.codec_roundtrip(pcm)
        self.assertEqual(struct.unpack("<8h", result),
                         (0, -16062, -32124, 0, 32124, 16062, 0, 0))
        expected_payload = bytes((0xff, 0x00, 0x80, 0xff)) + b"\xff" * 156
        self.assertEqual(wire_hash, hashlib.sha256(expected_payload).hexdigest())
        self.assertEqual(padding, 312)

    def test_whole_duration_including_odd_sample_tail_is_preserved(self):
        for samples in (1, 2, 319, 320, 321, 640, 643):
            with self.subTest(samples=samples):
                pcm = bytes(samples * 2)
                converted, digest, padding = prepare.codec_roundtrip(pcm)
                self.assertEqual(converted, pcm)
                self.assertEqual(padding, (-samples) % 320)
                self.assertEqual(len(digest), 64)

    def test_unequal_pairs_are_averaged_as_offline_preparation(self):
        # The preparation deliberately removes a Nyquist-frequency alternating
        # signal. The outgoing RtpPCMSink instead selects even samples at 16 kHz;
        # only the codec and inbound upsampler are shared with this fixture.
        pcm = struct.pack("<320h", *([32124, -32124] * 160))
        converted, digest, padding = prepare.codec_roundtrip(pcm)
        self.assertEqual(converted, bytes(640))
        self.assertEqual(digest, hashlib.sha256(b"\xff" * 160).hexdigest())
        self.assertEqual(padding, 0)

    def test_no_state_leaks_between_calls_or_frames(self):
        loud = struct.pack("<320h", *([32124] * 320))
        result, _, _ = prepare.codec_roundtrip(loud + bytes(640))
        self.assertEqual(result[:640], loud)
        self.assertEqual(result[640:], bytes(640))
        self.assertEqual(prepare.codec_roundtrip(bytes(640))[0], bytes(640))

    def test_rejects_partial_or_mutable_samples(self):
        for invalid in (b"x", bytearray(640), None):
            with self.subTest(kind=type(invalid).__name__), self.assertRaises(ValueError):
                prepare.codec_roundtrip(invalid)


class CorpusTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name).resolve()
        self.private = self.workspace / ".local"
        self.private.mkdir(mode=0o700)
        self.source = self.private / "source"
        self.source.mkdir(mode=0o700)
        self.manifest = self.source / "manifest.json"
        self.output = self.private / "converted"
        self.pcm = bytes(64000 + 6)  # Two seconds and three samples.
        (self.source / "source.wav").write_bytes(wav_bytes(self.pcm))
        (self.source / "source.wav").chmod(0o600)
        self.row = {"id": "opaque-001", "path": "source.wav", "label": "positive",
                    "domain": "speech", "label_source": "recorded",
                    "expected_events": [{"start_seconds": 1.3, "end_seconds": 1.4}]}
        self.write_manifest()

    def write_manifest(self):
        self.manifest.write_text(json.dumps({"format_version": 1, "clips": [self.row]}))
        self.manifest.chmod(0o600)

    def run_prepare(self, **kwargs):
        return prepare.prepare(self.manifest, workspace=self.workspace,
                               output=self.output, late_tolerance_seconds=.2, **kwargs)

    def test_ready_for_evaluator_preserves_annotations_and_provenance(self):
        with mock.patch("socket.socket", side_effect=AssertionError("No network allowed")):
            summary = self.run_prepare()
        result = json.loads((self.output / "manifest.json").read_text())
        converted = result["clips"][0]
        self.assertEqual(converted, {**self.row, "path": "audio/opaque-001.wav"})
        clips, _ = prepare.evaluation._load_inputs(
            self.output / "manifest.json", self.private, .2)
        self.assertEqual(clips[0]["pcm"], self.pcm)
        self.assertEqual(summary["clips"], 1)
        self.assertFalse(summary["model_inference"])
        self.assertNotIn("opaque-001", json.dumps(summary))
        provenance = json.loads((self.output / "provenance.private.json").read_text())
        self.assertEqual(provenance["clips"][0]["samples"], len(self.pcm) // 2)
        self.assertEqual(provenance["input_manifest_sha256"],
                         hashlib.sha256(self.manifest.read_bytes()).hexdigest())
        for path in (self.output, self.output / "audio"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        for path in self.output.rglob("*"):
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_existing_output_rejected_without_rewriting(self):
        self.run_prepare()
        before = (self.output / "manifest.json").read_bytes()
        with self.assertRaises(ValueError):
            self.run_prepare()
        self.assertEqual((self.output / "manifest.json").read_bytes(), before)

    def test_validation_before_output_creation(self):
        self.row["expected_events"] = [{"start_seconds": 0, "end_seconds": .5}]
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "scored coverage"):
            self.run_prepare()
        self.assertFalse(self.output.exists())

    def test_public_and_symlinked_inputs_are_rejected(self):
        wav = self.source / "source.wav"
        wav.chmod(0o644)
        with self.assertRaises(ValueError):
            self.run_prepare()
        self.assertFalse(self.output.exists())
        wav.chmod(0o600)
        link = self.source / "link.wav"
        link.symlink_to(wav)
        self.row["path"] = "link.wav"
        self.write_manifest()
        with self.assertRaises(ValueError):
            self.run_prepare()
        self.assertFalse(self.output.exists())

    def test_output_cannot_escape_private_directory(self):
        self.output = self.workspace / "public"
        with self.assertRaises(ValueError):
            self.run_prepare()
        self.assertFalse(self.output.exists())

    def test_unlabeled_and_negative_sources_are_never_reclassified(self):
        for label in ("unlabeled", "negative"):
            self.row.update(label=label, expected_events=[])
            self.write_manifest()
            self.output = self.private / label
            self.run_prepare()
            actual = json.loads((self.output / "manifest.json").read_text())["clips"][0]
            self.assertEqual(actual["label"], label)
            self.assertEqual(actual["expected_events"], [])


if __name__ == "__main__":
    unittest.main()
