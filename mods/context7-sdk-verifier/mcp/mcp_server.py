"""Static SDK reconciliation and reviewed memory commits over MCP stdio.

Target modules are read as source, never imported. All persistent paths are module
constants so callers can isolate review and commit operations in a temporary store.
"""
from __future__ import annotations

import ast
import hashlib
import json
import ntpath
import os
import re
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import tomllib
except ImportError:  # Python 3.9/3.10 installations can use tomli.
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

def _resolve_cross_platform_memory_dir(env_key: str, win_default: str, posix_default: str) -> Path:
    raw = os.environ.get(env_key, "").strip()
    if not raw:
        return Path(win_default if os.name == "nt" else posix_default)
    if os.name != "nt" and (":" in raw or "\\" in raw):
        return Path(posix_default)
    return Path(raw)


_DEFAULT_MEM_DIR = str(Path.home() / ".antigravity-agent-policy" / "05_Agent_Memory_Sync")
_DEFAULT_MIRROR_DIR = str(Path.home() / ".antigravity-agent-policy" / "05_Agent_Memory_Mirror")
MEMORY_ROOT = _resolve_cross_platform_memory_dir("CONTEXT7_MEMORY_ROOT", _DEFAULT_MEM_DIR, _DEFAULT_MEM_DIR)
MIRROR_ROOT = _resolve_cross_platform_memory_dir("CONTEXT7_MIRROR_ROOT", _DEFAULT_MIRROR_DIR, _DEFAULT_MIRROR_DIR)


def _resolve_reconcile_cache_path() -> Path:
    raw = os.environ.get("CONTEXT7_RECONCILE_CACHE_PATH", "").strip()
    if raw and not (os.name != "nt" and (":" in raw or "\\" in raw)):
        return Path(raw)
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_app_data) if local_app_data else (Path.home() / "AppData" / "Local")
        return base / "codex-agent-policy" / "reconcile_cache.json"
    xdg_cache = os.environ.get("XDG_CACHE_HOME", "").strip()
    cache_base = Path(xdg_cache) if xdg_cache else (Path.home() / ".cache")
    return cache_base / "codex-agent-policy" / "reconcile_cache.json"


CACHE_PATH = _resolve_reconcile_cache_path()
QUOTA_CACHE_PATH = Path(
    os.environ.get(
        "CONTEXT7_QUOTA_CACHE_PATH",
        str(Path.home() / ".antigravity-agent-policy" / "all-pools-quota.json"),
    )
)
QUOTA_STALE_MAX_AGE_MS = 30 * 60 * 1000
MANIFESTS = {"pyproject.toml", "uv.lock", "requirements.txt", "package.json", "Cargo.toml", "mod.json"}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".packages"}
_RECONCILE_CACHE = {}
_LOCK = threading.RLock()
INVALID_HASH = "INVALID_RECONCILE_HASH: 必须先调用 sdk_fact_reconcile_report 获取客观事实对账哈希"
BLOCKED_REVIEWER_MODELS = {
    "6.1sol", "gpt-6.1-sol", "gpt-6-sol",
    "opus5.5", "claude-opus-5-5", "claude-opus-5-5-high", "claude-opus-5-5-medium", "claude-opus-5-5-low",
}
ALLOWED_REVIEWER_ALIASES = {
    "6.1terra": "gpt-6.1-terra",
    "gpt-6.1-terra": "gpt-6.1-terra",
    "terra": "gpt-6.1-terra",
    "sonnet5.5": "claude-sonnet-5-5",
    "claude-sonnet-5-5": "claude-sonnet-5-5",
    "claude-sonnet-5-5-high": "claude-sonnet-5-5",
    "claude-sonnet-5-5-medium": "claude-sonnet-5-5",
    "claude-sonnet-5-5-low": "claude-sonnet-5-5",
    "sonnet": "claude-sonnet-5-5",
}
HOMOGENEOUS_MASTER_MODELS = {
    "gemini-4-argon", "gemini-4-argon-high", "gemini-4-argon-medium", "argon",
    "gemini-4-carbon", "gemini-4-carbon-high", "gemini-4-carbon-medium", "carbon",
    "gemini-4-hydrogen", "gemini-4-hydrogen-medium", "gemini-4-hydrogen-high", "gemini-4-hydrogen-xhigh", "hydrogen",
}


def _select_reviewer_by_weekly_quota(terra_weekly_pct: float | None = None,
                                     sonnet_weekly_pct: float | None = None) -> dict:
    """Select between 6.1terra and sonnet5.5 based on higher remaining weekly quota; block 6.1sol & opus5.5."""
    if terra_weekly_pct is None and os.environ.get("CONTEXT7_TERRA_WEEKLY_PCT"):
        try:
            terra_weekly_pct = float(os.environ["CONTEXT7_TERRA_WEEKLY_PCT"])
        except ValueError:
            pass
    if sonnet_weekly_pct is None and os.environ.get("CONTEXT7_SONNET_WEEKLY_PCT"):
        try:
            sonnet_weekly_pct = float(os.environ["CONTEXT7_SONNET_WEEKLY_PCT"])
        except ValueError:
            pass
    quota_stale = False
    fetched_at_ms = None
    if terra_weekly_pct is None or sonnet_weekly_pct is None:
        if QUOTA_CACHE_PATH.is_file():
            try:
                qdata = json.loads(QUOTA_CACHE_PATH.read_text(encoding="utf-8-sig"))
                raw_fetched = qdata.get("fetchedAtMs", qdata.get("fetched_at_ms"))
                if isinstance(raw_fetched, (int, float)) and not isinstance(raw_fetched, bool):
                    fetched_at_ms = int(raw_fetched)
                    if (time.time() * 1000.0 - float(raw_fetched)) > QUOTA_STALE_MAX_AGE_MS:
                        quota_stale = True
                else:
                    mtime_ms = QUOTA_CACHE_PATH.stat().st_mtime * 1000.0
                    if (time.time() * 1000.0 - mtime_ms) > QUOTA_STALE_MAX_AGE_MS:
                        quota_stale = True
                if terra_weekly_pct is None and isinstance(qdata.get("codex"), dict):
                    w = qdata["codex"].get("weekly")
                    if isinstance(w, (int, float)) and not isinstance(w, bool):
                        terra_weekly_pct = float(w)
                if sonnet_weekly_pct is None and isinstance(qdata.get("claude"), dict):
                    w = qdata["claude"].get("weekly")
                    if isinstance(w, (int, float)) and not isinstance(w, bool):
                        sonnet_weekly_pct = float(w)
            except (OSError, ValueError, TypeError):
                quota_stale = True
        else:
            quota_stale = True
    t_pct = round(float(75.0 if terra_weekly_pct is None else terra_weekly_pct), 2)
    s_pct = round(float(50.0 if sonnet_weekly_pct is None else sonnet_weekly_pct), 2)
    if t_pct >= s_pct:
        selected_model = "gpt-6.1-terra"
        selected_alias = "6.1terra"
        cli_agent = "codex-cli"
        cli_flag = '-c model="gpt-6.1-terra"'
        reason = f"6.1terra 周剩余额度 ({t_pct}%) >= sonnet5.5 周剩余额度 ({s_pct}%)"
    else:
        selected_model = "claude-sonnet-5-5"
        selected_alias = "sonnet5.5"
        cli_agent = "opus-cli"
        cli_flag = "--model claude-sonnet-5-5"
        reason = f"sonnet5.5 周剩余额度 ({s_pct}%) > 6.1terra 周剩余额度 ({t_pct}%)"
    return {
        "selected_model": selected_model,
        "selected_alias": selected_alias,
        "cli_agent": cli_agent,
        "cli_flag": cli_flag,
        "terra_weekly_remaining_percent": t_pct,
        "sonnet_weekly_remaining_percent": s_pct,
        "quota_stale": quota_stale,
        "quota_fetched_at_ms": fetched_at_ms,
        "allowed_models": ["gpt-6.1-terra", "claude-sonnet-5-5"],
        "blocked_models": sorted(BLOCKED_REVIEWER_MODELS),
        "selection_reason": reason,
    }


