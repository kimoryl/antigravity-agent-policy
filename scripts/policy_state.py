"""Small local state store shared by Codex policy hooks."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def data_dir() -> Path:
    configured = os.environ.get("CODEX_AGENT_POLICY_DATA") or os.environ.get("PLUGIN_DATA")
    if configured:
        base = Path(configured)
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "codex-agent-policy"
    else:
        base = Path.home() / ".local" / "state" / "codex-agent-policy"
    base.mkdir(parents=True, exist_ok=True)
    return base


@contextmanager
def _connect():
    connection = sqlite3.connect(data_dir() / "state.sqlite3", timeout=2)
    connection.execute("PRAGMA busy_timeout = 2000")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS slots ("
        "slot_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT, "
        "agent_id TEXT, status TEXT NOT NULL, created_at REAL NOT NULL, "
        "pool_id TEXT, team_id TEXT, parent_agent_id TEXT, depth INTEGER NOT NULL DEFAULT 0)"
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(slots)").fetchall()}
    for column, definition in (("pool_id", "TEXT"), ("team_id", "TEXT"), ("parent_agent_id", "TEXT"), ("depth", "INTEGER NOT NULL DEFAULT 0")):
        if column not in columns:
            connection.execute(f"ALTER TABLE slots ADD COLUMN {column} {definition}")
    connection.execute("UPDATE slots SET pool_id = session_id WHERE pool_id IS NULL")
    # Schema migration/update statements open an implicit transaction. Commit
    # it before callers that need to acquire the write lock explicitly.
    connection.commit()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def reserve_slot(session_id: str, turn_id: str, slot_id: str, maximum: int, *, pool_id: str | None = None, team_id: str | None = None, parent_agent_id: str | None = None, depth: int = 0) -> bool:
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        now = time.time()
        db.execute("DELETE FROM slots WHERE status = 'pending' AND created_at < ?", (now - 120,))
        db.execute("DELETE FROM slots WHERE created_at < ?", (now - 86400,))
        pool = pool_id or session_id
        count = db.execute(
            "SELECT COUNT(*) FROM slots WHERE pool_id = ?", (pool,)
        ).fetchone()[0]
        if count >= maximum:
            return False
        db.execute(
            "INSERT OR REPLACE INTO slots(slot_id,session_id,turn_id,agent_id,status,created_at,pool_id,team_id,parent_agent_id,depth) VALUES (?, ?, ?, NULL, 'pending', ?, ?, ?, ?, ?)",
            (slot_id, session_id, turn_id, now, pool, team_id, parent_agent_id, depth),
        )
        return True


def mark_started(session_id: str, turn_id: str, agent_id: str, *, pool_id: str | None = None, team_id: str | None = None, parent_agent_id: str | None = None, depth: int = 0) -> None:
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        pool = pool_id or session_id
        row = db.execute(
            "SELECT slot_id FROM slots WHERE pool_id = ? AND turn_id = ? "
            "AND status = 'pending' ORDER BY created_at LIMIT 1",
            (pool, turn_id),
        ).fetchone()
        if row is None:
            row = db.execute(
                "SELECT slot_id FROM slots WHERE pool_id = ? AND status = 'pending' "
                "ORDER BY created_at LIMIT 1",
                (pool,),
            ).fetchone()
        if row:
            db.execute(
                "UPDATE slots SET status = 'active', agent_id = ? WHERE slot_id = ?",
                (agent_id, row[0]),
            )
        else:
            db.execute(
                "INSERT OR REPLACE INTO slots(slot_id,session_id,turn_id,agent_id,status,created_at,pool_id,team_id,parent_agent_id,depth) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
                (agent_id, session_id, turn_id, agent_id, time.time(), pool, team_id, parent_agent_id, depth),
            )


def release_slot(slot_id: str) -> None:
    with _connect() as db:
        db.execute("DELETE FROM slots WHERE slot_id = ?", (slot_id,))


def mark_stopped(agent_id_or_session: str, agent_id: str | None = None) -> None:
    with _connect() as db:
        if agent_id is not None:
            db.execute("DELETE FROM slots WHERE session_id = ? AND agent_id = ?", (agent_id_or_session, agent_id))
        else:
            db.execute("DELETE FROM slots WHERE agent_id = ?", (agent_id_or_session,))


def clear_session(session_id: str) -> None:
    with _connect() as db:
        db.execute("DELETE FROM slots WHERE session_id = ?", (session_id,))


def slot_snapshot(pool_id: str | None = None) -> dict[str, Any]:
    with _connect() as db:
        if pool_id:
            rows = db.execute("SELECT status, COUNT(*) FROM slots WHERE pool_id = ? GROUP BY status", (pool_id,)).fetchall()
        else:
            rows = db.execute("SELECT status, COUNT(*) FROM slots GROUP BY status").fetchall()
    counts = {"pending": 0, "active": 0}
    counts.update({row[0]: row[1] for row in rows})
    return {"pool_id": pool_id, "pending": counts.get("pending", 0), "active": counts.get("active", 0), "running": counts.get("pending", 0) + counts.get("active", 0)}


def record_event(kind: str, details: dict[str, Any]) -> None:
    """Store only caller-provided metadata; never record prompts or tool output."""
    entry = {"time": time.time(), "kind": kind, **details}
    with (data_dir() / "trace.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    try:
        from runtime_events import append_legacy
        append_legacy(kind, details)
    except (ImportError, OSError, ValueError, TypeError):
        # Structured event persistence is additive; legacy trace must remain reliable.
        pass
