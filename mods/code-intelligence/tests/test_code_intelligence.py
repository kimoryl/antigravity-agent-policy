"""Isolated CodeGraph v2.1 contracts, algorithms, cache and stdio regression tests."""
from __future__ import annotations

import ast
from contextlib import closing
import importlib.util
import inspect
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

MOD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MOD / "mcp"))
import code_intelligence as ci

spec = importlib.util.spec_from_file_location("code_intelligence_test_server", MOD / "mcp/mcp_server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

EXPECTED = {"code_search", "symbol_trace", "impact_analysis", "architecture_extract",
            "codegraph_explore", "code_repomap_rank", "code_health_audit"}


class CodeIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.cache = self.base / "state/cache.sqlite3"
        self.baseline = self.base / "state/baseline.json"
        self.environment = patch.dict(os.environ, {
            "CODE_INTELLIGENCE_CACHE_PATH": str(self.cache),
            "CODE_INTELLIGENCE_BASELINE_PATH": str(self.baseline),
            "CODE_INTELLIGENCE_DISABLE_CACHE": "0",
            "CODE_INTELLIGENCE_MAX_FILES": "350",
            "CODE_INTELLIGENCE_PROVIDER": "polyglot",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write(self, path, source):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
        return target

    def graph(self):
        return ci._CodeGraph(self.root)

    def audit(self, **kwargs):
        return ci.code_health_audit(str(self.root), **kwargs)

    def ranks(self, **kwargs):
        return ci.code_repomap_rank(str(self.root), **kwargs)

    def ranking_fixture(self):
        self.write("core.py", """
            def core(value: int = 0) -> int: return value
            def leaf(): return 0
            def caller_a(): return core(1) + core(2) + core(3)
            def caller_b(): return core(4)
            def caller_c(): return core(5)
        """)
        self.write("other.py", "def isolated(): return 1\n")

    def polyglot_fixture(self):
        self.write("app.py", """
            def py_leaf(): return 1
            def py_entry(): return py_leaf()
            class Parent: pass
            class Child(Parent): pass
        """)
        self.write("web.ts", """
            function ts_leaf(value: number) { return value; }
            export function ts_entry() { return ts_leaf(1); }
            export const arrow = (value: number) => { return ts_leaf(value); };
            class Widget {
                run() { return ts_leaf(2); }
            }
        """)
        self.write("calc.go", """
            package calc
            func go_leaf() int { return 1 }
            func GoEntry() int { return go_leaf() }
            type Widget struct { value int }
            func (w *Widget) Run() int { return go_leaf() }
        """)
        self.write("worker.rs", """
            fn rs_leaf() -> i32 { 1 }
            pub fn rs_entry() -> i32 { rs_leaf() }
            pub struct Worker { value: i32 }
        """)

    def test_original_public_function_signatures(self):
        expected = {
            "code_search": ["project_root", "query", "kind", "provider", "max_results"],
            "symbol_trace": ["project_root", "symbol", "direction", "provider"],
            "impact_analysis": ["project_root", "paths", "symbols", "diff", "provider"],
            "architecture_extract": ["project_root", "provider"],
            "codegraph_explore": ["project_root", "query", "max_depth", "max_nodes", "provider"],
        }
        for name, parameters in expected.items():
            self.assertEqual(list(inspect.signature(getattr(ci, name)).parameters), parameters)
        self.assertEqual(inspect.signature(ci.code_search).parameters["max_results"].default, 50)
        self.assertEqual(inspect.signature(ci.codegraph_explore).parameters["provider"].default, "polyglot")

    def test_python_symbols_and_typed_signatures(self):
        self.polyglot_fixture()
        symbols = self.graph().symbols
        self.assertIn("app.Child", symbols)
        self.assertEqual(symbols["app.py_entry"]["signature"], "py_entry()")
        self.write("typed.py", "def typed(x: int, /, *, y: str = 'a') -> bool: return True\n")
        self.assertIn("*, y: str", self.graph().symbols["typed.typed"]["signature"])

    def test_typescript_symbols_calls_and_arrow(self):
        self.polyglot_fixture()
        graph = self.graph()
        self.assertIn("web.Widget.run", graph.symbols)
        self.assertIn("web.arrow", graph.symbols)
        self.assertIn("web.ts_leaf", graph.trace("web.ts_entry")["callees"])

    def test_go_symbols_calls_and_receiver(self):
        self.polyglot_fixture()
        graph = self.graph()
        self.assertIn("calc.Widget.Run", graph.symbols)
        self.assertIn("calc.go_leaf", graph.trace("calc.GoEntry")["callees"])

    def test_rust_symbols_and_calls(self):
        self.polyglot_fixture()
        graph = self.graph()
        self.assertIn("worker.Worker", graph.symbols)
        self.assertIn("worker.rs_leaf", graph.trace("worker.rs_entry")["callees"])

    def test_multilanguage_impact_and_cache_hits(self):
        self.polyglot_fixture()
        first, second = self.graph(), self.graph()
        self.assertEqual(first.cache_stats["re_parsed"], 4)
        self.assertEqual(second.cache_stats["cache_hits"], 4)
        self.assertEqual(second.cache_stats["re_parsed"], 0)
        for leaf, entry in (("app.py_leaf", "app.py_entry"), ("web.ts_leaf", "web.ts_entry"),
                            ("calc.go_leaf", "calc.GoEntry"), ("worker.rs_leaf", "worker.rs_entry")):
            with self.subTest(leaf=leaf):
                self.assertIn(entry, second.impact(symbols=[leaf])["affected_symbols"])
        self.assertTrue(self.cache.is_file())
        self.assertFalse((self.root / ".codegraph").exists())

    def test_cache_updates_only_changed_file_and_removes_deleted(self):
        self.polyglot_fixture()
        self.graph()
        self.write("app.py", "def replacement(): pass\n")
        changed = self.graph()
        self.assertEqual(changed.cache_stats["re_parsed"], 1)
        self.assertEqual(changed.cache_stats["cache_hits"], 3)
        self.assertNotIn("app.py_leaf", changed.symbols)
        (self.root / "worker.rs").unlink()
        self.assertNotIn("worker.rs_entry", self.graph().symbols)
        with closing(sqlite3.connect(self.cache)) as connection:
            rows = connection.execute("SELECT rel_path FROM file_ast_cache").fetchall()
        self.assertNotIn(("worker.rs",), rows)

    def test_cache_version_ignores_legacy_payload(self):
        self.write("a.py", "def fn(): pass\n")
        self.graph()
        with closing(sqlite3.connect(self.cache)) as connection:
            with connection:
                connection.execute("UPDATE file_ast_cache SET mode = 'polyglot'")
        graph = self.graph()
        self.assertEqual(graph.cache_stats["re_parsed"], 1)
        self.assertEqual(graph.symbols["a.fn"]["cyclomatic_complexity"], 1)

    def test_cross_file_alias_resolution_and_transitive_impact(self):
        self.write("a.py", "def core(): pass\n")
        self.write("b.py", "from a import core as run\ndef middle(): run()\n")
        self.write("test_flow.py", "from b import middle\ndef test_flow(): middle()\n")
        trace = ci.symbol_trace(str(self.root), "a.core")
        self.assertEqual(trace["callers"], ["b.middle"])
        impact = ci.impact_analysis(str(self.root), symbols=["a.core"])
        self.assertEqual(impact["affected_files"], ["a.py", "b.py", "test_flow.py"])
        self.assertEqual(impact["related_tests"], ["test_flow.py"])

    def test_pagerank_core_beats_leaf(self):
        self.ranking_fixture()
        result = self.ranks()
        scores = {item["symbol"]: item["pagerank"] for item in result["ranked_symbols"]}
        self.assertGreater(scores["core.core"], scores["core.leaf"] * 3)
        self.assertEqual(result["ranked_symbols"][0]["symbol"], "core.core")
        self.assertEqual(result["ranked_symbols"][0]["in_degree"], 3)
        self.assertEqual(result["ranked_symbols"][0]["out_degree"], 0)

    def test_pagerank_focus_symbols_boosts_isolated_symbol(self):
        self.ranking_fixture()
        normal = {item["symbol"]: item["pagerank"] for item in self.ranks()["ranked_symbols"]}
        focused = self.ranks(focus_symbols=["other.isolated"])
        scores = {item["symbol"]: item["pagerank"] for item in focused["ranked_symbols"]}
        self.assertGreater(scores["other.isolated"], normal["other.isolated"] * 3)
        self.assertEqual(focused["ranked_symbols"][0]["symbol"], "other.isolated")

    def test_pagerank_focus_files_boosts_module_and_members(self):
        self.ranking_fixture()
        normal = self.ranks()
        focused = self.ranks(focus_files=[str(self.root / "other.py")])
        self.assertEqual(focused["ranked_symbols"][0]["symbol"], "other.isolated")
        before = {item["module"]: item["pagerank"] for item in normal["ranked_modules"]}
        after = {item["module"]: item["pagerank"] for item in focused["ranked_modules"]}
        self.assertGreater(after["other"], before["other"])

    def test_pagerank_normalized_dangling_nodes_and_determinism(self):
        self.ranking_fixture()
        first = self.graph().pagerank()
        self.assertAlmostEqual(sum(first.values()), 1.0)
        self.assertEqual(first, self.graph().pagerank())
        self.assertTrue(all(isinstance(value, float) and value > 0 for value in first.values()))

    def test_repomap_budget_zero_tiny_unicode_and_compression(self):
        self.ranking_fixture()
        self.write("中文.py", "def 签名(输入: str = '内容') -> str: return 输入\n")
        for budget in (0, 1, 31, 80, 1000):
            with self.subTest(budget=budget):
                result = self.ranks(token_budget=budget)
                self.assertLessEqual(len(result["repomap_markdown"].encode("utf-8")), budget)
                self.assertLessEqual(result["estimated_tokens"], budget)
                self.assertGreater(result["compression_vs_full_graph_pct"], 0)
        self.assertTrue(self.ranks()["repomap_markdown"])
        with self.assertRaises(ValueError):
            self.ranks(token_budget=-1)

    def test_explore_sorted_scores_digest_and_legacy_keys(self):
        self.ranking_fixture()
        result = ci.codegraph_explore(str(self.root), "caller_a")
        for key in ("query", "seeds", "neighborhood_symbols", "relations", "touched_files", "affected_files", "related_tests", "cache"):
            self.assertIn(key, result)
        scores = [item["pagerank"] for item in result["neighborhood_symbols"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertIn("core.py", result["repomap_digest_markdown"])
        self.assertGreater(result["token_savings_estimate_pct"], 0)
        self.assertLessEqual(len(ci.codegraph_explore(str(self.root), "core", max_nodes=2)["neighborhood_symbols"]), 2)

    def test_trace_scores_compact_and_direction(self):
        self.ranking_fixture()
        result = ci.symbol_trace(str(self.root), "core.core", "callers")
        self.assertEqual(result["callees"], [])
        self.assertIn("core.caller_a", result["pagerank_scores"])
        self.assertIn("core.core", result["pagerank_scores"])
        self.assertIn("-> core.core", result["compact_trace_markdown"])
        missing = ci.symbol_trace(str(self.root), "missing")
        self.assertEqual(missing["pagerank_scores"], {})
        self.assertEqual(missing["compact_trace_markdown"], "")

    def test_dead_code_function_class_method_and_self_recursion(self):
        self.write("dead.py", """
            def unused(): pass
            def recursive(): recursive()
            class Unused:
                def method(self): pass
        """)
        result = self.audit()
        self.assertEqual({item["symbol"] for item in result["dead_code"]},
                         {"dead.unused", "dead.recursive", "dead.Unused", "dead.Unused.method"})
        self.assertTrue(all(item["reason"] and "signature" in item for item in result["dead_code"]))

    def test_dead_code_test_entry_visitor_and_magic_exemptions(self):
        self.write("api.py", """
            def main(): pass
            def dispatch(): pass
            def call_tool(): pass
            def handle_tool_call(): pass
            def cli(): pass
            def test_unit(): pass
            def SomeTest(): pass
            def setUpClass(): pass
            def tearDownClass(): pass
            def visit_Name(): pass
            def __magic__(): pass
        """)
        self.write("tests/helper.py", "def helper(): pass\n")
        self.write("test_helper.py", "def helper(): pass\n")
        self.assertEqual(self.audit()["dead_code"], [])

    def test_dead_code_exports_tools_tables_and_entry_points(self):
        self.write("api.py", """
            __all__ = ['public']
            __all__ += ['appended']
            TOOLS = [{'name': 'tool', 'handler': referenced}]
            TOOLS_SCHEMA: dict = {'handler': schema_handler}
            def public(): pass
            def appended(): pass
            def tool(): pass
            def referenced(): pass
            def schema_handler(): pass
            def explicit_entry(): pass
            def orphan(): pass
        """)
        result = self.audit(entry_points=["api.explicit_entry"])
        self.assertEqual([item["symbol"] for item in result["dead_code"]], ["api.orphan"])

    def test_dead_code_imported_or_inherited_symbols_are_used(self):
        self.write("api.py", "def exported_by_import(): pass\nclass Base: pass\n")
        self.write("client.py", "from api import exported_by_import\nfrom api import Base\nclass Child(Base): pass\n")
        names = {item["symbol"] for item in self.audit()["dead_code"]}
        self.assertNotIn("api.exported_by_import", names)
        self.assertNotIn("api.Base", names)
        self.assertIn("client.Child", names)

    def test_polyglot_explicit_exports(self):
        self.write("web.ts", "export function publicFn() {}\nfunction hiddenFn() {}\nfunction listed() {}\nexport { listed };\n")
        self.write("go.go", "package app\nfunc PublicFn() {}\nfunc hiddenFn() {}\n")
        self.write("rs.rs", "pub fn public_fn() {}\nfn hidden_fn() {}\n")
        dead = {item["symbol"] for item in self.audit()["dead_code"]}
        self.assertEqual(dead, {"web.hiddenFn", "go.hiddenFn", "rs.hidden_fn"})

    def test_circular_imports_a_b_a(self):
        self.write("a.py", "import b\n")
        self.write("b.py", "import a\n")
        self.assertEqual(self.audit()["circular_dependencies"], [{"cycle": ["a", "b", "a"], "length": 2}])

    def test_circular_cross_file_calls_without_imports(self):
        self.write("a.py", "def a(): b()\n")
        self.write("b.py", "def b(): a()\n")
        self.assertEqual(self.audit()["circular_dependencies"][0]["cycle"], ["a", "b", "a"])

    def test_tarjan_extracts_all_overlapping_cycles_once(self):
        self.write("a.py", "import b\nimport c\n")
        self.write("b.py", "import a\nimport c\n")
        self.write("c.py", "import a\n")
        cycles = [item["cycle"] for item in self.audit()["circular_dependencies"]]
        self.assertEqual(cycles, [["a", "b", "a"], ["a", "b", "c", "a"], ["a", "c", "a"]])

    def test_acyclic_and_same_module_calls_have_no_cycles(self):
        self.write("a.py", "import b\ndef leaf(): pass\ndef call(): leaf()\n")
        self.write("b.py", "def unused(): pass\n")
        self.assertEqual(self.audit()["circular_dependencies"], [])

    def test_python_exact_complexity_loc_and_nesting(self):
        self.write("metrics.py", """
            def branch(xs):
                if xs and len(xs) or False:
                    for x in xs:
                        while x:
                            x -= 1
                try:
                    with open('x') as f:
                        return [x for x in xs if x]
                except OSError:
                    return []
        """)
        result = self.audit(complexity_threshold=1)
        hotspot = result["complexity_hotspots"][0]
        self.assertEqual(hotspot["cyclomatic_complexity"], 10)
        self.assertEqual(hotspot["loc"], 10)
        self.assertEqual(hotspot["max_nesting_depth"], 3)
        self.assertEqual(self.audit()["complexity_hotspots"][0]["symbol"], "metrics.branch")

    def test_python_match_async_and_nested_function_independence(self):
        self.write("metrics.py", """
            async def outer(x):
                def inner(y):
                    if y: return 1
                    return 0
                match x:
                    case 1: return inner(x)
                    case _: return 0
        """)
        symbols = self.graph().symbols
        self.assertEqual(symbols["metrics.outer"]["cyclomatic_complexity"], 3)
        self.assertEqual(symbols["metrics.outer.inner"]["cyclomatic_complexity"], 2)

    def test_polyglot_complexity_and_string_comment_masking(self):
        for path, prefix in (("metrics_ts.ts", "function branch(x)"), ("metrics_go.go", "func branch(x int)"),
                             ("metrics_rs.rs", "fn branch(x: i32)")):
            self.write(path, prefix + ' {\n if x && x {\n while x || x { call(); }\n }\n // if while &&\n "if && while";\n}\n')
        hotspots = self.audit(complexity_threshold=5)["complexity_hotspots"]
        self.assertEqual(len(hotspots), 3)
        for item in hotspots:
            self.assertEqual(item["cyclomatic_complexity"], 5)
            self.assertEqual(item["max_nesting_depth"], 2)
            self.assertEqual(item["loc"], 7)

    def test_hotspots_sort_descending_and_threshold_inclusive(self):
        self.write("hot.py", "def warm(x):\n if x: return 1\n\ndef hot(x):\n if x:\n  while x: x -= 1\n")
        hotspots = self.audit(complexity_threshold=2)["complexity_hotspots"]
        self.assertEqual([item["cyclomatic_complexity"] for item in hotspots], [3, 2])

    def test_health_metrics_survive_cache_hit(self):
        self.write("a.py", "__all__ = ['fn']\ndef fn(x):\n if x: return 1\n")
        first = self.audit(complexity_threshold=2)
        second = self.audit(complexity_threshold=2)
        self.assertEqual(first["complexity_hotspots"], second["complexity_hotspots"])
        self.assertEqual(second["dead_code"], [])
        self.assertEqual(second["analysis"]["cache"]["re_parsed"], 0)

    def test_baseline_save_env_and_unchanged_comparison(self):
        self.write("a.py", "def old(): pass\n")
        saved = self.audit(save_baseline=True)
        self.assertTrue(saved["baseline_diff"]["baseline_saved"])
        self.assertTrue(self.baseline.is_file())
        self.assertFalse(self.audit()["baseline_diff"]["regression_detected"])

    def test_baseline_new_dead_code_and_resolved(self):
        self.write("a.py", "def old(): pass\n")
        self.audit(save_baseline=True)
        self.write("a.py", "def new(): pass\n")
        diff = self.audit()["baseline_diff"]
        self.assertEqual(diff["new_dead_code"][0]["symbol"], "a.new")
        self.assertEqual(diff["resolved_dead_code"][0]["symbol"], "a.old")
        self.assertTrue(diff["regression_detected"])

    def test_baseline_new_cycles_and_hotspots(self):
        self.write("a.py", "import b\ndef fn(x): return x\n")
        self.write("b.py", "pass\n")
        self.audit(complexity_threshold=2, save_baseline=True)
        self.write("a.py", "import b\ndef fn(x):\n if x: return x\n")
        self.write("b.py", "import a\n")
        diff = self.audit(complexity_threshold=2)["baseline_diff"]
        self.assertEqual(len(diff["new_circular_dependencies"]), 1)
        self.assertEqual(len(diff["new_complexity_hotspots"]), 1)
        self.assertTrue(diff["regression_detected"])

    def test_baseline_explicit_path_and_line_moves_do_not_regress(self):
        self.write("a.py", "def fn(): pass\n")
        selected = self.base / "explicit.json"
        self.audit(baseline_path=str(selected), save_baseline=True)
        self.assertFalse(self.baseline.exists())
        self.write("a.py", "# added comment\n\ndef fn(): pass\n")
        self.assertFalse(self.audit(baseline_path=str(selected))["baseline_diff"]["regression_detected"])

    def test_baseline_improvement_is_not_regression(self):
        self.write("a.py", "def fn(x):\n if x:\n  while x: x -= 1\n")
        self.audit(complexity_threshold=2, save_baseline=True)
        self.write("a.py", "__all__ = ['fn']\ndef fn(x):\n if x: return 1\n")
        self.assertFalse(self.audit(complexity_threshold=2)["baseline_diff"]["regression_detected"])

    def test_health_score_bounds_and_empty_project(self):
        self.assertEqual(self.audit()["health_score"], 100)
        self.assertEqual(self.ranks()["ranked_symbols"], [])
        self.write("dead.py", "\n".join(f"def unused_{i}(): pass" for i in range(60)))
        self.assertEqual(self.audit()["health_score"], 0)
        self.assertEqual(self.audit()["counts"]["total_symbols"], 60)

    def test_analysis_never_executes_target_project(self):
        marker = self.base / "executed"
        self.write("danger.py", f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\nraise RuntimeError('do not import')\ndef safe(): pass\n")
        for function, args in ((ci.code_search, ("safe",)), (ci.symbol_trace, ("safe",)),
                               (ci.impact_analysis, ()), (ci.architecture_extract, ()),
                               (ci.codegraph_explore, ("safe",)), (ci.code_repomap_rank, ()), (ci.code_health_audit, ())):
            function(str(self.root), *args)
        self.assertFalse(marker.exists())

    def test_symlink_files_and_directories_are_pruned(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.py").write_text("def escaped(): pass\n", encoding="utf-8")
        self.write("safe.py", "def safe(): pass\n")
        try:
            (self.root / "linked").symlink_to(outside, target_is_directory=True)
            (self.root / "linked.py").symlink_to(outside / "secret.py")
        except OSError:
            # Windows may deny creating symlinks. Exercise the symlink predicate
            # with real files/directories; a separate test uses a real junction.
            (self.root / "linked").mkdir(exist_ok=True)
            self.write("linked/secret.py", "def escaped(): pass\n")
            self.write("linked.py", "def escaped(): pass\n")
            original = Path.is_symlink
            targets = {self.root / "linked", self.root / "linked.py"}
            mocked = patch.object(Path, "is_symlink", lambda path: path in targets or original(path))
        else:
            from contextlib import nullcontext
            mocked = nullcontext()
        with mocked:
            graph = self.graph()
            self.assertEqual(set(graph.symbols), {"safe.safe"})
            with self.assertRaises(ValueError):
                ci.code_repomap_rank(str(self.root / "linked"))

    @unittest.skipUnless(os.name == "nt", "Windows reparse points")
    def test_windows_junction_directory_is_pruned(self):
        outside = self.base / "junction_target"
        outside.mkdir()
        (outside / "secret.py").write_text("def escaped(): pass\n", encoding="utf-8")
        link = self.root / "junction"
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
        self.addCleanup(lambda: os.rmdir(link) if link.exists() else None)
        self.assertTrue(link.lstat().st_file_attributes & 0x400)
        self.assertEqual(self.graph().symbols, {})
        with self.assertRaises(ValueError):
            ci.code_health_audit(str(link))

    def test_skipped_dirs_and_syntax_errors(self):
        self.write("node_modules/bad.py", "def hidden(): pass\n")
        self.write("broken.py", "def broken(\n")
        result = self.audit()
        self.assertEqual(result["counts"]["total_symbols"], 0)
        self.assertFalse(result["analysis"]["complete"])
        self.assertEqual(result["analysis"]["incomplete"][0]["reason"], "syntax_error")

    def test_seven_tools_schema_manifest_and_files_match(self):
        manifest = json.loads((MOD / "mod.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(manifest["version"], "2.1.0")
        self.assertEqual(manifest["mcp_tools"], server.TOOLS)
        self.assertEqual({tool["name"] for tool in server.TOOLS}, EXPECTED)
        self.assertIs(server.call_tool, server.dispatch)
        for tool in server.TOOLS:
            self.assertEqual(tool["inputSchema"], tool["parameters"])
            schema = json.loads((MOD / "mcp/schemas" / (tool["name"] + ".json")).read_text(encoding="utf-8"))
            self.assertEqual(schema, tool)

    def test_new_tools_dispatch(self):
        self.ranking_fixture()
        for name in ("code_repomap_rank", "code_health_audit"):
            self.assertTrue(server.call_tool(name, {"project_root": str(self.root)})["ok"])

    def rpc(self, requests):
        process = subprocess.run([sys.executable, "-X", "utf8", str(MOD / "mcp/mcp_server.py")],
                                 input="\n".join(json.dumps(item) for item in requests) + "\n",
                                 capture_output=True, text=True, encoding="utf-8", timeout=20,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        return [json.loads(line) for line in process.stdout.splitlines()]

    def test_stdio_initialize_and_tools_list(self):
        replies = self.rpc([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ])
        self.assertEqual([reply["id"] for reply in replies], [1, 2])
        self.assertEqual(replies[0]["result"]["serverInfo"]["version"], "2.1.0")
        self.assertEqual(len(replies[1]["result"]["tools"]), 7)

    def test_stdio_tools_call_all_seven(self):
        self.polyglot_fixture()
        requests = []
        for number, name in enumerate(sorted(EXPECTED), 1):
            arguments = {"project_root": str(self.root)}
            if name in {"code_search", "codegraph_explore"}:
                arguments["query"] = "py_entry"
            if name == "symbol_trace":
                arguments["symbol"] = "py_entry"
            requests.append({"jsonrpc": "2.0", "id": number, "method": "tools/call",
                             "params": {"name": name, "arguments": arguments}})
        replies = self.rpc(requests)
        self.assertEqual(len(replies), 7)
        for reply in replies:
            self.assertFalse(reply["result"]["isError"])
            self.assertIn("analysis", json.loads(reply["result"]["content"][0]["text"]))

    def test_polyglot_redos_resistance_on_com_interop_and_annotations(self):
        import time

        com_blocks = "\n".join(
            f'[ComImport, Guid("30cbe57d-d9d0-452a-ab13-7ac5ac4825ee"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]\n'
            f"public interface IUIAutomationProbe{i} {{\n"
            + ("    /* " + ("comment padding " * 12) + " */\n")
            + f"    int GetCurrentPropertyValue(int propId, [MarshalAs(UnmanagedType.Struct)] out object retVal);\n"
            + "}\n"
            for i in range(40)
        )
        cs_source = (
            com_blocks
            + '\n[ApiController]\n[Route("api/[controller]")]\n'
            + "public class ProbeController {\n"
            + "".join(
                f'    [MarshalAs(UnmanagedType.LPWStr)]\n    public static readonly string Field{i} = new string(\'a\', 4);\n'
                for i in range(40)
            )
            + '    [Authorize]\n    [HttpGet("ping")]\n'
            + "    public async Task<ActionResult<string>> Ping(CancellationToken ct = default(CancellationToken)) { return Ok(); }\n"
            + "}\n"
        )
        java_source = (
            "@RestController\n@RequestMapping(\"/v1\")\npublic class HeavyController {\n"
            + "".join(
                f'    @SuppressWarnings("unused")\n    private static final Map<String, List<String>> FIELD_{i} = null;\n'
                for i in range(40)
            )
            + '    @GetMapping("/ok")\n    public ResponseEntity<List<String>> ok() { return null; }\n}\n'
        )
        rust_source = (
            "pub trait Driver {\n"
            + "".join(f"    fn method_{i}(&self, x: i32) -> i32;\n" for i in range(40))
            + "}\n"
            + '#[get("/rs/ping")]\npub async fn rs_ping() -> i32 { 1 }\n'
        )
        bad_yaml = "openapi: 3.0.4\npaths:\n  " + ("'" * 30 + '"' * 30 + "unclosed_no_colon\n")
        self.write("probes/08-uia3-com.cs", cs_source)
        self.write("HeavyController.java", java_source)
        self.write("driver.rs", rust_source)
        self.write("broken_quotes.yaml", bad_yaml)

        t0 = time.perf_counter()
        arch = ci.architecture_extract(str(self.root), provider="polyglot")
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 1.0, f"Polyglot extraction regressed into slow backtracking: {elapsed:.3f}s")
        self.assertGreaterEqual(len(arch["nodes"]), 3)
        ep_search = ci.code_search(str(self.root), "/ping", kind="endpoint")
        routes = {item["contract_id"] for item in ep_search["results"]}
        self.assertIn("http:GET:/api/probe/ping", routes)
        self.assertIn("http:GET:/rs/ping", routes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
