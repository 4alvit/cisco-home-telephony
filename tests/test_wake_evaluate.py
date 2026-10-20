"""Offline evaluator semantics, using non-speech WAVs and scripted scores only."""

import copy
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import wave
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/wake_evaluate.py"
spec = importlib.util.spec_from_file_location("wake_evaluate_under_test", SCRIPT)
evaluate = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = evaluate
spec.loader.exec_module(evaluate)
HOP_SAMPLES = 1280
HOP_BYTES = 2560


class ScriptedDetector:
    """A deliberately observable fake; its labels make no acoustic claims."""

    def __init__(self, schedules):
        self.schedules = schedules
        self.resets = 0
        self.calls = []
        self.hop = 0
        self.ready = False
        self.last_score = None

    def reset(self):
        self.hop = 0
        self.ready = False
        self.last_score = None
        self.resets += 1

    def feed(self, pcm):
        if self.resets < 1:
            raise AssertionError("Every clip must begin with an explicit reset")
        if len(pcm) != HOP_BYTES:
            raise AssertionError("Evaluator must feed exactly one complete 80 ms hop")
        self.calls.append((self.resets, pcm))
        self.hop += 1
        self.ready = self.hop >= 16
        fired = self.hop in self.schedules[self.resets - 1]
        self.last_score = (.9 if fired else .1) if self.ready else None
        return self.ready and fired


class Factory:
    def __init__(self, schedules):
        self.detector = ScriptedDetector(schedules)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.detector


class WakeEvaluationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.private = self.workspace / ".local"
        self.private.mkdir(mode=0o755)
        self.private.chmod(0o755)
        self.corpus = self.private / "corpus"
        self.corpus.mkdir(mode=0o700)
        self.manifest = self.corpus / "manifest.json"
        self.sequence = 0

    def wav(self, name, hops=64, tail=0, rate=16000, channels=1, width=2):
        path = self.corpus / name
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(channels)
            stream.setsampwidth(width)
            stream.setframerate(rate)
            stream.writeframes(bytes((hops * HOP_SAMPLES + tail) * channels * width))
        path.chmod(0o600)
        return path

    def clip(self, name="a", *, label="negative", domain="noise", source="synthetic",
             hops=64, tail=0, events=None):
        self.wav(name + ".wav", hops=hops, tail=tail)
        return {"id": name, "path": name + ".wav", "label": label,
                "domain": domain, "label_source": source,
                "expected_events": events or []}

    def write(self, clips):
        self.manifest.write_text(json.dumps({"format_version": 1, "clips": clips}))
        self.manifest.chmod(0o600)

    def output(self):
        self.sequence += 1
        return self.private / ("result-" + str(self.sequence))

    def run_clips(self, clips, schedules=None, **kwargs):
        self.write(clips)
        factory = Factory(schedules or [set() for _ in clips])
        destination = self.output()
        with mock.patch("socket.socket", side_effect=AssertionError("Network is forbidden")):
            result = evaluate.evaluate_manifest(
                self.manifest, workspace=self.workspace, output=destination,
                detector_factory=factory, mode="scripted_demo", **kwargs)
        return result, factory, destination

    def assert_rejected(self, clips, **kwargs):
        self.write(clips)
        self.assert_manifest_rejected(**kwargs)

    def assert_manifest_rejected(self, **kwargs):
        factory = Factory([set()] * 20)
        destination = self.output()
        with self.assertRaises((ValueError, OSError)):
            evaluate.evaluate_manifest(self.manifest, workspace=self.workspace,
                output=destination, detector_factory=factory, mode="scripted_demo", **kwargs)
        self.assertEqual(factory.calls, 0, "Validate the whole corpus before loading a detector")
        self.assertFalse(destination.exists(), "Invalid inputs must not leave a report")

    def test_exact_hops_first_score_exposure_and_unpadded_tail(self):
        result, factory, _ = self.run_clips([self.clip(hops=20, tail=117)], [{16, 20}])
        clip = result["clips"][0]
        self.assertEqual(factory.calls, 1)
        self.assertEqual(len(factory.detector.calls), 20)
        self.assertEqual(factory.detector.resets, 1)
        self.assertAlmostEqual(clip["duration_seconds"], 1.6 + 117 / 16000)
        self.assertAlmostEqual(clip["processed_seconds"], 1.6)
        self.assertEqual(clip["discarded_tail_samples"], 117)
        self.assertEqual(clip["first_score_seconds"], 1.28)
        self.assertAlmostEqual(clip["warmup_unscored_seconds"], 1.2)
        self.assertAlmostEqual(clip["scored_exposure_seconds"], .4)
        self.assertEqual(clip["detections_seconds"], [1.28, 1.6])
        scores = clip["scores"]
        self.assertEqual(len(scores), 5)
        self.assertEqual([s["time_seconds"] for s in scores], [1.28, 1.36, 1.44, 1.52, 1.6])
        self.assertEqual([s["detected"] for s in scores], [True, False, False, False, True])
        self.assertEqual([s["score"] for s in scores], [.9, .1, .1, .1, .9])

    def test_first_scored_hop_contributes_one_opportunity_not_zero(self):
        result, _, _ = self.run_clips([self.clip(hops=16)], [{16}])
        overall = result["summary"]["overall"]
        self.assertAlmostEqual(overall["negative_scored_exposure_seconds"], .08)
        self.assertEqual(overall["negative_false_accepts"], 1)
        self.assertAlmostEqual(overall["false_accepts_per_hour"], 45000)

    def test_short_negative_has_no_score_or_invented_zero_rate(self):
        result, factory, _ = self.run_clips([self.clip(hops=15, tail=1279)])
        clip = result["clips"][0]
        self.assertEqual(len(factory.detector.calls), 15)
        self.assertEqual(clip["scores"], [])
        self.assertIsNone(clip["first_score_seconds"])
        self.assertEqual(clip["scored_exposure_seconds"], 0)
        self.assertIsNone(result["summary"]["overall"]["false_accepts_per_hour"])

    def test_only_explicit_negatives_contribute_fa_and_exposure(self):
        clips = [self.clip("negative", hops=40), self.clip("unlabeled", label="unlabeled", hops=60),
                 self.clip("positive", label="positive", hops=80,
                           events=[{"start_seconds": 1.5, "end_seconds": 2.0}])]
        result, _, _ = self.run_clips(clips, [{20}, {20, 40}, {20, 45, 65}])
        overall = result["summary"]["overall"]
        self.assertEqual(overall["negative_false_accepts"], 1)
        self.assertAlmostEqual(overall["negative_scored_exposure_seconds"], 2.0)
        self.assertAlmostEqual(overall["false_accepts_per_hour"], 1800)
        negative, unlabeled, positive = result["clips"]
        self.assertIsNone(negative["true_positives"])
        self.assertIsNone(unlabeled["false_accepts"])
        self.assertIsNone(unlabeled["false_negatives"])
        self.assertIsNone(positive["false_accepts"])
        self.assertEqual(positive["true_positives"], 1)
        self.assertEqual(positive["false_negatives"], 0)
        self.assertEqual(positive["unmatched_positive_detections"], 2)

    def test_no_negative_labels_means_no_false_accept_rate(self):
        result, _, _ = self.run_clips([self.clip(label="unlabeled")], [{16}])
        self.assertIsNone(result["summary"]["overall"]["false_accepts_per_hour"])
        self.assertIsNone(result["summary"]["overall"]["event_miss_rate"])
        self.assertEqual(result["summary"]["overall"]["labeled_positive_events"], 0)

    def test_synthetic_and_recorded_domains_remain_separate(self):
        clips = [self.clip("tv-synthetic", domain="tv", source="synthetic", hops=40),
                 self.clip("tv-recorded", domain="tv", source="recorded", hops=40),
                 self.clip("music", domain="music", source="recorded", hops=40),
                 self.clip("noise", domain="noise", source="recorded", hops=40)]
        result, _, _ = self.run_clips(clips, [{16, 35}, set(), {16}, set()])
        strata = {(s["domain"], s["label_source"]): s for s in result["summary"]["strata"]}
        self.assertEqual(set(strata), {("tv", "synthetic"), ("tv", "recorded"),
                                      ("music", "recorded"), ("noise", "recorded")})
        self.assertAlmostEqual(strata["tv", "synthetic"]["false_accepts_per_hour"], 3600)
        self.assertEqual(strata["tv", "recorded"]["false_accepts_per_hour"], 0)
        self.assertAlmostEqual(strata["music", "recorded"]["false_accepts_per_hour"], 1800)
        self.assertIsNone(result["summary"]["overall"]["false_accepts_per_hour"])
        self.assertIsNone(result["summary"]["overall"]["event_recall"])
        self.assertIsNone(result["summary"]["overall"]["event_miss_rate"])
        self.assertIsNone(result["summary"]["overall"]["labeled_positive_events"])
        self.assertEqual(result["summary"]["accuracy_aggregation"], "separate_label_sources")

    def test_nested_windows_use_maximum_one_to_one_matching(self):
        events = [{"start_seconds": 1.28, "end_seconds": 3.0},
                  {"start_seconds": 1.36, "end_seconds": 1.5}]
        result, _, _ = self.run_clips([self.clip(label="positive", events=events)],
                                      [{18, 37}], late_tolerance_seconds=0)
        clip = result["clips"][0]
        self.assertEqual(clip["true_positives"], 2)
        self.assertEqual(clip["false_negatives"], 0)
        self.assertEqual(clip["unmatched_positive_detections"], 0)

    def test_one_detection_cannot_match_two_overlapping_events(self):
        events = [{"start_seconds": 1.5, "end_seconds": 2.0},
                  {"start_seconds": 1.6, "end_seconds": 2.1}]
        result, _, _ = self.run_clips([self.clip(label="positive", events=events)], [{25}])
        self.assertEqual(result["clips"][0]["true_positives"], 1)
        self.assertEqual(result["clips"][0]["false_negatives"], 1)
        self.assertEqual(result["summary"]["overall"]["labeled_positive_events"], 2)
        self.assertEqual(result["summary"]["overall"]["event_recall"], .5)
        self.assertEqual(result["summary"]["overall"]["event_miss_rate"], .5)

    def test_multiple_detections_do_not_multiply_single_event_success(self):
        events = [{"start_seconds": 1.28, "end_seconds": 4.0}]
        result, _, _ = self.run_clips([self.clip(label="positive", events=events)],
                                      [{16, 35, 54}], late_tolerance_seconds=0)
        clip = result["clips"][0]
        self.assertEqual(clip["true_positives"], 1)
        self.assertEqual(clip["false_negatives"], 0)
        self.assertEqual(clip["unmatched_positive_detections"], 2)
        self.assertIsNone(clip["false_accepts"])

    def test_detection_during_phrase_has_negative_end_relative_latency(self):
        result, _, _ = self.run_clips([self.clip(label="positive", events=[
            {"start_seconds": 1.5, "end_seconds": 2.0}])], [{20}])
        match = result["clips"][0]["matches"][0]
        self.assertEqual(match["event_index"], 0)
        self.assertEqual(match["detection_index"], 0)
        self.assertAlmostEqual(match["latency_from_end_seconds"], -.4)

    def test_event_windows_are_inclusive_but_adjacent_outside_detections_do_not_match(self):
        events = [{"start_seconds": 1.36, "end_seconds": 1.44}]
        for hop, expected in ((16, 0), (17, 1), (18, 1), (19, 0)):
            with self.subTest(hop=hop):
                result, _, _ = self.run_clips([self.clip(label="positive", events=events)],
                                              [{hop}], late_tolerance_seconds=0)
                self.assertEqual(result["clips"][0]["true_positives"], expected)

    def test_event_before_readiness_is_rejected_not_counted_as_a_miss(self):
        for start in (0, 1.279):
            with self.subTest(start=start):
                self.assert_rejected([self.clip(label="positive", events=[
                    {"start_seconds": start, "end_seconds": 2.0}])])

    def test_missing_tail_and_partial_hop_cannot_supply_event_coverage(self):
        clip = self.clip(label="positive", hops=40, tail=1279,
                         events=[{"start_seconds": 2.5, "end_seconds": 3.21}])
        self.assert_rejected([clip], late_tolerance_seconds=0)
        clip["expected_events"][0]["end_seconds"] = 2.3
        clip["expected_events"][0]["start_seconds"] = 2.0
        self.assert_rejected([clip], late_tolerance_seconds=1.0)

    def test_exact_coverage_boundaries_are_evaluable(self):
        result, _, _ = self.run_clips([self.clip(label="positive", hops=40, events=[
            {"start_seconds": 1.28, "end_seconds": 2.2}])], [{40}], late_tolerance_seconds=1)
        self.assertEqual(result["clips"][0]["true_positives"], 1)

    def test_reset_is_per_clip_and_partial_audio_never_leaks(self):
        clips = [self.clip("one", hops=16, tail=1279), self.clip("two", hops=16)]
        result, factory, _ = self.run_clips(clips, [{16}, {16}])
        self.assertEqual(factory.calls, 1)
        self.assertEqual(factory.detector.resets, 2)
        self.assertEqual([len([x for x in factory.detector.calls if x[0] == n]) for n in (1, 2)], [16, 16])
        self.assertEqual([c["detections_seconds"] for c in result["clips"]], [[1.28], [1.28]])

    def test_invalid_audio_format_is_rejected_before_loading_models(self):
        for kwargs in ({"rate": 8000}, {"rate": 48000}, {"channels": 2}, {"width": 1}, {"width": 3}):
            with self.subTest(kwargs=kwargs):
                clip = self.clip()
                self.wav("a.wav", **kwargs)
                self.assert_rejected([clip])

    def test_malformed_or_empty_audio_is_rejected(self):
        for payload in (b"", b"not a wav", b"RIFF\x00\x00\x00\x00WAVE"):
            with self.subTest(payload=payload):
                clip = self.clip()
                (self.corpus / "a.wav").write_bytes(payload)
                self.assert_rejected([clip])
        clip = self.clip(hops=0)
        self.assert_rejected([clip])

    def test_truncated_wav_does_not_count_header_claimed_audio_exposure(self):
        clip = self.clip()
        path = self.corpus / "a.wav"
        path.write_bytes(path.read_bytes()[:-640])
        self.assert_rejected([clip])

    def test_invalid_second_clip_prevents_any_inference_or_partial_report(self):
        first, second = self.clip("valid"), self.clip("invalid")
        second["path"] = "missing.wav"
        self.assert_rejected([first, second])

    def test_duplicate_ids_duplicate_events_and_invalid_labels_rejected(self):
        clip = self.clip()
        self.assert_rejected([clip, copy.deepcopy(clip)])
        event = {"start_seconds": 1.5, "end_seconds": 2.0}
        self.assert_rejected([self.clip(label="positive", events=[event, copy.deepcopy(event)])])
        for field, value in (("label", "maybe"), ("domain", "unknown"),
                             ("label_source", "assumed"), ("id", "")):
            bad = copy.deepcopy(clip)
            bad[field] = value
            with self.subTest(field=field):
                self.assert_rejected([bad])

    def test_duplicate_json_keys_are_never_silently_overwritten(self):
        clip = self.clip()
        document = json.dumps({"format_version": 1, "clips": [clip]})
        for text in (document.replace('"label": "negative"', '"label":"positive","label":"negative"'),
                     document.replace('"format_version": 1', '"format_version":2,"format_version":1'),
                     document.replace('"expected_events": []', '"expected_events":[],"expected_events":[]')):
            self.manifest.write_text(text)
            self.manifest.chmod(0o600)
            with self.subTest(text=text):
                self.assert_manifest_rejected()

    def test_event_schema_and_numeric_boundaries_are_strict(self):
        for events in ([], [{"time_seconds": 2}], [{"start_seconds": 3, "end_seconds": 2}],
                       [{"start_seconds": True, "end_seconds": 2}],
                       [{"start_seconds": 1.5, "end_seconds": math.nan}],
                       [{"start_seconds": 1.5, "end_seconds": math.inf}]):
            with self.subTest(events=events):
                self.assert_rejected([self.clip(label="positive", events=events)])
        for label in ("negative", "unlabeled"):
            self.assert_rejected([self.clip(label=label, events=[{"start_seconds": 1.5, "end_seconds": 2}])])
        for tolerance in (-.1, math.inf, math.nan, True, "1"):
            with self.subTest(tolerance=tolerance):
                self.assert_rejected([self.clip()], late_tolerance_seconds=tolerance)

    def test_empty_corpus_invalid_versions_and_unknown_fields_are_rejected(self):
        clip = self.clip()
        for document in ({"format_version": 1, "clips": []},
                         {"format_version": True, "clips": [clip]},
                         {"format_version": 2, "clips": [clip]},
                         {"format_version": 1, "clips": [clip], "extra": True}):
            self.manifest.write_text(json.dumps(document))
            self.manifest.chmod(0o600)
            with self.subTest(document=document):
                self.assert_manifest_rejected()

    def test_nonprivate_manifest_or_wav_is_rejected_without_chmod(self):
        clip = self.clip()
        self.write([clip])
        self.manifest.chmod(0o644)
        self.assert_manifest_rejected()
        self.assertEqual(stat.S_IMODE(self.manifest.stat().st_mode), 0o644)
        self.manifest.chmod(0o600)
        path = self.corpus / "a.wav"
        path.chmod(0o640)
        self.assert_manifest_rejected()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_external_paths_and_symlink_escapes_are_rejected(self):
        clip = self.clip()
        outside = self.workspace / "outside.wav"
        outside.write_bytes((self.corpus / "a.wav").read_bytes())
        for path in (str(outside), "../../outside.wav"):
            bad = dict(clip, path=path)
            with self.subTest(path=path):
                self.assert_rejected([bad])
        link = self.corpus / "escape.wav"
        link.symlink_to(outside)
        self.assert_rejected([dict(clip, path="escape.wav")])

    def test_output_is_new_private_directory_and_does_not_chmod_shared_root(self):
        _, _, destination = self.run_clips([self.clip()])
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
        for name in ("results.private.json", "summary.safe.json"):
            self.assertEqual(stat.S_IMODE((destination / name).stat().st_mode), 0o600)
        original = (destination / "results.private.json").read_bytes()
        with self.assertRaises((ValueError, OSError)):
            evaluate.evaluate_manifest(self.manifest, workspace=self.workspace, output=destination,
                detector_factory=Factory([set()]), mode="scripted_demo")
        self.assertEqual((destination / "results.private.json").read_bytes(), original)

    def test_output_cannot_escape_through_parent_symlink(self):
        clip = self.clip()
        self.write([clip])
        outside = self.workspace / "outside"
        outside.mkdir()
        (self.private / "link").symlink_to(outside, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            evaluate.evaluate_manifest(self.manifest, workspace=self.workspace, output=self.private / "link/result",
                detector_factory=Factory([set()]), mode="scripted_demo")
        self.assertEqual(list(outside.iterdir()), [])

    def test_scripted_factory_cannot_be_reported_as_real_model_evaluation(self):
        self.write([self.clip()])
        factory = Factory([{16}])
        with self.assertRaises(ValueError):
            evaluate.evaluate_manifest(self.manifest, workspace=self.workspace,
                output=self.output(), detector_factory=factory, mode="onnx")
        self.assertEqual(factory.calls, 0)

    def test_provenance_identifies_scripted_mode_inputs_source_and_frontend(self):
        clip = self.clip()
        result, _, destination = self.run_clips([clip])
        provenance = result["provenance"]
        self.assertEqual(provenance["mode"], "scripted_demo")
        self.assertFalse(provenance["accuracy_evidence"])
        self.assertFalse(result["summary"]["accuracy_evidence"])
        self.assertIsNone(provenance["bundle"])
        self.assertEqual(provenance["manifest_sha256"], hashlib.sha256(self.manifest.read_bytes()).hexdigest())
        self.assertEqual(result["clips"][0]["wav_sha256"], hashlib.sha256((self.corpus / "a.wav").read_bytes()).hexdigest())
        for name, path in (("wake.py", ROOT / "voice_assistant/wake.py"),
                           ("audio.py", ROOT / "voice_assistant/audio.py"),
                           ("wake_evaluate.py", SCRIPT)):
            self.assertEqual(provenance["source_sha256"][name], hashlib.sha256(path.read_bytes()).hexdigest())
        frontend = provenance["frontend"]
        self.assertEqual(frontend["hop_samples"], 1280)
        self.assertEqual(frontend["classifier_frames"], 16)
        self.assertEqual(frontend["refractory_hops"], 19)
        self.assertEqual(frontend["required_context_seconds"], 1.28)
        self.assertIn("python", provenance["runtime"])
        self.assertIn("onnxruntime", provenance["runtime"]["packages"])
        saved = json.loads((destination / "results.private.json").read_text())
        self.assertEqual(saved, result)
        self.assertGreaterEqual(result["clips"][0]["processing_seconds"], 0)
        self.assertGreaterEqual(result["summary"]["realtime_factor"], 0)

    def test_onnx_mode_without_evaluable_labels_does_not_claim_accuracy_evidence(self):
        """Exercise metadata routing with synthetic model bytes and a stub loader."""
        bundle = {"format_version": 1, "frontend": evaluate.wake.FRONTEND_ID,
                  "threshold": .5, "models": {}}
        for role in ("mel", "embedding", "classifier"):
            payload = ("synthetic-" + role).encode()
            model_path = self.corpus / (role + ".onnx")
            model_path.write_bytes(payload)
            model_path.chmod(0o600)
            bundle["models"][role] = {"path": model_path.name,
                                      "sha256": hashlib.sha256(payload).hexdigest()}
        bundle_path = self.corpus / "bundle.json"
        bundle_path.write_text(json.dumps(bundle))
        bundle_path.chmod(0o600)

        def loader(path, *, private_root, session_factory):
            self.assertEqual(path, bundle_path)
            self.assertEqual(private_root, self.private)
            detector = ScriptedDetector([set()])
            detector.threshold = .5
            detector._sessions = {role: session_factory((self.corpus / model["path"]).read_bytes())
                                  for role, model in bundle["models"].items()}
            return detector

        def session(_):
            return SimpleNamespace(get_providers=lambda: ["CPUExecutionProvider"])

        cases = [("unlabeled", 40, [], False), ("negative", 15, [], False),
                 ("negative", 16, [], True),
                 ("positive", 50, [{"start_seconds": 1.5, "end_seconds": 2.0}], True)]
        for label, hops, events, expected in cases:
            with self.subTest(label=label, hops=hops):
                self.write([self.clip(label=label, hops=hops, events=events)])
                with mock.patch.object(evaluate.wake.FrozenOnnxWakeDetector, "from_bundle", side_effect=loader), \
                     mock.patch.object(evaluate.wake, "_onnx_session", side_effect=session):
                    result = evaluate.evaluate_manifest(self.manifest, workspace=self.workspace,
                        output=self.output(), bundle_path=bundle_path, mode="onnx")
                self.assertIs(result["summary"]["accuracy_evidence"], expected)
                self.assertIs(result["provenance"]["accuracy_evidence"], expected)
                self.assertEqual(result["provenance"]["loaded_model_sha256"],
                                 {role: model["sha256"] for role, model in bundle["models"].items()})
                self.assertEqual(result["provenance"]["actual_detector_threshold"], .5)

    def test_generated_fixtures_are_deterministic_private_nonspeech_negatives(self):
        with mock.patch("socket.socket", side_effect=AssertionError("Network is forbidden")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("Processes are forbidden")):
            first = evaluate.generate_fixtures(self.private / "fixtures1", workspace=self.workspace)
            second = evaluate.generate_fixtures(self.private / "fixtures2", workspace=self.workspace)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        manifest = json.loads(first.read_text())
        self.assertEqual({c["id"] for c in manifest["clips"]}, {"silence", "noise", "tone"})
        self.assertEqual(len(manifest["clips"]), 3)
        for clip in manifest["clips"]:
            self.assertEqual(clip["label"], "negative")
            self.assertEqual(clip["label_source"], "synthetic")
            self.assertEqual(clip["expected_events"], [])
            a, b = first.parent / clip["path"], second.parent / clip["path"]
            self.assertEqual(a.read_bytes(), b.read_bytes())
            self.assertEqual(stat.S_IMODE(a.stat().st_mode), 0o600)
            with wave.open(str(a)) as stream:
                self.assertEqual((stream.getframerate(), stream.getsampwidth(), stream.getnchannels()), (16000, 2, 1))
                self.assertEqual(stream.getnframes(), 48000)
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(first.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o755)

    def test_cli_demo_reports_scripted_nonaccuracy_without_private_paths(self):
        output = self.private / "private-demo-name"
        process = subprocess.run([sys.executable, "-B", str(SCRIPT), "demo", "--workspace",
                                  str(self.workspace), "--output", str(output)],
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        summary = json.loads(process.stdout)
        self.assertEqual(summary["mode"], "scripted_demo")
        self.assertFalse(summary["accuracy_evidence"])
        self.assertNotIn(str(self.workspace), process.stdout + process.stderr)
        self.assertEqual(summary["overall"]["positive_clips"], 0)
        self.assertIsNone(summary["overall"]["event_recall"])
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)

    def test_real_frozen_detector_retains_nineteen_hop_refractory_and_resets(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("Optional NumPy dependency unavailable; core tests remain dependency-free")
        from voice_assistant.wake import FrozenOnnxWakeDetector

        class Session:
            def __init__(self, role):
                self.role = role

            def get_inputs(self):
                return [SimpleNamespace(name="x", type="tensor(float)")]

            def run(self, _, values):
                if self.role == "mel":
                    frames = (values["x"].shape[1] - 480) // 160
                    value = np.zeros((1, frames, 32), dtype=np.float32)
                elif self.role == "embedding":
                    value = np.zeros((1, 96), dtype=np.float32)
                else:
                    value = np.array([[.5]], dtype=np.float32)
                return [value]

        def factory():
            return FrozenOnnxWakeDetector(*(Session(r) for r in ("mel", "embedding", "classifier")), threshold=.5)

        self.write([self.clip("one", hops=54, tail=79), self.clip("two", hops=54)])
        result = evaluate.evaluate_manifest(self.manifest, workspace=self.workspace,
            output=self.output(), detector_factory=factory, mode="scripted_demo")
        for clip in result["clips"]:
            self.assertEqual([round(t, 8) for t in clip["detections_seconds"]], [1.28, 2.8, 4.32])
            self.assertEqual(len(clip["scores"]), 39)
            self.assertAlmostEqual(clip["scored_exposure_seconds"], 3.12)
            self.assertEqual(clip["false_accepts"], 3)

    def test_import_has_no_network_process_or_optional_runtime_side_effects(self):
        code = (
            "import runpy,sys,socket,subprocess; from unittest.mock import patch; "
            "block=AssertionError('side effect'); "
            "ctx1=patch('socket.socket',side_effect=block); "
            "ctx2=patch('subprocess.Popen',side_effect=block); "
            "ctx1.start();ctx2.start(); "
            f"runpy.run_path({str(SCRIPT)!r},run_name='offline_import_test'); "
            "assert 'numpy' not in sys.modules; assert 'onnxruntime' not in sys.modules"
        )
        process = subprocess.run([sys.executable, "-B", "-c", code], cwd=self.workspace,
                                 text=True, capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout, "")


if __name__ == "__main__":
    unittest.main()
