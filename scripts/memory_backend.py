"""Managed historical memory with an optional Hindsight HTTP backend.

Task state and project facts remain in runtime_state.  This module only owns
the historical-memory boundary and never treats recalled text as instructions.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sqlite3
import sys as _sys
import time
from abc import ABC, abstractmethod
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from policy_state import data_dir, record_event
from runtime_state import add_memory, get_task, list_memories


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PLUGIN_ROOT / "memory" / "default.json"
KINDS = frozenset({"decision", "failed_attempt", "finding", "lesson", "task_summary"})
METADATA_KEYS = frozenset({
    "project_id", "task_id", "source_task_id", "session_id", "role", "kind",
    "task_type", "git_revision", "source", "step_id", "failure_kind",
    "changed_paths", "agent_model", "reasoning_effort", "risk",
    "architecture_change", "migration_source", "legacy_memory_id", "created_at",
    "scope", "hard_boundary", "source_doc", "supersede_id",
})
SENSITIVE_NAME = re.compile(r"^(?:\.env(?:\..*)?|credentials?(?:\..*)?|secrets?(?:\..*)?|id_(?:rsa|ed25519)|.*(?:password|token|private[_-]?key).*|.+\.(?:pem|p12|key))$", re.I)
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:api[_-]?key|access[_-]?token|auth(?:orization)?|password|passwd|secret|credential|private[_-]?key)\s*[:=]\s*['\"]?[^\s,'\";]{6,}", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{8,}", re.I),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|hsk)_[A-Za-z0-9_]{16,}\b", re.I),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b", re.I),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?:https?|ssh)://[^/\s@]+:[^/\s@]+@", re.I),
)


_prev_mb = _sys.modules.get(__name__)
if _prev_mb is not None and hasattr(_prev_mb, "MemoryBackendError"):
    MemoryBackendError = _prev_mb.MemoryBackendError
else:
    class MemoryBackendError(Exception):
        """A classified backend failure whose message is safe to return."""

        def __init__(self, reason: str):
            super().__init__(reason)
            self.reason = reason


class MemoryBackend(ABC):
    @abstractmethod
    def health(self) -> dict[str, Any]: ...

    @abstractmethod
    def retain(self, *, bank_id: str, content: str, kind: str,
               metadata: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def recall(self, *, bank_id: str, query: str, kinds: list[str] | None,
               limit: int, max_chars: int) -> dict[str, Any]: ...

    @abstractmethod
    def reflect(self, *, bank_id: str, query: str,
                max_chars: int) -> dict[str, Any]: ...


def _has_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in SECRET_PATTERNS)


def _sensitive_path(value: str) -> bool:
    return any(SENSITIVE_NAME.fullmatch(part) for part in value.replace("\\", "/").split("/") if part)


def _safe_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    result: dict[str, Any] = {}
    for key, value in metadata.items():
        if key not in METADATA_KEYS or value is None:
            continue
        if key == "changed_paths":
            if not isinstance(value, list):
                raise ValueError("changed_paths must be a list")
            paths = []
            for path in value[:30]:
                if not isinstance(path, str) or len(path) > 300 or Path(path).is_absolute() or _sensitive_path(path) or _has_secret(path):
                    raise ValueError("sensitive metadata")
                paths.append(path)
            result[key] = paths
        elif key in {"architecture_change", "hard_boundary"}:
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be boolean")
            result[key] = value
        elif isinstance(value, str) and len(value) <= 300 and not _has_secret(value) and not _sensitive_path(value):
            result[key] = value
        else:
            raise ValueError("sensitive or oversized metadata")
    return result


@lru_cache(maxsize=8)
def _read_config(path: str, modified_ns: int) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("invalid memory configuration")
    if data.get("backend") not in {"sqlite", "hindsight", "auto"}:
        raise ValueError("invalid memory backend")
    for section in ("hindsight", "bank", "recall", "retain", "reflect", "fallback"):
        if not isinstance(data.get(section), dict):
            raise ValueError("invalid memory configuration")
    bank = data["bank"]
    if bank.get("strategy") != "git_repository" or not isinstance(bank.get("prefix"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", bank["prefix"]):
        raise ValueError("invalid memory bank strategy")
    if data["fallback"].get("backend") != "sqlite":
        raise ValueError("invalid memory fallback")
    if data["reflect"].get("mode") != "on_demand":
        raise ValueError("invalid reflect mode")
    data["hindsight"].setdefault("local_only", False)
    for section, names in (("hindsight", ("connect_timeout_ms", "recall_timeout_ms", "retain_timeout_ms", "reflect_timeout_ms")),
                           ("recall", ("default_limit", "max_limit", "max_chars")),
                           ("retain", ("max_chars",)), ("reflect", ("max_chars",))):
        for name in names:
            value = data[section].get(name)
            if type(value) is not int or value <= 0:
                raise ValueError("invalid memory configuration limit")
    for section, names in (("hindsight", ("local_only",)),
                           ("recall", ("enabled", "automatic")),
                           ("retain", ("enabled", "automatic_task_summary", "store_raw_transcript", "store_tool_output")),
                           ("reflect", ("enabled",)), ("fallback", ("enabled",))):
        for name in names:
            if type(data[section].get(name)) is not bool:
                raise ValueError("invalid memory configuration flag")
    if data["retain"]["store_raw_transcript"] or data["retain"]["store_tool_output"]:
        raise ValueError("raw transcript and tool output storage is unsupported")
    return data


def load_memory_config(path: str | Path | None = None) -> dict[str, Any]:
    """Read configuration once per path; an API URL environment override is explicit opt-in."""
    source = Path(path or os.environ.get("CODEX_AGENT_POLICY_MEMORY_CONFIG") or DEFAULT_CONFIG).resolve()
    config = json.loads(json.dumps(_read_config(str(source), source.stat().st_mtime_ns)))
    configured_url = os.environ.get("HINDSIGHT_API_URL")
    if configured_url:
        config["hindsight"]["api_url"] = configured_url
    return config


def _git_head_revision(root: Path) -> str:
    """Resolve HEAD commit SHA in pure Python without spawning git.exe."""
    from runtime_state import canonical_project_root
    try:
        canon = canonical_project_root(root)
        dot_git = canon / ".git"
        git_dir = dot_git if dot_git.is_dir() else None
        if git_dir is None:
            return ""
        head_file = git_dir / "HEAD"
        if not head_file.is_file():
            return ""
        head_val = head_file.read_text(encoding="utf-8", errors="ignore").strip()
        if re.fullmatch(r"[0-9a-fA-F]{40}", head_val):
            return head_val.lower()
        if head_val.startswith("ref:"):
            ref_rel = head_val.split(":", 1)[1].strip()
            ref_path = git_dir / ref_rel
            if ref_path.is_file():
                sha = ref_path.read_text(encoding="utf-8", errors="ignore").strip()
                if re.fullmatch(r"[0-9a-fA-F]{40}", sha):
                    return sha.lower()
            packed = git_dir / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.startswith("#") or line.startswith("^") or " " not in line:
                        continue
                    sha, rname = line.strip().split(" ", 1)
                    if rname.strip() == ref_rel and re.fullmatch(r"[0-9a-fA-F]{40}", sha):
                        return sha.lower()
    except Exception:
        pass
    return ""


def _git_pure_python_identity(root: Path) -> str | None:
    """Resolve Git repository identity in pure Python without spawning git.exe child processes."""
    from runtime_state import canonical_project_root
    try:
        canon = canonical_project_root(root)
        dot_git = canon / ".git"
        git_dir = dot_git if dot_git.is_dir() else None
        if git_dir is None:
            return None
        cfg = git_dir / "config"
        if cfg.is_file():
            raw = cfg.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r'\[remote\s+"origin"\][^\[]*?url\s*=\s*(\S+)', raw, re.I | re.S)
            if m:
                ident = _canonical_remote(m.group(1).strip())
                if ident:
                    return ident
        common_identity = str(git_dir.resolve())
        repo_name = canon.name
        if os.name == "nt":
            common_identity = common_identity.casefold()
            repo_name = repo_name.casefold()
        return f"local:{common_identity}:{repo_name}"
    except Exception:
        return None


def _canonical_remote(remote: str) -> str | None:
    # Never include credentials, query strings, or local absolute paths.
    if "://" in remote:
        parsed = urlsplit(remote)
        if parsed.scheme not in {"https", "http", "ssh", "git"} or not parsed.hostname:
            return None
        host, path = parsed.hostname.lower(), parsed.path
    elif re.match(r"^(?:[^@/:]+@)?[^/:]+:[^:]+$", remote):
        host_part, path = remote.split(":", 1)
        host = host_part.rsplit("@", 1)[-1].lower()
    else:
        return None
    path = path.strip("/").removesuffix(".git")
    if not path or ".." in path.split("/"):
        return None
    return f"{host}/{path}"


def resolve_memory_bank(project_root: str | Path | None, project_id: str) -> str:
    """Map every workspace folder via 00_Project_Registry.json to a unified project bank (registered project_id or workbench_misc)."""
    config = load_memory_config()
    prefix = config["bank"].get("prefix", "codex-agent-policy")
    if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", prefix):
        raise ValueError("invalid memory bank prefix")
    try:
        from gdrive_memory_engine import normalize_project_id, resolve_project_id_via_registry
        if project_root is not None:
            mapped = resolve_project_id_via_registry(project_root)
            if mapped and mapped != "workbench_misc":
                return f"{prefix}::project::{mapped}"
            root = Path(project_root).resolve()
            identity = _git_pure_python_identity(root)
            if identity:
                return f"{prefix}::repo::{sha256(identity.encode('utf-8')).hexdigest()}"
        norm_pid = normalize_project_id(project_id)
        if norm_pid and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", norm_pid):
            return f"{prefix}::project::{norm_pid}"
    except Exception:
        pass
    if project_root is not None:
        root = Path(project_root).resolve()
        identity = _git_pure_python_identity(root)
        if identity:
            return f"{prefix}::repo::{sha256(identity.encode('utf-8')).hexdigest()}"
    if not isinstance(project_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", project_id):
        raise ValueError("invalid project_id")
    return f"{prefix}::project::{project_id}"


def _tokenize_query(text: str) -> set[str]:
    """Extract ASCII words plus Chinese character 2-grams for accurate bilingual recall."""
    lowered = (text or "").casefold()
    tokens = set(re.findall(r"[a-z0-9_.-]{2,}", lowered))
    for chunk in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(chunk) == 1:
            tokens.add(chunk)
        else:
            for idx in range(len(chunk) - 1):
                tokens.add(chunk[idx:idx + 2])
    return tokens


class SQLiteMemoryBackend(MemoryBackend):
    """Dual-layer (Global + Project) SQLite historical memory backend."""

    def __init__(self, task_id: str):
        self.task_id = task_id

    def health(self) -> dict[str, Any]:
        from gdrive_memory_engine import resolve_gdrive_memory_dir
        return {
            "status": "ok",
            "available": True,
            "dual_layer": True,
            "storage_engine": "gdrive_json_md + sqlite_in_memory_fts5",
            "gdrive_sync_dir": str(resolve_gdrive_memory_dir()),
        }

    def retain(self, *, bank_id: str, content: str, kind: str,
               metadata: dict[str, Any]) -> dict[str, Any]:
        scope = str(metadata.get("scope") or "project")
        if scope not in {"project", "global"}:
            scope = "project"
        hard_boundary = bool(metadata.get("hard_boundary", False))
        source_doc = metadata.get("source_doc")
        supersede_id = metadata.get("supersede_id")
        result = add_memory(
            self.task_id,
            kind,
            content,
            scope=scope,
            hard_boundary=hard_boundary,
            source_doc=source_doc,
            supersede_id=supersede_id,
        )
        return {"status": "ok", "backend": "sqlite", "bank_id": bank_id, **result,
                "deduplicated": bool(result.get("deduplicated", False))}

    def recall(self, *, bank_id: str, query: str, kinds: list[str] | None,
               limit: int, max_chars: int, scope: str = "project+global") -> dict[str, Any]:
        rows = list_memories(self.task_id, max(40, limit * 8), scope=scope, query=query)
        if kinds:
            rows = [row for row in rows if row["kind"] in kinds]
        q_tokens = _tokenize_query(query)

        def _score(row: dict[str, Any]) -> tuple[int, int, int, int]:
            s_tokens = _tokenize_query(row["summary"] + " " + str(row.get("source_doc") or ""))
            overlap = len(q_tokens & s_tokens) if q_tokens else 0
            is_hb = 1 if (row.get("scope") == "global" and row.get("hard_boundary")) else 0
            t_tier = str(row.get("trust_tier") or "TIER_2_RUNTIME_VERIFIED")
            tier_rank = 3 if t_tier == "TIER_1_SYSTEM_HARD" else (2 if t_tier == "TIER_2_RUNTIME_VERIFIED" else 1)
            layer_boost = 2 if is_hb else (1 if row.get("scope") == "project" else 0)
            return (1 if (is_hb and overlap > 0) else 0, overlap, tier_rank, layer_boost)

        rows.sort(key=_score, reverse=True)
        selected_rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        if scope in {"global", "project+global"}:
            hb_rows = [r for r in rows if r.get("scope") == "global" and r.get("hard_boundary")]
            for r in hb_rows[:max(1, min(2, limit // 2))]:
                selected_rows.append(r)
                seen_ids.add(r["memory_id"])
        for r in rows:
            if len(selected_rows) >= limit:
                break
            if r["memory_id"] not in seen_ids:
                selected_rows.append(r)
                seen_ids.add(r["memory_id"])

        results = []
        remaining = max_chars
        truncated = len(rows) > len(selected_rows)
        for row in selected_rows:
            if len(results) >= limit or remaining <= 0:
                break
            body = row["summary"]
            if _has_secret(body):
                continue
            clipped = body[:remaining]
            truncated |= len(clipped) < len(body)
            remaining -= len(clipped)
            r_scope = row.get("scope", "project")
            r_hb = bool(row.get("hard_boundary", False))
            r_prec = row.get("precedence", "PROJECT_LOCAL_SHADOW" if r_scope == "project" else ("MODALITY_CRAFT_STANDARD" if r_scope == "modality" else "GLOBAL_DEFAULT"))
            p_level = row.get("pyramid_level", "L1_ATOMIC_LESSON")
            t_tier = row.get("trust_tier", "TIER_2_RUNTIME_VERIFIED")
            if r_hb and r_scope == "global":
                label = "GLOBAL_HARD_BOUNDARY"
            elif r_scope == "modality":
                label = "MODALITY_CRAFT_MEMORY"
            elif r_scope == "global":
                label = "GLOBAL_HISTORICAL_MEMORY"
            else:
                label = "PROJECT_HISTORICAL_MEMORY"
            results.append({
                "memory_id": row["memory_id"],
                "text": clipped,
                "kind": row["kind"],
                "scope": r_scope,
                "layer": "GLOBAL_MEMORY" if r_scope == "global" else ("MODALITY_MEMORY" if r_scope == "modality" else "PROJECT_MEMORY"),
                "pyramid_level": p_level,
                "trust_tier": t_tier,
                "hard_boundary": r_hb,
                "precedence": r_prec,
                "score": len(q_tokens & _tokenize_query(body)) if q_tokens else None,
                "created_at": row.get("valid_from"),
                "source_task_id": row.get("source_task_id"),
                "source_doc": row.get("source_doc"),
                "metadata": {
                    "kind": row["kind"],
                    "scope": r_scope,
                    "pyramid_level": p_level,
                    "trust_tier": t_tier,
                    "hard_boundary": r_hb,
                    "precedence": r_prec,
                    "source_task_id": row.get("source_task_id"),
                    "source_doc": row.get("source_doc"),
                },
                "label": label,
            })
        return {"status": "ok", "backend": "sqlite", "bank_id": bank_id,
                "scope": scope, "query": query, "results": results, "truncated": truncated}

    def reflect(self, *, bank_id: str, query: str,
                max_chars: int) -> dict[str, Any]:
        recalled = self.recall(bank_id=bank_id, query=query, kinds=None, limit=8, max_chars=max_chars, scope="project+global")
        items = recalled.get("results", [])
        if not items:
            return {"status": "ok", "backend": "sqlite", "bank_id": bank_id, "text": "No matching Global, Modality, or Project memories found."}
        hb_lines = [f"- [{it['kind']}] {it['text']}" for it in items if it.get("hard_boundary") and it.get("scope") == "global"]
        proj_lines = [f"- [{it['kind']}] {it['text']}" for it in items if it.get("scope") == "project" and not it.get("hard_boundary")]
        mod_lines = [f"- [{it['kind']}] {it['text']}" for it in items if it.get("scope") == "modality"]
        glob_lines = [f"- [{it['kind']}] {it['text']}" for it in items if it.get("scope") == "global" and not it.get("hard_boundary")]
        sections = []
        if hb_lines:
            sections.append("### 1. 全局红线与宿主硬边界 (GLOBAL_HARD_OVERRIDE)\n" + "\n".join(hb_lines))
        if proj_lines:
            sections.append("### 2. 当前项目专属记忆 (PROJECT_LOCAL_SHADOW)\n" + "\n".join(proj_lines))
        if mod_lines:
            sections.append("### 3. 横向交付工艺规范 (MODALITY_CRAFT_STANDARD)\n" + "\n".join(mod_lines))
        if glob_lines:
            sections.append("### 4. 跨项目全局通用记忆 (GLOBAL_DEFAULT)\n" + "\n".join(glob_lines))
        text = "\n\n".join(sections)[:max_chars]
        return {"status": "ok", "backend": "sqlite", "bank_id": bank_id, "text": text}


def _endpoint_config(config: dict[str, Any]) -> tuple[str | None, str | None]:
    settings = config["hindsight"]
    url = settings.get("api_url")
    if not url:
        return None, None
    if not isinstance(url, str):
        raise MemoryBackendError("not_configured")
    try:
        parsed = urlsplit(url)
        port = parsed.port  # Also rejects non-numeric and out-of-range ports.
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                 and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment
                 and (port is None or 1 <= port <= 65535))
    except ValueError:
        valid = False
    if not valid:
        raise MemoryBackendError("not_configured")
    try:
        local = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        local = parsed.hostname.casefold() == "localhost"
    if settings.get("local_only", False) and not local:
        raise MemoryBackendError("not_configured")
    key_name = settings.get("api_key_env", "HINDSIGHT_API_KEY")
    if not isinstance(key_name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", key_name):
        raise MemoryBackendError("not_configured")
    token = os.environ.get(key_name) or (os.environ.get("HINDSIGHT_API_TOKEN") if key_name == "HINDSIGHT_API_KEY" else None)
    if token and (not token.isascii() or any(ord(char) < 33 or ord(char) > 126 for char in token)):
        raise MemoryBackendError("auth_error")
    if token and parsed.scheme == "http" and not local:
        raise MemoryBackendError("not_configured")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port_part = "" if port is None or (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443) else f":{port}"
    canonical_url = f"{parsed.scheme.lower()}://{host}{port_part}{parsed.path.rstrip('/')}"
    return canonical_url, token


def hindsight_endpoint_id(config: dict[str, Any]) -> str:
    """Opaque, stable identity for the configured Hindsight endpoint and account."""
    url, token = _endpoint_config(config)
    if not url:
        raise MemoryBackendError("not_configured")
    return sha256(f"{url}\0{token or ''}".encode("utf-8")).hexdigest()


def _event(event_kind: str, task_id: str | None, project_id: str | None,
           backend: str, status: str, started: float, **safe: Any) -> None:
    try:
        record_event(event_kind, {"task_id": task_id, "project_id": project_id,
                            "backend": backend, "status": status,
                            "latency_ms": int((time.monotonic() - started) * 1000), **safe})
    except OSError:
        pass


def _hindsight(config: dict[str, Any]) -> MemoryBackend:
    url, token = _endpoint_config(config)
    if not url:
        raise MemoryBackendError("not_configured")
    from hindsight_memory import HindsightMemoryBackend
    return HindsightMemoryBackend(url, token, config["hindsight"])


def _selection(task_id: str | None, config: dict[str, Any], *,
               force_hindsight: bool = False) -> tuple[MemoryBackend | None, dict[str, Any], dict[str, Any] | None]:
    task = get_task(task_id) if task_id else None
    bank_id = resolve_memory_bank(task["project_root"], task["project_id"]) if task else None
    mode = "hindsight" if force_hindsight else config["backend"]
    fallback_enabled = bool(config["fallback"].get("enabled", True)) and not force_hindsight
    base = {"bank_id": bank_id, "bank_strategy": config["bank"].get("strategy", "git_repository"),
            "configured": False, "available": False,
            "fallback": "sqlite" if fallback_enabled else None}
    if mode == "sqlite":
        return (SQLiteMemoryBackend(task_id) if task_id else None,
                {**base, "status": "ok", "backend": "sqlite", "available": True}, task)
    try:
        backend = _hindsight(config)
        health = backend.health()
        if not health.get("available"):
            raise MemoryBackendError(health.get("reason", "unavailable"))
        return backend, {**base, "status": "ok", "backend": "hindsight",
                         "configured": True, "available": True}, task
    except MemoryBackendError as exc:
        configured = bool(config["hindsight"].get("api_url"))
        if fallback_enabled:
            return (SQLiteMemoryBackend(task_id) if task_id else None,
                    {**base, "status": "fallback", "backend": "sqlite",
                     "configured": configured, "available": True, "reason": exc.reason}, task)
        return None, {**base, "status": "unavailable", "backend": "hindsight",
                      "configured": configured, "reason": exc.reason}, task


def memory_status(task_id: str | None = None) -> dict[str, Any]:
    started = time.monotonic()
    try:
        config = load_memory_config()
        _, status, task = _selection(task_id, config)
        status["recall_enabled"] = bool(config["recall"].get("enabled", True))
        status["reflect_mode"] = config["reflect"].get("mode", "on_demand")
        if status["backend"] == "hindsight" and _duplicate_hook_warning():
            status["backend_status"] = status["status"]
            status["status"] = "warning"
            status["reason"] = "duplicate_memory_pipeline"
        _event("memory_backend_ready", task_id, task["project_id"] if task else None,
               status["backend"], status["status"], started,
               error_type=status.get("reason"))
        return status
    except (ValueError, OSError, json.JSONDecodeError):
        return {"status": "unavailable", "backend": "none", "configured": False,
                "available": False, "reason": "bad_response", "fallback": None}


def _fingerprint(bank_id: str, kind: str, summary: str) -> str:
    normalized = " ".join(summary.split())
    return sha256(f"{bank_id}\0{kind}\0{normalized}".encode("utf-8")).hexdigest()


def _claim(bank_id: str, fingerprint: str) -> str:
    """Atomically reserve a fingerprint across child agents and MCP processes."""
    db = sqlite3.connect(data_dir() / "runtime.sqlite3", timeout=5)
    try:
        db.execute("PRAGMA busy_timeout = 5000")
        db.execute("CREATE TABLE IF NOT EXISTS memory_fingerprints (bank_id TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(bank_id, fingerprint))")
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT state, updated_at FROM memory_fingerprints WHERE bank_id = ? AND fingerprint = ?", (bank_id, fingerprint)).fetchone()
        if row and row[0] == "done":
            db.commit()
            return "duplicate"
        if row and time.time() - row[1] < 45:
            db.commit()
            return "in_progress"
        db.execute("INSERT OR REPLACE INTO memory_fingerprints VALUES (?, ?, 'pending', ?)",
                   (bank_id, fingerprint, time.time()))
        db.commit()
        return "claimed"
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _finish_claim(bank_id: str, fingerprint: str, success: bool) -> None:
    db = sqlite3.connect(data_dir() / "runtime.sqlite3", timeout=5)
    try:
        if success:
            db.execute("UPDATE memory_fingerprints SET state = 'done', updated_at = ? WHERE bank_id = ? AND fingerprint = ?",
                       (time.time(), bank_id, fingerprint))
        else:
            db.execute("DELETE FROM memory_fingerprints WHERE bank_id = ? AND fingerprint = ? AND state = 'pending'",
                       (bank_id, fingerprint))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _fallback(task_id: str, config: dict[str, Any], *, force_hindsight: bool) -> SQLiteMemoryBackend | None:
    if force_hindsight or not config["fallback"].get("enabled", True):
        return None
    return SQLiteMemoryBackend(task_id)


def _duplicate_hook_warning() -> bool:
    """Use explicit opt-in or path presence only; never inspect private hook configs."""
    if os.environ.get("HINDSIGHT_CODEX_HOOKS_ENABLED") == "1":
        return True
    return ((Path.home() / ".hindsight" / "codex" / "scripts").is_dir()
            and (Path.home() / ".codex" / "hooks.json").is_file())


def memory_retain(task_id: str, kind: str, summary: str,
                  metadata: dict[str, Any] | None = None, *,
                  scope: str = "project",
                  hard_boundary: bool = False,
                  source_doc: str | None = None,
                  supersede_id: str | None = None,
                  force_hindsight: bool = False) -> dict[str, Any]:
    """Retain a screened summary. force_hindsight is for migration and never falls back."""
    started = time.monotonic()
    try:
        config = load_memory_config()
    except (ValueError, OSError, json.JSONDecodeError):
        return {"status": "unavailable", "reason": "bad_response", "backend": "none"}
    if not config["retain"].get("enabled", True) and not force_hindsight:
        return {"status": "disabled", "reason": "disabled"}
    if kind not in KINDS or not isinstance(summary, str) or not summary.strip():
        return {"status": "rejected", "reason": "invalid_input"}
    max_chars = min(int(config["retain"].get("max_chars", 4000)), 8000)
    if len(summary) > max_chars:
        return {"status": "rejected", "reason": "size_limit"}
    if _has_secret(summary):
        return {"status": "rejected", "reason": "secret_detected"}
    merged_meta = dict(metadata or {})
    if scope in {"project", "global"}:
        merged_meta.setdefault("scope", scope)
    if hard_boundary:
        merged_meta["hard_boundary"] = True
    if source_doc:
        merged_meta.setdefault("source_doc", source_doc)
    if supersede_id:
        merged_meta.setdefault("supersede_id", supersede_id)
    try:
        safe_metadata = _safe_metadata(merged_meta)
    except ValueError:
        return {"status": "rejected", "reason": "secret_detected"}
    task = get_task(task_id)
    safe_metadata.update({"project_id": task["project_id"], "task_id": task_id,
                          "session_id": task.get("session_id") or "",
                          "role": safe_metadata.get("role", "codex"),
                          "kind": kind, "task_type": task["state"]["profile"].get("task_type", ""),
                          "source": safe_metadata.get("source", "global_runtime")})
    safe_metadata.setdefault("source_task_id", task_id)
    safe_metadata.setdefault("created_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    safe_metadata.setdefault("git_revision", _git_head_revision(Path(task["project_root"])))
    try:
        safe_metadata = _safe_metadata(safe_metadata)
    except ValueError:
        return {"status": "rejected", "reason": "secret_detected"}
    backend, status, _ = _selection(task_id, config, force_hindsight=force_hindsight)
    bank_id = status["bank_id"]
    if backend is None:
        return {**status, "kind": kind}
    _event("memory_retain_started", task_id, task["project_id"], status["backend"], "started", started, kind=kind)
    fingerprint = _fingerprint(bank_id, kind, summary)
    claim_scope = f"{bank_id}::{hindsight_endpoint_id(config)}" if status["backend"] == "hindsight" else bank_id
    claimed = False
    try:
        if status["backend"] == "hindsight":
            reservation = _claim(claim_scope, fingerprint)
            if reservation == "duplicate":
                return {"status": "ok", "backend": "hindsight", "bank_id": bank_id,
                        "memory_id": f"cap-{fingerprint}", "document_id": f"cap-{fingerprint}",
                        "project_id": task["project_id"], "kind": kind, "deduplicated": True}
            if reservation == "in_progress":
                return {"status": "unavailable", "backend": "hindsight", "bank_id": bank_id,
                        "project_id": task["project_id"], "kind": kind, "reason": "unavailable"}
            claimed = True
            safe_metadata["document_id"] = f"cap-{fingerprint}"
        result = backend.retain(bank_id=bank_id, content=summary, kind=kind, metadata=safe_metadata)
        if claimed:
            _finish_claim(claim_scope, fingerprint, True)
        if status["status"] == "fallback":
            result.update({"status": "fallback", "reason": status.get("reason"),
                           "fallback": "sqlite"})
            _event("memory_fallback", task_id, task["project_id"], "sqlite", "fallback", started,
                   kind=kind, error_type=status.get("reason"))
        _event("memory_retain_completed", task_id, task["project_id"], result["backend"],
               result["status"], started, kind=kind)
        return result
    except MemoryBackendError as exc:
        if claimed:
            _finish_claim(claim_scope, fingerprint, False)
        _event("memory_retain_failed", task_id, task["project_id"], status["backend"],
               "unavailable", started, kind=kind, error_type=exc.reason)
        fallback = _fallback(task_id, config, force_hindsight=force_hindsight)
        if fallback is not None:
            result = fallback.retain(bank_id=bank_id, content=summary, kind=kind, metadata=safe_metadata)
            result.update({"status": "fallback", "reason": exc.reason,
                           "fallback": "sqlite"})
            _event("memory_fallback", task_id, task["project_id"], "sqlite", "fallback", started,
                   kind=kind, error_type=exc.reason)
            return result
        return {"status": "unavailable", "backend": "hindsight", "bank_id": bank_id,
                "project_id": task["project_id"], "kind": kind, "reason": exc.reason}


def memory_retain_hindsight(task_id: str, kind: str, summary: str,
                            metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Migration entry point: Hindsight only, with no SQLite fallback."""
    return memory_retain(task_id, kind, summary, metadata, force_hindsight=True)


