"""The readiness probe must never submit speech or execute an intent."""

import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voice_assistant.ha import AssistError, AssistTimeout, HAClient
from voice_assistant.pipeline import PipelineStatus, main


PIPELINE = {
    "id": "synthetic-pipeline", "language": "ru",
    "stt_engine": "stt.synthetic_whisper", "stt_language": "ru",
    "conversation_engine": "conversation.synthetic_local",
    "tts_engine": "tts.synthetic_speech", "tts_language": "ru",
    "tts_voice": "synthetic-baya",
}
STATES = {
    "stt.synthetic_whisper": {"s": "unknown", "a": {"friendly_name": "Unused"}},
    "tts.synthetic_speech": {"s": "unknown"},
}


class Socket:
    def __init__(self, *, pipeline=None, states=None):
        self.pipeline = deepcopy(PIPELINE if pipeline is None else pipeline)
        self.states = deepcopy(STATES if states is None else states)
        self.queue = asyncio.Queue()
        self.queue.put_nowait({"type": "auth_required"})
        self.commands = []
        self.closed = False
        self.block = None
        self.entered = asyncio.Event()
        self.error = None

    async def send(self, payload):
        if not isinstance(payload, str):
            raise AssertionError("Readiness probe must not send audio")
        command = json.loads(payload)
        self.commands.append(command)
        kind = command["type"]
        if kind == self.block:
            self.entered.set()
            return
        if kind == "auth":
            self.queue.put_nowait({"type": "auth_ok"})
        elif kind == "assist_pipeline/pipeline/get":
            result = {"id": 1, "type": "result", "success": True, "result": self.pipeline}
            if self.error:
                result.update(success=False, error=self.error)
            self.queue.put_nowait(result)
        elif kind == "subscribe_entities":
            self.queue.put_nowait({"id": 2, "type": "result", "success": True})
            self.queue.put_nowait({"id": 2, "type": "event", "event": {"a": self.states}})
        else:
            raise AssertionError(f"Unexpected command: {kind}")

    async def recv(self):
        return json.dumps(await self.queue.get())

    async def close(self):
        self.closed = True


