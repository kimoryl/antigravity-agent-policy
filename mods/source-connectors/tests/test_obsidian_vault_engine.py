"""Isolated vault/index tests. Never open production services, vaults or sync scripts."""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp"))
import obsidian_vault_engine as vault
import mcp_server as server


class IsolatedVaultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="obsidian-unittest-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.notes = self.base / "vault"
        self.notes.mkdir()
        self.db = self.base / "index.sqlite3"
        self.env = {
            "OBSIDIAN_VAULT_ROOT": str(self.notes),
            "OBSIDIAN_GDRIVE_VAULT_ROOT": "",
            "OBSIDIAN_INDEX_DB": str(self.db),
            "OBSIDIAN_SYNC_SCRIPT": str(self.base / "missing-sync.py"),
            "OBSIDIAN_SYNC_STATE": str(self.base / "state.json"),
            "OBSIDIAN_CRAWL_STATE": str(self.base / "crawl.json"),
        }
        env_patch = mock.patch.dict(os.environ, self.env)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for target in ("urllib.request.urlopen", "urllib.request.build_opener"):
            guard = mock.patch(target, side_effect=AssertionError("Network access forbidden in isolated tests"))
            guard.start()
            self.addCleanup(guard.stop)
        for name, value in (("KB_SYNC_SCRIPT", Path(self.env["OBSIDIAN_SYNC_SCRIPT"])), ("KB_SYNC_STATE", Path(self.env["OBSIDIAN_SYNC_STATE"]))):
            patch = mock.patch.object(server, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.engine = vault.ObsidianVaultEngine()

    def write(self, name, text):
        path = self.notes / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def parse(self, name, text, **kwargs):
        path = self.write(name, text)
        return vault.parse_obsidian_markdown(path, self.notes, **kwargs)

    def seed(self):
        self.write("Windows.md", "# Windows process guide\nUse subprocess.CREATE_NO_WINDOW = 0x08000000.\nSet creationflags and timeout.\n")
        self.write("Recipes.md", "# Dinner recipes\nTomato soup and fresh bread.\n")
        self.engine.sync_vault_index()


class MarkdownParsingTests(IsolatedVaultTest):
    def test_yaml_heading_tree_and_source_line_numbers(self):
        raw = "---\ntitle: Explicit title\ntags:\n  - python\n  - 测试\naliases: [别名, alternate]\ncreated: 2026-11-01\n---\n\n# Root\nIntro\n## Child\nDetail\n### Leaf\nLeaf text\n#### Deep ####\nDeep text\n##### Not a section\nTrailing\n"
        note = self.parse("Tree.md", raw)
        self.assertEqual(note.title, "Explicit title")
        self.assertEqual(note.tags, ["python", "测试"])
        self.assertEqual(note.aliases, ["别名", "alternate"])
        self.assertEqual([h["level"] for h in note.headings], [1, 2, 3, 4])
        self.assertEqual(note.sections[-1].heading_path, "Root > Child > Leaf > Deep")
        self.assertIn("##### Not a section", note.sections[-1].content)
        lines = raw.splitlines()
        for section in note.sections:
            self.assertEqual(section.content, "\n".join(lines[section.start_line - 1:section.end_line]).strip())

    def test_long_code_blocks_are_never_split_at_blank_lines(self):
        block = "````python\n# Fake heading\n" + ("print('x')\n\n" * 40) + "```\n~~~\n## Still code\n````\n"
        raw = "# Real heading\n\nBefore\n\n" + block + "\nAfter\n\n## Next\nDone\n"
        note = self.parse("Fences.md", raw, max_section_chars=75)
        self.assertEqual([h["text"] for h in note.headings], ["Real heading", "Next"])
        containing = [s for s in note.sections if "print('x')" in s.content]
        self.assertEqual(len(containing), 1)
        self.assertIn(block.strip(), containing[0].content)

    def test_tilde_fence_and_unclosed_fence_protection(self):
        for code in ("~~~text\n# Fake\n\n" + "a" * 200 + "\n~~~", "```text\n## Unclosed\n\n" + "b" * 200):
            with self.subTest(code=code[:8]):
                note = self.parse("Unclosed.md", "# Real\n\n" + code, max_section_chars=40)
                self.assertEqual(len(note.headings), 1)
                self.assertTrue(any(code in section.content for section in note.sections))

    def test_frontmatter_bom_long_yaml_and_closing_dots(self):
        raw = "\ufeff---\ntitle: Long metadata\n" + "# comment\n" * 90 + "tags: [one]\n...\n# Body"
        meta, body, line = vault.parse_frontmatter(raw)
        self.assertEqual(meta["title"], "Long metadata")
        self.assertEqual(body, "# Body")
        self.assertEqual(raw.splitlines()[line - 1], "# Body")

    def test_frontmatter_missing_terminator_is_markdown(self):
        raw = "---\ntitle: Not YAML\n# Body"
        self.assertEqual(vault.parse_frontmatter(raw), ({}, raw, 1))

    def test_links_aliases_embeds_relative_links_and_code_exclusion(self):
        note = self.parse("Links.md", "---\ntags: [python]\naliases:\n - 入口\n---\n# Links\n[[Target#Part|label]] [[Target]] ![[Embedded]] [rel](folder/Other.md#x) #内联标签\n`[[InlineFake]]`\n```\n[[FenceFake]] #badtag\n```\n")
        self.assertEqual(note.outlinks, ["Target", "Embedded", "folder/Other.md"])
        self.assertEqual(note.aliases, ["入口"])
        self.assertIn("内联标签", note.tags)
        self.assertNotIn("badtag", note.tags)


class IndexAndGraphTests(IsolatedVaultTest):
    def test_incremental_add_update_delete_and_fts_cleanup(self):
        path = self.write("One.md", "# One\nuniquefirstterm")
        first = self.engine.sync_vault_index()
        self.assertEqual(first["ingested"], 1)
        self.assertEqual(self.engine.sync_vault_index()["skipped"], 1)
        path.write_text("# One\nuniquesecondterm and more", encoding="utf-8")
        self.assertEqual(self.engine.sync_vault_index()["updated"], 1)
        with self.engine._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM vault_fts WHERE vault_fts MATCH 'uniquefirstterm'").fetchone()[0], 0)
        path.unlink()
        self.assertEqual(self.engine.sync_vault_index()["deleted"], 1)
        with self.engine._connect() as conn:
            for table in ("vault_notes", "vault_sections", "vault_fts"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_curated_and_custom_root_notes_remain_indexed(self):
        self.write("01_Tech_Cheatsheets/A.md", "# A")
        self.write("Custom/B.md", "# B")
        self.write("Root.md", "# Root")
        self.write(".obsidian/Hidden.md", "# Hidden")
        self.assertEqual(self.engine.sync_vault_index()["total_notes"], 3)

    def test_yaml_alias_and_bidirectional_graph(self):
        self.write("A.md", "# A\n[[别名#Heading|展示]] [[B]] [[Missing]]")
        self.write("B.md", "---\ntitle: Beta\naliases: [别名]\n---\n# B\n[[A]]")
        self.engine.sync_vault_index()
        a = self.engine.get_graph_neighbors("A.md")
        self.assertEqual({o["note_id"] for o in a["outlinks"]}, {"B.md", ""})
        self.assertEqual([b["note_id"] for b in a["backlinks"]], ["B.md"])
        self.assertEqual([b["note_id"] for b in self.engine.get_graph_neighbors("B.md")["backlinks"]], ["A.md"])

    def test_deleted_target_clears_resolution_and_can_resolve_later(self):
        self.write("A.md", "# A\n[[B]]")
        target = self.write("B.md", "# B")
        self.engine.sync_vault_index()
        target.unlink()
        self.engine.sync_vault_index()
        self.assertEqual(self.engine.get_graph_neighbors("A.md")["outlinks"][0]["note_id"], "")
        self.write("B.md", "# B recreated")
        self.engine.sync_vault_index()
        self.assertEqual(self.engine.get_graph_neighbors("A.md")["outlinks"][0]["note_id"], "B.md")

    def test_qualified_relative_links_resolve_duplicate_stems(self):
        self.write("One/Target.md", "# Same")
        self.write("Two/Target.md", "# Same")
        self.write("One/Source.md", "# Source\n[[Target]] [[Two/Target]]")
        self.engine.sync_vault_index()
        neighbors = self.engine.get_graph_neighbors("One/Source.md")["outlinks"]
        self.assertEqual({o["note_id"] for o in neighbors}, {"One/Target.md", "Two/Target.md"})

    def test_connections_are_closed_after_context(self):
        with self.engine._connect() as conn:
            self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)
        import sqlite3
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


class HybridRetrievalTests(IsolatedVaultTest):
    def test_embeddings_have_384_normalized_float32_dimensions(self):
        self.assertIsNotNone(vault.np, "The requested NumPy retrieval path must be available")
        embedder = vault.LocalSemanticEmbedder()
        vector = vault.np.frombuffer(embedder.encode_blob("CREATE_NO_WINDOW"), dtype=vault.np.float32)
        self.assertEqual(vector.shape, (384,))
        self.assertAlmostEqual(float(vault.np.linalg.norm(vector)), 1.0, places=5)
        self.assertEqual(embedder.encode_blob("CREATE_NO_WINDOW"), vector.tobytes())

    def test_fts_bm25_and_vector_scores_are_fused(self):
        self.seed()
        with mock.patch.object(vault.np, "vstack", wraps=vault.np.vstack) as matrix:
            matches = self.engine.hybrid_search("CREATE_NO_WINDOW", match_count=1)["matches"]
        matrix.assert_called_once()
        self.assertEqual(matches[0]["note_id"], "Windows.md")
        self.assertGreater(matches[0]["bm25_score"], 0)
        self.assertGreater(matches[0]["vector_score"], 0)
        self.assertGreater(matches[0]["score"], 0)
        self.assertIn("L1-", matches[0]["line_range"])

    def test_semantic_concept_recalls_no_window_without_lexical_overlap(self):
        self.seed()
        matches = self.engine.hybrid_search("防黑框弹窗", match_count=2)["matches"]
        self.assertEqual(matches[0]["note_id"], "Windows.md")
        self.assertIn("CREATE_NO_WINDOW", matches[0]["content"])
        self.assertEqual(matches[0]["bm25_score"], 0)
        self.assertGreater(matches[0]["vector_score"], 0.05)

    def test_graph_neighbors_participate_in_ranking(self):
        self.write("Seed.md", "# rareseedword\n[[Neighbor]]")
        self.write("Neighbor.md", "# Related\nMore evidence")
        result = self.engine.hybrid_search("rareseedword", match_count=8)
        linked = next(m for m in result["matches"] if m["note_id"] == "Neighbor.md")
        self.assertIsNotNone(linked["graph_rank"])
        self.assertEqual(linked["graph_neighbors"]["backlinks"][0]["note_id"], "Seed.md")

    def test_category_filter_applies_before_fts_candidate_limit(self):
        for i in range(65):
            self.write(f"Other/{i:02}.md", "# exactfilterterm\nexactfilterterm")
        self.write("Wanted/Hit.md", "# Evidence\nexactfilterterm " + "other " * 20)
        result = self.engine.hybrid_search("exactfilterterm", category_or_kb="Wanted")
        self.assertEqual(len(result["matches"]), 1)
        self.assertGreater(result["matches"][0]["bm25_score"], 0)

    def test_empty_queries_zero_results_and_stable_order(self):
        self.seed()
        for query in ("", "  "):
            self.assertEqual(self.engine.hybrid_search(query)["matches"], [])
        self.assertEqual(self.engine.hybrid_search("process", match_count=0)["matches"], [])
        with self.assertRaises(ValueError):
            self.engine.hybrid_search("process", rrf_k=0)
        self.assertEqual(self.engine.hybrid_search("process"), self.engine.hybrid_search("process"))


class IngestTests(IsolatedVaultTest):
    def test_ingest_generates_yaml_links_and_updates_both_graph_directions(self):
        self.write("Original.md", "---\ntitle: 窗口控制\n---\n# 窗口控制\nCREATE_NO_WINDOW")
        result = self.engine.ingest_markdown('新增 "笔记"', "# Notes\n使用窗口控制避免弹窗。", tags=["测试", "python"])
        output = Path(result["vault_path"])
        self.assertTrue(output.is_relative_to(self.notes))
        self.assertIsNone(result["gdrive_vault_path"])
        meta, _, _ = vault.parse_frontmatter(output.read_text(encoding="utf-8"))
        self.assertEqual(meta["title"], '新增 "笔记"')
        self.assertEqual(meta["tags"], ["测试", "python"])
        self.assertEqual(result["auto_wikilinks"], ["窗口控制"])
        outgoing = self.engine.get_graph_neighbors(result["note_id"])["outlinks"]
        self.assertEqual(outgoing[0]["note_id"], "Original.md")
        self.assertEqual(self.engine.get_graph_neighbors("Original.md")["backlinks"][0]["note_id"], result["note_id"])

    def test_ingest_preserves_existing_frontmatter(self):
        raw = "---\ntitle: Existing\ntags: [preserved]\ncustom: value\n---\n# Existing\nBody"
        result = self.engine.ingest_markdown("Existing", raw)
        text = Path(result["vault_path"]).read_text(encoding="utf-8")
        self.assertEqual(text.count("title:"), 1)
        self.assertEqual(vault.parse_frontmatter(text)[0]["custom"], "value")

    def test_explicit_vault_never_defaults_to_production_storage(self):
        with mock.patch.dict(os.environ, {"OBSIDIAN_VAULT_ROOT": "", "OBSIDIAN_GDRIVE_VAULT_ROOT": "", "OBSIDIAN_INDEX_DB": ""}):
            engine = vault.ObsidianVaultEngine(vault_root=self.base / "explicit")
        self.assertEqual(engine.index_db_path, self.base / "explicit/.obsidian_index.sqlite3")
        self.assertIsNone(engine.gdrive_vault_root)
        engine.ingest_markdown("Isolated", "# Safe")

    def test_explicit_mirror_and_escaped_source_url_stay_in_temp(self):
        drive = self.base / "gdrive"
        engine = vault.ObsidianVaultEngine(self.notes, drive, self.db)
        result = engine.ingest_markdown("Web", "# Article", source_url='https://example.test/a?quote="x"')
        target = Path(result["gdrive_vault_path"])
        self.assertTrue(target.is_relative_to(drive))
        self.assertEqual(target.read_bytes(), Path(result["vault_path"]).read_bytes())
        self.assertEqual(vault.parse_frontmatter(target.read_text(encoding="utf-8"))[0]["source_url"], 'https://example.test/a?quote="x"')


class MCPContractTests(IsolatedVaultTest):
    def test_six_names_and_manifest_schemas_remain_compatible(self):
        names = {"source_connect", "weknora_kb_manage", "weknora_rag_query", "weknora_ingest", "source_ingest_url", "source_crawl_documentation"}
        manifest = json.loads((ROOT / "mod.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "3.1.0")
        self.assertEqual({t["name"] for t in server.TOOLS}, names)
        self.assertEqual({t["name"] for t in manifest["mcp_tools"]}, names)
        for actual, declared in zip(server.TOOLS, manifest["mcp_tools"]):
            self.assertEqual(actual["inputSchema"]["required"], declared["inputSchema"]["required"])
            for key, prop in declared["inputSchema"]["properties"].items():
                self.assertEqual(actual["inputSchema"]["properties"][key]["type"], prop["type"])
                if "enum" in prop:
                    self.assertEqual(actual["inputSchema"]["properties"][key]["enum"], prop["enum"])
        self.assertNotIn("WEKNORA_BASE_URL", manifest["mcp_servers"]["source_connectors"]["env"])
        self.assertNotIn("WEKNORA_API_KEY", manifest["mcp_servers"]["source_connectors"]["env"])

    def test_source_connect_status_search_read_and_repository_operations(self):
        self.seed()
        self.assertTrue(server.dispatch("source_connect", {"operation": "status"})["weknora"]["alive"])
        hits = server.dispatch("source_connect", {"operation": "knowledge_search", "query": "防黑框弹窗"})["matches"]
        note = server.dispatch("source_connect", {"operation": "document_read", "path": hits[0]["note_id"]})
        self.assertIn("CREATE_NO_WINDOW", note["knowledge"]["content"])
        section = server.dispatch("source_connect", {"operation": "document_read", "path": hits[0]["section_id"]})
        json.dumps(section)
        path = self.notes / "Windows.md"
        self.assertIn("CREATE_NO_WINDOW", server.dispatch("source_connect", {"operation": "repository_read", "path": str(path)})["content"])
        self.assertEqual(len(server.dispatch("source_connect", {"operation": "repository_search", "path": str(self.notes), "query": "Windows"})["matches"]), 1)

    def test_manage_health_list_create_list_docs_and_category_scope(self):
        self.seed()
        self.assertTrue(server.dispatch("weknora_kb_manage", {"action": "health"})["alive"])
        created = server.dispatch("weknora_kb_manage", {"action": "create", "kb": "Custom", "description": "Desc"})
        self.assertEqual(created["id"], "Custom")
        listed = server.dispatch("weknora_kb_manage", {"action": "list"})["knowledge_bases"]
        self.assertIn("Custom", [kb["id"] for kb in listed])
        added = server.dispatch("weknora_ingest", {"title": "Scoped", "content": "# Scoped\nCREATE_NO_WINDOW", "kb": "Custom"})
        self.assertTrue(added["result"]["id"].startswith("Custom/"))
        docs = server.dispatch("weknora_kb_manage", {"action": "list_docs", "kb": "Custom"})["documents"]
        self.assertEqual([d["id"] for d in docs], [added["result"]["id"]])

    def test_rag_search_and_chat_with_legacy_kb_id(self):
        self.seed()
        search = server.dispatch("weknora_rag_query", {"query": "防黑框弹窗", "top_k": 1, "kb": "historical-service-uuid"})
        self.assertEqual(len(search["matches"]), 1)
        for key in ("id", "knowledge_id", "knowledge_title", "content", "kb_id", "score"):
            self.assertIn(key, search["matches"][0])
        chat = server.dispatch("weknora_rag_query", {"query": "防黑框弹窗", "mode": "chat"})
        self.assertIn("CREATE_NO_WINDOW", chat["answer"])
        self.assertTrue(chat["references"])
        self.assertIn("session_id", chat)

    def test_ingest_markdown_and_url_dispatch(self):
        added = server.dispatch("weknora_ingest", {"title": "Local", "content": "# Local\nEvidence"})
        self.assertTrue(Path(added["result"]["vault_path"]).is_file())
        with mock.patch.object(server, "extract_webpage_markdown", return_value={"title": "External", "markdown": "# External\nEvidence"}):
            result = server.dispatch("weknora_ingest", {"url": "https://example.test/article"})
        metadata = vault.parse_frontmatter(Path(result["result"]["vault_path"]).read_text(encoding="utf-8"))[0]
        self.assertEqual(metadata["source_url"], "https://example.test/article")
        with self.assertRaises(ValueError):
            server.dispatch("weknora_ingest", {})

    def test_source_ingest_url_web_save_flag(self):
        with mock.patch.object(server, "extract_webpage_markdown", side_effect=lambda url: {"url": url, "type": "webpage", "title": "Article", "markdown": "# Article\nBody"}):
            result = server.dispatch("source_ingest_url", {"url": "https://example.test", "save_to_weknora": True})
            self.assertIn("weknora_ingest", result)
            self.assertTrue(Path(result["weknora_ingest"]["result"]["vault_path"]).is_relative_to(self.notes))
            only_extract = server.dispatch("source_ingest_url", {"url": "https://example.test"})
            self.assertNotIn("weknora_ingest", only_extract)

    def test_source_ingest_url_video_contract(self):
        with mock.patch.object(server, "inspect_bilibili_video", return_value={"title": "Video", "bvid": "BVTEST", "owner": "Author", "desc": "Description"}) as video:
            result = server.dispatch("source_ingest_url", {"url": "BVTEST", "download_mp4_to": str(self.base / "video.mp4"), "save_to_weknora": True})
        video.assert_called_once()
        self.assertEqual(result["type"], "bilibili_video")
        self.assertIn("weknora_ingest", result)

    def test_sync_uses_only_mirror_collector_and_keeps_stdout_clean(self):
        fixture = self.base / "sync_gdrive_to_weknora.py"
        fixture.write_text(
            "from pathlib import Path\nprint('module log')\n"
            "def collect_curated_md_documents():\n"
            "    print('mirror log')\n"
            f"    Path({str(self.notes / 'Synced.md')!r}).write_text('# Synced\\nCREATE_NO_WINDOW', encoding='utf-8')\n"
            "    return [{'title': 'Synced'}]\n"
            "def main():\n    raise AssertionError('main must not be called')\n"
            "def sync_dual_layer_memories():\n    raise AssertionError('memory writes forbidden')\n"
            "class WeKnoraClient:\n    def __init__(self):\n        raise AssertionError('legacy services forbidden')\n",
            encoding="utf-8",
        )
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(server, "KB_SYNC_SCRIPT", fixture), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = server.dispatch("weknora_kb_manage", {"action": "sync_gdrive"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["total_notes"], 1)
        self.assertEqual(result["total_collected"], 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("mirror log", err.getvalue())
        self.assertTrue((self.base / "state.json").is_file())

    def test_sync_missing_script_still_refreshes_index(self):
        self.write("Local.md", "# Local")
        result = server._run_kb_sync_now()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["total_notes"], 1)
        self.assertEqual(result["mirror_error"], "sync_script_not_found")

    def test_custom_vault_sync_never_loads_production_collector_or_state(self):
        self.write("Isolated.md", "# Isolated")
        with mock.patch.dict(os.environ, {"OBSIDIAN_SYNC_SCRIPT": "", "OBSIDIAN_SYNC_STATE": ""}), mock.patch.object(server.importlib.util, "spec_from_file_location", side_effect=AssertionError("Production collector forbidden")):
            result = server._run_kb_sync_now()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["mirror_error"], "custom_vault_requires_OBSIDIAN_SYNC_SCRIPT")
        self.assertTrue((self.notes / ".obsidian_sync_state.json").is_file())
        self.assertFalse((self.base / "state.json").exists())

    def test_no_legacy_services_and_all_subprocesses_have_limits(self):
        source = (ROOT / "mcp/mcp_server.py").read_text(encoding="utf-8")
        for value in ("127.0.0.1:8080", "127.0.0.1:11434", "WEKNORA_BASE_URL", "WEKNORA_API_KEY", "_ensure_local_ollama"):
            self.assertNotIn(value, source)
        for filename in ("mcp_server.py", "obsidian_vault_engine.py"):
            tree = ast.parse((ROOT / "mcp" / filename).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess":
                    self.assertIn(node.func.attr, ("run",), f"Review subprocess lifecycle: {filename}:{node.lineno}")
                    args = {kw.arg for kw in node.keywords}
                    self.assertTrue({"timeout", "creationflags"} <= args, f"Unbounded or visible process: {filename}:{node.lineno}")
        self.assertIn('getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)', source)

    def test_real_stdio_jsonrpc_round_trip_is_isolated(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "weknora_ingest", "arguments": {"title": "Round trip", "content": "# Round trip\nCREATE_NO_WINDOW"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "weknora_rag_query", "arguments": {"query": "防黑框弹窗"}}},
        ]
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(ROOT / "mcp/mcp_server.py")],
            input="\n".join(json.dumps(req, ensure_ascii=False) for req in requests) + "\n",
            capture_output=True, text=True, encoding="utf-8", env={**os.environ, **self.env},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        replies = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4])
        self.assertEqual(replies[0]["result"]["serverInfo"]["version"], "3.1.0")
        self.assertFalse(replies[3]["result"]["isError"])
        hits = json.loads(replies[3]["result"]["content"][0]["text"])["matches"]
        self.assertIn("CREATE_NO_WINDOW", hits[0]["content"])

    def test_unknown_tool_fails_explicitly(self):
        with self.assertRaises(ValueError):
            server.dispatch("unknown", {})