def memory_recall(task_id: str, query: str, kinds: list[str] | None = None,
                  limit: int = 5, max_chars: int = 4000, *,
                  scope: str = "project+global") -> dict[str, Any]:
    started = time.monotonic()
    try:
        config = load_memory_config()
    except (ValueError, OSError, json.JSONDecodeError):
        return {"status": "unavailable", "reason": "bad_response", "backend": "none", "results": []}
    if not config["recall"].get("enabled", True):
        return {"status": "disabled", "reason": "disabled", "results": []}
    if not isinstance(query, str) or not query.strip() or len(query) > 2000 or _has_secret(query):
        return {"status": "rejected", "reason": "secret_detected" if isinstance(query, str) and _has_secret(query) else "invalid_input", "results": []}
    if kinds is not None and (not isinstance(kinds, list) or any(kind not in KINDS for kind in kinds)):
        return {"status": "rejected", "reason": "invalid_input", "results": []}
    if scope not in {"project", "global", "modality", "project+global", "3d"}:
        scope = "project+global"
    limit = max(1, min(int(limit), int(config["recall"].get("max_limit", 10)), 10))
    max_chars = max(1, min(int(max_chars), int(config["recall"].get("max_chars", 4000)), 8000))
    task = get_task(task_id)
    backend, status, _ = _selection(task_id, config)
    bank_id = status["bank_id"]
    if backend is None:
        return {**status, "results": [], "truncated": False}
    _event("memory_recall_started", task_id, task["project_id"], status["backend"], "started", started)
    try:
        if isinstance(backend, SQLiteMemoryBackend):
            result = backend.recall(bank_id=bank_id, query=query.strip(), kinds=kinds,
                                    limit=limit, max_chars=max_chars, scope=scope)
        else:
            result = backend.recall(bank_id=bank_id, query=query.strip(), kinds=kinds,
                                    limit=limit, max_chars=max_chars)
        if status["status"] == "fallback":
            result.update({"status": "fallback", "reason": status.get("reason"),
                           "fallback": "sqlite"})
            _event("memory_fallback", task_id, task["project_id"], "sqlite", "fallback", started,
                   error_type=status.get("reason"))
        _event("memory_recall_completed", task_id, task["project_id"], result["backend"],
               result["status"], started, result_count=len(result["results"]))
        return result
    except MemoryBackendError as exc:
        _event("memory_recall_failed", task_id, task["project_id"], status["backend"],
               "unavailable", started, error_type=exc.reason)
        fallback = _fallback(task_id, config, force_hindsight=False)
        if fallback is not None:
            result = fallback.recall(bank_id=bank_id, query=query.strip(), kinds=kinds,
                                     limit=limit, max_chars=max_chars, scope=scope)
            result.update({"status": "fallback", "reason": exc.reason,
                           "fallback": "sqlite"})
            _event("memory_fallback", task_id, task["project_id"], "sqlite", "fallback", started,
                   error_type=exc.reason)
            return result
        return {"status": "unavailable", "backend": "hindsight", "bank_id": bank_id,
                "reason": exc.reason, "results": [], "truncated": False}


