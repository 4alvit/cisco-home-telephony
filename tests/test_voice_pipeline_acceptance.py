"""Offline acceptance of the RTP/wake/Assist/playback integration harness.

CI uses an explicitly labelled deterministic detector, never a private model.
The standalone harness requires the operator's pinned ONNX bundle separately.
"""

import asyncio
import contextlib
from dataclasses import replace
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import unittest
from unittest import mock
import wave


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/test-voice-pipeline.py"
SPEC = importlib.util.spec_from_file_location("voice_pipeline_acceptance_fixture", SCRIPT)
fixture = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fixture
SPEC.loader.exec_module(fixture)


def nonzero_pcm8k(seconds=1):
    """A repeatable, varying signal: no microphone, file, model or codec fake."""
    samples = [
        round(9500 * math.sin(2 * math.pi * 430 * sample / 8000))
        for sample in range(round(8000 * seconds))
    ]
    return struct.pack("<" + "h" * len(samples), *samples)


class DeterministicTestDetector:
    """Only a CI trigger; its result must never be labelled ONNX acceptance."""

    def __init__(self):
        self.frames = 0

    def feed(self, pcm):
        self.frames += 1
        return self.frames == 4 and any(pcm)

    def reset(self):
        self.frames = 0


class NeverWake:
    def feed(self, pcm):
        return False

    def reset(self):
        pass


def test_detector_factory(_phone_id):
    return DeterministicTestDetector()


class PipelineScenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        async def run_all():
            return {
                scenario: await fixture.run_scenario(
                    scenario, nonzero_pcm8k(), test_detector_factory,
                    detector_kind="injected_test", duration_seconds=3,
                )
                for scenario in fixture.SCENARIOS
            }

        cls.results = asyncio.run(run_all())

    def test_all_scenarios_run_without_private_assets_or_silent_skips(self):
        self.assertEqual(
            set(self.results), {"complete", "stop", "preempt", "provider-failure", "no-wake"},
        )
        for scenario, result in self.results.items():
            with self.subTest(scenario=scenario):
                self.assertIs(result["success"], True, result)
                self.assertEqual(result["scenario"], scenario)
                self.assertEqual(result["detector_kind"], "injected_test")
                self.assertIs(result["actual_onnx"], False)
                self.assertEqual(result["external_network_attempts"], 0)

    def test_complete_turn_carries_actual_nonzero_bytes_through_both_directions(self):
        result = self.results["complete"]
        self.assertIs(result["turn_success"], True, result)
        self.assertEqual(result["reason"], "turn_complete")
        self.assertGreater(result["wake_triggers"], 0)
        self.assertGreater(result["input_frames"], 0)
        self.assertGreater(result["encoded_input_bytes"], 0)
        self.assertGreater(result["decoded_input_bytes"], 0)
        self.assertEqual(result["ha_sessions"], 1)
        self.assertGreater(result["ha_audio_bytes"], 0)
        self.assertEqual(result["ha_eos"], 1)
        self.assertIs(result["upload_matches_postwake_pcm"], True)
        self.assertGreater(result["output_packets"], 0)
        self.assertGreater(result["output_pcm_bytes"], 0)
        self.assertIs(result["output_matches_tts_pcmu"], True)
        self.assertIs(result["output_nonzero"], True)
        for key in ("encoded_input_sha256", "decoded_input_sha256",
                    "ha_audio_sha256", "output_pcm_sha256"):
            self.assertRegex(result[key], r"^[0-9a-f]{64}$")
        states = result["states"]
        position = 0
        for state in ("listening", "thinking", "speaking", "stopped"):
            position = states.index(state, position) + 1

    def test_stop_and_call_preemption_interrupt_observed_output(self):
        completed_packets = self.results["complete"]["output_packets"]
        for scenario in ("stop", "preempt"):
            with self.subTest(scenario=scenario):
                result = self.results[scenario]
                self.assertIs(result["turn_success"], False)
                self.assertIn("speaking", result["states"])
                self.assertGreater(result["output_packets"], 0)
                self.assertLess(result["output_packets"], completed_packets)
                self.assertIs(result["upload_matches_postwake_pcm"], True)
                self.assertIs(result["output_matches_tts_pcmu"], True)
                self.assertIs(result["output_nonzero"], True)
                self.assertEqual(result["output_after_stop"], 0)

    def test_provider_failure_is_observed_failure_with_no_fabricated_reply(self):
        result = self.results["provider-failure"]
        self.assertIs(result["turn_success"], False)
        self.assertEqual(result["reason"], "assistant_error")
        self.assertEqual(result["ha_sessions"], 1)
        self.assertGreater(result["ha_audio_bytes"], 0)
        self.assertEqual(result["ha_eos"], 1)
        self.assertIs(result["upload_matches_postwake_pcm"], True)
        self.assertEqual(result["output_packets"], 0)
        self.assertNotIn("speaking", result["states"])

    def test_no_wake_never_reaches_ha_or_output(self):
        result = self.results["no-wake"]
        self.assertIs(result["turn_success"], False)
        self.assertEqual(result["wake_triggers"], 0)
        self.assertEqual(result["ha_sessions"], 0)
        self.assertEqual(result["ha_audio_bytes"], 0)
        self.assertEqual(result["ha_eos"], 0)
        self.assertEqual(result["output_packets"], 0)
        self.assertNotIn("speaking", result["states"])

    def test_every_exit_closes_owned_resources_and_rejects_stale_media(self):
        for scenario, result in self.results.items():
            with self.subTest(scenario=scenario):
                self.assertIs(result["cleanup_success"], True, result)
                self.assertIs(result["ha_closed"], True)
                self.assertIs(result["media_closed"], True)
                self.assertIs(result["stop_confirmed"], True)
                self.assertIs(result["stale_stream_rejected"], True)
                self.assertEqual(result["output_after_stop"], 0)


