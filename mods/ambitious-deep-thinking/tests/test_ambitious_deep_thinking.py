"""Comprehensive unit tests for Ambitious Deep Thinking Mod v1.2.0 (Altitude · Depth · Breadth + Red-Blue Confrontation)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MOD_DIR = Path(__file__).resolve().parents[1]
SERVER_PATH = MOD_DIR / "mcp" / "mcp_server.py"
MANIFEST_PATH = MOD_DIR / "mod.json"


class AmbitiousDeepThinkingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("ambitious_mcp_server", SERVER_PATH)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def setUp(self):
        self.mod._SESSION_LEDGER.clear()
        self.mod._SESSION_STORE_PATH = None

    def test_01_manifest_and_tools_list_schema_parity(self):
        self.assertEqual(self.manifest["version"], "1.2.0")
        self.assertEqual(self.mod.VERSION, "1.2.0")
        server_tools = {t["name"]: t["inputSchema"] for t in self.mod.tools_list()}
        manifest_tools = {t["name"]: t["inputSchema"] for t in self.manifest["mcp_tools"]}
        self.assertEqual(set(server_tools), {
            "ambitious_thinking_guide",
            "ambitious_prompt_rewrite",
            "ambitious_grill_alignment",
            "ambitious_sequential_reflect",
        })
        self.assertEqual(server_tools, manifest_tools)

    def test_02_domain_archetype_detection_software(self):
        key, arch = self.mod._detect_domain_archetype("重构后端 API 接口与数据库并发事务锁", "")
        self.assertEqual(key, "software_architecture")
        self.assertIn("AST", arch["unasked_expert_action"])
        self.assertIn("altitude_loop", arch)
        self.assertIn("red_blue_matrix", arch)

    def test_03_domain_archetype_detection_multiagent_mcp(self):
        key, arch = self.mod._detect_domain_archetype("优化多智能体子代理与 MCP 工具 Token 额度消耗", "")
        self.assertEqual(key, "multiagent_mcp")
        self.assertIn("JSON-RPC", arch["rawest_material"])

    def test_04_domain_archetype_detection_ui_visual_motion(self):
        key, arch = self.mod._detect_domain_archetype("设计 OKLCH 色彩系统与 SVG 物理弹簧动效组件", "")
        self.assertEqual(key, "ui_visual_motion")
        self.assertIn("OKLCH", arch["unasked_expert_action"])

    def test_05_domain_archetype_detection_distributed_systems(self):
        key, arch = self.mod._detect_domain_archetype("高并发分布式存储引擎 Raft 共识与 P99 尾延迟调优", "")
        self.assertEqual(key, "distributed_systems_infra")
        self.assertIn("WAL", arch["rawest_material"])

    def test_06_domain_archetype_detection_product_strategy(self):
        key, arch = self.mod._detect_domain_archetype("撰写用户痛点调研与商业化 MVP 产品 PRD 路线图", "")
        self.assertEqual(key, "product_data_strategy")
        self.assertIn("Short-Seller", arch["red_team_method"])

    def test_07_thinking_guide_sections(self):
        for sec in (
            "skill",
            "reference",
            "meta_prompt",
            "grill_protocol",
            "product_4step",
            "altitude_depth_breadth",
            "red_blue_protocol",
            "all",
        ):
            res = self.mod.call_tool("ambitious_thinking_guide", {"section": sec})
            self.assertEqual(res["version"], "1.2.0")
            self.assertIn("meta_prompt_zh", res)
            self.assertIn("meta_prompt_en", res)
        all_res = self.mod.call_tool("ambitious_thinking_guide", {"section": "all"})
        self.assertIn("skill_markdown", all_res)
        self.assertIn("reference_markdown", all_res)
        self.assertIn("grill_protocol_markdown", all_res)
        self.assertIn("product_4step_markdown", all_res)
        self.assertIn("altitude_depth_breadth_markdown", all_res)
        self.assertIn("red_blue_protocol_markdown", all_res)

    def test_08_grill_alignment_ambiguous_short_task(self):
        res = self.mod.call_tool("ambitious_grill_alignment", {"raw_task": "帮我改一下后端接口"})
        self.assertTrue(res["ok"])
        self.assertGreaterEqual(res["ambiguity_score"], 0.4)
        self.assertFalse(res["ready_to_execute"])
        self.assertEqual(len(res["grill_questions"]), 3)
        for q in res["grill_questions"]:
            self.assertTrue(q["options"][0].startswith("(Recommended)"))
            self.assertEqual(q["recommended_default"], q["options"][0])
        self.assertEqual(len(res["ask_question_payload"]["questions"]), 3)

    def test_09_grill_alignment_context_aware_filtering(self):
        res = self.mod.call_tool(
            "ambitious_grill_alignment",
            {
                "raw_task": "重构后端数据库接口",
                "known_context": "要求 100% 向后兼容既有签名，仅使用纯标准库 stdlib，并通过隔离单测 unittest 验证",
                "max_questions": 3,
            },
        )
        self.assertTrue(res["ok"])
        self.assertGreaterEqual(len(res["auto_aligned_dimensions"]), 2)
        self.assertTrue(res["ready_to_execute"])
        dims = [q["dimension"] for q in res["grill_questions"]]
        self.assertTrue(any("预算" in d or "容错" in d or "痛点" in d for d in dims))

    def test_10_grill_alignment_max_questions_clamping(self):
        res1 = self.mod.call_tool("ambitious_grill_alignment", {"raw_task": "优化 UI 组件", "max_questions": 1})
        self.assertEqual(len(res1["grill_questions"]), 1)
        res4 = self.mod.call_tool("ambitious_grill_alignment", {"raw_task": "优化 UI 组件", "max_questions": 9})
        self.assertLessEqual(len(res4["grill_questions"]), 4)

    def test_11_grill_alignment_empty_task_raises(self):
        with self.assertRaises(ValueError):
            self.mod.call_tool("ambitious_grill_alignment", {"raw_task": "   "})

    def test_12_sequential_reflect_stateful_progression(self):
        s1 = self.mod.call_tool(
            "ambitious_sequential_reflect",
            {"task_or_hypothesis": "构建零守护进程混合索引", "thought_step": 1, "total_thoughts": 3, "session_id": "s_prog"},
        )
        self.assertTrue(s1["ok"])
        self.assertTrue(s1["state"]["next_thought_needed"])
        self.assertEqual(s1["session_ledger"]["history_count"], 1)
        self.assertIn("altitude_depth_breadth_matrix", s1)
        self.assertIn("red_blue_confrontation", s1)

        s2 = self.mod.call_tool(
            "ambitious_sequential_reflect",
            {"task_or_hypothesis": "引入 SQLite FTS5 与 384d 向量", "thought_step": 2, "total_thoughts": 3, "session_id": "s_prog"},
        )
        self.assertEqual(s2["session_ledger"]["history_count"], 2)

        s3 = self.mod.call_tool(
            "ambitious_sequential_reflect",
            {"task_or_hypothesis": "完成 RRF 融合并通过 35 项测试", "thought_step": 3, "total_thoughts": 3, "session_id": "s_prog"},
        )
        self.assertFalse(s3["state"]["next_thought_needed"])
        self.assertEqual(s3["session_ledger"]["history_count"], 3)
        self.assertEqual(s3["session_ledger"]["branches"]["main"], [1, 2, 3])

    def test_13_sequential_reflect_branching_and_revision(self):
        self.mod.call_tool(
            "ambitious_sequential_reflect",
            {"task_or_hypothesis": "Step 1 主干", "thought_step": 1, "total_thoughts": 3, "session_id": "s_rev"},
        )
        self.mod.call_tool(
            "ambitious_sequential_reflect",
            {"task_or_hypothesis": "Step 2 原假设", "thought_step": 2, "total_thoughts": 3, "session_id": "s_rev"},
        )
        rev = self.mod.call_tool(
            "ambitious_sequential_reflect",
            {
                "task_or_hypothesis": "Step 2 修正假设：改用词边界正则",
                "thought_step": 3,
                "total_thoughts": 3,
                "is_revision": True,
                "revises_step": 2,
                "branch_from_step": 1,
                "branch_id": "branch_regex",
                "evidence_or_constraint": "Substring match caused false positive",
                "session_id": "s_rev",
            },
        )
        ledger = rev["session_ledger"]
        self.assertEqual(ledger["superseded_steps"], [2])
        self.assertIn("branch_regex", ledger["branches"])
        self.assertNotIn("2@main", ledger["active_chain_summary"])
        self.assertIn("3@branch_regex", ledger["active_chain_summary"])

    def test_14_sequential_reflect_failure_evidence_triggers_revision(self):
        res = self.mod.call_tool(
            "ambitious_sequential_reflect",
            {
                "task_or_hypothesis": "直接裸调 ZoneInfo",
                "thought_step": 3,
                "total_thoughts": 3,
                "evidence_or_constraint": "ZoneInfoNotFoundError: test failed on Windows without tzdata",
                "session_id": "s_fail",
            },
        )
        self.assertTrue(res["needs_revision"])
        self.assertTrue(res["state"]["next_thought_needed"])
        self.assertEqual(res["state"]["total_thoughts"], 4)
        # Live evidence must be prepended to the Red-Blue confrontation matrix
        top_fight = res["red_blue_confrontation"]["falsifiable_check_matrix"][0]
        self.assertIn("ZoneInfoNotFoundError", top_fight["red_team_attack"])

    def test_15_sequential_reflect_isolated_file_persistence_and_reset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store_file = Path(tmpdir) / "ledger.json"
            with patch.dict(os.environ, {"AMBITIOUS_SESSION_STORE": str(store_file)}):
                self.mod.call_tool(
                    "ambitious_sequential_reflect",
                    {"task_or_hypothesis": "持久化步骤 1", "thought_step": 1, "session_id": "disk_sess"},
                )
                self.assertTrue(store_file.is_file())
                saved = json.loads(store_file.read_text(encoding="utf-8"))
                self.assertEqual(len(saved["disk_sess"]["steps"]), 1)

                reset_res = self.mod.call_tool(
                    "ambitious_sequential_reflect",
                    {"task_or_hypothesis": "重置后步骤 1", "thought_step": 1, "session_id": "disk_sess", "reset_session": True},
                )
                self.assertEqual(reset_res["session_ledger"]["history_count"], 1)
                self.assertIn("重置后步骤 1", reset_res["session_ledger"]["active_chain_summary"])

    def test_16_prompt_rewrite_backward_compatibility_and_new_fields(self):
        res = self.mod.call_tool(
            "ambitious_prompt_rewrite",
            {"raw_task": "做个每日情报报告生成脚本", "domain": "多智能体编排", "language": "zh"},
        )
        for legacy_key in (
            "raw_task",
            "domain",
            "deep_rewrite_meta_prompt_zh",
            "deep_rewrite_meta_prompt_en",
            "ambitious_execution_prompt",
            "checklist",
        ):
            self.assertIn(legacy_key, res)
        for v12_key in (
            "version",
            "mode_used",
            "ambiguity_assessment",
            "grill_me_questions",
            "altitude_depth_breadth_matrix",
            "red_blue_confrontation",
            "sequential_branch_plan",
            "product_engineering_4step",
        ):
            self.assertIn(v12_key, res)
        self.assertEqual(len(res["checklist"]), 8)

    def test_17_prompt_rewrite_languages_and_modes(self):
        en_res = self.mod.call_tool(
            "ambitious_prompt_rewrite",
            {"raw_task": "Build an AST code analyzer", "language": "en", "mode": "product_to_code"},
        )
        self.assertEqual(en_res["mode_used"], "product_to_code")
        self.assertIn("[Stage 0: Grill-Me Trade-off Alignment", en_res["ambitious_execution_prompt"])
        self.assertIn("[Dimension 4: Red-Blue Confrontation", en_res["ambitious_execution_prompt"])

        bi_res = self.mod.call_tool(
            "ambitious_prompt_rewrite",
            {"raw_task": "Build an AST code analyzer", "language": "bilingual", "mode": "direct_rewrite"},
        )
        self.assertIn("【第零阶：Grill-Me", bi_res["ambitious_execution_prompt"])
        self.assertIn("[Stage 0: Grill-Me", bi_res["ambitious_execution_prompt"])

    def test_18_stdio_jsonrpc_subprocess_protocol(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "ambitious_grill_alignment", "arguments": {"raw_task": "优化分布式事务网关脚本"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "nonexistent_tool", "arguments": {}},
            },
        ]
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(SERVER_PATH)],
            input="\n".join(json.dumps(r, ensure_ascii=False) for r in requests) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        replies = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4])
        self.assertEqual(replies[0]["result"]["serverInfo"]["version"], "1.2.0")
        self.assertEqual(len(replies[1]["result"]["tools"]), 4)
        self.assertFalse(replies[2]["result"]["isError"])
        self.assertTrue(replies[3]["result"]["isError"])

    def test_19_altitude_depth_breadth_and_red_blue_depth_semantics(self):
        res = self.mod.call_tool(
            "ambitious_prompt_rewrite",
            {
                "raw_task": "把网页加载速度从 10秒 优化到 0.2秒，并修复这次闪退",
                "domain": "前端与架构",
                "constraints": ["保留现有页面全部业务模块", "通过隔离单测验证"],
            },
        )
        adb = res["altitude_depth_breadth_matrix"]
        self.assertIn("altitude", adb)
        self.assertIn("depth", adb)
        self.assertIn("breadth", adb)
        # Verify 3-state evidence triage captures the numeric claims (10秒 / 0.2秒)
        triage = adb["depth"]["evidence_triage_3state"]
        self.assertTrue(any("10秒" in c or "0.2秒" in c for c in triage["unverified_claims"]))
        self.assertIn("待验证", adb["depth"]["outside_judge_and_honesty_gate"])

        rb = res["red_blue_confrontation"]
        self.assertIn("blue_team_defense", rb)
        self.assertGreaterEqual(len(rb["red_team_attacks"]), 4)
        self.assertGreaterEqual(len(rb["blue_team_counter_hardening"]), 4)
        self.assertGreaterEqual(len(rb["falsifiable_check_matrix"]), 4)
        for fight in rb["falsifiable_check_matrix"]:
            self.assertIn("red_team_attack", fight)
            self.assertIn("blue_counter_patch", fight)
            self.assertIn("falsifiable_experiment", fight)


if __name__ == "__main__":
    unittest.main(verbosity=2)
