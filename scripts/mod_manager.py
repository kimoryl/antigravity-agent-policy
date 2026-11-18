"""Unified Mod Package & Cross-Agent Mount Manager for Antigravity, Codex CLI, and Claude Code CLI."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

import tomllib

MCP_SERVER_SHORT_KEY = "ap"
LEGACY_MCP_SERVER_KEYS = ("agent-policy", "agent_policy")
SOURCE_PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _real_user_home(target_home: Path | None = None) -> Path:
    """Resolve explicit target_home first; otherwise derive user home directory safely."""
    if target_home is not None:
        return Path(target_home)
    if os.name == "nt":
        for env_key in ("LOCALAPPDATA", "APPDATA"):
            raw = os.environ.get(env_key, "").strip()
            if raw:
                p = Path(raw)
                if p.parent.name.lower() == "appdata" and p.parent.parent.is_dir():
                    return p.parent.parent
    return Path.home()


MODS_ROOT = Path(os.environ.get("AGY_MODS_DIR", str(SOURCE_PLUGIN_ROOT / "mods")))
REAL_CODEX_HOME = _real_user_home() / ".codex"
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(REAL_CODEX_HOME)))
AGY_HOME = Path(os.environ.get("AGY_CLI_HOME", str(_real_user_home() / ".antigravity-cli-home")))
OPUS_HOME = Path(os.environ.get("OPUS_CLI_HOME", str(_real_user_home() / ".opus-cli-home")))
DESKTOP_PLUGINS = _real_user_home() / ".gemini" / "config" / "plugins"
_DEFAULT_STATE_BASE = (
    (_real_user_home() / "AppData" / "Local" / "codex-agent-policy")
    if os.name == "nt"
    else (_real_user_home() / ".local" / "state" / "codex-agent-policy")
)
SHARED_POLICY_DATA = Path(os.environ.get("AGY_POLICY_DATA") or os.environ.get("CODEX_AGENT_POLICY_DATA") or str(_DEFAULT_STATE_BASE))
PYTHON_EXE = sys.executable or "python"

PACKAGES_DIR = MODS_ROOT / ".packages"
MOUNTS_FILE = MODS_ROOT / "mounts.json"
POLICY_PLUGIN_ROOT = SOURCE_PLUGIN_ROOT if (SOURCE_PLUGIN_ROOT / "plugin.json").is_file() else (DESKTOP_PLUGINS / "antigravity-agent-policy")
ARCHIVED_PLUGINS_DIR = _real_user_home() / ".antigravity-agent-policy" / "plugins_archived"


def _resolve_mcp_server_script() -> Path:
    """Resolve active mcp_server.py path from the plugin root."""
    src_script = SOURCE_PLUGIN_ROOT / "scripts" / "mcp_server.py"
    if src_script.is_file():
        return src_script
    return POLICY_PLUGIN_ROOT / "scripts" / "mcp_server.py"


def resolve_policy_mcp_server_entry(config_data: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Read MCP server config entry supporting both new short key 'ap' and legacy 'agent-policy'."""
    servers = config_data.get("mcpServers") if isinstance(config_data, dict) else None
    if not isinstance(servers, dict):
        return None
    for candidate in (MCP_SERVER_SHORT_KEY, *LEGACY_MCP_SERVER_KEYS):
        entry = servers.get(candidate)
        if isinstance(entry, dict):
            return candidate, entry
    return None

CORE_MOD_IDS: list[str] = [
    "code-intelligence",
    "source-connectors",
    "ambitious-deep-thinking",
    "quota-monitor",
    "context7-sdk-verifier",
]

MOD_PROFILES: dict[str, list[str]] = {
    "core": list(CORE_MOD_IDS),
    "coding": [
        *CORE_MOD_IDS,
        "dev-setup-workflows",
        "github-suite",
    ],
    "mobile": [
        *CORE_MOD_IDS,
        "android-suite",
        "github-suite",
    ],
    "web": [
        *CORE_MOD_IDS,
        "chrome-devtools",
        "ui-aesthetic-studio",
        "anatomy-motion-studio",
    ],
    "office": [
        *CORE_MOD_IDS,
        "office-report-studio",
        "ppt-master",
        "anatomy-motion-studio",
        "engineering-bid-auditor",
    ],
    "automation": [
        *CORE_MOD_IDS,
        "windows-uia-automation",
        "office-report-studio",
    ],
    "ops": [
        *CORE_MOD_IDS,
        "win-system-guardian",
        "windows-uia-automation",
    ],
}


def infer_mod_scene_profiles(mod_id: str, manifest_data: dict[str, Any] | None = None) -> list[str]:
    """Automatically infer target scene profile keys (e.g. ['coding', 'office']) for any existing or newly-incubated Mod."""
    data = manifest_data or {}
    explicit: list[str] = []
    for key in ("profiles", "scene_profiles", "profile"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            explicit.append(val.strip().lstrip("@").lower())
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, str) and item.strip():
                    explicit.append(item.strip().lstrip("@").lower())
    valid_Explicit = [p for p in explicit if p in MOD_PROFILES and p != "core"]
    if valid_Explicit:
        return list(dict.fromkeys(valid_Explicit))

    haystack = " ".join(
        [
            str(mod_id),
            str(data.get("category", "")),
            str(data.get("name", "")),
            str(data.get("description", "")),
            " ".join(str(t) for t in (data.get("tags") or [])),
        ]
    ).lower()

    matched: list[str] = []
    profile_keywords: list[tuple[str, tuple[str, ...]]] = [
        ("automation", ("uia", "automation", "desktop", "workflow", "pipeline", "桌面", "自动化", "控件")),
        ("office", ("office", "ppt", "pptx", "docx", "word", "excel", "xlsx", "pdf", "report", "bid", "tender", "gdrive", "标书", "招投标", "报告", "三线表", "文档")),
        ("mobile", ("mobile", "android", "adb", "apk", "手机", "移动端")),
        ("web", ("web", "chrome", "devtools", "cdp", "ui", "frontend", "css", "oklch", "svg", "canvas", "remotion", "live-panel", "dashboard", "看板", "网页", "前端", "动效")),
        ("ops", ("ops", "guardian", "system", "disk", "port", "gcloud", "cloud-run", "persona", "系统", "运维", "磁盘", "守护", "人设")),
        ("coding", ("coding", "code", "poc", "sandbox", "patch", "unittest", "test", "git", "github", "dev", "workflow", "ast", "代码", "单测", "沙盒", "补丁")),
    ]
    for prof_key, kw_tuple in profile_keywords:
        if any(kw in haystack for kw in kw_tuple):
            matched.append(prof_key)
    return matched if matched else ["coding"]


