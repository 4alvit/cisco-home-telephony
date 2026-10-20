"""Read-only Assist pipeline inspection; no audio or intent is submitted.

An idle speech entity often has state ``unknown``. That is different from
``unavailable`` or a missing entity. This check verifies configuration and
reported availability, not recognition quality, latency, or offline operation.
Engine names alone do not prove that their upstream services are local.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from .ha import AssistError


_ENTITY_ID = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")


@dataclass(frozen=True)
class PipelineStatus:
    pipeline_id: str
    language: str | None
    stt_engine: str | None
    stt_language: str | None
    conversation_engine: str | None
    tts_engine: str | None
    tts_language: str | None
    tts_voice: str | None
    stt_state: str | None
    tts_state: str | None

    @classmethod
    def from_responses(
        cls, pipeline_id: str, pipeline: dict[str, Any], states: dict[str, Any]
    ) -> PipelineStatus:
        if not isinstance(pipeline, dict) or pipeline.get("id") != pipeline_id:
            raise AssistError("pipeline-response-mismatch")
        if not isinstance(states, dict):
            raise AssistError("invalid-entity-snapshot")
        fields = {}
        for field in (
            "language", "stt_engine", "stt_language", "conversation_engine",
            "tts_engine", "tts_language", "tts_voice",
        ):
            value = pipeline.get(field)
            if value is not None and (
                not isinstance(value, str) or not value or len(value) > 512
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                raise AssistError("invalid-pipeline-field")
            fields[field] = value
        for stage in ("stt", "tts"):
            entry = states.get(fields[f"{stage}_engine"])
            if entry is not None and (
                not isinstance(entry, dict) or not isinstance(entry.get("s"), str)
                or not entry["s"] or len(entry["s"]) > 128
                or any(ord(character) < 32 or ord(character) == 127 for character in entry["s"])
            ):
                raise AssistError("invalid-entity-snapshot")
            fields[f"{stage}_state"] = entry["s"] if entry is not None else None
        return cls(pipeline_id=pipeline_id, **fields)

    @property
    def issues(self) -> tuple[str, ...]:
        issues = []
        for stage in ("stt", "conversation", "tts"):
            if not getattr(self, f"{stage}_engine"):
                issues.append(f"{stage}-not-configured")
        for stage in ("stt", "tts"):
            engine = getattr(self, f"{stage}_engine")
            state = getattr(self, f"{stage}_state")
            if engine:
                if not _ENTITY_ID.fullmatch(engine):
                    issues.append(f"{stage}-availability-unverified")
                elif state is None:
                    issues.append(f"{stage}-entity-missing")
                elif state == "unavailable":
                    issues.append(f"{stage}-unavailable")
        return tuple(issues)

    def require(
        self, *, stt_engine: str | None = None,
        conversation_engine: str | None = None, tts_engine: str | None = None,
        tts_voice: str | None = None,
    ) -> None:
        """Fail closed on missing speech providers or changed provider/voice pins.

        Expected values are optional exact matches, never substring tests.
        Nothing selects a different pipeline or changes HA's preferred pipeline.
        A voice alias such as ``silero-ru-default`` still needs backend validation
        to establish which speaker it represents.
        """
        for field, expected in (
            ("stt_engine", stt_engine), ("conversation_engine", conversation_engine),
            ("tts_engine", tts_engine), ("tts_voice", tts_voice),
        ):
            if expected is not None:
                if not isinstance(expected, str) or not expected:
                    raise ValueError("pipeline expectations must be nonempty strings")
                if getattr(self, field) != expected:
                    raise AssistError("pipeline-" + field.replace("_", "-") + "-mismatch")
        if self.issues:
            raise AssistError("pipeline-" + self.issues[0])


async def inspect_exchange(client: Any, websocket: Any) -> PipelineStatus:
    """Use one already authenticated connection, closed by the owning client."""
    await client._send_json(websocket, {
        "id": 1, "type": "assist_pipeline/pipeline/get",
        "pipeline_id": client.pipeline_id,
    })
    response = await client._receive(websocket)
    if response.get("id") != 1 or response.get("type") != "result":
        raise AssistError("invalid-pipeline-response")
    if response.get("success") is not True:
        client._raise_server_error(response.get("error"))
    pipeline = response.get("result")
    # Validate the response before choosing entities. Do not query all HA states.
    partial = PipelineStatus.from_responses(client.pipeline_id, pipeline, {})
    entities = sorted({
        engine for engine in (partial.stt_engine, partial.tts_engine)
        if engine is not None and _ENTITY_ID.fullmatch(engine)
    })
    if not entities:
        return partial
    await client._send_json(websocket, {
        "id": 2, "type": "subscribe_entities", "entity_ids": entities,
    })
    response = await client._receive(websocket)
    if response.get("id") != 2 or response.get("type") != "result":
        raise AssistError("invalid-entity-subscription")
    if response.get("success") is not True:
        client._raise_server_error(response.get("error"))
    response = await client._receive(websocket)
    if response.get("id") != 2 or response.get("type") != "event":
        raise AssistError("invalid-entity-snapshot")
    event = response.get("event")
    if not isinstance(event, dict) or not isinstance(event.get("a"), dict):
        raise AssistError("invalid-entity-snapshot")
    return PipelineStatus.from_responses(client.pipeline_id, pipeline, event["a"])


async def inspect_config(args: Any) -> dict[str, Any]:
    """Inspect private configuration without constructing a listening service."""
    from dataclasses import asdict
    from pathlib import Path

    from .config import load_config, private_path
    from .ha import HAClient, aiohttp_connect

    workspace = Path(args.workspace).resolve()
    config = load_config(private_path(args.config, workspace))
    token = private_path(config["ha"]["token_file"], workspace).read_text().strip()
    client = HAClient(
        config["ha"]["base_url"], token, pipeline_id=config["ha"]["pipeline_id"],
        timeout=15, connection_timeout=10,
        connect=aiohttp_connect if args.transport == "aiohttp" else None,
    )
    status = await client.inspect_pipeline()
    error = None
    try:
        status.require(
            stt_engine=args.expect_stt, conversation_engine=args.expect_conversation,
            tts_engine=args.expect_tts, tts_voice=args.expect_voice,
        )
    except AssistError as exc:
        error = exc.code
    return {
        "success": error is None,
        "scope": "configuration and reported speech availability; no audio or intent",
        "pipeline": asdict(status),
        "issues": list(status.issues),
        "error_code": error,
    }


def main(argv: list[str] | None = None) -> int:
    """Standalone, metadata-only preflight for operators and deployment tools."""
    import argparse
    import asyncio
    import json

    parser = argparse.ArgumentParser(
        description="Inspect one explicit HA Assist pipeline without opening a microphone.",
        epilog="Output includes private pipeline/entity identifiers. Keep it out of public logs.",
    )
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--config", required=True, help="configuration inside workspace .local")
    parser.add_argument("--transport", choices=("websockets", "aiohttp"), default="websockets")
    parser.add_argument("--expect-stt", help="require this exact STT entity ID")
    parser.add_argument("--expect-conversation", help="require this exact conversation engine")
    parser.add_argument("--expect-tts", help="require this exact TTS entity ID")
    parser.add_argument("--expect-voice", help="require this exact configured TTS voice ID")
    parser.add_argument("--json", action="store_true", help="emit one JSON result")
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(inspect_config(args))
    except AssistError as exc:
        result = {"success": False, "error_code": exc.code}
    except KeyboardInterrupt:
        return 130
    except Exception:
        # File/JSON/dependency exceptions may contain a credential or private URL.
        result = {"success": False, "error_code": "pipeline-inspection-configuration-failed"}
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print("Pipeline inspection: " + ("ready" if result["success"] else "not ready"))
        if "scope" in result:
            print("Scope: " + result["scope"])
            for key, value in result["pipeline"].items():
                print(key.replace("_", " ").capitalize() + ": " + (value or "(not set)"))
        if result["error_code"]:
            print("Error: " + result["error_code"])
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
