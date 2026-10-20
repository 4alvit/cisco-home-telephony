"""Deterministic in-memory HA protocol peer for the headless demo and tests."""

import asyncio
import json


class SimulatedHASocket:
    """Exercises actual JSON/binary protocol code without a network connection."""

    def __init__(self):
        self.messages: asyncio.Queue[str] = asyncio.Queue()
        self.messages.put_nowait(json.dumps({"type": "auth_required"}))
        self.closed = False
        self.audio_bytes = 0
        self.command = None

    def _put(self, value):
        self.messages.put_nowait(json.dumps(value))

    def _event(self, kind, data=None):
        self._put({"id": 1, "type": "event", "event": {"type": kind, "data": data or {}}})

    async def send(self, message):
        if isinstance(message, bytes):
            if message[:1] != b"\x07":
                raise ValueError("incorrect binary handler prefix")
            self.audio_bytes += len(message) - 1
            if len(message) == 1:
                self._event("stt-end", {"stt_output": {"text": "Synthetic request."}})
                self._event("intent-start")
                self._event("intent-end", {"intent_output": {
                    "conversation_id": "synthetic-conversation", "response": {
                        "speech": {"plain": {"speech": "Synthetic response."}}}}})
                self._event("tts-start", {"tts_input": "Synthetic response."})
                self._event("tts-end", {"tts_output": {"url": "/api/tts_proxy/synthetic.wav",
                                                       "mime_type": "audio/wav"}})
                self._event("run-end")
            return
        value = json.loads(message)
        if value["type"] == "auth":
            self._put({"type": "auth_ok"})
        elif value["type"] == "assist_pipeline/run":
            self.command = value
            self._put({"id": 1, "type": "result", "success": True, "result": None})
            self._event("run-start", {"runner_data": {"stt_binary_handler_id": 7}})
            self._event("stt-start")
        elif value["type"] == "ping":
            self._put({"id": value["id"], "type": "pong"})
        else:
            raise ValueError("unsupported synthetic command")

    async def recv(self):
        return await self.messages.get()

    async def close(self):
        self.closed = True
