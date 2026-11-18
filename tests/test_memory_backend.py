from __future__ import annotations

import json
import gc
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import hindsight_memory  # noqa: E402
import memory_backend  # noqa: E402
from runtime_state import add_memory, create_task  # noqa: E402


class LocalHindsightServer:
    """Minimal Hindsight-shaped server; every request stays on loopback."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.health_status = 200
        self.health_delay = 0.0
        self.recall_status = 200
        self.recall_body: dict = {"results": []}
        self.recall_raw: bytes | None = None
        self.recall_chunks: list[bytes] | None = None
        self.recall_chunk_delay = 0.0
        self.retain_status = 200
        self.retain_fail_on: set[int] = set()
        self.retain_attempts = 0
        self.reflect_status = 200
        self.reflect_body: dict = {"text": "A concise reflection"}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                pass

            def _respond(self, status: int, body: dict | bytes) -> None:
                raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except OSError:
                    # A timed-out client may close while this handler is sleeping.
                    pass

            def _stream_recall(self) -> None:
                chunks = owner.recall_chunks or []
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(sum(map(len, chunks))))
                self.end_headers()
                try:
                    for index, chunk in enumerate(chunks):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        if index + 1 < len(chunks):
                            time.sleep(owner.recall_chunk_delay)
                except OSError:
                    pass

            def _handle(self) -> None:
                path = urlsplit(self.path).path
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length)) if length else None
                owner.requests.append({
                    "method": self.command, "path": path, "payload": payload,
                    "authorization": self.headers.get("Authorization"),
                })
                if self.command == "GET" and path == "/health/ready":
                    if owner.health_delay:
                        time.sleep(owner.health_delay)
                    self._respond(owner.health_status, {})
                    return
                if self.command != "POST" or not path.startswith("/v1/default/banks/"):
                    self._respond(404, {})
                    return
                bank = unquote(path.split("/")[4])
                if path.endswith("/memories/recall"):
                    if owner.recall_chunks is not None:
                        self._stream_recall()
                    else:
                        self._respond(owner.recall_status,
                                      owner.recall_raw if owner.recall_raw is not None else owner.recall_body)
                elif path.endswith("/memories"):
                    owner.retain_attempts += 1
                    status = 500 if owner.retain_attempts in owner.retain_fail_on else owner.retain_status
                    self._respond(status, {"success": True, "async": False,
                                           "bank_id": bank, "items_count": 1})
                elif path.endswith("/reflect"):
                    self._respond(owner.reflect_status, owner.reflect_body)
                else:
                    self._respond(404, {})

            do_GET = _handle
            do_POST = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def calls(self, suffix: str) -> list[dict]:
        return [request for request in self.requests if request["path"].endswith(suffix)]


class MemoryHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        # sqlite3's context manager commits but does not close the connection.
        # Collect short-lived adapter connections before Windows removes the temp DB.
        self.addCleanup(gc.collect)
        self.tmp = Path(self.folder.name)
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.config_path = self.tmp / "memory.json"
        self.env_patch = patch.dict(os.environ, {
            "CODEX_AGENT_POLICY_DATA": str(self.tmp / "data"),
            "CODEX_AGENT_POLICY_MEMORY_CONFIG": str(self.config_path),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "",
            "http_proxy": "", "https_proxy": "", "all_proxy": "",
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        for name in ("HINDSIGHT_API_URL", "HINDSIGHT_API_KEY", "HINDSIGHT_API_TOKEN",
                     "HINDSIGHT_CODEX_HOOKS_ENABLED"):
            os.environ.pop(name, None)
        self.allowed_ports: set[int] = set()
        self.real_create_connection = socket.create_connection

        def only_local(address: tuple, *args: object, **kwargs: object) -> socket.socket:
            host, port = address[:2]
            if host not in {"127.0.0.1", "localhost", "::1"} or port not in self.allowed_ports:
                raise AssertionError(f"Unexpected network destination: {host}:{port}")
            return self.real_create_connection(address, *args, **kwargs)

        self.network_patch = patch("socket.create_connection", side_effect=only_local)
        self.network_patch.start()
        self.addCleanup(self.network_patch.stop)
        hindsight_memory._HEALTH_CACHE.clear()
        self.addCleanup(hindsight_memory._HEALTH_CACHE.clear)
        self.server: LocalHindsightServer | None = None
        self.write_config()

    def write_config(self, *, backend: str = "auto", url: str | None = None) -> None:
        config = json.loads((ROOT / "memory" / "default.json").read_text(encoding="utf-8"))
        config["backend"] = backend
        config["hindsight"]["api_url"] = url
        config["hindsight"]["local_only"] = True
        config["hindsight"]["connect_timeout_ms"] = 100
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        memory_backend._read_config.cache_clear()

    def start_server(self) -> LocalHindsightServer:
        self.server = LocalHindsightServer()
        self.addCleanup(self.server.close)
        self.allowed_ports.add(self.server.port)
        self.write_config(url=self.server.url)
        return self.server

    def make_task(self, project: Path | None = None, *, session_id: str | None = None,
                  task_type: str = "testing") -> str:
        root = project or self.project
        profile = {
            "root_goal": "Keep runtime memories useful", "local_goal": "Verify memory behavior",
            "task_type": task_type, "constraints": ["Keep project data separate"],
            "expected_outputs": ["tests"], "acceptance_criteria": ["tests pass"],
            "required_capabilities": ["testing"], "complexity_hint": "moderate",
        }
        return create_task(profile, str(root), session_id)["task_id"]


class MemoryBackendTests(MemoryHarness):
    def test_unconfigured_auto_uses_sqlite_without_any_network_request(self) -> None:
        task_id = self.make_task()
        status = memory_backend.memory_status(task_id)
        self.assertEqual((status["status"], status["backend"], status["reason"]),
                         ("fallback", "sqlite", "not_configured"))
        retained = memory_backend.memory_retain(task_id, "decision", "Keep a stable API")
        recalled = memory_backend.memory_recall(task_id, "stable API")
        self.assertEqual(retained["backend"], "sqlite")
        self.assertEqual(recalled["status"], "fallback")
        self.assertEqual([item["text"] for item in recalled["results"]], ["Keep a stable API"])

    def test_healthy_hindsight_selected_and_retains_all_supported_kinds_once(self) -> None:
        server = self.start_server()
        task_id = self.make_task()
        status = memory_backend.memory_status(task_id)
        self.assertEqual((status["status"], status["backend"], status["available"]),
                         ("ok", "hindsight", True))
        for kind in sorted(memory_backend.KINDS):
            metadata = ({"changed_paths": ["src/app.py", "tests/test_app.py"],
                         "architecture_change": True} if kind == "decision" else None)
            result = memory_backend.memory_retain(task_id, kind,
                                                  f"A useful {kind} note", metadata)
            self.assertEqual((result["status"], result["backend"], result["kind"]),
                             ("ok", "hindsight", kind))
        writes = server.calls("/memories")
        self.assertEqual(len(writes), 5)
        self.assertEqual({call["payload"]["items"][0]["tags"][1] for call in writes},
                         {f"kind:{kind}" for kind in memory_backend.KINDS})
        self.assertTrue(all(call["payload"]["items"][0]["document_id"].startswith("cap-")
                            for call in writes))
        self.assertTrue(all(isinstance(value, str)
                            for call in writes
                            for value in call["payload"]["items"][0]["metadata"].values()))
        duplicate = memory_backend.memory_retain(task_id, "decision", "  A useful decision   note  ")
        self.assertTrue(duplicate["deduplicated"])
        self.assertEqual(len(server.calls("/memories")), 5)
        upper = memory_backend.memory_retain(task_id, "lesson", "Use Foo.py")
        lower = memory_backend.memory_retain(task_id, "lesson", "Use foo.py")
        self.assertFalse(upper["deduplicated"])
        self.assertFalse(lower["deduplicated"])
        self.assertEqual(len(server.calls("/memories")), 7)

    def test_secret_content_and_sensitive_metadata_never_leave_process(self) -> None:
        server = self.start_server()
        task_id = self.make_task()
        cases = (
            ("api_key=obviously_fake_value_123", None),
            ("Safe lesson", {"changed_paths": ["config/.env.local"]}),
            ("Another lesson", {"changed_paths": ["keys/id_ed25519"]}),
        )
        for summary, metadata in cases:
            with self.subTest(summary=summary):
                result = memory_backend.memory_retain(task_id, "lesson", summary, metadata)
                self.assertEqual((result["status"], result["reason"]),
                                 ("rejected", "secret_detected"))
        self.assertEqual(server.calls("/memories"), [])

    def test_automatic_task_fields_with_secrets_are_rejected_before_http(self) -> None:
        server = self.start_server()
        for task_id in (
            self.make_task(session_id="api_key=obviously_fake_value_123"),
            self.make_task(task_type="password=obviously_fake_value_123"),
        ):
            with self.subTest(task_id=task_id):
                result = memory_backend.memory_retain(task_id, "finding", "Safe summary")
                self.assertEqual((result["status"], result["reason"]),
                                 ("rejected", "secret_detected"))
        self.assertEqual(server.requests, [])

    def test_recall_enforces_kind_count_character_budget_and_bank_isolation(self) -> None:
        server = self.start_server()
        task_id = self.make_task()
        server.recall_body = {"results": [
            {"text": "D" * 80, "metadata": {"kind": "decision", "source_task_id": "old-1",
                                           "changed_paths": '["src/app.py","tests/test_app.py"]',
                                           "architecture_change": "true"},
             "scores": {"final": 0.9}},
            {"text": "F" * 80, "metadata": {"kind": "finding"}},
            {"text": "Q" * 80, "metadata": {"kind": "decision"}},
            {"text": "Bearer fake_secret_token_123456789", "metadata": {"kind": "decision"}},
        ]}
        result = memory_backend.memory_recall(task_id, "decision", ["decision"],
                                              limit=2, max_chars=90)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual([len(item["text"]) for item in result["results"]], [80, 10])
        self.assertTrue(result["truncated"])
        self.assertEqual({item["kind"] for item in result["results"]}, {"decision"})
        self.assertTrue(all(item["label"] == "HISTORICAL_MEMORY" for item in result["results"]))
        self.assertEqual(result["results"][0]["source_task_id"], "old-1")
        self.assertEqual(result["results"][0]["score"], 0.9)
        self.assertEqual(result["results"][0]["metadata"]["changed_paths"],
                         ["src/app.py", "tests/test_app.py"])
        self.assertIs(result["results"][0]["metadata"]["architecture_change"], True)
        other_project = self.tmp / "other-project"
        other_project.mkdir()
        other_task = self.make_task(other_project)
        other = memory_backend.memory_recall(other_task, "decision")
        self.assertNotEqual(result["bank_id"], other["bank_id"])
        calls = server.calls("/memories/recall")
        self.assertEqual(len(calls), 2)
        self.assertIn(result["bank_id"], unquote(calls[0]["path"]))
        self.assertIn(other["bank_id"], unquote(calls[1]["path"]))
        self.assertEqual(calls[0]["payload"]["tags"], ["kind:decision"])
        self.assertIn("tags_match", calls[0]["payload"])

    def test_health_http_errors_timeout_and_bad_recall_fall_back_to_sqlite(self) -> None:
        server = self.start_server()
        task_id = self.make_task()
        add_memory(task_id, "lesson", "Local recovery note")
        for status, reason in ((401, "auth_error"), (429, "rate_limited"), (500, "unavailable")):
            with self.subTest(status=status):
                hindsight_memory._HEALTH_CACHE.clear()
                server.health_status = status
                result = memory_backend.memory_recall(task_id, "recovery")
                self.assertEqual((result["status"], result["backend"], result["reason"]),
                                 ("fallback", "sqlite", reason))
                self.assertEqual(result["results"][0]["text"], "Local recovery note")
        hindsight_memory._HEALTH_CACHE.clear()
        server.health_status = 200
        server.health_delay = 0.3
        timed_out = memory_backend.memory_recall(task_id, "recovery")
        self.assertEqual((timed_out["status"], timed_out["reason"]), ("fallback", "timeout"))
        hindsight_memory._HEALTH_CACHE.clear()
        server.health_delay = 0
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["hindsight"]["connect_timeout_ms"] = 1000
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        memory_backend._read_config.cache_clear()
        server.recall_raw = b"{invalid json"
        malformed = memory_backend.memory_recall(task_id, "recovery")
        self.assertEqual(len(server.calls("/memories/recall")), 1)
        self.assertEqual((malformed["status"], malformed["reason"]),
                         ("fallback", "bad_response"))

    def test_failed_hindsight_write_falls_back_and_can_be_retried(self) -> None:
        server = self.start_server()
        task_id = self.make_task()
        server.retain_status = 500
        first = memory_backend.memory_retain(task_id, "decision", "Retry this write")
        self.assertEqual((first["status"], first["backend"], first["reason"]),
                         ("fallback", "sqlite", "unavailable"))
        self.assertEqual(server.retain_attempts, 1)
        self.assertEqual(memory_backend.memory_recall(task_id, "Retry")["results"][0]["text"],
                         "Retry this write")

        hindsight_memory._HEALTH_CACHE.clear()
        server.retain_status = 200
        second = memory_backend.memory_retain(task_id, "decision", "Retry this write")
        self.assertEqual((second["status"], second["backend"], second["deduplicated"]),
                         ("ok", "hindsight", False))
        self.assertEqual(server.retain_attempts, 2)

    def test_slow_stream_exceeding_total_recall_deadline_falls_back(self) -> None:
        server = self.start_server()
        task_id = self.make_task()
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["hindsight"]["recall_timeout_ms"] = 150
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        memory_backend._read_config.cache_clear()
        payload = b'{"results":[]}' + b" " * 100
        server.recall_chunks = [payload[index:index + 5]
                                for index in range(0, len(payload), 5)]
        server.recall_chunk_delay = 0.08  # Each gap is below the socket timeout.

        started = time.monotonic()
        result = memory_backend.memory_recall(task_id, "slow stream")
        elapsed = time.monotonic() - started
        self.assertEqual((result["status"], result["backend"], result.get("reason")),
                         ("fallback", "sqlite", "timeout"))
        self.assertLess(elapsed, 1.0)
        self.assertEqual(len(server.calls("/memories/recall")), 1)

    def test_invalid_endpoint_port_safely_falls_back_without_network(self) -> None:
        self.write_config(url="http://localhost:bad")
        status = memory_backend.memory_status(self.make_task())
        self.assertEqual((status["status"], status["backend"], status["reason"]),
                         ("fallback", "sqlite", "not_configured"))

    def test_token_over_plain_http_to_non_loopback_is_rejected_without_network(self) -> None:
        self.write_config(url="http://example.invalid")
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["hindsight"]["local_only"] = False
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        memory_backend._read_config.cache_clear()
        os.environ["HINDSIGHT_API_KEY"] = "a_fake_test_token"
        status = memory_backend.memory_status(self.make_task())
        self.assertEqual((status["status"], status["backend"], status["reason"]),
                         ("fallback", "sqlite", "not_configured"))

    def test_control_characters_in_token_are_rejected_before_http(self) -> None:
        server = self.start_server()
        task_id = self.make_task()
        for value in ("fake\nvalue", "fake\x01value"):
            with self.subTest(control=repr(value)):
                os.environ["HINDSIGHT_API_KEY"] = value
                hindsight_memory._HEALTH_CACHE.clear()
                status = memory_backend.memory_status(task_id)
                self.assertEqual((status["status"], status["backend"], status["reason"]),
                                 ("fallback", "sqlite", "auth_error"))
        self.assertEqual(server.requests, [])

    def test_reflect_is_explicit_and_does_not_write_memory(self) -> None:
        server = self.start_server()
        task_id = self.make_task()
        memory_backend.memory_status(task_id)
        self.assertEqual(server.calls("/reflect"), [])
        server.reflect_body = {"text": "Consider what was learned"}
        reflected = memory_backend.memory_reflect(task_id, "What happened?", max_chars=8)
        self.assertEqual((reflected["status"], reflected["text"], reflected["label"]),
                         ("ok", "Consider", "MEMORY_REFLECTION"))
        self.assertTrue(reflected["truncated"])
        self.assertEqual(len(server.calls("/reflect")), 1)
        self.assertEqual(server.calls("/memories"), [])

    def test_configuration_without_local_only_remains_compatible(self) -> None:
        server = self.start_server()
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        del config["hindsight"]["local_only"]
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        memory_backend._read_config.cache_clear()
        status = memory_backend.memory_status(self.make_task())
        self.assertEqual((status["status"], status["backend"]), ("ok", "hindsight"))
        self.assertEqual(len(server.calls("/health/ready")), 1)


class MemoryBankTests(MemoryHarness):
    def test_local_git_worktree_shares_bank(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is unavailable")
        repo = self.tmp / "repo"
        repo.mkdir()

        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                           text=True, timeout=10)

        git("init", "-q")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "--allow-empty", "-q", "-m", "base")
        worktree = self.tmp / "worktree"
        git("worktree", "add", "-q", "--detach", str(worktree), "HEAD")
        self.assertEqual(memory_backend.resolve_memory_bank(repo, "repo-one"),
                         memory_backend.resolve_memory_bank(worktree, "repo-two"))

    def test_non_git_project_uses_project_id(self) -> None:
        bank = memory_backend.resolve_memory_bank(self.project, "project-123")
        self.assertEqual(bank, "codex-agent-policy::project::project-123")


if __name__ == "__main__":
    unittest.main()