class PipelineNegativeControls(unittest.IsolatedAsyncioTestCase):
    async def test_a_detector_that_never_fires_cannot_pass_a_positive_scenario(self):
        result = await fixture.run_scenario(
            "complete", nonzero_pcm8k(), lambda _: NeverWake(),
            detector_kind="injected_test", duration_seconds=.3,
        )
        self.assertIs(result["success"], False, result)
        self.assertIs(result["turn_success"], False)
        self.assertEqual(result["wake_triggers"], 0)
        self.assertEqual(result["ha_sessions"], 0)
        self.assertEqual(result["output_packets"], 0)
        self.assertIs(result["cleanup_success"], True)

    async def test_invalid_inputs_and_false_onnx_classification_never_start_media(self):
        valid = {
            "scenario": "complete", "pcm8k": nonzero_pcm8k(),
            "detector_factory": test_detector_factory, "duration_seconds": 3,
        }
        invalid = [
            {"scenario": "unknown"}, {"detector_kind": "unknown"},
            {"detector_kind": "onnx"}, {"pcm8k": b""}, {"pcm8k": bytes(320)},
            {"pcm8k": b"x" * 319}, {"pcm8k": bytearray(b"x" * 320)},
            {"pcm8k": b"x" * (20 * 16000 + 320)},
            *({"duration_seconds": value} for value in (True, 0, -1, 31, math.inf, math.nan)),
        ]
        for override in invalid:
            with self.subTest(override=list(override), value=next(iter(override.values()))):
                with mock.patch.object(fixture, "MemoryDatagram") as media:
                    with self.assertRaises(fixture.FixtureFailure):
                        await fixture.run_scenario(**(valid | override))
                    media.assert_not_called()


