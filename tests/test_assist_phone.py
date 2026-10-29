"""Offline rendering and optional HA/Hassil checks; native actions are fakes."""
import copy
import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("assist_phone", ROOT / "scripts/render_assist_phone.py")
phone = importlib.util.module_from_spec(spec)
spec.loader.exec_module(phone)


def inventory():
    return json.loads((ROOT / "examples/assist-phone.example.json").read_text())


class AssistPhoneTests(unittest.TestCase):
    def test_rejects_unbounded_destinations_templates_and_ambiguous_names_before_writing(self):
        edits = [lambda c: c["destinations"][0].update(number="+18005551212"),
                 lambda c: c["destinations"][0]["names"].update(en=["{anything}"]),
                 lambda c: c["destinations"][1]["names"].update(en=["TIME SERVICE"]),
                 lambda c: c["target"]["actions"]["call"].update(action="{{ service }}"),
                 lambda c: c["target"]["actions"]["call"].update(token="not permitted"),
                 lambda c: c["target"]["actions"]["answer"].update(target_entity=["sensor.a", "sensor.b"]),
                 lambda c: c["target"].update(state_entity=c["target"]["peer_entity"]),
                 lambda c: c["namespaces"].update(ru="[телефон]"),
                 lambda c: c.update(version=True)]
        for edit in edits:
            config = inventory()
            edit(config)
            with self.subTest(edit=edit), tempfile.TemporaryDirectory() as temp:
                output = Path(temp) / "new"
                with self.assertRaises(ValueError):
                    phone.write_outputs(config, output)
                self.assertFalse(output.exists())

    def test_generic_button_requires_exactly_one_target(self):
        config = inventory()
        config["target"]["actions"]["answer"] = {"action": "button.press"}
        with self.assertRaises(ValueError):
            phone.render(config)
        config["target"]["actions"]["answer"]["target_entity"] = "button.example_answer"
        package, _ = phone.render(config)
        action = package["intent_script"]["LocalPhone_example_phone_en_answer"]["action"][1]["then"][0]
        self.assertEqual(action, {"action": "button.press", "target": {"entity_id": "button.example_answer"}})

    def test_output_is_offline_private_repeatable_and_keeps_input_unchanged(self):
        config = inventory()
        before = copy.deepcopy(config)
        with tempfile.TemporaryDirectory() as output:
            names = phone.write_outputs(config, output)
            self.assertEqual(set(names), {"packages/assist_phone_example_phone.yaml",
                "custom_sentences/en/assist_phone_example_phone.yaml",
                "custom_sentences/ru/assist_phone_example_phone.yaml"})
            first = [(Path(output) / name).read_text() for name in names]
            phone.write_outputs(config, output)
            self.assertEqual(first, [(Path(output) / name).read_text() for name in names])
            for name in names:
                path = Path(output) / name
                json.loads(path.read_text())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(config, before)


RUNTIME_READY = all(importlib.util.find_spec(name) is not None
                    for name in ("homeassistant", "xmltodict", "hassil"))


