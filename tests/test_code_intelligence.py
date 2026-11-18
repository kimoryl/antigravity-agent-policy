from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "code_intelligence"
MOD_MCP = ROOT / "mods" / "code-intelligence" / "mcp"
if MOD_MCP.exists():
    sys.path.insert(0, str(MOD_MCP))
sys.path.insert(0, str(ROOT / "scripts"))

from code_intelligence import (  # noqa: E402
    architecture_extract,
    capability_catalog,
    code_search,
    codegraph_explore,
    impact_analysis,
    symbol_trace,
)


class CodeIntelligenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        self.cache_db = Path(self.temp.name) / "test_codegraph_cache.sqlite3"
        os.environ["CODE_INTELLIGENCE_CACHE_PATH"] = str(self.cache_db)
        shutil.copytree(FIXTURE, self.project)
        # Keep the intentionally invalid source out of the repository's
        # compileall target while still exercising parser failure handling.
        invalid_source = (self.project / "invalid_source.txt").read_text(encoding="utf-8")
        (self.project / "invalid.py").write_text(invalid_source, encoding="utf-8")

    def tearDown(self) -> None:
        os.environ.pop("CODE_INTELLIGENCE_CACHE_PATH", None)
        self.temp.cleanup()

    def test_searches_functions_classes_and_non_python_files(self) -> None:
        functions = code_search(str(self.project), "duplicate", kind="function", provider="python_ast")
        self.assertEqual({item["qualified_name"] for item in functions["results"]}, {"a.duplicate", "b.duplicate"})
        classes = code_search(str(self.project), "Alpha", kind="class", provider="python_ast")
        self.assertEqual([item["qualified_name"] for item in classes["results"]], ["a.Alpha"])
        files = code_search(str(self.project), "notes.js", provider="python_ast")
        self.assertEqual(files["results"][0]["kind"], "file")

    def test_cross_file_callers_callees_and_duplicate_short_names(self) -> None:
        target = symbol_trace(str(self.project), "target", direction="callers", provider="python_ast")
        self.assertEqual(target["resolved"], ["b.target"])
        self.assertIn("a.Alpha.caller", target["callers"])
        caller = symbol_trace(str(self.project), "a.Alpha.caller", direction="callees", provider="python_ast")
        self.assertEqual(caller["callees"], ["b.target"])
        self.assertNotIn(
            {"path": "a.py", "reason": "unresolved_call", "line": 6, "name": "b.target"},
            caller["analysis"]["incomplete"],
        )
        duplicate = symbol_trace(str(self.project), "duplicate", provider="python_ast")
        self.assertEqual(duplicate["resolved"], ["a.duplicate", "b.duplicate"])

    def test_impact_marks_incomplete_dynamic_and_syntax_analysis(self) -> None:
        result = impact_analysis(str(self.project), symbols=["b.target"], provider="python_ast")
        self.assertIn("a.py", result["affected_files"])
        self.assertFalse(result["analysis"]["complete"])
        reasons = {item["reason"] for item in result["analysis"]["incomplete"]}
        self.assertIn("syntax_error", reasons)
        self.assertIn("dynamic_call", reasons)

    def test_architecture_includes_circular_imports(self) -> None:
        result = architecture_extract(str(self.project), provider="python_ast")
        imports = {(edge["from"], edge["to"], edge["type"]) for edge in result["edges"] if edge["type"] == "imports"}
        self.assertIn(("a.py", "b.py", "imports"), imports)
        self.assertIn(("b.py", "a.py", "imports"), imports)

    def test_relative_imports_resolve_within_the_package(self) -> None:
        package = self.project / "pkg"
        package.mkdir()
        (package / "__init__.py").write_text("from . import a\n", encoding="utf-8")
        (package / "a.py").write_text("from . import b\n", encoding="utf-8")
        (package / "b.py").write_text("from . import a\n", encoding="utf-8")
        result = architecture_extract(str(self.project), provider="python_ast")
        imports = {(edge["from"], edge["to"]) for edge in result["edges"] if edge["type"] == "imports"}
        self.assertIn(("pkg/__init__.py", "pkg/a.py"), imports)
        self.assertIn(("pkg/a.py", "pkg/b.py"), imports)
        self.assertIn(("pkg/b.py", "pkg/a.py"), imports)
        self.assertNotIn(("pkg/a.py", "b.py"), imports)
        self.assertNotIn(("pkg/b.py", "a.py"), imports)

    def test_empty_and_invalid_projects_are_safe(self) -> None:
        empty = Path(self.temp.name) / "empty"
        empty.mkdir()
        result = code_search(str(empty), provider="python_ast")
        self.assertEqual(result["results"], [])
        self.assertTrue(result["analysis"]["complete"])
        file_path = Path(self.temp.name) / "not-a-project.py"
        file_path.write_text("pass", encoding="utf-8")
        with self.assertRaises(ValueError):
            code_search(str(file_path), provider="python_ast")
        with self.assertRaises((ValueError, FileNotFoundError)):
            code_search(str(Path(self.temp.name) / "missing"), provider="python_ast")

    def test_external_providers_are_explicitly_reported_as_ast_fallbacks(self) -> None:
        catalog = capability_catalog()
        providers = {item["name"]: item for item in catalog["providers"]}
        for name in ("codegraph", "gitnexus"):
            with self.subTest(provider=name):
                self.assertEqual(providers[name]["implementation"], "python_ast_fallback")
                self.assertIn("external MCP adapter", providers[name]["fallback_reason"])
                result = code_search(str(self.project), "duplicate", provider=name)
                self.assertEqual(result["provider"]["name"], name)
                self.assertEqual(result["provider"]["implementation"], "python_ast_fallback")

    def test_symlinked_outside_file_is_not_read(self) -> None:
        outside = Path(self.temp.name) / "outside.py"
        outside.write_text("def secret():\n    return 1\n", encoding="utf-8")
        link = self.project / "outside.py"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        result = code_search(str(self.project), "secret", provider="python_ast")
        self.assertEqual(result["results"], [])

    def test_polyglot_ts_go_rust_csharp_and_explore(self) -> None:
        (self.project / "service.ts").write_text(
            "import { queryDb } from './db';\n"
            "// fake function comment() {}\n"
            "export class UserService extends BaseService {\n"
            "  async fetchUser(id: string): Promise<User> {\n"
            "    return queryDb(id);\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )
        (self.project / "db.ts").write_text(
            "export const queryDb = async (id: string) => {\n"
            "  return { id };\n"
            "}\n",
            encoding="utf-8",
        )
        (self.project / "service.spec.ts").write_text(
            "import { UserService } from './service';\n"
            "export function testFetch() {\n"
            "  const svc = new UserService();\n"
            "  return svc.fetchUser('1');\n"
            "}\n",
            encoding="utf-8",
        )
        (self.project / "worker.go").write_text(
            "package main\n"
            "type Engine struct {\n}\n"
            "func (e *Engine) RunJob(id string) {\n"
            "  ComputeHash(id)\n"
            "}\n"
            "func ComputeHash(s string) string {\n"
            "  return s\n"
            "}\n",
            encoding="utf-8",
        )

        trace_res = symbol_trace(str(self.project), "queryDb", direction="callers", provider="polyglot")
        self.assertIn("service.UserService.fetchUser", trace_res["callers"])

        go_trace = symbol_trace(str(self.project), "ComputeHash", direction="callers", provider="polyglot")
        self.assertIn("worker.Engine.RunJob", go_trace["callers"])

        impact_res = impact_analysis(str(self.project), symbols=["db.queryDb"], provider="polyglot")
        self.assertIn("service.ts", impact_res["affected_files"])
        self.assertIn("service.spec.ts", impact_res["related_tests"])

        explore_res = codegraph_explore(str(self.project), "queryDb", max_depth=2, max_nodes=10)
        self.assertTrue(any(c["symbol"] == "db.queryDb" for c in explore_res["neighborhood_symbols"]))
        self.assertIn("service.spec.ts", explore_res["related_tests"])

    def test_incremental_sqlite_cache_hits_and_partial_invalidation(self) -> None:
        first = code_search(str(self.project), "Alpha", provider="polyglot")
        c1 = first["analysis"]["cache"]
        self.assertEqual(c1["cache_hits"], 0)
        self.assertGreater(c1["re_parsed"], 0)

        second = code_search(str(self.project), "Alpha", provider="polyglot")
        c2 = second["analysis"]["cache"]
        self.assertEqual(c2["re_parsed"], 0)
        self.assertEqual(c2["cache_hits"], c1["total_files"])

        # Modify a single file and verify only 1 file is re-parsed
        (self.project / "b.py").write_text(
            "from a import duplicate\ndef target():\n    return duplicate()\ndef new_helper():\n    return 42\n",
            encoding="utf-8",
        )
        third = code_search(str(self.project), "new_helper", provider="polyglot")
        c3 = third["analysis"]["cache"]
        self.assertEqual(c3["re_parsed"], 1)
        self.assertEqual(c3["cache_hits"], c1["total_files"] - 1)
        self.assertEqual(third["results"][0]["qualified_name"], "b.new_helper")


if __name__ == "__main__":
    unittest.main()
