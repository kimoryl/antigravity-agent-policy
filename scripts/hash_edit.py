"""UTF-8 block reads, AST/symbol outline extraction, and optimistic edits with four-character hash anchors."""

from __future__ import annotations

import ast
import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from supervisor_engine import mark_mutated, record_read, resolve_file

_POLYGLOT_SYM_RE = re.compile(
    r"^\s*(?:export\s+)?(?:public\s+|private\s+|protected\s+|internal\s+|static\s+|async\s+)*"
    r"(?:def|class|function|interface|type|struct|enum|fn|func)\s+([A-Za-z_][A-Za-z0-9_]*)"
)


def compute_block_hash(lines: list[str]) -> str:
    normalized = "\n".join(line.rstrip() for line in lines)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:4]


def _load(path: str, start_line: int, end_line: int, session_id: str):
    if type(start_line) is not int or type(end_line) is not int or start_line < 1 or end_line < start_line:
        raise ValueError("Line range must satisfy 1 <= start_line <= end_line")
    target = resolve_file(session_id, path)
    raw = target.read_bytes()
    text = raw.decode("utf-8-sig")
    lines = text.splitlines(keepends=True)
    if end_line > len(lines):
        raise ValueError("Line range exceeds file length")
    return target, raw, lines, [line.rstrip("\r\n") for line in lines[start_line - 1:end_line]]


def _block(lines: list[str], start: int, end: int, anchor: str) -> str:
    return f'[Block L{start}-L{end} | anchor_hash: "{anchor}"]\n' + "\n".join(
        f"{i} | {line}" for i, line in enumerate(lines, start))


