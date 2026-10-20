"""Offline acceptance of the actual voice-session entry point.

Public tests inject an explicitly labelled detector. They never load a private
model, use household credentials, contact HA, or open physical phone media.
"""

import contextlib
import importlib.util
import io
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "voice_entrypoint_acceptance_fixture", ROOT / "scripts/test-voice-entrypoint.py")
fixture = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fixture
SPEC.loader.exec_module(fixture)


def nonzero_pcm8k():
    samples = [
        round(9500 * math.sin(2 * math.pi * 430 * index / 8000))
        for index in range(8000)
    ]
    return struct.pack("<8000h", *samples)


class DeterministicTestDetector:
    """A test trigger, not evidence of ONNX inference or wake-word accuracy."""

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


def run_case(scenario="complete", **kwargs):
    return fixture.run_scenario(
        scenario, nonzero_pcm8k(), detector_factory=test_detector_factory,
        duration_seconds=kwargs.pop("duration_seconds", 3), **kwargs)


class EntrypointScenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.complete = run_case()
        cls.no_wake = run_case("no-wake", duration_seconds=1.5)

    def test_completed_turn_and_no_wake_have_distinct_native_exit_results(self):
        self.assertIs(self.complete["assertion_success"], True, self.complete)
        self.assertEqual(self.complete["entrypoint_exit_code"], 0)
        self.assertIs(self.complete["turn_success"], True)
        self.assertIs(self.no_wake["assertion_success"], True, self.no_wake)
        self.assertEqual(self.no_wake["entrypoint_exit_code"], 1)
        self.assertIs(self.no_wake["turn_success"], False)

    def test_injected_ci_detector_is_never_labelled_actual_onnx(self):
        for result in (self.complete, self.no_wake):
            with self.subTest(scenario=result["scenario"]):
                self.assertEqual(result["detector_kind"], "injected_test")
                self.assertIs(result["actual_onnx"], False)
                self.assertEqual(result["external_network_attempts"], 0)

    def test_real_loaders_builder_and_model_factory_are_all_used(self):
        for result in (self.complete, self.no_wake):
            with self.subTest(scenario=result["scenario"]):
                self.assertEqual(result["transport_load_calls"], 1)
                self.assertEqual(result["voice_load_calls"], 1)
                self.assertEqual(result["build_service_calls"], 1)
                self.assertEqual(result["model_factory_calls"], 1)
                self.assertIs(result["loaded_config_matches_build"], True)
                self.assertEqual(result["controller_modes"], ["wake"])
                self.assertEqual(result["media_opened"], 1)
                for kind in ("voice", "transport"):
                    self.assertRegex(result["config_sha256"][kind], r"^[0-9a-f]{64}$")

    def test_completed_turn_preserves_both_audio_directions(self):
        result = self.complete
        self.assertGreater(result["wake_triggers"], 0)
        self.assertEqual(result["ha_sessions"], 1)
        self.assertGreater(result["ha_audio_bytes"], 0)
        self.assertEqual(result["ha_eos"], 1)
        self.assertIs(result["upload_matches_postwake_pcm"], True)
        self.assertGreater(result["output_packets"], 0)
        self.assertIs(result["output_matches_tts_pcmu"], True)
        self.assertIs(result["output_nonzero"], True)

    def test_no_wake_never_opens_ha_or_outputs_audio(self):
        result = self.no_wake
        self.assertEqual(result["minimum_silence_frames"], len(nonzero_pcm8k()) // 320)
        self.assertGreaterEqual(result["detector_frames"], result["minimum_silence_frames"])
        self.assertGreater(result["decoded_input_bytes"], 0)
        self.assertEqual(result["wake_triggers"], 0)
        self.assertEqual(result["ha_sessions"], 0)
        self.assertEqual(result["ha_audio_bytes"], 0)
        self.assertEqual(result["ha_eos"], 0)
        self.assertEqual(result["output_packets"], 0)
        self.assertNotIn("speaking", result["states"])

    def test_normal_entrypoint_cleanup_precedes_fixture_safety_cleanup(self):
        for result in (self.complete, self.no_wake):
            with self.subTest(scenario=result["scenario"]):
                self.assertIs(result["cleanup_success"], True, result)
                self.assertIs(result["rescue_cleanup_success"], True)
                self.assertIs(result["stop_confirmed"], True)
                self.assertIs(result["stale_stream_rejected"], True)
                self.assertEqual(result["output_after_stop"], 0)


class EntrypointNegativeControls(unittest.TestCase):
    def test_missing_audio_cannot_pass_the_no_wake_control(self):
        def wait_for_lease(transport, _voice):
            transport["receive_idle_seconds"] = 2

        with mock.patch.object(fixture.runner.transport.UdpMedia, "receive", return_value=[]):
            result = run_case("no-wake", duration_seconds=1.5, config_mutator=wait_for_lease)
        self.assertEqual(result["entrypoint_exit_code"], 1, result)
        self.assertIs(result["cleanup_success"], True)
        self.assertEqual(result["entrypoint_state_before_rescue"]["transport_reason"],
                         "lease_expired")
        self.assertEqual(result["model_factory_calls"], 1)
        self.assertEqual(result["detector_frames"], 0)
        self.assertEqual(result["decoded_input_bytes"], 0)
        self.assertEqual(result["wake_triggers"], 0)
        self.assertEqual(result["ha_sessions"], 0)
        self.assertEqual(result["output_packets"], 0)
        self.assertIs(result["assertion_success"], False, result)

    def test_early_transport_stop_is_not_a_successful_no_wake_lease(self):
        original_poll = fixture.runner.transport.VoiceSession.poll

        def preempt_after_full_input(session):
            frames = original_poll(session)
            if session.media.receiver.counters["received"] >= 55:
                session.stop("preempted_call")
            return frames

        with mock.patch.object(fixture.runner.transport.VoiceSession, "poll",
                               preempt_after_full_input):
            result = run_case("no-wake")
        self.assertEqual(result["entrypoint_exit_code"], 1, result)
        self.assertIs(result["cleanup_success"], True)
        self.assertGreaterEqual(result["detector_frames"], result["minimum_silence_frames"])
        self.assertEqual(result["entrypoint_state_before_rescue"]["transport_reason"],
                         "preempted_call")
        self.assertEqual(result["wake_triggers"], 0)
        self.assertEqual(result["ha_sessions"], 0)
        self.assertEqual(result["output_packets"], 0)
        self.assertIs(result["assertion_success"], False, result)

    def test_changed_configuration_cannot_pass_even_when_the_audio_turn_succeeds(self):
        original_load = fixture.runner.load_config

        def change_after_loading(path):
            value = original_load(path)
            # A valid setting that does not affect this short conversation:
            # only comparing the emitted audio would miss this substitution.
            value["service"]["queue_frames"] = 73
            return value

        with mock.patch.object(fixture.runner, "load_config", change_after_loading):
            result = run_case()
        self.assertEqual(result["entrypoint_exit_code"], 0, result)
        self.assertEqual(result["entrypoint_reason"], "turn_complete")
        self.assertEqual(result["controller_modes"], ["wake"])
        self.assertEqual(result["model_factory_calls"], 1)
        self.assertIs(result["cleanup_success"], True)
        self.assertIs(result["upload_matches_postwake_pcm"], True)
        self.assertIs(result["output_matches_tts_pcmu"], True)
        self.assertIs(result["loaded_config_matches_build"], False)
        self.assertIs(result["assertion_success"], False, result)

    def test_no_trigger_cannot_pass_the_positive_case(self):
        result = fixture.run_scenario(
            "complete", nonzero_pcm8k(), detector_factory=lambda _: NeverWake(),
            duration_seconds=.3)
        self.assertIs(result["assertion_success"], False, result)
        self.assertIs(result["turn_success"], False)
        self.assertEqual(result["wake_triggers"], 0)
        self.assertEqual(result["ha_sessions"], 0)
        self.assertEqual(result["output_packets"], 0)
        self.assertIs(result["cleanup_success"], True)

    def test_manual_configuration_cannot_qualify_as_wake_acceptance(self):
        def manual_mode(_transport, voice):
            voice["wake"]["mode"] = "manual"

        result = run_case(config_mutator=manual_mode, duration_seconds=.3)
        self.assertIs(result["assertion_success"], False, result)
        self.assertIs(result["actual_onnx"], False)
        self.assertEqual(result["wake_triggers"], 0)
        self.assertEqual(result["model_factory_calls"], 0)
        self.assertEqual(result["controller_modes"], ["manual"])

    def test_controller_switching_to_manual_cannot_bypass_the_wake_requirement(self):
        actual_run = fixture.runner.PhoneVoiceController.run

        async def wrong_mode(controller, **_kwargs):
            return await actual_run(controller, mode="manual")

        with mock.patch.object(fixture.runner.PhoneVoiceController, "run", wrong_mode):
            result = run_case(duration_seconds=.3)
        self.assertIs(result["assertion_success"], False, result)
        self.assertEqual(result["wake_triggers"], 0)

    def test_validation_default_does_not_enter_runtime(self):
        with mock.patch.object(fixture.runner, "run_session") as runtime:
            result = run_case(entrypoint_flags=())
        runtime.assert_not_called()
        self.assertEqual(result["entrypoint_exit_code"], 0)
        self.assertIs(result["assertion_success"], False, result)
        self.assertEqual(result["media_opened"], 0)
        self.assertEqual(result["build_service_calls"], 0)
        self.assertEqual(result["ha_sessions"], 0)
        self.assertEqual(result["output_packets"], 0)

    def test_each_missing_explicit_pilot_flag_refuses_before_runtime(self):
        flags = ("--run", "--attended", "--exclusive-media", "--enable-microphone")
        for missing in flags[1:]:
            with self.subTest(missing=missing):
                with mock.patch.object(fixture.runner, "run_session") as runtime:
                    result = run_case(
                        entrypoint_flags=tuple(flag for flag in flags if flag != missing))
                runtime.assert_not_called()
                self.assertEqual(result["entrypoint_exit_code"], 2)
                self.assertIs(result["assertion_success"], False, result)
                self.assertEqual(result["media_opened"], 0)
                self.assertEqual(result["build_service_calls"], 0)
                self.assertEqual(result["ha_sessions"], 0)
                self.assertEqual(result["output_packets"], 0)

    def test_real_loaders_reject_malformed_configuration_before_runtime(self):
        def invalid_voice(_transport, voice):
            voice["service"]["enabled"] = "true"

        def invalid_transport(transport, _voice):
            transport["bind_port"] = -1

        for mutate in (invalid_voice, invalid_transport):
            with self.subTest(mutate=mutate.__name__):
                with mock.patch.object(fixture.runner, "run_session") as runtime:
                    result = run_case(config_mutator=mutate)
                runtime.assert_not_called()
                self.assertEqual(result["entrypoint_exit_code"], 2)
                self.assertIs(result["assertion_success"], False, result)
                self.assertEqual(result["media_opened"], 0)
                self.assertEqual(result["build_service_calls"], 0)
                self.assertEqual(result["ha_sessions"], 0)
                self.assertEqual(result["output_packets"], 0)

    def test_disallowed_phone_is_rejected_before_runtime(self):
        def disallow_phone(_transport, voice):
            voice["phones"] = {key: False for key in voice["phones"]}

        with mock.patch.object(fixture.runner, "run_session") as runtime:
            result = run_case(config_mutator=disallow_phone)
        runtime.assert_not_called()
        self.assertEqual(result["entrypoint_exit_code"], 2)
        self.assertIs(result["assertion_success"], False, result)
        self.assertEqual(result["media_opened"], 0)
        self.assertEqual(result["build_service_calls"], 0)
        self.assertEqual(result["ha_sessions"], 0)
        self.assertEqual(result["output_packets"], 0)

    def test_disabled_service_and_invalid_token_path_stop_before_model_or_media(self):
        def disabled(_transport, voice):
            voice["service"]["enabled"] = False

        def missing_token(_transport, voice):
            voice["ha"]["token_file"] = ".local/absent-test-token"

        def escaped_token(_transport, voice):
            voice["ha"]["token_file"] = "../outside-private-assets"

        for mutate in (disabled, missing_token, escaped_token):
            with self.subTest(mutate=mutate.__name__):
                result = run_case(config_mutator=mutate)
                self.assertEqual(result["entrypoint_exit_code"], 2)
                self.assertEqual(result["build_service_calls"], 1)
                self.assertEqual(result["model_factory_calls"], 0)
                self.assertEqual(result["media_opened"], 0)
                self.assertEqual(result["ha_sessions"], 0)
                self.assertEqual(result["output_packets"], 0)
                self.assertIs(result["assertion_success"], False, result)

    def test_successful_exit_cannot_hide_media_left_open_until_fixture_rescue(self):
        original_main = fixture.runner.main

        def skip_normal_media_cleanup(argv):
            # Suppress both controller cleanup and run_session's real finally.
            # Restore stop before the fixture's last-resort safety cleanup.
            with mock.patch.object(fixture.runner.transport.VoiceSession, "stop",
                                   return_value=None):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    original_main(argv)
            report = json.loads(output.getvalue())
            # Model a faulty success report, without altering observed wake,
            # configuration, audio bytes, or the actual remaining resources.
            report["result"]["stop_confirmed"] = True
            report["result"]["assistant_state"] = "stopped"
            report["watchdog_stop_errors"] = 0
            report["watchdog_stop_confirmed"] = True
            print(json.dumps(report))
            return 0

        with mock.patch.object(fixture.runner, "main", skip_normal_media_cleanup):
            result = run_case()
        self.assertEqual(result["entrypoint_exit_code"], 0, result)
        self.assertEqual(result["entrypoint_reason"], "turn_complete")
        self.assertEqual(result["controller_modes"], ["wake"])
        self.assertEqual(result["model_factory_calls"], 1)
        self.assertIs(result["loaded_config_matches_build"], True)
        self.assertGreater(result["output_packets"], 0)
        self.assertIs(result["upload_matches_postwake_pcm"], True)
        self.assertIs(result["output_matches_tts_pcmu"], True)
        self.assertIs(result["assertion_success"], False, result)
        self.assertIs(result["cleanup_success"], False)
        self.assertIs(result["stop_confirmed"], False)
        self.assertIs(result["entrypoint_state_before_rescue"]["transport_stopped"], False)
        self.assertIs(result["entrypoint_state_before_rescue"]["media_closed"], False)
        self.assertIs(result["rescue_cleanup_success"], True)


class EntrypointCommandSafety(unittest.TestCase):
    def test_foreign_loaded_module_is_rejected_before_fixture_read_or_inference(self):
        args = SimpleNamespace(
            private_root="/synthetic-not-read/.local", bundle="unused-bundle.json",
            positive_wav="unused.wav", scenario="all", duration_seconds=3)
        runner = fixture.runner
        for owner, attribute in ((runner, "__file__"), (runner.__spec__, "origin")):
            with self.subTest(attribute=attribute):
                with mock.patch.object(owner, attribute, str(ROOT / "voice_assistant/config.py")):
                    with mock.patch.object(fixture.pipeline, "load_fixture") as read_fixture:
                        with mock.patch.object(fixture, "run_scenario") as scenario:
                            with self.assertRaisesRegex(fixture.pipeline.FixtureFailure,
                                                        "loaded_module_origin_mismatch"):
                                fixture.run_suite(args)
                read_fixture.assert_not_called()
                scenario.assert_not_called()

    def test_missing_private_fixture_arguments_never_launch_worker_or_model(self):
        with mock.patch.object(fixture.subprocess, "Popen") as launch:
            with mock.patch.object(fixture, "run_suite") as suite:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    code = fixture.main(["--json"])
        launch.assert_not_called()
        suite.assert_not_called()
        report = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertIs(report["assertion_success"], False)
        self.assertEqual(report["error"], "explicit_private_fixture_arguments_required")
        self.assertIs(report["physical_io"], False)
        self.assertIs(report["live_ha"], False)

    def test_worker_deadline_terminates_or_kills_without_waiting_for_native_inference(self):
        arguments = [
            "--private-root", "/synthetic-not-read/.local",
            "--bundle", "unused-bundle.json", "--positive-wav", "unused.wav",
            "--timeout", "1", "--json",
        ]
        for termination_stalls in (False, True):
            with self.subTest(termination_stalls=termination_stalls):
                child = mock.Mock()
                timeout = subprocess.TimeoutExpired("synthetic-worker", 1)
                child.wait.side_effect = (
                    [timeout, timeout, 0] if termination_stalls else [timeout, 0])
                with mock.patch.object(fixture.subprocess, "Popen", return_value=child):
                    with mock.patch.object(fixture, "run_suite") as suite:
                        with contextlib.redirect_stdout(io.StringIO()) as output:
                            code = fixture.main(arguments)
                suite.assert_not_called()
                child.terminate.assert_called_once_with()
                self.assertEqual(child.kill.call_count, int(termination_stalls))
                self.assertEqual(child.wait.call_args_list[0], mock.call(timeout=1))
                report = json.loads(output.getvalue())
                self.assertEqual(code, 2)
                self.assertIs(report["assertion_success"], False)
                self.assertEqual(report["error"], "entrypoint_worker_deadline")


if __name__ == "__main__":
    unittest.main()
