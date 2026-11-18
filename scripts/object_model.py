"""Stable Agent Governance Runtime object model helpers.

Schemas are data under ``object_model/``; this module provides the small
runtime contract used by adapters and MCP. It deliberately does not execute
Agents or write Memory.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
MODEL_ROOT = ROOT / "object_model"


class ObjectModelError(ValueError):
    pass


def registry() -> dict[str, Any]:
    return json.loads((MODEL_ROOT / "registry.json").read_text(encoding="utf-8"))


def schema(name: str) -> dict[str, Any]:
    item = registry().get("objects", {}).get(name)
    if not item:
        raise ObjectModelError(f"Unknown object type: {name}")
    return json.loads((MODEL_ROOT / item["schema"]).read_text(encoding="utf-8"))


def _type_matches(expected: str, value: Any) -> bool:
    # ``bool`` is a subclass of ``int`` in Python, but JSON Schema treats the
    # two types separately.
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _validate_value(definition: dict[str, Any], value: Any, path: str) -> None:
    expected = definition.get("type")
    expected_types = expected if isinstance(expected, list) else [expected] if expected else []
    if expected_types and not any(_type_matches(item, value) for item in expected_types):
        readable = " or ".join(str(item) for item in expected_types)
        raise ObjectModelError(f"{path} must be {readable}")
    if "enum" in definition and value not in definition["enum"]:
        raise ObjectModelError(f"{path} must be one of: {', '.join(map(str, definition['enum']))}")
    if isinstance(value, str):
        minimum = definition.get("minLength")
        if minimum is not None and len(value) < minimum:
            raise ObjectModelError(f"{path} must contain at least {minimum} characters")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if definition.get("minimum") is not None and value < definition["minimum"]:
            raise ObjectModelError(f"{path} must be at least {definition['minimum']}")
        if definition.get("maximum") is not None and value > definition["maximum"]:
            raise ObjectModelError(f"{path} must be at most {definition['maximum']}")
    if isinstance(value, list):
        if definition.get("minItems") is not None and len(value) < definition["minItems"]:
            raise ObjectModelError(f"{path} must contain at least {definition['minItems']} items")
        item_definition = definition.get("items")
        if isinstance(item_definition, dict):
            for index, item in enumerate(value):
                _validate_value(item_definition, item, f"{path}[{index}]")
    if not isinstance(value, dict):
        return
    properties = definition.get("properties", {})
    for key, child_definition in properties.items():
        if key in value and isinstance(child_definition, dict):
            _validate_value(child_definition, value[key], f"{path}.{key}")
    if definition.get("additionalProperties") is False:
        unknown = [key for key in value if key not in properties]
        if unknown:
            raise ObjectModelError(f"{path} has unknown fields: {', '.join(unknown)}")


def validate(name: str, value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ObjectModelError(f"{name} must be an object")
    definition = schema(name)
    missing = [key for key in definition.get("required", []) if key not in value]
    if missing:
        raise ObjectModelError(f"{name} missing required fields: {', '.join(missing)}")
    _validate_value(definition, value, name)
    return value


def normalize_event(raw: dict[str, Any], *, source: str = "hook", event_type: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ObjectModelError("External event must be an object")
    payload_keys = {"hook_event_name", "tool_name", "tool_input", "error", "result", "event"}
    payload = {key: raw[key] for key in payload_keys if key in raw}
    context = {key: raw[key] for key in ("session_id", "turn_id", "agent_id", "agent_type", "team_id", "project_id") if key in raw}
    event = {
        "id": str(raw.get("event_id") or raw.get("id") or f"evt_{uuid4().hex}"),
        "type": str(event_type or raw.get("hook_event_name") or raw.get("type") or "external.event"),
        "source": source,
        "timestamp": float(raw.get("timestamp") or time.time()),
        "payload": payload or dict(raw.get("payload") or {}),
        "context": context or dict(raw.get("context") or {}),
        "status": "normalized",
    }
    return validate("Event", event)


def lifecycle(name: str) -> list[str]:
    item = registry().get("objects", {}).get(name)
    if not item:
        raise ObjectModelError(f"Unknown object type: {name}")
    return list(item["lifecycle"])


def transition(name: str, current: str, target: str) -> bool:
    states = lifecycle(name)
    if current not in states or target not in states:
        raise ObjectModelError(f"Unknown {name} lifecycle state")
    if current == target:
        return True
    index = states.index(current)
    return states.index(target) >= index


def permission_matrix() -> dict[str, Any]:
    return registry().get("runtime_modules", {})

