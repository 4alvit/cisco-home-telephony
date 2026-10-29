#!/usr/bin/env python3
"""Render optional HA controls for existing SIP, native and Cast endpoints offline."""

import argparse
import copy
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

ID = r"[a-z][a-z0-9_]{0,39}"
ENTITY = r"[a-z][a-z0-9_]*\.[a-z0-9_]+"
SECRET_MARKER = "__ENDPOINT_GATEWAY_AUTHORIZATION_SECRET__"


def require(value, message):
    if not value:
        raise ValueError(message)


def keys(value, required, optional=()):
    require(isinstance(value, dict) and set(required) <= value.keys()
            and not value.keys() - set(required) - set(optional), "invalid inventory fields")


def literal(value, maximum=80):
    require(isinstance(value, str) and 0 < len(value) <= maximum and value == value.strip()
            and not any(ord(c) < 32 for c in value)
            and not any(c in value for c in ("{{", "{%", "{#", SECRET_MARKER)), "invalid literal text")


def entity(value, domain=None):
    require(isinstance(value, str) and re.fullmatch(ENTITY, value), "invalid entity ID")
    require(domain is None or value.startswith(domain + "."), "incorrect entity domain")


def validate_adapter(value, *, dial=False):
    keys(value, {"action"}, {"data", "target_entity", "destination_field"} if dial
         else {"data", "target_entity"})
    entity(value["action"])
    require(not value["action"].startswith("script.endpoint_"), "recursive endpoint action")
    if "target_entity" in value:
        entity(value["target_entity"])
    data = value.get("data", {})
    require(isinstance(data, dict) and len(data) <= 16, "invalid adapter data")
    for key, item in data.items():
        require(isinstance(key, str) and re.fullmatch(ID, key), "invalid adapter data key")
        require(not re.search(r"password|secret|token|authorization", key),
                "credentials do not belong in an endpoint inventory")
        require(type(item) in (str, int, bool), "adapter data must contain literal scalars")
        if isinstance(item, str):
            literal(item, 256)
    if dial:
        field = value.get("destination_field")
        require(isinstance(field, str) and re.fullmatch(ID, field), "dial needs destination_field")
        require(field not in data, "destination_field duplicates fixed adapter data")


def validate(inventory):
    keys(inventory, {"version", "endpoints"}, {"destinations", "gateway"})
    require(type(inventory["version"]) is int and inventory["version"] == 1, "unsupported inventory version")
    endpoints = inventory["endpoints"]
    require(isinstance(endpoints, list) and 1 <= len(endpoints) <= 50, "expected 1..50 endpoints")
    destinations = inventory.get("destinations", [])
    require(isinstance(destinations, list) and len(destinations) <= 100, "invalid destination list")
    numbers = set()
    for destination in destinations:
        keys(destination, {"extension", "label"})
        number = destination["extension"]
        require(isinstance(number, str) and re.fullmatch(r"[0-9]{3,8}", number),
                "destination must be an explicitly allowed number of 3..8 digits")
        require(number not in numbers, "duplicate destination")
        numbers.add(number)
        literal(destination["label"])
    if "gateway" in inventory:
        gateway = inventory["gateway"]
        keys(gateway, {"base_url", "authorization_secret"})
        literal(gateway["base_url"], 256)
        url = urlsplit(gateway["base_url"])
        require(url.scheme in ("http", "https") and url.hostname and not url.username
                and not url.password and not url.query and not url.fragment
                and url.path in ("", "/") and (url.port is None or 1 <= url.port <= 65535), "invalid gateway URL")
        require(isinstance(gateway["authorization_secret"], str)
                and re.fullmatch(ID, gateway["authorization_secret"]), "invalid secret name")
    seen, extensions = set(), set()
    for endpoint in endpoints:
        common = {"id", "label", "kind"}
        require(isinstance(endpoint, dict), "invalid endpoint")
        kind = endpoint.get("kind")
        if kind == "sip":
            keys(endpoint, common | {"extension"})
            extension = endpoint["extension"]
            require(isinstance(extension, str) and re.fullmatch(r"2[0-9]{2}", extension), "SIP extension must be in 200..299")
            require(extension not in extensions, "duplicate SIP endpoint")
            extensions.add(extension)
            require("gateway" in inventory, "SIP endpoint needs gateway configuration")
            require(numbers - {extension}, "SIP endpoint needs a destination other than itself")
        elif kind == "native":
            keys(endpoint, common | {"state_entity", "ready_entity", "actions"})
            entity(endpoint["state_entity"])
            entity(endpoint["ready_entity"], "binary_sensor")
            actions = endpoint["actions"]
            require(isinstance(actions, dict) and actions and
                    not actions.keys() - {"dial", "answer", "hangup"}, "invalid native capabilities")
            for operation, value in actions.items():
                validate_adapter(value, dial=operation == "dial")
            require("dial" not in actions or numbers, "native dial needs destinations")
        elif kind == "cast":
            keys(endpoint, common | {"media_player_entity", "tts_entity"})
            entity(endpoint["media_player_entity"], "media_player")
            entity(endpoint["tts_entity"], "tts")
        else:
            raise ValueError("endpoint kind must be sip, native or cast")
        endpoint_id = endpoint.get("id")
        require(isinstance(endpoint_id, str) and re.fullmatch(ID, endpoint_id), "invalid endpoint ID")
        require(endpoint_id not in seen, "duplicate endpoint ID")
        seen.add(endpoint_id)
        literal(endpoint["label"])
    return copy.deepcopy(inventory)


