"""Agent roles and input/output contracts, separate from resource policy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import time


REGISTRY_PATH = Path(__file__).resolve().parents[1] / "agent_registry.json"


def load_agents() -> dict[str, Any]:
    data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("roles"), dict):
        raise ValueError("Invalid Agent Registry")
    for name, role in data["roles"].items():
        if not isinstance(name, str) or not isinstance(role, dict):
            raise ValueError("Invalid Agent role")
        if not isinstance(role.get("purpose"), str) or not role["purpose"].strip():
            raise ValueError(f"Invalid Agent role {name}: purpose")
        for field in ("category", "display_name", "guidance"):
            if field in role and (not isinstance(role[field], str) or not role[field].strip()):
                raise ValueError(f"Invalid Agent role {name}: {field}")
        for field in ("default_skills", "input_fields", "output_fields"):
            if not isinstance(role.get(field), list) or any(not isinstance(x, str) for x in role[field]):
                raise ValueError(f"Invalid Agent role {name}: {field}")
        tags = role.get("skill_tags", [])
        if not isinstance(tags, list) or any(not isinstance(x, str) or not x.strip() for x in tags):
            raise ValueError(f"Invalid Agent role {name}: skill_tags")
    return data


def compose_agent_profile(role: str, *, capabilities: list[str] | None = None,
                          model_profile: dict[str, Any] | str | None = None,
                          reasoning_profile: dict[str, Any] | None = None,
                          skills: list[str] | None = None, permissions: list[str] | None = None,
                          mcp_servers: list[str] | None = None, mcp_tools: list[str] | None = None,
                          software_dependencies: list[dict[str, Any]] | None = None,
                          capability_version_lock: dict[str, str] | None = None,
                          context_snapshot_id: str | None = None,
                          private_context_refs: list[str] | None = None,
                          task: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compose a runtime instance from a finite registered role."""
    registry = load_agents()
    if role not in registry["roles"]:
        raise ValueError(f"Unknown Agent role: {role}")
    definition = registry["roles"][role]

    def strings(value: list[str] | None, name: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError(f"{name} must be a list of strings")
        return sorted(set(value))

    def objects(value: list[dict[str, Any]] | None, name: str) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ValueError(f"{name} must be a list of objects")
        return [dict(item) for item in value]

    def versions(value: dict[str, str] | None, name: str) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict) or any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
            raise ValueError(f"{name} must be an object of strings")
        return dict(sorted(value.items()))

    return {
        "role": role,
        "display_name": definition.get("display_name", role),
        "purpose": definition["purpose"],
        "capabilities": strings(capabilities, "capabilities"),
        "model_profile": model_profile if model_profile is not None else "policy-selected",
        "reasoning_profile": reasoning_profile if reasoning_profile is not None else {},
        "skills": strings(skills if skills is not None else definition.get("default_skills", []), "skills"),
        "permissions": strings(permissions, "permissions"),
        "mcp_servers": strings(mcp_servers, "mcp_servers"),
        "mcp_tools": strings(mcp_tools, "mcp_tools"),
        "software_dependencies": objects(software_dependencies, "software_dependencies"),
        "capability_version_lock": versions(capability_version_lock, "capability_version_lock"),
        "context_snapshot_id": context_snapshot_id,
        "private_context_refs": strings(private_context_refs, "private_context_refs"),
        "task": task if task is not None else {},
        "runtime_metadata": {"composed_at": time.time(), "registry_version": registry.get("schema_version", 1)},
    }
