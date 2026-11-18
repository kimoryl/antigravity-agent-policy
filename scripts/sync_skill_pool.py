"""Snapshot installed Skills and their resources for on-demand use."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections import Counter
from hashlib import sha256
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CACHE = Path.home() / ".codex" / "plugins" / "cache"
STAGES = ["planning", "execution", "verification"]


def description(skill_file: Path) -> tuple[str, str]:
    lines = skill_file.read_text(encoding="utf-8-sig").splitlines()
    if not lines or lines[0].strip() != "---":
        return skill_file.parent.name, ""
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), len(lines))
    metadata = lines[1:end]
    name = skill_file.parent.name
    summary = ""
    for index, line in enumerate(metadata):
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip('"\'') or name
        if line.startswith("description:"):
            value = line.split(":", 1)[1].strip()
            if value in {"|", ">", "|-", ">-"}:
                summary = " ".join(part.strip() for part in metadata[index + 1:] if part.startswith("  ")).strip()
            else:
                summary = value.strip('"\'')
            break
    return name, re.sub(r"\s+", " ", summary)[:500]


def installed_plugins() -> list[dict]:
    command = subprocess.run(["codex", "plugin", "list", "--json"], capture_output=True,
                             text=True, check=True, timeout=45,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0)
    return [item for item in json.loads(command.stdout)["installed"] if item.get("enabled")]


def sources() -> list[tuple[str, str, Path]]:
    found: list[tuple[str, str, Path]] = []
    personal = Path.home() / ".codex" / "skills"
    if personal.is_dir():
        for folder in personal.iterdir():
            if not folder.is_dir() or folder.name == ".system":
                continue
            candidates = [folder / "SKILL.md", *(folder / "skills").glob("*/SKILL.md")]
            found.extend(("personal-codex", "local", file) for file in candidates if file.is_file())
    agents = Path.home() / ".agents" / "skills"
    if agents.is_dir():
        found.extend(("personal-agents", "local", folder / "SKILL.md") for folder in agents.iterdir()
                     if (folder / "SKILL.md").is_file())
    for plugin in installed_plugins():
        if plugin["name"] == "codex-agent-policy":
            continue
        base = CACHE / plugin["marketplaceName"] / plugin["name"] / plugin["version"] / "skills"
        if base.is_dir():
            found.extend((plugin["pluginId"], plugin["version"], folder / "SKILL.md")
                         for folder in base.iterdir() if (folder / "SKILL.md").is_file())
    # This user-created plugin is exposed in Codex even though it is absent from `plugin list`.
    extra = CACHE / "created-by-me-remote" / "video-context-analyzer"
    if extra.is_dir():
        for version in extra.iterdir():
            base = version / "skills"
            if base.is_dir():
                found.extend(("video-context-analyzer@created-by-me-remote", version.name, folder / "SKILL.md")
                             for folder in base.iterdir() if (folder / "SKILL.md").is_file())
    return sorted({file.as_posix(): (origin, version, file) for origin, version, file in found}.values(),
                  key=lambda item: item[2].as_posix().casefold())


def build() -> dict:
    found = sources()
    pool = ROOT / "skill_pool"
    tags_by_name = json.loads((pool / "tags.json").read_text(encoding="utf-8"))
    providers = pool / "providers"
    providers.mkdir(parents=True, exist_ok=True)
    previous_path = pool / "registry.json"
    previous = json.loads(previous_path.read_text(encoding="utf-8"))["skills"] if previous_path.is_file() else []
    base_names = [re.sub(r"[^a-z0-9-]+", "-", file.parent.name.lower()).strip("-") for _, _, file in found]
    repeated = Counter(base_names)
    used_names: set[str] = set()
    used_capabilities: set[str] = set()
    copied_roots: set[str] = set()
    items = []
    for (origin, source_version, file), base in zip(found, base_names):
        prefix = re.sub(r"[^a-z0-9-]+", "-", origin.lower().replace("@", "-")).strip("-")
        name = f"{prefix}-{base}" if repeated[base] > 1 else base
        if name in used_names:
            name += "-" + sha256(file.as_posix().encode()).hexdigest()[:8]
        used_names.add(name)
        slug = re.sub(r"[^a-z0-9]+", "", name)
        capability = f"skill.{slug}"
        if capability in used_capabilities:
            capability += "x" + sha256(name.encode()).hexdigest()[:6]
        used_capabilities.add(capability)
        original_name, summary = description(file)
        digest = sha256(file.read_bytes()).hexdigest()[:12]
        actual = file.resolve()
        candidate_root = actual.parents[2] if actual.parent.parent.name == "skills" else actual.parent
        home_roots = {Path.home() / ".codex", Path.home() / ".agents"}
        provider_root = actual.parent if candidate_root in home_roots else candidate_root
        provider_name = re.sub(r"[^a-z0-9-]+", "-", f"{origin}-{provider_root.name}".lower()).strip("-")
        destination_root = providers / provider_name
        package = destination_root / "skills" / file.parent.name
        shutil.copytree(actual.parent, package, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".git", ".venv", "node_modules", "__pycache__", "*.pyc"))
        if provider_root != actual.parent and provider_name not in copied_roots:
            for shared in ("docs", "references", "scripts", "bin", "assets", "src", "packages"):
                source = provider_root / shared
                if source.is_dir():
                    shutil.copytree(source, destination_root / shared, dirs_exist_ok=True,
                                    ignore=shutil.ignore_patterns(".git", ".venv", "node_modules", "__pycache__", "*.pyc"))
            copied_roots.add(provider_name)
        items.append({
            "name": name,
            "description": summary or f"Use the {original_name} workflow when requested.",
            "tags": tags_by_name.get(name, ["uncategorized"]),
            "provides": [capability], "stage": STAGES, "requires": [], "conflicts": [],
            "priority": 50, "context_requirements": [],
            "version": f"{source_version}+{digest}", "trust": "untrusted",
            "path": f"providers/{provider_name}/skills/{file.parent.name}/SKILL.md",
            "source_path": file.as_posix(),
            "origin": origin, "aliases": [original_name],
        })
    # An installed plugin cache may be evicted between runs. Keep a verified
    # snapshot already in the pool until its provider can be refreshed.
    for old in previous:
        if old["name"] not in used_names and (pool / old["path"]).is_file():
            old["tags"] = tags_by_name.get(old["name"], ["uncategorized"])
            items.append(old)
            used_capabilities.update(old["provides"])
    return {"schema_version": 1, "capabilities": sorted(used_capabilities), "skills": items}


def main() -> None:
    registry = build()
    pool = ROOT / "skill_pool"
    pool.mkdir(exist_ok=True)
    (pool / "registry.json").write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"indexed": len(registry["skills"]), "registry": str(pool / "registry.json")}))


if __name__ == "__main__":
    main()