def _normalize_reviewer(reviewer_model: str | None) -> str | None:
    """Normalize reviewer model alias; NEVER fabricate a reviewer model when None/empty."""
    if reviewer_model is None or not str(reviewer_model).strip():
        return None
    raw = str(reviewer_model).strip().casefold()
    return ALLOWED_REVIEWER_ALIASES.get(raw, str(reviewer_model).strip())


def _validate_reviewer_model(reviewer_model: str | None, require_heterogeneous: bool = False) -> tuple[str | None, str | None]:
    """Validate supplied reviewer_model against blocklist and optional heterogeneous requirement."""
    normalized = _normalize_reviewer(reviewer_model)
    if normalized is not None:
        raw = str(reviewer_model).strip().casefold()
        if raw in BLOCKED_REVIEWER_MODELS or normalized.casefold() in BLOCKED_REVIEWER_MODELS:
            return None, (
                f"BLOCKED_REVIEWER_MODEL: 记忆审查严禁使用 {reviewer_model}（禁用 6.1sol 与 opus5.5），"
                "请改用周额度剩余更多的 6.1terra (gpt-6.1-terra) 或 sonnet5.5 (claude-sonnet-5-5)"
            )
        if require_heterogeneous and (raw in HOMOGENEOUS_MASTER_MODELS or raw not in ALLOWED_REVIEWER_ALIASES):
            return None, (
                f"HETEROGENEOUS_REVIEW_REQUIRED: 高影响记忆写入必须由异构模型 6.1terra 或 sonnet5.5 复核，"
                f"拒绝使用同源或未授权模型 ({reviewer_model})"
            )
        return normalized, None
    if require_heterogeneous:
        return None, "HETEROGENEOUS_REVIEW_REQUIRED: 高影响记忆写入必须显式指定 reviewer_model (6.1terra 或 sonnet5.5)"
    return "mechanical-only", None


def _json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _norm(path):
    return ntpath.normpath(str(path).replace("/", "\\")).casefold()


def _project(project_root):
    registry = _json(MEMORY_ROOT / "00_Project_Registry.json", {})
    target = _norm(project_root)
    matches = []
    for project in registry.get("projects", []):
        for folder in project.get("folders", []):
            folder = folder.get("path", "") if isinstance(folder, dict) else folder
            if folder:
                base = _norm(folder).rstrip("\\")
                if target == base or target.startswith(base + "\\"):
                    matches.append((len(base), project["project_id"]))
    project_id = max(matches)[1] if matches else "workbench_misc"
    if not re.fullmatch(r"[\w.-]+", project_id) or project_id in {".", ".."}:
        raise ValueError("Invalid project_id in registry")
    return project_id


