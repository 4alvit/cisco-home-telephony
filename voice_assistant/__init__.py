"""Opt-in, server-side voice adapter. Importing this package opens no devices."""

from .service import AssistantService, ServiceSettings, VoiceEvent

__all__ = ["AssistantService", "ServiceSettings", "VoiceEvent"]
