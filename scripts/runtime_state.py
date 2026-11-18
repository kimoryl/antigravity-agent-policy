"""Project-isolated task state, checkpoints, facts, and historical memory."""

from __future__ import annotations

import builtins as _builtins
import json
import os
import sqlite3
import subprocess
import sys as _sys
import time
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from policy_state import data_dir, record_event


_prev_rs = _sys.modules.get(__name__)
if _prev_rs is not None and hasattr(_prev_rs, "RuntimeError") and issubclass(_prev_rs.RuntimeError, _builtins.RuntimeError):
    RuntimeError = _prev_rs.RuntimeError
else:
    class RuntimeError(_builtins.RuntimeError, ValueError):
        pass


MAX_WORKSPACE_FILES = 64
MAX_WORKSPACE_FILE_BYTES = 2_000_000
MAX_WORKSPACE_SCAN_SECONDS = 0.75
WORKSPACE_SNAPSHOT_VERSION = 2
PRIVATE_WORKSPACE_NAMES = {".env", ".env.local", "credentials.json", "secrets.json", "id_rsa", "id_ed25519", "config.toml", "AGENTS.md"}


import re


def canonical_project_root(path: Path) -> Path:
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        dot_git = candidate / ".git"
        try:
            if dot_git.is_file():
                raw = dot_git.read_text(encoding="utf-8", errors="ignore").strip()
                if raw.lower().startswith("gitdir:"):
                    gitdir_raw = raw.split(":", 1)[1].strip()
                    gitdir = (candidate / gitdir_raw).resolve() if not Path(gitdir_raw).is_absolute() else Path(gitdir_raw).resolve()
                    for parent in (gitdir, *gitdir.parents):
                        if parent.name.casefold() == ".git":
                            return parent.parent.resolve()
                        if parent.parent.name.casefold() == "worktrees" and parent.parent.parent.name.casefold() == ".git":
                            return parent.parent.parent.parent.resolve()
                    return gitdir
            elif dot_git.is_dir():
                return candidate
        except OSError:
            pass
    for candidate in (resolved, *resolved.parents):
        if re.match(r"^task-[0-9a-fA-F-]+$", candidate.name) and candidate.parent != candidate:
            return candidate.parent.resolve()
    return resolved


def project_identity(root: str) -> tuple[str, str]:
    path = Path(root).resolve()
    if not path.is_dir():
        raise RuntimeError("project_root must be an existing directory")
    try:
        from gdrive_memory_engine import is_memory_sandbox_active, resolve_project_id_via_registry
        sandboxed = is_memory_sandbox_active()
        mapped_pid = resolve_project_id_via_registry(path)
        if mapped_pid and (not sandboxed or mapped_pid != "workbench_misc"):
            return mapped_pid, str(path)
    except Exception:
        pass
    canonical = canonical_project_root(path)
    return sha256(str(canonical).casefold().encode("utf-8")).hexdigest()[:20], str(path)