@unittest.skipUnless(RUNTIME_READY, "requires optional HA 2026.10.4, xmltodict and Hassil 3.12.1")
class AssistPhoneRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from homeassistant.core import HomeAssistant
        from homeassistant.components import intent_script
        from homeassistant.config import merge_packages_config
        from hassil import Intents
        self.temporary = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temporary.name)
        self.config = inventory()
        self.config["destinations"].append({"number": "00555", "names": {
            "en": ["alternate time service"], "ru": ["другой службе времени"]}})
        self.package, sentences = phone.render(self.config)
        self.sentences = {language: Intents.from_dict(value) for language, value in sentences.items()}
        self.calls = []

        class InstalledIntegration:
            def __init__(self, domain):
                self.domain = domain

            async def async_get_component(self):
                return importlib.import_module("homeassistant.components." + self.domain)

            async def async_get_platform(self, platform):
                return importlib.import_module("homeassistant.components." + self.domain + "." + platform)

        async def installed(_hass, domain):
            return InstalledIntegration(domain)

        errors, merged = [], {}
        with patch("homeassistant.config.async_get_integration_with_requirements", installed):
            await merge_packages_config(self.hass, merged, {"phone": copy.deepcopy(self.package)},
                                        _log_pkg_error=lambda *args: errors.append(args))
        self.assertEqual(errors, [])
        checked = intent_script.CONFIG_SCHEMA(merged)
        await intent_script.async_load_intents(self.hass, checked["intent_script"])

        async def native(call):
            self.calls.append((call.service, dict(call.data)))

        for operation in ("call", "answer", "hangup"):
            self.hass.services.async_register("esphome", "example_dot_phone_" + operation, native)
        self.hass.states.async_set("sensor.example_dot_phone", "Ready")
        self.hass.states.async_set("sensor.example_dot_phone_call", "")

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)
        self.temporary.cleanup()

    async def recognize_and_run(self, text, language="en", slots=None):
        from hassil import recognize
        from homeassistant.helpers import intent
        match = recognize(text, self.sentences[language])
        self.assertIsNotNone(match, text)
        response = await intent.async_handle(self.hass, "conversation", match.intent.name,
            slots=slots, text_input=text, language=language, device_id="unrelated_source_device")
        return response.speech["plain"]["speech"]

    async def test_languages_recognize_only_namespaced_allowlisted_literal_phrases(self):
        from hassil import recognize
        for language, texts in {
            "en": ["call time service", "example phone call 999", "other phone answer", "example phone call anybody"],
            "ru": ["позвони службе времени", "телефон пример позвони 999", "другой телефон ответь на звонок"],
        }.items():
            for text in texts:
                self.assertIsNone(recognize(text, self.sentences[language]), text)
        for language, text in [("en", "EXAMPLE PHONE call time service!"),
                               ("ru", "Телефон пример позвони службе времени.")]:
            self.assertEqual(await self.recognize_and_run(text, language), phone.WORDS[language]["sent"])
            self.assertEqual(self.calls[-1], ("example_dot_phone_call", {"number": "555"}))

    async def test_fixed_target_and_literal_number_ignore_supplied_number_slot(self):
        await self.recognize_and_run("example phone call alternate time service",
                                    slots={"number": {"value": "999"}, "entity_id": {"value": "all"}})
        self.assertEqual(self.calls, [("example_dot_phone_call", {"number": "00555"})])

    async def test_state_guards_refuse_without_native_action_and_return_honest_speech(self):
        for state in ("unknown", "unavailable", "Not set up", "Connecting", "Login refused",
                      "Cannot sign in", "Idle", "idle"):
            self.hass.states.async_set("sensor.example_dot_phone", state)
            for text in ("example phone call time service", "example phone answer", "example phone hang up"):
                with self.subTest(state=state, text=text):
                    self.assertEqual(await self.recognize_and_run(text), phone.WORDS["en"]["blocked"])
        self.hass.states.async_set("sensor.example_dot_phone", "Ready")
        self.hass.states.async_set("sensor.example_dot_phone_call", "existing peer")
        self.assertEqual(await self.recognize_and_run("телефон пример позвони службе времени", "ru"),
                         phone.WORDS["ru"]["blocked"])
        self.assertEqual(self.calls, [])

    async def test_only_ringing_answers_and_active_states_can_hang_up(self):
        for state in ("Ready", "Calling", "In call"):
            self.hass.states.async_set("sensor.example_dot_phone", state)
            self.assertEqual(await self.recognize_and_run("example phone answer"), phone.WORDS["en"]["blocked"])
        self.hass.states.async_set("sensor.example_dot_phone", "Ringing")
        self.assertEqual(await self.recognize_and_run("телефон пример ответь на звонок", "ru"), phone.WORDS["ru"]["sent"])
        self.assertEqual(self.calls[-1][0], "example_dot_phone_answer")
        for state in ("Ringing", "Calling", "In call"):
            self.hass.states.async_set("sensor.example_dot_phone", state)
            await self.recognize_and_run("example phone hang up")
        self.assertEqual([call[0] for call in self.calls], ["example_dot_phone_answer"] + ["example_dot_phone_hangup"] * 3)

    async def test_absent_or_failed_native_action_never_returns_sent_reply(self):
        from homeassistant.helpers.intent import IntentUnexpectedError
        from homeassistant.exceptions import HomeAssistantError
        self.hass.services.async_remove("esphome", "example_dot_phone_call")
        with self.assertLogs(level="ERROR"), self.assertRaises(IntentUnexpectedError):
            await self.recognize_and_run("example phone call time service")

        async def fail(_call):
            raise HomeAssistantError("Native action unavailable")

        self.hass.services.async_register("esphome", "example_dot_phone_call", fail)
        with self.assertLogs(level="ERROR"), self.assertRaises(IntentUnexpectedError):
            await self.recognize_and_run("example phone call time service")
        self.assertEqual(self.calls, [])

    async def test_button_adapter_refuses_missing_unavailable_and_restored_targets(self):
        from homeassistant.components import intent_script
        from homeassistant.helpers import intent
        config = inventory()
        config["target"]["actions"]["answer"] = {"action": "button.press", "target_entity": "button.example_answer"}
        package, _ = phone.render(config)
        checked = intent_script.CONFIG_SCHEMA(package)
        for name in checked["intent_script"]:
            intent.async_remove(self.hass, name)
        await intent_script.async_load_intents(self.hass, checked["intent_script"])
        self.hass.states.async_set("sensor.example_dot_phone", "Ringing")
        pressed = []

        async def press(call):
            pressed.append(dict(call.data))

        self.hass.services.async_register("button", "press", press)
        self.assertEqual(await self.recognize_and_run("example phone answer"), phone.WORDS["en"]["blocked"])
        self.hass.states.async_set("button.example_answer", "unavailable")
        self.assertEqual(await self.recognize_and_run("example phone answer"), phone.WORDS["en"]["blocked"])
        self.hass.states.async_set("button.example_answer", "unknown", {"restored": True})
        self.assertEqual(await self.recognize_and_run("example phone answer"), phone.WORDS["en"]["blocked"])
        self.assertEqual(pressed, [])
        # An available button that has never been pressed legitimately has unknown state.
        self.hass.states.async_set("button.example_answer", "unknown")
        self.assertEqual(await self.recognize_and_run("example phone answer"), phone.WORDS["en"]["sent"])
        self.assertEqual(pressed, [{"entity_id": ["button.example_answer"]}])


if __name__ == "__main__":
    unittest.main()
