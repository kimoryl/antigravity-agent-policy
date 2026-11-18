"""Project Team state stored in the existing Runtime SQLite database.

Teams are coordination metadata around the existing task/DAG state. This module
never creates or runs an Agent; Codex remains responsible for that.
"""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

from runtime_state import RuntimeError, _db, get_task
from policy_state import record_event


TEAM_STATUSES = {"active", "complete", "cancelled"}
MAX_TEAM_DEPTH = 2
MAX_CONTRACT_BYTES = 1024
ALLOWED_REASONING_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
CLI_WORKER_MODEL_PREFIXES = (
    "gemini-4-carbon",
    "gemini-4-argon",
    "gemini-4-hydrogen",
    "gemini-3.8-flash",
    "gemini-pro-orchestrator",
    "claude-opus-5-5",
    "claude-sonnet-5-5",
)
CLI_WORKER_EXACT_MODELS = {"gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-sol"}


def is_allowed_team_child_model(model: str) -> bool:
    if not isinstance(model, str) or not model.strip():
        return False
    if model.endswith("-luna") or model in CLI_WORKER_EXACT_MODELS:
        return True
    return any(model == p or model.startswith(p + "-") for p in CLI_WORKER_MODEL_PREFIXES)


def _ensure_schema(db: Any) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS teams (
          team_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT NOT NULL,
          status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS teams_project ON teams(project_id, updated_at DESC);
        CREATE TABLE IF NOT EXISTS team_members (
          member_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, role TEXT NOT NULL,
          display_name TEXT NOT NULL, last_used_at REAL NOT NULL, use_count INTEGER NOT NULL DEFAULT 0,
          UNIQUE(project_id, role)
        );
        CREATE TABLE IF NOT EXISTS team_messages (
          message_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, team_id TEXT NOT NULL,
          from_member TEXT NOT NULL, to_member TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS team_messages_lookup ON team_messages(team_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS team_agents (
          agent_id TEXT PRIMARY KEY, team_id TEXT NOT NULL, member_id TEXT,
          parent_agent_id TEXT, role TEXT NOT NULL, depth INTEGER NOT NULL,
          model TEXT, reasoning_effort TEXT, status TEXT NOT NULL,
          created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS team_agents_tree ON team_agents(team_id, parent_agent_id, created_at);
        CREATE TABLE IF NOT EXISTS team_contracts (
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          contract_id TEXT UNIQUE NOT NULL,
          project_id TEXT NOT NULL,
          team_id TEXT NOT NULL,
          from_role TEXT NOT NULL,
          file_path TEXT NOT NULL,
          symbol TEXT NOT NULL,
          contract_json TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'published',
          created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS team_contracts_seq ON team_contracts(team_id, seq ASC);
    """)
    # Older development databases may have the pre-display-name schema.
    columns = {row[1] for row in db.execute("PRAGMA table_info(team_members)").fetchall()}
    if "display_name" not in columns:
        db.execute("ALTER TABLE team_members ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
    agent_columns = {row[1] for row in db.execute("PRAGMA table_info(team_agents)").fetchall()}
    for column, definition in (("model", "TEXT"), ("reasoning_effort", "TEXT")):
        if column not in agent_columns:
            db.execute(f"ALTER TABLE team_agents ADD COLUMN {column} {definition}")


def _display_name(role: str, display_name: str | None = None) -> str:
    names = {
        "backend": "后端", "frontend": "前端", "qa": "测试", "tester": "测试", "test": "测试",
        "planner": "项目经理", "project_manager": "项目经理", "manager": "项目经理",
        "coder": "开发", "developer": "开发", "reviewer": "审查", "architect": "架构师",
        "devops": "运维", "security": "安全", "data": "数据",
    }
    explicit = (display_name or "").strip()
    if explicit:
        return explicit[:80]
    normalized = role.strip()
    # Preserve a user-supplied Chinese role; use a Chinese neutral label for
    # dynamically added roles whose English key has no built-in translation.
    if any("\u4e00" <= char <= "\u9fff" for char in normalized):
        return normalized[:80]
    return names.get(normalized.casefold(), "成员")[:80]


def create_team(task_id: str, team_id: str | None = None) -> dict[str, Any]:
    task = get_task(task_id)
    team_id = team_id or f"team_{uuid4().hex}"
    now = time.time()
    with _db() as db:
        _ensure_schema(db)
        existing = db.execute("SELECT * FROM teams WHERE project_id = ? AND status = 'active' ORDER BY updated_at DESC LIMIT 1", (task["project_id"],)).fetchone()
        if existing:
            return dict(existing)
        db.execute("INSERT INTO teams VALUES (?, ?, ?, 'active', ?, ?)", (team_id, task["project_id"], task_id, now, now))
        row = db.execute("SELECT * FROM teams WHERE team_id = ?", (team_id,)).fetchone()
    record_event("team_created", {"team_id": team_id, "task_id": task_id, "project_id": task["project_id"]})
    return dict(row)


def get_team(team_id: str) -> dict[str, Any]:
    with _db() as db:
        _ensure_schema(db)
        row = db.execute("SELECT * FROM teams WHERE team_id = ?", (team_id,)).fetchone()
    if row is None:
        raise RuntimeError("Unknown team_id")
    return dict(row)


def list_teams(project_id: str | None = None, task_id: str | None = None) -> list[dict[str, Any]]:
    with _db() as db:
        _ensure_schema(db)
        if task_id:
            rows = db.execute("SELECT * FROM teams WHERE task_id = ? ORDER BY created_at DESC", (task_id,)).fetchall()
        elif project_id:
            rows = db.execute("SELECT * FROM teams WHERE project_id = ? ORDER BY updated_at DESC", (project_id,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM teams ORDER BY updated_at DESC LIMIT 100").fetchall()
    return [dict(row) for row in rows]


def ensure_member(project_id: str, role: str, member_id: str | None = None, display_name: str | None = None, *, touch: bool = True) -> dict[str, Any]:
    if not isinstance(project_id, str) or not project_id or not isinstance(role, str) or not role.strip():
        raise RuntimeError("project_id and role are required")
    now = time.time()
    label = _display_name(role, display_name)
    with _db() as db:
        _ensure_schema(db)
        row = db.execute("SELECT * FROM team_members WHERE project_id = ? AND role = ?", (project_id, role)).fetchone()
        if row is None:
            chosen = member_id or f"member_{uuid4().hex}"
            db.execute("INSERT INTO team_members VALUES (?, ?, ?, ?, ?, ?)", (chosen, project_id, role[:120], label, now, 1 if touch else 0))
        elif touch:
            db.execute("UPDATE team_members SET display_name = ?, last_used_at = ?, use_count = use_count + 1 WHERE member_id = ?", (label, now, row["member_id"]))
        row = db.execute("SELECT * FROM team_members WHERE project_id = ? AND role = ?", (project_id, role)).fetchone()
    assert row is not None
    return dict(row)


def list_members(project_id: str) -> list[dict[str, Any]]:
    with _db() as db:
        _ensure_schema(db)
        rows = db.execute("SELECT * FROM team_members WHERE project_id = ? ORDER BY last_used_at DESC", (project_id,)).fetchall()
    return [dict(row) for row in rows]


def register_agent(team_id: str, agent_id: str, role: str, depth: int, parent_agent_id: str | None = None, status: str = "pending", model: str | None = None, reasoning_effort: str | None = None) -> dict[str, Any]:
    team = get_team(team_id)
    if not isinstance(agent_id, str) or not agent_id or not isinstance(role, str) or not role.strip():
        raise RuntimeError("agent_id and role are required")
    if not isinstance(depth, int) or depth < 0 or depth > MAX_TEAM_DEPTH:
        raise RuntimeError("Team depth must be between 0 and 2")
    if depth == 2 and not parent_agent_id:
        raise RuntimeError("Subagent must have a parent Team Agent")
    if depth > 0 and model is not None and not is_allowed_team_child_model(model):
        raise RuntimeError("Team child agent model must be an approved CLI worker or Luna model")
    if reasoning_effort is not None and reasoning_effort not in ALLOWED_REASONING_EFFORTS:
        raise RuntimeError("Team child reasoning effort must be low, medium, high, xhigh, or max")
    if status not in {"pending", "running", "done", "failed", "cancelled", "waiting"}:
        raise RuntimeError("Invalid team agent status")
    member = ensure_member(team["project_id"], role)
    now = time.time()
    with _db() as db:
        _ensure_schema(db)
        db.execute("INSERT OR REPLACE INTO team_agents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (agent_id, team_id, member["member_id"], parent_agent_id, role[:120], depth, model, reasoning_effort, status, now, now))
        row = db.execute("SELECT * FROM team_agents WHERE agent_id = ?", (agent_id,)).fetchone()
    record_event("team_agent_registered", {"team_id": team_id, "agent_id": agent_id, "parent_agent_id": parent_agent_id, "depth": depth, "role": role, "model": model, "reasoning_effort": reasoning_effort})
    return dict(row)


def update_agent(agent_id: str, status: str) -> dict[str, Any]:
    if status not in {"pending", "running", "done", "failed", "cancelled", "waiting"}:
        raise RuntimeError("Invalid team agent status")
    with _db() as db:
        _ensure_schema(db)
        updated = db.execute("UPDATE team_agents SET status = ?, updated_at = ? WHERE agent_id = ?", (status, time.time(), agent_id)).rowcount
        row = db.execute("SELECT * FROM team_agents WHERE agent_id = ?", (agent_id,)).fetchone()
    if not updated or row is None:
        raise RuntimeError("Unknown team agent")
    record_event("team_agent_status", {"agent_id": agent_id, "status": status, "team_id": row["team_id"]})
    return dict(row)


def list_agents(team_id: str) -> list[dict[str, Any]]:
    with _db() as db:
        _ensure_schema(db)
        rows = db.execute("SELECT * FROM team_agents WHERE team_id = ? ORDER BY depth, created_at", (team_id,)).fetchall()
    return [dict(row) for row in rows]


def send_message(team_id: str, from_member: str, to_member: str, content: str, *, sender_depth: int = 1) -> dict[str, Any]:
    team = get_team(team_id)
    if not isinstance(from_member, str) or not from_member or not isinstance(to_member, str) or not to_member:
        raise RuntimeError("Message sender and recipient are required")
    if not isinstance(content, str) or not content.strip() or len(content) > 4000:
        raise RuntimeError("Message content must be 1 to 4000 characters")
    if sender_depth >= MAX_TEAM_DEPTH:
        raise RuntimeError("Subagents cannot send Team Chat messages")
    if to_member == "@member" or (to_member.startswith("@member:") and len(to_member) <= len("@member:")):
        raise RuntimeError("@member requires a member id")
    if not (to_member in {"@team", "@lead"} or to_member.startswith("@member:") or to_member.startswith("member_")):
        raise RuntimeError("Recipient must be @team, @lead, or a member id")
    message_id = f"msg_{uuid4().hex}"
    now = time.time()
    with _db() as db:
        _ensure_schema(db)
        db.execute("INSERT INTO team_messages VALUES (?, ?, ?, ?, ?, ?, ?)", (message_id, team["project_id"], team_id, from_member[:160], to_member[:160], content, now))
        row = db.execute("SELECT * FROM team_messages WHERE message_id = ?", (message_id,)).fetchone()
    record_event("team_message", {"team_id": team_id, "message_id": message_id, "from_member": from_member[:160], "to_member": to_member[:160]})
    return dict(row)


def list_messages(team_id: str, limit: int = 100) -> list[dict[str, Any]]:
    get_team(team_id)
    with _db() as db:
        _ensure_schema(db)
        rows = db.execute("SELECT * FROM team_messages WHERE team_id = ? ORDER BY created_at ASC LIMIT ?", (team_id, max(1, min(limit, 500)))).fetchall()
    return [dict(row) for row in rows]


def publish_contract(
    team_id: str,
    from_role: str,
    file_path: str,
    symbol: str,
    contract: dict[str, Any] | str,
    *,
    status: str = "published",
    sender_depth: int = 1,
) -> dict[str, Any]:
    """Publish a bounded (<= 1024 bytes) structured JSON interface contract across parallel CLI workers."""
    team = get_team(team_id)
    if not isinstance(from_role, str) or not from_role.strip():
        raise RuntimeError("from_role is required")
    if not isinstance(file_path, str) or not file_path.strip():
        raise RuntimeError("file_path is required")
    if not isinstance(symbol, str) or not symbol.strip():
        raise RuntimeError("symbol is required")
    if not isinstance(sender_depth, int) or sender_depth < 0 or sender_depth > MAX_TEAM_DEPTH:
        raise RuntimeError("sender_depth must be between 0 and 2")
    if status not in {"published", "updated", "deprecated", "verified"}:
        raise RuntimeError("Invalid contract status")

    if isinstance(contract, str):
        try:
            parsed = json.loads(contract)
        except ValueError as exc:
            raise RuntimeError("Contract string must be valid JSON") from exc
    elif isinstance(contract, dict):
        parsed = contract
    else:
        raise RuntimeError("Contract payload must be a JSON object")

    if not isinstance(parsed, dict) or not parsed:
        raise RuntimeError("Contract payload must be a non-empty JSON object")

    serialized = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    byte_len = len(serialized.encode("utf-8"))
    if byte_len > MAX_CONTRACT_BYTES:
        raise RuntimeError(f"Contract payload exceeds 1024-byte hard limit ({byte_len} bytes)")

    norm_path = file_path.replace("\\", "/").strip().strip("/")
    contract_id = f"ctr_{uuid4().hex[:16]}"
    now = time.time()
    with _db() as db:
        _ensure_schema(db)
        db.execute(
            """INSERT INTO team_contracts
               (contract_id, project_id, team_id, from_role, file_path, symbol, contract_json, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                contract_id,
                team["project_id"],
                team_id,
                from_role.strip()[:120],
                norm_path[:260],
                symbol.strip()[:160],
                serialized,
                status,
                now,
            ),
        )
        row = db.execute("SELECT * FROM team_contracts WHERE contract_id = ?", (contract_id,)).fetchone()
    record_event(
        "team_contract_published",
        {
            "team_id": team_id,
            "contract_id": contract_id,
            "seq": row["seq"],
            "file_path": norm_path[:260],
            "symbol": symbol.strip()[:160],
            "byte_len": byte_len,
        },
    )
    out = dict(row)
    out["contract"] = parsed
    return out


def read_contracts(
    team_id: str,
    *,
    since_seq: int = 0,
    file_path: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Incrementally read structured interface contracts after since_seq."""
    get_team(team_id)
    safe_since = max(0, int(since_seq))
    safe_limit = max(1, min(int(limit), 500))
    with _db() as db:
        _ensure_schema(db)
        if file_path:
            norm_path = file_path.replace("\\", "/").strip().strip("/")
            rows = db.execute(
                "SELECT * FROM team_contracts WHERE team_id = ? AND seq > ? AND file_path = ? ORDER BY seq ASC LIMIT ?",
                (team_id, safe_since, norm_path, safe_limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM team_contracts WHERE team_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
                (team_id, safe_since, safe_limit),
            ).fetchall()
    results: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        try:
            item["contract"] = json.loads(item["contract_json"])
        except ValueError:
            item["contract"] = {}
        results.append(item)
    return results


def team_snapshot(team_id: str) -> dict[str, Any]:
    team = get_team(team_id)
    return {
        "team": team,
        "members": list_members(team["project_id"]),
        "agents": list_agents(team_id),
        "messages": list_messages(team_id),
        "contracts": read_contracts(team_id),
    }
