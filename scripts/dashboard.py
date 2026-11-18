"""Local, loopback-only dashboard for Codex hosts without embedded MCP Apps UI."""

from __future__ import annotations

import json
import math
import re
import secrets
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import delivery_state as deliveries

from policy_engine import PolicyError, load_policy, resolve
from agent_registry import load_agents
from policy_state import data_dir
from runtime_state import RuntimeError as TaskRuntimeError, get_task, list_facts, list_tasks
from memory_backend import memory_status
from experience_store import list_experience_metadata
from runtime_events import event_schema, read_events
from evidence_store import evidence_metrics
from module_registry import registry_snapshot


ROOT = Path(__file__).resolve().parents[1]
_server: ThreadingHTTPServer | None = None
_url: str | None = None
_memory_cache: tuple[float, dict[str, Any]] | None = None
_memory_lock = threading.Lock()
_EVENT_TEXT = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_EVENT_LABELS = frozenset({"kind", "task_id", "project_id", "role", "status", "backend", "error_type", "bank_id"})
_EVENT_NUMBERS = frozenset({"time", "latency_ms", "result_count", "source_count", "resolved_count", "unresolved_count", "skill_count", "mcp_count", "migrated", "deduplicated", "failed"})
_MEMORY_STATUS_FIELDS = frozenset({"status", "backend", "configured", "available", "bank_strategy", "fallback", "recall_enabled", "reflect_mode", "reason"})
_MEMORY_LABELS = {
    "status": {"ok", "fallback", "unavailable", "disabled", "warning"},
    "backend": {"sqlite", "hindsight", "none"},
    "bank_strategy": {"git_repository", "project_id"},
    "fallback": {"sqlite"},
    "reflect_mode": {"on_demand", "disabled", "off"},
    "reason": {"not_configured", "unavailable", "timeout", "auth_error", "bad_response",
               "bank_error", "rate_limited", "disabled", "duplicate_memory_pipeline"},
}


def _safe_event(event: dict[str, Any]) -> dict[str, Any]:
    """Expose only bounded diagnostic fields, never trace payloads or queries."""
    safe: dict[str, Any] = {}
    for key in _EVENT_LABELS:
        value = event.get(key)
        if isinstance(value, str) and _EVENT_TEXT.fullmatch(value):
            safe[key] = value
    for key in _EVENT_NUMBERS:
        value = event.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            safe[key] = value
    return safe


def _memory_health() -> dict[str, Any]:
    global _memory_cache
    with _memory_lock:
        if _memory_cache is None or time.monotonic() - _memory_cache[0] >= 5:
            try:
                status = memory_status(None)
                safe: dict[str, Any] = {}
                for key in _MEMORY_STATUS_FIELDS:
                    value = status.get(key)
                    if key in _MEMORY_LABELS and value in _MEMORY_LABELS[key]:
                        safe[key] = value
                    elif key in {"configured", "available", "recall_enabled"} and isinstance(value, bool):
                        safe[key] = value
                _memory_cache = (time.monotonic(), safe)
            except (OSError, ValueError, TypeError):
                _memory_cache = (time.monotonic(), {"status": "unavailable", "backend": "none", "available": False})
        return dict(_memory_cache[1])


def _compression_summary(task_id: str) -> dict[str, Any]:
    """Expose bounded Evidence metrics without publishing raw result content."""
    metrics = evidence_metrics(task_id)
    if metrics.get("status") != "ok":
        return {"status": "unavailable"}
    counts = ("result_count", "compressed_count", "bypass_count", "failed_count",
              "raw_chars", "compact_chars", "retrieve_count")
    summary: dict[str, Any] = {"status": "ok"}
    for key in counts:
        value = metrics.get(key)
        summary[key] = value if type(value) is int and value >= 0 else 0
    summary["compression_ratio"] = summary["compact_chars"] / summary["raw_chars"] if summary["raw_chars"] else 1.0
    expiry = metrics.get("next_expiry_at")
    summary["next_expiry_at"] = expiry if type(expiry) in (int, float) and math.isfinite(expiry) else None
    evidence_id = metrics.get("latest_evidence_id")
    summary["latest_evidence_id"] = evidence_id if isinstance(evidence_id, str) and re.fullmatch(r"ev_[0-9a-f]{32}", evidence_id) else None
    result_type = metrics.get("latest_result_type")
    allowed = {"test_output", "build_output", "lint_output", "git_diff", "search_result", "terminal_output", "mcp_result", "generic_text"}
    summary["latest_result_type"] = result_type if isinstance(result_type, str) and result_type in allowed else None
    return summary


