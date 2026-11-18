#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cross-Platform Mod Pool Registry & Manifest Sync Engine (mod_pool_sync_engine.py v1.0)
======================================================================================
Maintains deterministic SHA256 registries and Markdown architecture atlases for
encapsulated Mod packages in `mods/` (or `AGY_MODS_DIR`).
"""

from __future__ import annotations

import ast
import datetime
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

CORE_MOD_IDS: list[str] = [
    "code-intelligence",
    "source-connectors",
    "ambitious-deep-thinking",
    "quota-monitor",
    "context7-sdk-verifier",
]

MOD_PROFILES_MAP: dict[str, list[str]] = {
    "@core": list(CORE_MOD_IDS),
    "@coding": [*CORE_MOD_IDS, "dev-setup-workflows", "github-suite"],
    "@mobile": [*CORE_MOD_IDS, "android-suite", "github-suite"],
    "@web": [*CORE_MOD_IDS, "chrome-devtools", "ui-aesthetic-studio", "anatomy-motion-studio"],
    "@office": [*CORE_MOD_IDS, "office-report-studio", "ppt-master", "anatomy-motion-studio"],
    "@automation": [*CORE_MOD_IDS, "windows-uia-automation", "office-report-studio"],
    "@ops": [*CORE_MOD_IDS, "win-system-guardian", "windows-uia-automation"],
}

MOD_BENCHMARK_METADATA: dict[str, dict[str, Any]] = {
    "ambitious-deep-thinking": {
        "primary_profile": "@core",
        "modality": "coding_and_testing",
        "name": "四维深度思考、关键分歧追问与红蓝对抗推演引擎",
        "keywords": ["deep-thinking", "prompt-rewrite", "reasoning", "metacognition", "sequential-thinking", "grill-me"],
        "solve_problem": "提供动工前关键权衡点追问收敛、高度/深度/广度升维重写与红蓝可证伪推演。",
    },
    "code-intelligence": {
        "primary_profile": "@core",
        "modality": "coding_and_testing",
        "name": "多语言静态 AST 与跨边界代码知识图谱 (CodeGraph v2.1)",
        "keywords": ["ast", "codegraph", "symbol", "call-graph", "impact-analysis", "repomap"],
        "solve_problem": "提供增量 SQLite 符号索引、调用追踪、影响面分析、个性化 PageRank RepoMap 与健康度门禁。",
    },
    "context7-sdk-verifier": {
        "primary_profile": "@core",
        "modality": "coding_and_testing",
        "name": "Project × Modality × Global 三维交叉记忆与 SDK 真值校验引擎",
        "keywords": ["context7", "sdk-verifier", "fact-check", "memory-reconcile", "3d-memory"],
        "solve_problem": "消除第三方库 API 签名幻觉，保障三维记忆库的只读预检与机械对账原子提交。",
    },
    "quota-monitor": {
        "primary_profile": "@core",
        "modality": "coding_and_testing",
        "name": "多智能体模型池实时配额透视、输出净化与进展感知守卫",
        "keywords": ["quota", "rate-limit", "token-usage", "cache-tax", "router", "cost-monitor"],
        "solve_problem": "提供三池额度水位线路由、CLI 冗长日志折叠降噪与空转死循环熔断保护。",
    },
    "source-connectors": {
        "primary_profile": "@core",
        "modality": "coding_and_testing",
        "name": "Obsidian 静态知识库、二跳 WikiLinks 图扩散与文档连接器 (v3.1)",
        "keywords": ["obsidian", "rag", "wikilinks", "fts5", "knowledge-base", "graph-diffusion"],
        "solve_problem": "零守护进程实现 SQLite FTS5 BM25 + 384d 局部语义向量 + 二跳双链图扩散混合检索与网页抓取。",
    },
}

SKIP_DIR_NAMES = {".packages", "__pycache__", ".pytest_cache", ".git", ".mypy_cache", ".ruff_cache", "node_modules", ".venv"}
SKIP_FILE_NAMES = {".reconcile_cache.json", ".ds_store", "thumbs.db"}
SKIP_EXTENSIONS = {".pyc", ".pyo", ".mp4", ".wav", ".zip"}


def now_utc_str() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _compute_dir_sha256(mod_dir: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    count = 0
    for fp in sorted(mod_dir.rglob("*")):
        if any(part.lower() in SKIP_DIR_NAMES for part in fp.parts):
            continue
        if not fp.is_file() or fp.name.lower() in SKIP_FILE_NAMES or fp.suffix.lower() in SKIP_EXTENSIONS:
            continue
        rel = fp.relative_to(mod_dir).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(fp.read_bytes())
        count += 1
    return h.hexdigest(), count


def build_mod_pool_registry(source_mods_dir: Path) -> dict[str, Any]:
    mods_out: list[dict[str, Any]] = []
    if source_mods_dir.is_dir():
        for item in sorted(source_mods_dir.iterdir()):
            if not item.is_dir() or item.name.startswith("."):
                continue
            mjson_path = item / "mod.json"
            if not mjson_path.is_file():
                continue
            try:
                mjson = json.loads(mjson_path.read_text(encoding="utf-8"))
            except Exception:
                mjson = {}
            mid = str(mjson.get("id") or item.name)
            digest, fcount = _compute_dir_sha256(item)
            meta = MOD_BENCHMARK_METADATA.get(mid, {})
            mods_out.append({
                "id": mid,
                "name": mjson.get("name") or meta.get("name") or mid,
                "version": mjson.get("version", "1.0.0"),
                "primary_profile": meta.get("primary_profile", "@core" if mid in CORE_MOD_IDS else "@coding"),
                "modality": meta.get("modality", "coding_and_testing"),
                "files_count": fcount,
                "sha256": digest,
            })
    return {
        "schema_version": "1.0",
        "updated_at": now_utc_str(),
        "total_mods": len(mods_out),
        "profiles": MOD_PROFILES_MAP,
        "mods": mods_out,
    }


def sync_mod_pool_everywhere(
    source_mods_dir: Path | None = None,
    deploy_runtime_mods_dir: Path | None = None,
) -> dict[str, Any]:
    default_src = Path(__file__).resolve().parents[1] / "mods"
    src_root = source_mods_dir or Path(os.environ.get("AGY_MODS_DIR", str(default_src)))
    registry = build_mod_pool_registry(src_root)
    sync_target_env = os.environ.get("AGY_MODS_SYNC_DIR", "").strip()
    written_targets: list[str] = []
    if sync_target_env:
        dst = Path(sync_target_env)
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "00_Mod_Pool_Registry.json").write_text(
            json.dumps(registry, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written_targets.append(str(dst))
    if deploy_runtime_mods_dir and deploy_runtime_mods_dir.resolve() != src_root.resolve():
        deploy_runtime_mods_dir.mkdir(parents=True, exist_ok=True)
        for item in sorted(src_root.iterdir()):
            if item.is_dir() and (item / "mod.json").is_file():
                shutil.copytree(item, deploy_runtime_mods_dir / item.name, dirs_exist_ok=True)
        written_targets.append(str(deploy_runtime_mods_dir))
    return {
        "status": "ok",
        "source_mods_dir": str(src_root),
        "total_mods": registry["total_mods"],
        "synced_targets": written_targets,
    }
