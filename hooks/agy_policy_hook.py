"""Antigravity lifecycle hook adapter with Supervisor Engine & Shared Memory."""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from policy_engine import PolicyError, load_policy  # noqa: E402
from policy_state import (  # noqa: E402
    clear_session,
    record_event,
    reserve_slot,
)
from runtime_state import (  # noqa: E402
    active_session_task,
    bind_task_session,
    checkpoint,
    latest_session_task,
)
from supervisor_engine import (  # noqa: E402
    check_delivery_gate,
    database_session,
    flush_advisories,
    post_tool,
    pre_tool,
    push_advisory,
    reset_telemetry_on_prompt,
    set_workspace,
)

BLIND_AUDIT_FORBIDDEN_PHRASES = (
    "[master_reasoning_dump]",
    "我认为设计合理",
    "请直接通过审查",
    "无需核对直接提交",
    "skip verification",
)
BLOCKED_REVIEW_MODELS = (
    "6.1sol",
    "gpt-6.1-sol",
    "opus5.5",
    "claude-opus-5-5",
    "claude-opus-5-5-high",
)

RUNTIME_CONTEXT = (
    "【Antigravity 全局运行时、监督适配层与三维正交交叉记忆 (Project × Modality × Global) 已启用】锚定用户目标与约束条件；"
    "Gemini 4 三核协处理器 + 异构制衡铁律：当前 Antigravity 主会话（Gemini 4 Carbon / gemini-4-carbon-high 或 gemini-pro-orchestrator）专门担任唯一总控大脑进行任务拆解与派发调度；"
    "AGY-CLI 三核分流：1) 逻辑与攻坚重核 (gemini-4-carbon-high) 负责跨模块复杂重构、核心算法、深水区 Debug、安全漏洞修复与 Mod 自进化；"
    "2) 高熵正式文字与叙事主笔核 (gemini-4-argon-high) 专门负责 @office 正式技术报告撰稿、@ppt 叙事大纲策划、writing_* / presentation_planner / learning_* 角色与架构复盘文档润色；"
    "3) 常规编码与高频快反核 (gemini-4-hydrogen-high / xhigh / medium) 负责常规标准模块编码与渲染脚本（hydrogen-high / xhigh）、全库检索摸底、轻量脚本、伴飞机械对账承载（hydrogen-medium）及知识蒸馏流式处理；"
    "ChatGPT (Codex-CLI, gpt-6.1-sol) 专门负责单元测试/集成测试运行、红队挑刺与交付验收；Opus-CLI (claude-opus-5-5-high) 预留复杂顶层架构终审与 OKLCH 高审美 UI 设计；"
    "主会话与业务子代理对记忆库默认执行只读消费（调用 sdk_fact_lookup(project_root=..., modalities=[...], task_intent=...) 获取《三维交叉作战简报》，或调用 runtime_knowledge 的 fact_list/memory_recall/modality_recall/project_bind）；"
    "凡执行产出型/交付型复杂任务，启动伴飞审查员（Role='记忆事实伴飞审查员'，搭载 gemini-4-hydrogen-medium，遵守盲审隔离 Blind Audit：仅传入客观产物路径与 changed_files，严禁灌输主控推理过程）执行机械对账；高影响记忆复核严禁使用 6.1sol 与 opus5.5，统一按周剩余额度动态择优使用 6.1terra (gpt-6.1-terra) 或 sonnet5.5 (claude-sonnet-5-5)；"
    "复杂跨模块任务拆解 DAG 后须先经人工确认（STOP & WAIT）；"
    "优先使用 hash_outline（或 hash_read(mode='outline')）按需提取 AST 符号骨架与 4 字符锚点哈希，再使用 hash_read/hash_edit 精确编辑代码块；"
    "注意留意 MCP 响应中的 _advisories 字段（包含跨会话实时写冲突预警 Conflict Guard 与排障 Return Path 提示）；"
    "验证输出并在安全节点保存 Checkpoint。在关键阶段切换时向用户提供简短的中文进度说明；"
    "面向用户的操作步骤与排障说明遵循 ASD-STE100 航空维修手册受控技术语法（80% 强度：一词一义、零寒暄、禁模糊词、主动祈使句、单行单动作，内部思考与架构权衡阶段豁免句长限制）；"
    "全程使用简体中文编写内部思维链（Thinking）与工具步骤摘要（toolSummary）。"
)

TOOL_ALIAS = {
    "invoke_subagent": "Agent",
    "run_command": "Bash",
    "write_file": "Write",
    "edit_file": "Edit",
}