def snapshot(_legacy_service=None) -> dict[str, Any]:
    policy = load_policy()
    memory = _memory_health()
    trace_file = data_dir() / "trace.jsonl"
    events: deque[dict[str, Any]] = deque(maxlen=50)
    memory_events: deque[dict[str, Any]] = deque(maxlen=20)
    migration: dict[str, Any] | None = None
    last_recall: dict[str, Any] | None = None
    last_retain: dict[str, Any] | None = None
    if trace_file.exists():
        with trace_file.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(raw, dict):
                    continue
                event = _safe_event(raw)
                if not event.get("kind"):
                    continue
                events.append(event)
                if event["kind"].startswith("memory_"):
                    memory_events.append(event)
                    if event["kind"] == "memory_migration":
                        migration = event
                    elif event["kind"] in {"memory_recall_completed", "memory_recall_failed"}:
                        last_recall = event
                    elif event["kind"] in {"memory_retain_completed", "memory_retain_failed"}:
                        last_retain = event
    tasks = list_tasks(20)
    experiences = list_experience_metadata(limit=100)
    return {
        "policy": {
            "roles": policy["roles"],
            "limits": policy["limits"],
            "model_policy": policy["model_policy"],
        },
        "agent_registry": load_agents()["roles"],
        "modules": registry_snapshot(),
        "events": list(events),
        "memory_status": memory,
        "memory_events": list(memory_events),
        "migration_status": migration,
        "last_recall": last_recall,
        "last_retain": last_retain,
        "experiences": experiences,
        "runtime_event_schema": event_schema(),
        "tasks": [{"task_id": task["task_id"], "project_id": task["project_id"], "root_goal": task["state"]["profile"]["root_goal"], "revision": task["revision"], "status": task["status"], "plan_steps": len(task["state"]["plan"]), "done_steps": sum(step["status"] == "done" for step in task["state"]["plan"]), "usage": task["state"]["usage"], "updated_at": task["updated_at"], "compression": _compression_summary(task["task_id"])} for task in tasks],
    }


