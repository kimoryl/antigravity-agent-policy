"""Provider-neutral polyglot code intelligence & incremental SQLite CodeGraph for the Global Runtime.

Upgraded with Path-B Native Polyglot CodeGraph:
- 100% pure Python (zero native binary dependencies, Kaspersky-safe, zero background daemon).
- Full AST analysis for Python (.py, .pyi) + structural lexer/AST extraction for TypeScript/JavaScript
  (.ts, .tsx, .js, .jsx, .mjs, .cjs), Go (.go), Rust (.rs), C# (.cs), Java (.java), and C/C++ (.c, .cpp, .h, .hpp).
- On-demand SHA256 incremental SQLite cache (stored in LOCALAPPDATA/codex-agent-policy/codegraph_cache.sqlite3)
  so unchanged files are loaded in O(1) without re-parsing or polluting git repositories with .codegraph folders.
- Token-budgeted `codegraph_explore` for one-shot call-neighborhood & impact exploration without the "fat output trap".
"""

from __future__ import annotations

import ast
from contextlib import closing
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterable

from boundary_index import CONTRACT_EXTENSIONS, extract_facts, link_boundaries, match_protocol_query
from scip_protocol import build_scip_protocol_index, decode_scip_index, encode_scip_index

__all__ = [
    "CAPABILITY",
    "PROVIDERS",
    "architecture_extract",
    "capability_catalog",
    "code_health_audit",
    "code_repomap_rank",
    "code_search",
    "codegraph_explore",
    "decode_scip_index",
    "encode_scip_index",
    "export_scip_protocol_index",
    "impact_analysis",
    "symbol_trace",
]


CAPABILITY = {
    "name": "code-intelligence",
    "type": "hybrid",
    "provides": [
        "code.search",
        "symbol.trace",
        "dependency.analysis",
        "impact.analysis",
        "architecture.extract",
        "codegraph.explore",
        "code.repomap.rank",
        "code.health.audit",
    ],
    "stage": ["planning", "execution", "verification"],
}

PROVIDERS = {
    "polyglot": {
        "name": "polyglot",
        "kind": "builtin",
        "primary": True,
        "implementation": "polyglot_sqlite_codegraph",
        "capabilities": CAPABILITY["provides"],
        "status": "available",
    },
    "codegraph": {
        "name": "codegraph",
        "kind": "mcp",
        "primary": True,
        "mcp_server": "codegraph",
        "capabilities": CAPABILITY["provides"][:5],
        "status": "external-configurable",
    },
    "gitnexus": {
        "name": "gitnexus",
        "kind": "mcp",
        "primary": False,
        "mcp_server": "gitnexus",
        "capabilities": CAPABILITY["provides"][:5],
        "status": "optional-external-configurable",
    },
    "python_ast": {
        "name": "python_ast",
        "kind": "builtin",
        "primary": False,
        "implementation": "python_ast_builtin",
        "capabilities": CAPABILITY["provides"][:5],
        "status": "available",
    },
}

_PYTHON_EXTS = {".py", ".pyi"}
_TS_JS_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
_GO_EXTS = {".go"}
_RUST_EXTS = {".rs"}
_CS_JAVA_CPP_EXTS = {".cs", ".java", ".c", ".cpp", ".cc", ".h", ".hpp"}

_LEGACY_EXTENSIONS = _PYTHON_EXTS | _TS_JS_EXTS
_POLYGLOT_EXTENSIONS = _PYTHON_EXTS | _TS_JS_EXTS | _GO_EXTS | _RUST_EXTS | _CS_JAVA_CPP_EXTS

_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "out",
    "target",
    "__pycache__",
    ".pytest_cache",
    ".next",
    ".nuxt",
    ".codegraph",
    "vendor",
    "third_party",
    "3rdparty",
    "benchmarks",
    "fixtures",
    "testdata",
    "migrations",
}

_KEYWORDS_IGNORE = {
    "if", "else", "for", "while", "switch", "case", "return", "catch", "try",
    "sizeof", "typeof", "instanceof", "new", "delete", "void", "await", "yield",
    "super", "this", "self", "print", "println", "printf", "fmt", "log", "len",
    "make", "append", "panic", "recover", "close", "delete", "copy", "complex",
    "real", "imag", "Some", "None", "Ok", "Err", "Box", "Vec", "String", "str",
    "int", "float", "bool", "list", "dict", "set", "tuple", "range", "enumerate",
    "isinstance", "issubclass", "getattr", "setattr", "hasattr", "open", " max", "min",
}


def _default_cache_db_path() -> Path:
    custom = os.environ.get("CODE_INTELLIGENCE_CACHE_PATH", "").strip()
    if custom:
        return Path(custom).expanduser().resolve()
    base = os.environ.get("CODEX_AGENT_POLICY_DATA", "").strip()
    if not base:
        local_app = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
        base = os.path.join(local_app, "codex-agent-policy")
    return Path(base) / "codegraph_cache.sqlite3"


