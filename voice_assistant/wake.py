"""EchoMuse's frozen openWakeWord ONNX frontend with per-stream state.

NumPy and ONNX Runtime are optional. Importing the package does not load a
model, download assets, access an audio device, or enable listening. Production
callers use ``from_bundle`` once per phone; injected sessions support offline
tests. Calls to a detector must be serialized by its owning phone session.

The frontend matches ``ha-echomuse/training/oww_features.py``:
1280-sample hops, 480 samples of left context, 76 initial mel frames of ones,
raw mel / 10 + 2, [1,76,32,1] embedding input, and [1,16,96] classifier input.
No random warm-up, normalization, extra sigmoid, or score averaging is applied.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
from pathlib import Path
import re
from typing import Any, Callable, Protocol

from .audio import HOP_BYTES, validate_pcm16le

FRONTEND_ID = "echomuse-oww-16k-v1"
MEL_CONTEXT_SAMPLES = 480
MEL_FRAMES = 76
MEL_BINS = 32
EMBEDDING_SIZE = 96
CLASSIFIER_FRAMES = 16
REFRACTORY_HOPS = 19  # 1.5 seconds rounded up at the 80 ms scoring cadence.


class WakeDetector(Protocol):
    """One ordered stream per instance; reset on every gap or lifecycle stop."""

    def feed(self, pcm: bytes) -> bool: ...

    def reset(self) -> None: ...


class WakeModelError(RuntimeError):
    """A model/session failed its declared frozen frontend contract."""


def _numpy() -> Any:
    try:
        return importlib.import_module("numpy")
    except ImportError:
        raise RuntimeError("The optional wake backend requires NumPy") from None


def _onnx_session(model: bytes) -> Any:
    try:
        ort = importlib.import_module("onnxruntime")
    except ImportError:
        raise RuntimeError("The optional wake backend requires ONNX Runtime") from None
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    # Loading the verified bytes prevents a later path replacement from changing
    # the model and rejects models depending on unverified external-data files.
    return ort.InferenceSession(model, options, providers=["CPUExecutionProvider"])


def _threshold(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("A numeric frozen threshold is required")
    if not math.isfinite(value) or not 0 < value <= 1:
        raise ValueError("The frozen threshold must be in (0, 1]")
    return float(value)


def _private_path(path: Path, root: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        if not resolved.is_file():
            raise ValueError
    except (OSError, ValueError, RuntimeError):
        raise ValueError("Bundle files must exist inside the private .local directory") from None
    return resolved


def _load_bundle(bundle_path: str | Path, private_root: str | Path) -> tuple[dict[str, bytes], float]:
    # The caller supplies the repository's ignored .local directory explicitly.
    # Resolve it first so symlinks in asset names cannot escape that boundary.
    root = Path(private_root)
    if root.name != ".local":
        raise ValueError("private_root must name the repository's .local directory")
    try:
        root = root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("The private .local directory must exist") from None
    manifest_path = _private_path(Path(bundle_path), root)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise ValueError("Invalid private wake bundle manifest") from None
    if (not isinstance(manifest, dict) or type(manifest.get("format_version")) is not int
            or manifest["format_version"] != 1):
        raise ValueError("Unsupported wake bundle format")
    if manifest.get("frontend") != FRONTEND_ID:
        raise ValueError("The bundle must declare the exact frozen EchoMuse frontend")
    threshold = _threshold(manifest.get("threshold"))
    models = manifest.get("models")
    if not isinstance(models, dict) or set(models) != {"mel", "embedding", "classifier"}:
        raise ValueError("The bundle requires mel, embedding, and classifier models")
    verified = {}
    for role in ("mel", "embedding", "classifier"):
        entry = models[role]
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise ValueError("Each bundle model requires path and sha256")
        name, expected = entry["path"], entry["sha256"]
        if (not isinstance(name, str) or not name or Path(name).is_absolute()
                or ".." in Path(name).parts or Path(name).suffix.lower() != ".onnx"):
            raise ValueError("Bundle models must use relative ONNX paths")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("Bundle models require lowercase SHA256 digests")
        path = _private_path(manifest_path.parent / name, root)
        try:
            model = path.read_bytes()
        except OSError:
            raise ValueError("Could not read a private bundle model") from None
        if hashlib.sha256(model).hexdigest() != expected:
            raise ValueError("Private wake bundle model checksum mismatch")
        verified[role] = model
    return verified, threshold


class FrozenOnnxWakeDetector:
    """A frozen model's streaming detector, with no shared mutable audio state.

    ``feed`` processes every complete hop, including several in a single call,
    and returns whether any hop triggered. ``last_score`` is diagnostic only and
    is None until sixteen real embeddings exist. Reset clears all retained audio,
    features, score, and refractory state. A model failure resets before raising.

    The direct constructor is for explicitly injected sessions; production callers
    should use ``from_bundle`` to verify private paths and all model digests.
    """

    def __init__(self, mel: Any, embedding: Any, classifier: Any, *, threshold: float):
        self.threshold = _threshold(threshold)
        self._np = _numpy()
        self._sessions = {"mel": mel, "embedding": embedding, "classifier": classifier}
        self._inputs = {}
        for role, session in self._sessions.items():
            inputs = session.get_inputs()
            if len(inputs) != 1 or not isinstance(inputs[0].name, str):
                raise WakeModelError("Each frozen model must have one named input")
            if getattr(inputs[0], "type", "tensor(float)") != "tensor(float)":
                raise WakeModelError("Frozen models must consume float32 tensors")
            self._inputs[role] = inputs[0].name
        self.reset()

    @classmethod
    def from_bundle(
        cls, bundle_path: str | Path, *, private_root: str | Path,
        session_factory: Callable[[bytes], Any] | None = None,
    ) -> FrozenOnnxWakeDetector:
        """Verify all assets before creating three fresh CPU model sessions.

        The JSON manifest declares format_version=1, frontend=FRONTEND_ID, a frozen
        threshold, and models.{mel,embedding,classifier}.{path,sha256}. Paths are
        relative to the private manifest. The factory receives verified model bytes;
        it must return a new stateless inference session for every invocation.
        """
        models, threshold = _load_bundle(bundle_path, private_root)
        factory = session_factory or _onnx_session
        sessions = [factory(models[role]) for role in ("mel", "embedding", "classifier")]
        return cls(*sessions, threshold=threshold)

    @property
    def ready(self) -> bool:
        return len(self._features) == CLASSIFIER_FRAMES

    def reset(self) -> None:
        self._pending = b""
        self._context = self._np.empty(0, dtype=self._np.float32)
        self._mel = self._np.ones((MEL_FRAMES, MEL_BINS), dtype=self._np.float32)
        self._features: list[Any] = []
        self._hop = 0
        self._last_trigger: int | None = None
        self.last_score: float | None = None

    def _run(self, role: str, values: Any) -> Any:
        outputs = self._sessions[role].run(None, {self._inputs[role]: values})
        if len(outputs) != 1:
            raise WakeModelError("Frozen models must return a single output")
        result = self._np.asarray(outputs[0])
        if result.dtype != self._np.float32 or not self._np.isfinite(result).all():
            raise WakeModelError("Frozen models must return finite float32 values")
        return result

    def _process_hop(self, pcm: bytes) -> bool:
        np = self._np
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        audio = np.concatenate((self._context, samples))
        expected_frames = 5 if not len(self._context) else 8
        raw = self._run("mel", audio[None, :])
        if raw.size != expected_frames * MEL_BINS or raw.shape[-1] != MEL_BINS:
            raise WakeModelError("Mel model does not match the frozen 5/8-frame frontend")
        scaled = raw.reshape(expected_frames, MEL_BINS) / 10 + 2
        self._context = audio[-MEL_CONTEXT_SAMPLES:].copy()
        # Only the last 76 frames are consumed; retaining the reference's
        # unused 970-frame history would not change the next embedding.
        self._mel = np.concatenate((self._mel, scaled))[-MEL_FRAMES:]
        feature = self._run("embedding", self._mel[None, :, :, None])
        if feature.size != EMBEDDING_SIZE:
            raise WakeModelError("Embedding model must return exactly 96 values")
        self._features.append(feature.reshape(EMBEDDING_SIZE).copy())
        self._features = self._features[-CLASSIFIER_FRAMES:]
        self._hop += 1
        if not self.ready:
            return False
        output = self._run("classifier", np.stack(self._features)[None, :, :])
        if output.size != 1:
            raise WakeModelError("Classifier must return exactly one probability")
        self.last_score = float(output.reshape(-1)[0])
        if not 0 <= self.last_score <= 1:
            raise WakeModelError("Classifier probability must be in [0, 1]")
        if (self.last_score >= self.threshold and (self._last_trigger is None
                or self._hop - self._last_trigger >= REFRACTORY_HOPS)):
            self._last_trigger = self._hop
            return True
        return False

    def feed(self, pcm: bytes) -> bool:
        """Consume ordered PCM16 mono 16 kHz bytes; never inspect a microphone."""
        validate_pcm16le(pcm)
        data = self._pending + pcm
        whole = len(data) // HOP_BYTES * HOP_BYTES
        triggered = False
        try:
            for offset in range(0, whole, HOP_BYTES):
                # Do not short-circuit after a trigger: every hop advances state.
                hop_trigger = self._process_hop(data[offset:offset + HOP_BYTES])
                triggered = triggered or hop_trigger
            self._pending = data[whole:]
        except Exception:
            self.reset()
            raise WakeModelError("Frozen wake inference failed; stream state was reset") from None
        return triggered