def _atomic_write(path, value):
    """Replace one complete UTF-8 JSON document, retrying transient sync locks."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(0.05 * (2 ** attempt))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _manifest_paths(root, changed_files=()):
    found = set()
    if root.is_dir():
        for directory, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not Path(directory, d).is_symlink())
            found.update(Path(directory, name) for name in files if name in MANIFESTS)
    for path in changed_files:
        found.update(path.parent / name for name in MANIFESTS if (path.parent / name).is_file())
    return sorted(found, key=lambda p: str(p).casefold())


def _manifest_fact(path):
    raw = path.read_bytes()
    fact = {"source_path": str(path), "source_sha256": hashlib.sha256(raw).hexdigest(), "filename": path.name}
    try:
        text = raw.decode("utf-8-sig")
        if path.suffix == ".json":
            data = json.loads(text)
        elif path.suffix in {".toml", ".lock"}:
            if tomllib is None:
                fact["parse_error"] = "TOML parsing requires Python 3.11+ or tomli"
                fact["declarations"] = text
                return fact
            data = tomllib.loads(text)
        else:
            fact["dependencies"] = [line.strip() for line in text.splitlines()
                                     if line.strip() and not line.lstrip().startswith("#")]
            return fact
        for key in ("name", "version", "dependencies", "devDependencies", "optionalDependencies",
                    "peerDependencies", "dependency-groups", "package", "tool"):
            if key in data:
                fact[key] = data[key]
        for key in ("project", "package"):
            if isinstance(data.get(key), dict):
                fact.update({k: data[key][k] for k in ("name", "version", "dependencies") if k in data[key]})
        if "mcp_tools" in data:
            fact["mcp_tools"] = [t["name"] if isinstance(t, dict) else t for t in data["mcp_tools"]]
    except (ValueError, UnicodeError) as exc:
        fact["parse_error"] = str(exc)
    return fact


def _context7(packages, symbols):
    # Official REST contract: https://context7.com/docs/api-guide
    if not packages:
        return []
    try:
        query = urlencode({"libraryName": packages[0], "query": " ".join(symbols or packages)})
        headers = {"Accept": "application/json"}
        if os.environ.get("CONTEXT7_API_KEY"):
            headers["Authorization"] = "Bearer " + os.environ["CONTEXT7_API_KEY"]
        with urlopen(Request("https://context7.com/api/v2/libs/search?" + query, headers=headers), timeout=3) as response:
            return json.loads(response.read(1024 * 1024)).get("results", [])
    except Exception:
        return []  # Network access is optional; offline preflight must still work.


_MODALITY_KEYWORDS = {
    "ppt_product_pitch": ("产品介绍", "ppt", "路演", "汇报", "演示", "pitch", "slide", "deck"),
    "ui_aesthetic_design": ("ui", "界面", "前端", "配色", "美学", "视觉", "动效", "oklch", "bento",
                            "css", "html", "design", "motion", "审美", "排版", "svg", "设计"),
    "office_report_doc": ("报告", "word", "excel", "pdf", "docx", "xlsx", "计算书", "工程报告"),
    "systems_and_automation": ("自动化", "桌面", "流水线", "系统", "部署", "运维", "uia", "automation", "pipeline", "devops"),
    "coding_and_testing": ("代码", "编程", "测试", "开发", "调试", "mcp", "ast", "bug", "sdk", "api", "python"),
}


def _modality_ids(value):
    if value is not None and not isinstance(value, (str, list)):
        raise ValueError("Invalid modality_id: expected a string or list of strings")
    ids = []
    for raw in _as_ids(value):
        if not isinstance(raw, str):
            raise ValueError("Invalid modality_id: expected a string")
        mid = raw.strip().casefold()
        if not re.fullmatch(r"[\w.-]+", mid) or mid in {".", ".."}:
            raise ValueError("Invalid modality_id: " + repr(raw))
        if mid not in ids:
            ids.append(mid)
    return ids


def _infer_modalities(modalities=None, task_intent=None):
    ids = _modality_ids(modalities)
    intent = (task_intent or "").casefold()
    for mid, keywords in _MODALITY_KEYWORDS.items():
        if mid not in ids and any(keyword in intent for keyword in keywords):
            ids.append(mid)
    return ids or ["coding_and_testing"]


def _is_expired(item: dict) -> bool:
    if item.get("invalid_at"):
        return True
    now_utc = datetime.now(timezone.utc)
    for field in ("reverify_after", "expires_at"):
        exp = item.get(field)
        if not exp:
            continue
        try:
            exp_dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if exp_dt <= now_utc:
                return True
        except ValueError:
            continue
    return False


def _reverify_timestamp(now_dt: datetime, ttl_days: int | None) -> str | None:
    if ttl_days is None:
        return None
    if isinstance(ttl_days, bool) or not isinstance(ttl_days, int) or ttl_days < 1:
        raise ValueError("Invalid ttl_days: expected a positive integer")
    return (now_dt + timedelta(days=ttl_days)).isoformat()


def _active_items(data, key):
    return [item for item in data.get(key, []) if isinstance(item, dict) and not _is_expired(item)]


def sdk_fact_lookup(project_root: str = ".", symbols: list[str] | None = None,
                    packages: list[str] | None = None, query_context7: bool = False,
                    modalities: list[str] | str | None = None, task_intent: str | None = None) -> dict:
    try:
        matched_modalities = _infer_modalities(modalities, task_intent)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    project_id = _project(project_root)
    data = _json(MEMORY_ROOT / "L1_Projects" / (project_id + ".json"), {})
    terms = [term.casefold() for term in (packages or []) + (symbols or []) if term]

    def select(key):
        active = _active_items(data, key)
        return [item for item in active if not terms or any(t in _canonical(item).casefold() for t in terms)]

    facts, memories, experiences = select("facts"), select("memories"), select("experiences")
    manifests = []
    if terms:
        for path in _manifest_paths(Path(project_root)):
            if path.name in {"mod.json", "pyproject.toml", "package.json"}:
                fact = _manifest_fact(path)
                if any(t in _canonical(fact).casefold() for t in terms):
                    manifests.append(fact)
    modality_rules, modality_presets, modality_memories = [], [], []
    for mid in matched_modalities:
        modality = _json(MEMORY_ROOT / "L1_Modalities" / (mid + ".json"), {})
        for key, collection in (("craft_rules", modality_rules), ("design_presets", modality_presets),
                                ("memories", modality_memories), ("experiences", modality_memories)):
            collection.extend({**item, "modality_id": mid} for item in _active_items(modality, key))
    global_data = _json(MEMORY_ROOT / "L2_Experiences.json", {})
    if isinstance(global_data, list):
        global_data = {"experiences": global_data}
    global_experiences = _active_items(global_data, "experiences")
    core_path = MEMORY_ROOT / "L3_Core_Rules.md"
    core_rules = [line for line in core_path.read_text(encoding="utf-8-sig").splitlines()
                  if line.startswith("- ")] if core_path.is_file() else []
    lines = ["# 三维交叉作战简报 (Cross-Dimension Briefing Card): " + project_id,
             "SDK Pre-Flight: " + project_id,
             "## 一、项目客观真值（来自 L1_Projects/" + project_id + "）"]
    for item in facts + memories + experiences:
        lines.append("- " + str(item.get("summary", item.get("value", item.get("title", item.get("fact_key", "")))))[:500])
    for item in manifests:
        lines.append("- " + item["source_path"] + ": " + _canonical(item)[:500])
    lines.append("## 二、交付工艺与审美准则（来自 L1_Modalities/" + ", ".join(matched_modalities) + "）")
    for item in modality_rules + modality_presets + modality_memories:
        lines.append("- " + _canonical(item))
    lines.append("## 三、全局安全与表达底线（来自 L2_Experiences & L3_Core_Rules）")
    lines.extend("- " + _canonical(item) for item in global_experiences)
    lines.extend(core_rules)
    return {"ok": True, "project_id": project_id, "matched_facts": facts,
            "active_memories": memories, "active_experiences": experiences,
            "manifest_facts": manifests, "cheat_sheet_markdown": "\n".join(lines),
            "matched_modalities": matched_modalities, "modality_rules": modality_rules,
            "modality_presets": modality_presets, "modality_memories": modality_memories,
            "global_experiences": global_experiences, "core_rules_excerpt": "\n".join(core_rules),
            "context7_results": _context7(packages or [], symbols or []) if query_context7 else []}


def _module_name(path, root):
    try:
        parts = list(path.relative_to(root).with_suffix("").parts)
    except ValueError:
        parts = [path.stem]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _signature(node, path, module, qualname):
    parameters = []
    args = node.args
    positional = args.posonlyargs + args.args
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    for index, (arg, default) in enumerate(zip(positional, defaults)):
        parameters.append({"name": arg.arg, "kind": "positional_only" if index < len(args.posonlyargs) else "positional_or_keyword",
                           "required": default is None, "default": ast.unparse(default) if default is not None else None,
                           "annotation": ast.unparse(arg.annotation) if arg.annotation else None})
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        parameters.append({"name": arg.arg, "kind": "keyword_only", "required": default is None,
                           "default": ast.unparse(default) if default is not None else None,
                           "annotation": ast.unparse(arg.annotation) if arg.annotation else None})
    for arg, kind in ((args.vararg, "var_positional"), (args.kwarg, "var_keyword")):
        if arg:
            parameters.append({"name": arg.arg, "kind": kind, "required": False, "default": None})
    return {"name": node.name, "qualname": qualname, "module": module,
            "source_path": str(path), "line": node.lineno, "kind": "function", "parameters": parameters,
            "signature": "(" + ast.unparse(args) + ")", "accepts_kwargs": args.kwarg is not None,
            "decorators": [ast.unparse(d) for d in node.decorator_list],
            "required_keyword_only": [p["name"] for p in parameters if p["kind"] == "keyword_only" and p["required"]]}


def _python_facts(path, module, symbols, errors):
    try:
        # ast.parse accepts bytes and respects PEP 263 encoding declarations.
        raw = path.read_bytes()
        tree = ast.parse(raw, filename=str(path))
    except (OSError, SyntaxError, ValueError) as exc:
        errors.append({"source_path": str(path), "error": str(exc)})
        return [], None
    facts = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if node.name.startswith("_") and node.name not in symbols:
            continue
        if isinstance(node, ast.ClassDef):
            methods = [m for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
            constructor = next((m for m in methods if m.name in {"__init__", "__new__"}), None)
            fact = _signature(constructor, path, module, node.name) if constructor else {
                "name": node.name, "qualname": node.name, "module": module, "source_path": str(path),
                "line": node.lineno, "parameters": [], "signature": None, "accepts_kwargs": True,
                "required_keyword_only": []}  # Inherited constructors are unknown, not zero-argument.
            fact.update(name=node.name, kind="class", decorators=[ast.unparse(d) for d in node.decorator_list])
            facts.append(fact)
            for method in methods:
                if not method.name.startswith("_") or method.name in symbols or node.name + "." + method.name in symbols:
                    facts.append(_signature(method, path, module, node.name + "." + method.name))
        else:
            facts.append(_signature(node, path, module, node.name))
    digest = hashlib.sha256(raw).hexdigest()
    for fact in facts:
        fact["source_sha256"] = digest
    return facts, tree


def _package_sources(root, package):
    """Resolve files directly; find_spec/import_module may execute parent packages."""
    if not re.fullmatch(r"[@\w./-]+", package) or ".." in package.split("/"):
        return []
    sources = []
    bases = [root / ".venv" / "Lib" / "site-packages", root]
    bases += sorted((root / ".venv" / "lib").glob("python*/site-packages"))
    bases += [Path(p) for p in sys.path if p]
    bases += [Path(p) for p in sysconfig.get_paths().values() if p]
    for base in dict.fromkeys(bases):
        candidate = base.joinpath(*package.split("."))
        if candidate.is_dir():
            sources = sorted(set(candidate.rglob("*.py")) | set(candidate.rglob("*.pyi")))
        else:
            sources = [p for p in (candidate.with_suffix(".pyi"), candidate.with_suffix(".py")) if p.is_file()]
        if sources:
            return [(p, _module_name(p, base)) for p in sources]
    for candidate in (root / "node_modules" / package, root / "node_modules" / "@types" / package):
        if candidate.is_dir():
            return [(p, package) for p in sorted(candidate.rglob("*.d.ts"))]
    return []


def _typescript_facts(path, module):
    # Declarations are reported verbatim; Python keyword auditing never uses them.
    text = path.read_text(encoding="utf-8-sig")
    return [{"name": match.group(2), "qualname": match.group(2), "module": module,
             "kind": match.group(1), "language": "typescript", "declaration": match.group(0).strip(),
             "source_path": str(path), "source_sha256": _sha(path), "line": text.count("\n", 0, match.start()) + 1}
            for match in re.finditer(r"(?:export\s+)?(?:declare\s+)?(function|class|interface)\s+(\w+)[^;{]*(?:[;{]|$)", text)]


def _changed_paths(root):
    paths = set()
    for args in (["git", "status", "--porcelain", "-z", "--untracked-files=all"],
                 ["git", "diff", "--name-only", "-z", "HEAD"]):
        try:
            result = subprocess.run(args, cwd=root, stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                continue
            entries = iter(result.stdout.decode("utf-8", errors="replace").split("\0"))
            for entry in entries:
                if not entry:
                    continue
                if args[1] == "status":
                    paths.add(entry[3:])
                    if "R" in entry[:2] or "C" in entry[:2]:
                        next(entries, None)  # -z emits destination then original name.
                else:
                    paths.add(entry)
        except (OSError, subprocess.TimeoutExpired):
            continue
    return sorted(paths)


def _imports(tree, module, is_package=False):
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            prefix = node.module or ""
            if node.level:
                parts = module.split(".") if is_package else module.split(".")[:-1]
                prefix = ".".join(parts[:len(parts) - node.level + 1] + ([prefix] if prefix else []))
            for alias in node.names:
                aliases[alias.asname or alias.name] = (prefix + "." + alias.name).strip(".")
    return aliases


def _call_audit(trees, facts):
    index = {}
    for fact in facts:
        if fact.get("language") == "typescript":
            continue
        key = (fact["module"] + "." + fact["qualname"]).strip(".")
        index.setdefault(key, []).append(fact)
    calls, mismatches = [], []
    for path, (module, tree) in trees.items():
        aliases = _imports(tree, module, path.name == "__init__.py")
        # Rebinding an imported name makes static resolution uncertain.
        rebound = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        rebound.update(n.arg for n in ast.walk(tree) if isinstance(n, ast.arg))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            expression = ast.unparse(node.func)
            head, _, tail = expression.partition(".")
            if head in rebound:
                continue
            resolved = aliases[head] + ("." + tail if tail else "") if head in aliases else module + "." + expression
            candidates = index.get(resolved.strip("."), [])
            if not candidates:
                continue
            keywords = [k.arg for k in node.keywords if k.arg is not None]
            # An overload is valid when at least one declared signature accepts it.
            unknown_sets = []
            for fact in candidates:
                allowed = {p["name"] for p in fact["parameters"] if p["kind"] in {"positional_or_keyword", "keyword_only"}}
                unknown_sets.append(set() if fact["accepts_kwargs"] else set(keywords) - allowed)
            unknown = sorted(set.intersection(*unknown_sets))
            call = {"source_path": str(path), "line": node.lineno, "callee": expression,
                    "resolved_symbol": resolved, "keywords": keywords}
            calls.append(call)
            if unknown:
                mismatches.append({**call, "unexpected_keywords": unknown, "signature": candidates[0]["signature"]})
    return {"calls": calls, "signature_mismatches": mismatches}


def _report_hash(payload):
    return hashlib.sha256(("context7-sidecar-v1:" + _canonical(payload)).encode("utf-8")).hexdigest()[:24]


def sdk_fact_reconcile_report(project_root: str = ".", changed_files: list[str] | None = None,
                              inspect_packages: list[str] | None = None, inspect_symbols: list[str] | None = None) -> dict:
    root = Path(project_root).resolve()
    project_id = _project(project_root)
    changed = changed_files if changed_files is not None else _changed_paths(root)
    paths = sorted({(root / p).resolve() for p in changed}, key=str)
    errors, internal, external, trees = [], [], [], {}
    symbols = set(inspect_symbols or [])
    for path in paths:
        if path.suffix == ".py" and path.is_file():
            module = _module_name(path, root)
            facts, tree = _python_facts(path, module, symbols, errors)
            internal.extend(facts)
            if tree:
                trees[path] = (module, tree)
        elif path.name == "mod.json" and path.is_file():
            internal.append({"source_path": str(path), "source_sha256": _sha(path),
                             "kind": "mcp_tools", "mcp_tools": _manifest_fact(path).get("mcp_tools", [])})
    # Include local modules imported by changed files, even when unchanged.
    for path, (module, tree) in list(trees.items()):
        for imported in _imports(tree, module, path.name == "__init__.py").values():
            pieces = imported.split(".")
            for length in range(len(pieces), 0, -1):
                candidate = root.joinpath(*pieces[:length])
                candidates = [candidate.with_suffix(".py"), candidate / "__init__.py"]
                found = next((p for p in candidates if p.is_file()), None)
                if found:
                    if found not in trees:
                        facts, _ = _python_facts(found, _module_name(found, root), symbols, errors)
                        internal.extend(facts)
                    break
    for package in sorted(set(inspect_packages or [])):
        sources = _package_sources(root, package)
        if not sources:
            errors.append({"package": package, "error": "No local source or type declarations found"})
        for path, module in sources:
            if path.name.endswith(".d.ts"):
                try:
                    external.extend(_typescript_facts(path, module))
                except (OSError, UnicodeError) as exc:
                    errors.append({"source_path": str(path), "error": str(exc)})
            else:
                facts, _ = _python_facts(path, module, symbols, errors)
                external.extend(facts)
    # Deduplicate imported modules and sort every source for deterministic hashes.
    internal = list({_canonical(f): f for f in internal}.values())
    internal.sort(key=_canonical)
    external.sort(key=_canonical)
    lockfiles = [_manifest_fact(p) for p in _manifest_paths(root, paths)]
    payload = {"project_id": project_id, "lockfile_facts": lockfiles,
               "internal_seam_facts": internal, "package_ast_facts": external}
    reconcile_hash = _report_hash(payload)
    record = {"project_root": _norm(root), "payload": payload}
    with _LOCK:
        cache = _json(CACHE_PATH, {})
        cache[reconcile_hash] = record
        _atomic_write(CACHE_PATH, dict(list(cache.items())[-128:]))
        _RECONCILE_CACHE[reconcile_hash] = record
    audit = _call_audit(trees, [f for f in internal + external if "qualname" in f])
    return {"ok": True, **payload, "call_site_audit": audit,
            "signature_mismatches": audit["signature_mismatches"], "reconcile_hash": reconcile_hash,
            "reviewer_routing": _select_reviewer_by_weekly_quota(),
            "analysis_errors": errors}


def _age(item):
    value = item.get("created_at", item.get("updated_at", 0))
    try:
        return float(value)
    except (ValueError, TypeError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0


def _compact(items, limit):
    active = [item for item in items if not item.get("invalid_at")]
    archived = [item for item in items if item.get("invalid_at")]
    removable = sorted((item for item in active if not item.get("hard_boundary")), key=_age)
    count = max(0, len(active) - limit)
    if count > len(removable):
        raise ValueError("WATERMARK_HARD_BOUNDARY: protected entries exceed capacity")
    removed = {id(item) for item in removable[:count]}
    archived.extend(item for item in active if id(item) in removed)
    return [item for item in active if id(item) not in removed], archived


def _as_ids(value):
    return [value] if isinstance(value, str) else list(value or [])


def _resolve_source_file(project_root, source_path):
    """Resolve physical evidence without replacing the caller's logical path."""
    if not source_path:
        return None
    source = Path(str(source_path).replace("\\", "/"))
    candidates = [Path(project_root) / source]
    if source.is_absolute():
        candidates.append(source)
    else:
        relative = source.as_posix()
        prefix = "05_Agent_Memory_Sync/"
        if relative.startswith(prefix):
            relative = relative[len(prefix):]
        candidates.append(MEMORY_ROOT / relative)
    return next((path for path in candidates if path.is_file()), None)


