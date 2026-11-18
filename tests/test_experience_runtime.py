from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from experience_engine import call as experience_call  # noqa: E402
from experience_store import get_skill_proposal, propose_experience  # noqa: E402
from agent_registry import compose_agent_profile, load_agents  # noqa: E402
from runtime_engine import resolve_context  # noqa: E402
from runtime_events import emit, read_events  # noqa: E402
from runtime_state import (context_snapshot_fresh, create_context_snapshot, create_task,
                           get_context_snapshot, invalidate_context_snapshot)  # noqa: E402


class ExperienceRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.env = patch.dict(os.environ, {"CODEX_AGENT_POLICY_DATA": str(Path(self.temp.name) / "data")})
        self.env.start()
        self.addCleanup(self.env.stop)
        profile = {
            "root_goal": "Implement runtime learning",
            "local_goal": "verify experience",
            "task_type": "implementation",
            "constraints": [],
            "expected_outputs": ["tests"],
            "acceptance_criteria": ["bounded"],
            "required_capabilities": ["python"],
            "complexity_hint": "high",
        }
        self.task = create_task(profile, str(self.project))

    def test_promotion_gate_and_bounded_context(self) -> None:
        item = experience_call("propose", {
            "project_id": self.task["project_id"], "title": "Stable runtime pattern",
            "summary": "Use a bounded event and snapshot protocol.",
            "conditions": ["runtime state is available"], "recommendations": ["keep provenance"],
            "confidence": 0.9, "support_count": 3, "project_count": 2,
        })
        experience_call("validate", {"experience_id": item["experience_id"], "verdict": "pass", "validation_type": "reviewer"})
        promoted = experience_call("promote", {"experience_id": item["experience_id"], "approve": True})
        self.assertEqual(promoted["status"], "global")
        context = resolve_context(self.task["task_id"], "bounded event protocol", [], max_chars=2000,
                                  use_jev=False, memory=False, experience=True, experience_limit=3)
        self.assertTrue(context["experience"])
        self.assertEqual(context["experience"][0]["label"], "EXPERIENCE")
        self.assertTrue(context["experience_status"]["untrusted"])
        self.assertLessEqual(context["context_budget"]["used_chars"], 2000)

    def test_sensitive_experience_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            emit("unsafe", payload={"api_key": "never"})
        with self.assertRaises(Exception):
            experience_call("propose", {"project_id": self.task["project_id"], "title": "secret", "summary": "secret token"})

    def test_candidate_skill_requires_all_validations_and_policy_is_candidate_only(self) -> None:
        experience = propose_experience(project_id=self.task["project_id"], title="Reusable lesson", summary="Use explicit validation gates.")
        proposal = experience_call("skill_propose", {
            "experience_id": experience["experience_id"], "name": "bounded-runtime-pattern",
            "body": "Apply bounded context and explicit review gates.",
        })
        proposal_path = ROOT / "skill_pool" / "experimental" / "bounded-runtime-pattern" / "SKILL.md"
        self.addCleanup(lambda: (proposal_path.unlink(missing_ok=True), proposal_path.parent.rmdir()) if proposal_path.parent.exists() else None)
        for validation_type in ("original", "regression", "cross_task", "reviewer"):
            result = experience_call("skill_validate", {"proposal_id": proposal["proposal_id"],
                                                          "validation_type": validation_type, "verdict": "pass"})
        self.assertEqual(result["status"], "validated")
        active = experience_call("skill_activate", {"proposal_id": proposal["proposal_id"]})
        self.assertEqual(active["status"], "active")
        candidate = experience_call("policy_candidate", {"experience_id": experience["experience_id"], "hard_boundary": True})
        self.assertEqual(candidate["status"], "candidate_only")
        self.assertFalse((ROOT / "policy" / "default.json").read_text(encoding="utf-8").find("candidate_only") >= 0)
        self.assertTrue(proposal_path.exists())

    def test_snapshot_freshness_and_append_only_events(self) -> None:
        source = self.project / "architecture.md"
        source.write_text("stable", encoding="utf-8")
        import hashlib
        snapshot = create_context_snapshot(self.task["project_id"], str(self.project), "r1", [{
            "path": "architecture.md", "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}])
        self.assertTrue(context_snapshot_fresh(snapshot["snapshot_id"]))
        source.write_text("changed", encoding="utf-8")
        self.assertFalse(context_snapshot_fresh(snapshot["snapshot_id"]))
        self.assertTrue(get_context_snapshot(snapshot["snapshot_id"])["status"].startswith("invalidated:"))
        invalidated = invalidate_context_snapshot(snapshot["snapshot_id"], "test")
        self.assertTrue(invalidated["status"].startswith("invalidated:"))
        event = emit("test.cursor", project_id=self.task["project_id"], task_id=self.task["task_id"], payload={"ok": True})
        page = read_events(event["seq"] - 1, 10, task_id=self.task["task_id"])
        self.assertEqual(page["events"][-1]["event_id"], event["event_id"])

    def test_dynamic_profile_reuses_registered_role(self) -> None:
        role = next(iter(load_agents()["roles"]))
        profile = compose_agent_profile(role, capabilities=["testing"], model_profile={"model": "luna"},
                                        permissions=["read_repo"], task={"task_id": self.task["task_id"]})
        self.assertEqual(profile["role"], role)
        self.assertEqual(profile["model_profile"]["model"], "luna")
        self.assertEqual(profile["task"]["task_id"], self.task["task_id"])
        with self.assertRaises(ValueError):
            compose_agent_profile("不存在的角色")


if __name__ == "__main__":
    unittest.main()
