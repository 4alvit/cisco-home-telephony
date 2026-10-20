#!/usr/bin/env python3
"""Evaluate a frozen wake detector on private WAV files, entirely offline.

No microphone, phone, network, resampling, threshold search, or end padding.
The demo uses a scripted detector and is not evidence of model accuracy.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import platform
import random
import re
import stat
import struct
import sys
import time
import wave
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from voice_assistant import audio, wake

HOP_SECONDS = audio.HOP_SAMPLES / audio.SAMPLE_RATE
FIRST_SCORE_SECONDS = wake.CLASSIFIER_FRAMES * HOP_SECONDS
MAX_CLIPS = 100
MAX_CLIP_SECONDS = 600
MAX_TOTAL_SECONDS = 1800
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_EVENTS = 100
DOMAINS = {"noise", "tv", "music", "speech", "silence", "other"}
LABEL_SOURCES = {"recorded", "synthetic"}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate JSON keys are not allowed")
        result[key] = value
    return result


def _json(data: bytes) -> Any:
    return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON number")))


def _number(value: Any) -> bool:
    return type(value) in (float, int) and math.isfinite(value)


def _private_root(workspace: str | Path) -> Path:
    workspace = Path(workspace).resolve(strict=True)
    root = workspace / ".local"
    _require(root.is_dir() and not root.is_symlink(), "Workspace needs a real .local directory")
    return root.resolve(strict=True)


def _inside(path: str | Path, root: Path, *, base: Path, exists: bool = True) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = base / candidate
    resolved = candidate.resolve(strict=exists)
    _require(resolved != root and resolved.is_relative_to(root), "Path must remain inside workspace .local")
    # Reject symlink components, including a link whose target remains private.
    lexical = Path(os.path.abspath(candidate))
    _require(lexical.is_relative_to(root), "Path must remain inside workspace .local")
    current = root
    for part in lexical.relative_to(root).parts:
        current = current / part
        _require(not current.is_symlink(), "Symlink paths are not allowed")
    return resolved


def _read(path: Path, limit: int, *, owner_only: bool = True) -> bytes:
    info = path.stat()
    _require(stat.S_ISREG(info.st_mode), "Input must be a regular file")
    if owner_only:
        _require(info.st_uid == os.getuid() and not info.st_mode & 0o077,
                 "Manifest and WAV inputs must be owner-only files")
    _require(info.st_size <= limit, "Input file exceeds byte limit")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    _require(len(data) <= limit, "Input file exceeds byte limit")
    return data


def _new_output(output: str | Path, root: Path) -> Path:
    path = _inside(output, root, base=root.parent, exists=False)
    _require(not path.exists(), "Output already exists; choose a new directory")
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _write(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)


def _save(path: Path, data: Any) -> None:
    _write(path, (json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode())


def _load_inputs(manifest_path: str | Path, root: Path, late: float) -> tuple[list[dict], str]:
    path = _inside(manifest_path, root, base=root.parent)
    raw = _read(path, 1024 * 1024)
    manifest = _json(raw)
    _require(isinstance(manifest, dict) and set(manifest) == {"format_version", "clips"}
             and type(manifest["format_version"]) is int and manifest["format_version"] == 1,
             "Manifest requires format_version=1 and clips")
    rows = manifest["clips"]
    _require(isinstance(rows, list) and 0 < len(rows) <= MAX_CLIPS, "Invalid clip count")
    clips, ids = [], set()
    total_bytes = 0
    total_seconds = 0.0
    for row in rows:
        _require(isinstance(row, dict) and set(row) == {
            "id", "path", "label", "domain", "label_source", "expected_events"}, "Invalid clip fields")
        ident = row["id"]
        _require(isinstance(ident, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", ident)
                 and ident not in ids, "Invalid or duplicate clip ID")
        ids.add(ident)
        _require(row["label"] in ("positive", "negative", "unlabeled"), "Invalid explicit label")
        _require(isinstance(row["domain"], str) and row["domain"] in DOMAINS, "Invalid domain")
        _require(isinstance(row["label_source"], str) and row["label_source"] in LABEL_SOURCES, "Invalid label source")
        _require(isinstance(row["path"], str) and bool(row["path"]), "Invalid WAV path")
        wav_path = _inside(row["path"], root, base=path.parent)
        data = _read(wav_path, MAX_FILE_BYTES)
        total_bytes += len(data)
        _require(total_bytes <= MAX_TOTAL_BYTES, "Corpus exceeds byte limit")
        try:
            with wave.open(io.BytesIO(data), "rb") as source:
                _require(source.getnchannels() == 1 and source.getsampwidth() == 2
                         and source.getframerate() == 16000 and source.getcomptype() == "NONE",
                         "WAV must be uncompressed mono PCM16 at 16000 Hz")
                frames = source.getnframes()
                _require(0 < frames <= MAX_CLIP_SECONDS * 16000, "Invalid WAV duration")
                pcm = source.readframes(frames + 1)
                _require(len(pcm) == frames * 2, "Truncated or inconsistent WAV data")
        except (wave.Error, EOFError) as error:
            raise ValueError("Invalid PCM WAV") from error
        duration = frames / audio.SAMPLE_RATE
        total_seconds += duration
        _require(total_seconds <= MAX_TOTAL_SECONDS, "Corpus exceeds duration limit")
        whole_hops = frames // audio.HOP_SAMPLES
        processed = whole_hops * HOP_SECONDS
        events = row["expected_events"]
        _require(isinstance(events, list) and len(events) <= MAX_EVENTS, "Invalid expected events")
        _require(bool(events) if row["label"] == "positive" else not events,
                 "Positive labels require events; negative and unlabeled labels require none")
        seen = set()
        for event in events:
            _require(isinstance(event, dict) and set(event) == {"start_seconds", "end_seconds"}, "Invalid event fields")
            start, end = event["start_seconds"], event["end_seconds"]
            _require(_number(start) and _number(end) and 0 <= start <= end, "Invalid event interval")
            _require((start, end) not in seen, "Duplicate expected event")
            seen.add((start, end))
            _require(start >= FIRST_SCORE_SECONDS and end + late <= processed + 1e-9,
                     "Expected event window lacks complete scored coverage")
        clips.append({**row, "resolved_path": str(wav_path), "wav_sha256": _hash(data),
                      "pcm": pcm, "duration_seconds": duration, "processed_seconds": processed,
                      "complete_hops": whole_hops,
                      "discarded_tail_samples": frames % audio.HOP_SAMPLES})
    return clips, _hash(raw)


def _bundle(bundle_path: str | Path, root: Path) -> tuple[Path, dict]:
    path = _inside(bundle_path, root, base=root.parent)
    raw = _read(path, 1024 * 1024)
    data = _json(raw)
    _require(isinstance(data, dict) and data.get("frontend") == wake.FRONTEND_ID
             and type(data.get("format_version")) is int and data["format_version"] == 1, "Invalid frozen bundle")
    threshold = data.get("threshold")
    _require(_number(threshold) and 0 < threshold <= 1, "Invalid frozen threshold")
    models = data.get("models")
    _require(isinstance(models, dict) and set(models) == {"mel", "embedding", "classifier"}, "Invalid model roles")
    verified = {}
    for role, entry in models.items():
        _require(isinstance(entry, dict) and set(entry) == {"path", "sha256"}, "Invalid model entry")
        name = entry["path"]
        _require(isinstance(name, str) and bool(name) and not Path(name).is_absolute()
                 and ".." not in Path(name).parts and Path(name).suffix.lower() == ".onnx", "Invalid model path")
        model_path = _inside(name, root, base=path.parent)
        model = _read(model_path, 64 * 1024 * 1024, owner_only=False)
        _require(_hash(model) == entry["sha256"], "Model SHA256 mismatch")
        verified[role] = {"sha256": _hash(model), "bytes": len(model)}
    return path, {"bundle_sha256": _hash(raw), "models": verified, "threshold": float(threshold)}


def _matches(events: list[dict], detections: list[float], late: float) -> list[dict]:
    """Maximum cardinality matching; handles overlapping and nested windows."""
    owners: dict[int, int] = {}

    def assign(event_index: int, seen: set[int]) -> bool:
        event = events[event_index]
        for detection_index, detected in enumerate(detections):
            if detection_index in seen or not event["start_seconds"] - 1e-9 <= detected <= event["end_seconds"] + late + 1e-9:
                continue
            seen.add(detection_index)
            if detection_index not in owners or assign(owners[detection_index], seen):
                owners[detection_index] = event_index
                return True
        return False

    for event_index in range(len(events)):
        assign(event_index, set())
    return [{"event_index": event_index, "detection_index": detection_index,
             "latency_from_end_seconds": detections[detection_index] - events[event_index]["end_seconds"]}
            for detection_index, event_index in sorted(owners.items())]


def _metrics(clips: list[dict]) -> dict:
    negatives = [clip for clip in clips if clip["label"] == "negative"]
    positives = [clip for clip in clips if clip["label"] == "positive"]
    exposure = sum(clip["scored_exposure_seconds"] for clip in negatives)
    false_accepts = sum(clip["false_accepts"] for clip in negatives)
    tp = sum(clip["true_positives"] for clip in positives)
    fn = sum(clip["false_negatives"] for clip in positives)
    return {"clips": len(clips), "positive_clips": len(positives), "negative_clips": len(negatives),
            "unlabeled_clips": sum(clip["label"] == "unlabeled" for clip in clips),
            "duration_seconds": sum(clip["duration_seconds"] for clip in clips),
            "labeled_positive_events": tp + fn,
            "true_positives": tp, "false_negatives": fn,
            "event_recall": tp / (tp + fn) if tp + fn else None,
            "event_miss_rate": fn / (tp + fn) if tp + fn else None,
            "unmatched_positive_detections": sum(clip["unmatched_positive_detections"] for clip in positives),
            "negative_false_accepts": false_accepts,
            "negative_scored_exposure_seconds": exposure,
            "negative_full_duration_seconds": sum(clip["duration_seconds"] for clip in negatives),
            "negative_warmup_unscored_seconds": sum(clip["warmup_unscored_seconds"] for clip in negatives),
            "negative_discarded_tail_seconds": sum(clip["discarded_tail_samples"] / 16000 for clip in negatives),
            "false_accepts_per_hour": false_accepts * 3600 / exposure if exposure else None}


def evaluate_manifest(manifest_path: str | Path, *, workspace: str | Path, output: str | Path,
                      bundle_path: str | Path | None = None, detector_factory: Callable | None = None,
                      late_tolerance_seconds: float = 1.0, mode: str = "onnx") -> dict:
    """Validate the entire corpus before loading a detector or creating output.

    Injected factories are accepted only in explicitly non-accuracy demo mode.
    Paths in a manifest are relative to that manifest. Output is workspace-relative.
    """
    _require(_number(late_tolerance_seconds) and 0 <= late_tolerance_seconds <= 30, "Invalid late tolerance")
    _require(mode in ("onnx", "scripted_demo"), "Invalid evaluation mode")
    _require((mode == "onnx" and bundle_path is not None and detector_factory is None)
             or (mode == "scripted_demo" and bundle_path is None and detector_factory is not None),
             "Use a frozen bundle for ONNX or an explicit scripted demo factory")
    root = _private_root(workspace)
    output_path = _inside(output, root, base=root.parent, exists=False)
    _require(not output_path.exists(), "Output already exists; choose a new directory")
    clips, manifest_hash = _load_inputs(manifest_path, root, float(late_tolerance_seconds))
    accuracy_evidence = mode == "onnx" and any(
        (clip["label"] == "positive" and bool(clip["expected_events"]))
        or (clip["label"] == "negative" and clip["complete_hops"] >= wake.CLASSIFIER_FRAMES)
        for clip in clips)
    bundle_info = None
    verified_bundle = None
    if mode == "onnx":
        verified_bundle, bundle_info = _bundle(bundle_path, root)
    versions = {}
    for package in ("numpy", "onnxruntime"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    provenance = {"mode": mode, "accuracy_evidence": accuracy_evidence, "manifest_sha256": manifest_hash,
                  "bundle": bundle_info, "source_sha256": {
                      "wake.py": _hash(Path(wake.__file__).read_bytes()),
                      "audio.py": _hash(Path(audio.__file__).read_bytes()),
                      "wake_evaluate.py": _hash(Path(__file__).read_bytes())},
                  "frontend": {"id": wake.FRONTEND_ID, "sample_rate": audio.SAMPLE_RATE,
                      "channels": audio.CHANNELS, "sample_width": audio.SAMPLE_WIDTH,
                      "hop_samples": audio.HOP_SAMPLES, "hop_bytes": audio.HOP_BYTES,
                      "mel_context_samples": wake.MEL_CONTEXT_SAMPLES, "mel_frames": wake.MEL_FRAMES,
                      "mel_bins": wake.MEL_BINS, "embedding_size": wake.EMBEDDING_SIZE,
                      "initial_mel_value": 1.0, "mel_divisor": 10.0, "mel_offset": 2.0,
                      "embedding_input_shape": [1, wake.MEL_FRAMES, wake.MEL_BINS, 1],
                      "classifier_input_shape": [1, wake.CLASSIFIER_FRAMES, wake.EMBEDDING_SIZE],
                      "pcm_amplitude": "signed_int16_unscaled", "extra_score_transform": False,
                      "classifier_frames": wake.CLASSIFIER_FRAMES, "refractory_hops": wake.REFRACTORY_HOPS,
                      "refractory_seconds": wake.REFRACTORY_HOPS * HOP_SECONDS,
                      "required_context_seconds": FIRST_SCORE_SECONDS},
                  "runtime": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                      "system": platform.system(), "release": platform.release(), "machine": platform.machine(),
                      "packages": versions},
                  "settings": {"late_tolerance_seconds": late_tolerance_seconds, "padding": False,
                      "resampling": False, "reset_each_clip": True, "one_feed_per_hop": True,
                      "realtime_factor_scope": "Hop feeding and score collection only; excludes input validation, model setup, reset, matching and output writing.",
                      "max_clips": MAX_CLIPS, "max_clip_seconds": MAX_CLIP_SECONDS,
                      "max_total_seconds": MAX_TOTAL_SECONDS, "max_total_bytes": MAX_TOTAL_BYTES,
                      "max_file_bytes": MAX_FILE_BYTES, "max_events_per_clip": MAX_EVENTS},
                  "started_unix_seconds": time.time()}
    setup_start = time.perf_counter()
    if mode == "onnx":
        loaded = []

        def session_factory(model: bytes):
            loaded.append(_hash(model))
            return wake._onnx_session(model)

        detector = wake.FrozenOnnxWakeDetector.from_bundle(verified_bundle, private_root=root,
                                                          session_factory=session_factory)
        _require(loaded == [bundle_info["models"][role]["sha256"] for role in ("mel", "embedding", "classifier")]
                 and detector.threshold == bundle_info["threshold"], "Bundle changed while loading")
        _require(_hash(_read(verified_bundle, 1024 * 1024)) == bundle_info["bundle_sha256"],
                 "Bundle manifest changed while loading")
        provenance["loaded_model_sha256"] = dict(zip(("mel", "embedding", "classifier"), loaded))
        provenance["actual_detector_threshold"] = detector.threshold
        provenance["runtime"]["execution_providers"] = {
            role: session.get_providers() for role, session in detector._sessions.items()}
    else:
        detector = detector_factory()
    provenance["detector_setup_seconds"] = time.perf_counter() - setup_start
    output_path = _new_output(output_path, root)
    _save(output_path / "provenance.private.json", provenance)
    results = []
    try:
        for clip in clips:
            detector.reset()
            scored, detections = [], []
            started = time.perf_counter()
            for hop_index in range(clip["complete_hops"]):
                offset = hop_index * audio.HOP_BYTES
                detected = detector.feed(clip["pcm"][offset:offset + audio.HOP_BYTES])
                score = detector.last_score
                ready = hop_index + 1 >= wake.CLASSIFIER_FRAMES
                _require(bool(detector.ready) == ready, "Detector readiness differs from frozen cadence")
                _require((score is not None) == ready, "Detector score availability differs from frozen cadence")
                _require(type(detected) is bool and (ready or not detected), "Invalid detection before scoring")
                if ready:
                    _require(_number(score) and 0 <= score <= 1, "Invalid detector score")
                    elapsed = (hop_index + 1) * HOP_SECONDS
                    scored.append({"time_seconds": elapsed, "score": float(score), "detected": detected})
                    if detected:
                        detections.append(elapsed)
            elapsed = time.perf_counter() - started
            matches = _matches(clip["expected_events"], detections, late_tolerance_seconds) if clip["label"] == "positive" else []
            result = {key: value for key, value in clip.items() if key != "pcm"}
            result.update(first_score_seconds=scored[0]["time_seconds"] if scored else None,
                          scored_exposure_seconds=len(scored) * HOP_SECONDS,
                          warmup_unscored_seconds=(clip["complete_hops"] - len(scored)) * HOP_SECONDS,
                          detections_seconds=detections, scores=scored, matches=matches,
                          true_positives=len(matches) if clip["label"] == "positive" else None,
                          false_negatives=len(clip["expected_events"]) - len(matches) if clip["label"] == "positive" else None,
                          unmatched_positive_detections=len(detections) - len(matches) if clip["label"] == "positive" else None,
                          false_accepts=len(detections) if clip["label"] == "negative" else None,
                          processing_seconds=elapsed,
                          realtime_factor=elapsed / clip["duration_seconds"])
            results.append(result)
        summary = {"format_version": 1, "mode": mode, "accuracy_evidence": accuracy_evidence,
                   "overall": _metrics(results), "strata": []}
        mixed_sources = len({clip["label_source"] for clip in results}) > 1
        summary["accuracy_aggregation"] = "separate_label_sources" if mixed_sources else "single_label_source"
        if mixed_sources:
            # Combining synthetic fixtures with recordings would make the pooled
            # accuracy/rate easy to misread as evidence about real environments.
            for key in ("labeled_positive_events", "true_positives", "false_negatives", "event_recall", "event_miss_rate",
                        "unmatched_positive_detections", "negative_false_accepts",
                        "negative_scored_exposure_seconds", "negative_full_duration_seconds",
                        "negative_warmup_unscored_seconds", "negative_discarded_tail_seconds",
                        "false_accepts_per_hour"):
                summary["overall"][key] = None
        for domain, label_source in sorted({(clip["domain"], clip["label_source"]) for clip in results}):
            summary["strata"].append({"domain": domain, "label_source": label_source,
                **_metrics([clip for clip in results if (clip["domain"], clip["label_source"]) == (domain, label_source)])})
        summary["processing_seconds"] = sum(clip["processing_seconds"] for clip in results)
        summary["realtime_factor"] = summary["processing_seconds"] / summary["overall"]["duration_seconds"]
        summary["interpretation"] = ("Scripted reporting demonstration; not model accuracy evidence." if mode == "scripted_demo" else
            "Only explicit labels contribute outcomes. Synthetic and recorded strata are distinct. No reliability guarantee follows from this sample.")
        summary["exposure_definition"] = "Scored opportunities times 0.08s; first score at 1.28s counts 0.08s. Warmup and partial tail excluded. Refractory scoring remains included."
        result = {"summary": summary, "clips": results, "provenance": provenance}
        _save(output_path / "results.private.json", result)
        _save(output_path / "summary.safe.json", summary)
        return result
    except BaseException as error:
        _save(output_path / "failure.safe.json", {"success": False, "error_type": type(error).__name__,
              "completed_clips": len(results), "accuracy_report_complete": False})
        _save(output_path / "partial-results.private.json", {"clips": results, "provenance": provenance})
        raise


def generate_fixtures(output: str | Path, *, workspace: str | Path) -> Path:
    """Create deterministic generic audio; none of these files contains speech."""
    root = _private_root(workspace)
    directory = _new_output(output, root)
    rng = random.Random(20261019)
    samples = 3 * audio.SAMPLE_RATE
    signals = {"silence": [0] * samples,
               "noise": [rng.randint(-2000, 2000) for _ in range(samples)],
               "tone": [round(2000 * math.sin(2 * math.pi * 440 * n / 16000)) for n in range(samples)]}
    clips = []
    for name, values in signals.items():
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(struct.pack("<" + "h" * samples, *values))
        _write(directory / (name + ".wav"), buffer.getvalue())
        clips.append({"id": name, "path": name + ".wav", "label": "negative",
                      "domain": name if name in ("silence", "noise") else "other",
                      "label_source": "synthetic", "expected_events": []})
    manifest = directory / "manifest.json"
    _save(manifest, {"format_version": 1, "clips": clips})
    return manifest


class ScriptedDetector:
    """Deliberate reporting example, unrelated to audio content or any model."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.hops = 0
        self.last_score = None
        self.ready = False

    def feed(self, pcm: bytes) -> bool:
        _require(len(pcm) == audio.HOP_BYTES, "Demo expects one complete hop")
        self.hops += 1
        self.ready = self.hops >= wake.CLASSIFIER_FRAMES
        self.last_score = (0.9 if self.hops == 20 else 0.1) if self.ready else None
        return self.hops == 20


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("fixtures", "evaluate", "demo"):
        command = commands.add_parser(name)
        command.add_argument("--workspace", type=Path, default=REPO)
        command.add_argument("--output", type=Path, required=True)
        if name == "evaluate":
            command.add_argument("--manifest", type=Path, required=True)
            command.add_argument("--bundle", type=Path, required=True)
            command.add_argument("--late-tolerance-seconds", type=float, default=1.0)
    args = parser.parse_args(argv)
    try:
        if args.command == "fixtures":
            generate_fixtures(args.output, workspace=args.workspace)
            summary = {"success": True, "synthetic_clips": 3, "contains_speech": False, "model_evaluated": False}
        elif args.command == "evaluate":
            summary = evaluate_manifest(args.manifest, workspace=args.workspace, output=args.output,
                                        bundle_path=args.bundle, late_tolerance_seconds=args.late_tolerance_seconds)["summary"]
        else:
            root = _private_root(args.workspace)
            directory = _new_output(args.output, root)
            manifest = generate_fixtures(directory / "fixtures", workspace=args.workspace)
            summary = evaluate_manifest(manifest, workspace=args.workspace, output=directory / "evaluation",
                                        detector_factory=ScriptedDetector, mode="scripted_demo")["summary"]
        print(json.dumps(summary, allow_nan=False))
        return 0
    except (ValueError, OSError, RuntimeError, ImportError) as error:
        print(json.dumps({"success": False, "error_type": type(error).__name__, "error": str(error) if isinstance(error, ValueError) else "Offline evaluation failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