def _normalize_experience(raw: dict, project_id: str, reconcile_hash: str, now_iso: str) -> dict:
    """Upgrade reviewed lessons to the shared runtime/distiller experience schema."""
    def strings(value):
        return list(dict.fromkeys(v.strip() for v in _as_ids(value) if isinstance(v, str) and v.strip()))

    summary = str(raw.get("summary") or raw.get("title") or "已验证经验").strip()
    projects = sorted(set(strings(raw.get("validated_projects"))) | {project_id})
    source_doc = str(raw.get("source_doc") or "context7-sdk-verifier")
    experience_id = raw.get("experience_id") or raw.get("memory_id") or "exp_" + hashlib.sha256(
        _canonical([project_id, summary, raw.get("kind", "lesson")]).encode("utf-8")).hexdigest()[:12]
    conditions = strings(raw.get("conditions")) or [str(raw.get("kind") or "lesson") + " 场景：" + summary]
    recommendations = strings(raw.get("recommendations")) or ["执行前核对并遵循已验证约束：" + summary]
    anti_patterns = strings(raw.get("anti_patterns"))
    if "anti_patterns" not in raw:
        anti_patterns = [part.strip() for part in re.split(r"[。\n]+", summary)
                         if re.search(r"\b(?:never|do not|must not)\b|禁止|不得|避免", part, re.I)]
    return {**raw, "experience_id": str(experience_id),
            "scope": "global" if raw.get("scope") == "global" else "project", "project_id": project_id,
            "title": str(raw.get("title") or summary.splitlines()[0][:100]), "summary": summary,
            "conditions": conditions, "recommendations": recommendations, "anti_patterns": anti_patterns,
            "confidence": float(raw.get("confidence", 0.95)), "status": "active",
            "support_count": int(raw.get("support_count", max(1, len(projects)))),
            "project_count": int(raw.get("project_count", max(1, len(projects)))),
            "validated_projects": projects,
            "source_refs": strings(_as_ids(raw.get("source_refs")) + ["sidecar:" + reconcile_hash, source_doc]),
            "hard_boundary": bool(raw.get("hard_boundary", False)), "updated_at": now_iso}


