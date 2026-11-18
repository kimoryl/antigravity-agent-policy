from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from policy_engine import load_policy  # noqa: E402
from adapters.codex.adapter import CodexAdapter  # noqa: E402


class FakeCodexHost:
    def __init__(self, outcomes: list[dict[str, object]]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    def __call__(self, request: dict[str, object]) -> dict[str, object]:
        self.calls.append(dict(request))
        outcome = self.outcomes.pop(0)
        if outcome.get("status") == "error":
            return dict(outcome)
        return {"status": "ok", "model": request["model"], **outcome}


class ModelFallbackHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_policy()
        self.request = {
            "role": "tester",
            "user_model": "gpt-6.1-sol",
            "available_models": ["gpt-6.1-sol", "gpt-6-sol"],
        }

    def test_capacity_fallback_is_injected_and_logged_at_host_boundary(self) -> None:
        host = FakeCodexHost([
            {"status": "error", "failure_kind": "model_at_capacity"},
            {"status": "ok", "output": "fallback result"},
        ])
        events: list[dict[str, object]] = []
        result = CodexAdapter().execute_with_policy(
            self.request, policy=self.policy, runner=host, audit=events.append
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual([call["model"] for call in host.calls], ["gpt-6.1-sol", "gpt-6-sol"])
        self.assertEqual(result["selected_model"], "gpt-6-sol")
        self.assertEqual(result["executed_model"], "gpt-6-sol")
        self.assertEqual(result["fallback_count"], 1)
        self.assertEqual(result["retries"], 1)
        self.assertEqual(result["attempts"][-1]["executed_model"], "gpt-6-sol")
        self.assertEqual(events, result["attempts"])

    def test_unavailable_fallback_stops_before_host_execution(self) -> None:
        host = FakeCodexHost([{"status": "error", "failure_kind": "model_capacity"}])
        request = {**self.request, "available_models": ["gpt-6.1-sol"]}
        result = CodexAdapter().execute_with_policy(request, policy=self.policy, runner=host)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["reason"], "model_unavailable")
        self.assertTrue(result["retry_later"])
        self.assertEqual(len(host.calls), 1)
        self.assertEqual(result["retries"], 1)
        self.assertEqual(result["executed_model"], None)

    def test_repeated_capacity_failures_are_bounded(self) -> None:
        host = FakeCodexHost([{"status": "error", "failure_kind": "capacity"}] * 10)
        result = CodexAdapter().execute_with_policy(self.request, policy=self.policy, runner=host)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["reason"], "model_unavailable")
        self.assertLessEqual(len(host.calls), 2)
        self.assertTrue(result["retry_later"])
        self.assertEqual(result["executed_model"], None)

    def test_host_model_mismatch_is_reported_without_claiming_success(self) -> None:
        host = FakeCodexHost([{"status": "ok", "model": "wrong-model"}])
        result = CodexAdapter().execute_with_policy(self.request, policy=self.policy, runner=host)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["reason"], "host_model_mismatch")
        self.assertIsNone(result["executed_model"])

    def test_host_must_explicitly_report_the_executed_model(self) -> None:
        result = CodexAdapter().execute_with_policy(
            self.request, policy=self.policy, runner=lambda request: {"status": "ok"}
        )
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["reason"], "host_model_unreported")
        self.assertIsNone(result["executed_model"])

    def test_inherit_leaves_model_selection_to_the_host(self) -> None:
        policy = copy.deepcopy(self.policy)
        policy["model_policy"]["default"] = "inherit"
        host_requests: list[dict[str, object]] = []

        def host(request: dict[str, object]) -> dict[str, object]:
            host_requests.append(request)
            return {"status": "ok", "model": "host-default"}

        result = CodexAdapter().execute_with_policy(
            {"role": "tester"}, policy=policy, runner=host
        )
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["selected_model"])
        self.assertEqual(result["executed_model"], "host-default")
        self.assertNotIn("model", host_requests[0])


if __name__ == "__main__":
    unittest.main()