class V31GraphTests(IsolatedVaultTest):
    def chain(self, reverse=False):
        self.write("A.md", "# A\nquartzsignal\n" + ("" if reverse else "[[B]]"))
        self.write("B.md", "# B\n" + ("[[A]]" if reverse else "[[C]]"))
        self.write("C.md", "# C\nmarigold\n" + ("[[B]]" if reverse else "[[D]]"))
        self.write("D.md", "# D\norchid")
        self.engine.sync_vault_index()

    def search(self, hops=2, **kwargs):
        # Disable incidental feature-hash similarity to isolate graph-only recall.
        with mock.patch.object(vault._EMBEDDER, "encode_vector", return_value=[0.0] * 384):
            return self.engine.hybrid_search("quartzsignal", graph_hops=hops, **kwargs)

    def test_two_hop_recalls_second_order_but_not_third(self):
        self.chain()
        first = self.search(1)
        second = self.search()
        self.assertEqual({m["note_id"] for m in first["matches"]}, {"A.md", "B.md"})
        self.assertEqual({m["note_id"] for m in second["matches"]}, {"A.md", "B.md", "C.md"})
        self.assertEqual(second["graph_hops"], 2)
        self.assertEqual(second["graph_activated_notes"], 2)
        scores = {m["note_id"]: m["score"] for m in second["matches"]}
        self.assertAlmostEqual(scores["C.md"] / scores["B.md"], 0.45 * 31 / 32, delta=0.01)

    def test_two_hop_follows_backlinks_and_stops_cycles(self):
        self.chain(reverse=True)
        result = self.search(5)
        self.assertEqual({m["note_id"] for m in result["matches"]}, {"A.md", "B.md", "C.md"})
        self.assertEqual(result["graph_hops"], 5)
        self.assertEqual(result["graph_activated_notes"], 2)

    def test_graph_category_filter_and_empty_statistics(self):
        self.chain()
        self.assertEqual(self.search(category_or_kb="missing")["graph_activated_notes"], 0)
        for query in ("", " "):
            result = self.engine.hybrid_search(query, graph_hops=1)
            self.assertEqual((result["graph_hops"], result["graph_activated_notes"]), (1, 0))
        with self.assertRaises(ValueError):
            self.engine.hybrid_search("query", graph_hops=0)

    def test_rag_and_bridge_forward_hops_in_search_and_chat(self):
        self.chain()
        for mode in ("search", "chat"):
            result = server.call_tool("weknora_rag_query", {"query": "quartzsignal", "mode": mode, "graph_hops": 1})
            self.assertEqual(result["graph_hops"], 1)
            self.assertIn("graph_activated_notes", result)