def _promotions(data, project_id, reconcile_hash, now_iso):
    promoted = []
    for item in data.get("memories", []) + data.get("experiences", []):
        if item.get("invalid_at"):
            continue
        if "experience_id" not in item and not item.get("hard_boundary"):
            continue
        projects = {project_id} | set(item.get("validated_projects", []))
        for path in sorted((MEMORY_ROOT / "L1_Projects").glob("*.json")):
            other = _json(path, {})
            for candidate in other.get("memories", []) + other.get("experiences", []):
                if not candidate.get("invalid_at") and (candidate.get("hard_boundary") or "experience_id" in item) and (
                        candidate.get("summary") == item.get("summary") or
                        (item.get("experience_id") and candidate.get("experience_id") == item["experience_id"]) or
                        (item.get("memory_id") and candidate.get("memory_id") == item["memory_id"])):
                    projects.add(other.get("project_id", path.stem))
        if len(projects) >= 2 or item.get("cross_project_verified") is True or item.get("scope") == "global":
            promoted.append(_normalize_experience(
                {**item, "scope": "global", "validated_projects": sorted(projects),
                 "project_count": len(projects), "support_count": max(item.get("support_count", 1), len(projects))},
                project_id, reconcile_hash, now_iso))
    return promoted


def _modality_writes(modality_id, modalities, rules, memories, presets, reconcile_hash, now,
                     reviewer_model=None, reverify_after=None):
    """Validate and stage modality documents before any persistent write occurs."""
    targets = _modality_ids(_modality_ids(modality_id) + _modality_ids(modalities))
    default_mid = targets[0] if len(targets) == 1 else None
    grouped = {mid: {"craft_rules": [], "memories": [], "design_presets": []} for mid in targets}
    for key, items in (("craft_rules", rules), ("memories", memories), ("design_presets", presets)):
        for supplied in items or []:
            if not isinstance(supplied, dict):
                raise ValueError(key + " requires object entries")
            mid = supplied.get("modality_id", default_mid)
            if mid is None:
                raise ValueError("Each modality entry requires modality_id when no single target is specified")
            mid = _modality_ids([mid])[0]
            if mid not in grouped:
                targets.append(mid)
                grouped[mid] = {"craft_rules": [], "memories": [], "design_presets": []}
            grouped[mid][key].append(supplied)
    writes = []
    active_rules = active_memories = 0
    for mid in targets:
        relative = Path("L1_Modalities") / (mid + ".json")
        data = _json(MEMORY_ROOT / relative, {"modality_id": mid, "modality_name": mid,
                     "associated_mods": [], "craft_rules": [], "design_presets": [], "memories": [], "experiences": []})
        data.update(modality_id=mid, updated_at=now)
        for key, id_key, prefix in (("craft_rules", "rule_id", "rule_"), ("memories", "memory_id", "memory_")):
            items = data.setdefault(key, [])
            by_id = {}
            for idx, item in enumerate(items):
                if isinstance(item, dict):
                    iid = item.get(id_key) or item.get("id") or f"{prefix}legacy_{idx}"
                    item.setdefault(id_key, iid)
                    by_id[iid] = item
            for supplied in grouped[mid][key]:
                identity = {k: v for k, v in supplied.items() if k != "modality_id"}
                item_id = supplied.get(id_key) or prefix + hashlib.sha256(
                    _canonical([mid, identity]).encode("utf-8")).hexdigest()[:12]
                item = {**by_id.get(item_id, {}), **supplied}
                supersedes = set(_as_ids(supplied.get("supersedes")) + _as_ids(supplied.get("supersede_ids")))
                if item_id in supersedes:
                    raise ValueError("A modality entry cannot supersede itself")
                for target in supersedes:
                    if target in by_id:
                        by_id[target].update(invalid_at=now, superseded_by=item_id)
                item.update({id_key: item_id, "created_at": by_id.get(item_id, {}).get("created_at", now),
                             "updated_at": now, "invalid_at": None, "superseded_by": None, "modality_id": mid})
                item["hard_boundary"] = bool(item.get("hard_boundary", False))
                item["summary"] = str(item.get("summary") or item.get("rule") or "")
                if reviewer_model:
                    item["reviewer_model"] = reviewer_model
                if reverify_after:
                    item["reverify_after"] = reverify_after
                if key == "craft_rules":
                    item["category"] = str(item.get("category") or "general")
                    item["title"] = str(item.get("title") or item["summary"][:100] or item_id)
                    item["anti_patterns"] = _as_ids(item.get("anti_patterns"))
                else:
                    item.update(valid_from=by_id.get(item_id, {}).get("valid_from", now),
                                source_task_id="sidecar_" + reconcile_hash[:8])
                if item_id in by_id:
                    by_id[item_id].update(item)
                else:
                    items.append(item)
                    by_id[item_id] = item
        presets_by_key = {}
        for idx, item in enumerate(data.setdefault("design_presets", [])):
            if isinstance(item, dict):
                pk = item.get("preset_key") or item.get("key") or item.get("id") or f"preset_{idx}"
                item.setdefault("preset_key", pk)
                presets_by_key[pk] = item
        for supplied in grouped[mid]["design_presets"]:
            if not supplied.get("preset_key"):
                raise ValueError("verified_design_presets requires preset_key")
            item = {**presets_by_key.get(supplied["preset_key"], {}), **supplied, "updated_at": now}
            item.setdefault("value", None)
            item.setdefault("source_mod", "")
            item["invalid_at"] = None
            presets_by_key[item["preset_key"]] = item
        data["design_presets"] = list(presets_by_key.values())
        archive_relative = Path("L1_Modalities/archive") / (mid + "_archive.json")
        archive = _json(MEMORY_ROOT / archive_relative, {"modality_id": mid, "craft_rules": [], "memories": []})
        for key, limit in (("craft_rules", 60), ("memories", 50)):
            data[key], removed = _compact(data[key], limit)
            existing = {_canonical(item) for item in archive.get(key, [])}
            archive.setdefault(key, []).extend(item for item in removed if _canonical(item) not in existing)
        active_rules += len(data["craft_rules"])
        active_memories += len(data["memories"])
        writes.extend([(archive_relative, archive), (relative, data)])
    return writes, targets, active_rules, active_memories
