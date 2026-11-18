from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_memory_backend import MemoryHarness  # noqa: E402

from runtime_engine import resolve_context  # noqa: E402
from runtime_state import add_memory, put_fact  # noqa: E402


class MemoryContextTests(MemoryHarness):
    def test_repository_fact_and_memory_have_distinct_labels_and_share_budget(self) -> None:
        task_id = self.make_task()
        source = self.project / "architecture.txt"
        source.write_text("The stable interface remains public.\n" + "Current repository detail. " * 50,
                          encoding="utf-8")
        put_fact(task_id, "interface", "Keep the stable public interface " * 8,
                 "architecture.txt", "The stable interface remains public.")
        add_memory(task_id, "lesson", "Historical lesson: validate interfaces. " * 10)

        context = resolve_context(task_id, "stable interface", ["architecture.txt"],
                                  max_chars=700, use_jev=False, memory_max_chars=500)
        self.assertEqual(context["sources"][0]["label"], "REFERENCE")
        self.assertEqual(context["facts"][0]["label"], "FACT")
        self.assertEqual(context["memory"][0]["label"], "HISTORICAL_MEMORY")
        self.assertEqual(context["memory_status"]["backend"], "sqlite")
        budget = context["context_budget"]
        visible_chars = (
            len(context["task_snapshot"]["root_goal"])
            + sum(map(len, context["task_snapshot"]["constraints"]))
            + sum(len(item["value"]) + len(item["evidence"]) for item in context["facts"])
            + sum(len(item["text"]) for item in context["memory"])
            + sum(len(item["excerpt"]) for item in context["sources"])
        )
        self.assertLessEqual(visible_chars, 700)
        self.assertLessEqual(budget["used_chars"], budget["max_chars"])
        self.assertLessEqual(budget["memory_chars"], 140)  # 20% of total context
        self.assertLessEqual(budget["fact_chars"], 105)    # 15% of total context
        self.assertEqual(budget["source_chars"], len(context["sources"][0]["excerpt"]))

        # A fact tied to a changed file is no longer presented as current truth.
        source.write_text("A new interface replaced the old one.\n", encoding="utf-8")
        refreshed = resolve_context(task_id, "stable interface", ["architecture.txt"],
                                    max_chars=700, use_jev=False)
        self.assertEqual(refreshed["facts"], [])

    def test_context_does_not_recall_when_memory_is_disabled(self) -> None:
        server = self.start_server()
        task_id = self.make_task()
        (self.project / "source.txt").write_text("Current source of truth", encoding="utf-8")
        context = resolve_context(task_id, "source", ["source.txt"], max_chars=600,
                                  use_jev=False, memory=False)
        self.assertEqual(context["memory"], [])
        self.assertEqual(context["memory_status"]["status"], "disabled")
        self.assertEqual(server.requests, [])

    def test_all_dynamic_context_text_including_git_and_paths_fits_budget(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is unavailable")
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True,
                       capture_output=True, timeout=10)
        subprocess.run(["git", "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit", "--allow-empty",
                        "-q", "-m", "base"], cwd=self.project, check=True,
                       capture_output=True, timeout=10)
        directory = self.project / ("long_directory_" * 7)
        directory.mkdir()
        source = directory / ("source_name_" * 6 + ".txt")
        relative = source.relative_to(self.project).as_posix()
        source.write_text("Evidence stays tied to its source.\n" + "Relevant content " * 30,
                          encoding="utf-8")
        for index in range(12):
            (self.project / (f"changed_{index:02d}_" + "x" * 90 + ".txt")).write_text(
                "Untracked context candidate", encoding="utf-8")
        task_id = self.make_task()
        put_fact(task_id, "important_fact_" * 10, "Long fact value " * 30,
                 relative, "Evidence stays tied to its source.")
        add_memory(task_id, "lesson", "Long historical lesson " * 20)

        context = resolve_context(task_id, "Relevant content", [relative],
                                  max_chars=500, use_jev=False)
        self.assertTrue(context["git"]["available"])
        raw_status = subprocess.run(["git", "status", "--short"], cwd=self.project,
                                    check=True, capture_output=True, text=True, timeout=10)
        self.assertGreaterEqual(len(raw_status.stdout.splitlines()), 12)

        def size(value: object) -> int:
            return len(value) if isinstance(value, str) else 0

        snapshot = context["task_snapshot"]
        visible = size(snapshot["root_goal"])
        visible += sum(map(size, snapshot["constraints"]))
        visible += sum(size(item["id"]) + size(item["status"])
                       for item in snapshot["plan"])
        visible += sum(size(fact.get(field)) for fact in context["facts"]
                       for field in ("key", "value", "source_path", "evidence",
                                     "source_revision"))
        visible += sum(size(item.get(field)) for item in context["memory"]
                       for field in ("text", "kind", "source_task_id", "created_at"))
        visible += sum(size(item.get(field)) for item in context["sources"]
                       for field in ("path", "excerpt"))
        visible += size(context["git"].get("revision"))
        visible += sum(map(size, context["git"].get("changed_paths", [])))
        self.assertLessEqual(visible, 500)
        self.assertLessEqual(visible, context["context_budget"]["used_chars"])
        self.assertLessEqual(context["context_budget"]["used_chars"], 500)


if __name__ == "__main__":
    import unittest
    unittest.main()
