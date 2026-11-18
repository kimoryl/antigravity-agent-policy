"""Copy one project's legacy SQLite memories to Hindsight without deleting them.

Only confirmed Hindsight writes enter the migration ledger. A failed or interrupted
run can therefore be repeated with the same command.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory_backend import (MemoryBackendError, hindsight_endpoint_id,
                            load_memory_config, memory_retain, resolve_memory_bank)
from policy_state import data_dir, record_event
from runtime_state import project_identity


LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS memory_migrations (
    legacy_memory_id TEXT NOT NULL,
    bank_id TEXT NOT NULL,
    endpoint_id TEXT NOT NULL,
    migrated_at REAL NOT NULL,
    PRIMARY KEY (legacy_memory_id, bank_id, endpoint_id)
)
"""


def _source_rows(db: sqlite3.Connection, project_id: str) -> list[sqlite3.Row]:
    return db.execute(
        """SELECT m.id, m.kind, m.summary, m.source_task_id, m.created_at,
                  t.id AS existing_task_id
           FROM memories AS m LEFT JOIN tasks AS t
             ON t.id = m.source_task_id AND t.project_id = m.project_id
           WHERE m.project_id = ? ORDER BY m.created_at, m.id""",
        (project_id,),
    ).fetchall()


def migrate(project: str | Path, *, execute: bool = False) -> dict[str, Any]:
    """Return counts for a project; dry-run performs no database or network writes."""
    project_id, project_root = project_identity(str(project))
    bank_id = resolve_memory_bank(project_root, project_id)
    try:
        endpoint_id = hindsight_endpoint_id(load_memory_config())
    except (MemoryBackendError, OSError, ValueError):
        endpoint_id = None
    summary: dict[str, Any] = {
        "project": project_root,
        "source_count": 0,
        "migrated": 0,
        "deduplicated": 0,
        "failed": 0,
        "bank_id": bank_id,
    }
    database = data_dir() / "runtime.sqlite3"
    if not database.is_file():
        return summary

    with closing(sqlite3.connect(database, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 5000")
        rows = _source_rows(db, project_id)
        summary["source_count"] = len(rows)
        ledger_exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memory_migrations'"
        ).fetchone() is not None
        scoped_ledger = ledger_exists and any(
            column[1] == "endpoint_id"
            for column in db.execute("PRAGMA table_info(memory_migrations)")
        )
        completed = (
            {row[0] for row in db.execute(
                "SELECT legacy_memory_id FROM memory_migrations WHERE bank_id = ? AND endpoint_id = ?",
                (bank_id, endpoint_id)
            )}
            if scoped_ledger and endpoint_id else set()
        )
        summary["deduplicated"] = sum(row["id"] in completed for row in rows)
        if not execute:
            return summary

        if ledger_exists and not scoped_ledger:
            # Old entries do not identify the destination. Preserve them for
            # audit, but do not let them suppress a copy to a new endpoint.
            db.execute("ALTER TABLE memory_migrations RENAME TO memory_migrations_unscoped")
        if not scoped_ledger:
            db.execute(LEDGER_SQL)
            db.commit()

        for row in rows:
            if row["id"] in completed:
                continue
            if row["existing_task_id"] is None:
                summary["failed"] += 1
                continue
            try:
                created_at = datetime.fromtimestamp(row["created_at"], timezone.utc)
                result = memory_retain(
                    row["source_task_id"], row["kind"], row["summary"],
                    {
                        "source": "sqlite_migration",
                        "migration_source": "sqlite_v1",
                        "legacy_memory_id": row["id"],
                        "created_at": created_at.isoformat().replace("+00:00", "Z"),
                    },
                    force_hindsight=True,
                )
                if (result.get("status") != "ok" or result.get("backend") != "hindsight"
                        or endpoint_id is None):
                    summary["failed"] += 1
                    continue
                inserted = db.execute(
                    "INSERT OR IGNORE INTO memory_migrations VALUES (?, ?, ?, ?)",
                    (row["id"], bank_id, endpoint_id, time.time()),
                ).rowcount
                db.commit()
                if inserted and not result.get("deduplicated"):
                    summary["migrated"] += 1
                else:
                    summary["deduplicated"] += 1
            except (OSError, ValueError, TypeError, OverflowError, sqlite3.Error):
                db.rollback()
                summary["failed"] += 1

    try:
        record_event("memory_migration", {
            "project_id": project_id,
            "bank_id": bank_id,
            "status": "complete" if summary["failed"] == 0 else "partial_failure",
            "source_count": summary["source_count"],
            "migrated": summary["migrated"],
            "deduplicated": summary["deduplicated"],
            "failed": summary["failed"],
        })
    except OSError:
        pass
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="Project directory with legacy task memory")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Count legacy records without writing")
    mode.add_argument("--execute", action="store_true", help="Copy pending records to Hindsight")
    args = parser.parse_args(argv)
    try:
        summary = migrate(args.project, execute=args.execute)
    except (OSError, ValueError, sqlite3.Error):
        parser.exit(2, "Migration could not read the project or runtime database.\n")
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