class V31WebTests(IsolatedVaultTest):
    def extract(self, html_text, mode="auto", **kwargs):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = html_text.encode("utf-8")
        response.info.return_value = {}
        response.geturl.return_value = "https://example.test/docs/start"
        with mock.patch("urllib.request.urlopen", return_value=response) as opened:
            result = server.extract_webpage_markdown("https://example.test/docs/start", mode, **kwargs)
            self.assertEqual(opened.call_args.kwargs["timeout"], kwargs.get("timeout", 15))
        return result

    def test_spa_shell_requires_marker_and_short_visible_body(self):
        for text in ('<div id="app"></div>', '<div id="root"></div>', '<script id="__NEXT_DATA__">{}</script>', '<script>window.__INITIAL_STATE__={};</script>', '<noscript>Please enable JavaScript</noscript>'):
            self.assertTrue(server._is_spa_shell(text), text)
        self.assertFalse(server._is_spa_shell("<p>Short static article</p>"))
        self.assertFalse(server._is_spa_shell('<div id="root">' + "Real content " * 30 + "</div>"))

    def test_next_data_hydration_and_http_fast_opt_out(self):
        state = {"props": {"pageProps": {"article": {"title": "Guide", "content": "<h2>Install</h2><p>Package instructions.</p>"}}}}
        text = '<title>Docs</title><div id="root"></div><script id="__NEXT_DATA__" type="application/json">' + json.dumps(state) + "</script>"
        result = self.extract(text)
        self.assertEqual(result["render_tier_used"], "spa_state_hydration")
        self.assertTrue(result["spa_shell_detected"])
        self.assertIn("## Install", result["markdown"])
        self.assertNotIn("pageProps", result["markdown"])
        self.assertNotIn("Package instructions", self.extract(text, "http_fast")["markdown"])

    def test_jsonld_initial_state_and_template_hydration(self):
        variants = [
            '<div id="app"></div><script type="application/ld+json">{"articleBody":"Trusted article text"}</script>',
            '<div id="app"></div><script>window.__INITIAL_STATE__ = {"content":"Trusted article text"};</script>',
            '<div id="app"></div><template><h4>Trusted article text</h4></template>',
        ]
        for text in variants:
            result = self.extract(text)
            self.assertEqual(result["render_tier_used"], "spa_state_hydration")
            self.assertIn("Trusted article text", result["markdown"])

    def test_noise_strip_preserves_headings_code_tables_and_source_links(self):
        text = '''<title>Article</title><nav>MENU<a href="/docs/next">Next</a></nav><header>HEADER</header><footer>FOOTER</footer><aside>SIDEBAR</aside><div class="cookie-consent">COOKIES</div><div id="ad-banner">ADVERTISEMENT</div><script>TRACKER</script><style>STYLES</style><article><h1>One</h1><h2>Two</h2><h3>Three</h3><h4>Four</h4><pre><code class="language-python">if x &lt; 2:
    print("ok")

    print("done")</code></pre><table><tr><th>Name</th><th>Value</th></tr><tr><td>A</td><td>1</td></tr></table></article>'''
        result = self.extract(text)
        for noise in ("MENU", "HEADER", "FOOTER", "SIDEBAR", "COOKIES", "ADVERTISEMENT", "TRACKER", "STYLES"):
            self.assertNotIn(noise, result["markdown"])
        for fragment in ("# One", "## Two", "### Three", "#### Four", '```python\nif x < 2:\n    print("ok")\n\n    print("done")\n```', "| Name | Value |", "| --- | --- |", "| A | 1 |"):
            self.assertIn(fragment, result["markdown"])
        self.assertGreater(result["noise_stripped_chars"], 30)
        self.assertIn("/docs/next", result["links"])

    def test_optional_renderer_and_safe_no_browser_fallback(self):
        text = '<div id="app"></div>'
        with mock.patch.object(server, "DOM_RENDER_HOOK", None), mock.patch.object(server.subprocess, "run", side_effect=AssertionError("Browser launch forbidden")):
            result = self.extract(text, "headless_dom")
            self.assertEqual(result["render_tier_used"], "http_fast")
        def render(url, timeout):
            print("renderer log")
            return "<h2>Rendered content</h2>"
        stdout = io.StringIO()
        with mock.patch.object(server, "DOM_RENDER_HOOK", render), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            result = self.extract(text, "headless_dom", timeout=7)
        self.assertEqual(result["render_tier_used"], "headless_dom")
        self.assertIn("## Rendered content", result["markdown"])
        self.assertEqual(stdout.getvalue(), "")
        with mock.patch.object(server, "DOM_RENDER_HOOK", side_effect=RuntimeError("not installed")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.extract(text)["render_tier_used"], "http_fast")

    def test_sha256_is_stable_and_tracks_cleaned_body_changes(self):
        first = self.extract("<p>Evidence</p><nav>Old navigation</nav>")
        second = self.extract("<p>Evidence</p><nav>New navigation</nav>")
        changed = self.extract("<p>Different evidence</p>")
        self.assertEqual(first["content_sha256"], hashlib.sha256(b"Evidence").hexdigest())
        self.assertEqual(first["content_sha256"], second["content_sha256"])
        self.assertNotEqual(first["content_sha256"], changed["content_sha256"])

    def test_metadata_only_state_does_not_block_dom_rendering(self):
        text = '<div id="root"></div><script id="__NEXT_DATA__">{"props":{"pageProps":{"title":"Docs","description":"Website metadata"}}}</script>'
        with mock.patch.object(server, "DOM_RENDER_HOOK", return_value="<h2>Actual document body</h2>") as render:
            result = self.extract(text)
        render.assert_called_once()
        self.assertEqual(result["render_tier_used"], "headless_dom")
        self.assertIn("Actual document body", result["markdown"])

    def test_source_ingest_url_forwards_render_options_and_legacy_fields(self):
        extracted = {"title": "Page", "markdown": "# Page", "type": "webpage"}
        with mock.patch.object(server, "extract_webpage_markdown", return_value=extracted) as extract:
            result = server.call_tool("source_ingest_url", {"url": "https://example.test/", "render_mode": "http_fast", "timeout": 6, "save_to_weknora": True})
        extract.assert_called_once_with("https://example.test/", render_mode="http_fast", timeout=6)
        self.assertIn("weknora_ingest", result)
        self.assertTrue(list(self.notes.rglob("*.md")))
        with self.assertRaises(ValueError):
            server.extract_webpage_markdown("https://example.test/", "invalid")


