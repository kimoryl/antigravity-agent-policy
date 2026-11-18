"""Comprehensive regression unit tests for W3 Global Runtime & 3-CLI fixes."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
sys.path.insert(0, str(PLUGIN_ROOT / "hooks"))

import delivery_state  # noqa: E402
import gdrive_memory_engine as gme  # noqa: E402
import mcp_server  # noqa: E402
import mod_manager as mm  # noqa: E402
import runtime_engine  # noqa: E402
import runtime_state  # noqa: E402


class W3RuntimeAndCliFixesTest(unittest.TestCase):
    def test_1_production_codex_policy_data_never_hijacks_gdrive_memory(self) -> None:
        """[P0-L1] Production CODEX_AGENT_POLICY_DATA paths must never hijack 05_Agent_Memory_Sync."""
        for prod_data_dir in (
            "/home/runner/AppData/Local/codex-agent-policy",
            "/home/runner/.local/state/codex-agent-policy",
            "/var/lib/agent/.agent_policy_data",
        ):
            env_clean = {
                k: v
                for k, v in os.environ.items()
                if k not in ("AGY_MEMORY_SANDBOX", "AGY_MEMORY_SANDBOX_DIR", "AGY_MEMORY_SYNC_DIR")
            }
            env_clean["CODEX_AGENT_POLICY_DATA"] = prod_data_dir
            with patch.dict(os.environ, env_clean, clear=True):
                self.assertFalse(gme.is_memory_sandbox_active())
                resolved = str(gme.resolve_gdrive_memory_dir())
                self.assertNotIn("codex-agent-policy", resolved.lower())
                self.assertNotIn(".agent_policy_data", resolved.lower())
                self.assertIn("05_Agent_Memory_Sync", resolved)

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"CODEX_AGENT_POLICY_DATA": tmp}, clear=False):
                self.assertTrue(gme.is_memory_sandbox_active())
                self.assertTrue(str(gme.resolve_gdrive_memory_dir()).startswith(str(Path(tmp))))

    def test_2_atomic_json_write_and_l2_modality_flush_no_resurrection(self) -> None:
        """[P1-5] Deleted/evicted L2_Experiences & L1_Modalities entries must not resurrect on reload."""
        with tempfile.TemporaryDirectory() as tmp:
            sandbox_mem = Path(tmp) / "mem_sync"
            (sandbox_mem / "L1_Projects").mkdir(parents=True, exist_ok=True)
            (sandbox_mem / "L1_Modalities").mkdir(parents=True, exist_ok=True)

            l2_path = sandbox_mem / "L2_Experiences.json"
            l2_path.write_text(
                json.dumps(
                    {
                        "experiences": [
                            {"experience_id": "exp-obsolete", "scope": "global", "title": "Old", "summary": "Obsolete lesson", "status": "validated"},
                            {"experience_id": "exp-active", "scope": "global", "title": "Keep", "summary": "Active lesson", "status": "validated"},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            mod_path = sandbox_mem / "L1_Modalities" / "ui_aesthetic_design.json"
            mod_path.write_text(
                json.dumps(
                    {
                        "modality_id": "ui_aesthetic_design",
                        "modality_name": "UI 视觉美学",
                        "craft_rules": [
                            {"rule_id": "craft-obsolete", "title": "Old Craft", "summary": "Drop me"},
                            {"rule_id": "craft-active", "title": "OKLCH Craft", "summary": "Keep me"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "AGY_MEMORY_SANDBOX": "1",
                    "AGY_MEMORY_SANDBOX_DIR": str(sandbox_mem),
                },
                clear=False,
            ):
                gme.reset_connection_if_needed(force=True)
                with gme.gdrive_memory_db() as conn:
                    conn.execute("DELETE FROM experiences WHERE experience_id = ?", ("exp-obsolete",))
                    conn.execute("DELETE FROM memories WHERE id = ?", ("craft-obsolete",))

                # Verify no .tmp files left behind
                self.assertEqual(list(sandbox_mem.rglob("*.tmp")), [])

                # Force fresh in-memory reload from disk JSONs and confirm deleted items stay gone
                gme.reset_connection_if_needed(force=True)
                with gme.gdrive_memory_db() as conn2:
                    exp_ids = {r[0] for r in conn2.execute("SELECT experience_id FROM experiences").fetchall()}
                    craft_ids = {r[0] for r in conn2.execute("SELECT id FROM memories WHERE scope = 'modality'").fetchall()}
                self.assertIn("exp-active", exp_ids)
                self.assertNotIn("exp-obsolete", exp_ids)
                self.assertIn("craft-active", craft_ids)
                self.assertNotIn("craft-obsolete", craft_ids)
            gme.reset_connection_if_needed(force=True)

    def test_3_mcp_short_name_ap_and_64_char_limit(self) -> None:
        """[P0-L3] .mcp.json & mcp_config.json use short key 'ap' and all tool names <= 64 chars."""
        for fname in (".mcp.json", "mcp_config.json"):
            raw = (PLUGIN_ROOT / fname).read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"{fname} must not have UTF-8 BOM")
            parsed = json.loads(raw.decode("utf-8"))
            self.assertIn("ap", parsed["mcpServers"])
            self.assertNotIn("agent-policy", parsed["mcpServers"])
            resolved = mm.resolve_policy_mcp_server_entry(parsed)
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved[0], "ap")

        legacy = {"mcpServers": {"agent-policy": {"command": "python"}}}
        self.assertEqual(mm.resolve_policy_mcp_server_entry(legacy)[0], "agent-policy")

        prefix = "mcp_antigravity-agent-policy_ap_"
        for tool in mcp_server.get_dynamic_tools():
            full_name = prefix + tool["name"]
            self.assertLessEqual(len(full_name), 64, f"Tool name exceeds 64 chars: {full_name} ({len(full_name)})")

    def test_4_zip_slip_blocked_and_mod_call_error_passthrough(self) -> None:
        """[P1-4] & [P2-3] Zip Slip is blocked; mod_call propagates JSON-RPC error and isError: true."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            evil_zip = tmp_dir / "evil.mod.zip"
            with zipfile.ZipFile(evil_zip, "w") as zf:
                zf.writestr("../../escaped.txt", "pwned")
            with self.assertRaises(mm.ModError):
                mm.install_mod(evil_zip)
            self.assertFalse((tmp_dir.parent / "escaped.txt").exists())

            # Fake Mod MCP server returning JSON-RPC error and isError: true
            mods_root = tmp_dir / "mods"
            err_mod = mods_root / "err-mod"
            err_mod.mkdir(parents=True)
            srv_py = err_mod / "srv.py"
            srv_py.write_text(
                "import json, sys\n"
                "for line in sys.stdin:\n"
                "    msg = json.loads(line)\n"
                "    if msg.get('method') == 'initialize':\n"
                "        print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': {}}), flush=True)\n"
                "    elif msg.get('method') == 'tools/call':\n"
                "        mode = (msg.get('params', {}).get('arguments') or {}).get('mode')\n"
                "        if mode == 'rpc_error':\n"
                "            print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'error': {'code': -32602, 'message': 'Bad param'}}), flush=True)\n"
                "        else:\n"
                "            print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': {'isError': True, 'content': [{'type': 'text', 'text': 'Tool boom'}]}}), flush=True)\n"
                "        break\n",
                encoding="utf-8",
            )
            (err_mod / "mod.json").write_text(
                json.dumps(
                    {
                        "id": "err-mod",
                        "name": "Err Mod",
                        "version": "1.0.0",
                        "mcp_servers": {
                            "err-srv": {
                                "command": sys.executable,
                                "args": ["${MOD_DIR}/srv.py"],
                            }
                        },
                        "mcp_tools": [{"name": "boom_tool", "description": "Fails"}],
                    }
                ),
                encoding="utf-8",
            )
            (mods_root / "mounts.json").write_text(
                json.dumps({"mounts": {"codex-cli": ["err-mod"], "agy-cli": ["err-mod"], "opus-cli": ["err-mod"]}}),
                encoding="utf-8",
            )

            with (
                patch.object(mm, "MODS_ROOT", mods_root),
                patch.object(mm, "MOUNTS_FILE", mods_root / "mounts.json"),
                patch.object(mm, "PACKAGES_DIR", mods_root / ".packages"),
                patch.dict(os.environ, {"CODEX_AGENT_POLICY_DATA": str(tmp_dir / "pdata"), "AGY_MEMORY_SANDBOX": "1"}),
            ):
                resp1 = mcp_server.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 101,
                        "method": "tools/call",
                        "params": {
                            "name": "mod_call",
                            "arguments": {"tool_name": "boom_tool", "mod_id": "err-mod", "arguments": {"mode": "rpc_error"}},
                        },
                    }
                )
                self.assertTrue(resp1["result"].get("isError"))
                self.assertEqual(resp1["result"]["structuredContent"]["error"]["code"], -32602)

                resp2 = mcp_server.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 102,
                        "method": "tools/call",
                        "params": {
                            "name": "mod_call",
                            "arguments": {"tool_name": "boom_tool", "mod_id": "err-mod", "arguments": {"mode": "is_error"}},
                        },
                    }
                )
                self.assertTrue(resp2["result"].get("isError"))
                self.assertEqual(resp2["result"]["structuredContent"]["error"], "Tool boom")

    def test_5_boolean_exit_code_and_delivery_missing_artifact_guard(self) -> None:
        """[P2-4] Boolean False must not pass exit_code == 0; delivery submit rejects missing files."""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"CODEX_AGENT_POLICY_DATA": tmp, "AGY_MEMORY_SANDBOX": "1"}):
                ws = Path(tmp) / "ws"
                ws.mkdir()
                profile = {
                    "root_goal": "Verify boolean exit code guard",
                    "local_goal": "Test exit code",
                    "task_type": "code",
                    "constraints": [],
                    "expected_outputs": [],
                    "acceptance_criteria": [],
                    "required_capabilities": [],
                    "complexity_hint": "simple",
                }
                task = runtime_state.create_task(profile, str(ws))
                vres = runtime_engine.verify(
                    task["task_id"],
                    checks=[{"name": "bool-check", "kind": "exit_code", "expected_exit_code": 0, "actual_exit_code": False}],
                )
                self.assertNotEqual(vres["status"], "passed")

                refreshed = runtime_state.get_task(task["task_id"])
                runtime_state.update_task(task["task_id"], refreshed["revision"], status="complete")
                done_task = runtime_state.get_task(task["task_id"])
                with self.assertRaises(delivery_state.DeliveryError):
                    delivery_state.submit(
                        task["task_id"],
                        expected_revision=done_task["revision"],
                        expected_version=0,
                        summary="Test missing artifact",
                        artifacts=[{"path": str(ws / "nonexistent_file.txt")}],
                        check_report={"summary": "ok"},
                    )

    def test_6_sync_codex_opus_agy_isolated_homes(self) -> None:
        """[CLI-1] & [CLI-2] Verify isolated sync for Codex (dedup @core), Opus (Claude 2.x JSON), and AGY."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            codex_dir = base / "codex_home"
            codex_dir.mkdir()
            (codex_dir / "config.toml").write_text(
                'model = "gpt-6.1-sol"\n'
                'model_reasoning_effort = "xhigh"\n\n'
                '[marketplaces.codex-agent-policy-local]\n'
                'source = "/tmp/deprecated-marketplace/codex-agent-policy-marketplace"\n\n'
                '[plugins."codex-agent-policy@codex-agent-policy-local"]\n'
                'enabled = true\n\n'
                '[mcp_servers.jev]\n'
                'command = "node"\n'
                'args = ["jev.mjs"]\n\n'
                '[projects."/workspace/demo"]\n'
                'trust_level = "trusted"\n\n'
                '[[skills.config]]\n'
                'path = "/tmp/deprecated-marketplace/old/SKILL.md"\n'
                'enabled = false\n\n'
                '[[skills.config]]\n'
                'name = "context-mode:ctx-doctor"\n'
                'enabled = false\n',
                encoding="utf-8",
            )

            import shutil as _shutil
            temp_mods = base / "mods_pool"
            _shutil.copytree(PLUGIN_ROOT / "mods", temp_mods)
            ext_mod = temp_mods / "github-suite"
            (ext_mod / "skills" / "github-workflow").mkdir(parents=True)
            (ext_mod / "skills" / "github-workflow" / "SKILL.md").write_text("---\nname: github-workflow\ndescription: test\n---\n", encoding="utf-8")
            (ext_mod / "mod.json").write_text(
                json.dumps({
                    "id": "github-suite",
                    "name": "Sample GitHub Suite",
                    "version": "1.0.0",
                    "skills": [{"name": "github-workflow", "path": "skills/github-workflow/SKILL.md"}],
                    "mcp_servers": {"github-mcp": {"command": "python", "args": ["${MOD_DIR}/noop.py"]}},
                    "mcp_tools": [],
                }),
                encoding="utf-8",
            )
            mounted = [*mm.CORE_MOD_IDS, "github-suite"]
            with patch.object(mm, "MODS_ROOT", temp_mods):
                mm.sync_codex_runtime(mounted, target_home=codex_dir)
            self.assertTrue((codex_dir / "config.toml.bak").is_file())
            parsed_toml = tomllib.loads((codex_dir / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(parsed_toml["model_reasoning_effort"], "xhigh")
            self.assertEqual(parsed_toml["projects"]["/workspace/demo"]["trust_level"], "trusted")
            self.assertIn("ap", parsed_toml["mcp_servers"])
            self.assertIn("github_mcp", parsed_toml["mcp_servers"])
            self.assertIn("jev", parsed_toml["mcp_servers"])
            self.assertNotIn("context7_sdk_verifier", parsed_toml["mcp_servers"])
            self.assertNotIn("marketplaces", parsed_toml)
            self.assertNotIn("plugins", parsed_toml)
            self.assertEqual(len(parsed_toml["skills"]["config"]), 1)

            # Opus / Claude Code 2.x sync
            opus_dir = base / "opus_home"
            opus_dir.mkdir()
            (opus_dir / ".claude.json").write_text(
                json.dumps({"numStartups": 42, "oauthAccount": {"email": "u@example.com"}, "projects": {"p1": {}}}),
                encoding="utf-8",
            )
            with patch.object(mm, "MODS_ROOT", temp_mods):
                mm.sync_opus_runtime(mounted, target_home=opus_dir)
            self.assertTrue((opus_dir / ".claude.json.bak").is_file())
            raw_claude = (opus_dir / ".claude.json").read_bytes()
            self.assertFalse(raw_claude.startswith(b"\xef\xbb\xbf"))
            claude_cfg = json.loads(raw_claude.decode("utf-8"))
            self.assertEqual(claude_cfg["numStartups"], 42)
            self.assertEqual(claude_cfg["oauthAccount"]["email"], "u@example.com")
            self.assertIn("ap", claude_cfg["mcpServers"])
            self.assertIn("github-mcp", claude_cfg["mcpServers"])
            self.assertNotIn("context7-sdk-verifier", claude_cfg["mcpServers"])
            self.assertTrue((opus_dir / ".claude" / "CLAUDE.md").is_file())
            self.assertTrue((opus_dir / ".claude" / "skills" / "mod-github-suite-github-workflow" / "SKILL.md").is_file())

            # AGY isolated sync: conversations created & no duplicate mcp_config.json in mod-github-suite
            agy_dir = base / "agy_home"
            with patch.object(mm, "MODS_ROOT", temp_mods):
                mm.sync_agy_runtime(mounted, target_home=agy_dir, is_desktop=False)
            self.assertTrue((agy_dir / ".gemini" / "antigravity" / "conversations").is_dir())
            mod_gh = agy_dir / ".gemini" / "config" / "plugins" / "mod-github-suite"
            self.assertTrue((mod_gh / "plugin.json").is_file())
            self.assertFalse((mod_gh / "mcp_config.json").exists())
            self.assertFalse((mod_gh / ".mcp.json").exists())


if __name__ == "__main__":
    unittest.main()
