"""Standalone stdio MCP server for Code Intelligence Mod (Polyglot + Incremental SQLite CodeGraph)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from code_intelligence import (
    architecture_extract,
    capability_catalog,
    code_search,
    codegraph_explore,
    code_repomap_rank,
    code_health_audit,
    impact_analysis,
    symbol_trace,
)

TOOLS = [
    {
        "name": "code_search",
        "description": "Search project symbols and files through Polyglot CodeGraph AST (Python, TS/JS, Go, Rust, C#, Java, C/C++) with incremental SQLite cache.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {"type": "string"},
                "query": {"type": "string"},
                "kind": {"type": "string"},
                "max_results": {"type": "integer"},
                "provider": {"type": "string", "description": "Optional provider ('polyglot' default, or 'python_ast')"},
            },
            "required": ["project_root", "query"],
        },
    },
    {
        "name": "symbol_trace",
        "description": "Trace callers and callees for a project symbol or HTTP/gRPC/SCIP contract across Python, TS/JS, Go, Rust, C#, Java, and C/C++.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {"type": "string"},
                "symbol": {"type": "string"},
                "direction": {"type": "string", "enum": ["callers", "callees", "both"]},
                "provider": {"type": "string"},
            },
            "required": ["project_root", "symbol"],
        },
    },
    {
        "name": "impact_analysis",
        "description": "Analyze affected files, transitive callers/subclasses, and related tests for a change.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"}},
                "symbols": {"type": "array", "items": {"type": "string"}},
                "diff": {"type": "string"},
                "provider": {"type": "string"},
            },
            "required": ["project_root"],
        },
    },
    {
        "name": "architecture_extract",
        "description": "Extract a structured polyglot project architecture and dependency graph.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {"type": "string"},
                "provider": {"type": "string"},
            },
            "required": ["project_root"],
        },
    },
    {
        "name": "codegraph_explore",
        "description": "One-shot token-budgeted CodeGraph neighborhood explorer: returns matched symbols, N-hop caller/callee & inheritance graph, touched files, impact analysis, and SQLite incremental cache stats.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {"type": "string"},
                "query": {"type": "string"},
                "max_depth": {"type": "integer", "description": "Call/inheritance hop depth (default 2)"},
                "max_nodes": {"type": "integer", "description": "Maximum symbol nodes to return to avoid fat output (default 25)"},
                "provider": {"type": "string"},
            },
            "required": ["project_root", "query"],
        },
    },
    {
        "name": "code_repomap_rank",
        "description": "Rank symbols/modules with personalized PageRank and produce a strictly budgeted compact signature tree.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {"type": "string"},
                "focus_files": {"type": ["array", "null"], "items": {"type": "string"}},
                "focus_symbols": {"type": ["array", "null"], "items": {"type": "string"}},
                "token_budget": {"type": "integer", "minimum": 0, "default": 1000},
                "provider": {"type": ["string", "null"], "default": "polyglot"},
            },
            "required": ["project_root"],
        },
    },
    {
        "name": "code_health_audit",
        "description": "Audit static dead code, cross-module dependency cycles and complexity hotspots with an isolated JSON baseline gate.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {"type": "string"},
                "entry_points": {"type": ["array", "null"], "items": {"type": "string"}},
                "complexity_threshold": {"type": "integer", "default": 10},
                "baseline_path": {"type": ["string", "null"]},
                "save_baseline": {"type": "boolean", "default": False},
                "provider": {"type": ["string", "null"], "default": "polyglot"},
            },
            "required": ["project_root"],
        },
    },
]

for tool in TOOLS:
    tool["parameters"] = tool["inputSchema"]


def dispatch(name: str, args: dict) -> dict:
    provider = args.get("provider", "polyglot")
    if name == "code_search":
        return code_search(args["project_root"], args["query"], args.get("kind"), provider, args.get("max_results", 25))
    if name == "symbol_trace":
        return symbol_trace(args["project_root"], args["symbol"], args.get("direction", "both"), provider)
    if name == "impact_analysis":
        return impact_analysis(args["project_root"], args.get("paths"), args.get("symbols"), args.get("diff"), provider)
    if name == "architecture_extract":
        return architecture_extract(args["project_root"], provider)
    if name == "codegraph_explore":
        return codegraph_explore(
            args["project_root"],
            args["query"],
            int(args.get("max_depth", 2)),
            int(args.get("max_nodes", 25)),
            provider,
        )
    if name == "code_repomap_rank":
        return code_repomap_rank(args["project_root"], args.get("focus_files"), args.get("focus_symbols"),
                                 args.get("token_budget", 1000), provider)
    if name == "code_health_audit":
        return code_health_audit(args["project_root"], args.get("entry_points"), args.get("complexity_threshold", 10),
                                 args.get("baseline_path"), args.get("save_baseline", False), provider)
    return capability_catalog()


call_tool = dispatch


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        method = req.get("method")
        req_id = req.get("id")
        if method == "initialize":
            print(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "code-intelligence", "version": "2.1.0"}}}), flush=True)
        elif method == "tools/list":
            print(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}), flush=True)
        elif method == "tools/call":
            params = req.get("params", {})
            try:
                res = dispatch(params.get("name", ""), params.get("arguments") or {})
                print(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}], "isError": False}}, ensure_ascii=False), flush=True)
            except Exception as exc:
                print(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps({"error": str(exc)}, ensure_ascii=False)}], "isError": True}}, ensure_ascii=False), flush=True)
        elif req_id is not None:
            print(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {}}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