def sdk_memory_review_commit(project_root: str = ".", reconcile_hash: str = "",
                             verified_facts: list[dict] | None = None, verified_memories: list[dict] | None = None,
                             supersede_memory_ids: list[str] | None = None, promote_to_global: bool = False,
                             verified_experiences: list[dict] | None = None, project_name: str | None = None,
                             modality_id: str | None = None, modalities: list[str] | None = None,
                             verified_modality_rules: list[dict] | None = None,
                             verified_modality_memories: list[dict] | None = None,
                             verified_design_presets: list[dict] | None = None,
                             reviewer_model: str | None = None, mechanical_only: bool = False,
                             require_heterogeneous_review: bool | None = None, ttl_days: int | None = None) -> dict:
    with _LOCK:
        return _commit(project_root, reconcile_hash, verified_facts or [], verified_memories or [],
                       supersede_memory_ids or [], promote_to_global, verified_experiences or [], project_name,
                       modality_id, modalities, verified_modality_rules, verified_modality_memories,
                       verified_design_presets, reviewer_model=reviewer_model, mechanical_only=mechanical_only,
                       require_heterogeneous_review=require_heterogeneous_review, ttl_days=ttl_days)


def _commit(project_root, reconcile_hash, verified_facts, verified_memories, supersede_ids, promote,
            verified_experiences, project_name, modality_id=None, modalities=None,
            verified_modality_rules=None, verified_modality_memories=None, verified_design_presets=None,
            reviewer_model=None, mechanical_only=False, require_heterogeneous_review=None, ttl_days=None):
    project_id = _project(project_root)
    try:
        cache = _json(CACHE_PATH, {})
    except (ValueError, OSError):
        cache = {}
    record = cache.get(reconcile_hash) or _RECONCILE_CACHE.get(reconcile_hash)
    if (not reconcile_hash or not record or record.get("project_root") != _norm(Path(project_root).resolve())
            or record.get("payload", {}).get("project_id") != project_id
            or _report_hash(record["payload"]) != reconcile_hash):
        return {"ok": False, "error": INVALID_HASH}
    if mechanical_only and (verified_memories or verified_experiences or verified_modality_rules
                            or verified_modality_memories or promote):
        return {
            "ok": False,
            "error": (
                "MECHANICAL_AUDIT_ONLY: 机械审计模式仅允许写入可确定性校验的客观事实 "
                "(verified_facts / verified_design_presets)，拒绝写入主观归纳条目"
            ),
        }
    all_supplied_dicts = [
        item for group in (verified_facts, verified_memories, verified_experiences,
                           verified_modality_rules or [], verified_modality_memories or [])
        for item in group if isinstance(item, dict)
    ]
    high_impact = bool(promote or any(item.get("hard_boundary") or item.get("scope") == "global" for item in all_supplied_dicts))
    if require_heterogeneous_review is None:
        need_hetero = bool(high_impact and not mechanical_only)
    else:
        need_hetero = bool(require_heterogeneous_review)
    resolved_reviewer, reviewer_err = _validate_reviewer_model(reviewer_model, require_heterogeneous=need_hetero)
    if reviewer_err:
        return {"ok": False, "error": reviewer_err}
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    try:
        reverify_after = _reverify_timestamp(now_dt, ttl_days)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        modality_writes, updated_modalities, active_modality_rules, active_modality_memories = _modality_writes(
            modality_id, modalities, verified_modality_rules, verified_modality_memories,
            verified_design_presets, reconcile_hash, now, reviewer_model=resolved_reviewer,
            reverify_after=reverify_after)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    relative = Path("L1_Projects") / (project_id + ".json")
    data = _json(MEMORY_ROOT / relative, {"project_id": project_id, "facts": [], "memories": [], "experiences": []})
    facts = {}
    for idx, f in enumerate(data.get("facts", [])):
        if isinstance(f, dict):
            fk = f.get("fact_key") or f.get("id") or f.get("key") or f"legacy_fact_{idx}"
            f.setdefault("fact_key", fk)
            facts[fk] = f
    for supplied in verified_facts:
        if not supplied.get("fact_key"):
            return {"ok": False, "error": "verified_facts requires fact_key"}
        fact = {**facts.get(supplied["fact_key"], {}), **supplied}
        source = fact.get("source_path") or ""
        source_path = _resolve_source_file(project_root, source)
        if source_path is not None:
            digest = _sha(source_path)
            if fact.get("source_sha256") and fact["source_sha256"] != digest:
                fact["sha256_corrected"] = True
                fact["reported_source_sha256"] = fact["source_sha256"]
            fact["source_sha256"] = digest
        fact.update(project_id=project_id, source_path=source, source_revision="sidecar:" + reconcile_hash,
                    updated_at=now, invalid_at=None, verification_mode="deterministic_mechanical",
                    reviewer_model=resolved_reviewer)
        if reverify_after:
            fact["reverify_after"] = reverify_after
        fact.setdefault("source_sha256", "")
        fact.setdefault("evidence", "Static reconciliation " + reconcile_hash)
        facts[fact["fact_key"]] = fact
    memories = data.get("memories", [])
    by_id = {}
    for idx, m in enumerate(memories):
        if isinstance(m, dict):
            mid = m.get("memory_id") or m.get("id") or f"legacy_mem_{idx}"
            m.setdefault("memory_id", mid)
            by_id[mid] = m
    for index, supplied in enumerate(verified_memories):
        memory = dict(supplied)
        memory_id = memory.get("memory_id") or "sidecar_" + hashlib.sha256(
            (project_id + _canonical(memory)).encode("utf-8")).hexdigest()[:20]
        old = by_id.get(memory_id, {})
        targets = set(_as_ids(memory.get("supersedes")) + _as_ids(memory.get("supersede_ids")))
        if index == 0:
            targets.update(supersede_ids)
        if memory_id in targets:
            return {"ok": False, "error": "A memory cannot supersede itself"}
        for target in targets:
            if target in by_id:
                by_id[target].update(invalid_at=now, superseded_by=memory_id)
        memory.update(memory_id=memory_id, project_id=project_id,
                      scope="global" if memory.get("scope") == "global" else "project",
                      hard_boundary=bool(memory.get("hard_boundary", False)), pyramid_level="L1_ATOMIC_LESSON",
                      trust_tier="TIER_2_RUNTIME_VERIFIED", kind=memory.get("kind", "lesson"),
                      summary=str(memory.get("summary", "")), source_task_id="sidecar_" + reconcile_hash[:8],
                      created_at=old.get("created_at", now), valid_from=old.get("valid_from", now),
                      invalid_at=None, superseded_by=None, source_doc="context7-sdk-verifier",
                      verification_mode="heuristic_reviewed", reviewer_model=resolved_reviewer)
        if reverify_after:
            memory["reverify_after"] = reverify_after
        if memory["kind"] not in {"decision", "lesson", "pitfall"}:
            return {"ok": False, "error": "Invalid memory kind"}
        if memory_id in by_id:
            by_id[memory_id].update(memory)
        else:
            memories.append(memory)
            by_id[memory_id] = memory
    if supersede_ids and not verified_memories:
        return {"ok": False, "error": "Superseding memories requires a replacement memory"}
    experiences = {}
    for raw in data.get("experiences", []):
        item = _normalize_experience(raw, project_id, reconcile_hash, now)
        previous = experiences.get(item["experience_id"], {})
        experiences[item["experience_id"]] = _normalize_experience(
            {**previous, **raw, "experience_id": item["experience_id"]}, project_id, reconcile_hash, now)
    for raw in verified_experiences:
        item = _normalize_experience(raw, project_id, reconcile_hash, now)
        previous = experiences.get(item["experience_id"], {})
        extra = {"reviewer_model": resolved_reviewer}
        if reverify_after:
            extra["reverify_after"] = reverify_after
        experiences[item["experience_id"]] = _normalize_experience(
            {**previous, **raw, **extra, "experience_id": item["experience_id"]}, project_id, reconcile_hash, now)
    data.update(project_id=project_id, facts=list(facts.values()), memories=memories,
                experiences=list(experiences.values()), updated_at=now)
    archive_relative = Path("L1_Projects/archive") / (project_id + "_archive.json")
    archive = _json(MEMORY_ROOT / archive_relative, {"project_id": project_id, "facts": [], "memories": []})
    for key, limit in (("facts", 80), ("memories", 50)):
        try:
            data[key], removed = _compact(data[key], limit)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        existing = {_canonical(item) for item in archive.get(key, [])}
        archive.setdefault(key, []).extend(item for item in removed if _canonical(item) not in existing)
    writes = [(archive_relative, archive), (relative, data)]  # Preserve history before removing active entries.
    name = project_name.strip() if project_name else ""
    mod_count = next((str(f.get("value")).strip() for f in reversed(verified_facts)
                      if f.get("fact_key") == "mod_total_count" and re.fullmatch(r"\d+", str(f.get("value")).strip())), None)
    refresh_mods = project_id == "mods" and mod_count is not None
    if name or refresh_mods:
        registry = _json(MEMORY_ROOT / "00_Project_Registry.json", {"projects": []})
        project = next((p for p in registry.get("projects", []) if p.get("project_id") == project_id), None)
        name = name or data.get("project_name") or data.get("name") or (project or {}).get("name") or "mods（统一封装场景化 Mod 插件中心）"
        if refresh_mods:
            name = re.sub(r"\b\d+\s*大", mod_count + " 大", name)
        elif project_id == "mods":
            name = name.replace("20 大", "21 大")
        data["project_name"] = name
        if "name" in data:
            data["name"] = name
        if project is not None:
            project["name"] = name
        writes.insert(1, (Path("00_Project_Registry.json"), registry))
    promoted = _promotions(data, project_id, reconcile_hash, now) if promote else []
    if promote:
        global_data = _json(MEMORY_ROOT / "L2_Experiences.json", {"experiences": []})
        old_items = global_data if isinstance(global_data, list) else global_data.get("experiences", [])
        global_items = {}
        for raw in old_items + promoted:
            if not isinstance(raw, dict) or raw.get("invalid_at") or not (raw.get("summary") or raw.get("title")):
                continue
            item = _normalize_experience({**raw, "scope": "global"}, raw.get("project_id") or project_id,
                                         reconcile_hash, now)
            previous = global_items.get(item["experience_id"], {})
            projects = sorted(set(previous.get("validated_projects", []) + item["validated_projects"]))
            item.update(validated_projects=projects, project_count=len(projects),
                        support_count=max(item["support_count"], previous.get("support_count", 0), len(projects)))
            global_items[item["experience_id"]] = item
        global_data = {} if isinstance(global_data, list) else global_data
        global_data["experiences"] = list(global_items.values())
        writes.append((Path("L2_Experiences.json"), global_data))
    writes.extend(modality_writes)
    written = []
    try:
        for path, value in writes:
            if path == relative and (name or refresh_mods):
                # The registry is now physical; hash its final bytes before saving dependent facts.
                registry_path = (MEMORY_ROOT / "00_Project_Registry.json").resolve()
                for fact in data["facts"]:
                    source_file = _resolve_source_file(project_root, fact.get("source_path"))
                    if source_file is not None and _norm(source_file.resolve()) == _norm(registry_path):
                        digest = _sha(source_file)
                        if fact.get("source_sha256") and fact["source_sha256"] != digest:
                            fact["sha256_corrected"] = True
                            fact.setdefault("reported_source_sha256", fact["source_sha256"])
                        fact["source_sha256"] = digest
            _atomic_write(MEMORY_ROOT / path, value)
            written.append(str(MEMORY_ROOT / path))
    except OSError as exc:
        return {"ok": False, "error": str(exc), "written_paths": written, "mirror_synced": False}

    mirror_synced = True
    mirror_warning = None
    if _norm(MIRROR_ROOT) != _norm(MEMORY_ROOT):
        try:
            mirror_exists = MIRROR_ROOT.is_dir()
        except OSError as exc:
            mirror_exists = False
            mirror_synced = False
            mirror_warning = f"MIRROR_OFFLINE: {exc}"
        if mirror_warning is None:
            if modality_writes or mirror_exists:
                try:
                    for path, value in writes:
                        _atomic_write(MIRROR_ROOT / path, value)
                        written.append(str(MIRROR_ROOT / path))
                except OSError as exc:
                    mirror_synced = False
                    mirror_warning = f"MIRROR_SYNC_FAILED: {exc}"
            else:
                mirror_synced = False
                mirror_warning = f"MIRROR_OFFLINE: mirror directory not mounted ({MIRROR_ROOT})"

    response = {"ok": True, "project_id": project_id, "reconcile_hash": reconcile_hash,
                "reviewer_model": resolved_reviewer, "mirror_synced": mirror_synced,
                "active_facts": len(data["facts"]), "active_memories": len(data["memories"]),
                "archived_facts": len(archive["facts"]), "archived_memories": len(archive["memories"]),
                "updated_modalities": updated_modalities, "active_modality_rules": active_modality_rules,
                "active_modality_memories": active_modality_memories,
                "promoted": len(promoted), "written_paths": written}
    if mirror_warning:
        response["mirror_warning"] = mirror_warning
    return response


