"""Standard-library HTTP adapter for the Hindsight memory API."""

from __future__ import annotations

import json
from hashlib import sha256
from http.client import HTTPException
import math
import socket
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from memory_backend import (KINDS, MemoryBackend, MemoryBackendError,
                            _has_secret, _safe_metadata)


_HEALTH_LOCK = threading.Lock()
_HEALTH_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_REQUEST_SLOTS = threading.BoundedSemaphore(16)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request: Request, response: Any, code: int,
                         message: str, headers: Any, newurl: str) -> None:
        # The configured endpoint is the network boundary, including redirects.
        return None


class HindsightMemoryBackend(MemoryBackend):
    def __init__(self, api_url: str, api_key: str | None,
                 settings: dict[str, Any]):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.settings = settings
        self._opener = build_opener(_NoRedirect())
        self._health_key = (self.api_url, sha256((api_key or "").encode("utf-8")).hexdigest())

    def _remember_failure(self, reason: str) -> None:
        if reason in {"auth_error", "rate_limited", "timeout", "unavailable"}:
            with _HEALTH_LOCK:
                _HEALTH_CACHE[self._health_key] = (
                    time.monotonic() + 3,
                    {"status": "unavailable", "available": False, "reason": reason},
                )

    def _request(self, method: str, path: str, payload: dict[str, Any] | None,
                 timeout_ms: int, *, expect_json: bool = True) -> Any:
        """Enforce one wall-clock budget, including DNS and response streaming."""
        if not _REQUEST_SLOTS.acquire(blocking=False):
            raise MemoryBackendError("unavailable")
        finished = threading.Event()
        outcome: dict[str, Any] = {}

        def run() -> None:
            try:
                outcome["value"] = self._request_sync(method, path, payload, timeout_ms,
                                                      expect_json=expect_json)
            except Exception as exc:
                outcome["error"] = exc
            finally:
                _REQUEST_SLOTS.release()
                finished.set()

        worker = threading.Thread(target=run, daemon=True, name="hindsight-http")
        worker.start()
        if not finished.wait(max(0.001, timeout_ms / 1000)):
            self._remember_failure("timeout")
            raise MemoryBackendError("timeout")
        error = outcome.get("error")
        if error is not None:
            if isinstance(error, MemoryBackendError):
                self._remember_failure(error.reason)
                raise error
            reason = "bad_response" if isinstance(error, (ValueError, HTTPException)) else "unavailable"
            self._remember_failure(reason)
            raise MemoryBackendError(reason) from None
        return outcome.get("value")

    def _request_sync(self, method: str, path: str, payload: dict[str, Any] | None,
                      timeout_ms: int, *, expect_json: bool = True) -> Any:
        headers = {"Accept": "application/json", "Cache-Control": "no-store"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.api_url + path, data=body, headers=headers, method=method)
        timeout_seconds = max(0.001, timeout_ms / 1000)
        deadline = time.monotonic() + timeout_seconds
        expired = threading.Event()
        active: dict[str, Any] = {"response": None, "socket": None}

        def abort_at_deadline() -> None:
            expired.set()
            connection = active["socket"]
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    connection.close()
                except OSError:
                    pass
            response = active["response"]
            if response is not None:
                try:
                    response.close()
                except OSError:
                    pass

        watchdog = threading.Timer(timeout_seconds, abort_at_deadline)
        watchdog.daemon = True
        watchdog.start()
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                active["response"] = response
                active["socket"] = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                if expired.is_set() or time.monotonic() >= deadline:
                    raise MemoryBackendError("timeout")
                if not expect_json:
                    return None
                chunks = []
                size = 0
                while True:
                    remaining = deadline - time.monotonic()
                    if expired.is_set() or remaining <= 0:
                        raise MemoryBackendError("timeout")
                    connection = active["socket"]
                    if connection is not None:
                        try:
                            fileno = connection.fileno()
                        except OSError:
                            fileno = -1
                        if not getattr(connection, "_closed", False) and fileno >= 0:
                            connection.settimeout(max(0.001, remaining))
                    read_size = min(65536, 2_000_001 - size)
                    chunk = response.read1(read_size) if hasattr(response, "read1") else response.read(read_size)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 2_000_000:
                        raise MemoryBackendError("bad_response")
                    chunks.append(chunk)
                if expired.is_set() or time.monotonic() >= deadline:
                    raise MemoryBackendError("timeout")
                raw = b"".join(chunks)
        except MemoryBackendError as exc:
            raise
        except HTTPError as exc:
            try:
                exc.close()
            except OSError:
                pass
            if expired.is_set() or time.monotonic() >= deadline:
                reason = "timeout"
            elif exc.code in {401, 403}:
                reason = "auth_error"
            elif exc.code == 429:
                reason = "rate_limited"
            elif exc.code in {400, 404, 409, 422}:
                reason = "bank_error"
            elif exc.code in {408, 504}:
                reason = "timeout"
            else:
                reason = "unavailable"
            raise MemoryBackendError(reason) from None
        except (TimeoutError, socket.timeout):
            raise MemoryBackendError("timeout") from None
        except URLError as exc:
            reason = ("timeout" if expired.is_set() or time.monotonic() >= deadline
                      or isinstance(exc.reason, (TimeoutError, socket.timeout)) else "unavailable")
            raise MemoryBackendError(reason) from None
        except (OSError, HTTPException, ValueError) as exc:
            reason = ("timeout" if expired.is_set() or time.monotonic() >= deadline
                      else "bad_response" if isinstance(exc, (HTTPException, ValueError))
                      else "unavailable")
            raise MemoryBackendError(reason) from None
        finally:
            watchdog.cancel()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise MemoryBackendError("bad_response") from None
        if time.monotonic() >= deadline:
            raise MemoryBackendError("timeout")
        return parsed

    def health(self) -> dict[str, Any]:
        now = time.monotonic()
        with _HEALTH_LOCK:
            cached = _HEALTH_CACHE.get(self._health_key)
            if cached and cached[0] > now:
                return dict(cached[1])
        try:
            self._request("GET", "/health/ready", None,
                          int(self.settings.get("connect_timeout_ms", 1000)),
                          expect_json=False)
            result = {"status": "ok", "available": True}
            ttl = 15
        except MemoryBackendError as exc:
            result = {"status": "unavailable", "available": False, "reason": exc.reason}
            ttl = 3
        with _HEALTH_LOCK:
            _HEALTH_CACHE[self._health_key] = (time.monotonic() + ttl, result)
        return dict(result)

    @staticmethod
    def _bank_path(bank_id: str) -> str:
        return "/v1/default/banks/" + quote(bank_id, safe="")

    def retain(self, *, bank_id: str, content: str, kind: str,
               metadata: dict[str, Any]) -> dict[str, Any]:
        document_id = metadata.get("document_id")
        if not isinstance(document_id, str) or not document_id.startswith("cap-"):
            raise MemoryBackendError("bad_response")
        safe_metadata = _safe_metadata({key: value for key, value in metadata.items()
                                        if key != "document_id"})
        # Hindsight's metadata map is string-valued. Preserve the local typed
        # contract while encoding the two structured fields on the wire.
        wire_metadata = {key: value if isinstance(value, str)
                         else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                         for key, value in safe_metadata.items()}
        payload = {"items": [{"content": content,
                              "context": f"Historical {kind} from Codex Global Runtime",
                              "document_id": document_id,
                              "tags": ["global_runtime", f"kind:{kind}"],
                              "metadata": wire_metadata}],
                   "async": False}
        result = self._request("POST", self._bank_path(bank_id) + "/memories",
                               payload, int(self.settings.get("retain_timeout_ms", 5000)))
        if (not isinstance(result, dict) or result.get("success") is not True
                or result.get("async") is not False
                or result.get("bank_id") != bank_id
                or type(result.get("items_count")) is not int
                or result["items_count"] != 1):
            raise MemoryBackendError("bad_response")
        return {"status": "ok", "backend": "hindsight", "bank_id": bank_id,
                "memory_id": document_id, "document_id": document_id,
                "project_id": safe_metadata.get("project_id"), "kind": kind,
                "deduplicated": False}

    def recall(self, *, bank_id: str, query: str, kinds: list[str] | None,
               limit: int, max_chars: int) -> dict[str, Any]:
        payload = {"query": query, "budget": "mid",
                   "max_tokens": max(256, min(4096, max_chars // 3))}
        if kinds:
            payload["tags"] = [f"kind:{kind}" for kind in kinds]
            payload["tags_match"] = "any_strict"
        raw = self._request("POST", self._bank_path(bank_id) + "/memories/recall",
                            payload, int(self.settings.get("recall_timeout_ms", 2500)))
        if not isinstance(raw, dict) or not isinstance(raw.get("results"), list):
            raise MemoryBackendError("bad_response")
        results = []
        remaining = max_chars
        truncated = False
        for entry in raw["results"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("text"), str):
                raise MemoryBackendError("bad_response")
            source_metadata = entry.get("metadata") or {}
            if not isinstance(source_metadata, dict):
                raise MemoryBackendError("bad_response")
            try:
                decoded_metadata = dict(source_metadata)
                for key in ("changed_paths", "architecture_change"):
                    value = decoded_metadata.get(key)
                    if isinstance(value, str):
                        if len(value) > 10000:
                            raise ValueError("oversized metadata")
                        decoded_metadata[key] = json.loads(value)
                metadata = _safe_metadata(decoded_metadata)
            except ValueError:
                # Retrieved legacy content can be sensitive; exclude it entirely.
                continue
            kind = metadata.get("kind")
            if kind not in KINDS:
                tags = entry.get("tags") or []
                if isinstance(tags, list):
                    kind = next((tag[5:] for tag in tags
                                 if isinstance(tag, str) and tag.startswith("kind:")
                                 and tag[5:] in KINDS), None)
            if kinds and kind not in kinds:
                continue
            body = entry["text"]
            if _has_secret(body):
                continue
            if len(results) >= limit or remaining <= 0:
                truncated = True
                break
            clipped = body[:remaining]
            truncated |= len(clipped) < len(body)
            remaining -= len(clipped)
            scores = entry.get("scores")
            score = scores.get("final") if isinstance(scores, dict) else entry.get("score")
            if (isinstance(score, bool) or not isinstance(score, (int, float))
                    or not math.isfinite(score)):
                score = None
            source_task_id = metadata.get("source_task_id") or metadata.get("task_id")
            created_at = metadata.get("created_at") or entry.get("mentioned_at")
            results.append({"text": clipped, "kind": kind, "score": score,
                            "source_task_id": source_task_id, "created_at": created_at,
                            "metadata": metadata, "label": "HISTORICAL_MEMORY"})
        return {"status": "ok", "backend": "hindsight", "bank_id": bank_id,
                "query": query, "results": results, "truncated": truncated}

    def reflect(self, *, bank_id: str, query: str,
                max_chars: int) -> dict[str, Any]:
        raw = self._request("POST", self._bank_path(bank_id) + "/reflect",
                            {"query": query},
                            int(self.settings.get("reflect_timeout_ms", 15000)))
        if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
            raise MemoryBackendError("bad_response")
        body = raw["text"]
        if _has_secret(body):
            raise MemoryBackendError("bad_response")
        return {"status": "ok", "backend": "hindsight", "bank_id": bank_id,
                "text": body[:max_chars], "truncated": len(body) > max_chars,
                "label": "MEMORY_REFLECTION"}