def memory_reflect(task_id: str, query: str, max_chars: int = 4000) -> dict[str, Any]:
    started = time.monotonic()
    try:
        config = load_memory_config()
    except (ValueError, OSError, json.JSONDecodeError):
        return {"status": "unavailable", "reason": "bad_response", "backend": "none", "text": ""}
    if not config["reflect"].get("enabled", True):
        return {"status": "disabled", "reason": "disabled", "text": ""}
    if not isinstance(query, str) or not query.strip() or len(query) > 2000 or _has_secret(query):
        return {"status": "rejected", "reason": "secret_detected" if isinstance(query, str) and _has_secret(query) else "invalid_input", "text": ""}
    max_chars = max(1, min(int(max_chars), int(config["reflect"].get("max_chars", 4000)), 8000))
    task = get_task(task_id)
    backend, status, _ = _selection(task_id, config)
    bank_id = status["bank_id"]
    if backend is None:
        return {"status": "unavailable", "backend": status["backend"], "bank_id": bank_id,
                "reason": status.get("reason", "not_configured"), "text": ""}
    _event("memory_reflect_started", task_id, task["project_id"], status["backend"], "started", started)
    try:
        result = backend.reflect(bank_id=bank_id, query=query.strip(), max_chars=max_chars)
        _event("memory_reflect_completed", task_id, task["project_id"], status["backend"], "ok", started)
        return result
    except MemoryBackendError as exc:
        _event("memory_reflect_failed", task_id, task["project_id"], status["backend"],
               "unavailable", started, error_type=exc.reason)
        return {"status": "unavailable", "backend": status["backend"], "bank_id": bank_id,
                "reason": exc.reason, "text": ""}
