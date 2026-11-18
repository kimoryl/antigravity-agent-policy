"""Bounded, task/project-scoped temporary evidence in the existing Runtime DB.

This module never resolves evidence IDs as paths and never writes Memory/Facts.
Raw content is exact UTF-8 text; metadata and trace events contain no raw output.
"""

from __future__ import annotations

import re
import time
from hashlib import sha256
from secrets import token_hex
from typing import Any

from policy_state import record_event
from runtime_state import _db, get_task


class EvidenceError(ValueError):
    """An expected, safe-to-report evidence failure code."""


_HANDLE = re.compile(r"ev_[0-9a-f]{32}\Z")
_LIVE = ("active", "completed")
_SAFE_LABEL = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_METADATA_COLUMNS = """e.evidence_id, e.task_id, e.project_id, e.step_id, e.tool,
    e.result_type, e.created_at, e.expires_at, e.completed_at, e.status,
    e.raw_chars, e.raw_bytes, e.line_count, e.hash, e.compact_result_id,
    e.compact_chars, e.parser_type, e.compression_time_ms, e.retrieve_count,
    e.retrieved_chars, e.max_retrieve_chars"""


def _trace(kind: str, details: dict[str, Any]) -> None:
    try:
        record_event(kind, details)
    except Exception:
        # Auxiliary telemetry must not turn a successful tool into a failure.
        pass


def _schema(db: Any) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS result_evidence (
        evidence_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id), project_id TEXT NOT NULL,
        step_id TEXT, tool TEXT NOT NULL, result_type TEXT NOT NULL,
        created_at REAL NOT NULL, expires_at REAL NOT NULL, completed_at REAL,
        status TEXT NOT NULL, raw_chars INTEGER NOT NULL, raw_bytes INTEGER NOT NULL,
        line_count INTEGER NOT NULL, hash TEXT NOT NULL, content TEXT,
        compact_result TEXT, compact_result_id TEXT NOT NULL, compact_chars INTEGER NOT NULL,
        parser_type TEXT NOT NULL, compression_time_ms REAL NOT NULL,
        retrieve_count INTEGER NOT NULL DEFAULT 0, retrieved_chars INTEGER NOT NULL DEFAULT 0,
        max_retrieve_chars INTEGER NOT NULL, retention_days REAL NOT NULL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS result_evidence_scope ON result_evidence(task_id, project_id, created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS result_evidence_expiry ON result_evidence(status, expires_at)")
    db.execute("""CREATE TABLE IF NOT EXISTS result_compression_metrics (
        result_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
        project_id TEXT NOT NULL, evidence_id TEXT, created_at REAL NOT NULL,
        compression TEXT NOT NULL, reason TEXT, result_type TEXT NOT NULL,
        parser_type TEXT NOT NULL, raw_chars INTEGER NOT NULL, compact_chars INTEGER NOT NULL,
        compression_time_ms REAL NOT NULL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS result_metrics_scope ON result_compression_metrics(task_id, project_id, created_at)")


def _expire(db: Any, now: float, task_id: str | None = None, project_id: str | None = None) -> int:
    query = "UPDATE result_evidence SET status = 'expired', content = NULL, compact_result = NULL WHERE status IN ('active', 'completed') AND expires_at <= ?"
    params: list[Any] = [now]
    if task_id is not None:
        query += " AND task_id = ? AND project_id = ?"
        params += [task_id, project_id]
    return db.execute(query, params).rowcount


def _insert_metric(db: Any, task: dict[str, Any], result: dict[str, Any], result_id: str) -> None:
    db.execute("""INSERT INTO result_compression_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
        result_id, task["task_id"], task["project_id"], result.get("evidence_id"), time.time(),
        result["compression"], result.get("reason"), result["result_type"], result["parser_type"],
        result["raw_chars"], result["compact_chars"], result["compression_time_ms"],
    ))
    # Bypass/failed calls have no raw body, but their telemetry must also be bounded.
    db.execute("""DELETE FROM result_compression_metrics WHERE task_id = ? AND project_id = ?
        AND result_id NOT IN (SELECT result_id FROM result_compression_metrics
        WHERE task_id = ? AND project_id = ? ORDER BY created_at DESC LIMIT 1000)""",
        (task["task_id"], task["project_id"], task["task_id"], task["project_id"]))
    db.execute("""DELETE FROM result_compression_metrics WHERE result_id NOT IN
        (SELECT result_id FROM result_compression_metrics ORDER BY created_at DESC LIMIT 10000)""")


