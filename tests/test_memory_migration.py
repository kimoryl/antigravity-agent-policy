from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_memory_backend import MemoryHarness, hindsight_memory  # noqa: E402

from migrate_memory import migrate  # noqa: E402
from policy_state import data_dir  # noqa: E402
from runtime_state import add_memory, list_memories  # noqa: E402


class MemoryMigrationTests(MemoryHarness):
    def test_dry_run_is_read_only_and_partial_failure_resumes_idempotently(self) -> None:
        task_id = self.make_task()
        add_memory(task_id, "decision", "Preserve the public interface")
        add_memory(task_id, "lesson", "Retry only the failed copy")
        database = data_dir() / "runtime.sqlite3"

        preview = migrate(self.project, execute=False)
        self.assertEqual((preview["source_count"], preview["migrated"],
                          preview["deduplicated"], preview["failed"]), (2, 0, 0, 0))
        with sqlite3.connect(database) as db:
            self.assertIsNone(db.execute(
                "SELECT name FROM sqlite_master WHERE name = 'memory_migrations'").fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 2)

        server = self.start_server()
        server.retain_fail_on = {2}
        first = migrate(self.project, execute=True)
        self.assertEqual((first["source_count"], first["migrated"], first["failed"]),
                         (2, 1, 1))
        self.assertEqual(server.retain_attempts, 2)
        with sqlite3.connect(database) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memory_migrations").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 2)

        # A new execution checks confirmed rows in the ledger and retries the failed row.
        hindsight_memory._HEALTH_CACHE.clear()
        server.retain_fail_on.clear()
        second = migrate(self.project, execute=True)
        self.assertEqual((second["migrated"], second["deduplicated"], second["failed"]),
                         (1, 1, 0))
        self.assertEqual(server.retain_attempts, 3)
        third = migrate(self.project, execute=True)
        self.assertEqual((third["migrated"], third["deduplicated"], third["failed"]),
                         (0, 2, 0))
        self.assertEqual(server.retain_attempts, 3)
        with sqlite3.connect(database) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memory_migrations").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 2)
        self.assertEqual(len(list_memories(task_id)), 2)

    def test_failed_destination_never_marks_legacy_row_migrated(self) -> None:
        task_id = self.make_task()
        add_memory(task_id, "finding", "Legacy data remains available")
        server = self.start_server()
        server.health_status = 401

        result = migrate(self.project, execute=True)
        self.assertEqual((result["source_count"], result["migrated"], result["failed"]),
                         (1, 0, 1))
        self.assertEqual(server.calls("/memories"), [])
        with sqlite3.connect(data_dir() / "runtime.sqlite3") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memory_migrations").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 1)

    def test_each_endpoint_receives_legacy_rows_once(self) -> None:
        task_id = self.make_task()
        add_memory(task_id, "decision", "A portable project decision")
        first_server = self.start_server()
        first = migrate(self.project, execute=True)
        self.assertEqual((first["migrated"], first["failed"]), (1, 0))
        self.assertEqual(first_server.retain_attempts, 1)

        second_server = self.start_server()
        second = migrate(self.project, execute=True)
        self.assertEqual((second["migrated"], second["failed"]), (1, 0))
        self.assertEqual(second_server.retain_attempts, 1)

        self.write_config(url=first_server.url)
        third = migrate(self.project, execute=True)
        self.assertEqual((third["deduplicated"], third["migrated"], third["failed"]),
                         (1, 0, 0))
        self.assertEqual(first_server.retain_attempts, 1)
        self.assertEqual(second_server.retain_attempts, 1)
        with sqlite3.connect(data_dir() / "runtime.sqlite3") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memory_migrations").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 1)


if __name__ == "__main__":
    import unittest
    unittest.main()
