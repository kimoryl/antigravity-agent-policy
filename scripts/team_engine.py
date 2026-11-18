"""Team-specific policy helpers and one adaptive global concurrency pool."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from policy_state import data_dir, slot_snapshot


CAPACITY_OUTCOMES = {"capacity_failure", "model_capacity", "model_at_capacity", "capacity"}
FAILURE_OUTCOMES = CAPACITY_OUTCOMES | {"rate_limit", "rate_limited", "spawn_failure", "runtime_failure"}


def pool_id(project_id: str) -> str:
    return f"team:{project_id}"


def child_model(policy: dict[str, Any], complexity: str | None, *, cli_worker: bool = False) -> dict[str, str]:
    config = policy.get("team", {})
    difficulty = complexity if complexity in {"simple", "moderate", "complex"} else "moderate"
    if cli_worker:
        cli_map = config.get(
            "cli_difficulty_models",
            {
                "simple": ("gemini-4-hydrogen-medium", "medium"),
                "moderate": ("gemini-4-hydrogen-high", "high"),
                "complex": ("gemini-4-hydrogen-xhigh", "xhigh"),
            },
        )
        pair = cli_map.get(difficulty, ("gemini-4-hydrogen-high", "high"))
        return {"model": str(pair[0]), "reasoning_effort": str(pair[1]), "complexity": difficulty}
    return {"model": str(config.get("child_model", "gpt-6-luna")), "reasoning_effort": str(config.get("difficulty_effort", {}).get(difficulty, "medium")), "complexity": difficulty}


def team_context(event: dict[str, Any]) -> dict[str, Any]:
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    depth = event.get("agent_depth", event.get("depth", 0))
    try:
        depth = max(0, int(depth))
    except (TypeError, ValueError):
        depth = 0
    profile = event.get("task_profile") if isinstance(event.get("task_profile"), dict) else tool_input.get("task_profile", {})
    if not isinstance(profile, dict):
        profile = {}
    return {
        "team_id": event.get("team_id") or tool_input.get("team_id"),
        "project_id": event.get("project_id") or tool_input.get("project_id"),
        "parent_agent_id": event.get("agent_id") or event.get("parent_agent_id") or tool_input.get("parent_agent_id"),
        "depth": depth,
        "role": event.get("agent_role") or event.get("agent_type") or tool_input.get("role"),
        "task_profile": profile,
    }


def validate_spawn(policy: dict[str, Any], event: dict[str, Any]) -> tuple[bool, str, dict[str, str]]:
    context = team_context(event)
    if not context["team_id"]:
        return True, "not_team", child_model(policy, context["task_profile"].get("complexity"))
    config = policy.get("team", {})
    if not config.get("enabled", False):
        return False, "team_disabled", child_model(policy, None)
    if context["depth"] >= int(config.get("max_depth", 2)):
        return False, "team_max_depth", child_model(policy, context["task_profile"].get("complexity"))
    selected = child_model(policy, context["task_profile"].get("complexity"))
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    if tool_input.get("model") and tool_input["model"] != selected["model"]:
        return False, "team_child_model_denied", selected
    if tool_input.get("reasoning_effort") and tool_input["reasoning_effort"] != selected["reasoning_effort"]:
        return False, "team_effort_must_follow_complexity", selected
    return True, "ready", selected


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(data_dir() / "state.sqlite3", timeout=2)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=2000")
    db.execute("""
        CREATE TABLE IF NOT EXISTS team_concurrency (
          pool_id TEXT PRIMARY KEY, current_limit INTEGER NOT NULL,
          min_limit INTEGER NOT NULL, max_limit INTEGER NOT NULL, step INTEGER NOT NULL,
          ready_work INTEGER NOT NULL DEFAULT 0, waiting INTEGER NOT NULL DEFAULT 0,
          failure_streak INTEGER NOT NULL DEFAULT 0, last_failure TEXT,
          last_capacity_failure_at REAL, consecutive_successes INTEGER NOT NULL DEFAULT 0,
          cooldown_until REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL
        )
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(team_concurrency)").fetchall()}
    for column, definition in (
        ("last_capacity_failure_at", "REAL"),
        ("consecutive_successes", "INTEGER NOT NULL DEFAULT 0"),
        ("cooldown_until", "REAL NOT NULL DEFAULT 0"),
    ):
        if column not in columns:
            db.execute(f"ALTER TABLE team_concurrency ADD COLUMN {column} {definition}")
    db.execute("""
        CREATE TABLE IF NOT EXISTS team_outcome_events (
          event_key TEXT PRIMARY KEY, pool_id TEXT NOT NULL, outcome TEXT NOT NULL,
          created_at REAL NOT NULL
        )
    """)
    db.commit()
    return db


