"""Exercise the real reservation wrapper and child with synthetic offline I/O.

The deterministic detector is a public test seam, never ONNX acceptance.
These tests do not use private models, household settings or physical media.
"""

import contextlib
import copy
import importlib.util
import io
import itertools
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "voice_reserved_acceptance_fixture", ROOT / "scripts/test-voice-reserved.py")
fixture = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fixture
SPEC.loader.exec_module(fixture)


def nonzero_pcm8k():
    return struct.pack("<8000h", *(
        round(9500 * math.sin(2 * math.pi * 430 * sample / 8000))
        for sample in range(8000)
    ))


def run_case(scenario="complete", **kwargs):
    return fixture.run_scenario(
        scenario, nonzero_pcm8k(), detector_kind="injected_test",
        duration_seconds=kwargs.pop("duration_seconds", 3),
        timeout=20, **kwargs)


class ReservedScenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {
            scenario: run_case(
                scenario, duration_seconds=3 if scenario in ("complete", "ha-timeout") else 1.5)
            for scenario in ("complete", "no-wake", "ha-timeout", "slow-ha", "cancel")
        }

    def test_all_five_real_wrapper_scenarios_pass_without_private_inference(self):
        self.assertEqual(set(self.results),
                         {"complete", "no-wake", "ha-timeout", "slow-ha", "cancel"})
        for scenario, result in self.results.items():
            with self.subTest(scenario=scenario):
                self.assertIs(result["assertion_success"], True, result)
                self.assertIs(result["child"]["actual_onnx"], False)
                self.assertEqual(result["external_network_attempts"], 0)
                for process in ("child", "wrapper", "replay"):
                    self.assertEqual(result[process]["source_sha256"], result["source_sha256"])

    def test_wrapper_uses_an_exited_real_child_and_drains_its_reader(self):
        for scenario, result in self.results.items():
            with self.subTest(scenario=scenario):
                wrapper = result["wrapper"]
                self.assertEqual(wrapper["child_launches"], 1)
                self.assertIsInstance(wrapper["child_pid"], int)
                self.assertGreater(wrapper["child_pid"], 0)
                self.assertNotEqual(wrapper["child_pid"], os.getpid())
                self.assertIs(wrapper["child_exited"], True)
                self.assertIs(wrapper["reader_drained"], True)
                self.assertIs(result["child"]["state_before_rescue"]["observer_stop_requested"], True)
                self.assertIs(result["child"]["state_before_rescue"]["observer_stopped"], True)

    def test_inherited_lease_excludes_other_owners_until_verified_cleanup(self):
        for scenario, result in self.results.items():
            with self.subTest(scenario=scenario):
                wrapper = result["wrapper"]
                self.assertIs(wrapper["lease_inherited"], True)
                self.assertIs(wrapper["concurrent_lock_blocked"], True)
                self.assertIs(result["cleanup_success"], True)
                self.assertIs(wrapper["lock_available"], True)
                self.assertIs(wrapper["snapshot_removed"], True)
                self.assertIs(wrapper["journal_reserved"], False)

    def test_consumed_attempt_survives_completion_and_refuses_a_new_process(self):
        for scenario, result in self.results.items():
            with self.subTest(scenario=scenario):
                self.assertIs(result["wrapper"]["consumed_marker_persisted"], True)
                replay = result["replay"]
                self.assertIs(replay["rejected"], True)
                self.assertEqual(replay["exit_code"], 2)
                self.assertEqual(replay["child_launches"], 0)
                self.assertIs(replay["marker_unchanged"], True)
                self.assertIs(replay["journal_unchanged"], True)

    def test_complete_uses_actual_upload_and_output_with_verified_cleanup(self):
        result = self.results["complete"]
        child = result["child"]
        self.assertIs(result["turn_success"], True)
        self.assertEqual(result["wrapper"]["exit_code"], 0)
        self.assertEqual(child["entrypoint_exit_code"], 0)
        self.assertEqual(child["reason"], "turn_complete")
        self.assertGreater(child["wake_triggers"], 0)
        self.assertEqual(child["ha_sessions"], 1)
        self.assertGreater(child["ha_audio_bytes"], 0)
        self.assertEqual(child["ha_eos"], 1)
        self.assertIs(child["upload_matches_postwake_pcm"], True)
        self.assertGreater(child["output_packets"], 0)
        self.assertIs(child["output_matches_tts_pcmu"], True)
        self.assertIs(child["output_nonzero"], True)
        self.assertIs(child["cleanup_success"], True)

    def test_selected_pipeline_reaches_the_ha_wire_in_every_answer_attempt(self):
        for scenario in ("complete", "ha-timeout", "slow-ha", "cancel"):
            with self.subTest(scenario=scenario):
                self.assertEqual(self.results[scenario]["child"]["ha_pipeline"],
                                 fixture.PIPELINE_ID)

    def test_production_claim_snapshot_journal_and_finish_functions_execute(self):
        calls = self.results["complete"]["wrapper"]["profile_calls"]
        for name in ("claim_attempt", "prepare", "journal_write", "reserved_finish",
                     "control_finish", "drain_cleanup", "flock", "fsync"):
            with self.subTest(function=name):
                self.assertGreater(calls.get(name, 0), 0)

    def test_child_consumes_the_exact_snapshot_and_arguments_prepared_by_wrapper(self):
        for scenario, result in self.results.items():
            with self.subTest(scenario=scenario):
                child, wrapper = result["child"], result["wrapper"]
                self.assertIs(child["prepared_config_used"], True)
                self.assertEqual(child["model_factory_calls"], 1)
                self.assertEqual(child["prepared_snapshot_sha256"],
                                 wrapper["prepared_snapshot_sha256"])
                self.assertEqual(set(child["prepared_snapshot_sha256"]),
                                 {"transport.json", "voice.json", "gateway.json", "ha-token.txt"})
                self.assertEqual(child["native_argv_sha256"], wrapper["native_argv_sha256"])
                for checksum in (*child["prepared_snapshot_sha256"].values(),
                                 child["native_argv_sha256"]):
                    self.assertRegex(checksum, r"^[0-9a-f]{64}$")

    def test_silence_reaches_detector_without_creating_an_answer(self):
        result = self.results["no-wake"]
        child = result["child"]
        self.assertIs(result["turn_success"], False)
        self.assertEqual(result["wrapper"]["exit_code"], 1)
        self.assertEqual(child["entrypoint_exit_code"], 1)
        self.assertGreaterEqual(child["detector_frames"], child["minimum_silence_frames"])
        self.assertGreater(child["decoded_input_bytes"], 0)
        self.assertIs(child["decoded_input_nonzero"], False)
        self.assertEqual(child["wake_triggers"], 0)
        self.assertEqual(child["ha_sessions"], 0)
        self.assertEqual(child["ha_audio_bytes"], 0)
        self.assertEqual(child["output_packets"], 0)

    def test_slow_provider_and_cancellation_close_without_a_fabricated_reply(self):
        for scenario in ("ha-timeout", "slow-ha", "cancel"):
            with self.subTest(scenario=scenario):
                result = self.results[scenario]
                child = result["child"]
                self.assertIs(result["turn_success"], False)
                self.assertEqual(result["wrapper"]["exit_code"], 1)
                self.assertEqual(child["ha_sessions"], 1)
                self.assertGreater(child["ha_audio_bytes"], 0)
                self.assertEqual(child["ha_eos"], 1)
                self.assertEqual(child["output_packets"], 0)
                self.assertIs(child["cleanup_success"], True)
                self.assertEqual(child["output_after_stop"], 0)

    def test_provider_deadline_and_outer_lease_have_different_outcomes(self):
        timed_out = self.results["ha-timeout"]["child"]
        expired = self.results["slow-ha"]["child"]
        self.assertEqual(timed_out["reason"], "assistant_error")
        self.assertIn("turn_timeout", timed_out["assistant_errors"])
        self.assertIn(expired["reason"], ("shutdown", "lease_expired", "transport_stopped"))
        self.assertNotEqual(expired["reason"], timed_out["reason"])

    def test_fixture_rescue_cannot_make_failed_child_cleanup_acceptable(self):
        # This checks the acceptance verifier against retained real-child
        # evidence. It does not claim another child cleanup fault was executed.
        good = self.results["complete"]
        self.assertIs(fixture.assess(good), True)
        damaged = copy.deepcopy(good)
        damaged["child"]["cleanup_success"] = False
        damaged["child"]["rescue_cleanup_success"] = True
        damaged["cleanup_success"] = True
        damaged["assertion_success"] = True
        self.assertIs(fixture.assess(damaged), False)

    def test_verifier_checks_pre_rescue_state_instead_of_only_cleanup_labels(self):
        good = self.results["complete"]
        for key in ("transport_stopped", "media_closed", "stream_invalidated",
                    "task_cleared", "queue_cleared", "owner_released",
                    "observer_stop_requested", "observer_stopped"):
            with self.subTest(key=key):
                damaged = copy.deepcopy(good)
                damaged["child"]["state_before_rescue"][key] = False
                damaged["child"]["cleanup_success"] = True
                damaged["child"]["rescue_cleanup_success"] = True
                self.assertIs(fixture.assess(damaged), False)

    def test_claimed_success_cannot_hide_an_active_child_or_reserved_journal(self):
        good = self.results["complete"]
        for key, bad_value in (
            ("child_exited", False), ("reader_drained", False),
            ("journal_reserved", True), ("lock_available", False),
        ):
            with self.subTest(key=key):
                damaged = copy.deepcopy(good)
                damaged["wrapper"][key] = bad_value
                self.assertIs(fixture.assess(damaged), False)

    def test_replayed_attempt_that_launches_another_child_is_rejected(self):
        damaged = copy.deepcopy(self.results["complete"])
        damaged["replay"]["child_launches"] = 1
        self.assertIs(fixture.assess(damaged), False)
        damaged = copy.deepcopy(self.results["complete"])
        damaged["replay"]["journal_unchanged"] = False
        self.assertIs(fixture.assess(damaged), False)

    def test_verifier_rejects_different_sources_even_if_each_process_claims_stability(self):
        good = self.results["complete"]
        for process in ("child", "wrapper", "replay"):
            with self.subTest(process=process):
                damaged = copy.deepcopy(good)
                hashes = damaged[process]["source_sha256"]
                hashes[next(iter(hashes))] = "0" * 64
                damaged[process]["loaded_sources_unchanged"] = True
                self.assertIs(fixture.assess(damaged), False)

    def test_verifier_rejects_changed_prepared_files_and_child_arguments(self):
        good = self.results["complete"]
        changed_file = copy.deepcopy(good)
        changed_file["child"]["prepared_snapshot_sha256"]["voice.json"] = "0" * 64
        self.assertIs(fixture.assess(changed_file), False)
        changed_arguments = copy.deepcopy(good)
        changed_arguments["child"]["native_argv_sha256"] = "0" * 64
        self.assertIs(fixture.assess(changed_arguments), False)

    def test_verifier_requires_real_decoded_silence_for_no_wake(self):
        for field, bad_value in (("decoded_input_bytes", 0), ("decoded_input_nonzero", True)):
            with self.subTest(field=field):
                damaged = copy.deepcopy(self.results["no-wake"])
                damaged["child"][field] = bad_value
                self.assertIs(fixture.assess(damaged), False)


