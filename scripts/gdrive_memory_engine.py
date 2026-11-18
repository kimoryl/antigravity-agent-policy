"""Google Drive File-Backed + In-Memory SQLite (:memory: + FTS5) Dual-Layer Memory Engine.

Architecture:
1. Single Source of Truth in `05_Agent_Memory_Sync` (`AGY_MEMORY_SYNC_DIR` or `~/.antigravity-agent-policy/05_Agent_Memory_Sync`):
   - `L3_Core_Rules.md`          : L3 Core Persona & Hard Boundaries (Tier 1)
   - `L2_Domain_Index.md`        : L2 Domain Cognitive Maps (8 Domains)
   - `L1_Global_Memory.json`     : L1 Global Atomic Memories & Experiences
   - `L1_Projects/<proj>.json`   : Per-project isolated JSON files (Memories, Facts, Experiences)
   - `Inbox_Traces/*.json`       : Append-only waking traces & daytime delta operations
   - `Nightly_Dream_Reports/`    : Nightly Dreaming Journals & Skill Patch Proposals
2. Zero Binary Lock on Google Drive:
   - No `.sqlite3` or `-wal` file is ever placed on Google Drive.
   - Local processes load the JSON files into an in-memory SQLite database (`sqlite3.connect(":memory:")`)
     with FTS5 BM25 full-text search indexes, auto-reloading in <5ms whenever cloud files change (`mtime`).
"""

from __future__ import annotations

from contextlib import closing, contextmanager
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator

GLOBAL_PROJECT_ID = "__global__"

_LOCK = threading.RLock()
_MEM_CONN: sqlite3.Connection | None = None
_LAST_DISK_SIGNATURE: tuple[Any, ...] | None = None
_ACTIVE_SYNC_DIR: Path | None = None
_MIGRATED_SYNC_DIRS: set[str] = set()

_PRODUCTION_POLICY_DATA_SUFFIXES = (
    "/appdata/local/codex-agent-policy",
    "/.local/state/codex-agent-policy",
    "/.agent_policy_data",
    "/.codex/agent-policy",
)


def _is_production_policy_data_dir(raw_data: str | Path) -> bool:
    norm = str(Path(raw_data)).replace("\\", "/").rstrip("/").casefold()
    return any(norm.endswith(sfx) or norm == sfx for sfx in _PRODUCTION_POLICY_DATA_SUFFIXES)


def resolve_memory_sandbox_dir() -> Path | None:
    """Return isolated test sandbox memory dir only when explicitly in sandbox/temp test context."""
    sandbox_override = os.environ.get("AGY_MEMORY_SANDBOX_DIR")
    if sandbox_override:
        p = Path(sandbox_override)
        p.mkdir(parents=True, exist_ok=True)
        return p

    sandbox_data = os.environ.get("CODEX_AGENT_POLICY_DATA") or os.environ.get("PLUGIN_DATA")
    if not sandbox_data:
        return None

    explicit_flag = os.environ.get("AGY_MEMORY_SANDBOX", "").strip().lower() in {"1", "true", "yes"}
    if not explicit_flag and _is_production_policy_data_dir(sandbox_data):
        return None

    data_path = Path(sandbox_data)
    norm = str(data_path).replace("\\", "/").rstrip("/").casefold()
    sys_tmp = str(Path(tempfile.gettempdir())).replace("\\", "/").rstrip("/").casefold()
    in_temp = (
        bool(sys_tmp and (norm == sys_tmp or norm.startswith(sys_tmp + "/")))
        or any(
            seg in {"temp", "tmp", "pytest", "unittest"} or seg.startswith(("tmp", "pytest-", "star-test"))
            for seg in norm.split("/")
        )
    )
    sync_sub = data_path / "gdrive_memory_sync"
    has_seeded_files = False
    try:
        if sync_sub.is_dir():
            has_seeded_files = any(
                f.is_file() and f.suffix.lower() in {".json", ".md"}
                for f in sync_sub.rglob("*")
            )
    except OSError:
        has_seeded_files = False

    if explicit_flag or in_temp or has_seeded_files:
        sync_sub.mkdir(parents=True, exist_ok=True)
        return sync_sub
    return None


def is_memory_sandbox_active() -> bool:
    if os.environ.get("AGY_MEMORY_SYNC_DIR") or os.environ.get("AGY_GDRIVE_MEMORY_DIR"):
        return False
    return resolve_memory_sandbox_dir() is not None


def reset_connection_if_needed(force: bool = False) -> bool:
    """Safely close and reset the cached :memory: SQLite connection when sync_dir changes or force=True."""
    global _MEM_CONN, _LAST_DISK_SIGNATURE, _ACTIVE_SYNC_DIR
    target_dir = resolve_gdrive_memory_dir().resolve()
    with _LOCK:
        if _MEM_CONN is not None and (force or _ACTIVE_SYNC_DIR != target_dir):
            try:
                _MEM_CONN.close()
            except sqlite3.Error:
                pass
            _MEM_CONN = None
            _LAST_DISK_SIGNATURE = None
            _ACTIVE_SYNC_DIR = target_dir
            return True
        _ACTIVE_SYNC_DIR = target_dir
        return False


def resolve_gdrive_memory_dir() -> Path:
    """Resolve the `05_Agent_Memory_Sync` directory across local or cloud-synced storage."""
    env_override = os.environ.get("AGY_MEMORY_SYNC_DIR") or os.environ.get("AGY_GDRIVE_MEMORY_DIR")
    if env_override:
        p = Path(env_override)
        p.mkdir(parents=True, exist_ok=True)
        return p

    sandbox_dir = resolve_memory_sandbox_dir()
    if sandbox_dir is not None:
        return sandbox_dir

    fallback = Path.home() / ".antigravity-agent-policy" / "05_Agent_Memory_Sync"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def resolve_local_mirror_memory_dir() -> Path | None:
    """Resolve an optional secondary mirror directory configured via AGY_MEMORY_MIRROR_DIR."""
    if (
        os.environ.get("AGY_MEMORY_SYNC_DIR")
        or os.environ.get("AGY_GDRIVE_MEMORY_DIR")
        or resolve_memory_sandbox_dir() is not None
    ):
        return None
    mirror_env = os.environ.get("AGY_MEMORY_MIRROR_DIR", "").strip()
    if not mirror_env:
        return None
    try:
        p = Path(mirror_env)
        p.mkdir(parents=True, exist_ok=True)
        return p
    except OSError:
        return None



def resolve_local_sqlite_paths() -> list[Path]:
    """Resolve local physical disk SQLite database paths (never on Google Drive)."""
    paths: list[Path] = []
    try:
        from policy_state import data_dir
        paths.append(data_dir() / "runtime.sqlite3")
    except Exception:
        pass
    return paths


_DEFAULT_PROJECT_REGISTRY: dict[str, Any] = {
    "schema_version": "2.0",
    "default_unmapped_project_id": "workbench_misc",
    "projects": [],
}


def load_project_registry() -> dict[str, Any]:
    """Load 00_Project_Registry.json from Google Drive or local mirror with built-in fallback."""
    sync_dir = resolve_gdrive_memory_dir()
    reg_file = sync_dir / "00_Project_Registry.json"
    try:
        if reg_file.is_file():
            data = json.loads(reg_file.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict) and isinstance(data.get("projects"), list):
                return data
    except Exception:
        pass
    return _DEFAULT_PROJECT_REGISTRY


def save_project_registry(reg_data: dict[str, Any]) -> None:
    """Persist 00_Project_Registry.json to Google Drive and local mirror."""
    reg_data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    sync_dir = resolve_gdrive_memory_dir()
    _write_json_with_retry(sync_dir / "00_Project_Registry.json", reg_data)
    mirror_dir = resolve_local_mirror_memory_dir()
    if mirror_dir and mirror_dir != sync_dir:
        _write_json_with_retry(mirror_dir / "00_Project_Registry.json", reg_data)