def client(socket, **kwargs):
    async def connect(_url):
        return socket
    return HAClient("https://example.invalid", "synthetic-token",
                    pipeline_id="synthetic-pipeline", connect=connect, **kwargs)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_pipeline_and_filtered_entities_without_any_run(self):
        socket = Socket()
        result = await client(socket).inspect_pipeline()
        result.require(stt_engine="stt.synthetic_whisper",
                       conversation_engine="conversation.synthetic_local",
                       tts_engine="tts.synthetic_speech", tts_voice="synthetic-baya")
        self.assertEqual(result.issues, ())
        self.assertEqual(result.stt_state, "unknown")
        self.assertEqual(socket.commands[1], {
            "id": 1, "type": "assist_pipeline/pipeline/get",
            "pipeline_id": "synthetic-pipeline",
        })
        self.assertEqual(socket.commands[2]["entity_ids"], sorted(STATES))
        self.assertTrue(socket.closed)

    async def test_no_preferred_or_conversation_entity_fallback(self):
        for value in (None, "conversation.synthetic"):
            socket = Socket()
            instance = client(socket)
            instance.pipeline_id = value
            with self.assertRaisesRegex(AssistError, "explicit-pipeline-required"):
                await instance.inspect_pipeline()
            self.assertEqual(socket.commands, [])

    async def test_changed_provider_and_voice_are_rejected_without_echoing_values(self):
        result = await client(Socket()).inspect_pipeline()
        for field in ("stt_engine", "conversation_engine", "tts_engine", "tts_voice"):
            with self.subTest(field=field), self.assertRaises(AssistError) as raised:
                result.require(**{field: "private-expected-provider"})
            self.assertNotIn("private-expected-provider", str(raised.exception))
            self.assertIn("mismatch", raised.exception.code)

    async def test_missing_and_unavailable_speech_entity_are_not_idle_unknown(self):
        for state, issue in ((None, "stt-entity-missing"), ("unavailable", "stt-unavailable")):
            states = deepcopy(STATES)
            if state is None:
                del states["stt.synthetic_whisper"]
            else:
                states["stt.synthetic_whisper"]["s"] = state
            result = await client(Socket(states=states)).inspect_pipeline()
            self.assertIn(issue, result.issues)
            with self.assertRaisesRegex(AssistError, issue):
                result.require()

    async def test_missing_engines_do_not_subscribe_to_all_entities(self):
        pipeline = dict(PIPELINE, stt_engine=None, tts_engine=None)
        socket = Socket(pipeline=pipeline)
        result = await client(socket).inspect_pipeline()
        self.assertEqual(result.issues, ("stt-not-configured", "tts-not-configured"))
        self.assertEqual(len(socket.commands), 2)
        self.assertTrue(socket.closed)

    async def test_legacy_nonentity_engine_is_reported_as_unverified(self):
        result = await client(Socket(pipeline=dict(PIPELINE, tts_engine="google_translate"))).inspect_pipeline()
        self.assertIn("tts-availability-unverified", result.issues)

    async def test_wrong_pipeline_response_fails_closed(self):
        socket = Socket(pipeline=dict(PIPELINE, id="unexpected"))
        with self.assertRaisesRegex(AssistError, "pipeline-response-mismatch"):
            await client(socket).inspect_pipeline()
        self.assertTrue(socket.closed)

    async def test_malformed_entity_snapshot_is_rejected(self):
        for invalid in ({"secret": "detail"}, "", "state\ninjected", "a" * 129):
            socket = Socket(states={"stt.synthetic_whisper": {"s": invalid}})
            with self.assertRaisesRegex(AssistError, "invalid-entity-snapshot"):
                await client(socket).inspect_pipeline()
            self.assertTrue(socket.closed)

    async def test_server_errors_are_sanitized_and_connection_closed(self):
        socket = Socket()
        socket.error = {"code": "not_found", "message": "private URL and name"}
        with self.assertRaises(AssistError) as raised:
            await client(socket).inspect_pipeline()
        self.assertEqual(raised.exception.code, "not_found")
        self.assertNotIn("private", str(raised.exception))
        self.assertTrue(socket.closed)

    async def test_deadline_closes_temporary_subscription(self):
        socket = Socket()
        socket.block = "subscribe_entities"
        with self.assertRaises(AssistTimeout):
            await client(socket, timeout=.02).inspect_pipeline()
        self.assertTrue(socket.closed)

    async def test_cancellation_closes_temporary_subscription(self):
        socket = Socket()
        socket.block = "subscribe_entities"
        task = asyncio.create_task(client(socket).inspect_pipeline())
        await asyncio.wait_for(socket.entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(socket.closed)


class PipelineCLITests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        private = self.workspace / ".local"
        private.mkdir()
        self.config = json.loads((
            Path(__file__).resolve().parents[1] / "examples/local-voice-assistant.example.json"
        ).read_text())
        self.config["ha"].update(
            token_file=".local/token.txt", base_url="https://example.invalid",
            pipeline_id="synthetic-pipeline",
        )
        self.config["service"]["enabled"] = False
        (private / "token.txt").write_text("synthetic-secret-token")
        (private / "voice.json").write_text(json.dumps(self.config))
        self.args = ["--workspace", str(self.workspace), "--config", ".local/voice.json"]

    def invoke(self, socket, *args):
        instance = client(socket)
        with patch("voice_assistant.ha.HAClient", return_value=instance) as constructor:
            output = io.StringIO()
            with redirect_stdout(output):
                result = main([*self.args, *args])
        self.assertNotIn("synthetic-secret-token", output.getvalue())
        return result, output.getvalue(), constructor

    def test_json_readiness_works_with_listening_disabled_and_exact_pins(self):
        socket = Socket()
        code, output, constructor = self.invoke(
            socket, "--json", "--expect-stt", "stt.synthetic_whisper",
            "--expect-tts", "tts.synthetic_speech", "--expect-voice", "synthetic-baya",
        )
        self.assertEqual(code, 0)
        result = json.loads(output)
        self.assertTrue(result["success"])
        self.assertEqual(result["pipeline"]["pipeline_id"], "synthetic-pipeline")
        self.assertIsNone(result["error_code"])
        self.assertEqual(constructor.call_args.kwargs["timeout"], 15)
        self.assertTrue(socket.closed)

    def test_text_output_explains_observation_and_failed_expectation(self):
        code, output, _ = self.invoke(Socket(), "--expect-voice", "different-voice")
        self.assertEqual(code, 1)
        self.assertIn("Pipeline inspection: not ready", output)
        self.assertIn("no audio or intent", output)
        self.assertIn("pipeline-tts-voice-mismatch", output)
        self.assertNotIn("different-voice", output)

    def test_unavailable_provider_returns_nonzero_and_structured_issue(self):
        code, output, _ = self.invoke(
            Socket(states=dict(STATES, **{"stt.synthetic_whisper": {"s": "unavailable"}})),
            "--json",
        )
        self.assertEqual(code, 1)
        self.assertIn("stt-unavailable", json.loads(output)["issues"])

    def test_private_path_violation_never_constructs_network_client(self):
        self.args = ["--workspace", str(self.workspace), "--config", "outside.json"]
        code, output, constructor = self.invoke(Socket(), "--json")
        self.assertEqual(code, 1)
        constructor.assert_not_called()
        self.assertEqual(json.loads(output)["error_code"], "pipeline-inspection-configuration-failed")
        self.assertNotIn(str(self.workspace), output)

    def test_bad_server_response_emits_only_safe_error_code(self):
        socket = Socket()
        socket.error = {"code": "private detail\n", "message": "private transcript"}
        code, output, _ = self.invoke(socket, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output), {"success": False, "error_code": "server-error"})
        self.assertNotIn("private", output)


if __name__ == "__main__":
    unittest.main()
