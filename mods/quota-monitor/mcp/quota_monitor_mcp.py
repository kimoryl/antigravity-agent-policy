#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Quota Monitor MCP Server v1.4.0 (quota-monitor Mod)
Provides real-time remaining quota, rate-limit window, reset countdown, active task load,
and task-scoped budget circuit breaker / 180s zero-token stall guard across CLI Agents.

Capacity & Routing Philosophy (Gemini-First, Elite-On-Demand):
- Gemini Pool (`antigravity_gemini`: AGY-CLI) has NO weekly cap and its total capacity
  exceeds `Opus-CLI` + `Codex-CLI` (Sol) combined. `AGY-CLI` is the Primary Full-Stack Workhorse.
- `Opus-CLI` (`claude-opus-5-5-high`) and `Codex-CLI` (`gpt-6.1-sol`) are weekly-capped Elite
  Specialists reserved for high-difficulty architecture/deep-reasoning/aesthetics (`Opus-CLI`)
  and precision backend/test-loop攻坚 (`Codex-CLI`).
"""

import copy
import ctypes
from ctypes import wintypes
import datetime
import glob
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, List, Optional, Tuple


AGENTS_META = {
    "AGY-CLI": {
        "id": "agent-agy-cli",
        "key": "agy-cli",
        "runtime": "Antigravity",
        "model": "gemini-4-carbon",
        "submodels": {
            "deep_reasoning_core": "gemini-4-carbon-high",
            "prose_engine": "gemini-4-argon-high",
            "standard_coding_core": "gemini-4-hydrogen-high",
            "high_density_coding_core": "gemini-4-hydrogen-xhigh",
            "fast_explore_core": "gemini-4-hydrogen-medium",
        },
        "quota_pool": "antigravity_gemini",
        "capacity_tier": "abundant_primary (海量无周限额主力池 - 额度 > Opus+Sol 之和)",
        "base_capacity_bonus": 22.0,
        "role": "第一主力代码研发、正式长文与全栈主战子Agent（Gemini 4 三核协处理器：gemini-4-carbon-high 逻辑与攻坚重核、gemini-4-argon-high 正式长文与叙事主笔核、gemini-4-hydrogen-high/xhigh/medium 常规编码与高频快反核）",
        "keywords": [
            "开发", "实现", "功能", "全栈", "前端", "后端", "重构", "修复", "脚本", "配置",
            "复杂思考", "深度思考", "深度推理", "复杂推理", "方案推演", "算法推导", "根因分析",
            "工程报告", "正式长文", "招投标", "撰稿", "大纲", "人设", "润色", "伴飞", "蒸馏",
            "多模态", "浏览器", "网页", "截图", "devtools", "自动化", "分布式", "系统",
            "检索", "全库", "扫描", "摸底", "分析", "文档", "ppt", "google cloud", "gcp",
            "agy", "gemini", "carbon", "argon", "hydrogen"
        ],
    },
    "Codex-CLI": {
        "id": "agent-codex-cli",
        "key": "codex-cli",
        "runtime": "Codex",
        "model": "gpt-6.1-sol",
        "quota_pool": "openai_codex",
        "capacity_tier": "specialist_weekly_capped (精锐特种池 - 受7天周限额约束，定向攻坚)",
        "base_capacity_bonus": 2.0,
        "role": "精锐后端攻坚、异构红队审查与测试闭环子Agent（定向用于高密度核心算法/接口攻坚、异构Code Review、棘手单元测试自动修复闭环）",
        "keywords": [
            "核心算法", "单测闭环", "单元测试", "pytest", "集成测试", "高密度后端",
            "异构审查", "review", "流水线", "ci/cd", "codex", "sol"
        ],
    },
    "Opus-CLI": {
        "id": "agent-opus-cli",
        "key": "opus-cli",
        "runtime": "Claude-Code-CLI",
        "model": "claude-opus-5-5-high",
        "quota_pool": "antigravity_claude_3p",
        "capacity_tier": "elite_weekly_capped (精锐王牌池 - 预留 Claude Code CLI 通道)",
        "base_capacity_bonus": 0.0,
        "role": "王牌架构与高审美UI特种专家（预留 Claude Code CLI 通道，用于复杂顶层架构终审与高审美UI核心设计）",
        "keywords": [
            "顶层架构", "复杂架构", "疑难", "死锁", "竞态", "根因",
            "高审美", "旗舰", "终审", "安全审计", "opus", "claude"
        ],
    },
}


CODEX_POOL_POLICY = {
    "shared_chatgpt_codex_pool": True,
    "budget_protection_policy": "ChatGPT & Codex 共享额度池保护 + CLI 输出净化降噪",
}

QUOTA_CACHE_TTL_SEC = 15.0
_QUOTA_MEM_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_CACHE_LOCK = threading.RLock()


def clear_quota_cache() -> None:
    """Clear in-memory short-lived quota fetch cache."""
    with _CACHE_LOCK:
        _QUOTA_MEM_CACHE.clear()


def _get_cached_quota(cache_key: str, ttl_sec: float = QUOTA_CACHE_TTL_SEC) -> Optional[Dict[str, Any]]:
    if ttl_sec <= 0:
        return None
    with _CACHE_LOCK:
        entry = _QUOTA_MEM_CACHE.get(cache_key)
        if not entry:
            return None
        ts, payload = entry
        if (time.monotonic() - ts) <= ttl_sec:
            return copy.deepcopy(payload)
        _QUOTA_MEM_CACHE.pop(cache_key, None)
        return None


def _set_cached_quota(cache_key: str, payload: Dict[str, Any]) -> None:
    with _CACHE_LOCK:
        _QUOTA_MEM_CACHE[cache_key] = (time.monotonic(), copy.deepcopy(payload))


def _unavailable_pool(
    pool_name: str,
    capacity_tier: str,
    extra: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    pool: Dict[str, Any] = {
        "pool_name": pool_name,
        "capacity_tier": capacity_tier,
        "available": False,
        "status": "unavailable",
        "primary_5h_remaining_percent": None,
        "primary_5h_used_percent": None,
        "primary_5h_reset_local": None,
        "primary_5h_reset_in_minutes": None,
        "secondary_weekly_remaining_percent": None,
        "secondary_weekly_used_percent": None,
        "secondary_weekly_reset_local": None,
        "secondary_weekly_reset_in_minutes": None,
        "effective_remaining_percent": None,
        "health": "unavailable",
    }
    if extra:
        pool.update(extra)
    if error:
        pool["error"] = error
    return pool


def _iter_file_lines_reverse(
    filepath: str,
    chunk_size: int = 65536,
    max_bytes: int = 8 * 1024 * 1024,
) -> Iterator[str]:
    """Stream lines from the end of a potentially huge log/JSONL file without reading the whole file into memory."""
    try:
        with open(filepath, "rb") as fb:
            fb.seek(0, os.SEEK_END)
            file_size = fb.tell()
            if file_size <= 0:
                return
            min_pos = max(0, file_size - max_bytes)
            pos = file_size
            tail = b""
            while pos > min_pos:
                read_len = min(chunk_size, pos - min_pos)
                pos -= read_len
                fb.seek(pos, os.SEEK_SET)
                data = fb.read(read_len) + tail
                parts = data.split(b"\n")
                tail = parts[0]
                for raw_line in reversed(parts[1:]):
                    text = raw_line.rstrip(b"\r").decode("utf-8", errors="ignore")
                    if text:
                        yield text
            if pos == 0 and tail:
                text = tail.rstrip(b"\r").decode("utf-8", errors="ignore")
                if text:
                    yield text
            return
    except (OSError, TypeError, ValueError, io.UnsupportedOperation):
        pass

    with open(filepath, "r", encoding="utf-8", errors="ignore") as ft:
        recent: List[str] = []
        for line in ft:
            recent.append(line.rstrip("\r\n"))
            if len(recent) > 2048:
                recent = recent[-1024:]
        for line in reversed(recent):
            if line:
                yield line


def _parse_iso_to_local(iso_str: Optional[str]) -> Tuple[Optional[str], Optional[float]]:
    if not iso_str:
        return None, None
    try:
        s = iso_str.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(s)
        now = datetime.datetime.now(datetime.timezone.utc)
        diff_sec = (dt - now).total_seconds()
        local_dt = dt.astimezone()
        return local_dt.strftime("%Y-%m-%d %H:%M:%S"), round(max(0.0, diff_sec / 60.0), 1)
    except Exception:
        return iso_str, None


def _epoch_to_local(epoch_sec: Optional[float]) -> Tuple[Optional[str], Optional[float]]:
    if not epoch_sec:
        return None, None
    try:
        dt = datetime.datetime.fromtimestamp(float(epoch_sec), tz=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        diff_sec = (dt - now).total_seconds()
        local_dt = dt.astimezone()
        return local_dt.strftime("%Y-%m-%d %H:%M:%S"), round(max(0.0, diff_sec / 60.0), 1)
    except Exception:
        return None, None


def _progress_bar(remaining_pct: Optional[float], width: int = 8) -> str:
    if remaining_pct is None:
        return "[" + "?" * width + "]"
    pct = max(0.0, min(100.0, float(remaining_pct)))
    filled = int(round((pct / 100.0) * width))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def _health_level(remaining_pct: Optional[float]) -> str:
    if remaining_pct is None:
        return "unknown"
    if remaining_pct <= 0.5:
        return "exhausted"
    if remaining_pct < 15.0:
        return "critical"
    if remaining_pct < 35.0:
        return "warning"
    return "healthy"


def _has_valid_remaining_percent(bucket: Any) -> bool:
    if not isinstance(bucket, dict):
        return False
    val = bucket.get("remaining_percent")
    return isinstance(val, (int, float)) and not isinstance(val, bool) and not math.isnan(float(val))


def _select_gemini_quota_bucket(models: Dict[str, Any]) -> Dict[str, Any]:
    """Select active Gemini quota bucket: prefer gemini-4-*, then legacy tiers, then any gemini-* bucket."""
    if not isinstance(models, dict) or not models:
        return {}

    preferred_v4 = (
        "gemini-4-carbon-high",
        "gemini-4-carbon",
        "gemini-4-argon-high",
        "gemini-4-argon",
        "gemini-4-hydrogen-high",
        "gemini-4-hydrogen-xhigh",
        "gemini-4-hydrogen-medium",
        "gemini-4-hydrogen",
    )
    for mid in preferred_v4:
        b = models.get(mid)
        if _has_valid_remaining_percent(b):
            return b

    for mid, b in models.items():
        if isinstance(mid, str) and mid.startswith("gemini-4-") and _has_valid_remaining_percent(b):
            return b

    for mid in ("gemini-3.8-flash-tiered", "gemini-2.5-pro", "gemini-3-flash", "gemini-3-flash-agent"):
        b = models.get(mid)
        if _has_valid_remaining_percent(b):
            return b

    for mid, b in models.items():
        if isinstance(mid, str) and mid.startswith("gemini-") and _has_valid_remaining_percent(b):
            return b

    return {}


def _select_claude_quota_bucket(models: Dict[str, Any]) -> Dict[str, Any]:
    """Select active Claude/3P model quota bucket without matching Gemini models."""
    if not isinstance(models, dict) or not models:
        return {}
    preferred_claude = (
        "claude-opus-5-5-high",
        "claude-opus-5-5",
        "claude-sonnet-5-5-high",
        "claude-sonnet-5-5",
    )
    for mid in preferred_claude:
        b = models.get(mid)
        if _has_valid_remaining_percent(b):
            return b
    for mid, b in models.items():
        if isinstance(mid, str) and ("claude" in mid.lower() or mid.lower().startswith("3p-")) and _has_valid_remaining_percent(b):
            return b
    return {}


def _is_claude_3p_bucket(group: Dict[str, Any], bucket: Dict[str, Any]) -> bool:
    """Verify a summary quota bucket genuinely belongs to Claude / 3P and NOT Gemini."""
    if not isinstance(bucket, dict):
        return False
    bucket_id = str(bucket.get("bucketId") or bucket.get("id") or "").strip().lower()
    model_id = str(bucket.get("modelId") or bucket.get("name") or "").strip().lower()
    group_dict = group if isinstance(group, dict) else {}
    group_fields = " ".join(
        str(group_dict.get(k) or "")
        for k in ("groupId", "id", "name", "displayName", "family", "provider", "pool")
    ).lower()
    raw_gmodels = group_dict.get("modelIds") or group_dict.get("models") or []
    group_models = " ".join(str(m) for m in raw_gmodels if isinstance(m, (str, int, float))).lower()

    if "gemini" in bucket_id or "gemini" in model_id:
        return False
    has_explicit_3p_bucket = bucket_id.startswith("3p-") or any(
        tok in bucket_id or tok in model_id for tok in ("claude", "anthropic", "opus", "sonnet")
    )
    if "gemini" in group_fields and not has_explicit_3p_bucket:
        return False
    if group_models and "gemini" in group_models and not any(
        tok in group_models for tok in ("claude", "anthropic", "3p", "opus", "sonnet")
    ) and not has_explicit_3p_bucket:
        return False

    combined = f"{group_fields} {group_models} {bucket_id} {model_id}"
    return has_explicit_3p_bucket or any(
        tok in combined for tok in ("claude", "anthropic", "3p", "third_party", "third-party", "opus", "sonnet")
    )


def _read_windows_keyring_cred(target_name: str) -> Optional[Dict[str, Any]]:
    if os.name != "nt":
        return None
    try:
        advapi32 = ctypes.WinDLL("advapi32.dll", use_last_error=True)

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        PCREDENTIALW = ctypes.POINTER(CREDENTIALW)
        pcred = PCREDENTIALW()
        if advapi32.CredReadW(target_name, 1, 0, ctypes.byref(pcred)):
            c = pcred.contents
            blob = bytes(c.CredentialBlob[: c.CredentialBlobSize]).decode("utf-8", errors="ignore")
            advapi32.CredFree(pcred)
            return json.loads(blob)
    except Exception:
        pass
    return None


def fetch_antigravity_quotas(
    *,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Fetch real-time quotas for Antigravity models (Gemini pool & Claude/3P pool)."""
    if use_cache and not force_refresh:
        cached = _get_cached_quota("antigravity")
        if cached is not None:
            return cached

    claude_pool_name = "Antigravity Claude/3P 精锐池 (支持 Claude Ultra 动态窗口，受周限额保护)"
    gemini_pool_name = "Antigravity Gemini 海量主力池 (无周限额，总容量 > Opus+Sol 之和)"
    claude_extra = {
        "five_hour_cap_active": False,
        "prompt_cache_ttl_policy": "5m TTL (避免中途挂载/卸载工具触发 Cache Tax 失效重算)",
    }

    result: Dict[str, Any] = {
        "source": "cloudcode-pa.googleapis.com",
        "ok": False,
        "pools": {
            "antigravity_claude_3p": _unavailable_pool(claude_pool_name, "elite_weekly_capped", extra=claude_extra),
            "antigravity_gemini": _unavailable_pool(gemini_pool_name, "abundant_primary"),
        },
        "models": {},
        "groups": [],
    }
    stored = _read_windows_keyring_cred("gemini:antigravity")
    if not stored:
        err_msg = "Windows Keyring credential 'gemini:antigravity' not found"
        result["error"] = err_msg
        result["pools"]["antigravity_claude_3p"]["error"] = err_msg
        result["pools"]["antigravity_gemini"]["error"] = err_msg
        return result

    tok_obj = stored.get("token", {})
    refresh_token = tok_obj.get("refresh_token")
    access_token = tok_obj.get("access_token")
    project_id = stored.get("project_id") or "default-cli-project"
    for pfile in [
        str(Path(os.environ.get("AGY_CLI_HOME", str(Path.home() / ".antigravity-cli-home"))) / ".gemini" / "antigravity-cli" / "cache" / "default_project_id.txt"),
        str(Path.home() / ".gemini" / "antigravity-cli" / "cache" / "default_project_id.txt"),
    ]:
        if os.path.exists(pfile):
            try:
                with open(pfile, "r", encoding="utf-8") as pf:
                    val = pf.read().strip()
                if val:
                    project_id = val
                    break
            except Exception:
                pass

    oauth_pairs: list[tuple[str, str]] = []
    for cid_cand, csec_cand in (
        (os.environ.get("AGY_OAUTH_CLIENT_ID", "").strip(), os.environ.get("AGY_OAUTH_CLIENT_SECRET", "").strip()),
        (str(stored.get("client_id") or tok_obj.get("client_id") or "").strip(), str(stored.get("client_secret") or tok_obj.get("client_secret") or "").strip()),
    ):
        if cid_cand and csec_cand and (cid_cand, csec_cand) not in oauth_pairs:
            oauth_pairs.append((cid_cand, csec_cand))

    if refresh_token and oauth_pairs:
        for cid, csec in oauth_pairs:
            try:
                body = urllib.parse.urlencode(
                    {
                        "client_id": cid,
                        "client_secret": csec,
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                    }
                ).encode("utf-8")
                req = urllib.request.Request("https://oauth2.googleapis.com/token", data=body, method="POST")
                with urllib.request.urlopen(req, timeout=8) as r:
                    access_token = json.loads(r.read().decode("utf-8"))["access_token"]
                    break
            except Exception:
                continue

    if not access_token:
        err_msg = "Failed to obtain Antigravity access_token"
        result["error"] = err_msg
        result["pools"]["antigravity_claude_3p"]["error"] = err_msg
        result["pools"]["antigravity_gemini"]["error"] = err_msg
        return result

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "antigravity/2.0",
    }
    payload_bytes = json.dumps({"project": project_id}).encode("utf-8")

    try:
        req_sum = urllib.request.Request(
            "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
            data=payload_bytes,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req_sum, timeout=8) as r:
            sum_data = json.loads(r.read().decode("utf-8"))
            result["groups"] = sum_data.get("groups", [])
    except Exception as e:
        result["summary_warning"] = str(e)

    try:
        req_q = urllib.request.Request(
            "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
            data=payload_bytes,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req_q, timeout=8) as r:
            q_data = json.loads(r.read().decode("utf-8"))
            for b in q_data.get("buckets", []):
                mid = b.get("modelId")
                if not mid:
                    continue
                rem_frac = float(b.get("remainingFraction", 0.0))
                rem_pct = round(rem_frac * 100.0, 2)
                used_pct = round(max(0.0, 100.0 - rem_pct), 2)
                reset_local, reset_mins = _parse_iso_to_local(b.get("resetTime"))
                result["models"][mid] = {
                    "remaining_percent": rem_pct,
                    "used_percent": used_pct,
                    "reset_time_utc": b.get("resetTime"),
                    "reset_time_local": reset_local,
                    "reset_in_minutes": reset_mins,
                }
        result["ok"] = True
    except Exception as e:
        err_msg = str(e)
        result["error"] = err_msg
        result["pools"]["antigravity_claude_3p"]["error"] = err_msg
        result["pools"]["antigravity_gemini"]["error"] = err_msg
        return result

    # Pool A: antigravity_claude_3p (strictly verify Claude / 3P bucket ownership; never cross-pollinate Gemini)
    claude_5h = dict(_select_claude_quota_bucket(result["models"]))
    claude_weekly_pct: Optional[float] = None
    claude_weekly_reset_local = None
    claude_weekly_reset_mins = None
    has_5h_bucket = bool(claude_5h)
    has_weekly_bucket = False
    for g in result.get("groups", []):
        for b in (g.get("buckets", []) if isinstance(g, dict) else []):
            if not _is_claude_3p_bucket(g, b):
                continue
            bid = str(b.get("bucketId") or "").strip().lower()
            win = str(b.get("window") or "").strip().lower()
            if bid == "3p-weekly" or win == "weekly":
                has_weekly_bucket = True
                claude_weekly_pct = round(float(b.get("remainingFraction", 0.0)) * 100.0, 2)
                claude_weekly_reset_local, claude_weekly_reset_mins = _parse_iso_to_local(b.get("resetTime"))
            elif bid == "3p-5h" or win == "5h":
                has_5h_bucket = True
                if not claude_5h:
                    rem_p = round(float(b.get("remainingFraction", 0.0)) * 100.0, 2)
                    rl, rm = _parse_iso_to_local(b.get("resetTime"))
                    claude_5h = {
                        "remaining_percent": rem_p,
                        "used_percent": round(max(0.0, 100.0 - rem_p), 2),
                        "reset_time_local": rl,
                        "reset_in_minutes": rm,
                    }

    if has_5h_bucket or has_weekly_bucket:
        p5_val = claude_5h.get("remaining_percent")
        if p5_val is None:
            p5_val = claude_weekly_pct
        if claude_weekly_pct is None:
            claude_weekly_pct = p5_val
        c_eff_pct = min(p5_val, claude_weekly_pct) if (p5_val is not None and claude_weekly_pct is not None) else (p5_val if p5_val is not None else claude_weekly_pct)
        result["pools"]["antigravity_claude_3p"] = {
            "pool_name": claude_pool_name,
            "capacity_tier": "elite_weekly_capped",
            "available": True,
            "status": "ok",
            "five_hour_cap_active": has_5h_bucket,
            "prompt_cache_ttl_policy": "5m TTL (避免中途挂载/卸载工具触发 Cache Tax 失效重算)",
            "primary_5h_remaining_percent": p5_val,
            "primary_5h_used_percent": claude_5h.get("used_percent", round(100.0 - p5_val, 2) if p5_val is not None else None),
            "primary_5h_reset_local": claude_5h.get("reset_time_local") or claude_weekly_reset_local,
            "primary_5h_reset_in_minutes": claude_5h.get("reset_in_minutes") or claude_weekly_reset_mins,
            "secondary_weekly_remaining_percent": claude_weekly_pct,
            "secondary_weekly_used_percent": round(100.0 - claude_weekly_pct, 2) if claude_weekly_pct is not None else None,
            "secondary_weekly_reset_local": claude_weekly_reset_local,
            "secondary_weekly_reset_in_minutes": claude_weekly_reset_mins,
            "effective_remaining_percent": c_eff_pct,
            "health": _health_level(c_eff_pct),
        }

    # Pool B: antigravity_gemini (Gemini 4 Carbon / Argon / Hydrogen + legacy fallback)
    gem_info = _select_gemini_quota_bucket(result["models"])
    if _has_valid_remaining_percent(gem_info):
        g_rem = float(gem_info["remaining_percent"])
        result["pools"]["antigravity_gemini"] = {
            "pool_name": gemini_pool_name,
            "capacity_tier": "abundant_primary",
            "available": True,
            "status": "ok",
            "primary_5h_remaining_percent": g_rem,
            "primary_5h_used_percent": gem_info.get("used_percent", round(100.0 - g_rem, 2)),
            "primary_5h_reset_local": gem_info.get("reset_time_local"),
            "primary_5h_reset_in_minutes": gem_info.get("reset_in_minutes"),
            "secondary_weekly_remaining_percent": None,
            "effective_remaining_percent": g_rem,
            "health": _health_level(g_rem),
        }

    if use_cache:
        _set_cached_quota("antigravity", result)
    return result