def record_result_metrics(task_id: str, result: dict[str, Any]) -> None:
    """Best-effort metadata accounting; callers pass only controlled labels/counters."""
    try:
        task = get_task(task_id)
        with _db() as db:
            _schema(db)
            _insert_metric(db, task, result, "res_" + token_hex(16))
    except Exception:
        pass


def save_evidence(task_id: str, content: str, result: dict[str, Any], config: dict[str, Any], *,
                  step_id: str | None = None, tool: str = "unknown") -> dict[str, Any]:
    """Internal optimizer write. The optimizer screens content before this call."""
    task = get_task(task_id)
    if task["status"] != "active":
        raise EvidenceError("task_not_active")
    if not isinstance(content, str):
        raise EvidenceError("invalid_content")
    # Defense in depth: an internal caller cannot bypass credential screening.
    from result_optimizer import screen_content
    if (screen_content(content) is not None
            or screen_content(str(result.get("compact_result", ""))) is not None):
        raise EvidenceError("credential_like_content")
    if (not isinstance(tool, str) or _SAFE_LABEL.fullmatch(tool) is None
            or (step_id is not None and (not isinstance(step_id, str) or _SAFE_LABEL.fullmatch(step_id) is None))
            or result.get("result_type") not in {"test_output", "build_output", "lint_output", "git_diff",
                                                 "search_result", "terminal_output", "mcp_result", "generic_text"}):
        raise EvidenceError("invalid_evidence_metadata")
    if not isinstance(result.get("compact_result"), str):
        raise EvidenceError("invalid_compact_result")
    raw = content.encode("utf-8")
    if len(content) > config["max_result_chars"] or len(raw) > config["max_result_chars"]:
        raise EvidenceError("result_size_limit")
    now = time.time()
    evidence_id = result["evidence_id"]
    if not isinstance(evidence_id, str) or _HANDLE.fullmatch(evidence_id) is None:
        raise EvidenceError("invalid_evidence_id")
    result_id = "res_" + token_hex(16)
    with _db() as db:
        _schema(db)
        db.execute("BEGIN IMMEDIATE")
        # Expired raw bodies do not occupy the global live-evidence quota.
        _expire(db, now)
        owner = db.execute("SELECT status FROM tasks WHERE id = ? AND project_id = ?",
                           (task_id, task["project_id"])).fetchone()
        if owner is None or owner["status"] != "active":
            raise EvidenceError("task_not_active")
        local = db.execute("""SELECT COUNT(*), COALESCE(SUM(raw_chars), 0), COALESCE(SUM(raw_bytes), 0) FROM result_evidence
            WHERE task_id = ? AND project_id = ? AND status IN ('active', 'completed')""",
            (task_id, task["project_id"])).fetchone()
        global_usage = db.execute("SELECT COUNT(*), COALESCE(SUM(raw_chars), 0), COALESCE(SUM(raw_bytes), 0) FROM result_evidence WHERE status IN ('active', 'completed')").fetchone()
        if (local[0] >= config["max_task_evidence"] or local[1] + len(content) > config["max_task_chars"]
                or local[2] + len(raw) > config["max_task_chars"]):
            raise EvidenceError("task_storage_limit")
        if (global_usage[0] >= config["max_global_evidence"] or global_usage[1] + len(content) > config["max_global_chars"]
                or global_usage[2] + len(raw) > config["max_global_chars"]):
            raise EvidenceError("global_storage_limit")
        db.execute("""INSERT INTO result_evidence (
            evidence_id, task_id, project_id, step_id, tool, result_type, created_at,
            expires_at, completed_at, status, raw_chars, raw_bytes, line_count, hash,
            content, compact_result, compact_result_id, compact_chars, parser_type,
            compression_time_ms, max_retrieve_chars, retention_days
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            evidence_id, task_id, task["project_id"], step_id, tool, result["result_type"],
            now, now + config["retention_days"] * 86400, len(content), len(raw),
            len(content.splitlines(keepends=True)), sha256(raw).hexdigest(), content,
            result["compact_result"], result_id, result["compact_chars"], result["parser_type"],
            result["compression_time_ms"], config["max_retrieve_chars"], config["retention_days"],
        ))
        _insert_metric(db, task, result, result_id)
    _trace("evidence_created", {"task_id": task_id, "project_id": task["project_id"],
                               "evidence_id": evidence_id, "raw_chars": len(content)})
    return {"evidence_id": evidence_id, "compact_result_id": result_id, "hash": sha256(raw).hexdigest()}


def _metadata(row: Any) -> dict[str, Any]:
    raw_chars = row["raw_chars"]
    compact_chars = row["compact_chars"]
    retrieved_chars = row["retrieved_chars"]
    gross_saved = max(0, raw_chars - compact_chars)
    net_saved = max(0, raw_chars - compact_chars - retrieved_chars)
    return {
        "evidence_id": row["evidence_id"], "task_id": row["task_id"], "project_id": row["project_id"],
        "step_id": row["step_id"], "type": row["result_type"], "result_type": row["result_type"],
        "tool": row["tool"], "status": row["status"], "size": raw_chars, "raw_chars": raw_chars,
        "raw_bytes": row["raw_bytes"], "line_count": row["line_count"], "created_at": row["created_at"],
        "expires_at": row["expires_at"], "hash": row["hash"], "compact_chars": compact_chars,
        "compact_result_id": row["compact_result_id"], "parser_type": row["parser_type"],
        "compression_ratio": compact_chars / raw_chars if raw_chars else 1.0,
        "compression_time_ms": row["compression_time_ms"], "retrieve_count": row["retrieve_count"],
        "retrieved_chars": retrieved_chars, "max_retrieve_chars": row["max_retrieve_chars"],
        "gross_saved_chars": gross_saved,
        "net_saved_chars": net_saved,
        "bounce_rate": round(retrieved_chars / raw_chars, 4) if raw_chars else 0.0,
    }


def _error(status: str, evidence_id: str | None = None, **details: Any) -> dict[str, Any]:
    return {"status": status, **({"evidence_id": evidence_id} if evidence_id else {}), **details}


def retrieve_evidence(task_id: str, evidence_id: str, action: str, *, query: str | None = None,
                      start_line: int | None = None, end_line: int | None = None,
                      max_chars: int | None = None) -> dict[str, Any]:
    """Progressive retrieval. Full reads exceeding the cap are refused, never hidden."""
    try:
        task = get_task(task_id)
    except Exception:
        return _error("task_not_found")
    if not isinstance(evidence_id, str) or _HANDLE.fullmatch(evidence_id) is None:
        return _error("invalid_evidence_id")
    if action not in {"metadata", "search", "range", "get"}:
        return _error("invalid_action", evidence_id)
    if max_chars is not None and (type(max_chars) is not int or max_chars <= 0):
        return _error("invalid_retrieval_limit", evidence_id)
    try:
        with _db() as db:
            _schema(db)
            _expire(db, time.time(), task_id, task["project_id"])
            columns = _METADATA_COLUMNS + (", e.content" if action != "metadata" else "")
            row = db.execute(f"""SELECT {columns} FROM result_evidence e JOIN tasks t
                ON t.id = e.task_id AND t.project_id = e.project_id
                WHERE e.evidence_id = ? AND e.task_id = ? AND e.project_id = ?""",
                (evidence_id, task_id, task["project_id"])).fetchone()
            if row is None:
                return _error("evidence_not_found", evidence_id)
            if row["status"] == "expired":
                return _error("evidence_expired", evidence_id)
            if row["status"] == "deleted":
                return _error("evidence_deleted", evidence_id)
            limit = min(row["max_retrieve_chars"], 20000, max_chars if max_chars is not None else 20000)
            base: dict[str, Any] = {"status": "ok", "action": action, "evidence_id": evidence_id,
                                    "max_chars": limit}
            if action == "metadata":
                base["metadata"] = _metadata(row)
                base["hash_verified"] = False
            else:
                content = row["content"]
                if not isinstance(content, str) or sha256(content.encode("utf-8")).hexdigest() != row["hash"]:
                    return _error("evidence_hash_mismatch", evidence_id)
                from result_optimizer import screen_content
                if screen_content(content) is not None:
                    return _error("evidence_unsafe", evidence_id)
                base["hash_verified"] = True
            if action == "metadata":
                pass
            elif action == "get":
                if len(content) > limit:
                    return _error("evidence_too_large", evidence_id, required_chars=len(content),
                                  max_chars=limit, recovery="Use search, then range; full get exceeds the retrieval cap.")
                base.update(content=content, hash=row["hash"], truncated=False)
            elif action == "range":
                if type(start_line) is not int or start_line < 1 or type(end_line) is not int or end_line < start_line:
                    return _error("invalid_line_range", evidence_id)
                lines = content.splitlines(keepends=True)
                if start_line > len(lines):
                    return _error("invalid_line_range", evidence_id, line_count=len(lines))
                actual_end = min(end_line, len(lines))
                selected = "".join(lines[start_line - 1:actual_end])
                base.update(content=selected[:limit], start_line=start_line, end_line=actual_end,
                            line_count=len(lines), truncated=len(selected) > limit)
            else:
                if not isinstance(query, str) or not query or len(query) > 512:
                    return _error("invalid_search_query", evidence_id)
                lines = content.splitlines(keepends=True)
                folded = query.casefold()
                hits = [i for i, line in enumerate(lines) if folded in line.casefold()]
                selected: set[int] = set()
                match_refs = []
                for index in hits[:50]:
                    first, last = max(0, index - 2), min(len(lines), index + 3)
                    selected.update(range(first, last))
                    match_refs.append({"line": index + 1, "start_line": first + 1, "end_line": last})
                excerpt = "".join(f"L{i + 1}: {lines[i].rstrip(chr(13) + chr(10))}\n" for i in sorted(selected))
                base.update(content=excerpt[:limit], matches=match_refs, total_matches=len(hits),
                            truncated=len(hits) > 50 or len(excerpt) > limit)
            returned_chars = len(base.get("content", ""))
            if action != "metadata":
                db.execute("""UPDATE result_evidence SET retrieve_count = retrieve_count + 1,
                    retrieved_chars = retrieved_chars + ? WHERE evidence_id = ? AND task_id = ? AND project_id = ?""",
                    (returned_chars, evidence_id, task_id, task["project_id"]))
            base["returned_chars"] = returned_chars
        _trace("evidence_retrieved", {"task_id": task_id, "project_id": task["project_id"],
                                      "evidence_id": evidence_id, "action": action,
                                      "retrieved_chars": returned_chars})
        return base
    except Exception:
        return _error("evidence_store_unavailable", evidence_id)


def cleanup_expired(task_id: str) -> dict[str, Any]:
    try:
        task = get_task(task_id)
        with _db() as db:
            _schema(db)
            expired = _expire(db, time.time(), task_id, task["project_id"])
        return {"status": "ok", "expired": expired, "task_id": task_id}
    except Exception:
        return {"status": "evidence_store_unavailable", "expired": 0}


def complete_task_evidence(task_id: str) -> None:
    try:
        task = get_task(task_id)
        now = time.time()
        with _db() as db:
            _schema(db)
            _expire(db, now, task_id, task["project_id"])
            db.execute("""UPDATE result_evidence SET status = 'completed', completed_at = ?,
                expires_at = ? + retention_days * 86400 WHERE task_id = ? AND project_id = ? AND status = 'active'""",
                (now, now, task_id, task["project_id"]))
            _expire(db, now, task_id, task["project_id"])
    except Exception:
        pass


def evidence_refs(task_id: str) -> list[dict[str, Any]]:
    try:
        task = get_task(task_id)
        with _db() as db:
            _schema(db)
            _expire(db, time.time(), task_id, task["project_id"])
            rows = db.execute(f"""SELECT {_METADATA_COLUMNS} FROM result_evidence e JOIN tasks t
                ON t.id = e.task_id AND t.project_id = e.project_id
                WHERE e.task_id = ? AND e.project_id = ? AND e.status IN ('active', 'completed')
                ORDER BY e.created_at DESC LIMIT 1000""", (task_id, task["project_id"])).fetchall()
        return [_metadata(row) for row in rows]
    except Exception:
        return []


def evidence_metrics(task_id: str) -> dict[str, Any]:
    try:
        task = get_task(task_id)
        with _db() as db:
            _schema(db)
            _expire(db, time.time(), task_id, task["project_id"])
            counters = db.execute("""SELECT COUNT(*) AS result_count,
                COALESCE(SUM(raw_chars), 0) AS raw_chars, COALESCE(SUM(compact_chars), 0) AS compact_chars,
                COALESCE(SUM(compression_time_ms), 0) AS compression_time_ms,
                COALESCE(SUM(compression = 'compressed'), 0) AS compressed_count,
                COALESCE(SUM(compression = 'bypass'), 0) AS bypass_count,
                COALESCE(SUM(compression = 'failed'), 0) AS failed_count
                FROM result_compression_metrics WHERE task_id = ? AND project_id = ?""",
                (task_id, task["project_id"])).fetchone()
            retrieved = db.execute("""SELECT COALESCE(SUM(retrieve_count), 0), COALESCE(SUM(retrieved_chars), 0)
                FROM result_evidence WHERE task_id = ? AND project_id = ?""", (task_id, task["project_id"])).fetchone()
            next_expiry = db.execute("""SELECT MIN(expires_at) FROM result_evidence
                WHERE task_id = ? AND project_id = ? AND status IN ('active', 'completed')
                AND expires_at > ?""", (task_id, task["project_id"], time.time())).fetchone()[0]
            latest_row = db.execute("""SELECT evidence_id, result_type FROM result_evidence
                WHERE task_id = ? AND project_id = ? AND status IN ('active', 'completed')
                ORDER BY created_at DESC LIMIT 1""", (task_id, task["project_id"])).fetchone()
            rows = db.execute("""SELECT m.*, e.tool, e.status AS evidence_status, e.retrieve_count,
                e.retrieved_chars FROM result_compression_metrics m LEFT JOIN result_evidence e
                ON e.evidence_id = m.evidence_id AND e.task_id = m.task_id AND e.project_id = m.project_id
                WHERE m.task_id = ? AND m.project_id = ? ORDER BY m.created_at DESC LIMIT 100""",
                (task_id, task["project_id"])).fetchall()
        metrics = dict(counters)
        raw_total = metrics["raw_chars"]
        compact_total = metrics["compact_chars"]
        retrieved_total = retrieved[1]
        metrics.update(status="ok", task_id=task_id, project_id=task["project_id"],
                       retrieve_count=retrieved[0], retrieved_chars=retrieved_total,
                       gross_saved_chars=max(0, raw_total - compact_total),
                       net_saved_chars=max(0, raw_total - compact_total - retrieved_total),
                       bounce_rate=round(retrieved_total / raw_total, 4) if raw_total else 0.0,
                       next_expiry_at=next_expiry,
                       latest_evidence_id=latest_row["evidence_id"] if latest_row else None,
                       latest_result_type=latest_row["result_type"] if latest_row else None,
                       compression_ratio=compact_total / raw_total if raw_total else 1.0,
                       usage_kind="returned_text_chars_only; not observed model context usage")
        metrics["results"] = [{**dict(row), "compression_ratio": row["compact_chars"] / row["raw_chars"] if row["raw_chars"] else 1.0} for row in rows]
        return metrics
    except Exception:
        return {"status": "evidence_store_unavailable", "results": []}
