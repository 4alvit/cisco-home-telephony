"""Headless local demo, configuration validation and explicit WAV replay."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import wave

from .config import NeverWake, build_service, load_config, private_path
from .playback import decode_wave
from .service import AssistantService, ServiceSettings


class MemorySink:
    """No device output. The optional reply WAV is written only by the CLI."""

    def __init__(self):
        self.audio = bytearray()
        self.rate: int | None = None

    async def write_pcm(self, phone_id: str, data: bytes, sample_rate: int) -> None:
        if self.rate is not None and self.rate != sample_rate:
            raise ValueError("sample rate changed during one reply")
        self.rate = sample_rate
        self.audio.extend(data)

    async def stop(self, phone_id: str) -> None:
        pass


async def demo() -> int:
    from .ha import HAClient
    from .simulation import SimulatedHASocket

    socket = SimulatedHASocket()
    async def connect(_url: str):
        return socket

    class DemoPlayback:
        async def play(self, _phone: str, result: object) -> None:
            assert result.text == "Synthetic response."
        async def stop(self, _phone: str) -> None:
            pass

    events = []
    service = AssistantService(ServiceSettings(enabled=True, preroll_ms=0),
                               {"synthetic-phone": True}, lambda _: NeverWake(),
                               HAClient("http://example.invalid", "synthetic-token", connect=connect),
                               DemoPlayback(), events.append)
    stream = await service.activate("synthetic-phone")
    service.feed_pcm("synthetic-phone", stream, bytes(640))
    service.end_utterance("synthetic-phone", stream)
    await service.wait_idle("synthetic-phone")
    status = service.status("synthetic-phone")
    print(json.dumps({"mode": "in_memory", "network_connections": 0,
                      "states": [event.state for event in events],
                      "audio_bytes": socket.audio_bytes,
                      "success": status.state == "stopped" and socket.audio_bytes == 640}))
    await service.close()
    return int(status.state != "stopped" or socket.audio_bytes != 640)


async def replay(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    config = load_config(private_path(args.config, workspace))
    # Replay is deliberately manual even if normal operation uses ONNX.
    config["wake"]["mode"] = "manual"
    source = decode_wave(private_path(args.input, workspace).read_bytes(),
                         max_seconds=config["service"].get("max_utterance_seconds", 12))
    if source.sample_rate != 16000:
        raise ValueError("replay WAV must be 16 kHz PCM; resample it offline first")
    sink = MemorySink()
    connect = None
    if args.transport == "aiohttp":
        from .ha import aiohttp_connect
        connect = aiohttp_connect
    service = build_service(config, workspace, sink,
                            on_event=lambda item: print(json.dumps(asdict(item)), flush=True),
                            connect=connect)
    try:
        stream = await service.activate(args.phone)
        for offset in range(0, len(source.data), 640):
            frame = source.data[offset:offset + 640].ljust(640, b"\0")
            if not service.feed_pcm(args.phone, stream, frame):
                break
            await asyncio.sleep(.02)
        service.end_utterance(args.phone, stream)
        await service.wait_idle(args.phone)
        status = service.status(args.phone)
        if args.output and sink.audio:
            target = private_path(args.output, workspace)
            target.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation prevents silently replacing an existing recording.
            with target.open("xb") as output:
                target.chmod(0o600)
                with wave.open(output, "wb") as result:
                    result.setnchannels(1)
                    result.setsampwidth(2)
                    result.setframerate(sink.rate)
                    result.writeframes(sink.audio)
        print(json.dumps({"success": status.state == "stopped", "reply_bytes": len(sink.audio),
                          "sample_rate": sink.rate}))
        return int(status.state != "stopped")
    finally:
        await service.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="run the real client against an in-memory HA protocol peer")
    validate = commands.add_parser("validate", help="validate JSON without opening a connection")
    validate.add_argument("--config", required=True)
    run = commands.add_parser("replay", help="send a private WAV to explicitly configured HA; no phone audio")
    run.add_argument("--workspace", default=".")
    run.add_argument("--config", required=True)
    run.add_argument("--input", required=True)
    run.add_argument("--phone", required=True, help="opaque key from config.phones")
    run.add_argument("--output", help="optional new .local reply WAV, never played")
    run.add_argument("--transport", choices=("websockets", "aiohttp"), default="websockets")
    args = parser.parse_args()
    if args.command == "demo":
        return asyncio.run(demo())
    if args.command == "validate":
        load_config(args.config)
        print("Configuration valid; no connection opened.")
        return 0
    try:
        return asyncio.run(replay(args))
    except Exception:
        # Config/provider errors may contain private text, paths and credentials.
        print("Replay failed; check private configuration and stage status.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
