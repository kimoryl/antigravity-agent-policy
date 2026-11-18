"""Deterministic Experience and Candidate Skill orchestration.

Codex supplies the proposed lesson text. This module only validates boundaries,
persists provenance and applies review gates; it never edits formal Policy or
activates a Skill automatically.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from policy_state import record_event
from runtime_state import RuntimeError
from experience_store import (
    auto_promote_eligible_experiences,
    create_skill_proposal,
    create_policy_candidate,
    get_experience,
    get_skill_proposal,
    list_experience_metadata,
    load_config,
    promote_experience,
    propose_experience,
    reject_experience,
    search_experiences,
    set_skill_status,
    validate_experience,
    validate_skill_proposal,
)


ROOT = Path(__file__).resolve().parents[1]


def search(project_id: str | None, query: str = "", scope: str = "project+global", limit: int = 3, max_chars: int = 2500) -> dict[str, Any]:
    config = load_config()
    if not config["enabled"] or not config["retrieval"]["automatic"]:
        return {"experiences": [], "disabled": True}
    items = search_experiences(project_id, query, scope, limit)
    output = []
    used = 0
    for item in items:
        compact = {
            "label": "EXPERIENCE",
            "experience_id": item["experience_id"],
            "scope": item["scope"],
            "title": item["title"],
            "summary": item["summary"],
            "conditions": item["conditions"],
            "recommendations": item["recommendations"],
            "confidence": item["confidence"],
            "support_count": item["support_count"],
            "project_count": item["project_count"],
            "status": item["status"],
        }
        text = json.dumps(compact, ensure_ascii=False)
        if used + len(text) > max_chars:
            break
        used += len(text)
        output.append(compact)
    return {"experiences": output, "scope": scope, "budget_chars": max_chars, "untrusted": True}


def propose(args: dict[str, Any]) -> dict[str, Any]:
    return propose_experience(
        project_id=args.get("project_id") or "__global__", title=args["title"], summary=args["summary"],
        conditions=args.get("conditions"), recommendations=args.get("recommendations"),
        anti_patterns=args.get("anti_patterns"), confidence=args.get("confidence", 0.5),
        source_refs=args.get("source_refs"), scope=args.get("scope", "project"),
        support_count=args.get("support_count", 1), project_count=args.get("project_count", 1),
        supersede_id=args.get("supersede_id"), hard_boundary=bool(args.get("hard_boundary", False)),
    )


def promote(args: dict[str, Any]) -> dict[str, Any]:
    return promote_experience(args["experience_id"], args.get("reviewer", "codex"), bool(args.get("approve", True)), args.get("reason", "auto_desensitized_promotion"))


def skill_propose(args: dict[str, Any]) -> dict[str, Any]:
    item = create_skill_proposal(args["experience_id"], args["name"], args["body"], args.get("version", "0.1.0"))
    path = ROOT / item["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the proposal outside the formal registry. The provenance header is
    # deliberately metadata-only and contains no raw Memory or Evidence.
    content = "---\n" + f"name: {item['name']}\nversion: {item['version']}\ngenerated_from_experience: {item['experience_id']}\nstatus: experimental\n---\n\n" + args["body"].strip() + "\n"
    path.write_text(content, encoding="utf-8")
    return item


def skill_validate(args: dict[str, Any]) -> dict[str, Any]:
    item = validate_skill_proposal(args["proposal_id"], args["validation_type"], args["verdict"], args.get("reviewer", "codex"))
    if item["status"] == "validated":
        record_event("skill_validated", {"proposal_id": item["proposal_id"], "experience_id": item["experience_id"]})
    return item


def policy_candidate(args: dict[str, Any]) -> dict[str, Any]:
    if not args.get("hard_boundary", False):
        raise RuntimeError("Only a reviewed hard boundary may become a Policy Candidate")
    item = get_experience(args["experience_id"])
    candidate = create_policy_candidate(item["experience_id"], str(args.get("reason", "")), args.get("reviewer", "codex"))
    candidate["review_required"] = True
    record_event("policy_candidate_created", {"experience_id": item["experience_id"]})
    return candidate


def call(action: str, args: dict[str, Any]) -> Any:
    if action == "search":
        return search(args.get("project_id"), args.get("query", ""), args.get("scope", "project+global"), args.get("limit", 3), args.get("max_chars", 2500))
    if action == "propose":
        return propose(args)
    if action == "get":
        return get_experience(args["experience_id"])
    if action == "validate":
        return validate_experience(args["experience_id"], args["verdict"], args.get("reviewer", "codex"), args.get("validation_type", "reviewer"), args.get("evidence_ref"), args.get("task_id"))
    if action == "promote":
        return promote(args)
    if action == "auto_promote":
        return auto_promote_eligible_experiences(args.get("reviewer", "agy-auto-distiller"))
    if action == "reject":
        return reject_experience(args["experience_id"], args.get("reviewer", "codex"), args.get("reason", ""))
    if action == "skill_propose":
        return skill_propose(args)
    if action == "skill_validate":
        return skill_validate(args)
    if action in {"skill_activate", "skill_rollback", "skill_reject"}:
        mapping = {"skill_activate": "active", "skill_rollback": "rolled_back", "skill_reject": "rejected"}
        return set_skill_status(args["proposal_id"], mapping[action], args.get("reviewer", "codex"))
    if action == "policy_candidate":
        return policy_candidate(args)
    if action == "list":
        return {"experiences": list_experience_metadata(args.get("project_id"), args.get("status"), args.get("limit", 50))}
    if action == "skill_get":
        return get_skill_proposal(args["proposal_id"])
    raise RuntimeError(f"Unknown runtime_experience action: {action}")
