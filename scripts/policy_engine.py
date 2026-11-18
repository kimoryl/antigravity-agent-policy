"""Deterministic Agent Policy decisions. No Codex or JEV calls happen here."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = PLUGIN_ROOT / "policy" / "default.json"
PERMISSIONS = {"read", "write", "execute", "delegate", "external"}
EFFORT_ORDER = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
TEAM_DEFAULTS = {
    "enabled": True,
    "min_concurrency": 1,
    "max_concurrency": 3,
    "step": 1,
    "capacity_cooldown_seconds": 30,
    "capacity_success_threshold": 2,
    "max_depth": 2,
    "child_model": "gpt-6-luna",
    "difficulty_effort": {"simple": "low", "moderate": "medium", "complex": "high"},
}
MODEL_CAPACITY_FAILURES = {"model_capacity", "model_at_capacity", "capacity"}
RATE_LIMIT_FAILURES = {"rate_limit", "rate_limited"}
TERMINAL_FAILURES = {"malformed_output", "malformed_schema", "invalid_arguments", "auth_error"}


class PolicyError(ValueError):
    pass


def load_policy(path: Path | None = None) -> dict[str, Any]:
    source = path or DEFAULT_POLICY
    data = json.loads(source.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise PolicyError("Unsupported policy schema_version")
    required = ("model_policy", "defaults", "roles", "overrides", "escalation", "limits")
    if any(key not in data for key in required):
        raise PolicyError("Policy is missing a required section")
    limits = data["limits"]
    for key in ("max_parallel_agents", "max_agent_depth", "max_context_tokens"):
        if not isinstance(limits.get(key), int) or limits[key] < 0:
            raise PolicyError(f"Invalid limit: {key}")
    if limits["max_context_tokens"] == 0:
        raise PolicyError("max_context_tokens must be positive")
    if not isinstance(limits.get("allow_subagent_creation"), bool):
        raise PolicyError("allow_subagent_creation must be a boolean")
    for name, settings in [("defaults", data["defaults"]), *data["roles"].items()]:
        if settings.get("reasoning_effort") not in EFFORT_ORDER:
            raise PolicyError(f"Invalid reasoning_effort for {name}")
        budget = settings.get("context_budget_tokens")
        if not isinstance(budget, int) or budget <= 0:
            raise PolicyError(f"Invalid context_budget_tokens for {name}")
        permissions = settings.get("permissions")
        if not isinstance(permissions, list) or not set(permissions).issubset(PERMISSIONS):
            raise PolicyError(f"Invalid permissions for {name}")
    for index, override in enumerate(data["overrides"]):
        if not isinstance(override.get("when"), dict) or not isinstance(override.get("set"), dict):
            raise PolicyError(f"Invalid override: {index}")
        changes = override["set"]
        if "reasoning_effort" in changes and changes["reasoning_effort"] not in EFFORT_ORDER:
            raise PolicyError(f"Invalid override effort: {index}")
        if "context_budget_tokens" in changes and (not isinstance(changes["context_budget_tokens"], int) or changes["context_budget_tokens"] <= 0):
            raise PolicyError(f"Invalid override budget: {index}")
        if "permissions" in changes and (not isinstance(changes["permissions"], list) or not set(changes["permissions"]).issubset(PERMISSIONS)):
            raise PolicyError(f"Invalid override permissions: {index}")
    model_policy = data["model_policy"]
    if not isinstance(model_policy.get("default"), str) or not model_policy["default"]:
        raise PolicyError("model_policy.default must be a model ID or inherit")
    allowed_models = model_policy.get("allowed_models", [])
    if not isinstance(allowed_models, list) or not all(isinstance(item, str) for item in allowed_models):
        raise PolicyError("allowed_models must be a list of model IDs")
    if allowed_models and model_policy.get("default") != "inherit" and model_policy["default"] not in allowed_models:
        raise PolicyError("Default model must be inherit or allowed")
    stronger = model_policy.get("stronger")
    if allowed_models and stronger is not None and stronger not in allowed_models:
        raise PolicyError("Stronger model must be allowed")
    denied = data.get("global_denied_tools", [])
    if not isinstance(denied, list) or not all(isinstance(item, str) for item in denied):
        raise PolicyError("global_denied_tools must be a list of tool names")
    _validate_team(data)
    _validate_supervisor(data)
    return data



SUPERVISOR_DEFAULTS = {
    "window_size": 12,
    "max_duplicate_tool_calls": 3,
    "max_read_only_turns": 8,
    "strict_evidence_gate": True,
    "max_stop_rejections": 3,
    "co_occurrence_decay": 0.95,
    "co_occurrence_min_weight": 0.5,
    "mutate_tool_patterns": ["write", "write_file", "edit", "edit_file", "hash_edit", "str_replace_editor", "apply_patch"],
    "verify_tool_patterns": ["runtime_verify", "test", "verify", "lint", "schema_check"],
    "transparent_tools": ["todo_write", "present", "manage_task", "ask_question", "send_message", "schedule", "list_resources", "read_resource"],
}


def _validate_supervisor(data: dict[str, Any]) -> None:
    import re
    supplied = data.get("supervisor")
    if supplied is None:
        data["supervisor"] = {k: list(v) if isinstance(v, list) else v for k, v in SUPERVISOR_DEFAULTS.items()}
        return
    if not isinstance(supplied, dict):
        raise PolicyError("supervisor must be an object")
    settings = {**SUPERVISOR_DEFAULTS, **supplied}
    for key in ("window_size", "max_duplicate_tool_calls", "max_read_only_turns", "max_stop_rejections"):
        if type(settings.get(key)) is not int or settings[key] < 1:
            raise PolicyError(f"Invalid supervisor.{key}")
    if settings["max_read_only_turns"] > settings["window_size"]:
        raise PolicyError("Momentum threshold exceeds telemetry window")
    if type(settings.get("strict_evidence_gate")) is not bool:
        raise PolicyError("strict_evidence_gate must be boolean")
    for key in ("co_occurrence_decay", "co_occurrence_min_weight"):
        value = settings.get(key)
        if type(value) not in (int, float) or not 0 < value <= 1:
            raise PolicyError(f"Invalid supervisor.{key}")
    for key in ("mutate_tool_patterns", "verify_tool_patterns", "transparent_tools"):
        if not isinstance(settings.get(key), list) or any(not isinstance(p, str) or not p for p in settings[key]):
            raise PolicyError(f"Invalid supervisor.{key}")
    patterns = settings.get("verify_command_patterns", [])
    if isinstance(patterns, str):
        patterns = [patterns]
    if not isinstance(patterns, list) or any(not isinstance(p, str) for p in patterns):
        raise PolicyError("Invalid verify_command_patterns")
    try:
        for pattern in patterns:
            re.compile(pattern)
    except re.error as error:
        raise PolicyError(f"Invalid verification regex: {error}") from error
    data["supervisor"] = settings


def _validate_team(data: dict[str, Any]) -> None:
    supplied = data.get("team")
    if supplied is None:
        data["team"] = {**TEAM_DEFAULTS, "difficulty_effort": dict(TEAM_DEFAULTS["difficulty_effort"])}
        return
    if not isinstance(supplied, dict):
        raise PolicyError("team must be an object")
    config = {**TEAM_DEFAULTS, **supplied}
    if not isinstance(config["enabled"], bool):
        raise PolicyError("team.enabled must be a boolean")
    for key in ("min_concurrency", "max_concurrency", "step", "max_depth", "capacity_success_threshold"):
        if not isinstance(config[key], int) or config[key] <= 0:
            raise PolicyError(f"team.{key} must be a positive integer")
    if isinstance(config["capacity_cooldown_seconds"], bool) or not isinstance(config["capacity_cooldown_seconds"], (int, float)) or config["capacity_cooldown_seconds"] < 0:
        raise PolicyError("team.capacity_cooldown_seconds must be nonnegative")
    if config["min_concurrency"] > config["max_concurrency"] or config["step"] > config["max_concurrency"] or config["max_depth"] != 2:
        raise PolicyError("team concurrency/depth bounds are invalid")
    if not isinstance(config["child_model"], str) or not config["child_model"].endswith("-luna"):
        raise PolicyError("team.child_model must be a Luna model")
    efforts = config.get("difficulty_effort")
    if not isinstance(efforts, dict) or set(efforts) != {"simple", "moderate", "complex"} or any(value not in {"low", "medium", "high"} for value in efforts.values()):
        raise PolicyError("team.difficulty_effort must map simple/moderate/complex to low/medium/high")
    data["team"] = config


def _object(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PolicyError(f"{name} must be an object")
    return value


def _matches(conditions: dict[str, Any], role: str, profile: dict[str, Any]) -> bool:
    for key, expected in conditions.items():
        actual = role if key == "role" else profile.get(key)
        if actual != expected:
            return False
    return True


def _fallback_candidates(policy: dict[str, Any], model: str) -> list[str]:
    """Return the finite, ordered fallback list configured for one model."""
    fallback_map = policy["model_policy"].get("fallbacks", {}).get("model_capacity", {})
    if not isinstance(fallback_map, dict):
        raise PolicyError("model_policy.fallbacks.model_capacity must be an object")
    configured = fallback_map.get(model, [])
    if configured is None:
        return []
    candidates = [configured] if isinstance(configured, str) else configured
    if not isinstance(candidates, list) or any(not isinstance(item, str) or not item for item in candidates):
        raise PolicyError(f"Invalid model capacity fallback list for {model}")
    # Configuration is deterministic and finite; duplicates cannot create a
    # fallback loop even when a host reports capacity repeatedly.
    return list(dict.fromkeys(candidates))


def resolve(request: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """Return an effective config for one prospective agent execution."""
    request = _object(request, "request")
    role = request.get("role")
    if not isinstance(role, str) or not role:
        raise PolicyError("role is required")
    profile = _object(request.get("task_profile"), "task_profile")
    retries = request.get("retries", 0)
    depth = request.get("depth", 0)
    if not isinstance(retries, int) or retries < 0:
        raise PolicyError("retries must be a nonnegative integer")
    if not isinstance(depth, int) or depth < 0:
        raise PolicyError("depth must be a nonnegative integer")

    limits = policy["limits"]
    escalation = policy["escalation"]
    team_mode = bool(request.get("team_id")) and bool(policy.get("team", {}).get("enabled", False))
    depth_limit = policy.get("team", {}).get("max_depth", 2) if team_mode else limits["max_agent_depth"]
    if retries > escalation["retry_limit"]:
        return {"status": "stop", "reason": "retry_limit_exceeded", "role": role}
    if depth > depth_limit:
        return {"status": "stop", "reason": "max_agent_depth_exceeded", "role": role}

    trace = ["global defaults"]
    settings = dict(policy["defaults"])
    role_settings = policy["roles"].get(role)
    if role_settings:
        settings.update(role_settings)
        trace.append(f"role:{role}")
    else:
        trace.append(f"unknown role:{role}; using defaults")
    for index, override in enumerate(policy["overrides"]):
        if _matches(override.get("when", {}), role, profile):
            settings.update(override.get("set", {}))
            trace.append(f"override:{index}")

    model = policy["model_policy"]["default"]
    if settings.get("model"):
        model = settings["model"]
    failure_kind = request.get("failure_kind")
    if failure_kind in TERMINAL_FAILURES and retries >= 1:
        return {
            "status": "stop",
            "reason": "terminal_failure_non_retryable",
            "failure_kind": failure_kind,
            "role": role,
        }
    refresh_context = failure_kind == "missing_context"
    non_model_failure = failure_kind in {"missing_context", "permission_denied", "tool_unavailable"} | RATE_LIMIT_FAILURES
    if (request.get("verification_failed") is True or failure_kind == "verification_failed") and not non_model_failure:
        current = settings["reasoning_effort"]
        cur_idx = EFFORT_ORDER.index(current)
        if cur_idx < EFFORT_ORDER.index("high"):
            settings["reasoning_effort"] = EFFORT_ORDER[min(cur_idx + 1, EFFORT_ORDER.index("high"))]
            trace.append("escalation:verification_failed_effort")
    if retries >= escalation["increase_effort_after_retries"] and not non_model_failure:
        current = settings["reasoning_effort"]
        if EFFORT_ORDER.index(current) < EFFORT_ORDER.index("high"):
            settings["reasoning_effort"] = "high"
            trace.append("escalation:reasoning")
    confidence = profile.get("confidence")
    if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1):
        raise PolicyError("confidence must be between 0 and 1")
    if confidence is not None and confidence < escalation["low_confidence_threshold"] and not non_model_failure:
        stronger = policy["model_policy"].get("stronger")
        if stronger:
            model = stronger
            trace.append("escalation:stronger_model")

    user_model = request.get("user_model")
    if user_model is not None:
        if not isinstance(user_model, str) or not user_model:
            raise PolicyError("user_model must be a model id")
        model = user_model
        trace.append("user:model")
    available_models = request.get("available_models")
    if available_models is not None and (not isinstance(available_models, list) or not all(isinstance(item, str) for item in available_models)):
        raise PolicyError("available_models must be a list of model IDs")
    attempted_models = request.get("attempted_models", [])
    if not isinstance(attempted_models, list) or any(not isinstance(item, str) or not item for item in attempted_models):
        raise PolicyError("attempted_models must be a list of model IDs")
    attempted_models = list(dict.fromkeys(attempted_models))
    model_locked = request.get("model_locked", False)
    if not isinstance(model_locked, bool):
        raise PolicyError("model_locked must be a boolean")
    allowed_models = set(policy["model_policy"].get("allowed_models", []))
    capacity_failure = failure_kind in MODEL_CAPACITY_FAILURES
    availability_failure = failure_kind is None and available_models is not None and model != "inherit" and model not in available_models
    if capacity_failure or availability_failure:
        if model_locked and capacity_failure:
            return {"status": "stop", "reason": "model_locked", "role": role, "model_id": model,
                    "model_override": None, "failure_kind": failure_kind, "retry_later": False, "trace": trace}
        candidates = _fallback_candidates(policy, model)
        chosen = None
        for candidate in candidates:
            if candidate in attempted_models:
                continue
            if available_models is not None and candidate not in available_models:
                trace.append(f"fallback_unavailable:{candidate}")
                continue
            if allowed_models and candidate not in allowed_models:
                trace.append(f"fallback_not_allowed:{candidate}")
                continue
            chosen = candidate
            break
        if chosen is None:
            if availability_failure and not capacity_failure:
                raise PolicyError(f"Model is not available in Codex: {model}")
            return {"status": "stop", "reason": "model_unavailable", "role": role,
                    "model_id": model, "model_override": None, "failure_kind": failure_kind,
                    "attempted_models": attempted_models, "fallback_candidates": candidates,
                    "retry_later": True, "trace": trace}
        model = chosen
        trace.append(f"fallback:model_capacity:{chosen}")
    if allowed_models and model != "inherit" and model not in allowed_models:
        raise PolicyError(f"Model is not allowed by policy: {model}")
    if available_models is not None and model != "inherit" and model not in available_models:
        raise PolicyError(f"Model is not available in Codex: {model}")

    effort = settings["reasoning_effort"]
    user_effort = request.get("user_reasoning_effort")
    if user_effort is not None:
        if user_effort not in EFFORT_ORDER:
            raise PolicyError("Invalid user_reasoning_effort")
        effort = user_effort
        trace.append("user:reasoning_effort")
    supported_efforts = request.get("supported_efforts")
    if supported_efforts is not None and effort not in supported_efforts:
        raise PolicyError(f"Selected model does not support reasoning effort: {effort}")
    budget = settings["context_budget_tokens"]
    cap = limits["max_context_tokens"]
    host_budget = request.get("host_context_budget_tokens")
    if host_budget is not None:
        if not isinstance(host_budget, int) or host_budget <= 0:
            raise PolicyError("host_context_budget_tokens must be positive")
        cap = min(cap, host_budget)
    if budget > cap:
        budget = cap
        trace.append("context_budget:clamped")

    permissions = set(settings["permissions"])
    for key in ("parent_permissions", "user_allowed_permissions"):
        supplied = request.get(key)
        if supplied is not None:
            if not isinstance(supplied, list) or not set(supplied).issubset(PERMISSIONS):
                raise PolicyError(f"{key} contains invalid permissions")
            permissions.intersection_update(supplied)
            trace.append(f"permissions:intersect:{key}")
    if not limits["allow_subagent_creation"] or depth >= depth_limit:
        permissions.discard("delegate")

    if team_mode and depth > 0:
        profile_complexity = profile.get("complexity", "moderate")
        if profile_complexity not in {"simple", "moderate", "complex"}:
            profile_complexity = "moderate"
        team_model = policy["team"]["child_model"]
        team_effort = policy["team"]["difficulty_effort"][profile_complexity]
        if user_model is not None and user_model != team_model:
            raise PolicyError("Team child agents must use the configured Luna model")
        if user_effort is not None and user_effort != team_effort:
            raise PolicyError("Team child reasoning effort must follow task complexity")
        model = team_model
        effort = team_effort
        trace.append(f"team:child:{team_model}:{team_effort}")

    require_reviewer = bool(
        escalation["require_reviewer_for_architecture_change"]
        and profile.get("architecture_change") is True
    )
    return {
        "status": "ready",
        "role": role,
        "model_id": None if model == "inherit" else model,
        "model_override": None if model == "inherit" else model,
        "reasoning_effort": effort,
        "context_budget_tokens": budget,
        "context_loading_budget_tokens": budget,
        "tool_permissions": sorted(permissions),
        "max_parallel_agents": limits["max_parallel_agents"],
        "max_agent_depth": limits["max_agent_depth"],
        "require_reviewer": require_reviewer,
        "team_mode": team_mode,
        "max_team_depth": depth_limit if team_mode else None,
        "refresh_context_first": refresh_context,
        "trace": trace,
    }


def tool_capability(tool_name: str) -> str:
    if tool_name in {"Agent", "spawn_agent"}:
        return "delegate"
    if tool_name in {"Bash", "exec_command"}:
        return "execute"
    if tool_name in {"apply_patch", "Edit", "Write"}:
        return "write"
    if tool_name.startswith("mcp__"):
        return "external"
    return "external"


def check_tool(tool_name: str, effective_policy: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    if tool_name in policy.get("global_denied_tools", []):
        return {"allowed": False, "reason": "global_denied_tool"}
    capability = tool_capability(tool_name)
    return {
        "allowed": capability in effective_policy.get("tool_permissions", []),
        "capability": capability,
        "reason": "allowed" if capability in effective_policy.get("tool_permissions", []) else "role_permission_denied",
    }
