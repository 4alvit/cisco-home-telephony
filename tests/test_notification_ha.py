"""Notification HA template regressions; no network or device actions.

Jinja/PyYAML checks run when installed; the optional actual-HA check also
verifies native template coercion against the Home Assistant runtime.
"""
import ast
import datetime
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest


PACKAGE_FILE = Path(__file__).parents[1] / "examples/cisco_notifications.yaml"
DOC_FILE = Path(__file__).parents[1] / "docs/automatic-notifications.md"
TEMPLATES_READY = all(importlib.util.find_spec(name) is not None
                      for name in ("yaml", "jinja2"))
HA_READY = TEMPLATES_READY and importlib.util.find_spec("homeassistant") is not None


def load_package():
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor("!secret", lambda loader, node: loader.construct_scalar(node))
    return yaml.load(PACKAGE_FILE.read_text(), Loader=Loader)


def event_trigger(data):
    return SimpleNamespace(event=SimpleNamespace(
        data=data,
        context=SimpleNamespace(id="boundary-context-001"),
        time_fired=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)))


@unittest.skipUnless(TEMPLATES_READY, "requires optional Jinja2 and PyYAML")
class NotificationHAPackageTests(unittest.TestCase):
    def setUp(self):
        from jinja2 import Environment, StrictUndefined
        from jinja2.nativetypes import NativeEnvironment
        self.package = load_package()
        self.automation = self.package["automation"][0]
        self.payload = self.package["rest_command"]["cisco_forward_notification"]["payload"]
        self.native = NativeEnvironment(undefined=StrictUndefined)
        self.native.globals["as_timestamp"] = lambda value: value.timestamp()
        self.text = Environment(undefined=StrictUndefined)
        self.text.filters["to_json"] = lambda value: json.dumps(value, ensure_ascii=False)

    def forward(self, event):
        template = self.automation["actions"][0]["data"]["notification"]
        return self.native_result(template, trigger=event_trigger(event))

    def native_result(self, template, **variables):
        result = self.native.from_string(template).render(**variables)
        # HA strips template whitespace before its native literal parsing.
        # Plain Jinja NativeEnvironment does not always do that for blocks.
        # The separate actual-HA tests verify the production implementation.
        if isinstance(result, str):
            try:
                return ast.literal_eval(result.strip())
            except (ValueError, SyntaxError):
                pass
        return result

    def body(self, notification, **variables):
        return json.loads(self.text.from_string(self.payload).render(
            notification=notification, **variables))

    def test_only_explicit_event_and_no_private_trace_or_retry(self):
        self.assertEqual(self.automation["triggers"], [{
            "trigger": "event", "event_type": "cisco_notification_requested"}])
        self.assertEqual(self.automation["trace"], {"stored_traces": 0})
        self.assertEqual(self.automation["mode"], "parallel")
        self.assertEqual(self.automation["max"], 4)
        self.assertEqual(len(self.automation["actions"]), 2)
        self.assertIs(self.automation["actions"][0]["continue_on_error"], True)
        self.assertNotIn("shell_command", self.package)
        self.assertEqual(set(self.automation["actions"][0]["data"]), {"notification"})

    def test_failures_log_only_fixed_metadata_and_never_retry(self):
        warning = self.automation["actions"][1]
        condition = warning["if"][0]["value_template"]
        self.assertIs(self.native_result(condition), True)
        for result in (None, {"status": 403, "content": {}},
                       {"status": 200, "content": "bad response"},
                       {"status": 200, "content": {"status": "partial"}},
                       {"status": 200, "content": {"status": "refused"}}):
            self.assertIs(self.native_result(condition, forwarding_result=result), True)
        self.assertIs(self.native_result(condition, forwarding_result={
            "status": 200, "content": {"status": "ok", "reason": "disabled"}}), False)
        self.assertEqual(len(warning["then"]), 1)
        log = warning["then"][0]
        self.assertEqual(log["action"], "system_log.write")
        self.assertNotIn("{{", log["data"]["message"])
        self.assertNotIn("{%", log["data"]["message"])

    def test_dry_run_true_is_transmitted_and_omitted_defaults_false(self):
        event = self.forward({"source": "general", "title": "Review", "message": "Preview"})
        self.assertIs(self.body(event, dry_run=True)["dry_run"], True)
        self.assertIs(self.body(event)["dry_run"], False)
        self.assertIs(self.body(event, dry_run=False)["dry_run"], False)

    def test_json_roundtrip_preserves_unicode_escapes_and_numeric_text(self):
        for message in ('Привет\n"Dinner" \\ ready', "123", "true", "null", "[1, 2]"):
            with self.subTest(message=message):
                event = {"source": "general", "event_id": "source-event-001",
                         "event_at": 1767225500.25, "title": "123", "message": message}
                body = self.body(self.forward(event))
                self.assertEqual(body["message"], message)
                self.assertIsInstance(body["message"], str)
                self.assertEqual(body["title"], "123")
                self.assertIsInstance(body["event_at"], float)
                self.assertEqual(body["event_at"], event["event_at"])
                self.assertEqual(body["event_id"], event["event_id"])
                self.assertEqual(body, self.body(self.forward({"notification": event})))

    def test_fallback_identity_is_boundary_event_and_explicit_null_is_not_hidden(self):
        event = {"source": "general", "title": "Review", "message": "Preview"}
        body = self.body(self.forward(event))
        self.assertEqual(body["event_id"], "boundary-context-001:1767225600.0")
        self.assertEqual(body["event_at"], 1767225600.0)
        invalid = self.body(self.forward(dict(event, event_id=None, event_at=None)))
        self.assertIsNone(invalid["event_id"])
        self.assertIsNone(invalid["event_at"])

    def test_different_events_sharing_a_context_do_not_share_fallback_identity(self):
        event = {"source": "general", "title": "Review", "message": "Preview"}
        first = event_trigger(event)
        second = event_trigger(event)
        second.event.time_fired += datetime.timedelta(seconds=1)
        template = self.automation["actions"][0]["data"]["notification"]
        one = self.native_result(template, trigger=first)
        two = self.native_result(template, trigger=second)
        self.assertNotEqual(one["event_id"], two["event_id"])
        self.assertEqual(one, self.native_result(template, trigger=first))


