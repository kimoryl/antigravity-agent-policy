"""Persistent, bounded Experience assets built on the existing Runtime SQLite DB."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from policy_state import record_event
from runtime_state import GLOBAL_PROJECT_ID, RuntimeError, auto_desensitize_for_global
from gdrive_memory_engine import gdrive_memory_db as _db


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "experience" / "default.json"
SENSITIVE = re.compile(r"(-----BEGIN|api[_-]?key|access[_-]?token|password|secret|credential|private[_ -]?key|\.env)", re.I)
EXPERIENCE_STATUSES = {"candidate", "validated", "promotion_candidate", "global", "rejected"}
SKILL_STATUSES = {"experimental", "validated", "active", "deprecated", "rejected", "rolled_back"}


def load_config() -> dict[str, Any]:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise RuntimeError("Invalid Experience configuration")
    return config


def _safe_text(value: Any, field: str, limit: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Experience {field} must be non-empty text")
    if SENSITIVE.search(value):
        raise RuntimeError(f"Experience {field} contains sensitive content")
    return value.strip()[:limit]


def _safe_json(value: Any, field: str, limit: int = 12) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit or any(not isinstance(x, str) for x in value):
        raise RuntimeError(f"Experience {field} must be a short string list")
    result = []
    for item in value:
        result.append(_safe_text(item, field, 500))
    return result


def _ensure_schema(db: Any) -> None:
    db.executescript("""
      CREATE TABLE IF NOT EXISTS experiences (
        experience_id TEXT PRIMARY KEY, scope TEXT NOT NULL, project_id TEXT,
        title TEXT NOT NULL, summary TEXT NOT NULL, conditions TEXT NOT NULL,
        recommendations TEXT NOT NULL, anti_patterns TEXT NOT NULL,
        confidence REAL NOT NULL, status TEXT NOT NULL, support_count INTEGER NOT NULL DEFAULT 1,
        contradiction_count INTEGER NOT NULL DEFAULT 0, project_count INTEGER NOT NULL DEFAULT 1,
        source_refs TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
      );
      CREATE INDEX IF NOT EXISTS experiences_scope_project ON experiences(scope, project_id, status, updated_at DESC);
      CREATE TABLE IF NOT EXISTS experience_sources (
        source_id TEXT PRIMARY KEY, experience_id TEXT NOT NULL, source_type TEXT NOT NULL,
        source_ref TEXT NOT NULL, excerpt_ref TEXT, created_at REAL NOT NULL
      );
      CREATE TABLE IF NOT EXISTS experience_validations (
        validation_id TEXT PRIMARY KEY, experience_id TEXT NOT NULL, verdict TEXT NOT NULL,
        validation_type TEXT NOT NULL, reviewer TEXT NOT NULL, evidence_ref TEXT,
        task_id TEXT, created_at REAL NOT NULL
      );
      CREATE TABLE IF NOT EXISTS experience_promotions (
        promotion_id TEXT PRIMARY KEY, experience_id TEXT NOT NULL, from_scope TEXT NOT NULL,
        to_scope TEXT NOT NULL, decision TEXT NOT NULL, reviewer TEXT NOT NULL,
        reason TEXT, created_at REAL NOT NULL
      );
      CREATE TABLE IF NOT EXISTS skill_proposals (
        proposal_id TEXT PRIMARY KEY, experience_id TEXT NOT NULL, name TEXT NOT NULL,
        version TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL,
        path TEXT NOT NULL, validations TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
      );
      CREATE INDEX IF NOT EXISTS skill_proposals_experience ON skill_proposals(experience_id, status);
      CREATE TABLE IF NOT EXISTS policy_candidates (
        candidate_id TEXT PRIMARY KEY, experience_id TEXT NOT NULL, status TEXT NOT NULL,
        reason TEXT NOT NULL, reviewer TEXT NOT NULL, created_at REAL NOT NULL
      );
    """)
    cols = {row[1] for row in db.execute("PRAGMA table_info(experiences)").fetchall()}
    if "valid_from" not in cols:
        db.execute("ALTER TABLE experiences ADD COLUMN valid_from REAL")
    if "invalid_at" not in cols:
        db.execute("ALTER TABLE experiences ADD COLUMN invalid_at REAL")
    if "superseded_by" not in cols:
        db.execute("ALTER TABLE experiences ADD COLUMN superseded_by TEXT")
    if "hard_boundary" not in cols:
        db.execute("ALTER TABLE experiences ADD COLUMN hard_boundary INTEGER NOT NULL DEFAULT 0")


def _decode(row: Any) -> dict[str, Any]:
    if row is None:
        raise RuntimeError("Unknown experience")
    item = dict(row)
    for key in ("conditions", "recommendations", "anti_patterns", "source_refs"):
        item[key] = json.loads(item[key])
    if item.get("valid_from") is None:
        item["valid_from"] = item.get("created_at")
    item["hard_boundary"] = bool(item.get("hard_boundary", 0))
    return item


def get_experience(experience_id: str) -> dict[str, Any]:
    with _db() as db:
        _ensure_schema(db)
        return _decode(db.execute("SELECT * FROM experiences WHERE experience_id = ?", (experience_id,)).fetchone())


def create_policy_candidate(experience_id: str, reason: str, reviewer: str = "codex") -> dict[str, Any]:
    get_experience(experience_id)
    candidate = {"candidate_id": "policy_" + uuid4().hex, "experience_id": experience_id,
                 "status": "candidate_only", "reason": _safe_text(reason or "", "reason", 500) if reason else "",
                 "reviewer": _safe_text(reviewer, "reviewer", 120), "created_at": time.time()}
    with _db() as db:
        _ensure_schema(db)
        db.execute("INSERT INTO policy_candidates VALUES (?, ?, ?, ?, ?, ?)", tuple(candidate.values()))
    return candidate


def _tokenize_exp_query(text: str) -> set[str]:
    lowered = (text or "").casefold()
    tokens = set(re.findall(r"[a-z0-9_.-]{2,}", lowered))
    for chunk in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(chunk) == 1:
            tokens.add(chunk)
        else:
            for idx in range(len(chunk) - 1):
                tokens.add(chunk[idx:idx + 2])
    return tokens


def search_experiences(project_id: str | None = None, query: str = "", scope: str = "project+global", limit: int = 3, *, include_invalidated: bool = False) -> list[dict[str, Any]]:
    from gdrive_memory_engine import build_fts_match_query
    config = load_config()["retrieval"]
    limit = max(1, min(int(limit), int(config["max_limit"])))
    if scope not in {"project", "global", "project+global"}:
        raise RuntimeError("Invalid Experience scope")
    q_tokens = _tokenize_exp_query(query)
    fts_expr = build_fts_match_query(query) if query and query.strip() else ""
    with _db() as db:
        _ensure_schema(db)
        clauses: list[str] = []
        params: list[Any] = []
        if scope == "project":
            clauses.append("e.scope = 'project' AND e.project_id = ?")
            params.append(project_id)
        elif scope == "global":
            clauses.append("e.scope = 'global'")
        else:
            clauses.append("((e.scope = 'project' AND e.project_id = ?) OR e.scope = 'global')")
            params.append(project_id)
        clauses.append("e.status IN ('validated', 'global', 'promotion_candidate')")
        if not include_invalidated:
            clauses.append("e.invalid_at IS NULL")
        if fts_expr:
            rows = db.execute(
                "SELECT e.*, fts.fts_rank FROM experiences e "
                "LEFT JOIN (SELECT experience_id, bm25(experiences_fts) AS fts_rank FROM experiences_fts WHERE experiences_fts MATCH ?) fts "
                "ON e.experience_id = fts.experience_id "
                "WHERE " + " AND ".join(clauses) + " AND (e.hard_boundary = 1 OR fts.fts_rank IS NOT NULL) "
                "ORDER BY CASE WHEN fts.fts_rank IS NOT NULL THEN 0 ELSE 1 END ASC, e.hard_boundary DESC, fts.fts_rank ASC, e.confidence DESC, e.support_count DESC, e.updated_at DESC",
                [fts_expr, *params],
            ).fetchall()
        else:
            rows = db.execute("SELECT e.* FROM experiences e WHERE " + " AND ".join(clauses) + " ORDER BY e.hard_boundary DESC, e.confidence DESC, e.support_count DESC, e.updated_at DESC", params).fetchall()
    decoded = [_decode(row) for row in rows]
    if q_tokens:
        decoded.sort(
            key=lambda item: (
                1 if item.get("hard_boundary") and (q_tokens & _tokenize_exp_query(item["title"] + " " + item["summary"])) else 0,
                len(q_tokens & _tokenize_exp_query(item["title"] + " " + item["summary"])),
                1 if item.get("hard_boundary") else 0,
                float(item.get("confidence", 0.0)),
            ),
            reverse=True,
        )
    return decoded[:limit]


def propose_experience(*, project_id: str | None = None, title: str, summary: str, conditions: list[str] | None = None,
                       recommendations: list[str] | None = None, anti_patterns: list[str] | None = None,
                       confidence: float = 0.5, source_refs: list[str] | None = None,
                       scope: str = "project", support_count: int = 1, project_count: int = 1,
                       supersede_id: str | None = None, hard_boundary: bool = False) -> dict[str, Any]:
    if scope not in {"project", "global"}:
        raise RuntimeError("Experience proposals require a valid scope ('project' or 'global')")
    target_project_id = GLOBAL_PROJECT_ID if scope == "global" else (project_id or GLOBAL_PROJECT_ID)
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise RuntimeError("Experience confidence must be between 0 and 1")
    if scope == "global":
        title = auto_desensitize_for_global(title)
        summary = auto_desensitize_for_global(summary)
        conditions = [auto_desensitize_for_global(c) for c in (conditions or [])]
        recommendations = [auto_desensitize_for_global(r) for r in (recommendations or [])]
        anti_patterns = [auto_desensitize_for_global(a) for a in (anti_patterns or [])]
    title = _safe_text(title, "title", 200)
    summary = _safe_text(summary, "summary")
    conditions = _safe_json(conditions, "conditions")
    recommendations = _safe_json(recommendations, "recommendations")
    anti_patterns = _safe_json(anti_patterns, "anti_patterns")
    source_refs = _safe_json(source_refs, "source_refs")
    hb_int = 1 if hard_boundary else 0
    initial_status = "global" if scope == "global" else "candidate"
    now = time.time()
    with _db() as db:
        _ensure_schema(db)
        duplicate = None if supersede_id else db.execute(
            "SELECT * FROM experiences WHERE (project_id = ? OR (scope = 'global' AND ? = 'global')) AND scope = ? AND title = ? AND invalid_at IS NULL",
            (target_project_id, scope, scope, title),
        ).fetchone()
        if duplicate:
            db.execute(
                "UPDATE experiences SET support_count = support_count + ?, confidence = MAX(confidence, ?), hard_boundary = MAX(hard_boundary, ?), updated_at = ? WHERE experience_id = ?",
                (max(1, support_count), confidence, hb_int, now, duplicate["experience_id"]),
            )
            experience_id = duplicate["experience_id"]
        else:
            experience_id = "exp_" + uuid4().hex
            db.execute(
                """INSERT INTO experiences (
                    experience_id, scope, project_id, title, summary, conditions,
                    recommendations, anti_patterns, confidence, status, support_count,
                    contradiction_count, project_count, source_refs, created_at, updated_at,
                    valid_from, invalid_at, superseded_by, hard_boundary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, NULL, NULL, ?)""",
                (
                    experience_id, scope, target_project_id, title, summary, json.dumps(conditions, ensure_ascii=False),
                    json.dumps(recommendations, ensure_ascii=False), json.dumps(anti_patterns, ensure_ascii=False), float(confidence),
                    initial_status, max(1, support_count), max(1, project_count), json.dumps(source_refs, ensure_ascii=False),
                    now, now, now, hb_int,
                ),
            )
            for ref in source_refs:
                db.execute("INSERT INTO experience_sources VALUES (?, ?, 'reference', ?, NULL, ?)", ("src_" + uuid4().hex, experience_id, ref, now))
            if supersede_id:
                db.execute(
                    "UPDATE experiences SET invalid_at = ?, superseded_by = ?, updated_at = ? WHERE experience_id = ? AND invalid_at IS NULL",
                    (now, experience_id, now, supersede_id),
                )
    record_event("experience_proposed", {"experience_id": experience_id, "project_id": target_project_id, "scope": scope, "superseded": supersede_id})
    return get_experience(experience_id)


def validate_experience(experience_id: str, verdict: str, reviewer: str = "codex", validation_type: str = "reviewer", evidence_ref: str | None = None, task_id: str | None = None) -> dict[str, Any]:
    if verdict not in {"pass", "fail"}:
        raise RuntimeError("Experience validation verdict must be pass or fail")
    reviewer = _safe_text(reviewer, "reviewer", 120)
    if evidence_ref and SENSITIVE.search(evidence_ref):
        raise RuntimeError("Sensitive evidence reference")
    now = time.time()
    with _db() as db:
        _ensure_schema(db)
        item = db.execute("SELECT * FROM experiences WHERE experience_id = ?", (experience_id,)).fetchone()
        if item is None:
            raise RuntimeError("Unknown experience")
        db.execute("INSERT INTO experience_validations VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ("val_" + uuid4().hex, experience_id, verdict, validation_type[:80], reviewer, evidence_ref, task_id, now))
        if verdict == "pass":
            db.execute("UPDATE experiences SET status = CASE WHEN status = 'candidate' THEN 'validated' ELSE status END, support_count = support_count + 1, updated_at = ? WHERE experience_id = ?", (now, experience_id))
        else:
            db.execute("UPDATE experiences SET contradiction_count = contradiction_count + 1, updated_at = ? WHERE experience_id = ?", (now, experience_id))
    record_event("experience_validated", {"experience_id": experience_id, "verdict": verdict})
    return get_experience(experience_id)


def promote_experience(experience_id: str, reviewer: str = "codex", approve: bool = True, reason: str = "auto_desensitized_promotion") -> dict[str, Any]:
    reviewer = _safe_text(reviewer, "reviewer", 120)
    item = get_experience(experience_id)
    now = time.time()
    if not approve:
        with _db() as db:
            _ensure_schema(db)
            db.execute("UPDATE experiences SET status = 'promotion_candidate', updated_at = ? WHERE experience_id = ?", (now, experience_id))
            db.execute("INSERT INTO experience_promotions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ("prom_" + uuid4().hex, experience_id, item["scope"], item["scope"], "gate_pending", reviewer, reason[:500], now))
        return get_experience(experience_id)
    clean_title = auto_desensitize_for_global(item["title"])
    clean_summary = auto_desensitize_for_global(item["summary"])
    clean_conds = [auto_desensitize_for_global(c) for c in item.get("conditions", [])]
    clean_recs = [auto_desensitize_for_global(r) for r in item.get("recommendations", [])]
    clean_antis = [auto_desensitize_for_global(a) for a in item.get("anti_patterns", [])]
    with _db() as db:
        _ensure_schema(db)
        db.execute(
            "UPDATE experiences SET scope = 'global', status = 'global', title = ?, summary = ?, conditions = ?, recommendations = ?, anti_patterns = ?, updated_at = ? WHERE experience_id = ?",
            (
                clean_title, clean_summary,
                json.dumps(clean_conds, ensure_ascii=False),
                json.dumps(clean_recs, ensure_ascii=False),
                json.dumps(clean_antis, ensure_ascii=False),
                now, experience_id,
            ),
        )
        db.execute("INSERT INTO experience_promotions VALUES (?, ?, 'project', 'global', 'approved', ?, ?, ?)", ("prom_" + uuid4().hex, experience_id, reviewer, reason[:500], now))
    record_event("experience_promoted", {"experience_id": experience_id, "reviewer": reviewer})
    return get_experience(experience_id)


def auto_promote_eligible_experiences(reviewer: str = "agy-auto-distiller") -> dict[str, Any]:
    """Automatically desensitize and promote eligible project experiences to global scope without manual intervention."""
    promoted_ids: list[str] = []
    with _db() as db:
        _ensure_schema(db)
        rows = db.execute(
            """SELECT experience_id FROM experiences
               WHERE scope = 'project' AND invalid_at IS NULL
                 AND contradiction_count = 0
                 AND (support_count >= 2 OR project_count >= 2 OR confidence >= 0.75 OR status IN ('validated', 'promotion_candidate'))"""
        ).fetchall()
    for row in rows:
        exp_id = row["experience_id"]
        promote_experience(exp_id, reviewer=reviewer, approve=True, reason="auto_desensitize_and_promote")
        promoted_ids.append(exp_id)
    return {"promoted_count": len(promoted_ids), "promoted_ids": promoted_ids}


def reject_experience(experience_id: str, reviewer: str = "codex", reason: str = "") -> dict[str, Any]:
    reviewer = _safe_text(reviewer, "reviewer", 120)
    with _db() as db:
        _ensure_schema(db)
        db.execute("UPDATE experiences SET status = 'rejected', updated_at = ? WHERE experience_id = ?", (time.time(), experience_id))
        db.execute("INSERT INTO experience_promotions VALUES (?, ?, 'project', 'project', 'rejected', ?, ?, ?)", ("prom_" + uuid4().hex, experience_id, reviewer, reason[:500], time.time()))
    return get_experience(experience_id)


def create_skill_proposal(experience_id: str, name: str, body: str, version: str = "0.1.0") -> dict[str, Any]:
    item = get_experience(experience_id)
    name = _safe_text(name, "skill name", 80).lower().replace(" ", "-")
    body = _safe_text(body, "skill body", 12000)
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,79}", name):
        raise RuntimeError("Invalid candidate Skill name")
    path = f"skill_pool/experimental/{name}/SKILL.md"
    now = time.time()
    with _db() as db:
        _ensure_schema(db)
        proposal_id = "skill_" + uuid4().hex
        db.execute("INSERT INTO skill_proposals VALUES (?, ?, ?, ?, ?, 'experimental', ?, '[]', ?, ?)", (proposal_id, item["experience_id"], name, version, body, path, now, now))
    record_event("skill_proposed", {"proposal_id": proposal_id, "experience_id": experience_id})
    return get_skill_proposal(proposal_id)


def get_skill_proposal(proposal_id: str) -> dict[str, Any]:
    with _db() as db:
        _ensure_schema(db)
        row = db.execute("SELECT * FROM skill_proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
    if row is None:
        raise RuntimeError("Unknown Skill proposal")
    item = dict(row)
    item["validations"] = json.loads(item["validations"])
    return item


def validate_skill_proposal(proposal_id: str, validation_type: str, verdict: str, reviewer: str = "codex") -> dict[str, Any]:
    if verdict not in {"pass", "fail"} or not validation_type:
        raise RuntimeError("Invalid Skill validation")
    item = get_skill_proposal(proposal_id)
    validations = list(item["validations"])
    validations.append({"type": validation_type, "verdict": verdict, "reviewer": _safe_text(reviewer, "reviewer", 120), "at": time.time()})
    required = {"original", "regression", "cross_task", "reviewer"}
    passed = {v["type"] for v in validations if v["verdict"] == "pass"}
    status = "validated" if required.issubset(passed) else ("rejected" if verdict == "fail" else item["status"])
    with _db() as db:
        _ensure_schema(db)
        db.execute("UPDATE skill_proposals SET validations = ?, status = ?, updated_at = ? WHERE proposal_id = ?", (json.dumps(validations), status, time.time(), proposal_id))
    return get_skill_proposal(proposal_id)


def set_skill_status(proposal_id: str, status: str, reviewer: str = "codex") -> dict[str, Any]:
    if status not in {"active", "deprecated", "rejected", "rolled_back"}:
        raise RuntimeError("Invalid Skill proposal status")
    item = get_skill_proposal(proposal_id)
    if status == "active" and item["status"] != "validated":
        raise RuntimeError("Candidate Skill requires all validations before activation")
    with _db() as db:
        _ensure_schema(db)
        db.execute("UPDATE skill_proposals SET status = ?, updated_at = ? WHERE proposal_id = ?", (status, time.time(), proposal_id))
    record_event("skill_status_changed", {"proposal_id": proposal_id, "status": status})
    return get_skill_proposal(proposal_id)


def list_experience_metadata(project_id: str | None = None, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    with _db() as db:
        _ensure_schema(db)
        clauses, params = [], []
        if project_id:
            clauses.append("(project_id = ? OR scope = 'global')")
            params.append(project_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = db.execute("SELECT experience_id, scope, project_id, title, confidence, status, support_count, contradiction_count, project_count, updated_at FROM experiences" + where + " ORDER BY updated_at DESC LIMIT ?", [*params, max(1, min(limit, 100))]).fetchall()
    return [dict(row) for row in rows]
