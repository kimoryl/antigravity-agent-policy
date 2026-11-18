from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "hooks"))

from policy_engine import PolicyError, check_tool, load_policy, resolve  # noqa: E402
from agent_registry import load_agents  # noqa: E402
from policy_hook import handle as handle_hook  # noqa: E402
try:
    from skill_resolver import SkillError, SkillService, load_registry, resolve_skills  # noqa: E402
    HAS_SKILL_RESOLVER = True
except ImportError:
    HAS_SKILL_RESOLVER = False
    SkillError = RuntimeError
    SkillService = lambda *args, **kwargs: None  # noqa: E731
    load_registry = resolve_skills = None
import dashboard  # noqa: E402
from runtime_engine import (ready_steps, record_context_refs, record_usage, resolve_context, set_plan,
                            update_step, validate_plan, verify)  # noqa: E402
from runtime_state import (RuntimeError as TaskRuntimeError, add_memory, checkpoint,
                           active_session_task, bind_task_session, create_task, get_task, list_facts,
                           list_memories, restore, update_operation, update_task)  # noqa: E402
from runtime_tools import call as runtime_call  # noqa: E402
from jev_adapter import JevUnavailable, check_verification, rank_context, rank_skills, suggest_task_profile  # noqa: E402


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_policy()

    def test_simple_and_complex_coder(self) -> None:
        simple = resolve({"role": "coder", "task_profile": {"complexity_hint": "simple"}}, self.policy)
        complex_task = resolve({"role": "coder", "task_profile": {"complexity_hint": "complex"}}, self.policy)
        self.assertEqual(simple["reasoning_effort"], "medium")
        self.assertEqual(simple["model_override"], "gpt-6-luna")
        self.assertEqual(simple["context_budget_tokens"], 20000)
        self.assertEqual(complex_task["reasoning_effort"], "medium")
        self.assertEqual(complex_task["model_override"], "gpt-6-sol")
        self.assertEqual(complex_task["context_budget_tokens"], 20000)

    def test_reasoning_defaults_are_role_specific_and_explicit_max_is_preserved(self) -> None:
        self.assertEqual(resolve({"role": "coder", "task_profile": {}}, self.policy)["reasoning_effort"], "medium")
        self.assertEqual(resolve({"role": "tester", "task_profile": {}}, self.policy)["reasoning_effort"], "low")
        self.assertEqual(resolve({"role": "planner", "task_profile": {}}, self.policy)["reasoning_effort"], "high")
        explicit = resolve({"role": "coder", "user_reasoning_effort": "max", "task_profile": {}}, self.policy)
        self.assertEqual(explicit["reasoning_effort"], "max")
        self.assertNotIn("override:0", explicit["trace"])
        legacy_simple = resolve({"role": "coder", "task_profile": {"complexity": "simple"}}, self.policy)
        legacy_complex = resolve({"role": "coder", "task_profile": {"complexity": "complex"}}, self.policy)
        self.assertEqual(legacy_simple["model_override"], "gpt-6-luna")
        self.assertEqual(legacy_simple["context_budget_tokens"], 10000)
        self.assertEqual(legacy_complex["model_override"], "gpt-6-sol")
        self.assertEqual(legacy_complex["context_budget_tokens"], 30000)

    def test_important_task_takes_precedence(self) -> None:
        for marker in ({"risk": "high"}, {"importance": "high"}, {"architecture_change": True}):
            decision = resolve({"role": "tester", "task_profile": {"complexity_hint": "simple", **marker}}, self.policy)
            self.assertEqual(decision["model_override"], "gpt-6.1-sol")
            self.assertEqual(decision["reasoning_effort"], "low")

    def test_permission_intersection_and_depth(self) -> None:
        decision = resolve({"role": "coder", "depth": 1, "parent_permissions": ["read", "delegate"]}, self.policy)
        self.assertEqual(decision["tool_permissions"], ["read"])
        self.assertEqual(resolve({"role": "coder", "depth": 2}, self.policy)["reason"], "max_agent_depth_exceeded")

    def test_retry_and_failure_classification(self) -> None:
        retry = resolve({"role": "tester", "retries": 2, "failure_kind": "logic_error"}, self.policy)
        missing_context = resolve({"role": "tester", "retries": 2, "failure_kind": "missing_context"}, self.policy)
        self.assertEqual(retry["reasoning_effort"], "high")
        self.assertEqual(missing_context["reasoning_effort"], "low")
        self.assertTrue(missing_context["refresh_context_first"])
        self.assertEqual(resolve({"role": "tester", "retries": 3}, self.policy)["status"], "stop")

    def test_model_and_effort_validation(self) -> None:
        policy = copy.deepcopy(self.policy)
        policy["model_policy"]["stronger"] = "codex-strong"
        decision = resolve({
            "role": "coder",
            "task_profile": {"confidence": 0.3},
            "available_models": ["codex-strong"],
            "user_reasoning_effort": "max",
            "supported_efforts": ["max"],
        }, policy)
        self.assertEqual(decision["model_override"], "codex-strong")
        with self.assertRaisesRegex(PolicyError, "not available"):
            resolve({"role": "coder", "user_model": "missing", "available_models": ["codex-strong"]}, policy)
        with self.assertRaisesRegex(PolicyError, "does not support"):
            resolve({"role": "coder", "supported_efforts": ["low"]}, policy)

    def test_model_capacity_fallback(self) -> None:
        decision = resolve({"role": "tester", "user_model": "gpt-6.1-sol", "failure_kind": "model_at_capacity"}, self.policy)
        self.assertEqual(decision["model_override"], "gpt-6-sol")
        self.assertIn("fallback:model_capacity:gpt-6-sol", decision["trace"])

    def test_capacity_fallback_skips_unavailable_candidates_and_reports_exhaustion(self) -> None:
        policy = copy.deepcopy(self.policy)
        policy["model_policy"]["fallbacks"]["model_capacity"]["gpt-6.1-sol"] = ["gpt-6-sol", "gpt-6.1-mini"]
        decision = resolve({
            "role": "tester", "user_model": "gpt-6.1-sol", "failure_kind": "model_capacity",
            "available_models": ["gpt-6.1-sol", "gpt-6.1-mini"],
        }, policy)
        self.assertEqual(decision["model_override"], "gpt-6.1-mini")
        exhausted = resolve({
            "role": "tester", "user_model": "gpt-6.1-sol", "failure_kind": "model_capacity",
            "attempted_models": ["gpt-6.1-sol", "gpt-6.1-mini"],
            "available_models": ["gpt-6.1-sol", "gpt-6.1-mini"],
        }, policy)
        self.assertEqual(exhausted["status"], "stop")
        self.assertEqual(exhausted["reason"], "model_unavailable")
        self.assertTrue(exhausted["retry_later"])

    def test_capacity_and_rate_limit_failures_remain_distinct_and_locked_model_stays_locked(self) -> None:
        rate_limited = resolve({"role": "tester", "user_model": "gpt-6.1-sol", "failure_kind": "rate_limited"}, self.policy)
        self.assertEqual(rate_limited["status"], "ready")
        self.assertEqual(rate_limited["model_override"], "gpt-6.1-sol")
        locked = resolve({"role": "tester", "user_model": "gpt-6.1-sol", "failure_kind": "model_capacity", "model_locked": True}, self.policy)
        self.assertEqual(locked["status"], "stop")
        self.assertEqual(locked["reason"], "model_locked")

    def test_context_cap_and_review(self) -> None:
        decision = resolve({
            "role": "coder",
            "task_profile": {"complexity": "complex", "architecture_change": True},
            "host_context_budget_tokens": 18000,
        }, self.policy)
        self.assertEqual(decision["context_budget_tokens"], 18000)
        self.assertTrue(decision["require_reviewer"])

    def test_tool_permission_is_not_a_grant(self) -> None:
        reviewer = resolve({"role": "reviewer"}, self.policy)
        self.assertFalse(check_tool("apply_patch", reviewer, self.policy)["allowed"])
        self.assertFalse(check_tool("Bash", reviewer, self.policy)["allowed"])
        self.assertTrue(check_tool("mcp__docs__search", reviewer, self.policy)["allowed"])

    def test_specialist_roles_inherit_model_and_keep_permission_boundaries(self) -> None:
        registry = load_agents()["roles"]
        groups = {
            "presentation": ["presentation_planner", "presentation_builder", "presentation_reviewer"],
            "learning": ["learning_planner", "learning_tutor", "learning_assessor"],
            "writing": ["writing_researcher", "writing_drafter", "writing_editor"],
        }
        argon_prose_roles = {
            "presentation_planner",
            "learning_planner",
            "learning_tutor",
            "writing_researcher",
            "writing_drafter",
            "writing_editor",
        }
        self.assertEqual(set(registry), {"planner", "coder", "tester", "reviewer"} | {name for names in groups.values() for name in names})
        self.assertEqual(set(registry), set(self.policy["roles"]))
        for category, names in groups.items():
            for name in names:
                self.assertEqual(registry[name]["category"], category)
                self.assertTrue(registry[name]["display_name"])
                expected_model = "gemini-4-argon-high" if name in argon_prose_roles else "gpt-6-sol"
                self.assertEqual(resolve({"role": name}, self.policy)["model_override"], expected_model)
                self.assertTrue(registry[name]["guidance"])
                self.assertTrue(registry[name]["skill_tags"])
        for name in ("presentation_reviewer", "learning_assessor", "writing_researcher"):
            self.assertNotIn("write", resolve({"role": name}, self.policy)["tool_permissions"])
        for name in ("presentation_builder", "writing_drafter", "writing_editor"):
            self.assertIn("write", resolve({"role": name}, self.policy)["tool_permissions"])

    def test_gemini_4_tri_core_capacity_fallbacks(self) -> None:
        carbon_fb = resolve(
            {"role": "coder", "user_model": "gemini-4-carbon-high", "failure_kind": "model_at_capacity"},
            self.policy,
        )
        self.assertEqual(carbon_fb["model_override"], "gemini-4-hydrogen-xhigh")
        argon_fb = resolve(
            {"role": "writing_drafter", "failure_kind": "model_at_capacity"},
            self.policy,
        )
        self.assertEqual(argon_fb["model_override"], "gemini-4-hydrogen-high")
        hydrogen_fb = resolve(
            {"role": "coder", "user_model": "gemini-4-hydrogen-high", "failure_kind": "model_at_capacity"},
            self.policy,
        )
        self.assertEqual(hydrogen_fb["model_override"], "gemini-4-hydrogen-medium")

    def test_specialist_contracts_support_three_stage_workflows(self) -> None:
        roles = load_agents()["roles"]
        stages = [
            ["presentation_planner", "presentation_builder", "presentation_reviewer"],
            ["learning_planner", "learning_tutor", "learning_assessor"],
            ["writing_researcher", "writing_drafter", "writing_editor"],
        ]
        steps = []
        for group in stages:
            previous = None
            for name in group:
                role = roles[name]
                contract = runtime_call("runtime_plan", {
                    "action": "contract", "role": name,
                    "agent_input": {key: "sample" for key in role["input_fields"]},
                    "agent_output": {key: "sample" for key in role["output_fields"]},
                }, SkillService())
                self.assertTrue(contract["valid"], name)
                self.assertEqual(contract["guidance"], role["guidance"])
                self.assertEqual(contract["display_name"], role["display_name"])
                self.assertEqual(contract["skill_tags"], role["skill_tags"])
                steps.append({"id": name, "goal": role["purpose"], "role": name,
                              "depends_on": [previous] if previous else [],
                              "expected_outputs": role["output_fields"], "write_paths": []})
                previous = name
        self.assertTrue(validate_plan(steps)["valid"])


class HookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("CODEX_AGENT_POLICY_DATA")
        os.environ["CODEX_AGENT_POLICY_DATA"] = self.folder.name

    def tearDown(self) -> None:
        if self.previous is None:
            os.environ.pop("CODEX_AGENT_POLICY_DATA", None)
        else:
            os.environ["CODEX_AGENT_POLICY_DATA"] = self.previous
        self.folder.cleanup()

    def test_parallel_cap_and_release(self) -> None:
        def spawn(index: int) -> dict:
            return handle_hook({
                "hook_event_name": "PreToolUse",
                "tool_name": "Agent",
                "session_id": "s1",
                "turn_id": "t1",
                "tool_use_id": f"call-{index}",
                "tool_input": {"task_name": "tester"},
            })

        for index in range(3):
            self.assertEqual(spawn(index), {})
        self.assertEqual(spawn(3)["hookSpecificOutput"]["permissionDecision"], "deny")
        handle_hook({"hook_event_name": "SubagentStart", "session_id": "s1", "turn_id": "t1", "agent_id": "a1"})
        handle_hook({"hook_event_name": "SubagentStop", "session_id": "s1", "agent_id": "a1"})
        self.assertEqual(spawn(4), {})

    def test_depth_and_global_tool_deny(self) -> None:
        event = {
            "hook_event_name": "PreToolUse", "tool_name": "Agent",
            "session_id": "s2", "turn_id": "t2", "agent_depth": 1,
            "tool_use_id": "call-depth", "tool_input": {},
        }
        self.assertEqual(handle_hook(event)["hookSpecificOutput"]["permissionDecision"], "deny")


