#!/usr/bin/env python3
"""Render opt-in English/Russian Assist commands for one native TECHO5 phone."""

import argparse
import json
import os
from pathlib import Path
import re
import tempfile

ID = r"[a-z][a-z0-9_]{0,39}"
ENTITY = r"[a-z][a-z0-9_]*\.[a-z0-9_]+"
# TECHO5 v1.0.40 Phone sensor values, not generic SIP registration states.
STATES = {"call": ["Ready"], "answer": ["Ringing"],
          "hangup": ["Ringing", "Calling", "In call"]}
WORDS = {
    "en": {"call": "call", "answer": "answer", "hangup": "hang up",
           "sent": "Phone command sent.",
           "blocked": "The phone is not in the required state. No command was sent."},
    "ru": {"call": "позвони", "answer": "ответь на звонок", "hangup": "заверши звонок",
           "sent": "Команда телефону отправлена.",
           "blocked": "Телефон не в нужном состоянии. Команда не отправлена."},
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def keys(value, required, optional=()):
    require(isinstance(value, dict) and set(required) <= value.keys()
            and not value.keys() - set(required) - set(optional), "invalid inventory fields")


def phrase(value):
    # Literal words only: reject Hassil grammar, templates and wildcards.
    require(isinstance(value, str) and 1 <= len(value) <= 80
            and value == " ".join(value.split())
            and all(c.isalnum() or c == " " for c in value), "expected literal phrase words")
    return value.casefold()


def validate(config):
    keys(config, {"version", "id", "namespaces", "target", "destinations"})
    require(type(config["version"]) is int and config["version"] == 1, "unsupported version")
    require(isinstance(config["id"], str) and re.fullmatch(ID, config["id"]), "invalid ID")
    keys(config["namespaces"], {"en", "ru"})
    for value in config["namespaces"].values():
        phrase(value)
    target = config["target"]
    keys(target, {"state_entity", "peer_entity", "actions"})
    for field in ("state_entity", "peer_entity"):
        require(isinstance(target[field], str) and re.fullmatch(r"sensor\.[a-z0-9_]+", target[field]),
                "native Phone and Phone call sensor IDs are required")
    require(target["state_entity"] != target["peer_entity"], "state and peer sensors must differ")
    keys(target["actions"], {"call", "answer", "hangup"})
    for operation, adapter in target["actions"].items():
        keys(adapter, {"action", "destination_field"} if operation == "call" else {"action"},
             {"target_entity"})
        action = adapter["action"]
        require(isinstance(action, str) and re.fullmatch(ENTITY, action), "invalid action")
        domain, service = action.split(".")
        require(domain in {"esphome", "script", "button"}, "expected native action or named adapter script")
        require(not (domain == "script" and service in {"turn_on", "turn_off", "toggle", "reload"}),
                "use a named adapter script")
        if domain == "button":
            require(operation != "call" and service == "press" and "target_entity" in adapter,
                    "button action needs one explicit target")
            require(isinstance(adapter["target_entity"], str)
                    and re.fullmatch(r"button\.[a-z0-9_]+", adapter["target_entity"]), "expected button entity")
        if "target_entity" in adapter:
            require(isinstance(adapter["target_entity"], str) and re.fullmatch(ENTITY, adapter["target_entity"]),
                    "invalid single target entity")
        if operation == "call":
            require(isinstance(adapter["destination_field"], str)
                    and re.fullmatch(ID, adapter["destination_field"]), "invalid destination field")
    destinations = config["destinations"]
    require(isinstance(destinations, list) and 1 <= len(destinations) <= 30, "expected 1..30 destinations")
    numbers, spoken = set(), {"en": set(), "ru": set()}
    for destination in destinations:
        keys(destination, {"number", "names"})
        number = destination["number"]
        require(isinstance(number, str) and re.fullmatch(r"[0-9]{3,8}", number)
                and number not in numbers, "expected distinct allowed numbers of 3..8 digits")
        numbers.add(number)
        keys(destination["names"], {"en", "ru"})
        for language, names in destination["names"].items():
            require(isinstance(names, list) and 1 <= len(names) <= 5, "expected 1..5 destination names")
            for name in names:
                name = phrase(name)
                require(name not in spoken[language], "duplicate spoken destination")
                spoken[language].add(name)


def render(config):
    """Return a package and two sentence dictionaries without contacting HA."""
    validate(config)
    package = {"intent_script": {}}
    sentences = {language: {"language": language, "intents": {}} for language in WORDS}
    target = config["target"]
    for language, words in WORDS.items():
        namespace = phrase(config["namespaces"][language])
        entries = [("call", item) for item in config["destinations"]] + [("answer", None), ("hangup", None)]
        for operation, destination in entries:
            name = "LocalPhone_" + config["id"] + "_" + language + "_" + operation
            if destination:
                name += "_" + destination["number"]
            base = namespace + " " + words[operation]
            texts = [base + " " + phrase(n) for n in destination["names"][language]] if destination else [base]
            sentences[language]["intents"][name] = {"data": [{"sentences": texts}]}
            adapter = target["actions"][operation]
            action = {"action": adapter["action"]}
            if "target_entity" in adapter:
                action["target"] = {"entity_id": adapter["target_entity"]}
            if destination:
                # Literal strings retain leading zeros through HA script rendering.
                action["data"] = {adapter["destination_field"]: destination["number"]}
            guard = "states(" + repr(target["state_entity"]) + ") in " + repr(STATES[operation])
            if operation == "call":
                guard += " and is_state(" + repr(target["peer_entity"]) + ", '')"
            if "target_entity" in adapter:
                entity = repr(adapter["target_entity"])
                guard += " and states[" + entity + "] is not none and not is_state(" + entity + ", 'unavailable')"
                guard += " and not state_attr(" + entity + ", 'restored')"
            package["intent_script"][name] = {
                "description": "Explicit native phone command for " + config["id"],
                "platforms": ["conversation"], "mode": "single", "async_action": False,
                "action": [
                    {"variables": {"outcome": {"text": words["blocked"]}}},
                    {"if": "{{ " + guard + " }}", "then": [action,
                        {"variables": {"outcome": {"text": words["sent"]}}}]},
                    {"stop": "Return the command result", "response_variable": "outcome"},
                ],
                "speech": {"text": "{{ action_response.text }}"},
            }
    return package, sentences


def write_outputs(config, output):
    package, sentences = render(config)
    name = "assist_phone_" + config["id"] + ".yaml"
    documents = {"packages/" + name: package}
    documents.update({"custom_sentences/" + language + "/" + name: data for language, data in sentences.items()})
    for relative, data in documents.items():
        path = Path(output) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
                temporary = stream.name
                os.chmod(temporary, 0o600)
                json.dump(data, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
            os.replace(temporary, path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
    return list(documents)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        names = write_outputs(json.loads(args.config.read_text(encoding="utf-8")), args.output)
    except (OSError, ValueError, TypeError):
        parser.exit(1, "Cannot render Assist phone configuration; check the inventory and output directory.\n")
    print("Rendered " + str(len(names)) + " files offline. No HA action was performed.")


if __name__ == "__main__":
    main()
