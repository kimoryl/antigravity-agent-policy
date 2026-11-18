"""Dependency-free stdio MCP server for Agent Policy & Unified Mod System."""

from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from policy_engine import PolicyError, check_tool, load_policy, resolve  # noqa: E402
    from policy_state import record_event  # noqa: E402
    from mod_manager import ModError, call_mod_tool, get_mounted_mcp_tools  # noqa: E402
    from dashboard import start_dashboard  # noqa: E402
    from runtime_state import RuntimeError as TaskRuntimeError  # noqa: E402
    from runtime_tools import TOOLS as RUNTIME_TOOLS, call as runtime_call  # noqa: E402
    from supervisor_engine import database_session, flush_advisories  # noqa: E402
except Exception:
    import traceback
    (Path(__file__).resolve().parent / "mcp_boot_error.log").write_text(traceback.format_exc(), encoding="utf-8")
    raise


SERVER_INFO = {"name": "codex-agent-policy", "version": json.loads(
    (Path(__file__).resolve().parents[1] / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
)["version"]}

TOOLS = [
    {
        "name": "policy_resolve",
        "description": "Resolve model override, reasoning effort, context budget, permissions and delegation limits for one agent role. Call before spawning an agent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "task_profile": {"type": "object"},
                "team_id": {"type": "string"},
                "retries": {"type": "integer", "minimum": 0},
                "depth": {"type": "integer", "minimum": 0},
                "failure_kind": {"type": "string"},
                "verification_failed": {"type": "boolean"},
                "user_model": {"type": "string"},
                "user_reasoning_effort": {"type": "string"},
                "available_models": {"type": "array", "items": {"type": "string"}},
                "supported_efforts": {"type": "array", "items": {"type": "string"}},
                "host_context_budget_tokens": {"type": "integer", "minimum": 1},
                "parent_permissions": {"type": "array", "items": {"type": "string"}},
                "user_allowed_permissions": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["role"]
        }
    },
    {
        "name": "policy_check_tool",
        "description": "Check a tool name against an effective policy returned by policy_resolve. This does not grant permissions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string"},
                "effective_policy": {"type": "object"}
            },
            "required": ["tool_name", "effective_policy"]
        }
    },
    {
        "name": "policy_record_result",
        "description": "Record compact outcome metadata for policy trace and escalation. Do not pass prompts, source code or secrets.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "role": {"type": "string"},
                "outcome": {"type": "string", "enum": ["success", "failure", "cancelled"]},
                "failure_kind": {"type": "string"},
                "retries": {"type": "integer", "minimum": 0}
            },
            "required": ["task_id", "role", "outcome"]
        }
    },
    {
        "name": "dashboard_open",
        "description": "Start the loopback Agent Policy dashboard and return a URL to open in the browser panel.",
        "inputSchema": {"type": "object", "properties": {}}
    }
] + RUNTIME_TOOLS


def get_dynamic_tools() -> list[dict[str, Any]]:
    """Combine core agent-policy tools with MCP tools from currently mounted Mods in deterministic order."""
    core_names = {t["name"] for t in TOOLS}
    merged = list(TOOLS)
    dynamic_extras: list[dict[str, Any]] = []
    for mtool in get_mounted_mcp_tools():
        if mtool["name"] not in core_names:
            clean_tool = {k: v for k, v in mtool.items() if not k.startswith("_")}
            dynamic_extras.append(clean_tool)
            core_names.add(mtool["name"])
    dynamic_extras.sort(key=lambda item: str(item.get("name", "")))
    return merged + dynamic_extras


def _emit_tools_list_changed() -> None:
    try:
        notif = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
        sys.stdout.write(json.dumps(notif, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def _tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    policy = load_policy()
    if name == "policy_resolve":
        result = resolve(arguments, policy)
        record_event("decision", {
            "role": result["role"],
            "status": result["status"],
            "model_id": result.get("model_id"),
            "model_override": result.get("model_override"),
            "reasoning_effort": result.get("reasoning_effort"),
            "trace": result.get("trace", []),
        })
        return result
    if name == "policy_check_tool":
        return check_tool(arguments["tool_name"], arguments["effective_policy"], policy)
    if name == "policy_record_result":
        if arguments.get("outcome") not in {"success", "failure", "cancelled"}:
            raise PolicyError("Invalid outcome")
        task_id = arguments.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise PolicyError("task_id is required")
        fields = {
            "task_id_hash": sha256(task_id.encode("utf-8")).hexdigest()[:16],
            "role": arguments.get("role") if arguments.get("role") in policy["roles"] else "unknown",
            "outcome": arguments["outcome"],
            "failure_kind": arguments.get("failure_kind") if arguments.get("failure_kind") in {"logic_error", "missing_context", "permission_denied", "tool_unavailable", "rate_limited", "verification_failed"} else None,
            "retries": arguments.get("retries") if isinstance(arguments.get("retries"), int) else None,
        }
        record_event("outcome", fields)
        return {"recorded": True}
    if name == "dashboard_open":
        return start_dashboard()
    if name in {"hash_read", "hash_outline", "hash_edit", "object_model", "cli_worker_dispatch"} or name.startswith("mod_") or name.startswith("runtime_") or name.startswith("team_"):
        import runtime_tools as _rt
        res = _rt.call(name, arguments, None)
        if name in {"mod_mount", "mod_unmount", "mod_resolve", "runtime_intake"} and arguments.get("emit_list_changed") is True:
            _emit_tools_list_changed()
        return res
    # Dynamic Mod tool hot-routing: forward any mounted (or auto-mountable) Mod tool call over stdio JSON-RPC
    return call_mod_tool(name, arguments, auto_mount=True)


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        raise ValueError("MCP message must be an object")
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        return None
    if method == "initialize":
        requested = message.get("params", {}).get("protocolVersion", "2024-11-05")
        result = {
            "protocolVersion": requested,
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": SERVER_INFO,
            "instructions": "Use mod_catalog, mod_info, mod_mount, mod_unmount, mod_resolve, and mod_call to dynamically hot-mount and invoke encapsulated Mod packages across Agents without restarting the CLI. Use runtime_knowledge for cross-CLI shared facts and memories, and hash_read/hash_edit for anchored code edits."
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": get_dynamic_tools()}
    elif method == "tools/call":
        params = message.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("MCP params must be an object")
        try:
            call_args = params.get("arguments") or {}
            value = _tool_call(params.get("name", ""), call_args)
            is_err = isinstance(value, dict) and (
                value.get("isError") is True or value.get("status") == "error"
            )
            if isinstance(value, dict):
                sid = str(call_args.get("session_id") or "default")
                advisories = flush_advisories(sid)
                if advisories:
                    value = {**value, "_advisories": advisories}
            result = {
                "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                "structuredContent": value
            }
            if is_err:
                result["isError"] = True
        except Exception as error:
            result = {
                "isError": True,
                "content": [{"type": "text", "text": str(error)}]
            }
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _serve() -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
            response = handle(message)
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(error)}}
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()


def main() -> None:
    try:
        with database_session():
            _serve()
    except Exception as exc:
        import traceback
        err_log = Path(__file__).resolve().parent / "mcp_boot_error.log"
        err_log.write_text(traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    main()