@unittest.skipUnless(HA_READY, "requires optional Home Assistant runtime")
class NotificationHAActualRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_documented_producer_validates_and_preserves_text_without_external_delivery(self):
        import yaml
        from homeassistant.core import Context, HomeAssistant
        from homeassistant.components.script.config import async_validate_config_item
        from homeassistant.helpers.script import Script
        text = DOC_FILE.read_text().split("Append this block after the original notification deliveries:", 1)[1]
        block = text.split("```yaml\n", 1)[1].split("```", 1)[0]
        sequence = yaml.safe_load(block)
        with tempfile.TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            checked = await async_validate_config_item(hass, "notification_example", {"sequence": sequence})
            script = Script(hass, checked["sequence"], "Notification example", "script")
            events = []
            remove = hass.bus.async_listen("cisco_notification_requested", lambda event: events.append(event.data))
            variables = {"also_devices": False, "notify_data": {"title": "123", "message": "123"}}
            await script.async_run(variables, context=Context())
            await hass.async_block_till_done()
            self.assertEqual(events, [])
            for message in ("123", 'Привет\n"Dinner" \\ ready'):
                variables = {"also_devices": True, "notify_data": {"title": "123", "message": message}}
                await script.async_run(variables, context=Context())
                await hass.async_block_till_done()
                self.assertEqual(events[-1], {"notification": {
                    "source": "general", "title": "123", "message": message}})
            variables.update(device_event_id="original-event-001", device_event_at=1767225500.25)
            await script.async_run(variables, context=Context())
            await hass.async_block_till_done()
            self.assertEqual(events[-1]["notification"]["event_id"], "original-event-001")
            self.assertEqual(events[-1]["notification"]["event_at"], 1767225500.25)
            remove()
            await hass.async_stop(force=True)

    async def test_actual_ha_native_template_keeps_message_strings_and_dry_run(self):
        from homeassistant.core import HomeAssistant
        from homeassistant.helpers.template import Template
        package = load_package()
        with tempfile.TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            forward = package["automation"][0]["actions"][0]["data"]["notification"]
            payload = package["rest_command"]["cisco_forward_notification"]["payload"]
            for message in ("123", "true", 'Привет\n"Dinner" \\ ready'):
                event = {"source": "general", "event_id": "source-event-001",
                         "event_at": 1767225500.25, "title": "123", "message": message}
                notification = Template(forward, hass).async_render({"trigger": event_trigger(event)})
                for dry_run in (False, True):
                    body = json.loads(Template(payload, hass).async_render(
                        {"notification": notification, "dry_run": dry_run}, parse_result=False))
                    self.assertEqual(body["message"], message)
                    self.assertEqual(body["title"], "123")
                    self.assertIsInstance(body["event_at"], float)
                    self.assertIs(body["dry_run"], dry_run)
            await hass.async_stop(force=True)


if __name__ == "__main__":
    unittest.main()