def start_dashboard(_legacy_service=None) -> dict[str, Any]:
    global _server, _url
    if _server is not None:
        return {"url": _url, "scope": "loopback", "lifetime": "MCP server process"}
    token = secrets.token_urlsafe(32)
    prefix = f"/s/{token}"
    html = (ROOT / "ui" / "dashboard.html").read_bytes()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def _send(self, status: int, value: bytes, content_type: str, filename: str | None = None, download: bool = False) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(value)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            if filename:
                self.send_header("Content-Disposition", f"{'attachment' if download else 'inline'}; filename*=UTF-8''{quote(filename, safe='')}")
                self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; frame-ancestors 'none'")
            else:
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(value)

        def _json(self, status: int, value: Any) -> None:
            self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _path(self) -> str | None:
            path = urlsplit(self.path).path
            if not path.startswith(prefix + "/"):
                self._json(404, {"error": "not found"})
                return None
            return path[len(prefix):]

        def do_GET(self) -> None:
            path = self._path()
            if path == "/":
                self._send(200, html, "text/html; charset=utf-8")
            elif path in {'/delivery.js', '/delivery.css'}:
                self._send(200, (ROOT / 'ui' / path[1:]).read_bytes(), 'text/javascript; charset=utf-8' if path.endswith('.js') else 'text/css; charset=utf-8')
            elif path == "/api/state":
                try:
                    self._json(200, snapshot())
                except (OSError, ValueError, KeyError) as error:
                    self._json(500, {"error": str(error)})
            elif path == "/api/experience":
                query = parse_qs(urlsplit(self.path).query)
                try:
                    self._json(200, {"experiences": list_experience_metadata(
                        project_id=query.get("project_id", [None])[0],
                        status=query.get("status", [None])[0], limit=min(int(query.get("limit", [100])[0]), 200))})
                except (OSError, ValueError, KeyError) as error:
                    self._json(400, {"error": str(error)})
            elif path == "/api/events":
                query = parse_qs(urlsplit(self.path).query)
                try:
                    self._json(200, read_events(int(query.get("cursor", [0])[0]), min(int(query.get("limit", [100])[0]), 500),
                                                project_id=query.get("project_id", [None])[0], task_id=query.get("task_id", [None])[0],
                                                event_type=query.get("type", [None])[0]))
                except (OSError, ValueError, KeyError) as error:
                    self._json(400, {"error": str(error)})
            elif path == "/api/task":
                task_id = parse_qs(urlsplit(self.path).query).get("task_id", [""])[0]
                try:
                    self._json(200, {"task": get_task(task_id), "facts": list_facts(task_id), "delivery": deliveries.detail(task_id)})
                except TaskRuntimeError as error:
                    self._json(404, {"error": str(error)})
            elif path in {"/api/deliveries", "/api/delivery", "/api/artifact"}:
                query = parse_qs(urlsplit(self.path).query)
                try:
                    if path == "/api/deliveries":
                        self._json(200, deliveries.board(query.get('project_id', [None])[0], query.get('task_type', [None])[0]))
                    elif path == "/api/delivery":
                        self._json(200, deliveries.detail(query.get('task_id', [''])[0]))
                    else:
                        filename, content = deliveries.artifact(query.get('file_id', [''])[0])
                        download = query.get('download', ['0'])[0] == '1'
                        suffix = Path(filename).suffix.lower()
                        mime = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.pdf': 'application/pdf'}.get(suffix, 'text/plain; charset=utf-8')
                        if suffix not in {'.png', '.jpg', '.jpeg', '.webp', '.pdf', '.txt', '.md', '.json', '.csv', '.py', '.js', '.css', '.html', '.svg', '.log'}:
                            download = True
                        self._send(200, content, 'application/octet-stream' if download else mime, filename, download)
                except (TaskRuntimeError, OSError) as error:
                    self._json(404, {"error": str(error)})
            elif path is not None:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            path = self._path()
            if path is None:
                return
            if path not in {"/api/policy/resolve", "/api/delivery/review"}:
                self._json(404, {"error": "not found"})
                return
            try:
                origin = self.headers.get('Origin')
                if origin and origin != f"http://127.0.0.1:{self.server.server_port}":
                    self._json(403, {"error": "Cross-origin requests are not allowed"})
                    return
                if path == '/api/delivery/review' and self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError('JSON request required')
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 16384:
                    raise ValueError("Invalid request size")
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict):
                    raise ValueError("Request must be an object")
                if path == "/api/delivery/review":
                    value = deliveries.review(request['task_id'], request['version'], request['decision'], request.get('comment', ''))
                else:
                    value = resolve(request, load_policy())
                self._json(200, value)
            except deliveries.DeliveryConflict as error:
                self._json(409, {"error": str(error)})
            except (PolicyError, OSError, KeyError, TypeError, ValueError) as error:
                self._json(400, {"error": str(error)})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    _server = server
    _url = f"http://127.0.0.1:{server.server_port}{prefix}/"
    threading.Thread(target=server.serve_forever, daemon=True, name="agent-policy-dashboard").start()
    try:
        (data_dir() / "dashboard_url.txt").write_text(_url, encoding="utf-8")
    except OSError:
        pass
    return {"url": _url, "scope": "loopback", "lifetime": "MCP server process"}


if __name__ == "__main__":
    import webbrowser
    info = start_dashboard()
    print(f"[Agent Policy 控制台] 已启动: {info['url']}", flush=True)
    try:
        webbrowser.open(info["url"])
    except Exception:
        pass
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