def normalize_project_id(pid: str | None) -> str:
    """Normalize project names, aliases, or legacy hashes to canonical project_id."""
    raw = str(pid or "").strip()
    if not raw or raw == GLOBAL_PROJECT_ID:
        return GLOBAL_PROJECT_ID
    clean = raw.removeprefix("shared-")
    clean_cf = clean.casefold()
    reg = load_project_registry()
    for proj in reg.get("projects", []):
        canon = str(proj.get("project_id") or "").strip()
        if not canon:
            continue
        aliases = {
            str(a).strip().casefold()
            for a in (proj.get("aliases", []) + proj.get("legacy_aliases", []))
            if str(a).strip()
        }
        repo_short = str(proj.get("github_repo") or "").split("/")[-1].strip().casefold()
        if repo_short:
            aliases.add(repo_short)
        if clean_cf == canon.casefold() or raw.casefold() in aliases or clean_cf in aliases:
            return canon
    if clean_cf == str(reg.get("default_unmapped_project_id") or "workbench_misc").casefold():
        return str(reg.get("default_unmapped_project_id") or "workbench_misc")
    return clean


def resolve_project_id_via_registry(path_or_id: str | Path) -> str:
    """Resolve any workspace folder path or project alias to its bound project_id (or 'workbench_misc' if unbound)."""
    raw_str = str(path_or_id or "").strip()
    if not raw_str:
        return "workbench_misc"
    reg = load_project_registry()
    default_unmapped = str(reg.get("default_unmapped_project_id") or "workbench_misc")

    # 1. Check direct project_id or alias match first
    clean = raw_str.removeprefix("shared-")
    clean_cf = clean.casefold()
    for proj in reg.get("projects", []):
        canon = str(proj.get("project_id") or "").strip()
        aliases = {
            str(a).strip().casefold()
            for a in (proj.get("aliases", []) + proj.get("legacy_aliases", []))
            if str(a).strip()
        }
        if clean_cf == canon.casefold() or raw_str.casefold() in aliases or clean_cf in aliases:
            return canon
    if clean_cf == default_unmapped.casefold():
        return default_unmapped

    # 2. Normalize filesystem path and match registered folder prefixes
    norm_target = raw_str.replace("\\", "/").rstrip("/").casefold()
    try:
        resolved_str = str(Path(raw_str).resolve()).replace("\\", "/").rstrip("/").casefold()
    except Exception:
        resolved_str = norm_target

    for proj in reg.get("projects", []):
        canon = str(proj.get("project_id") or "").strip()
        if not canon:
            continue
        for folder in proj.get("folders", []):
            fpath = str(folder.get("path") if isinstance(folder, dict) else folder or "").strip()
            if not fpath:
                continue
            norm_folder = fpath.replace("\\", "/").rstrip("/").casefold()
            for candidate in (norm_target, resolved_str):
                if candidate == norm_folder or candidate.startswith(norm_folder + "/"):
                    return canon

    return default_unmapped


