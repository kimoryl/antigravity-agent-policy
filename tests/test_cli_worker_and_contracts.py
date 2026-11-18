from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "hooks"))

from cli_worker_runner import (  # noqa: E402
    CREATE_NO_WINDOW,
    build_cli_command,
    cleanup_workspace,
    merge_workspace,
    prepare_workspace,
    run_cli_worker,
)
from policy_hook import handle as handle_hook  # noqa: E402
from policy_state import data_dir  # noqa: E402
from runtime_engine import validate_plan  # noqa: E402
from runtime_state import RuntimeError as TaskRuntimeError, canonical_project_root, create_task  # noqa: E402
import runtime_tools  # noqa: E402
from team_state import create_team, publish_contract, read_contracts, register_agent, send_message  # noqa: E402


class CliWorkerAndContractsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.prev_data = os.environ.get("CODEX_AGENT_POLICY_DATA")
        os.environ["CODEX_AGENT_POLICY_DATA"] = str(Path(self.tmp.name) / "policy_data")
        self.ws = Path(self.tmp.name) / "workspace"
        self.ws.mkdir(parents=True, exist_ok=True)
        (self.ws / "pkg_a").mkdir()
        (self.ws / "pkg_b").mkdir()
        (self.ws / "pkg_a" / "mod_a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
        (self.ws / "pkg_b" / "mod_b.py").write_text("def bar():\n    return 2\n", encoding="utf-8")

        self.task = create_task(
            {
                "root_goal": "CLI worker test",
                "local_goal": "Verify pure CLI orchestration",
                "task_type": "implementation",
                "constraints": [],
                "expected_outputs": [],
                "acceptance_criteria": [],
                "required_capabilities": [],
                "complexity_hint": "complex",
            },
            str(self.ws),
            "cli-worker-test-session",
        )
        self.team = create_team(self.task["task_id"])

    def tearDown(self) -> None:
        if self.prev_data is None:
            os.environ.pop("CODEX_AGENT_POLICY_DATA", None)
        else:
            os.environ["CODEX_AGENT_POLICY_DATA"] = self.prev_data
        self.tmp.cleanup()

    def test_tri_core_cli_models_and_1kb_contract_board(self) -> None:
        lead = register_agent(self.team["team_id"], "lead-h", "调度主控", 0, model="gemini-4-hydrogen-high", reasoning_effort="high")
        w1 = register_agent(self.team["team_id"], "w-code", "后端开发", 1, lead["agent_id"], model="gemini-4-hydrogen-xhigh", reasoning_effort="xhigh")
        w2 = register_agent(self.team["team_id"], "w-verify", "测试验收", 2, w1["agent_id"], model="gpt-6.1-sol", reasoning_effort="high")
        self.assertEqual(w1["model"], "gemini-4-hydrogen-xhigh")
        self.assertEqual(w2["model"], "gpt-6.1-sol")

        with self.assertRaises(TaskRuntimeError):
            register_agent(self.team["team_id"], "w-bad", "未知", 1, lead["agent_id"], model="unknown-random-model")

        # Freeform chat remains blocked at depth 2, but structured <=1KB contracts are allowed
        with self.assertRaises(TaskRuntimeError):
            send_message(self.team["team_id"], "w-verify", "@lead", "长篇自然语言闲聊", sender_depth=2)

        c1 = publish_contract(
            self.team["team_id"],
            "后端开发",
            "pkg_a/mod_a.py",
            "foo",
            {"params": ["x: int"], "returns": "dict[str, int]"},
            sender_depth=2,
        )
        self.assertGreaterEqual(c1["seq"], 1)

        c2 = runtime_tools.call(
            "team_message",
            {
                "action": "publish_contract",
                "team_id": self.team["team_id"],
                "from_role": "前端开发",
                "file_path": "pkg_b/mod_b.py",
                "symbol": "bar",
                "contract": {"consumes": "pkg_a.mod_a.foo", "version": 2},
                "sender_depth": 2,
            },
        )
        self.assertGreater(c2["seq"], c1["seq"])

        incremental = read_contracts(self.team["team_id"], since_seq=c1["seq"])
        self.assertEqual(len(incremental), 1)
        self.assertEqual(incremental[0]["symbol"], "bar")

        # Enforce 1024-byte hard cap
        oversized = {"blob": "x" * 1100}
        with self.assertRaises(TaskRuntimeError):
            publish_contract(self.team["team_id"], "后端开发", "pkg_a/mod_a.py", "foo", oversized)

    def test_nested_write_path_conflict_and_co_occurrence_warning(self) -> None:
        # Parent directory vs child file and case/dot-segment aliases must be caught
        res = validate_plan(
            [
                {"id": "s1", "goal": "Edit pkg_a dir", "role": "coder", "depends_on": [], "expected_outputs": [], "write_paths": ["pkg_a"]},
                {"id": "s2", "goal": "Edit nested file", "role": "coder", "depends_on": [], "expected_outputs": [], "write_paths": ["./PKG_A/mod_a.py" if os.name == "nt" else "./pkg_a/mod_a.py"]},
            ]
        )
        self.assertFalse(res["valid"])
        self.assertEqual(len(res["parallel_write_conflicts"]), 1)

        # Co-occurrence warning check
        from supervisor_engine import _workspace_key

        db_path = data_dir() / "runtime.sqlite3"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        ws_id = _workspace_key(self.ws)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS co_occurrence_edges (workspace_id TEXT, file_a TEXT, file_b TEXT, weight REAL, updated_at REAL, PRIMARY KEY(workspace_id,file_a,file_b))"
        )
        conn.execute(
            "INSERT INTO co_occurrence_edges VALUES (?, ?, ?, ?, ?)",
            (ws_id, "pkg_a/mod_a.py", "pkg_b/mod_b.py", 0.85, 1.0),
        )
        conn.commit()
        conn.close()

        disjoint = validate_plan(
            [
                {"id": "s1", "goal": "A", "role": "coder", "depends_on": [], "expected_outputs": [], "write_files": ["pkg_a/mod_a.py"]},
                {"id": "s2", "goal": "B", "role": "coder", "depends_on": [], "expected_outputs": [], "write_files": ["pkg_b/mod_b.py"]},
            ],
            project_root=str(self.ws),
        )
        self.assertTrue(disjoint["valid"])
        self.assertEqual(len(disjoint["co_occurrence_warnings"]), 1)

    def test_directory_sandbox_isolation_and_write_allowlist_guard(self) -> None:
        prep = prepare_workspace(str(self.ws), ["pkg_a/mod_a.py"], worker_id="worker_alpha")
        self.assertEqual(prep["mode"], "directory_sandbox")
        sandbox = Path(prep["sandbox_dir"])

        # Authorized edit inside sandbox
        (sandbox / "pkg_a" / "mod_a.py").write_text("def foo():\n    return 42\n", encoding="utf-8")
        # Refuses cleanup before merge when dirty
        with self.assertRaises(TaskRuntimeError):
            cleanup_workspace("worker_alpha")

        # Unauthorized edit outside write_paths must be rejected during merge
        (sandbox / "pkg_b" / "mod_b.py").write_text("HACKED\n", encoding="utf-8")
        with self.assertRaises(TaskRuntimeError):
            merge_workspace("worker_alpha")

        # Restore unauthorized file and merge cleanly
        (sandbox / "pkg_b" / "mod_b.py").write_text("def bar():\n    return 2\n", encoding="utf-8")
        merged = merge_workspace("worker_alpha")
        self.assertEqual(merged["changed_files"], ["pkg_a/mod_a.py"])
        self.assertIn("return 42", (self.ws / "pkg_a" / "mod_a.py").read_text(encoding="utf-8"))

        cleaned = cleanup_workspace("worker_alpha")
        self.assertTrue(cleaned["cleaned"])
        self.assertFalse(sandbox.exists())

        # Red-team regression: worker_id traversal must be rejected
        with self.assertRaises(TaskRuntimeError):
            merge_workspace("../escape_worker")
        with self.assertRaises(TaskRuntimeError):
            cleanup_workspace("../escape_worker")

        # Red-team regression: Windows junction / symlink escape inside allowed directory must be blocked
        outside = Path(self.tmp.name) / "outside_secret"
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "victim.py").write_text("SAFE = True\n", encoding="utf-8")
        prep_j = prepare_workspace(str(self.ws), ["pkg_a"], worker_id="worker_junc")
        sb_j = Path(prep_j["sandbox_dir"])
        link_dir = sb_j / "pkg_a" / "link_out"
        created_reparse = False
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(outside), str(link_dir))
            created_reparse = True
        else:
            os.symlink(outside, link_dir)
            created_reparse = True
        if created_reparse:
            (link_dir / "pwned.py").write_text("PWNED = True\n", encoding="utf-8")
            with self.assertRaises(TaskRuntimeError):
                merge_workspace("worker_junc")
        cleanup_workspace("worker_junc", force=True)

    def test_git_worktree_isolation_preserves_uncommitted_diff(self) -> None:
        git_ws = Path(self.tmp.name) / "git_repo"
        git_ws.mkdir()
        flags = CREATE_NO_WINDOW
        subprocess.run(["git", "init"], cwd=git_ws, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, creationflags=flags)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=git_ws, check=True, stdin=subprocess.DEVNULL, creationflags=flags)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=git_ws, check=True, stdin=subprocess.DEVNULL, creationflags=flags)
        (git_ws / "core.py").write_text("X = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=git_ws, check=True, stdin=subprocess.DEVNULL, creationflags=flags)
        subprocess.run(["git", "commit", "-m", "init"], cwd=git_ws, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, creationflags=flags)

        # Uncommitted local edit + untracked file
        (git_ws / "core.py").write_text("X = 2\n", encoding="utf-8")
        (git_ws / "local_note.py").write_text("NOTE = True\n", encoding="utf-8")

        prep = prepare_workspace(str(git_ws), ["core.py"], worker_id="git_worker_1")
        self.assertEqual(prep["mode"], "git_worktree")
        wt = Path(prep["sandbox_dir"])
        self.assertEqual((wt / "core.py").read_text(encoding="utf-8"), "X = 2\n")
        self.assertEqual((wt / "local_note.py").read_text(encoding="utf-8"), "NOTE = True\n")

        (wt / "core.py").write_text("X = 99\n", encoding="utf-8")
        merge_workspace("git_worker_1")
        self.assertEqual((git_ws / "core.py").read_text(encoding="utf-8"), "X = 99\n")
        cleanup_workspace("git_worker_1")

    def test_architect_requires_verbatim_request_and_cloud_blocks_swarm(self) -> None:
        with self.assertRaises(TaskRuntimeError):
            build_cli_command(role_preset="architect", prompt="拆解计划", cwd=str(self.ws))

        cmd_spec = build_cli_command(
            role_preset="architect",
            prompt="输出 JSON 计划",
            raw_user_request="重构认证与计费模块",
            cwd=str(self.ws),
            conversation_id="conv-123",
        )
        self.assertEqual(cmd_spec["model"], "gemini-4-carbon-high")
        self.assertEqual(cmd_spec["stdin_policy"], "DEVNULL_EOF")
        self.assertIn("--conversation", cmd_spec["command"])

        codex_spec = build_cli_command(role_preset="verifier", prompt="运行单测验收", cwd=str(self.ws))
        self.assertEqual(codex_spec["cli_kind"], "codex")
        self.assertEqual(codex_spec["model"], "gpt-6.1-sol")

        # Real silent subprocess execution using Python itself as mock binary
        exec_res = run_cli_worker(
            role_preset="coder",
            prompt="unused",
            cwd=str(self.ws),
            cli_kind="claude",
            binary_override=sys.executable,
            timeout_sec=15,
            dry_run=True,
        )
        self.assertTrue(exec_res["dry_run"])

        # Cloud host blocks /swarm tool
        prev_env = os.environ.get("AGY_RUNTIME_ENV")
        os.environ["AGY_RUNTIME_ENV"] = "cloud"
        try:
            hook_out = handle_hook({"hook_event_name": "PreToolUse", "tool_name": "swarm", "session_id": "s1", "tool_input": {}})
            self.assertEqual(hook_out["hookSpecificOutput"]["permissionDecision"], "deny")
        finally:
            if prev_env is None:
                os.environ.pop("AGY_RUNTIME_ENV", None)
            else:
                os.environ["AGY_RUNTIME_ENV"] = prev_env


if __name__ == "__main__":
    unittest.main()
