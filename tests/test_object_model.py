from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from object_model import ObjectModelError, normalize_event, validate  # noqa: E402


class ObjectModelValidationTests(unittest.TestCase):
    def test_valid_task_with_nested_fields_is_accepted(self) -> None:
        value = {
            "id": "task-1",
            "goal": "verify",
            "owner": "agent-1",
            "status": "created",
            "capability": ["code.search"],
            "input": {"query": "policy"},
            "output": {},
            "verification": [{"name": "tests"}],
        }
        self.assertIs(validate("Task", value), value)

    def test_schema_types_and_nested_items_are_enforced(self) -> None:
        base = {
            "id": "task-1",
            "goal": "verify",
            "owner": "agent-1",
            "status": "created",
            "capability": ["code.search"],
            "input": {},
            "output": {},
            "verification": [],
        }
        cases = (
            ({**base, "status": "unknown"}, "status"),
            ({**base, "capability": [1]}, "capability"),
            ({**base, "input": []}, "input"),
            ({**base, "verification": [1]}, "verification"),
            ({**base, "id": 1}, "id"),
        )
        for value, field in cases:
            with self.subTest(field=field):
                with self.assertRaisesRegex(ObjectModelError, field):
                    validate("Task", value)

    def test_scalar_constraints_are_enforced(self) -> None:
        event = {
            "id": "",
            "type": "hook",
            "source": "test",
            "timestamp": 1.0,
            "payload": {},
            "context": {},
        }
        with self.assertRaisesRegex(ObjectModelError, "id"):
            validate("Event", event)
        with self.assertRaisesRegex(ObjectModelError, "timestamp"):
            validate("Event", {**event, "id": "evt-1", "timestamp": True})

    def test_normalized_external_event_remains_compatible(self) -> None:
        result = normalize_event({"hook_event_name": "PreToolUse", "tool_name": "Agent"})
        self.assertEqual(result["status"], "normalized")
        self.assertEqual(result["payload"]["tool_name"], "Agent")


if __name__ == "__main__":
    unittest.main()