def _runtime_context(session_id: str) -> str:
    if not session_id:
        return RUNTIME_CONTEXT
    try:
        task = active_session_task(session_id)
    except Exception:
        task = None
    if not task:
        return RUNTIME_CONTEXT
    state = task["state"]
    summary = {
        "task_id": task["task_id"],
        "revision": task["revision"],
        "root_goal": state["profile"]["root_goal"],
        "constraints": state["profile"]["constraints"],
        "plan": [{"id": step["id"], "status": step["status"]} for step in state["plan"]],
        "context_refs": state["context_refs"],
    }
    return RUNTIME_CONTEXT + " 任务恢复上下文: " + json.dumps(summary, ensure_ascii=False)[:2200]


def _unwrap_mcp_tool(tool_name: str, tool_args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if tool_name == "call_mcp_tool":
        server = str(tool_args.get("ServerName", ""))
        inner_tool = str(tool_args.get("ToolName", ""))
        inner_args = tool_args.get("Arguments")
        if inner_tool and isinstance(inner_args, dict):
            if (
                "agent-policy" in server
                or inner_tool in {"hash_read", "hash_outline", "hash_edit", "object_model"}
                or inner_tool.startswith(("runtime_", "mod_", "policy_", "team_"))
            ):
                return inner_tool, inner_args
    return tool_name, tool_args


def _extract_runtime_task_args(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any] | None:
    effective_name, effective_args = _unwrap_mcp_tool(tool_name, tool_args)
    if effective_name.endswith("runtime_task"):
        return effective_args
    return None


def handle_agy(event_type: str, event: dict[str, Any]) -> dict[str, Any]:
    with database_session():
        return _handle_agy(event_type, event)


def _handle_agy(event_type: str, event: dict[str, Any]) -> dict[str, Any]:
    session_id = str(event.get("conversationId") or "default")
    step_idx = str(event.get("stepIdx", "0"))
    workspaces = event.get("workspacePaths") or []
    cwd = workspaces[0] if isinstance(workspaces, list) and workspaces else ""
    policy = load_policy()
    if cwd:
        set_workspace(session_id, cwd)

    if event_type in {"SessionStart", "PreInvocation"}:
        reset_telemetry_on_prompt(session_id)
        reminders = flush_advisories(session_id)
        if event_type == "PreInvocation":
            if not reminders:
                return {}
            return {
                "injectSteps": [
                    {
                        "systemMessage": {
                            "systemMessage": reminders
                        }
                    }
                ]
            }
        ctx = _runtime_context(session_id)
        if reminders:
            ctx = ctx + "\n" + reminders
        return {
            "injectSteps": [
                {
                    "systemMessage": {
                        "systemMessage": ctx
                    }
                }
            ]
        }

    if event_type == "PreToolUse":
        tool_call = event.get("toolCall") or {}
        raw_tool_name = str(tool_call.get("name", ""))
        raw_args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
        tool_name, tool_args = _unwrap_mcp_tool(raw_tool_name, raw_args)
        mapped_name = TOOL_ALIAS.get(raw_tool_name, raw_tool_name)
        denied = set(policy.get("global_denied_tools", []))
        if raw_tool_name in denied or mapped_name in denied or tool_name in denied:
            record_event("tool_denied", {"session_id": session_id, "tool": raw_tool_name, "reason": "global_denied_tool"})
            return {
                "decision": "deny",
                "reason": f"已被 Antigravity Agent Policy 拦截：全局禁用工具 ({raw_tool_name})。",
            }

        if raw_tool_name == "invoke_subagent":
            limits = policy["limits"]
            depth = int(event.get("agentDepth", event.get("agent_depth", 0)) or 0)
            max_depth = policy.get("team", {}).get("max_depth", 2) if event.get("teamId") else limits["max_agent_depth"]
            if not limits.get("allow_subagent_creation", True) or depth >= max_depth:
                return {
                    "decision": "deny",
                    "reason": "已被 Antigravity Agent Policy 拦截：当前策略已禁用创建子代理或已超出递归深度上限。",
                }
            subagent_role = str(raw_args.get("Role") or raw_args.get("role") or "").strip()
            if subagent_role in {"记忆事实伴飞审查员", "Memory Fact Sidecar Reviewer"}:
                prompt_low = str(raw_args.get("Prompt") or raw_args.get("prompt") or "").casefold()
                model_low = str(raw_args.get("Model") or raw_args.get("model") or "").strip().casefold()
                if model_low in BLOCKED_REVIEW_MODELS:
                    return {
                        "decision": "deny",
                        "reason": f"[BLOCKED_REVIEWER_MODEL] 记忆审查严禁使用 {model_low}（禁用 6.1sol 与 opus5.5），请使用周额度剩的更多的 6.1terra 或 sonnet5.5！",
                    }
                if any(phrase in prompt_low for phrase in BLIND_AUDIT_FORBIDDEN_PHRASES):
                    return {
                        "decision": "deny",
                        "reason": "[BLIND_AUDIT_VIOLATION] 伴飞盲审隔离拦截：严禁向伴飞审查员灌输主控主观推理结论或跳过核验指令，仅允许传递客观产物路径、changed_files 与测试结果！",
                    }
                record_event("sidecar_reserved", {"session_id": session_id, "role": subagent_role})
                return {"decision": "allow"}
            slot_id = f"{session_id}:{step_idx}:{uuid.uuid4().hex[:8]}"
            if not reserve_slot(session_id, step_idx, slot_id, limits["max_parallel_agents"]):
                return {
                    "decision": "deny",
                    "reason": "已被 Antigravity Agent Policy 拦截：已达到最大并行子代理数量上限。",
                }
            record_event("agent_reserved", {"session_id": session_id, "slot_id": slot_id})
            return {"decision": "allow"}

        if tool_name.endswith("runtime_plan") and isinstance(tool_args.get("steps"), list) and len(tool_args["steps"]) >= 3:
            if not tool_args.get("user_approved"):
                push_advisory(
                    session_id,
                    "dag-human-gate",
                    "warn",
                    "[DAG_HUMAN_GATE] 检测到已拆解多步复杂 DAG 计划。为消除主控同源理解盲区，请在批量派发子代理前先向用户展示简明计划并获得确认（STOP & WAIT）。",
                )

        call_id = str(tool_call.get("id") or f"{session_id}:{step_idx}:{tool_name}")
        reason = pre_tool(session_id, tool_name, tool_args, call_id, policy)
        if reason:
            return {"decision": "deny", "reason": reason}

        return {"decision": "allow"}

    if event_type == "PostToolUse":
        tool_call = event.get("toolCall") or {}
        raw_tool_name = str(tool_call.get("name", ""))
        raw_args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
        tool_name, tool_args = _unwrap_mcp_tool(raw_tool_name, raw_args)

        if raw_tool_name == "invoke_subagent" and event.get("error"):
            clear_session(session_id)

        call_id = str(tool_call.get("id") or f"{session_id}:{step_idx}:{tool_name}")
        tool_response = event.get("toolResponse", event.get("output", event.get("result", {})))
        if event.get("error") and not tool_response:
            tool_response = {"isError": True, "error": str(event.get("error"))}
        post_tool(session_id, tool_name, tool_args, tool_response, call_id, policy)

        rt_args = _extract_runtime_task_args(raw_tool_name, raw_args)
        if rt_args and rt_args.get("action") == "create":
            project_root = str(rt_args.get("project_root") or cwd or "")
            task_id = rt_args.get("task_id")
            if isinstance(task_id, str) and task_id and session_id:
                try:
                    bind_task_session(task_id, session_id, cwd or project_root, project_root, None)
                except Exception:
                    pass
        # Antigravity PostToolUse protojson schema only accepts an empty object {}
        return {}

    if event_type == "Stop":
        reason = str(event.get("terminationReason", ""))
        if reason in {"MAX_TOKEN_BUDGET_EXCEEDED", "MAX_INVOCATIONS"} and session_id:
            try:
                task = active_session_task(session_id)
                if task:
                    checkpoint(task["task_id"], "pre_compact")
                elif latest_session_task(session_id):
                    record_event("task_session_ambiguous", {"session_id": session_id, "source": "stop_limit"})
            except Exception:
                pass
            if event.get("fullyIdle", True):
                clear_session(session_id)
            return {"decision": "allow"}

        gate = check_delivery_gate(session_id, policy)
        if gate.get("decision") == "block":
            return {"decision": "deny", "reason": gate["reason"]}

        if event.get("fullyIdle", True) and session_id:
            clear_session(session_id)
        return {"decision": "allow"}

    return {}


def main() -> None:
    event_type = sys.argv[1] if len(sys.argv) > 1 else "SessionStart"
    raw_event: dict[str, Any] = {}
    try:
        raw_text = sys.stdin.buffer.read().decode("utf-8-sig").strip()
        if raw_text:
            parsed = json.loads(raw_text)
            if isinstance(parsed, dict):
                raw_event = parsed
        result = handle_agy(event_type, raw_event)
    except (PolicyError, ValueError, OSError, AttributeError, sqlite3.Error) as error:
        if event_type == "PreToolUse":
            result = {
                "decision": "deny",
                "reason": f"Antigravity Agent Policy 安全校验异常：{error}",
            }
        else:
            result = {}
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