def template(expression):
    return "{{ " + expression + " }}"


def guard(expression, reason):
    """Fail visibly; a condition action would silently stop a script."""
    return {"if": [{"condition": "template", "value_template": template("not (" + expression + ")")}],
            "then": [{"stop": reason, "error": True}]}


def available(entity_id):
    return f"(expand('{entity_id}') | count) == 1 and not is_state('{entity_id}', 'unavailable')"


def adapter_action(value, *, dial=False):
    result = {"action": value["action"], "data": dict(value.get("data", {}))}
    if dial:
        result["data"][value["destination_field"]] = "{{ destination }}"
    if "target_entity" in value:
        result["target"] = {"entity_id": value["target_entity"]}
    return result


def dial_fields(allowed):
    return {"destination": {"name": "Destination", "required": True,
            "selector": {"select": {"options": [{"value": d["extension"],
            "label": d["label"] + " (" + d["extension"] + ")"} for d in allowed]}}}}


def render_sip(inventory, endpoint, package, add):
    prefix = "endpoint_" + endpoint["id"]
    url = inventory["gateway"]["base_url"].rstrip("/") + "/v1/endpoints/" + endpoint["extension"]
    headers = {"Authorization": SECRET_MARKER}
    commands = package.setdefault("rest_command", {})
    commands[prefix + "_status"] = {"url": url, "method": "GET", "timeout": 15, "headers": headers}
    for operation, payload in {
            "call": "{{ {'number': inputs.destination, 'idempotency_key': request_id} | to_json }}",
            "hangup": "{{ {'channel': call.channel, 'uniqueid': call.uniqueid, 'idempotency_key': request_id} | to_json }}",
            "dnd": "{{ {'enabled': enabled, 'idempotency_key': request_id} | to_json }}"}.items():
        commands[prefix + "_" + operation] = {"url": url + "/" + operation, "method": "POST",
            "timeout": 15, "headers": headers, "content_type": "application/json", "payload": payload}
    sensor_name = prefix + "_status"
    package.setdefault("rest", []).append({"resource": url, "method": "GET", "timeout": 15,
        "headers": headers, "scan_interval": 30, "sensor": [{"name": sensor_name,
            "unique_id": sensor_name, "value_template": "{{ value_json.call_state | default('unknown') }}",
            "json_attributes": ["configured", "available", "ami_fresh", "observed_at", "callback_state",
                                "dnd", "dnd_pending", "calls", "capabilities"]}]})
    rows = [{"entity": "sensor." + sensor_name, "name": "Call state (polls every 30 seconds)"}]
    fresh = [{"action": "rest_command." + prefix + "_status", "response_variable": "endpoint_status"},
        guard("endpoint_status.status == 200 and endpoint_status.content is mapping "
              "and endpoint_status.content.get('configured') == true "
              "and endpoint_status.content.get('ami_fresh') == true", "Fresh endpoint control is unavailable or disabled")]
    content = "endpoint_status.content"
    for operation, label in (("call", "Request call"), ("hangup", "Hang up"), ("dnd", "Set do not disturb")):
        sequence = copy.deepcopy(fresh)
        sequence.append(guard(f"{content}.get('capabilities', {{}}).get('{operation}') == true",
                              "Endpoint does not support this action"))
        data = {"request_id": "{{ request_id }}"}
        fields, description = None, None
        if operation == "call":
            allowed = [d for d in inventory.get("destinations", []) if d["extension"] != endpoint["extension"]]
            sequence += [guard(f"{content}.get('available') == true", "SIP endpoint is not available"),
                guard("destination is defined and destination is string and destination in " +
                      repr([d["extension"] for d in allowed]), "Choose a configured destination")]
            fields = dial_fields(allowed)
            data["inputs"] = "{{ endpoint_inputs }}"
            description = "Rings this endpoint first; after answer Asterisk dials the destination. Submission is not proof of answer."
        elif operation == "hangup":
            sequence.append(guard(f"{content}.get('calls') is sequence and ({content}.calls | count) == 1",
                                  "Expected exactly one current call; select the call with the endpoint client"))
            data["call"] = "{{ endpoint_status.content.calls[0] }}"
        else:
            # Package merge discards empty mappings; null is the supported
            # default selector config and survives the recursive merge.
            fields = {"enabled": {"name": "Do not disturb", "required": True, "selector": {"boolean": None}}}
            sequence.append(guard("enabled is defined and enabled is boolean", "Choose a boolean do-not-disturb state"))
            data["enabled"] = "{{ enabled }}"
        sequence += [{"variables": {"request_id": "{{ 'ha-' ~ now().strftime('%Y%m%d%H%M%S%f') ~ '-' ~ (range(100000) | random) }}"}},
            {"action": "rest_command." + prefix + "_" + operation, "data": data, "response_variable": "endpoint_result"},
            guard("200 <= endpoint_result.status < 300 and endpoint_result.content is mapping "
                  "and endpoint_result.content.get('status') == 'accepted' "
                  "and endpoint_result.content.get('action_id') == request_id",
                  "Gateway did not confirm the action; refresh endpoint state before retrying")]
        rows.append(add(endpoint, operation, label, sequence, fields, description))
    return rows