class McpTests(unittest.TestCase):
    def test_stdio_handshake_and_policy_call(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            environment = dict(os.environ, CODEX_AGENT_POLICY_DATA=folder)
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "policy_resolve", "arguments": {"role": "coder", "task_profile": {"complexity_hint": "simple"}}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "runtime_task", "arguments": {"action": "create", "project_root": str(ROOT), "profile": {"root_goal": "Inspect plugin", "local_goal": "Inspect", "task_type": "review", "constraints": [], "expected_outputs": [], "acceptance_criteria": [], "required_capabilities": [], "complexity_hint": "simple"}}}},
            ]
            process = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "mcp_server.py")],
                input="\n".join(json.dumps(item) for item in messages) + "\n",
                text=True, capture_output=True, env=environment, check=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            replies = [json.loads(line) for line in process.stdout.splitlines()]
            self.assertEqual([reply["id"] for reply in replies], [1, 2, 3, 4])
            self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-03-26")
            tool_names = [item["name"] for item in replies[1]["result"]["tools"]]
            self.assertIn("policy_resolve", tool_names)
            self.assertIn("mod_resolve", tool_names)
            self.assertIn("mod_catalog", tool_names)
            self.assertIn("dashboard_open", tool_names)
            schemas = {item["name"]: item["inputSchema"]["properties"] for item in replies[1]["result"]["tools"]}
            for name, field in (("runtime_plan", "steps"), ("runtime_context", "sources"), ("runtime_verify", "checks")):
                self.assertEqual(schemas[name][field]["items"]["type"], "object")
            for name, field in (("runtime_task", "selected_skills"), ("runtime_task", "tool_permissions"), ("runtime_context", "requested_paths")):
                self.assertEqual(schemas[name][field]["items"]["type"], "string")
            self.assertEqual(replies[2]["result"]["structuredContent"]["reasoning_effort"], "medium")
            self.assertEqual(replies[2]["result"]["structuredContent"]["model_override"], "gpt-6-luna")
            self.assertEqual(replies[3]["result"]["structuredContent"]["status"], "active")