def bind_workspace_to_project(folder_path: str | Path, project_id_or_alias: str, role: str = "") -> dict[str, Any]:
    """Bind a workspace folder to one of the 5 registered projects (or create a new project entry if requested)."""
    raw_folder = str(folder_path or "").strip()
    if not raw_folder:
        raise ValueError("folder_path is required to bind a workspace")
    try:
        norm_folder_str = str(Path(raw_folder).resolve()) if not raw_folder.startswith("/") else raw_folder
    except Exception:
        norm_folder_str = raw_folder
    norm_cmp = norm_folder_str.replace("\\", "/").rstrip("/").casefold()

    target_pid = normalize_project_id(project_id_or_alias)
    reg = load_project_registry()
    projects_list = reg.setdefault("projects", [])

    # Remove this folder from any previously bound project first
    previous_project: str | None = None
    for proj in projects_list:
        new_folders = []
        for fitem in proj.get("folders", []):
            fp = str(fitem.get("path") if isinstance(fitem, dict) else fitem or "").strip()
            if fp.replace("\\", "/").rstrip("/").casefold() == norm_cmp:
                previous_project = str(proj.get("project_id"))
            else:
                new_folders.append(fitem)
        proj["folders"] = new_folders

    # Locate target project (or create if brand new)
    target_obj: dict[str, Any] | None = None
    for proj in projects_list:
        if str(proj.get("project_id") or "").casefold() == target_pid.casefold():
            target_obj = proj
            target_pid = str(proj["project_id"])
            break

    if target_obj is None:
        target_obj = {
            "project_id": target_pid,
            "name": target_pid,
            "github_repo": "",
            "visibility": "private",
            "memory_file": f"05_Agent_Memory_Sync/L1_Projects/{target_pid}.json",
            "aliases": [target_pid.lower()],
            "description": f"{target_pid} 项目",
            "folders": [],
        }
        projects_list.append(target_obj)

    folder_entry: dict[str, str] = {
        "path": norm_folder_str,
        "role": role or "绑定工作区",
        "bound_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    target_obj.setdefault("folders", []).append(folder_entry)
    save_project_registry(reg)

    # Ensure L1_Projects/<target_pid>.json exists on Google Drive
    sync_dir = resolve_gdrive_memory_dir()
    pfile = sync_dir / "L1_Projects" / f"{target_pid}.json"
    if not pfile.is_file():
        _write_json_with_retry(
            pfile,
            {
                "project_id": target_pid,
                "project_name": target_obj.get("name", target_pid),
                "github_repo": target_obj.get("github_repo", ""),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "memories": [],
                "experiences": [],
                "facts": [],
            },
        )

    return {
        "status": "bound",
        "project_id": target_pid,
        "github_repo": target_obj.get("github_repo"),
        "memory_file": target_obj.get("memory_file"),
        "bound_folder": norm_folder_str,
        "previous_project": previous_project or "workbench_misc",
        "all_bound_folders": target_obj.get("folders", []),
    }


def unbind_workspace_from_project(folder_path: str | Path) -> dict[str, Any]:
    """Unbind a workspace folder so it reverts to workbench_misc."""
    raw_folder = str(folder_path or "").strip()
    try:
        norm_folder_str = str(Path(raw_folder).resolve()) if not raw_folder.startswith("/") else raw_folder
    except Exception:
        norm_folder_str = raw_folder
    norm_cmp = norm_folder_str.replace("\\", "/").rstrip("/").casefold()

    reg = load_project_registry()
    removed_from: list[str] = []
    for proj in reg.get("projects", []):
        before = len(proj.get("folders", []))
        proj["folders"] = [
            fitem for fitem in proj.get("folders", [])
            if str(fitem.get("path") if isinstance(fitem, dict) else fitem or "").replace("\\", "/").rstrip("/").casefold() != norm_cmp
        ]
        if len(proj["folders"]) < before:
            removed_from.append(str(proj.get("project_id")))

    if removed_from:
        save_project_registry(reg)
    return {
        "status": "unbound",
        "folder": norm_folder_str,
        "removed_from_projects": removed_from,
        "current_project_id": "workbench_misc",
    }


def fts_tokenize_text(text: str) -> str:
    """Convert bilingual text into space-delimited ASCII words + Chinese character bigrams for SQLite FTS5."""
    lowered = (text or "").casefold()
    tokens: list[str] = re.findall(r"[a-z0-9_]{2,}", lowered)
    for chunk in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(chunk) == 1:
            tokens.append(chunk)
        else:
            for idx in range(len(chunk) - 1):
                tokens.append(chunk[idx:idx + 2])
    return " ".join(tokens)


def build_fts_match_query(query: str) -> str:
    """Build a safe SQLite FTS5 MATCH expression with OR semantics for BM25 ranking."""
    segmented = fts_tokenize_text(query)
    unique_tokens = []
    seen: set[str] = set()
    for tok in segmented.split():
        if tok not in seen:
            seen.add(tok)
            unique_tokens.append(f'"{tok}"')
        if len(unique_tokens) >= 24:
            break
    return " OR ".join(unique_tokens)


def _init_in_memory_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS facts (
            project_id TEXT NOT NULL,
            fact_key TEXT NOT NULL,
            value TEXT NOT NULL,
            source_path TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            evidence TEXT NOT NULL,
            source_revision TEXT,
            updated_at REAL NOT NULL,
            PRIMARY KEY (project_id, fact_key)
        );

        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            summary TEXT NOT NULL,
            source_task_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            scope TEXT NOT NULL DEFAULT 'project',
            hard_boundary INTEGER NOT NULL DEFAULT 0,
            valid_from REAL,
            invalid_at REAL,
            superseded_by TEXT,
            source_doc TEXT,
            pyramid_level TEXT DEFAULT 'L1_ATOMIC_LESSON',
            trust_tier TEXT DEFAULT 'TIER_2_RUNTIME_VERIFIED'
        );
        CREATE INDEX IF NOT EXISTS idx_mem_scope_proj ON memories(scope, project_id, invalid_at, hard_boundary DESC, created_at DESC);

        CREATE TABLE IF NOT EXISTS experiences (
            experience_id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            project_id TEXT,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            conditions TEXT NOT NULL,
            recommendations TEXT NOT NULL,
            anti_patterns TEXT NOT NULL,
            confidence REAL NOT NULL,
            status TEXT NOT NULL,
            support_count INTEGER NOT NULL DEFAULT 1,
            contradiction_count INTEGER NOT NULL DEFAULT 0,
            project_count INTEGER NOT NULL DEFAULT 1,
            source_refs TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            valid_from REAL,
            invalid_at REAL,
            superseded_by TEXT,
            hard_boundary INTEGER NOT NULL DEFAULT 0,
            pyramid_level TEXT DEFAULT 'L1_ATOMIC_LESSON',
            trust_tier TEXT DEFAULT 'TIER_2_RUNTIME_VERIFIED'
        );
        CREATE INDEX IF NOT EXISTS idx_exp_scope_proj ON experiences(scope, project_id, status, invalid_at, updated_at DESC);

        CREATE TABLE IF NOT EXISTS experience_sources (
            source_id TEXT PRIMARY KEY,
            experience_id TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            excerpt_ref TEXT,
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS experience_validations (
            validation_id TEXT PRIMARY KEY,
            experience_id TEXT NOT NULL,
            verdict TEXT NOT NULL,
            validation_type TEXT NOT NULL,
            reviewer TEXT NOT NULL,
            evidence_ref TEXT,
            task_id TEXT,
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS experience_promotions (
            promotion_id TEXT PRIMARY KEY,
            experience_id TEXT NOT NULL,
            from_scope TEXT NOT NULL,
            to_scope TEXT NOT NULL,
            decision TEXT NOT NULL,
            reviewer TEXT NOT NULL,
            reason TEXT,
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS skill_proposals (
            proposal_id TEXT PRIMARY KEY,
            experience_id TEXT NOT NULL,
            name TEXT NOT NULL,
            version TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT NOT NULL,
            path TEXT NOT NULL,
            validations TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS policy_candidates (
            candidate_id TEXT PRIMARY KEY,
            experience_id TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            reviewer TEXT NOT NULL,
            created_at REAL NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            id UNINDEXED,
            tokens
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS experiences_fts USING fts5(
            experience_id UNINDEXED,
            tokens
        );
    """)


def _read_json_with_retry(path: Path, retries: int = 3) -> dict[str, Any] | None:
    for attempt in range(retries):
        try:
            if not path.is_file():
                return None
            return json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            if attempt + 1 < retries:
                time.sleep(0.15 * (attempt + 1))
    return None


def _write_json_with_retry(path: Path, payload: dict[str, Any], retries: int = 3) -> bool:
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    for attempt in range(retries):
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp_path, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
            return True
        except OSError:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            if attempt + 1 < retries:
                time.sleep(0.2 * (attempt + 1))
    return False


def _safe_timestamp(val: Any, default: float) -> float:
    """Safely convert a numeric timestamp or ISO-8601 string into a float epoch timestamp."""
    if val is None or val == "":
        return default
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    try:
        return float(s)
    except ValueError:
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return default


def load_modalities_registry() -> list[dict[str, Any]]:
    """Return the list of 5 horizontal craft modalities from 00_Project_Registry.json."""
    reg = load_project_registry()
    mods = reg.get("modalities")
    if isinstance(mods, list) and mods:
        return mods
    return [
        {"modality_id": "ppt_product_pitch", "name": "产品介绍与演示文稿工艺", "memory_file": "05_Agent_Memory_Sync/L1_Modalities/ppt_product_pitch.json"},
        {"modality_id": "ui_aesthetic_design", "name": "UI 视觉美学、OKLCH 色彩与前端组件交互工艺", "memory_file": "05_Agent_Memory_Sync/L1_Modalities/ui_aesthetic_design.json"},
        {"modality_id": "office_report_doc", "name": "Word/Excel/PDF 工程报告与三线表排版工艺", "memory_file": "05_Agent_Memory_Sync/L1_Modalities/office_report_doc.json"},
        {"modality_id": "coding_and_testing", "name": "代码架构、静态 AST 边界与单元测试工艺", "memory_file": "05_Agent_Memory_Sync/L1_Modalities/coding_and_testing.json"},
        {"modality_id": "systems_and_automation", "name": "系统工程、桌面自动化与数据管线工艺", "memory_file": "05_Agent_Memory_Sync/L1_Modalities/systems_and_automation.json"},
    ]


def load_modality_memories(modality_id: str | None = None, query: str = "") -> dict[str, Any]:
    """Load horizontal craft rules and memories from 05_Agent_Memory_Sync/L1_Modalities/*.json."""
    sync_dir = resolve_gdrive_memory_dir()
    mod_dir = sync_dir / "L1_Modalities"
    target_id = str(modality_id or "").strip().casefold()
    q_tokens = set(fts_tokenize_text(query).split()) if query and query.strip() else set()

    matched_modalities: list[dict[str, Any]] = []
    craft_rules: list[dict[str, Any]] = []
    if mod_dir.is_dir():
        for mf in sorted(mod_dir.glob("*.json")):
            mdata = _read_json_with_retry(mf)
            if not isinstance(mdata, dict):
                continue
            mid = str(mdata.get("modality_id") or mf.stem)
            if target_id and mid.casefold() != target_id:
                continue
            matched_modalities.append({
                "modality_id": mid,
                "modality_name": mdata.get("modality_name", mid),
                "associated_mods": mdata.get("associated_mods", []),
                "updated_at": mdata.get("updated_at"),
            })
            for item in [*mdata.get("craft_rules", []), *mdata.get("memories", [])]:
                if not isinstance(item, dict) or item.get("invalid_at") is not None:
                    continue
                if q_tokens:
                    haystack = f"{item.get('title', '')} {item.get('summary', '')} {mid} {' '.join(str(x) for x in item.get('recommendations', []))}"
                    overlap = len(q_tokens & set(fts_tokenize_text(haystack).split()))
                    if overlap == 0 and not target_id:
                        continue
                craft_rules.append({**item, "modality_id": mid, "scope": "modality"})
    return {
        "modalities": matched_modalities,
        "craft_rules": craft_rules,
        "count": len(craft_rules),
    }


def _compute_disk_signature(sync_dir: Path) -> tuple[Any, ...]:
    sig: list[Any] = [str(sync_dir)]
    global_f = sync_dir / "L1_Global_Memory.json"
    l2_exp_f = sync_dir / "L2_Experiences.json"
    reg_f = sync_dir / "00_Project_Registry.json"
    bundle_f = sync_dir / "distilled_memory_bundle.json"
    projects_dir = sync_dir / "L1_Projects"
    modalities_dir = sync_dir / "L1_Modalities"
    inbox_dir = sync_dir / "Inbox_Traces"

    for f in (global_f, l2_exp_f, reg_f, bundle_f):
        try:
            if f.is_file():
                st = f.stat()
                sig.append((f.name, st.st_mtime_ns, st.st_size))
        except OSError:
            pass

    for d in (projects_dir, modalities_dir, inbox_dir):
        try:
            if d.is_dir():
                for jf in sorted(d.glob("*.json")):
                    if jf.name.startswith("waking_traces_"):
                        continue
                    st = jf.stat()
                    sig.append((f"{d.name}/{jf.name}", st.st_mtime_ns, st.st_size))
        except OSError:
            pass
    return tuple(sig)


def _infer_pyramid_and_tier(item: dict[str, Any]) -> tuple[str, str]:
    p_level = item.get("pyramid_level")
    t_tier = item.get("trust_tier")
    if p_level and t_tier:
        return str(p_level), str(t_tier)
    if item.get("hard_boundary"):
        return "L3_CORE_PERSONA", "TIER_1_SYSTEM_HARD"
    sdoc = str(item.get("source_doc") or "")
    stask = str(item.get("source_task_id") or "")
    if sdoc.endswith("__DOMAIN_SYNTHESIS__"):
        return "L2_DOMAIN_SCENE", "TIER_2_RUNTIME_VERIFIED"
    if stask in {"cloud-kb-distiller"} or sdoc.startswith(("01_", "02_", "03_", "04_")):
        return "L1_ATOMIC_LESSON", "TIER_3_EXTERNAL_KB"
    return "L1_ATOMIC_LESSON", "TIER_2_RUNTIME_VERIFIED"


def _bootstrap_gdrive_from_legacy_if_needed(sync_dir: Path) -> None:
    """One-time migration: move any remaining local runtime.sqlite3 or legacy bundle into Google Drive L1 files, then purge local SQLite memory tables."""
    sync_key = str(sync_dir)
    if sync_key in _MIGRATED_SYNC_DIRS:
        return
    _MIGRATED_SYNC_DIRS.add(sync_key)

    global_file = sync_dir / "L1_Global_Memory.json"
    bundle_file = sync_dir / "distilled_memory_bundle.json"
    projects_dir = sync_dir / "L1_Projects"
    inbox_dir = sync_dir / "Inbox_Traces"
    projects_dir.mkdir(parents=True, exist_ok=True)
    inbox_dir.mkdir(parents=True, exist_ok=True)

    # Seed L1_Global_Memory.json from distilled_memory_bundle.json if L1_Global_Memory.json does not exist yet
    if not global_file.is_file() and bundle_file.is_file():
        bundle = _read_json_with_retry(bundle_file)
        if bundle:
            _write_json_with_retry(
                global_file,
                {
                    "schema_version": "2.0-gdrive-native",
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "global_memories": bundle.get("global_memories", []),
                    "global_experiences": bundle.get("global_experiences", []),
                    "global_facts": [],
                },
            )
            by_proj: dict[str, list[dict[str, Any]]] = {}
            for pm in bundle.get("project_memories", []):
                pid = str(pm.get("project_id") or "default")
                if pid != GLOBAL_PROJECT_ID:
                    by_proj.setdefault(pid, []).append(pm)
            for pid, pms in by_proj.items():
                pfile = projects_dir / f"{pid}.json"
                if not pfile.is_file():
                    _write_json_with_retry(
                        pfile,
                        {
                            "project_id": pid,
                            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                            "memories": pms,
                            "experiences": [],
                            "facts": [],
                        },
                    )

    # Migrate & purge local runtime.sqlite3 memory/fact/experience rows so local disk holds zero memory state
    try:
        from policy_state import data_dir
        local_db_path = data_dir() / "runtime.sqlite3"
        if local_db_path.is_file():
            with closing(sqlite3.connect(local_db_path, timeout=5.0)) as ldb, ldb:
                ldb.row_factory = sqlite3.Row
                tables = {r[0] for r in ldb.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                local_proj_mems: dict[str, list[dict[str, Any]]] = {}
                local_proj_facts: dict[str, list[dict[str, Any]]] = {}
                local_proj_exps: dict[str, list[dict[str, Any]]] = {}

                if "memories" in tables:
                    for r in ldb.execute("SELECT * FROM memories WHERE scope = 'project' AND invalid_at IS NULL").fetchall():
                        rk = r.keys()
                        pid = str(r["project_id"])
                        if pid and pid != GLOBAL_PROJECT_ID:
                            local_proj_mems.setdefault(pid, []).append({
                                "memory_id": r["id"],
                                "project_id": pid,
                                "scope": "project",
                                "hard_boundary": bool(r["hard_boundary"]) if "hard_boundary" in rk else False,
                                "kind": r["kind"],
                                "summary": r["summary"],
                                "source_task_id": r["source_task_id"],
                                "created_at": r["created_at"],
                                "valid_from": r["valid_from"] if "valid_from" in rk else r["created_at"],
                                "invalid_at": None,
                                "superseded_by": None,
                                "source_doc": r["source_doc"] if "source_doc" in rk else None,
                            })

                if "facts" in tables:
                    for r in ldb.execute("SELECT * FROM facts").fetchall():
                        pid = str(r["project_id"])
                        local_proj_facts.setdefault(pid, []).append(dict(r))

                if "experiences" in tables:
                    for r in ldb.execute("SELECT * FROM experiences WHERE scope = 'project' AND invalid_at IS NULL").fetchall():
                        rk = r.keys()
                        pid = str(r["project_id"] or "default")
                        if pid != GLOBAL_PROJECT_ID:
                            local_proj_exps.setdefault(pid, []).append({
                                "experience_id": r["experience_id"],
                                "scope": "project",
                                "project_id": pid,
                                "title": r["title"],
                                "summary": r["summary"],
                                "conditions": json.loads(r["conditions"] or "[]"),
                                "recommendations": json.loads(r["recommendations"] or "[]"),
                                "anti_patterns": json.loads(r["anti_patterns"] or "[]"),
                                "confidence": float(r["confidence"]),
                                "status": r["status"],
                                "support_count": int(r["support_count"]),
                                "contradiction_count": int(r["contradiction_count"]),
                                "project_count": int(r["project_count"]),
                                "source_refs": json.loads(r["source_refs"] or "[]"),
                                "created_at": r["created_at"],
                                "updated_at": r["updated_at"],
                                "hard_boundary": bool(r["hard_boundary"]) if "hard_boundary" in rk else False,
                            })

                all_pids = set(local_proj_mems) | set(local_proj_facts) | set(local_proj_exps)
                for pid in all_pids:
                    pfile = projects_dir / f"{pid}.json"
                    existing = _read_json_with_retry(pfile) or {"project_id": pid, "memories": [], "experiences": [], "facts": []}
                    mem_ids = {m.get("memory_id") or m.get("id") for m in existing.get("memories", [])}
                    for m in local_proj_mems.get(pid, []):
                        if m["memory_id"] not in mem_ids:
                            existing.setdefault("memories", []).append(m)
                    exp_ids = {e.get("experience_id") for e in existing.get("experiences", [])}
                    for e in local_proj_exps.get(pid, []):
                        if e["experience_id"] not in exp_ids:
                            existing.setdefault("experiences", []).append(e)
                    fact_keys = {f.get("fact_key") for f in existing.get("facts", [])}
                    for f in local_proj_facts.get(pid, []):
                        if f["fact_key"] not in fact_keys:
                            existing.setdefault("facts", []).append(f)
                    existing["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                    _write_json_with_retry(pfile, existing)
    except Exception:
        pass


def _reload_memory_db_from_gdrive(conn: sqlite3.Connection, sync_dir: Path) -> None:
    """Populate the in-memory SQLite tables and FTS5 indexes from Google Drive JSON files."""
    _init_in_memory_schema(conn)
    now = time.time()

    global_file = sync_dir / "L1_Global_Memory.json"
    l2_exp_file = sync_dir / "L2_Experiences.json"
    bundle_file = sync_dir / "distilled_memory_bundle.json"
    projects_dir = sync_dir / "L1_Projects"
    modalities_dir = sync_dir / "L1_Modalities"
    inbox_dir = sync_dir / "Inbox_Traces"

    global_data = _read_json_with_retry(global_file)
    if not global_data and bundle_file.is_file():
        global_data = _read_json_with_retry(bundle_file)
    l2_exp_data = _read_json_with_retry(l2_exp_file)

    project_payloads: list[dict[str, Any]] = []
    if projects_dir.is_dir():
        for pf in sorted(projects_dir.glob("*.json")):
            pdata = _read_json_with_retry(pf)
            if pdata:
                project_payloads.append(pdata)

    modality_payloads: list[dict[str, Any]] = []
    if modalities_dir.is_dir():
        for mf in sorted(modalities_dir.glob("*.json")):
            mdata = _read_json_with_retry(mf)
            if mdata:
                modality_payloads.append(mdata)

    inbox_payloads: list[dict[str, Any]] = []
    if inbox_dir.is_dir():
        for inf in sorted(inbox_dir.glob("*.json")):
            if inf.name.startswith("waking_traces_"):
                continue
            indata = _read_json_with_retry(inf)
            if indata:
                inbox_payloads.append(indata)

    # Clear and repopulate primary tables
    conn.execute("DELETE FROM facts")
    conn.execute("DELETE FROM memories")
    conn.execute("DELETE FROM experiences")
    conn.execute("DELETE FROM memories_fts")
    conn.execute("DELETE FROM experiences_fts")

    def _insert_mem(m: dict[str, Any], default_scope: str = "global") -> None:
        mid = str(m.get("memory_id") or m.get("rule_id") or m.get("id") or "")
        if not mid:
            return
        scope = str(m.get("scope") or default_scope)
        if scope == "global":
            pid = GLOBAL_PROJECT_ID
        elif scope == "modality":
            pid = str(m.get("modality_id") or m.get("project_id") or "modality")
        else:
            pid = normalize_project_id(str(m.get("project_id") or "default"))
        kind = str(m.get("kind") or "lesson")
        summary = str(m.get("summary") or "")
        if not summary:
            return
        stask = str(m.get("source_task_id") or "gdrive-sync")
        c_at = _safe_timestamp(m.get("created_at"), now)
        v_from = _safe_timestamp(m.get("valid_from"), c_at)
        inv_at = _safe_timestamp(m.get("invalid_at"), 0.0) if m.get("invalid_at") is not None else None
        sup_by = m.get("superseded_by")
        s_doc = m.get("source_doc")
        hb = 1 if m.get("hard_boundary") else 0
        p_level, t_tier = _infer_pyramid_and_tier(m)
        conn.execute(
            """INSERT OR REPLACE INTO memories
               (id, project_id, kind, summary, source_task_id, created_at, scope, hard_boundary,
                valid_from, invalid_at, superseded_by, source_doc, pyramid_level, trust_tier)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (mid, pid, kind, summary, stask, c_at, scope, hb, v_from, inv_at, sup_by, s_doc, p_level, t_tier),
        )

    def _insert_exp(e: dict[str, Any], default_scope: str = "global") -> None:
        eid = str(e.get("experience_id") or "")
        if not eid:
            return
        scope = str(e.get("scope") or default_scope)
        pid = GLOBAL_PROJECT_ID if scope == "global" else normalize_project_id(str(e.get("project_id") or "default"))
        title = str(e.get("title") or "")
        summary = str(e.get("summary") or "")
        if not title or not summary:
            return
        conds = e.get("conditions", [])
        recs = e.get("recommendations", [])
        antis = e.get("anti_patterns", [])
        refs = e.get("source_refs", [])
        if isinstance(conds, str):
            conds = json.loads(conds or "[]")
        if isinstance(recs, str):
            recs = json.loads(recs or "[]")
        if isinstance(antis, str):
            antis = json.loads(antis or "[]")
        if isinstance(refs, str):
            refs = json.loads(refs or "[]")
        c_at = _safe_timestamp(e.get("created_at"), now)
        u_at = _safe_timestamp(e.get("updated_at"), c_at)
        v_from = _safe_timestamp(e.get("valid_from"), c_at)
        inv_at = _safe_timestamp(e.get("invalid_at"), 0.0) if e.get("invalid_at") is not None else None
        sup_by = e.get("superseded_by")
        hb = 1 if e.get("hard_boundary") else 0
        p_level, t_tier = _infer_pyramid_and_tier(e)
        conn.execute(
            """INSERT OR REPLACE INTO experiences
               (experience_id, scope, project_id, title, summary, conditions, recommendations, anti_patterns,
                confidence, status, support_count, contradiction_count, project_count, source_refs,
                created_at, updated_at, valid_from, invalid_at, superseded_by, hard_boundary, pyramid_level, trust_tier)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                eid, scope, pid, title, summary,
                json.dumps(conds, ensure_ascii=False),
                json.dumps(recs, ensure_ascii=False),
                json.dumps(antis, ensure_ascii=False),
                float(e.get("confidence", 0.85)),
                str(e.get("status") or ("global" if scope == "global" else "validated")),
                int(e.get("support_count", 1)),
                int(e.get("contradiction_count", 0)),
                int(e.get("project_count", 1)),
                json.dumps(refs, ensure_ascii=False),
                c_at, u_at, v_from, inv_at, sup_by, hb, p_level, t_tier,
            ),
        )

    def _insert_fact(f: dict[str, Any], default_pid: str = GLOBAL_PROJECT_ID) -> None:
        raw_pid = str(f.get("project_id") or default_pid)
        pid = GLOBAL_PROJECT_ID if raw_pid == GLOBAL_PROJECT_ID else normalize_project_id(raw_pid)
        fkey = str(f.get("fact_key") or f.get("key") or "")
        val = str(f.get("value") or "")
        if not fkey or not val:
            return
        conn.execute(
            "INSERT OR REPLACE INTO facts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pid, fkey, val,
                str(f.get("source_path") or ""),
                str(f.get("source_sha256") or ""),
                str(f.get("evidence") or ""),
                f.get("source_revision"),
                _safe_timestamp(f.get("updated_at"), now),
            ),
        )

    if global_data:
        for gm in global_data.get("global_memories", []):
            _insert_mem(gm, "global")
        for ge in global_data.get("global_experiences", []):
            _insert_exp(ge, "global")
        for gf in global_data.get("global_facts", []):
            _insert_fact(gf, GLOBAL_PROJECT_ID)
        for pm in global_data.get("project_memories", []):
            _insert_mem(pm, "project")
        for pe in global_data.get("project_experiences", []):
            _insert_exp(pe, "project")

    if isinstance(l2_exp_data, dict):
        for ge in l2_exp_data.get("experiences", []):
            if isinstance(ge, dict):
                _insert_exp(ge, "global")

    for pdata in project_payloads:
        pid = str(pdata.get("project_id") or "default")
        for pm in pdata.get("memories", []):
            _insert_mem({**pm, "project_id": pid}, "project")
        for pe in pdata.get("experiences", []):
            _insert_exp({**pe, "project_id": pid}, "project")
        for pf in pdata.get("facts", []):
            _insert_fact(pf, pid)

    for mdata in modality_payloads:
        mid = str(mdata.get("modality_id") or "modality")
        for cm in [*mdata.get("craft_rules", []), *mdata.get("memories", [])]:
            if isinstance(cm, dict):
                title_prefix = f"[{cm['title']}] " if cm.get("title") and not str(cm.get("summary", "")).startswith("[") else ""
                _insert_mem(
                    {
                        **cm,
                        "memory_id": cm.get("memory_id") or cm.get("rule_id"),
                        "modality_id": mid,
                        "project_id": mid,
                        "scope": "modality",
                        "summary": f"{title_prefix}{cm.get('summary', '')}".strip(),
                        "source_doc": cm.get("source_doc") or f"05_Agent_Memory_Sync/L1_Modalities/{mid}.json",
                    },
                    "modality",
                )

    for indata in inbox_payloads:
        for m in indata.get("memories", []):
            _insert_mem(m, str(m.get("scope") or "project"))
        for e in indata.get("experiences", []):
            _insert_exp(e, str(e.get("scope") or "project"))
        for f in indata.get("facts", []):
            _insert_fact(f, str(f.get("project_id") or GLOBAL_PROJECT_ID))

    # Build FTS5 BM25 full-text indexes for active memories & experiences
    for row in conn.execute("SELECT id, summary, source_doc FROM memories WHERE invalid_at IS NULL").fetchall():
        toks = fts_tokenize_text(f"{row['summary']} {row['source_doc'] or ''}")
        conn.execute("INSERT INTO memories_fts (id, tokens) VALUES (?, ?)", (row["id"], toks))

    for row in conn.execute("SELECT experience_id, title, summary, conditions, recommendations FROM experiences WHERE invalid_at IS NULL").fetchall():
        toks = fts_tokenize_text(f"{row['title']} {row['summary']} {row['conditions']} {row['recommendations']}")
        conn.execute("INSERT INTO experiences_fts (experience_id, tokens) VALUES (?, ?)", (row["experience_id"], toks))

    conn.commit()


def _flush_in_memory_db_to_gdrive(conn: sqlite3.Connection, sync_dir: Path) -> None:
    """Persist any mutations made to the in-memory SQLite DB directly back to Google Drive JSON files & Inbox."""
    projects_dir = sync_dir / "L1_Projects"
    inbox_dir = sync_dir / "Inbox_Traces"
    projects_dir.mkdir(parents=True, exist_ok=True)
    inbox_dir.mkdir(parents=True, exist_ok=True)

    # Rebuild FTS5 index after mutations
    conn.execute("DELETE FROM memories_fts")
    for row in conn.execute("SELECT id, summary, source_doc FROM memories WHERE invalid_at IS NULL").fetchall():
        conn.execute("INSERT INTO memories_fts (id, tokens) VALUES (?, ?)", (row["id"], fts_tokenize_text(f"{row['summary']} {row['source_doc'] or ''}")))

    conn.execute("DELETE FROM experiences_fts")
    for row in conn.execute("SELECT experience_id, title, summary, conditions, recommendations FROM experiences WHERE invalid_at IS NULL").fetchall():
        conn.execute("INSERT INTO experiences_fts (experience_id, tokens) VALUES (?, ?)", (row["experience_id"], fts_tokenize_text(f"{row['title']} {row['summary']} {row['conditions']} {row['recommendations']}")))
    conn.commit()

    # 1. Persist per-project JSON files (`L1_Projects/<project_id>.json`)
    proj_mems: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute("SELECT * FROM memories WHERE scope = 'project' ORDER BY created_at DESC").fetchall():
        pid = str(r["project_id"])
        proj_mems.setdefault(pid, []).append({
            "memory_id": r["id"],
            "project_id": pid,
            "scope": "project",
            "hard_boundary": bool(r["hard_boundary"]),
            "pyramid_level": r["pyramid_level"],
            "trust_tier": r["trust_tier"],
            "kind": r["kind"],
            "summary": r["summary"],
            "source_task_id": r["source_task_id"],
            "created_at": r["created_at"],
            "valid_from": r["valid_from"],
            "invalid_at": r["invalid_at"],
            "superseded_by": r["superseded_by"],
            "source_doc": r["source_doc"],
        })

    proj_exps: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute("SELECT * FROM experiences WHERE scope = 'project' ORDER BY updated_at DESC").fetchall():
        pid = str(r["project_id"] or "default")
        proj_exps.setdefault(pid, []).append({
            "experience_id": r["experience_id"],
            "scope": "project",
            "project_id": pid,
            "title": r["title"],
            "summary": r["summary"],
            "conditions": json.loads(r["conditions"] or "[]"),
            "recommendations": json.loads(r["recommendations"] or "[]"),
            "anti_patterns": json.loads(r["anti_patterns"] or "[]"),
            "confidence": float(r["confidence"]),
            "status": r["status"],
            "support_count": int(r["support_count"]),
            "contradiction_count": int(r["contradiction_count"]),
            "project_count": int(r["project_count"]),
            "source_refs": json.loads(r["source_refs"] or "[]"),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "valid_from": r["valid_from"],
            "invalid_at": r["invalid_at"],
            "superseded_by": r["superseded_by"],
            "hard_boundary": bool(r["hard_boundary"]),
            "pyramid_level": r["pyramid_level"],
            "trust_tier": r["trust_tier"],
        })

    proj_facts: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute("SELECT * FROM facts ORDER BY fact_key").fetchall():
        pid = str(r["project_id"])
        proj_facts.setdefault(pid, []).append(dict(r))

    all_pids = (set(proj_mems) | set(proj_exps) | set(proj_facts)) - {GLOBAL_PROJECT_ID}
    for pid in all_pids:
        safe_pid = re.sub(r"[^A-Za-z0-9_.-]+", "_", pid)
        pfile = projects_dir / f"{safe_pid}.json"
        existing_proj = _read_json_with_retry(pfile) or {}
        preserved_header = {
            k: v
            for k, v in existing_proj.items()
            if k not in {"project_id", "updated_at", "memories", "experiences", "facts"}
        }
        _write_json_with_retry(
            pfile,
            {
                "project_id": pid,
                **preserved_header,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "memories": proj_mems.get(pid, []),
                "experiences": proj_exps.get(pid, []),
                "facts": proj_facts.get(pid, []),
            },
        )

    # 2. Write daytime global additions & project deltas into `Inbox_Traces/delta_local_ops.json`
    #    (and update L1_Global_Memory.json so global writes are also immediately persisted)
    global_mems = []
    daytime_global_mems = []
    for r in conn.execute("SELECT * FROM memories WHERE scope = 'global' ORDER BY hard_boundary DESC, created_at DESC").fetchall():
        item = {
            "memory_id": r["id"],
            "project_id": GLOBAL_PROJECT_ID,
            "scope": "global",
            "hard_boundary": bool(r["hard_boundary"]),
            "pyramid_level": r["pyramid_level"],
            "trust_tier": r["trust_tier"],
            "kind": r["kind"],
            "summary": r["summary"],
            "source_task_id": r["source_task_id"],
            "created_at": r["created_at"],
            "valid_from": r["valid_from"],
            "invalid_at": r["invalid_at"],
            "superseded_by": r["superseded_by"],
            "source_doc": r["source_doc"],
        }
        global_mems.append(item)
        if str(r["source_task_id"]) not in {"cloud-kb-distiller", "agy-cloud-domain-synthesizer", "system-seed"}:
            daytime_global_mems.append(item)

    global_exps = []
    daytime_global_exps = []
    for r in conn.execute("SELECT * FROM experiences WHERE scope = 'global' ORDER BY hard_boundary DESC, confidence DESC, updated_at DESC").fetchall():
        refs = json.loads(r["source_refs"] or "[]")
        item = {
            "experience_id": r["experience_id"],
            "scope": "global",
            "project_id": GLOBAL_PROJECT_ID,
            "title": r["title"],
            "summary": r["summary"],
            "conditions": json.loads(r["conditions"] or "[]"),
            "recommendations": json.loads(r["recommendations"] or "[]"),
            "anti_patterns": json.loads(r["anti_patterns"] or "[]"),
            "confidence": float(r["confidence"]),
            "status": r["status"],
            "support_count": int(r["support_count"]),
            "contradiction_count": int(r["contradiction_count"]),
            "project_count": int(r["project_count"]),
            "source_refs": refs,
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "valid_from": r["valid_from"],
            "invalid_at": r["invalid_at"],
            "superseded_by": r["superseded_by"],
            "hard_boundary": bool(r["hard_boundary"]),
            "pyramid_level": r["pyramid_level"],
            "trust_tier": r["trust_tier"],
        }
        global_exps.append(item)
        if not any(isinstance(x, str) and x.startswith(("01_", "02_", "03_", "04_")) for x in refs):
            daytime_global_exps.append(item)

    _write_json_with_retry(
        inbox_dir / "delta_local_ops.json",
        {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "host": "windows_host" if os.name == "nt" else "posix_host",
            "memories": daytime_global_mems,
            "experiences": daytime_global_exps,
            "facts": proj_facts.get(GLOBAL_PROJECT_ID, []),
        },
    )

    _write_json_with_retry(
        sync_dir / "L1_Global_Memory.json",
        {
            "schema_version": "2.0-gdrive-native",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "global_memories": global_mems,
            "global_experiences": global_exps,
            "global_facts": proj_facts.get(GLOBAL_PROJECT_ID, []),
        },
    )

    # 3. Synchronize L2_Experiences.json so invalidated/superseded/deleted global experiences never resurrect
    l2_exp_file = sync_dir / "L2_Experiences.json"
    if l2_exp_file.is_file() or global_exps:
        existing_l2 = _read_json_with_retry(l2_exp_file) or {}
        existing_l2_map: dict[str, dict[str, Any]] = {}
        if isinstance(existing_l2, dict):
            for old_exp in existing_l2.get("experiences", []):
                if isinstance(old_exp, dict) and old_exp.get("experience_id"):
                    existing_l2_map[str(old_exp["experience_id"])] = old_exp
        merged_l2_exps: list[dict[str, Any]] = []
        for gexp in global_exps:
            eid = str(gexp["experience_id"])
            prev = existing_l2_map.get(eid, {})
            merged_l2_exps.append({**prev, **gexp})
        if l2_exp_file.is_file() or merged_l2_exps:
            l2_header = {
                k: v
                for k, v in (existing_l2.items() if isinstance(existing_l2, dict) else [])
                if k not in {"experiences", "updated_at"}
            }
            if "schema_version" not in l2_header:
                l2_header["schema_version"] = "2.0"
            _write_json_with_retry(
                l2_exp_file,
                {
                    **l2_header,
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "experiences": merged_l2_exps,
                },
            )

    # 4. Synchronize L1_Modalities/*.json so invalidated/superseded/updated modality rules & memories persist
    modalities_dir = sync_dir / "L1_Modalities"
    mod_rows_by_id: dict[str, dict[str, dict[str, Any]]] = {}
    for r in conn.execute("SELECT * FROM memories WHERE scope = 'modality' ORDER BY hard_boundary DESC, created_at DESC").fetchall():
        mid = str(r["project_id"] or "modality")
        item_id = str(r["id"])
        mod_rows_by_id.setdefault(mid, {})[item_id] = {
            "memory_id": item_id,
            "modality_id": mid,
            "scope": "modality",
            "hard_boundary": bool(r["hard_boundary"]),
            "pyramid_level": r["pyramid_level"],
            "trust_tier": r["trust_tier"],
            "kind": r["kind"],
            "summary": r["summary"],
            "source_task_id": r["source_task_id"],
            "created_at": r["created_at"],
            "valid_from": r["valid_from"],
            "invalid_at": r["invalid_at"],
            "superseded_by": r["superseded_by"],
            "source_doc": r["source_doc"],
        }

    existing_mod_files: dict[str, Path] = {}
    if modalities_dir.is_dir():
        for mf in sorted(modalities_dir.glob("*.json")):
            mdata = _read_json_with_retry(mf)
            if isinstance(mdata, dict):
                mid = str(mdata.get("modality_id") or mf.stem)
                existing_mod_files[mid] = mf

    for mid in sorted(set(existing_mod_files) | set(mod_rows_by_id)):
        safe_mid = re.sub(r"[^A-Za-z0-9_.-]+", "_", mid)
        mfile = existing_mod_files.get(mid) or (modalities_dir / f"{safe_mid}.json")
        existing_mod = _read_json_with_retry(mfile) or {"modality_id": mid}
        db_map = dict(mod_rows_by_id.get(mid, {}))
        seen_ids: set[str] = set()

        new_craft_rules: list[dict[str, Any]] = []
        for cr in existing_mod.get("craft_rules", []):
            if not isinstance(cr, dict):
                continue
            cid = str(cr.get("rule_id") or cr.get("memory_id") or cr.get("id") or "")
            if not cid or cid not in db_map:
                continue
            seen_ids.add(cid)
            db_item = db_map[cid]
            clean_summary = str(db_item["summary"])
            title = cr.get("title")
            if title and clean_summary.startswith(f"[{title}] "):
                clean_summary = clean_summary[len(f"[{title}] "):]
            updated_cr = {
                **cr,
                "summary": clean_summary,
                "hard_boundary": db_item["hard_boundary"],
                "valid_from": db_item["valid_from"],
                "invalid_at": db_item["invalid_at"],
                "superseded_by": db_item["superseded_by"],
            }
            new_craft_rules.append(updated_cr)

        new_mod_memories: list[dict[str, Any]] = []
        for mm in existing_mod.get("memories", []):
            if not isinstance(mm, dict):
                continue
            cid = str(mm.get("memory_id") or mm.get("rule_id") or mm.get("id") or "")
            if not cid or cid not in db_map or cid in seen_ids:
                continue
            seen_ids.add(cid)
            db_item = db_map[cid]
            new_mod_memories.append({
                **mm,
                "memory_id": cid,
                "modality_id": mid,
                "scope": "modality",
                "kind": db_item["kind"],
                "summary": db_item["summary"],
                "hard_boundary": db_item["hard_boundary"],
                "valid_from": db_item["valid_from"],
                "invalid_at": db_item["invalid_at"],
                "superseded_by": db_item["superseded_by"],
            })

        for cid, db_item in db_map.items():
            if cid not in seen_ids:
                new_mod_memories.append(db_item)

        mod_header = {
            k: v
            for k, v in existing_mod.items()
            if k not in {"modality_id", "updated_at", "craft_rules", "memories"}
        }
        modalities_dir.mkdir(parents=True, exist_ok=True)
        _write_json_with_retry(
            mfile,
            {
                "modality_id": mid,
                **mod_header,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "craft_rules": new_craft_rules,
                "memories": new_mod_memories,
            },
        )


def _persist_to_local_disk_and_mirror(conn: sqlite3.Connection, sync_dir: Path, push_cloud_rclone: bool = False) -> None:
    """Dual-Persistence: Mirror Google Drive JSON/MD files to local disk AND persist into local disk runtime.sqlite3."""
    import shutil
    import subprocess

    # 1. Mirror JSON/MD files between Cloud Drive dir and Local Physical Disk mirror dir

    mirror_dir = resolve_local_mirror_memory_dir()
    if mirror_dir and mirror_dir.resolve() != sync_dir.resolve():
        try:
            for item in ("00_Project_Registry.json", "L3_Core_Rules.md", "L2_Domain_Index.md", "L2_Experiences.json", "L1_Global_Memory.json", "distilled_memory_bundle.json", "00_Global_and_Project_Memory_Atlas.md"):
                src_f = sync_dir / item
                if src_f.is_file():
                    shutil.copy2(src_f, mirror_dir / item)
            for sub in ("L1_Projects", "L1_Modalities", "Inbox_Traces", "Nightly_Dream_Reports"):
                src_sub = sync_dir / sub
                if src_sub.is_dir():
                    shutil.copytree(src_sub, mirror_dir / sub, dirs_exist_ok=True)
        except OSError:
            pass

    # 2. Persist memories, facts, and experiences into local disk SQLite (`runtime.sqlite3`)
    mem_rows = conn.execute(
        """SELECT id, project_id, kind, summary, source_task_id, created_at, scope,
                  hard_boundary, valid_from, invalid_at, superseded_by, source_doc
           FROM memories"""
    ).fetchall()
    fact_rows = conn.execute(
        """SELECT project_id, fact_key, value, source_path, source_sha256, evidence, source_revision, updated_at
           FROM facts"""
    ).fetchall()
    exp_rows = conn.execute(
        """SELECT experience_id, scope, project_id, title, summary, conditions, recommendations,
                  anti_patterns, confidence, status, support_count, contradiction_count,
                  project_count, source_refs, created_at, updated_at, valid_from, invalid_at,
                  superseded_by, hard_boundary
           FROM experiences"""
    ).fetchall()

    for db_path in resolve_local_sqlite_paths():
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(db_path, timeout=5.0)) as ldb, ldb:
                ldb.executescript("""
                    CREATE TABLE IF NOT EXISTS facts (
                        project_id TEXT NOT NULL,
                        fact_key TEXT NOT NULL,
                        value TEXT NOT NULL,
                        source_path TEXT NOT NULL,
                        source_sha256 TEXT NOT NULL,
                        evidence TEXT NOT NULL,
                        source_revision TEXT,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (project_id, fact_key)
                    );
                    CREATE TABLE IF NOT EXISTS memories (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        source_task_id TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        scope TEXT NOT NULL DEFAULT 'project',
                        hard_boundary INTEGER NOT NULL DEFAULT 0,
                        valid_from REAL,
                        invalid_at REAL,
                        superseded_by TEXT,
                        source_doc TEXT
                    );
                    CREATE TABLE IF NOT EXISTS experiences (
                        experience_id TEXT PRIMARY KEY,
                        scope TEXT NOT NULL,
                        project_id TEXT,
                        title TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        conditions TEXT NOT NULL,
                        recommendations TEXT NOT NULL,
                        anti_patterns TEXT NOT NULL,
                        confidence REAL NOT NULL,
                        status TEXT NOT NULL,
                        support_count INTEGER NOT NULL DEFAULT 1,
                        contradiction_count INTEGER NOT NULL DEFAULT 0,
                        project_count INTEGER NOT NULL DEFAULT 1,
                        source_refs TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        valid_from REAL,
                        invalid_at REAL,
                        superseded_by TEXT,
                        hard_boundary INTEGER NOT NULL DEFAULT 0
                    );
                """)
                cols_m = {r[1] for r in ldb.execute("PRAGMA table_info(memories)").fetchall()}
                for cname, cdef in [("scope", "TEXT NOT NULL DEFAULT 'project'"), ("hard_boundary", "INTEGER NOT NULL DEFAULT 0"), ("valid_from", "REAL"), ("invalid_at", "REAL"), ("superseded_by", "TEXT"), ("source_doc", "TEXT")]:
                    if cname not in cols_m:
                        ldb.execute(f"ALTER TABLE memories ADD COLUMN {cname} {cdef}")
                cols_e = {r[1] for r in ldb.execute("PRAGMA table_info(experiences)").fetchall()}
                for cname, cdef in [("valid_from", "REAL"), ("invalid_at", "REAL"), ("superseded_by", "TEXT"), ("hard_boundary", "INTEGER NOT NULL DEFAULT 0")]:
                    if cname not in cols_e:
                        ldb.execute(f"ALTER TABLE experiences ADD COLUMN {cname} {cdef}")

                ldb.executemany(
                    """INSERT OR REPLACE INTO memories
                       (id, project_id, kind, summary, source_task_id, created_at, scope,
                        hard_boundary, valid_from, invalid_at, superseded_by, source_doc)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [tuple(r) for r in mem_rows],
                )
                ldb.executemany(
                    """INSERT OR REPLACE INTO facts
                       (project_id, fact_key, value, source_path, source_sha256, evidence, source_revision, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    [tuple(r) for r in fact_rows],
                )
                ldb.executemany(
                    """INSERT OR REPLACE INTO experiences
                       (experience_id, scope, project_id, title, summary, conditions, recommendations,
                        anti_patterns, confidence, status, support_count, contradiction_count,
                        project_count, source_refs, created_at, updated_at, valid_from, invalid_at,
                        superseded_by, hard_boundary)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [tuple(r) for r in exp_rows],
                )
                ldb.commit()
        except Exception:
            continue

    rclone_target = os.environ.get("AGY_RCLONE_REMOTE", "").strip()
    if push_cloud_rclone and rclone_target and os.name != "nt":
        try:
            subprocess.Popen(
                ["/usr/bin/rclone", "copy", str(sync_dir), rclone_target, "-u", "-q"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


@contextmanager
def gdrive_memory_db() -> Iterator[sqlite3.Connection]:
    """Context manager providing Dual-Persistence: Google Drive JSON/MD + Local Disk SQLite/Mirror + In-Memory FTS5."""
    global _MEM_CONN, _LAST_DISK_SIGNATURE, _ACTIVE_SYNC_DIR
    sync_dir = resolve_gdrive_memory_dir()
    resolved_sync = sync_dir.resolve()
    _bootstrap_gdrive_from_legacy_if_needed(sync_dir)

    with _LOCK:
        if _MEM_CONN is not None and _ACTIVE_SYNC_DIR != resolved_sync:
            try:
                _MEM_CONN.close()
            except sqlite3.Error:
                pass
            _MEM_CONN = None
            _LAST_DISK_SIGNATURE = None
        _ACTIVE_SYNC_DIR = resolved_sync
        if _MEM_CONN is None:
            _MEM_CONN = sqlite3.connect(":memory:", check_same_thread=False)
            _MEM_CONN.row_factory = sqlite3.Row
            _init_in_memory_schema(_MEM_CONN)

        current_sig = _compute_disk_signature(sync_dir)
        if _LAST_DISK_SIGNATURE != current_sig:
            try:
                _reload_memory_db_from_gdrive(_MEM_CONN, sync_dir)
                _persist_to_local_disk_and_mirror(_MEM_CONN, sync_dir, push_cloud_rclone=False)
                _LAST_DISK_SIGNATURE = current_sig
            except Exception:
                pass

        before_changes = _MEM_CONN.total_changes
        try:
            yield _MEM_CONN
            _MEM_CONN.commit()
        finally:
            if _MEM_CONN.total_changes > before_changes:
                try:
                    _flush_in_memory_db_to_gdrive(_MEM_CONN, sync_dir)
                    _persist_to_local_disk_and_mirror(_MEM_CONN, sync_dir, push_cloud_rclone=True)
                    _LAST_DISK_SIGNATURE = _compute_disk_signature(sync_dir)
                except Exception:
                    pass


def read_pyramid_layer_markdown(layer: str = "L3") -> str:
    """Read L3_Core_Rules.md or L2_Domain_Index.md directly from Google Drive."""
    sync_dir = resolve_gdrive_memory_dir()
    fname = "L3_Core_Rules.md" if layer.upper() == "L3" else "L2_Domain_Index.md"
    target = sync_dir / fname
    try:
        if target.is_file():
            return target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return ""
