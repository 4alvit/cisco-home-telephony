"""Offline timing and replay-plan checks; no service, network, or inference."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import wave

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("benchmark_voice_stages", ROOT / "scripts/benchmark_voice_stages.py")
subject = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(subject)


def summary():
    events = [("run-start", 2.1), ("stt-start", 2.2), ("stt-end", 6.2),
              ("intent-start", 6.3), ("intent-end", 11.3), ("tts-start", 11.4),
              ("tts-end", 11.5), ("run-end", 11.6)]
    return {"activation_seconds": 2, "total_seconds": 15.4, "turn_seconds": 13.4,
            "events": [{"type": name, "elapsed": value, "data": {"secret": "private sentinel"}} for name, value in events],
            "states": [{"state": "speaking", "elapsed": 11.7, "phone_id": "private sentinel"}],
            "first_output_pcm_seconds": 13.8, "last_output_pcm_seconds": 15.3,
            "fetch": {"seconds": 2, "url": "private sentinel"}, "success": True,
            "answer_matches_requested_phrase": True, "transcript_matches_input": True}


class TimingTests(unittest.TestCase):
    def test_stage_semantics_and_residuals(self):
        row = subject.analyze_trial(summary(), 1)
        expected = {"stt_s": 4, "conversation_s": 5, "tts_metadata_s": .1,
                    "pipeline_s": 9.5, "run_end_to_client_return_s": .1,
                    "audio_fetch_s": 2, "decode_and_output_setup_residual_s": .1,
                    "paced_output_s": 1.5, "cleanup_after_output_s": .1,
                    "total_s": 13.4, "pipeline_unaccounted_s": .4}
        for key, value in expected.items():
            self.assertAlmostEqual(row["metrics"][key], value, msg=key)
        self.assertTrue(row["accepted"])
        self.assertNotIn("private sentinel", json.dumps(row))

    def test_safe_relative_events_match_summary_stages_without_fabricating_fetch(self):
        original = summary()
        safe = {"events": [{"stage": row["type"], "seconds_from_activation": row["elapsed"]-2}
                           for row in original["events"]], "turn_seconds": 13.4,
                "sink_delivery_seconds": 1.5}
        row = subject.analyze_trial(safe, 1)
        self.assertEqual(row["metrics"]["stt_s"], 4)
        self.assertEqual(row["metrics"]["paced_output_s"], 1.5)
        self.assertIsNone(row["metrics"]["audio_fetch_s"])
        self.assertIsNone(row["quality_pass"])
        self.assertFalse(row["accepted"])

    def test_optional_instrumentation_is_separate_from_pure_compute_and_audible_onset(self):
        value = summary()
        value.update(assist_run_started_seconds=2.05, assist_run_returned_seconds=11.65,
                     input_feed_started_seconds=2.05, input_feed_finished_seconds=4.05,
                     first_wire_pcm_sent_seconds=2.25, last_wire_pcm_sent_seconds=4.1,
                     wire_end_marker_sent_seconds=4.12, fetch_started_seconds=11.75,
                     fetch_finished_seconds=13.75, decode_started_seconds=13.75,
                     decode_finished_seconds=13.77, first_delivered_pcm_seconds=13.82,
                     output={"seconds": 1.48})
        row = subject.analyze_trial(value, 1)
        for key, expected in {"run_end_to_client_return_s": .05, "decode_s": .02,
                              "input_feed_s": 2, "wire_upload_span_s": 1.85,
                              "post_eos_stt_s": 2.08, "output_setup_after_decode_s": .03,
                              "first_sink_block_delivery_s": .02, "output_pacing_overhead_s": .02}.items():
            self.assertAlmostEqual(row["metrics"][key], expected, msg=key)
        self.assertEqual(row["measurement_sources"]["client_return"], "explicit_assist_return")
        value["wire_end_marker_sent_seconds"] = 6.3
        early = subject.analyze_trial(value, 1)
        self.assertAlmostEqual(early["metrics"]["post_eos_stt_s"], -.1)
        self.assertIn("stt_ended_before_explicit_eos", early["issues"])
        self.assertTrue(early["timing_boundaries_consistent"])

    def test_overlapping_stages_use_union(self):
        value = summary()
        for row in value["events"]:
            if row["type"] == "intent-start":
                row["elapsed"] = 5.2
        row = subject.analyze_trial(value, 1)
        self.assertAlmostEqual(row["metrics"]["stage_overlap_s"], 1)
        self.assertAlmostEqual(row["metrics"]["pipeline_unaccounted_s"], .3)
        self.assertIn("overlapping_pipeline_stages", row["issues"])

    def test_missing_stage_stays_null(self):
        value = summary()
        value["events"] = [row for row in value["events"] if row["type"] != "stt-end"]
        row = subject.analyze_trial(value, 1)
        self.assertIsNone(row["metrics"]["stt_s"])
        self.assertIsNone(row["metrics"]["pipeline_unaccounted_s"])
        self.assertFalse(row["core_stage_timings_complete"])

    def test_duplicate_and_negative_boundaries_do_not_pass(self):
        for duplicate in (True, False):
            value = summary()
            if duplicate:
                value["events"].append({"type": "stt-end", "elapsed": 6.4})
            else:
                value["events"][2]["elapsed"] = 2.1
            row = subject.analyze_trial(value, 1)
            self.assertIsNone(row["metrics"]["stt_s"])
            self.assertFalse(row["accepted"])
            self.assertFalse(row["timing_boundaries_consistent"])

    def test_error_timeout_and_inconsistent_total_never_pass(self):
        for kind in ("error", "timeout", "contradiction"):
            value = summary()
            if kind == "error":
                value["events"].append({"type": "error", "elapsed": 8})
            elif kind == "timeout":
                value["final_status"] = {"reason": "turn_timeout"}
            else:
                value["turn_seconds"] = 1
            self.assertFalse(subject.analyze_trial(value, 1)["accepted"])

    def test_explicit_failed_final_state_or_reason_never_passes(self):
        for final in ({"final_status": {"state": "error", "reason": "turn_complete"}},
                      {"final_status": {"state": "stopped", "reason": "playback_failed"}},
                      {"final_state": "thinking", "final_reason": "turn_complete"},
                      {"final_state": "stopped", "final_reason": "cancelled"}):
            with self.subTest(final=final):
                row = subject.analyze_trial({**summary(), **final}, 1)
                self.assertFalse(row["accepted"])
                self.assertTrue(any(issue.startswith("reported_failure_") for issue in row["issues"]))
        self.assertTrue(subject.analyze_trial(summary(), 1)["accepted"])
        success = {"final_status": {"state": "stopped", "reason": "turn_complete"}}
        self.assertTrue(subject.analyze_trial({**summary(), **success}, 1)["accepted"])

    def test_safe_receipt_boundaries_cannot_exceed_total_turn(self):
        safe = {"events": [{"stage": "run-start", "seconds_from_activation": 0},
                           {"stage": "run-end", "seconds_from_activation": 10.002}],
                "turn_seconds": 10, "success": True,
                "transcript_matches_input": True, "answer_matches_requested_phrase": True}
        row = subject.analyze_trial(safe, 1)
        self.assertFalse(row["accepted"])
        self.assertIn("event_outside_turn:run-end", row["issues"])
        self.assertFalse(row["timing_boundaries_consistent"])
        safe["events"][-1]["seconds_from_activation"] = 10.001
        self.assertTrue(subject.analyze_trial(safe, 1)["accepted"])

    def test_exact_budget_boundary(self):
        value = summary()
        value.update(turn_seconds=30, total_seconds=32)
        self.assertTrue(subject.analyze_trial(value, 1)["accepted"])
        value.update(turn_seconds=30.000001, total_seconds=32.000001)
        self.assertFalse(subject.analyze_trial(value, 1)["accepted"])

    def test_invalid_numbers_and_event_bounds(self):
        for value in (True, -1, float("nan"), float("inf"), 1e100, 10**1000):
            self.assertIsNone(subject.number(value))
        with self.assertRaises(subject.InputError):
            subject.analyze_trial({"events": [{}]*(subject.MAX_EVENTS+1)}, 1)
        row = subject.analyze_trial({"events": [{"type": []}, {"type": "error", "elapsed": True}]}, 1)
        self.assertTrue(row["error_event_present"])
        self.assertFalse(row["accepted"])


class FileAndPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.local = self.root / ".local"
        self.local.mkdir()
        patcher = mock.patch.object(subject, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name, value):
        path = self.local / name
        path.write_text(json.dumps(value))
        return path

    def spec(self):
        runner = self.local / "runner.py"
        runner.write_text("raise RuntimeError('must never execute in offline tests')\n")
        config = self.write("config.json", {"token_file": "private sentinel"})
        expected = self.write("expected.json", {"transcript": "private sentinel"})
        audio = self.local / "saved.wav"
        with wave.open(str(audio), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b"\0\0"*16000)
        return {"schema_version": 1, "runner": str(runner), "python": sys.executable,
                "config": str(config), "expected_files": [str(expected)], "inputs": [str(audio)],
                "stt_engine": "stt.synthetic_fixture", "output_root": str(self.local / "future-run")}

    def test_aggregate_counts_only_present_values_and_redacts_input(self):
        one = self.write("one.json", summary())
        two = self.write("two.json", {"turn_seconds": 20, "events": []})
        report = subject.analyze([one, two])
        self.assertEqual(report["aggregate"]["stt_s"]["measured_count"], 1)
        self.assertEqual(report["aggregate"]["stt_s"]["missing_count"], 1)
        self.assertEqual(report["aggregate"]["total_s"]["median_s"], 16.7)
        self.assertNotIn("private sentinel", json.dumps(report))
        self.assertNotIn(str(self.root), json.dumps(report))
        with self.assertRaises(subject.InputError):
            subject.analyze([one, one])

    def test_input_size_symlink_and_output_freshness(self):
        one = self.write("one.json", summary())
        link = self.local / "link.json"
        link.symlink_to(one)
        with self.assertRaises(subject.InputError):
            subject.read_json(link)
        with mock.patch.object(subject, "MAX_INPUT_BYTES", 10), self.assertRaises(subject.InputError):
            subject.read_json(one)
        output = self.local / "output.json"
        subject.save_private(output, {})
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(subject.InputError):
            subject.save_private(output, {})
        with self.assertRaises(subject.InputError):
            subject.save_private(self.root / "public.json", {})

    def test_plan_has_exact_argv_hashes_and_never_executes(self):
        spec = self.spec()
        spec["repetitions"] = 2
        plan = subject.plan(spec)
        self.assertFalse(plan["execution_enabled"])
        self.assertFalse(plan["caps_enforced"])
        self.assertTrue(plan["serial"])
        self.assertEqual(len(plan["trials"]), 2)
        self.assertFalse(Path(plan["output_root"]).exists())
        trial = plan["trials"][0]
        self.assertEqual(trial["argv"][2:], ["--config", spec["config"], "--input", spec["inputs"][0],
                         "--output", str(self.local / "future-run/trial-01"), "--stt-engine", spec["stt_engine"]])
        for item in trial["required_files"]:
            self.assertEqual(item["sha256"], hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest())
        self.assertNotIn("private sentinel", json.dumps(plan))

    def test_plan_caps_unknown_fields_and_audio_format(self):
        spec = self.spec()
        for edit in ({"repetitions": 11}, {"repetitions": True}, {"per_trial_timeout_seconds": 61},
                     {"overall_timeout_seconds": 301}, {"overall_timeout_seconds": 10},
                     {"per_trial_timeout_seconds": float("nan")}, {"unknown": True}, {"stt_engine": ""},
                     {"schema_version": True}):
            with self.assertRaises(subject.InputError, msg=str(edit)):
                subject.plan({**spec, **edit})
        valid = subject.plan({**spec, "repetitions": 10, "per_trial_timeout_seconds": 60, "overall_timeout_seconds": 300})
        self.assertEqual(len(valid["trials"]), 10)
        with wave.open(spec["inputs"][0], "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(8000)
            stream.writeframes(b"\0\0"*8000)
        with self.assertRaises(subject.InputError):
            subject.plan(spec)

    def test_plan_preserves_virtualenv_interpreter_symlink(self):
        spec = self.spec()
        venv_bin = self.local / "venv/bin"
        venv_bin.mkdir(parents=True)
        interpreter = venv_bin / "python"
        interpreter.symlink_to(Path(sys.executable).resolve())
        spec["python"] = str(interpreter)
        plan = subject.plan(spec)
        self.assertEqual(plan["trials"][0]["argv"][0], str(interpreter))
        self.assertEqual(plan["interpreter"]["realpath"], str(Path(sys.executable).resolve()))

    def test_cli_analysis_and_plan_have_only_safe_receipts(self):
        one = self.write("one.json", summary())
        output = self.local / "analysis.json"
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(subject.main(["analyze", "--input", str(one), "--output", str(output)]), 0)
            self.assertNotIn("private sentinel", stdout.getvalue())
            self.assertNotIn(str(self.root), stdout.getvalue())
        spec_path = self.write("spec.json", self.spec())
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(subject.main(["plan", "--spec", str(spec_path), "--output", str(self.local / "plan.json")]), 0)
            self.assertFalse(json.loads(stdout.getvalue())["execution_enabled"])


if __name__ == "__main__":
    unittest.main()