class ReservedNegativeControls(unittest.TestCase):
    def test_nontriggering_detector_cannot_pass_a_positive_turn(self):
        result = run_case(test_fault="never-wake", duration_seconds=1.5)
        self.assertIs(result["assertion_success"], False, result)
        self.assertIs(result["turn_success"], False)
        self.assertEqual(result["child"]["wake_triggers"], 0)
        self.assertEqual(result["child"]["ha_sessions"], 0)
        self.assertEqual(result["child"]["output_packets"], 0)

    def test_wrong_selected_pipeline_cannot_pass_the_actual_wire_contract(self):
        result = run_case(test_fault="wrong-pipeline", duration_seconds=1.5)
        self.assertIs(result["assertion_success"], False, result)
        self.assertIs(result["turn_success"], False)
        self.assertEqual(result["child"]["output_packets"], 0)

    def test_missing_input_cannot_pass_the_no_wake_control(self):
        result = run_case("no-wake", test_fault="drop-input", duration_seconds=1.5)
        self.assertIs(result["assertion_success"], False, result)
        self.assertEqual(result["child"]["detector_frames"], 0)
        self.assertEqual(result["child"]["ha_sessions"], 0)
        self.assertEqual(result["child"]["output_packets"], 0)


class ReservedCommandSafety(unittest.TestCase):
    ARGUMENTS = [
        "--private-root", "/synthetic-not-read/.local",
        "--bundle", "unused-bundle.json", "--positive-wav", "unused.wav", "--json",
    ]

    def test_missing_private_paths_never_load_models_or_spawn_workers(self):
        with mock.patch.object(fixture.pipeline, "load_fixture") as load:
            with mock.patch.object(fixture.subprocess, "Popen") as launch:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    code = fixture.main(["--json"])
        load.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(code, 2)
        report = json.loads(output.getvalue())
        self.assertIs(report["assertion_success"], False)
        self.assertEqual(report["error"], "explicit_private_fixture_arguments_required")

    def test_invalid_durations_and_timeouts_fail_before_loading_private_files(self):
        for option, values in (
            ("--duration-seconds", ("nan", "inf", "0", "31")),
            ("--timeout", ("nan", "inf", "0", "121")),
        ):
            for value in values:
                with self.subTest(option=option, value=value):
                    with mock.patch.object(fixture.pipeline, "load_fixture") as load:
                        with mock.patch.object(fixture.subprocess, "Popen") as launch:
                            with contextlib.redirect_stdout(io.StringIO()) as output:
                                code = fixture.main(self.ARGUMENTS + [option, value])
                    load.assert_not_called()
                    launch.assert_not_called()
                    self.assertEqual(code, 2)
                    self.assertIs(json.loads(output.getvalue())["assertion_success"], False)

    def test_public_cli_does_not_offer_an_injected_detector_or_fault_option(self):
        for option in ("--detector-kind", "--test-fault"):
            with self.subTest(option=option):
                with mock.patch.object(fixture.pipeline, "load_fixture") as load:
                    with mock.patch.object(fixture.subprocess, "Popen") as launch:
                        with contextlib.redirect_stderr(io.StringIO()):
                            with self.assertRaises(SystemExit) as error:
                                fixture.main(self.ARGUMENTS + [option, "injected_test"])
                self.assertEqual(error.exception.code, 2)
                load.assert_not_called()
                launch.assert_not_called()

    def test_onnx_mode_without_a_private_fixture_cannot_launch_a_test_detector(self):
        with mock.patch.object(fixture.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(fixture.pipeline.FixtureFailure,
                                        "actual_onnx_requires_private_fixture"):
                fixture.run_scenario("complete", nonzero_pcm8k(), detector_kind="onnx")
        launch.assert_not_called()

    def test_changed_module_file_or_origin_is_rejected_without_spawning(self):
        for owner, field in ((fixture.runner, "__file__"), (fixture.runner.__spec__, "origin")):
            with self.subTest(field=field):
                with mock.patch.object(owner, field, str(ROOT / "voice_assistant/config.py")):
                    with mock.patch.object(fixture.subprocess, "Popen") as launch:
                        with self.assertRaisesRegex(fixture.pipeline.FixtureFailure,
                                                    "loaded_module_origin_mismatch"):
                            fixture.sources()
                launch.assert_not_called()

    def test_total_suite_budget_is_shared_instead_of_restarted_for_each_scenario(self):
        fake_fixture = SimpleNamespace(pcm8k=nonzero_pcm8k())
        with mock.patch.object(fixture.pipeline, "load_fixture", return_value=fake_fixture):
            with mock.patch.object(fixture, "run_scenario", return_value={}) as run:
                with mock.patch.object(fixture.time, "monotonic", side_effect=[0, 1, 4]):
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        code = fixture.main(self.ARGUMENTS + ["--timeout", "3"])
        self.assertEqual(code, 2)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["timeout"], 2)
        self.assertEqual(run.call_args.kwargs["detector_kind"], "onnx")
        self.assertEqual(json.loads(output.getvalue())["error"], "suite_deadline")

    def test_wrapper_deadline_stops_the_owned_process_without_waiting_for_a_report(self):
        with tempfile.TemporaryDirectory(prefix="reserved-deadline-test-") as directory:
            local = Path(directory) / ".local"
            local.mkdir()
            descriptor = local / "fixture.json"
            child = mock.Mock()
            child.poll.return_value = None
            with mock.patch.object(fixture.subprocess, "Popen", return_value=child):
                with mock.patch.object(fixture, "terminate_owned") as terminate_owned:
                    with mock.patch.object(fixture.time, "monotonic",
                                           side_effect=itertools.count(10, 2)):
                        with self.assertRaises(subprocess.TimeoutExpired):
                            fixture.invoke_wrapper(descriptor, 1)
            child.terminate.assert_called_once_with()
            child.kill.assert_called_once_with()
            self.assertEqual(child.wait.call_args_list,
                             [mock.call(timeout=2), mock.call(timeout=2)])
            terminate_owned.assert_not_called()

    def test_malformed_report_after_wrapper_exit_still_cleans_its_recorded_child(self):
        with tempfile.TemporaryDirectory(prefix="reserved-bad-report-") as directory:
            local = Path(directory) / ".local"
            local.mkdir()
            descriptor = local / "fixture.json"
            (local / "owned-child.json").write_text(json.dumps({
                "pid": 123456, "descriptor": str(descriptor),
            }))
            wrapper = mock.Mock()
            wrapper.poll.return_value = 0
            wrapper.returncode = 0
            with mock.patch.object(fixture.subprocess, "Popen", return_value=wrapper):
                with mock.patch.object(fixture, "terminate_owned", return_value=True) as cleanup:
                    with self.assertRaises((fixture.pipeline.FixtureFailure, ValueError)):
                        # Mock Popen leaves stdout empty, so no valid child
                        # report exists even though the wrapper exited zero.
                        fixture.invoke_wrapper(descriptor, 1)
            cleanup.assert_called_once_with(123456, descriptor)
            wrapper.kill.assert_not_called()

    def test_valid_report_with_live_or_unconfirmed_child_still_triggers_cleanup(self):
        for child_exited, cleanup_confirmed in ((False, True), (True, False)):
            with self.subTest(child_exited=child_exited, cleanup_confirmed=cleanup_confirmed):
                with tempfile.TemporaryDirectory(prefix="reserved-unsafe-report-") as directory:
                    local = Path(directory) / ".local"
                    local.mkdir()
                    descriptor = local / "fixture.json"
                    (local / "owned-child.json").write_text(json.dumps({
                        "pid": 123456, "descriptor": str(descriptor),
                    }))
                    wrapper = mock.Mock()
                    wrapper.poll.return_value = 0
                    wrapper.returncode = 0
                    report = {
                        "child_launches": 1, "child_pid": 123456,
                        "child_exited": child_exited,
                        "native_cleanup_success": cleanup_confirmed,
                        "reader_drained": True, "snapshot_removed": True,
                        "lock_available": True, "journal_reserved": False,
                    }

                    def launch(_command, **kwargs):
                        kwargs["stdout"].write(json.dumps(report).encode())
                        kwargs["stdout"].flush()
                        return wrapper

                    with mock.patch.object(fixture.subprocess, "Popen", side_effect=launch):
                        with mock.patch.object(fixture, "terminate_owned", return_value=True) as cleanup:
                            with self.assertRaises(fixture.pipeline.FixtureFailure):
                                fixture.invoke_wrapper(descriptor, 1)
                    cleanup.assert_called_once_with(123456, descriptor)
                    wrapper.kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