class StrictHAContract(unittest.IsolatedAsyncioTestCase):
    async def peer(self, expected):
        peer = fixture.StrictHAPeer("complete", lambda: expected)
        self.assertEqual(json.loads(await peer.recv())["type"], "auth_required")
        await peer.send(json.dumps({"type": "auth", "access_token": "fixture-token"}))
        self.assertEqual(json.loads(await peer.recv())["type"], "auth_ok")
        await peer.send(json.dumps({
            "id": 1, "type": "assist_pipeline/run", "start_stage": "stt",
            "end_stage": "tts", "pipeline": "fixture-pipeline",
            "input": {"sample_rate": 16000}, "timeout": 3,
        }))
        for _ in range(3):
            await peer.recv()
        self.assertTrue(peer.messages.empty())
        self.addAsyncCleanup(peer.close)
        return peer

    async def test_wrong_handler_partial_samples_and_premature_eos_produce_no_reply(self):
        expected = struct.pack("<320h", *range(320))
        for message in (b"\x08" + expected, b"\x07" + expected[:-2], b"\x07"):
            with self.subTest(length=len(message), handler=message[0]):
                peer = await self.peer(expected)
                with self.assertRaises(fixture.FixtureFailure):
                    await peer.send(message)
                self.assertEqual(peer.eos, 0)
                self.assertTrue(peer.messages.empty(), "Bad input must not queue a canned response")

    async def test_silence_and_duplicate_eos_cannot_pass_the_positive_peer(self):
        silent = await self.peer(bytes(640))
        await silent.send(b"\x07" + bytes(640))
        with self.assertRaises(fixture.FixtureFailure):
            await silent.send(b"\x07")
        self.assertEqual(silent.eos, 0)
        self.assertTrue(silent.messages.empty())

        expected = struct.pack("<320h", *range(320))
        valid = await self.peer(expected)
        await valid.send(b"\x07" + expected)
        await valid.send(b"\x07")
        self.assertEqual(valid.eos, 1)
        pending = valid.messages.qsize()
        with self.assertRaises(fixture.FixtureFailure):
            await valid.send(b"\x07")
        self.assertEqual(valid.eos, 1)
        self.assertEqual(valid.messages.qsize(), pending)

    async def test_ha_connection_close_rejects_late_audio(self):
        peer = await self.peer(struct.pack("<320h", *range(320)))
        await peer.close()
        with self.assertRaisesRegex(fixture.FixtureFailure, "ha_send_after_close"):
            await peer.send(b"\x07" + bytes(640))


