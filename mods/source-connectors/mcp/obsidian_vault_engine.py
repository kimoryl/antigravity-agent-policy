"""Obsidian Static Vault + SQLite FTS5 BM25 Section Index + WikiLinks Graph + Local Semantic Vector Hybrid Engine (v3.1.0).

Replaces the heavy Docker-based WeKnora (PostgreSQL + Ollama daemon) stack with a zero-daemon,
embedded hybrid retrieval engine directly operating over the Obsidian Markdown Vault:
- Primary Vault: `~/.antigravity-agent-policy/vault` (overridable via `OBSIDIAN_VAULT_ROOT`)
- Persistent Index: `~/.antigravity-agent-policy/obsidian_vault_index.sqlite3` (overridable via `OBSIDIAN_INDEX_DB`)
- 3-Way RRF Hybrid Search:
  1. SQLite FTS5 BM25 over Markdown AST Sections (preserving full code blocks & heading breadcrumbs)
  2. 384-d L2-normalized Local Semantic Vectors (Concept Clusters + Signed Subword/Char N-gram Hashing)
     via fast NumPy cosine similarity (deterministic, no downloaded model)
  3. Obsidian Metadata & [[WikiLinks]] Graph Boost (2-hop decaying spreading activation)
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
import posixpath
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


DEFAULT_MIRROR_VAULT = Path.home() / ".antigravity-agent-policy" / "vault"
DEFAULT_GDRIVE_VAULT: Path | None = None
DEFAULT_INDEX_DB = Path.home() / ".antigravity-agent-policy" / "obsidian_vault_index.sqlite3"

CURATED_CATEGORIES = (
    "01_Tech_Cheatsheets",
    "02_AI_Skills_&_Prompts",
    "03_Media_Notes",
    "03_Architecture_Notes",
    "04_Web_Clippings",
    "05_Agent_Memory_Sync",
)

EXCLUDED_MD_NAMES = {
    "agents.md",
    "memory.md",
    "skill.md",
    "readme.md",
    "install.zh.md",
    "dynamic.zh.md",
    "format.md",
    "00_obsidian_vault_home.md",
}

EMBED_DIM = 384

# Cross-lingual & domain-specific semantic concept clusters (maps synonyms/concepts to dedicated dimensions 0..63)
SEMANTIC_CONCEPT_CLUSTERS: list[tuple[int, tuple[str, ...]]] = [
    (0, ("弹窗", "黑框", "闪窗", "无窗口", "隐藏窗口", "create_no_window", "0x08000000", "creationflags", "popup")),
    (1, ("知识库", "笔记", "双链", "仓库", "obsidian", "vault", "wikilink", "frontmatter", "weknora", "markdown")),
    (2, ("向量", "检索", "搜索", "召回", "重排", "混合检索", "embedding", "vector", "bm25", "fts5", "rag", "rrf", "hybrid")),
    (3, ("记忆", "蒸馏", "做梦", "伴飞", "对账", "memory", "distill", "dream", "sidecar", "experience", "episodic")),
    (4, ("沙箱", "隔离", "容器", "权限", "逃逸", "安全", "sandbox", "isolation", "docker", "container", "seccomp", "landlock")),
    (5, ("缓存", "显存", "前缀", "命中率", "kv-cache", "kvcache", "radix", "cache", "prefill", "ttft")),
    (6, ("网关", "路由", "负载均衡", "调度", "gateway", "router", "switchyard", "bifrost", "smg", "proxy")),
    (7, ("智能体", "子代理", "编排", "主从", "多智能体", "agent", "subagent", "orchestrator", "multiagent", "teamwork")),
    (8, ("状态机", "断点续跑", "持久化", "重放", "事务", "state", "durable", "replay", "checkpoint", "sqlite")),
    (9, ("视频", "字幕", "评论", "流媒体", "video", "subtitle", "transcript", "bilibili", "bvid", "youtube", "mp4")),
    (10, ("网页", "抓取", "爬虫", "剪藏", "提取", "反爬", "jina", "scrape", "crawler", "clipping", "412")),
    (11, ("代码图谱", "抽象语法树", "调用链", "影响面", "符号", "ast", "codegraph", "symbol_trace", "impact_analysis")),
    (12, ("界面", "美学", "视觉", "配色", "对比度", "动效", "弹簧", "ui", "oklch", "wcag", "motion", "spring", "glass")),
    (13, ("分布式", "微服务", "一致性", "共识", "吞吐", "延迟", "distributed", "consensus", "raft", "grpc", "protobuf")),
    (14, ("云盘", "同步", "镜像", "双落盘", "备份", "gdrive", "google drive", "mirror", "sync", "rclone")),
    (15, ("测试", "单测", "回归", "断言", "临时目录", "pytest", "unittest", "temporarydirectory", "verify")),
    (16, ("模型", "推理", "量化", "微调", "上下文", "llm", "ollama", "onnx", "qwen", "gemini", "claude", "codex")),
    (17, ("工作流", "自动化", "定时任务", "守护进程", "调度器", "cron", "schedule", "workflow", "daemon")),
    (18, ("表格", "报告", "文档", "幻灯片", "路演", "三线表", "office", "word", "excel", "ppt", "pptx", "pdf")),
    (19, ("报错", "异常", "恢复", "自愈", "超时", "死锁", "杀软", "error", "winerror", "recovery", "kaspersky", "timeout")),
    (20, ("插件", "扩展", "挂载", "卸载", "预设", "mod", "plugin", "mount", "profile", "mcp")),
    (21, ("终端", "命令行", "脚本", "环境变量", "进程", "shell", "powershell", "cli", "subprocess", "path")),
    (22, ("版本控制", "分支", "提交", "仓库", "合并", "工作树", "git", "github", "worktree", "commit", "branch")),
    (23, ("移动端", "安卓", "模拟器", "真机", "预览", "android", "adb", "emulator", "compose", "apk")),
    (24, ("浏览器", "调试", "抓包", "无障碍", "截图", "chrome", "devtools", "puppeteer", "playwright", "a11y")),
    (25, ("额度", "配额", "限流", "监控", "计费", "开销", "quota", "token", "rate limit", "cost", "usage")),
    (26, ("图数据库", "知识图谱", "三元组", "节点", "边", "时态", "graph", "graphiti", "entity", "edge", "backlink")),
    (27, ("高可用", "分布式", "共识", "无冲突", "集群", "gossip", "swim", "crdt", "raft", "cluster")),
    (28, ("办公", "出行", "旅游", "攻略", "生活", "签证", "酒店", "travel", "trip", "flight", "itinerary")),
    (29, ("提示词", "人设", "思维链", "深度思考", "重写", "prompt", "persona", "thinking", "ambitious", "cot")),
    (30, ("性能", "延迟", "吞吐量", "并发", "异步", "内存", "latency", "throughput", "concurrency", "async")),
    (31, ("加密", "脱敏", "密钥", "令牌", "认证", "凭据", "token", "secret", "ssh", "auth", "desensitize")),
]


@dataclass
class VaultSection:
    section_index: int
    heading_path: str
    start_line: int
    end_line: int
    content: str
    tokens: str
    embedding_blob: bytes


@dataclass
class ParsedNote:
    note_id: str
    title: str
    stem: str
    category: str
    abs_path: str
    mtime_ns: int
    size_bytes: int
    sha256: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    headings: list[dict[str, Any]] = field(default_factory=list)
    outlinks: list[str] = field(default_factory=list)
    sections: list[VaultSection] = field(default_factory=list)


def tokenize_for_fts5(text: str) -> str:
    """Hybrid tokenizer for SQLite FTS5: English identifiers + Chinese unigrams/bigrams/trigrams."""
    low = (text or "").casefold()
    tokens: list[str] = []
    # Extract alphanumeric & snake/kebab terms
    for word in re.findall(r"[a-z0-9_.\-]+", low):
        clean_w = word.strip("._-")
        if len(clean_w) >= 2:
            tokens.append(clean_w)
            # Also split snake_case / kebab-case parts
            if "_" in clean_w or "-" in clean_w:
                for sub in re.split(r"[_\-]+", clean_w):
                    if len(sub) >= 2:
                        tokens.append(sub)
    # Extract CJK character sequences: keep 1-grams (for short queries) + 2-grams + 3-grams
    for cjk in re.findall(r"[\u4e00-\u9fff]+", low):
        n = len(cjk)
        if n == 1:
            tokens.append(cjk)
        else:
            for i in range(n - 1):
                tokens.append(cjk[i : i + 2])
            for i in range(n - 2):
                tokens.append(cjk[i : i + 3])
    return " ".join(tokens)


class LocalSemanticEmbedder:
    """Deterministic concept features + signed hashing; no model, daemon or network."""

    def __init__(self, dim: int = EMBED_DIM) -> None:
        if dim != EMBED_DIM:
            raise ValueError("The index requires 384-dimensional vectors")
        self.dim = dim

    def encode_vector(self, text: str) -> list[float]:
        """Encode text into a 384-dimensional L2-normalized semantic vector."""
        low = (text or "").casefold()
        vec = [0.0] * self.dim

        # 1. Semantic Concept Cluster Activation (Dims 0..63)
        for cluster_idx, keywords in SEMANTIC_CONCEPT_CLUSTERS:
            hits = 0
            for kw in keywords:
                if kw in low:
                    hits += 1
            if hits > 0:
                # Boost concept dimension with sublinear saturation
                vec[cluster_idx % 64] += 2.2 * (1.0 + math.log1p(hits))
                # Secondary harmonic slot in upper space for cross-interaction
                vec[64 + (cluster_idx * 5) % (self.dim - 64)] += 1.2 * (1.0 + math.log1p(hits))

        # 2. Signed Feature Hashing over Words & CJK N-grams (Dims 64..383)
        features: dict[str, int] = {}
        for word in re.findall(r"[a-z0-9_]{2,}", low):
            features[f"w:{word}"] = features.get(f"w:{word}", 0) + 1
            if len(word) >= 4:
                for i in range(len(word) - 2):
                    tri = f"c3:{word[i:i+3]}"
                    features[tri] = features.get(tri, 0) + 1

        for cjk in re.findall(r"[\u4e00-\u9fff]+", low):
            for ch in cjk:
                k1 = f"z1:{ch}"
                features[k1] = features.get(k1, 0) + 1
            for i in range(len(cjk) - 1):
                k2 = f"z2:{cjk[i:i+2]}"
                features[k2] = features.get(k2, 0) + 2
            for i in range(len(cjk) - 2):
                k3 = f"z3:{cjk[i:i+3]}"
                features[k3] = features.get(k3, 0) + 3

        hash_space = self.dim - 64
        for feat, count in features.items():
            digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
            h_val = int.from_bytes(digest, "little")
            slot = 64 + (h_val % hash_space)
            sign = 1.0 if ((h_val >> 32) & 1) == 0 else -1.0
            weight = 1.0 + math.log(count)
            vec[slot] += sign * weight

        # L2 Normalization
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 1e-9:
            inv = 1.0 / norm
            vec = [v * inv for v in vec]
        return vec

    def encode_blob(self, text: str) -> bytes:
        vec = self.encode_vector(text)
        if np is not None:
            return np.asarray(vec, dtype=np.float32).tobytes()
        import struct
        return struct.pack(f"<{self.dim}f", *vec)

    def blob_to_array(self, blob: bytes) -> Any:
        if np is not None:
            return np.frombuffer(blob, dtype=np.float32)
        import struct
        return list(struct.unpack(f"<{self.dim}f", blob))


_EMBEDDER = LocalSemanticEmbedder()


def parse_frontmatter(raw_md: str) -> tuple[dict[str, Any], str, int]:
    """Extract YAML frontmatter if present at line 1, returning (metadata_dict, body_markdown, body_start_line)."""
    lines = raw_md.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, raw_md, 1
    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end_idx = i
            break
    if end_idx == -1:
        return {}, raw_md, 1
    yaml_text = "\n".join(lines[1:end_idx])
    body_text = "\n".join(lines[end_idx + 1 :])
    meta: dict[str, Any] = {}
    if yaml is not None:
        try:
            loaded = yaml.safe_load(yaml_text)
            if isinstance(loaded, dict):
                meta = loaded
        except Exception:
            meta = {}
    if not meta:
        # Lightweight regex fallback for key: value frontmatter
        for ln in lines[1:end_idx]:
            m = re.match(r"^([A-Za-z0-9_\-]+)\s*:\s*(.+)$", ln.strip())
            if m:
                k, v = m.group(1), m.group(2).strip().strip("'\"")
                if v.startswith("[") and v.endswith("]"):
                    meta[k] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
                else:
                    meta[k] = v
    return meta, body_text, end_idx + 2


def _fence_state(line: str, current: str | None) -> str | None:
    """A closing fence must use the opener's character and at least its length."""
    if current:
        if re.fullmatch(r" {0,3}" + re.escape(current[0]) + "{" + str(len(current)) + r",}\s*", line):
            return None
        return current
    match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
    if match and not (match[1][0] == "`" and "`" in match[2]):
        return match[1]
    return None