def render(inventory):
    inventory = validate(inventory)
    package, cards, helper_rows = {"script": {}}, [], {}
    destinations = inventory.get("destinations", [])

    def add(endpoint, operation, label, sequence, fields=None, description=None):
        key = "endpoint_" + endpoint["id"] + "_" + operation
        if fields:
            defaults = []
            for field, schema in fields.items():
                helper = "endpoint_" + endpoint["id"] + "_" + field
                if field == "destination":
                    domain, settings = "input_select", {"options": [v["value"] for v in schema["selector"]["select"]["options"]]}
                    fallback = f"states('{domain}.{helper}')"
                elif field == "message":
                    domain, settings = "input_text", {"max": 255}
                    fallback = f"states('{domain}.{helper}') if has_value('{domain}.{helper}') else ''"
                else:
                    domain, settings = "input_boolean", {}
                    fallback = f"is_state('{domain}.{helper}', 'on') if has_value('{domain}.{helper}') else none"
                package.setdefault(domain, {})[helper] = {"name": endpoint["label"] + ": " + schema["name"], **settings}
                helper_rows.setdefault(endpoint["id"], []).append({"entity": domain + "." + helper, "name": schema["name"]})
                defaults.append(f"'{field}': ({field} if {field} is defined else ({fallback}))")

            def scoped(value):
                if isinstance(value, dict):
                    return {k: scoped(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [scoped(v) for v in value]
                if isinstance(value, str) and "{{" in value:
                    for field in fields:
                        value = re.sub(r"(?<![.\w'\"])" + field + r"\b(?!['\"])",
                                       "endpoint_inputs." + field, value)
                return value

            # One structured template preserves numeric-looking strings inside
            # the mapping; standalone HA templates would coerce "600" to int.
            sequence = [{"variables": {"endpoint_inputs": template("{" + ", ".join(defaults) + "}")}}] + scoped(sequence)
        spec = {"alias": endpoint["label"] + ": " + label, "mode": "single", "sequence": sequence}
        if description:
            spec["description"] = description
        if fields:
            spec["fields"] = fields
        package["script"][key] = spec
        return {"entity": "script." + key, "name": label}

    for endpoint in inventory["endpoints"]:
        kind, rows = endpoint["kind"], []
        if kind == "sip":
            rows = render_sip(inventory, endpoint, package, add)
        elif kind == "native":
            ready = endpoint["ready_entity"]
            rows += [{"entity": ready, "name": "Endpoint ready"}, {"entity": endpoint["state_entity"], "name": "Call state"}]
            for operation, value in endpoint["actions"].items():
                sequence = [guard(f"is_state('{ready}', 'on')", "Native endpoint is not ready")]
                if "target_entity" in value:
                    sequence.append(guard(available(value["target_entity"]), "Native endpoint target is unavailable"))
                fields = None
                if operation == "dial":
                    sequence.append(guard("destination is defined and destination is string and destination in " +
                                          repr([d["extension"] for d in destinations]), "Choose a configured destination"))
                    fields = dial_fields(destinations)
                if operation == "dial":
                    choices = []
                    for destination in destinations:
                        action = adapter_action(value)
                        action["data"][value["destination_field"]] = destination["extension"]
                        choices.append({"conditions": [{"condition": "template", "value_template":
                            template("destination == " + repr(destination["extension"]))}], "sequence": [action]})
                    sequence.append({"choose": choices})
                else:
                    sequence.append(adapter_action(value))
                rows.append(add(endpoint, operation, {"dial": "Dial", "answer": "Answer", "hangup": "Hang up"}[operation],
                                sequence, fields, "Uses the explicitly configured native endpoint action."))
        else:
            player, tts = endpoint["media_player_entity"], endpoint["tts_entity"]
            feature = lambda mask: f"((state_attr('{player}', 'supported_features') | int(0)) | bitwise_and({mask})) == {mask}"
            rows += [{"entity": player, "name": "Cast player"}, {"entity": tts, "name": "Speech provider"}]
            sequence = [guard(available(player) + f" and states('{player}') != 'unknown'", "Cast player is unavailable"),
                guard(feature(512), "Cast player cannot play media"), guard(available(tts), "Speech provider is unavailable"),
                guard("message is defined and message is string and 0 < (message | trim | length) <= 500",
                      "Enter an announcement of 1..500 characters"),
                {"action": "tts.speak", "target": {"entity_id": tts}, "data": template(
                    "{'media_player_entity_id': '" + player + "', 'message': message | trim, 'cache': false}")}]
            rows.append(add(endpoint, "speak", "Speak announcement", sequence,
                {"message": {"name": "Announcement", "required": True, "selector": {"text": {"multiline": True}}}},
                "Plays speech on this Cast player. It may interrupt media; automatic resume is not promised."))
            rows.append(add(endpoint, "stop", "Stop media", [guard(available(player), "Cast player is unavailable"), guard(feature(4096), "Cast player cannot stop media"),
                {"action": "media_player.media_stop", "target": {"entity_id": player}}],
                description="Stops whatever media is currently playing on this Cast endpoint."))
        cards.append({"type": "entities", "title": endpoint["label"], "show_header_toggle": False,
                      "entities": helper_rows.get(endpoint["id"], []) + rows})
    return package, {"title": "Existing endpoints", "views": [{"title": "Endpoints", "path": "endpoints", "cards": cards}]}


def serialize(value, inventory):
    """JSON is YAML; use a YAML tag only for the secret reference, never a token."""
    result = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    if SECRET_MARKER in result:
        result = result.replace(json.dumps(SECRET_MARKER), "!secret " + inventory["gateway"]["authorization_secret"])
    return result


def write_outputs(inventory, output):
    package, dashboard = render(inventory)
    output = Path(output)
    require(not output.is_symlink(), "output directory must not be a symlink")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    names = ("endpoint-package.yaml", "endpoint-dashboard.yaml")
    for name in names:
        require(not (output / name).is_symlink(), "output files must not be symlinks")
    for name, value in zip(names, (package, dashboard)):
        descriptor, temporary = tempfile.mkstemp(prefix=".endpoint-", dir=output)
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(serialize(value, inventory))
            os.replace(temporary, output / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return names


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "duplicate JSON field")
        value[key] = item
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        require(args.inventory.stat().st_size <= 256 * 1024, "inventory exceeds size limit")
        inventory = json.loads(args.inventory.read_text(), object_pairs_hook=unique_object)
        write_outputs(inventory, args.out)
    except (OSError, ValueError):
        parser.exit(2, "Endpoint HA generation failed: invalid inventory or output; no live action was sent.\n")
    print("Rendered endpoint package and dashboard; nothing was installed or called.")


if __name__ == "__main__":
    main()
