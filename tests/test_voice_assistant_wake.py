"""Synthetic frozen-frontend contract tests; no model weights or devices."""

import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from voice_assistant.audio import HOP_BYTES, HOP_SAMPLES, pcm_duration, validate_pcm16le
from voice_assistant.service import AssistantService, ServiceSettings
from voice_assistant.wake import FRONTEND_ID, FrozenOnnxWakeDetector, WakeModelError

try:
    import numpy as np
except ImportError:
    np = None


class PCMTests(unittest.TestCase):
    def test_complete_immutable_samples_and_duration(self):
        self.assertEqual(validate_pcm16le(b""), b"")
        self.assertEqual(validate_pcm16le(b"\x00\x80\xff\x7f"), b"\x00\x80\xff\x7f")
        self.assertEqual(pcm_duration(bytes(HOP_BYTES)), .08)
        for bad in (bytearray(2), memoryview(bytes(2)), [0, 1], "00", None):
            with self.subTest(value=type(bad).__name__), self.assertRaises(TypeError):
                validate_pcm16le(bad)
        with self.assertRaises(ValueError):
            validate_pcm16le(b"x")

    def test_import_does_not_import_optional_runtime(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([
            sys.executable, "-c",
            "import sys; import voice_assistant.wake; "
            "assert 'numpy' not in sys.modules; assert 'onnxruntime' not in sys.modules",
        ], cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class FakeSession:
    """Deliberately distinctive values expose padding, scale, and ordering."""

    def __init__(self, role, score=.5):
        self.role, self.score = role, score
        self.inputs = []
        self.outputs = []
        self.fail_next = False

    def get_inputs(self):
        return [SimpleNamespace(name=self.role + "_input", type="tensor(float)")]

    def run(self, requested, values):
        assert requested is None
        assert set(values) == {self.role + "_input"}
        x = values[self.role + "_input"]
        assert x.dtype == np.float32
        self.inputs.append(x.copy())
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("synthetic inference failure")
        if self.role == "mel":
            frames = (x.shape[1] - 480) // 160
            result = np.full((1, frames, 32), 10 * (len(self.inputs) - 1), dtype=np.float32)
        elif self.role == "embedding":
            assert x.shape == (1, 76, 32, 1)
            result = np.full((1, 96), len(self.inputs), dtype=np.float32)
        else:
            assert x.shape == (1, 16, 96)
            result = np.array([[self.score]], dtype=np.float32)
        self.outputs.append(result)
        return [result]


@unittest.skipIf(np is None, "Optional NumPy wake dependency is unavailable")
class FrozenFrontendTests(unittest.TestCase):
    def detector(self, score=.5, threshold=.5):
        sessions = [FakeSession(role, score) for role in ("mel", "embedding", "classifier")]
        return FrozenOnnxWakeDetector(*sessions, threshold=threshold), sessions

    def test_int16_scale_little_endian_context_and_exact_mel_padding(self):
        detector, (mel, emb, classifier) = self.detector()
        samples = np.resize(np.array([-32768, 32767, 1, -1], dtype="<i2"), HOP_SAMPLES * 2)
        self.assertFalse(detector.feed(samples[:HOP_SAMPLES].tobytes()))
        np.testing.assert_array_equal(mel.inputs[0][0], samples[:HOP_SAMPLES].astype(np.float32))
        self.assertEqual(mel.inputs[0].shape, (1, 1280))
        # Five initial mel frames displace five of the 76 padding frames.
        np.testing.assert_array_equal(emb.inputs[0][0, :71, :, 0], np.ones((71, 32)))
        np.testing.assert_array_equal(emb.inputs[0][0, 71:, :, 0], np.full((5, 32), 2))
        self.assertFalse(detector.feed(samples[HOP_SAMPLES:].tobytes()))
        self.assertEqual(mel.inputs[1].shape, (1, 1760))
        np.testing.assert_array_equal(mel.inputs[1][0], samples[-1760:].astype(np.float32))
        # The second raw output is 10, hence 10 / 10 + 2 == 3.
        np.testing.assert_array_equal(emb.inputs[1][0, :63, :, 0], np.ones((63, 32)))
        np.testing.assert_array_equal(emb.inputs[1][0, 63:68, :, 0], np.full((5, 32), 2))
        np.testing.assert_array_equal(emb.inputs[1][0, 68:, :, 0], np.full((8, 32), 3))
        self.assertFalse(classifier.inputs)

    def test_sixteen_real_embeddings_before_scoring_then_sliding_order(self):
        detector, (_, _, classifier) = self.detector()
        self.assertFalse(detector.feed(bytes(HOP_BYTES * 15)))
        self.assertFalse(detector.ready)
        self.assertIsNone(detector.last_score)
        self.assertFalse(classifier.inputs)
        self.assertTrue(detector.feed(bytes(HOP_BYTES)))
        self.assertTrue(detector.ready)
        self.assertEqual(detector.last_score, .5)
        np.testing.assert_array_equal(classifier.inputs[0][0, :, 0], np.arange(1, 17))
        detector.feed(bytes(HOP_BYTES))
        np.testing.assert_array_equal(classifier.inputs[1][0, :, 0], np.arange(2, 18))

    def test_refractory_is_nineteen_audio_hops_and_includes_threshold_equality(self):
        detector, (_, _, classifier) = self.detector()
        triggered = [i for i in range(1, 55) if detector.feed(bytes(HOP_BYTES))]
        self.assertEqual(triggered, [16, 35, 54])
        self.assertEqual(len(classifier.inputs), 39)

    def test_ineligible_crossings_keep_features_without_arming_refractory(self):
        detector, (_, _, classifier) = self.detector()
        self.assertFalse(detector.feed_with_eligibility(bytes(HOP_BYTES * 16), eligible=False))
        self.assertTrue(detector.ready)
        self.assertEqual(detector.last_score, .5)
        self.assertIsNone(detector._last_trigger)
        self.assertFalse(detector.feed_with_eligibility(bytes(HOP_BYTES), eligible=False))
        np.testing.assert_array_equal(classifier.inputs[-1][0, :, 0], np.arange(2, 18))

        # The old feed API remains eligible by default, including immediately
        # after a suppressed crossing with continuous classifier history.
        self.assertTrue(detector.feed(bytes(HOP_BYTES)))
        np.testing.assert_array_equal(classifier.inputs[-1][0, :, 0], np.arange(3, 19))
        self.assertEqual(detector._last_trigger, 18)
        for _ in range(18):
            self.assertFalse(detector.feed_with_eligibility(bytes(HOP_BYTES), eligible=False))
        self.assertEqual(detector._last_trigger, 18)
        self.assertTrue(detector.feed_with_eligibility(bytes(HOP_BYTES), eligible=True))

    def test_eligibility_flag_is_explicit_and_does_not_consume_invalid_input(self):
        detector, (mel, _, _) = self.detector()
        for invalid in (None, 0, 1, "true"):
            with self.subTest(value=invalid), self.assertRaises(TypeError):
                detector.feed_with_eligibility(bytes(HOP_BYTES), eligible=invalid)
        self.assertFalse(mel.inputs)
        self.assertFalse(detector.ready)

    def test_packet_boundaries_and_multi_hop_calls_do_not_change_features(self):
        blob = np.arange(HOP_SAMPLES * 40, dtype=np.int32).astype("<i2").tobytes()
        batch, batch_sessions = self.detector()
        packets, packet_sessions = self.detector()
        self.assertTrue(batch.feed(blob))
        triggers = []
        for i in range(0, len(blob), 640):
            triggers.append(packets.feed(blob[i:i + 640]))
        self.assertEqual(sum(triggers), 2)
        for batch_session, packet_session in zip(batch_sessions, packet_sessions):
            self.assertEqual(len(batch_session.inputs), len(packet_session.inputs))
            for left, right in zip(batch_session.inputs, packet_session.inputs):
                np.testing.assert_array_equal(left, right)

    def test_arbitrary_whole_sample_tail_and_empty_chunk(self):
        detector, (mel, _, _) = self.detector()
        self.assertFalse(detector.feed(bytes(HOP_BYTES - 2)))
        self.assertFalse(detector.feed(b""))
        self.assertFalse(mel.inputs)
        self.assertFalse(detector.feed(struct.pack("<h", -1234) + bytes(6)))
        self.assertEqual(len(mel.inputs), 1)
        self.assertEqual(mel.inputs[0][0, -1], -1234)
        detector.feed(bytes(HOP_BYTES - 6))
        self.assertEqual(len(mel.inputs), 2)

    def test_reset_clears_audio_padding_readiness_and_refractory(self):
        detector, (mel, emb, _) = self.detector()
        self.assertTrue(detector.feed(bytes(HOP_BYTES * 16) + b"\xff\x7f"))
        detector.reset()
        self.assertFalse(detector.ready)
        self.assertIsNone(detector.last_score)
        detector.feed(bytes(HOP_BYTES))
        self.assertEqual(mel.inputs[-1].shape, (1, 1280))
        self.assertFalse(np.any(mel.inputs[-1]))
        np.testing.assert_array_equal(emb.inputs[-1][0, :71, :, 0], np.ones((71, 32)))
        self.assertTrue(detector.feed(bytes(HOP_BYTES * 15)))

    def test_streams_have_independent_features_partial_audio_and_trigger_state(self):
        first, first_sessions = self.detector()
        second, second_sessions = self.detector()
        first.feed(bytes(HOP_BYTES * 15) + b"\x7f\x00")
        self.assertFalse(second.feed(bytes(HOP_BYTES)))
        self.assertTrue(first.feed(bytes(HOP_BYTES - 2)))
        first.reset()
        self.assertTrue(second.feed(bytes(HOP_BYTES * 15)))
        self.assertFalse(first.ready)
        self.assertTrue(second.ready)
        self.assertIsNot(first._features, second._features)
        self.assertTrue(all(a is not b for a, b in zip(first_sessions, second_sessions)))

    def test_model_failure_resets_incomplete_state(self):
        detector, (_, emb, _) = self.detector()
        detector.feed(bytes(HOP_BYTES * 15))
        emb.fail_next = True
        with self.assertRaisesRegex(WakeModelError, "state was reset"):
            detector.feed(bytes(HOP_BYTES))
        self.assertFalse(detector.ready)
        self.assertIsNone(detector.last_score)
        self.assertFalse(detector.feed(bytes(HOP_BYTES * 15)))
        self.assertTrue(detector.feed(bytes(HOP_BYTES)))

    def test_incompatible_and_nonfinite_outputs_fail_closed(self):
        for role, output in (("mel", np.zeros((1, 8, 32), np.float32)),
                             ("mel", np.zeros((1, 5, 32), np.float64)),
                             ("embedding", np.zeros((1, 95), np.float32)),
                             ("embedding", np.full((1, 96), np.nan, np.float32)),
                             ("classifier", np.array([[1.1]], np.float32)),
                             ("classifier", np.array([[.1, .2]], np.float32))):
            detector, _ = self.detector()
            detector._sessions[role].run = lambda *_: [output]
            with self.subTest(role=role, shape=output.shape), self.assertRaises(WakeModelError):
                detector.feed(bytes(HOP_BYTES * 16))
            self.assertFalse(detector.ready)

    def test_frozen_threshold_is_required_and_not_recalibrated(self):
        for invalid in (0, -1, 1.1, True, "0.5", None, float("nan"), float("inf")):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                self.detector(threshold=invalid)
        detector, _ = self.detector(score=.5, threshold=.75)
        self.assertFalse(detector.feed(bytes(HOP_BYTES * 16)))


@unittest.skipIf(np is None, "Optional NumPy wake dependency is unavailable")
class FrozenServiceEligibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_cooldown_crossing_keeps_history_for_first_eligible_hop(self):
        now = [0.0]
        sessions = [FakeSession(role) for role in ("mel", "embedding", "classifier")]
        detector = FrozenOnnxWakeDetector(*sessions, threshold=.5)
        calls, events = [], []

        class Assist:
            async def run(self, _audio, **_kwargs):
                calls.append(True)
                return SimpleNamespace(conversation_id=None)

        class Playback:
            async def play(self, *_args):
                pass

            async def stop(self, *_args):
                pass

        service = AssistantService(
            ServiceSettings(enabled=True, rearm_after_turn=True, duplicate_window_seconds=2),
            {"phone": True}, lambda _: detector, Assist(), Playback(), events.append,
            clock=lambda: now[0])
        self.addAsyncCleanup(service.close)
        # An actual completed turn establishes the service's two-second gate.
        await service.activate("phone")
        await service.wait_idle("phone")
        self.assertEqual(len(calls), 1)
        self.assertEqual(service._quiet_until, 2.0)
        stream = await service.listen("phone")
        for frame in range(63):
            now[0] = (frame + 1) * .02
            service.feed_pcm("phone", stream, bytes(640))
        now[0] = 1.999
        service.feed_pcm("phone", stream, bytes(640))
        self.assertTrue(detector.ready)
        self.assertEqual(detector.last_score, .5)
        self.assertIsNone(detector._last_trigger)
        self.assertEqual(service.status("phone").reason, "model_warmup")
        self.assertFalse(any(event.reason == "wake_ready" for event in events))
        self.assertEqual(len(calls), 1)

        now[0] = 2.0
        service.feed_pcm("phone", stream, bytes(640))
        self.assertEqual(service.status("phone").reason, "wake_ready")
        for frame in range(3):
            now[0] = 2.02 + frame * .02
            service.feed_pcm("phone", stream, bytes(640))
        self.assertEqual(service.status("phone").state, "listening")
        self.assertEqual(len(sessions[2].inputs), 2)
        np.testing.assert_array_equal(sessions[2].inputs[-1][0, :, 0], np.arange(2, 18))
        await service.wait_idle("phone")
        self.assertEqual(len(calls), 2)


@unittest.skipIf(np is None, "Optional NumPy wake dependency is unavailable")
class PrivateBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / ".local"
        self.root.mkdir()
        self.bundle = self.root / "bundle.json"
        self.manifest = {"format_version": 1, "frontend": FRONTEND_ID, "threshold": .5, "models": {}}
        for role in ("mel", "embedding", "classifier"):
            payload = role.encode()
            name = role + ".onnx"
            (self.root / name).write_bytes(payload)
            self.manifest["models"][role] = {"path": name, "sha256": hashlib.sha256(payload).hexdigest()}
        self.save()
        self.created = []

    def save(self):
        self.bundle.write_text(json.dumps(self.manifest), encoding="utf-8")

    def factory(self, model):
        self.assertIsInstance(model, bytes)
        session = FakeSession(model.decode())
        self.created.append(session)
        return session

    def load(self):
        return FrozenOnnxWakeDetector.from_bundle(self.bundle, private_root=self.root,
                                                  session_factory=self.factory)

    def test_load_verifies_bytes_and_creates_fresh_sessions_for_each_phone(self):
        first, second = self.load(), self.load()
        self.assertEqual(len(self.created), 6)
        self.assertEqual(len({id(s) for s in self.created}), 6)
        self.assertTrue(first.feed(bytes(HOP_BYTES * 16)))
        self.assertFalse(second.ready)

    def test_every_digest_is_checked_before_any_runtime_is_created(self):
        (self.root / "classifier.onnx").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.load()
        self.assertFalse(self.created)

    def test_escape_and_external_symlink_are_rejected(self):
        outside = Path(self.temp.name) / "external.onnx"
        outside.write_bytes(b"mel")
        for name in ("../external.onnx", str(outside)):
            self.manifest["models"]["mel"]["path"] = name
            self.save()
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.load()
        (self.root / "link.onnx").symlink_to(outside)
        self.manifest["models"]["mel"]["path"] = "link.onnx"
        self.save()
        with self.assertRaises(ValueError):
            self.load()
        self.assertFalse(self.created)

    def test_manifest_must_also_stay_private(self):
        public = Path(self.temp.name) / "bundle.json"
        public.write_bytes(self.bundle.read_bytes())
        with self.assertRaises(ValueError):
            FrozenOnnxWakeDetector.from_bundle(public, private_root=self.root,
                                              session_factory=self.factory)
        with self.assertRaises(ValueError):
            FrozenOnnxWakeDetector.from_bundle(self.bundle, private_root=self.root.parent,
                                              session_factory=self.factory)

    def test_unrecognized_frontend_and_legacy_model_are_rejected(self):
        self.manifest["frontend"] = "different-frontend"
        self.save()
        with self.assertRaises(ValueError):
            self.load()
        self.manifest["frontend"] = FRONTEND_ID
        self.manifest["models"]["classifier"]["path"] = "legacy.tflite"
        self.save()
        with self.assertRaisesRegex(ValueError, "relative ONNX paths"):
            self.load()

    def test_format_version_and_digest_types_are_strict(self):
        self.manifest["format_version"] = True
        self.save()
        with self.assertRaises(ValueError):
            self.load()
        self.manifest["format_version"] = 1
        self.manifest["models"]["mel"]["sha256"] = 123
        self.save()
        with self.assertRaises(ValueError):
            self.load()

    def test_missing_runtime_gives_actionable_optional_dependency_error(self):
        real_import = __import__("importlib").import_module

        def unavailable(name):
            if name == "onnxruntime":
                raise ImportError
            return real_import(name)

        with patch("voice_assistant.wake.importlib.import_module", side_effect=unavailable):
            with self.assertRaisesRegex(RuntimeError, "optional wake backend requires ONNX Runtime"):
                FrozenOnnxWakeDetector.from_bundle(self.bundle, private_root=self.root)


if __name__ == "__main__":
    unittest.main()
