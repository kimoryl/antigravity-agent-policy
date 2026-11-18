"""Codex / CLI hook adapter with Supervisor Engine integration."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from policy_engine import PolicyError, check_tool, load_policy, resolve  # noqa: E402
from policy_state import (  # noqa: E402
    clear_session,
    mark_started,
    mark_stopped,
    record_event,
    release_slot,
    reserve_slot,
)
from supervisor_engine import (  # noqa: E402
    NATIVE_DELEGATION,
    check_delivery_gate,
    database_session,
    flush_advisories,
    post_tool,
    pre_tool,
    reset_telemetry_on_prompt,
    set_workspace,
)
from runtime_state import active_session_task, bind_task_session, checkpoint, latest_session_task  # noqa: E402
from result_optimizer import optimize_result  # noqa: E402

EXT_SHELL_TOOLS = {"bash", "pwsh", "run_code", "run_command"}

RUNTIME_CONTEXT = (
    "Codex Global Runtime (全局运行时与三维正交交叉记忆 Project × Modality × Global 已启用): anchor the user's goal and constraints; "
    "Gemini 4 Tri-Core + Heterogeneous Balance: Antigravity master (gemini-4-carbon-high) orchestrates; AGY-CLI routes deep reasoning/refactoring/security to gemini-4-carbon-high, formal reports/PPT narrative/personas to gemini-4-argon-high, and standard coding/fast exploration to gemini-4-hydrogen-high/xhigh/medium; Codex-CLI (gpt-6.1-sol) runs unit/integration tests, red-team checks, and acceptance; Opus-CLI (claude-opus-5-5-high) handles top-level architecture review and OKLCH UI design; "
    "operate as a read-only consumer of 3D memories via sdk_fact_lookup(project_root=..., modalities=[...], task_intent=...) and runtime_knowledge (fact_list/memory_recall/modality_recall/project_bind); "
    "complex deliverable tasks use a parallel gemini-4-hydrogen-medium Sidecar Reviewer (记忆事实伴飞审查员) for blind-audit mechanical reconciliation, while high-impact memory review forbids 6.1sol and opus5.5 and dynamically selects 6.1terra (gpt-6.1-terra) or sonnet5.5 (claude-sonnet-5-5) by weekly remaining quota; "
    "use runtime_experience(propose, supersede_id=...) for bi-temporal experience evolution; "
    "prefer hash_outline (or hash_read with mode='outline') for AST skeleton extraction and hash_read/hash_edit for anchored file changes; "
    "pay attention to _advisories in MCP responses (Conflict Guard & Return Path hints); "
    "verify outputs and checkpoint task state at safe points. "
    "At meaningful phase changes, give the user a brief visible progress update in Simplified Chinese (简体中文) with the current phase, "
    "established result, and next action; never expose private reasoning. "
    "Follow ASD-STE100 Controlled Technical Grammar (80% strength) for user-facing steps, CLI commands, and troubleshooting (one word one meaning, zero fluff, no vague modals, active imperative sentences ≤20 words / ≤25 Chinese chars, one action per step; exempt internal thinking and trade-off analysis from length limits). "
    "始终默认使用简体中文进行内部思考（Thinking）与对话回复。"
)


def _runtime_context(session_id: str) -> str:
    try:
        task = active_session_task(session_id)
    except Exception:
        task = None
    if not task:
        return RUNTIME_CONTEXT
    state = task["state"]
    summary = {
        "task_id": task["task_id"], "revision": task["revision"],
        "root_goal": state["profile"]["root_goal"],
        "constraints": state["profile"]["constraints"],
        "plan": [{"id": step["id"], "status": step["status"]} for step in state["plan"]],
        "context_refs": state["context_refs"],
    }
    return RUNTIME_CONTEXT + " Resume reference: " + json.dumps(summary, ensure_ascii=False)[:2200]


def _terminal_response_text(response: Any) -> tuple[str | None, int | None]:
    if isinstance(response, str):
        return response, None
    if not isinstance(response, dict):
        return None, None
    exit_code = response.get("exit_code", response.get("exitCode"))
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        exit_code = None
    if isinstance(response.get("output"), str):
        return response["output"], exit_code
    if isinstance(response.get("stdout"), str) and not response.get("stderr"):
        return response["stdout"], exit_code
    content = response.get("content")
    if (isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
            and content[0].get("type") == "text" and isinstance(content[0].get("text"), str)):
        return content[0]["text"], exit_code
    return None, exit_code


def _post_tool_result(event: dict[str, Any], session_id: str, tool_name: str) -> dict[str, Any]:
    if tool_name.endswith("runtime_task"):
        tool_input = event.get("tool_input")
        response = event.get("tool_response")
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except ValueError:
                response = None
        if (isinstance(tool_input, dict) and tool_input.get("action") == "create"
                and isinstance(response, dict) and not response.get("isError")):
            nested = response.get("result")
            if isinstance(nested, dict):
                response = nested
            payload = response.get("structuredContent", response)
            task_id = payload.get("task_id") if isinstance(payload, dict) else None
            if isinstance(task_id, str):
                try:
                    bind_task_session(task_id, session_id, event.get("cwd", ""), tool_input.get("project_root", ""), event.get("agent_run_id"))
                except Exception:
                    pass
        return {}
    if tool_name not in ({"Bash", "exec_command"} | EXT_SHELL_TOOLS):
        return {}
    try:
        task = active_session_task(session_id)
    except Exception:
        return {}
    if task is None:
        return {}
    raw, exit_code = _terminal_response_text(event.get("tool_response"))
    if raw is None:
        return {}
    tool_input = event.get("tool_input")
    command = tool_input.get("command", tool_input.get("cmd", tool_input.get("code"))) if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        command = None
    try:
        outcome = optimize_result(task["task_id"], raw, tool=tool_name, command=command, exit_code=exit_code)
    except Exception:
        return {}
    if outcome.get("compression") in {"compressed", "failed"}:
        compact = outcome.get("compact_result")
        if isinstance(compact, str) and compact:
            return {"continue": False, "stopReason": compact, "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": compact}}
    return {}


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _append_advisories(result: dict, session_id: str, event_name: str) -> dict:
    reminders = flush_advisories(session_id)
    if reminders:
        output = result.setdefault("hookSpecificOutput", {"hookEventName": event_name})
        output["additionalContext"] = "\n".join(filter(None, [output.get("additionalContext"), reminders]))
    return result


def handle(event: dict[str, Any]) -> dict[str, Any]:
    with database_session():
        return _handle(event)


def _handle(event: dict[str, Any]) -> dict[str, Any]:
    name = event.get("hook_event_name")
    tool_name = event.get("tool_name", "")
    session_id = str(event.get("session_id") or "default")
    args = event.get("tool_input", event.get("arguments", {})) or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    if not isinstance(args, dict):
        raise ValueError("tool_input must be an object")
    policy = load_policy()
    if name == "SubagentStart":
        mark_started(session_id, str(event.get("turn_id", "")), str(event.get("agent_id", "")))
        return {}
    if name == "SubagentStop":
        mark_stopped(session_id, str(event.get("agent_id", "")))
        return {}
    if name in {"PreToolUse", "PostToolUse"} and tool_name in NATIVE_DELEGATION:
        if name == "PreToolUse":
            depth = int(event.get("agent_depth", 0) or 0)
            team_mode = bool(event.get("team_id")) and bool(policy.get("team", {}).get("enabled", False))
            max_depth = policy.get("team", {}).get("max_depth", 2) if team_mode else policy["limits"]["max_agent_depth"]
            if not policy["limits"].get("allow_subagent_creation", True) or depth >= max_depth:
                return _deny("Blocked by Agent Policy: subagent recursion depth limit exceeded.")
            turn_id = str(event.get("turn_id", ""))
            call_id = str(event.get("tool_use_id", uuid.uuid4().hex))
            if not reserve_slot(session_id, turn_id, call_id, policy["limits"]["max_parallel_agents"]):
                return _deny("Blocked by Agent Policy: parallel subagent limit exceeded.")
        elif name == "PostToolUse" and (event.get("error") or (isinstance(event.get("tool_response"), dict) and event["tool_response"].get("isError"))):
            release_slot(session_id, str(event.get("tool_use_id", "")))
        return {}
    set_workspace(session_id, event.get("cwd"))
    if name == "SessionStart":
        return {"hookSpecificOutput": {"hookEventName": name, "additionalContext": _runtime_context(session_id)}}
    if name == "UserPromptSubmit":
        reset_telemetry_on_prompt(session_id)
        return _append_advisories({}, session_id, name)
    if name == "PreCompact":
        task = active_session_task(session_id)
        if task:
            checkpoint(task["task_id"], "pre_compact")
        elif latest_session_task(session_id):
            record_event("task_session_ambiguous", {"session_id": session_id, "source": "pre_compact"})
        return {}
    if name == "PreToolUse":
        if tool_name == "swarm" and (os.environ.get("AGY_RUNTIME_ENV") == "cloud" or os.name != "nt"):
            return _deny("Blocked by Agent Policy: native /swarm is disabled on 2C4G cloud hosts to prevent OOM.")
        if tool_name in policy.get("global_denied_tools", []):
            return _deny("Blocked by Agent Policy: globally denied tool.")
        role = event.get("agent_type") or event.get("agent_role")
        if isinstance(role, str) and role in policy["roles"]:
            effective = resolve({"role": role, "task_profile": event.get("task_profile", {}),
                                 "retries": event.get("retries", 0)}, policy)
            if effective["status"] == "stop":
                return _deny("Blocked by Agent Policy: retry limit exceeded.")
            if not check_tool(tool_name, effective, policy)["allowed"]:
                return _deny(f"Blocked by Agent Policy: {role} may not use {tool_name}.")
        reason = pre_tool(session_id, tool_name, args, str(event.get("tool_use_id", "")), policy)
        return _deny(reason) if reason else {}
    if name == "PostToolUse":
        post_tool(session_id, tool_name, args, event.get("tool_response"),
                  str(event.get("tool_use_id", "")), policy)
        result = _post_tool_result(event, session_id, tool_name)
        return _append_advisories(result, session_id, name)
    if name == "Stop":
        return check_delivery_gate(session_id, policy)
    if name == "SessionEnd":
        clear_session(session_id)
        return {}
    return {}


def main() -> None:
    event: dict[str, Any] = {}
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig").strip()
        if raw:
            event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError("Hook event must be an object")
        result = handle(event)
    except (PolicyError, ValueError, OSError, AttributeError, sqlite3.Error) as error:
        if event.get("hook_event_name") == "PreToolUse" and event.get("tool_name") not in NATIVE_DELEGATION:
            result = _deny(f"Codex Agent Policy could not validate the request safely: {error}")
        else:
            result = {"systemMessage": f"Codex Agent Policy hook failed: {error}"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