def get_effective_mod_profiles() -> dict[str, list[str]]:
    """Return MOD_PROFILES dynamically enriched with any newly-incubated Mods present in MODS_ROOT."""
    effective: dict[str, list[str]] = {k: list(v) for k, v in MOD_PROFILES.items()}
    known_assigned = {mid for mids in effective.values() for mid in mids}
    if MODS_ROOT.is_dir():
        for child in sorted(MODS_ROOT.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            mfile = child / "mod.json"
            if not mfile.is_file():
                continue
            try:
                mdata = json.loads(mfile.read_text(encoding="utf-8", errors="ignore"))
                mid = str(mdata.get("id") or child.name).strip()
            except Exception:
                mid = child.name
                mdata = {}
            inferred_profs = infer_mod_scene_profiles(mid, mdata)
            if mid not in known_assigned or any(k in mdata for k in ("profiles", "scene_profiles", "profile")):
                for pkey in inferred_profs:
                    if pkey in effective and mid not in effective[pkey]:
                        effective[pkey].append(mid)
    for pkey, mids in effective.items():
        MOD_PROFILES[pkey] = mids
    return effective

KNOWN_AGENTS = {
    "agy-cli": {
        "id": "agent-agy-cli",
        "key": "agy-cli",
        "name": "AGY-CLI",
        "provider": "antigravity",
        "aliases": ["agy-cli", "agy", "agy_cli", "antigravity", "agent-agy-cli"],
    },
    "codex-cli": {
        "id": "agent-codex-cli",
        "key": "codex-cli",
        "name": "Codex-CLI",
        "provider": "codex",
        "aliases": ["codex-cli", "codex", "codex_cli", "agent-codex-cli"],
    },
    "opus-cli": {
        "id": "agent-opus-cli",
        "key": "opus-cli",
        "name": "Claude-Code-CLI",
        "provider": "claude",
        "aliases": ["opus-cli", "opus", "claude-code-cli", "claude_code_cli", "claude", "agent-opus-cli"],
    },
}


def expand_mod_ids(mod_ids: str | list[str]) -> list[str]:
    """Expand @profile / profile:name shortcuts into concrete Mod IDs (including newly-incubated Mods)."""
    profiles_map = get_effective_mod_profiles()
    raw_list = [mod_ids] if isinstance(mod_ids, str) else list(mod_ids)
    expanded: list[str] = []
    for item in raw_list:
        s = str(item).strip()
        if not s:
            continue
        low = s.lower()
        profile_key = None
        if low.startswith("@"):
            profile_key = low[1:]
        elif low.startswith("profile:"):
            profile_key = low.split(":", 1)[1].strip()
        elif low in profiles_map:
            profile_key = low
        if profile_key and profile_key in profiles_map:
            for mid in profiles_map[profile_key]:
                if mid not in expanded:
                    expanded.append(mid)
        else:
            if s not in expanded:
                expanded.append(s)
    return expanded


class ModError(ValueError):
    """Raised when a Mod package or mount operation is invalid."""


def normalize_agent_key(agent_ref: str | None) -> str:
    if not agent_ref or not str(agent_ref).strip():
        return "all"
    raw = str(agent_ref).strip().lower()
    if raw in {"all", "*", "workspace", "global"}:
        return "all"
    for key, info in KNOWN_AGENTS.items():
        if raw == key or raw in [a.lower() for a in info["aliases"]] or raw == info["name"].lower():
            return key
    return raw


def _db_conn() -> sqlite3.Connection:
    SHARED_POLICY_DATA.mkdir(parents=True, exist_ok=True)
    db_path = SHARED_POLICY_DATA / "runtime.sqlite3"
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mod_mounts (
            agent_key TEXT NOT NULL,
            mod_id TEXT NOT NULL,
            mounted_at REAL NOT NULL,
            PRIMARY KEY (agent_key, mod_id)
        )
        """
    )
    return conn


def load_mounts() -> dict[str, list[str]]:
    MODS_ROOT.mkdir(parents=True, exist_ok=True)
    data: dict[str, list[str]] = {}
    if MOUNTS_FILE.is_file():
        try:
            raw = json.loads(MOUNTS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                mounts_src = raw.get("mounts") if isinstance(raw.get("mounts"), dict) else raw
                for k, v in mounts_src.items():
                    nk = normalize_agent_key(str(k))
                    if nk == "all" or not isinstance(v, list):
                        continue
                    data.setdefault(nk, [])
                    for x in v:
                        sx = str(x).strip()
                        if sx and sx not in data[nk]:
                            data[nk].append(sx)
        except Exception:
            data = {}
    if not data:
        try:
            with _db_conn() as conn:
                rows = conn.execute("SELECT agent_key, mod_id FROM mod_mounts ORDER BY mounted_at ASC").fetchall()
                for agent_key, mod_id in rows:
                    data.setdefault(agent_key, [])
                    if mod_id not in data[agent_key]:
                        data[agent_key].append(mod_id)
        except Exception:
            pass
    for key in KNOWN_AGENTS:
        data.setdefault(key, [])
    return data


def save_mounts(mounts: dict[str, list[str]]) -> dict[str, list[str]]:
    MODS_ROOT.mkdir(parents=True, exist_ok=True)
    cleaned: dict[str, list[str]] = {}
    for key in KNOWN_AGENTS:
        cleaned[key] = []
    for k, v in mounts.items():
        nk = normalize_agent_key(k)
        if nk == "all":
            continue
        cleaned.setdefault(nk, [])
        for item in v:
            s = str(item).strip()
            if s and s not in cleaned[nk]:
                cleaned[nk].append(s)

    orchestrator_meta = {
        "name": "Antigravity-Master",
        "role": "master-orchestrator",
    }
    if MOUNTS_FILE.is_file():
        try:
            existing_raw = json.loads(MOUNTS_FILE.read_text(encoding="utf-8"))
            if isinstance(existing_raw, dict) and isinstance(existing_raw.get("orchestrator"), dict):
                orchestrator_meta = existing_raw["orchestrator"]
        except Exception:
            pass

    payload = {
        "schema_version": 1,
        "updated_at": time.time(),
        "orchestrator": orchestrator_meta,
        "agents": KNOWN_AGENTS,
        "mounts": cleaned,
        "profiles": MOD_PROFILES,
    }
    mounts_json_text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    MOUNTS_FILE.write_text(mounts_json_text, encoding="utf-8")

    sync_dir_env = os.environ.get("AGY_MODS_SYNC_DIR", "").strip()
    if sync_dir_env:
        try:
            sdir = Path(sync_dir_env)
            sdir.mkdir(parents=True, exist_ok=True)
            (sdir / "mounts.json").write_text(mounts_json_text, encoding="utf-8")
        except OSError:
            pass

    try:
        with _db_conn() as conn:
            conn.execute("DELETE FROM mod_mounts")
            now = time.time()
            for agent_key, mod_ids in cleaned.items():
                for idx, mod_id in enumerate(mod_ids):
                    conn.execute(
                        "INSERT OR REPLACE INTO mod_mounts (agent_key, mod_id, mounted_at) VALUES (?, ?, ?)",
                        (agent_key, mod_id, now + idx * 0.001),
                    )
            conn.commit()
    except Exception:
        pass
    return cleaned


MOD_POISON_PATTERNS = [
    ("prompt_injection_ignore", re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions", re.I)),
    ("prompt_injection_disregard", re.compile(r"disregard\s+(?:all\s+)?(?:previous|prior)\s+(?:instructions|rules)", re.I)),
    ("system_prompt_override", re.compile(r"system\s+prompt\s+override|<\s*system_override\s*>", re.I)),
    ("remote_pipe_shell", re.compile(r"(?:curl|wget)\s+https?://[^\s|]+\|\s*(?:ba)?sh", re.I)),
    ("hidden_exfiltration", re.compile(r"send\s+(?:all\s+)?(?:env|credentials|private[_\s]key|api[_\s]key)\s+to\s+https?://", re.I)),
]


def compute_mod_fingerprint(mod_dir: Path) -> str:
    h = hashlib.sha256()
    if not mod_dir.is_dir():
        return h.hexdigest()
    for file_path in sorted(mod_dir.rglob("*")):
        if file_path.is_file() and "__pycache__" not in file_path.parts:
            rel = file_path.relative_to(mod_dir).as_posix()
            h.update(rel.encode("utf-8") + b"\0")
            try:
                h.update(file_path.read_bytes() + b"\0")
            except OSError:
                pass
    return h.hexdigest()


def scan_mod_safety(mod_dir: Path, data: dict[str, Any] | None = None) -> dict[str, Any]:
    if data is None and (mod_dir / "mod.json").is_file():
        try:
            data = json.loads((mod_dir / "mod.json").read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data = data or {}
    issues: list[dict[str, str]] = []
    texts_to_scan: list[tuple[str, str]] = [
        ("mod.json:description", str(data.get("description", ""))),
    ]
    for tool in data.get("mcp_tools", []):
        if isinstance(tool, dict):
            texts_to_scan.append((f"mcp_tool:{tool.get('name', 'unknown')}", json.dumps(tool, ensure_ascii=False)))
    skills_dir = mod_dir / "skills"
    if skills_dir.is_dir():
        for smd in sorted(skills_dir.rglob("SKILL.md")):
            try:
                texts_to_scan.append((smd.relative_to(mod_dir).as_posix(), smd.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                pass
    for source, text in texts_to_scan:
        for rule_id, pattern in MOD_POISON_PATTERNS:
            if pattern.search(text):
                issues.append({"rule": rule_id, "source": source})
    return {
        "safety_verified": len(issues) == 0,
        "safety_issues": issues,
        "sha256_fingerprint": compute_mod_fingerprint(mod_dir),
    }


def load_mod_manifest(mod_dir: Path) -> dict[str, Any]:
    manifest_file = mod_dir / "mod.json"
    if not manifest_file.is_file():
        raise ModError(f"Missing mod.json in {mod_dir}")
    data = json.loads(manifest_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data.get("id"):
        raise ModError(f"Invalid mod.json in {mod_dir}")
    mod_id = str(data["id"]).strip()
    skills_dir = mod_dir / "skills"
    skill_items = data.get("skills", [])
    if not skill_items and skills_dir.is_dir():
        for sdir in sorted(skills_dir.iterdir()):
            if sdir.is_dir() and (sdir / "SKILL.md").is_file():
                skill_items.append({"name": sdir.name, "path": f"skills/{sdir.name}/SKILL.md"})
    mcp_servers = data.get("mcp_servers", {})
    mcp_tools = data.get("mcp_tools", [])
    rules = data.get("rules", [])
    package_zip = PACKAGES_DIR / f"{mod_id}.mod.zip"
    safety = scan_mod_safety(mod_dir, data)
    return {
        "id": mod_id,
        "name": data.get("name", mod_id),
        "version": data.get("version", "1.0.0"),
        "category": data.get("category", "general"),
        "description": data.get("description", ""),
        "tags": data.get("tags", []),
        "skills": skill_items,
        "skill_count": len(skill_items),
        "mcp_servers": mcp_servers,
        "mcp_server_names": list(mcp_servers.keys()) if isinstance(mcp_servers, dict) else [],
        "mcp_tools": mcp_tools,
        "mcp_tool_count": len(mcp_tools),
        "rules": rules,
        "rule_count": len(rules) if isinstance(rules, list) else 0,
        "plugin_extensions": data.get("plugin_extensions"),
        "capabilities": {
            "mcp_tools": [t.get("name") if isinstance(t, dict) else str(t) for t in mcp_tools],
            "skills": [s.get("name") if isinstance(s, dict) else str(s) for s in skill_items],
        },
        "path": str(mod_dir),
        "mod_dir": str(mod_dir),
        "package_path": str(package_zip) if package_zip.is_file() else None,
        "packaged": package_zip.is_file(),
        "package_exists": package_zip.is_file(),
        "sha256_fingerprint": safety["sha256_fingerprint"],
        "safety_verified": safety["safety_verified"],
        "safety_issues": safety["safety_issues"],
    }


def list_all_mods() -> list[dict[str, Any]]:
    MODS_ROOT.mkdir(parents=True, exist_ok=True)
    mounts = load_mounts()
    mods: list[dict[str, Any]] = []
    for child in sorted(MODS_ROOT.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if not (child / "mod.json").is_file():
            continue
        try:
            item = load_mod_manifest(child)
            mounted_by = [
                {
                    "agent_key": agent_key,
                    "agent_id": KNOWN_AGENTS.get(agent_key, {}).get("id", agent_key),
                    "agent_name": KNOWN_AGENTS.get(agent_key, {}).get("name", agent_key),
                }
                for agent_key, mod_list in mounts.items()
                if item["id"] in mod_list
            ]
            item["mounted_by"] = mounted_by
            agent_refs: list[str] = []
            for m in mounted_by:
                for ref in (m["agent_id"], m["agent_key"], m["agent_name"]):
                    if ref and ref not in agent_refs:
                        agent_refs.append(ref)
            item["mounted_agents"] = agent_refs
            item["mounted"] = bool(mounted_by)
            mods.append(item)
        except Exception as exc:
            mods.append({"id": child.name, "name": child.name, "status": "invalid", "error": str(exc)})
    return mods


def catalog_mods(
    query: str = "",
    *,
    category: str | None = None,
    tag: str | None = None,
    agent_id: str | None = None,
    mounted_only: bool = False,
) -> dict[str, Any]:
    mods = list_all_mods()
    mounts = load_mounts()
    target_agent = normalize_agent_key(agent_id) if agent_id else None
    q = (query or "").strip().casefold()
    filtered: list[dict[str, Any]] = []
    for item in mods:
        if item.get("status") == "invalid":
            continue
        if category and item.get("category", "").casefold() != category.strip().casefold():
            continue
        if tag and tag.strip().casefold() not in {t.casefold() for t in item.get("tags", [])}:
            continue
        if target_agent and target_agent != "all":
            is_mounted = item["id"] in mounts.get(target_agent, [])
            item["mounted_for_agent"] = is_mounted
            item["mounted"] = is_mounted
            if mounted_only and not is_mounted:
                continue
        elif mounted_only and not item.get("mounted_agents"):
            continue
        if q:
            haystack = json.dumps(
                {
                    "id": item["id"],
                    "name": item["name"],
                    "description": item["description"],
                    "category": item.get("category"),
                    "tags": item.get("tags"),
                    "skills": [s.get("name") if isinstance(s, dict) else s for s in item.get("skills", [])],
                    "mcp_tools": [t.get("name") if isinstance(t, dict) else t for t in item.get("mcp_tools", [])],
                },
                ensure_ascii=False,
            ).casefold()
            if q not in haystack:
                continue
        filtered.append(item)
    mounted_count = sum(1 for m in filtered if m.get("mounted"))
    return {
        "status": "ok",
        "mods": filtered,
        "count": len(filtered),
        "mounted_count": mounted_count,
        "total_mods": len(mods),
        "mounts": mounts,
        "agents": KNOWN_AGENTS,
        "mods_root": str(MODS_ROOT),
        "mods_dir": str(MODS_ROOT),
    }


def get_mod_info(mod_id: str, *, skill_name: str | None = None, include_skill_content: bool = False) -> dict[str, Any]:
    mod_dir = MODS_ROOT / str(mod_id).strip()
    if not mod_dir.is_dir() or not (mod_dir / "mod.json").is_file():
        raise ModError(f"Mod not found: {mod_id}")
    info = load_mod_manifest(mod_dir)
    mounts = load_mounts()
    info["mounted_agents"] = [k for k, v in mounts.items() if info["id"] in v]
    if skill_name:
        target_skill = str(skill_name).strip()
        skill_md = mod_dir / "skills" / target_skill / "SKILL.md"
        if not skill_md.is_file():
            # Also check workflows dir if present
            wf_md = mod_dir / "workflows" / target_skill / "WORKFLOW.md"
            if wf_md.is_file():
                skill_md = wf_md
            else:
                raise ModError(f"Skill or workflow '{target_skill}' not found in Mod '{mod_id}'")
        info["selected_skill"] = {
            "name": target_skill,
            "path": str(skill_md),
            "content": skill_md.read_text(encoding="utf-8"),
        }
    elif include_skill_content:
        contents: dict[str, str] = {}
        for s in info.get("skills", []):
            sname = s["name"] if isinstance(s, dict) else str(s)
            sfile = mod_dir / "skills" / sname / "SKILL.md"
            if sfile.is_file():
                contents[sname] = sfile.read_text(encoding="utf-8")
        info["skill_contents"] = contents
    return info


def pack_mod(mod_id: str, source_dir: str | Path | None = None) -> dict[str, Any]:
    MODS_ROOT.mkdir(parents=True, exist_ok=True)
    PACKAGES_DIR.mkdir(parents=True, exist_ok=True)
    if source_dir is not None:
        src = Path(source_dir).resolve()
        if not src.is_dir():
            raise ModError(f"Source directory does not exist: {src}")
        dest = MODS_ROOT / mod_id
        if src != dest.resolve():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
    mod_dir = MODS_ROOT / mod_id
    info = load_mod_manifest(mod_dir)
    zip_path = PACKAGES_DIR / f"{info['id']}.mod.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for file_path in sorted(mod_dir.rglob("*")):
            if file_path.is_file() and "__pycache__" not in file_path.parts:
                rel = file_path.relative_to(mod_dir).as_posix()
                zf.write(file_path, f"{info['id']}/{rel}")
    sha256 = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    return {
        "status": "ok",
        "action_status": "packed",
        "mod_id": info["id"],
        "name": info["name"],
        "version": info["version"],
        "package_path": str(zip_path),
        "sha256": sha256,
        "size_bytes": zip_path.stat().st_size,
    }


def _safe_extract_zip(zf: zipfile.ZipFile, dest_dir: Path) -> None:
    """Validate every archive member against Zip Slip traversal before extracting."""
    dest_resolved = dest_dir.resolve()
    for member in zf.infolist():
        raw_name = member.filename
        norm_name = raw_name.replace("\\", "/")
        parts = [p for p in norm_name.split("/") if p]
        if (
            norm_name.startswith("/")
            or ":" in norm_name.split("/")[0]
            or ".." in parts
        ):
            raise ModError(f"Unsafe path in Mod archive (Zip Slip blocked): {raw_name}")
        target = (dest_dir / norm_name).resolve()
        if target != dest_resolved and not target.is_relative_to(dest_resolved):
            raise ModError(f"Unsafe path in Mod archive (Zip Slip blocked): {raw_name}")
    for member in zf.infolist():
        zf.extract(member, dest_dir)


def install_mod(source_path: str | Path, *, overwrite: bool = True, agent_id: str | None = None) -> dict[str, Any]:
    src = Path(source_path).resolve()
    if not src.exists():
        raise ModError(f"Mod source does not exist: {src}")
    MODS_ROOT.mkdir(parents=True, exist_ok=True)
    PACKAGES_DIR.mkdir(parents=True, exist_ok=True)

    if src.is_file() and src.suffix.lower() in {".zip", ".mod", ".skill"}:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with zipfile.ZipFile(src, "r") as zf:
                _safe_extract_zip(zf, tmp_path)
            candidates = list(tmp_path.rglob("mod.json"))
            if candidates:
                mod_root = candidates[0].parent
                manifest = json.loads(candidates[0].read_text(encoding="utf-8"))
                mod_id = str(manifest["id"]).strip()
            else:
                # Wrap a raw skill zip into a standard Mod automatically
                skill_mds = list(tmp_path.rglob("SKILL.md"))
                if not skill_mds:
                    raise ModError("Archive contains neither mod.json nor SKILL.md")
                mod_id = src.stem.replace(".mod", "").lower().replace("_", "-")
                mod_root = tmp_path / "_wrapped_mod"
                mod_root.mkdir(parents=True, exist_ok=True)
                skills_list = []
                for smd in skill_mds:
                    sname = smd.parent.name if smd.parent != tmp_path else mod_id
                    target_sdir = mod_root / "skills" / sname
                    shutil.copytree(smd.parent, target_sdir, dirs_exist_ok=True)
                    skills_list.append({"name": sname, "path": f"skills/{sname}/SKILL.md", "description": f"Imported skill {sname}"})
                manifest = {
                    "schema_version": 1,
                    "id": mod_id,
                    "name": mod_id,
                    "version": "1.0.0",
                    "category": "imported",
                    "description": f"从归档包 {src.name} 导入的封装 Mod",
                    "tags": ["imported"],
                    "skills": skills_list,
                    "mcp_servers": {},
                    "mcp_tools": [],
                    "rules": [],
                }
                (mod_root / "mod.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            dest = MODS_ROOT / mod_id
            if dest.exists():
                if not overwrite:
                    raise ModError(f"Mod already exists: {mod_id}")
                shutil.rmtree(dest)
            shutil.copytree(mod_root, dest)
            packed = pack_mod(mod_id)
            if agent_id:
                mount_mod(mod_id, agent_id)
            return {"status": "ok", "action_status": "installed", "mod_id": mod_id, "mod": load_mod_manifest(dest), "package": packed}

    if src.is_dir():
        if (src / "mod.json").is_file():
            manifest = json.loads((src / "mod.json").read_text(encoding="utf-8"))
            mod_id = str(manifest["id"]).strip()
            dest = MODS_ROOT / mod_id
            if src.resolve() != dest.resolve():
                if dest.exists():
                    if not overwrite:
                        raise ModError(f"Mod already exists: {mod_id}")
                    shutil.rmtree(dest)
                shutil.copytree(src, dest)
            packed = pack_mod(mod_id)
            if agent_id:
                mount_mod(mod_id, agent_id)
            return {"status": "ok", "action_status": "installed", "mod_id": mod_id, "mod": load_mod_manifest(dest), "package": packed}
        elif (src / "SKILL.md").is_file():
            mod_id = src.name.lower().replace("_", "-")
            dest = MODS_ROOT / mod_id
            if dest.exists():
                if not overwrite:
                    raise ModError(f"Mod already exists: {mod_id}")
                shutil.rmtree(dest)
            target_sdir = dest / "skills" / mod_id
            shutil.copytree(src, target_sdir)
            manifest = {
                "schema_version": 1,
                "id": mod_id,
                "name": mod_id,
                "version": "1.0.0",
                "category": "imported",
                "description": f"从目录 {src.name} 封装的 Mod",
                "tags": ["imported"],
                "skills": [{"name": mod_id, "path": f"skills/{mod_id}/SKILL.md", "description": f"Skill {mod_id}"}],
                "mcp_servers": {},
                "mcp_tools": [],
                "rules": [],
            }
            (dest / "mod.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            packed = pack_mod(mod_id)
            if agent_id:
                mount_mod(mod_id, agent_id)
            return {"status": "ok", "action_status": "installed", "mod_id": mod_id, "mod": load_mod_manifest(dest), "package": packed}
    raise ModError(f"Unsupported Mod source: {src}")


def uninstall_mod(mod_id: str, *, delete_package: bool = False) -> dict[str, Any]:
    target = str(mod_id).strip()
    mounts = load_mounts()
    for agent_key in list(mounts.keys()):
        if target in mounts[agent_key]:
            mounts[agent_key] = [m for m in mounts[agent_key] if m != target]
    save_mounts(mounts)
    sync_all_runtimes(mounts)

    mod_dir = MODS_ROOT / target
    removed_dir = False
    if mod_dir.is_dir():
        shutil.rmtree(mod_dir)
        removed_dir = True
    zip_path = PACKAGES_DIR / f"{target}.mod.zip"
    removed_pkg = False
    if delete_package and zip_path.is_file():
        zip_path.unlink()
        removed_pkg = True
    return {
        "status": "ok",
        "action_status": "uninstalled",
        "mod_id": target,
        "backup_package": str(zip_path),
        "removed_directory": removed_dir,
        "removed_package": removed_pkg,
        "mounts": mounts,
    }


def mount_mod(
    mod_ids: str | list[str],
    agent_id: str | None = "all",
    *,
    replace: bool = False,
) -> dict[str, Any]:
    ids = expand_mod_ids(mod_ids)
    for mid in ids:
        mod_dir = MODS_ROOT / mid
        pkg_zip = PACKAGES_DIR / f"{mid}.mod.zip"
        if not (mod_dir / "mod.json").is_file() and pkg_zip.is_file():
            install_mod(pkg_zip, overwrite=True)
        if not (mod_dir / "mod.json").is_file():
            raise ModError(f"Unknown Mod '{mid}'. Available mods: {[m['id'] for m in list_all_mods()]} or profiles: {list(MOD_PROFILES.keys())}")
        manifest = load_mod_manifest(mod_dir)
        if not manifest.get("safety_verified", True):
            raise ModError(f"Mod '{mid}' blocked by Tool Poisoning Guard: {manifest.get('safety_issues')}")

    mounts = load_mounts()
    agent_key = normalize_agent_key(agent_id)
    target_keys = list(KNOWN_AGENTS.keys()) if agent_key == "all" else [agent_key]

    for tk in target_keys:
        current = [] if replace else list(mounts.get(tk, []))
        for mid in ids:
            if mid not in current:
                current.append(mid)
        mounts[tk] = current

    saved = save_mounts(mounts)
    sync_report = sync_all_runtimes(saved)
    return {
        "status": "ok",
        "action_status": "mounted",
        "mounted_mods": ids,
        "available_profiles": MOD_PROFILES,
        "target_agents": target_keys,
        "mounts": saved,
        "runtime_sync": sync_report,
    }


def unmount_mod(
    mod_ids: str | list[str],
    agent_id: str | None = "all",
) -> dict[str, Any]:
    ids = set(expand_mod_ids(mod_ids))
    mounts = load_mounts()
    agent_key = normalize_agent_key(agent_id)
    target_keys = list(KNOWN_AGENTS.keys()) if agent_key == "all" else [agent_key]

    removed: dict[str, list[str]] = {}
    for tk in target_keys:
        before = list(mounts.get(tk, []))
        after = [m for m in before if m not in ids]
        removed[tk] = [m for m in before if m in ids]
        mounts[tk] = after

    saved = save_mounts(mounts)
    sync_report = sync_all_runtimes(saved)
    return {
        "status": "ok",
        "action_status": "unmounted",
        "unmounted_mods": sorted(ids),
        "removed_by_agent": removed,
        "target_agents": target_keys,
        "mounts": saved,
        "runtime_sync": sync_report,
    }


_TOML_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _format_toml_str(val: str) -> str:
    return json.dumps(str(val), ensure_ascii=False)


def _format_toml_key(key: str) -> str:
    s = str(key)
    return s if _TOML_BARE_KEY_RE.match(s) else json.dumps(s, ensure_ascii=False)


def _format_toml_value(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        return repr(val)
    if isinstance(val, str):
        return json.dumps(val, ensure_ascii=False)
    if isinstance(val, list):
        return "[" + ", ".join(_format_toml_value(x) for x in val) + "]"
    raise TypeError(f"Unsupported TOML value type: {type(val)}")


def _is_toml_table_array(val: Any) -> bool:
    return isinstance(val, list) and len(val) > 0 and all(isinstance(x, dict) for x in val)


def _emit_toml_table(prefix: list[str], tbl: dict[str, Any], out: list[str]) -> None:
    scalars: list[tuple[str, Any]] = []
    subtables: list[tuple[str, dict[str, Any]]] = []
    tarrays: list[tuple[str, list[dict[str, Any]]]] = []
    for k, v in tbl.items():
        if v is None:
            continue
        if isinstance(v, dict):
            subtables.append((k, v))
        elif _is_toml_table_array(v):
            tarrays.append((k, v))
        else:
            scalars.append((k, v))
    if prefix and (scalars or (not subtables and not tarrays)):
        if out:
            out.append("")
        out.append("[" + ".".join(_format_toml_key(p) for p in prefix) + "]")
    for k, v in scalars:
        out.append(f"{_format_toml_key(k)} = {_format_toml_value(v)}")
    for k, arr in tarrays:
        arr_prefix = [*prefix, k]
        for item in arr:
            if out:
                out.append("")
            out.append("[[" + ".".join(_format_toml_key(p) for p in arr_prefix) + "]]")
            item_scalars = [(ik, iv) for ik, iv in item.items() if iv is not None and not isinstance(iv, dict) and not _is_toml_table_array(iv)]
            for ik, iv in item_scalars:
                out.append(f"{_format_toml_key(ik)} = {_format_toml_value(iv)}")
            for ik, iv in item.items():
                if isinstance(iv, dict):
                    _emit_toml_table([*arr_prefix, ik], iv, out)
    for k, sub in subtables:
        _emit_toml_table([*prefix, k], sub, out)


def _dump_toml_document(data: dict[str, Any], managed_blocks: list[str] | None = None) -> str:
    lines: list[str] = []
    _emit_toml_table([], data, lines)
    for block in managed_blocks or []:
        if lines:
            lines.append("")
        lines.append(block)
    rendered = "\n".join(lines).strip() + "\n"
    # Verify syntactic validity before returning
    tomllib.loads(rendered)
    return rendered


def _known_mod_mcp_server_keys(underscore: bool = False) -> set[str]:
    keys: set[str] = {
        MCP_SERVER_SHORT_KEY,
        *LEGACY_MCP_SERVER_KEYS,
        *(m.replace("-", "_") if underscore else m for m in CORE_MOD_IDS),
    }
    if MODS_ROOT.is_dir():
        try:
            for child in MODS_ROOT.iterdir():
                mfile = child / "mod.json"
                if child.is_dir() and mfile.is_file():
                    try:
                        mdata = json.loads(mfile.read_text(encoding="utf-8", errors="ignore"))
                        for srv_name in (mdata.get("mcp_servers") or {}).keys():
                            keys.add(str(srv_name).replace("-", "_") if underscore else str(srv_name))
                            keys.add(str(srv_name))
                    except Exception:
                        continue
        except OSError:
            pass
    return keys


_ENV_PLACEHOLDER_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _resolve_mod_env_map(raw_env: dict[str, Any] | None, mod_dir: Path) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for ek, ev in (raw_env or {}).items():
        val_str = str(ev).replace("${MOD_DIR}", mod_dir.as_posix())
        m = _ENV_PLACEHOLDER_RE.match(val_str.strip())
        if m:
            env_name = m.group(1)
            actual = os.environ.get(env_name, "").strip()
            if not actual:
                # Omit unresolved placeholder so wrapper script falls back to its file-based credential chain cleanly
                continue
            resolved[str(ek)] = actual
        else:
            resolved[str(ek)] = val_str
    return resolved


def _resolve_mod_command(raw_cmd: str, mod_dir: Path) -> str:
    cmd = str(raw_cmd).replace("${MOD_DIR}", mod_dir.as_posix())
    low = cmd.strip().lower().replace("\\", "/")
    if low in {"python", "python3", "py"}:
        return PYTHON_EXE
    if os.name != "nt":
        if "python" in low and (low.startswith("c:/") or low.endswith(".exe")):
            return PYTHON_EXE
        if low.endswith("npx.cmd"):
            return "/usr/bin/npx"
        if low.endswith("node.exe"):
            return "/usr/bin/node"
    return cmd


def _build_agents_markdown(mounted_mod_ids: list[str], mounted_rule_texts: list[str], *, cli_label: str = "Codex-CLI") -> str:
    md_lines = [
        "# Global Runtime, Supervisor Layer, 3D Cross-Memory Matrix & 24-Mod System",
        "",
        "1. **Master-Subagent Architecture**: You operate as a specialized CLI subagent coordinated by the Master Antigravity Orchestrator.",
        "2. **3D Orthogonal Cross-Memory Matrix (`Project × Modality × Global`) & Sidecar Read/Write Isolation (`ap` MCP)**:",
        "   - You share `runtime.sqlite3` (`CODEX_AGENT_POLICY_DATA`), `05_Agent_Memory_Sync/L1_Projects/<project_id>.json` (5 Projects + `workbench_misc`), `L1_Modalities/<modality_id>.json` (5 Craft Modalities: `ppt_product_pitch`, `ui_aesthetic_design`, `office_report_doc`, `coding_and_testing`, `systems_and_automation`), and `L2_Experiences.json` with Antigravity (`agy-cli`) and Claude Code (`opus-cli`).",
        "   - **Read-Only Consumer / Single-Writer Sidecar Rule**: During coding, refactoring, testing, or product deliverable tasks, you operate as a **read-only consumer** of 3D memories (`sdk_fact_lookup(project_root=..., modalities=[...], task_intent=...)`, `fact_list`, `memory_recall`). Do NOT self-report subjective summaries directly into `L1_Projects/<id>.json` or `L1_Modalities/<id>.json`; the Master Orchestrator spawns a parallel Sidecar Reviewer (`记忆事实伴飞审查员`) to independently audit lockfiles, static AST signatures, and deliverable artifacts via `sdk_fact_reconcile_report` and commit verified facts/crafts via `sdk_memory_review_commit`.",
        "   - **Sidecar ON/OFF Dichotomy**: Sidecar ON for all approved complex deliverable tasks (code/contract R&D + product pitch/PPT/UI aesthetics/Office reports); Sidecar OFF for lightweight interactive Q&A, early brainstorming, or read-only status checks.",
        "3. **Scene-Based 24-Mod Mounting System (`mod_*` tools)**:",
        f"   - Currently Mounted Mods on {cli_label}: `{', '.join(mounted_mod_ids) if mounted_mod_ids else '(none)'}`",
        "   - Available Scene Profiles: `@core`, `@coding`, `@mobile`, `@web`, `@office`, `@automation`, `@ops`.",
        "   - Use `mod_catalog`, `mod_info`, `mod_mount`, `mod_unmount`, `mod_mounted`, `mod_call` via the `ap` MCP server to inspect, mount, or invoke encapsulated Mod packages across Agents.",
        "4. **Supervisor & Anchored Editing**:",
        "   - Prefer `hash_outline` (or `hash_read(mode='outline')`) for AST skeleton extraction, and `hash_read` / `hash_edit` for anchored code block modifications.",
        "   - Use `runtime_experience` (`propose` with `supersede_id`) for bi-temporal experience evolution, and check `_advisories` in MCP tool responses for real-time cross-session write conflict warnings.",
        "   - Default to Simplified Chinese (简体中文) for internal reasoning and user responses unless instructed otherwise.",
        "5. **ASD-STE100 Controlled Technical Expression (80% Strength)**:",
        "   - For user-facing operational steps, CLI instructions, and troubleshooting checklists: enforce one-word-one-meaning, zero filler/pleasantries, no vague modals ('maybe/probably/try to'), active imperative voice (`[Verb] + [Exact Target] + [Expected State]`), and one executable action per bullet (≤20 English words / ≤25 Chinese characters).",
        "   - **Exemption Boundary**: Exempt internal reasoning (`thinking`) and complex architectural trade-off analyses from vocabulary and sentence-length limits to preserve causal conditions (`unless` / `provided that`).",
    ]
    if mounted_rule_texts:
        md_lines.append("")
        md_lines.append("## Mounted Mod Rules")
        md_lines.extend(mounted_rule_texts)
    return "\n\n".join(md_lines) + "\n"


def _sync_single_codex_home(
    chome: Path,
    mounted_mod_ids: list[str],
    non_core_mcp_entries: dict[str, dict[str, Any]],
    mounted_rule_texts: list[str],
    managed_mcp_keys: set[str],
) -> tuple[list[str], list[str]]:
    """Safely update exact paths inside one Codex home without traversing root .guard lockfiles."""
    try:
        chome.mkdir(parents=True, exist_ok=True)
    except OSError:
        return [], []

    cfg_path = chome / "config.toml"
    managed_blocks: list[str] = []
    existing_data: dict[str, Any] = {}

    try:
        if cfg_path.is_file():
            raw_text = cfg_path.read_text(encoding="utf-8", errors="replace")
            try:
                (chome / "config.toml.bak").write_text(raw_text, encoding="utf-8")
            except OSError:
                pass
            in_block = False
            buf: list[str] = []
            for line in raw_text.splitlines():
                if line.startswith("# BEGIN policy-managed"):
                    in_block = True
                    buf = []
                if in_block:
                    buf.append(line)
                if in_block and line.startswith("# END policy-managed"):
                    in_block = False
                    managed_blocks.append("\n".join(buf))
            try:
                existing_data = tomllib.loads(raw_text)
            except Exception:
                existing_data = {}
    except OSError:
        existing_data = {}

    merged: dict[str, Any] = {
        "model": existing_data.get("model", "gpt-6.1-sol"),
        "model_reasoning_effort": existing_data.get("model_reasoning_effort", "high"),
        "service_tier": existing_data.get("service_tier", "default"),
        "web_search": existing_data.get("web_search", "cached"),
        "sandbox_mode": existing_data.get("sandbox_mode", "danger-full-access"),
        "model_reasoning_summary": existing_data.get("model_reasoning_summary", "concise"),
        "model_verbosity": existing_data.get("model_verbosity", "low"),
    }
    if merged["model"] == "gpt-5.4":
        merged["model"] = "gpt-6.1-sol"

    for k, v in existing_data.items():
        if k not in merged:
            merged[k] = v

    features = dict(merged.get("features") or {})
    features.setdefault("js_repl", False)
    features["hooks"] = True
    merged["features"] = features

    sww = dict(merged.get("sandbox_workspace_write") or {})
    sww.setdefault("network_access", True)
    merged["sandbox_workspace_write"] = sww

    # Clean deprecated local marketplace remnants
    if isinstance(merged.get("marketplaces"), dict):
        mkt = {
            mk: mv
            for mk, mv in merged["marketplaces"].items()
            if mk != "codex-agent-policy-local"
            and "codex-agent-policy-marketplace" not in json.dumps(mv, ensure_ascii=False)
        }
        if mkt:
            merged["marketplaces"] = mkt
        else:
            merged.pop("marketplaces", None)

    if isinstance(merged.get("plugins"), dict):
        plg = {
            pk: pv
            for pk, pv in merged["plugins"].items()
            if not str(pk).startswith("codex-agent-policy@")
        }
        if plg:
            merged["plugins"] = plg
        else:
            merged.pop("plugins", None)

    if isinstance(merged.get("hooks"), dict) and isinstance(merged["hooks"].get("state"), dict):
        hstate = {
            hk: hv
            for hk, hv in merged["hooks"]["state"].items()
            if not str(hk).startswith("codex-agent-policy@")
        }
        if hstate:
            merged["hooks"] = {**merged["hooks"], "state": hstate}
        else:
            rest_hooks = {hk: hv for hk, hv in merged["hooks"].items() if hk != "state"}
            if rest_hooks:
                merged["hooks"] = rest_hooks
            else:
                merged.pop("hooks", None)

    if isinstance(merged.get("skills"), dict) and isinstance(merged["skills"].get("config"), list):
        cleaned_skill_cfg = []
        for item in merged["skills"]["config"]:
            if not isinstance(item, dict):
                continue
            p_str = str(item.get("path") or "")
            if "deprecated-marketplace" in p_str or "codex-agent-policy-marketplace" in p_str:
                continue
            cleaned_skill_cfg.append(item)
        if cleaned_skill_cfg:
            merged["skills"] = {**merged["skills"], "config": cleaned_skill_cfg}
        else:
            rest_skills = {sk: sv for sk, sv in merged["skills"].items() if sk != "config"}
            if rest_skills:
                merged["skills"] = rest_skills
            else:
                merged.pop("skills", None)

    # Preserve non-Mod user MCP servers (e.g. jev, node_repl) while refreshing ap + non-@core mounted Mods
    user_mcp: dict[str, Any] = {}
    for mk, mv in (merged.get("mcp_servers") or {}).items():
        if mk not in managed_mcp_keys and isinstance(mv, dict):
            user_mcp[mk] = mv

    mcp_script = _resolve_mcp_server_script().as_posix()
    new_mcp: dict[str, Any] = {
        MCP_SERVER_SHORT_KEY: {
            "command": PYTHON_EXE,
            "args": ["-X", "utf8", mcp_script],
            "env": {
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
                "CODEX_AGENT_POLICY_DATA": SHARED_POLICY_DATA.as_posix(),
                "AGY_MODS_DIR": MODS_ROOT.as_posix(),
            },
        }
    }
    for srv_key, srv_val in non_core_mcp_entries.items():
        new_mcp[srv_key] = srv_val
    for uk, uv in user_mcp.items():
        if uk not in new_mcp:
            new_mcp[uk] = uv
    merged["mcp_servers"] = new_mcp

    rendered_toml = _dump_toml_document(merged, managed_blocks)
    try:
        cfg_path.write_text(rendered_toml, encoding="utf-8")
    except OSError:
        pass

    # Sync skills/mod-* only inside exact chome/skills directory
    synced_skills: list[str] = []
    try:
        skills_root = chome / "skills"
        skills_root.mkdir(parents=True, exist_ok=True)
        for child in list(skills_root.iterdir()):
            if child.name.startswith("mod-"):
                try:
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
                except OSError:
                    pass
        for mod_id in mounted_mod_ids:
            mod_skills_dir = MODS_ROOT / mod_id / "skills"
            if mod_skills_dir.is_dir():
                for sdir in sorted(mod_skills_dir.iterdir()):
                    if sdir.is_dir() and (sdir / "SKILL.md").is_file():
                        dest_skill = skills_root / f"mod-{mod_id}-{sdir.name}"
                        try:
                            shutil.copytree(sdir, dest_skill, dirs_exist_ok=True)
                            synced_skills.append(sdir.name)
                        except OSError:
                            pass
    except OSError:
        pass

    try:
        agents_md_content = _build_agents_markdown(mounted_mod_ids, mounted_rule_texts, cli_label="Codex-CLI")
        (chome / "AGENTS.md").write_text(agents_md_content, encoding="utf-8")
    except OSError:
        pass

    return list(new_mcp.keys()), synced_skills


def sync_codex_runtime(mounted_mod_ids: list[str], target_home: Path | None = None) -> dict[str, Any]:
    managed_mcp_keys = _known_mod_mcp_server_keys(underscore=True)
    non_core_mcp_entries: dict[str, dict[str, Any]] = {}
    mounted_rule_texts: list[str] = []

    for mod_id in mounted_mod_ids:
        mod_dir = MODS_ROOT / mod_id
        if not (mod_dir / "mod.json").is_file():
            continue
        manifest = load_mod_manifest(mod_dir)
        # Skip @core Mods here because their MCP tools are already aggregated inside 'ap'
        if mod_id not in CORE_MOD_IDS:
            for srv_name, srv_cfg in (manifest.get("mcp_servers") or {}).items():
                if not isinstance(srv_cfg, dict) or not srv_cfg.get("command"):
                    continue
                toml_key = srv_name.replace("-", "_")
                cmd = _resolve_mod_command(str(srv_cfg["command"]), mod_dir)
                raw_args = srv_cfg.get("args", [])
                resolved_args = [str(a).replace("${MOD_DIR}", mod_dir.as_posix()) for a in raw_args]
                entry: dict[str, Any] = {
                    "command": cmd,
                    "args": resolved_args,
                }
                env_map = _resolve_mod_env_map(srv_cfg.get("env"), mod_dir)
                if env_map:
                    entry["env"] = env_map
                non_core_mcp_entries[toml_key] = entry

        mod_rules_dir = mod_dir / "rules"
        if mod_rules_dir.is_dir():
            for rfile in sorted(mod_rules_dir.glob("*.md")):
                try:
                    mounted_rule_texts.append(f"<!-- MOD:{mod_id}:{rfile.name} -->\n" + rfile.read_text(encoding="utf-8"))
                except OSError:
                    pass

    if target_home is not None:
        target_homes = [Path(target_home)]
    elif os.name == "nt":
        target_homes = [CODEX_HOME]
        if REAL_CODEX_HOME.resolve() != CODEX_HOME.resolve():
            target_homes.append(REAL_CODEX_HOME)
    else:
        target_homes = [CODEX_HOME]

    synced_mcp_servers: list[str] = []
    synced_skills: list[str] = []
    synced_homes: list[str] = []
    for chome in target_homes:
        mcps, skills = _sync_single_codex_home(
            chome,
            mounted_mod_ids,
            non_core_mcp_entries,
            mounted_rule_texts,
            managed_mcp_keys,
        )
        synced_homes.append(str(chome))
        if not synced_mcp_servers:
            synced_mcp_servers = mcps
            synced_skills = skills

    auth_sync_info = sync_codex_auth_state(target_homes[0]) if target_home is None else {"ok": False, "reason": "explicit_target_home"}

    return {
        "home": str(target_homes[0]),
        "synced_homes": synced_homes,
        "mounted_mods": mounted_mod_ids,
        "skipped_core_mcp_mods": [m for m in mounted_mod_ids if m in CORE_MOD_IDS],
        "mcp_servers": synced_mcp_servers,
        "skills_count": len(synced_skills),
        "auth_sync": auth_sync_info,
    }


def _extract_codex_auth_freshness(auth_path: Path) -> tuple[float, int, str, dict[str, Any]]:
    """Return (refresh_epoch, jwt_exp, raw_text, parsed_dict) for a Codex auth.json candidate."""
    import base64
    import datetime as _dt

    if not auth_path.is_file():
        return (0.0, 0, "", {})
    try:
        raw_text = auth_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
        if not isinstance(data, dict):
            return (0.0, 0, "", {})
        toks = data.get("tokens") or {}
        access_tok = str(toks.get("access_token") or "")
        refresh_tok = str(toks.get("refresh_token") or "")
        if not access_tok and not data.get("OPENAI_API_KEY"):
            return (0.0, 0, "", {})
        jwt_exp = 0
        if "." in access_tok:
            parts = access_tok.split(".")
            if len(parts) >= 2:
                padded = parts[1] + "=" * (-len(parts[1]) % 4)
                jwt_payload = json.loads(base64.urlsafe_b64decode(padded))
                jwt_exp = int(jwt_payload.get("exp") or 0)
        last_ref_str = str(data.get("last_refresh") or "").strip()
        ref_epoch = 0.0
        if last_ref_str:
            norm_iso = last_ref_str.replace("Z", "+00:00")
            if "." in norm_iso and "+" in norm_iso:
                main_p, tz_p = norm_iso.split("+", 1)
                sec_p, frac_p = main_p.split(".", 1)
                norm_iso = f"{sec_p}.{frac_p[:6]}+{tz_p}"
            ref_epoch = _dt.datetime.fromisoformat(norm_iso).timestamp()
        elif jwt_exp > 0:
            ref_epoch = float(jwt_exp - 864000)
        if not refresh_tok and not data.get("OPENAI_API_KEY"):
            return (0.0, 0, "", {})
        return (ref_epoch, jwt_exp, raw_text, data)
    except Exception:
        return (0.0, 0, "", {})


def sync_codex_auth_state(target_home: Path | None = None) -> dict[str, Any]:
    """Newest-Wins synchronization of Codex auth.json across local Codex homes and Google Drive sync mirrors."""
    candidates: list[Path] = []
    for p in (
        REAL_CODEX_HOME / "auth.json",
        CODEX_HOME / "auth.json",
        (target_home / "auth.json") if target_home else None,
    ):
        if p is not None and p not in candidates:
            candidates.append(p)

    best_score = (0.0, 0)
    best_text = ""
    best_src = ""
    for cpath in candidates:
        ref_ts, j_exp, raw_txt, _ = _extract_codex_auth_freshness(cpath)
        score = (round(ref_ts, 3), j_exp)
        if raw_txt and score > best_score:
            best_score = score
            best_text = raw_txt
            best_src = str(cpath)

    if not best_text:
        return {"ok": False, "reason": "no_valid_auth_found"}

    updated_paths: list[str] = []
    for dst in candidates:
        try:
            if not dst.parent.is_dir():
                dst.parent.mkdir(parents=True, exist_ok=True)
            cur_ref, cur_exp, cur_txt, _ = _extract_codex_auth_freshness(dst)
            if (round(cur_ref, 3), cur_exp) < best_score or cur_txt.strip() != best_text.strip():
                dst.write_text(best_text, encoding="utf-8")
                if os.name != "nt":
                    try:
                        os.chmod(dst, 0o600)
                    except OSError:
                        pass
                updated_paths.append(str(dst))
        except OSError:
            continue

    return {
        "ok": True,
        "winner_source": best_src,
        "jwt_exp": best_score[1],
        "updated_targets": updated_paths,
    }


def _safe_remove_dir_or_link(target: Path) -> None:
    """Remove a directory, symlink, or Windows Junction in pure Python without spawning cmd.exe."""
    if not (target.exists() or target.is_symlink()):
        return
    try:
        attrs = os.stat(target, follow_symlinks=False).st_file_attributes
        is_reparse = bool(attrs & 0x400)
    except Exception:
        is_reparse = target.is_symlink()
    if is_reparse:
        try:
            os.rmdir(str(target))
            return
        except Exception:
            try:
                target.unlink(missing_ok=True)
                return
            except Exception:
                pass
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    else:
        target.unlink(missing_ok=True)


def _copy_dir_pure_python(dst_path: Path, src_path: Path) -> None:
    """Copy small skill/rule directories in pure Python without spawning cmd.exe /c mklink."""
    if not src_path.is_dir():
        return
    try:
        if dst_path.exists() and src_path.resolve() == dst_path.resolve():
            return
    except Exception:
        pass
    shutil.copytree(src_path, dst_path, dirs_exist_ok=True)


def _sync_single_agy_home(
    home: Path,
    plugins_dir: Path,
    mcp_cache_root: Path,
    mounted_mod_ids: list[str],
    *,
    is_desktop: bool,
) -> dict[str, Any]:
    # Ensure conversations directories exist to prevent cli.log ENOENT errors
    for conv_dir in (
        home / ".gemini" / "antigravity" / "conversations",
        home / ".gemini" / "antigravity-cli" / "conversations",
    ):
        try:
            conv_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    # If plugins_dir is currently a Junction to Desktop plugins, remove the junction link in pure Python
    if not is_desktop and (plugins_dir.exists() or plugins_dir.is_symlink()):
        try:
            attrs = os.stat(plugins_dir, follow_symlinks=False).st_file_attributes
            if bool(attrs & 0x400):
                os.rmdir(str(plugins_dir))
        except Exception:
            pass

    plugins_dir.mkdir(parents=True, exist_ok=True)

    # Ensure AGY-CLI settings.json persistently uses gemini-4-carbon and includes trustedWorkspaces
    if not is_desktop:
        cli_cfg_dirs: list[Path] = [home / ".gemini" / "antigravity-cli"]
        try:
            if home.resolve() == AGY_HOME.resolve():
                cli_cfg_dirs.append(_real_user_home() / ".gemini" / "antigravity-cli")
        except Exception:
            pass
        for cli_cfg_dir in cli_cfg_dirs:
            try:
                cli_cfg_dir.mkdir(parents=True, exist_ok=True)
                cfg_file = cli_cfg_dir / "settings.json"
                cfg_data: dict[str, Any] = {}
                if cfg_file.is_file():
                    try:
                        cfg_data = json.loads(cfg_file.read_text(encoding="utf-8"))
                    except Exception:
                        cfg_data = {}
                cfg_data["model"] = "gemini-4-carbon"
                cfg_data["defaultModel"] = "gemini-4-carbon"
                tw = list(cfg_data.get("trustedWorkspaces") or [])
                req_workspaces = (str(MODS_ROOT), str(home))
                for req_ws in req_workspaces:
                    if req_ws not in tw:
                        tw.append(req_ws)
                if tw:
                    cfg_data["trustedWorkspaces"] = tw
                cfg_file.write_text(json.dumps(cfg_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except Exception:
                pass

    # Sync core base plugins into non-desktop CLI homes from canonical repo (fallback read-only from DESKTOP_PLUGINS)
    if not is_desktop:
        repo_plugins_root = SOURCE_PLUGIN_ROOT.parent
        for base_pname in ("antigravity-agent-policy", "antigravity-zh-cn"):
            src_base = repo_plugins_root / base_pname
            if not src_base.is_dir():
                src_base = DESKTOP_PLUGINS / base_pname
            dst_base = plugins_dir / base_pname
            if src_base.is_dir():
                _copy_dir_pure_python(dst_base, src_base)

    # Sync @core Mod skills ONLY into the target plugins_dir/antigravity-agent-policy/skills (never mutating host when is_desktop=False)
    target_policy_plugin = plugins_dir / "antigravity-agent-policy"
    if target_policy_plugin.is_dir():
        for core_mid in CORE_MOD_IDS:
            core_skills_src = MODS_ROOT / core_mid / "skills"
            if core_skills_src.is_dir():
                for sdir in sorted(core_skills_src.iterdir()):
                    if sdir.is_dir() and (sdir / "SKILL.md").is_file():
                        _copy_dir_pure_python(target_policy_plugin / "skills" / sdir.name, sdir)

    # Remove any non-core / old mod-* plugin folders in pure Python (zero cmd.exe spawn)
    for child in list(plugins_dir.iterdir()):
        if child.name not in {"antigravity-agent-policy", "antigravity-zh-cn"}:
            _safe_remove_dir_or_link(child)

    mcp_cache_root.mkdir(parents=True, exist_ok=True)
    core_mcp_dst = mcp_cache_root / f"antigravity-agent-policy_{MCP_SERVER_SHORT_KEY}"
    core_mcp_dst.mkdir(parents=True, exist_ok=True)
    for old_sf in list(core_mcp_dst.glob("*.json")):
        old_sf.unlink(missing_ok=True)

    try:
        from mcp_server import TOOLS as CORE_MCP_TOOLS
        for tool_def in CORE_MCP_TOOLS:
            tname = tool_def.get("name")
            if tname:
                schema_text = json.dumps(tool_def, ensure_ascii=False, indent=2) + "\n"
                (core_mcp_dst / f"{tname}.json").write_text(schema_text, encoding="utf-8")
    except Exception:
        pass

    # Write currently mounted Mods' MCP tool schemas into antigravity-agent-policy_ap
    for mod_id in mounted_mod_ids:
        mod_dir = MODS_ROOT / mod_id
        if not (mod_dir / "mod.json").is_file():
            continue
        try:
            manifest = load_mod_manifest(mod_dir)
            for tool in manifest.get("mcp_tools", []):
                if isinstance(tool, dict) and tool.get("name"):
                    tname = str(tool["name"]).strip()
                    schema = tool.get("inputSchema") or tool.get("parameters") or {"type": "object", "properties": {}}
                    tool_obj = {
                        "name": tname,
                        "description": f"[Mod:{mod_id}] " + str(tool.get("description") or tname),
                        "inputSchema": schema,
                    }
                    (core_mcp_dst / f"{tname}.json").write_text(
                        json.dumps(tool_obj, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
        except Exception:
            pass

    # Clean stale/duplicate mcp schema folders (including legacy antigravity-agent-policy_agent-policy & mod-*_*)
    for child in list(mcp_cache_root.iterdir()):
        if child.name != core_mcp_dst.name and child.is_dir():
            shutil.rmtree(child, ignore_errors=True)

    synced_plugins: list[str] = []
    for mod_id in mounted_mod_ids:
        # Skip @core Mods here because their skills & MCP tools are already aggregated inside antigravity-agent-policy
        if mod_id in CORE_MOD_IDS:
            continue
        mod_dir = MODS_ROOT / mod_id
        if not (mod_dir / "mod.json").is_file():
            continue
        manifest = load_mod_manifest(mod_dir)
        plugin_name = f"mod-{mod_id}"
        target_pdir = plugins_dir / plugin_name
        target_pdir.mkdir(parents=True, exist_ok=True)

        # Write plugin.json ONLY with skills / rules / assets / extensions (NO duplicate mcp_config.json / .mcp.json!)
        plugin_json: dict[str, Any] = {
            "name": plugin_name,
            "version": manifest.get("version", "1.0.0"),
            "description": manifest.get("description", ""),
        }
        if (mod_dir / "skills").is_dir():
            plugin_json["skills"] = "./skills"
            _copy_dir_pure_python(target_pdir / "skills", mod_dir / "skills")
        if (mod_dir / "rules").is_dir():
            plugin_json["rules"] = "./rules"
            _copy_dir_pure_python(target_pdir / "rules", mod_dir / "rules")
        if (mod_dir / "assets").is_dir():
            _copy_dir_pure_python(target_pdir / "assets", mod_dir / "assets")
        if manifest.get("plugin_extensions"):
            plugin_json["extensions"] = manifest["plugin_extensions"]

        for stale_mcp_cfg in ("mcp_config.json", ".mcp.json"):
            (target_pdir / stale_mcp_cfg).unlink(missing_ok=True)

        (target_pdir / "plugin.json").write_text(
            json.dumps(plugin_json, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        synced_plugins.append(plugin_name)

    return {
        "home": str(home),
        "plugins_dir": str(plugins_dir),
        "mcp_schema_dir": str(core_mcp_dst),
        "mounted_mods": mounted_mod_ids,
        "core_aggregated": [m for m in mounted_mod_ids if m in CORE_MOD_IDS],
        "extension_plugins": synced_plugins,
    }


def sync_agy_runtime(
    mounted_mod_ids: list[str],
    target_home: Path | None = None,
    *,
    is_desktop: bool = False,
    skip_host_plugins: bool = False,
) -> dict[str, Any]:
    effective_skip_host = skip_host_plugins or os.environ.get("AGY_SKIP_HOST_PLUGINS") == "1"
    if is_desktop:
        home = _real_user_home()
        if effective_skip_host:
            for conv_dir in (
                home / ".gemini" / "antigravity" / "conversations",
                home / ".gemini" / "antigravity-cli" / "conversations",
            ):
                try:
                    conv_dir.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
            return {
                "home": str(home),
                "plugins_dir": str(DESKTOP_PLUGINS),
                "skipped_host_plugins": True,
                "mounted_mods": mounted_mod_ids,
                "core_aggregated": [m for m in mounted_mod_ids if m in CORE_MOD_IDS],
                "extension_plugins": [],
            }
        return _sync_single_agy_home(
            home,
            DESKTOP_PLUGINS,
            home / ".gemini" / "antigravity" / "mcp",
            mounted_mod_ids,
            is_desktop=True,
        )

    if target_home is not None:
        h = _real_user_home(target_home)
        return _sync_single_agy_home(
            h,
            h / ".gemini" / "config" / "plugins",
            h / ".gemini" / "antigravity-cli" / "mcp",
            mounted_mod_ids,
            is_desktop=False,
        )

    primary_home = AGY_HOME
    res = _sync_single_agy_home(
        primary_home,
        primary_home / ".gemini" / "config" / "plugins",
        primary_home / ".gemini" / "antigravity-cli" / "mcp",
        mounted_mod_ids,
        is_desktop=False,
    )


    return res


def sync_opus_runtime(mounted_mod_ids: list[str], target_home: Path | None = None) -> dict[str, Any]:
    """Synchronize Claude Code CLI 2.x (.claude.json mcpServers, .claude/CLAUDE.md, .claude/skills/)."""
    managed_mcp_keys = _known_mod_mcp_server_keys(underscore=False) | _known_mod_mcp_server_keys(underscore=True)
    mcp_script = _resolve_mcp_server_script().as_posix()

    non_core_mcp_servers: dict[str, dict[str, Any]] = {}
    mounted_rule_texts: list[str] = []

    for mod_id in mounted_mod_ids:
        mod_dir = MODS_ROOT / mod_id
        if not (mod_dir / "mod.json").is_file():
            continue
        manifest = load_mod_manifest(mod_dir)
        if mod_id not in CORE_MOD_IDS:
            for srv_name, srv_cfg in (manifest.get("mcp_servers") or {}).items():
                if not isinstance(srv_cfg, dict) or not srv_cfg.get("command"):
                    continue
                resolved_srv: dict[str, Any] = {
                    "command": _resolve_mod_command(str(srv_cfg["command"]), mod_dir),
                    "args": [str(a).replace("${MOD_DIR}", mod_dir.as_posix()) for a in srv_cfg.get("args", [])],
                }
                env_map = _resolve_mod_env_map(srv_cfg.get("env"), mod_dir)
                if env_map:
                    resolved_srv["env"] = env_map
                non_core_mcp_servers[str(srv_name)] = resolved_srv

        mod_rules_dir = mod_dir / "rules"
        if mod_rules_dir.is_dir():
            for rfile in sorted(mod_rules_dir.glob("*.md")):
                try:
                    mounted_rule_texts.append(f"<!-- MOD:{mod_id}:{rfile.name} -->\n" + rfile.read_text(encoding="utf-8"))
                except OSError:
                    pass

    if target_home is not None:
        target_homes = [_real_user_home(target_home)]
    elif os.name == "nt":
        real_home = _real_user_home()
        target_homes = [real_home]
        if OPUS_HOME.resolve() != real_home.resolve():
            target_homes.append(OPUS_HOME)
    else:
        target_homes = [_real_user_home()]

    claude_md_content = _build_agents_markdown(mounted_mod_ids, mounted_rule_texts, cli_label="Claude-Code-CLI (Opus-CLI)")
    synced_homes: list[str] = []
    synced_skills_count = 0
    active_mcp_names: list[str] = []

    for ohome in target_homes:
        try:
            ohome.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue

        claude_json_path = ohome / ".claude.json"
        claude_data: dict[str, Any] = {}
        if claude_json_path.is_file():
            try:
                raw_json = claude_json_path.read_text(encoding="utf-8-sig")
                try:
                    (ohome / ".claude.json.bak").write_text(raw_json, encoding="utf-8")
                except OSError:
                    pass
                parsed = json.loads(raw_json)
                if isinstance(parsed, dict):
                    claude_data = parsed
            except Exception:
                claude_data = {}

        existing_mcp = dict(claude_data.get("mcpServers") or {}) if isinstance(claude_data.get("mcpServers"), dict) else {}
        preserved_user_mcp = {
            k: v for k, v in existing_mcp.items() if k not in managed_mcp_keys and isinstance(v, dict)
        }

        updated_mcp: dict[str, Any] = {
            MCP_SERVER_SHORT_KEY: {
                "command": PYTHON_EXE,
                "args": ["-X", "utf8", mcp_script],
                "env": {
                    "PYTHONUTF8": "1",
                    "PYTHONIOENCODING": "utf-8",
                    "CODEX_AGENT_POLICY_DATA": SHARED_POLICY_DATA.as_posix(),
                    "AGY_MODS_DIR": MODS_ROOT.as_posix(),
                },
            }
        }
        for sk, sv in non_core_mcp_servers.items():
            updated_mcp[sk] = sv
        for uk, uv in preserved_user_mcp.items():
            if uk not in updated_mcp:
                updated_mcp[uk] = uv

        claude_data["mcpServers"] = updated_mcp
        try:
            claude_json_path.write_text(json.dumps(claude_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass

        claude_dir = ohome / ".claude"
        try:
            claude_dir.mkdir(parents=True, exist_ok=True)
            (claude_dir / "CLAUDE.md").write_text(claude_md_content, encoding="utf-8")

            skills_root = claude_dir / "skills"
            skills_root.mkdir(parents=True, exist_ok=True)
            for child in list(skills_root.iterdir()):
                if child.name.startswith("mod-"):
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)

            skill_ids_to_sync = list(dict.fromkeys([*CORE_MOD_IDS, *mounted_mod_ids]))
            local_skill_cnt = 0
            for mod_id in skill_ids_to_sync:
                mod_skills_dir = MODS_ROOT / mod_id / "skills"
                if mod_skills_dir.is_dir():
                    for sdir in sorted(mod_skills_dir.iterdir()):
                        if sdir.is_dir() and (sdir / "SKILL.md").is_file():
                            shutil.copytree(sdir, skills_root / f"mod-{mod_id}-{sdir.name}", dirs_exist_ok=True)
                            local_skill_cnt += 1
            if not synced_skills_count:
                synced_skills_count = local_skill_cnt
        except OSError:
            pass

        synced_homes.append(str(ohome))
        if not active_mcp_names:
            active_mcp_names = list(updated_mcp.keys())

    return {
        "home": str(target_homes[0]),
        "synced_homes": synced_homes,
        "mounted_mods": mounted_mod_ids,
        "mcp_servers": active_mcp_names,
        "skills_count": synced_skills_count,
    }


def sync_all_runtimes(
    mounts: dict[str, list[str]] | None = None,
    *,
    skip_host_plugins: bool = False,
) -> dict[str, Any]:
    active = mounts if mounts is not None else load_mounts()
    codex_mods = active.get("codex-cli", list(CORE_MOD_IDS))
    agy_mods = active.get("agy-cli", list(CORE_MOD_IDS))
    opus_mods = active.get("opus-cli", list(CORE_MOD_IDS))
    codex_res = sync_codex_runtime(codex_mods)
    desktop_res = sync_agy_runtime(agy_mods, is_desktop=True, skip_host_plugins=skip_host_plugins)
    agy_res = sync_agy_runtime(agy_mods, target_home=AGY_HOME, skip_host_plugins=skip_host_plugins)
    opus_res = sync_opus_runtime(opus_mods)
    pool_sync_res = None
    default_mods_root = SOURCE_PLUGIN_ROOT / "mods"
    if os.environ.get("AGY_MEMORY_SANDBOX") != "1" and MODS_ROOT.resolve() == default_mods_root.resolve():
        try:
            from mod_pool_sync_engine import sync_mod_pool_everywhere
            pool_sync_res = sync_mod_pool_everywhere(source_mods_dir=MODS_ROOT)
        except Exception as exc:
            pool_sync_res = {"status": "skipped", "reason": str(exc)[:100]}
    else:
        pool_sync_res = {"status": "skipped", "reason": "non_default_or_sandbox_mods_root"}
    return {
        "desktop": desktop_res,
        "codex": codex_res,
        "antigravity": agy_res,
        "opus": opus_res,
        "mod_pool_sync": pool_sync_res,
    }


def get_mounted_mcp_tools(agent_id: str | None = None) -> list[dict[str, Any]]:
    """Return MCP tool definitions from all currently mounted Mods for dynamic hot-injection."""
    mounts = load_mounts()
    if agent_id and normalize_agent_key(agent_id) != "all":
        ak = normalize_agent_key(agent_id)
        mounted_ids = list(mounts.get(ak, []))
    else:
        env_agent = os.environ.get("AGY_AGENT_KEY") or os.environ.get("AGY_AGENT_ID") or os.environ.get("CODEX_HOME") or os.environ.get("USERPROFILE")
        if env_agent and "codex" in str(env_agent).lower():
            mounted_ids = list(mounts.get("codex-cli", []))
        elif env_agent and ("opus" in str(env_agent).lower() or "claude" in str(env_agent).lower()):
            mounted_ids = list(mounts.get("opus-cli", []))
        elif env_agent and "agy" in str(env_agent).lower():
            mounted_ids = list(mounts.get("agy-cli", []))
        else:
            mounted_ids = []
            for mlist in mounts.values():
                for mid in mlist:
                    if mid not in mounted_ids:
                        mounted_ids.append(mid)

    dynamic_tools: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for mid in mounted_ids:
        mod_dir = MODS_ROOT / mid
        if not (mod_dir / "mod.json").is_file():
            continue
        try:
            manifest = load_mod_manifest(mod_dir)
            for t in manifest.get("mcp_tools", []):
                if not isinstance(t, dict) or not t.get("name"):
                    continue
                tname = str(t["name"]).strip()
                if tname in seen_names:
                    continue
                seen_names.add(tname)
                schema = t.get("inputSchema") or t.get("parameters") or {"type": "object", "properties": {}}
                dynamic_tools.append({
                    "name": tname,
                    "description": f"[Mod:{mid}] " + str(t.get("description") or tname),
                    "inputSchema": schema,
                    "_mod_id": mid,
                })
        except Exception:
            continue
    return dynamic_tools


def call_mod_tool(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    mod_id: str | None = None,
    auto_mount: bool = True,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Hot-call an MCP tool inside a Mod package over stdio JSON-RPC without restarting the host CLI."""
    tname = str(tool_name).strip()
    args = dict(arguments or {})
    target_mod_id = str(mod_id).strip() if mod_id else None

    if not target_mod_id:
        for item in list_all_mods():
            for t in item.get("mcp_tools", []):
                if isinstance(t, dict) and t.get("name") == tname:
                    target_mod_id = item["id"]
                    break
            if target_mod_id:
                break
    if not target_mod_id:
        raise ModError(f"Tool '{tname}' was not found in any encapsulated Mod.")

    mod_dir = MODS_ROOT / target_mod_id
    if not (mod_dir / "mod.json").is_file():
        raise ModError(f"Mod '{target_mod_id}' is not installed in {MODS_ROOT}")

    mounts = load_mounts()
    ak = normalize_agent_key(agent_id) if agent_id else "all"
    check_keys = list(KNOWN_AGENTS.keys()) if ak == "all" else [ak]
    is_mounted = any(target_mod_id in mounts.get(k, []) for k in check_keys)
    auto_mounted_now = False
    if not is_mounted:
        if auto_mount:
            mount_mod(target_mod_id, agent_id=ak)
            auto_mounted_now = True
        else:
            raise ModError(f"Mod '{target_mod_id}' is not mounted on '{ak}'. Call mod_mount first or set auto_mount=True.")

    manifest = load_mod_manifest(mod_dir)
    mcp_servers = manifest.get("mcp_servers") or {}
    if not mcp_servers:
        raise ModError(f"Mod '{target_mod_id}' does not define any executable mcp_servers.")

    srv_name, srv_cfg = next(iter(mcp_servers.items()))
    cmd = _resolve_mod_command(str(srv_cfg["command"]), mod_dir)
    cmd_args = [str(a).replace("${MOD_DIR}", mod_dir.as_posix()) for a in srv_cfg.get("args", [])]
    env = dict(os.environ)
    for ek, ev in _resolve_mod_env_map(srv_cfg.get("env"), mod_dir).items():
        env[ek] = ev

    rpc_input = (
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "mod-hot-router", "version": "1.0.0"}}}, ensure_ascii=False)
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tname, "arguments": args}}, ensure_ascii=False)
        + "\n"
    )
    proc = subprocess.run(
        [cmd, *cmd_args],
        input=rpc_input,
        cwd=str(mod_dir),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0,
    )
    parsed_result: Any = None
    rpc_error: Any = None
    tool_is_error = False
    matched_response = False

    for line in proc.stdout.splitlines():
        s = line.strip()
        if not s.startswith("{"):
            continue
        try:
            obj = json.loads(s)
            if obj.get("id") == 2:
                matched_response = True
                if "error" in obj and obj["error"] is not None:
                    rpc_error = obj["error"]
                    break
                res_body = obj.get("result")
                if isinstance(res_body, dict) and res_body.get("isError") is True:
                    tool_is_error = True
                if isinstance(res_body, dict):
                    content_list = res_body.get("content") or []
                    if content_list and isinstance(content_list[0], dict) and "text" in content_list[0]:
                        raw_txt = content_list[0]["text"]
                        try:
                            parsed_result = json.loads(raw_txt)
                        except Exception:
                            parsed_result = raw_txt
                    else:
                        parsed_result = res_body
                else:
                    parsed_result = res_body
                break
        except Exception:
            continue

    if rpc_error is not None:
        return {
            "status": "error",
            "isError": True,
            "mod_id": target_mod_id,
            "server": srv_name,
            "tool_name": tname,
            "auto_mounted": auto_mounted_now,
            "error": rpc_error,
        }

    if tool_is_error:
        return {
            "status": "error",
            "isError": True,
            "mod_id": target_mod_id,
            "server": srv_name,
            "tool_name": tname,
            "auto_mounted": auto_mounted_now,
            "error": parsed_result,
        }

    if not matched_response:
        return {
            "status": "error",
            "isError": True,
            "mod_id": target_mod_id,
            "server": srv_name,
            "tool_name": tname,
            "auto_mounted": auto_mounted_now,
            "exit_code": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
            "error": proc.stderr.strip() or f"Subprocess exited with code {proc.returncode} without JSON-RPC response",
        }

    return {
        "status": "ok",
        "mod_id": target_mod_id,
        "server": srv_name,
        "tool_name": tname,
        "auto_mounted": auto_mounted_now,
        "result": parsed_result,
    }