def _schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_INT = {"type": "integer", "minimum": 1}
_STRINGS = {"type": "array", "items": _STR}
_OBJECTS = {"type": "array", "items": {"type": "object"}}
TOOLS = [
    {"name": "sdk_fact_lookup", "description": "Read-only Project x Modality x Global briefing and optional Context7 lookup.",
     "inputSchema": _schema({"project_root": _STR, "symbols": _STRINGS, "packages": _STRINGS,
                             "modalities": {"anyOf": [_STR, _STRINGS]}, "task_intent": _STR,
                             "query_context7": _BOOL, "library_or_module": _STR, "symbol_or_topic": _STR, "allow_network": _BOOL})},
    {"name": "sdk_fact_reconcile_report", "description": "Audit physical manifests, source signatures and changed call sites without importing code.",
     "inputSchema": _schema({"project_root": _STR, "changed_files": _STRINGS, "inspect_packages": _STRINGS,
                             "inspect_symbols": _STRINGS, "target_files": _STRINGS,
                             "phase": {"type": "string", "enum": ["pre_flight", "post_diff"]}})},
    {"name": "sdk_memory_review_commit", "description": "Commit reviewed facts and bitemporal memories using an issued reconciliation hash.",
     "inputSchema": _schema({"project_root": _STR, "reconcile_hash": _STR, "verified_facts": _OBJECTS,
                             "verified_memories": _OBJECTS, "verified_experiences": _OBJECTS, "project_name": _STR,
                             "modality_id": _STR, "modalities": _STRINGS, "verified_modality_rules": _OBJECTS,
                             "verified_modality_memories": _OBJECTS, "verified_design_presets": _OBJECTS,
                             "supersede_memory_ids": _STRINGS, "reviewer_model": _STR,
                             "mechanical_only": _BOOL, "require_heterogeneous_review": _BOOL, "ttl_days": _INT,
                             "promote_to_global": _BOOL, "invalidate_memory_ids": _STRINGS}, ["reconcile_hash"])},
]


