"""Isolated quota-monitor v1.3.0 contracts and behavioral regression tests."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import mock_open, patch


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "mcp/quota_monitor_mcp.py"
spec = importlib.util.spec_from_file_location("quota_monitor_test", SERVER)
quota = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quota)
TOOL_NAMES = {"quota_status", "quota_recommend_agent", "quota_check_agent",
              "quota_purify_cli_output", "quota_cognitive_mirror_sniff", "quota_task_guard"}


class QuotaTests(unittest.TestCase):
    def setUp(self):
        quota.clear_quota_cache()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.env = patch.dict(os.environ, {"QUOTA_COGNITIVE_TRACE_STORE": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        # A missed fixture must fail, never reach the network or real credentials.
        network = patch.object(quota.urllib.request, "urlopen", side_effect=AssertionError("network prohibited"))
        network.start()
        self.addCleanup(network.stop)
        self.agy = self.start_patch("fetch_antigravity_quotas", return_value={"ok": True, "models": {"mock": {}}, "pools": {
            "antigravity_gemini": self.pool(90), "antigravity_claude_3p": self.pool(80)}})
        self.codex = self.start_patch("fetch_codex_quotas", return_value={"ok": True, "pool": self.pool(85)})
        self.start_patch("fetch_agent_active_load", return_value={
            name: {"in_progress": 0, "todo": 0, "active_titles": []} for name in quota.AGENTS_META})

    def start_patch(self, name, **kwargs):
        item = patch.object(quota, name, **kwargs)
        result = item.start()
        self.addCleanup(item.stop)
        return result

    @staticmethod
    def pool(percent):
        return {"primary_5h_remaining_percent": percent, "secondary_weekly_remaining_percent": percent,
                "effective_remaining_percent": percent, "health": quota._health_level(percent)}

    def test_codex_model_and_report_pool_policy(self):
        report = quota.call_tool("quota_status", {})
        self.assertEqual(report["agents"]["AGY-CLI"]["model"], "gemini-4-carbon")
        self.assertEqual(report["agents"]["Codex-CLI"]["model"], "gpt-6.1-sol")
        self.assertTrue(report["pools"]["openai_codex"]["shared_chatgpt_codex_pool"])
        self.assertEqual(report["pools"]["openai_codex"]["budget_protection_policy"],
                         "ChatGPT & Codex 共享额度池保护 + CLI 输出净化降噪")

    def test_fetch_codex_live_pool_policy(self):
        # Stop just the fetch mock while keeping all external IO controlled.
        with patch.object(quota.os.path, "exists", return_value=True), \
             patch("builtins.open", mock_open(read_data='{"tokens":{"access_token":"fake"}}')), \
             patch.object(quota.glob, "glob", return_value=[]), \
             patch.object(quota.urllib.request, "urlopen") as request:
            request.return_value.__enter__.return_value.read.return_value = json.dumps({"rate_limit": {
                "primary_window": {"used_percent": 20}, "secondary_window": {"used_percent": 30}}}).encode()
            result = self.original_fetch()
        self.assertTrue(result["ok"])
        self.assertEqual(result["pool"]["effective_remaining_percent"], 70)
        self.assertTrue(result["pool"]["shared_chatgpt_codex_pool"])

    def test_fetch_codex_session_fallback_policy(self):
        session = self.root / "session.jsonl"
        session.write_text(json.dumps({"payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 50}},
            "rate_limits": {"primary": {"used_percent": 10}, "secondary": {"used_percent": 60}}}}), encoding="utf-8")
        with patch.object(quota.os.path, "exists", return_value=False), \
             patch.object(quota.glob, "glob", return_value=[str(session)]):
            result = self.original_fetch()
        self.assertTrue(result["ok"])
        self.assertEqual(result["pool"]["effective_remaining_percent"], 40)
        self.assertEqual(result["pool"]["budget_protection_policy"], quota.CODEX_POOL_POLICY["budget_protection_policy"])

    def test_fetch_codex_unavailable_still_has_policy(self):
        with patch.object(quota.os.path, "exists", return_value=False), patch.object(quota.glob, "glob", return_value=[]):
            result = self.original_fetch()
        self.assertFalse(result["ok"])
        self.assertTrue(result["pool"]["shared_chatgpt_codex_pool"])

    def test_status_legacy_keys_and_raw_models(self):
        report = quota.call_tool("quota_status", {"include_raw_models": True})
        self.assertEqual(set(report), {"timestamp", "routing_policy", "summary_markdown", "agents", "pools",
                                       "codex_latest_session_tokens", "raw_antigravity_models"})
        self.assertEqual(report["raw_antigravity_models"], {"mock": {}})
        self.assertEqual(len(report["agents"]), 3)
    def test_default_route_and_fallback(self):
        result = quota.call_tool("quota_recommend_agent", {"task_description": "开发全栈功能"})
        self.assertEqual(result["recommended_agent"], "AGY-CLI")
        self.assertIn(result["fallback_agent"], ("Codex-CLI", "Opus-CLI"))
        self.assertEqual(len(result["candidates"]), 3)
        think_res = quota.call_tool("quota_recommend_agent", {"task_description": "复杂思考与深度推理方案推演"})
        self.assertEqual(think_res["recommended_agent"], "AGY-CLI")
        self.assertIn("复杂思考", think_res["candidates"][0]["affinity_matched_keywords"])

    def test_memory_reviewer_weekly_selection_and_blocklist(self):
        # Default fixture: codex weekly 85% > claude weekly 80% -> 6.1terra
        res1 = quota.call_tool("quota_recommend_agent", {"task_description": "记忆审查 reconcile"})
        mr1 = res1["memory_reviewer_routing"]
        self.assertEqual(mr1["selected_model"], "gpt-6.1-terra")
        self.assertEqual(mr1["selected_alias"], "6.1terra")
        self.assertIn("gpt-6.1-sol", mr1["blocked_models"])
        self.assertIn("claude-opus-5-5", mr1["blocked_models"])
        self.assertIn("6.1terra", res1["recommendation_reason"])

        # When claude weekly (80%) > codex weekly (40%) -> sonnet5.5
        self.codex.return_value = {"ok": True, "pool": self.pool(40)}
        res2 = quota.call_tool("quota_recommend_agent", {"task_description": "记忆审查 reconcile"})
        mr2 = res2["memory_reviewer_routing"]
        self.assertEqual(mr2["selected_model"], "claude-sonnet-5-5")
        self.assertEqual(mr2["selected_alias"], "sonnet5.5")
        self.assertIn("sonnet5.5", res2["recommendation_reason"])

    def test_specialist_route(self):
        result = quota.call_tool("quota_recommend_agent", {"task_description": "高密度后端 pytest 单元测试"})
        self.assertEqual(result["recommended_agent"], "Codex-CLI")

    def test_preferred_agent_below_minimum_degrades(self):
        self.codex.return_value = {"pool": self.pool(5)}
        result = quota.call_tool("quota_recommend_agent", {"task_description": "开发功能", "preferred_agent": "Codex-CLI"})
        self.assertEqual(result["recommended_agent"], "AGY-CLI")
        codex = next(c for c in result["candidates"] if c["agent"] == "Codex-CLI")
        self.assertEqual(codex["score_breakdown"]["quota_penalty"], -60)

    def test_weekly_pool_conservation_and_exhaustion(self):
        for percent, penalty in ((30, -25), (0, -1000)):
            with self.subTest(percent=percent):
                self.codex.return_value = {"pool": self.pool(percent)}
                result = quota.recommend_agent_for_task("普通任务")
                codex = next(c for c in result["candidates"] if c["agent"] == "Codex-CLI")
                self.assertEqual(codex["score_breakdown"]["quota_penalty"], penalty)

    def test_check_agent_case_and_dispatch(self):
        self.assertTrue(quota.call_tool("quota_check_agent", {"agent_name": "codex-cli"})["can_dispatch"])
        self.codex.return_value = {"pool": self.pool(3)}
        self.assertFalse(quota.call_tool("quota_check_agent", {"agent_name": "Codex-CLI"})["can_dispatch"])
        self.assertFalse(quota.call_tool("quota_check_agent", {"agent_name": "missing"})["ok"])

    def test_pytest_green_compression(self):
        raw = "=== test session starts ===\nplatform win32 -- Python 3.12, pytest-8\n"
        raw += "\n".join(f"tests/test_api.py::test_{i} PASSED [ 50%]" for i in range(200))
        raw += "\ntest_ok ... ok\n=== 201 passed in 2.1s ==="
        result = quota.purify_cli_output(raw)
        self.assertEqual(result["detected_command_type"], "pytest")
        self.assertGreaterEqual(result["compression_ratio_pct"], 60)
        self.assertIn("201 passed", result["purified_output"])
        self.assertIn("platform win32", result["purified_output"])
        self.assertNotIn("PASSED", result["purified_output"])
        self.assertNotIn("... ok", result["purified_output"])

    def test_pytest_failure_assertion_and_frames(self):
        raw = "test_good PASSED\n=== FAILURES ===\n___ test_bad ___\nTraceback (most recent call last):\n"
        raw += '  File "tests/test_api.py", line 42, in test_bad\n> assert actual == 2\nE   AssertionError: 1 != 2\n'
        raw += "FAILED tests/test_api.py::test_bad\n=== 1 failed, 1 passed in 0.1s ==="
        result = quota.purify_cli_output(raw, "pytest")
        for key in ("E   AssertionError", "line 42", "FAILED tests/test_api.py::test_bad", "1 failed, 1 passed"):
            self.assertIn(key, result["purified_output"])
        self.assertNotIn("test_good", result["purified_output"])
        self.assertGreater(result["retained_error_lines"], 0)

    def test_ansi_and_carriage_return(self):
        result = quota.purify_cli_output("\x1b[31mloading 1%\rloading 90%\rdone\x1b[0m\r\nsummary")
        self.assertEqual(result["purified_output"], "done\nsummary")

    def test_git_diff_counts_and_context(self):
        raw = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,20 +1,20 @@\n"
        raw += "\n".join(f" context {i}" for i in range(20)) + "\n-old\n+new\n+extra\n"
        raw += "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-before\n+after"
        result = quota.purify_cli_output(raw)
        self.assertEqual(result["detected_command_type"], "git_diff")
        for key in ("+2 / -1", "+1 / -1", "@@ -1,20", "-old", "+extra", "context folded"):
            self.assertIn(key, result["purified_output"])

    def test_git_status_duplicate_files(self):
        raw = "On branch main\n M app.py\n M app.py\n?? new.py\n"
        result = quota.purify_cli_output(raw)
        self.assertEqual(result["detected_command_type"], "git_status")
        self.assertEqual(result["purified_output"].count("app.py"), 1)
        self.assertIn("repeated 2 times", result["purified_output"])
        self.assertIn("+0 / -0", result["purified_output"])

    def test_git_diff_hunk_payload_resembling_file_headers(self):
        raw = "diff --git a/counter.py b/counter.py\n--- a/counter.py\n+++ b/counter.py\n"
        raw += "@@ -1,2 +1,2 @@\n---counter\n--- payload\n+++counter\n+++ payload"
        result = quota.purify_cli_output(raw, "git_diff")
        self.assertIn("+2 / -2", result["purified_output"])
        self.assertIn("+++counter", result["purified_output"])
        self.assertIn("---counter", result["purified_output"])

    def test_logs_normalize_timestamp_uuid_address_and_line(self):
        raw = "\n".join(f"2026-11-02T12:00:0{i} request {i:08x}-1234-1234-1234-123456789abc at 0x{i+100:x} line {i}" for i in range(6))
        result = quota.purify_cli_output(raw)
        self.assertEqual(len(result["purified_output"].splitlines()), 1)
        self.assertIn("[repeated 6 times]", result["purified_output"])

    def test_budget_prioritizes_errors_and_is_strict(self):
        raw = "\n".join(f"progress item {i}: " + "x" * 60 for i in range(100))
        raw += "\nE   AssertionError: keep me\nWARNING: keep me too\nend summary"
        result = quota.purify_cli_output(raw, max_lines=5, token_budget=40)
        self.assertTrue(result["budget_circuit_breaker_triggered"])
        self.assertLessEqual(result["estimated_purified_tokens"], 40)
        self.assertLessEqual(len(result["purified_output"]), 160)
        self.assertLessEqual(len(result["purified_output"].splitlines()), 5)
        self.assertIn("E   AssertionError: keep me", result["purified_output"])
        self.assertIn("WARNING: keep me too", result["purified_output"])

    def test_budget_extreme_and_empty(self):
        for budget in (1, 2, 10):
            result = quota.purify_cli_output("ERROR " + "a" * 1000, token_budget=budget, max_lines=1)
            self.assertLessEqual(result["estimated_purified_tokens"], budget)
            self.assertLessEqual(len(result["purified_output"].splitlines()), 1)
        result = quota.purify_cli_output("")
        self.assertEqual(result["purified_output"], "")
        self.assertFalse(result["budget_circuit_breaker_triggered"])
        self.assertTrue(0 <= result["compression_ratio_pct"] <= 100)

    def test_build_detection_and_invalid_options(self):
        self.assertEqual(quota.purify_cli_output("Compiling module\nBuild succeeded")["detected_command_type"], "build_log")
        for kwargs in ({"command_hint": "unknown"}, {"max_lines": 0}, {"token_budget": 0}):
            with self.assertRaises(ValueError):
                quota.purify_cli_output("text", **kwargs)

    def test_cognitive_commands_normalize_and_sum(self):
        events = [{"command": f'python "C:/work folder/task{i}.py" --retry {i} --hash {i:08x}', "duration_sec": 2.5} for i in range(4)]
        result = quota.cognitive_mirror_sniff(events)
        self.assertTrue(result["rule_of_three_triggered"])
        self.assertEqual(result["total_events_analyzed"], 4)
        pattern = result["repeated_patterns"][0]
        self.assertEqual(pattern["count"], 4)
        self.assertEqual(pattern["total_duration_sec"], 10)
        self.assertEqual(len(pattern["examples"]), 3)
        for placeholder in ("<PATH>", "<NUM>", "<HEX>"):
            self.assertIn(placeholder, pattern["pattern_signature"])
        proposal = result["automation_proposals"][0]
        self.assertEqual(proposal["recommended_target"], "Skill 一键脚本")
        self.assertIn("发现您近期 4 次手动执行了【", proposal["proactive_prompt_zh"])

    def test_cognitive_correction_and_prompt(self):
        result = quota.cognitive_mirror_sniff([
            {"correction": "请修正 src/one.py 的第 12 行"},
            {"prompt": "请修正 src/two.py 的第 13 行"},
            {"correction": "请修正 src/three.py 的第 14 行"}])
        self.assertTrue(result["rule_of_three_triggered"])
        self.assertEqual(len(result["repeated_patterns"]), 1)
        self.assertEqual(result["automation_proposals"][0]["recommended_target"], "AGENTS.md 默认规则")

    def test_cognitive_workflow_and_categories_do_not_merge(self):
        result = quota.cognitive_mirror_sniff([{"action": "export report", "duration_sec": 5}] * 3 +
                                               [{"command": "export report"}] * 2)
        self.assertEqual(len(result["repeated_patterns"]), 1)
        self.assertEqual(result["automation_proposals"][0]["recommended_target"], "MCP 工具封装")
        self.assertEqual(result["repeated_patterns"][0]["total_duration_sec"], 15)

    def test_cognitive_threshold_and_empty(self):
        self.assertFalse(quota.cognitive_mirror_sniff([{"command": "git status"}] * 2)["rule_of_three_triggered"])
        self.assertTrue(quota.cognitive_mirror_sniff([{"command": "git status"}] * 2, min_repeat_threshold=2)["rule_of_three_triggered"])
        self.assertEqual(quota.cognitive_mirror_sniff()["total_events_analyzed"], 0)
        with self.assertRaises(ValueError):
            quota.cognitive_mirror_sniff(min_repeat_threshold=0)

    def test_cognitive_env_jsonl_read_only(self):
        path = self.root / "trace.jsonl"
        path.write_text("\n".join(json.dumps({"command": f"pytest /tmp/test{i}.py", "duration_sec": 1}) for i in range(3)) + "\ninvalid", encoding="utf-8")
        before = path.read_bytes()
        result = quota.cognitive_mirror_sniff()
        self.assertTrue(result["rule_of_three_triggered"])
        self.assertEqual(result["total_events_analyzed"], 3)
        self.assertEqual(before, path.read_bytes())
        with patch.dict(os.environ, {"QUOTA_COGNITIVE_TRACE_STORE": str(path)}):
            self.assertEqual(quota.cognitive_mirror_sniff()["total_events_analyzed"], 3)

    def test_explicit_events_and_paths_combine_env_is_ignored(self):
        (self.root / "env.json").write_text(json.dumps([{"command": "unrelated"}] * 9), encoding="utf-8")
        other = self.root / "explicit.json"
        other.write_text(json.dumps({"events": [{"command": "git status"}] * 2}), encoding="utf-8")
        result = quota.cognitive_mirror_sniff([{"command": "git status"}], [str(other)])
        self.assertEqual(result["total_events_analyzed"], 3)
        self.assertTrue(result["rule_of_three_triggered"])
        self.assertEqual(quota.cognitive_mirror_sniff([{"command": "x"}])["total_events_analyzed"], 1)

    def test_trace_malformed_missing_and_invalid_duration(self):
        malformed = self.root / "bad.json"
        malformed.write_text("{broken", encoding="utf-8")
        result = quota.cognitive_mirror_sniff([{"command": "x", "duration_sec": value} for value in ("nan", "bad", -4)],
                                             [str(malformed), str(self.root / "missing.json")])
        self.assertEqual(result["repeated_patterns"][0]["total_duration_sec"], 0)

    def test_normalization_word_boundaries_and_uuid(self):
        values = [quota._pattern_signature(f"cmd --session-id=tmp{i} task{i}.py 123e4567-e89b-12d3-a456-42661417400{i} {i}") for i in range(3)]
        self.assertEqual(len(set(values)), 1)
        self.assertEqual(quota._pattern_signature("python3 model6 --v2"), "python3 model6 --v2")

    def test_cognitive_chinese_number_boundaries(self):
        result = quota.cognitive_mirror_sniff([{"correction": f"请重试{i}次并检查第{i}行"} for i in range(3)])
        self.assertTrue(result["rule_of_three_triggered"])
        self.assertEqual(result["repeated_patterns"][0]["pattern_signature"], "请重试<NUM>次并检查第<NUM>行")

    def test_trace_directory_access_error_is_ignored(self):
        with patch.object(quota.Path, "iterdir", side_effect=PermissionError("test denied")):
            self.assertEqual(quota.cognitive_mirror_sniff()["total_events_analyzed"], 0)

    def test_trace_json_variants_oversize_and_nonregular_are_skipped(self):
        (self.root / "one.json").write_text('{"command":"git status"}', encoding="utf-8")
        (self.root / "two.json").write_text('[{"command":"git status"},{"command":"git status"}]', encoding="utf-8")
        (self.root / "invalid_events.json").write_text('{"events":null}', encoding="utf-8")
        (self.root / "oversize.json").write_text(" " * 2_000_001, encoding="utf-8")
        (self.root / "directory.json").mkdir()
        result = quota.cognitive_mirror_sniff()
        self.assertEqual(result["total_events_analyzed"], 3)
        self.assertTrue(result["rule_of_three_triggered"])

    def test_line_budget_alone_triggers_and_keeps_tail(self):
        raw = "header\n" + "\n".join(f"item {i}" for i in range(20)) + "\nfinal summary"
        result = quota.purify_cli_output(raw, max_lines=6, token_budget=1000)
        self.assertTrue(result["budget_circuit_breaker_triggered"])
        self.assertEqual(len(result["purified_output"].splitlines()), 6)
        self.assertIn("header", result["purified_output"])
        self.assertIn("final summary", result["purified_output"])

    def test_manifest_schema_and_exports(self):
        manifest = json.loads((ROOT / "mod.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "1.4.0")
        self.assertIs(quota.TOOLS, quota.TOOLS_SCHEMA)
        self.assertIs(quota.call_tool, quota.handle_tool_call)
        self.assertEqual({t["name"] for t in quota.TOOLS}, TOOL_NAMES)
        self.assertEqual(manifest["mcp_tools"], quota.TOOLS)
        for tool in quota.TOOLS:
            self.assertEqual(tool["parameters"], tool["inputSchema"])

    def test_stdio_initialize_list_and_new_calls_subprocess(self):
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                        "name": "quota_purify_cli_output", "arguments": {"raw_output": "ok"}}},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                        "name": "quota_cognitive_mirror_sniff", "arguments": {"events": [{"command": "git status"}] * 3}}}]
        result = subprocess.run([sys.executable, "-X", "utf8", str(SERVER)],
                                input="\n".join(json.dumps(r) for r in requests) + "\n", text=True, encoding="utf-8",
                                capture_output=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4])
        self.assertEqual(replies[0]["result"]["serverInfo"]["version"], "1.4.0")
        self.assertEqual(len(replies[1]["result"]["tools"]), 6)
        for reply in replies[2:]:
            self.assertFalse(reply["result"]["isError"])
            self.assertTrue(json.loads(reply["result"]["content"][0]["text"])["ok"])

    def test_stdio_legacy_calls_use_mock_quotas(self):
        requests = [{"id": i, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
                    for i, (name, arguments) in enumerate([
                        ("quota_status", {}), ("quota_recommend_agent", {"task_description": "开发功能"}),
                        ("quota_check_agent", {"agent_name": "Codex-CLI"})])]
        output = io.StringIO()
        with patch.object(sys, "argv", [str(SERVER)]), patch.object(sys, "stdin", io.StringIO(
                "\n".join(json.dumps(r) for r in requests))), patch.object(sys, "stdout", output):
            quota.main()
        replies = [json.loads(line)["result"] for line in output.getvalue().splitlines()]
        self.assertTrue(all(not r["isError"] for r in replies))
        payloads = [json.loads(r["content"][0]["text"]) for r in replies]
        self.assertIn("agents", payloads[0])
        self.assertEqual(payloads[1]["recommended_agent"], "AGY-CLI")
        self.assertTrue(payloads[2]["can_dispatch"])

    def test_gemini_tri_core_submodels_and_routing(self):
        report = quota.call_tool("quota_status", {})
        submodels = report["agents"]["AGY-CLI"]["submodels"]
        self.assertEqual(submodels["deep_reasoning_core"], "gemini-4-carbon-high")
        self.assertEqual(submodels["prose_engine"], "gemini-4-argon-high")
        self.assertEqual(submodels["standard_coding_core"], "gemini-4-hydrogen-high")
        self.assertEqual(submodels["high_density_coding_core"], "gemini-4-hydrogen-xhigh")
        self.assertEqual(submodels["fast_explore_core"], "gemini-4-hydrogen-medium")

        r_prose = quota.recommend_gemini_submodel("撰写正式工程报告与招投标叙事大纲", 85.0)
        self.assertEqual(r_prose["selected_model"], "gemini-4-argon-high")
        self.assertEqual(r_prose["role"], "prose_engine")
        self.assertFalse(r_prose["pool_guard_triggered"])

        r_deep = quota.recommend_gemini_submodel("跨模块复杂重构与核心算法建模", 85.0)
        self.assertEqual(r_deep["selected_model"], "gemini-4-carbon-high")
        self.assertEqual(r_deep["role"], "deep_reasoning_core")
        self.assertFalse(r_deep["pool_guard_triggered"])

        r_fast = quota.recommend_gemini_submodel("全库扫描摸底与伴飞小脚本蒸馏", 85.0)
        self.assertEqual(r_fast["selected_model"], "gemini-4-hydrogen-medium")
        self.assertEqual(r_fast["role"], "fast_explore_core")

        r_std = quota.recommend_gemini_submodel("实现用户设置页面常规模块编码与渲染脚本", 85.0)
        self.assertEqual(r_std["selected_model"], "gemini-4-hydrogen-high")
        self.assertEqual(r_std["role"], "standard_coding_core")

        # <30% 5h pool guard downgrades non-security tasks to hydrogen-medium
        r_guard_prose = quota.recommend_gemini_submodel("撰写正式工程报告", 24.5)
        self.assertTrue(r_guard_prose["pool_guard_triggered"])
        self.assertEqual(r_guard_prose["original_model"], "gemini-4-argon-high")
        self.assertEqual(r_guard_prose["selected_model"], "gemini-4-hydrogen-medium")
        self.assertEqual(r_guard_prose["role"], "fast_explore_core")

        r_guard_refactor = quota.recommend_gemini_submodel("跨模块复杂重构与核心算法优化", 18.0)
        self.assertTrue(r_guard_refactor["pool_guard_triggered"])
        self.assertEqual(r_guard_refactor["selected_model"], "gemini-4-hydrogen-medium")

        # Core security / deadlock bypasses <30% downgrade
        r_sec = quota.recommend_gemini_submodel("修复高危安全漏洞与并发死锁竞态", 18.0)
        self.assertFalse(r_sec["pool_guard_triggered"])
        self.assertTrue(r_sec["is_core_security_or_deadlock"])
        self.assertEqual(r_sec["selected_model"], "gemini-4-carbon-high")
        self.assertEqual(r_sec["role"], "deep_reasoning_core")

        # Integrated inside quota_recommend_agent
        self.agy.return_value = {
            "ok": True,
            "models": {},
            "pools": {"antigravity_gemini": self.pool(22), "antigravity_claude_3p": self.pool(80)},
        }
        rec = quota.call_tool("quota_recommend_agent", {"task_description": "编写正式工程报告与人设提炼"})
        self.assertIn("gemini_submodel_routing", rec)
        self.assertTrue(rec["gemini_submodel_routing"]["pool_guard_triggered"])
        self.assertEqual(rec["gemini_submodel_routing"]["selected_model"], "gemini-4-hydrogen-medium")

    def test_select_gemini_quota_bucket_and_fetch_fallback(self):
        # 1. Prefer gemini-4-* over legacy buckets (even when 0.0% remaining!)
        b_v4 = quota._select_gemini_quota_bucket({
            "gemini-2.5-pro": {"remaining_percent": 95.0},
            "gemini-4-carbon-high": {"remaining_percent": 0.0},
        })
        self.assertEqual(b_v4["remaining_percent"], 0.0)

        # 2. Custom gemini-4-* variant
        b_v4_custom = quota._select_gemini_quota_bucket({
            "gemini-4-experimental": {"remaining_percent": 61.5},
            "gemini-3-flash": {"remaining_percent": 88.0},
        })
        self.assertEqual(b_v4_custom["remaining_percent"], 61.5)

        # 3. Legacy fallback order: gemini-3.8-flash-tiered -> gemini-2.5-pro -> gemini-3-flash
        self.assertEqual(
            quota._select_gemini_quota_bucket({"gemini-3-flash": {"remaining_percent": 42.0}})["remaining_percent"],
            42.0,
        )

        # 4. Arbitrary gemini-* bucket fallback (avoids misreporting 100% when old models retire)
        self.assertEqual(
            quota._select_gemini_quota_bucket({
                "claude-opus-5-5-high": {"remaining_percent": 70.0},
                "gemini-next-ultra": {"remaining_percent": 33.3},
            })["remaining_percent"],
            33.3,
        )

        # 5. End-to-end fetch_antigravity_quotas with gemini-4-hydrogen-high bucket
        class _FakeResp:
            def __init__(self, payload):
                self._data = json.dumps(payload).encode("utf-8")
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return self._data

        responses = [
            _FakeResp({"groups": []}),
            _FakeResp({"buckets": [
                {"modelId": "gemini-4-hydrogen-high", "remainingFraction": 0.27, "resetTime": "2026-11-14T00:00:00Z"},
            ]}),
        ]
        with patch.object(quota, "_read_windows_keyring_cred", return_value={"token": {"access_token": "tok"}}), \
             patch.object(quota.os.path, "exists", return_value=False), \
             patch.object(quota.urllib.request, "urlopen", side_effect=responses):
            live_res = self.original_agy_fetch()
        self.assertTrue(live_res["ok"])
        self.assertEqual(live_res["pools"]["antigravity_gemini"]["primary_5h_remaining_percent"], 27.0)
        self.assertEqual(live_res["pools"]["antigravity_gemini"]["effective_remaining_percent"], 27.0)

    def test_p1_3_cross_pool_isolation_unavailable_status_cache_tail_and_jsonrpc_errors(self):
        class _FakeResp:
            def __init__(self, payload):
                self._data = json.dumps(payload).encode("utf-8")
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return self._data

        # 1. Cross-pool bucket isolation: Gemini 5h/weekly buckets must NEVER overwrite Claude 3P pool
        responses = [
            _FakeResp({
                "groups": [
                    {
                        "groupId": "gemini-pro-group",
                        "name": "Gemini Family",
                        "modelIds": ["gemini-4-carbon-high"],
                        "buckets": [
                            {"bucketId": "gemini-5h", "window": "5h", "remainingFraction": 0.99},
                            {"bucketId": "gemini-weekly", "window": "weekly", "remainingFraction": 0.98},
                        ],
                    },
                    {
                        "groupId": "claude-3p-group",
                        "name": "Claude 3P",
                        "buckets": [
                            {"bucketId": "3p-weekly", "window": "weekly", "remainingFraction": 0.42},
                        ],
                    },
                ]
            }),
            _FakeResp({
                "buckets": [
                    {"modelId": "claude-opus-5-5-high", "remainingFraction": 0.65, "resetTime": "2026-11-15T12:00:00Z"},
                    {"modelId": "gemini-4-carbon-high", "remainingFraction": 0.91, "resetTime": "2026-11-15T12:00:00Z"},
                ]
            }),
        ]
        quota.clear_quota_cache()
        with patch.object(quota, "_read_windows_keyring_cred", return_value={"token": {"access_token": "tok"}}), \
             patch.object(quota.os.path, "exists", return_value=False), \
             patch.object(quota.urllib.request, "urlopen", side_effect=responses) as mock_url:
            res = self.original_agy_fetch()
            # Second call hits 15s TTL memory cache without extra HTTP calls
            res_cached = self.original_agy_fetch()
            self.assertEqual(mock_url.call_count, 2)
        self.assertEqual(res, res_cached)
        c_pool = res["pools"]["antigravity_claude_3p"]
        self.assertTrue(c_pool["available"])
        self.assertEqual(c_pool["status"], "ok")
        self.assertEqual(c_pool["primary_5h_remaining_percent"], 65.0)
        self.assertEqual(c_pool["secondary_weekly_remaining_percent"], 42.0)
        self.assertEqual(c_pool["effective_remaining_percent"], 42.0)

        # 2. Missing/timeout pool never lies 100% / healthy; explicitly reports available=False, status="unavailable"
        quota.clear_quota_cache()
        with patch.object(quota, "_read_windows_keyring_cred", return_value=None):
            fail_agy = self.original_agy_fetch(use_cache=False)
        self.assertFalse(fail_agy["ok"])
        for pk in ("antigravity_claude_3p", "antigravity_gemini"):
            self.assertFalse(fail_agy["pools"][pk]["available"])
            self.assertEqual(fail_agy["pools"][pk]["status"], "unavailable")
            self.assertEqual(fail_agy["pools"][pk]["health"], "unavailable")
            self.assertIsNone(fail_agy["pools"][pk]["effective_remaining_percent"])

        # 3. Single fetch for include_raw_models=True (no double fetch)
        self.agy.reset_mock()
        rep = quota.call_tool("quota_status", {"include_raw_models": True})
        self.assertEqual(self.agy.call_count, 1)
        self.assertIn("raw_antigravity_models", rep)

        # 4. Reverse streaming tail reader on multi-chunk JSONL
        big_jsonl = self.root / "huge_session.jsonl"
        with open(big_jsonl, "w", encoding="utf-8") as f:
            for i in range(1500):
                f.write(json.dumps({"seq": i, "msg": "padding-" + ("x" * 100)}) + "\n")
            f.write(json.dumps({
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"total_tokens": 9876}},
                    "rate_limits": {"primary": {"used_percent": 25}, "secondary": {"used_percent": 35}},
                }
            }) + "\n")
        tail_lines = list(quota._iter_file_lines_reverse(str(big_jsonl), chunk_size=4096))
        self.assertIn('"token_count"', tail_lines[0])

        # 5. Standard JSON-RPC error codes (-32700, -32600, -32601, -32602)
        self.assertEqual(quota._handle_jsonrpc_line("{bad json")["error"]["code"], -32700)
        self.assertEqual(quota._handle_jsonrpc_line('{"jsonrpc": "2.0", "id": 1}')["error"]["code"], -32600)
        self.assertEqual(quota._handle_jsonrpc_line('{"jsonrpc": "2.0", "id": 2, "method": "no_such"}')["error"]["code"], -32601)
        self.assertEqual(quota._handle_jsonrpc_line('{"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": []}')["error"]["code"], -32602)
        self.assertIsNone(quota._handle_jsonrpc_line('{"jsonrpc": "2.0", "method": "notifications/initialized"}'))

    def test_task_budget_guard_delta_rollover_reject_and_180s_stall(self):
        # Start task at 90% Gemini remaining (10% used) and 85% Codex remaining (15% used)
        started = quota.call_tool(
            "quota_task_guard",
            {
                "action": "start",
                "task_id": "t-guard-1",
                "max_gemini_5h_delta_pct": 15.0,
                "max_codex_5h_delta_pct": 10.0,
                "max_reject_rounds": 2,
                "stall_timeout_sec": 180,
            },
        )
        self.assertTrue(started["ok"])
        self.assertFalse(started["tripped"])

        # Consume 8% Gemini (remaining 82%) -> within 15% cap
        self.agy.return_value = {
            "ok": True,
            "models": {},
            "pools": {"antigravity_gemini": self.pool(82), "antigravity_claude_3p": self.pool(80)},
        }
        chk_ok = quota.call_tool("quota_task_guard", {"action": "check", "task_id": "t-guard-1"})
        self.assertFalse(chk_ok["tripped"])
        self.assertAlmostEqual(chk_ok["gemini_delta_pct"], 8.0)

        # Simulate 5h window rollover: reset timestamp changes and used resets to 9% (remaining 91%)
        # Total task consumption = 8% (banked) + 9% (new window) = 17% > 15% cap -> trips!
        rolled_pool = {**self.pool(91), "primary_5h_reset_local": "2026-11-16 21:00:00"}
        self.agy.return_value = {
            "ok": True,
            "models": {},
            "pools": {"antigravity_gemini": rolled_pool, "antigravity_claude_3p": self.pool(80)},
        }
        chk_trip = quota.call_tool("quota_task_guard", {"action": "check", "task_id": "t-guard-1"})
        self.assertTrue(chk_trip["tripped"])
        self.assertEqual(chk_trip["decision"], "circuit_break")
        self.assertAlmostEqual(chk_trip["gemini_delta_pct"], 17.0)

        # Rejection loop escalation to gemini-4-carbon-high at round 2
        quota.call_tool("quota_task_guard", {"action": "start", "task_id": "t-reject", "max_reject_rounds": 2})
        r1 = quota.call_tool("quota_task_guard", {"action": "record_reject", "task_id": "t-reject", "reject_reason": "type mismatch"})
        self.assertFalse(r1["tripped"])
        r2 = quota.call_tool("quota_task_guard", {"action": "record_reject", "task_id": "t-reject", "reject_reason": "assertion failed"})
        self.assertTrue(r2["tripped"])
        self.assertEqual(r2["escalate_to"], "gemini-4-carbon-high")

        # 180s zero-token mtime stall detection & alive PID check
        log_file = self.root / "worker.log"
        log_file.write_text("booting...\n", encoding="utf-8")
        with patch.object(quota.time, "time", return_value=1000.0), patch.object(
            quota, "_latest_watch_mtime", return_value=1000.0
        ):
            quota.call_tool(
                "quota_task_guard",
                {"action": "start", "task_id": "t-stall", "stall_timeout_sec": 180, "watch_paths": [str(log_file)]},
            )
        with patch.object(quota.time, "time", return_value=1205.0), patch.object(
            quota, "_latest_watch_mtime", return_value=1000.0
        ):
            stalled = quota.call_tool(
                "quota_task_guard",
                {"action": "check", "task_id": "t-stall", "pid": os.getpid(), "watch_paths": [str(log_file)]},
            )
        self.assertTrue(stalled["tripped"])
        self.assertTrue(stalled["pid_alive"])
        self.assertEqual(stalled["stall_kind"], "process_hung_stall")

        reset_res = quota.call_tool("quota_task_guard", {"action": "reset", "task_id": "t-stall"})
        self.assertTrue(reset_res["removed"])

        # Red-team regressions:
        # 1) Unavailable telemetry (60% used -> 62% used -> unavailable -> 62% used) must equal 2.0%, never 64.0%
        self.agy.return_value = {"ok": True, "models": {}, "pools": {"antigravity_gemini": self.pool(40), "antigravity_claude_3p": self.pool(80)}}
        quota.call_tool("quota_task_guard", {"action": "start", "task_id": "t-unavail"})
        self.agy.return_value = {"ok": True, "models": {}, "pools": {"antigravity_gemini": self.pool(38), "antigravity_claude_3p": self.pool(80)}}
        self.assertAlmostEqual(quota.call_tool("quota_task_guard", {"action": "check", "task_id": "t-unavail"})["gemini_delta_pct"], 2.0)
        self.agy.return_value = {"ok": False, "models": {}, "pools": {"antigravity_gemini": {"available": False, "status": "unavailable"}, "antigravity_claude_3p": self.pool(80)}}
        self.assertAlmostEqual(quota.call_tool("quota_task_guard", {"action": "check", "task_id": "t-unavail"})["gemini_delta_pct"], 2.0)
        self.agy.return_value = {"ok": True, "models": {}, "pools": {"antigravity_gemini": self.pool(38), "antigravity_claude_3p": self.pool(80)}}
        self.assertAlmostEqual(quota.call_tool("quota_task_guard", {"action": "check", "task_id": "t-unavail"})["gemini_delta_pct"], 2.0)

        # 2) Sub-1% rollover without reset timestamp (0.5% -> 0.9% -> rollover 0.1% => 0.4% + 0.1% = 0.5%)
        self.agy.return_value = {"ok": True, "models": {}, "pools": {"antigravity_gemini": self.pool(99.5), "antigravity_claude_3p": self.pool(80)}}
        quota.call_tool("quota_task_guard", {"action": "start", "task_id": "t-sub1"})
        self.agy.return_value = {"ok": True, "models": {}, "pools": {"antigravity_gemini": self.pool(99.1), "antigravity_claude_3p": self.pool(80)}}
        self.assertAlmostEqual(quota.call_tool("quota_task_guard", {"action": "check", "task_id": "t-sub1"})["gemini_delta_pct"], 0.4)
        self.agy.return_value = {"ok": True, "models": {}, "pools": {"antigravity_gemini": self.pool(99.9), "antigravity_claude_3p": self.pool(80)}}
        self.assertAlmostEqual(quota.call_tool("quota_task_guard", {"action": "check", "task_id": "t-sub1"})["gemini_delta_pct"], 0.5)

        # 3) Late-arriving reset timestamp in same window must not double-bank
        self.agy.return_value = {"ok": True, "models": {}, "pools": {"antigravity_gemini": self.pool(90), "antigravity_claude_3p": self.pool(80)}}
        quota.call_tool("quota_task_guard", {"action": "start", "task_id": "t-late-reset"})
        self.agy.return_value = {"ok": True, "models": {}, "pools": {"antigravity_gemini": {**self.pool(81), "primary_5h_reset_local": "2026-11-16 16:00:00"}, "antigravity_claude_3p": self.pool(80)}}
        self.assertAlmostEqual(quota.call_tool("quota_task_guard", {"action": "check", "task_id": "t-late-reset"})["gemini_delta_pct"], 9.0)
        self.agy.return_value = {"ok": True, "models": {}, "pools": {"antigravity_gemini": {**self.pool(72), "primary_5h_reset_local": "2026-11-16 16:00:00"}, "antigravity_claude_3p": self.pool(80)}}
        self.assertAlmostEqual(quota.call_tool("quota_task_guard", {"action": "check", "task_id": "t-late-reset"})["gemini_delta_pct"], 18.0)

        # 4) Progress-aware long task: 4 steps x 11% burn = 44% total (>15% per-step cap), NEVER trips when advance_step is called!
        self.agy.return_value = {"ok": True, "models": {}, "pools": {"antigravity_gemini": self.pool(90), "antigravity_claude_3p": self.pool(80)}}
        quota.call_tool("quota_task_guard", {"action": "start", "task_id": "t-long-marathon", "max_step_gemini_delta_pct": 15.0, "min_gemini_reserve_pct": 10.0})
        for idx, rem in enumerate((79, 68, 57, 46), start=1):
            self.agy.return_value = {"ok": True, "models": {}, "pools": {"antigravity_gemini": self.pool(rem), "antigravity_claude_3p": self.pool(80)}}
            # 1 single rejection on an earlier step must also reset upon advance_step
            if idx in (1, 3):
                rej = quota.call_tool("quota_task_guard", {"action": "record_reject", "task_id": "t-long-marathon"})
                self.assertFalse(rej["tripped"])
            chk = quota.call_tool("quota_task_guard", {"action": "check", "task_id": "t-long-marathon"})
            self.assertFalse(chk["tripped"])
            self.assertEqual(chk["decision"], "proceed")
            self.assertAlmostEqual(chk["step_gemini_delta_pct"], 11.0)
            adv = quota.call_tool("quota_task_guard", {"action": "advance_step", "task_id": "t-long-marathon", "step_id": f"step_{idx}"})
            self.assertEqual(adv["completed_steps"], idx)
            self.assertEqual(adv["step_gemini_delta_pct"], 0.0)
            self.assertEqual(adv["reject_rounds"], 0)
        final_chk = quota.call_tool("quota_task_guard", {"action": "check", "task_id": "t-long-marathon"})
        self.assertFalse(final_chk["tripped"])
        self.assertAlmostEqual(final_chk["total_gemini_delta_pct"], 44.0)

        # 5) Reserve floor (<10% remaining) parks gracefully for refill instead of tripping error
        self.agy.return_value = {"ok": True, "models": {}, "pools": {"antigravity_gemini": {**self.pool(8.5), "primary_5h_reset_in_minutes": 42.0}, "antigravity_claude_3p": self.pool(80)}}
        quota.call_tool("quota_task_guard", {"action": "advance_step", "task_id": "t-long-marathon", "step_id": "step_pre_floor"})
        parked = quota.call_tool("quota_task_guard", {"action": "check", "task_id": "t-long-marathon"})
        self.assertFalse(parked["tripped"])
        self.assertTrue(parked["paused_for_refill"])
        self.assertEqual(parked["decision"], "park_for_refill")
        self.assertEqual(parked["refill_wait_minutes"], 42.0)


# Preserve the real function before per-test mocks replace the module attribute.
QuotaTests.original_fetch = staticmethod(quota.fetch_codex_quotas)
QuotaTests.original_agy_fetch = staticmethod(quota.fetch_antigravity_quotas)

if __name__ == "__main__":
    unittest.main(verbosity=2)
