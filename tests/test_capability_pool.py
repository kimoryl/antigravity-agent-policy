from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

try:
    from capability_loader import load_capabilities  # noqa: E402
    from capability_registry import catalog_registry, load_registry, validate_registry  # noqa: E402
    from capability_resolver import resolve_capabilities  # noqa: E402
    from skill_resolver import SkillService  # noqa: E402
    from sync_capability_pool import build_registry  # noqa: E402
    HAS_CAPABILITY_POOL = True
except ImportError:
    HAS_CAPABILITY_POOL = False
    SkillService = lambda *args, **kwargs: None  # noqa: E731
from runtime_state import checkpoint, create_task, restore  # noqa: E402
from runtime_tools import call as runtime_call, TOOLS  # noqa: E402


@unittest.skipUnless(HAS_CAPABILITY_POOL, "Migrated to Unified Mod System")
class CapabilityPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skills = SkillService()

    def test_registry_catalog_supports_filters_and_health_metadata(self) -> None:
        registry = load_registry()
        self.assertEqual({item["type"] for item in registry["capabilities"]}, {"skill", "mcp", "hybrid"})
        result = catalog_registry(capability="repository.read")
        self.assertEqual(result["total"], 1)
        item = result["capabilities"][0]
        self.assertEqual(item["type"], "mcp")
        self.assertIn("availability", item)
        self.assertEqual(catalog_registry(type="skill")["total"], 40)

    def test_every_skill_pool_entry_has_a_capability_mirror(self) -> None:
        skills = self.skills.registry()["skills"]
        registry = load_registry()
        mirrored = {item["skill"]["name"] for item in registry["capabilities"] if item.get("managed_source") == "skill_pool"}
        self.assertEqual(mirrored, {item["name"] for item in skills})
        self.assertEqual(len(mirrored), 39)
        self.assertEqual(validate_registry(build_registry()), registry)

    def test_live_panel_skill_loads_from_the_pool_snapshot(self) -> None:
        loaded = load_capabilities({
            "task_id": "live-panel",
            "stage": "execution",
            "required_capabilities": ["skill.livepanel"],
            "task_goal": "render a live architecture panel",
            "load_skill_content": True,
        }, skills=self.skills)
        self.assertTrue(loaded["loaded"])
        self.assertEqual(loaded["loaded_skills"][0]["name"], "live-panel")
        self.assertIn("Turn a description of a system", loaded["loaded_skills"][0]["content"])

    def test_manual_skill_requires_explicit_invocation(self) -> None:
        request = {
            "task_id": "t",
            "stage": "execution",
            "required_capabilities": ["skill.mattpocockimplement"],
            "task_goal": "implement",
        }
        blocked = resolve_capabilities(request, skills=self.skills)
        self.assertIn("skill.mattpocockimplement", blocked["unresolved_capabilities"])
        self.assertEqual(blocked["decisions"][0]["reason"], "user_invocation_required")
        allowed = resolve_capabilities({**request, "user_invoked_skills": ["mattpocock-implement"]}, skills=self.skills)
        self.assertEqual(allowed["status"], "ready")

    def test_resolve_pure_skill_mcp_and_child_minimum(self) -> None:
        skill = resolve_capabilities({
            "task_id": "t",
            "stage": "planning",
            "required_capabilities": ["architecture.review"],
            "task_goal": "review architecture",
            "version_lock": {},
        }, skills=self.skills)
        self.assertEqual(skill["status"], "ready")
        self.assertEqual(skill["mcp_requirements"], [])
        self.assertEqual(skill["skill_requirements"][0]["name"], "mattpocock-code-review")

        mcp = resolve_capabilities({
            "task_id": "t",
            "stage": "execution",
            "required_capabilities": ["repository.read"],
            "task_goal": "read repository",
            "version_lock": {},
            "parent_capabilities": ["repository.read", "browser.open"],
            "policy_permissions": ["repository.read"],
        }, skills=self.skills)
        self.assertEqual(mcp["status"], "ready")
        self.assertEqual(mcp["agent_profile"]["mcp_tools"], ["search_repository", "read_repository"])
        self.assertEqual(mcp["effective_permissions"], ["repository.read"])

        child = resolve_capabilities({
            "task_id": "child",
            "stage": "execution",
            "required_capabilities": ["browser.open"],
            "task_goal": "child",
            "version_lock": {},
            "parent_capabilities": ["repository.read"],
        }, skills=self.skills)
        self.assertIn("browser.open", child["unresolved_capabilities"])

    def test_dependency_unavailable_is_explicit_and_loader_keeps_layers_separate(self) -> None:
        unavailable = resolve_capabilities({
            "task_id": "t",
            "stage": "execution",
            "required_capabilities": ["knowledge.search"],
            "task_goal": "search knowledge",
            "version_lock": {},
        }, skills=self.skills)
        self.assertEqual(unavailable["status"], "software_dependency_unavailable")
        self.assertIn("knowledge.search", unavailable["unresolved_capabilities"])

        loaded = load_capabilities({
            "task_id": "t",
            "stage": "planning",
            "required_capabilities": ["architecture.review"],
            "task_goal": "review",
        }, skills=self.skills)
        self.assertTrue(loaded["loaded"])
        self.assertEqual(loaded["mcp_allowlist"], [])
        self.assertEqual(loaded["loaded_skills"][0]["name"], "mattpocock-code-review")

    def test_runtime_tools_and_agent_profile_expose_capability_pool(self) -> None:
        names = {item["name"] for item in TOOLS}
        self.assertTrue({"capability_catalog", "capability_resolve", "capability_load"}.issubset(names))
        catalog = runtime_call("capability_catalog", {"type": "mcp"}, self.skills)
        self.assertEqual(catalog["total"], 1)
        profile = runtime_call("runtime_agent_profile", {
            "role": "coder",
            "capability_profile": {
                "capabilities": ["repository-read"],
                "mcp_servers": ["github"],
                "mcp_tools": ["read_repository"],
                "version_lock": {"repository-read": "1.0.0"},
            },
        }, self.skills)
        self.assertEqual(profile["mcp_tools"], ["read_repository"])
        self.assertEqual(profile["capability_version_lock"]["repository-read"], "1.0.0")

    def test_checkpoint_restore_marks_capability_version_change(self) -> None:
        profile = {
            "root_goal": "capability",
            "local_goal": "checkpoint",
            "task_type": "testing",
            "constraints": [],
            "expected_outputs": [],
            "acceptance_criteria": [],
            "required_capabilities": [],
            "complexity_hint": "simple",
            "capability_version_lock": {"architecture-review": "9.9.9"},
        }
        with tempfile.TemporaryDirectory() as directory:
            task = create_task(profile, directory)
            saved = checkpoint(task["task_id"], "capability version test")
            restored = restore(task["task_id"], saved["checkpoint_id"])
        self.assertTrue(restored["capability_changed"])
        self.assertEqual(restored["capability_change_details"]["architecture-review"]["actual"], "1.0.0")


if __name__ == "__main__":
    unittest.main()