def _prose_only(raw_md: str) -> str:
    lines = []
    fence = None
    for line in raw_md.splitlines():
        previous = fence
        fence = _fence_state(line, fence)
        lines.append("" if previous or fence else line)
    return re.sub(r"(`+).*?\1", "", "\n".join(lines))


def extract_wikilinks_and_tags(raw_md: str, frontmatter: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """Extract Obsidian [[WikiLinks]], #inline_tags + YAML tags, and YAML aliases."""
    outlinks: list[str] = []
    seen_links: set[str] = set()
    raw_md = _prose_only(raw_md)

    # 1. [[Target|Alias]] or [[Target#Heading]]
    for m in re.finditer(r"\[\[([^\]\n]+)\]\]", raw_md):
        inner = m.group(1).strip()
        target = inner.split("|", 1)[0].split("#", 1)[0].strip()
        if target and target not in seen_links:
            seen_links.add(target)
            outlinks.append(target)

    # 2. Internal relative markdown links [text](note.md)
    for m in re.finditer(r"(?<!!)\[[^\]]+\]\(([^)\n]+\.md(?:#[^)\n]*)?)\)", raw_md):
        href = m.group(1).strip()
        if not href.startswith(("http://", "https://", "file://")):
            stem = href.split("#", 1)[0]
            if stem and stem not in seen_links:
                seen_links.add(stem)
                outlinks.append(stem)

    # 3. Tags from frontmatter + inline #tag
    tags: list[str] = []
    seen_tags: set[str] = set()
    fm_tags = frontmatter.get("tags") or frontmatter.get("tag") or []
    if isinstance(fm_tags, str):
        fm_tags = [x.strip() for x in re.split(r"[,;\s]+", fm_tags) if x.strip()]
    if isinstance(fm_tags, list):
        for t in fm_tags:
            ts = str(t).strip().lstrip("#")
            if ts and ts.lower() not in seen_tags:
                seen_tags.add(ts.lower())
                tags.append(ts)

    for m in re.finditer(r"(?:^|\s)#([A-Za-z\u4e00-\u9fff][A-Za-z0-9_\-\u4e00-\u9fff/]{1,30})", raw_md):
        t = m.group(1).strip()
        if t.lower() not in seen_tags:
            seen_tags.add(t.lower())
            tags.append(t)

    # 4. Aliases
    aliases: list[str] = []
    fm_aliases = frontmatter.get("aliases") or frontmatter.get("alias") or []
    if isinstance(fm_aliases, str):
        fm_aliases = [x.strip() for x in fm_aliases.split(",") if x.strip()]
    if isinstance(fm_aliases, list):
        aliases = [str(a).strip() for a in fm_aliases if str(a).strip()]

    return outlinks, tags, aliases


def parse_obsidian_markdown(
    file_path: Path,
    vault_root: Path,
    raw_content: str | None = None,
    max_section_chars: int = 1600,
) -> ParsedNote:
    """Parse an Obsidian Markdown file into structured metadata, heading tree, WikiLinks, and AST Sections."""
    if raw_content is None:
        raw_content = file_path.read_text(encoding="utf-8", errors="replace")
    try:
        st = file_path.stat()
        mtime_ns = st.st_mtime_ns
        size_bytes = st.st_size
    except OSError:
        mtime_ns = int(time.time() * 1e9)
        size_bytes = len(raw_content.encode("utf-8"))

    try:
        rel_path = file_path.relative_to(vault_root).as_posix()
    except ValueError:
        rel_path = file_path.name

    parts = rel_path.split("/")
    category = parts[0] if len(parts) > 1 else "root"
    stem = file_path.stem
    sha256 = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()

    frontmatter, body_md, body_start_line = parse_frontmatter(raw_content)
    outlinks, tags, aliases = extract_wikilinks_and_tags(raw_content, frontmatter)

    # Determine note title: frontmatter title > first H1 > filename stem
    title = str(frontmatter.get("title") or "").strip()
    lines = raw_content.splitlines()
    headings: list[dict[str, Any]] = []
    in_code_fence = None

    # Track heading hierarchy for section splitting
    raw_sections: list[tuple[str, int, int, list[str]]] = []
    heading_stack: list[tuple[int, str]] = []
    curr_heading_path = stem
    curr_start_line = body_start_line
    curr_lines: list[str] = []

    for idx, line in enumerate(lines, start=1):
        if idx < body_start_line:
            continue
        stripped = line.strip()
        old_fence = in_code_fence
        in_code_fence = _fence_state(line, in_code_fence)
        if old_fence or in_code_fence:
            curr_lines.append(line)
            continue

        if not in_code_fence:
            m_h = re.match(r"^ {0,3}(#{1,4})\s+(.+?)\s*$", line)
            if m_h:
                level = len(m_h.group(1))
                h_text = re.sub(r"\s+#+\s*$", "", m_h.group(2).strip())
                h_text = re.sub(r"\\([\[\]])", r"\1", h_text)
                if not title and level == 1:
                    title = h_text

                # Flush previous section if it has meaningful content
                joined_prev = "\n".join(curr_lines).strip()
                if joined_prev:
                    raw_sections.append((curr_heading_path, curr_start_line, idx - 1, curr_lines))

                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, h_text))
                curr_heading_path = " > ".join(h[1] for h in heading_stack)
                headings.append({"level": level, "text": h_text, "line": idx, "path": curr_heading_path})
                curr_start_line = idx
                curr_lines = [line]
                continue

        curr_lines.append(line)

    if curr_lines and "\n".join(curr_lines).strip():
        raw_sections.append((curr_heading_path, curr_start_line, len(lines), curr_lines))

    if not title:
        title = stem

    if not raw_sections:
        raw_sections = [(title, 1, max(1, len(lines)), lines)]

    # Build final VaultSections (smoothly sub-chunking overly long sections by paragraphs)
    final_sections: list[VaultSection] = []
    tags_str = " ".join(tags + aliases)
    sec_counter = 0

    for h_path, s_line, e_line, sec_lines in raw_sections:
        sec_text = "\n".join(sec_lines).strip()
        if not sec_text:
            continue
        if len(sec_text) <= max_section_chars:
            sec_counter += 1
            embed_input = f"{title} | {h_path} | {tags_str}\n{sec_text}"
            final_sections.append(
                VaultSection(
                    section_index=sec_counter,
                    heading_path=h_path,
                    start_line=s_line,
                    end_line=e_line,
                    content=sec_text,
                    tokens=tokenize_for_fts5(embed_input),
                    embedding_blob=_EMBEDDER.encode_blob(embed_input),
                )
            )
        else:
            # Split by paragraph blocks while preserving line numbers
            chunk_lines: list[str] = []
            chunk_chars = 0
            c_start = s_line
            fence = None
            for offset, ln in enumerate(sec_lines):
                curr_ln_no = s_line + offset
                previous_fence = fence
                fence = _fence_state(ln, fence)
                if chunk_chars + len(ln) > max_section_chars and chunk_lines and not ln.strip() and not previous_fence and not fence:
                    sub_txt = "\n".join(chunk_lines).strip()
                    if sub_txt:
                        sec_counter += 1
                        embed_input = f"{title} | {h_path} | {tags_str}\n{sub_txt}"
                        final_sections.append(
                            VaultSection(
                                section_index=sec_counter,
                                heading_path=h_path,
                                start_line=c_start,
                                end_line=curr_ln_no - 1,
                                content=sub_txt,
                                tokens=tokenize_for_fts5(embed_input),
                                embedding_blob=_EMBEDDER.encode_blob(embed_input),
                            )
                        )
                    chunk_lines = []
                    chunk_chars = 0
                    c_start = curr_ln_no + 1
                else:
                    chunk_lines.append(ln)
                    chunk_chars += len(ln) + 1
            if chunk_lines:
                sub_txt = "\n".join(chunk_lines).strip()
                if sub_txt:
                    sec_counter += 1
                    embed_input = f"{title} | {h_path} | {tags_str}\n{sub_txt}"
                    final_sections.append(
                        VaultSection(
                            section_index=sec_counter,
                            heading_path=h_path,
                            start_line=c_start,
                            end_line=e_line,
                            content=sub_txt,
                            tokens=tokenize_for_fts5(embed_input),
                            embedding_blob=_EMBEDDER.encode_blob(embed_input),
                        )
                    )

    return ParsedNote(
        note_id=rel_path,
        title=title,
        stem=stem,
        category=category,
        abs_path=str(file_path),
        mtime_ns=mtime_ns,
        size_bytes=size_bytes,
        sha256=sha256,
        frontmatter=frontmatter,
        tags=tags,
        aliases=aliases,
        headings=headings,
        outlinks=outlinks,
        sections=final_sections,
    )


