"""Build the unified Capability Pool from the current Skill Pool metadata.

The Skill Pool remains the source of truth for Skill bodies and invocation
rules.  This script mirrors every registered Skill as a ``skill`` Capability
while preserving hand-authored MCP and hybrid entries.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "capability_pool" / "registry.json"
SKILL_REGISTRIES = (ROOT / "skill_registry.json", ROOT / "skill_pool" / "registry.json")


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _skill_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    names: set[str] = set()
    for path in SKILL_REGISTRIES:
        data = _load(path)
        for skill in data.get("skills", []):
            name = skill["name"]
            if name in names:
                raise ValueError(f"Duplicate Skill across registries: {name}")
            names.add(name)
            invocation_mode = skill.get("invocation_mode", "auto")
            entry = {
                "name": f"skill-{name}",
                "type": "skill",
                "provides": list(skill["provides"]),
                "stage": list(skill["stage"]),
                "skill": {
                    "name": name,
                    "required_capabilities": list(skill["provides"]),
                    "invocation_mode": invocation_mode,
                },
                "permissions": [],
                "version": skill["version"],
                "enabled": True,
                "tags": list(skill.get("tags", [])),
                "description": skill.get("description", ""),
                "priority": skill.get("priority", 0),
                "invocation_mode": invocation_mode,
                "requires": list(skill.get("requires", [])),
                "conflicts": list(skill.get("conflicts", [])),
                "trust": skill.get("trust", "untrusted"),
                "source": skill.get("path", ""),
                "origin": skill.get("origin", "bundled"),
                "managed_source": "skill_pool",
            }
            if skill.get("source_commit"):
                entry["source_commit"] = skill["source_commit"]
            entries.append(entry)
    return entries


def build_registry(registry_path: Path = REGISTRY_PATH) -> dict[str, Any]:
    current = _load(registry_path)
    preserved = [
        item for item in current.get("capabilities", [])
        if item.get("managed_source") != "skill_pool"
    ]
    capabilities = preserved + _skill_entries()
    capabilities.sort(key=lambda item: item["name"])
    return {
        "schema_version": 1,
        "providers": current.get("providers", []),
        "capabilities": capabilities,
    }


def _text(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail when the checked-in registry is stale")
    parser.add_argument("--write", action="store_true", help="write the generated registry")
    args = parser.parse_args()
    generated = _text(build_registry())
    current = REGISTRY_PATH.read_text(encoding="utf-8")
    if args.write:
        REGISTRY_PATH.write_text(generated, encoding="utf-8")
    if args.check and current != generated:
        print("Capability registry is stale; run sync_capability_pool.py --write")
        return 1
    print(json.dumps({
        "skills_migrated": len(_skill_entries()),
        "capabilities_total": len(build_registry()["capabilities"]),
        "registry": str(REGISTRY_PATH),
        "status": "updated" if args.write else "in_sync" if current == generated else "stale",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
