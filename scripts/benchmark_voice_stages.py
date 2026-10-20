#!/usr/bin/env python3
"""Analyze saved synthetic voice timings and prepare offline replay plans.

Only whitelisted timing and quality fields enter reports. Transcript text,
provider identifiers, paths, audio and credentials are never copied. Supply
one artifact per trial: a summary and its event-timings file are the same trial.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import statistics
import sys
import wave

ROOT = Path(__file__).resolve().parents[1]
MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_INPUTS = 50
MAX_EVENTS = 2000
MAX_ELAPSED_SECONDS = 1_000_000_000
BOUNDARIES = {"run-start", "run-end", "stt-start", "stt-end", "intent-start",
              "intent-end", "tts-start", "tts-end", "error"}
STAGES = {"stt_s": ("stt-start", "stt-end"),
          "conversation_s": ("intent-start", "intent-end"),
          "tts_metadata_s": ("tts-start", "tts-end"),
          "pipeline_s": ("run-start", "run-end")}
METRIC_DEFINITIONS = {
    "stt_s": "HA stt-start to stt-end, including streaming/end detection; not direct model latency.",
    "conversation_s": "HA intent-start to intent-end.",
    "tts_metadata_s": "HA tts-start to tts-end; metadata/scheduling, not waveform synthesis time.",
    "pipeline_s": "HA run-start to run-end.",
    "audio_fetch_s": "Recorded audio fetch; may include synthesis or cache delivery. Cache state is unknown.",
    "decode_and_output_setup_residual_s": "First output minus client return minus audio fetch; combined residual, not isolated decode time.",
    "paced_output_s": "First sink-write entry to completion of the final delivered PCM block, or saved sink-delivery interval; first timestamp precedes the first paced sleep in the existing runner.",
    "total_s": "Activation through cleanup, excluding preflight, where the source records that boundary.",
    "stage_overlap_s": "Sum of STT/conversation/TTS-metadata intervals minus their union. Do not add overlapping durations.",
    "pipeline_unaccounted_s": "Pipeline duration minus the union of all three contained stage intervals; unavailable when a stage is missing.",
    "input_feed_s": "Elapsed time spent feeding saved PCM at real-time pace, including local scheduling.",
    "wire_upload_span_s": "First to last completed PCM send; includes input pacing/backpressure, not isolated network transfer time.",
    "post_eos_stt_s": "Signed offset of STT end after completed explicit EOS send; negative means STT ended earlier. Neither sign isolates model compute.",
    "decode_s": "Explicit local audio decoder start to finish, when instrumented.",
    "activation_to_first_delivered_pcm_s": "First completed silent-sink block relative to activation; not audible onset or hardware latency.",
    "output_pacing_overhead_s": "Signed difference between paced sink elapsed time and PCM media duration; never added to media duration again.",
}


class InputError(ValueError):
    """Message must be a public-safe constant, never user data."""


def number(value):
    try:
        return float(value) if type(value) in (float, int) and math.isfinite(value) and 0 <= value <= MAX_ELAPSED_SECONDS else None
    except OverflowError:
        return None


def boolean(value):
    return value if type(value) is bool else None


def read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise InputError("input_must_be_a_regular_local_file")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise InputError("input_byte_limit_exceeded")
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise InputError("input_byte_limit_exceeded")
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise InputError("invalid_json_input") from None
    if not isinstance(data, dict):
        raise InputError("input_must_be_a_json_object")
    return data, hashlib.sha256(raw).hexdigest()


def save_private(path, value):
    path = Path(path)
    local = (ROOT / ".local").resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(local):
        raise InputError("output_must_be_fresh_and_inside_workspace_local")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        raise InputError("output_must_be_fresh_and_writable") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def interval_union(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return sum(end-start for start, end in merged)


def analyze_trial(data, trial_index, limit_seconds=30):
    issues, metrics = [], {}
    activation = number(data.get("activation_seconds"))
    preflight = activation if activation is not None else number(data.get("preflight_seconds"))
    events = data.get("events", data.get("event_timings", []))
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise InputError("invalid_or_oversized_event_list")
    boundaries = {}
    error_event_present = False
    for event in events:
        if not isinstance(event, dict):
            issues.append("malformed_event")
            continue
        name = event.get("stage", event.get("type"))
        if not isinstance(name, str) or name not in BOUNDARIES:
            continue
        error_event_present = error_event_present or name == "error"
        if "seconds_from_activation" in event:
            seconds = number(event["seconds_from_activation"])
        else:
            elapsed = number(event.get("elapsed"))
            seconds = elapsed - activation if elapsed is not None and activation is not None else None
        if seconds is None or seconds < 0:
            issues.append("invalid_or_unknown_event_time_origin")
            continue
        boundaries.setdefault(name, []).append(seconds)
    times = {}
    for name, values in boundaries.items():
        if len(values) == 1:
            times[name] = values[0]
        else:
            issues.append("duplicate_boundary:" + name)

    def duration(key, start, end):
        if start is None or end is None:
            metrics[key] = None
        elif end < start:
            metrics[key] = None
            issues.append("negative_interval:" + key)
        else:
            metrics[key] = round(end-start, 6)

    for key, (start, end) in STAGES.items():
        duration(key, times.get(start), times.get(end))
    metrics["preflight_s"] = preflight
    total = number(data.get("turn_seconds"))
    outer_total = number(data.get("total_seconds"))
    if total is None and outer_total is not None and activation is not None:
        total = outer_total-activation
        if total < 0:
            total = None
            issues.append("negative_interval:total_s")
    if total is not None and outer_total is not None and activation is not None and abs(total-(outer_total-activation)) > 0.001:
        issues.append("inconsistent_total_boundaries")
    if total is not None:
        for name, values in boundaries.items():
            if any(value > total + 0.001 for value in values):
                issues.append("event_outside_turn:" + name)
    metrics["total_s"] = total
    metrics["activation_to_stt_start_s"] = times.get("stt-start")
    metrics["activation_to_pipeline_end_s"] = times.get("run-end")

    def relative(name):
        value = number(data.get(name))
        return value-activation if value is not None and activation is not None and value >= activation else None

    first, last = relative("first_output_pcm_seconds"), relative("last_output_pcm_seconds")
    speaking = []
    states = data.get("states", [])
    if isinstance(states, list) and len(states) <= MAX_EVENTS:
        for state in states:
            if isinstance(state, dict) and state.get("state") == "speaking":
                elapsed = number(state.get("elapsed"))
                if elapsed is not None and activation is not None and elapsed >= activation:
                    speaking.append(elapsed-activation)
    explicit_return = relative("assist_run_returned_seconds")
    returned = explicit_return if explicit_return is not None else speaking[0] if len(speaking) == 1 else None
    if len(speaking) > 1:
        issues.append("duplicate_speaking_state")
    fetch = data.get("fetch")
    fetch_seconds = number(fetch.get("seconds")) if isinstance(fetch, dict) else None
    fetch_start, fetch_end = relative("fetch_started_seconds"), relative("fetch_finished_seconds")
    duration("audio_fetch_instrumented_s", fetch_start, fetch_end)
    if metrics["audio_fetch_instrumented_s"] is not None:
        fetch_seconds = metrics["audio_fetch_instrumented_s"]
    metrics["audio_fetch_s"] = fetch_seconds
    metrics["activation_to_first_output_s"] = first
    metrics["activation_to_last_output_s"] = last
    duration("run_end_to_client_return_s", times.get("run-end"), returned)
    duration("decode_and_output_setup_residual_s", returned+fetch_seconds if returned is not None and fetch_seconds is not None else None, first)
    duration("paced_output_s", first, last)
    if first is None or last is None:
        metrics["paced_output_s"] = number(data.get("sink_delivery_seconds"))
    duration("cleanup_after_output_s", last, total)
    duration("assist_run_s", relative("assist_run_started_seconds"), explicit_return)
    duration("input_feed_s", relative("input_feed_started_seconds"), relative("input_feed_finished_seconds"))
    duration("wire_upload_span_s", relative("first_wire_pcm_sent_seconds"), relative("last_wire_pcm_sent_seconds"))
    eos = relative("wire_end_marker_sent_seconds")
    metrics["post_eos_stt_s"] = round(times["stt-end"]-eos, 6) if eos is not None and "stt-end" in times else None
    if metrics["post_eos_stt_s"] is not None and metrics["post_eos_stt_s"] < 0:
        issues.append("stt_ended_before_explicit_eos")
    decode_start, decode_end = relative("decode_started_seconds"), relative("decode_finished_seconds")
    duration("decode_s", decode_start, decode_end)
    duration("output_setup_after_decode_s", decode_end, first)
    duration("tts_metadata_end_to_fetch_start_s", times.get("tts-end"), fetch_start)
    delivered = relative("first_delivered_pcm_seconds")
    metrics["activation_to_first_delivered_pcm_s"] = delivered
    duration("first_sink_block_delivery_s", first, delivered)
    output = data.get("output")
    media = number(output.get("seconds")) if isinstance(output, dict) else None
    metrics["output_media_s"] = media
    metrics["output_pacing_overhead_s"] = round(metrics["paced_output_s"]-media, 6) if metrics["paced_output_s"] is not None and media is not None else None

    intervals = [(times[a], times[b]) for key, (a, b) in STAGES.items()
                 if key != "pipeline_s" and metrics[key] is not None]
    union = interval_union(intervals)
    metrics["known_stage_union_s"] = round(union, 6) if intervals else None
    metrics["stage_overlap_s"] = round(sum(b-a for a, b in intervals)-union, 6) if intervals else None
    if metrics["stage_overlap_s"] and metrics["stage_overlap_s"] > 0.001:
        issues.append("overlapping_pipeline_stages")
    contained = metrics["pipeline_s"] is not None and all(
        start >= times["run-start"] and end <= times["run-end"] for start, end in intervals)
    if metrics["pipeline_s"] is not None and intervals and not contained:
        issues.append("stage_outside_pipeline")
    metrics["pipeline_unaccounted_s"] = round(metrics["pipeline_s"]-union, 6) if len(intervals) == 3 and contained else None

    final = data.get("final_status", {})
    reason = data.get("final_reason", final.get("reason") if isinstance(final, dict) else None)
    state = data.get("final_state", final.get("state") if isinstance(final, dict) else None)
    reason_supplied = "final_reason" in data or isinstance(final, dict) and "reason" in final
    state_supplied = "final_state" in data or isinstance(final, dict) and "state" in final
    if state_supplied and state != "stopped":
        issues.append("reported_failure_state")
    if reason_supplied and reason != "turn_complete":
        issues.append("reported_failure_reason")
    timeout = isinstance(reason, str) and ("timeout" in reason.lower() or "timed_out" in reason.lower())
    if timeout:
        issues.append("reported_timeout")
    quality = {"success": boolean(data.get("success")),
               "transcript_match": boolean(data.get("transcript_matches_input")),
               "answer_match": boolean(data.get("answer_matches_requested_phrase"))}
    quality_pass = False if False in quality.values() else True if all(v is True for v in quality.values()) else None
    under_budget = total <= limit_seconds if total is not None else None
    contradictory = any(issue.startswith(("negative_interval:", "duplicate_boundary:",
        "invalid_or_unknown_event_time_origin", "inconsistent_total_boundaries", "stage_outside_pipeline",
        "duplicate_speaking_state", "event_outside_turn:")) for issue in issues)
    final_failure = "reported_failure_state" in issues or "reported_failure_reason" in issues
    return {"trial_index": trial_index, "metrics": metrics, "quality": quality,
            "quality_pass": quality_pass, "under_total_budget": under_budget,
            "accepted": quality_pass is True and under_budget is True and not (timeout or error_event_present or contradictory or final_failure),
            "timing_boundaries_consistent": not contradictory,
            "core_stage_timings_complete": all(metrics[key] is not None for key in STAGES),
            "reported_timeout": timeout, "error_event_present": error_event_present,
            "measurement_sources": {"client_return": "explicit_assist_return" if explicit_return is not None else "speaking_state_fallback" if returned is not None else "unavailable",
                                    "audio_fetch": "explicit_fetch_interval" if metrics["audio_fetch_instrumented_s"] is not None else "recorded_fetch_duration" if fetch_seconds is not None else "unavailable"},
            "missing_metrics": sorted(key for key, value in metrics.items() if value is None),
            "issues": sorted(set(issues))}


def analyze(paths, limit_seconds=30):
    if not 1 <= len(paths) <= MAX_INPUTS:
        raise InputError("input_count_must_be_1_to_50")
    if number(limit_seconds) is None or not 0 < limit_seconds <= 3600:
        raise InputError("invalid_total_budget")
    trials, seen = [], set()
    for index, path in enumerate(paths, 1):
        data, digest = read_json(path)
        if digest in seen:
            raise InputError("duplicate_input_artifact")
        seen.add(digest)
        trials.append(analyze_trial(data, index, limit_seconds))
    aggregate = {}
    for key in trials[0]["metrics"]:
        values = [row["metrics"][key] for row in trials if row["metrics"][key] is not None]
        aggregate[key] = {"measured_count": len(values), "missing_count": len(trials)-len(values),
                          "min_s": min(values) if values else None,
                          "median_s": statistics.median(values) if values else None,
                          "max_s": max(values) if values else None}
    return {"schema_version": 1, "mode": "offline_analysis", "network_actions": 0,
            "sample_count": len(trials), "total_budget_seconds": limit_seconds,
            "accepted_count": sum(row["accepted"] for row in trials),
            "timeout_count": sum(row["reported_timeout"] for row in trials),
            "definitions": METRIC_DEFINITIONS, "trials": trials, "aggregate": aggregate,
            "limitations": ["One input artifact per trial; summary and event-timings files are alternative views, not independent samples.",
                "No percentile, reliability, cache-state or cold-start guarantee follows from these samples.",
                "Stage durations may overlap; the report computes a union instead of adding them.",
                "Missing measurements stay null. Direct model timings cannot substitute for HA stage timing."]}


def private_input(path):
    original = Path(path)
    if not original.is_absolute():
        original = ROOT / original
    resolved = original.resolve()
    if original.is_symlink() or not resolved.is_relative_to((ROOT / ".local").resolve()) or not resolved.is_file():
        raise InputError("plan_inputs_must_be_regular_private_files")
    if resolved.stat().st_size > MAX_INPUT_BYTES:
        raise InputError("plan_input_byte_limit_exceeded")
    with resolved.open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise InputError("plan_input_byte_limit_exceeded")
    return resolved, raw


def fingerprint(role, path):
    resolved, raw = private_input(path)
    return {"role": role, "path": str(resolved), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}, raw


def plan(spec):
    allowed = {"schema_version", "runner", "python", "config", "expected_files", "inputs", "repetitions",
               "per_trial_timeout_seconds", "overall_timeout_seconds", "stt_engine", "output_root"}
    if not isinstance(spec, dict) or set(spec)-allowed or type(spec.get("schema_version")) is not int or spec.get("schema_version") != 1:
        raise InputError("invalid_plan_schema")
    inputs = spec.get("inputs")
    repetitions = spec.get("repetitions", 1)
    expected_files = spec.get("expected_files", [])
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= 10 or not all(isinstance(p, str) for p in inputs):
        raise InputError("plan_requires_1_to_10_private_wavs")
    if type(repetitions) is not int or repetitions < 1 or len(inputs)*repetitions > 10:
        raise InputError("plan_trial_limit_exceeded")
    if not isinstance(expected_files, list) or len(expected_files) > 5 or not all(isinstance(p, str) for p in expected_files):
        raise InputError("invalid_expected_files")
    per_trial = number(spec.get("per_trial_timeout_seconds", 60))
    overall = number(spec.get("overall_timeout_seconds", 300))
    if per_trial is None or overall is None or not 1 <= per_trial <= 60 or not per_trial <= overall <= 300:
        raise InputError("invalid_proposed_deadlines")
    engine = spec.get("stt_engine")
    if not isinstance(engine, str) or not engine.startswith("stt.") or not engine[4:] or not all(c.isascii() and (c.isalnum() or c == "_") for c in engine[4:]):
        raise InputError("explicit_stt_entity_required")
    if not all(isinstance(spec.get(key), str) for key in ("runner", "config", "output_root")):
        raise InputError("missing_private_plan_paths")
    interpreter_value = spec.get("python", sys.executable)
    if not isinstance(interpreter_value, str):
        raise InputError("invalid_interpreter")
    # Preserve a virtualenv's lexical executable path: resolving its symlink
    # would launch the base Python and silently discard the virtualenv.
    interpreter = Path(os.path.abspath(interpreter_value))
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise InputError("interpreter_must_be_an_existing_executable")
    output = Path(spec["output_root"])
    output = (ROOT / output).resolve() if not output.is_absolute() else output.resolve()
    if not output.is_relative_to((ROOT / ".local").resolve()) or output.exists():
        raise InputError("run_output_root_must_be_fresh_and_private")
    runner, _ = fingerprint("runner", spec["runner"])
    if Path(runner["path"]).suffix != ".py":
        raise InputError("runner_must_be_a_reviewed_private_python_script")
    config, _ = fingerprint("config", spec["config"])
    required = [runner, config]
    for value in expected_files:
        row, _ = fingerprint("expectation", value)
        required.append(row)
    recordings = []
    for value in inputs:
        row, raw = fingerprint("input_wav", value)
        try:
            with wave.open(io.BytesIO(raw), "rb") as source:
                if (source.getframerate(), source.getnchannels(), source.getsampwidth(), source.getcomptype()) != (16000, 1, 2, "NONE"):
                    raise InputError("plan_audio_must_be_mono_pcm16_16khz")
                frames = source.getnframes()
                pcm = source.readframes(frames)
            if not 1600 <= frames <= 480000 or len(pcm) != frames*2:
                raise InputError("plan_audio_duration_or_length_invalid")
        except (wave.Error, EOFError):
            raise InputError("invalid_plan_wav") from None
        row.update(pcm_sha256=hashlib.sha256(pcm).hexdigest(), audio_seconds=frames/16000)
        recordings.append(row)
    trials = []
    for _ in range(repetitions):
        for recording in recordings:
            index = len(trials)+1
            directory = output / ("trial-%02d" % index)
            trials.append({"trial_index": index, "deadline_seconds": per_trial,
                           "output_directory": str(directory),
                           "argv": [str(interpreter), runner["path"], "--config", config["path"],
                                    "--input", recording["path"], "--output", str(directory), "--stt-engine", engine],
                           "required_files": [*required, recording]})
    return {"schema_version": 1, "kind": "voice_stage_serial_plan", "execution_enabled": False,
            "caps_enforced": False, "serial": True, "output_root": str(output),
            "interpreter": {"path": str(interpreter), "realpath": str(interpreter.resolve()),
                            "validated": "exists and executable; dependency versions not probed"},
            "limits": {"trial_count": len(trials), "per_trial_timeout_seconds": per_trial,
                       "overall_timeout_seconds": overall}, "trials": trials,
            "resource_footprint": {"concurrent_replay_processes": 1, "physical_audio_devices": 0,
                                   "optional_decoder_child_processes": 1,
                                   "intended_new_deployments": 0, "intended_pipeline_mutations": 0,
                                   "network_usage": "Future replay through the existing selected local HA pipeline only.",
                                   "input_audio_pacing": "Real-time PCM input; HA STT stage includes input and endpointing.",
                                   "output_audio_pacing": "Existing runner delivers decoded PCM to a silent sink at sample-rate pace."},
            "supervisor_requirements": ["Require explicit --execute and --local-backends-confirmed authorization for this run.",
                "Recheck every required file hash and that each output directory is fresh immediately before execution.",
                "Review the pinned private runner for the declared silent-sink and no-pipeline-mutation contract; hashes alone do not prove behavior.",
                "Enforce serial execution and per-child/overall wall-clock deadlines, including process-group cleanup; this plan enforces no runtime cap.",
                "Capture stdout/stderr privately and stop after timeout or failed trial; do not retry automatically.",
                "The overall deadline may stop the sequence before all proposed trials finish."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    analysis = actions.add_parser("analyze", help="analyze saved JSON only; no network or inference")
    analysis.add_argument("--input", action="append", required=True, type=Path)
    analysis.add_argument("--output", required=True, type=Path)
    analysis.add_argument("--limit-seconds", type=float, default=30)
    planning = actions.add_parser("plan", help="prepare private replay argv and hashes; never execute")
    planning.add_argument("--spec", required=True, type=Path)
    planning.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "analyze":
            result = analyze(args.input, args.limit_seconds)
            receipt = {"mode": "offline_analysis", "sample_count": result["sample_count"],
                       "accepted_count": result["accepted_count"], "network_actions": 0}
        else:
            spec, _ = read_json(args.spec)
            result = plan(spec)
            if args.output.resolve().is_relative_to(Path(result["output_root"])):
                raise InputError("plan_file_must_be_outside_fresh_run_root")
            receipt = {"mode": "offline_plan", "trial_count": len(result["trials"]),
                       "execution_enabled": False, "caps_enforced": False, "network_actions": 0}
        save_private(args.output, result)
        print(json.dumps(receipt))
        return 0
    except (InputError, OSError):
        print(json.dumps({"error": "invalid_input_or_output", "network_actions": 0}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
