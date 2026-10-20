"""Bounded offline video-to-stills experiment; never a SIP endpoint.

Run ``python3 -m tools.firmware_lab.video probe`` for local dependencies, or
``python3 -m tools.firmware_lab.video extract clip.mkv --name example``.
Only local regular files are read. FFmpeg receives bytes through a pipe with
no file/network input protocols. Fresh private results go below .local/.
Matroska/WebM, AVI and streamable MP4/MOV are accepted; MP4 requiring backward
seeking may need an independently prepared fast-start copy. Originals are
never changed. No camera, microphone, call, RTP or gateway is opened.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import zlib


ROOT = Path(__file__).resolve().parents[2]
WIDTH, HEIGHT = 298, 156
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_FRAMES = 30
MAX_SECONDS = 120
TIMEOUT_SECONDS = 30
FORMATS = "matroska,webm,mov,avi"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class VideoError(ValueError):
    """A bounded offline conversion could not be completed."""


def _bounds(frame_count: int, interval_seconds: float) -> None:
    if type(frame_count) is not int or not 1 <= frame_count <= MAX_FRAMES:
        raise VideoError("frame count must be an integer from 1 to 30")
    if (isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, (int, float))
            or not math.isfinite(interval_seconds)
            or not 1 <= interval_seconds <= 5
            or frame_count * interval_seconds > MAX_SECONDS):
        raise VideoError("interval must be 1 to 5 seconds and total at most 120 seconds")


def command_plan(frame_count: int = 3, interval_seconds: float = 2) -> list[str]:
    """Return an argument list with pipe-only input and bounded PNG output."""
    _bounds(frame_count, interval_seconds)
    interval = format(interval_seconds, ".8g")
    seconds = format(frame_count * interval_seconds, ".8g")
    filters = (
        f"setpts=PTS-STARTPTS,fps=1/{interval}:start_time=0,"
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=0x101820,setsar=1"
    )
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-max_alloc", str(MAX_INPUT_BYTES), "-threads", "1",
        "-filter_threads", "1", "-protocol_whitelist", "pipe",
        "-format_whitelist", FORMATS, "-probesize", "1048576",
        "-analyzeduration", "2000000", "-t", seconds, "-i", "pipe:0",
        "-map", "0:v:0", "-an", "-sn", "-dn", "-map_metadata", "-1",
        "-vf", filters, "-frames:v", str(frame_count), "-t", seconds,
        "-threads", "1", "-c:v", "png", "-pix_fmt", "rgb24",
        "-fs", str(MAX_OUTPUT_BYTES), "-f", "image2pipe", "pipe:1",
    ]


def _run(args: list[str], data: bytes | None = None, timeout: int = TIMEOUT_SECONDS) -> bytes:
    """Do not include decoder stderr: it can contain private input metadata."""
    try:
        result = subprocess.run(args, input=data, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=timeout,
                                check=False)
    except FileNotFoundError:
        raise VideoError(f"required tool unavailable: {args[0]}") from None
    except subprocess.TimeoutExpired:
        raise VideoError(f"{args[0]} exceeded the offline time limit") from None
    if result.returncode:
        raise VideoError(f"{args[0]} rejected the local media (exit {result.returncode})")
    if len(result.stdout) > MAX_OUTPUT_BYTES:
        raise VideoError("decoder output exceeded the byte limit")
    return result.stdout


def probe_dependencies() -> dict:
    """Read installed tool versions/capabilities without opening media devices."""
    binaries = {}
    for name in ("ffmpeg", "ffprobe", "pjsua", "baresip", "gst-launch-1.0"):
        binaries[name] = {"available": shutil.which(name) is not None}
        if name in ("ffmpeg", "ffprobe") and binaries[name]["available"]:
            try:
                version = _run([name, "-version"], timeout=5).decode("utf-8", "replace")
                binaries[name]["version"] = version.splitlines()[0]
            except VideoError as exc:
                binaries[name]["error"] = str(exc)
    if binaries["ffmpeg"]["available"]:
        try:
            decoders = _run(["ffmpeg", "-hide_banner", "-decoders"], timeout=5).decode()
            encoders = _run(["ffmpeg", "-hide_banner", "-encoders"], timeout=5).decode()
            binaries["ffmpeg"]["h264_decoder"] = bool(re.search(r"^\s*V\S*\s+h264\s", decoders, re.M))
            binaries["ffmpeg"]["png_encoder"] = bool(re.search(r"^\s*V\S*\s+png\s", encoders, re.M))
        except VideoError as exc:
            binaries["ffmpeg"]["capability_error"] = str(exc)
    return {"schema_version": 1, "scope": "local dependency discovery only",
            "tools": binaries,
            "python": {name: importlib.util.find_spec(name) is not None
                       for name in ("PIL", "pjsua2", "gi")},
            "sip_video_acceptance": "not tested; no SIP endpoint implemented"}


def _read_source(source: str | Path) -> bytes:
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_INPUT_BYTES:
                raise VideoError("input must be a nonempty regular file of at most 64 MiB")
            data = stream.read(MAX_INPUT_BYTES + 1)
    except (OSError, TypeError):
        raise VideoError("input must be a readable local regular file, not a URL or symlink") from None
    if len(data) > MAX_INPUT_BYTES:
        raise VideoError("input grew beyond the byte limit")
    return data


def _probe_media(data: bytes) -> dict:
    raw = _run([
        "ffprobe", "-v", "error", "-max_alloc", str(MAX_INPUT_BYTES),
        "-threads", "1", "-protocol_whitelist", "pipe",
        "-format_whitelist", FORMATS, "-probesize", "1048576",
        "-analyzeduration", "2000000", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height,sample_aspect_ratio:format=format_name,duration",
        "-of", "json", "-i", "pipe:0",
    ], data, timeout=10)
    try:
        metadata = json.loads(raw)
        stream = metadata["streams"][0]
        width, height = stream["width"], stream["height"]
        if (type(width) is not int or type(height) is not int
                or not 0 < width <= 4096 or not 0 < height <= 4096
                or width * height > 8_000_000):
            raise VideoError("source dimensions exceed the offline limit")
        # Non-square input pixels need a different display-aspect conversion.
        if stream.get("sample_aspect_ratio", "1:1") not in ("1:1", "N/A", "0:1"):
            raise VideoError("this prototype requires square-pixel input")
        return {"codec": stream.get("codec_name", "unknown"), "width": width,
                "height": height, "container": metadata.get("format", {}).get("format_name"),
                "duration_seconds": metadata.get("format", {}).get("duration")}
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        if isinstance(exc, VideoError):
            raise
        raise VideoError("no valid first video stream found") from None


def split_pngs(data: bytes, frame_count: int) -> list[bytes]:
    """Validate dimensions and PNG chunk CRCs before saving decoder output."""
    if len(data) > MAX_OUTPUT_BYTES:
        raise VideoError("PNG output exceeded the byte limit")
    frames, offset = [], 0
    while offset < len(data):
        start = offset
        if len(frames) >= frame_count or data[offset:offset + 8] != PNG_SIGNATURE:
            raise VideoError("unexpected decoder output")
        offset += 8
        first = True
        while True:
            if offset + 12 > len(data):
                raise VideoError("truncated PNG chunk")
            length = struct.unpack_from(">I", data, offset)[0]
            kind = data[offset + 4:offset + 8]
            end = offset + 12 + length
            if length > 1024 * 1024 or end > len(data):
                raise VideoError("invalid PNG chunk size")
            payload = data[offset + 8:offset + 8 + length]
            crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
            if zlib.crc32(kind + payload) & 0xFFFFFFFF != crc:
                raise VideoError("invalid PNG checksum")
            if first:
                if kind != b"IHDR" or length != 13:
                    raise VideoError("missing PNG header")
                if struct.unpack(">IIBBBBB", payload) != (WIDTH, HEIGHT, 8, 2, 0, 0, 0):
                    raise VideoError("unexpected PNG dimensions or pixel format")
                first = False
            offset = end
            if kind == b"IEND":
                if length:
                    raise VideoError("invalid PNG end")
                break
        frames.append(data[start:offset])
    if not frames:
        raise VideoError("decoder produced no still images")
    return frames


def _fresh_directory(repository: Path, name: str) -> Path:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
        raise VideoError("output name must be 1 to 64 simple filename characters")
    path = repository.resolve(strict=True)
    for part in (".local", "firmware-lab", "video"):
        path = path / part
        if path.is_symlink():
            raise VideoError("output ancestors must not be symlinks")
        path.mkdir(mode=0o700, exist_ok=True)
    path /= name
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        raise VideoError("output name already exists; choose a fresh name") from None
    return path


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def extract_stills(source: str | Path, *, name: str, repository: Path = ROOT,
                   frame_count: int = 3, interval_seconds: float = 2) -> dict:
    """Convert a local clip, preserving it and refusing an existing output run."""
    command = command_plan(frame_count, interval_seconds)
    data = _read_source(source)
    metadata = _probe_media(data)
    frames = split_pngs(_run(command, data), frame_count)
    directory = _fresh_directory(Path(repository), name)
    report = {
        "schema_version": 1, "scope": "offline video decode and PNG conversion only",
        "source": {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), **metadata},
        "requested_frames": frame_count, "interval_seconds": interval_seconds,
        "timestamp_semantics": "nominal output sampling schedule, not source capture time",
        "dimensions": [WIDTH, HEIGHT], "frames": [], "command": command,
        "acceptance": {"offline_conversion": "passed", "sip_negotiation": "not tested",
                       "rtp_reception": "not tested", "phone_display": "not tested",
                       "native_phone_video": "not implemented"},
    }
    for index, frame in enumerate(frames):
        filename = f"frame-{index:03d}.png"
        _write_private(directory / filename, frame)
        report["frames"].append({"file": filename, "bytes": len(frame),
                                 "sha256": hashlib.sha256(frame).hexdigest(),
                                 "nominal_seconds": index * interval_seconds})
    _write_private(directory / "manifest.json", (json.dumps(report, indent=2) + "\n").encode())
    return {**report, "output_directory": str(directory)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("probe", help="inspect local dependencies without opening devices")
    extract = commands.add_parser("extract", help="decode only a local regular video file")
    extract.add_argument("source", type=Path)
    extract.add_argument("--name", required=True, help="fresh run name under .local/firmware-lab/video")
    extract.add_argument("--frames", type=int, default=3)
    extract.add_argument("--interval", type=float, default=2)
    args = parser.parse_args(argv)
    try:
        result = probe_dependencies() if args.command == "probe" else extract_stills(
            args.source, name=args.name, frame_count=args.frames, interval_seconds=args.interval)
    except (VideoError, OSError) as exc:
        parser.exit(2, f"Offline video experiment failed: {exc}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
