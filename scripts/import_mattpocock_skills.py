"""Import a pinned mattpocock/skills checkout as Codex Skill Pool snapshots.

This script copies data only; it never runs code from the upstream checkout.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

from sync_skill_pool import description


ROOT = Path(__file__).resolve().parents[1]
POOL = ROOT / "skill_pool"
STAGES = ["planning", "execution", "verification"]
PROVIDER = "mattpocock-skills"


def _adapt(name: str, source: str) -> str:
    if not source.startswith("---\n"):
        raise ValueError(f"Missing frontmatter: {name}")
    end = source.find("\n---\n", 4)
    if end < 0:
        raise ValueError(f"Invalid frontmatter: {name}")
    front, body = source[:end + 5], source[end + 5:]
    note = (
        "\n> **Codex Skill Pool adapter.** This is an adapted copy of a skill from "
        "`mattpocock/skills`. Names like `/tdd` and references to the Claude `Skill` "
        "tool mean the corresponding `mattpocock-<name>` Skill Pool entry. Use "
        "`skill_catalog` → `skill_resolve` → `skill_load` to access it. If that "
        "entry has `invocation_mode: user`, load it only when the user explicitly "
        "invoked that entry. Follow current Codex permissions, task policy, and "
        "available tools for any agents, branches, PRs, or external writes. Never "
        "read credential files or private configuration.\n"
    )
    if name == "claude-handoff":
        body = (
            "\n# Codex handoff\n\n"
            "Write a concise, redacted handoff summary with the goal, current state, "
            "next actions, source pointers, and suggested Skill Pool names. "
            "If the user explicitly requested a new Codex task, create it with "
            "Codex's `create_thread` tool using that summary as the prompt. "
            "Otherwise provide the summary in the current chat. Do not launch the "
            "Claude CLI or expose secrets.\n"
        )
    elif name == "implement-spec":
        body = (
            "\n# Implement a spec in Codex\n\n"
            "Read the spec and associated tickets, then form a dependency graph. "
            "Implement all ready work according to the current task's agent policy. "
            "Create branches or worktrees only when needed and permitted by the "
            "user's request and Codex workflow. Use Codex native subagents only "
            "when available and permitted; otherwise work sequentially. "
            "Use `mattpocock-tdd` and `mattpocock-code-review` through the Skill "
            "Pool when the user has explicitly invoked those manual Skills; "
            "otherwise apply equivalent testing and review directly. Run relevant "
            "tests, resolve remaining tickets, and report the result. Create or "
            "publish a PR only when the user has authorized that action.\n"
        )
    else:
        body = re.sub(
            r"(?i)call the Skill tool twice, for [\"']([a-z0-9-]+)[\"'] and [\"']([a-z0-9-]+)[\"']",
            lambda match: f"load `mattpocock-{match.group(1)}` and `mattpocock-{match.group(2)}` through the Skill Pool when invocation is permitted",
            body,
        )
        body = re.sub(
            r"(?i)call the Skill tool with [\"'`]([a-z0-9-]+)[\"'`]",
            lambda match: f"load `mattpocock-{match.group(1)}` through the Skill Pool when invocation is permitted",
            body,
        )
        body = re.sub(r"(?i)call the Skill tool", "use the Skill Pool", body)
        if name == "wizard":
            body = body.replace(
                "For setup: `.env`, `.env.example`, `.env.*`, `README`, `docker-compose*`, framework config, and `.github/workflows/*`",
                "For setup: `.env.example`, public `README`, `docker-compose*`, framework config, and `.github/workflows/*`; do not read `.env` or `.env.*` files",
            )
        if name == "setup-matt-pocock-skills":
            body = body.replace(
                "`git remote -v` and `.git/config`: is this a GitHub repo? Which one?",
                "Use the repository name from public project docs or the user's context; do not read `.git/config` or print credential-bearing remote URLs. Ask if the hosting provider remains unclear.",
            )
            body = body.replace("If a `git remote` points at GitHub", "If public project context identifies GitHub")
            body = body.replace("If a `git remote` points at GitLab", "If public project context identifies GitLab")
        if name == "implement":
            body = body.replace(
                "Commit your work to the current branch.",
                "Commit the work only when the user's request or existing workflow authorizes a commit.",
            )
        if name == "git-guardrails-claude-code":
            body = "\n> This skill configures **Claude Code** hooks only. Use it only when the user explicitly asks for Claude Code guardrails.\n" + body
    return front + note + body


def import_checkout(checkout: Path, review: Path) -> dict:
    checkout = checkout.resolve()
    review_data = json.loads(review.read_text(encoding="utf-8"))
    commit = review_data["commit"]
    if review_data["repository"] != "mattpocock/skills" or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Unexpected review manifest")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True,
                          text=True, check=True, timeout=10,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0).stdout.strip()
    if head != commit:
        raise ValueError("Checkout does not match reviewed commit")
    files = sorted((checkout / "skills").glob("*/*/SKILL.md"))
    if len(files) != len(review_data["tags"]) or {p.parent.name for p in files} != set(review_data["tags"]):
        raise ValueError("Skill files do not match reviewed tags")
    if any(path.is_symlink() for path in (checkout / "skills").rglob("*")):
        raise ValueError("Refusing upstream symlinks")
    target = POOL / "providers" / f"{PROVIDER}-{commit[:7]}"
    for existing_dir in (POOL / "providers").glob(f"{PROVIDER}-*"):
        if existing_dir.is_dir():
            shutil.rmtree(existing_dir)
    target.mkdir(parents=True, exist_ok=True)
    registry_path = POOL / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    tags_path = POOL / "tags.json"
    tags = json.loads(tags_path.read_text(encoding="utf-8"))
    removed_caps = {
        cap
        for item in registry["skills"]
        if item.get("origin") == "mattpocock/skills@github" or item["name"].startswith("mattpocock-")
        for cap in item.get("provides", [])
    }
    registry["skills"] = [
        item for item in registry["skills"]
        if item.get("origin") != "mattpocock/skills@github" and not item["name"].startswith("mattpocock-")
    ]
    registry["capabilities"] = [c for c in registry["capabilities"] if c not in removed_caps]
    old_names = {item["name"] for item in registry["skills"]}
    new_names = {f"mattpocock-{file.parent.name}" for file in files}
    if old_names & new_names:
        raise ValueError("These Skill names are already registered")
    imported = []
    for skill_file in files:
        name = skill_file.parent.name
        relative = skill_file.parent.relative_to(checkout / "skills")
        destination = target / "skills" / relative
        shutil.copytree(skill_file.parent, destination, dirs_exist_ok=False)
        source = skill_file.read_text(encoding="utf-8-sig")
        adapted = _adapt(name, source.replace("\r\n", "\n"))
        (destination / "SKILL.md").write_text(adapted, encoding="utf-8")
        pool_name = f"mattpocock-{name}"
        slug = re.sub(r"[^a-z0-9]", "", pool_name)
        capability = f"skill.{slug}"
        if capability in registry["capabilities"]:
            raise ValueError(f"Capability collision: {capability}")
        _, summary = description(skill_file)
        manual = "disable-model-invocation: true" in source or name == "git-guardrails-claude-code"
        entry = {
            "name": pool_name, "description": summary or f"Use {name} when requested.",
            "tags": review_data["tags"][name], "provides": [capability], "stage": STAGES,
            "requires": [], "conflicts": [], "priority": 50, "context_requirements": [],
            "version": f"{commit[:7]}+{sha256(adapted.encode('utf-8')).hexdigest()[:12]}",
            "trust": "untrusted", "invocation_mode": "user" if manual else "auto",
            "path": (destination / "SKILL.md").relative_to(POOL).as_posix(),
            "origin": "mattpocock/skills@github",
            "source_commit": commit, "aliases": [name],
        }
        imported.append(entry)
        tags[pool_name] = entry["tags"]
        registry["capabilities"].append(capability)
    registry["capabilities"].sort()
    registry["skills"] = imported + registry["skills"]
    tags_path.write_text(json.dumps(tags, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    registry_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (target / "ADAPTATIONS.md").write_text(
        f"# Codex adaptations of mattpocock/skills\n\nUpstream commit: `{commit}`. "
        f"All {len(imported)} Skills and their local resources were copied without running upstream code. "
        "The original checkout is unchanged. The Skill Pool maps slash/Claude Skill "
        "references to prefixed Pool names, preserves explicit-invocation flags, "
        "adapts the Claude handoff and spec orchestrator, avoids reading secret "
        "configurations, and conditions commits/PRs on authorization. "
        "The Claude Code guardrail remains explicitly Claude-targeted.\n",
        encoding="utf-8",
    )
    return {"count": len(imported), "manual": sum(x["invocation_mode"] == "user" for x in imported),
            "provider": str(target)}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: import_mattpocock_skills.py CHECKOUT REVIEW_JSON")
    print(json.dumps(import_checkout(Path(sys.argv[1]), Path(sys.argv[2])), ensure_ascii=False))
