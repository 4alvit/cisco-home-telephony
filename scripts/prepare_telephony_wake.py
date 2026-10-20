#!/usr/bin/env python3
"""Prepare a private PCMU wake-evaluation corpus, entirely offline.

This is a reproducible digital codec fixture, not a recording of a telephone
microphone or a simulation of room acoustics. It reuses the deployed G.711
codec and 8 -> 16 kHz input converter without loading a wake model.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import wave

REPO = Path(__file__).resolve().parents[1]
for directory in (REPO / "scripts", REPO / "containers" / "asterisk"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import wake_evaluate as evaluation
import phone_voice_controller as controller
import phone_voice_transport as transport

METHOD = "pair-average-pcmu-inbound-v1"
INPUT_FRAME_BYTES = 640  # 20 ms at mono PCM16 / 16 kHz.


def codec_roundtrip(pcm: bytes) -> tuple[bytes, str, int]:
    """Return equal-duration PCM, the encoded-stream digest, and padding count.

    Pair averaging performs deterministic 16 -> 8 kHz preparation. Every block
    then follows the production G.711 encoder/decoder and per-frame linear
    upsampler. Only the last block may be padded; padding is removed from the
    returned PCM. No synthetic samples extend its annotation time line.

    Pair averaging is a two-tap low-pass filter, not a full telephone band-pass
    or anti-aliasing model. Frame-boundary interpolation and G.711 quantization
    match this repository's current inbound adapter; acoustic effects do not.
    The preparation is not the outgoing RtpPCMSink resampler, which uses
    different interpolation. Actual microphone RTP already arrives at 8 kHz.
    """
    if type(pcm) is not bytes or len(pcm) % 2:
        raise ValueError("Input must be immutable whole-sample PCM16 bytes")
    result = bytearray()
    encoded = hashlib.sha256()
    for offset in range(0, len(pcm), INPUT_FRAME_BYTES):
        chunk = pcm[offset:offset + INPUT_FRAME_BYTES]
        frame = next(controller.playback_frames(chunk, 16000))
        payload = transport.encode_ulaw(frame)
        encoded.update(payload)
        result.extend(controller.upsample_8k_frame(transport.decode_ulaw(payload)))
    padded_samples = (len(result) - len(pcm)) // 2
    return bytes(result[:len(pcm)]), encoded.hexdigest(), padded_samples


def _wav(pcm: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(pcm)
    return output.getvalue()


def prepare(manifest_path: str | Path, *, output: str | Path,
            workspace: str | Path = REPO, late_tolerance_seconds: float = 1.0) -> dict:
    """Validate the entire corpus before creating an owner-only output tree."""
    if (not evaluation._number(late_tolerance_seconds)
            or not 0 <= late_tolerance_seconds <= 10):
        raise ValueError("Late tolerance must be a finite number in 0..10")
    private = evaluation._private_root(workspace)
    clips, manifest_hash = evaluation._load_inputs(
        manifest_path, private, float(late_tolerance_seconds))
    destination = evaluation._new_output(output, private)
    audio_directory = destination / "audio"
    audio_directory.mkdir(mode=0o700)
    manifest = {"format_version": 1, "clips": []}
    details = []
    for clip in clips:
        pcm, wire_hash, padding = codec_roundtrip(clip["pcm"])
        wav = _wav(pcm)
        relative = "audio/" + clip["id"] + ".wav"
        evaluation._write(destination / relative, wav)
        manifest["clips"].append({
            key: relative if key == "path" else clip[key]
            for key in ("id", "path", "label", "domain", "label_source", "expected_events")
        })
        details.append({
            "id": clip["id"],
            "source_wav_sha256": clip["wav_sha256"],
            "source_pcm_sha256": evaluation._hash(clip["pcm"]),
            "output_wav_sha256": evaluation._hash(wav),
            "output_pcm_sha256": evaluation._hash(pcm),
            "encoded_pcmu_sha256": wire_hash,
            "samples": len(pcm) // 2,
            "temporary_tail_padding_samples": padding,
        })
    provenance = {
        "format_version": 1,
        "method": METHOD,
        "input_manifest_sha256": manifest_hash,
        "late_tolerance_seconds": float(late_tolerance_seconds),
        "algorithm": {
            "input": "mono signed PCM16 little-endian / 16000 Hz",
            "downsample": "pair average, integer floor; pad only final 20 ms block",
            "downsample_scope": "offline preparation only; not outgoing RtpPCMSink parity",
            "wire": "G.711 mu-law / 8000 Hz / 160 samples per block",
            "upsample": "production per-frame linear interpolation / 16000 Hz",
            "output": "exact original sample count, labels and annotation times",
            "packet_loss": False,
            "room_acoustics": False,
            "microphone": False,
        },
        "implementation_sha256": {
            "phone_voice_controller.py": evaluation._hash(Path(controller.__file__).read_bytes()),
            "phone_voice_transport.py": evaluation._hash(Path(transport.__file__).read_bytes()),
            "prepare_telephony_wake.py": evaluation._hash(Path(__file__).read_bytes()),
        },
        "clips": details,
    }
    summary = {
        "method": METHOD,
        "clips": len(clips),
        "audio_seconds": sum(len(clip["pcm"]) for clip in clips) / 32000,
        "annotations_preserved": True,
        "model_inference": False,
        "interpretation": "Digital codec fixture only; no room or microphone qualification.",
    }
    # Write the usable manifest last: a partial run has no complete corpus entry.
    evaluation._save(destination / "provenance.private.json", provenance)
    evaluation._save(destination / "summary.safe.json", summary)
    evaluation._save(destination / "manifest.json", manifest)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="Owner-only source manifest under .local")
    parser.add_argument("--output", required=True, help="New private output directory under .local")
    parser.add_argument("--late-tolerance-seconds", type=float, default=1.0)
    args = parser.parse_args(argv)
    try:
        summary = prepare(args.manifest, output=args.output,
                          late_tolerance_seconds=args.late_tolerance_seconds)
    except (ValueError, TypeError, OSError, wave.Error):
        print("Could not prepare corpus; check private inputs, labels, and fresh output path.",
              file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