MOD_INTENT_RULES: list[tuple[str, list[str]]] = [
    ("quota-monitor", ["额度", "剩余额度", "配额", "限流", "重置时间", "quota", "rate limit", "usage", "调度", "分派", "派发", "负载", "quota_status", "quota_recommend_agent"]),
    ("context7-sdk-verifier", ["sdk", "签名", "对账", "伴飞", "交叉记忆", "modality", "工艺库", "sdk_fact_lookup", "sdk_fact_reconcile_report", "sdk_memory_review_commit", "context7"]),
    ("ui-aesthetic-studio", ["ui", "审美", "美学", "oklch", "色阶", "配色", "对比度", "wcag", "圆角", "流派", "ai廉价感", "anti-ai slop", "ui_audit_anti_slop", "ui_generate_oklch_tokens", "ui_recommend_domain_preset"]),
    ("anatomy-motion-studio", ["解剖图", "爆炸图", "弹簧", "动效", "缓动", "状态机", "spring", "easing", "anatomy_generate_svg_spec", "motion_spring_physics_tokens", "motion_state_transition_audit"]),
    ("windows-uia-automation", ["uia", "无障碍树", "窗口自动化", "点击控件", "桌面自动化", "uia_inspect_window_tree", "uia_invoke_element_safe", "uia_wait_condition_gate"]),
    ("office-report-studio", ["报告", "word", "excel", "pdf", "三线表", "台账", "docx", "xlsx", "office_word_three_line_table", "office_excel_formula_sheet", "office_pdf_page_inspect"]),
    ("ppt-master", ["ppt", "pptx", "幻灯片", "课件", "演示文稿", "汇报材料", "路演", "产品介绍", "pitch", "slide", "deck", "presentation", "ppt-master", "svg_to_pptx"]),
    ("win-system-guardian", ["c盘", "磁盘清理", "缓存清理", "端口占用", "进程诊断", "健康巡检", "wsl", "vhdx", "sys_disk_cache_audit", "sys_port_process_diagnose", "sys_runtime_health_check"]),
    ("chrome-devtools", ["浏览器", "网页", "devtools", "chrome", "lcp", "a11y", "无障碍", "内存泄漏", "cookie", "抓包", "前端调试", "screenshot"]),
    ("github-suite", ["github", "pull request", "issue", "仓库", "pr", "分支", "commit", "merge"]),
    ("google-cloud-suite", ["gcp", "google cloud", "gcloud", "gke", "cloud run", "bigquery", "vertex"]),
    ("android-suite", ["android", "adb", "avd", "emulator", "模拟器", "安卓", "compose-preview", "device-hub", "jetpack compose", "apk"]),
    ("gdrive-suite", ["gdrive", "google drive", "云端硬盘", "网盘", "drive.google.com", "docs.google.com", "sheets"]),
    ("code-intelligence", ["ast", "调用链", "symbol_trace", "impact_analysis", "影响面", "架构提取", "代码搜索", "code_search"]),
    ("engineering-workflows", ["重构", "测试", "review", "debug", "架构", "数据库", "api", "安全", "bug", "deploy"]),
    ("dev-setup-workflows", ["脚手架", "环境搭建", "mcp构建", "规则初始化", "scaffold", "setup"]),
    ("productivity-workflows", ["交接", "handoff", "汇报", "周报", "changelog", "总结", "计划"]),
    ("experimental-workflows", ["实验工作流", "tdd", "red-green", "红绿重构", "领域建模"]),
    ("persona-studio", ["人设", "persona", "角色画像", "专家组", "圆桌"]),
    ("source-connectors", ["外部仓库", "知识源", "weknora", "obsidian", "文档抓取", "connector", "source_connect"]),
    ("live-panel", ["可视化面板", "live panel", "监控面板"]),
    ("frontend-design", ["前端页面", "landing page", "react", "tailwind", "响应式"]),
    ("stitch-ui-design", ["stitch", "设计系统", "design md", "原型图"]),
    ("interactive-viz", ["可视化图表", "数据大屏", "chart", "svg图表", "canvas图表"]),
    ("canvas-design", ["海报", "视觉平面", "排版构图", "几何海报"]),
    ("remotion-video", ["remotion", "视频生成", "代码视频", "动画短片"]),
    ("engineering-bid-auditor", ["标书", "招标", "投标", "废标", "红线矩阵", "偏离表", "工程量清单", "boq", "bid_redline_matrix", "bid_boq_audit"]),
    ("vision-blueprint-inspector", ["蓝图", "施工图识别", "图纸ocr", "霍夫纠偏", "构件尺寸核对", "blueprint_prepare", "blueprint_cross_check"]),
    ("auto-poc-foundry", ["poc", "微实验", "沙盒验证", "预制补丁", "poc_run_isolated_experiment", "auto-poc-foundry"]),
    ]


