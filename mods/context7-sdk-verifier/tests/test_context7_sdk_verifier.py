"""Isolated behavioral tests; never write to the user's shared project memory."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SERVER = Path(__file__).resolve().parents[1] / "mcp" / "mcp_server.py"
spec = importlib.util.spec_from_file_location("context7_sdk_verifier_under_test", SERVER)
sdk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sdk)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.memory = self.base / "memory"
        self.mirror = self.base / "mirror"
        self.mirror.mkdir()
        self.cache = self.base / "cache.json"
        self.project_path = self.memory / "L1_Projects/mods.json"
        for name, value in (("MEMORY_ROOT", self.memory), ("MIRROR_ROOT", self.mirror),
                            ("CACHE_PATH", self.cache), ("_RECONCILE_CACHE", {})):
            patcher = patch.object(sdk, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        write_json(self.memory / "00_Project_Registry.json", {"projects": [
            {"project_id": "mods", "folders": [{"path": str(self.root)}, {"path": "/workspace/mods"}]}]})
        self.save({"project_id": "mods", "facts": [
            {"fact_key": "sdk", "value": "FastAPI 1.2", "updated_at": 1},
            {"fact_key": "old", "value": "FastAPI obsolete", "invalid_at": "2025-01-01"}],
            "memories": [{"memory_id": "old-memory", "summary": "FastAPI requires named arguments",
                          "created_at": 1, "invalid_at": None},
                         {"memory_id": "expired", "summary": "FastAPI expired", "invalid_at": "yesterday"}],
            "experiences": [{"summary": "FastAPI static signatures"},
                            {"summary": "FastAPI expired experience", "invalid_at": "yesterday"}]})

    def save(self, value):
        write_json(self.project_path, value)

    def load(self):
        return json.loads(self.project_path.read_text(encoding="utf-8"))

    def report(self, **kwargs):
        return sdk.sdk_fact_reconcile_report(str(self.root), changed_files=kwargs.pop("changed_files", []), **kwargs)

    def commit(self, **kwargs):
        token = kwargs.pop("reconcile_hash", None) or self.report()["reconcile_hash"]
        return sdk.sdk_memory_review_commit(str(self.root), token, **kwargs)

    def test_lookup_routes_mods_and_excludes_expired_entries(self):
        for path in ("/workspace/mods", "/workspace/mods/context7-sdk-verifier", "/workspace/mods/../mods"):
            result = sdk.sdk_fact_lookup(path, packages=["FASTAPI"])
            self.assertTrue(result["ok"])
            self.assertEqual(result["project_id"], "mods")
            self.assertEqual([f["fact_key"] for f in result["matched_facts"]], ["sdk"])
            self.assertEqual(len(result["active_memories"]), 1)
            self.assertEqual(len(result["active_experiences"]), 1)
            self.assertIn("FastAPI", result["cheat_sheet_markdown"])

    def test_routing_respects_boundaries_and_most_specific_folder(self):
        registry = self.memory / "00_Project_Registry.json"
        data = json.loads(registry.read_text())
        data["projects"].append({"project_id": "nested", "folders": ["/workspace/mods/nested"]})
        write_json(registry, data)
        self.assertEqual(sdk.sdk_fact_lookup("/workspace/mods/nested/src")["project_id"], "nested")
        self.assertEqual(sdk.sdk_fact_lookup("/workspace/mods-other")["project_id"], "workbench_misc")

    def test_lookup_manifest_supplement_is_read_only(self):
        write_json(self.root / "package.json", {"name": "demo", "dependencies": {"fastapi": "1.2"}})
        before = self.project_path.read_bytes()
        result = sdk.sdk_fact_lookup(str(self.root), packages=["fastapi"])
        self.assertEqual(len(result["manifest_facts"]), 1)
        self.assertEqual(self.project_path.read_bytes(), before)
        self.assertFalse(self.cache.exists())

    def test_context7_offline_timeout_and_success(self):
        with patch.object(sdk, "urlopen", side_effect=TimeoutError) as request:
            result = sdk.sdk_fact_lookup(str(self.root), packages=["fastapi"], query_context7=True)
            self.assertTrue(result["ok"])
            self.assertEqual(result["context7_results"], [])
            self.assertEqual(request.call_args.kwargs["timeout"], 3)
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"results": [{"id": "/fastapi/fastapi"}]}'
        with patch.object(sdk, "urlopen", return_value=response):
            result = sdk.sdk_fact_lookup(str(self.root), packages=["fastapi"], query_context7=True)
            self.assertEqual(result["context7_results"][0]["id"], "/fastapi/fastapi")

    def test_static_signatures_and_ghost_keyword_audit(self):
        marker = self.root / "EXECUTED"
        (self.root / "api.py").write_text(
            "from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n"
            "def api(value, /, optional=3, *, required, enabled=True):\n    pass\n"
            "def permissive(*, real=1, **kwargs):\n    pass\n"
            "@decorator\nasync def worker(*, timeout=3):\n    pass\n"
            "class Client:\n    def __init__(self, *, token):\n        pass\n"
            "    @staticmethod\n    def build(*, name):\n        pass\n"
            "api(1, required=2, ghost=True)\npermissive(ghost=True)\nClient(token='x', phantom=1)\n",
            encoding="utf-8")
        report = self.report(changed_files=["api.py"], inspect_symbols=["Client.build"])
        self.assertFalse(marker.exists())
        facts = {f["qualname"]: f for f in report["internal_seam_facts"]}
        self.assertEqual(facts["api"]["required_keyword_only"], ["required"])
        self.assertEqual(facts["api"]["parameters"][0]["kind"], "positional_only")
        self.assertEqual(facts["api"]["parameters"][1]["default"], "3")
        self.assertEqual(facts["worker"]["decorators"], ["decorator"])
        self.assertEqual(facts["Client.build"]["decorators"], ["staticmethod"])
        mismatches = report["signature_mismatches"]
        self.assertEqual([m["unexpected_keywords"] for m in mismatches], [["ghost"], ["phantom"]])
        self.assertEqual(len(report["reconcile_hash"]), 24)
        self.assertEqual(report["reconcile_hash"], self.report(changed_files=["api.py"])["reconcile_hash"])
        self.assertIn(report["reconcile_hash"], json.loads(self.cache.read_text(encoding="utf-8")))

    def test_import_aliases_resolve_unchanged_local_module(self):
        (self.root / "api.py").write_text("def run(*, real):\n    pass\n", encoding="utf-8")
        (self.root / "caller.py").write_text(
            "from api import run as invoke\nimport api as sdk\ninvoke(ghost=1)\nsdk.run(real=1)\n",
            encoding="utf-8")
        report = self.report(changed_files=["caller.py"])
        self.assertEqual(len(report["signature_mismatches"]), 1)
        self.assertEqual(report["signature_mismatches"][0]["resolved_symbol"], "api.run")

    def test_same_named_functions_are_resolved_by_module(self):
        (self.root / "a.py").write_text("def run(*, left):\n    pass\n", encoding="utf-8")
        (self.root / "b.py").write_text("def run(*, right):\n    pass\n", encoding="utf-8")
        (self.root / "caller.py").write_text("from a import run as call\ncall(left=1)\n", encoding="utf-8")
        result = self.report(changed_files=["a.py", "b.py", "caller.py"])
        self.assertEqual(result["signature_mismatches"], [])

    def test_shadowed_import_is_not_falsely_audited(self):
        (self.root / "a.py").write_text("def run(*, left):\n    pass\n", encoding="utf-8")
        (self.root / "caller.py").write_text(
            "from a import run\ndef caller(run):\n    run(right=1)\n", encoding="utf-8")
        self.assertEqual(self.report(changed_files=["a.py", "caller.py"])["signature_mismatches"], [])

    def test_manifest_sha_and_toml_dependency_extraction(self):
        write_json(self.root / "mod.json", {"version": "1.0", "mcp_tools": [{"name": "tool_a"}]})
        write_json(self.root / "package.json", {"version": "2.0", "dependencies": {"react": "19"}})
        (self.root / "requirements.txt").write_text("# comment\nfastapi==1.2\n", encoding="utf-8")
        (self.root / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "3.0"\ndependencies = ["httpx>=0.28"]\n', encoding="utf-8")
        (self.root / "uv.lock").write_text('version = 1\n[[package]]\nname = "httpx"\nversion = "0.28.1"\n', encoding="utf-8")
        (self.root / "Cargo.toml").write_text('[package]\nname = "demo"\nversion = "1.0"\n[dependencies]\nserde = "1"\n', encoding="utf-8")
        report = self.report(changed_files=["mod.json"])
        facts = {f["filename"]: f for f in report["lockfile_facts"]}
        self.assertEqual(set(facts), sdk.MANIFESTS)
        self.assertEqual(facts["mod.json"]["mcp_tools"], ["tool_a"])
        self.assertEqual(facts["package.json"]["dependencies"], {"react": "19"})
        if sdk.tomllib is not None:
            self.assertEqual(facts["pyproject.toml"]["dependencies"], ["httpx>=0.28"])
            self.assertEqual(facts["uv.lock"]["package"][0]["version"], "0.28.1")
            self.assertEqual(facts["Cargo.toml"]["dependencies"], {"serde": "1"})
        for filename, fact in facts.items():
            self.assertEqual(fact["source_sha256"], hashlib.sha256((self.root / filename).read_bytes()).hexdigest())
        self.assertEqual(report["internal_seam_facts"][0]["mcp_tools"], ["tool_a"])

    def test_external_package_and_stdlib_are_never_imported(self):
        package = self.root / ".venv/Lib/site-packages/danger"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("raise RuntimeError('MUST NOT IMPORT')\n", encoding="utf-8")
        (package / "client.pyi").write_text("def send(*, token: str, retries: int = 3) -> None: ...\n", encoding="utf-8")
        (self.root / "caller.py").write_text("from danger.client import send\nsend(ghost=True)\n", encoding="utf-8")
        report = self.report(changed_files=["caller.py"], inspect_packages=["danger", "json"])
        self.assertNotIn("danger", sys.modules)
        send = next(f for f in report["package_ast_facts"] if f["name"] == "send")
        self.assertEqual(send["required_keyword_only"], ["token"])
        self.assertEqual(report["signature_mismatches"][0]["unexpected_keywords"], ["ghost"])
        self.assertTrue(any(f["name"] == "loads" for f in report["package_ast_facts"]))

    def test_node_type_declarations_are_static_facts(self):
        package = self.root / "node_modules/demo"
        package.mkdir(parents=True)
        (package / "index.d.ts").write_text("export declare function render(node: string): void;\nexport class Client {}\n", encoding="utf-8")
        report = self.report(inspect_packages=["demo"])
        self.assertEqual({f["name"] for f in report["package_ast_facts"]}, {"render", "Client"})

    def test_bad_python_and_missing_package_are_reported(self):
        (self.root / "broken.py").write_text("def broken(\n", encoding="utf-8")
        result = self.report(changed_files=["broken.py"], inspect_packages=["no_such_package_918287"])
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["analysis_errors"]), 2)

    def test_git_discovery_uses_no_window_and_handles_renames(self):
        (self.root / "new file.py").write_text("def public(): pass\n", encoding="utf-8")
        results = [subprocess.CompletedProcess([], 0, b'R  new file.py\0old file.py\0', b''),
                   subprocess.CompletedProcess([], 0, b'new file.py\0', b'')]
        with patch.object(sdk.subprocess, "run", side_effect=results) as run:
            report = sdk.sdk_fact_reconcile_report(str(self.root))
        self.assertEqual(len(report["internal_seam_facts"]), 1)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_invalid_and_cross_project_hashes_cannot_write(self):
        before = self.project_path.read_bytes()
        for token in ("", "forged"):
            result = sdk.sdk_memory_review_commit(str(self.root), token, verified_facts=[{"fact_key": "bad"}])
            self.assertFalse(result["ok"])
            self.assertTrue(result["error"].startswith("INVALID_RECONCILE_HASH"))
        token = self.report()["reconcile_hash"]
        other = self.base / "other"
        other.mkdir()
        result = sdk.sdk_memory_review_commit(str(other), token)
        self.assertFalse(result["ok"])
        self.assertEqual(self.project_path.read_bytes(), before)

    def test_tampered_cached_payload_is_rejected(self):
        token = self.report()["reconcile_hash"]
        cache = json.loads(self.cache.read_text(encoding="utf-8"))
        cache[token]["payload"]["lockfile_facts"] = [{"fake": True}]
        write_json(self.cache, cache)
        sdk._RECONCILE_CACHE.clear()
        self.assertFalse(sdk.sdk_memory_review_commit(str(self.root), token)["ok"])

    def test_fact_merge_real_sha_and_dual_directory_sync(self):
        source = self.root / "api.py"
        source.write_text("def api(): pass\n", encoding="utf-8")
        token = self.report(changed_files=["api.py"])["reconcile_hash"]
        sdk._RECONCILE_CACHE.clear()  # Simulate a second process using only persisted evidence.
        supplied = [{"fact_key": "api", "value": "signature", "source_path": "api.py", "source_sha256": "wrong"}]
        result = self.commit(reconcile_hash=token, verified_facts=supplied)
        self.assertTrue(result["ok"])
        self.assertTrue(self.commit(reconcile_hash=token, verified_facts=supplied)["ok"])
        facts = [f for f in self.load()["facts"] if f["fact_key"] == "api"]
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["source_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertTrue(facts[0]["sha256_corrected"])
        self.assertEqual(facts[0]["source_revision"], "sidecar:" + token)
        self.assertIn("evidence", facts[0])
        self.assertEqual(self.project_path.read_bytes(), (self.mirror / "L1_Projects/mods.json").read_bytes())
        self.assertEqual(supplied[0]["source_sha256"], "wrong")

    def test_missing_sha_is_filled_from_physical_source(self):
        (self.root / "source.py").write_text("# real\n", encoding="utf-8")
        self.assertTrue(self.commit(verified_facts=[{"fact_key": "real", "source_path": "source.py"}])["ok"])
        fact = next(f for f in self.load()["facts"] if f["fact_key"] == "real")
        self.assertEqual(fact["source_sha256"], sdk._sha(self.root / "source.py"))

    def test_supersession_and_normalized_memory_are_archived(self):
        result = self.commit(verified_memories=[{"memory_id": "replacement", "summary": "Use real signatures", "kind": "pitfall"}],
                             supersede_memory_ids=["old-memory"])
        self.assertTrue(result["ok"])
        memory = next(m for m in self.load()["memories"] if m["memory_id"] == "replacement")
        for key in ("project_id", "scope", "hard_boundary", "pyramid_level", "trust_tier", "kind", "summary",
                    "source_task_id", "created_at", "valid_from", "invalid_at", "superseded_by", "source_doc"):
            self.assertIn(key, memory)
        self.assertEqual(memory["pyramid_level"], "L1_ATOMIC_LESSON")
        self.assertEqual(memory["trust_tier"], "TIER_2_RUNTIME_VERIFIED")
        self.assertIsNone(memory["invalid_at"])
        archive = json.loads((self.memory / "L1_Projects/archive/mods_archive.json").read_text(encoding="utf-8"))
        old = next(m for m in archive["memories"] if m["memory_id"] == "old-memory")
        self.assertIsNotNone(old["invalid_at"])
        self.assertEqual(old["superseded_by"], "replacement")

    def test_memory_supersedes_field_and_idempotent_generated_id(self):
        memory = {"summary": "Replacement", "supersedes": "old-memory"}
        first = self.commit(verified_memories=[memory])
        self.assertTrue(first["ok"])
        second = self.commit(verified_memories=[memory])
        self.assertTrue(second["ok"])
        self.assertEqual(len(self.load()["memories"]), 1)

    def test_watermarks_keep_newest_entries_and_hard_boundaries(self):
        self.save({"project_id": "mods", "facts": [
            {"fact_key": str(i), "updated_at": i, "hard_boundary": i == 0} for i in range(85)],
            "memories": [{"memory_id": str(i), "summary": str(i), "created_at": i, "hard_boundary": i == 0}
                         for i in range(55)], "experiences": []})
        result = self.commit()
        self.assertTrue(result["ok"])
        data = self.load()
        self.assertEqual(len(data["facts"]), 80)
        self.assertEqual(len(data["memories"]), 50)
        self.assertIn("0", [f["fact_key"] for f in data["facts"]])
        self.assertNotIn("1", [m["memory_id"] for m in data["memories"]])
        self.assertEqual((result["archived_facts"], result["archived_memories"]), (5, 5))

    def test_excess_protected_memories_refuse_lossy_compaction(self):
        self.save({"facts": [], "memories": [{"memory_id": str(i), "hard_boundary": True} for i in range(51)]})
        before = self.project_path.read_bytes()
        result = self.commit()
        self.assertFalse(result["ok"])
        self.assertIn("WATERMARK_HARD_BOUNDARY", result["error"])
        self.assertEqual(self.project_path.read_bytes(), before)

    def test_promotion_requires_cross_project_evidence_and_is_idempotent(self):
        memory = {"memory_id": "boundary", "summary": "Never import target source", "hard_boundary": True}
        self.assertEqual(self.commit(verified_memories=[memory], promote_to_global=True,
                                     require_heterogeneous_review=False)["promoted"], 0)
        write_json(self.memory / "L1_Projects/second.json", {"project_id": "second", "memories": [memory]})
        self.assertEqual(self.commit(promote_to_global=True, require_heterogeneous_review=False)["promoted"], 1)
        self.assertEqual(self.commit(promote_to_global=True, require_heterogeneous_review=False)["promoted"], 1)
        global_path = self.memory / "L2_Experiences.json"
        data = json.loads(global_path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["experiences"]), 1)
        self.assertEqual(data["experiences"][0]["validated_projects"], ["mods", "second"])
        self.assertEqual(global_path.read_bytes(), (self.mirror / "L2_Experiences.json").read_bytes())

    def test_explicit_cross_project_promotion(self):
        result = self.commit(verified_memories=[{"summary": "Universal", "hard_boundary": True,
                                                "cross_project_verified": True}],
                             promote_to_global=True, reviewer_model="6.1terra")
        self.assertEqual(result["promoted"], 1)

    def assert_experience(self, experience, token=None):
        types = {"experience_id": str, "scope": str, "project_id": str, "title": str, "summary": str,
                 "conditions": list, "recommendations": list, "anti_patterns": list, "confidence": float,
                 "status": str, "support_count": int, "project_count": int, "validated_projects": list,
                 "source_refs": list, "hard_boundary": bool, "updated_at": str}
        for field, expected in types.items():
            self.assertIsInstance(experience[field], expected, field)
        for field in ("conditions", "recommendations", "anti_patterns", "validated_projects", "source_refs"):
            self.assertTrue(all(isinstance(value, str) and value.strip() for value in experience[field]), field)
        self.assertTrue(experience["experience_id"])
        self.assertTrue(experience["conditions"])
        self.assertTrue(experience["recommendations"])
        self.assertEqual(experience["status"], "active")
        self.assertIn(experience["scope"], {"global", "project"})
        self.assertGreaterEqual(experience["support_count"], 1)
        self.assertGreaterEqual(experience["project_count"], 1)
        if token:
            self.assertIn("sidecar:" + token, experience["source_refs"])

    def test_verified_experiences_merge_by_id_and_preserve_structured_fields(self):
        supplied = {"experience_id": "reviewed", "summary": "Use static signatures", "title": "Static review",
                    "conditions": ["Before editing SDK calls"], "recommendations": ["Read the source signature"],
                    "anti_patterns": ["Importing target modules"], "source_doc": "signature-audit",
                    "source_refs": ["audit.py"], "confidence": 0.99, "hard_boundary": True}
        original = json.loads(json.dumps(supplied))
        token = self.report()["reconcile_hash"]
        result = self.commit(reconcile_hash=token, verified_experiences=[supplied],
                             require_heterogeneous_review=False)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reviewer_model"], "mechanical-only")
        result = self.commit(reconcile_hash=token, verified_experiences=[
            {"experience_id": "reviewed", "summary": "Use verified static signatures"}],
            require_heterogeneous_review=False)
        self.assertTrue(result["ok"], result)
        items = self.load()["experiences"]
        self.assertEqual(len(items), 3)  # Two legacy entries plus the reviewed experience.
        for item in items:
            self.assert_experience(item, token)
        experience = next(item for item in items if item["experience_id"] == "reviewed")
        self.assertEqual(experience["summary"], "Use verified static signatures")
        for field in ("conditions", "recommendations", "anti_patterns", "confidence", "title"):
            self.assertEqual(experience[field], supplied[field])
        self.assertIn("signature-audit", experience["source_refs"])
        self.assertEqual(supplied, original)
        self.assertEqual(self.project_path.read_bytes(), (self.mirror / "L1_Projects/mods.json").read_bytes())

    def test_normalized_experience_defaults_and_stable_ids(self):
        now = "2026-11-01T00:00:00+00:00"
        raw = {"summary": "Never import target source", "kind": "pitfall", "conditions": ["", 1],
               "recommendations": [], "validated_projects": ["other", "other", ""]}
        item = sdk._normalize_experience(raw, "mods", "abc", now)
        self.assert_experience(item, "abc")
        self.assertTrue(item["experience_id"].startswith("exp_"))
        self.assertEqual(item["experience_id"], sdk._normalize_experience(raw, "mods", "def", "later")["experience_id"])
        self.assertEqual(item["confidence"], 0.95)
        self.assertEqual(item["updated_at"], now)
        self.assertEqual(item["validated_projects"], ["mods", "other"])
        self.assertEqual((item["support_count"], item["project_count"]), (2, 2))
        self.assertEqual(item["anti_patterns"], [raw["summary"]])
        self.assertEqual(sdk._normalize_experience({"memory_id": "legacy", "summary": "Legacy"},
                                                 "mods", "abc", now)["experience_id"], "legacy")

    def test_global_experience_promotes_without_hard_boundary_and_is_idempotent(self):
        experience = {"experience_id": "global-rule", "summary": "Inspect signatures", "scope": "global"}
        token = self.report()["reconcile_hash"]
        for _ in range(2):
            result = self.commit(reconcile_hash=token, verified_experiences=[experience],
                                 promote_to_global=True, reviewer_model="sonnet5.5")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["promoted"], 1)
        path = self.memory / "L2_Experiences.json"
        items = json.loads(path.read_text(encoding="utf-8"))["experiences"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["experience_id"], "global-rule")
        self.assert_experience(items[0], token)
        self.assertEqual(items[0]["scope"], "global")
        self.assertEqual(path.read_bytes(), (self.mirror / "L2_Experiences.json").read_bytes())

    def test_project_experience_requires_cross_project_validation(self):
        experience = {"experience_id": "project-rule", "summary": "Inspect source before calls"}
        self.assertEqual(self.commit(verified_experiences=[experience], promote_to_global=True,
                                     require_heterogeneous_review=False)["promoted"], 0)
        write_json(self.memory / "L1_Projects/second.json", {"project_id": "second", "experiences": [experience]})
        result = self.commit(promote_to_global=True, require_heterogeneous_review=False)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["promoted"], 1)
        item = json.loads((self.memory / "L2_Experiences.json").read_text(encoding="utf-8"))["experiences"][0]
        self.assert_experience(item)
        self.assertEqual(item["validated_projects"], ["mods", "second"])
        self.assertEqual((item["support_count"], item["project_count"]), (2, 2))

    def test_memory_promotion_produces_standard_experience(self):
        result = self.commit(verified_memories=[{"memory_id": "legacy-memory", "kind": "pitfall",
                                                "summary": "Never execute inspected sources", "hard_boundary": True,
                                                "cross_project_verified": True}],
                             promote_to_global=True, reviewer_model="6.1terra")
        self.assertTrue(result["ok"], result)
        items = json.loads((self.memory / "L2_Experiences.json").read_text(encoding="utf-8"))["experiences"]
        self.assertEqual(len(items), 1)
        self.assert_experience(items[0], result["reconcile_hash"])
        self.assertEqual(items[0]["experience_id"], "legacy-memory")
        self.assertTrue(items[0]["anti_patterns"])

    def test_global_legacy_entries_are_upgraded_and_deduplicated(self):
        path = self.memory / "L2_Experiences.json"
        legacy = {"memory_id": "legacy-global", "summary": "Read signatures", "project_id": "second"}
        for store in ([legacy, legacy, None, {}], {"version": 1, "experiences": [legacy, legacy, None, {}]}):
            with self.subTest(store=type(store).__name__):
                write_json(path, store)
                result = self.commit(promote_to_global=True, require_heterogeneous_review=False)
                self.assertTrue(result["ok"], result)
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(len(data["experiences"]), 1)
                self.assert_experience(data["experiences"][0])
                self.assertEqual(data["experiences"][0]["project_id"], "second")
                if isinstance(store, dict):
                    self.assertEqual(data["version"], 1)

    def test_absolute_external_source_sha_uses_physical_bytes(self):
        source = self.base / "external/config.toml"
        source.parent.mkdir()
        source.write_text('model = "reviewed"\n', encoding="utf-8")
        original_path = source.as_posix()
        supplied = {"fact_key": "external", "source_path": original_path, "source_sha256": "substitute"}
        for content in ('model = "reviewed"\n', 'model = "updated"\n'):
            source.write_text(content, encoding="utf-8")
            result = self.commit(verified_facts=[supplied])
            self.assertTrue(result["ok"], result)
            fact = next(f for f in self.load()["facts"] if f["fact_key"] == "external")
            self.assertEqual(fact["source_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(fact["source_path"], original_path)
            self.assertTrue(fact["sha256_corrected"])

    def test_memory_registry_source_sha_for_both_separators_and_relative_path(self):
        source = self.memory / "00_Project_Registry.json"
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        for source_path in ("05_Agent_Memory_Sync/00_Project_Registry.json",
                            "05_Agent_Memory_Sync\\00_Project_Registry.json", "00_Project_Registry.json"):
            with self.subTest(source_path=source_path):
                result = self.commit(verified_facts=[{"fact_key": "registry", "source_path": source_path}])
                self.assertTrue(result["ok"], result)
                fact = next(f for f in self.load()["facts"] if f["fact_key"] == "registry")
                self.assertEqual(fact["source_sha256"], digest)
                self.assertEqual(fact["source_path"], source_path)

    def test_project_source_takes_priority_over_memory_fallback(self):
        relative = Path("05_Agent_Memory_Sync/00_Project_Registry.json")
        write_json(self.root / relative, {"local": True})
        self.assertEqual(sdk._resolve_source_file(self.root, str(relative)), self.root / relative)
        self.assertIsNone(sdk._resolve_source_file(self.root, "nonexistent-source.json"))

    def test_project_name_and_registry_sync_for_explicit_name_and_mod_count(self):
        old = "mods（统一封装场景化 Mod 插件中心）"
        expected = old.replace("20 大", "21 大")
        for kwargs, name in (({"project_name": old}, expected),
                             ({"verified_facts": [{"fact_key": "mod_total_count", "value": "21"}]}, expected),
                             ({"project_name": "Reviewed Mod center"}, "Reviewed Mod center")):
            with self.subTest(kwargs=kwargs):
                data = self.load()
                data["project_name"] = old
                self.save(data)
                registry_path = self.memory / "00_Project_Registry.json"
                registry = json.loads(registry_path.read_text(encoding="utf-8"))
                registry["projects"][0]["name"] = old
                write_json(registry_path, registry)
                result = self.commit(**kwargs)
                self.assertTrue(result["ok"], result)
                self.assertEqual(self.load()["project_name"], name)
                updated = json.loads(registry_path.read_text(encoding="utf-8"))
                self.assertEqual(updated["projects"][0]["name"], name)
                self.assertEqual(updated["projects"][0]["folders"], registry["projects"][0]["folders"])
                self.assertEqual(registry_path.read_bytes(), (self.mirror / registry_path.name).read_bytes())
                self.assertEqual(self.project_path.read_bytes(), (self.mirror / "L1_Projects/mods.json").read_bytes())

    def test_commit_schema_advertises_new_arguments_in_manifest_and_stdio(self):
        manifest = json.loads((SERVER.parents[1] / "mod.json").read_text(encoding="utf-8"))
        for tools in (manifest["mcp_tools"], sdk.TOOLS):
            tool = next(t for t in tools if t["name"] == "sdk_memory_review_commit")
            schema = tool.get("inputSchema", tool.get("parameters"))
            self.assertEqual(schema["properties"]["verified_experiences"]["type"], "array")
            self.assertEqual(schema["properties"]["project_name"]["type"], "string")
            self.assertEqual(schema["properties"]["promote_to_global"]["type"], "boolean")
            self.assertNotIn("verified_experiences", schema["required"])
            self.assertNotIn("project_name", schema["required"])

    def test_registry_fact_sha_matches_registry_after_name_update(self):
        result = self.commit(project_name="Updated project", verified_facts=[
            {"fact_key": "registry", "source_path": "05_Agent_Memory_Sync/00_Project_Registry.json"}])
        self.assertTrue(result["ok"], result)
        fact = next(f for f in self.load()["facts"] if f["fact_key"] == "registry")
        self.assertEqual(fact["source_sha256"], hashlib.sha256(
            (self.memory / "00_Project_Registry.json").read_bytes()).hexdigest())
        self.assertEqual(self.project_path.read_bytes(), (self.mirror / "L1_Projects/mods.json").read_bytes())

    def test_atomic_replace_retries_and_cleans_temporary_files(self):
        path = self.base / "atomic.json"
        replace = os.replace
        calls = []

        def flaky(source, destination):
            calls.append(destination)
            if len(calls) < 3:
                raise PermissionError("sync lock")
            replace(source, destination)

        with patch.object(sdk.os, "replace", side_effect=flaky), patch.object(sdk.time, "sleep") as sleep:
            sdk._atomic_write(path, {"value": "完整中文"})
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["value"], "完整中文")
        self.assertEqual(list(self.base.glob("*.tmp")), [])

    def test_mirror_failure_is_reported_with_completed_paths(self):
        original = sdk._atomic_write
        def write(path, value):
            if str(path).startswith(str(self.mirror)):
                raise PermissionError("mirror unavailable")
            return original(path, value)
        token = self.report()["reconcile_hash"]
        with patch.object(sdk, "_atomic_write", side_effect=write):
            result = self.commit(reconcile_hash=token)
        self.assertTrue(result["ok"])
        self.assertFalse(result["mirror_synced"])
        self.assertIn("mirror unavailable", result["mirror_warning"])
        self.assertEqual(len(result["written_paths"]), 2)

        def write_primary_fail(path, value):
            if str(path).startswith(str(self.memory)):
                raise PermissionError("primary unavailable")
            return original(path, value)
        with patch.object(sdk, "_atomic_write", side_effect=write_primary_fail):
            primary_res = self.commit(reconcile_hash=token)
        self.assertFalse(primary_res["ok"])
        self.assertFalse(primary_res["mirror_synced"])

    def test_legacy_lookup_and_reconcile_parameter_aliases(self):
        result = sdk.dispatch("sdk_fact_lookup", {"project_root": str(self.root), "library_or_module": "fastapi", "allow_network": False})
        self.assertEqual(len(result["matched_facts"]), 1)
        result = sdk.dispatch("sdk_fact_reconcile_report", {"project_root": str(self.root), "target_files": [], "phase": "post_diff"})
        self.assertTrue(result["ok"])

    def test_v110_modality_inference_and_explicit_order(self):
        cases = [
            ({"task_intent": "产品介绍 PPT 和 UI 设计"}, ["ppt_product_pitch", "ui_aesthetic_design"]),
            ({"task_intent": "Product PITCH SLIDE DECK and UI DESIGN"}, ["ppt_product_pitch", "ui_aesthetic_design"]),
            ({"task_intent": "系统部署与自动化流水线"}, ["systems_and_automation"]),
            ({"task_intent": "正式工程报告 Word Excel PDF"}, ["office_report_doc"]),
            ({"task_intent": "Python SDK API 测试"}, ["coding_and_testing"]),
            ({"task_intent": "无关键词"}, ["coding_and_testing"]),
            ({}, ["coding_and_testing"]),
            ({"modalities": []}, ["coding_and_testing"]),
            ({"modalities": " UI_AESTHETIC_DESIGN "}, ["ui_aesthetic_design"]),
            ({"modalities": ["ui_aesthetic_design", " UI_AESTHETIC_DESIGN ", "ppt_product_pitch"],
              "task_intent": "Word PPT"}, ["ui_aesthetic_design", "ppt_product_pitch", "office_report_doc"]),
        ]
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(sdk._infer_modalities(**arguments), expected)
                result = sdk.sdk_fact_lookup(str(self.root), **arguments)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["matched_modalities"], expected)

    def test_v110_lookup_assembles_three_sections_and_excludes_expired(self):
        mid = "ui_aesthetic_design"
        path = self.memory / "L1_Modalities" / (mid + ".json")
        write_json(path, {"modality_id": mid,
                         "craft_rules": [{"rule_id": "active", "summary": "Use visual hierarchy"},
                                         {"rule_id": "old", "invalid_at": "yesterday"}],
                         "design_presets": [{"preset_key": "color", "value": "OKLCH"},
                                            {"preset_key": "old", "invalid_at": "yesterday"}],
                         "memories": [{"memory_id": "layout", "summary": "Verified layout"},
                                      {"memory_id": "old", "invalid_at": "yesterday"}],
                         "experiences": [{"experience_id": "contrast", "summary": "Verify contrast"},
                                         {"experience_id": "old", "invalid_at": "yesterday"}]})
        global_path = self.memory / "L2_Experiences.json"
        write_json(global_path, {"experiences": [{"summary": "Use hidden subprocess windows"},
                                                  {"summary": "expired", "invalid_at": "yesterday"}]})
        core_path = self.memory / "L3_Core_Rules.md"
        core_path.write_text("# Core rules\n- 简体中文\ntext\n - not a core bullet\n- 精炼表达\n", encoding="utf-8")
        write_json(self.root / "package.json", {"dependencies": {"fastapi": "1.2"}})
        before = {p: p.read_bytes() for p in self.memory.rglob("*") if p.is_file()}
        result = sdk.dispatch("sdk_fact_lookup", {"project_root": str(self.root), "modalities": [mid],
                                                 "packages": ["fastapi"]})
        self.assertEqual([item["rule_id"] for item in result["modality_rules"]], ["active"])
        self.assertEqual([item["preset_key"] for item in result["modality_presets"]], ["color"])
        self.assertEqual(len(result["modality_memories"]), 2)
        self.assertTrue(all(item["modality_id"] == mid for key in (
            "modality_rules", "modality_presets", "modality_memories") for item in result[key]))
        self.assertEqual(len(result["global_experiences"]), 1)
        self.assertEqual(result["core_rules_excerpt"], "- 简体中文\n- 精炼表达")
        card = result["cheat_sheet_markdown"]
        for text in ("三维交叉作战简报", "Cross-Dimension Briefing Card", "SDK Pre-Flight: mods",
                     "项目客观真值", "交付工艺与审美准则", "全局安全与表达底线",
                     "L1_Projects/mods", "L1_Modalities/ui_aesthetic_design", "L2_Experiences", "L3_Core_Rules",
                     "FastAPI 1.2", "FastAPI requires named arguments", "FastAPI static signatures",
                     "package.json", "Use visual hierarchy", "OKLCH", "Verified layout", "Verify contrast",
                     "Use hidden subprocess windows", "简体中文", "精炼表达"):
            self.assertIn(text, card)
        self.assertEqual(sum(line.startswith("## ") for line in card.splitlines()), 3)
        self.assertNotIn("expired", card)
        self.assertEqual(before, {p: p.read_bytes() for p in self.memory.rglob("*") if p.is_file()})
        self.assertFalse(self.cache.exists())

    def test_v110_lookup_supports_global_list_and_missing_stores(self):
        empty = sdk.sdk_fact_lookup(str(self.root), modalities=["missing.craft-v2"])
        self.assertEqual(empty["matched_modalities"], ["missing.craft-v2"])
        for key in ("modality_rules", "modality_presets", "modality_memories", "global_experiences"):
            self.assertEqual(empty[key], [])
        self.assertEqual(empty["core_rules_excerpt"], "")
        write_json(self.memory / "L2_Experiences.json", [None, {}, {"summary": "Active"},
                                                        {"summary": "Old", "invalid_at": "yesterday"}])
        self.assertEqual(sdk.sdk_fact_lookup(str(self.root))["global_experiences"], [{}, {"summary": "Active"}])

    def test_v110_bidirectional_split_commit_supersession_compaction_and_sync(self):
        mid = "ui_aesthetic_design"
        relative = Path("L1_Modalities") / (mid + ".json")
        write_json(self.memory / relative, {
            "modality_id": mid, "modality_name": "UI 工艺", "associated_mods": ["ui-aesthetic-studio"],
            "craft_rules": [{"rule_id": "r" + str(i), "summary": "Rule " + str(i),
                             "created_at": i, "hard_boundary": i == 0} for i in range(65)],
            "design_presets": [{"preset_key": "style", "value": "old", "source_mod": "old"}],
            "memories": [{"memory_id": "m" + str(i), "summary": "Memory " + str(i),
                          "created_at": i, "hard_boundary": i == 0} for i in range(55)],
            "experiences": [{"summary": "Retain existing experience"}]})
        old_name = "mods（统一封装场景化 Mod 插件中心）"
        data = self.load()
        data["project_name"] = old_name
        self.save(data)
        registry_path = self.memory / "00_Project_Registry.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["projects"][0]["name"] = old_name
        write_json(registry_path, registry)
        token = self.report()["reconcile_hash"]
        supplied = {"rule_id": "replacement", "summary": "New rule", "supersedes": ["r64"]}
        result = self.commit(
            reconcile_hash=token, verified_facts=[{"fact_key": "mod_total_count", "value": "24"}],
            verified_memories=[{"memory_id": "project-new", "summary": "New project fact"}],
            verified_experiences=[{"experience_id": "global", "summary": "Verified across crafts", "scope": "global"}],
            promote_to_global=True, reviewer_model="6.1terra", modality_id=mid, verified_modality_rules=[supplied],
            verified_modality_memories=[{"memory_id": "modality-new", "summary": "New craft preference"}],
            verified_design_presets=[{"preset_key": "style", "value": "OKLCH", "source_mod": "ui-aesthetic-studio"}])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["updated_modalities"], [mid])
        self.assertEqual((result["active_modality_rules"], result["active_modality_memories"]), (60, 50))
        project = self.load()
        expected_name = old_name.replace("21 大", "24 大")
        self.assertEqual(project["project_name"], expected_name)
        self.assertIn("project-new", [m["memory_id"] for m in project["memories"]])
        updated_registry = json.loads(registry_path.read_text(encoding="utf-8"))
        self.assertEqual(updated_registry["projects"][0]["name"], expected_name)
        self.assertEqual(updated_registry["projects"][0]["folders"], registry["projects"][0]["folders"])
        modality = json.loads((self.memory / relative).read_text(encoding="utf-8"))
        self.assertEqual(modality["modality_name"], "UI 工艺")
        self.assertEqual(len(modality["craft_rules"]), 60)
        self.assertEqual(len(modality["memories"]), 50)
        self.assertIn("r0", [r["rule_id"] for r in modality["craft_rules"]])
        self.assertIn("m0", [m["memory_id"] for m in modality["memories"]])
        rule = next(r for r in modality["craft_rules"] if r["rule_id"] == "replacement")
        for key, expected_type in (("rule_id", str), ("category", str), ("title", str), ("summary", str),
                                   ("anti_patterns", list), ("hard_boundary", bool), ("created_at", str), ("updated_at", str)):
            self.assertIsInstance(rule[key], expected_type)
        self.assertIsNone(rule["invalid_at"])
        self.assertIsNone(rule["superseded_by"])
        memory = next(m for m in modality["memories"] if m["memory_id"] == "modality-new")
        self.assertEqual(memory["modality_id"], mid)
        self.assertEqual(memory["source_task_id"], "sidecar_" + token[:8])
        self.assertTrue(memory["created_at"])
        self.assertTrue(memory["valid_from"])
        self.assertIsNone(memory["invalid_at"])
        self.assertIsNone(memory["superseded_by"])
        self.assertEqual(len(modality["design_presets"]), 1)
        self.assertEqual(modality["design_presets"][0]["value"], "OKLCH")
        self.assertTrue(modality["design_presets"][0]["updated_at"])
        self.assertEqual(modality["experiences"], [{"summary": "Retain existing experience"}])
        archive_relative = Path("L1_Modalities/archive") / (mid + "_archive.json")
        archive = json.loads((self.memory / archive_relative).read_text(encoding="utf-8"))
        self.assertEqual(len(archive["craft_rules"]), 6)
        self.assertEqual(len(archive["memories"]), 6)
        old_rule = next(r for r in archive["craft_rules"] if r["rule_id"] == "r64")
        self.assertEqual(old_rule["superseded_by"], "replacement")
        self.assertTrue(old_rule["invalid_at"])
        self.assertFalse(any(r.get("hard_boundary") for r in archive["craft_rules"]))
        self.assertFalse(any(m.get("hard_boundary") for m in archive["memories"]))
        global_items = json.loads((self.memory / "L2_Experiences.json").read_text(encoding="utf-8"))["experiences"]
        self.assertEqual([e["experience_id"] for e in global_items], ["global"])
        for path in self.memory.rglob("*.json"):
            self.assertEqual(path.read_bytes(), (self.mirror / path.relative_to(self.memory)).read_bytes())
        self.assertEqual(supplied, {"rule_id": "replacement", "summary": "New rule", "supersedes": ["r64"]})

    def test_v110_modality_merges_are_idempotent_and_preserve_temporal_fields(self):
        mid = "ui_aesthetic_design"
        token = self.report()["reconcile_hash"]
        arguments = {"modality_id": mid, "verified_modality_rules": [{"summary": "Stable rule"}],
                     "verified_modality_memories": [{"summary": "Stable memory"}],
                     "verified_design_presets": [{"preset_key": "style", "value": {"color": "blue"}, "source_mod": "ui"}]}
        original = json.loads(json.dumps(arguments))
        first = self.commit(reconcile_hash=token, **arguments)
        self.assertTrue(first["ok"], first)
        path = self.memory / "L1_Modalities" / (mid + ".json")
        before = json.loads(path.read_text(encoding="utf-8"))
        second = self.commit(reconcile_hash=token, **arguments)
        self.assertTrue(second["ok"], second)
        after = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(after["craft_rules"]), 1)
        self.assertEqual(len(after["memories"]), 1)
        self.assertEqual(len(after["design_presets"]), 1)
        self.assertTrue(after["craft_rules"][0]["rule_id"].startswith("rule_"))
        self.assertEqual(len(after["craft_rules"][0]["rule_id"]), 17)
        for key, id_key in (("craft_rules", "rule_id"), ("memories", "memory_id")):
            self.assertEqual(before[key][0][id_key], after[key][0][id_key])
            self.assertEqual(before[key][0]["created_at"], after[key][0]["created_at"])
        self.assertEqual(before["memories"][0]["valid_from"], after["memories"][0]["valid_from"])
        self.assertEqual(after["modality_name"], mid)
        self.assertEqual(after["associated_mods"], [])
        self.assertEqual(after["experiences"], [])
        self.assertEqual(arguments, original)
        self.assertEqual(path.read_bytes(), (self.mirror / path.relative_to(self.memory)).read_bytes())

    def test_v110_multi_modality_and_per_item_routing(self):
        result = self.commit(modality_id="ppt_product_pitch", modalities=["ui_aesthetic_design", "ppt_product_pitch"],
                             verified_modality_rules=[{"modality_id": "ppt_product_pitch", "summary": "Pitch rule"},
                                                      {"modality_id": "ui_aesthetic_design", "summary": "UI rule"}],
                             verified_design_presets=[{"modality_id": "ui_aesthetic_design", "preset_key": "color", "value": "blue"}],
                             verified_modality_memories=[{"modality_id": "office_report_doc", "summary": "Report memory"}])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["updated_modalities"], ["ppt_product_pitch", "ui_aesthetic_design", "office_report_doc"])
        self.assertEqual((result["active_modality_rules"], result["active_modality_memories"]), (2, 1))
        for mid, rule_count, memory_count in (("ppt_product_pitch", 1, 0), ("ui_aesthetic_design", 1, 0), ("office_report_doc", 0, 1)):
            path = self.memory / "L1_Modalities" / (mid + ".json")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(data["craft_rules"]), rule_count)
            self.assertEqual(len(data["memories"]), memory_count)
            self.assertEqual(path.read_bytes(), (self.mirror / path.relative_to(self.memory)).read_bytes())
        inferred_target = self.commit(verified_modality_rules=[{"modality_id": "custom.craft-v2", "summary": "Custom craft"}])
        self.assertTrue(inferred_target["ok"], inferred_target)
        self.assertEqual(inferred_target["updated_modalities"], ["custom.craft-v2"])

    def test_v110_invalid_modality_ids_cannot_read_or_write_paths(self):
        token = self.report()["reconcile_hash"]
        for mid in ("../escape", "..\\escape", "a/b", "a\\b", "", " ", ".", "..", "a:b", 123):
            with self.subTest(mid=mid):
                lookup = sdk.sdk_fact_lookup(str(self.root), modalities=[mid])
                self.assertFalse(lookup["ok"], lookup)
                before = {p: p.read_bytes() for p in self.memory.rglob("*") if p.is_file()}
                for arguments in ({"modality_id": mid}, {"modalities": [mid]},
                                  {"verified_modality_rules": [{"modality_id": mid}]},
                                  {"verified_design_presets": [{"modality_id": mid, "preset_key": "p"}]},
                                  {"verified_modality_memories": [{"modality_id": mid}]}):
                    result = self.commit(reconcile_hash=token, **arguments)
                    self.assertFalse(result["ok"], result)
                    self.assertIn("Invalid modality_id", result["error"])
                    self.assertEqual(before, {p: p.read_bytes() for p in self.memory.rglob("*") if p.is_file()})

    def test_v110_ambiguous_or_missing_targets_reject_all_writes(self):
        token = self.report()["reconcile_hash"]
        before = self.project_path.read_bytes()
        for field in ("verified_modality_rules", "verified_modality_memories", "verified_design_presets"):
            for targets in ({}, {"modalities": ["ppt_product_pitch", "ui_aesthetic_design"]}):
                with self.subTest(field=field, targets=targets):
                    result = self.commit(reconcile_hash=token, verified_facts=[{"fact_key": "must-not-write"}],
                                         **targets, **{field: [{"summary": "No target", "preset_key": "style"}]})
                    self.assertFalse(result["ok"], result)
                    self.assertIn("modality_id", result["error"])
                    self.assertEqual(self.project_path.read_bytes(), before)
                    self.assertFalse((self.memory / "L1_Modalities").exists())
                    self.assertEqual(list(self.mirror.iterdir()), [])

    def test_v110_modality_invalid_items_and_self_supersession_reject_writes(self):
        token = self.report()["reconcile_hash"]
        before = self.project_path.read_bytes()
        cases = [
            {"verified_modality_rules": [{"rule_id": "self", "supersedes": "self"}]},
            {"verified_modality_memories": [{"memory_id": "self", "supersede_ids": ["self"]}]},
            {"verified_design_presets": [{"value": "missing key"}]},
            {"verified_modality_rules": [None]},
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.commit(reconcile_hash=token, modality_id="ui_aesthetic_design", **arguments)
                self.assertFalse(result["ok"], result)
                self.assertEqual(self.project_path.read_bytes(), before)
                self.assertFalse((self.memory / "L1_Modalities").exists())

    def test_v110_modality_memory_and_rule_supersede_aliases(self):
        mid = "coding_and_testing"
        initial = self.commit(modality_id=mid, verified_modality_rules=[{"rule_id": "old-rule"}],
                              verified_modality_memories=[{"memory_id": "old-memory"}])
        self.assertTrue(initial["ok"], initial)
        result = self.commit(modality_id=mid,
                             verified_modality_rules=[{"rule_id": "new-rule", "supersede_ids": ["old-rule"]}],
                             verified_modality_memories=[{"memory_id": "new-memory", "supersedes": "old-memory"}])
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["active_modality_rules"], result["active_modality_memories"]), (1, 1))
        archive_path = self.memory / "L1_Modalities/archive" / (mid + "_archive.json")
        archive = json.loads(archive_path.read_text(encoding="utf-8"))
        self.assertEqual(archive["craft_rules"][0]["superseded_by"], "new-rule")
        self.assertEqual(archive["memories"][0]["superseded_by"], "new-memory")
        self.assertTrue(archive["craft_rules"][0]["invalid_at"])
        self.assertTrue(archive["memories"][0]["invalid_at"])
        lookup = sdk.sdk_fact_lookup(str(self.root), modalities=[mid])
        self.assertEqual([r["rule_id"] for r in lookup["modality_rules"]], ["new-rule"])
        self.assertEqual([m["memory_id"] for m in lookup["modality_memories"]], ["new-memory"])

    def test_v110_protected_modality_overflow_rejects_entire_commit(self):
        token = self.report()["reconcile_hash"]
        for key, id_key, limit in (("craft_rules", "rule_id", 60), ("memories", "memory_id", 50)):
            with self.subTest(key=key):
                path = self.memory / "L1_Modalities/ui_aesthetic_design.json"
                write_json(path, {"modality_id": "ui_aesthetic_design", key: [
                    {id_key: str(i), "hard_boundary": True} for i in range(limit + 1)]})
                before = {p: p.read_bytes() for p in self.memory.rglob("*") if p.is_file()}
                result = self.commit(reconcile_hash=token, modalities=["ppt_product_pitch", "ui_aesthetic_design"],
                                     verified_facts=[{"fact_key": "must-not-write"}],
                                     verified_experiences=[{"summary": "No promotion", "scope": "global"}],
                                     promote_to_global=True, reviewer_model="6.1terra")
                self.assertFalse(result["ok"], result)
                self.assertIn("WATERMARK_HARD_BOUNDARY", result["error"])
                self.assertEqual(before, {p: p.read_bytes() for p in self.memory.rglob("*") if p.is_file()})
                self.assertEqual(list(self.mirror.iterdir()), [])

    def test_v110_modality_commit_creates_missing_mirror(self):
        missing_mirror = self.base / "new-mirror/nested"
        with patch.object(sdk, "MIRROR_ROOT", missing_mirror):
            result = self.commit(modalities=["ui_aesthetic_design"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["updated_modalities"], ["ui_aesthetic_design"])
        self.assertEqual((result["active_modality_rules"], result["active_modality_memories"]), (0, 0))
        for path in self.memory.rglob("*.json"):
            # The registry was not changed by this commit.
            if path.name != "00_Project_Registry.json":
                self.assertEqual(path.read_bytes(), (missing_mirror / path.relative_to(self.memory)).read_bytes())

    def test_v110_forged_hash_cannot_create_modality_store(self):
        result = sdk.sdk_memory_review_commit(str(self.root), "forged", modality_id="ui_aesthetic_design",
                                              verified_modality_rules=[{"summary": "Unverified"}])
        self.assertFalse(result["ok"], result)
        self.assertFalse((self.memory / "L1_Modalities").exists())
        self.assertEqual(list(self.mirror.iterdir()), [])

    def test_v110_modality_mirror_failure_reports_completed_paths(self):
        original = sdk._atomic_write
        failed_path = self.mirror / "L1_Modalities/ui_aesthetic_design.json"
        def write(path, value):
            if path == failed_path:
                raise PermissionError("modality mirror unavailable")
            original(path, value)
        with patch.object(sdk, "_atomic_write", side_effect=write):
            result = self.commit(modality_id="ui_aesthetic_design", verified_modality_rules=[{"summary": "Rule"}])
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["mirror_synced"], result)
        self.assertIn("modality mirror unavailable", result["mirror_warning"])
        self.assertNotIn(str(failed_path), result["written_paths"])
        self.assertIn(str(self.memory / "L1_Modalities/ui_aesthetic_design.json"), result["written_paths"])
        self.assertIn(str(self.mirror / "L1_Modalities/archive/ui_aesthetic_design_archive.json"), result["written_paths"])

    def test_v110_dynamic_mod_counts_update_project_and_registry(self):
        for count in ("22", "23", "24", 105):
            with self.subTest(count=count):
                name = "mods（统一封装场景化 Mod 插件中心）"
                data = self.load()
                data.update(project_name=name, name=name)
                self.save(data)
                registry_path = self.memory / "00_Project_Registry.json"
                registry = json.loads(registry_path.read_text(encoding="utf-8"))
                registry["projects"][0]["name"] = name
                write_json(registry_path, registry)
                result = self.commit(verified_facts=[{"fact_key": "mod_total_count", "value": count}])
                self.assertTrue(result["ok"], result)
                expected = name.replace("21 大", str(count) + " 大")
                self.assertEqual(self.load()["project_name"], expected)
                self.assertEqual(self.load()["name"], expected)
                self.assertEqual(json.loads(registry_path.read_text(encoding="utf-8"))["projects"][0]["name"], expected)
                self.assertEqual(registry_path.read_bytes(), (self.mirror / registry_path.name).read_bytes())

    def test_v110_stdio_and_manifest_schemas_advertise_cross_memory(self):
        manifest = json.loads((SERVER.parents[1] / "mod.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "1.1.0")
        initialized = sdk._handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(initialized["result"]["serverInfo"]["version"], "1.1.0")
        for tools in (manifest["mcp_tools"], sdk.TOOLS):
            lookup = next(t for t in tools if t["name"] == "sdk_fact_lookup")
            commit = next(t for t in tools if t["name"] == "sdk_memory_review_commit")
            lookup_schema = lookup.get("inputSchema", lookup.get("parameters"))
            commit_schema = commit.get("inputSchema", commit.get("parameters"))
            self.assertEqual(lookup_schema["properties"]["task_intent"]["type"], "string")
            self.assertEqual({v["type"] for v in lookup_schema["properties"]["modalities"]["anyOf"]}, {"string", "array"})
            self.assertEqual(lookup_schema["required"], [])
            for field, expected_type in (("modality_id", "string"), ("modalities", "array"),
                                         ("verified_modality_rules", "array"), ("verified_modality_memories", "array"),
                                         ("verified_design_presets", "array")):
                self.assertEqual(commit_schema["properties"][field]["type"], expected_type)
                self.assertNotIn(field, commit_schema["required"])
        token = self.report()["reconcile_hash"]
        committed = sdk.dispatch("sdk_memory_review_commit", {"project_root": str(self.root), "reconcile_hash": token,
                                                              "modality_id": "ui_aesthetic_design",
                                                              "verified_modality_rules": [{"summary": "Dispatched rule"}]})
        self.assertTrue(committed["ok"], committed)
        lookup = sdk.dispatch("sdk_fact_lookup", {"project_root": str(self.root), "task_intent": "UI design"})
        self.assertEqual(lookup["modality_rules"][0]["summary"], "Dispatched rule")

    def test_weekly_quota_reviewer_selection_and_blocked_models(self):
        qfile = self.base / "quota_cache.json"
        with patch.object(sdk, "QUOTA_CACHE_PATH", qfile):
            write_json(qfile, {"codex": {"weekly": 78.0}, "claude": {"weekly": 48.9}})
            r1 = sdk._select_reviewer_by_weekly_quota()
            self.assertEqual(r1["selected_model"], "gpt-6.1-terra")
            self.assertEqual(r1["selected_alias"], "6.1terra")

            write_json(qfile, {"codex": {"weekly": 42.0}, "claude": {"weekly": 81.5}})
            r2 = sdk._select_reviewer_by_weekly_quota()
            self.assertEqual(r2["selected_model"], "claude-sonnet-5-5")
            self.assertEqual(r2["selected_alias"], "sonnet5.5")

        token = self.report()["reconcile_hash"]
        for blocked in ("6.1sol", "gpt-6.1-sol", "opus5.5", "claude-opus-5-5", "claude-opus-5-5-high"):
            res = self.commit(reconcile_hash=token, reviewer_model=blocked,
                              verified_facts=[{"fact_key": "must_block"}])
            self.assertFalse(res["ok"])
            self.assertIn("BLOCKED_REVIEWER_MODEL", res["error"])

        for allowed, canonical in (("6.1terra", "gpt-6.1-terra"), ("sonnet5.5", "claude-sonnet-5-5")):
            res = self.commit(reconcile_hash=token, reviewer_model=allowed,
                              verified_facts=[{"fact_key": "allowed_" + allowed, "value": "ok"}])
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["reviewer_model"], canonical)

    def test_mechanical_only_and_heterogeneous_review_and_ttl(self):
        token = self.report()["reconcile_hash"]
        mech_fail = self.commit(reconcile_hash=token, mechanical_only=True,
                                verified_memories=[{"summary": "Subjective opinion"}])
        self.assertFalse(mech_fail["ok"])
        self.assertIn("MECHANICAL_AUDIT_ONLY", mech_fail["error"])

        mech_ok = self.commit(reconcile_hash=token, mechanical_only=True,
                              verified_facts=[{"fact_key": "det_fact", "value": "123"}],
                              ttl_days=14)
        self.assertTrue(mech_ok["ok"], mech_ok)
        fact = next(f for f in self.load()["facts"] if f["fact_key"] == "det_fact")
        self.assertEqual(fact["verification_mode"], "deterministic_mechanical")
        self.assertTrue(fact.get("reverify_after"))

        for sidecar_model in ("gemini-4-argon", "gemini-4-hydrogen", "gemini-4-hydrogen-medium",
                              "gemini-4-hydrogen-high", "hydrogen"):
            sidecar_mech = self.commit(reconcile_hash=token, mechanical_only=True,
                                       reviewer_model=sidecar_model,
                                       verified_facts=[{"fact_key": f"mech_{sidecar_model}", "value": "ok"}])
            self.assertTrue(sidecar_mech["ok"], sidecar_mech)
            self.assertEqual(sidecar_mech["reviewer_model"], sidecar_model)

            hetero_fail = self.commit(reconcile_hash=token, require_heterogeneous_review=True,
                                      reviewer_model=sidecar_model,
                                      verified_memories=[{"summary": "Boundary", "hard_boundary": True}])
            self.assertFalse(hetero_fail["ok"])
            self.assertIn("HETEROGENEOUS_REVIEW_REQUIRED", hetero_fail["error"])

        hetero_ok = self.commit(reconcile_hash=token, require_heterogeneous_review=True,
                                reviewer_model="6.1terra",
                                verified_memories=[{"memory_id": "hb_ok", "summary": "Boundary", "hard_boundary": True}])
        self.assertTrue(hetero_ok["ok"], hetero_ok)

    def test_stdio_lifecycle_errors_and_notification_silence(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "unknown"}},
            {"jsonrpc": "2.0", "id": 5, "method": "unknown"},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {
                "name": "sdk_memory_review_commit", "arguments": {"project_root": str(self.root), "reconcile_hash": "forged"}}}]
        process = subprocess.run([sys.executable, "-X", "utf8", str(SERVER)],
                                 input="{malformed\n" + "\n".join(json.dumps(r) for r in requests) + "\n",
                                 text=True, encoding="utf-8", capture_output=True, timeout=15,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(process.returncode, 0, process.stderr)
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(responses), 7)
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["result"]["serverInfo"]["name"], "context7-sdk-verifier")
        self.assertEqual(responses[2]["result"], {})
        self.assertEqual(len(responses[3]["result"]["tools"]), 3)
        self.assertTrue(responses[4]["result"]["isError"])
        self.assertEqual(responses[5]["error"]["code"], -32601)
        self.assertTrue(responses[6]["result"]["isError"])

    def test_p1_2_ttl_expiry_reviewer_guard_cache_path_and_quota_staleness(self):
        import time as _time
        from datetime import datetime, timedelta, timezone

        # 1. _is_expired supports both reverify_after and expires_at
        past_iso = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        future_iso = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        self.assertTrue(sdk._is_expired({"reverify_after": past_iso}))
        self.assertTrue(sdk._is_expired({"expires_at": past_iso}))
        self.assertFalse(sdk._is_expired({"reverify_after": future_iso}))
        self.assertFalse(sdk._is_expired({"expires_at": future_iso}))

        self.save({
            "project_id": "mods",
            "facts": [
                {"fact_key": "stale_reverify", "value": "FastAPI old", "reverify_after": past_iso},
                {"fact_key": "stale_expires", "value": "FastAPI old2", "expires_at": past_iso},
                {"fact_key": "fresh_reverify", "value": "FastAPI fresh", "reverify_after": future_iso},
            ],
            "memories": [],
            "experiences": [],
        })
        lookup = sdk.sdk_fact_lookup(str(self.root), packages=["fastapi"])
        self.assertEqual([f["fact_key"] for f in lookup["matched_facts"]], ["fresh_reverify"])

        # 2. _normalize_reviewer(None) never fabricates gpt-6.1-terra; high-impact requires heterogeneous review
        self.assertIsNone(sdk._normalize_reviewer(None))
        self.assertIsNone(sdk._normalize_reviewer("   "))
        token = self.report()["reconcile_hash"]

        # High-impact without reviewer_model is rejected by default
        hi_rejected = self.commit(reconcile_hash=token,
                                  verified_memories=[{"summary": "Boundary rule", "hard_boundary": True}])
        self.assertFalse(hi_rejected["ok"])
        self.assertIn("HETEROGENEOUS_REVIEW_REQUIRED", hi_rejected["error"])

        # Explicit require_heterogeneous_review=True rejects missing reviewer even on low-impact facts
        explicit_rejected = self.commit(reconcile_hash=token, require_heterogeneous_review=True,
                                        verified_facts=[{"fact_key": "f1", "value": "v1"}])
        self.assertFalse(explicit_rejected["ok"])
        self.assertIn("HETEROGENEOUS_REVIEW_REQUIRED", explicit_rejected["error"])

        # Explicit require_heterogeneous_review=False without reviewer_model records "mechanical-only"
        compat_ok = self.commit(reconcile_hash=token, require_heterogeneous_review=False,
                                verified_facts=[{"fact_key": "f_mech", "value": "v_mech"}])
        self.assertTrue(compat_ok["ok"], compat_ok)
        self.assertEqual(compat_ok["reviewer_model"], "mechanical-only")
        saved_fact = next(f for f in self.load()["facts"] if f["fact_key"] == "f_mech")
        self.assertEqual(saved_fact["reviewer_model"], "mechanical-only")

        # 3. Reconcile cache path is outside Mod source tree
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONTEXT7_RECONCILE_CACHE_PATH", None)
            resolved_cache = sdk._resolve_reconcile_cache_path()
            self.assertNotEqual(resolved_cache.parent, SERVER.parent)
            self.assertEqual(resolved_cache.name, "reconcile_cache.json")
            self.assertFalse((SERVER.parent / ".reconcile_cache.json").exists())

        # 4. all-pools-quota.json fetchedAtMs staleness check (> 30 min -> quota_stale=True)
        qfile = self.base / "quota_freshness.json"
        with patch.object(sdk, "QUOTA_CACHE_PATH", qfile):
            now_ms = int(_time.time() * 1000)
            write_json(qfile, {"fetchedAtMs": now_ms - 5 * 60 * 1000, "codex": {"weekly": 80.0}, "claude": {"weekly": 60.0}})
            fresh_q = sdk._select_reviewer_by_weekly_quota()
            self.assertFalse(fresh_q["quota_stale"])
            self.assertEqual(fresh_q["quota_fetched_at_ms"], now_ms - 5 * 60 * 1000)

            write_json(qfile, {"fetchedAtMs": now_ms - 35 * 60 * 1000, "codex": {"weekly": 80.0}, "claude": {"weekly": 60.0}})
            stale_q = sdk._select_reviewer_by_weekly_quota()
            self.assertTrue(stale_q["quota_stale"])

        # 5. Offline unmounted mirror path sets mirror_synced=False without failing primary commit
        unmounted_mirror = self.base / "offline_drive_not_mounted"
        with patch.object(sdk, "MIRROR_ROOT", unmounted_mirror):
            offline_res = self.commit(reconcile_hash=token, verified_facts=[{"fact_key": "offline_ok", "value": "1"}])
            self.assertTrue(offline_res["ok"], offline_res)
            self.assertFalse(offline_res["mirror_synced"])
            self.assertIn("MIRROR_OFFLINE", offline_res["mirror_warning"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