class V31CrawlTests(IsolatedVaultTest):
    START = "https://example.test/docs/start"

    def pages(self, url):
        name = url.rsplit("/", 1)[-1]
        links = {
            "start": ["b#one", "b#two", "/docs/c", "https://other.test/docs/no", "/outside/no", "mailto:someone@test", "image.png", "https://example.test/docs/../escape", "/docs/%2e%2e/escape"],
            "b": ["d", "start"], "c": ["d"], "d": ["too-deep"],
        }.get(name, [])
        body = "Unique page " + name
        return {"url": url, "title": name, "markdown": "# " + body, "links": links,
                "content_sha256": hashlib.sha256(body.encode()).hexdigest()}

    def crawl(self, **kwargs):
        return server.crawl_documentation(self.START, **kwargs)

    def test_bfs_same_prefix_depth_fragment_and_page_bounds(self):
        with mock.patch.object(server, "extract_webpage_markdown", side_effect=self.pages) as extract:
            result = self.crawl(max_pages=20, max_depth=2, save_to_vault=False)
        urls = [call.args[0] for call in extract.call_args_list]
        self.assertEqual(urls, [self.START, "https://example.test/docs/b", "https://example.test/docs/c", "https://example.test/docs/d"])
        self.assertEqual(result["pages_crawled"], 4)
        self.assertEqual(result["pages_ingested"], 0)
        self.assertEqual(result["url_prefix"], "https://example.test/docs/")
        self.assertEqual(result["discovered_urls"], urls)
        self.assertEqual(Path(result["state_file"]), self.base / "crawl.json")
        self.assertFalse(list(self.notes.rglob("*.md")))

    def test_page_limit_and_resume_prioritize_unfinished_queue(self):
        with mock.patch.object(server, "extract_webpage_markdown", side_effect=self.pages):
            first = self.crawl(max_pages=1)
        self.assertEqual(first["pages_ingested"], 1)
        state = json.loads((self.base / "crawl.json").read_text(encoding="utf-8"))
        self.assertEqual(next(iter(state["crawls"].values()))["queue"][0], ["https://example.test/docs/b", 1])
        with mock.patch.object(server, "extract_webpage_markdown", side_effect=self.pages) as extract:
            second = self.crawl(max_pages=1, resume=True)
        self.assertEqual(extract.call_args.args[0], "https://example.test/docs/b")
        self.assertEqual(second["pages_ingested"], 1)

    def test_sha256_unchanged_skips_ingest_changed_content_reingests(self):
        with mock.patch.object(server, "extract_webpage_markdown", side_effect=self.pages):
            self.assertEqual(self.crawl(max_depth=0)["pages_ingested"], 1)
            second = self.crawl(max_depth=0)
        self.assertEqual(second["skipped_cached_urls"], [self.START])
        self.assertEqual(second["pages_ingested"], 0)
        changed = self.pages(self.START)
        changed.update(markdown="# Updated evidence", content_sha256=hashlib.sha256(b"updated").hexdigest())
        with mock.patch.object(server, "extract_webpage_markdown", return_value=changed):
            self.assertEqual(self.crawl(max_depth=0)["pages_ingested"], 1)

    def test_preview_then_save_and_kb_scopes_do_not_share_ingest_cache(self):
        with mock.patch.object(server, "extract_webpage_markdown", side_effect=self.pages):
            self.crawl(max_depth=0, save_to_vault=False)
            self.assertEqual(self.crawl(max_depth=0)["pages_ingested"], 1)
            server.call_tool("weknora_kb_manage", {"action": "create", "kb": "Other"})
            self.assertEqual(self.crawl(max_depth=0, kb="Other")["pages_ingested"], 1)
            self.assertEqual(self.crawl(max_depth=0, resume=False)["pages_ingested"], 1)

    def test_duplicate_body_across_urls_is_ingested_once(self):
        def pages(url):
            result = self.pages(url)
            result["content_sha256"] = hashlib.sha256(b"same content").hexdigest()
            return result
        with mock.patch.object(server, "extract_webpage_markdown", side_effect=pages):
            result = self.crawl()
        self.assertEqual(result["pages_crawled"], 4)
        self.assertEqual(result["pages_ingested"], 1)
        self.assertEqual(len(result["skipped_cached_urls"]), 3)

    def test_same_title_pages_keep_separate_notes_and_bad_links_are_ignored(self):
        def pages(url):
            result = self.pages(url)
            result["title"] = "Shared documentation title" * 5
            result["links"].extend(["https://[invalid", "https://example.test:bad/docs/oops"])
            return result
        with mock.patch.object(server, "extract_webpage_markdown", side_effect=pages):
            result = self.crawl()
        self.assertEqual(result["pages_ingested"], 4)
        self.assertEqual(len(list(self.notes.rglob("*.md"))), 4)
        self.assertTrue(all(item["status"] == "ingested" for item in result["crawl_results"]))

    def test_interruption_retains_inflight_url_for_resume(self):
        def interrupt(url):
            if url.endswith("/b"):
                raise KeyboardInterrupt()
            return self.pages(url)
        with mock.patch.object(server, "extract_webpage_markdown", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.crawl()
        with mock.patch.object(server, "extract_webpage_markdown", side_effect=self.pages) as extract:
            result = self.crawl(max_pages=1)
        self.assertEqual(extract.call_args.args[0], "https://example.test/docs/b")
        self.assertEqual(result["pages_ingested"], 1)

    def test_failed_requests_are_bounded_and_can_resume(self):
        with mock.patch.object(server, "extract_webpage_markdown", side_effect=TimeoutError("offline")) as extract:
            result = self.crawl(max_pages=1)
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(result["crawl_results"][0]["status"], "error")
        with mock.patch.object(server, "extract_webpage_markdown", side_effect=self.pages):
            self.assertEqual(self.crawl(max_pages=1)["pages_ingested"], 1)

    def test_maximum_50_pages_and_depth_zero_are_enforced(self):
        def pages(url):
            result = self.pages(url)
            result["links"] = [f"page{i}" for i in range(100)]
            return result
        with mock.patch.object(server, "extract_webpage_markdown", side_effect=pages) as extract:
            result = self.crawl(max_pages=500, save_to_vault=False)
            self.assertEqual(extract.call_count, 50)
            self.assertEqual(result["pages_crawled"], 50)
        with mock.patch.object(server, "extract_webpage_markdown", side_effect=pages) as extract:
            self.crawl(max_depth=0, save_to_vault=False, resume=False)
            self.assertEqual(extract.call_count, 1)

    def test_default_state_is_inside_custom_vault_and_invalid_bounds_fail(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("OBSIDIAN_CRAWL_STATE")
            with mock.patch.object(server, "extract_webpage_markdown", side_effect=self.pages):
                result = self.crawl(max_depth=0, save_to_vault=False)
        self.assertEqual(Path(result["state_file"]), self.notes / ".obsidian_crawl_state.json")
        for kwargs in ({"max_pages": 0}, {"max_depth": -1}, {"url_prefix": "https://other.test/docs/"}):
            with self.assertRaises(ValueError):
                self.crawl(**kwargs)

    def test_six_schemas_and_stdio_crawl_error_protocol(self):
        manifest = json.loads((ROOT / "mod.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["mcp_tools"], server.TOOLS)
        self.assertIs(server.call_tool, server.dispatch)
        for tool in server.TOOLS:
            self.assertEqual(tool["inputSchema"], tool["parameters"])
            self.assertEqual(json.loads((ROOT / "mcp/schemas" / (tool["name"] + ".json")).read_text(encoding="utf-8")), tool)
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "source_crawl_documentation", "arguments": {"start_url": self.START, "max_pages": 0}}},
            {"jsonrpc": "2.0", "id": 4, "method": "ping"},
        ]
        proc = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "mcp/mcp_server.py")],
                              input="\n".join(json.dumps(req) for req in requests) + "\n", capture_output=True,
                              text=True, encoding="utf-8", env={**os.environ, **self.env}, timeout=30,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        replies = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3, 4])
        self.assertEqual(replies[0]["result"]["serverInfo"]["version"], "3.1.0")
        self.assertEqual(len(replies[1]["result"]["tools"]), 6)
        self.assertTrue(replies[2]["result"]["isError"])
        self.assertEqual(replies[3]["result"], {})

    def test_redirect_boundary_blocks_followup_before_network(self):
        handler = server._CrawlRedirectHandler(lambda url: url.startswith("https://example.test/docs/"))
        request = server.urllib.request.Request(self.START)
        for url in ("https://other.test/docs/page", "https://example.test/docs/../escape", "https://example.test/docs/%2e%2e/escape"):
            with self.assertRaisesRegex(ValueError, "redirect_outside_crawl_boundary"):
                handler.redirect_request(request, None, 302, "Found", {}, url)
        redirect = handler.redirect_request(request, None, 302, "Found", {}, "https://example.test/docs/b")
        self.assertEqual(redirect.full_url, "https://example.test/docs/b")

    def test_successful_stdio_crawl_with_mock_http_and_indexing(self):
        # The child runs the real stdio loop, extractor and crawler; HTTP is mocked.
        program = '''
import sys
from unittest import mock
sys.path.insert(0, sys.argv[1])
import mcp_server
def fetch(opener, request, **kwargs):
    url = request.full_url
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.geturl.return_value = url
    response.info.return_value = {}
    response.read.return_value = (b'<title>Start</title><nav>Noise</nav><h2>First evidence</h2><a href="b">Next</a>' if url.endswith('/start') else b'<title>Second</title><h3>Second evidence</h3>')
    return response
with mock.patch('urllib.request.OpenerDirector.open', autospec=True, side_effect=fetch):
    mcp_server.main()
'''
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "source_crawl_documentation", "arguments": {"start_url": self.START, "max_pages": 2}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "weknora_rag_query", "arguments": {"query": "Second evidence"}}},
        ]
        proc = subprocess.run([sys.executable, "-X", "utf8", "-c", program, str(ROOT / "mcp")],
                              input="\n".join(json.dumps(req) for req in requests) + "\n", capture_output=True,
                              text=True, encoding="utf-8", env={**os.environ, **self.env}, timeout=30,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        replies = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2])
        self.assertFalse(replies[0]["result"]["isError"])
        result = json.loads(replies[0]["result"]["content"][0]["text"])
        self.assertEqual((result["pages_crawled"], result["pages_ingested"]), (2, 2))
        hits = json.loads(replies[1]["result"]["content"][0]["text"])["matches"]
        self.assertTrue(any("Second evidence" in hit["content"] for hit in hits))
        self.assertEqual(len(list(self.notes.rglob("*.md"))), 2)


if __name__ == "__main__":
    unittest.main()
