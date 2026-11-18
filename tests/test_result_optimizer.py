"""Behavioral acceptance tests for recoverable V1 tool-result compression."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "hooks"))

import dashboard  # noqa: E402
import evidence_store  # noqa: E402
import result_optimizer  # noqa: E402
from evidence_store import (EvidenceError, cleanup_expired, complete_task_evidence,  # noqa: E402
                            evidence_metrics, evidence_refs, retrieve_evidence)
from policy_hook import handle as handle_hook  # noqa: E402
from result_optimizer import load_result_config, optimize_result  # noqa: E402
from runtime_state import (checkpoint, create_task, get_task, list_facts,  # noqa: E402
                           list_memories, restore)
from runtime_tools import call as runtime_call  # noqa: E402
try:
    from skill_resolver import SkillService  # noqa: E402
except ImportError:
    SkillService = lambda: None


PROFILE = {
    "root_goal": "Test temporary result evidence", "local_goal": "Exercise recovery",
    "task_type": "test", "constraints": [], "expected_outputs": [],
    "acceptance_criteria": [], "required_capabilities": [], "complexity_hint": "simple",
}
HIDDEN = "UNIQUE_RECOVERY_PROBE_92b7e"


def test_output() -> str:
    filler = [f"routine case {i:03d} completed normally with ordinary diagnostic detail" for i in range(240)]
    filler[60] = HIDDEN
    return "\n".join([
        "pytest -q tests/test_math.py",
        *filler[:120],
        "FAILED tests/test_math.py::test_boundary - AssertionError: 4 != 5",
        "tests/test_math.py:41: AssertionError",
        *filler[120:],
        "1 failed, 5 passed, 1 skipped in 0.42s",
    ]) + "\n"


class ResultCompressionAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        previous = os.environ.get("CODEX_AGENT_POLICY_DATA")
        os.environ["CODEX_AGENT_POLICY_DATA"] = self.folder.name
        self.addCleanup(self._restore_data_dir, previous)
        self.base = Path(self.folder.name)
        self.project_a = self.base / "project-a"
        self.project_b = self.base / "project-b"
        self.project_a.mkdir()
        self.project_b.mkdir()
        self.task = create_task(PROFILE, str(self.project_a))
        self.task_id = self.task["task_id"]
        self.config_path = self.base / "compression.json"
        self.config()

    @staticmethod
    def _restore_data_dir(previous: str | None) -> None:
        if previous is None:
            os.environ.pop("CODEX_AGENT_POLICY_DATA", None)
        else:
            os.environ["CODEX_AGENT_POLICY_DATA"] = previous

    def config(self, **overrides: object) -> Path:
        settings = {"threshold_chars": 256, "max_compact_chars": 1400,
                    "max_retrieve_chars": 20000, "retention_days": 7}
        settings.update(overrides)
        self.config_path.write_text(json.dumps({"result_compression": settings}), encoding="utf-8")
        return self.config_path

    def optimize(self, content: str, **kwargs: object) -> dict:
        return optimize_result(self.task_id, content, config_path=self.config_path, **kwargs)

    def test_ac01_small_result_bypasses_without_evidence(self) -> None:
        original = "ordinary short output\r\n"
        result = self.optimize(original, tool="exec_command")
        self.assertEqual(result["compression"], "bypass")
        self.assertEqual(result["compact_result"], original)
        self.assertIsNone(result["evidence_id"])
        self.assertEqual(evidence_refs(self.task_id), [])
        self.assertEqual(evidence_metrics(self.task_id)["bypass_count"], 1)

    def test_common_authorization_and_cloud_secret_assignments_are_screened(self) -> None:
        prefixes = ("Authorization: Basic " + "YWFhYmJiY2Nj" * 4,
                    "AWS_SECRET_ACCESS_KEY=" + "a" * 40,
                    json.dumps({"AWS_SECRET_ACCESS_KEY": "a" * 40}))
        for prefix in prefixes:
            with self.subTest(prefix=prefix.split(":")[0].split("=")[0]):
                self.assertEqual(result_optimizer.screen_content(prefix), "credential_like_content")
                result = self.optimize(prefix + "\n" + "ordinary case\n" * 700)
                self.assertEqual(result["compression"], "failed")
                self.assertIsNone(result["evidence_id"])
                self.assertNotIn(prefix, result["compact_result"])
        self.assertEqual(evidence_refs(self.task_id), [])

    def test_ac02_large_test_output_is_compact_and_diagnostic(self) -> None:
        original = test_output()
        result = self.optimize(original, tool="exec_command", command="pytest -q tests/test_math.py",
                               exit_code=1)
        compact = result["compact_result"]
        self.assertEqual(result["compression"], "compressed")
        self.assertEqual(result["result_type"], "test_output")
        self.assertTrue(result["evidence_id"].startswith("ev_"))
        self.assertLess(len(compact), len(original) // 2)
        self.assertIn("failed=1", compact)
        self.assertIn("passed=5", compact)
        self.assertIn("skipped=1", compact)
        self.assertIn("tests/test_math.py::test_boundary", compact)
        self.assertIn("tests/test_math.py:41", compact)
        self.assertIn(result["evidence_id"], compact)
        self.assertNotIn(HIDDEN, compact)

    def test_unittest_expected_failures_are_not_reported_as_failures(self) -> None:
        original = ("ordinary test line\n" * 700) + "Ran 10 tests in 0.1s\n\nOK (expected failures=2)\n"
        result = self.optimize(original, result_type="test_output", command="python -m unittest")
        self.assertEqual(result["compression"], "compressed")
        self.assertIn("passed=8", result["compact_result"])
        self.assertIn("failed=0", result["compact_result"])
        self.assertIn("expected_failures=2", result["compact_result"])

    def test_ac03_unicode_and_crlf_are_retrieved_byte_exact_with_hash(self) -> None:
        original = ("雪☃️\tαβγ\r\n" * 80) + "last line without newline"
        result = self.optimize(original, result_type="generic_text")
        self.assertEqual(result["compression"], "compressed")
        recovered = retrieve_evidence(self.task_id, result["evidence_id"], "get")
        self.assertEqual(recovered["status"], "ok")
        self.assertTrue(recovered["hash_verified"])
        self.assertEqual(recovered["content"], original)
        self.assertEqual(recovered["content"].encode("utf-8"), original.encode("utf-8"))
        self.assertEqual(recovered["hash"], hashlib.sha256(original.encode("utf-8")).hexdigest())

    def test_ac04_progressive_retrieval_and_explicit_full_read_limit(self) -> None:
        lines = [f"src/a.py:{i}:ordinary match {i}" for i in range(1, 140)]
        lines[69] = f"src/a.py:70:{HIDDEN}"
        original = "\n".join(lines) + "\n"
        result = self.optimize(original, result_type="search_result", command="rg ordinary src")
        handle = result["evidence_id"]
        compact = result["compact_result"]
        self.assertEqual(result["compression"], "compressed")
        self.assertIn("matches", compact)
        self.assertNotIn(HIDDEN, compact)

        metadata = retrieve_evidence(self.task_id, handle, "metadata")
        self.assertEqual(metadata["status"], "ok")
        self.assertEqual(metadata["metadata"]["hash"], hashlib.sha256(original.encode()).hexdigest())
        self.assertNotIn("content", metadata)
        found = retrieve_evidence(self.task_id, handle, "search", query=HIDDEN, max_chars=800)
        self.assertEqual(found["total_matches"], 1)
        self.assertIn(HIDDEN, found["content"])
        self.assertLess(len(found["content"]), len(original))
        self.assertEqual(found["matches"][0]["line"], 70)
        excerpt = retrieve_evidence(self.task_id, handle, "range", start_line=69, end_line=71,
                                    max_chars=800)
        self.assertEqual(excerpt["content"], "".join(original.splitlines(keepends=True)[68:71]))
        self.assertLess(len(excerpt["content"]), len(original))
        too_large = retrieve_evidence(self.task_id, handle, "get", max_chars=800)
        self.assertEqual(too_large["status"], "evidence_too_large")
        self.assertNotIn("content", too_large)
        complete = retrieve_evidence(self.task_id, handle, "get")
        self.assertEqual(complete["content"], original)

    def test_ac05_evidence_is_scoped_to_task_and_project_and_ids_are_not_paths(self) -> None:
        result = self.optimize(test_output(), result_type="test_output")
        handle = result["evidence_id"]
        other_project = create_task(PROFILE, str(self.project_b))["task_id"]
        other_task = create_task(PROFILE, str(self.project_a))["task_id"]
        for task_id in (other_project, other_task):
            with self.subTest(task_id=task_id):
                self.assertEqual(retrieve_evidence(task_id, handle, "get")["status"],
                                 "evidence_not_found")
        self.assertEqual(retrieve_evidence(self.task_id, "../" + handle, "get")["status"],
                         "invalid_evidence_id")
        self.assertEqual(retrieve_evidence("../" + self.task_id, handle, "get")["status"],
                         "task_not_found")
        with self.assertRaisesRegex(Exception, "filesystem path"):
            runtime_call("runtime_evidence", {"action": "get", "task_id": self.task_id,
                                               "evidence_id": handle, "path": "../evidence"},
                         SkillService())

    def test_ac06_raw_text_stays_out_of_memory_facts_trace_and_checkpoint(self) -> None:
        result = self.optimize(test_output(), result_type="test_output")
        handle = result["evidence_id"]
        self.assertEqual(list_memories(self.task_id), [])
        self.assertEqual(list_facts(self.task_id), [])
        self.assertNotIn(HIDDEN, json.dumps(get_task(self.task_id)["state"]))
        saved = checkpoint(self.task_id, "after_result")
        restored = restore(self.task_id, saved["checkpoint_id"])
        self.assertNotIn(HIDDEN, json.dumps(restored["state"]))
        trace = (self.base / "trace.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(HIDDEN, trace)
        self.assertEqual(retrieve_evidence(self.task_id, handle, "search", query=HIDDEN)["total_matches"], 1)

    def test_ac07_parser_and_storage_failures_do_not_fail_the_task(self) -> None:
        original = test_output()

        def broken_parser(*_args: object) -> str:
            raise ValueError("synthetic parser failure")

        with patch.dict(result_optimizer.PARSER_REGISTRY, {"test_output": broken_parser}):
            fallback = self.optimize(original, result_type="test_output")
        self.assertEqual(fallback["compression"], "compressed")
        self.assertEqual(fallback["parser_type"], "safe_truncation")
        self.assertEqual(retrieve_evidence(self.task_id, fallback["evidence_id"], "get")["content"],
                         original)

        with patch.object(result_optimizer, "save_evidence", side_effect=EvidenceError("storage_error")):
            failed = self.optimize(original, result_type="test_output")
        self.assertEqual(failed["compression"], "failed")
        self.assertIsNone(failed["evidence_id"])
        self.assertNotIn(HIDDEN, failed["compact_result"])
        self.assertLessEqual(len(failed["compact_result"]), load_result_config(self.config_path)["max_compact_chars"])
        self.assertEqual(get_task(self.task_id)["status"], "active")

    def test_ac08_compression_metrics_do_not_invent_context_token_usage(self) -> None:
        initial_usage = get_task(self.task_id)["state"]["usage"]
        result = self.optimize(test_output(), result_type="test_output")
        retrieve_evidence(self.task_id, result["evidence_id"], "search", query=HIDDEN)
        metrics = evidence_metrics(self.task_id)
        self.assertEqual(metrics["status"], "ok")
        self.assertEqual(metrics["raw_chars"], len(test_output()))
        self.assertEqual(metrics["compact_chars"], len(result["compact_result"]))
        self.assertIn("not observed model context usage", metrics["usage_kind"])
        self.assertEqual(get_task(self.task_id)["state"]["usage"], initial_usage)

    def test_ac09_checkpoint_keeps_metadata_refs_and_recovery_handle(self) -> None:
        original = test_output()
        result = self.optimize(original, result_type="test_output")
        saved = checkpoint(self.task_id, "safe_point")
        restored = restore(self.task_id, saved["checkpoint_id"])
        refs = restored["state"]["evidence_refs"]
        self.assertEqual([item["evidence_id"] for item in refs], [result["evidence_id"]])
        compact_refs = restored["state"]["compact_result_refs"]
        self.assertEqual(compact_refs, [{"evidence_id": result["evidence_id"],
                                         "compact_result_id": refs[0]["compact_result_id"]}])
        self.assertEqual(refs[0]["hash"], hashlib.sha256(original.encode()).hexdigest())
        self.assertNotIn(HIDDEN, json.dumps(refs))
        self.assertNotIn(HIDDEN, json.dumps(compact_refs))
        self.assertEqual(retrieve_evidence(self.task_id, result["evidence_id"], "get")["content"],
                         original)

    def test_ac10_completed_evidence_expires_without_broken_handles(self) -> None:
        self.config(retention_days=1)
        result = self.optimize(test_output(), result_type="test_output")
        handle = result["evidence_id"]
        complete_task_evidence(self.task_id)
        self.assertEqual(retrieve_evidence(self.task_id, handle, "metadata")["metadata"]["status"],
                         "completed")
        future = time.time() + 2 * 86400
        with patch.object(evidence_store.time, "time", return_value=future):
            cleanup = cleanup_expired(self.task_id)
        self.assertEqual(cleanup["status"], "ok")
        self.assertEqual(cleanup["expired"], 1)
        self.assertEqual(retrieve_evidence(self.task_id, handle, "get")["status"],
                         "evidence_expired")
        self.assertEqual(evidence_refs(self.task_id), [])

    def test_ac11_dashboard_publishes_metadata_without_raw_body(self) -> None:
        result = self.optimize(test_output(), result_type="test_output")
        retrieve_evidence(self.task_id, result["evidence_id"], "search", query=HIDDEN)
        state = dashboard.snapshot(SkillService())
        entry = next(item for item in state["tasks"] if item["task_id"] == self.task_id)
        summary = entry["compression"]
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["raw_chars"], len(test_output()))
        self.assertEqual(summary["compact_chars"], len(result["compact_result"]))
        self.assertGreater(summary["retrieve_count"], 0)
        self.assertEqual(summary["latest_result_type"], "test_output")
        self.assertIn(result["evidence_id"], json.dumps(summary))
        self.assertNotIn(HIDDEN, json.dumps(state))

    def test_runtime_tools_route_optimizer_and_progressive_retrieval(self) -> None:
        original = test_output()
        service = SkillService()
        result = runtime_call("runtime_result", {"action": "optimize", "task_id": self.task_id,
                                                 "content": original, "result_type": "test_output"}, service)
        self.assertEqual(result["compression"], "compressed")
        handle = result["evidence_id"]
        metadata = runtime_call("runtime_evidence", {"action": "metadata", "task_id": self.task_id,
                                                    "evidence_id": handle}, service)
        self.assertEqual(metadata["metadata"]["hash"], hashlib.sha256(original.encode()).hexdigest())
        found = runtime_call("runtime_evidence", {"action": "search", "task_id": self.task_id,
                                                 "evidence_id": handle, "query": HIDDEN}, service)
        self.assertEqual(found["total_matches"], 1)
        lines = original.splitlines(keepends=True)
        line_number = next(i for i, line in enumerate(lines, 1) if HIDDEN in line)
        excerpt = runtime_call("runtime_evidence", {"action": "range", "task_id": self.task_id,
                                                   "evidence_id": handle, "start_line": line_number,
                                                   "end_line": line_number}, service)
        self.assertEqual(excerpt["content"], lines[line_number - 1])
        self.assertEqual(runtime_call("runtime_evidence", {"action": "get", "task_id": self.task_id,
                                                       "evidence_id": handle}, service)["content"], original)
        self.assertEqual(runtime_call("runtime_evidence", {"action": "cleanup", "task_id": self.task_id},
                                      service)["status"], "ok")

    def test_post_tool_hook_requires_unambiguous_bound_session_and_screens_secrets(self) -> None:
        session = "synthetic-session"
        created = handle_hook({
            "hook_event_name": "PostToolUse", "tool_name": "mcp__agent_policy__runtime_task",
            "session_id": session, "cwd": str(self.project_a),
            "tool_input": {"action": "create", "project_root": str(self.project_a)},
            "tool_response": {"structuredContent": {"task_id": self.task_id}},
        })
        self.assertEqual(created, {})
        event = {"hook_event_name": "PostToolUse", "tool_name": "exec_command",
                 "session_id": session, "tool_input": {"cmd": "pytest -q"},
                 "tool_response": {"output": test_output(), "exit_code": 1}}
        compact = handle_hook(event)
        self.assertFalse(compact["continue"])
        self.assertIn("failed=1", compact["stopReason"])
        self.assertNotIn(HIDDEN, compact["stopReason"])
        self.assertEqual(len(evidence_refs(self.task_id)), 1)

        self.assertEqual(handle_hook({**event, "session_id": "missing-session"}), {})
        screened = handle_hook({**event, "tool_response": {
            "output": "api_key=synthetic-only-value\n" + ("ordinary line\n" * 1000)}})
        self.assertNotIn("synthetic-only-value", json.dumps(screened))
        self.assertEqual(len(evidence_refs(self.task_id)), 1)

        create_task(PROFILE, str(self.project_a), session_id=session)
        self.assertEqual(handle_hook(event), {})
        self.assertEqual(len(evidence_refs(self.task_id)), 1)


if __name__ == "__main__":
    unittest.main()
