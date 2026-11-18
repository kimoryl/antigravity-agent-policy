"""Disable pooled Skill paths through Codex's official app-server API."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    entries = [item for item in json.loads((ROOT / "skill_pool" / "registry.json").read_text(encoding="utf-8"))["skills"]
               if isinstance(item.get("source_path"), str)
               and Path(item["source_path"]).is_absolute()
               and Path(item["source_path"]).is_file()]
    process = subprocess.Popen(["codex", "app-server", "--stdio"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, encoding="utf-8", bufsize=1,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0)
    responses: queue.Queue[dict] = queue.Queue()

    def collect() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in response:
                responses.put(response)

    threading.Thread(target=collect, daemon=True).start()

    def request(request_id: int, method: str, params: dict) -> dict:
        assert process.stdin is not None
        process.stdin.write(json.dumps({"method": method, "id": request_id, "params": params}) + "\n")
        process.stdin.flush()
        response = responses.get(timeout=20)
        if response["id"] != request_id:
            raise RuntimeError(f"Unexpected app-server response ID {response['id']}")
        return response

    try:
        initialized = request(1, "initialize", {"clientInfo": {
            "name": "codex-agent-policy-skill-pool", "title": "Skill Pool Setup", "version": "0.1.0"}})
        if "error" in initialized:
            raise RuntimeError(f"App-server initialization failed: {initialized['error']}")
        assert process.stdin is not None
        process.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
        process.stdin.flush()
        failed = []
        for number, item in enumerate(entries, start=2):
            response = request(number, "skills/config/write", {"path": item["source_path"], "enabled": False})
            if "error" in response:
                failed.append({"name": item["name"], "code": response["error"].get("code")})
        listed = request(len(entries) + 2, "skills/list", {"cwds": [str(ROOT)], "forceReload": True})
        skills = listed.get("result", {}).get("data", [{}])[0].get("skills", [])
        targets = {Path(item["source_path"]).resolve() for item in entries}
        observed = [item for item in skills if Path(item["path"]).resolve() in targets]
        print(json.dumps({"requested": len(entries), "failed": failed,
                          "observed_pooled": len(observed),
                          "observed_disabled": sum(item.get("enabled") is False for item in observed),
                          "resident_enabled": [item["name"] for item in skills if item.get("enabled")],
                          "list_error": listed.get("error")}, ensure_ascii=False))
        if failed or listed.get("error") or any(item.get("enabled") for item in observed):
            raise SystemExit(1)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


if __name__ == "__main__":
    main()
