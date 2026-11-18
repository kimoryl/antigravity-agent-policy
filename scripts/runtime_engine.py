"""Algorithmic plan, context, verification, and context-lifecycle operations."""

from __future__ import annotations

import os
import re
import subprocess
from hashlib import sha256
from pathlib import Path
from typing import Any

from policy_state import record_event
from policy_engine import load_policy
from agent_registry import load_agents
from jev_adapter import JevUnavailable, check_verification, rank_context
from memory_backend import memory_recall
from runtime_state import RuntimeError, checkpoint, get_task, list_facts, update_task


SKIP_DIRS = {".git", ".codex", ".agents", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next"}
BLOCKED_NAMES = {".env", ".env.local", "credentials.json", "secrets.json", "id_rsa", "id_ed25519", "config.toml", "AGENTS.md"}
STATES = {"pending", "running", "done", "failed", "cancelled"}


def _context_text_chars(value: Any) -> int:
    """Count returned text in the context channels, including provenance."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(_context_text_chars(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_context_text_chars(item) for item in value)
    return 0


def _safe_source(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise RuntimeError("Source must be a relative project path")
    path = (root / relative).resolve()
    private_name = path.name.casefold()
    if not path.is_relative_to(root) or any(part.startswith(".") for part in path.relative_to(root).parts) or private_name in {name.casefold() for name in BLOCKED_NAMES} or any(word in private_name for word in ("secret", "credential", "password", "token")) or path.suffix.casefold() in {".pem", ".key", ".p12"}:
        raise RuntimeError("Source path is outside the allowed project files")
    if not path.is_file() or path.stat().st_size > 1_000_000:
        raise RuntimeError("Source file is missing or exceeds 1 MB")
    return path


def _norm_plan_path(raw: str) -> str:
    parts = [p for p in raw.replace("\\", "/").strip().split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise RuntimeError(f"Traversal segment '..' is not allowed in write_paths: {raw}")
    return "/".join(parts)


def _paths_overlap(left_path: str, right_path: str) -> bool:
    lp, rp = _norm_plan_path(left_path), _norm_plan_path(right_path)
    if not lp or not rp:
        return False
    if os.name == "nt":
        lp, rp = lp.casefold(), rp.casefold()
    return lp == rp or lp.startswith(rp + "/") or rp.startswith(lp + "/")


def _co_occurrence_warnings(
    steps: list[dict[str, Any]],
    ancestors_fn: Any,
    project_root: str | None = None,
) -> list[dict[str, Any]]:
    """Query historical co_occurrence_edges to warn when strongly coupled files are split across parallel workers."""
    if not project_root:
        return []
    try:
        import sqlite3
        from policy_state import data_dir
        from supervisor_engine import _workspace_key

        db_path = data_dir() / "runtime.sqlite3"
        if not db_path.is_file():
            return []
        ws_id = _workspace_key(Path(project_root))
        conn = sqlite3.connect(str(db_path), timeout=1.0)
        try:
            rows = conn.execute(
                "SELECT file_a, file_b, weight FROM co_occurrence_edges WHERE workspace_id = ? AND weight >= 0.5",
                (ws_id,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []

    if not rows:
        return []
    coupled: dict[tuple[str, str], float] = {}
    for fa, fb, w in rows:
        na, nb = _norm_plan_path(str(fa)), _norm_plan_path(str(fb))
        coupled[(na, nb)] = float(w)
        coupled[(nb, na)] = float(w)

    warnings: list[dict[str, Any]] = []
    for i, left in enumerate(steps):
        for right in steps[i + 1 :]:
            if left["id"] in ancestors_fn(right["id"]) or right["id"] in ancestors_fn(left["id"]):
                continue
            lpaths = [_norm_plan_path(x) for x in left.get("write_paths", [])]
            rpaths = [_norm_plan_path(x) for x in right.get("write_paths", [])]
            for lp in lpaths:
                for rp in rpaths:
                    weight = coupled.get((lp, rp))
                    if weight is not None:
                        warnings.append(
                            {
                                "steps": [left["id"], right["id"]],
                                "files": [lp, rp],
                                "weight": round(weight, 3),
                                "advice": "Files historically co-change; publish interface signature via team_message(action='publish_contract')",
                            }
                        )
    return warnings


def validate_plan(steps: list[dict[str, Any]], *, project_root: str | None = None) -> dict[str, Any]:
    if not isinstance(steps, list) or not steps or len(steps) > 100:
        raise RuntimeError("Plan must contain 1 to 100 steps")
    ids: set[str] = set()
    roles = set(load_agents()["roles"])
    normalized_steps: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", step["id"]):
            raise RuntimeError("Every step needs a short id")
        if step["id"] in ids:
            raise RuntimeError("Duplicate plan step id")
        ids.add(step["id"])
        if not isinstance(step.get("goal"), str) or not step["goal"].strip() or not isinstance(step.get("role"), str) or not step["role"].strip():
            raise RuntimeError(f"Step {step['id']} needs goal and role")
        if step["role"] not in roles:
            raise RuntimeError(f"Unknown Agent role in {step['id']}")
        for field in ("depends_on", "expected_outputs", "write_paths", "write_files"):
            if field in step and (not isinstance(step.get(field), list) or any(not isinstance(x, str) for x in step.get(field, []))):
                raise RuntimeError(f"Invalid {field} in {step['id']}")
        if step.get("status", "pending") not in STATES:
            raise RuntimeError(f"Invalid status in {step['id']}")
        merged_writes = list(dict.fromkeys([*(step.get("write_paths") or []), *(step.get("write_files") or [])]))
        normalized_steps.append({**step, "write_paths": merged_writes})
    graph = {step["id"]: step.get("depends_on", []) for step in normalized_steps}
    for name, deps in graph.items():
        if name in deps or not set(deps).issubset(ids):
            raise RuntimeError(f"Unknown or self dependency in {name}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise RuntimeError("Plan contains a dependency cycle")
        if name in visited:
            return
        visiting.add(name)
        for dep in graph[name]:
            visit(dep)
        visiting.remove(name)
        visited.add(name)

    for name in ids:
        visit(name)

    def ancestors(name: str) -> set[str]:
        result: set[str] = set()
        for dep in graph[name]:
            result.add(dep)
            result.update(ancestors(dep))
        return result

    conflicts = []
    for i, left in enumerate(normalized_steps):
        for right in normalized_steps[i + 1:]:
            if left["id"] in ancestors(right["id"]) or right["id"] in ancestors(left["id"]):
                continue
            overlapping: set[str] = set()
            for lp in left.get("write_paths", []):
                for rp in right.get("write_paths", []):
                    if _paths_overlap(lp, rp):
                        overlapping.add(_norm_plan_path(lp) if len(_norm_plan_path(lp)) >= len(_norm_plan_path(rp)) else _norm_plan_path(rp))
            if overlapping:
                conflicts.append({"steps": [left["id"], right["id"]], "write_paths": sorted(overlapping)})
    co_warnings = _co_occurrence_warnings(normalized_steps, ancestors, project_root)
    return {
        "valid": not conflicts,
        "parallel_write_conflicts": conflicts,
        "co_occurrence_warnings": co_warnings,
        "step_count": len(normalized_steps),
    }


def set_plan(task_id: str, expected_revision: int, steps: list[dict[str, Any]]) -> dict[str, Any]:
    task = get_task(task_id)
    validation = validate_plan(steps, project_root=task.get("project_root"))
    if not validation["valid"]:
        raise RuntimeError("Plan has parallel writes to the same path")
    if task["revision"] != expected_revision:
        raise RuntimeError("Task revision changed; reload before writing")
    state = task["state"]
    existing = {step["id"]: step for step in state["plan"]}
    for old in existing.values():
        if old["status"] in {"running", "done"} and old["id"] not in {step["id"] for step in steps}:
            raise RuntimeError("Cannot remove a running or completed step")
    normalized_plan: list[dict[str, Any]] = []
    for step in steps:
        if step["id"] in existing and existing[step["id"]]["status"] in {"running", "done"}:
            if step.get("goal") != existing[step["id"]]["goal"] or step.get("depends_on", []) != existing[step["id"]]["depends_on"]:
                raise RuntimeError("Cannot rewrite a running or completed step")
            step = {**step, "status": existing[step["id"]]["status"]}
        merged_writes = list(dict.fromkeys([*(step.get("write_paths") or []), *(step.get("write_files") or [])]))
        clean_step = {k: v for k, v in step.items() if k != "write_files"}
        normalized_plan.append(
            {
                **clean_step,
                "depends_on": step.get("depends_on", []),
                "expected_outputs": step.get("expected_outputs", []),
                "write_paths": merged_writes,
                "status": step.get("status", "pending"),
            }
        )
    state["plan"] = normalized_plan
    updated = update_task(task_id, expected_revision, state=state, event="plan_saved")
    return {"task_id": task_id, "revision": updated["revision"], "plan": updated["state"]["plan"], "validation": validation}


def ready_steps(task_id: str, maximum: int = 3, *, team_limit: int | None = None, occupied_slots: int = 0) -> dict[str, Any]:
    task = get_task(task_id)
    plan = task["state"]["plan"]
    done = {step["id"] for step in plan if step["status"] == "done"}
    running = sum(step["status"] == "running" for step in plan)
    ready = [step for step in plan if step["status"] == "pending" and set(step["depends_on"]).issubset(done)]
    if type(maximum) is not int or maximum < 0:
        raise RuntimeError("maximum must be a nonnegative integer")
    if team_limit is not None and (type(team_limit) is not int or team_limit < 0):
        raise RuntimeError("team_limit must be a nonnegative integer")
    if type(occupied_slots) is not int or occupied_slots < 0:
        raise RuntimeError("occupied_slots must be a nonnegative integer")
    global_available = max(0, maximum - running)
    team_available = max(0, team_limit - occupied_slots) if team_limit is not None else global_available
    effective_limit = min(global_available, team_available)
    return {
        "task_id": task_id, "revision": task["revision"],
        "ready": ready[:effective_limit], "running": running,
        "dispatch_limit": {"dag_ready": len(ready), "global_limit": maximum,
                            "team_limit": team_limit, "occupied_slots": occupied_slots,
                            "effective_limit": effective_limit},
    }


def update_step(task_id: str, expected_revision: int, step_id: str, status: str, result: str = "") -> dict[str, Any]:
    task = get_task(task_id)
    if task["revision"] != expected_revision:
        raise RuntimeError("Task revision changed; reload before writing")
    plan = task["state"]["plan"]
    step = next((x for x in plan if x["id"] == step_id), None)
    if step is None:
        raise RuntimeError("Unknown plan step")
    allowed = {"pending": {"running", "cancelled"}, "running": {"done", "failed", "cancelled"}, "failed": {"running", "cancelled"}, "done": set(), "cancelled": set()}
    if status not in allowed[step["status"]]:
        raise RuntimeError("Invalid step transition")
    if status == "running":
        ready = ready_steps(task_id, maximum=load_policy()["limits"]["max_parallel_agents"])["ready"]
        if step["status"] != "failed" and step not in ready:
            raise RuntimeError("Step dependencies are not complete")
    step["status"] = status
    if result:
        step["result"] = result[:2000]
    updated = update_task(task_id, expected_revision, state=task["state"], event="step_updated")
    return {"task_id": task_id, "revision": updated["revision"], "step_id": step_id, "status": status}


def resolve_context(task_id: str, query: str, requested_paths: list[str] | None = None, max_chars: int = 12000,
                    use_jev: bool = True, memory: bool = True, memory_limit: int = 5,
                    memory_max_chars: int = 4000, memory_kinds: list[str] | None = None,
                    experience: bool = True, experience_limit: int = 3,
                    experience_max_chars: int = 2500, experience_scope: str = "project+global",
                    context_snapshot_id: str | None = None) -> dict[str, Any]:
    task = get_task(task_id)
    root = Path(task["project_root"])
    if not isinstance(query, str) or not query.strip() or not 500 <= max_chars <= 50000:
        raise RuntimeError("query or max_chars is invalid")
    if requested_paths is not None and (not isinstance(requested_paths, list) or len(requested_paths) > 20):
        raise RuntimeError("requested_paths must contain at most 20 paths")
    if not isinstance(memory, bool) or not isinstance(memory_limit, int) or not 1 <= memory_limit <= 10:
        raise RuntimeError("memory or memory_limit is invalid")
    if not isinstance(memory_max_chars, int) or not 1 <= memory_max_chars <= 8000:
        raise RuntimeError("memory_max_chars is invalid")
    if memory_kinds is not None and (not isinstance(memory_kinds, list) or any(not isinstance(kind, str) for kind in memory_kinds)):
        raise RuntimeError("memory_kinds must be a list of strings")
    if not isinstance(experience, bool) or not isinstance(experience_limit, int) or not 1 <= experience_limit <= 8:
        raise RuntimeError("experience or experience_limit is invalid")
    if not isinstance(experience_max_chars, int) or not 1 <= experience_max_chars <= 8000:
        raise RuntimeError("experience_max_chars is invalid")
    if experience_scope not in {"project", "global", "project+global"}:
        raise RuntimeError("experience_scope is invalid")
    record_event("context_requested", {"task_id": task_id, "explicit_paths": len(requested_paths or []), "max_chars": max_chars})
    terms = set(re.findall(r"[a-zA-Z0-9_]{3,}", query.casefold()))
    if requested_paths:
        candidates = [path for path in requested_paths]
    else:
        candidates = []
        for directory, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs if name not in SKIP_DIRS and not name.startswith(".")]
            for name in files:
                if name in BLOCKED_NAMES or name.startswith("."):
                    continue
                relative = (Path(directory) / name).relative_to(root).as_posix()
                candidates.append(relative)
                if len(candidates) >= 3000:
                    break
            if len(candidates) >= 3000:
                break
        candidates.sort(key=lambda name: (-sum(term in name.casefold() for term in terms), len(name), name))
        candidates = candidates[:8]
    jev_status = "skipped"
    if not requested_paths and use_jev and len(candidates) > 1:
        previews = []
        for relative in candidates:
            try:
                previews.append({"path": relative, "excerpt": _safe_source(root, relative).read_text(encoding="utf-8")[:220]})
            except (RuntimeError, UnicodeDecodeError, OSError):
                continue
        if len(previews) > 1:
            try:
                candidates = rank_context(query, previews)
                jev_status = "ranked"
            except JevUnavailable:
                jev_status = "unavailable"
    # The four context channels share one character budget. Reserve bounded space for
    # the task, facts and memory; unused space goes to current repository sources.
    profile = task["state"]["profile"]
    snapshot_limit = max_chars // 20
    fact_limit = max_chars * 15 // 100
    memory_limit_chars = min(memory_max_chars, max_chars * 20 // 100) if memory else 0
    root_goal = profile["root_goal"][:snapshot_limit // 2]
    constraint_remaining = snapshot_limit // 4
    constraints = []
    for constraint in profile["constraints"]:
        if constraint_remaining <= 0:
            break
        item = constraint[:constraint_remaining]
        if item:
            constraints.append(item)
            constraint_remaining -= len(item)
    plan_remaining = snapshot_limit - len(root_goal) - sum(map(len, constraints))
    plan = []
    for step in task["state"]["plan"]:
        size = len(step["id"]) + len(step["status"])
        if size > plan_remaining:
            break
        plan.append({"id": step["id"], "status": step["status"]})
        plan_remaining -= size
    snapshot = {"root_goal": root_goal, "constraints": constraints,
                "plan": plan}
    snapshot_chars = _context_text_chars(snapshot)

    facts = []
    fact_remaining = fact_limit
    for fact in list_facts(task_id):
        if fact["stale"] or fact_remaining <= 0 or len(facts) >= 10:
            continue
        metadata_chars = _context_text_chars({**fact, "value": "", "evidence": "", "label": "FACT"})
        if metadata_chars >= fact_remaining:
            continue
        fact_remaining -= metadata_chars
        value = fact["value"][:fact_remaining]
        fact_remaining -= len(value)
        evidence = fact["evidence"][:fact_remaining]
        fact_remaining -= len(evidence)
        facts.append({**fact, "value": value, "evidence": evidence, "label": "FACT"})
    fact_chars = fact_limit - fact_remaining

    memories: list[dict[str, Any]] = []
    memory_state: dict[str, Any] = {"status": "disabled", "backend": None}
    if memory and memory_limit_chars:
        step_goals = [item["goal"] for item in task["state"]["plan"] if item["status"] == "running"]
        recall_query = " | ".join(filter(None, [profile["root_goal"][:240], profile["local_goal"][:240],
                                               step_goals[0][:200] if step_goals else "",
                                               profile["task_type"][:80], query[:300]]))[:1100]
        try:
            recall = memory_recall(task_id, recall_query, memory_kinds, memory_limit, memory_limit_chars)
            memory_state = {key: value for key, value in recall.items() if key in {"status", "backend", "reason", "bank_id", "fallback", "configured", "available"}}
            memory_remaining = memory_limit_chars
            for item in recall.get("results", [])[:memory_limit]:
                if not isinstance(item, dict):
                    continue
                body = item.get("text", item.get("summary", ""))
                if not isinstance(body, str) or not body or memory_remaining <= 0:
                    continue
                memory_item = {"text": "", "kind": item.get("kind"), "score": item.get("score"),
                               "source_task_id": item.get("source_task_id"), "created_at": item.get("created_at"),
                               "label": "HISTORICAL_MEMORY"}
                metadata_chars = _context_text_chars(memory_item)
                if metadata_chars >= memory_remaining:
                    continue
                memory_remaining -= metadata_chars
                excerpt = body[:memory_remaining]
                memory_remaining -= len(excerpt)
                memories.append({**memory_item, "text": excerpt})
        except Exception as error:
            # Memory is optional context. Do not put exception details into the trace
            # or model response because HTTP errors may contain private data.
            memory_state = {"status": "unavailable", "reason": type(error).__name__, "backend": None}
    memory_chars = sum(_context_text_chars(item) for item in memories)

    experiences: list[dict[str, Any]] = []
    experience_state: dict[str, Any] = {"status": "disabled", "label": "EXPERIENCE", "untrusted": True}
    experience_chars = 0
    if experience:
        try:
            from experience_engine import search as search_experience
            experience_budget = min(experience_max_chars, max_chars * 20 // 100)
            experience_result = search_experience(task["project_id"], query, experience_scope,
                                                   experience_limit, experience_budget)
            experiences = experience_result.get("experiences", [])
            experience_state = {"status": "available", "label": "EXPERIENCE", "untrusted": True,
                                "scope": experience_scope, "count": len(experiences)}
            experience_chars = _context_text_chars(experiences)
        except Exception as error:
            experience_state = {"status": "unavailable", "reason": type(error).__name__,
                                "label": "EXPERIENCE", "untrusted": True}

    snapshot_state: dict[str, Any] | None = None
    if context_snapshot_id:
        try:
            from runtime_state import context_snapshot_fresh, get_context_snapshot
            snapshot_state = get_context_snapshot(context_snapshot_id)
            snapshot_state["fresh"] = context_snapshot_fresh(context_snapshot_id)
        except (RuntimeError, OSError, ValueError):
            snapshot_state = {"snapshot_id": context_snapshot_id, "fresh": False, "status": "unavailable"}

    remaining = max_chars - snapshot_chars - fact_chars - memory_chars - experience_chars
    git = {"available": False}
    git_chars = 0
    git_budget = min(max(0, remaining), max_chars // 10)
    try:
        _no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3, check=True, creationflags=_no_win)
        status = subprocess.run(["git", "status", "--short"], cwd=root, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3, check=True, creationflags=_no_win)
        git = {"available": True, "revision": None, "changed_paths": []}
        revision_text = revision.stdout.strip()
        if len(revision_text) <= git_budget:
            git["revision"] = revision_text
            git_budget -= len(revision_text)
        for path in status.stdout.splitlines()[:50]:
            if len(path) > git_budget:
                break
            git["changed_paths"].append(path)
            git_budget -= len(path)
        git_chars = _context_text_chars(git)
        remaining -= git_chars
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass

    selected = []
    source_metadata_chars = 0
    for relative in candidates:
        try:
            source = _safe_source(root, relative)
            body = source.read_text(encoding="utf-8")
        except (RuntimeError, UnicodeDecodeError, OSError):
            continue
        metadata = {"path": source.relative_to(root).as_posix(),
                    "sha256": sha256(source.read_bytes()).hexdigest(), "label": "REFERENCE"}
        overhead = _context_text_chars(metadata)
        if remaining <= overhead:
            break
        excerpt = body[:min(remaining - overhead, 5000)]
        remaining -= overhead + len(excerpt)
        source_metadata_chars += overhead
        selected.append({**metadata, "excerpt": excerpt, "truncated": len(excerpt) < len(body)})
    record_event("context_loaded", {"task_id": task_id, "paths": [item["path"] for item in selected], "characters": max_chars - remaining, "jev_status": jev_status})
    return {"task_id": task_id, "project_id": task["project_id"], "task_snapshot": snapshot, "git": git,
            "sources": selected, "facts": facts, "memory": memories, "memory_status": memory_state,
            "experience": experiences, "experience_status": experience_state, "context_snapshot": snapshot_state,
            "context_budget": {"max_chars": max_chars, "used_chars": max_chars - remaining,
                               "snapshot_chars": snapshot_chars, "fact_chars": fact_chars,
                               "memory_chars": memory_chars, "experience_chars": experience_chars,
                               "source_chars": sum(len(item["excerpt"]) for item in selected),
                               "source_metadata_chars": source_metadata_chars, "git_chars": git_chars},
            "jev_status": jev_status, "unresolved": not bool(selected or facts or memories)}


def record_context_refs(task_id: str, expected_revision: int, sources: list[dict[str, Any]]) -> dict[str, Any]:
    task = get_task(task_id)
    if task["revision"] != expected_revision:
        raise RuntimeError("Task revision changed; reload before writing")
    if not isinstance(sources, list) or len(sources) > 50:
        raise RuntimeError("Too many context references")
    refs = []
    root = Path(task["project_root"])
    for item in sources:
        source = _safe_source(root, item["path"])
        digest = sha256(source.read_bytes()).hexdigest()
        if item.get("sha256") != digest:
            raise RuntimeError("Source changed since context loading")
        refs.append({"path": source.relative_to(root).as_posix(), "sha256": digest})
    task["state"]["context_refs"] = refs
    return update_task(task_id, expected_revision, state=task["state"], event="context_refs_saved")


def verify(task_id: str, checks: list[dict[str, Any]], semantic_evidence: str | None = None, strong_review: str | None = None) -> dict[str, Any]:
    task = get_task(task_id)
    root = Path(task["project_root"])
    if not isinstance(checks, list) or len(checks) > 100:
        raise RuntimeError("Invalid verification checks")
    results = []
    for check in checks:
        if not isinstance(check, dict):
            raise RuntimeError("Each verification check must be an object")
        kind = check.get("kind")
        if kind == "file_exists":
            path = (root / check.get("path", "")).resolve()
            passed = path.is_relative_to(root) and path.is_file()
        elif kind == "exit_code":
            actual = check.get("actual")
            expected = check.get("expected", 0)
            passed = (
                isinstance(actual, int)
                and not isinstance(actual, bool)
                and isinstance(expected, int)
                and not isinstance(expected, bool)
                and actual == expected
            )
        elif kind == "value_equals":
            passed = check.get("actual") == check.get("expected")
        else:
            raise RuntimeError("Unknown verification check")
        results.append({"kind": kind, "passed": passed, "label": str(check.get("label", kind))[:100], "criterion": check.get("criterion")})
    risky = task["state"]["profile"].get("risk") == "high" or task["state"]["profile"].get("architecture_change") is True
    covered = {item["criterion"] for item in results if item["passed"] and isinstance(item["criterion"], str)}
    missing = [criterion for criterion in task["state"]["profile"]["acceptance_criteria"] if criterion not in covered]
    semantic: dict[str, Any] | None = None
    if semantic_evidence:
        try:
            semantic = check_verification(task["state"]["profile"]["root_goal"], semantic_evidence)
        except JevUnavailable as error:
            semantic = {"verdict": "unavailable", "reason": str(error)}
    status = "failed" if not results or any(not x["passed"] for x in results) or missing else "needs_review" if (risky and not strong_review) or (semantic and semantic["verdict"] in {"unsupported", "uncertain", "unavailable"} and not strong_review) else "passed"
    report = {"status": status, "algorithmic": results, "missing_criteria": missing, "semantic_review": semantic, "strong_review": strong_review, "requires_strong_review": risky, "verified_at_revision": task["revision"]}
    task["state"]["verification"].append(report)
    updated = update_task(task_id, task["revision"], state=task["state"], event="verification")
    report["revision"] = updated["revision"]
    return report


def context_refs_fresh(task_id: str) -> bool:
    task = get_task(task_id)
    root = Path(task["project_root"])
    for ref in task["state"]["context_refs"]:
        try:
            source = _safe_source(root, ref["path"])
            if sha256(source.read_bytes()).hexdigest() != ref["sha256"]:
                return False
        except (RuntimeError, OSError):
            return False
    return True


def record_usage(
    task_id: str,
    expected_revision: int,
    input_tokens: int,
    output_tokens: int,
    runtime_ms: int,
    budget_tokens: int | None = None,
    current_context_tokens: int | None = None,
    *,
    context_loading_budget_tokens: int | None = None,
    host_context_window_tokens: int | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    task = get_task(task_id)
    if task["revision"] != expected_revision:
        raise RuntimeError("Task revision changed; reload before writing")
    for value in (input_tokens, output_tokens, runtime_ms):
        if not isinstance(value, int) or value < 0:
            raise RuntimeError("Usage values must be nonnegative integers")
    if budget_tokens is not None and (not isinstance(budget_tokens, int) or budget_tokens <= 0):
        raise RuntimeError("budget_tokens must be positive")
    if context_loading_budget_tokens is None:
        context_loading_budget_tokens = budget_tokens
    if context_loading_budget_tokens is not None and (not isinstance(context_loading_budget_tokens, int) or context_loading_budget_tokens <= 0):
        raise RuntimeError("context_loading_budget_tokens must be positive")
    if host_context_window_tokens is not None and (not isinstance(host_context_window_tokens, int) or host_context_window_tokens <= 0):
        raise RuntimeError("host_context_window_tokens must be positive")
    if model_id is not None and (not isinstance(model_id, str) or not model_id.strip()):
        raise RuntimeError("model_id must be a nonempty string")
    if current_context_tokens is not None and (not isinstance(current_context_tokens, int) or current_context_tokens < 0):
        raise RuntimeError("current_context_tokens must be nonnegative")
    usage = task["state"]["usage"]
    usage["input_tokens"] += input_tokens
    usage["output_tokens"] += output_tokens
    usage["runtime_ms"] += runtime_ms
    pressure_reason = "host" if current_context_tokens is not None and host_context_window_tokens is not None else "missing_host_measurement"
    if current_context_tokens is not None and host_context_window_tokens is not None and current_context_tokens > host_context_window_tokens:
        pressure_reason = "host_values_conflict"
    ratio = (current_context_tokens / host_context_window_tokens
             if current_context_tokens is not None and host_context_window_tokens is not None
             and current_context_tokens <= host_context_window_tokens else None)
    action = "unknown_pressure" if ratio is None else "checkpoint_and_compact" if ratio >= .9 else "checkpoint_when_safe" if ratio >= .7 else "continue"
    if ratio is not None:
        task["state"]["compact_pending"] = action in {"checkpoint_when_safe", "checkpoint_and_compact"}
    if context_loading_budget_tokens is not None:
        usage["context_loading_budget_tokens"] = context_loading_budget_tokens
    if host_context_window_tokens is not None:
        usage["host_context_window_tokens"] = host_context_window_tokens
    if current_context_tokens is not None:
        usage["current_context_tokens"] = current_context_tokens
    if model_id is not None:
        usage["model_id"] = model_id
    usage["context_pressure"] = round(ratio, 3) if ratio is not None else None
    usage["context_pressure_source"] = "host" if ratio is not None else "unknown"
    usage["context_pressure_reason"] = pressure_reason
    updated = update_task(task_id, expected_revision, state=task["state"], event="usage_recorded")
    if action == "checkpoint_and_compact":
        checkpoint(task_id, "hard_context_limit")
    return {
        "task_id": task_id,
        "revision": updated["revision"],
        "usage": usage,
        "budget_tokens": context_loading_budget_tokens,
        "context_loading_budget_tokens": context_loading_budget_tokens,
        "host_context_window_tokens": host_context_window_tokens,
        "model_id": model_id,
        "current_context_tokens": current_context_tokens,
        "ratio": round(ratio, 3) if ratio is not None else None,
        "context_pressure": round(ratio, 3) if ratio is not None else None,
        "context_pressure_source": "host" if ratio is not None else "unknown",
        "context_pressure_reason": pressure_reason,
        "action": action,
    }
