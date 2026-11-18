"""Append-only Runtime Event Protocol backed by the existing SQLite state."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from policy_state import data_dir


_TEXT = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SENSITIVE = re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|password|secret|credential|private[_-]?key|begin [a-z ]*private key)")
_MAX_PAYLOAD = 12000


def _db_path() -> Path:
    return data_dir() / "state.sqlite3"


@contextmanager
def _connect_db(path: Path):
    conn = sqlite3.connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _safe_payload(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("event payload must be an object")
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(raw) > _MAX_PAYLOAD or _SENSITIVE.search(raw):
        raise ValueError("event payload contains unsafe or oversized data")
    return value


def _ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS runtime_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            schema_version INTEGER NOT NULL,
            type TEXT NOT NULL,
            timestamp REAL NOT NULL,
            project_id TEXT,
            task_id TEXT,
            agent_id TEXT,
            parent_agent_id TEXT,
            payload TEXT NOT NULL
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_runtime_events_project ON runtime_events(project_id, seq)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_runtime_events_task ON runtime_events(task_id, seq)")


def emit(event_type: str, *, project_id: str | None = None, task_id: str | None = None,
         agent_id: str | None = None, parent_agent_id: str | None = None,
         payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(event_type, str) or not _TEXT.fullmatch(event_type):
        raise ValueError("event type is invalid")
    fields = {"project_id": project_id, "task_id": task_id, "agent_id": agent_id,
              "parent_agent_id": parent_agent_id}
    for key, value in fields.items():
        if value is not None and (not isinstance(value, str) or len(value) > 256):
            raise ValueError(f"{key} is invalid")
    item = {
        "event_id": uuid4().hex,
        "schema_version": 1,
        "type": event_type,
        "timestamp": time.time(),
        **fields,
        "payload": _safe_payload(payload),
    }
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect_db(path) as db:
        _ensure_schema(db)
        db.execute("""INSERT INTO runtime_events
            (event_id, schema_version, type, timestamp, project_id, task_id, agent_id, parent_agent_id, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            item["event_id"], item["schema_version"], item["type"], item["timestamp"],
            project_id, task_id, agent_id, parent_agent_id,
            json.dumps(item["payload"], ensure_ascii=False, separators=(",", ":"))))
        item["seq"] = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
    return item


def append_legacy(kind: str, details: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Bridge existing JSONL events into the structured protocol."""
    mapping = {
        "task_created": "task.created", "task_completed": "task.completed",
        "task_revised": "task.revised", "context_requested": "context.requested",
        "context_loaded": "context.loaded", "team_created": "team.created",
        "team_agent_registered": "team.agent_registered", "experience_proposed": "experience.proposed",
        "experience_promoted": "experience.promoted", "experience_validated": "experience.validated",
    }
    details = details if isinstance(details, dict) else {}
    event_type = mapping.get(kind, "legacy." + re.sub(r"[^A-Za-z0-9_.:-]", "_", str(kind))[:100])
    try:
        return emit(event_type, project_id=details.get("project_id"), task_id=details.get("task_id"),
                    agent_id=details.get("agent_id"), parent_agent_id=details.get("parent_agent_id"),
                    payload=details)
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return None


def read_events(cursor: int = 0, limit: int = 100, *, project_id: str | None = None,
                task_id: str | None = None, event_type: str | None = None,
                since: float | None = None) -> dict[str, Any]:
    if not isinstance(cursor, int) or cursor < 0 or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("cursor or limit is invalid")
    clauses = ["seq > ?"]
    params: list[Any] = [cursor]
    if project_id is not None:
        clauses.append("project_id = ?"); params.append(project_id)
    if task_id is not None:
        clauses.append("task_id = ?"); params.append(task_id)
    if event_type is not None:
        clauses.append("type = ?"); params.append(event_type)
    if since is not None:
        clauses.append("timestamp >= ?"); params.append(float(since))
    query = "SELECT * FROM runtime_events WHERE " + " AND ".join(clauses) + " ORDER BY seq LIMIT ?"
    params.append(limit)
    path = _db_path()
    if not path.exists():
        return {"events": [], "next_cursor": cursor}
    with _connect_db(path) as db:
        _ensure_schema(db)
        rows = db.execute(query, params).fetchall()
    events = []
    for row in rows:
        events.append({"seq": row[0], "event_id": row[1], "schema_version": row[2], "type": row[3],
                       "timestamp": row[4], "project_id": row[5], "task_id": row[6],
                       "agent_id": row[7], "parent_agent_id": row[8], "payload": json.loads(row[9])})
    return {"events": events, "next_cursor": events[-1]["seq"] if events else cursor}


def event_schema() -> dict[str, Any]:
    return {"schema_version": 1, "append_only": True,
            "fields": ["seq", "event_id", "schema_version", "type", "timestamp",
                       "project_id", "task_id", "agent_id", "parent_agent_id", "payload"]}