def report_outcome(event: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any] | None:
    """Feed host completion/failure signals back into the Team limiter."""
    context = team_context(event)
    if not context.get("team_id"):
        return None
    response = event.get("tool_response")
    failure = event.get("failure_kind")
    detail = str(event.get("error") or response or "").lower()
    if "selected model is at capacity" in detail or "model_capacity" in detail:
        failure = "model_capacity"
    failed = bool(event.get("error") or failure or (isinstance(response, dict) and (response.get("isError") or response.get("error"))))
    outcome = failure if failure in FAILURE_OUTCOMES else "runtime_failure" if failed else "success"
    project_id = context.get("project_id")
    if not project_id:
        from team_state import get_team
        project_id = get_team(str(context["team_id"]))["project_id"]
    event_id = event.get("event_id") or event.get("tool_use_id") or event.get("operation_id")
    event_id = str(event_id)[:200] if event_id else None
    try:
        ready_work = max(0, int(event.get("ready_work", 1)))
    except (TypeError, ValueError):
        ready_work = 1
    try:
        waiting = max(0, int(event.get("waiting", 0)))
    except (TypeError, ValueError):
        waiting = 0
    return concurrency_status(str(project_id), policy, ready_work, waiting, outcome, event_id=event_id)


def concurrency_status(project_id: str, policy: dict[str, Any], ready_work: int = 0, waiting: int | None = None,
                       outcome: str | None = None, *, event_id: str | None = None) -> dict[str, Any]:
    config = policy.get("team", {})
    try:
        global_max = int(policy.get("limits", {}).get("max_parallel_agents", 3))
    except (TypeError, ValueError):
        global_max = 3
    try:
        configured_max = int(config.get("max_concurrency", 3))
    except (TypeError, ValueError):
        configured_max = 3
    maximum = max(1, min(configured_max, max(1, global_max)))
    try:
        configured_min = int(config.get("min_concurrency", 1))
    except (TypeError, ValueError):
        configured_min = 1
    minimum = max(1, min(configured_min, maximum))
    try:
        step = int(config.get("step", 1))
    except (TypeError, ValueError):
        step = 1
    step = max(1, min(step, maximum))
    try:
        cooldown_seconds = float(config.get("capacity_cooldown_seconds", 30))
    except (TypeError, ValueError):
        cooldown_seconds = 30.0
    cooldown_seconds = max(0.0, min(cooldown_seconds, 86400.0))
    try:
        success_threshold = int(config.get("capacity_success_threshold", 2))
    except (TypeError, ValueError):
        success_threshold = 2
    success_threshold = max(1, min(success_threshold, 100))
    key = pool_id(project_id)
    now = time.time()
    try:
        ready_value = max(0, int(ready_work))
    except (TypeError, ValueError):
        ready_value = 0
    try:
        waiting_value = max(0, int(waiting if waiting is not None else 0))
    except (TypeError, ValueError):
        waiting_value = 0
    slots_before = slot_snapshot(key)
    db = _connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM team_concurrency WHERE pool_id = ?", (key,)).fetchone()
        if row is None:
            current = minimum
            streak = 0
            last_failure = None
            last_capacity_failure_at = None
            consecutive_successes = 0
            cooldown_until = 0.0
            db.execute("""INSERT INTO team_concurrency
                (pool_id, current_limit, min_limit, max_limit, step, ready_work, waiting,
                 failure_streak, last_failure, last_capacity_failure_at, consecutive_successes,
                 cooldown_until, updated_at)
                VALUES (?, ?, ?, ?, ?, 0, 0, 0, NULL, NULL, 0, 0, ?)""", (key, current, minimum, maximum, step, now))
        else:
            current = max(minimum, min(int(row["current_limit"]), maximum))
            streak = int(row["failure_streak"])
            last_failure = row["last_failure"]
            last_capacity_failure_at = row["last_capacity_failure_at"]
            consecutive_successes = int(row["consecutive_successes"] or 0)
            cooldown_until = float(row["cooldown_until"] or 0)

        if event_id:
            event_key = f"{key}:{event_id}"
            inserted = db.execute(
                "INSERT OR IGNORE INTO team_outcome_events(event_key, pool_id, outcome, created_at) VALUES (?, ?, ?, ?)",
                (event_key, key, outcome or "success", now),
            ).rowcount
            if inserted == 0:
                # Duplicate hook feedback is a read-only observation. It must
                # not reset streaks or raise concurrency a second time.
                row = db.execute("SELECT * FROM team_concurrency WHERE pool_id = ?", (key,)).fetchone()
                if row is not None:
                    current = max(minimum, min(int(row["current_limit"]), maximum))
                    streak = int(row["failure_streak"])
                    last_failure = row["last_failure"]
                    last_capacity_failure_at = row["last_capacity_failure_at"]
                    consecutive_successes = int(row["consecutive_successes"] or 0)
                    cooldown_until = float(row["cooldown_until"] or 0)
                    db.commit()
                    slots = slot_snapshot(key)
                    return {"pool_id": key, "current_limit": current, "min_concurrency": minimum,
                            "max_concurrency": maximum, "step": step, "ready_work": ready_value,
                            "waiting": waiting_value, "actual_concurrency": min(current, ready_value),
                            "running": slots["running"], "active": slots["active"], "pending": slots["pending"],
                            "failure_streak": streak, "last_failure": last_failure,
                            "last_capacity_failure_at": last_capacity_failure_at,
                            "consecutive_successes": consecutive_successes, "cooldown_until": cooldown_until,
                            "deduplicated": True}
        if outcome in FAILURE_OUTCOMES:
            current = minimum if outcome in CAPACITY_OUTCOMES else max(minimum, current - step)
            streak += 1
            last_failure = outcome
            consecutive_successes = 0
            if outcome in CAPACITY_OUTCOMES:
                last_capacity_failure_at = now
                cooldown_until = now + cooldown_seconds
        else:
            if outcome in {"success", "capacity_recovered"}:
                streak = 0
                if now >= cooldown_until:
                    consecutive_successes += 1
                    if (consecutive_successes >= success_threshold and ready_value > current and
                            ((waiting_value > 0) or slots_before["running"] >= max(0, current - 1))):
                        current = min(maximum, current + step)
                        consecutive_successes = 0
                else:
                    consecutive_successes = 0
        actual = min(current, ready_value)
        db.execute("""UPDATE team_concurrency SET current_limit=?, min_limit=?, max_limit=?, step=?,
                     ready_work=?, waiting=?, failure_streak=?, last_failure=?,
                     last_capacity_failure_at=?, consecutive_successes=?, cooldown_until=?, updated_at=?
                     WHERE pool_id=?""", (current, minimum, maximum, step, ready_value, waiting_value,
                     streak, last_failure, last_capacity_failure_at, consecutive_successes, cooldown_until, now, key))
        db.commit()
    finally:
        db.close()
    slots = slot_snapshot(key)
    return {"pool_id": key, "current_limit": current, "min_concurrency": minimum, "max_concurrency": maximum,
            "step": step, "ready_work": ready_value, "waiting": waiting_value, "actual_concurrency": actual,
            "running": slots["running"], "active": slots["active"], "pending": slots["pending"],
            "failure_streak": streak, "last_failure": last_failure,
            "last_capacity_failure_at": last_capacity_failure_at,
            "consecutive_successes": consecutive_successes, "cooldown_until": cooldown_until,
            "deduplicated": False}
