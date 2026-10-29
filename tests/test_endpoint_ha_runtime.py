"""Optional actual-HA schema/template tests; services are in-process fakes.

Run in an isolated environment with homeassistant==2026.10.4 and xmltodict.
No HA server, gateway, media player, PBX or network request is used.
"""
import copy
import importlib.util
import json
import tempfile
import unittest
from unittest.mock import patch

from test_endpoint_ha import ROOT, ha

RUNTIME_READY = (importlib.util.find_spec("homeassistant") is not None
                 and importlib.util.find_spec("xmltodict") is not None)


@unittest.skipUnless(RUNTIME_READY, "requires optional Home Assistant runtime and xmltodict")
class EndpointHARuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from homeassistant.core import HomeAssistant, SupportsResponse
        from homeassistant.helpers.template import Template
        self.temporary = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temporary.name)
        inventory = json.loads((ROOT / "examples/endpoint-ha.example.json").read_text())
        inventory["destinations"] += [{"extension": "555", "label": "Time"},
                                      {"extension": "00555", "label": "Example alternate route"}]
        self.package, _ = ha.render(inventory)
        self.mutations = []
        self.status = {"status": 200, "content": {"configured": True, "ami_fresh": True,
            "available": True, "capabilities": {"call": True, "hangup": True, "dnd": True},
            "calls": [{"channel": "PJSIP/209-00000001", "uniqueid": "1234.10", "state": "Up"}]}}

        async def status(_call):
            return copy.deepcopy(self.status)

        self.hass.services.async_register("rest_command", "endpoint_example_sip_status", status,
                                          supports_response=SupportsResponse.ONLY)
        for operation in ("call", "hangup", "dnd"):
            async def mutate(call, operation=operation):
                payload = self.package["rest_command"]["endpoint_example_sip_" + operation]["payload"]
                body = json.loads(Template(payload, self.hass).async_render(call.data, parse_result=False))
                self.mutations.append((operation, body))
                return {"status": 200, "content": {"status": "accepted", "action_id": body["idempotency_key"]}}
            self.hass.services.async_register("rest_command", "endpoint_example_sip_" + operation,
                                              mutate, supports_response=SupportsResponse.ONLY)
        for name, value in {"input_select.endpoint_example_sip_destination": "600",
                            "input_select.endpoint_example_native_destination": "600",
                            "input_boolean.endpoint_example_sip_enabled": "off",
                            "input_text.endpoint_example_cast_message": "Test announcement",
                            "binary_sensor.example_phone_ready": "on",
                            "sensor.example_phone_call_state": "idle",
                            "media_player.example_cast": "idle", "tts.example_provider": "unknown"}.items():
            self.hass.states.async_set(name, value, {"supported_features": 512 | 4096})

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)
        self.temporary.cleanup()

    async def run_script(self, suffix, variables=None):
        from homeassistant.core import Context
        from homeassistant.components.script.config import async_validate_config_item
        from homeassistant.helpers.script import Script
        name = "endpoint_example_" + suffix
        checked = await async_validate_config_item(self.hass, name, self.package["script"][name])
        return await Script(self.hass, checked["sequence"], name, "script").async_run(variables or {}, context=Context())

    async def test_actual_package_merge_script_rest_and_helper_schemas(self):
        from homeassistant.components import rest, rest_command, input_boolean, input_select, input_text
        from homeassistant.components.script.config import async_validate_config_item
        from homeassistant.config import merge_packages_config

        class InstalledIntegration:
            def __init__(self, domain):
                self.domain = domain

            async def async_get_component(self):
                return importlib.import_module("homeassistant.components." + self.domain)

            async def async_get_platform(self, platform):
                return importlib.import_module("homeassistant.components." + self.domain + "." + platform)

        async def installed(_hass, domain):
            # Use actual installed modules without integration discovery or
            # dependency installation; the production merge function is real.
            return InstalledIntegration(domain)

        errors = []
        merged = {}
        with patch("homeassistant.config.async_get_integration_with_requirements", installed):
            await merge_packages_config(self.hass, merged, {"endpoints": copy.deepcopy(self.package)},
                                        _log_pkg_error=lambda *args: errors.append(args))
        self.assertEqual(errors, [])
        selector = merged["script"]["endpoint_example_sip_dnd"]["fields"]["enabled"]["selector"]
        self.assertEqual(selector, {"boolean": None})
        for name, config in merged["script"].items():
            checked = await async_validate_config_item(self.hass, name, config)
            self.assertIn("sequence", checked, name)
        for component in (rest, rest_command, input_boolean, input_select, input_text):
            component.CONFIG_SCHEMA(merged)

    async def test_sip_dashboard_inputs_retain_number_strings_and_boolean(self):
        await self.run_script("sip_call")
        await self.run_script("sip_dnd")
        self.assertEqual(self.mutations[0][1]["number"], "600")
        self.assertIs(self.mutations[1][1]["enabled"], False)
        self.assertNotEqual(self.mutations[0][1]["idempotency_key"], self.mutations[1][1]["idempotency_key"])

    async def test_explicit_allowlist_supports_time_and_preserves_leading_zeros(self):
        for destination in ("555", "00555"):
            await self.run_script("sip_call", {"destination": destination})
            self.assertEqual(self.mutations[-1][1]["number"], destination)

    async def test_sip_hangup_passes_exact_fresh_pair(self):
        await self.run_script("sip_hangup")
        self.assertEqual(self.mutations[0][1]["channel"], "PJSIP/209-00000001")
        self.assertEqual(self.mutations[0][1]["uniqueid"], "1234.10")

    async def test_missing_stale_unsupported_or_ambiguous_status_never_mutates(self):
        original = copy.deepcopy(self.status)
        for patch, operation in (({"configured": False}, "call"), ({"ami_fresh": False}, "call"),
                ({"available": False}, "call"), ({"capabilities": {}}, "call"), ({"calls": []}, "hangup"),
                ({"calls": original["content"]["calls"] * 2}, "hangup")):
            self.status = copy.deepcopy(original)
            self.status["content"].update(patch)
            with self.subTest(patch=patch):
                await self.run_script("sip_" + operation)
        self.assertEqual(self.mutations, [])

    async def test_native_dial_preserves_string_and_missing_action_fails(self):
        from homeassistant.exceptions import HomeAssistantError
        with self.assertRaises(HomeAssistantError):
            await self.run_script("native_dial")
        calls = []

        async def native(call):
            calls.append(dict(call.data))

        self.hass.services.async_register("script", "example_phone_dial", native)
        await self.run_script("native_dial")
        self.assertEqual(calls, [{"number": "600"}])
        self.hass.states.async_set("binary_sensor.example_phone_ready", "off")
        await self.run_script("native_dial")
        self.assertEqual(len(calls), 1)

    async def test_cast_checks_features_and_accepts_unused_tts_provider(self):
        calls = []

        async def speak(call):
            calls.append(dict(call.data))

        self.hass.services.async_register("tts", "speak", speak)
        await self.run_script("cast_speak")
        self.assertEqual(calls[0]["message"], "Test announcement")
        self.assertEqual(calls[0]["media_player_entity_id"], "media_player.example_cast")
        await self.run_script("cast_speak", {"message": "1234.10"})
        self.assertEqual(calls[-1]["message"], "1234.10")
        self.hass.states.async_set("media_player.example_cast", "idle", {"supported_features": 0})
        await self.run_script("cast_speak")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