class _IncrementalCache:
    """Zero-daemon SQLite incremental AST cache keyed by (project_root, rel_path, sha256)."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or _default_cache_db_path()
        self.enabled = os.environ.get("CODE_INTELLIGENCE_DISABLE_CACHE", "").strip() not in {"1", "true", "yes"}
        if self.enabled:
            try:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
                self._init_db()
            except OSError:
                self.enabled = False

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS file_ast_cache (
                        project_root TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        rel_path TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        mtime_ns INTEGER NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (project_root, mode, rel_path)
                    )
                    """
                )

    def load_project(self, root_key: str, mode: str) -> dict[str, dict[str, Any]]:
        if not self.enabled:
            return {}
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    "SELECT rel_path, sha256, mtime_ns, size_bytes, payload_json FROM file_ast_cache WHERE project_root = ? AND mode = ?",
                    (root_key, mode),
                ).fetchall()
            out: dict[str, dict[str, Any]] = {}
            for rel_path, sha256, mtime_ns, size_bytes, payload_json in rows:
                out[rel_path] = {
                    "sha256": sha256,
                    "mtime_ns": mtime_ns,
                    "size_bytes": size_bytes,
                    "payload": json.loads(payload_json),
                }
            return out
        except Exception:
            return {}

    def save_updates(
        self,
        root_key: str,
        mode: str,
        upserts: list[tuple[str, str, int, int, dict[str, Any]]],
        stale_paths: set[str],
    ) -> None:
        if not self.enabled or (not upserts and not stale_paths):
            return
        now = time.time()
        try:
            with closing(self._connect()) as conn:
                with conn:
                    if stale_paths:
                        conn.executemany(
                            "DELETE FROM file_ast_cache WHERE project_root = ? AND mode = ? AND rel_path = ?",
                            [(root_key, mode, rel) for rel in stale_paths],
                        )
                    if upserts:
                        conn.executemany(
                            """
                            INSERT OR REPLACE INTO file_ast_cache
                            (project_root, mode, rel_path, sha256, mtime_ns, size_bytes, payload_json, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            [
                                (root_key, mode, rel, sha, mtime_ns, size_b, json.dumps(payload, ensure_ascii=False), now)
                                for rel, sha, mtime_ns, size_b, payload in upserts
                            ],
                        )
        except Exception:
            pass


def _provider_descriptor(name: str) -> dict[str, Any]:
    descriptor = dict(PROVIDERS[name])
    if descriptor["kind"] == "mcp":
        descriptor["implementation"] = "python_ast_fallback"
        descriptor["fallback_reason"] = "external MCP adapter is metadata-only until configured"
    return descriptor


def capability_catalog() -> dict[str, Any]:
    """Return the capability and provider metadata used by MCP/Dashboard."""
    preferred = os.environ.get("CODE_INTELLIGENCE_PROVIDER", "codegraph").strip().lower() or "codegraph"
    if preferred not in PROVIDERS:
        preferred = "codegraph"
    return {
        "capability": CAPABILITY,
        "providers": [_provider_descriptor(name) for name in ("codegraph", "gitnexus", "python_ast", "polyglot") if name in PROVIDERS],
        "preferred_provider": preferred,
    }


def _is_link(path: Path) -> bool:
    """Reject symlinks and all Windows reparse points without following them."""
    stat = path.lstat()
    return path.is_symlink() or bool(getattr(stat, "st_file_attributes", 0) & 0x400)


def _root(project_root: str | os.PathLike[str]) -> Path:
    requested = Path(project_root).expanduser()
    for part in (requested, *requested.absolute().parents):
        if _is_link(part):
            raise ValueError(f"project_root must not traverse a symlink/reparse point: {project_root}")
    root = requested.resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"project_root is not a directory: {project_root}")
    return root


def _files(root: Path, allowed_exts: set[str]) -> Iterable[Path]:
    max_files = int(os.environ.get("CODE_INTELLIGENCE_MAX_FILES", "350") or "350")
    candidates: list[tuple[int, int, str, Path]] = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        safe_dirs = []
        for name in dirs:
            try:
                if name not in _SKIP_DIRS and not _is_link(Path(directory) / name):
                    safe_dirs.append(name)
            except OSError:
                continue
        dirs[:] = safe_dirs
        for name in files:
            path = Path(directory) / name
            try:
                if _is_link(path) or path.suffix.lower() not in allowed_exts or path.stat().st_size > 262144:
                    continue
            except OSError:
                continue
            rel = path.relative_to(root)
            is_aux = int(any(p.lower() in {"tests", "test", "examples", "example", "docs"} for p in rel.parts))
            candidates.append((is_aux, len(rel.parts), rel.as_posix(), path))
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    for _, _, _, path in candidates[:max_files]:
        yield path


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _module(path: Path, root: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] in {"__init__", "index", "mod", "lib"} and len(parts) > 1:
        parts.pop()
    return ".".join(parts) or path.stem


def _attribute_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _attribute_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


# ---------------------------------------------------------------------------
# Comment & String Masker for C-family / TS / Go / Rust / C# / Java languages
# Preserves exact line numbers and character offsets while blanking comments
# and string contents so structural regexes never match inside literals!
# ---------------------------------------------------------------------------

_TOKEN_CLEAN_RE = re.compile(
    r"//[^\n]*|/\*[\s\S]*?\*/|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`"
)


def _mask_comments_and_strings(source: str) -> str:
    return _TOKEN_CLEAN_RE.sub(
        lambda m: (
            " " * len(m.group(0))
            if m.group(0).startswith("//")
            else "".join("\n" if ch == "\n" else " " for ch in m.group(0))
            if m.group(0).startswith("/*")
            else m.group(0)[0] + "".join("\n" if ch == "\n" else " " for ch in m.group(0)[1:-1]) + m.group(0)[-1]
        ),
        source,
    )


def _line_number_at(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _find_matching_brace(masked: str, open_pos: int) -> int:
    depth = 0
    for idx in range(open_pos, len(masked)):
        ch = masked[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return idx
    return len(masked) - 1


def _find_matching_paren(masked: str, open_pos: int) -> int:
    depth = 0
    for idx in range(open_pos, len(masked)):
        ch = masked[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return idx
    return -1


def _extract_calls_in_span(masked_span: str) -> list[str]:
    calls: list[str] = []
    for m in re.finditer(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\(", masked_span):
        callee = m.group(1)
        short = callee.rsplit(".", 1)[-1]
        if short in _KEYWORDS_IGNORE or callee in _KEYWORDS_IGNORE:
            continue
        if callee.startswith(("this.", "self.")):
            callee = callee.split(".", 1)[1]
        calls.append(callee)
    return calls


def _body_metrics(item: dict[str, Any], masked: str, opening: int, closing: int) -> None:
    """One lexical pass; strings/comments are already masked, braces retain offsets."""
    complexity, depth, maximum = 1, 0, 0
    stack: list[bool] = []
    pending = False
    for token in re.findall(r"\b(?:if|elif|for|while|except|with|and|or|case|catch|switch|match|else|try)\b|&&|\|\||[{};]", masked[opening + 1:closing]):
        if token == "{":
            stack.append(pending)
            depth += int(pending)
            maximum = max(maximum, depth)
            pending = False
        elif token == "}":
            if stack:
                depth -= int(stack.pop())
            pending = False
        elif token == ";":
            pending = False
        elif token in {"and", "or", "&&", "||"}:
            complexity += 1
        else:
            if token not in {"else", "try", "switch", "match"}:
                complexity += 1
            pending = True
    item.update(cyclomatic_complexity=complexity,
                loc=_line_number_at(masked, closing) - item["line"] + 1,
                max_nesting_depth=maximum, body_start=opening + 1, body_end=closing)


def _estimated_tokens(text: str) -> int:
    # A conservative byte-token upper bound, independent of a model/tokenizer.
    return len(text.encode("utf-8"))


def _compression_pct(compact: str, full: Any) -> float:
    total = _estimated_tokens(json.dumps(full, ensure_ascii=False))
    return round(max(0.0, 100.0 * (1 - _estimated_tokens(compact) / max(1, total))), 2)


def _health_baseline(root: Path, dead: list[dict[str, Any]], cycles: list[dict[str, Any]],
                     hotspots: list[dict[str, Any]], baseline_path: str | None,
                     save: bool) -> dict[str, Any]:
    diff: dict[str, Any] = {"new_dead_code": [], "resolved_dead_code": [],
                            "new_circular_dependencies": [], "new_complexity_hotspots": [],
                            "regression_detected": False}
    selected = baseline_path or os.environ.get("CODE_INTELLIGENCE_BASELINE_PATH", "").strip()
    if not selected and not save:
        return diff
    path = (Path(selected).expanduser() if selected else _default_cache_db_path().parent /
            (hashlib.sha256(str(root).encode()).hexdigest()[:16] + ".baseline.json"))
    current = {"version": "2.1.0", "dead_code": dead, "circular_dependencies": cycles,
               "complexity_hotspots": hotspots}
    diff["baseline_path"] = str(path)
    if save:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
                temporary = stream.name
                json.dump(current, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary, path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        diff["baseline_saved"] = True
    elif path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        def symbol_key(item: dict[str, Any]) -> tuple[str, str]:
            return item["path"], item["symbol"]
        old_dead = {symbol_key(item): item for item in previous.get("dead_code", [])}
        new_dead = {symbol_key(item): item for item in dead}
        old_cycles = {tuple(item["cycle"]) for item in previous.get("circular_dependencies", [])}
        old_hot = {symbol_key(item): item["cyclomatic_complexity"] for item in previous.get("complexity_hotspots", [])}
        diff["new_dead_code"] = [item for key, item in new_dead.items() if key not in old_dead]
        diff["resolved_dead_code"] = [item for key, item in old_dead.items() if key not in new_dead]
        diff["new_circular_dependencies"] = [item for item in cycles if tuple(item["cycle"]) not in old_cycles]
        diff["new_complexity_hotspots"] = [item for item in hotspots
                                          if item["cyclomatic_complexity"] > old_hot.get(symbol_key(item), 0)]
        diff["regression_detected"] = any(diff[key] for key in
                                          ("new_dead_code", "new_circular_dependencies", "new_complexity_hotspots"))
    return diff


def _parse_polyglot_file(source: str, rel: str, module: str, ext: str) -> dict[str, Any]:
    """Extract declarations, imports, inheritance, and call facts for non-Python files."""
    masked = _mask_comments_and_strings(source)
    declarations: list[dict[str, Any]] = []
    raw_imports: list[str] = []
    raw_inherits: list[tuple[str, str]] = []
    raw_calls: list[tuple[str, str, int]] = []

    if ext in _TS_JS_EXTS:
        # 1. Imports: import ... from 'target' or require('target')
        for m in re.finditer(r"""\bfrom\s+['"]([^'"]+)['"]|\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)""", source):
            imp = (m.group(1) or m.group(2) or "").strip()
            if imp:
                raw_imports.append(imp)

        # 2. Classes & Interfaces
        class_spans: list[tuple[str, int, int]] = []
        for m in re.finditer(
            r"\b(?:export\s+)?(?:default\s+)?(?:abstract\s+)?(class|interface)\s+([A-Za-z_]\w*)"
            r"(?:\s*<[^{;>]*>)?"
            r"(?:\s+extends\s+([A-Za-z_][\w.]*)(?:\s*<[^{;>]*>)?)?"
            r"(?:\s+implements\s+([A-Za-z_][\w.,\s]*?[A-Za-z0-9_]))?\s*\{",
            masked,
        ):
            kind = "class" if m.group(1) == "class" else "interface"
            cname = m.group(2)
            line = _line_number_at(masked, m.start())
            qname = f"{module}.{cname}"
            declarations.append({"qualified": qname, "name": cname, "kind": kind, "path": rel, "line": line, "signature": cname})
            if m.group(3):
                raw_inherits.append((qname, m.group(3).strip()))
            if m.group(4):
                for iface in m.group(4).split(","):
                    iface = iface.strip()
                    if iface:
                        raw_inherits.append((qname, iface))
            brace_open = m.end() - 1
            brace_close = _find_matching_brace(masked, brace_open)
            class_spans.append((cname, brace_open, brace_close))

            # Extract methods inside class body (including NestJS/TS decorated methods)
            body = masked[brace_open + 1 : brace_close]
            for mm in re.finditer(
                r"(?:^|\n)[ \t]*(?:@\w+(?:\([^)]*\))?\s*)*(?:(?:public|private|protected|static|async|readonly|override)\s+)*"
                r"([A-Za-z_]\w*)\s*\(",
                body,
            ):
                mname = mm.group(1)
                if mname in _KEYWORDS_IGNORE or mname == "constructor":
                    continue
                p_open = mm.end() - 1
                p_close = _find_matching_paren(body, p_open)
                if p_close < 0:
                    continue
                tail = re.match(r"\s*(?::\s*[^{;]+)?\{", body[p_close + 1 :])
                if not tail:
                    continue
                name_rel_off = mm.start(1)
                mline = _line_number_at(masked, brace_open + 1 + name_rel_off)
                clean_args = re.sub(r"@\w+(?:\([^)]*\))?\s*", "", body[p_open + 1 : p_close])
                params = ", ".join(p.strip().split(":")[0].strip() for p in clean_args.split(",") if p.strip())
                mqname = f"{qname}.{mname}"
                declarations.append({
                    "qualified": mqname,
                    "name": mname,
                    "kind": "method",
                    "path": rel,
                    "line": mline,
                    "signature": f"{mname}({params})",
                })
                m_open = brace_open + 1 + p_close + 1 + tail.end() - 1
                m_close = _find_matching_brace(masked, m_open)
                _body_metrics(declarations[-1], masked, m_open, m_close)
                for callee in _extract_calls_in_span(masked[m_open + 1 : m_close]):
                    raw_calls.append((mqname, callee, mline))

        def _in_class(pos: int) -> bool:
            return any(start <= pos <= end for _, start, end in class_spans)

        # 3. Top-level functions and arrow functions
        for m in re.finditer(
            r"\b(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_]\w*)\s*\(([^();]*)\)\s*(?::\s*[^{;]+)?\{",
            masked,
        ):
            if _in_class(m.start()):
                continue
            fname = m.group(1)
            fline = _line_number_at(masked, m.start())
            params = ", ".join(p.strip().split(":")[0].strip() for p in m.group(2).split(",") if p.strip())
            fqname = f"{module}.{fname}"
            declarations.append({
                "qualified": fqname,
                "name": fname,
                "kind": "function",
                "path": rel,
                "line": fline,
                "signature": f"{fname}({params})",
            })
            f_open = m.end() - 1
            f_close = _find_matching_brace(masked, f_open)
            _body_metrics(declarations[-1], masked, f_open, f_close)
            for callee in _extract_calls_in_span(masked[f_open + 1 : f_close]):
                raw_calls.append((fqname, callee, fline))

        for m in re.finditer(
            r"\b(?:export\s+)?(?:const|let)\s+([A-Za-z_]\w*)\s*(?::\s*[^=;\n{]+)?=\s*(?:async\s*)?\(([^();]*)\)\s*(?::\s*[^=;\n{]+)?=>\s*\{",
            masked,
        ):
            if _in_class(m.start()):
                continue
            fname = m.group(1)
            fline = _line_number_at(masked, m.start())
            params = ", ".join(p.strip().split(":")[0].strip() for p in m.group(2).split(",") if p.strip())
            fqname = f"{module}.{fname}"
            declarations.append({
                "qualified": fqname,
                "name": fname,
                "kind": "function",
                "path": rel,
                "line": fline,
                "signature": f"{fname}({params})",
            })
            f_open = m.end() - 1
            f_close = _find_matching_brace(masked, f_open)
            _body_metrics(declarations[-1], masked, f_open, f_close)
            for callee in _extract_calls_in_span(masked[f_open + 1 : f_close]):
                raw_calls.append((fqname, callee, fline))

        # Expression-bodied arrows are common request entry points in frontends.
        for m in re.finditer(
            r"\b(?:export\s+)?(?:const|let)\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?"
            r"(?:\([^();\n]*\)|[A-Za-z_]\w*)\s*=>\s*(?![\s{])([^;\n]+)", masked,
        ):
            if _in_class(m.start()):
                continue
            fname = m.group(1)
            qname = f"{module}.{fname}"
            if any(d["qualified"] == qname for d in declarations):
                continue
            line = _line_number_at(masked, m.start())
            declarations.append(dict(qualified=qname, name=fname, kind="function", path=rel,
                line=line, signature=f"{fname}()", loc=1, cyclomatic_complexity=1, max_nesting_depth=0,
                body_start=m.start(2), body_end=m.end(2)))
            raw_calls.extend((qname, called, line) for called in _extract_calls_in_span(m.group(2)))

    elif ext in _GO_EXTS:
        for m in re.finditer(r'"([^"]+)"', source):
            # Go import lines
            line_prefix = source[max(0, m.start() - 80) : m.start()]
            if "import" in line_prefix or "\n\t" in line_prefix:
                raw_imports.append(m.group(1))
        for m in re.finditer(r"\btype\s+([A-Za-z_]\w*)\s+(struct|interface)\s*\{", masked):
            tname = m.group(1)
            kind = "class" if m.group(2) == "struct" else "interface"
            line = _line_number_at(masked, m.start())
            declarations.append({"qualified": f"{module}.{tname}", "name": tname, "kind": kind, "path": rel, "line": line, "signature": tname})
        for m in re.finditer(r"\bfunc\s+(?:\(\s*\w+\s+\*?([A-Za-z_]\w*)\s*\)\s*)?([A-Za-z_]\w*)\s*\(([^)]*)\)[^{;\n]*\{", masked):
            recv = m.group(1)
            fname = m.group(2)
            line = _line_number_at(masked, m.start())
            qname = f"{module}.{recv}.{fname}" if recv else f"{module}.{fname}"
            kind = "method" if recv else "function"
            declarations.append({"qualified": qname, "name": fname, "kind": kind, "path": rel, "line": line, "signature": f"{fname}()"})
            f_open = m.end() - 1
            f_close = _find_matching_brace(masked, f_open)
            _body_metrics(declarations[-1], masked, f_open, f_close)
            for callee in _extract_calls_in_span(masked[f_open + 1 : f_close]):
                raw_calls.append((qname, callee, line))

    elif ext in _RUST_EXTS:
        for m in re.finditer(r"\buse\s+([A-Za-z0-9_:]+)", masked):
            raw_imports.append(m.group(1).replace("::", "."))
        for m in re.finditer(r"\b(?:pub(?:\([^)]*\))?\s+)?(struct|enum|trait)\s+([A-Za-z_]\w*)", masked):
            kind = "class" if m.group(1) in {"struct", "enum"} else "interface"
            tname = m.group(2)
            line = _line_number_at(masked, m.start())
            declarations.append({"qualified": f"{module}.{tname}", "name": tname, "kind": kind, "path": rel, "line": line, "signature": tname})
        impl_spans: list[tuple[str, int, int]] = []
        for im in re.finditer(r"\bimpl(?:\s*<[^>]*>)?\s+(?:[A-Za-z0-9_:<>]+\s+for\s+)?([A-Za-z_]\w*)\s*\{", masked):
            i_open = im.end() - 1
            i_close = _find_matching_brace(masked, i_open)
            impl_spans.append((im.group(1), i_open, i_close))
        for m in re.finditer(r"\b(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\(([^)]*)\)[^{;]*\{", masked):
            fname = m.group(1)
            line = _line_number_at(masked, m.start())
            enclosing_impl = next((sname for sname, s_open, s_close in impl_spans if s_open < m.start() < s_close), "")
            qname = f"{module}.{enclosing_impl}.{fname}" if enclosing_impl else f"{module}.{fname}"
            kind = "method" if enclosing_impl else "function"
            declarations.append({"qualified": qname, "name": fname, "kind": kind, "path": rel, "line": line, "signature": f"{fname}()"})
            f_open = m.end() - 1
            f_close = _find_matching_brace(masked, f_open)
            _body_metrics(declarations[-1], masked, f_open, f_close)
            for callee in _extract_calls_in_span(masked[f_open + 1 : f_close]):
                raw_calls.append((qname, callee.replace("::", "."), line))

    elif ext in _CS_JAVA_CPP_EXTS:
        for m in re.finditer(r"\b(?:using|import)\s+([A-Za-z0-9_.]+)\s*;", masked):
            raw_imports.append(m.group(1))
        for m in re.finditer(
            r"\b(class|interface|struct)\s+([A-Za-z_]\w*)"
            r"(?:\s*<[^{;>]*>)?"
            r"(?:\s*:\s*([A-Za-z_][\w.<>,\s]*?[A-Za-z0-9_>]))?"
            r"(?:\s+extends\s+([A-Za-z_][\w.<>]*))?"
            r"(?:\s+implements\s+([A-Za-z_][\w.<>,\s]*?[A-Za-z0-9_>]))?"
            r"(?:\s+where\s+[^{;]+?)?\s*\{",
            masked,
        ):
            kind = "interface" if m.group(1) == "interface" else "class"
            cname = m.group(2)
            line = _line_number_at(masked, m.start())
            qname = f"{module}.{cname}"
            declarations.append({"qualified": qname, "name": cname, "kind": kind, "path": rel, "line": line, "signature": cname})
            base_str = ",".join(part for part in (m.group(3), m.group(4), m.group(5)) if part)
            for b in base_str.split(","):
                b = b.strip()
                if b:
                    raw_inherits.append((qname, b))
            c_open = m.end() - 1
            c_close = _find_matching_brace(masked, c_open)
            body = masked[c_open + 1 : c_close]
            for mm in re.finditer(
                r"\b(?:(?:public|private|protected|internal|static|virtual|override|async|sealed|abstract|extern|unsafe|new|partial|synchronized|final|default)\s+)+"
                r"(?:<[^>{};()]*>\s*)?[A-Za-z_][\w.]*(?:\s*<[^{};()]*>)?(?:\s*\[\s*\])*\??\s+"
                r"([A-Za-z_]\w*)\s*\(",
                body,
            ):
                mname = mm.group(1)
                if mname in _KEYWORDS_IGNORE or mname == cname:
                    continue
                p_open = mm.end() - 1
                p_close = _find_matching_paren(body, p_open)
                if p_close < 0:
                    continue
                tail = re.match(
                    r"\s*(?:throws\s+[A-Za-z_][\w.,\s]*?[A-Za-z0-9_])?\s*(?:where\s+[^{;]+?)?\s*\{",
                    body[p_close + 1 :],
                )
                if not tail:
                    continue
                mline = _line_number_at(masked, c_open + 1 + mm.start(1))
                mqname = f"{qname}.{mname}"
                declarations.append({"qualified": mqname, "name": mname, "kind": "method", "path": rel, "line": mline, "signature": f"{mname}()"})
                m_open = c_open + 1 + p_close + 1 + tail.end() - 1
                m_close = _find_matching_brace(masked, m_open)
                _body_metrics(declarations[-1], masked, m_open, m_close)
                for callee in _extract_calls_in_span(masked[m_open + 1 : m_close]):
                    raw_calls.append((mqname, callee, mline))

    exports: set[str] = set()
    explicit_exports = set(re.findall(
        r"\bexport\s+(?:default\s+)?(?:abstract\s+|async\s+)?(?:function|class|interface|const|let|var)\s+(\w+)", masked))
    rust_exports = set(re.findall(
        r"\bpub(?:\([^)]*\))?\s+(?:async\s+)?(?:fn|struct|enum|trait)\s+(\w+)", masked))
    for item in declarations:
        if (ext in _TS_JS_EXTS and item["name"] in explicit_exports
                or ext in _RUST_EXTS and item["name"] in rust_exports
                or ext in _GO_EXTS and item["name"][0].isupper()):
            exports.add(item["name"])
    if ext in _TS_JS_EXTS:
        for match in re.finditer(r"\bexport\s*\{([^}]+)\}", masked):
            exports.update(re.findall(r"(?:^|,)\s*(\w+)", match.group(1)))
    return {
        "declarations": declarations,
        "raw_imports": raw_imports,
        "raw_inherits": raw_inherits,
        "raw_calls": raw_calls,
        "diagnostics": [],
        "exports": sorted(exports),
    }


def _parse_python_file(source: str, rel: str, module: str) -> dict[str, Any]:
    """Extract raw AST facts for a single Python file (cacheable before cross-file resolution)."""
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError as exc:
        return {
            "declarations": [],
            "raw_imports": [],
            "raw_inherits": [],
            "raw_calls": [],
            "diagnostics": [{"path": rel, "reason": "syntax_error", "line": exc.lineno or 1}],
        }

    declarations: list[dict[str, Any]] = []
    raw_imports: list[str] = []
    raw_inherits: list[tuple[str, str]] = []
    raw_calls: list[tuple[str, str, int]] = []
    diagnostics: list[dict[str, Any]] = []
    exports: set[str] = set()
    import_bindings: dict[str, str] = {}

    class _RawExtractor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []
            self.current: str | None = None
            self.metrics: dict[str, Any] | None = None
            self.depth = 0

        def visit(self, node: ast.AST) -> Any:
            control = isinstance(node, (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While,
                                       ast.ExceptHandler, ast.With, ast.AsyncWith,
                                       ast.comprehension, ast.match_case))
            metrics = self.metrics
            if metrics is not None:
                if control:
                    metrics["cyclomatic_complexity"] += 1
                    if isinstance(node, ast.comprehension):
                        metrics["cyclomatic_complexity"] += len(node.ifs)
                elif isinstance(node, ast.BoolOp):
                    metrics["cyclomatic_complexity"] += len(node.values) - 1
            if control:
                self.depth += 1
                if metrics is not None:
                    metrics["max_nesting_depth"] = max(metrics["max_nesting_depth"], self.depth)
            result = super().visit(node)
            if control:
                self.depth -= 1
            return result

        def _qual(self, name: str) -> str:
            return ".".join([module, *self.scope, name])

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            qname = self._qual(node.name)
            declarations.append({
                "qualified": qname,
                "name": node.name,
                "kind": "class",
                "path": rel,
                "line": getattr(node, "lineno", 1),
                "signature": "",
            })
            for base in node.bases:
                bname = _attribute_name(base)
                if bname:
                    raw_inherits.append((qname, bname))
            prev = self.current
            prev_metrics, prev_depth = self.metrics, self.depth
            self.metrics, self.depth = None, 0
            self.scope.append(node.name)
            self.current = qname
            self.generic_visit(node)
            self.scope.pop()
            self.current = prev
            self.metrics, self.depth = prev_metrics, prev_depth

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            sig = f"{node.name}({ast.unparse(node.args)})"
            if node.returns:
                sig += f" -> {ast.unparse(node.returns)}"
            qname = self._qual(node.name)
            declarations.append({
                "qualified": qname,
                "name": node.name,
                "kind": "method" if self.scope else "function",
                "path": rel,
                "line": getattr(node, "lineno", 1),
                "signature": sig,
                "cyclomatic_complexity": 1,
                "loc": (node.end_lineno or node.lineno) - node.lineno + 1,
                "max_nesting_depth": 0,
            })
            prev = self.current
            prev_metrics, prev_depth = self.metrics, self.depth
            self.metrics, self.depth = declarations[-1], 0
            self.scope.append(node.name)
            self.current = qname
            self.generic_visit(node)
            self.scope.pop()
            self.current = prev
            self.metrics, self.depth = prev_metrics, prev_depth

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Assign(self, node: ast.Assign) -> None:
            names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            self._exports(names, node.value)
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            names = {node.target.id} if isinstance(node.target, ast.Name) else set()
            self._exports(names, node.value)
            self.generic_visit(node)

        visit_AugAssign = visit_AnnAssign

        def _exports(self, names: set[str], value: ast.AST | None) -> None:
            if self.scope or value is None or not names & {"__all__", "TOOLS", "TOOLS_SCHEMA"}:
                return
            for child in ast.walk(value):
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    exports.add(child.value)
                elif "__all__" not in names and isinstance(child, (ast.Name, ast.Attribute)):
                    exports.add(_attribute_name(child))

        def visit_Call(self, node: ast.Call) -> None:
            if self.current:
                called = _attribute_name(node.func)
                line = getattr(node, "lineno", 1)
                if not called:
                    diagnostics.append({"path": rel, "reason": "dynamic_call", "line": line})
                else:
                    raw_calls.append((self.current, called, line))
            self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                raw_imports.append(alias.name)
                import_bindings[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
            self.generic_visit(node)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            target = node.module or ""
            if node.level:
                package = module.split(".")
                if Path(rel).stem != "__init__":
                    package = package[:-1]
                if node.level > len(package):
                    diagnostics.append({"path": rel, "reason": "unresolved_relative_import", "line": getattr(node, "lineno", 1)})
                    return
                parent = package[: len(package) - node.level + 1]
                target = ".".join([*parent, *([target] if target else [])])
            for alias in node.names:
                imported = f"{target}.{alias.name}".strip(".")
                raw_imports.append(imported)
                import_bindings[alias.asname or alias.name] = imported
            self.generic_visit(node)

    _RawExtractor().visit(tree)
    return {
        "declarations": declarations,
        "raw_imports": raw_imports,
        "raw_inherits": raw_inherits,
        "raw_calls": raw_calls,
        "diagnostics": diagnostics,
        "exports": sorted(exports),
        "import_bindings": import_bindings,
    }


def _resolve_relative_ts_import(from_rel: str, imp: str) -> str:
    if not imp.startswith("."):
        return imp.replace("/", ".")
    base_dir = Path(from_rel).parent
    joined = (base_dir / imp).as_posix()
    # Normalize ./ and ../ segments
    parts: list[str] = []
    for seg in joined.split("/"):
        if seg in {"", "."}:
            continue
        if seg == "..":
            if parts:
                parts.pop()
        else:
            parts.append(seg)
    if parts and Path(parts[-1]).suffix.lower() in _POLYGLOT_EXTENSIONS:
        parts[-1] = Path(parts[-1]).stem
    if parts and parts[-1] == "index" and len(parts) > 1:
        parts.pop()
    return ".".join(parts)


def _is_test_rel_path(rel_path: str) -> bool:
    p = Path(rel_path)
    return (
        any(part.lower() in {"test", "tests", "__tests__"} for part in p.parts)
        or p.name.startswith("test_")
        or p.stem.endswith(("_test", "Test", "Tests"))
        or ".test." in p.name
        or ".spec." in p.name
    )


class _CodeGraph:
    """Unified graph engine supporting legacy python_ast mode and polyglot + SQLite incremental cache mode."""

    def __init__(self, root: Path, polyglot: bool = True) -> None:
        self.root = root
        self.polyglot = polyglot
        self.mode = "polyglot" if polyglot else "python_ast"
        self.files: dict[str, dict[str, Any]] = {}
        self.symbols: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self.boundary_analysis: dict[str, Any] = {}
        self.boundary_endpoints: list[dict[str, Any]] = []
        self._bases: dict[str, list[str]] = defaultdict(list)
        self.diagnostics: list[dict[str, Any]] = []
        self._by_short: dict[str, list[str]] = defaultdict(list)
        self.cache_stats: dict[str, Any] = {"total_files": 0, "cache_hits": 0, "re_parsed": 0}
        self._parse()

    def _mark_incomplete(self, path: str, reason: str, **details: Any) -> None:
        item: dict[str, Any] = {"path": path, "reason": reason}
        item.update(details)
        self.diagnostics.append(item)

    def analysis(self) -> dict[str, Any]:
        return {
            "complete": not self.diagnostics,
            "incomplete": list(self.diagnostics),
            "cache": self.cache_stats,
            "cross_boundary": self.boundary_analysis,
        }

    def _add_symbol(self, qualified: str, item: dict[str, Any]) -> None:
        if qualified in self.symbols:
            return
        self.symbols[qualified] = item
        self._by_short[item["name"]].append(qualified)

    def resolve_protocol(self, query: str) -> list[str]:
        matched = match_protocol_query(query, self.boundary_endpoints)
        return sorted({ep["handler"] for ep in matched if ep.get("handler") in self.symbols})

    def _parse(self) -> None:
        cache = _IncrementalCache()
        root_key = self.root.as_posix()
        cache_mode = self.mode + ":2.3.0"
        cached_entries = cache.load_project(root_key, cache_mode)
        seen_paths: set[str] = set()
        upserts: list[tuple[str, str, int, int, dict[str, Any]]] = []
        file_payloads: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []

        allowed_exts = (_POLYGLOT_EXTENSIONS | CONTRACT_EXTENSIONS) if self.polyglot else _LEGACY_EXTENSIONS

        for path in _files(self.root, allowed_exts):
            rel = _relative(path, self.root)
            seen_paths.add(rel)
            module = _module(path, self.root)
            ext = path.suffix.lower()
            raw_source: str | bytes
            try:
                stat = path.stat()
                mtime_ns = stat.st_mtime_ns
                size_bytes = stat.st_size
                if ext == ".scip":
                    raw_bytes = path.read_bytes()
                    digest = hashlib.sha256(raw_bytes).hexdigest()[:16]
                    raw_source = raw_bytes
                else:
                    source_text = path.read_text(encoding="utf-8")
                    digest = hashlib.sha256(source_text.encode("utf-8", "ignore")).hexdigest()[:16]
                    raw_source = source_text
            except (OSError, UnicodeError):
                self._mark_incomplete(rel, "unreadable")
                continue

            record = {
                "path": rel,
                "module": module,
                "language": ext.lstrip("."),
                "sha256": digest,
                "symbols": [],
            }
            self.files[rel] = record

            if not self.polyglot and ext not in _PYTHON_EXTS:
                continue

            self.cache_stats["total_files"] += 1
            cached = cached_entries.get(rel)
            if cached and cached.get("sha256") == digest:
                payload = cached["payload"]
                self.cache_stats["cache_hits"] += 1
            else:
                if ext in _PYTHON_EXTS and isinstance(raw_source, str):
                    payload = _parse_python_file(raw_source, rel, module)
                elif ext in CONTRACT_EXTENSIONS:
                    payload = {"declarations": []}
                else:
                    payload = _parse_polyglot_file(str(raw_source), rel, module, ext)
                if self.polyglot:
                    payload["boundary"] = extract_facts(raw_source, rel, module, ext, payload["declarations"])
                self.cache_stats["re_parsed"] += 1
                upserts.append((rel, digest, mtime_ns, size_bytes, payload))

            file_payloads.append((module, rel, record, payload))

        stale_paths = set(cached_entries.keys()) - seen_paths
        cache.save_updates(root_key, cache_mode, upserts, stale_paths)

        # Pass 1: Register all local source declarations across all files first
        for _module_name, _rel, record, payload in file_payloads:
            file_syms: list[str] = []
            for decl in payload.get("declarations", []):
                qname = decl["qualified"]
                self._add_symbol(
                    qname,
                    {
                        "name": decl["name"],
                        "kind": decl["kind"],
                        "path": decl["path"],
                        "line": decl["line"],
                        "signature": decl.get("signature", ""),
                        **{key: decl[key] for key in ("cyclomatic_complexity", "loc", "max_nesting_depth") if key in decl},
                    },
                )
                file_syms.append(qname)
            record["symbols"] = file_syms
            record["exports"] = payload.get("exports", [])
            for diag in payload.get("diagnostics", []):
                self.diagnostics.append(dict(diag))

        # Pass 1b: Project external cross-repository SCIP symbols only when absent locally
        if self.polyglot:
            for _module_name, _rel, record, payload in file_payloads:
                for s_item in payload.get("boundary", {}).get("scip_symbols", []):
                    local_cand = s_item["local_candidate"]
                    if local_cand in self.symbols or any(
                        k.endswith("." + local_cand) and not v.get("external") for k, v in self.symbols.items()
                    ):
                        continue
                    ext_q = s_item["external_qualified"]
                    self._add_symbol(
                        ext_q,
                        {
                            "name": s_item["name"],
                            "kind": s_item["kind"],
                            "path": s_item["path"],
                            "line": s_item["line"],
                            "signature": f"[SCIP:{s_item.get('external_path', '')}] {s_item['name']}()",
                            "external": True,
                            "scip_symbol": s_item["scip_symbol"],
                            "external_repo": s_item.get("external_repo", ""),
                            "external_path": s_item.get("external_path", ""),
                        },
                    )
                    if ext_q not in record["symbols"]:
                        record["symbols"].append(ext_q)

        # Pass 2a: Resolve inheritance hierarchy across the repository first
        for module_name, _rel, _record, payload in file_payloads:
            bindings = payload.get("import_bindings", {})
            for from_q, base_name in payload.get("raw_inherits", []):
                targets = self._resolve_reference(base_name, from_q, module_name, bindings)
                resolved_base = targets[0] if targets else base_name
                self._bases[from_q].append(resolved_base)
                self.edges.append({"from": from_q, "to": resolved_base, "type": "inherits"})

        # Pass 2b: Resolve calls and imports across the repository
        for module_name, rel, record, payload in file_payloads:
            is_py = Path(rel).suffix.lower() in _PYTHON_EXTS
            bindings = payload.get("import_bindings", {})
            network_calls = {tuple(item) for item in payload.get("boundary", {}).get("suppress_calls", [])}
            for from_q, called, line in payload.get("raw_calls", []):
                if (from_q, called) in network_calls:
                    continue
                targets = self._resolve_reference(called, from_q, module_name, bindings)
                if not targets and is_py:
                    self._mark_incomplete(rel, "unresolved_call", line=line, name=called)
                self.edges.append({"from": from_q, "to": targets[0] if targets else called, "type": "calls"})

            for imp in payload.get("raw_imports", []):
                norm_imp = imp if is_py else _resolve_relative_ts_import(rel, imp)
                self.edges.append({"from": module_name, "to": norm_imp, "type": "imports"})

        if self.polyglot:
            boundary_edges, self.boundary_analysis, self.boundary_endpoints = link_boundaries(
                [p for _, _, _, p in file_payloads], self.symbols
            )
            self.edges.extend(boundary_edges)
            self.diagnostics.extend(self.boundary_analysis["diagnostics"])

    def _resolve_self_member(self, class_q: str, member: str) -> list[str]:
        queue: deque[str] = deque([class_q])
        seen: set[str] = set()
        while queue:
            curr = queue.popleft()
            if curr in seen:
                continue
            seen.add(curr)
            candidate = f"{curr}.{member}"
            if candidate in self.symbols:
                return [candidate]
            for base in self._bases.get(curr, []):
                if base in self.symbols:
                    queue.append(base)
        return []

    def _resolve_reference(self, value: str, owner: str, module: str, bindings: dict[str, str]) -> list[str]:
        head, _, tail = value.partition(".")
        imported = bindings.get(head)
        if imported:
            qualified = imported + ("." + tail if tail else "")
            if qualified in self.symbols:
                return [qualified]
            # If head is explicitly bound to an external/stdlib import, never guess unrelated local short names
            imp_root = imported.split(".")[0]
            known_modules = {item["module"] for item in self.files.values()}
            if not any(
                m == imp_root or m.startswith(imp_root + ".") or m.endswith("." + imp_root) or f".{imp_root}." in m
                for m in known_modules
            ):
                return []
        if head in {"self", "cls", "this"}:
            return self._resolve_self_member(owner.rsplit(".", 1)[0], tail)
        scope = owner.rsplit(".", 1)[0]
        while scope == module or scope.startswith(module + "."):
            candidate = scope + "." + value
            if candidate in self.symbols:
                return [candidate]
            scope = scope.rsplit(".", 1)[0] if "." in scope else ""
        candidates = self.resolve(value, allow_substring=False)
        owner_path = self.symbols.get(owner, {}).get("path", "")
        if owner_path and not _is_test_rel_path(owner_path):
            candidates = [c for c in candidates if not _is_test_rel_path(self.symbols[c]["path"])]
        return candidates

    def resolve(self, value: str, allow_substring: bool = True) -> list[str]:
        query = value.strip()
        if query in self.symbols:
            return [query]
        short = query.rsplit(".", 1)[-1]
        exact = self._by_short.get(short, [])
        if exact:
            return exact
        if not allow_substring:
            return []
        return [key for key in self.symbols if query.lower() in key.lower()]

    def _rank_network(self) -> dict[str, dict[str, int]]:
        modules = {item["module"] for item in self.files.values()}
        network: dict[str, dict[str, int]] = {key: {} for key in sorted(set(self.symbols) | modules)}
        for edge in self.edges:
            left, right = edge["from"], edge["to"]
            # Imports may name a member or a package below a known module.
            while right not in network and "." in right:
                right = right.rsplit(".", 1)[0]
            if left in network and right in network:
                network[left][right] = network[left].get(right, 0) + 1
        # Project cross-file calls/inheritance onto module nodes as well.
        for edge in self.architecture()["edges"]:
            if edge["type"] in {"calls", "inherits"}:
                left = self.files[edge["from"]]["module"]
                right = self.files[edge["to"]]["module"]
                network[left][right] = network[left].get(right, 0) + 1
        return network

    def pagerank(self, focus_files: list[str] | None = None,
                 focus_symbols: list[str] | None = None) -> dict[str, float]:
        """Weighted personalized PageRank, damping .85 and exactly 20 iterations."""
        network = self._rank_network()
        if not network:
            return {}
        prior = dict.fromkeys(network, 1.0)
        for path in focus_files or []:
            normalized = str(path).replace("\\", "/")
            if Path(path).is_absolute():
                try:
                    normalized = Path(path).relative_to(self.root).as_posix()
                except ValueError:
                    continue
            if normalized.startswith("./"):
                normalized = normalized[2:]
            for key in self.files.get(normalized, {}).get("symbols", []):
                prior[key] += 20.0
            module = self.files.get(normalized, {}).get("module")
            if module in prior:
                prior[module] += 20.0
        for query in focus_symbols or []:
            for key in self.resolve(query, allow_substring=False):
                prior[key] += 20.0
        total = sum(prior.values())
        prior = {key: value / total for key, value in prior.items()}
        scores = dict(prior)
        for _ in range(20):
            dangling = sum(scores[key] for key, out in network.items() if not out)
            next_scores = {key: (0.15 + 0.85 * dangling) * value for key, value in prior.items()}
            for key, out in network.items():
                weight = sum(out.values())
                for target, count in out.items():
                    next_scores[target] += 0.85 * scores[key] * count / weight
            scores = next_scores
        return scores

    def _digest(self, symbols: Iterable[str], token_budget: int) -> str:
        """Keep complete module/signature lines within a UTF-8 byte-token bound."""
        groups: dict[str, list[str]] = {}
        used = 0
        for symbol in symbols:
            item = self.symbols.get(symbol)
            if item is None:
                continue
            path = item["path"]
            header = f"{path}\n" if path not in groups else ""
            signature = item.get("signature") or f"{item['kind']} {item['name']}"
            line = f"  |- {signature} :{item['line']}\n"
            cost = _estimated_tokens(header + line)
            if used + cost > token_budget:
                continue
            groups.setdefault(path, []).append(line)
            used += cost
        return "".join(path + "\n" + "".join(lines) for path, lines in groups.items())

    def repomap(self, focus_files: list[str] | None, focus_symbols: list[str] | None,
                token_budget: int) -> dict[str, Any]:
        if token_budget < 0:
            raise ValueError("token_budget must be non-negative")
        scores = self.pagerank(focus_files, focus_symbols)
        network = self._rank_network()
        incoming: dict[str, set[str]] = defaultdict(set)
        for left, out in network.items():
            for right in out:
                incoming[right].add(left)
        ordered = sorted(self.symbols, key=lambda key: (-scores[key], key))
        ranked = [{"symbol": key, **{field: self.symbols[key][field] for field in
                   ("kind", "path", "line", "signature")}, "pagerank": scores[key],
                   "in_degree": len(incoming[key]), "out_degree": len(network[key])} for key in ordered]
        modules = [{"module": item["module"], "path": path, "pagerank": scores[item["module"]],
                    "in_degree": len(incoming[item["module"]]), "out_degree": len(network[item["module"]])}
                   for path, item in self.files.items()]
        modules.sort(key=lambda item: (-item["pagerank"], item["path"]))
        markdown = self._digest(ordered, token_budget)
        return {"token_budget": token_budget, "estimated_tokens": _estimated_tokens(markdown),
                "compression_vs_full_graph_pct": _compression_pct(markdown, self._full_graph()),
                "ranked_symbols": ranked, "ranked_modules": modules, "repomap_markdown": markdown}

    def _full_graph(self) -> dict[str, Any]:
        return {"files": self.files, "symbols": self.symbols, "edges": self.edges}

    def _cycles(self) -> list[dict[str, Any]]:
        """Tarjan SCC followed by canonical enumeration of directed simple cycles."""
        adjacency: dict[str, set[str]] = {item["module"]: set() for item in self.files.values()}
        for edge in self.architecture()["edges"]:
            left, right = self.files[edge["from"]]["module"], self.files[edge["to"]]["module"]
            if left != right:
                adjacency[left].add(right)
        indices: dict[str, int] = {}
        low: dict[str, int] = {}
        stack: list[str] = []
        active: set[str] = set()
        components: list[set[str]] = []

        def connect(node: str) -> None:
            indices[node] = low[node] = len(indices)
            stack.append(node)
            active.add(node)
            for neighbor in sorted(adjacency[node]):
                if neighbor not in indices:
                    connect(neighbor)
                    low[node] = min(low[node], low[neighbor])
                elif neighbor in active:
                    low[node] = min(low[node], indices[neighbor])
            if low[node] == indices[node]:
                component: set[str] = set()
                while True:
                    member = stack.pop()
                    active.remove(member)
                    component.add(member)
                    if member == node:
                        break
                if len(component) > 1:
                    components.append(component)

        for node in sorted(adjacency):
            if node not in indices:
                connect(node)
        cycles: list[dict[str, Any]] = []
        for component in components:
            for start in sorted(component):
                path, visited = [start], {start}

                def walk(node: str) -> None:
                    for neighbor in sorted(adjacency[node] & component):
                        if neighbor == start:
                            cycles.append({"cycle": path + [start], "length": len(path)})
                        elif neighbor > start and neighbor not in visited:
                            visited.add(neighbor)
                            path.append(neighbor)
                            walk(neighbor)
                            path.pop()
                            visited.remove(neighbor)

                walk(start)
        return sorted(cycles, key=lambda item: item["cycle"])

    def health(self, entry_points: list[str] | None, complexity_threshold: int,
               baseline_path: str | None, save_baseline: bool) -> dict[str, Any]:
        protected: set[str] = set()
        for name in entry_points or []:
            protected.update(self.resolve(name, allow_substring=False))
        used = {edge["to"] for edge in self.edges
                if edge["type"] in {"calls", "inherits", "imports"} and edge["from"] != edge["to"]}
        dead, hotspots = [], []
        for key, item in sorted(self.symbols.items()):
            name, path = item["name"], Path(item["path"])
            exports = self.files[item["path"]].get("exports", [])
            test_file = (any(part.lower() in {"test", "tests", "__tests__"} for part in path.parts)
                         or path.name.startswith("test_") or path.stem.endswith(("_test", "Test", "Tests"))
                         or ".test." in path.name or ".spec." in path.name)
            exempt = (test_file or item.get("external") or name.startswith(("test_", "setUp", "tearDown", "visit_"))
                      or "Test" in name or name in {"main", "dispatch", "call_tool", "handle_tool_call", "cli"}
                      or name.startswith("__") and name.endswith("__")
                      or key in protected or name in exports or key in exports)
            if item["kind"] in {"function", "method", "class"} and key not in used and not exempt:
                dead.append({"symbol": key, **{field: item[field] for field in ("kind", "path", "line", "signature")},
                             "reason": "No incoming call, inheritance or explicit cross-module import"})
            if item["kind"] in {"function", "method"} and item.get("cyclomatic_complexity", 1) >= complexity_threshold:
                hotspots.append({"symbol": key, **{field: item[field] for field in
                                 ("kind", "path", "line", "cyclomatic_complexity", "loc", "max_nesting_depth")}})
        hotspots.sort(key=lambda item: (-item["cyclomatic_complexity"], item["symbol"]))
        cycles = self._cycles()
        baseline_diff = _health_baseline(self.root, dead, cycles, hotspots, baseline_path, save_baseline)
        # Bounded, deterministic gate: two points per unused symbol, ten per cycle,
        # and one per branch at/above the chosen hotspot threshold.
        penalty = 2 * len(dead) + 10 * len(cycles) + sum(
            max(1, item["cyclomatic_complexity"] - complexity_threshold + 1) for item in hotspots)
        score = max(0, 100 - penalty)
        counts = {"dead_code": len(dead), "circular_dependencies": len(cycles), "complexity_hotspots": len(hotspots),
                  "total_symbols": len(self.symbols), "total_modules": len(self.files)}
        summary = (f"Static code health: {score}/100\n\n"
                   f"- Dead code: {len(dead)}\n- Module cycles: {len(cycles)}\n"
                   f"- Complexity hotspots: {len(hotspots)}\n"
                   f"- Baseline regression: {baseline_diff['regression_detected']}\n")
        return {"health_score": score, "dead_code": dead, "circular_dependencies": cycles,
                "complexity_hotspots": hotspots, "counts": counts,
                "baseline_diff": baseline_diff, "summary_markdown": summary}

    def search(self, query: str = "", kind: str | None = None, max_results: int = 50) -> list[dict[str, Any]]:
        needle = query.strip().lower()
        results: list[dict[str, Any]] = []
        if kind in {"endpoint", "contract"}:
            proto_hits = match_protocol_query(query, self.boundary_endpoints) if needle else list(self.boundary_endpoints)
            for ep in proto_hits or self.boundary_endpoints:
                hay = f"{ep.get('contract_id', '')} {ep.get('scip_symbol', '')} {ep.get('handler', '')} {ep['path']}".lower()
                if needle and ep not in proto_hits and needle not in hay:
                    continue
                results.append({
                    "qualified_name": ep.get("contract_id", ep["handler"]),
                    "name": ep.get("route", ep.get("method", "")),
                    "kind": "endpoint",
                    "path": ep["path"],
                    "line": ep["line"],
                    "signature": ep.get("contract_id", ""),
                    "handler": ep["handler"],
                    "contract_id": ep.get("contract_id", ""),
                    "scip_symbol": ep.get("scip_symbol", ""),
                })
            return results[: max(1, min(int(max_results), 200))]
        for qualified, item in self.symbols.items():
            if kind and item["kind"] != kind:
                continue
            haystack = f"{qualified} {item['path']} {item.get('signature', '')}".lower()
            if needle and needle not in haystack:
                continue
            results.append({"qualified_name": qualified, **item})
        if not kind or kind == "file":
            for rel, item in self.files.items():
                if needle and needle not in rel.lower():
                    continue
                if any(result["path"] == rel for result in results):
                    continue
                results.append({"kind": "file", "name": Path(rel).stem, "qualified_name": _module(self.root / rel, self.root), **item})
        if not results and not kind and needle:
            for ep in match_protocol_query(query, self.boundary_endpoints):
                h_sym = ep.get("handler", "")
                if h_sym in self.symbols and not any(r.get("qualified_name") == h_sym for r in results):
                    results.append({"qualified_name": h_sym, **self.symbols[h_sym],
                                    "contract_id": ep.get("contract_id", ""), "scip_symbol": ep.get("scip_symbol", "")})
        results.sort(key=lambda item: (item["kind"] != "file", item["path"], item.get("line", 0), item["name"]))
        return results[: max(1, min(int(max_results), 200))]

    def trace(self, symbol: str, direction: str = "both") -> dict[str, Any]:
        targets = set(self.resolve(symbol))
        matched_contracts: set[str] = set()
        if not targets:
            matched_eps = match_protocol_query(symbol, self.boundary_endpoints)
            targets = {ep["handler"] for ep in matched_eps if ep.get("handler") in self.symbols}
            matched_contracts = {ep["contract_id"] for ep in matched_eps if ep.get("contract_id")}
        if not targets:
            return {"symbol": symbol, "resolved": [], "callers": [], "callees": [], "edges": [],
                    "cross_boundary_edges": [], "pagerank_scores": {}, "compact_trace_markdown": ""}
        callers: set[str] = set()
        callees: set[str] = set()
        selected: list[dict[str, Any]] = []
        for edge in self.edges:
            if edge["type"] != "calls":
                continue
            if edge["to"] in targets and direction != "callees":
                if matched_contracts and "boundary" in edge and edge.get("contract_id") not in matched_contracts:
                    continue
                callers.add(edge["from"])
                selected.append(edge)
            if edge["from"] in targets and direction != "callers":
                callees.add(edge["to"])
                selected.append(edge)
        if direction == "callers":
            callees.clear()
        elif direction == "callees":
            callers.clear()
        scores = self.pagerank(focus_symbols=sorted(targets))
        relevant = targets | callers | callees
        ordered = sorted(relevant, key=lambda key: (-scores.get(key, 0.0), key))
        compact = self._digest(ordered, 2000)
        compact += "".join(
            f"{edge['from']} -> {edge['to']} [{edge.get('contract_id', edge['boundary'])}]\n"
            if "boundary" in edge
            else f"{edge['from']} -> {edge['to']}\n"
            for edge in selected
        )
        cross_edges = [edge for edge in selected if "boundary" in edge]
        return {"symbol": symbol, "resolved": sorted(targets), "callers": sorted(callers), "callees": sorted(callees),
                "edges": selected, "cross_boundary_edges": cross_edges,
                "pagerank_scores": {key: scores.get(key, 0.0) for key in ordered},
                "compact_trace_markdown": compact}

    def impact(self, paths: list[str] | None = None, symbols: list[str] | None = None, diff: str | None = None) -> dict[str, Any]:
        seeds: set[str] = set()
        seed_files = {str(Path(item).as_posix()).lstrip("./") for item in (paths or [])}
        if diff:
            seed_files.update(re.findall(r"(?:^|\n)\+\+\+ [ab]/([^\n]+)", diff))
            seed_files.update(re.findall(r"(?:^|\n)--- [ab]/([^\n]+)", diff))
        for qualified in symbols or []:
            seeds.update(self.resolve(qualified) or self.resolve_protocol(qualified))
        for qualified, item in self.symbols.items():
            if item["path"] in seed_files:
                seeds.add(qualified)
        affected = set(seeds)
        reverse: dict[str, set[str]] = defaultdict(set)
        for edge in self.edges:
            if edge["type"] in {"calls", "inherits"}:
                reverse[edge["to"]].add(edge["from"])
        queue = deque(seeds)
        while queue:
            current = queue.popleft()
            for caller in reverse.get(current, set()):
                if caller not in affected:
                    affected.add(caller)
                    queue.append(caller)
        files = set(seed_files)
        for qualified in affected:
            if qualified in self.symbols:
                files.add(self.symbols[qualified]["path"])
        tests = sorted(
            path
            for path in files
            if Path(path).name.startswith("test_")
            or "test" in Path(path).name.lower()
            or ".spec." in Path(path).name.lower()
            or ".test." in Path(path).name.lower()
        )
        return {
            "seed_paths": sorted(seed_files),
            "seed_symbols": sorted(seeds),
            "affected_files": sorted(files),
            "affected_symbols": sorted(affected),
            "related_tests": tests,
            "edges": [edge for edge in self.edges if edge.get("from") in affected or edge.get("to") in affected],
            "counts": {"files": len(files), "symbols": len(affected), "tests": len(tests)},
        }

    def architecture(self) -> dict[str, Any]:
        nodes = [
            {
                "id": rel,
                "type": "module",
                "module": item["module"],
                "language": item["language"],
                "symbols": len(item["symbols"]),
            }
            for rel, item in sorted(self.files.items())
        ]
        by_symbol_file = {key: item["path"] for key, item in self.symbols.items()}
        by_module_file = {item["module"]: rel for rel, item in self.files.items()}
        edges: set[tuple[str, str, str]] = set()
        for edge in self.edges:
            if edge["type"] == "imports":
                left = by_module_file.get(edge["from"])
                imported = edge["to"]
                right = by_module_file.get(imported)
                while right is None and "." in imported:
                    imported = imported.rsplit(".", 1)[0]
                    right = by_module_file.get(imported)
                if left and right and left != right:
                    edges.add((left, right, edge["type"]))
                continue
            left, right = by_symbol_file.get(edge["from"]), by_symbol_file.get(edge["to"])
            if left and right and left != right:
                edges.add((left, right, edge["type"]))
        return {
            "nodes": nodes,
            "edges": [{"from": left, "to": right, "type": kind} for left, right, kind in sorted(edges)],
            "counts": {"modules": len(nodes), "relations": len(edges)},
        }

    def explore(self, query: str, max_depth: int = 2, max_nodes: int = 25) -> dict[str, Any]:
        """Token-budgeted one-shot neighborhood explorer (mitigates CodeGraph's Fat Output Trap)."""
        matched = self.search(query, max_results=min(10, max_nodes))
        seed_symbols: list[str] = []
        for item in matched:
            if item["kind"] != "file":
                seed_symbols.append(item["qualified_name"])
            else:
                seed_symbols.extend(item.get("symbols", [])[:5])
        if not seed_symbols:
            seed_symbols = (self.resolve(query) or self.resolve_protocol(query))[:5]
        scores = self.pagerank(focus_symbols=seed_symbols)

        visited: set[str] = set(seed_symbols[:max_nodes])
        frontier: deque[tuple[str, int]] = deque((sym, 0) for sym in visited)
        sub_edges: list[dict[str, str]] = []

        adj_out: dict[str, list[dict[str, str]]] = defaultdict(list)
        adj_in: dict[str, list[dict[str, str]]] = defaultdict(list)
        for edge in self.edges:
            if edge["type"] in {"calls", "inherits"}:
                adj_out[edge["from"]].append(edge)
                adj_in[edge["to"]].append(edge)

        while frontier and len(visited) < max_nodes:
            curr, depth = frontier.popleft()
            if depth >= max_depth:
                continue
            candidates = adj_out.get(curr, []) + adj_in.get(curr, [])
            candidates.sort(key=lambda edge: -scores.get(edge["to"] if edge["from"] == curr else edge["from"], 0.0))
            for edge in candidates:
                if edge not in sub_edges:
                    sub_edges.append(edge)
                neighbor = edge["to"] if edge["from"] == curr else edge["from"]
                if neighbor not in visited and len(visited) < max_nodes:
                    visited.add(neighbor)
                    frontier.append((neighbor, depth + 1))

        symbol_cards = []
        touched_files: set[str] = set()
        for sym in sorted(visited, key=lambda key: (-scores.get(key, 0.0), key)):
            info = self.symbols.get(sym)
            if info:
                touched_files.add(info["path"])
                symbol_cards.append({
                    "symbol": sym,
                    "kind": info["kind"],
                    "location": f"{info['path']}:{info['line']}",
                    "signature": info.get("signature", ""),
                    "pagerank": scores.get(sym, 0.0),
                })
        impact_summary = self.impact(symbols=seed_symbols[:5]) if seed_symbols else {"affected_files": [], "related_tests": []}
        digest = self._digest((item["symbol"] for item in symbol_cards), 1000)
        return {
            "query": query,
            "seeds": seed_symbols[:10],
            "neighborhood_symbols": symbol_cards,
            "relations": sub_edges[: max_nodes * 2],
            "touched_files": sorted(touched_files),
            "affected_files": impact_summary.get("affected_files", []),
            "related_tests": impact_summary.get("related_tests", []),
            "cache": self.cache_stats,
            "repomap_digest_markdown": digest,
            "token_savings_estimate_pct": _compression_pct(digest, self._full_graph()),
        }


# Keep _PythonGraph alias for backwards compatibility
class _PythonGraph(_CodeGraph):
    def __init__(self, root: Path) -> None:
        super().__init__(root, polyglot=False)


def _graph(project_root: str, provider: str | None = None) -> tuple[_CodeGraph, dict[str, Any]]:
    catalog = capability_catalog()
    selected = (provider or catalog["preferred_provider"]).strip().lower()
    if selected not in PROVIDERS:
        raise ValueError(f"unknown code intelligence provider: {selected}")
    descriptor = _provider_descriptor(selected)
    use_polyglot = selected == "polyglot"
    return _CodeGraph(_root(project_root), polyglot=use_polyglot), descriptor


def code_search(project_root: str, query: str = "", kind: str | None = None, provider: str | None = None, max_results: int = 50) -> dict[str, Any]:
    graph, descriptor = _graph(project_root, provider)
    return {"provider": descriptor, "analysis": graph.analysis(), "query": query, "results": graph.search(query, kind, max_results)}


def symbol_trace(project_root: str, symbol: str, direction: str = "both", provider: str | None = None) -> dict[str, Any]:
    if direction not in {"callers", "callees", "both"}:
        raise ValueError("direction must be callers, callees, or both")
    graph, descriptor = _graph(project_root, provider)
    return {"provider": descriptor, "analysis": graph.analysis(), **graph.trace(symbol, direction)}


def impact_analysis(project_root: str, paths: list[str] | None = None, symbols: list[str] | None = None, diff: str | None = None, provider: str | None = None) -> dict[str, Any]:
    graph, descriptor = _graph(project_root, provider)
    return {"provider": descriptor, "analysis": graph.analysis(), **graph.impact(paths, symbols, diff)}


def architecture_extract(project_root: str, provider: str | None = None) -> dict[str, Any]:
    graph, descriptor = _graph(project_root, provider)
    return {"provider": descriptor, "analysis": graph.analysis(), "capability": CAPABILITY["name"], **graph.architecture()}


def codegraph_explore(
    project_root: str,
    query: str,
    max_depth: int = 2,
    max_nodes: int = 25,
    provider: str | None = "polyglot",
) -> dict[str, Any]:
    graph, descriptor = _graph(project_root, provider or "polyglot")
    return {"provider": descriptor, "analysis": graph.analysis(), **graph.explore(query, max_depth=max_depth, max_nodes=max_nodes)}


def code_repomap_rank(project_root: str, focus_files: list[str] | None = None,
                      focus_symbols: list[str] | None = None, token_budget: int = 1000,
                      provider: str | None = "polyglot") -> dict[str, Any]:
    graph, descriptor = _graph(project_root, provider or "polyglot")
    return {"ok": True, "provider": descriptor, "analysis": graph.analysis(),
            **graph.repomap(focus_files, focus_symbols, token_budget)}


def code_health_audit(project_root: str, entry_points: list[str] | None = None,
                      complexity_threshold: int = 10, baseline_path: str | None = None,
                      save_baseline: bool = False, provider: str | None = "polyglot") -> dict[str, Any]:
    graph, descriptor = _graph(project_root, provider or "polyglot")
    return {"ok": True, "provider": descriptor, "analysis": graph.analysis(),
            **graph.health(entry_points, complexity_threshold, baseline_path, save_baseline)}


def export_scip_protocol_index(
    project_root: str,
    project_name: str | None = None,
    as_bytes: bool = False,
    provider: str | None = "polyglot",
) -> dict[str, Any] | bytes:
    """Export workspace declarations and HTTP/gRPC contracts as a Sourcegraph SCIP index."""
    root = _root(project_root)
    graph, _ = _graph(project_root, provider or "polyglot")
    name = project_name or root.name or "workspace"
    index_dict = build_scip_protocol_index(
        name, graph.files, graph.symbols, graph.boundary_endpoints, graph.edges
    )
    return encode_scip_index(index_dict) if as_bytes else index_dict
