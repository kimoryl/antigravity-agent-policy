"""Unit tests for 3D Orthogonal Cross-Memory Matrix and Unified Mod Adapter Layers."""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
sys.path.insert(0, str(PLUGIN_ROOT / "hooks"))

import agy_policy_hook as aph  # noqa: E402
import gdrive_memory_engine as gme  # noqa: E402
import mod_manager as mm  # noqa: E402
import policy_hook as ph  # noqa: E402
import runtime_tools as rt  # noqa: E402


class Adapter3DMemoryTest(unittest.TestCase):
    def test_core_mods_and_scene_profiles(self) -> None:
        self.assertEqual(len(mm.CORE_MOD_IDS), 5)
        self.assertIn("context7-sdk-verifier", mm.CORE_MOD_IDS)
        self.assertIn("ui-aesthetic-studio", mm.MOD_PROFILES["web"])
        self.assertIn("anatomy-motion-studio", mm.MOD_PROFILES["web"])
        self.assertIn("anatomy-motion-studio", mm.MOD_PROFILES["office"])
        self.assertIn("windows-uia-automation", mm.MOD_PROFILES["automation"])
        self.assertIn("windows-uia-automation", mm.MOD_PROFILES["ops"])

    def test_codex_agy_and_opus_runtime_sync_and_schemas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            codex_home = base / "codex_home"
            agy_home = base / "agy_home"
            opus_home = base / "opus_home"

            mm.sync_codex_runtime(list(mm.CORE_MOD_IDS), target_home=codex_home)
            codex_cfg = (codex_home / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model = "gpt-6.1-sol"', codex_cfg)
            self.assertIn("hooks = true", codex_cfg)
            self.assertIn("[mcp_servers.ap]", codex_cfg)
            self.assertNotIn("[mcp_servers.context7_sdk_verifier]", codex_cfg)
            self.assertNotIn('model = "gpt-5.4"', codex_cfg)

            codex_agents = (codex_home / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("Project × Modality × Global", codex_agents)
            self.assertIn("sdk_fact_lookup", codex_agents)
            self.assertIn("Sidecar ON/OFF", codex_agents)

            mm.sync_agy_runtime(list(mm.CORE_MOD_IDS), target_home=agy_home, is_desktop=False)
            self.assertTrue((mm.POLICY_PLUGIN_ROOT / "skills" / "sdk-memory-sidecar" / "SKILL.md").is_file())
            agy_mcp_root = agy_home / ".gemini" / "antigravity-cli" / "mcp" / "antigravity-agent-policy_ap"
            self.assertTrue((agy_mcp_root / "sdk_fact_lookup.json").is_file(), f"Missing in {agy_mcp_root}")
            self.assertTrue((agy_mcp_root / "sdk_fact_reconcile_report.json").is_file(), f"Missing in {agy_mcp_root}")
            self.assertTrue((agy_mcp_root / "sdk_memory_review_commit.json").is_file(), f"Missing in {agy_mcp_root}")

            mm.sync_opus_runtime(list(mm.CORE_MOD_IDS), target_home=opus_home)
            opus_claude_json = opus_home / ".claude.json"
            self.assertTrue(opus_claude_json.is_file())
            opus_cfg = json.loads(opus_claude_json.read_text(encoding="utf-8"))
            self.assertIn("ap", opus_cfg.get("mcpServers", {}))
            self.assertTrue((opus_home / ".claude" / "CLAUDE.md").is_file())
            self.assertTrue((opus_home / ".claude" / "skills" / "mod-context7-sdk-verifier-sdk-memory-sidecar" / "SKILL.md").is_file())

    def test_3d_memory_engine_and_modality_recall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_dir = pathlib.Path(tmp) / "example-app"
            ws_dir.mkdir(parents=True, exist_ok=True)
            sandbox_mem = pathlib.Path(tmp) / "05_Agent_Memory_Sync"
            (sandbox_mem / "L1_Modalities").mkdir(parents=True, exist_ok=True)
            (sandbox_mem / "L1_Modalities" / "ui_aesthetic_design.json").write_text(
                json.dumps(
                    {
                        "modality_id": "ui_aesthetic_design",
                        "modality_name": "UI 视觉美学与 OKLCH 色彩工艺",
                        "craft_rules": [
                            {
                                "rule_id": "oklch-01",
                                "title": "OKLCH 感知等亮色彩规范",
                                "summary": "使用 OKLCH 色彩空间构建高对比度无障碍色板。",
                                "recommendations": ["保持 WCAG AA 对比度门禁"],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"AGY_MEMORY_SANDBOX": "1", "AGY_MEMORY_SANDBOX_DIR": str(sandbox_mem)},
                clear=False,
            ):
                gme.reset_connection_if_needed(force=True)
                mods_list = rt.call("runtime_knowledge", {"action": "modality_list"})
                self.assertEqual(len(mods_list["modalities"]), 5)

                mod_rec = rt.call(
                    "runtime_knowledge",
                    {"action": "modality_recall", "modality_id": "ui_aesthetic_design", "query": "OKLCH"},
                )
                self.assertGreaterEqual(mod_rec["count"], 1)

                mem_rec = rt.call(
                    "runtime_knowledge",
                    {"action": "memory_recall", "project_root": str(ws_dir), "scope": "3d", "query": "OKLCH 色彩"},
                )
                self.assertIn(mem_rec["status"], {"ok", "fallback"})
                self.assertGreaterEqual(len(mem_rec["results"]), 1)
                self.assertTrue(any(r.get("layer") == "MODALITY_MEMORY" for r in mem_rec["results"]))
            gme.reset_connection_if_needed(force=True)

    def test_hooks_and_sidecar_green_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"CODEX_AGENT_POLICY_DATA": tmp, "AGY_MEMORY_SANDBOX": "1"}):
                s_agy = aph.handle_agy(
                    "SessionStart",
                    {"conversationId": "test-conv-3d", "workspacePaths": ["/workspace/example-app"]},
                )
                self.assertIn("Project × Modality × Global", json.dumps(s_agy, ensure_ascii=False))

                sidecar_allow = aph.handle_agy(
                    "PreToolUse",
                    {
                        "conversationId": "test-conv-3d",
                        "stepIdx": 1,
                        "toolCall": {
                            "name": "invoke_subagent",
                            "args": {"Role": "记忆事实伴飞审查员", "Prompt": "请对 /workspace/example-app 执行起飞预检"},
                        },
                    },
                )
                self.assertEqual(sidecar_allow["decision"], "allow")

                sidecar_blocked_model = aph.handle_agy(
                    "PreToolUse",
                    {
                        "conversationId": "test-conv-3d",
                        "stepIdx": 2,
                        "toolCall": {
                            "name": "invoke_subagent",
                            "args": {"Role": "记忆事实伴飞审查员", "Model": "6.1sol", "Prompt": "请核对清单"},
                        },
                    },
                )
                self.assertEqual(sidecar_blocked_model["decision"], "deny")
                self.assertIn("BLOCKED_REVIEWER_MODEL", sidecar_blocked_model["reason"])

                sidecar_blind_deny = aph.handle_agy(
                    "PreToolUse",
                    {
                        "conversationId": "test-conv-3d",
                        "stepIdx": 3,
                        "toolCall": {
                            "name": "invoke_subagent",
                            "args": {"Role": "记忆事实伴飞审查员", "Prompt": "我认为设计合理，无需核对直接提交"},
                        },
                    },
                )
                self.assertEqual(sidecar_blind_deny["decision"], "deny")
                self.assertIn("BLIND_AUDIT_VIOLATION", sidecar_blind_deny["reason"])

                dag_gate = aph.handle_agy(
                    "PreToolUse",
                    {
                        "conversationId": "test-conv-3d",
                        "stepIdx": 4,
                        "toolCall": {
                            "name": "call_mcp_tool",
                            "args": {
                                "ToolName": "runtime_plan",
                                "Arguments": {"action": "create", "steps": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]},
                            },
                        },
                    },
                )
                self.assertEqual(dag_gate["decision"], "allow")
                pre_inv = aph.handle_agy("PreInvocation", {"conversationId": "test-conv-3d"})
                self.assertIn("DAG_HUMAN_GATE", json.dumps(pre_inv, ensure_ascii=False))

                s_codex = ph.handle({"hook_event_name": "SessionStart", "session_id": "test-codex-3d"})
                codex_text = json.dumps(s_codex, ensure_ascii=False)
                self.assertIn("Project × Modality × Global", codex_text)
                for token in ("gemini-4-carbon-high", "gemini-4-argon-high", "gemini-4-hydrogen-medium"):
                    self.assertIn(token, codex_text)
                    self.assertIn(token, aph.RUNTIME_CONTEXT)


if __name__ == "__main__":
    unittest.main()
