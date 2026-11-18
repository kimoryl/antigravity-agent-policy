"""Deterministic supervision shared by MCP and Hooks through runtime.sqlite3."""

from __future__ import annotations

import fnmatch
import hashlib
import html
import itertools
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from policy_engine import load_policy
from policy_state import data_dir
from runtime_state import canonical_project_root

NATIVE_DELEGATION = {"subagent", "subagent_fork", "subagent_codex", "subagent_claude_code",
                     "spawn_teammate", "Agent", "spawn_agent", "invoke_subagent"}
SHELL_TOOLS = {"bash", "Bash", "pwsh", "exec_command", "run_code", "run_command"}
VERIFY_COMMAND = (r"\b(pytest|unittest|vitest|jest|npm\s+test|pnpm\s+(run\s+)?(test|typecheck|lint|build)"
                  r"|cargo\s+test|go\s+test|py\s+-3\s+-m\s+unittest"
                  r"|python\d*(\.exe)?\s+-m\s+(unittest|pytest))\b")
EXEMPT_MUTATION_FILES = {"description.md", "comment.md", "reply.md", "agents.md"}
_local = threading.local()
SCHEMA = """
CREATE TABLE IF NOT EXISTS advisory_queue (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, source TEXT NOT NULL,
 level TEXT NOT NULL CHECK(level IN ('info','warn','critical')), message TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_advisory_session ON advisory_queue(session_id,id);
CREATE UNIQUE INDEX IF NOT EXISTS advisory_dedup ON advisory_queue(session_id,source,message);
CREATE TABLE IF NOT EXISTS telemetry_window (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, tool_name TEXT NOT NULL,
 sig_hash TEXT NOT NULL, is_mutate INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_telemetry_session ON telemetry_window(session_id,id);
CREATE TABLE IF NOT EXISTS evidence_ledger (
 session_id TEXT NOT NULL, target_path TEXT NOT NULL, mutated_at REAL NOT NULL,
 verified INTEGER NOT NULL DEFAULT 0, verified_at REAL, PRIMARY KEY(session_id,target_path));
CREATE TABLE IF NOT EXISTS session_gate_state (
 session_id TEXT PRIMARY KEY, last_verify_passed INTEGER NOT NULL DEFAULT 1,
 stop_reject_count INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS session_touched_files (
 session_id TEXT NOT NULL, file_path TEXT NOT NULL, touch_kind TEXT NOT NULL CHECK(touch_kind IN ('read','mutate')),
 workspace_id TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL DEFAULT 0,
 PRIMARY KEY(session_id,file_path));
CREATE TABLE IF NOT EXISTS co_occurrence_edges (
 workspace_id TEXT NOT NULL, file_a TEXT NOT NULL, file_b TEXT NOT NULL,
 weight REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL, PRIMARY KEY(workspace_id,file_a,file_b));
CREATE INDEX IF NOT EXISTS graph_reverse ON co_occurrence_edges(workspace_id,file_b,weight);
CREATE TABLE IF NOT EXISTS supervisor_sessions (session_id TEXT PRIMARY KEY, workspace TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending_verifications (
 session_id TEXT NOT NULL, call_id TEXT NOT NULL, started_at REAL NOT NULL, PRIMARY KEY(session_id,call_id));
"""


def config(policy: dict | None = None) -> dict:
    return (policy or load_policy())["supervisor"]


def close_connections() -> None:
    """Release the connection owned by the current explicit database session."""
    connection = getattr(_local, "connection", None)
    if connection is not None:
        connection.close()
        _local.connection = None


@contextmanager
def database_session():
    """Reuse a connection for one Hook invocation or MCP server lifetime, then close it."""
    if getattr(_local, "active", False):
        yield
        return
    _local.active = True
    try:
        yield
    finally:
        close_connections()
        _local.active = False