@unittest.skipUnless(HAS_SKILL_RESOLVER, "Migrated to Unified Mod System")
class SkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.base = Path(self.folder.name)
        self.registry_path = self.base / "skill_registry.json"

    def tearDown(self) -> None:
        self.folder.cleanup()

    def registry(self, skills: list[dict], *, defer_links: bool = False) -> dict:
        for item in skills:
            folder = self.base / item["name"]
            folder.mkdir(exist_ok=True)
            (folder / "SKILL.md").write_text(f"# {item['name']}\nprivate body\n", encoding="utf-8")
            item["path"] = f"{item['name']}/SKILL.md"
        self.registry_path.write_text(json.dumps({"schema_version": 1, "capabilities": ["code.implement", "test.unit"], "skills": skills}), encoding="utf-8")
        return load_registry(self.registry_path, _defer_links=defer_links)

    @staticmethod
    def skill(name: str, provides: list[str], *, priority: int = 1, requires: list[str] | None = None, conflicts: list[str] | None = None) -> dict:
        return {"name": name, "provides": provides, "stage": ["execution"], "requires": requires or [], "conflicts": conflicts or [], "priority": priority, "context_requirements": [], "version": "1.0.0", "trust": "trusted"}

    def test_selected_only_and_version_lock(self) -> None:
        registry = self.registry([self.skill("code", ["code.implement"], requires=["helper"]), self.skill("helper", []), self.skill("test", ["test.unit"])])
        service = SkillService(self.registry_path)
        result = service.resolve({"stage": "execution", "required_capabilities": ["code.implement", "unknown.capability"]})
        self.assertEqual([item["name"] for item in result["selected_skills"]], ["helper", "code"])
        self.assertEqual(result["unresolved_capabilities"], ["unknown.capability"])
        self.assertEqual(result["version_lock"], {"helper": "1.0.0", "code": "1.0.0"})
        self.assertIn("private body", service.load(result["selection_id"], "code")["content"])
        with self.assertRaises(SkillError):
            service.load(result["selection_id"], "test")
        locked = resolve_skills({"stage": "execution", "required_capabilities": ["code.implement"], "version_lock": {"code": "0.9.0"}}, registry)
        self.assertEqual(locked["unresolved_capabilities"], ["code.implement"])

    def test_user_invoked_skill_needs_explicit_user_name(self) -> None:
        manual = self.skill("manual", ["code.implement"])
        manual["invocation_mode"] = "user"
        self.registry([manual])
        service = SkillService(self.registry_path)
        self.assertEqual(service.catalog("manual")["skills"][0]["invocation_mode"], "user")
        request = {"stage": "execution", "required_capabilities": ["code.implement"],
                   "preferred_skills": ["manual"]}
        denied = service.resolve(request)
        self.assertEqual(denied["unresolved_capabilities"], ["code.implement"])
        with self.assertRaises(SkillError):
            service.load(denied["selection_id"], "manual")
        chosen = service.resolve({**request, "user_invoked_skills": ["manual"]})
        self.assertEqual(chosen["selected_skills"][0]["name"], "manual")
        self.assertIn("private body", service.load(chosen["selection_id"], "manual")["content"])

    def test_conflict_tie_and_dependency_cycle(self) -> None:
        registry = self.registry([
            self.skill("alpha", ["code.implement"], conflicts=["beta"]),
            self.skill("beta", ["code.implement"], conflicts=["alpha"]),
            self.skill("cycle-a", ["test.unit"], requires=["cycle-b"]),
            self.skill("cycle-b", [], requires=["cycle-a"]),
        ])
        undecided = resolve_skills({"stage": "execution", "required_capabilities": ["code.implement", "test.unit"]}, registry)
        self.assertEqual(undecided["status"], "needs_rerank")
        self.assertEqual(undecided["ambiguous_candidates"][0]["candidates"], ["alpha", "beta"])
        self.assertIn("test.unit", undecided["unresolved_capabilities"])
        chosen = resolve_skills({"stage": "execution", "required_capabilities": ["code.implement"], "preferred_skills": ["beta"]}, registry)
        self.assertEqual([item["name"] for item in chosen["selected_skills"]], ["beta"])

    def test_path_escape_and_changed_body_are_rejected(self) -> None:
        self.registry([self.skill("code", ["code.implement"])])
        service = SkillService(self.registry_path)
        result = service.resolve({"stage": "execution", "required_capabilities": ["code.implement"]})
        (self.base / "code" / "SKILL.md").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(SkillError, "content changed"):
            service.load(result["selection_id"], "code")
        value = json.loads(self.registry_path.read_text(encoding="utf-8"))
        value["skills"][0]["path"] = "../outside/SKILL.md"
        self.registry_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(SkillError, "Unsafe"):
            load_registry(self.registry_path)

    def test_catalog_and_approved_external_skill_path(self) -> None:
        tagged = self.skill("code", ["code.implement"])
        tagged["tags"] = ["coding", "代码"]
        registry = self.registry([tagged])
        service = SkillService(self.registry_path)
        self.assertEqual(service.catalog("code", "execution")["skills"][0]["name"], "code")
        self.assertEqual(service.catalog("代码")["skills"][0]["tags"], ["coding", "代码"])
        self.assertEqual(service.catalog(tags=["coding"])["total"], 1)
        self.assertEqual(service.catalog(tags=["missing"])["total"], 0)
        self.assertEqual(service.catalog("unrelated")["total"], 0)
        external = self.base / ".codex" / "skills" / "external"
        external.mkdir(parents=True)
        (external / "SKILL.md").write_text("# External skill", encoding="utf-8")
        registry["skills"][0]["path"] = str(external / "SKILL.md")
        self.registry_path.write_text(json.dumps({"schema_version": 1, "capabilities": registry["capabilities"],
                                                   "skills": registry["skills"]}), encoding="utf-8")
        with patch("skill_resolver.Path.home", return_value=self.base):
            self.assertEqual(load_registry(self.registry_path)["skills"][0]["_path"], str(external / "SKILL.md"))
            registry["skills"][0]["path"] = str(self.base / "private" / "SKILL.md")
            (self.base / "private").mkdir()
            (self.base / "private" / "SKILL.md").write_text("# Private", encoding="utf-8")
            self.registry_path.write_text(json.dumps({"schema_version": 1, "capabilities": registry["capabilities"],
                                                       "skills": registry["skills"]}), encoding="utf-8")
            with self.assertRaisesRegex(SkillError, "outside approved roots"):
                load_registry(self.registry_path)

    def test_pool_topic_tags_cover_snapshots(self) -> None:
        pool = ROOT / "skill_pool"
        tags = json.loads((pool / "tags.json").read_text(encoding="utf-8"))
        imported = json.loads((pool / "registry.json").read_text(encoding="utf-8"))["skills"]
        self.assertTrue({item["name"] for item in imported}.issubset(tags))
        self.assertTrue(all(item["tags"] == tags[item["name"]] for item in imported))
        self.assertEqual(SkillService().catalog(tags=["template"])["total"],
                         sum("template" in item["tags"] for item in imported))

    def test_external_pool_merges_with_bundled_skill(self) -> None:
        self.registry([self.skill("code", ["code.implement"], requires=["agent-policy"])], defer_links=True)
        previous = os.environ.get("CODEX_AGENT_POLICY_SKILL_REGISTRY")
        os.environ["CODEX_AGENT_POLICY_SKILL_REGISTRY"] = str(self.registry_path)
        try:
            result = SkillService().resolve({"stage": "execution", "required_capabilities": ["code.implement"]})
            self.assertEqual([item["name"] for item in result["selected_skills"]], ["agent-policy", "code"])
        finally:
            if previous is None:
                os.environ.pop("CODEX_AGENT_POLICY_SKILL_REGISTRY", None)
            else:
                os.environ["CODEX_AGENT_POLICY_SKILL_REGISTRY"] = previous