def fetch_codex_quotas(
    *,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Fetch real-time quotas for OpenAI Codex (live wham/usage API + local session JSONL fallback)."""
    if use_cache and not force_refresh:
        cached = _get_cached_quota("codex")
        if cached is not None:
            return cached

    result: Dict[str, Any] = {
        "source": "chatgpt.com/backend-api/wham/usage",
        "ok": False,
        "pool": _unavailable_pool(
            "OpenAI Codex 精锐特种池",
            "specialist_weekly_capped",
            extra=dict(CODEX_POOL_POLICY),
        ),
        "latest_session_tokens": None,
    }

    auth_files = [
        str(Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"),
        str(Path.home() / ".codex" / "auth.json"),
    ]
    for af in auth_files:
        if os.path.exists(af):
            try:
                with open(af, "r", encoding="utf-8") as auth_stream:
                    auth = json.load(auth_stream)
                tokens = auth.get("tokens", {})
                access_token = tokens.get("access_token")
                account_id = tokens.get("account_id")
                if access_token:
                    headers = {
                        "Authorization": f"Bearer {access_token}",
                        "User-Agent": "codex_cli_rs/0.1.0",
                    }
                    if account_id:
                        headers["ChatGPT-Account-Id"] = account_id
                    req = urllib.request.Request("https://chatgpt.com/backend-api/wham/usage", headers=headers)
                    with urllib.request.urlopen(req, timeout=8) as r:
                        data = json.loads(r.read().decode("utf-8"))
                        rl = data.get("rate_limit", {})
                        pw = rl.get("primary_window") or {}
                        sw = rl.get("secondary_window") or {}
                        p_used = float(pw.get("used_percent", 0.0))
                        p_rem = round(max(0.0, 100.0 - p_used), 2)
                        p_reset_local, p_reset_mins = _epoch_to_local(pw.get("reset_at"))

                        s_used = float(sw.get("used_percent", 0.0))
                        s_rem = round(max(0.0, 100.0 - s_used), 2)
                        s_reset_local, s_reset_mins = _epoch_to_local(sw.get("reset_at"))

                        eff_rem = min(p_rem, s_rem)
                        result["ok"] = True
                        result["plan_type"] = data.get("plan_type")
                        result["email"] = data.get("email")
                        result["pool"] = {
                            **CODEX_POOL_POLICY,
                            "pool_name": f"OpenAI Codex 精锐特种池 (Plan: {data.get('plan_type', 'plus')}，受7天周限额约束)",
                            "capacity_tier": "specialist_weekly_capped",
                            "available": True,
                            "status": "ok",
                            "allowed": rl.get("allowed", True),
                            "limit_reached": rl.get("limit_reached", False),
                            "primary_5h_remaining_percent": p_rem,
                            "primary_5h_used_percent": p_used,
                            "primary_5h_reset_local": p_reset_local,
                            "primary_5h_reset_in_minutes": p_reset_mins,
                            "secondary_weekly_remaining_percent": s_rem,
                            "secondary_weekly_used_percent": s_used,
                            "secondary_weekly_reset_local": s_reset_local,
                            "secondary_weekly_reset_in_minutes": s_reset_mins,
                            "effective_remaining_percent": eff_rem,
                            "health": "exhausted" if rl.get("limit_reached") else _health_level(eff_rem),
                        }
                        break
            except Exception as e:
                result["live_api_warning"] = str(e)

    for chome in list(dict.fromkeys([os.environ.get("CODEX_HOME", str(Path.home() / ".codex")), str(Path.home() / ".codex")])):
        sessions = glob.glob(os.path.join(chome, "sessions", "**", "*.jsonl"), recursive=True)
        if sessions:
            latest = max(sessions, key=os.path.getmtime)
            try:
                for line in _iter_file_lines_reverse(latest):
                    if '"token_count"' in line:
                        ev = json.loads(line)
                        pl = ev.get("payload", {})
                        info = pl.get("info") or {}
                        result["latest_session_tokens"] = info.get("total_token_usage")
                        if not result["ok"] and pl.get("rate_limits"):
                            rl = pl["rate_limits"]
                            pw = rl.get("primary") or {}
                            sw = rl.get("secondary") or {}
                            p_used = float(pw.get("used_percent", 0.0))
                            p_rem = round(max(0.0, 100.0 - p_used), 2)
                            p_reset_local, p_reset_mins = _epoch_to_local(pw.get("resets_at"))
                            s_used = float(sw.get("used_percent", 0.0))
                            s_rem = round(max(0.0, 100.0 - s_used), 2)
                            s_reset_local, s_reset_mins = _epoch_to_local(sw.get("resets_at"))
                            eff_rem = min(p_rem, s_rem)
                            result["ok"] = True
                            result["source"] = f"session_jsonl ({os.path.basename(latest)})"
                            result["pool"] = {
                                **CODEX_POOL_POLICY,
                                "pool_name": "OpenAI Codex 精锐特种池 (Local Session Rollout)",
                                "capacity_tier": "specialist_weekly_capped",
                                "available": True,
                                "status": "ok",
                                "primary_5h_remaining_percent": p_rem,
                                "primary_5h_used_percent": p_used,
                                "primary_5h_reset_local": p_reset_local,
                                "primary_5h_reset_in_minutes": p_reset_mins,
                                "secondary_weekly_remaining_percent": s_rem,
                                "secondary_weekly_used_percent": s_used,
                                "secondary_weekly_reset_local": s_reset_local,
                                "secondary_weekly_reset_in_minutes": s_reset_mins,
                                "effective_remaining_percent": eff_rem,
                                "health": _health_level(eff_rem),
                            }
                        break
            except Exception:
                pass
            if result.get("latest_session_tokens"):
                break

    if result["ok"] and use_cache:
        _set_cached_quota("codex", result)
    return result


def fetch_agent_active_load() -> Dict[str, Dict[str, Any]]:
    """Return zero-overhead local task load dict without spawning external daemon processes."""
    return {
        name: {"in_progress": 0, "todo": 0, "active_titles": []} for name in AGENTS_META
    }


def _normalize_pool_status(raw_pool: Dict[str, Any], default_name: str, default_tier: str) -> Dict[str, Any]:
    if not isinstance(raw_pool, dict) or not raw_pool:
        return _unavailable_pool(default_name, default_tier)
    pool = dict(raw_pool)
    eff = pool.get("effective_remaining_percent")
    has_num = isinstance(eff, (int, float)) and not isinstance(eff, bool) and not math.isnan(float(eff))
    if "available" not in pool:
        pool["available"] = has_num
    if "status" not in pool:
        pool["status"] = "ok" if pool["available"] else "unavailable"
    if not pool["available"] and pool.get("health") in (None, "unknown", "healthy"):
        pool["health"] = "unavailable"
    elif "health" not in pool:
        pool["health"] = _health_level(eff) if has_num else "unavailable"
    return pool


def build_full_quota_report(
    include_raw_models: bool = False,
    *,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    if use_cache and not force_refresh:
        agy = fetch_antigravity_quotas()
        codex = fetch_codex_quotas()
    else:
        agy = fetch_antigravity_quotas(use_cache=use_cache, force_refresh=force_refresh)
        codex = fetch_codex_quotas(use_cache=use_cache, force_refresh=force_refresh)
    loads = fetch_agent_active_load()

    pools = {
        "antigravity_gemini": _normalize_pool_status(
            agy.get("pools", {}).get("antigravity_gemini", {}),
            "Antigravity Gemini 海量主力池 (无周限额，总容量 > Opus+Sol 之和)",
            "abundant_primary",
        ),
        "antigravity_claude_3p": _normalize_pool_status(
            agy.get("pools", {}).get("antigravity_claude_3p", {}),
            "Antigravity Claude/3P 精锐池 (支持 Claude Ultra 动态窗口，受周限额保护)",
            "elite_weekly_capped",
        ),
        "openai_codex": {
            **_normalize_pool_status(
                codex.get("pool", {}),
                "OpenAI Codex 精锐特种池",
                "specialist_weekly_capped",
            ),
            **CODEX_POOL_POLICY,
        },
    }

    agents_status: Dict[str, Any] = {}
    md_rows: List[str] = [
        "| 智能体 (Agent) | 定位与容量体量 | 底层模型 | 5h 窗口剩余 | 周窗口剩余 | 重置时间 (本地) | 在途任务 | 状态 |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]

    for name, meta in AGENTS_META.items():
        pool_key = meta["quota_pool"]
        pool_info = pools.get(pool_key) or {}
        available = bool(pool_info.get("available", False))
        status_str = str(pool_info.get("status", "ok" if available else "unavailable"))
        p5_rem = pool_info.get("primary_5h_remaining_percent")
        wk_rem = pool_info.get("secondary_weekly_remaining_percent")
        eff_rem = pool_info.get("effective_remaining_percent")
        reset_local = pool_info.get("primary_5h_reset_local") or pool_info.get("secondary_weekly_reset_local") or "-"
        reset_mins = pool_info.get("primary_5h_reset_in_minutes")
        health = pool_info.get("health", "unavailable" if not available else "unknown")
        load = loads.get(name, {"in_progress": 0, "todo": 0, "active_titles": []})

        agent_entry: Dict[str, Any] = {
            "agent_id": meta["id"],
            "agent_key": meta["key"],
            "runtime": meta["runtime"],
            "model": meta["model"],
            "quota_pool": pool_key,
            "capacity_tier": meta["capacity_tier"],
            "role": meta["role"],
            "available": available,
            "status": status_str,
            "primary_5h_remaining_percent": p5_rem,
            "secondary_weekly_remaining_percent": wk_rem,
            "effective_remaining_percent": eff_rem,
            "reset_time_local": reset_local,
            "reset_in_minutes": reset_mins,
            "health": health,
            "can_dispatch": available and health not in ("exhausted", "critical", "unavailable"),
            "active_load": load,
        }
        if "submodels" in meta:
            agent_entry["submodels"] = dict(meta["submodels"])
        agents_status[name] = agent_entry

        p5_str = f"{_progress_bar(p5_rem, 8)} **{p5_rem:.1f}%**" if p5_rem is not None else "N/A"
        if wk_rem is not None:
            wk_str = f"**{wk_rem:.1f}%** (周限额)"
        elif pool_key == "antigravity_gemini" and available:
            wk_str = "**∞ 无周上限**"
        else:
            wk_str = "N/A"
        reset_str = f"{reset_local} ({int(reset_mins)}m)" if reset_mins is not None else reset_local
        load_str = f"{load['in_progress']} 运行 / {load['todo']} 待办"
        badge = {
            "healthy": "🟢 充足",
            "warning": "🟡 注意",
            "critical": "🟠 紧缺",
            "exhausted": "🔴 耗尽",
            "unavailable": "⚪ 不可用",
        }.get(health, "⚪ 未知")
        tier_short = "🔥 海量主力 (>Opus+Sol)" if pool_key == "antigravity_gemini" else "💎 精锐特种 (周限额保护)"
        md_rows.append(
            f"| **{name}** | {tier_short} | `{meta['model']}` | {p5_str} | {wk_str} | {reset_str} | {load_str} | {badge} |"
        )

    report_out: Dict[str, Any] = {
        "timestamp": datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
        "routing_policy": "Gemini 4 Tri-Core + Heterogeneous Balance (Antigravity 主会话任总控调度；AGY-CLI 三核分流 carbon-high 攻坚 / argon-high 正式长文主笔 / hydrogen-high|medium 常规编码与快反；Codex-CLI 负责测试与验收；Opus-CLI 负责顶层架构终审与高审美 UI)",
        "summary_markdown": "\n".join(md_rows),
        "agents": agents_status,
        "pools": pools,
        "codex_latest_session_tokens": codex.get("latest_session_tokens"),
    }
    if include_raw_models:
        report_out["raw_antigravity_models"] = agy.get("models", {})
    return report_out


_CORE_SECURITY_DEADLOCK_KEYWORDS = (
    "死锁", "deadlock", "竞态", "race condition", "安全漏洞", "高危漏洞",
    "漏洞修复", "红队修复", "核心安全", "security vulnerability", "cve",
)
_PROSE_ENGINE_KEYWORDS = (
    "正式长文", "工程报告", "正式报告", "招投标", "标书", "ppt叙事", "ppt 叙事",
    "叙事大纲", "大纲", "人设", "撰稿", "技术周报",
    "复盘报告", "writing_", "presentation_planner", "learning_",
)
_DEEP_REASONING_KEYWORDS = (
    "复杂重构", "跨模块重构", "核心算法", "死锁", "deadlock", "竞态", "race condition",
    "安全漏洞", "高危漏洞", "漏洞修复", "红队修复", "核心安全",     "自进化", "深水区", "根因分析",
)
_FAST_EXPLORE_KEYWORDS = (
    "检索", "摸底", "扫描", "小脚本", "配置微调", "单行微调", "伴飞", "机械对账", "蒸馏",
)


def recommend_gemini_submodel(
    task_description: str,
    gemini_5h_remaining_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """Route inside AGY-CLI Gemini 4 Tri-Core (Carbon + Argon + Hydrogen) with <30% 5h pool guard."""
    desc_lower = (task_description or "").lower()
    security_hits = [kw for kw in _CORE_SECURITY_DEADLOCK_KEYWORDS if kw in desc_lower]
    prose_hits = [kw for kw in _PROSE_ENGINE_KEYWORDS if kw in desc_lower]
    deep_hits = [kw for kw in _DEEP_REASONING_KEYWORDS if kw in desc_lower]
    fast_hits = [kw for kw in _FAST_EXPLORE_KEYWORDS if kw in desc_lower]

    is_core_security_or_deadlock = bool(security_hits)

    if is_core_security_or_deadlock:
        base_model = "gemini-4-carbon-high"
        base_role = "deep_reasoning_core"
        matched_kws = deep_hits or security_hits
    elif prose_hits:
        base_model = "gemini-4-argon-high"
        base_role = "prose_engine"
        matched_kws = prose_hits
    elif deep_hits:
        base_model = "gemini-4-carbon-high"
        base_role = "deep_reasoning_core"
        matched_kws = deep_hits
    elif fast_hits:
        base_model = "gemini-4-hydrogen-medium"
        base_role = "fast_explore_core"
        matched_kws = fast_hits
    else:
        base_model = "gemini-4-hydrogen-high"
        base_role = "standard_coding_core"
        matched_kws = []

    final_model = base_model
    final_role = base_role
    pool_guard_triggered = False
    rem_pct_val = round(float(gemini_5h_remaining_pct), 2) if gemini_5h_remaining_pct is not None else None

    if rem_pct_val is not None and rem_pct_val < 30.0 and not is_core_security_or_deadlock:
        pool_guard_triggered = True
        final_model = "gemini-4-hydrogen-medium"
        final_role = "fast_explore_core"
        reason = (
            f"触发 Gemini 5h 护池熔断（剩余 {rem_pct_val}% < 30.0%）：非核心安全/死锁任务由 "
            f"`{base_model}` ({base_role}) 自动降档至 `{final_model}` ({final_role})。"
        )
    elif rem_pct_val is not None and rem_pct_val < 30.0 and is_core_security_or_deadlock:
        reason = (
            f"Gemini 5h 剩余 {rem_pct_val}% < 30.0%，但命中核心安全/死锁攻坚特征 {security_hits}，"
            f"豁免降档并保留 `{final_model}` ({final_role})。"
        )
    else:
        reason = (
            f"正常水位路由至 `{final_model}` ({final_role})，"
            f"匹配特征词：{matched_kws or ['常规标准模块开发/编码']}。"
        )

    return {
        "selected_model": final_model,
        "submodel": final_model,
        "role": final_role,
        "original_model": base_model,
        "original_role": base_role,
        "matched_keywords": matched_kws,
        "gemini_5h_remaining_pct": rem_pct_val,
        "pool_guard_triggered": pool_guard_triggered,
        "is_core_security_or_deadlock": is_core_security_or_deadlock,
        "reason": reason,
    }


def select_memory_reviewer_model(report: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Select between 6.1terra and sonnet5.5 based on higher weekly remaining quota; block 6.1sol and opus5.5."""
    rep = report or build_full_quota_report()
    pools = rep.get("pools") or {}
    codex_pool = pools.get("openai_codex") or {}
    claude_pool = pools.get("antigravity_claude_3p") or {}

    terra_wk = codex_pool.get("secondary_weekly_remaining_percent")
    if terra_wk is None:
        terra_wk = codex_pool.get("effective_remaining_percent")
    if terra_wk is None:
        terra_wk = 75.0
    sonnet_wk = claude_pool.get("secondary_weekly_remaining_percent")
    if sonnet_wk is None:
        sonnet_wk = claude_pool.get("effective_remaining_percent")
    if sonnet_wk is None:
        sonnet_wk = 50.0

    t_pct = round(float(terra_wk), 2)
    s_pct = round(float(sonnet_wk), 2)
    if t_pct >= s_pct:
        selected_model = "gpt-6.1-terra"
        selected_alias = "6.1terra"
        cli_agent = "Codex-CLI"
        cli_flag = '-c model="gpt-6.1-terra"'
        reason = f"选用 6.1terra (`gpt-6.1-terra`)：周剩余额度 {t_pct}% >= sonnet5.5 周剩余额度 {s_pct}%（严禁使用 6.1sol 与 opus5.5）"
    else:
        selected_model = "claude-sonnet-5-5"
        selected_alias = "sonnet5.5"
        cli_agent = "Opus-CLI"
        cli_flag = "--model claude-sonnet-5-5"
        reason = f"选用 sonnet5.5 (`claude-sonnet-5-5`)：周剩余额度 {s_pct}% > 6.1terra 周剩余额度 {t_pct}%（严禁使用 6.1sol 与 opus5.5）"

    return {
        "selected_model": selected_model,
        "selected_alias": selected_alias,
        "cli_agent": cli_agent,
        "cli_flag": cli_flag,
        "terra_weekly_remaining_percent": t_pct,
        "sonnet_weekly_remaining_percent": s_pct,
        "allowed_models": ["gpt-6.1-terra", "claude-sonnet-5-5"],
        "blocked_models": ["gpt-6.1-sol", "claude-opus-5-5", "claude-opus-5-5-high"],
        "selection_reason": reason,
    }


def recommend_agent_for_task(
    task_description: str,
    preferred_agent: Optional[str] = None,
    min_quota_percent: float = 15.0,
) -> Dict[str, Any]:
    report = build_full_quota_report(use_cache=False)
    agents = report["agents"]
    desc_lower = (task_description or "").lower()

    candidates: List[Dict[str, Any]] = []
    for name, info in agents.items():
        meta = AGENTS_META[name]
        affinity_hits = [kw for kw in meta["keywords"] if kw.lower() in desc_lower]
        # Specialist keywords for Opus-CLI and Codex-CLI carry higher weight when explicitly matched,
        # while AGY-CLI gets the base_capacity_bonus (+22.0) reflecting its massive Gemini quota pool.
        per_hit_weight = 16.0 if name in ("Opus-CLI", "Codex-CLI") else 10.0
        affinity_score = min(48.0, len(affinity_hits) * per_hit_weight)
        if preferred_agent and preferred_agent.lower() in (name.lower(), meta["key"].lower()):
            affinity_score += 30.0

        capacity_bonus = float(meta.get("base_capacity_bonus", 0.0))

        raw_eff_rem = info.get("effective_remaining_percent")
        is_unavailable = info.get("available") is False or info.get("health") == "unavailable"
        if raw_eff_rem is None:
            eff_rem = 0.0 if is_unavailable else 50.0
        else:
            eff_rem = float(raw_eff_rem)
        quota_score = (eff_rem / 100.0) * 35.0

        quota_penalty = 0.0
        if is_unavailable:
            quota_penalty = -200.0
        elif eff_rem <= 1.0:
            quota_penalty = -1000.0
        elif eff_rem < min_quota_percent:
            quota_penalty = -60.0
        elif name in ("Opus-CLI", "Codex-CLI") and eff_rem < 40.0:
            # Extra conservation penalty for weekly-capped elite pools when below 40%
            quota_penalty = -25.0

        in_prog = info["active_load"]["in_progress"]
        todo_cnt = info["active_load"]["todo"]
        load_penalty = -(in_prog * 12.0 + todo_cnt * 3.0)

        total_score = round(affinity_score + capacity_bonus + quota_score + quota_penalty + load_penalty, 2)
        candidates.append(
            {
                "agent": name,
                "agent_id": info["agent_id"],
                "model": info["model"],
                "quota_pool": info["quota_pool"],
                "capacity_tier": info["capacity_tier"],
                "effective_remaining_percent": eff_rem,
                "primary_5h_remaining_percent": info["primary_5h_remaining_percent"],
                "secondary_weekly_remaining_percent": info["secondary_weekly_remaining_percent"],
                "reset_time_local": info["reset_time_local"],
                "reset_in_minutes": info["reset_in_minutes"],
                "health": info["health"],
                "active_in_progress": in_prog,
                "affinity_matched_keywords": affinity_hits,
                "score_breakdown": {
                    "affinity_score": affinity_score,
                    "capacity_bonus": capacity_bonus,
                    "quota_score": round(quota_score, 2),
                    "quota_penalty": quota_penalty,
                    "load_penalty": load_penalty,
                    "total_score": total_score,
                },
                "total_score": total_score,
            }
        )

    candidates.sort(key=lambda x: x["total_score"], reverse=True)
    best = candidates[0] if candidates else None
    fallback = candidates[1] if len(candidates) > 1 else None
    mem_reviewer = select_memory_reviewer_model(report)
    gem_pool = (report.get("pools") or {}).get("antigravity_gemini") or {}
    gemini_5h_pct = gem_pool.get("primary_5h_remaining_percent")
    if gemini_5h_pct is None:
        gemini_5h_pct = gem_pool.get("effective_remaining_percent")
    gemini_submodel = recommend_gemini_submodel(task_description, gemini_5h_pct)

    reason = ""
    if best:
        reason = (
            f"推荐派发给 **{best['agent']}** (`{best['model']}` | {best['capacity_tier']})："
            f"有效剩余额度 **{best['effective_remaining_percent']}%** ({best['health']})，"
            f"容量体量加分 +{best['score_breakdown']['capacity_bonus']}，"
            f"匹配特征词 {best['affinity_matched_keywords'] or ['默认主力全栈工程']}，"
            f"综合得分 {best['total_score']}。"
        )
        if best["agent"] == "AGY-CLI":
            reason += f" 【AGY-CLI 三核路由】`{gemini_submodel['selected_model']}` ({gemini_submodel['role']})。"
        if fallback:
            reason += (
                f" 备选协同/分流智能体：**{fallback['agent']}** "
                f"(剩余额度 {fallback['effective_remaining_percent']}%, 得分 {fallback['total_score']})。"
            )
        if any(k in desc_lower for k in ("记忆审查", "记忆复核", "伴飞", "memory review", "reconcile")):
            reason += f" 【记忆审查路由】{mem_reviewer['selection_reason']}。"

    return {
        "task_description": task_description,
        "routing_policy": report["routing_policy"],
        "min_quota_percent": min_quota_percent,
        "recommended_agent": best["agent"] if best else None,
        "recommended_agent_id": best["agent_id"] if best else None,
        "fallback_agent": fallback["agent"] if fallback else None,
        "fallback_agent_id": fallback["agent_id"] if fallback else None,
        "gemini_submodel_routing": gemini_submodel,
        "memory_reviewer_routing": mem_reviewer,
        "recommendation_reason": reason,
        "candidates": candidates,
        "summary_markdown": report["summary_markdown"],
    }


_ERROR_LINE = re.compile(r"\b(?:FAILED|ERROR\w*|FAILURES|WARN\w*|Traceback|\w*Exception|\w*Error)\b|^\s*E\s+", re.I)
_PASS_LINE = re.compile(r"\bPASSED\b|(?:^ok\b|\bok)(?:\s+\[.*\])?\s*$", re.I)
_COMMAND_TYPES = ("auto", "pytest", "git_status", "git_diff", "build_log", "generic")


def _normalize_log_line(line: str) -> str:
    line = re.sub(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:\d{2})?", "<TIME>", line)
    line = re.sub(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b", "<TIME>", line)
    line = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|\b0x[0-9a-f]+\b", "<HEX>", line, flags=re.I)
    return re.sub(r"(?i)(\bline\s+|:\s*)\d+\b", r"\1<NUM>", line)


def _fold_repeated_lines(lines: List[str]) -> List[str]:
    groups: Dict[str, List[Any]] = {}
    for line in lines:
        key = _normalize_log_line(line)
        if key not in groups:
            groups[key] = [line, 0]
        groups[key][1] += 1
    return [line + (f" [repeated {count} times]" if count > 1 else "")
            for line, count in groups.values()]


def _purify_git(lines: List[str], kind: str) -> List[str]:
    if kind == "git_status":
        # Status has no line counts; report zero observed diff lines, not invented counts.
        unique = _fold_repeated_lines(lines)
        result = []
        for line in unique:
            if re.match(r"^[ MADRCU?!]{2} .+", line) or re.match(r"\s*(?:modified:|new file:|deleted:|renamed:)", line):
                line += " | +0 / -0 (diff counts unavailable in status)"
            result.append(line)
        return result
    result, block = [], []

    def flush() -> None:
        if not block:
            return
        added, removed, in_hunk = 0, 0, False
        for line in block:
            if line.startswith("@@"):
                in_hunk = True
            elif in_hunk:
                added += line.startswith("+")
                removed += line.startswith("-")
        result.append(f"{block[0]} | +{added} / -{removed}")
        context = 0
        for line in block[1:]:
            if line.startswith(" "):
                context += 1
                if context <= 2:
                    result.append(line)
                elif context == 3:
                    result.append("[unchanged context folded]")
            else:
                context = 0
                result.append(line)

    for line in lines:
        if line.startswith("diff --git "):
            flush()
            block = [line]
        elif block:
            block.append(line)
        else:
            result.append(line)
    flush()
    return result


def _fit_output_budget(lines: List[str], max_lines: int, token_budget: int) -> Tuple[str, bool]:
    text = "\n".join(lines)
    char_limit = token_budget * 4
    triggered = len(lines) > max_lines or len(text) > char_limit
    if not triggered:
        return text, False
    # Spend the budget on errors first, then the opening and closing summaries.
    errors = [i for i, line in enumerate(lines) if _ERROR_LINE.search(line)]
    edges = list(range(min(3, len(lines)))) + list(range(max(0, len(lines) - 3), len(lines)))
    selected: Dict[int, str] = {}
    remaining = char_limit
    for i in dict.fromkeys(errors + edges + list(range(len(lines)))):
        if len(selected) >= max_lines:
            break
        available = remaining - bool(selected)
        if available <= 0:
            break
        line = lines[i][:available]
        selected[i] = line
        remaining -= len(line) + (len(selected) > 1)
    return "\n".join(selected[i] for i in sorted(selected)), True


def purify_cli_output(raw_output: str, command_hint: str = "auto", max_lines: int = 80,
                      token_budget: int = 1200) -> Dict[str, Any]:
    """Compress captured terminal text locally; budgets apply to the returned text."""
    if command_hint not in _COMMAND_TYPES:
        raise ValueError(f"Unsupported command_hint: {command_hint}")
    if max_lines < 1 or token_budget < 1:
        raise ValueError("max_lines and token_budget must be positive")
    clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", raw_output).replace("\r\n", "\n")
    # Each carriage return replaces the current terminal row.
    lines = [row.rsplit("\r", 1)[-1].rstrip() for row in clean.split("\n")]
    lines = [line for line in lines if line.strip()]
    clean = "\n".join(lines)
    kind = command_hint
    if kind == "auto":
        if re.search(r"diff --git |^@@ .*@@|^--- a/", clean, re.M):
            kind = "git_diff"
        elif re.search(r"test session starts|\bpytest\b|\b\d+ (?:passed|failed)\b|::\S+.*(?:PASSED|FAILED)", clean, re.I):
            kind = "pytest"
        elif re.search(r"^On branch |^Changes (?:not staged|to be committed)|^[ MADRCU?!]{2} \S", clean, re.M):
            kind = "git_status"
        elif re.search(r"\b(?:building|compiling|webpack|ninja|cmake|build (?:succeeded|failed))\b", clean, re.I):
            kind = "build_log"
        else:
            kind = "generic"
    if kind == "pytest":
        failed = any(_ERROR_LINE.search(line) for line in lines)
        filtered = []
        for line in lines:
            if (_PASS_LINE.search(line) and not _ERROR_LINE.search(line)
                    and not re.search(r"\b\d+ passed\b", line, re.I)):
                continue
            if not failed and re.match(r"^[.s]+(?:\s+\[.*\])?$", line.strip()):
                continue
            filtered.append(line)
        lines = filtered
    if kind in ("git_diff", "git_status"):
        lines = _purify_git(lines, kind)
    else:
        lines = _fold_repeated_lines(lines)
    purified, triggered = _fit_output_budget(lines, max_lines, token_budget)
    original_tokens = max(1, len(raw_output) // 4)
    purified_tokens = max(1, len(purified) // 4)
    return {
        "ok": True, "detected_command_type": kind, "purified_output": purified,
        "original_chars": len(raw_output), "purified_chars": len(purified),
        "estimated_original_tokens": original_tokens, "estimated_purified_tokens": purified_tokens,
        "saved_tokens": max(0, original_tokens - purified_tokens),
        "compression_ratio_pct": round(max(0.0, 100.0 * (1 - len(purified) / max(1, len(raw_output)))), 2),
        "budget_circuit_breaker_triggered": triggered,
        "retained_error_lines": sum(bool(_ERROR_LINE.search(line)) for line in purified.splitlines()),
    }


def _pattern_signature(text: str) -> str:
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|\b(?:0x[0-9a-f]+|[0-9a-f]{8,64})\b", "<HEX>", text, flags=re.I)
    # Quotes keep paths containing spaces together; boundaries preserve command/options.
    text = re.sub(r'''["'](?:[A-Za-z]:[\\/]|\.{0,2}/|\\\\)[^"'\n]+["']''', "<PATH>", text)
    text = re.sub(r"(?<![\w:])(?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|/|\\\\)[^\s,;，。]+", "<PATH>", text)
    text = re.sub(r"(?<![\w-])(?:[\w.-]+[\\/])+[\w.*-]+|\b[\w-]+\.(?:py|js|ts|tsx|jsonl?|md|txt|log|yaml|yml|toml|csv)\b", "<PATH>", text, flags=re.I)
    text = re.sub(r"(?i)(--(?:temp|tmp|session|request|run)[\w-]*[= ]+)[^\s]+", r"\1<HEX>", text)
    text = re.sub(r"(?<![A-Za-z0-9_])\d+(?:\.\d+)?(?![A-Za-z0-9_])", "<NUM>", text)
    return " ".join(text.split()).strip()


def _read_trace_events(paths: List[str]) -> List[Dict[str, Any]]:
    """Read bounded regular JSON/JSONL files only; never execute or modify traces."""
    files: Dict[str, Path] = {}
    for source in paths:
        path = Path(source)
        try:
            if path.is_symlink():
                continue
            candidates = sorted(path.iterdir()) if path.is_dir() else [path]
        except OSError:
            continue
        for candidate in candidates:
            try:
                if (candidate.suffix.lower() in (".json", ".jsonl")
                        and not candidate.is_symlink() and candidate.is_file()):
                    files[str(candidate.resolve())] = candidate
            except OSError:
                continue
            if len(files) >= 100:
                break
        if len(files) >= 100:
            break
    events = []
    for path in files.values():
        try:
            with path.open("r", encoding="utf-8-sig") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 2_000_000:
                    continue
                if path.suffix.lower() == ".jsonl":
                    records = []
                    for line in stream:
                        try:
                            records.append(json.loads(line))
                        except ValueError:
                            continue
                else:
                    data = json.load(stream)
                    records = data if isinstance(data, list) else data.get("events", [data]) if isinstance(data, dict) else []
                if isinstance(records, list):
                    events.extend(item for item in records if isinstance(item, dict))
        except (OSError, ValueError, UnicodeError):
            continue
    return events


def cognitive_mirror_sniff(events: Optional[List[Dict[str, Any]]] = None,
                          trace_paths: Optional[List[str]] = None,
                          min_repeat_threshold: int = 3) -> Dict[str, Any]:
    if min_repeat_threshold < 1:
        raise ValueError("min_repeat_threshold must be positive")
    records = list(events or [])
    paths = list(trace_paths or [])
    if not records and not paths and os.environ.get("QUOTA_COGNITIVE_TRACE_STORE"):
        paths.append(os.environ["QUOTA_COGNITIVE_TRACE_STORE"])
    records.extend(_read_trace_events(paths))
    patterns: Dict[Tuple[str, str], Dict[str, Any]] = {}
    analyzed = 0
    for event in records:
        if not isinstance(event, dict):
            continue
        category = event.get("category")
        if event.get("correction"):
            text, inferred = event["correction"], "user_correction"
        elif event.get("command"):
            text, inferred = event["command"], "cli_command"
        elif event.get("prompt"):
            text, inferred = event["prompt"], "user_correction"
        else:
            text, inferred = event.get("action", ""), "manual_workflow"
        if not isinstance(text, str) or not text.strip():
            continue
        if category not in ("cli_command", "user_correction", "manual_workflow"):
            category = inferred
        signature = _pattern_signature(text)
        key = (category, signature)
        if key not in patterns:
            patterns[key] = {"pattern_signature": signature, "category": category,
                             "count": 0, "total_duration_sec": 0.0, "examples": []}
        pattern = patterns[key]
        pattern["count"] += 1
        try:
            duration = float(event.get("duration_sec", 0) or 0)
            if math.isfinite(duration) and duration >= 0:
                pattern["total_duration_sec"] += duration
        except (ValueError, TypeError):
            pass
        if len(pattern["examples"]) < 3:
            pattern["examples"].append(text)
        analyzed += 1
    repeated = sorted((p for p in patterns.values() if p["count"] >= min_repeat_threshold),
                      key=lambda p: (-p["count"], p["category"], p["pattern_signature"]))
    targets = {"cli_command": "Skill 一键脚本", "user_correction": "AGENTS.md 默认规则", "manual_workflow": "MCP 工具封装"}
    proposals = [{**{k: v for k, v in p.items() if k != "examples"},
                  "recommended_target": targets[p["category"]],
                  "proactive_prompt_zh": f"发现您近期 {p['count']} 次手动执行了【{p['pattern_signature']}】，是否为您固化为默认规则或一键脚本？"}
                 for p in repeated]
    summary = [f"已分析 {analyzed} 条事件；重复阈值 {min_repeat_threshold}；发现 {len(repeated)} 个高频模式。"]
    summary.extend(f"- {p['pattern_signature']}：{p['count']} 次，累计 {p['total_duration_sec']:g} 秒；建议 {p['recommended_target']}。" for p in proposals)
    return {"ok": True, "total_events_analyzed": analyzed, "min_repeat_threshold": min_repeat_threshold,
            "rule_of_three_triggered": bool(repeated), "repeated_patterns": repeated,
            "automation_proposals": proposals, "summary_markdown": "\n".join(summary)}


_GUARD_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv"}


def _guard_store_path() -> Path:
    explicit = os.environ.get("QUOTA_TASK_GUARD_STORE", "").strip()
    if explicit:
        p = Path(explicit)
        return p if p.suffix.lower() == ".json" else (p / "task_budget_guard.json")
    trace_env = os.environ.get("QUOTA_COGNITIVE_TRACE_STORE", "").strip()
    if trace_env:
        tp = Path(trace_env)
        base = tp.parent if tp.suffix else tp
        return base / "task_budget_guard.json"
    return Path.home() / ".antigravity-agent-policy" / "task_budget_guard.json"


def _load_guard_store() -> Dict[str, Any]:
    path = _guard_store_path()
    if not path.is_file():
        return {"tasks": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("tasks"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"tasks": {}}


def _save_guard_store(store: Dict[str, Any]) -> None:
    path = _guard_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _is_pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return int(exit_code.value) == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _latest_watch_mtime(watch_paths: Optional[List[str]], max_entries: int = 2000) -> Optional[float]:
    if not watch_paths:
        return None
    latest: Optional[float] = None
    scanned = 0
    for raw in watch_paths:
        if not isinstance(raw, str) or not raw.strip():
            continue
        target = Path(raw)
        if not target.exists():
            continue
        try:
            if target.is_file():
                mt = target.stat().st_mtime
                latest = mt if latest is None or mt > latest else latest
                scanned += 1
            elif target.is_dir():
                for dirpath, dirnames, filenames in os.walk(target):
                    dirnames[:] = [d for d in dirnames if d not in _GUARD_SKIP_DIRS]
                    for fname in filenames:
                        scanned += 1
                        if scanned > max_entries:
                            return latest
                        fp = Path(dirpath) / fname
                        try:
                            mt = fp.stat().st_mtime
                            latest = mt if latest is None or mt > latest else latest
                        except OSError:
                            continue
        except OSError:
            continue
    return latest


def _pool_used_and_reset(report: Dict[str, Any], pool_key: str) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    pool = report.get("pools", {}).get(pool_key, {})
    if pool.get("available") is False or pool.get("status") == "unavailable":
        return None, None, None
    rem = pool.get("primary_5h_remaining_percent")
    used = pool.get("primary_5h_used_percent")
    if used is None and isinstance(rem, (int, float)):
        used = round(max(0.0, 100.0 - float(rem)), 4)
    used_val = float(used) if isinstance(used, (int, float)) else None
    rem_val = float(rem) if isinstance(rem, (int, float)) else None
    reset_str = pool.get("primary_5h_reset_local")
    return used_val, rem_val, (str(reset_str) if reset_str else None)


def _update_pool_delta(entry: Dict[str, Any], prefix: str, current_used: Optional[float], current_reset: Optional[str]) -> float:
    if current_used is None:
        # Telemetry temporarily unavailable: preserve existing accounting without zero-reset pollution
        return float(entry.get(f"{prefix}_delta_pct", 0.0))

    anchor_reset = entry.get(f"{prefix}_anchor_reset")
    raw_anchor = entry.get(f"{prefix}_anchor_used")
    if raw_anchor is None:
        entry[f"{prefix}_anchor_used"] = current_used
        entry[f"{prefix}_last_used"] = current_used
        entry[f"{prefix}_peak_used"] = current_used
        if current_reset:
            entry[f"{prefix}_anchor_reset"] = current_reset
        return float(entry.get(f"{prefix}_delta_pct", 0.0))
    anchor_used = float(raw_anchor)
    banked = float(entry.get(f"{prefix}_banked_delta", 0.0))
    raw_last = entry.get(f"{prefix}_last_used")
    last_seen = float(raw_last) if raw_last is not None else anchor_used
    raw_peak = entry.get(f"{prefix}_peak_used")
    peak_used = float(raw_peak) if raw_peak is not None else max(anchor_used, last_seen)

    if anchor_reset and current_reset:
        if current_reset != anchor_reset:
            # Authoritative window rollover via reset timestamp change
            banked += max(0.0, peak_used - anchor_used)
            entry[f"{prefix}_banked_delta"] = round(banked, 4)
            anchor_used = 0.0
            peak_used = current_used
            entry[f"{prefix}_anchor_used"] = 0.0
            entry[f"{prefix}_anchor_reset"] = current_reset
        else:
            # Explicitly same 5h window: ignore transient downward jitter
            peak_used = max(peak_used, current_used)
    elif current_reset and not anchor_reset:
        if current_used + 1e-6 < last_seen:
            # Window rolled over while reset timestamp first became available
            banked += max(0.0, peak_used - anchor_used)
            entry[f"{prefix}_banked_delta"] = round(banked, 4)
            anchor_used = 0.0
            peak_used = current_used
            entry[f"{prefix}_anchor_used"] = 0.0
        else:
            # Same window, late-arriving reset timestamp: bind without double-banking
            peak_used = max(peak_used, current_used)
        entry[f"{prefix}_anchor_reset"] = current_reset
    else:
        # Reset timestamp absent: any real drop indicates 5h window rollover (captures sub-1% rollovers too)
        if current_used + 1e-6 < last_seen:
            banked += max(0.0, peak_used - anchor_used)
            entry[f"{prefix}_banked_delta"] = round(banked, 4)
            anchor_used = 0.0
            peak_used = current_used
            entry[f"{prefix}_anchor_used"] = 0.0
        else:
            peak_used = max(peak_used, current_used)

    entry[f"{prefix}_peak_used"] = peak_used
    entry[f"{prefix}_last_used"] = current_used
    total_delta = banked + max(0.0, peak_used - anchor_used)
    entry[f"{prefix}_delta_pct"] = round(total_delta, 3)
    return entry[f"{prefix}_delta_pct"]


def task_budget_guard(
    action: str,
    task_id: str,
    *,
    max_step_gemini_delta_pct: Optional[float] = None,
    max_step_codex_delta_pct: Optional[float] = None,
    max_gemini_5h_delta_pct: float = 15.0,
    max_codex_5h_delta_pct: float = 10.0,
    max_total_gemini_delta_pct: Optional[float] = None,
    max_total_codex_delta_pct: Optional[float] = None,
    min_gemini_reserve_pct: float = 10.0,
    min_codex_reserve_pct: float = 10.0,
    max_reject_rounds: int = 2,
    stall_timeout_sec: int = 180,
    step_id: Optional[str] = None,
    pid: Optional[int] = None,
    watch_paths: Optional[List[str]] = None,
    reject_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Progress-aware quota guard: unlimited total burn on healthy progress, per-step stall/loop breaker, and 10% pool reserve floor."""
    act = (action or "").strip().lower()
    if act not in {"start", "check", "advance_step", "record_reject", "status", "reset"}:
        raise ValueError(f"Invalid action '{action}'; expected start|check|advance_step|record_reject|status|reset")
    tid = (task_id or "").strip()
    if not tid:
        raise ValueError("task_id is required")

    store = _load_guard_store()
    tasks = store.setdefault("tasks", {})
    now = time.time()

    if act == "reset":
        existed = tasks.pop(tid, None) is not None
        _save_guard_store(store)
        return {"ok": True, "action": "reset", "task_id": tid, "removed": existed}

    if act == "status":
        entry = tasks.get(tid)
        if not entry:
            return {"ok": False, "error": f"Task '{tid}' not registered in budget guard", "task_id": tid}
        return {"ok": True, "action": "status", "task_id": tid, "guard": entry}

    report = build_full_quota_report(use_cache=False)
    g_used, g_rem, g_reset = _pool_used_and_reset(report, "antigravity_gemini")
    c_used, c_rem, c_reset = _pool_used_and_reset(report, "openai_codex")
    g_reset_min = report.get("pools", {}).get("antigravity_gemini", {}).get("primary_5h_reset_in_minutes")
    c_reset_min = report.get("pools", {}).get("openai_codex", {}).get("primary_5h_reset_in_minutes")

    if act == "start":
        step_g_cap = float(max_step_gemini_delta_pct if max_step_gemini_delta_pct is not None else max_gemini_5h_delta_pct)
        step_c_cap = float(max_step_codex_delta_pct if max_step_codex_delta_pct is not None else max_codex_5h_delta_pct)
        if step_g_cap <= 0 or step_c_cap <= 0:
            raise ValueError("Per-step delta caps must be positive")
        if max_reject_rounds < 1 or stall_timeout_sec < 10:
            raise ValueError("max_reject_rounds must be >= 1 and stall_timeout_sec >= 10")
        if not (0.0 <= float(min_gemini_reserve_pct) < 100.0) or not (0.0 <= float(min_codex_reserve_pct) < 100.0):
            raise ValueError("Reserve floor percentages must be in [0, 100)")
        mtime = _latest_watch_mtime(watch_paths)
        entry = {
            "task_id": tid,
            "mode": "progress_aware_unlimited_total" if max_total_gemini_delta_pct is None else "bounded_total",
            "started_at": now,
            "updated_at": now,
            "last_activity_at": mtime if mtime is not None else now,
            "last_observed_mtime": mtime,
            "completed_steps": 0,
            "last_completed_step": None,
            "max_step_gemini_delta_pct": step_g_cap,
            "max_step_codex_delta_pct": step_c_cap,
            "max_total_gemini_delta_pct": float(max_total_gemini_delta_pct) if max_total_gemini_delta_pct is not None else None,
            "max_total_codex_delta_pct": float(max_total_codex_delta_pct) if max_total_codex_delta_pct is not None else None,
            "min_gemini_reserve_pct": float(min_gemini_reserve_pct),
            "min_codex_reserve_pct": float(min_codex_reserve_pct),
            "max_reject_rounds": int(max_reject_rounds),
            "stall_timeout_sec": int(stall_timeout_sec),
            "gemini_anchor_used": g_used,
            "gemini_last_used": g_used,
            "gemini_peak_used": g_used,
            "gemini_anchor_reset": g_reset,
            "gemini_banked_delta": 0.0,
            "gemini_delta_pct": 0.0,
            "step_gemini_baseline": 0.0,
            "step_gemini_delta_pct": 0.0,
            "codex_anchor_used": c_used,
            "codex_last_used": c_used,
            "codex_peak_used": c_used,
            "codex_anchor_reset": c_reset,
            "codex_banked_delta": 0.0,
            "codex_delta_pct": 0.0,
            "step_codex_baseline": 0.0,
            "step_codex_delta_pct": 0.0,
            "reject_rounds": 0,
            "reject_history": [],
        }
        tasks[tid] = entry
        _save_guard_store(store)
        return {
            "ok": True,
            "action": "start",
            "task_id": tid,
            "tripped": False,
            "paused_for_refill": False,
            "decision": "proceed",
            "guard": entry,
        }

    entry = tasks.get(tid)
    if not entry:
        raise ValueError(f"Task '{tid}' not initialized; call quota_task_guard(action='start') first")

    g_total_delta = _update_pool_delta(entry, "gemini", g_used, g_reset)
    c_total_delta = _update_pool_delta(entry, "codex", c_used, c_reset)
    entry["updated_at"] = now

    if act == "advance_step":
        entry["completed_steps"] = int(entry.get("completed_steps", 0)) + 1
        if step_id:
            entry["last_completed_step"] = str(step_id)[:120]
        entry["step_gemini_baseline"] = g_total_delta
        entry["step_codex_baseline"] = c_total_delta
        entry["step_gemini_delta_pct"] = 0.0
        entry["step_codex_delta_pct"] = 0.0
        entry["reject_rounds"] = 0
        entry["last_activity_at"] = now
        _save_guard_store(store)
        return {
            "ok": True,
            "action": "advance_step",
            "task_id": tid,
            "completed_steps": entry["completed_steps"],
            "last_completed_step": entry.get("last_completed_step"),
            "tripped": False,
            "paused_for_refill": False,
            "decision": "proceed",
            "step_gemini_delta_pct": 0.0,
            "step_codex_delta_pct": 0.0,
            "total_gemini_delta_pct": g_total_delta,
            "total_codex_delta_pct": c_total_delta,
            "gemini_delta_pct": g_total_delta,
            "codex_delta_pct": c_total_delta,
            "gemini_remaining_pct": g_rem,
            "codex_remaining_pct": c_rem,
            "reject_rounds": 0,
        }

    if act == "record_reject":
        entry["reject_rounds"] = int(entry.get("reject_rounds", 0)) + 1
        if reject_reason:
            hist = entry.setdefault("reject_history", [])
            hist.append({"round": entry["reject_rounds"], "reason": str(reject_reason)[:300], "at": now})
            entry["reject_history"] = hist[-10:]

    step_g_delta = round(max(0.0, g_total_delta - float(entry.get("step_gemini_baseline", 0.0))), 3)
    step_c_delta = round(max(0.0, c_total_delta - float(entry.get("step_codex_baseline", 0.0))), 3)
    entry["step_gemini_delta_pct"] = step_g_delta
    entry["step_codex_delta_pct"] = step_c_delta

    step_g_cap = float(entry.get("max_step_gemini_delta_pct", entry.get("max_gemini_5h_delta_pct", 15.0)))
    step_c_cap = float(entry.get("max_step_codex_delta_pct", entry.get("max_codex_5h_delta_pct", 10.0)))
    total_g_cap = entry.get("max_total_gemini_delta_pct")
    total_c_cap = entry.get("max_total_codex_delta_pct")

    reasons: List[str] = []
    stall_kind: Optional[str] = None
    recommended_action: Optional[str] = None
    escalate_to: Optional[str] = None

    # 1. Per-step zero-progress burn cap (never kills healthy multi-step long tasks!)
    if step_g_delta > step_g_cap:
        reasons.append(
            f"Single-step Gemini burn {step_g_delta:.1f}% without step completion exceeded cap {step_g_cap:.1f}%"
        )
    if step_c_delta > step_c_cap:
        reasons.append(
            f"Single-step Codex burn {step_c_delta:.1f}% without step completion exceeded cap {step_c_cap:.1f}%"
        )
    if total_g_cap is not None and g_total_delta > float(total_g_cap):
        reasons.append(f"Total Gemini burn {g_total_delta:.1f}% exceeded explicit total cap {float(total_g_cap):.1f}%")
    if total_c_cap is not None and c_total_delta > float(total_c_cap):
        reasons.append(f"Total Codex burn {c_total_delta:.1f}% exceeded explicit total cap {float(total_c_cap):.1f}%")

    # 2. Per-step verification rejection loop cap
    if int(entry.get("reject_rounds", 0)) >= int(entry["max_reject_rounds"]) and act == "record_reject":
        reasons.append(
            f"Current step rejected {entry['reject_rounds']} times consecutively (cap={entry['max_reject_rounds']})"
        )
        escalate_to = "gemini-4-carbon-high"
        recommended_action = "Stop blind Hydrogen retries; escalate root-cause diagnosis to gemini-4-carbon-high or human review"
    elif int(entry.get("reject_rounds", 0)) > int(entry["max_reject_rounds"]):
        reasons.append(
            f"Current step rejected {entry['reject_rounds']} times (exceeded cap={entry['max_reject_rounds']})"
        )
        escalate_to = "gemini-4-carbon-high"

    # 3. Zero-token PID & mtime stall check
    pid_alive: Optional[bool] = None
    if pid is not None:
        pid_alive = _is_pid_alive(int(pid))
    mtime = _latest_watch_mtime(watch_paths)
    prev_mtime = entry.get("last_observed_mtime")
    if mtime is not None:
        if prev_mtime is None or mtime > float(prev_mtime):
            entry["last_observed_mtime"] = mtime
            entry["last_activity_at"] = max(float(entry.get("last_activity_at", now)), mtime)

    idle_sec = max(0.0, now - float(entry.get("last_activity_at", now)))
    if pid is not None and pid_alive is False:
        stall_kind = "process_exited_unexpectedly"
        reasons.append(f"Monitored CLI worker PID {pid} is no longer alive")
        recommended_action = "Inspect worker log tail and exit code before restarting"
    elif (watch_paths or pid is not None) and idle_sec >= float(entry["stall_timeout_sec"]):
        if (g_rem is not None and g_rem <= 2.0) or (c_rem is not None and c_rem <= 2.0):
            stall_kind = "quota_exhausted_stall"
            reasons.append(f"No output/file activity for {int(idle_sec)}s and model pool remaining <= 2%")
            recommended_action = "Park task until 5h window refill or hot-downgrade worker model"
        else:
            stall_kind = "process_hung_stall"
            reasons.append(f"No output/file activity for {int(idle_sec)}s (threshold={entry['stall_timeout_sec']}s)")
            recommended_action = "Terminate hung CLI PID and resume with --conversation and log tail context"

    # 4. Pool reserve floor check (graceful pause for refill — does NOT fail/abort long task!)
    paused_for_refill = False
    refill_wait_minutes: Optional[float] = None
    g_floor = float(entry.get("min_gemini_reserve_pct", 10.0))
    c_floor = float(entry.get("min_codex_reserve_pct", 10.0))
    if not reasons:
        if g_rem is not None and g_rem < g_floor:
            paused_for_refill = True
            refill_wait_minutes = float(g_reset_min) if isinstance(g_reset_min, (int, float)) else None
            recommended_action = (
                f"Gemini 5h pool at {g_rem:.1f}% (< {g_floor:.1f}% reserve floor); park and resume after window refill"
            )
        elif c_rem is not None and c_rem < c_floor:
            paused_for_refill = True
            refill_wait_minutes = float(c_reset_min) if isinstance(c_reset_min, (int, float)) else None
            recommended_action = (
                f"Codex 5h pool at {c_rem:.1f}% (< {c_floor:.1f}% reserve floor); defer Codex verification until refill"
            )

    _save_guard_store(store)
    tripped = bool(reasons)
    decision = "circuit_break" if tripped else ("park_for_refill" if paused_for_refill else "proceed")
    return {
        "ok": True,
        "action": act,
        "task_id": tid,
        "tripped": tripped,
        "paused_for_refill": paused_for_refill,
        "refill_wait_minutes": refill_wait_minutes,
        "decision": decision,
        "reasons": reasons,
        "stall_kind": stall_kind,
        "idle_seconds": round(idle_sec, 1),
        "pid_alive": pid_alive,
        "escalate_to": escalate_to,
        "recommended_action": recommended_action,
        "completed_steps": int(entry.get("completed_steps", 0)),
        "step_gemini_delta_pct": step_g_delta,
        "step_codex_delta_pct": step_c_delta,
        "total_gemini_delta_pct": g_total_delta,
        "total_codex_delta_pct": c_total_delta,
        "gemini_delta_pct": g_total_delta,
        "codex_delta_pct": c_total_delta,
        "gemini_remaining_pct": g_rem,
        "codex_remaining_pct": c_rem,
        "reject_rounds": entry["reject_rounds"],
    }


TOOLS_SCHEMA = [
    {
        "name": "quota_status",
        "description": "实时查询 三大 CLI 智能体（AGY-CLI、Opus-CLI、Codex-CLI）及三大底层模型池的剩余额度百分比、容量体量分级（Gemini 海量无周限额主力池 vs Opus/Sol 周限额精锐池）、重置倒计时与在途任务负载。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_raw_models": {
                    "type": "boolean",
                    "description": "是否返回所有细分模型的原始配额桶列表（默认 false）",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "quota_recommend_agent",
        "description": "额度感知型智能调度推荐器（遵循 Gemini-First, Elite-On-Demand 策略）：综合 Gemini 海量无周限额主力加权、任务专长特征词、5h/周窗口实时剩余额度与在途并发数，推荐最优执行智能体。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_description": {
                    "type": "string",
                    "description": "待派发的任务描述或需求摘要",
                },
                "preferred_agent": {
                    "type": "string",
                    "description": "可选的首选智能体名称（如 AGY-CLI、Opus-CLI、Codex-CLI）",
                },
                "min_quota_percent": {
                    "type": "number",
                    "description": "最低安全剩余额度百分比阈值（默认 15.0）",
                },
            },
            "required": ["task_description"],
            "additionalProperties": False,
        },
    },
    {
        "name": "quota_check_agent",
        "description": "快速检查单个指定智能体（AGY-CLI / Opus-CLI / Codex-CLI）的实时剩余额度、容量分级及是否可派发新任务。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_name": {
                    "type": "string",
                    "description": "智能体名称（AGY-CLI、Opus-CLI 或 Codex-CLI）",
                }
            },
            "required": ["agent_name"],
            "additionalProperties": False,
        },
    },
]


TOOLS_SCHEMA.extend([
    {
        "name": "quota_purify_cli_output",
        "description": "CLI 输出压缩净化：保留 pytest 失败、汇总 Git 变更、折叠重复日志，并执行任务级 Token 预算熔断。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "raw_output": {"type": "string", "description": "待净化的终端原始输出"},
                "command_hint": {"type": "string", "enum": list(_COMMAND_TYPES), "default": "auto"},
                "max_lines": {"type": "integer", "minimum": 1, "default": 80},
                "token_budget": {"type": "integer", "minimum": 1, "default": 1200},
            },
            "required": ["raw_output"],
            "additionalProperties": False,
        },
    },
    {
        "name": "quota_cognitive_mirror_sniff",
        "description": "认知镜像嗅探器：只读聚类命令、纠偏和手工工作流，按 Rule of Three 生成规则、Skill 或 MCP 固化建议。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "events": {"type": ["array", "null"], "items": {"type": "object", "properties": {
                    "command": {"type": "string"}, "prompt": {"type": "string"},
                    "correction": {"type": "string"}, "action": {"type": "string"},
                    "duration_sec": {"type": "number", "minimum": 0},
                    "category": {"type": "string", "enum": ["cli_command", "user_correction", "manual_workflow"]},
                }, "additionalProperties": True}, "default": None},
                "trace_paths": {"type": ["array", "null"], "items": {"type": "string"}, "default": None},
                "min_repeat_threshold": {"type": "integer", "minimum": 1, "default": 3},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "quota_task_guard",
        "description": "进展感知型长任务守卫（正常推进总消耗不封顶）：通过 advance_step 在节点完成时重置单步计数，仅拦截同一节点无进展空烧（默认单步 15%/10%）、连续打回死循环（2 次升级 Carbon）、180s 进程停滞，并在跌破 10% 池底余量时温和挂起等待回血（park_for_refill）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start", "check", "advance_step", "record_reject", "status", "reset"],
                    "description": "守卫动作：start 初始化、check 巡检空转与池底线、advance_step 节点完成重置单步锚点、record_reject 记录打回、status 查看、reset 清理",
                },
                "task_id": {"type": "string", "description": "绑定的任务唯一 ID"},
                "step_id": {"type": ["string", "null"], "default": None, "description": "advance_step 时记录已完成的 DAG 节点 ID"},
                "max_step_gemini_delta_pct": {"type": ["number", "null"], "minimum": 0.5, "default": None, "description": "单步无进展最大 Gemini 5h 消耗百分比（默认 15.0，节点完成后归零）"},
                "max_step_codex_delta_pct": {"type": ["number", "null"], "minimum": 0.5, "default": None, "description": "单步无进展最大 Codex 5h 消耗百分比（默认 10.0，节点完成后归零）"},
                "max_gemini_5h_delta_pct": {"type": "number", "minimum": 0.5, "default": 15.0},
                "max_codex_5h_delta_pct": {"type": "number", "minimum": 0.5, "default": 10.0},
                "max_total_gemini_delta_pct": {"type": ["number", "null"], "minimum": 1.0, "default": None, "description": "整单总消耗上限（默认 null 不封顶，保障健康长任务不断流）"},
                "max_total_codex_delta_pct": {"type": ["number", "null"], "minimum": 1.0, "default": None, "description": "整单 Codex 总消耗上限（默认 null 不封顶）"},
                "min_gemini_reserve_pct": {"type": "number", "minimum": 0.0, "maximum": 95.0, "default": 10.0, "description": "Gemini 5h 池底保命余量线（低于此值返回 park_for_refill 温和挂起等刷新）"},
                "min_codex_reserve_pct": {"type": "number", "minimum": 0.0, "maximum": 95.0, "default": 10.0, "description": "Codex 5h 池底保命余量线"},
                "max_reject_rounds": {"type": "integer", "minimum": 1, "default": 2},
                "stall_timeout_sec": {"type": "integer", "minimum": 10, "default": 180},
                "pid": {"type": ["integer", "null"], "default": None, "description": "可选：被监控的无头 CLI 子进程 PID"},
                "watch_paths": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "default": None,
                    "description": "可选：被监控的 CLI 日志文件或隔离工作区目录列表",
                },
                "reject_reason": {"type": ["string", "null"], "default": None},
            },
            "required": ["action", "task_id"],
            "additionalProperties": False,
        },
    },
])
for _tool in TOOLS_SCHEMA:
    _tool["parameters"] = _tool["inputSchema"]
TOOLS = TOOLS_SCHEMA


def handle_tool_call(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    if name == "quota_status":
        return build_full_quota_report(
            include_raw_models=bool(arguments.get("include_raw_models")),
            use_cache=bool(arguments.get("use_cache", True)),
            force_refresh=bool(arguments.get("force_refresh", False)),
        )
    elif name == "quota_recommend_agent":
        return recommend_agent_for_task(
            task_description=arguments.get("task_description", ""),
            preferred_agent=arguments.get("preferred_agent"),
            min_quota_percent=float(arguments.get("min_quota_percent", 15.0)),
        )
    elif name == "quota_check_agent":
        target = (arguments.get("agent_name") or "").strip()
        report = build_full_quota_report(use_cache=False)
        for k, v in report["agents"].items():
            if k.lower() == target.lower() or v["agent_key"].lower() == target.lower():
                return {"ok": True, "agent": k, **v}
        return {"ok": False, "error": f"Unknown agent: {target}", "available_agents": list(report["agents"].keys())}
    elif name == "quota_purify_cli_output":
        return purify_cli_output(**arguments)
    elif name == "quota_cognitive_mirror_sniff":
        return cognitive_mirror_sniff(**arguments)
    elif name == "quota_task_guard":
        return task_budget_guard(**arguments)
    else:
        raise ValueError(f"Unknown tool: {name}")


call_tool = handle_tool_call


def _handle_jsonrpc_line(raw_line: str) -> Optional[Dict[str, Any]]:
    line = raw_line.strip()
    if not line:
        return None
    try:
        req = json.loads(line)
    except Exception:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}

    if not isinstance(req, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    if "jsonrpc" in req and req["jsonrpc"] != "2.0":
        return {"jsonrpc": "2.0", "id": req.get("id"), "error": {"code": -32600, "message": "Invalid Request"}}
    method = req.get("method")
    if not isinstance(method, str) or not method.strip():
        return {"jsonrpc": "2.0", "id": req.get("id"), "error": {"code": -32600, "message": "Invalid Request"}}
    if "id" not in req:
        return None

    req_id = req["id"]
    params = req.get("params")
    if params is not None and not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "Invalid params"}}
    params = params or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "quota-monitor", "version": "1.4.0"},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS_SCHEMA}}
    if method == "tools/call":
        tname = params.get("name")
        targs = params.get("arguments") if "arguments" in params and params["arguments"] is not None else {}
        if not isinstance(tname, str) or not tname.strip() or not isinstance(targs, dict):
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "Invalid params"}}
        try:
            out = handle_tool_call(tname, targs)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=2)}],
                    "isError": False,
                },
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps({"error": str(e)}, ensure_ascii=False)}],
                    "isError": True,
                },
            }
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        cmd = sys.argv[2] if len(sys.argv) > 2 else "status"
        if cmd == "recommend":
            desc = " ".join(sys.argv[3:]) or "通用开发任务"
            print(json.dumps(recommend_agent_for_task(desc), ensure_ascii=False, indent=2))
        else:
            print(json.dumps(build_full_quota_report(), ensure_ascii=False, indent=2))
        return

    for line in sys.stdin:
        resp = _handle_jsonrpc_line(line)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