@contextmanager
def _db():
    path = str(data_dir() / "runtime.sqlite3")
    connection = getattr(_local, "connection", None)
    try:
        if connection is None or getattr(_local, "path", None) != path:
            close_connections()
            connection = sqlite3.connect(path, timeout=1.0, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='pending_verifications'").fetchone():
                connection.executescript(SCHEMA)
            touched_cols = {row[1] for row in connection.execute("PRAGMA table_info(session_touched_files)").fetchall()}
            if touched_cols and "workspace_id" not in touched_cols:
                connection.execute("ALTER TABLE session_touched_files ADD COLUMN workspace_id TEXT NOT NULL DEFAULT ''")
            if touched_cols and "updated_at" not in touched_cols:
                connection.execute("ALTER TABLE session_touched_files ADD COLUMN updated_at REAL NOT NULL DEFAULT 0")
            if getattr(_local, "active", False):
                _local.connection, _local.path = connection, path
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.Error:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        if not getattr(_local, "active", False):
            connection.close()


def _push(db, session_id: str, source: str, level: str, message: str) -> None:
    db.execute("INSERT OR IGNORE INTO advisory_queue(session_id,source,level,message,created_at) VALUES(?,?,?,?,?)",
               (session_id, source, level, message, time.time()))


def push_advisory(session_id: str, source: str, level: str, message: str) -> None:
    if level not in {"info", "warn", "critical"}:
        raise ValueError("Invalid advisory level")
    with _db() as db:
        _push(db, session_id, source, level, message)


def flush_advisories(session_id: str) -> str | None:
    with _db() as db:
        rows = db.execute("SELECT * FROM advisory_queue WHERE session_id=? ORDER BY "
                          "CASE level WHEN 'critical' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END,id", (session_id,)).fetchall()
        db.execute("DELETE FROM advisory_queue WHERE session_id=?", (session_id,))
    return "\n".join(f'<system-reminder source="{html.escape(r["source"], quote=True)}" level="{r["level"]}">\n'
                     f'{html.escape(r["message"])}\n</system-reminder>' for r in rows) or None


def canonical_sig_hash(tool_name: str, tool_input: dict[str, Any]) -> str:
    canonical = json.dumps(tool_input or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(f"{tool_name}:{canonical}".encode("utf-8")).hexdigest()[:16]


def short_tool(tool_name: str) -> str:
    return tool_name.rsplit("__", 1)[-1]


def matches(tool_name: str, patterns: list[str]) -> bool:
    name = short_tool(tool_name).lower()
    return any(fnmatch.fnmatchcase(name, p.lower()) or name.endswith("_" + p.lower()) for p in patterns)


def _extract_command(args: dict) -> str:
    return str(args.get("command", args.get("cmd", args.get("code", args.get("CommandLine", "")))))


def _extract_path(args: dict, default: str = "workspace") -> str:
    return str(args.get("path", args.get("file_path", args.get("target_file", args.get("Path", default)))))


def _is_exempt_target(target_path: str) -> bool:
    name = Path(target_path).name.casefold()
    return name in EXEMPT_MUTATION_FILES or name.startswith(".agy-state")


def is_mutation(tool_name: str, args: dict, settings: dict) -> bool:
    if matches(tool_name, settings["mutate_tool_patterns"]):
        if short_tool(tool_name) == "str_replace_editor" and args.get("command") == "view":
            return False
        target = _extract_path(args, "")
        if target and _is_exempt_target(target):
            return False
        return True
    command = _extract_command(args).strip()
    if not command or tool_name not in SHELL_TOOLS:
        return False
    if any(re.search(rf"(?i)\b{re.escape(exempt)}\b", command) for exempt in EXEMPT_MUTATION_FILES):
        return False
    # Strip harmless null redirections (e.g. 2>$null, >$null, >/dev/null, 2>NUL) before checking file-write operators
    cleaned = re.sub(r"(?:\d+)?>>?\s*(?:\$null|/dev/null|NUL)\b", "", command, flags=re.I)
    return bool(re.search(
        r"(?i)(write_text|write_bytes|Set-Content|Add-Content|Out-File|apply_patch|\b(sed\s+-i|tee|touch|mkdir|rm|mv|cp)\b|(?<![>])>(?![>&]))", cleaned))


def is_verification(tool_name: str, args: dict, settings: dict) -> bool:
    if matches(tool_name, settings["verify_tool_patterns"]):
        return True
    command = _extract_command(args)
    patterns = settings.get("verify_command_patterns", [VERIFY_COMMAND])
    if isinstance(patterns, str):
        patterns = [patterns]
    if re.match(r"\s*(echo|printf|Write-Output)\b", command, re.I):
        return False
    return tool_name in SHELL_TOOLS and any(re.search(p, command, re.I) for p in patterns)


RING0_PROTECTED_BASENAMES = {"default.json", "agy_policy_hook.py", "policy_hook.py", "supervisor_engine.py"}


def check_ring0_mutation_guard(tool_name: str, args: dict, settings: dict) -> str | None:
    if os.environ.get("AGY_ALLOW_RING0_EDIT") == "1":
        return None
    if not is_mutation(tool_name, args, settings):
        return None
    target = _extract_path(args, "").replace("\\", "/")
    command = _extract_command(args).replace("\\", "/")
    combined = f"{target}\n{command}"
    if re.search(r"(?i)(?:^|[\s/\"'])\.git/(?:hooks(?:/|$|\b)|config\b)", combined):
        return "[RING0_GUARD_BLOCKED] 严禁修改 .git/hooks 或 .git/config（防止 Git Hook 延迟执行逃逸）！"
    if os.environ.get("AGY_SUBAGENT") == "1" or os.environ.get("AGY_CLI_WORKER") == "1":
        if any(name in combined for name in RING0_PROTECTED_BASENAMES):
            return "[RING0_GUARD_BLOCKED] 受限子代理禁止篡改 Ring-0 策略与监督内核文件（防止 Self-Loosening 自我松绑）！"
    return None


def pre_tool(session_id: str, tool_name: str, args: dict, call_id: str = "", policy: dict | None = None) -> str | None:
    settings = config(policy)
    if tool_name in NATIVE_DELEGATION or matches(tool_name, settings["transparent_tools"]):
        return None
    ring0_reason = check_ring0_mutation_guard(tool_name, args, settings)
    if ring0_reason:
        with _db() as db:
            _push(db, session_id, "ring0-guard", "critical", ring0_reason)
        return ring0_reason
    signature = canonical_sig_hash(tool_name, args)
    with _db() as db:
        rows = db.execute("SELECT sig_hash,is_mutate FROM telemetry_window WHERE session_id=? ORDER BY id DESC LIMIT ?",
                          (session_id, settings["window_size"])).fetchall()
        count = sum(r["sig_hash"] == signature for r in rows)
        if count >= settings["max_duplicate_tool_calls"] - 1:
            reason = f'工具 "{tool_name}" 已在滑动窗口内使用完全相同的参数调用 {count + 1} 次，已被强制熔断，请立即修改参数或更换解决路径！'
            _push(db, session_id, "circuit-breaker", "critical", reason)
            return reason
        mutation = int(is_mutation(tool_name, args, settings))
        if mutation:
            target = _extract_path(args, "")
            if target and not _is_exempt_target(target):
                workspace, normalized = _path(db, session_id, target)
                conflicts = db.execute(
                    "SELECT session_id FROM session_touched_files WHERE workspace_id=? AND file_path=? AND session_id!=? AND touch_kind='mutate' AND updated_at>=?",
                    (workspace, normalized, session_id, time.time() - 600),
                ).fetchall()
                if conflicts:
                    others = ", ".join(sorted({r["session_id"] for r in conflicts}))
                    _push(
                        db,
                        session_id,
                        "conflict-guard",
                        "warn",
                        f'[跨会话写冲突预警] 文件 "{normalized}" 在最近 10 分钟内正被其它活跃会话 ({others}) 修改且尚未提交，请注意协调或先使用 hash_read 核对最新哈希锚点！',
                    )
        db.execute("INSERT INTO telemetry_window(session_id,tool_name,sig_hash,is_mutate,created_at) VALUES(?,?,?,?,?)",
                   (session_id, tool_name, signature, mutation, time.time()))
        db.execute("DELETE FROM telemetry_window WHERE session_id=? AND id NOT IN "
                   "(SELECT id FROM telemetry_window WHERE session_id=? ORDER BY id DESC LIMIT ?)",
                   (session_id, session_id, settings["window_size"]))
        n = settings["max_read_only_turns"]
        recent = [mutation] + [r["is_mutate"] for r in rows]
        if len(recent) >= n and not any(recent[:n]):
            _push(db, session_id, "momentum", "warn", f"已连续 {n} 轮仅执行只读探查（动量 Momentum = 0）。请停止空转探查，立即推进实质性变更或输出阶段结论。")
        if is_verification(tool_name, args, settings):
            db.execute("INSERT OR REPLACE INTO pending_verifications VALUES(?,?,?)", (session_id, call_id, time.time()))
    return None


def reset_telemetry_on_prompt(session_id: str) -> None:
    with _db() as db:
        db.execute("DELETE FROM telemetry_window WHERE session_id=?", (session_id,))
        db.execute("DELETE FROM pending_verifications WHERE session_id=?", (session_id,))
        db.execute("UPDATE session_gate_state SET stop_reject_count=0 WHERE session_id=?", (session_id,))


def set_workspace(session_id: str, cwd: str | None) -> None:
    if cwd:
        with _db() as db:
            db.execute("INSERT OR REPLACE INTO supervisor_sessions VALUES(?,?)", (session_id, str(Path(cwd).resolve())))


def _workspace(db, session_id: str) -> Path:
    row = db.execute("SELECT workspace FROM supervisor_sessions WHERE session_id=?", (session_id,)).fetchone()
    return Path(row[0]) if row else Path.cwd().resolve()


def _workspace_key(root: Path) -> str:
    try:
        return str(canonical_project_root(root)).casefold()
    except Exception:
        return str(root.resolve()).casefold()


def _path(db, session_id: str, path: str) -> tuple[str, str]:
    root = _workspace(db, session_id)
    absolute = (root / path).resolve()
    relative = absolute.relative_to(root).as_posix() if absolute.is_relative_to(root) else absolute.as_posix()
    return _workspace_key(root), relative


def resolve_file(session_id: str, path: str) -> Path:
    """Resolve MCP relative paths against the workspace supplied by the host Hook."""
    with _db() as db:
        return (_workspace(db, session_id) / path).resolve()


def _touch(db, session_id: str, path: str, kind: str) -> str:
    workspace, normalized = _path(db, session_id, path)
    now = time.time()
    db.execute(
        "INSERT INTO session_touched_files(session_id,file_path,touch_kind,workspace_id,updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(session_id,file_path) DO UPDATE SET "
        "touch_kind=CASE WHEN excluded.touch_kind='mutate' THEN 'mutate' ELSE touch_kind END,"
        "workspace_id=excluded.workspace_id,updated_at=excluded.updated_at",
        (session_id, normalized, kind, workspace, now),
    )
    return normalized


def mark_mutated(session_id: str, target_path: str) -> None:
    if _is_exempt_target(target_path):
        return
    with _db() as db:
        path = _touch(db, session_id, target_path, "mutate")
        now = time.time()
        db.execute("INSERT INTO evidence_ledger VALUES(?,?,?,0,NULL) ON CONFLICT(session_id,target_path) "
                   "DO UPDATE SET mutated_at=excluded.mutated_at,verified=0,verified_at=NULL", (session_id, path, now))
        db.execute("INSERT INTO session_gate_state VALUES(?,0,0,?) ON CONFLICT(session_id) "
                   "DO UPDATE SET last_verify_passed=0,updated_at=excluded.updated_at", (session_id, now))


def mark_verified(session_id: str, passed: bool, specific_targets: list[str] | None = None,
                  *, started_at: float | None = None) -> None:
    now = time.time()
    with _db() as db:
        prev_gate = db.execute("SELECT last_verify_passed, stop_reject_count FROM session_gate_state WHERE session_id=?", (session_id,)).fetchone()
        db.execute("INSERT INTO session_gate_state VALUES(?,?,0,?) ON CONFLICT(session_id) DO UPDATE SET "
                   "last_verify_passed=excluded.last_verify_passed,stop_reject_count=CASE WHEN excluded.last_verify_passed=1 "
                   "THEN 0 ELSE stop_reject_count END,updated_at=excluded.updated_at", (session_id, int(passed), now))
        if passed:
            sql = "UPDATE evidence_ledger SET verified=1,verified_at=? WHERE session_id=? AND mutated_at<=?"
            params: list[Any] = [now, session_id, started_at if started_at is not None else now]
            if specific_targets is not None:
                targets = [_path(db, session_id, p)[1] for p in specific_targets]
                sql += " AND target_path IN (" + ",".join("?" for _ in targets) + ")"
                params.extend(targets)
            db.execute(sql, params)
            if prev_gate is not None and (prev_gate["stop_reject_count"] > 0 or prev_gate["last_verify_passed"] == 0):
                _push(
                    db,
                    session_id,
                    "return-path",
                    "info",
                    "[Return Path 经验沉淀提示] 检测到当前会话已完成修改并通过验证闭环。若本次涉及典型排障或架构改进，可调用 runtime_experience(action='propose') 沉淀结构化经验。",
                )


def get_related(path: str, top_k: int = 3, session_id: str = "default", policy: dict | None = None) -> list[dict]:
    settings = config(policy)
    with _db() as db:
        workspace, normalized = _path(db, session_id, path)
        rows = db.execute("SELECT CASE WHEN file_a=? THEN file_b ELSE file_a END AS path,weight "
                          "FROM co_occurrence_edges WHERE workspace_id=? AND (file_a=? OR file_b=?) AND weight>=? "
                          "ORDER BY weight DESC,path LIMIT ?", (normalized, workspace, normalized, normalized,
                                                               settings["co_occurrence_min_weight"], top_k)).fetchall()
    return [dict(row) for row in rows]


def record_read(session_id: str, path: str, policy: dict | None = None) -> list[dict]:
    with _db() as db:
        normalized = _touch(db, session_id, path, "read")
    related = get_related(path, session_id=session_id, policy=policy)
    if related:
        push_advisory(session_id, "co-occurrence", "info",
                      f'[共现图谱提示] 读取 "{normalized}" 时，历史变更通常伴随关联文件: '
                      + ", ".join(r["path"] for r in related) + "。如涉及接口或逻辑联动请一并检查。")
    return related


def _commit_session(db, session_id: str, settings: dict) -> bool:
    debt = db.execute("SELECT 1 FROM evidence_ledger WHERE session_id=? AND verified=0 LIMIT 1", (session_id,)).fetchone()
    gate = db.execute("SELECT last_verify_passed FROM session_gate_state WHERE session_id=?", (session_id,)).fetchone()
    if debt or (gate and not gate[0]):
        return False
    touched = db.execute("SELECT * FROM session_touched_files WHERE session_id=? ORDER BY file_path", (session_id,)).fetchall()
    if not any(row["touch_kind"] == "mutate" for row in touched):
        return False
    workspace = _workspace_key(_workspace(db, session_id))
    now = time.time()
    db.execute("UPDATE co_occurrence_edges SET weight=weight*?,updated_at=? WHERE workspace_id=?",
               (settings["co_occurrence_decay"], now, workspace))
    db.execute("DELETE FROM co_occurrence_edges WHERE workspace_id=? AND weight<0.05", (workspace,))
    for a, b in itertools.combinations(touched, 2):
        count = int(a["touch_kind"] == "mutate") + int(b["touch_kind"] == "mutate")
        if count:
            db.execute("INSERT INTO co_occurrence_edges VALUES(?,?,?,?,?) ON CONFLICT(workspace_id,file_a,file_b) "
                       "DO UPDATE SET weight=weight+excluded.weight,updated_at=excluded.updated_at",
                       (workspace, a["file_path"], b["file_path"], 1.5 if count == 2 else 1.0, now))
    db.execute("DELETE FROM session_touched_files WHERE session_id=?", (session_id,))
    return True


def commit_session(session_id: str, policy: dict | None = None) -> bool:
    with _db() as db:
        return _commit_session(db, session_id, config(policy))


def check_delivery_gate(session_id: str, policy: dict | None = None) -> dict:
    settings = config(policy)
    with _db() as db:
        debt = [r[0] for r in db.execute("SELECT target_path FROM evidence_ledger WHERE session_id=? AND verified=0 ORDER BY target_path", (session_id,))]
        gate = db.execute("SELECT * FROM session_gate_state WHERE session_id=?", (session_id,)).fetchone()
        if debt or (gate and not gate["last_verify_passed"]):
            count = gate["stop_reject_count"] if gate else 0
            if settings["strict_evidence_gate"] and count < settings["max_stop_rejections"]:
                db.execute("INSERT INTO session_gate_state VALUES(?,0,1,?) ON CONFLICT(session_id) DO UPDATE SET "
                           "stop_reject_count=stop_reject_count+1", (session_id, time.time()))
                return {"decision": "block", "reason": "[EVIDENCE_GATE_REJECTED] 存在未验证的变更目标: "
                        + json.dumps(debt, ensure_ascii=False) + "，或最近一次校验未通过。请先运行测试/校验工具或 runtime_verify 验证修改！"}
            return {}
        _commit_session(db, session_id, settings)
    return {}


def response_status(response: Any) -> tuple[bool, int | None, dict]:
    """Unwrap CLI/MCP output; absence of an error is insufficient evidence of verification."""
    payload = response
    failed = False
    texts = []
    exit_code = None
    for _ in range(6):
        if isinstance(payload, str):
            try:
                decoded = json.loads(payload)
            except ValueError:
                decoded = None
            if isinstance(decoded, dict):
                payload = decoded
        if not isinstance(payload, dict):
            texts.append(str(payload))
            break
        failed |= bool(payload.get("isError") or payload.get("error") or payload.get("ok") is False)
        code = payload.get("exit_code", payload.get("exitCode"))
        if type(code) is int:
            exit_code = code
        texts.extend(str(payload[k]) for k in ("output", "stdout", "stderr", "text") if k in payload)
        if "structuredContent" in payload:
            payload = payload["structuredContent"]
        elif "result" in payload:
            payload = payload["result"]
        elif isinstance(payload.get("content"), list):
            texts.extend(c.get("text", "") for c in payload["content"] if isinstance(c, dict))
            try:
                nested = json.loads(texts[-1])
            except (ValueError, IndexError):
                break
            if not isinstance(nested, dict):
                break
            payload = nested
        else:
            break
    text = "\n".join(texts)
    if exit_code is None:
        code_match = re.search(r"(?:exit[_ ]code|exited with code)\s*[:=]?\s*(-?\d+)", text, re.I)
        if code_match:
            exit_code = int(code_match[1])
    failed |= exit_code is not None and exit_code != 0
    failed |= bool(re.search(r"(?im)(\b[1-9]\d*\s+(?:failed|errors?)\b|^FAILED\b|^FAIL\b|Traceback \(|\bError:|\"status\"\s*:\s*\"failed\")", text))
    data = payload if isinstance(payload, dict) else {}
    failed |= data.get("status") in {"failed", "needs_review", "error"}
    failed |= bool(data.get("session_id") or data.get("sessionId")) and exit_code is None
    failed |= bool(re.search(r"(?i)\[(timed out|running in background|still running)|\bError\b|\bFAIL(?:ED|URE)?\b", text))
    if exit_code is None and not failed and re.search(r"(?m)^OK(?:\s|$)|\b[1-9]\d* passed\b|Tests:\s+.*\bpassed\b", text):
        exit_code = 0
    return not failed, exit_code, data


def post_tool(session_id: str, tool_name: str, args: dict, response: Any, call_id: str = "",
              policy: dict | None = None) -> None:
    if tool_name in NATIVE_DELEGATION:
        return
    settings = config(policy)
    success, exit_code, payload = response_status(response)
    path = _extract_path(args, "workspace")
    if success and is_mutation(tool_name, args, settings):
        if tool_name not in SHELL_TOOLS or exit_code == 0:
            paths = re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", str(args.get("patch", args.get("input", ""))), re.M)
            for target in paths or [str(path)]:
                mark_mutated(session_id, target)
    if success and (short_tool(tool_name) in {"read", "read_file", "hash_read"} or
                    (short_tool(tool_name) == "str_replace_editor" and args.get("command") == "view")) and path != "workspace":
        record_read(session_id, str(path), policy)
    if is_verification(tool_name, args, settings):
        with _db() as db:
            row = db.execute("SELECT started_at FROM pending_verifications WHERE session_id=? AND call_id=?", (session_id, call_id)).fetchone()
            db.execute("DELETE FROM pending_verifications WHERE session_id=? AND call_id=?", (session_id, call_id))
        if row:
            passed = success and (exit_code == 0 or (short_tool(tool_name) == "runtime_verify" and payload.get("status") == "passed"))
            mark_verified(session_id, passed, started_at=row[0])


class AdvisoryService:
    push_advisory = staticmethod(push_advisory)
    flush_advisories = staticmethod(flush_advisories)


class SessionTelemetry:
    canonical_sig_hash = staticmethod(canonical_sig_hash)
    pre_tool = staticmethod(pre_tool)
    reset_telemetry_on_prompt = staticmethod(reset_telemetry_on_prompt)


class EvidenceLedger:
    mark_mutated = staticmethod(mark_mutated)
    mark_verified = staticmethod(mark_verified)
    check_delivery_gate = staticmethod(check_delivery_gate)


class CoOccurrenceGraph:
    record_read = staticmethod(record_read)
    get_related = staticmethod(get_related)
    commit_session = staticmethod(commit_session)