class PrivateFixtureValidation(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # /var is a symlink on macOS; the actual temporary location is intentional.
        self.base = Path(temporary.name).resolve()
        self.root = self.base / ".local"
        self.root.mkdir(mode=0o700)
        self.bundle = self.root / "bundle.json"
        self.wav = self.root / "positive.wav"
        self.manifest = {
            "format_version": 1, "frontend": "echomuse-oww-16k-v1",
            "threshold": .5, "models": {},
        }
        for role in ("mel", "embedding", "classifier"):
            raw = ("synthetic non-executable model " + role).encode()
            self.put(self.root / (role + ".onnx"), raw)
            self.manifest["models"][role] = {
                "path": role + ".onnx", "sha256": hashlib.sha256(raw).hexdigest(),
            }
        self.write_manifest()
        self.write_wav()

    @staticmethod
    def put(path, raw):
        path.write_bytes(raw)
        path.chmod(0o600)

    def write_manifest(self):
        self.put(self.bundle, json.dumps(self.manifest).encode())

    def write_wav(self, *, rate=16000, channels=1, silence=False):
        memory = io.BytesIO()
        with wave.open(memory, "wb") as target:
            target.setnchannels(channels)
            target.setsampwidth(2)
            target.setframerate(rate)
            target.writeframes(bytes(640) if silence else struct.pack("<320h", *range(320)))
        self.put(self.wav, memory.getvalue())

    def load(self):
        return fixture.load_fixture(self.root, self.bundle, self.wav)

    def test_valid_private_fixture_is_hashed_without_executing_models(self):
        with mock.patch.object(fixture.VerifiedOnnxFactory, "__call__",
                               side_effect=AssertionError("Model execution is not validation")):
            result = self.load()
        self.assertEqual(set(result.models), {"mel", "embedding", "classifier"})
        self.assertTrue(any(result.pcm8k))
        self.assertEqual(len(result.pcm8k), 320)
        self.assertEqual(result.provenance["prepared_pcm8k_sha256"],
                         hashlib.sha256(result.pcm8k).hexdigest())
        self.assertNotIn(str(self.root), json.dumps(result.provenance))

    def test_modified_model_and_duplicate_manifest_keys_are_refused(self):
        self.put(self.root / "mel.onnx", b"modified")
        with self.assertRaisesRegex(fixture.FixtureFailure, "model_digest_mismatch"):
            self.load()
        self.put(self.bundle, b'{"format_version":1,"format_version":1}')
        with self.assertRaisesRegex(fixture.FixtureFailure, "duplicate_json_key"):
            self.load()

    def test_fixture_cannot_escape_private_root_or_follow_symlinks(self):
        outside = self.base / "outside.onnx"
        self.put(outside, b"outside")
        with self.assertRaises(fixture.FixtureFailure):
            fixture.read_private(outside, self.root, 100)
        linked = self.root / "linked.onnx"
        linked.symlink_to(outside)
        with self.assertRaises(fixture.FixtureFailure):
            fixture.read_private(linked, self.root, 100)
        directory_link = self.root / "alias"
        directory_link.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(fixture.FixtureFailure):
            fixture.read_private(directory_link / outside.name, self.root, 100)
        self.manifest["models"]["mel"]["path"] = "../outside.onnx"
        self.write_manifest()
        with self.assertRaises(fixture.FixtureFailure):
            self.load()

    def test_public_permissions_special_files_and_size_limits_are_refused(self):
        self.wav.chmod(0o644)
        with self.assertRaises(fixture.FixtureFailure):
            self.load()
        self.wav.chmod(0o600)
        with mock.patch.object(fixture, "MAX_WAV_BYTES", 16):
            with self.assertRaises(fixture.FixtureFailure):
                self.load()
        fifo = self.root / "pipe"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(fixture.FixtureFailure):
            fixture.read_private(fifo, self.root, 100)

    def test_wrong_audio_format_and_silent_positive_fixture_are_refused(self):
        for options in ({"rate": 8000}, {"channels": 2}, {"silence": True}):
            with self.subTest(options=options):
                self.write_wav(**options)
                with self.assertRaises(fixture.FixtureFailure):
                    self.load()

    def test_missing_cli_fixtures_fail_explicitly_without_onnx_fallback(self):
        with mock.patch.object(fixture, "VerifiedOnnxFactory") as factory:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = fixture.main(["--json"])
        self.assertEqual(code, 1)
        report = json.loads(output.getvalue())
        self.assertIs(report["success"], False)
        self.assertIs(report["physical_io"], False)
        self.assertIs(report["live_ha"], False)
        self.assertEqual(report["error"], "explicit_private_fixtures_required")
        factory.assert_not_called()

    def test_guard_blocks_socket_dns_and_datagram_boundaries(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as datagram:
            with fixture.network_guard() as counters:
                attempts = (
                    lambda: socket.getaddrinfo("example.invalid", 443),
                    lambda: socket.create_connection(("192.0.2.1", 443)),
                    lambda: datagram.connect(("192.0.2.1", 443)),
                    lambda: datagram.connect_ex(("192.0.2.1", 443)),
                    lambda: datagram.sendto(b"never sent", ("192.0.2.1", 443)),
                )
                for attempt in attempts:
                    with self.assertRaisesRegex(fixture.FixtureFailure, "network_forbidden"):
                        attempt()
                self.assertEqual(counters["external_network_attempts"], len(attempts))

class ContentLineageNegativeControls(unittest.IsolatedAsyncioTestCase):
    async def test_rescue_cleanup_cannot_hide_skipped_controller_cleanup(self):
        real_run = fixture.PhoneVoiceController.run

        async def skip_controller_cleanup(controller, *args, **kwargs):
            # Retain the real wake, upload and playback path. Only the normal
            # cleanup is broken; the fixture's later safety rescue still works.
            with mock.patch.object(controller.session, "stop", return_value=None):
                with mock.patch.object(controller.assistant, "stop",
                                       new=mock.AsyncMock(return_value=None)):
                    result = await real_run(controller, *args, **kwargs)
            return replace(result, stop_confirmed=True, assistant_state="stopped")

        with mock.patch.object(fixture.PhoneVoiceController, "run", skip_controller_cleanup):
            result = await fixture.run_scenario(
                "complete", nonzero_pcm8k(), test_detector_factory,
                detector_kind="injected_test", duration_seconds=3,
            )
        self.assertIs(result["upload_matches_postwake_pcm"], True)
        self.assertIs(result["output_matches_tts_pcmu"], True)
        self.assertGreater(result["output_packets"], 0)
        self.assertEqual(result["reason"], "turn_complete")
        self.assertIs(result["success"], False, result)
        self.assertIs(result["cleanup_success"], False)
        self.assertIs(result["stop_confirmed"], False)
        self.assertIs(result["rescue_cleanup_success"], True)

    async def test_nonzero_corruption_after_wake_cannot_pass_content_lineage(self):
        real_send = fixture.StrictHAPeer.send
        altered = []

        async def corrupt_one_frame(peer, message):
            if isinstance(message, bytes) and len(message) > 1 and not altered:
                corrupted = bytearray(message)
                corrupted[3] ^= 1
                message = bytes(corrupted)
                altered.append(True)
            return await real_send(peer, message)

        with mock.patch.object(fixture.StrictHAPeer, "send", corrupt_one_frame):
            result = await fixture.run_scenario(
                "complete", nonzero_pcm8k(), test_detector_factory,
                detector_kind="injected_test", duration_seconds=3,
            )
        self.assertEqual(altered, [True])
        self.assertGreater(result["wake_triggers"], 0)
        self.assertIs(result["success"], False, result)
        self.assertIs(result["turn_success"], False)
        self.assertEqual(result["output_packets"], 0)
        self.assertIs(result["cleanup_success"], True)

    async def test_changed_output_payload_fails_even_when_packets_are_nonzero(self):
        real_send = fixture.MemoryDatagram.sendto

        def corrupt_output(wire, packet, address):
            corrupted = bytearray(packet)
            corrupted[-1] ^= 1
            return real_send(wire, bytes(corrupted), address)

        with mock.patch.object(fixture.MemoryDatagram, "sendto", corrupt_output):
            result = await fixture.run_scenario(
                "complete", nonzero_pcm8k(), test_detector_factory,
                detector_kind="injected_test", duration_seconds=3,
            )
        self.assertIs(result["upload_matches_postwake_pcm"], True)
        self.assertIs(result["output_nonzero"], True)
        self.assertGreater(result["output_packets"], 0)
        self.assertIs(result["output_matches_tts_pcmu"], False)
        self.assertIs(result["success"], False, result)
        self.assertIs(result["turn_success"], False)
        self.assertIs(result["cleanup_success"], True)

    async def test_dropped_upload_frame_cannot_be_hidden_by_a_canned_ha_answer(self):
        real_send = fixture.StrictHAPeer.send
        dropped = []

        async def drop_one_frame(peer, message):
            if isinstance(message, bytes) and len(message) > 1 and not dropped:
                dropped.append(True)
                return None
            return await real_send(peer, message)

        with mock.patch.object(fixture.StrictHAPeer, "send", drop_one_frame):
            result = await fixture.run_scenario(
                "complete", nonzero_pcm8k(), test_detector_factory,
                detector_kind="injected_test", duration_seconds=3,
            )
        self.assertEqual(dropped, [True])
        self.assertGreater(result["wake_triggers"], 0)
        self.assertIs(result["success"], False, result)
        self.assertIs(result["turn_success"], False)
        self.assertEqual(result["output_packets"], 0)
        self.assertIs(result["cleanup_success"], True)


if __name__ == "__main__":
    unittest.main()
