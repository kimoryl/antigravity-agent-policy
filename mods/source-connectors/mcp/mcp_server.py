"""Standalone stdio MCP server for Source Connectors v3.1.0 (Obsidian hybrid retrieval + Bilibili & Web Ingestor)."""

from __future__ import annotations

import gzip
import hashlib
import html
import contextlib
import contextvars
import http.cookiejar
import importlib.util
import json
import os
import posixpath
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0

# Support both direct stdio execution and package imports without creating a vault at import time.
if __package__:
    from .obsidian_vault_engine import ObsidianVaultEngine, CURATED_CATEGORIES
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from obsidian_vault_engine import ObsidianVaultEngine, CURATED_CATEGORIES

KB_SYNC_SCRIPT = Path(os.environ.get("OBSIDIAN_SYNC_SCRIPT", str(Path.home() / ".antigravity-agent-policy" / "sync_vault.py")))
KB_SYNC_STATE = Path(os.environ.get("OBSIDIAN_SYNC_STATE") or (
    str(Path(os.environ["OBSIDIAN_VAULT_ROOT"]) / ".obsidian_sync_state.json")
    if os.environ.get("OBSIDIAN_VAULT_ROOT") else str(Path.home() / ".antigravity-agent-policy" / "sync_state.json")
))


def _run_kb_sync_now(sync_remote: bool = False) -> dict[str, Any]:
    """Call only the incremental Markdown mirror collector, then refresh the local index."""
    engine = ObsidianVaultEngine()
    docs = []
    mirror_error = None
    remote_triggered = False
    isolated = bool(os.environ.get("OBSIDIAN_VAULT_ROOT"))
    if isolated and not os.environ.get("OBSIDIAN_SYNC_SCRIPT"):
        # The legacy collector uses fixed production roots. Custom vaults must opt
        # into an explicitly configured collector instead of touching those roots.
        mirror_error = "custom_vault_requires_OBSIDIAN_SYNC_SCRIPT"
    elif KB_SYNC_SCRIPT.is_file():
        try:
            spec = importlib.util.spec_from_file_location("sync_gdrive_to_weknora", str(KB_SYNC_SCRIPT))
            if not spec or not spec.loader:
                raise RuntimeError("sync_script_not_loadable")
            mod = importlib.util.module_from_spec(spec)
            # The collector logs progress. Keep stdio stdout exclusively for JSON-RPC.
            with contextlib.redirect_stdout(sys.stderr):
                spec.loader.exec_module(mod)
                if sync_remote and hasattr(mod, "trigger_remote_gdrive_sync"):
                    remote_triggered = bool(mod.trigger_remote_gdrive_sync())
                docs = mod.collect_curated_md_documents()
        except Exception as exc:
            mirror_error = str(exc)
    else:
        mirror_error = "sync_script_not_found"

    stats = engine.sync_vault_index(force=False)
    payload = {
        **stats,
        "status": "partial" if mirror_error else "ok",
        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "version": "3.1.0",
        "kb_id": "obsidian-knowledge-vault",
        "remote_triggered": remote_triggered,
        "total_collected": len(docs),
        "failed": int(mirror_error is not None),
        "newly_ingested_docs": [],
        "updated_docs": [],
    }
    if mirror_error:
        payload["mirror_error"] = mirror_error
    state_file = (engine.vault_root / ".obsidian_sync_state.json"
                  if isolated and not os.environ.get("OBSIDIAN_SYNC_STATE") else KB_SYNC_STATE)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class WeKnoraBridge:
    """Compatibility facade for the five legacy MCP tool names; all KB I/O is local."""

    def __init__(self, base_url: str = "", api_key: str = "", *, engine=None) -> None:
        # Legacy constructor arguments remain accepted, without opening any service connection.
        self.engine = engine if engine is not None else ObsidianVaultEngine()

    def health(self) -> dict[str, Any]:
        stats = self.engine.health_and_stats()
        return {**stats, "auto_sync_triggered": False, "models": []}

    def list_kbs(self) -> list[dict[str, Any]]:
        stats = self.engine.health_and_stats()
        result = stats["knowledge_bases"][:]
        categories = {item["id"]: item for item in result[0]["categories"]}
        if self.engine.vault_root.is_dir():
            for folder in sorted(self.engine.vault_root.iterdir()):
                if folder.is_dir() and not folder.name.startswith("."):
                    categories.setdefault(folder.name, {"id": folder.name, "name": folder.name, "knowledge_count": 0})
        result.extend(categories.values())
        return result

    def _category(self, kb: str | None) -> str | None:
        if kb:
            for item in self.list_kbs():
                if kb.casefold() in (item["id"].casefold(), item["name"].casefold()):
                    return None if item["id"] == "obsidian-knowledge-vault" else item["id"]
            for category in CURATED_CATEGORIES:
                if kb.casefold() in category.casefold():
                    return category
        # Historical service UUIDs and local-public-brain-fts5 resolve to the single vault.
        return None

    def resolve_kb_id(self, kb_id_or_name: str | None = None, auto_create_default: bool = True) -> str:
        return self._category(kb_id_or_name) or "obsidian-knowledge-vault"

    def create_kb(self, name: str, description: str = "", embedding_model_id: str = "", llm_model_id: str = "") -> dict[str, Any]:
        if name in ("Core Knowledge Vault", "default", "obsidian-knowledge-vault"):
            category = None
            self.engine.vault_root.mkdir(parents=True, exist_ok=True)
        else:
            category = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" ._")
            if not category:
                raise ValueError("Invalid knowledge base name")
            (self.engine.vault_root / category).mkdir(parents=True, exist_ok=True)
        return {"id": category or "obsidian-knowledge-vault", "name": name, "description": description}

    def ingest_markdown(self, title: str, content: str, kb_id_or_name: str | None = None, status: str = "publish", source_url: str | None = None) -> dict[str, Any]:
        result = self.engine.ingest_markdown(title, content, kb_or_category=self._category(kb_id_or_name), source_url=source_url)
        return {"kb_id": self.resolve_kb_id(kb_id_or_name), "result": {**result, "id": result["note_id"]}}

    def ingest_url(self, url: str, kb_id_or_name: str | None = None, enable_multimodel: bool = True) -> dict[str, Any]:
        extracted = extract_webpage_markdown(url)
        return self.ingest_markdown(extracted["title"], extracted["markdown"], kb_id_or_name, source_url=url)

    def list_docs(self, kb_id_or_name: str | None = None, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        self.engine.sync_vault_index()
        category = self._category(kb_id_or_name)
        page_size = max(1, min(100, int(page_size)))
        with self.engine._connect() as conn:
            rows = conn.execute(
                "SELECT note_id AS id, title, category, abs_path AS path, size_bytes FROM vault_notes WHERE (? IS NULL OR category = ?) ORDER BY note_id LIMIT ? OFFSET ?",
                (category, category, page_size, max(0, int(page) - 1) * page_size),
            ).fetchall()
        return {"kb_id": self.resolve_kb_id(kb_id_or_name), "documents": [dict(row) for row in rows]}

    def read_doc(self, knowledge_id: str) -> dict[str, Any]:
        note = self.engine.read_note_or_section(knowledge_id)
        note_id = note.get("note_id") or note.get("section", {}).get("note_id")
        with self.engine._connect() as conn:
            chunks = conn.execute("SELECT section_id AS id, heading_path, start_line, end_line, content FROM vault_sections WHERE note_id = ? ORDER BY section_index", (note_id,)).fetchall()
        return {"knowledge": note, "chunks": [dict(row) for row in chunks]}

    def hybrid_search(self, query: str, kb_id_or_name: str | None = None, match_count: int = 8, vector_threshold: float = 0.3, keyword_threshold: float = 0.2, graph_hops: int = 2) -> dict[str, Any]:
        result = self.engine.hybrid_search(query, category_or_kb=self._category(kb_id_or_name), match_count=match_count, graph_hops=graph_hops)
        kb_id = self.resolve_kb_id(kb_id_or_name)
        for match in result["matches"]:
            match.update({"id": match["section_id"], "knowledge_id": match["note_id"], "kb_id": kb_id})
        return result

    def rag_chat(self, query: str, kb_id_or_name: str | None = None, session_id: str | None = None, graph_hops: int = 2) -> dict[str, Any]:
        result = self.hybrid_search(query, kb_id_or_name, graph_hops=graph_hops)
        matches = result["matches"]
        excerpts = [f"[{i}] {m['knowledge_title']} ({m['line_range']})\n{m['content']}" for i, m in enumerate(matches, 1)]
        return {
            "session_id": session_id or "obsidian-local-session",
            "kb_id": self.resolve_kb_id(kb_id_or_name),
            "mode": "local_evidence_excerpts",
            "query": query,
            "answer": "本地知识库原文证据（由调用方模型综合回答）：\n\n" + ("\n\n".join(excerpts) or "未匹配到相关章节。"),
            "references": matches,
            "graph_hops": result["graph_hops"],
            "graph_activated_notes": result["graph_activated_notes"],
        }


# ---------------------------------------------------------------------------
# Bilibili & General Web Deep Ingestor (Bypass 412 via Mobile Initial State + Subtitles + Agent-Reach Jina Fallback)
# ---------------------------------------------------------------------------

def _resolve_github_token() -> str:
    for env_key in ("GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_TOKEN"):
        env_tok = os.environ.get(env_key, "").strip()
        if env_tok and not env_tok.startswith("${"):
            return env_tok
    return ""


def inspect_bilibili_video(url_or_bvid: str, download_mp4_to: str | None = None) -> dict[str, Any]:
    target = url_or_bvid.strip()
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [("User-Agent", MOBILE_UA)]

    if target.startswith("http") and "b23.tv" in target:
        resp = opener.open(target, timeout=10)
        target = resp.geturl()

    m_bv = re.search(r"(BV[a-zA-Z0-9]+)", target)
    if not m_bv:
        raise ValueError(f"Cannot extract BVID from: {url_or_bvid}")
    bvid = m_bv.group(1)

    m_url = f"https://m.bilibili.com/video/{bvid}"
    resp = opener.open(m_url, timeout=12)
    raw = resp.read()
    if resp.info().get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    html_text = raw.decode("utf-8", errors="replace")

    m_state = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});\s*\(function", html_text, re.S)
    if not m_state:
        raise RuntimeError(f"Failed to parse __INITIAL_STATE__ from mobile page for {bvid}")

    state = json.loads(m_state.group(1))
    video = state.get("video", {})
    vi = video.get("viewInfo", {})
    aid = vi.get("aid")
    cid = vi.get("cid")
    play_urls = video.get("playUrlInfo", [])
    mp4_url = play_urls[0].get("url") if play_urls else None

    # Extract CC subtitles & top comments when available
    subtitles_text = ""
    top_comments: list[str] = []
    if aid and cid:
        try:
            sub_req = urllib.request.Request(
                f"https://api.bilibili.com/x/player/wbi/v2?aid={aid}&cid={cid}",
                headers={"User-Agent": DESKTOP_UA, "Referer": f"https://www.bilibili.com/video/{bvid}"},
            )
            with urllib.request.urlopen(sub_req, timeout=8) as sresp:
                sdata = json.loads(sresp.read().decode("utf-8", errors="replace"))
                sub_list = sdata.get("data", {}).get("subtitle", {}).get("subtitles") or []
                if sub_list:
                    sub_url = sub_list[0].get("subtitle_url", "")
                    if sub_url.startswith("//"):
                        sub_url = "https:" + sub_url
                    if sub_url.startswith("http"):
                        with urllib.request.urlopen(
                            urllib.request.Request(sub_url, headers={"User-Agent": DESKTOP_UA}), timeout=8
                        ) as bresp:
                            bjson = json.loads(bresp.read().decode("utf-8", errors="replace"))
                            lines = [item.get("content", "").strip() for item in (bjson.get("body") or []) if item.get("content")]
                            subtitles_text = "\n".join(lines[:400])
        except Exception:
            pass
        try:
            rep_req = urllib.request.Request(
                f"https://api.bilibili.com/x/v2/reply/main?type=1&oid={aid}&mode=3&ps=5",
                headers={"User-Agent": DESKTOP_UA, "Referer": f"https://www.bilibili.com/video/{bvid}"},
            )
            with urllib.request.urlopen(rep_req, timeout=8) as rresp:
                rdata = json.loads(rresp.read().decode("utf-8", errors="replace")).get("data") or {}
                top_upper = (rdata.get("top") or {}).get("upper")
                if isinstance(top_upper, dict):
                    msg = (top_upper.get("content") or {}).get("message", "").strip()
                    if msg:
                        top_comments.append(f"[置顶] {msg}")
                for r in (rdata.get("replies") or [])[:5]:
                    if isinstance(r, dict):
                        msg = (r.get("content") or {}).get("message", "").strip()
                        likes = r.get("like", 0)
                        if msg:
                            top_comments.append(f"[👍{likes}] {msg}")
        except Exception:
            pass

    downloaded_path = None
    if download_mp4_to and mp4_url:
        out_path = Path(download_mp4_to).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        req_mp4 = urllib.request.Request(mp4_url, headers={"User-Agent": MOBILE_UA, "Referer": m_url})
        with urllib.request.urlopen(req_mp4, timeout=30) as vresp, open(out_path, "wb") as f:
            f.write(vresp.read())
        downloaded_path = str(out_path)

    return {
        "bvid": bvid,
        "aid": aid,
        "cid": cid,
        "title": vi.get("title"),
        "desc": vi.get("desc"),
        "duration_sec": vi.get("duration"),
        "owner": vi.get("owner", {}).get("name"),
        "stat": vi.get("stat", {}),
        "tags": [t.get("name") or t.get("tag_name") for t in video.get("tags", []) if isinstance(t, dict)],
        "subtitles_text": subtitles_text,
        "top_comments": top_comments,
        "mp4_stream_url": mp4_url,
        "downloaded_mp4_path": downloaded_path,
    }


def _fetch_via_jina_reader(url: str) -> dict[str, Any] | None:
    """Agent-Reach No-Key multi-platform extractor via r.jina.ai for SPA/social platforms (X/Twitter, Xiaohongshu, YouTube, WeChat)."""
    try:
        jina_url = f"https://r.jina.ai/{url}"
        req = urllib.request.Request(
            jina_url,
            headers={
                "User-Agent": DESKTOP_UA,
                "Accept": "text/markdown, text/plain, */*",
                "X-Return-Format": "markdown",
            },
        )
        with urllib.request.urlopen(req, timeout=18) as resp:
            md_text = resp.read().decode("utf-8", errors="replace").strip()
        if len(md_text) >= 80:
            title_m = re.search(r"^Title:\s*(.+)$", md_text, re.M) or re.search(r"^#\s+(.+)$", md_text, re.M)
            title = title_m.group(1).strip() if title_m else url
            return {
                "url": url,
                "type": "webpage_jina_reader",
                "title": title,
                "markdown": f"# {title}\n\nSource: {url}\n\n{md_text[:25000]}",
            }
    except Exception:
        pass
    return None


# Optional embedding hook: callable(url, timeout=seconds) -> rendered HTML.
# No browser executable is launched or imported by default.
DOM_RENDER_HOOK = None
_CRAWL_URL_GUARD = contextvars.ContextVar("source_crawl_url_guard", default=None)


class _CrawlRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, guard):
        self.guard = guard

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self.guard(_normalize_crawl_url(newurl, req.full_url)):
            raise ValueError("redirect_outside_crawl_boundary")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _PageParser(HTMLParser):
    """Small tolerant HTML tree; keep source links and structured state before cleaning."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "document", "attrs": {}, "children": []}
        self.stack = [self.root]
        self.links: list[str] = []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "children": []}
        self.stack[-1]["children"].append(node)
        if tag == "a" and node["attrs"].get("href"):
            self.links.append(node["attrs"]["href"])
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1]["children"].append(data)


def _node_text(node) -> str:
    if isinstance(node, str):
        return node
    return "".join(_node_text(child) for child in node["children"])


def _walk_nodes(node):
    if isinstance(node, dict):
        yield node
        for child in node["children"]:
            yield from _walk_nodes(child)


def _is_spa_shell(html_text: str) -> bool:
    visible = re.sub(r"<(script|style|template|head)\b[^>]*>.*?</\1\s*>", "", html_text, flags=re.I | re.S)
    visible = re.sub(r"<!--.*?-->", "", visible, flags=re.S)
    visible = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", visible))).strip()
    marker = re.search(r'''<(?:div|main)\b[^>]*\bid\s*=\s*["'](?:app|root|__next)["']|__NEXT_DATA__|window\.__INITIAL_STATE__|<noscript\b[^>]*>.*?(?:javascript|enable.{0,20}js|启用.{0,20}(?:js|脚本))''', html_text, re.I | re.S)
    return len(visible) < 120 and bool(marker)


def _clean_page(page: _PageParser) -> tuple[str, int]:
    stripped = 0

    def render(node):
        nonlocal stripped
        if isinstance(node, str):
            return re.sub(r"\s+", " ", node)
        tag, attrs = node["tag"], node["attrs"]
        identity = " ".join(str(attrs.get(key) or "") for key in ("id", "class", "role", "aria-label"))
        noise = re.search(r"(?:^|[\s_-])(?:ads?|advert(?:isement|ising)?|banner|cookie(?:s|[-_]consent)?|consent[-_]?(?:modal|banner)|popup)(?:$|[\s_-])", identity, re.I)
        if tag in {"head", "nav", "footer", "aside", "header", "script", "style", "noscript", "template"} or noise or "hidden" in attrs or attrs.get("aria-hidden") == "true":
            stripped += len(_node_text(node))
            return ""
        if tag == "pre":
            code = next((child for child in node["children"] if isinstance(child, dict) and child["tag"] == "code"), node)
            language = re.search(r"language-([\w+-]+)", str(code["attrs"].get("class") or ""))
            text = _node_text(code).strip("\n")
            fence = "`" * max(3, max((len(m.group()) + 1 for m in re.finditer(r"`{3,}", text)), default=3))
            return f"\n\n{fence}{language.group(1) if language else ''}\n{text}\n{fence}\n\n"
        if tag == "table":
            rows = []
            for row in _walk_nodes(node):
                if row["tag"] == "tr":
                    cells = [render(cell).strip().replace("|", "\\|").replace("\n", "<br>") for cell in row["children"] if isinstance(cell, dict) and cell["tag"] in {"td", "th"}]
                    if cells:
                        rows.append(cells)
            if not rows:
                return ""
            width = max(map(len, rows))
            lines = ["| " + " | ".join(row + [""] * (width - len(row))) + " |" for row in rows]
            lines.insert(1, "| " + " | ".join(["---"] * width) + " |")
            return "\n\n" + "\n".join(lines) + "\n\n"
        text = "".join(render(child) for child in node["children"])
        if tag in {"h1", "h2", "h3", "h4"}:
            return "\n\n" + "#" * int(tag[1]) + " " + text.strip() + "\n\n"
        if tag == "code":
            return "`" + text + "`"
        if tag == "a" and attrs.get("href") and not attrs["href"].lower().startswith("javascript:"):
            return f"[{text.strip()}]({attrs['href']})"
        if tag == "li":
            return "\n- " + text.strip() + "\n"
        if tag == "br":
            return "\n"
        if tag in {"p", "div", "section", "article", "main", "blockquote", "ul", "ol"}:
            return "\n\n" + text.strip() + "\n\n"
        return text

    # Normalize prose spacing in render(), never rewrite newlines inside fenced code.
    return render(page.root).strip(), stripped


def _hydrate_state(page: _PageParser) -> str:
    pieces: list[str] = []
    body_found = False
    content_keys = {"articlebody", "text", "content", "markdown", "body", "description", "html", "title", "headline"}

    def collect(value, key=""):
        nonlocal body_found
        if isinstance(value, dict):
            for name, child in value.items():
                collect(child, name.casefold())
        elif isinstance(value, list):
            for child in value:
                collect(child, key)
        elif isinstance(value, str) and key in content_keys and value.strip() and value not in pieces:
            pieces.append(value)
            if key not in {"title", "headline", "description"}:
                body_found = True

    for node in _walk_nodes(page.root):
        tag, attrs = node["tag"], node["attrs"]
        if tag == "template":
            # Preserve the actual markup tree, rather than losing headings/code.
            template = _PageParser("")
            template.root["children"] = node["children"]
            body, _ = _clean_page(template)
            if body:
                pieces.append(body)
                body_found = True
        elif tag == "script":
            text = _node_text(node).strip()
            if attrs.get("id") == "__NEXT_DATA__" or attrs.get("type") == "application/ld+json":
                try:
                    collect(json.loads(text))
                except (ValueError, TypeError):
                    pass
            else:
                match = re.search(r"window\.__INITIAL_STATE__\s*=\s*", text)
                if match:
                    try:
                        collect(json.JSONDecoder().raw_decode(text[match.end():])[0])
                    except ValueError:
                        pass
    return "\n\n".join(pieces) if body_found else ""


def _web_metadata(result: dict[str, Any], tier="http_fast", shell=False, stripped=0, body=None) -> dict[str, Any]:
    result.update({"render_tier_used": tier, "spa_shell_detected": shell,
                   "noise_stripped_chars": stripped,
                   "content_sha256": hashlib.sha256((body if body is not None else result["markdown"]).encode("utf-8")).hexdigest()})
    return result


def extract_webpage_markdown(url: str, render_mode: str = "auto", timeout: int = 15) -> dict[str, Any]:
    if render_mode not in {"auto", "http_fast", "headless_dom"}:
        raise ValueError("render_mode must be auto, http_fast or headless_dom")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if "b23.tv" in url or "bilibili.com/video/" in url:
        binfo = inspect_bilibili_video(url)
        md_parts = [
            f"# {binfo.get('title')}\n",
            f"- **BVID**: `{binfo.get('bvid')}`",
            f"- **UP主**: {binfo.get('owner')}",
            f"- **时长**: {binfo.get('duration_sec')} 秒",
            f"- **标签**: {', '.join(str(x) for x in binfo.get('tags', []) if x)}",
            f"- **简介**: {binfo.get('desc')}\n",
        ]
        if binfo.get("subtitles_text"):
            md_parts.append(f"## 字幕全文 (CC Subtitles)\n\n{binfo['subtitles_text']}\n")
        if binfo.get("top_comments"):
            md_parts.append("## 高赞脱水评论\n\n" + "\n".join(f"- {c}" for c in binfo["top_comments"]) + "\n")
        md = "\n".join(md_parts)
        return _web_metadata({"url": url, "type": "bilibili_video", "title": binfo.get("title") or binfo["bvid"], "markdown": md, "metadata": binfo})

    try:
        req = urllib.request.Request(url, headers={"User-Agent": DESKTOP_UA, "Accept": "text/html,application/xhtml+xml"})
        guard = _CRAWL_URL_GUARD.get()
        open_url = urllib.request.build_opener(_CrawlRedirectHandler(guard)).open if guard else urllib.request.urlopen
        with open_url(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.info().get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            html_text = raw.decode("utf-8", errors="replace")
            final_url = resp.geturl()
    except Exception:
        # Preserve the existing remote-reader fallback on HTTP failures.
        if _CRAWL_URL_GUARD.get() is not None:
            raise
        fallback = _fetch_via_jina_reader(url)
        if fallback:
            return _web_metadata(fallback)
        raise

    page = _PageParser(html_text)
    title = next((_node_text(node).strip() for node in _walk_nodes(page.root) if node["tag"] == "title"), url)
    body, stripped = _clean_page(page)
    shell = _is_spa_shell(html_text)
    tier = "http_fast"
    links = page.links[:]
    if render_mode != "http_fast" and (shell or render_mode == "headless_dom"):
        hydrated = _hydrate_state(page)
        if hydrated:
            if re.search(r"<[a-z][^>]*>", hydrated, re.I):
                hydrated_page = _PageParser(hydrated)
                body, extra_stripped = _clean_page(hydrated_page)
                stripped += extra_stripped
                links.extend(hydrated_page.links)
            else:
                body = hydrated
            tier = "spa_state_hydration"
        elif callable(DOM_RENDER_HOOK):
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    rendered = DOM_RENDER_HOOK(url, timeout=timeout)
                if isinstance(rendered, str) and rendered.strip():
                    rendered_page = _PageParser(rendered)
                    body, extra_stripped = _clean_page(rendered_page)
                    stripped += extra_stripped
                    links.extend(rendered_page.links)
                    tier = "headless_dom"
            except Exception as exc:
                print(f"DOM renderer fallback: {exc}", file=sys.stderr)
    return _web_metadata({"url": url, "type": "webpage", "title": html.unescape(title),
                          "markdown": f"# {html.unescape(title)}\n\nSource: {url}\n\n{body}",
                          "links": list(dict.fromkeys(links)), "final_url": final_url}, tier, shell, stripped, body)


def _normalize_crawl_url(url: str, base: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(urllib.parse.urljoin(base, url.strip()))
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
        port = parsed.port
    except ValueError:
        return ""
    # Normalize fragments, host case and default ports; retain meaningful queries.
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port and (parsed.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
        host += f":{port}"
    decoded_path = urllib.parse.unquote(parsed.path or "/").replace("\\", "/")
    path = posixpath.normpath(decoded_path)
    if decoded_path.endswith("/") and not path.endswith("/"):
        path += "/"
    path = urllib.parse.quote(path, safe="/:@!$&'()*+,;=-._~")
    return urllib.parse.urlunsplit((parsed.scheme.lower(), host, path, parsed.query, ""))


def crawl_documentation(start_url: str, url_prefix: str | None = None, max_pages: int = 10,
                        max_depth: int = 2, kb: str | None = None, save_to_vault: bool = True,
                        resume: bool = True) -> dict[str, Any]:
    """Bounded BFS with an atomic, environment-isolated per-crawl checkpoint."""
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
        raise ValueError("max_pages must be a positive integer")
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0:
        raise ValueError("max_depth must be a nonnegative integer")
    max_pages = min(max_pages, 50)
    start_url = _normalize_crawl_url(start_url, start_url)
    if not start_url:
        raise ValueError("start_url must be an absolute HTTP(S) URL")
    url_prefix = _normalize_crawl_url(url_prefix, start_url) if url_prefix else urllib.parse.urljoin(start_url, ".")
    origin = urllib.parse.urlsplit(start_url).netloc

    def allowed(url):
        return bool(url and urllib.parse.urlsplit(url).netloc == origin and url.startswith(url_prefix))

    if not url_prefix or not allowed(start_url):
        raise ValueError("start_url must match url_prefix on the same domain")
    vault_root = Path(os.environ.get("OBSIDIAN_VAULT_ROOT") or str(Path.home() / ".antigravity-agent-policy" / "vault"))
    state_file = Path(os.environ.get("OBSIDIAN_CRAWL_STATE") or vault_root / ".obsidian_crawl_state.json")
    try:
        state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
    except (ValueError, OSError) as exc:
        if resume:
            raise ValueError(f"Cannot read crawl checkpoint: {state_file}") from exc
        state = {}
    crawl_key = hashlib.sha256(json.dumps([start_url, url_prefix, kb, str(vault_root.resolve())], ensure_ascii=False).encode("utf-8")).hexdigest()
    crawls = state.setdefault("crawls", {})
    if not resume:
        crawls.pop(crawl_key, None)
    crawl = crawls.setdefault(crawl_key, {"queue": [], "ledger": {}, "discovered": {start_url: 0}})
    ledger, discovered = crawl["ledger"], crawl["discovered"]
    # Unfinished pages take priority. Revisit known URLs to detect changed content.
    pending = crawl["queue"] or [[start_url, 0]]
    queue = []
    queued = set()
    for url, depth in sorted(pending, key=lambda item: item[1]):
        if allowed(url) and depth <= max_depth and url not in queued:
            queue.append([url, depth])
            queued.add(url)
    rechecks = []
    for url, depth in sorted(discovered.items(), key=lambda item: item[1]):
        if allowed(url) and depth <= max_depth and url not in queued:
            rechecks.append([url, depth])
            queued.add(url)
    crawl["queue"] = queue
    state["version"] = "3.1.0"

    def checkpoint():
        import tempfile
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=state_file.parent, delete=False, suffix=".tmp") as stream:
            temp_path = Path(stream.name)
            json.dump(state, stream, ensure_ascii=False, indent=2)
        try:
            os.replace(temp_path, state_file)
        finally:
            temp_path.unlink(missing_ok=True)

    bridge = None
    results, skipped, failed = [], [], []
    crawled = ingested = attempts = 0
    checkpoint()
    while (queue or rechecks) and attempts < max_pages:
        if not queue:
            queue.append(rechecks.pop(0))
        # Finish the interrupted frontier in BFS order before reviewing completed pages.
        queue.sort(key=lambda item: item[1])
        checkpoint()
        url, depth = queue[0]
        attempts += 1
        try:
            guard_token = _CRAWL_URL_GUARD.set(allowed)
            try:
                extracted = extract_webpage_markdown(url)
            finally:
                _CRAWL_URL_GUARD.reset(guard_token)
            final_url = _normalize_crawl_url(extracted.get("final_url") or url, url)
            if not allowed(final_url):
                raise ValueError("redirect_outside_crawl_boundary")
            crawled += 1
            digest = extracted.get("content_sha256") or hashlib.sha256(extracted["markdown"].encode("utf-8")).hexdigest()
            previous = ledger.get(url, {})
            cached = previous.get("content_sha256") == digest and (not save_to_vault or previous.get("ingested", False))
            # The same cleaned body under a different URL also shares an ingest record.
            duplicate = save_to_vault and any(entry.get("content_sha256") == digest and entry.get("ingested") for entry in ledger.values())
            item = {"url": url, "depth": depth, "content_sha256": digest, "ingested": False}
            if cached or duplicate:
                skipped.append(url)
                item["status"] = "skipped_cached"
            elif save_to_vault:
                if bridge is None:
                    bridge = WeKnoraBridge()
                # Batch pages frequently share a site title. Keep their filenames distinct.
                ingest_title = f"{extracted['title'][:65]} [{hashlib.sha256(url.encode('utf-8')).hexdigest()[:12]}]"
                item["weknora_ingest"] = bridge.ingest_markdown(ingest_title, extracted["markdown"], kb_id_or_name=kb, source_url=url)
                item["ingested"] = True
                item["status"] = "ingested"
                ingested += 1
            else:
                item["status"] = "extracted"
            links = extracted.get("links", [])
            for link in links:
                linked = _normalize_crawl_url(link, final_url)
                if not allowed(linked):
                    continue
                path = urllib.parse.urlsplit(linked).path.lower()
                if re.search(r"\.(?:png|jpe?g|gif|svg|webp|ico|css|js|zip|mp4|pdf)$", path):
                    continue
                if depth < max_depth:
                    discovered[linked] = min(discovered.get(linked, depth + 1), depth + 1)
                    if linked not in queued:
                        queue.append([linked, depth + 1])
                        queued.add(linked)
            ledger[url] = {"content_sha256": digest, "ingested": bool(item["ingested"] or duplicate or (cached and previous.get("ingested"))), "depth": depth}
            results.append(item)
        except Exception as exc:
            results.append({"url": url, "depth": depth, "status": "error", "error": str(exc)})
            failed.append([url, depth])
        queue.pop(0)
        checkpoint()
    queue.extend(failed)
    checkpoint()
    return {"ok": True, "start_url": start_url, "url_prefix": url_prefix,
            "pages_crawled": crawled, "pages_ingested": ingested,
            "skipped_cached_urls": skipped, "discovered_urls": list(discovered),
            "crawl_results": results, "state_file": str(state_file)}


TOOLS = [
    {
        "name": "source_connect",
        "description": "Inspect or query Obsidian vault status, hybrid knowledge search, documents, and repository connectors; legacy operations remain compatible.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "status",
                        "repository_search",
                        "repository_read",
                        "knowledge_search",
                        "document_read"
                    ]
                },
                "query": {
                    "type": "string"
                },
                "path": {
                    "type": "string"
                },
                "kb": {
                    "type": "string",
                    "description": "Optional Obsidian category or legacy knowledge base name/ID"
                }
            },
            "required": [
                "operation"
            ]
        }
    },
    {
        "name": "weknora_kb_manage",
        "description": "管理 Obsidian 静态知识库：health 状态、list 知识库、create 分类、list_docs 文档、sync_gdrive 增量镜像与本地混合索引刷新。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "health",
                        "list",
                        "create",
                        "list_docs",
                        "sync_gdrive"
                    ]
                },
                "kb": {
                    "type": "string",
                    "description": "Optional Obsidian category or legacy knowledge base name/ID"
                },
                "description": {
                    "type": "string",
                    "description": "创建知识库时的描述"
                }
            },
            "required": [
                "action"
            ]
        }
    },
    {
        "name": "weknora_rag_query",
        "description": "检索 Obsidian：search 返回 SQLite FTS5 BM25 + 384d 轻量语义向量 + 双链 RRF 章节；chat 返回原文证据与 references，由调用方模型综合回答。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索关键词或自然语言问题"
                },
                "mode": {
                    "type": "string",
                    "enum": [
                        "search",
                        "chat"
                    ],
                    "description": "search 返回原文分块，chat 返回 LLM 综合回答与引用"
                },
                "kb": {
                    "type": "string",
                    "description": "Optional Obsidian category or legacy knowledge base name/ID"
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回最大片段数（默认 8）"
                }
            },
            "required": [
                "query"
            ]
        }
    },
    {
        "name": "weknora_ingest",
        "description": "将 Markdown 或 URL 导入 Obsidian 静态知识库，自动生成 YAML、关联 WikiLinks 并刷新本地章节与向量索引。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "知识文档标题"
                },
                "content": {
                    "type": "string",
                    "description": "Markdown 正文内容"
                },
                "url": {
                    "type": "string",
                    "description": "可选：要抓取入库的网页或 B 站视频 URL"
                },
                "kb": {
                    "type": "string",
                    "description": "Optional Obsidian category or legacy knowledge base name/ID"
                }
            },
            "required": []
        }
    },
    {
        "name": "source_ingest_url",
        "description": "解析 B 站视频元数据及 MP4 或提取网页 Markdown；save_to_weknora 保留旧参数名，写入 Obsidian Vault。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "B 站短链/BV号链接或任意网页文章 URL"
                },
                "download_mp4_to": {
                    "type": "string",
                    "description": "可选：若为 B 站视频，将 MP4 视频流下载到指定本地路径以便用 read_file 观看"
                },
                "save_to_weknora": {
                    "type": "boolean",
                    "description": "是否将提取内容写入 Obsidian Vault（兼容旧参数名）"
                },
                "kb": {
                    "type": "string",
                    "description": "Optional Obsidian category or legacy knowledge base name/ID"
                }
            },
            "required": [
                "url"
            ]
        }
    }
]


TOOLS.append({
    "name": "source_crawl_documentation",
    "description": "同域/前缀有界 BFS 文档采集；支持 SHA256 去重、断点续爬与 Obsidian 批量入库。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "start_url": {"type": "string"},
            "url_prefix": {"type": ["string", "null"]},
            "max_pages": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            "max_depth": {"type": "integer", "minimum": 0, "default": 2},
            "kb": {"type": ["string", "null"]},
            "save_to_vault": {"type": "boolean", "default": True},
            "resume": {"type": "boolean", "default": True},
        },
        "required": ["start_url"],
    },
})
for _tool in TOOLS:
    if _tool["name"] == "weknora_rag_query":
        _tool["inputSchema"]["properties"]["graph_hops"] = {"type": "integer", "minimum": 1, "default": 2}
    if _tool["name"] == "source_ingest_url":
        _tool["inputSchema"]["properties"]["render_mode"] = {"type": "string", "enum": ["auto", "http_fast", "headless_dom"], "default": "auto"}
        _tool["inputSchema"]["properties"]["timeout"] = {"type": "integer", "minimum": 1, "default": 15}
    _tool["parameters"] = _tool["inputSchema"]


def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "source_crawl_documentation":
        return crawl_documentation(**args)
    bridge = WeKnoraBridge()

    if name == "source_connect":
        op = args.get("operation", "status")
        if op == "status":
            return {
                "operation": "status",
                "status": "ready",
                "weknora": bridge.health(),
                "capabilities": ["weknora_rag", "obsidian_hybrid_search", "wikilinks_graph", "bilibili_video_ingestor", "webpage_markdown_extractor", "local_repo_reader", "github_my_repos"],
            }
        if op == "github_my_repos":
            tok = _resolve_github_token()
            if not tok:
                return {"error": "Missing GITHUB_PERSONAL_ACCESS_TOKEN or GITHUB_TOKEN environment variable"}
            headers = {
                "Authorization": f"Bearer {tok}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "Antigravity-Agent",
            }
            u_req = urllib.request.Request("https://api.github.com/user", headers=headers)
            with urllib.request.urlopen(u_req, timeout=12) as resp:
                u_info = json.loads(resp.read().decode("utf-8", errors="replace"))
            r_req = urllib.request.Request(
                "https://api.github.com/user/repos?per_page=100&sort=updated&affiliation=owner,collaborator,organization_member",
                headers=headers,
            )
            with urllib.request.urlopen(r_req, timeout=15) as resp:
                repos_raw = json.loads(resp.read().decode("utf-8", errors="replace"))
            repos = [
                {
                    "full_name": r.get("full_name"),
                    "name": r.get("name"),
                    "private": r.get("private"),
                    "fork": r.get("fork"),
                    "description": r.get("description") or "",
                    "language": r.get("language") or "",
                    "default_branch": r.get("default_branch"),
                    "stars": r.get("stargazers_count", 0),
                    "updated_at": r.get("updated_at"),
                    "html_url": r.get("html_url"),
                }
                for r in repos_raw
            ]
            return {
                "login": u_info.get("login"),
                "html_url": u_info.get("html_url"),
                "repos_total_fetched": len(repos),
                "repositories": repos,
            }
        if op == "knowledge_search":
            return bridge.hybrid_search(args.get("query", ""), kb_id_or_name=args.get("kb"))
        if op == "document_read":
            target = args.get("path") or args.get("query") or ""
            if Path(target).is_file():
                return {"path": target, "content": Path(target).read_text(encoding="utf-8", errors="replace")[:30000]}
            return bridge.read_doc(target)
        if op == "repository_read":
            p = Path(args.get("path", "")).expanduser().resolve()
            return {"path": str(p), "content": p.read_text(encoding="utf-8", errors="replace")[:30000]}
        if op == "repository_search":
            root = Path(args.get("path") or ".").expanduser().resolve()
            q = (args.get("query") or "").lower()
            matches = []
            for item in root.rglob("*"):
                if item.is_file() and q in item.name.lower():
                    matches.append(str(item))
                    if len(matches) >= 30:
                        break
            return {"query": q, "root": str(root), "matches": matches}

    if name == "weknora_kb_manage":
        action = args.get("action", "health")
        if action == "health":
            return bridge.health()
        if action == "list":
            return {"knowledge_bases": bridge.list_kbs()}
        if action == "create":
            kb_name = args.get("kb") or "Core Knowledge Vault"
            return bridge.create_kb(kb_name, args.get("description", ""))
        if action == "list_docs":
            return bridge.list_docs(args.get("kb"))
        if action == "sync_gdrive":
            return _run_kb_sync_now(sync_remote=False)

    if name == "weknora_rag_query":
        q = args["query"]
        mode = args.get("mode", "search")
        if mode == "chat":
            return bridge.rag_chat(q, kb_id_or_name=args.get("kb"), graph_hops=args.get("graph_hops", 2))
        return bridge.hybrid_search(q, kb_id_or_name=args.get("kb"), match_count=int(args.get("top_k", 8)), graph_hops=args.get("graph_hops", 2))

    if name == "weknora_ingest":
        url = args.get("url")
        title = args.get("title", "")
        content = args.get("content", "")
        if url and not content:
            extracted = extract_webpage_markdown(url)
            title = title or extracted["title"]
            content = extracted["markdown"]
        if not content:
            raise ValueError("Either 'content' or 'url' must be provided for weknora_ingest.")
        return bridge.ingest_markdown(title or "Untitled Note", content, kb_id_or_name=args.get("kb"), source_url=url)

    if name == "source_ingest_url":
        url = args["url"]
        dl_to = args.get("download_mp4_to")
        if ("b23.tv" in url or "bilibili.com/video/" in url or re.match(r"^BV[a-zA-Z0-9]+$", url)) and dl_to:
            binfo = inspect_bilibili_video(url, download_mp4_to=dl_to)
            res: dict[str, Any] = {"url": url, "type": "bilibili_video", "title": binfo.get("title"), "bilibili": binfo}
            if args.get("save_to_weknora"):
                md = f"# {binfo.get('title')}\n\n- BVID: {binfo.get('bvid')}\n- UP主: {binfo.get('owner')}\n- 简介: {binfo.get('desc')}\n"
                res["weknora_ingest"] = bridge.ingest_markdown(binfo.get("title") or binfo["bvid"], md, kb_id_or_name=args.get("kb"), source_url=url)
            return res

        extracted = extract_webpage_markdown(url, **{key: args[key] for key in ("render_mode", "timeout") if key in args})
        if args.get("save_to_weknora"):
            extracted["weknora_ingest"] = bridge.ingest_markdown(
                extracted["title"], extracted["markdown"], kb_id_or_name=args.get("kb"), source_url=url
            )
        return extracted

    raise ValueError(f"Unknown tool: {name}")


call_tool = dispatch


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        method = req.get("method")
        req_id = req.get("id")
        if method == "initialize":
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "source-connectors", "version": "3.1.0"},
                        },
                    }
                ),
                flush=True,
            )
        elif method == "tools/list":
            print(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}, ensure_ascii=False), flush=True)
        elif method == "tools/call":
            params = req.get("params") or {}
            tname = params.get("name", "")
            targs = params.get("arguments") or {}
            try:
                data = dispatch(tname, targs)
                print(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}], "isError": False},
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {"content": [{"type": "text", "text": json.dumps({"error": str(exc)}, ensure_ascii=False)}], "isError": True},
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        elif req_id is not None:
            print(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {}}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