def migrate_and_sync_local_plugins() -> dict[str, Any]:
    """Encapsulate local static plugins into MODS_ROOT, archive static copies, and sync runtimes to @core."""
    MODS_ROOT.mkdir(parents=True, exist_ok=True)
    PACKAGES_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVED_PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    extra_skills_archive = ARCHIVED_PLUGINS_DIR / "_policy_extra_skills"
    extra_skills_archive.mkdir(parents=True, exist_ok=True)

    wrapped_mods: list[str] = []
    archived_plugins: list[str] = []
    archived_extra_skills: list[str] = []

    # 1. Wrap android-cli-plugin -> android-suite
    android_src = DESKTOP_PLUGINS / "android-cli-plugin"
    if not android_src.is_dir():
        android_src = ARCHIVED_PLUGINS_DIR / "android-cli-plugin"
    android_mod_dir = MODS_ROOT / "android-suite"
    if android_src.is_dir():
        if android_mod_dir.exists():
            shutil.rmtree(android_mod_dir, ignore_errors=True)
        (android_mod_dir / "skills" / "android-cli").mkdir(parents=True, exist_ok=True)
        (android_mod_dir / "skills" / "android-device-hub").mkdir(parents=True, exist_ok=True)
        (android_mod_dir / "skills" / "compose-preview").mkdir(parents=True, exist_ok=True)

        if (android_src / "skills" / "SKILL.md").is_file():
            shutil.copy2(android_src / "skills" / "SKILL.md", android_mod_dir / "skills" / "android-cli" / "SKILL.md")
            shutil.copy2(android_src / "skills" / "SKILL.md", android_mod_dir / "skills" / "SKILL.md")
        if (android_src / "skills" / "references").is_dir():
            _copy_dir_pure_python(android_mod_dir / "skills" / "android-cli" / "references", android_src / "skills" / "references")
            _copy_dir_pure_python(android_mod_dir / "skills" / "references", android_src / "skills" / "references")
        if (android_src / "skills" / "device-hub").is_dir():
            _copy_dir_pure_python(android_mod_dir / "skills" / "android-device-hub", android_src / "skills" / "device-hub")
            _copy_dir_pure_python(android_mod_dir / "skills" / "device-hub", android_src / "skills" / "device-hub")
        if (android_src / "skills" / "compose-preview").is_dir():
            _copy_dir_pure_python(android_mod_dir / "skills" / "compose-preview", android_src / "skills" / "compose-preview")
        if (android_src / "assets").is_dir():
            _copy_dir_pure_python(android_mod_dir / "assets", android_src / "assets")

        android_manifest = {
            "schema_version": 1,
            "id": "android-suite",
            "name": "Android 移动开发与设备镜像 Mod (3 Skills: CLI + Device Hub + Compose Preview)",
            "version": "1.0.16625370",
            "category": "mobile",
            "description": "封装自官方 android-cli-plugin：提供 Android CLI 工程创建与 SDK 管理、Android Device Hub 模拟器/真机屏幕镜像与 UI 树检查（android layout/screen/adb）、以及 Jetpack Compose @Preview 实时渲染预览。",
            "tags": ["android", "mobile", "adb", "emulator", "avd", "device-hub", "compose-preview", "jetpack-compose"],
            "source": "wrapped_plugin:android-cli-plugin",
            "skills": [
                {
                    "name": "android-cli",
                    "description": "Provides instructions for installing and using the android CLI to create projects, manage AVDs, inspect UI, and manage Android SDK components.",
                    "path": "skills/android-cli/SKILL.md",
                },
                {
                    "name": "android-device-hub",
                    "description": "Instructions for working with the Android Device Hub sidecar (AVDs, physical adb devices, live screen mirroring, android layout and screen capture).",
                    "path": "skills/android-device-hub/SKILL.md",
                },
                {
                    "name": "compose-preview",
                    "description": "Instructions for the Android Compose Preview sidecar, rendering Jetpack Compose @Preview functions with android preview render.",
                    "path": "skills/compose-preview/SKILL.md",
                },
            ],
            "mcp_servers": {},
            "mcp_tools": [],
            "rules": [],
        }
        (android_mod_dir / "mod.json").write_text(json.dumps(android_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pack_mod("android-suite")
        wrapped_mods.append("android-suite")

    # 2. Wrap gdrive -> gdrive-suite
    gdrive_src = DESKTOP_PLUGINS / "gdrive"
    if not gdrive_src.is_dir():
        gdrive_src = ARCHIVED_PLUGINS_DIR / "gdrive"
    gdrive_mod_dir = MODS_ROOT / "gdrive-suite"
    if gdrive_src.is_dir():
        if gdrive_mod_dir.exists():
            shutil.rmtree(gdrive_mod_dir, ignore_errors=True)
        if (gdrive_src / "skills").is_dir():
            _copy_dir_pure_python(gdrive_mod_dir / "skills", gdrive_src / "skills")
        gdrive_ext = None
        if (gdrive_src / "plugin.json").is_file():
            try:
                pjson = json.loads((gdrive_src / "plugin.json").read_text(encoding="utf-8"))
                gdrive_ext = pjson.get("extensions")
            except Exception:
                gdrive_ext = None
        gdrive_manifest = {
            "schema_version": 1,
            "id": "gdrive-suite",
            "name": "Google Drive 云端硬盘管理 Mod (Skill + Native CLI Scripts + Context Provider)",
            "version": "0.0.8",
            "category": "cloud-storage",
            "description": "封装自官方 gdrive 插件：支持 Google Drive 文件与文件夹搜索、上传、下载、原地更新、导出 PDF/Markdown/CSV、评论管理与权限共享检查。",
            "tags": ["gdrive", "google-drive", "cloud-storage", "docs", "sheets", "slides"],
            "source": "wrapped_plugin:gdrive",
            "skills": [
                {
                    "name": "gdrive",
                    "description": "Browse, search, and manage Google Drive files and folders (download, upload, update, export PDF/MD/CSV, comments, permissions).",
                    "path": "skills/gdrive/SKILL.md",
                }
            ],
            "mcp_servers": {},
            "mcp_tools": [],
            "rules": [],
            "plugin_extensions": gdrive_ext,
        }
        (gdrive_mod_dir / "mod.json").write_text(json.dumps(gdrive_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pack_mod("gdrive-suite")
        wrapped_mods.append("gdrive-suite")

    # 3. Enrich google-cloud-suite with developer-knowledge MCP server & 3 tools
    gcloud_mod_dir = MODS_ROOT / "google-cloud-suite"
    if (gcloud_mod_dir / "mod.json").is_file():
        try:
            gc_manifest = json.loads((gcloud_mod_dir / "mod.json").read_text(encoding="utf-8"))
            gc_manifest["name"] = "Google Cloud 云原生架构与开发者知识库 Mod (3 MCP Tools + 5 Skills + Rules)"
            gc_manifest["mcp_servers"] = {
                "developer-knowledge": {
                    "serverUrl": "https://developerknowledge.googleapis.com/mcp",
                    "authProviderType": "google_credentials",
                }
            }
            gc_manifest["mcp_tools"] = [
                {
                    "name": "search_documents",
                    "description": "Search official Google developer documentation across Google Cloud, Android, Chrome, Firebase, Flutter, Go, Gemini, and TensorFlow.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Required. The raw query string to search Google developer documentation."}
                        },
                        "required": ["query"],
                    },
                },
                {
                    "name": "answer_query",
                    "description": "Get a grounded, synthesized conceptual answer to a question about Google developer products.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Required. The query to answer."}
                        },
                        "required": ["query"],
                    },
                },
                {
                    "name": "get_documents",
                    "description": "Retrieve the full content of 1 to 20 official Google developer documents by resource name (documents/...).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "names": {"type": "array", "items": {"type": "string"}, "description": "Document resource names (documents/...)."}
                        },
                        "required": ["names"],
                    },
                },
            ]
            (gcloud_mod_dir / "mod.json").write_text(json.dumps(gc_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            pack_mod("google-cloud-suite")
            wrapped_mods.append("google-cloud-suite")
        except Exception:
            pass

    # 4. Merge c-drive-cleaner into win-system-guardian Mod if present
    c_cleaner_src = POLICY_PLUGIN_ROOT / "skills" / "c-drive-cleaner"
    wsg_mod_dir = MODS_ROOT / "win-system-guardian"
    if c_cleaner_src.is_dir() and (wsg_mod_dir / "mod.json").is_file():
        _copy_dir_pure_python(wsg_mod_dir / "skills" / "c-drive-cleaner", c_cleaner_src)
        try:
            wsg_manifest = json.loads((wsg_mod_dir / "mod.json").read_text(encoding="utf-8"))
            existing_snames = {s.get("name") for s in wsg_manifest.get("skills", []) if isinstance(s, dict)}
            if "c-drive-cleaner" not in existing_snames:
                wsg_manifest.setdefault("skills", []).append({
                    "name": "c-drive-cleaner",
                    "description": "Windows C盘空间深度诊断、开发者与AI缓存清理、大目录软链接无损迁移及虚拟磁盘(WSL2/Docker)压缩专家技能。",
                    "path": "skills/c-drive-cleaner/SKILL.md",
                })
            (wsg_mod_dir / "mod.json").write_text(json.dumps(wsg_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            pack_mod("win-system-guardian")
            wrapped_mods.append("win-system-guardian")
        except Exception:
            pass

    # 5. Ensure quota-monitor and sdk-memory-sidecar skills are present in POLICY_PLUGIN_ROOT / "skills" (@core base)
    qm_skill_src = MODS_ROOT / "quota-monitor" / "skills" / "quota-monitor"
    if qm_skill_src.is_dir():
        _copy_dir_pure_python(POLICY_PLUGIN_ROOT / "skills" / "quota-monitor", qm_skill_src)
    sdk_skill_src = MODS_ROOT / "context7-sdk-verifier" / "skills" / "sdk-memory-sidecar"
    if sdk_skill_src.is_dir():
        _copy_dir_pure_python(POLICY_PLUGIN_ROOT / "skills" / "sdk-memory-sidecar", sdk_skill_src)

    # 6. Archive non-@core skills inside antigravity-agent-policy/skills to _policy_extra_skills
    core_policy_skills = {
        "global-runtime",
        "code-intelligence",
        "source-connectors",
        "ambitious-deep-thinking",
        "quota-monitor",
        "sdk-memory-sidecar",
    }
    policy_skills_dir = POLICY_PLUGIN_ROOT / "skills"
    if policy_skills_dir.is_dir():
        for sdir in list(policy_skills_dir.iterdir()):
            if sdir.is_dir() and sdir.name not in core_policy_skills:
                dst_arch = extra_skills_archive / sdir.name
                _copy_dir_pure_python(dst_arch, sdir)
                _safe_remove_dir_or_link(sdir)
                archived_extra_skills.append(sdir.name)

    # 7. Archive non-core static plugins from DESKTOP_PLUGINS to ARCHIVED_PLUGINS_DIR
    if DESKTOP_PLUGINS.is_dir():
        for pdir in list(DESKTOP_PLUGINS.iterdir()):
            if pdir.name in {"antigravity-agent-policy", "antigravity-zh-cn"} or pdir.name.startswith("mod-"):
                continue
            if pdir.is_dir():
                dst_arch = ARCHIVED_PLUGINS_DIR / pdir.name
                _copy_dir_pure_python(dst_arch, pdir)
                _safe_remove_dir_or_link(pdir)
                archived_plugins.append(pdir.name)

    # 8. Sync all runtimes (Desktop + Codex + AGY-CLI + Opus-CLI)
    sync_res = sync_all_runtimes()
    return {
        "status": "ok",
        "wrapped_or_updated_mods": wrapped_mods,
        "archived_desktop_plugins": archived_plugins,
        "archived_non_core_policy_skills": archived_extra_skills,
        "archive_dir": str(ARCHIVED_PLUGINS_DIR),
        "runtime_sync": sync_res,
    }


def resolve_mods_for_task(
    task_text: str,
    *,
    auto_mount: bool = True,
    replace_existing: bool = False,
    agent_id: str | None = "all",
) -> dict[str, Any]:
    """Dynamically resolve and optionally hot-mount Mods matching a user task description."""
    text_lower = (task_text or "").strip().casefold()
    matched_mods: list[dict[str, Any]] = []
    matched_ids: list[str] = []

    all_mods = {m["id"]: m for m in list_all_mods() if m.get("status") != "invalid"}
    for mod_id, keywords in MOD_INTENT_RULES:
        if mod_id not in all_mods:
            continue
        hits = [kw for kw in keywords if kw.casefold() in text_lower]
        if hits:
            matched_ids.append(mod_id)
            matched_mods.append({
                "mod_id": mod_id,
                "name": all_mods[mod_id]["name"],
                "matched_keywords": hits,
                "mcp_tool_count": all_mods[mod_id]["mcp_tool_count"],
                "skill_count": all_mods[mod_id]["skill_count"],
            })

    # Also match by mod id, skill name, or mcp tool name directly
    for mod_id, m in all_mods.items():
        if mod_id in matched_ids:
            continue
        direct_hits = []
        if mod_id.casefold() in text_lower:
            direct_hits.append(mod_id)
        for s in m.get("skills", []):
            sname = s.get("name") if isinstance(s, dict) else str(s)
            if len(sname) >= 4 and sname.casefold() in text_lower:
                direct_hits.append(sname)
        for t in m.get("mcp_tools", []):
            tname = t.get("name") if isinstance(t, dict) else str(t)
            if len(tname) >= 4 and tname.casefold() in text_lower:
                direct_hits.append(tname)
        if direct_hits:
            matched_ids.append(mod_id)
            matched_mods.append({
                "mod_id": mod_id,
                "name": m["name"],
                "matched_keywords": direct_hits,
                "mcp_tool_count": m["mcp_tool_count"],
                "skill_count": m["skill_count"],
            })

    mount_result = None
    if auto_mount and matched_ids:
        mount_result = mount_mod(matched_ids, agent_id=agent_id, replace=replace_existing)

    return {
        "status": "ok",
        "task_text": task_text,
        "recommended_mod_ids": matched_ids,
        "recommended_mods": matched_mods,
        "auto_mounted": bool(auto_mount and matched_ids),
        "mount_result": mount_result,
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print(
            "Usage:\n"
            "  mod_manager.py list [--query Q] [--agent AGENT] [--mounted]\n"
            "  mod_manager.py info <mod-id> [--skill SKILL_NAME]\n"
            "  mod_manager.py mount <mod-id...> [--agent AGENT] [--replace]\n"
            "  mod_manager.py unmount <mod-id...> [--agent AGENT]\n"
            "  mod_manager.py mounted [--agent AGENT]\n"
            "  mod_manager.py pack <mod-id> [source-dir]\n"
            "  mod_manager.py install <zip-or-dir-path>\n"
            "  mod_manager.py uninstall <mod-id> [--delete-package]\n"
            "  mod_manager.py sync"
        )
        return 0

    cmd = args[0].lower()
    rest = args[1:]
    try:
        if cmd in {"list", "catalog"}:
            query = ""
            agent = None
            mounted = False
            i = 0
            while i < len(rest):
                if rest[i] in {"--query", "-q"} and i + 1 < len(rest):
                    query = rest[i + 1]
                    i += 2
                elif rest[i] in {"--agent", "-a"} and i + 1 < len(rest):
                    agent = rest[i + 1]
                    i += 2
                elif rest[i] == "--mounted":
                    mounted = True
                    i += 1
                elif rest[i] in {"--output", "-o"} and i + 1 < len(rest):
                    i += 2
                else:
                    if not query and not rest[i].startswith("-"):
                        query = rest[i]
                    i += 1
            res = catalog_mods(query, agent_id=agent, mounted_only=mounted)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0

        if cmd in {"info", "get"}:
            if not rest:
                raise ModError("mod_id is required")
            mod_id = rest[0]
            skill_name = None
            i = 1
            while i < len(rest):
                if rest[i] in {"--skill", "-s"} and i + 1 < len(rest):
                    skill_name = rest[i + 1]
                    i += 2
                else:
                    i += 1
            res = get_mod_info(mod_id, skill_name=skill_name, include_skill_content=bool(skill_name))
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0

        if cmd in {"mount", "add", "enable", "set"}:
            agent = "all"
            replace = cmd == "set"
            mods: list[str] = []
            i = 0
            while i < len(rest):
                if rest[i] in {"--agent", "-a"} and i + 1 < len(rest):
                    agent = rest[i + 1]
                    i += 2
                elif rest[i] == "--replace":
                    replace = True
                    i += 1
                elif rest[i] in {"--mod-ids", "--skill-ids", "--mods"} and i + 1 < len(rest):
                    mods.extend(x.strip() for x in rest[i + 1].split(",") if x.strip())
                    i += 2
                elif rest[i] in {"--output", "-o"} and i + 1 < len(rest):
                    i += 2
                elif not rest[i].startswith("-"):
                    mods.append(rest[i])
                    i += 1
                else:
                    i += 1
            if not mods and not replace:
                raise ModError("At least one mod_id is required")
            res = mount_mod(mods, agent_id=agent, replace=replace)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0

        if cmd in {"unmount", "remove", "disable"}:
            agent = "all"
            mods = []
            i = 0
            while i < len(rest):
                if rest[i] in {"--agent", "-a"} and i + 1 < len(rest):
                    agent = rest[i + 1]
                    i += 2
                elif rest[i] in {"--mod-ids", "--skill-ids", "--mods"} and i + 1 < len(rest):
                    mods.extend(x.strip() for x in rest[i + 1].split(",") if x.strip())
                    i += 2
                elif rest[i] in {"--output", "-o"} and i + 1 < len(rest):
                    i += 2
                elif not rest[i].startswith("-"):
                    mods.append(rest[i])
                    i += 1
                else:
                    i += 1
            if not mods:
                raise ModError("At least one mod_id is required to unmount")
            res = unmount_mod(mods, agent_id=agent)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0

        if cmd == "mounted":
            agent = None
            i = 0
            while i < len(rest):
                if rest[i] in {"--agent", "-a"} and i + 1 < len(rest):
                    agent = rest[i + 1]
                    i += 2
                elif not rest[i].startswith("-"):
                    agent = rest[i]
                    i += 1
                else:
                    i += 1
            mounts = load_mounts()
            if agent:
                ak = normalize_agent_key(agent)
                print(json.dumps({"agent": ak, "mounted_mods": mounts.get(ak, []), "mounts": mounts}, ensure_ascii=False, indent=2))
            else:
                print(json.dumps({"mounts": mounts, "agents": KNOWN_AGENTS}, ensure_ascii=False, indent=2))
            return 0

        if cmd == "pack":
            if not rest:
                raise ModError("mod_id is required")
            src_dir = rest[1] if len(rest) > 1 and not rest[1].startswith("-") else None
            res = pack_mod(rest[0], src_dir)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0

        if cmd in {"install", "import"}:
            if not rest:
                raise ModError("source_path is required")
            res = install_mod(rest[0], overwrite=True)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0

        if cmd in {"uninstall", "delete"}:
            if not rest:
                raise ModError("mod_id is required")
            del_pkg = "--delete-package" in rest
            res = uninstall_mod(rest[0], delete_package=del_pkg)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0

        if cmd == "sync":
            skip_host = "--skip-host-plugins" in rest or os.environ.get("AGY_SKIP_HOST_PLUGINS") == "1"
            res = sync_all_runtimes(skip_host_plugins=skip_host)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0

        raise ModError(f"Unknown command: {cmd}")
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


def _sync_main_runtime_call() -> None:
    if "__main__" in sys.modules and "runtime_tools" in sys.modules:
        setattr(sys.modules["__main__"], "runtime_call", sys.modules["runtime_tools"].call)


_sync_main_runtime_call()

if __name__ == "__main__":
    raise SystemExit(main())
