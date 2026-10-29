import copy
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("endpoint_ha", ROOT / "scripts/render_endpoint_ha.py")
ha = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ha)


class EndpointHATests(unittest.TestCase):
    def setUp(self):
        self.inventory = json.loads((ROOT / "examples/endpoint-ha.example.json").read_text())

    def test_example_separates_capabilities_and_uses_existing_entities(self):
        package, dashboard = ha.render(self.inventory)
        self.assertEqual(len(dashboard["views"][0]["cards"]), 3)
        self.assertEqual(set(package["script"]), {
            "endpoint_example_sip_call", "endpoint_example_sip_hangup", "endpoint_example_sip_dnd",
            "endpoint_example_native_dial", "endpoint_example_native_hangup",
            "endpoint_example_cast_speak", "endpoint_example_cast_stop"})
        self.assertNotIn("media_player", package)
        self.assertNotIn("automation", package)
        choices = package["script"]["endpoint_example_native_dial"]["sequence"][-1]["choose"]
        self.assertEqual(choices[0]["sequence"], [{"action": "script.example_phone_dial", "data": {"number": "600"}}])

    def test_every_sip_mutation_reads_fresh_status_and_capabilities_first(self):
        package, _ = ha.render(self.inventory)
        for operation in ("call", "hangup", "dnd"):
            with self.subTest(operation=operation):
                steps = package["script"]["endpoint_example_sip_" + operation]["sequence"]
                actions = [s for s in steps if "action" in s]
                self.assertEqual([s["action"] for s in actions],
                                 ["rest_command.endpoint_example_sip_status", "rest_command.endpoint_example_sip_" + operation])
                rendered = json.dumps(steps)
                self.assertIn("get('ami_fresh') == true", rendered)
                self.assertIn("get('configured') == true", rendered)
                self.assertIn("get('" + operation + "') == true", rendered)
                self.assertNotIn("sensor.endpoint_example_sip_status", rendered)
                self.assertNotIn("repeat", rendered)
                self.assertIn("request_id", actions[-1]["data"])
                command = package["rest_command"]["endpoint_example_sip_" + operation]
                self.assertIn("'idempotency_key': request_id", command["payload"])
                self.assertNotIn("Idempotency-Key", command["headers"])

    def test_hangup_has_one_call_guard_and_exact_pair(self):
        package, _ = ha.render(self.inventory)
        steps = package["script"]["endpoint_example_sip_hangup"]["sequence"]
        self.assertIn("calls | count) == 1", json.dumps(steps))
        mutation = [s for s in steps if s.get("action", "").endswith("_hangup")][0]
        self.assertEqual(mutation["data"]["call"], "{{ endpoint_status.content.calls[0] }}")
        self.assertEqual(package["script"]["endpoint_example_sip_dnd"]["fields"]["enabled"]["selector"],
                         {"boolean": None})

    def test_destination_is_allowlisted_and_self_call_excluded(self):
        self.inventory["destinations"].append({"extension": "209", "label": "Self"})
        package, _ = ha.render(self.inventory)
        call = package["script"]["endpoint_example_sip_call"]
        self.assertEqual([d["value"] for d in call["fields"]["destination"]["selector"]["select"]["options"]], ["600", "201"])
        self.assertIn("endpoint_inputs.destination is string and endpoint_inputs.destination in ['600', '201']", json.dumps(call))
        self.assertEqual(package["input_select"]["endpoint_example_sip_destination"]["options"], ["600", "201"])

    def test_native_adapter_checks_action_and_readiness(self):
        native = self.inventory["endpoints"][1]
        native["actions"]["answer"] = {"action": "example.phone_answer", "data": {"device_id": "example_device"},
                                       "target_entity": "sensor.example_phone_call_state"}
        package, _ = ha.render(self.inventory)
        steps = package["script"]["endpoint_example_native_answer"]["sequence"]
        self.assertIn("is_state('binary_sensor.example_phone_ready', 'on')", json.dumps(steps))
        self.assertEqual(steps[-1], {"action": "example.phone_answer", "data": {"device_id": "example_device"},
                                    "target": {"entity_id": "sensor.example_phone_call_state"}})

    def test_cast_uses_existing_tts_and_actual_media_features(self):
        package, _ = ha.render(self.inventory)
        speak = package["script"]["endpoint_example_cast_speak"]
        self.assertIn("bitwise_and(512)", json.dumps(speak))
        self.assertIn("bitwise_and(4096)", json.dumps(package["script"]["endpoint_example_cast_stop"]))
        self.assertIn("message is string", json.dumps(speak))
        self.assertEqual(speak["sequence"][-1]["action"], "tts.speak")
        self.assertEqual(speak["sequence"][-1]["target"], {"entity_id": "tts.example_provider"})
        self.assertIn("'media_player_entity_id': 'media_player.example_cast'", speak["sequence"][-1]["data"])
        self.assertIn("'message': endpoint_inputs.message | trim", speak["sequence"][-1]["data"])
        self.assertNotIn("announce", speak["sequence"][-1]["data"])
        self.assertEqual(package["input_text"]["endpoint_example_cast_message"]["max"], 255)

    def test_cast_only_package_needs_no_gateway_or_destination(self):
        package, _ = ha.render({"version": 1, "endpoints": [self.inventory["endpoints"][2]]})
        self.assertNotIn("rest", package)
        self.assertNotIn("rest_command", package)

    def test_invalid_configuration_is_rejected_before_writing(self):
        for index, edit in enumerate((
            lambda i: i.update(version=True),
            lambda i: i.update(unknown=True),
            lambda i: i["endpoints"].append(i["endpoints"][0]),
            lambda i: i["endpoints"][0].update(extension="209\r\nAction: Hangup"),
            lambda i: i["endpoints"][0].update(auto_answer=True),
            lambda i: i["endpoints"][1]["actions"]["dial"].update(destination_field="number\n"),
            lambda i: i["endpoints"][1]["actions"]["dial"].update(data={"token": "private"}),
            lambda i: i["endpoints"][1]["actions"]["dial"].update(data={"device_id": "{{ bad }}"}),
            lambda i: i["endpoints"][1]["actions"]["dial"].update(action="script.endpoint_example_native_dial"),
            lambda i: i["endpoints"][2].update(tts_entity="media_player.fake"),
            lambda i: i["destinations"].append({"extension": "+18005551212", "label": "External"}),
            lambda i: i["gateway"].update(base_url="http://user:password@example.invalid"),
            lambda i: i["gateway"].update(base_url="http://example.invalid/?token=private"),
            lambda i: i["gateway"].update(authorization_secret="bad key"),
        )):
            inventory = copy.deepcopy(self.inventory)
            edit(inventory)
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "new"
                with self.assertRaises(ValueError):
                    ha.write_outputs(inventory, output)
                self.assertFalse(output.exists())

    def test_output_is_private_deterministic_and_references_secret_name(self):
        original = copy.deepcopy(self.inventory)
        with tempfile.TemporaryDirectory() as temporary:
            names = ha.write_outputs(self.inventory, temporary)
            first = [(Path(temporary) / name).read_text() for name in names]
            ha.write_outputs(self.inventory, temporary)
            self.assertEqual(first, [(Path(temporary) / name).read_text() for name in names])
            for name in names:
                self.assertEqual(stat.S_IMODE((Path(temporary) / name).stat().st_mode), 0o600)
            self.assertIn("!secret endpoint_gateway_authorization", first[0])
            self.assertNotIn(ha.SECRET_MARKER, first[0])
            decoded = json.loads(first[0].replace("!secret endpoint_gateway_authorization", '"secret-reference"'))
            self.assertEqual(decoded["rest"][0]["headers"]["Authorization"], "secret-reference")
        self.assertEqual(self.inventory, original)

    def test_output_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            target = output / "preserve"
            target.write_text("existing")
            (output / "endpoint-package.yaml").symlink_to(target)
            with self.assertRaises(ValueError):
                ha.write_outputs(self.inventory, output)
            self.assertEqual(target.read_text(), "existing")
            self.assertFalse((output / "endpoint-dashboard.yaml").exists())

    def test_cli_rejects_duplicate_json_without_printing_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.json"
            path.write_text('{"version": 1, "version": "private-value", "endpoints": []}')
            result = subprocess.run([sys.executable, str(ROOT / "scripts/render_endpoint_ha.py"),
                                     "--inventory", str(path), "--out", str(Path(temporary) / "out")],
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("private-value", result.stdout + result.stderr)
            self.assertFalse((Path(temporary) / "out").exists())


if __name__ == "__main__":
    unittest.main()