class ObsidianVaultEngine:
    """Embedded Obsidian Vault Hybrid Search & Graph Engine (SQLite FTS5 + 384d Local Semantic Vector + WikiLinks)."""

    def __init__(
        self,
        vault_root: Path | str | None = None,
        gdrive_vault_root: Path | str | None = None,
        index_db_path: Path | str | None = None,
    ) -> None:
        env_vault = os.environ.get("OBSIDIAN_VAULT_ROOT", "").strip()
        env_gdrive = os.environ.get("OBSIDIAN_GDRIVE_VAULT_ROOT", "").strip()
        env_db = os.environ.get("OBSIDIAN_INDEX_DB", "").strip()

        if vault_root is not None:
            self.vault_root = Path(vault_root)
        elif env_vault:
            self.vault_root = Path(env_vault)
        elif DEFAULT_MIRROR_VAULT.is_dir():
            self.vault_root = DEFAULT_MIRROR_VAULT
        else:
            self.vault_root = DEFAULT_GDRIVE_VAULT

        if gdrive_vault_root is not None:
            self.gdrive_vault_root: Path | None = Path(gdrive_vault_root)
        elif env_gdrive:
            self.gdrive_vault_root = Path(env_gdrive)
        elif env_vault or vault_root is not None:
            # When running in isolated test mode via OBSIDIAN_VAULT_ROOT, do not touch real Google Drive
            self.gdrive_vault_root = None
        else:
            self.gdrive_vault_root = DEFAULT_GDRIVE_VAULT if DEFAULT_GDRIVE_VAULT.is_dir() else None

        if index_db_path is not None:
            self.index_db_path = Path(index_db_path)
        elif env_db:
            self.index_db_path = Path(env_db)
        elif env_vault or vault_root is not None:
            self.index_db_path = self.vault_root / ".obsidian_index.sqlite3"
        else:
            self.index_db_path = DEFAULT_INDEX_DB

        self._ensure_schema()

    @contextmanager
    def _connect(self):
        self.index_db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.index_db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            with conn:
                yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS vault_notes (
                    note_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    stem TEXT NOT NULL,
                    category TEXT NOT NULL,
                    abs_path TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    aliases_json TEXT NOT NULL,
                    frontmatter_json TEXT NOT NULL,
                    headings_json TEXT NOT NULL,
                    outlinks_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_vault_notes_stem ON vault_notes(stem);
                CREATE INDEX IF NOT EXISTS idx_vault_notes_category ON vault_notes(category);

                CREATE TABLE IF NOT EXISTS vault_sections (
                    section_id TEXT PRIMARY KEY,
                    note_id TEXT NOT NULL,
                    section_index INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    heading_path TEXT NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    embedding_blob BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_vault_sections_note ON vault_sections(note_id);

                CREATE VIRTUAL TABLE IF NOT EXISTS vault_fts USING fts5(
                    section_id UNINDEXED,
                    note_id UNINDEXED,
                    title,
                    heading_path,
                    tags_text,
                    content,
                    tokens
                );

                CREATE TABLE IF NOT EXISTS vault_links (
                    source_note_id TEXT NOT NULL,
                    target_key TEXT NOT NULL,
                    resolved_note_id TEXT,
                    PRIMARY KEY (source_note_id, target_key)
                );
                CREATE INDEX IF NOT EXISTS idx_vault_links_target ON vault_links(target_key);
                CREATE INDEX IF NOT EXISTS idx_vault_links_resolved ON vault_links(resolved_note_id);
                """
            )

    def ensure_obsidian_vault_config(self) -> dict[str, Any]:
        """Ensure standard .obsidian workspace settings exist so opening the folder in Obsidian works out-of-the-box."""
        roots = [self.vault_root]
        if self.gdrive_vault_root and self.gdrive_vault_root != self.vault_root and self.gdrive_vault_root.is_dir():
            roots.append(self.gdrive_vault_root)

        configured: list[str] = []
        app_cfg = {
            "alwaysUpdateLinks": True,
            "newLinkFormat": "shortest",
            "useMarkdownLinks": False,
            "showFrontmatter": True,
            "attachmentFolderPath": "assets",
            "promptDelete": False,
        }
        graph_cfg = {
            "collapse-filter": False,
            "search": "",
            "showTags": True,
            "showAttachments": False,
            "hideUnresolved": False,
            "showOrphans": True,
            "colorGroups": [
                {"query": "path:01_Tech_Cheatsheets", "color": {"a": 1, "rgb": 3447003}},
                {"query": "path:02_AI_Skills_&_Prompts", "color": {"a": 1, "rgb": 10181046}},
                {"query": "path:03_Media_Notes", "color": {"a": 1, "rgb": 15158332}},
                {"query": "path:04_Web_Clippings", "color": {"a": 1, "rgb": 3066993}},
                {"query": "path:05_Agent_Memory_Sync", "color": {"a": 1, "rgb": 15844367}},
            ],
        }
        for r in roots:
            if not r.is_dir():
                continue
            obs_dir = r / ".obsidian"
            obs_dir.mkdir(parents=True, exist_ok=True)
            app_f = obs_dir / "app.json"
            graph_f = obs_dir / "graph.json"
            if not app_f.is_file():
                app_f.write_text(json.dumps(app_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            if not graph_f.is_file():
                graph_f.write_text(json.dumps(graph_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            configured.append(str(r))
        return {"configured_vaults": configured}

    def _discover_markdown_files(self) -> list[Path]:
        if not self.vault_root.is_dir():
            return []
        md_files: list[Path] = []
        for mf in sorted(self.vault_root.rglob("*")):
            rel = mf.relative_to(self.vault_root)
            if not mf.is_file() or mf.suffix.lower() != ".md":
                continue
            if any(part.startswith(".") for part in rel.parts):
                continue
            if not mf.resolve().is_relative_to(self.vault_root.resolve()):
                continue
            if mf.name.lower() not in EXCLUDED_MD_NAMES:
                md_files.append(mf)
        return md_files

    def sync_vault_index(self, force: bool = False) -> dict[str, Any]:
        """Incrementally index added/modified/deleted Obsidian Markdown notes into SQLite FTS5 + Vector + Graph tables."""
        t0 = time.perf_counter()
        self.ensure_obsidian_vault_config()
        md_files = self._discover_markdown_files()

        with self._connect() as conn:
            existing_rows = conn.execute(
                "SELECT note_id, mtime_ns, size_bytes, sha256, stem, title FROM vault_notes"
            ).fetchall()
            existing_map = {r["note_id"]: dict(r) for r in existing_rows}

            disk_note_ids: set[str] = set()
            to_parse: list[Path] = []
            skipped = 0

            for mf in md_files:
                try:
                    rel_id = mf.relative_to(self.vault_root).as_posix()
                    st = mf.stat()
                except OSError:
                    continue
                disk_note_ids.add(rel_id)
                prev = existing_map.get(rel_id)
                if not force and prev and prev["mtime_ns"] == st.st_mtime_ns and prev["size_bytes"] == st.st_size:
                    skipped += 1
                    continue
                to_parse.append(mf)

            deleted_ids = set(existing_map.keys()) - disk_note_ids

            # Remove deleted notes
            for del_id in deleted_ids:
                conn.execute("DELETE FROM vault_notes WHERE note_id = ?", (del_id,))
                conn.execute("DELETE FROM vault_sections WHERE note_id = ?", (del_id,))
                conn.execute("DELETE FROM vault_fts WHERE note_id = ?", (del_id,))
                conn.execute("DELETE FROM vault_links WHERE source_note_id = ?", (del_id,))

            ingested = 0
            updated = 0
            sections_indexed = 0
            now_ts = time.time()

            for mf in to_parse:
                try:
                    parsed = parse_obsidian_markdown(mf, self.vault_root)
                except OSError:
                    continue
                is_update = parsed.note_id in existing_map
                if is_update:
                    conn.execute("DELETE FROM vault_sections WHERE note_id = ?", (parsed.note_id,))
                    conn.execute("DELETE FROM vault_fts WHERE note_id = ?", (parsed.note_id,))
                    conn.execute("DELETE FROM vault_links WHERE source_note_id = ?", (parsed.note_id,))
                    updated += 1
                else:
                    ingested += 1

                conn.execute(
                    """
                    INSERT OR REPLACE INTO vault_notes (
                        note_id, title, stem, category, abs_path, mtime_ns, size_bytes, sha256,
                        tags_json, aliases_json, frontmatter_json, headings_json, outlinks_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        parsed.note_id,
                        parsed.title,
                        parsed.stem,
                        parsed.category,
                        parsed.abs_path,
                        parsed.mtime_ns,
                        parsed.size_bytes,
                        parsed.sha256,
                        json.dumps(parsed.tags, ensure_ascii=False),
                        json.dumps(parsed.aliases, ensure_ascii=False),
                        json.dumps(parsed.frontmatter, default=str, ensure_ascii=False),
                        json.dumps(parsed.headings, ensure_ascii=False),
                        json.dumps(parsed.outlinks, ensure_ascii=False),
                        now_ts,
                    ),
                )

                tags_text = " ".join(parsed.tags + parsed.aliases)
                for sec in parsed.sections:
                    sec_id = f"{parsed.note_id}#s{sec.section_index}"
                    conn.execute(
                        """
                        INSERT INTO vault_sections (
                            section_id, note_id, section_index, title, category,
                            heading_path, start_line, end_line, content, embedding_blob
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sec_id,
                            parsed.note_id,
                            sec.section_index,
                            parsed.title,
                            parsed.category,
                            sec.heading_path,
                            sec.start_line,
                            sec.end_line,
                            sec.content,
                            sec.embedding_blob,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO vault_fts (
                            section_id, note_id, title, heading_path, tags_text, content, tokens
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sec_id,
                            parsed.note_id,
                            tokenize_for_fts5(parsed.title),
                            tokenize_for_fts5(sec.heading_path),
                            tokenize_for_fts5(tags_text),
                            sec.content,
                            sec.tokens,
                        ),
                    )
                    sections_indexed += 1

                for link_target in parsed.outlinks:
                    conn.execute(
                        "INSERT OR REPLACE INTO vault_links (source_note_id, target_key, resolved_note_id) VALUES (?, ?, NULL)",
                        (parsed.note_id, link_target),
                    )

            # Resolve [[WikiLinks]] targets across the entire Vault if any note changed or deleted
            if to_parse or deleted_ids or force:
                all_notes = conn.execute("SELECT note_id, stem, title, aliases_json FROM vault_notes").fetchall()
                lookup: dict[str, set[str]] = {}
                for r in all_notes:
                    nid = r["note_id"]
                    keys = [nid, Path(nid).with_suffix("").as_posix(), r["stem"], r["title"]]
                    keys.extend(json.loads(r["aliases_json"] or "[]"))
                    for key in keys:
                        lookup.setdefault(str(key).casefold(), set()).add(nid)

                unresolved = conn.execute("SELECT source_note_id, target_key FROM vault_links").fetchall()
                for ur in unresolved:
                    tkey = ur["target_key"].replace("\\", "/").casefold()
                    local_key = posixpath.normpath(posixpath.join(posixpath.dirname(ur["source_note_id"]), tkey)).casefold()
                    candidates = lookup.get(local_key) or lookup.get(tkey) or lookup.get(Path(tkey).stem.casefold(), set())
                    resolved = next(iter(candidates)) if len(candidates) == 1 else None
                    conn.execute(
                        "UPDATE vault_links SET resolved_note_id = ? WHERE source_note_id = ? AND target_key = ?",
                        (resolved, ur["source_note_id"], ur["target_key"]),
                    )

            total_notes = conn.execute("SELECT COUNT(*) FROM vault_notes").fetchone()[0]
            total_sections = conn.execute("SELECT COUNT(*) FROM vault_sections").fetchone()[0]
            total_links = conn.execute("SELECT COUNT(*) FROM vault_links").fetchone()[0]

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        return {
            "status": "ok",
            "engine": "obsidian_sqlite_fts5_vector_hybrid_v3",
            "vault_root": str(self.vault_root),
            "index_db_path": str(self.index_db_path),
            "total_notes": total_notes,
            "total_sections": total_sections,
            "total_wikilinks": total_links,
            "ingested": ingested,
            "updated": updated,
            "deleted": len(deleted_ids),
            "skipped": skipped,
            "sections_newly_indexed": sections_indexed,
            "elapsed_ms": elapsed_ms,
        }

    def get_graph_neighbors(self, note_id: str) -> dict[str, list[dict[str, str]]]:
        """Return 1-hop Outlinks and Backlinks (incoming links) for a given note in the Obsidian Vault."""
        with self._connect() as conn:
            out_rows = conn.execute(
                """
                SELECT l.target_key, l.resolved_note_id, n.title, n.abs_path
                FROM vault_links l
                LEFT JOIN vault_notes n ON l.resolved_note_id = n.note_id
                WHERE l.source_note_id = ?
                """,
                (note_id,),
            ).fetchall()
            back_rows = conn.execute(
                """
                SELECT DISTINCT l.source_note_id, n.title, n.abs_path
                FROM vault_links l
                JOIN vault_notes n ON l.source_note_id = n.note_id
                WHERE l.resolved_note_id = ?
                """,
                (note_id,),
            ).fetchall()

        outlinks = [
            {
                "target": r["target_key"],
                "note_id": r["resolved_note_id"] or "",
                "title": r["title"] or r["target_key"],
                "path": r["abs_path"] or "",
            }
            for r in out_rows
        ]
        backlinks = [
            {
                "note_id": r["source_note_id"],
                "title": r["title"],
                "path": r["abs_path"],
            }
            for r in back_rows
        ]
        return {"outlinks": outlinks, "backlinks": backlinks}

    def hybrid_search(
        self,
        query: str,
        category_or_kb: str | None = None,
        match_count: int = 8,
        rrf_k: int = 30,
        graph_hops: int = 2,
    ) -> dict[str, Any]:
        """3-Way Hybrid Search: SQLite FTS5 BM25 + 384d Local Semantic Vector Cosine + Obsidian Metadata/Graph RRF."""
        self.sync_vault_index(force=False)
        match_count = int(match_count)
        if rrf_k <= 0:
            raise ValueError("rrf_k must be positive")
        if not isinstance(graph_hops, int) or isinstance(graph_hops, bool) or graph_hops < 1:
            raise ValueError("graph_hops must be a positive integer")
        q_clean = (query or "").strip()
        if not q_clean or match_count <= 0:
            return {"query": query, "searched_kbs": ["obsidian-knowledge-vault"], "matches": [], "graph_hops": graph_hops, "graph_activated_notes": 0}

        cat_filter: str | None = None
        if category_or_kb:
            needle = category_or_kb.strip()
            if needle not in ("local-vault-fts5", "obsidian-knowledge-vault", "Core Knowledge Vault", "default", "all"):
                cat_filter = needle
                for c in CURATED_CATEGORIES:
                    if needle.lower() in c.lower():
                        cat_filter = c
                        break

        with self._connect() as conn:
            # 1. Path A: SQLite FTS5 BM25 Section Retrieval
            q_tok_list: list[str] = []
            seen_t: set[str] = set()
            for t in tokenize_for_fts5(q_clean).split():
                if t not in seen_t:
                    seen_t.add(t)
                    q_tok_list.append(f'"{t}"')
                if len(q_tok_list) >= 24:
                    break

            bm25_ranks: dict[str, tuple[int, float]] = {}
            if q_tok_list:
                fts_expr = " OR ".join(q_tok_list)
                sql_fts = """
                    SELECT section_id, note_id,
                           bm25(vault_fts, 0.0, 0.0, 5.0, 3.5, 4.0, 1.0, 2.0) AS raw_bm25
                    FROM vault_fts
                    WHERE vault_fts MATCH ? AND (? IS NULL OR note_id IN (
                        SELECT note_id FROM vault_notes WHERE category = ?))
                    ORDER BY raw_bm25 ASC
                    LIMIT 60
                """
                fts_rows = conn.execute(sql_fts, (fts_expr, cat_filter, cat_filter)).fetchall()
                for rank_idx, r in enumerate(fts_rows, start=1):
                    bm25_ranks[r["section_id"]] = (rank_idx, -float(r["raw_bm25"]))

            # 2. Path B: Local 384d Semantic Vector Cosine Retrieval over all sections
            if cat_filter:
                sec_rows = conn.execute(
                    "SELECT section_id, note_id, title, category, heading_path, start_line, end_line, content, embedding_blob FROM vault_sections WHERE category = ?",
                    (cat_filter,),
                ).fetchall()
            else:
                sec_rows = conn.execute(
                    "SELECT section_id, note_id, title, category, heading_path, start_line, end_line, content, embedding_blob FROM vault_sections"
                ).fetchall()

            if not sec_rows:
                return {"query": query, "searched_kbs": [cat_filter or "obsidian-knowledge-vault"], "matches": [], "graph_hops": graph_hops, "graph_activated_notes": 0}

            q_vec = _EMBEDDER.encode_vector(q_clean)
            vec_scores: dict[str, float] = {}
            sec_map: dict[str, sqlite3.Row] = {}

            if np is not None:
                q_arr = np.asarray(q_vec, dtype=np.float32)
                mat = np.vstack([np.frombuffer(r["embedding_blob"], dtype=np.float32) for r in sec_rows])
                sims = mat @ q_arr
                for idx, r in enumerate(sec_rows):
                    sid = r["section_id"]
                    sec_map[sid] = r
                    vec_scores[sid] = float(sims[idx])
            else:
                for r in sec_rows:
                    sid = r["section_id"]
                    sec_map[sid] = r
                    s_vec = _EMBEDDER.blob_to_array(r["embedding_blob"])
                    vec_scores[sid] = sum(a * b for a, b in zip(q_vec, s_vec))

            sorted_vec = sorted(vec_scores.items(), key=lambda x: x[1], reverse=True)[:60]
            vec_ranks: dict[str, tuple[int, float]] = {
                sid: (rank_idx, round(score, 4)) for rank_idx, (sid, score) in enumerate(sorted_vec, start=1) if score > 0.05
            }

            # 3. Path C: Note Metadata (Title/Tags/Aliases) & Graph Link Boost
            note_rows = conn.execute(
                "SELECT note_id, title, stem, abs_path, tags_json, aliases_json, outlinks_json FROM vault_notes"
            ).fetchall()
            note_meta: dict[str, dict[str, Any]] = {r["note_id"]: dict(r) for r in note_rows}

            candidate_sids = set(bm25_ranks.keys()) | set(vec_ranks.keys())
            q_low = q_clean.casefold()
            q_terms = [w for w in re.split(r"\s+", q_low) if len(w) >= 2]

            metadata_hits = set()
            for nid, metadata in note_meta.items():
                fields = [metadata["title"], metadata["stem"]]
                fields += json.loads(metadata["tags_json"]) + json.loads(metadata["aliases_json"])
                if any(q_low in str(value).casefold() for value in fields):
                    metadata_hits.add(nid)
            candidate_sids.update(sid for sid, sec in sec_map.items() if sec["note_id"] in metadata_hits)

            # Expand the strongest seeds along incoming/outgoing edges, without cycles.
            seed_scores: dict[str, float] = {nid: 0.08 for nid in metadata_hits}
            for sid in candidate_sids:
                if sid not in sec_map:
                    continue
                score = (1.35 / (rrf_k + bm25_ranks[sid][0]) if sid in bm25_ranks else 0.0)
                score += (1.0 / (rrf_k + vec_ranks[sid][0]) if sid in vec_ranks else 0.0)
                nid = sec_map[sid]["note_id"]
                seed_scores[nid] = max(seed_scores.get(nid, 0.0), score)
            seeds = {nid for nid, _ in sorted(seed_scores.items(), key=lambda item: (-item[1], item[0]))[:3]}
            adjacency: dict[str, set[str]] = {}
            for edge in conn.execute("SELECT source_note_id, resolved_note_id FROM vault_links WHERE resolved_note_id IS NOT NULL"):
                source, target = edge
                if source != target:
                    adjacency.setdefault(source, set()).add(target)
                    adjacency.setdefault(target, set()).add(source)
            graph_scores: dict[str, float] = {}
            graph_distances: dict[str, int] = {}
            for seed in sorted(seeds):
                visited = {seed}
                frontier = {seed}
                for hop in range(1, min(graph_hops, 2) + 1):
                    next_frontier = set().union(*(adjacency.get(nid, set()) for nid in frontier)) - visited
                    for nid in next_frontier:
                        graph_scores[nid] = graph_scores.get(nid, 0.0) + 0.45 ** (hop - 1)
                        graph_distances[nid] = min(graph_distances.get(nid, hop), hop)
                    visited.update(next_frontier)
                    frontier = next_frontier
            graph_ranks = {nid: rank for rank, (nid, _) in enumerate(sorted(graph_scores.items(), key=lambda item: (-item[1], item[0])), 1)}
            candidate_sids.update(sid for sid, sec in sec_map.items() if sec["note_id"] in graph_ranks)

            fused_results: list[tuple[float, str]] = []
            for sid in candidate_sids:
                if sid not in sec_map:
                    continue
                sec = sec_map[sid]
                nid = sec["note_id"]
                nmeta = note_meta.get(nid, {})

                rrf_score = 0.0
                if sid in bm25_ranks:
                    b_rank, _ = bm25_ranks[sid]
                    rrf_score += 1.35 / (rrf_k + b_rank)
                if sid in vec_ranks:
                    v_rank, v_sim = vec_ranks[sid]
                    rrf_score += (1.0 + max(0.0, v_sim) * 0.5) / (rrf_k + v_rank)
                if nid in graph_ranks:
                    rrf_score += 0.35 * (0.45 ** (graph_distances[nid] - 1)) / (rrf_k + graph_ranks[nid])
                if nid in metadata_hits:
                    rrf_score += 0.015

                # Exact substring boost on title, heading_path, or tags
                title_low = (sec["title"] or "").casefold()
                hpath_low = (sec["heading_path"] or "").casefold()
                tags_low = (nmeta.get("tags_json") or "").casefold()
                if q_low in title_low or q_low in hpath_low:
                    rrf_score += 0.035
                for qt in q_terms:
                    if qt in title_low:
                        rrf_score += 0.015
                    if qt in hpath_low:
                        rrf_score += 0.012
                    if qt in tags_low:
                        rrf_score += 0.015

                fused_results.append((rrf_score, sid))

            fused_results.sort(key=lambda x: (-x[0], x[1]))

            # Deduplicate overly dense sections from the exact same note (max 2 sections per note)
            note_hit_counts: dict[str, int] = {}
            top_matches: list[dict[str, Any]] = []

            for rrf_score, sid in fused_results:
                sec = sec_map[sid]
                nid = sec["note_id"]
                if note_hit_counts.get(nid, 0) >= 2:
                    continue
                note_hit_counts[nid] = note_hit_counts.get(nid, 0) + 1
                nmeta = note_meta.get(nid, {})
                neighbors = self.get_graph_neighbors(nid)

                tags_list: list[str] = []
                try:
                    tags_list = json.loads(nmeta.get("tags_json") or "[]")
                except Exception:
                    pass

                b_score = bm25_ranks[sid][1] if sid in bm25_ranks else 0.0
                v_score = vec_ranks[sid][1] if sid in vec_ranks else round(vec_scores.get(sid, 0.0), 4)

                top_matches.append(
                    {
                        "section_id": sid,
                        "note_id": nid,
                        "knowledge_title": sec["title"],
                        "category": sec["category"],
                        "heading_path": sec["heading_path"],
                        "line_range": f"L{sec['start_line']}-L{sec['end_line']}",
                        "source_path": nmeta.get("abs_path") or str(self.vault_root / nid),
                        "content": sec["content"],
                        "tags": tags_list,
                        "graph_neighbors": neighbors,
                        "score": round(rrf_score, 4),
                        "bm25_score": b_score,
                        "vector_score": v_score,
                        "graph_rank": graph_ranks.get(nid),
                        "engine": "obsidian_sqlite_fts5_vector_hybrid_v3",
                    }
                )
                if len(top_matches) >= match_count:
                    break

        return {
            "query": query,
            "searched_kbs": [cat_filter or "obsidian-knowledge-vault"],
            "engine": "obsidian_sqlite_fts5_vector_hybrid_v3",
            "matches": top_matches,
            "graph_hops": graph_hops,
            "graph_activated_notes": len({sec["note_id"] for sec in sec_map.values()} & graph_ranks.keys()),
        }

    def ingest_markdown(
        self,
        title: str,
        content: str,
        kb_or_category: str | None = None,
        source_url: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Write a curated Markdown note directly into the Obsidian Vault (Mirror + GDrive) with YAML frontmatter & auto-WikiLinks, and index immediately."""
        clean_title = (title or "Untitled_Note").strip()
        self.sync_vault_index()
        # Determine target Obsidian folder
        target_cat = "01_Tech_Cheatsheets"
        if kb_or_category:
            if kb_or_category not in ("obsidian-knowledge-vault", "local-vault-fts5", "Core Knowledge Vault", "default", "all"):
                target_cat = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", kb_or_category).strip(" ._")
                if not target_cat:
                    raise ValueError("Invalid category")
            for c in CURATED_CATEGORIES:
                if kb_or_category.strip().lower() in c.lower():
                    target_cat = c
                    break
        if source_url:
            u_low = source_url.lower()
            if "b23.tv" in u_low or "bilibili.com" in u_low:
                target_cat = "03_Media_Notes"
            elif kb_or_category is None:
                target_cat = "04_Web_Clippings"

        # Sanitize filename for Windows & Obsidian
        safe_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", clean_title).strip(" ._")[:95]
        if not safe_stem:
            safe_stem = f"Note_{int(time.time())}"
        date_prefix = time.strftime("%Y-%m-%d")
        if not re.match(r"^\d{4}-\d{2}-\d{2}", safe_stem):
            filename = f"{date_prefix}_{safe_stem}.md"
        else:
            filename = f"{safe_stem}.md"

        # Discover existing note titles/stems to suggest automatic [[WikiLinks]] if mentioned in content
        auto_links: list[str] = []
        with self._connect() as conn:
            existing_notes = conn.execute("SELECT note_id, stem, title FROM vault_notes ORDER BY note_id").fetchall()
        content_low = _prose_only(content).casefold()
        link_markup: list[str] = []
        for r in existing_notes:
            cand = (r["title"] or r["stem"] or "").strip()
            if 2 <= len(cand) <= 80 and cand.casefold() != clean_title.casefold():
                if cand.casefold() in content_low and f"[[{cand}]]" not in content:
                    auto_links.append(cand)
                    target = str(Path(r["note_id"]).with_suffix("")).replace("\\", "/")
                    link_markup.append(f"[[{target}|{cand}]]")
                    if len(auto_links) >= 5:
                        break

        # Ensure Obsidian YAML Frontmatter
        fm, _, _ = parse_frontmatter(content)
        final_md = content.strip()
        if not fm:
            inferred_tags = list(tags or [])
            if target_cat == "03_Media_Notes" and "bilibili" not in inferred_tags:
                inferred_tags.append("bilibili")
            elif target_cat == "04_Web_Clippings" and "web-clipping" not in inferred_tags:
                inferred_tags.append("web-clipping")
            elif not inferred_tags:
                inferred_tags.append("tech-note")

            yaml_lines = [
                "---",
                f'title: {json.dumps(clean_title, ensure_ascii=False)}',
                f'created: "{time.strftime("%Y-%m-%d %H:%M:%S")}"',
                f'category: "{target_cat}"',
                f"tags: [{', '.join(json.dumps(t, ensure_ascii=False) for t in inferred_tags)}]",
            ]
            if source_url:
                yaml_lines.append(f'source_url: {json.dumps(source_url, ensure_ascii=False)}')
            yaml_lines.append("---")
            final_md = "\n".join(yaml_lines) + "\n\n" + final_md

        if auto_links and "## 关联知识图谱" not in final_md:
            links_md = " | ".join(link_markup)
            final_md += f"\n\n---\n## 关联知识图谱 (Obsidian Backlinks)\n\n- 自动关联笔记：{links_md}\n"

        rel_path = f"{target_cat}/{filename}"
        mirror_file = self.vault_root / target_cat / filename
        mirror_file.parent.mkdir(parents=True, exist_ok=True)
        mirror_file.write_text(final_md + "\n", encoding="utf-8")

        gdrive_written = None
        if self.gdrive_vault_root and self.gdrive_vault_root != self.vault_root:
            try:
                g_file = self.gdrive_vault_root / target_cat / filename
                g_file.parent.mkdir(parents=True, exist_ok=True)
                g_file.write_text(final_md + "\n", encoding="utf-8")
                gdrive_written = str(g_file)
            except OSError:
                gdrive_written = None

        sync_stats = self.sync_vault_index(force=False)
        return {
            "kb_id": "obsidian-knowledge-vault",
            "note_id": rel_path,
            "title": clean_title,
            "category": target_cat,
            "vault_path": str(mirror_file),
            "gdrive_vault_path": gdrive_written,
            "auto_wikilinks": auto_links,
            "index_sync": sync_stats,
        }

    def read_note_or_section(self, identifier: str) -> dict[str, Any]:
        """Read a note by note_id, path, title, or section_id, including its heading outline and 1-hop WikiLinks."""
        self.sync_vault_index(force=False)
        ident = (identifier or "").strip()
        p = Path(ident)
        if p.is_file():
            raw = p.read_text(encoding="utf-8", errors="replace")
            return {"path": str(p), "content": raw[:30000]}

        with self._connect() as conn:
            # Check section_id first
            if "#s" in ident:
                srow = conn.execute("SELECT * FROM vault_sections WHERE section_id = ?", (ident,)).fetchone()
                if srow:
                    neighbors = self.get_graph_neighbors(srow["note_id"])
                    return {
                        "section": {key: srow[key] for key in srow.keys() if key != "embedding_blob"},
                        "graph_neighbors": neighbors,
                    }

            nrow = conn.execute(
                "SELECT * FROM vault_notes WHERE note_id = ? OR stem = ? OR title = ? LIMIT 1",
                (ident, ident, ident),
            ).fetchone()
            if not nrow:
                nrow = conn.execute(
                    "SELECT * FROM vault_notes WHERE title LIKE ? OR note_id LIKE ? LIMIT 1",
                    (f"%{ident}%", f"%{ident}%"),
                ).fetchone()
            if not nrow:
                raise FileNotFoundError(f"Note not found in Obsidian Vault: {identifier}")

            nid = nrow["note_id"]
            abs_p = Path(nrow["abs_path"])
            raw_md = abs_p.read_text(encoding="utf-8", errors="replace")[:30000] if abs_p.is_file() else ""
            neighbors = self.get_graph_neighbors(nid)
            return {
                "note_id": nid,
                "title": nrow["title"],
                "category": nrow["category"],
                "path": nrow["abs_path"],
                "tags": json.loads(nrow["tags_json"] or "[]"),
                "headings": json.loads(nrow["headings_json"] or "[]"),
                "graph_neighbors": neighbors,
                "content": raw_md,
            }

    def health_and_stats(self) -> dict[str, Any]:
        sync_info = self.sync_vault_index(force=False)
        with self._connect() as conn:
            cat_rows = conn.execute(
                "SELECT category, COUNT(*) AS cnt FROM vault_notes GROUP BY category ORDER BY category"
            ).fetchall()
        categories = [{"id": r["category"], "name": r["category"], "knowledge_count": r["cnt"]} for r in cat_rows]
        return {
            "alive": True,
            "storage_architecture": "Obsidian Static Markdown Vault (Single Source of Truth)",
            "search_mode": "obsidian_sqlite_fts5_bm25 + 384d_local_semantic_vector + wikilinks_graph_rrf (zero_daemon)",
            "vault_root": str(self.vault_root),
            "gdrive_vault_root": str(self.gdrive_vault_root) if self.gdrive_vault_root else None,
            "index_db_path": str(self.index_db_path),
            "total_notes": sync_info["total_notes"],
            "total_sections": sync_info["total_sections"],
            "total_wikilinks": sync_info["total_wikilinks"],
            "knowledge_bases_count": len(categories) or 1,
            "knowledge_bases": [
                {
                    "id": "obsidian-knowledge-vault",
                    "name": "Obsidian Knowledge Vault",
                    "description": f"Obsidian 静态知识库 + SQLite FTS5 章节检索 + 384d 轻量向量 + 双链图谱 ({self.vault_root})",
                    "knowledge_count": sync_info["total_notes"],
                    "sections_count": sync_info["total_sections"],
                    "wikilinks_count": sync_info["total_wikilinks"],
                    "categories": categories,
                }
            ],
        }