class DashboardTests(unittest.TestCase):
    def test_loopback_dashboard_and_api(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            previous = os.environ.get("CODEX_AGENT_POLICY_DATA")
            os.environ["CODEX_AGENT_POLICY_DATA"] = folder
            try:
                url = dashboard.start_dashboard(SkillService())["url"]
                self.assertTrue(url.startswith("http://127.0.0.1:"))
                with urlopen(url, timeout=3) as response:
                    self.assertIn("Agent Policy 控制台", response.read().decode("utf-8"))
                with urlopen(url + "api/state", timeout=3) as response:
                    state = json.load(response)
                self.assertIn("coder", state["policy"]["roles"])
                self.assertIn("modules", state)
                body = json.dumps({"role": "writing_drafter"}).encode("utf-8")
                with urlopen(Request(url + "api/policy/resolve", data=body, headers={"Content-Type": "application/json"}), timeout=3) as response:
                    selected = json.load(response)
                self.assertEqual(selected["model_override"], "gemini-4-argon-high")
                with self.assertRaises(HTTPError) as error:
                    urlopen(url.replace("/s/", "/s/wrong-") + "api/state", timeout=3)
                self.assertEqual(error.exception.code, 404)
            finally:
                dashboard._server.shutdown()
                dashboard._server.server_close()
                dashboard._server = None
                dashboard._url = None
                if previous is None:
                    os.environ.pop("CODEX_AGENT_POLICY_DATA", None)
                else:
                    os.environ["CODEX_AGENT_POLICY_DATA"] = previous


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("CODEX_AGENT_POLICY_DATA")
        os.environ["CODEX_AGENT_POLICY_DATA"] = self.folder.name
        self.root = Path(self.folder.name) / "project"
        self.root.mkdir()
        (self.root / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
        self.profile = {"root_goal": "Implement and verify app", "local_goal": "Update app", "task_type": "implementation", "constraints": ["keep public API"], "expected_outputs": ["app.py"], "acceptance_criteria": ["tests pass"], "required_capabilities": ["code.implement"], "complexity_hint": "moderate"}

    def tearDown(self) -> None:
        if self.previous is None:
            os.environ.pop("CODEX_AGENT_POLICY_DATA", None)
        else:
            os.environ["CODEX_AGENT_POLICY_DATA"] = self.previous
        self.folder.cleanup()

    def test_task_plan_context_knowledge_verification_and_restore(self) -> None:
        task = create_task(self.profile, str(self.root), "session-1")
        task_id = task["task_id"]
        bad = [{"id": "a", "goal": "A", "role": "coder", "depends_on": ["b"]}, {"id": "b", "goal": "B", "role": "tester", "depends_on": ["a"]}]
        with self.assertRaisesRegex(TaskRuntimeError, "cycle"):
            validate_plan(bad)
        conflict = [{"id": "a", "goal": "A", "role": "coder", "write_paths": ["app.py"]}, {"id": "b", "goal": "B", "role": "coder", "write_paths": ["app.py"]}]
        self.assertFalse(validate_plan(conflict)["valid"])
        steps = [{"id": "code", "goal": "Implement", "role": "coder", "write_paths": ["app.py"]}, {"id": "test", "goal": "Verify", "role": "tester", "depends_on": ["code"]}]
        saved = set_plan(task_id, 1, steps)
        self.assertEqual(saved["revision"], 2)
        context = resolve_context(task_id, "app implementation", ["app.py"])
        self.assertEqual(context["sources"][0]["path"], "app.py")
        pinned = record_context_refs(task_id, 2, context["sources"])
        self.assertEqual(pinned["revision"], 3)
        put = runtime_call("runtime_knowledge", {"action": "fact_put", "task_id": task_id, "key": "entry", "value": "app.py is entrypoint", "source_path": "app.py", "evidence": "def run():"}, SkillService())
        self.assertEqual(put["source_path"], "app.py")
        add_memory(task_id, "decision", "Keep the public API")
        self.assertFalse(list_facts(task_id)[0]["stale"])
        self.assertEqual(list_memories(task_id)[0]["kind"], "decision")
        self.assertEqual(update_step(task_id, 3, "code", "running")["revision"], 4)
        self.assertEqual(update_step(task_id, 4, "code", "done")["revision"], 5)
        self.assertEqual(update_step(task_id, 5, "test", "running")["revision"], 6)
        self.assertEqual(update_step(task_id, 6, "test", "done")["revision"], 7)
        report = verify(task_id, [{"kind": "file_exists", "path": "app.py"}, {"kind": "exit_code", "actual": 0, "expected": 0, "criterion": "tests pass"}])
        self.assertEqual(report["status"], "passed")
        saved_checkpoint = checkpoint(task_id, "before finish")
        self.assertEqual(restore(task_id)["checkpoint_id"], saved_checkpoint["checkpoint_id"])
        finished = runtime_call("runtime_task", {"action": "complete", "task_id": task_id, "expected_revision": 8, "final_result": "Implemented and verified"}, SkillService())
        self.assertEqual(finished["status"], "complete")
        (self.root / "app.py").write_text("changed", encoding="utf-8")
        self.assertTrue(list_facts(task_id)[0]["stale"])

    def test_binding_restore_and_operation_gates(self) -> None:
        task = create_task(self.profile, str(self.root))
        self.assertEqual(task["state"]["binding_status"], "unbound")
        self.assertTrue(bind_task_session(task["task_id"], "session-bound", str(self.root), str(self.root)))
        self.assertEqual(get_task(task["task_id"])["state"]["binding_status"], "bound")
        self.assertEqual(active_session_task("session-bound")["task_id"], task["task_id"])

        saved = checkpoint(task["task_id"], "before operation")
        started = update_operation(task["task_id"], 1, "publish-1", "started", "Publish artifact")
        self.assertEqual(started["status"], "started")
        restored = restore(task["task_id"], saved["checkpoint_id"])
        self.assertEqual(restored["status"], "needs_reconciliation")
        self.assertIn("task_revision_changed", restored["conflicts"])
        self.assertIn("publish-1", restored["current_operations"])
        succeeded = update_operation(task["task_id"], 2, "publish-1", "succeeded", result_ref="evidence-1")
        self.assertEqual(succeeded["status"], "succeeded")
        with self.assertRaisesRegex(TaskRuntimeError, "cannot be replayed"):
            update_operation(task["task_id"], succeeded["revision"], "publish-1", "started")

    def test_unknown_side_effect_requires_reconciliation_before_retry(self) -> None:
        task = create_task(self.profile, str(self.root))
        started = update_operation(task["task_id"], 1, "publish-timeout", "started", "Publish artifact")
        unknown = update_operation(task["task_id"], started["revision"], "publish-timeout", "unknown")
        with self.assertRaisesRegex(TaskRuntimeError, "reconciliation"):
            update_operation(task["task_id"], unknown["revision"], "publish-timeout", "started")
        reconciled = update_operation(task["task_id"], unknown["revision"], "publish-timeout", "succeeded", result_ref="evidence-2")
        self.assertEqual(reconciled["status"], "succeeded")

    def test_binding_cas_preserves_concurrent_state_and_rejects_stale_sessions(self) -> None:
        task = create_task(self.profile, str(self.root))
        barrier = __import__("threading").Barrier(2)

        def bind() -> bool:
            barrier.wait()
            return bind_task_session(task["task_id"], "session-cas", str(self.root), str(self.root))

        def update() -> dict:
            barrier.wait()
            current = get_task(task["task_id"])
            current["state"]["cas_marker"] = "preserved"
            try:
                return update_task(task["task_id"], current["revision"], state=current["state"])
            except TaskRuntimeError:
                return get_task(task["task_id"])

        with ThreadPoolExecutor(max_workers=2) as pool:
            bound, updated = pool.map(lambda fn: fn(), (bind, update))
        final = get_task(task["task_id"])
        self.assertTrue(bound or final["session_id"] == "session-cas")
        self.assertEqual(final["state"].get("cas_marker"), "preserved")
        self.assertTrue(bind_task_session(task["task_id"], "session-cas", str(self.root), str(self.root)))
        self.assertFalse(bind_task_session(task["task_id"], "other-session", str(self.root), str(self.root)))
        cancelled = create_task(self.profile, str(self.root))
        update_task(cancelled["task_id"], 1, status="cancelled")
        self.assertFalse(bind_task_session(cancelled["task_id"], "late-session", str(self.root), str(self.root)))

    def test_checkpoint_detects_file_drift_while_git_state_stays_dirty(self) -> None:
        task = create_task(self.profile, str(self.root))
        tracked = self.root / "tracked.py"
        tracked.write_text("version one", encoding="utf-8")
        set_plan(task["task_id"], 1, [{"id": "build", "goal": "Build", "role": "coder", "write_paths": ["tracked.py"]}])
        saved = checkpoint(task["task_id"], "file drift")
        tracked.write_text("version two", encoding="utf-8")
        restored = restore(task["task_id"], saved["checkpoint_id"])
        self.assertEqual(restored["status"], "needs_reconciliation")
        self.assertIn("workspace_file_drift", restored["conflicts"])
        self.assertIn("tracked.py", restored["workspace_drift_files"])

    def test_checkpoint_reports_deleted_and_unsafe_symlink_files(self) -> None:
        task = create_task(self.profile, str(self.root))
        tracked = self.root / "tracked.py"
        tracked.write_text("version one", encoding="utf-8")
        outside = Path(self.folder.name) / "outside.py"
        outside.write_text("outside", encoding="utf-8")
        link = self.root / "link.py"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        set_plan(task["task_id"], 1, [{"id": "build", "goal": "Build", "role": "coder", "write_paths": ["tracked.py", "link.py"]}])
        saved = checkpoint(task["task_id"], "delete and symlink")
        tracked.unlink()
        restored = restore(task["task_id"], saved["checkpoint_id"])
        self.assertIn("tracked.py", restored["workspace_drift_files"])
        self.assertIn("workspace_snapshot_incomplete", restored["conflicts"])

    def test_checkpoint_reports_snapshot_limits_and_legacy_state(self) -> None:
        task = create_task(self.profile, str(self.root))
        for name in ("a.py", "b.py"):
            (self.root / name).write_text(name, encoding="utf-8")
        set_plan(task["task_id"], 1, [{"id": "build", "goal": "Build", "role": "coder", "write_paths": ["a.py", "b.py"]}])
        with patch("runtime_state.MAX_WORKSPACE_FILES", 1):
            saved = checkpoint(task["task_id"], "bounded snapshot")
        restored = restore(task["task_id"], saved["checkpoint_id"])
        self.assertEqual(restored["checkpoint_evidence_status"], "available")
        self.assertIn("workspace_snapshot_incomplete", restored["conflicts"])

    def test_ready_steps_uses_team_and_global_dispatch_intersection(self) -> None:
        task = create_task(self.profile, str(self.root))
        steps = [{"id": f"step-{i}", "goal": f"Step {i}", "role": "coder"} for i in range(5)]
        set_plan(task["task_id"], 1, steps)
        ready = ready_steps(task["task_id"], maximum=3, team_limit=1, occupied_slots=0)
        self.assertEqual(len(ready["ready"]), 1)
        self.assertEqual(ready["dispatch_limit"]["effective_limit"], 1)

        gated = create_task(self.profile, str(self.root), "session-gated")
        set_plan(gated["task_id"], 1, [{"id": "build", "goal": "Build", "role": "coder"}])
        update_step(gated["task_id"], 2, "build", "running")
        update_step(gated["task_id"], 3, "build", "done")
        verify(gated["task_id"], [{"kind": "exit_code", "actual": 0, "criterion": "tests pass"}])
        operation = update_operation(gated["task_id"], 5, "publish-2", "started")
        verify(gated["task_id"], [{"kind": "exit_code", "actual": 0, "criterion": "tests pass"}])
        with self.assertRaisesRegex(TaskRuntimeError, "unresolved operations"):
            runtime_call("runtime_task", {"action": "complete", "task_id": gated["task_id"], "expected_revision": operation["revision"] + 1, "final_result": "done"}, SkillService())

    def test_verification_criteria_and_private_context(self) -> None:
        profile = dict(self.profile, risk="high")
        task = create_task(profile, str(self.root))
        missing = verify(task["task_id"], [{"kind": "file_exists", "path": "app.py"}])
        self.assertEqual(missing["status"], "failed")
        needs_review = verify(task["task_id"], [{"kind": "exit_code", "actual": 0, "criterion": "tests pass"}])
        self.assertEqual(needs_review["status"], "needs_review")
        passed = verify(task["task_id"], [{"kind": "exit_code", "actual": 0, "criterion": "tests pass"}], strong_review="Reviewed architecture and cross-module behavior")
        self.assertEqual(passed["status"], "passed")
        (self.root / "secret-token.txt").write_text("private", encoding="utf-8")
        self.assertTrue(resolve_context(task["task_id"], "app", ["secret-token.txt"])["unresolved"])

    def test_revision_isolation_compaction_and_hook_context(self) -> None:
        first = create_task(self.profile, str(self.root), "session-2")
        other_root = Path(self.folder.name) / "other"
        other_root.mkdir()
        second = create_task(self.profile, str(other_root), "session-3")
        add_memory(first["task_id"], "lesson", "Project one only")
        self.assertEqual(list_memories(second["task_id"]), [])
        with self.assertRaisesRegex(TaskRuntimeError, "revision changed"):
            record_usage(first["task_id"], 0, 100, 100, 5, 200)
        pressure = record_usage(first["task_id"], 1, 90, 5, 500, 200, 95, host_context_window_tokens=100)
        self.assertEqual(pressure["action"], "checkpoint_and_compact")
        start = handle_hook({"hook_event_name": "SessionStart", "session_id": "session-2", "source": "compact"})
        self.assertIn(first["task_id"], start["hookSpecificOutput"]["additionalContext"])
        handle_hook({"hook_event_name": "PreCompact", "session_id": "session-2"})
        self.assertEqual(restore(first["task_id"])["task_id"], first["task_id"])
        with self.assertRaisesRegex(TaskRuntimeError, "verification"):
            runtime_call("runtime_task", {"action": "complete", "task_id": first["task_id"], "expected_revision": 2}, SkillService())

    def test_host_context_pressure_is_separate_from_plugin_loading_budget(self) -> None:
        task = create_task(self.profile, str(self.root), "pressure-24")
        measured = record_usage(
            task["task_id"], 1, 1, 2, 3, 50_000, 120_000,
            context_loading_budget_tokens=50_000,
            host_context_window_tokens=500_000,
            model_id="host-reported-model",
        )
        self.assertEqual(measured["context_pressure"], 0.24)
        self.assertEqual(measured["context_pressure_source"], "host")
        self.assertEqual(measured["action"], "continue")
        saved = get_task(task["task_id"])["state"]["usage"]
        self.assertEqual(saved["context_loading_budget_tokens"], 50_000)
        self.assertEqual(saved["host_context_window_tokens"], 500_000)
        self.assertEqual(saved["model_id"], "host-reported-model")

        unknown = record_usage(task["task_id"], measured["revision"], 1, 1, 1, 50_000, 120_000)
        self.assertIsNone(unknown["context_pressure"])
        self.assertEqual(unknown["context_pressure_source"], "unknown")
        self.assertEqual(unknown["action"], "unknown_pressure")

        soft = create_task(self.profile, str(self.root), "pressure-70")
        self.assertEqual(record_usage(soft["task_id"], 1, 0, 0, 0, 50_000, 360_000,
                                      host_context_window_tokens=500_000)["action"], "checkpoint_when_safe")
        hard = create_task(self.profile, str(self.root), "pressure-90")
        self.assertEqual(record_usage(hard["task_id"], 1, 0, 0, 0, 50_000, 460_000,
                                      host_context_window_tokens=500_000)["action"], "checkpoint_and_compact")
        conflict = create_task(self.profile, str(self.root), "pressure-conflict")
        conflicted = record_usage(conflict["task_id"], 1, 0, 0, 0, 50_000, 600_000,
                                  host_context_window_tokens=500_000)
        self.assertEqual((conflicted["context_pressure"], conflicted["context_pressure_reason"], conflicted["action"]),
                         (None, "host_values_conflict", "unknown_pressure"))
        with self.assertRaises(TaskRuntimeError):
            record_usage(conflict["task_id"], conflicted["revision"], 0, 0, 0, 50_000, 1,
                         host_context_window_tokens=0)

    def test_user_revision_cancels_old_plan_and_agent_contract(self) -> None:
        task = create_task(self.profile, str(self.root))
        steps = [{"id": "old", "goal": "Old goal", "role": "coder"}]
        set_plan(task["task_id"], 1, steps)
        revised = runtime_call("runtime_task", {"action": "revise_profile", "task_id": task["task_id"], "expected_revision": 2, "profile": dict(self.profile, root_goal="New user goal")}, SkillService())
        self.assertEqual(revised["status"], "replan_required")
        self.assertEqual(get_task(task["task_id"])["state"]["plan"], [])
        self.assertEqual(revised["cancelled_old_steps"], ["old"])
        contract = runtime_call("runtime_plan", {"action": "contract", "role": "coder", "agent_input": {"task_id": task["task_id"]}}, SkillService())
        self.assertFalse(contract["valid"])
        self.assertIn("local_goal", contract["missing_input"])

    def test_selected_skill_versions_persist(self) -> None:
        task = create_task(self.profile, str(self.root))
        selected = runtime_call("runtime_task", {"action": "skills_pin", "task_id": task["task_id"], "expected_revision": 1, "stage": "execution", "selected_skills": ["agent-policy"], "version_lock": {"agent-policy": "0.3.0"}}, SkillService())
        self.assertEqual(selected["selected_skills"], [{"name": "agent-policy", "version": "0.3.0"}])
        saved = checkpoint(task["task_id"], "skills resolved")
        self.assertEqual(restore(task["task_id"], saved["checkpoint_id"])["state"]["selected_skills"]["execution"][0]["name"], "agent-policy")


class JevAdapterTests(unittest.TestCase):
    def test_rank_and_abstain(self) -> None:
        candidates = [{"name": "a", "provides": ["code.implement"], "description": "A"}, {"name": "b", "provides": ["code.implement"], "description": "B"}]
        with patch("jev_adapter._ask", return_value={"answers": {"c0": {"type": "noul", "noul": .9}, "c1": {"type": "noul", "noul": .2}}, "model": "local"}):
            self.assertEqual(rank_skills("Implement feature", candidates)["chosen"], "a")
            self.assertEqual(rank_context("Implement", [{"path": "a.py", "excerpt": "A"}, {"path": "b.py", "excerpt": "B"}]), ["a.py", "b.py"])
        with patch("jev_adapter._ask", return_value={"answers": {"c0": {"type": "noul", "noul": .6}, "c1": {"type": "noul", "noul": .55}}}):
            self.assertIsNone(rank_skills("Implement feature", candidates)["chosen"])
        with patch("jev_adapter._ask", return_value={"answers": {"coverage": {"type": "noul", "noul": .91}}}):
            self.assertEqual(check_verification("Implement", "Test passed")["verdict"], "supported")

    def test_unavailable_fallback(self) -> None:
        with patch("runtime_tools.suggest_task_profile", side_effect=JevUnavailable("offline")):
            result = runtime_call("runtime_intake", {"raw_task": "Review app"}, SkillService())
        self.assertEqual(result["status"], "jev_unavailable")

    def test_low_confidence_profile_abstains(self) -> None:
        response = {"answers": {"task_type": {"choice": "implementation", "probabilities": {"implementation": .53, "debugging": .47}}, "complexity": {"choice": "moderate", "probabilities": {"moderate": .58, "simple": .42}}}}
        with patch("jev_adapter._ask", return_value=response):
            profile = suggest_task_profile("Fix a test", [])
        self.assertEqual(profile["task_type"], "uncertain")
        self.assertEqual(profile["complexity_hint"], "uncertain")


if __name__ == "__main__":
    unittest.main()
