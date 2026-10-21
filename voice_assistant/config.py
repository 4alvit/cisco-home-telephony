"""Private runtime configuration; validation does not connect to Home Assistant."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .service import AssistantService, ServiceSettings


class NeverWake:
    """Manual activation mode has no model or inference dependency."""

    def feed(self, pcm: bytes) -> bool:
        return False

    def reset(self) -> None:
        pass


def private_path(value: str, root: Path) -> Path:
    private_root = root / ".local"
    if private_root.is_symlink():
        raise ValueError("workspace .local must not be a symlink")
    base = private_root.resolve()
    candidate = (root / value).resolve()
    if candidate == base or not candidate.is_relative_to(base):
        raise ValueError("private assets must resolve inside workspace .local")
    return candidate


def load_config(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text())
    return validate_config(data)


def validate_config(data: dict[str, Any]) -> dict[str, Any]:
    required = {"service", "phones", "ha", "wake"}
    if (not isinstance(data, dict) or not required <= set(data) or
            not set(data) <= required | {"display_reply_text"}):
        raise ValueError("config requires service, phones, ha and wake objects")
    if type(data.get("display_reply_text", False)) is not bool:
        raise ValueError("display_reply_text must be a boolean")
    ServiceSettings(**data["service"])
    if not isinstance(data["phones"], dict) or not data["phones"] or any(
            not isinstance(key, str) or not key or type(value) is not bool
            for key, value in data["phones"].items()):
        raise ValueError("phones must map opaque IDs to explicit booleans")
    ha = data["ha"]
    required_ha = {"base_url", "token_file", "pipeline_id"}
    if (not isinstance(ha, dict) or not required_ha <= set(ha)
            or not set(ha) <= required_ha | {"device_id"}):
        raise ValueError("ha requires base_url, token_file and explicit pipeline_id")
    if "device_id" in ha and (type(ha["device_id"]) is not str
            or not 1 <= len(ha["device_id"]) <= 128
            or any(char.isspace() for char in ha["device_id"])):
        raise ValueError("device_id must be a nonempty opaque ID without whitespace")
    parsed = urlsplit(ha["base_url"])
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or
            parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("invalid Home Assistant base URL")
    if (not isinstance(ha["pipeline_id"], str) or not ha["pipeline_id"] or
            ha["pipeline_id"].startswith("conversation.")):
        raise ValueError("an explicit pipeline ID is required")
    if not isinstance(ha["token_file"], str) or not ha["token_file"]:
        raise ValueError("token_file must be a path")
    wake = data["wake"]
    if (not isinstance(wake, dict) or set(wake) != {"mode", "bundle_file"} or
            wake["mode"] not in ("manual", "onnx") or not isinstance(wake["bundle_file"], str)):
        raise ValueError("wake requires mode manual/onnx and a bundle_file path")
    return data


def build_service(config: dict[str, Any], workspace: Path, sink: Any, *,
                  on_event: Any = None, compressed_decoder: Any = None,
                  connect: Any = None, on_reply: Any = None) -> AssistantService:
    """Construct the adapter; caller still must explicitly activate each stream."""
    from .ha import HAClient
    from .playback import WavePlayback

    validate_config(config)
    settings = ServiceSettings(**config["service"])
    if not settings.enabled:
        raise PermissionError("service.enabled must be explicitly true")
    token = private_path(config["ha"]["token_file"], workspace).read_text().strip()
    if not token:
        raise ValueError("empty Home Assistant token")
    client = HAClient(config["ha"]["base_url"], token, pipeline_id=config["ha"]["pipeline_id"],
                      device_id=config["ha"].get("device_id"),
                      timeout=settings.turn_timeout_seconds,
                      max_input_bytes=math.ceil(settings.max_utterance_seconds * 50) * 640,
                      connect=connect)
    if config["wake"]["mode"] == "onnx":
        from .wake import FrozenOnnxWakeDetector
        bundle = private_path(config["wake"]["bundle_file"], workspace)
        factory = lambda _phone: FrozenOnnxWakeDetector.from_bundle(bundle, private_root=workspace / ".local")
    else:
        factory = lambda _phone: NeverWake()
    return AssistantService(settings, config["phones"], factory, client,
                            WavePlayback(client.fetch_audio, sink, compressed_decoder=compressed_decoder),
                            on_event=on_event,
                            on_reply=on_reply if config.get("display_reply_text", False) else None)