def _python_outline(text: str, clean_lines: list[str]) -> list[dict[str, Any]]:
    tree = ast.parse(text)
    symbols: list[dict[str, Any]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = int(getattr(node, "lineno", 1))
            end = int(getattr(node, "end_lineno", start) or start)
            end = min(max(start, end), len(clean_lines))
            sig_line = clean_lines[start - 1].strip() if 1 <= start <= len(clean_lines) else node.name
            kind = "class" if isinstance(node, ast.ClassDef) else ("async_function" if isinstance(node, ast.AsyncFunctionDef) else "function")
            entry: dict[str, Any] = {
                "name": node.name,
                "kind": kind,
                "signature": sig_line,
                "start_line": start,
                "end_line": end,
                "anchor_hash": compute_block_hash(clean_lines[start - 1:end]),
            }
            if isinstance(node, ast.ClassDef):
                methods: list[dict[str, Any]] = []
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        m_start = int(getattr(child, "lineno", start))
                        m_end = min(max(m_start, int(getattr(child, "end_lineno", m_start) or m_start)), len(clean_lines))
                        methods.append({
                            "name": child.name,
                            "kind": "method",
                            "signature": clean_lines[m_start - 1].strip() if 1 <= m_start <= len(clean_lines) else child.name,
                            "start_line": m_start,
                            "end_line": m_end,
                            "anchor_hash": compute_block_hash(clean_lines[m_start - 1:m_end]),
                        })
                if methods:
                    entry["methods"] = methods
            symbols.append(entry)
    return symbols


def _regex_outline(clean_lines: list[str]) -> list[dict[str, Any]]:
    raw_hits: list[tuple[int, str, str]] = []
    for idx, raw_line in enumerate(clean_lines, start=1):
        match = _POLYGLOT_SYM_RE.match(raw_line)
        if match:
            raw_hits.append((idx, match.group(1), raw_line.strip()))
    symbols: list[dict[str, Any]] = []
    for i, (start, name, sig) in enumerate(raw_hits):
        next_start = raw_hits[i + 1][0] - 1 if i + 1 < len(raw_hits) else len(clean_lines)
        end = max(start, min(next_start, len(clean_lines)))
        while end > start and not clean_lines[end - 1].strip():
            end -= 1
        symbols.append({
            "name": name,
            "kind": "symbol",
            "signature": sig[:160],
            "start_line": start,
            "end_line": end,
            "anchor_hash": compute_block_hash(clean_lines[start - 1:end]),
        })
    return symbols


def hash_outline(path: str, session_id: str = "default") -> dict[str, Any]:
    """Return AST/symbol skeleton with line ranges and 4-char anchor hashes without loading full body."""
    target = resolve_file(session_id, path)
    raw = target.read_bytes()
    text = raw.decode("utf-8-sig")
    clean_lines = [line.rstrip("\r\n") for line in text.splitlines(keepends=True)]
    related = record_read(session_id, path)
    if not clean_lines:
        return {
            "ok": True,
            "path": path,
            "mode": "outline",
            "total_lines": 0,
            "symbols": [],
            "content": "",
            "raw_chars": 0,
            "outline_chars": 0,
            "estimated_saved_chars": 0,
            "related_files": related,
        }
    parser_used = "regex"
    if target.suffix.lower() == ".py":
        try:
            symbols = _python_outline(text, clean_lines)
            parser_used = "python_ast"
        except SyntaxError:
            symbols = _regex_outline(clean_lines)
    else:
        symbols = _regex_outline(clean_lines)

    formatted_lines: list[str] = []
    for sym in symbols:
        formatted_lines.append(f'[L{sym["start_line"]}-L{sym["end_line"]} | anchor_hash: "{sym["anchor_hash"]}"] {sym["signature"]}')
        for m in sym.get("methods", []):
            formatted_lines.append(f'  [L{m["start_line"]}-L{m["end_line"]} | anchor_hash: "{m["anchor_hash"]}"] {m["signature"]}')
    outline_text = "\n".join(formatted_lines)
    raw_chars = len(text)
    outline_chars = len(outline_text)
    return {
        "ok": True,
        "path": path,
        "mode": "outline",
        "parser": parser_used,
        "total_lines": len(clean_lines),
        "anchor_hash": compute_block_hash(clean_lines),
        "symbols": symbols,
        "content": outline_text,
        "raw_chars": raw_chars,
        "outline_chars": outline_chars,
        "estimated_saved_chars": max(0, raw_chars - outline_chars),
        "related_files": related,
    }


def hash_read(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    session_id: str = "default",
    mode: str = "block",
) -> dict:
    if mode == "outline" or (start_line is None and end_line is None):
        return hash_outline(path, session_id=session_id)
    _, _, lines, block = _load(path, start_line, end_line, session_id)
    anchor = compute_block_hash(block)
    return {"ok": True, "path": path, "start_line": start_line, "end_line": end_line,
            "total_lines": len(lines), "anchor_hash": anchor,
            "related_files": record_read(session_id, path), "content": _block(block, start_line, end_line, anchor)}


def hash_edit(path: str, start_line: int, end_line: int, expected_hash: str, new_content: str,
              session_id: str = "default") -> dict:
    target, raw, lines, block = _load(path, start_line, end_line, session_id)
    actual = compute_block_hash(block)
    if actual != expected_hash.strip().lower():
        return {"ok": False, "error_code": "HASH_MISMATCH", "expected_hash": expected_hash,
                "actual_hash": actual, "current_block": _block(block, start_line, end_line, actual),
                "message": f'[HASH_MISMATCH] 拒绝写入！目标区间 L{start_line}-L{end_line} 当前实际哈希为 "{actual}"。请使用最新内容与哈希重试。'}
    newline = "\r\n" if b"\r\n" in raw else "\n"
    replacement = newline.join(new_content.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    if replacement and not replacement.endswith(newline) and lines[end_line - 1].endswith(("\n", "\r")):
        replacement += newline
    text = "".join(lines[:start_line - 1]) + replacement + "".join(lines[end_line:])
    updated = (b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b"") + text.encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".hash-edit-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, target.stat().st_mode)
        if target.read_bytes() != raw:
            return {"ok": False, "error_code": "HASH_MISMATCH", "message": "[HASH_MISMATCH] 文件在编辑期间发生变化，请重新 hash_read。"}
        os.replace(temporary, target)
        mark_mutated(session_id, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"ok": True, "path": path, "start_line": start_line, "end_line": end_line,
            "anchor_hash": compute_block_hash(new_content.splitlines()), "verification_required": True}
