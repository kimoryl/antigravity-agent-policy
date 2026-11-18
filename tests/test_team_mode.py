from __future__ import annotations

import os
import copy
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "hooks"))

from policy_engine import PolicyError, load_policy, resolve  # noqa: E402
from policy_hook import handle as handle_hook  # noqa: E402
from policy_state import release_slot, reserve_slot  # noqa: E402
from runtime_state import create_task  # noqa: E402
from team_engine import concurrency_status, pool_id, report_outcome  # noqa: E402
from team_state import (RuntimeError as TeamRuntimeError, create_team, ensure_member, register_agent,
                        send_message, team_snapshot)


class TeamModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("CODEX_AGENT_POLICY_DATA")
        os.environ["CODEX_AGENT_POLICY_DATA"] = self.folder.name
        self.policy = load_policy()
        self.task = create_task({
            "root_goal": "Team test", "local_goal": "Team state", "task_type": "implementation",
            "constraints": [], "expected_outputs": [], "acceptance_criteria": [],
            "required_capabilities": [], "complexity_hint": "moderate",
        }, str(ROOT), "team-test-session")
        self.team = create_team(self.task["task_id"])

    def tearDown(self) -> None:
        if self.previous is None:
            os.environ.pop("CODEX_AGENT_POLICY_DATA", None)
        else:
            os.environ["CODEX_AGENT_POLICY_DATA"] = self.previous
        self.folder.cleanup()

    def test_team_members_are_project_preferences_with_chinese_names(self) -> None:
        member = ensure_member(self.team["project_id"], "tester")
        self.assertEqual(member["display_name"], "测试")
        self.assertEqual(ensure_member(self.team["project_id"], "tester")["member_id"], member["member_id"])

    def test_three_levels_and_chat_boundary(self) -> None:
        lead = register_agent(self.team["team_id"], "lead-1", "项目经理", 0)
        agent = register_agent(self.team["team_id"], "backend-1", "后端", 1, lead["agent_id"], model="gpt-6-luna", reasoning_effort="medium")
        sub = register_agent(self.team["team_id"], "sub-1", "测试", 2, agent["agent_id"], model="gpt-6-luna", reasoning_effort="low")
        self.assertEqual(sub["depth"], 2)
        with self.assertRaises(TeamRuntimeError):
            register_agent(self.team["team_id"], "sub-2", "测试", 3, sub["agent_id"], model="gpt-6-luna", reasoning_effort="low")
        message = send_message(self.team["team_id"], "backend-1", "@team", "API schema 已更新。", sender_depth=1)
        self.assertEqual(message["to_member"], "@team")
        with self.assertRaises(TeamRuntimeError):
            send_message(self.team["team_id"], "sub-1", "@lead", "跨域问题", sender_depth=2)
        self.assertEqual(len(team_snapshot(self.team["team_id"])["messages"]), 1)

    def test_policy_forces_luna_and_difficulty_effort(self) -> None:
        complex_child = resolve({"role": "coder", "depth": 1, "team_id": self.team["team_id"], "task_profile": {"complexity": "complex"}}, self.policy)
        self.assertEqual(complex_child["model_override"], "gpt-6-luna")
        self.assertEqual(complex_child["reasoning_effort"], "high")
        self.assertTrue(complex_child["team_mode"])
        with self.assertRaises(PolicyError):
            resolve({"role": "coder", "depth": 1, "team_id": self.team["team_id"], "user_model": "gpt-6.1-sol", "task_profile": {"complexity": "simple"}}, self.policy)
        leaf = resolve({"role": "coder", "depth": 2, "team_id": self.team["team_id"], "task_profile": {"complexity": "simple"}}, self.policy)
        self.assertNotIn("delegate", leaf["tool_permissions"])

    def test_shared_slots_and_adaptive_steps(self) -> None:
        pool = pool_id(self.team["project_id"])
        self.assertTrue(reserve_slot("s1", "t", "a", 2, pool_id=pool, team_id=self.team["team_id"], depth=1))
        self.assertTrue(reserve_slot("s2", "t", "b", 2, pool_id=pool, team_id=self.team["team_id"], depth=2))
        self.assertFalse(reserve_slot("s3", "t", "c", 2, pool_id=pool, team_id=self.team["team_id"], depth=2))
        release_slot("a")
        self.assertTrue(reserve_slot("s3", "t", "c", 2, pool_id=pool, team_id=self.team["team_id"], depth=2))
        raised = concurrency_status(self.team["project_id"], self.policy, ready_work=4, waiting=1, outcome="success")
        self.assertLessEqual(raised["current_limit"], self.policy["limits"]["max_parallel_agents"])
        lowered = concurrency_status(self.team["project_id"], self.policy, ready_work=4, waiting=1, outcome="rate_limit")
        self.assertEqual(lowered["current_limit"], self.policy["team"]["min_concurrency"])

    def test_model_capacity_forces_conservative_limit(self) -> None:
        result = concurrency_status(self.team["project_id"], self.policy, ready_work=8, waiting=4, outcome="model_capacity")
        self.assertEqual(result["current_limit"], self.policy["team"]["min_concurrency"])
        self.assertEqual(result["last_failure"], "model_capacity")

    def test_capacity_cooldown_requires_consecutive_successes_and_deduplicates(self) -> None:
        policy = copy.deepcopy(self.policy)
        policy["team"]["capacity_cooldown_seconds"] = 10
        policy["team"]["capacity_success_threshold"] = 2
        event = {
            "team_id": self.team["team_id"], "project_id": self.team["project_id"],
            "failure_kind": "model_capacity", "event_id": "capacity-1", "ready_work": 4, "waiting": 1,
        }
        with patch("team_engine.time.time", return_value=100.0):
            failed = report_outcome(event, policy)
        self.assertEqual(failed["current_limit"], 1)
        self.assertEqual(failed["cooldown_until"], 110.0)
        duplicate = report_outcome(event, policy)
        self.assertEqual(duplicate["failure_streak"], failed["failure_streak"])
        self.assertEqual(duplicate["consecutive_successes"], 0)
        with patch("team_engine.time.time", return_value=105.0):
            cooling = report_outcome({**event, "event_id": "success-cooling", "failure_kind": None}, policy)
        self.assertEqual(cooling["current_limit"], 1)
        self.assertEqual(cooling["consecutive_successes"], 0)
        with patch("team_engine.time.time", return_value=111.0):
            first = report_outcome({**event, "event_id": "success-1", "failure_kind": None}, policy)
            second = report_outcome({**event, "event_id": "success-2", "failure_kind": None}, policy)
        self.assertEqual(first["current_limit"], 1)
        self.assertEqual(second["current_limit"], 2)
        self.assertLessEqual(second["current_limit"], policy["limits"]["max_parallel_agents"])

    def test_concurrent_feedback_is_serialized_and_persisted(self) -> None:
        policy = copy.deepcopy(self.policy)
        policy["team"]["capacity_cooldown_seconds"] = 0
        policy["team"]["capacity_success_threshold"] = 1
        base = {"team_id": self.team["team_id"], "project_id": self.team["project_id"], "failure_kind": None}
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda i: report_outcome({**base, "event_id": f"success-{i}"}, policy), range(4)))
        self.assertTrue(results)
        self.assertLessEqual(results[-1]["current_limit"], policy["limits"]["max_parallel_agents"])
        self.assertEqual(results[-1]["last_failure"], None)

    def test_old_team_concurrency_schema_migrates_backoff_columns(self) -> None:
        from contextlib import closing
        path = Path(self.folder.name) / "state.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""CREATE TABLE team_concurrency (
                pool_id TEXT PRIMARY KEY, current_limit INTEGER NOT NULL,
                min_limit INTEGER NOT NULL, max_limit INTEGER NOT NULL, step INTEGER NOT NULL,
                ready_work INTEGER NOT NULL DEFAULT 0, waiting INTEGER NOT NULL DEFAULT 0,
                failure_streak INTEGER NOT NULL DEFAULT 0, last_failure TEXT, updated_at REAL NOT NULL)""")
            db.execute("INSERT INTO team_concurrency VALUES (?, 2, 1, 3, 1, 0, 0, 0, NULL, 0)", (pool_id(self.team["project_id"]),))
        result = concurrency_status(self.team["project_id"], self.policy, ready_work=4, waiting=1, outcome="model_capacity")
        self.assertIn("cooldown_until", result)
        with closing(sqlite3.connect(path)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(team_concurrency)")}
        self.assertTrue({"last_capacity_failure_at", "consecutive_successes", "cooldown_until"}.issubset(columns))

    def test_hook_blocks_subagent_recursion(self) -> None:
        event = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": "s", "turn_id": "t", "agent_depth": 2, "team_id": self.team["team_id"], "project_id": self.team["project_id"], "agent_id": "sub-1", "tool_use_id": "recursive", "tool_input": {"role": "tester", "task_profile": {"complexity": "simple"}}}
        result = handle_hook(event)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