@contextmanager
def _db():
    connection = sqlite3.connect(data_dir() / "runtime.sqlite3", timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, project_root TEXT NOT NULL,
          session_id TEXT, revision INTEGER NOT NULL, status TEXT NOT NULL,
          state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS tasks_session ON tasks(session_id, updated_at);
        CREATE TABLE IF NOT EXISTS checkpoints (
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
          revision INTEGER NOT NULL, reason TEXT NOT NULL, state TEXT NOT NULL,
          created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS facts (
          project_id TEXT NOT NULL, fact_key TEXT NOT NULL, value TEXT NOT NULL,
          source_path TEXT NOT NULL, source_sha256 TEXT NOT NULL, evidence TEXT NOT NULL,
          source_revision TEXT, updated_at REAL NOT NULL,
          PRIMARY KEY(project_id, fact_key)
        );
        CREATE TABLE IF NOT EXISTS memories (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL,
          summary TEXT NOT NULL, source_task_id TEXT NOT NULL,
          created_at REAL NOT NULL,
          scope TEXT NOT NULL DEFAULT 'project',
          hard_boundary INTEGER NOT NULL DEFAULT 0,
          valid_from REAL,
          invalid_at REAL,
          superseded_by TEXT,
          source_doc TEXT
        );
        CREATE TABLE IF NOT EXISTS context_snapshots (
          snapshot_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, project_root TEXT NOT NULL,
          project_revision TEXT, sources TEXT NOT NULL, facts TEXT NOT NULL,
          memory_refs TEXT NOT NULL, architecture_refs TEXT NOT NULL, skill_refs TEXT NOT NULL,
          content_hash TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS context_snapshots_project ON context_snapshots(project_id, updated_at);
    """)
    mem_cols = {row[1] for row in connection.execute("PRAGMA table_info(memories)").fetchall()}
    for col_name, col_def in (
        ("scope", "TEXT NOT NULL DEFAULT 'project'"),
        ("hard_boundary", "INTEGER NOT NULL DEFAULT 0"),
        ("valid_from", "REAL"),
        ("invalid_at", "REAL"),
        ("superseded_by", "TEXT"),
        ("source_doc", "TEXT"),
    ):
        if col_name not in mem_cols:
            connection.execute(f"ALTER TABLE memories ADD COLUMN {col_name} {col_def}")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _row(row: sqlite3.Row) -> dict[str, Any]:
    state = json.loads(row["state"])
    state.setdefault("binding_status", "bound" if row["session_id"] else "unbound")
    state.setdefault("binding_source", "legacy_session_id" if row["session_id"] else "session_id_missing")
    state.setdefault("binding_error", None)
    state.setdefault("operations", {})
    raw_pid = str(row["project_id"])
    try:
        from gdrive_memory_engine import normalize_project_id
        raw_pid = normalize_project_id(raw_pid)
    except Exception:
        pass
    return {
        "task_id": row["id"], "project_id": raw_pid,
        "project_root": row["project_root"], "session_id": row["session_id"],
        "revision": row["revision"], "status": row["status"],
        "state": state, "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "snapshot_id": row["snapshot_id"], "project_id": row["project_id"],
        "project_root": row["project_root"], "project_revision": row["project_revision"],
        "sources": json.loads(row["sources"]), "facts": json.loads(row["facts"]),
        "memory_refs": json.loads(row["memory_refs"]), "architecture_refs": json.loads(row["architecture_refs"]),
        "skill_refs": json.loads(row["skill_refs"]), "content_hash": row["content_hash"],
        "status": row["status"], "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def create_context_snapshot(project_id: str, project_root: str, project_revision: str | None,
                            sources: list[dict[str, Any]], facts: list[Any] | None = None,
                            memory_refs: list[Any] | None = None, architecture_refs: list[Any] | None = None,
                            skill_refs: list[Any] | None = None, content_hash: str | None = None) -> dict[str, Any]:
    if not isinstance(project_id, str) or not isinstance(project_root, str) or not Path(project_root).is_dir():
        raise RuntimeError("Invalid project snapshot identity")
    if not isinstance(sources, list) or any(not isinstance(item, dict) for item in sources):
        raise RuntimeError("sources must be a list of objects")
    lists = [facts, memory_refs, architecture_refs, skill_refs]
    if any(value is not None and not isinstance(value, list) for value in lists):
        raise RuntimeError("snapshot references must be lists")
    payload = {"sources": sources, "facts": facts or [], "memory_refs": memory_refs or [],
               "architecture_refs": architecture_refs or [], "skill_refs": skill_refs or []}
    digest = content_hash or sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    now = time.time(); snapshot_id = uuid4().hex
    with _db() as db:
        db.execute("INSERT INTO context_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                   (snapshot_id, project_id, str(Path(project_root).resolve()), project_revision,
                    json.dumps(sources, ensure_ascii=False), json.dumps(payload["facts"], ensure_ascii=False),
                    json.dumps(payload["memory_refs"], ensure_ascii=False), json.dumps(payload["architecture_refs"], ensure_ascii=False),
                    json.dumps(payload["skill_refs"], ensure_ascii=False), digest, now, now))
    record_event("context_snapshot_created", {"snapshot_id": snapshot_id, "project_id": project_id,
                                               "source_count": len(sources), "content_hash": digest})
    return get_context_snapshot(snapshot_id)


def get_context_snapshot(snapshot_id: str) -> dict[str, Any]:
    with _db() as db:
        row = db.execute("SELECT * FROM context_snapshots WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
    if row is None:
        raise RuntimeError("Unknown context_snapshot_id")
    return _snapshot_row(row)


def invalidate_context_snapshot(snapshot_id: str, reason: str = "manual") -> dict[str, Any]:
    snapshot = get_context_snapshot(snapshot_id)
    with _db() as db:
        db.execute("UPDATE context_snapshots SET status = ?, updated_at = ? WHERE snapshot_id = ?",
                   ("invalidated:" + str(reason)[:120], time.time(), snapshot_id))
    record_event("context_snapshot_invalidated", {"snapshot_id": snapshot_id, "reason": str(reason)[:120]})
    return get_context_snapshot(snapshot_id)


def context_snapshot_fresh(snapshot_id: str) -> bool:
    snapshot = get_context_snapshot(snapshot_id)
    if not snapshot["status"] == "active":
        return False
    for source in snapshot["sources"]:
        path = source.get("path")
        expected = source.get("sha256") or source.get("hash")
        if not isinstance(path, str) or not expected:
            continue
        candidate = Path(snapshot["project_root"]) / path
        try:
            actual = sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            invalidate_context_snapshot(snapshot_id, "missing_source")
            return False
        if actual != expected:
            invalidate_context_snapshot(snapshot_id, "source_changed")
            return False
    return True


def validate_profile(profile: dict[str, Any]) -> None:
    required = ("root_goal", "local_goal", "task_type", "constraints", "expected_outputs", "acceptance_criteria", "required_capabilities", "complexity_hint")
    if not isinstance(profile, dict) or any(key not in profile for key in required):
        raise RuntimeError("Task Profile is missing required fields")
    for key in ("root_goal", "local_goal", "task_type", "complexity_hint"):
        if not isinstance(profile[key], str) or not profile[key].strip():
            raise RuntimeError(f"Invalid Task Profile field: {key}")
    for key in ("constraints", "expected_outputs", "acceptance_criteria", "required_capabilities"):
        if not isinstance(profile[key], list) or any(not isinstance(x, str) for x in profile[key]):
            raise RuntimeError(f"Invalid Task Profile field: {key}")


def create_task(profile: dict[str, Any], project_root: str, session_id: str | None = None) -> dict[str, Any]:
    validate_profile(profile)
    project_id, root = project_identity(project_root)
    now = time.time()
    task_id = uuid4().hex
    state = {
        "profile": profile, "plan": [], "plan_history": [], "selected_skills": {},
        "context_refs": [], "agent_results": [], "verification": [],
        "capability_version_lock": dict(profile.get("capability_version_lock", profile.get("version_lock", {})))
        if isinstance(profile.get("capability_version_lock", profile.get("version_lock", {})), dict) else {},
        "usage": {"input_tokens": 0, "output_tokens": 0, "runtime_ms": 0},
        "compact_pending": False,
        "binding_status": "bound" if session_id else "unbound",
        "binding_source": "task_create" if session_id else "session_id_missing",
        "binding_error": None if session_id else "No trusted session_id was supplied at task creation",
        "operations": {},
    }
    with _db() as db:
        db.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, 1, 'active', ?, ?, ?)",
                   (task_id, project_id, root, session_id, json.dumps(state, ensure_ascii=False), now, now))
    record_event("task_created", {"task_id": task_id, "project_id": project_id})
    return get_task(task_id)


def get_task(task_id: str) -> dict[str, Any]:
    with _db() as db:
        row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise RuntimeError("Unknown task_id")
    return _row(row)


def latest_session_task(session_id: str) -> dict[str, Any] | None:
    if not session_id:
        return None
    with _db() as db:
        row = db.execute("SELECT * FROM tasks WHERE session_id = ? ORDER BY updated_at DESC LIMIT 1", (session_id,)).fetchone()
    return _row(row) if row else None


def bind_task_session(task_id: str, session_id: str, cwd: str, requested_root: str, agent_run_id: str | None = None) -> bool:
    """Bind a task with a serialized read/merge/update transaction.

    Binding is metadata, so it intentionally preserves the task revision for
    compatibility with the create->bind hook path. The immediate transaction
    prevents an old JSON snapshot from overwriting a concurrent task update.
    """
    if (not isinstance(session_id, str) or not session_id or not isinstance(cwd, str) or not cwd
            or not isinstance(requested_root, str) or not requested_root):
        return False
    project_root = Path(requested_root).resolve()
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return False
        task = _row(row)
        if task["status"] != "active":
            return False
        project_root = Path(task["project_root"]).resolve()
        if str(Path(requested_root).resolve()).casefold() != str(project_root).casefold():
            return False
        session_cwd = Path(cwd).resolve()
        if not (session_cwd.is_relative_to(project_root) or project_root.is_relative_to(session_cwd)):
            return False
        if task["session_id"] not in (None, session_id):
            record_event("task_session_binding_conflict", {"task_id": task_id, "binding_source": "trusted_session_start"})
            return False
        state = task["state"]
        state["binding_status"] = "bound"
        state["binding_source"] = "trusted_session_start"
        state["binding_error"] = None
        if agent_run_id:
            state["agent_run_id"] = agent_run_id
        cursor = db.execute(
            "UPDATE tasks SET session_id = ?, state = ?, updated_at = ? WHERE id = ? AND revision = ? AND (session_id IS NULL OR session_id = ?)",
            (session_id, json.dumps(state, ensure_ascii=False), time.time(), task_id, task["revision"], session_id),
        )
        if cursor.rowcount != 1:
            record_event("task_session_binding_conflict", {"task_id": task_id, "binding_source": "trusted_session_start"})
            return False
    record_event("task_session_bound", {"task_id": task_id, "binding_source": "trusted_session_start"})
    return True


def active_session_task(session_id: str) -> dict[str, Any] | None:
    """Avoid guessing when one Host session has more than one active task."""
    if not isinstance(session_id, str) or not session_id:
        return None
    with _db() as db:
        rows = db.execute(
            "SELECT * FROM tasks WHERE session_id = ? AND status = 'active' LIMIT 2", (session_id,)
        ).fetchall()
    return _row(rows[0]) if len(rows) == 1 else None


def update_task(task_id: str, expected_revision: int, *, state: dict[str, Any] | None = None, status: str | None = None, event: str = "task_updated") -> dict[str, Any]:
    task = get_task(task_id)
    if task['status'] == 'complete':
        raise RuntimeError('Completed execution is immutable; use runtime_delivery(reopen) before changing it')
    if task["revision"] != expected_revision:
        raise RuntimeError("Task revision changed; reload before writing")
    if status is not None and status not in {"active", "paused", "complete", "cancelled"}:
        raise RuntimeError("Invalid task status")
    state = task["state"] if state is None else state
    now = time.time()
    with _db() as db:
        cursor = db.execute(
            "UPDATE tasks SET revision = revision + 1, status = ?, state = ?, updated_at = ? WHERE id = ? AND revision = ?",
            (status or task["status"], json.dumps(state, ensure_ascii=False), now, task_id, expected_revision),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Task revision changed; reload before writing")
    record_event(event, {"task_id": task_id, "revision": expected_revision + 1, "status": status or task["status"]})
    return get_task(task_id)


def update_operation(task_id: str, expected_revision: int, operation_id: str, status: str,
                     intent: str = "", result_ref: str | None = None) -> dict[str, Any]:
    """Record an idempotent side-effect operation without executing it."""
    if not isinstance(operation_id, str) or not operation_id.strip() or len(operation_id) > 160:
        raise RuntimeError("operation_id is required")
    if status not in {"pending", "started", "succeeded", "failed", "unknown"}:
        raise RuntimeError("Invalid operation status")
    task = get_task(task_id)
    if task["revision"] != expected_revision:
        raise RuntimeError("Task revision changed; reload before writing")
    operations = task["state"].setdefault("operations", {})
    existing = operations.get(operation_id)
    if existing and existing.get("status") == "succeeded" and status != "succeeded":
        raise RuntimeError("Succeeded operation cannot be replayed")
    if existing and existing.get("status") == "unknown" and status == "started":
        raise RuntimeError("Unknown operation requires reconciliation before retry")
    if existing and existing.get("status") == status and not intent and result_ref is None:
        return {"task_id": task_id, "revision": task["revision"], "operation_id": operation_id, **existing, "idempotent": True}
    operations[operation_id] = {
        "operation_id": operation_id,
        "status": status,
        "intent": (intent or (existing or {}).get("intent", ""))[:500],
        "result_ref": result_ref if isinstance(result_ref, str) else (existing or {}).get("result_ref"),
        "updated_at": time.time(),
    }
    updated = update_task(task_id, expected_revision, state=task["state"], event="operation_updated")
    return {"task_id": task_id, "revision": updated["revision"], **updated["state"]["operations"][operation_id], "idempotent": False}


def checkpoint(task_id: str, reason: str) -> dict[str, Any]:
    task = get_task(task_id)
    if not isinstance(reason, str) or not reason.strip():
        raise RuntimeError("Checkpoint reason is required")
    checkpoint_id = uuid4().hex
    saved_state = dict(task["state"])
    evidence_status = "available"
    try:
        from evidence_store import evidence_refs
        refs = evidence_refs(task_id)
        saved_state["evidence_refs"] = refs
        saved_state["compact_result_refs"] = [
            {"evidence_id": ref["evidence_id"], "compact_result_id": ref["compact_result_id"]}
            for ref in refs if ref.get("evidence_id") and ref.get("compact_result_id")
        ]
    except Exception:
        # Optional result evidence must not prevent the normal task checkpoint,
        # but recovery must expose that the evidence was unavailable.
        saved_state["evidence_refs"] = []
        saved_state["compact_result_refs"] = []
        evidence_status = "missing"
    saved_state["checkpoint_evidence_status"] = evidence_status
    saved_state["workspace_snapshot"] = _workspace_snapshot(task["project_root"], task["state"], task_id)
    with _db() as db:
        db.execute("INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?, ?)",
                   (checkpoint_id, task_id, task["revision"], reason[:200], json.dumps(saved_state, ensure_ascii=False), time.time()))
    record_event("checkpoint", {"task_id": task_id, "checkpoint_id": checkpoint_id, "revision": task["revision"]})
    return {"checkpoint_id": checkpoint_id, "task_id": task_id, "revision": task["revision"], "reason": reason[:200]}


def restore(task_id: str, checkpoint_id: str | None = None) -> dict[str, Any]:
    task = get_task(task_id)
    with _db() as db:
        if checkpoint_id:
            row = db.execute("SELECT * FROM checkpoints WHERE id = ? AND task_id = ?", (checkpoint_id, task_id)).fetchone()
        else:
            row = db.execute("SELECT * FROM checkpoints WHERE task_id = ? ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
    if row is None:
        raise RuntimeError("Checkpoint not found")
    saved = json.loads(row["state"])
    if not isinstance(saved, dict):
        raise RuntimeError("Checkpoint state is invalid")
    root = Path(task["project_root"])
    stale_refs = []
    for ref in saved.get("context_refs", []):
        source = (root / ref["path"]).resolve()
        if not source.is_relative_to(root) or not source.is_file() or sha256(source.read_bytes()).hexdigest() != ref["sha256"]:
            stale_refs.append(ref["path"])
    saved_lock = saved.get("capability_version_lock", {})
    current_lock: dict[str, str] = {}
    try:
        from capability_registry import version_lock as capability_versions
        current_lock.update(capability_versions())
        from skill_resolver import load_registry as load_skill_registry
        current_lock.update({item["name"]: item["version"] for item in load_skill_registry().get("skills", [])})
    except Exception:
        # A missing optional registry should not make an old checkpoint unusable.
        current_lock = {}
    changes = {
        key: {"expected": value, "actual": current_lock.get(key)}
        for key, value in saved_lock.items()
        if not isinstance(value, str) or current_lock.get(key) != value
    } if isinstance(saved_lock, dict) else {"invalid": {"expected": saved_lock, "actual": current_lock}}
    workspace_expected = saved.get("workspace_snapshot") or {}
    workspace_actual = _workspace_snapshot(task["project_root"], task["state"], task_id)
    workspace_drift = {}
    workspace_drift_files: list[str] = []
    workspace_snapshot_legacy = not isinstance(workspace_expected, dict) or workspace_expected.get("version") != WORKSPACE_SNAPSHOT_VERSION
    if isinstance(workspace_expected, dict):
        for key in ("git_head", "dirty"):
            if key in workspace_expected and workspace_expected.get(key) != workspace_actual.get(key):
                workspace_drift[key] = {"expected": workspace_expected.get(key), "actual": workspace_actual.get(key)}
        expected_files = {item.get("path"): item for item in workspace_expected.get("files", []) if isinstance(item, dict) and isinstance(item.get("path"), str)}
        actual_files = {item.get("path"): item for item in workspace_actual.get("files", []) if isinstance(item, dict) and isinstance(item.get("path"), str)}
        for path in sorted(set(expected_files) | set(actual_files)):
            if expected_files.get(path) != actual_files.get(path):
                workspace_drift_files.append(path)
    snapshot_incomplete = workspace_snapshot_legacy or bool(workspace_expected.get("snapshot_incomplete")) or bool(workspace_actual.get("snapshot_incomplete"))
    stale_checkpoint = task["revision"] != row["revision"]
    needs_reconciliation = bool(stale_checkpoint or stale_refs or changes or workspace_drift or workspace_drift_files or snapshot_incomplete or saved.get("checkpoint_evidence_status") == "missing")
    status = "needs_reconciliation" if needs_reconciliation else "resumable"
    conflicts = []
    if stale_checkpoint:
        conflicts.append("task_revision_changed")
    if workspace_drift:
        conflicts.append("workspace_drift")
    if workspace_drift_files:
        conflicts.append("workspace_file_drift")
    if snapshot_incomplete:
        conflicts.append("workspace_snapshot_incomplete")
    if stale_refs:
        conflicts.append("context_refs_stale")
    if changes:
        conflicts.append("capability_changed")
    if saved.get("checkpoint_evidence_status") == "missing":
        conflicts.append("evidence_missing")
    return {
        "task_id": task_id, "project_id": task["project_id"], "project_root": task["project_root"],
        "current_revision": task["revision"], "checkpoint_id": row["id"],
        "checkpoint_revision": row["revision"], "state": saved,
        "status": status, "conflicts": conflicts,
        "stale_context_refs": stale_refs,
        "workspace_drift": workspace_drift,
        "workspace_drift_files": workspace_drift_files,
        "workspace_snapshot": workspace_actual,
        "checkpoint_evidence_status": saved.get("checkpoint_evidence_status", "unknown"),
        "current_operations": task["state"].get("operations", {}),
        "capability_changed": bool(changes),
        "capability_change_details": changes,
        "current_capability_version_lock": current_lock,
        "restore_instruction": "Re-resolve selected Skills, Capability and MCP Provider references when capability_changed is true; then verify source revisions before continuing. This read does not roll back current task state.",
    }


def _workspace_candidates(project_root: Path, state: dict[str, Any] | None, task_id: str | None) -> list[str]:
    candidates: list[str] = []
    if isinstance(state, dict):
        for ref in state.get("context_refs", []):
            if isinstance(ref, dict) and isinstance(ref.get("path"), str):
                candidates.append(ref["path"])
        plan = state.get("plan", [])
        if isinstance(plan, list):
            for step in plan:
                if isinstance(step, dict):
                    for field in ("write_paths", "expected_outputs"):
                        values = step.get(field, [])
                        if isinstance(values, list):
                            candidates.extend(item for item in values if isinstance(item, str))
        profile = state.get("profile", {})
        if isinstance(profile, dict) and isinstance(profile.get("expected_outputs"), list):
            candidates.extend(item for item in profile["expected_outputs"] if isinstance(item, str))
    if task_id:
        try:
            with _db() as db:
                rows = db.execute("""SELECT path FROM delivery_files WHERE delivery_id IN
                    (SELECT id FROM delivery_versions WHERE task_id = ? AND version =
                     (SELECT MAX(version) FROM delivery_versions WHERE task_id = ?))""", (task_id, task_id)).fetchall()
            candidates.extend(row["path"] for row in rows if isinstance(row["path"], str))
        except sqlite3.Error:
            pass
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        try:
            candidate = Path(raw)
            resolved = candidate.resolve() if candidate.is_absolute() else (project_root / candidate).resolve()
            relative = resolved.relative_to(project_root).as_posix()
        except (OSError, ValueError):
            relative = str(raw)
        key = relative.casefold()
        if key not in seen:
            seen.add(key)
            normalized.append(relative)
    return normalized


def _workspace_snapshot(project_root: str, state: dict[str, Any] | None = None, task_id: str | None = None) -> dict[str, Any]:
    """Return bounded, metadata-only workspace facts for reconciliation."""
    root = Path(project_root).resolve()
    result: dict[str, Any] = {"version": WORKSPACE_SNAPSHOT_VERSION, "git_head": None, "dirty": None,
                              "files": [], "snapshot_incomplete": False, "incomplete_reasons": []}
    try:
        _nw = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=2, check=True, creationflags=_nw)
        result["git_head"] = head.stdout.strip() or None
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True, timeout=2, check=True, creationflags=_nw)
        result["dirty"] = bool(dirty.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    candidates = _workspace_candidates(root, state, task_id)
    deadline = time.monotonic() + MAX_WORKSPACE_SCAN_SECONDS
    if len(candidates) > MAX_WORKSPACE_FILES:
        result["snapshot_incomplete"] = True
        result["incomplete_reasons"].append("file_count_limit")
    for relative in candidates[:MAX_WORKSPACE_FILES]:
        if time.monotonic() > deadline:
            result["snapshot_incomplete"] = True
            result["incomplete_reasons"].append("scan_timeout")
            break
        original_path = root / relative
        path = original_path
        item: dict[str, Any] = {"path": relative, "exists": False, "size": 0, "sha256": None}
        try:
            resolved_path = path.resolve(strict=False)
            if not resolved_path.is_relative_to(root):
                item["unsafe"] = "path_outside_project"
                result["snapshot_incomplete"] = True
                result["incomplete_reasons"].append("unsafe_path")
                result["files"].append(item)
                continue
            path = resolved_path
            if any(part.startswith(".") or part.casefold() in PRIVATE_WORKSPACE_NAMES or any(word in part.casefold() for word in ("secret", "credential", "password", "token")) for part in path.relative_to(root).parts):
                item["unsafe"] = "private_path"
                result["snapshot_incomplete"] = True
                result["incomplete_reasons"].append("private_path")
                result["files"].append(item)
                continue
            if original_path.is_symlink():
                target = original_path.resolve()
                if not target.is_relative_to(root):
                    item["unsafe"] = "symlink_outside_project"
                    result["snapshot_incomplete"] = True
                    result["incomplete_reasons"].append("unsafe_path")
                    result["files"].append(item)
                    continue
            if not path.exists():
                result["files"].append(item)
                continue
            if not path.is_file():
                item["unsafe"] = "not_a_file"
                result["snapshot_incomplete"] = True
                result["incomplete_reasons"].append("unsafe_path")
                result["files"].append(item)
                continue
            size = path.stat().st_size
            item["exists"] = True
            item["size"] = size
            if size > MAX_WORKSPACE_FILE_BYTES:
                item["skipped"] = "size_limit"
                result["snapshot_incomplete"] = True
                result["incomplete_reasons"].append("file_size_limit")
            else:
                item["sha256"] = sha256(path.read_bytes()).hexdigest()
            result["files"].append(item)
        except (OSError, ValueError):
            item["unsafe"] = "unreadable"
            result["snapshot_incomplete"] = True
            result["incomplete_reasons"].append("unreadable")
            result["files"].append(item)
    return result


def list_tasks(limit: int = 20) -> list[dict[str, Any]]:
    with _db() as db:
        rows = db.execute("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (min(max(limit, 1), 100),)).fetchall()
    return [_row(row) for row in rows]


def put_fact(task_id: str, key: str, value: str, source_path: str, evidence: str, source_revision: str | None = None) -> dict[str, Any]:
    from gdrive_memory_engine import gdrive_memory_db
    task = get_task(task_id)
    root = Path(task["project_root"])
    if not isinstance(source_path, str) or Path(source_path).is_absolute():
        raise RuntimeError("Fact source must be a relative project file")
    source = (root / source_path).resolve()
    blocked = {".env", ".env.local", "credentials.json", "secrets.json", "id_rsa", "id_ed25519", "config.toml"}
    private_name = source.name.casefold()
    if not source.is_relative_to(root) or any(part.startswith(".") for part in source.relative_to(root).parts) or private_name in blocked or any(word in private_name for word in ("secret", "credential", "password", "token")) or source.suffix.casefold() in {".pem", ".key", ".p12"} or not source.is_file() or source.stat().st_size > 2_000_000:
        raise RuntimeError("Fact source must be a project file up to 2 MB")
    if not key or not isinstance(value, str) or not value.strip():
        raise RuntimeError("Fact key and value are required")
    if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 500:
        raise RuntimeError("A short source evidence excerpt is required")
    raw = source.read_bytes()
    if evidence not in raw.decode("utf-8", errors="replace"):
        raise RuntimeError("Fact evidence does not occur in the source file")
    digest = sha256(raw).hexdigest()
    relative = source.relative_to(root).as_posix()
    with gdrive_memory_db() as db:
        db.execute("INSERT OR REPLACE INTO facts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (task["project_id"], key, value[:2000], relative, digest, evidence, source_revision, time.time()))
    record_event("fact_saved", {"task_id": task_id, "project_id": task["project_id"], "key": key})
    return {"key": key, "source_path": relative, "source_sha256": digest, "evidence": evidence, "source_revision": source_revision}


def list_facts(task_id: str) -> list[dict[str, Any]]:
    from gdrive_memory_engine import gdrive_memory_db
    task = get_task(task_id)
    root = Path(task["project_root"])
    with gdrive_memory_db() as db:
        rows = db.execute("SELECT * FROM facts WHERE project_id = ? ORDER BY fact_key", (task["project_id"],)).fetchall()
    result = []
    for row in rows:
        source = (root / row["source_path"]).resolve()
        fresh = source.is_relative_to(root) and source.is_file() and source.stat().st_size <= 2_000_000 and sha256(source.read_bytes()).hexdigest() == row["source_sha256"]
        result.append({"key": row["fact_key"], "value": row["value"], "source_path": row["source_path"], "evidence": row["evidence"], "source_revision": row["source_revision"], "stale": not fresh})
    return result


GLOBAL_PROJECT_ID = "__global__"
GLOBAL_TASK_ID = "shared-__global__"


def auto_desensitize_for_global(text: str) -> str:
    """Automatically scrub project-specific paths, user home dirs, task IDs, and tokens when promoting to Global Memory."""
    s = str(text or "").strip()
    # Scrub tokens/secrets
    s = re.sub(r"\b(?:ghp|gho|ghu|ghs|ghr|hsk)_[A-Za-z0-9_]{16,}\b", "<REDACTED_TOKEN>", s)
    s = re.sub(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b", "<REDACTED_KEY>", s)
    # Normalize task-specific workdirs
    s = re.sub(r"[A-Za-z]:[\\/][^\s\"'`]*?task-[0-9a-fA-F-]+[\\/]workdir", "<TASK_WORKDIR>", s)
    # Normalize user home paths while preserving explicit external workspace mounts
    s = re.sub(r"C:[\\/]Users[\\/][A-Za-z0-9_.-]+", "<USER_HOME>", s, flags=re.I)
    s = re.sub(r"/home/[A-Za-z0-9_.-]+", "<USER_HOME>", s)
    return s


def add_memory(
    task_id: str,
    kind: str,
    summary: str,
    *,
    scope: str = "project",
    hard_boundary: bool = False,
    source_doc: str | None = None,
    supersede_id: str | None = None,
    pyramid_level: str | None = None,
    trust_tier: str | None = None,
) -> dict[str, Any]:
    from gdrive_memory_engine import gdrive_memory_db
    if scope not in {"project", "global"}:
        raise RuntimeError("Memory scope must be 'project' or 'global'")
    if kind not in {"decision", "failed_attempt", "finding", "lesson", "task_summary"} or not isinstance(summary, str) or not summary.strip():
        raise RuntimeError("Invalid memory kind or summary")

    if scope == "global":
        target_project_id = GLOBAL_PROJECT_ID
        clean_summary = auto_desensitize_for_global(summary)[:2000]
    else:
        task = get_task(task_id)
        target_project_id = task["project_id"]
        clean_summary = summary.strip()[:2000]

    hb_int = 1 if hard_boundary else 0
    p_level = pyramid_level or ("L3_CORE_PERSONA" if hb_int else "L1_ATOMIC_LESSON")
    t_tier = trust_tier or ("TIER_1_SYSTEM_HARD" if hb_int else "TIER_2_RUNTIME_VERIFIED")
    now = time.time()
    with gdrive_memory_db() as db:
        row = db.execute(
            "SELECT id FROM memories WHERE project_id = ? AND kind = ? AND summary = ? AND invalid_at IS NULL LIMIT 1",
            (target_project_id, kind, clean_summary),
        ).fetchone()
        if row:
            return {
                "memory_id": row["id"],
                "project_id": target_project_id,
                "scope": scope,
                "hard_boundary": bool(hb_int),
                "pyramid_level": p_level,
                "trust_tier": t_tier,
                "kind": kind,
                "deduplicated": True,
            }
        memory_id = uuid4().hex
        if supersede_id:
            db.execute(
                "UPDATE memories SET invalid_at = ?, superseded_by = ? WHERE id = ? AND invalid_at IS NULL",
                (now, memory_id, supersede_id),
            )
        db.execute(
            "INSERT INTO memories (id, project_id, kind, summary, source_task_id, created_at, scope, hard_boundary, valid_from, invalid_at, superseded_by, source_doc, pyramid_level, trust_tier) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)",
            (memory_id, target_project_id, kind, clean_summary, task_id, now, scope, hb_int, now, source_doc, p_level, t_tier),
        )
    record_event("memory_saved", {"task_id": task_id, "project_id": target_project_id, "scope": scope, "kind": kind})
    return {
        "memory_id": memory_id,
        "project_id": target_project_id,
        "scope": scope,
        "hard_boundary": bool(hb_int),
        "pyramid_level": p_level,
        "trust_tier": t_tier,
        "kind": kind,
        "superseded": supersede_id,
    }


def list_memories(
    task_id: str,
    limit: int = 20,
    *,
    scope: str = "project+global",
    include_invalidated: bool = False,
    query: str = "",
) -> list[dict[str, Any]]:
    from gdrive_memory_engine import build_fts_match_query, gdrive_memory_db
    if scope not in {"project", "global", "modality", "project+global", "3d"}:
        scope = "project+global"
    project_id = GLOBAL_PROJECT_ID
    if scope in {"project", "project+global", "3d"} and task_id != GLOBAL_TASK_ID:
        try:
            task = get_task(task_id)
            project_id = task["project_id"]
        except Exception:
            project_id = GLOBAL_PROJECT_ID

    lim = min(max(limit, 1), 250)
    inv_clause = "" if include_invalidated else " AND m.invalid_at IS NULL"
    fts_expr = build_fts_match_query(query) if query and query.strip() else ""

    with gdrive_memory_db() as db:
        if fts_expr:
            if scope == "modality":
                rows = db.execute(
                    f"""SELECT m.*, fts.fts_rank
                        FROM memories m
                        LEFT JOIN (
                            SELECT id, bm25(memories_fts) AS fts_rank
                            FROM memories_fts
                            WHERE memories_fts MATCH ?
                        ) fts ON m.id = fts.id
                        WHERE m.scope = 'modality'{inv_clause}
                          AND (m.hard_boundary = 1 OR fts.fts_rank IS NOT NULL)
                        ORDER BY CASE WHEN fts.fts_rank IS NOT NULL THEN 0 ELSE 1 END ASC,
                                 m.hard_boundary DESC,
                                 fts.fts_rank ASC,
                                 m.created_at DESC
                        LIMIT ?""",
                    (fts_expr, lim),
                ).fetchall()
            elif scope == "global" or project_id == GLOBAL_PROJECT_ID:
                rows = db.execute(
                    f"""SELECT m.*, fts.fts_rank
                        FROM memories m
                        LEFT JOIN (
                            SELECT id, bm25(memories_fts) AS fts_rank
                            FROM memories_fts
                            WHERE memories_fts MATCH ?
                        ) fts ON m.id = fts.id
                        WHERE (m.project_id = ? OR m.scope IN ('global', 'modality')){inv_clause}
                          AND (m.hard_boundary = 1 OR fts.fts_rank IS NOT NULL)
                        ORDER BY CASE WHEN fts.fts_rank IS NOT NULL THEN 0 ELSE 1 END ASC,
                                 m.hard_boundary DESC,
                                 CASE m.trust_tier WHEN 'TIER_1_SYSTEM_HARD' THEN 0 WHEN 'TIER_2_RUNTIME_VERIFIED' THEN 1 ELSE 2 END ASC,
                                 fts.fts_rank ASC,
                                 m.created_at DESC
                        LIMIT ?""",
                    (fts_expr, GLOBAL_PROJECT_ID, lim),
                ).fetchall()
            elif scope == "project":
                rows = db.execute(
                    f"""SELECT m.*, fts.fts_rank
                        FROM memories m
                        JOIN (
                            SELECT id, bm25(memories_fts) AS fts_rank
                            FROM memories_fts
                            WHERE memories_fts MATCH ?
                        ) fts ON m.id = fts.id
                        WHERE m.project_id = ? AND m.scope = 'project'{inv_clause}
                        ORDER BY fts.fts_rank ASC, m.created_at DESC
                        LIMIT ?""",
                    (fts_expr, project_id, lim),
                ).fetchall()
            else:
                rows = db.execute(
                    f"""SELECT m.*, fts.fts_rank
                        FROM memories m
                        LEFT JOIN (
                            SELECT id, bm25(memories_fts) AS fts_rank
                            FROM memories_fts
                            WHERE memories_fts MATCH ?
                        ) fts ON m.id = fts.id
                        WHERE (m.project_id = ? OR m.project_id = ? OR m.scope IN ('global', 'modality')){inv_clause}
                          AND (m.hard_boundary = 1 OR m.scope = 'project' OR fts.fts_rank IS NOT NULL)
                        ORDER BY CASE WHEN fts.fts_rank IS NOT NULL THEN 0 ELSE 1 END ASC,
                                 m.hard_boundary DESC,
                                 CASE m.scope WHEN 'project' THEN 0 WHEN 'modality' THEN 1 ELSE 2 END ASC,
                                 CASE m.trust_tier WHEN 'TIER_1_SYSTEM_HARD' THEN 0 WHEN 'TIER_2_RUNTIME_VERIFIED' THEN 1 ELSE 2 END ASC,
                                 fts.fts_rank ASC,
                                 m.created_at DESC
                        LIMIT ?""",
                    (fts_expr, project_id, GLOBAL_PROJECT_ID, lim),
                ).fetchall()
        else:
            if scope == "modality":
                rows = db.execute(
                    f"SELECT m.* FROM memories m WHERE m.scope = 'modality'{inv_clause} ORDER BY m.hard_boundary DESC, m.created_at DESC LIMIT ?",
                    (lim,),
                ).fetchall()
            elif scope == "global" or project_id == GLOBAL_PROJECT_ID:
                rows = db.execute(
                    f"SELECT m.* FROM memories m WHERE (m.project_id = ? OR m.scope IN ('global', 'modality')){inv_clause} ORDER BY m.hard_boundary DESC, m.created_at DESC LIMIT ?",
                    (GLOBAL_PROJECT_ID, lim),
                ).fetchall()
            elif scope == "project":
                rows = db.execute(
                    f"SELECT m.* FROM memories m WHERE m.project_id = ? AND m.scope = 'project'{inv_clause} ORDER BY m.created_at DESC LIMIT ?",
                    (project_id, lim),
                ).fetchall()
            else:
                rows = db.execute(
                    f"SELECT m.* FROM memories m WHERE (m.project_id = ? OR m.project_id = ? OR m.scope IN ('global', 'modality')){inv_clause} ORDER BY m.hard_boundary DESC, CASE m.scope WHEN 'project' THEN 0 WHEN 'modality' THEN 1 ELSE 2 END ASC, m.created_at DESC LIMIT ?",
                    (project_id, GLOBAL_PROJECT_ID, lim),
                ).fetchall()

    results = []
    for row in rows:
        rk = row.keys()
        r_scope = row["scope"] if "scope" in rk and row["scope"] else ("global" if row["project_id"] == GLOBAL_PROJECT_ID else "project")
        r_hb = bool(row["hard_boundary"]) if "hard_boundary" in rk else False
        if r_scope == "global" and r_hb:
            precedence = "GLOBAL_HARD_OVERRIDE"
        elif r_scope == "project":
            precedence = "PROJECT_LOCAL_SHADOW"
        elif r_scope == "modality":
            precedence = "MODALITY_CRAFT_STANDARD"
        else:
            precedence = "GLOBAL_DEFAULT"
        results.append({
            "memory_id": row["id"],
            "project_id": row["project_id"],
            "scope": r_scope,
            "hard_boundary": r_hb,
            "pyramid_level": row["pyramid_level"] if "pyramid_level" in rk else "L1_ATOMIC_LESSON",
            "trust_tier": row["trust_tier"] if "trust_tier" in rk else "TIER_2_RUNTIME_VERIFIED",
            "precedence": precedence,
            "kind": row["kind"],
            "summary": row["summary"],
            "source_task_id": row["source_task_id"],
            "source_doc": row["source_doc"] if "source_doc" in rk else None,
            "valid_from": row["valid_from"] if "valid_from" in rk else row["created_at"],
            "invalid_at": row["invalid_at"] if "invalid_at" in rk else None,
            "superseded_by": row["superseded_by"] if "superseded_by" in rk else None,
        })
    return results


def ensure_project_task(task_id: str | None = None, project_root: str | None = None) -> str:
    if isinstance(task_id, str) and task_id.strip():
        with _db() as db:
            row = db.execute("SELECT id, project_root FROM tasks WHERE id = ?", (task_id.strip(),)).fetchone()
            if row is not None:
                if project_root and Path(project_root).is_dir() and not Path(row["project_root"]).is_dir():
                    db.execute("UPDATE tasks SET project_root = ?, updated_at = ? WHERE id = ?",
                               (str(Path(project_root).resolve()), time.time(), row["id"]))
                return str(row["id"])
    target_root = project_root if isinstance(project_root, str) and project_root.strip() else str(Path.cwd())
    project_id, resolved_root = project_identity(target_root)
    shared_task_id = f"shared-{project_id}"
    now = time.time()
    with _db() as db:
        row = db.execute("SELECT id, project_root FROM tasks WHERE id = ?", (shared_task_id,)).fetchone()
        if row is not None:
            if Path(resolved_root).is_dir() and row["project_root"] != resolved_root:
                db.execute("UPDATE tasks SET project_root = ?, updated_at = ? WHERE id = ?",
                           (resolved_root, now, shared_task_id))
            return shared_task_id
        state = {
            "profile": {
                "root_goal": "Cross-CLI shared project memory anchor",
                "local_goal": "Maintain shared project facts and historical memories across Codex and Antigravity CLIs",
                "task_type": "engineering",
                "constraints": ["cross-cli memory sharing"],
                "expected_outputs": ["shared facts and memories"],
                "acceptance_criteria": ["persisted in runtime.sqlite3"],
                "complexity_hint": "moderate",
            },
            "plan": [], "plan_history": [], "selected_skills": {},
            "context_refs": [], "agent_results": [], "verification": [],
            "usage": {"input_tokens": 0, "output_tokens": 0, "runtime_ms": 0},
            "compact_pending": False, "binding_status": "unbound",
            "binding_source": "shared_project_memory", "binding_error": None,
            "operations": {},
        }
        db.execute("INSERT INTO tasks VALUES (?, ?, ?, NULL, 1, 'active', ?, ?, ?)",
                   (shared_task_id, project_id, resolved_root, json.dumps(state, ensure_ascii=False), now, now))
    return shared_task_id