def dispatch(name, arguments):
    functions = {"sdk_fact_lookup": sdk_fact_lookup, "sdk_fact_reconcile_report": sdk_fact_reconcile_report,
                 "sdk_memory_review_commit": sdk_memory_review_commit}
    if name not in functions:
        raise ValueError("Unknown tool: " + str(name))
    args = dict(arguments)
    if name == "sdk_fact_lookup":
        for old, new in (("library_or_module", "packages"), ("symbol_or_topic", "symbols")):
            if old in args:
                args.setdefault(new, [args.pop(old)])
        if "allow_network" in args:
            args.setdefault("query_context7", args.pop("allow_network"))
    elif name == "sdk_fact_reconcile_report":
        if "target_files" in args:
            args.setdefault("changed_files", args.pop("target_files"))
        args.pop("phase", None)
    elif "invalidate_memory_ids" in args:
        args.setdefault("supersede_memory_ids", args.pop("invalidate_memory_ids"))
    return functions[name](**args)


def _handle_request(request):
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    if "id" not in request:
        return None
    result = {"jsonrpc": "2.0", "id": request["id"]}
    method = request["method"]
    if method == "initialize":
        result["result"] = {"protocolVersion": (request.get("params") or {}).get("protocolVersion", "2024-11-05"),
                            "capabilities": {"tools": {}}, "serverInfo": {"name": "context7-sdk-verifier", "version": "1.1.0"}}
    elif method == "ping":
        result["result"] = {}
    elif method == "tools/list":
        result["result"] = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            params = request.get("params") or {}
            data = dispatch(params.get("name"), params.get("arguments") or {})
            result["result"] = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                                "isError": not data.get("ok", True)}
        except Exception as exc:
            result["result"] = {"content": [{"type": "text", "text": json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)}], "isError": True}
    else:
        result["error"] = {"code": -32601, "message": "Method not found: " + method}
    return result


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            response = _handle_request(json.loads(line))
        except (ValueError, TypeError, AttributeError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
