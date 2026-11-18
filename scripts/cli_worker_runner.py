"""Unified headless CLI worker dispatcher and dual-mode workspace isolator.

Replaces host-coupled native subagents with deterministic OS subprocesses across
AGY-CLI (Carbon/Argon/Hydrogen), Codex-CLI (gpt-6.1-sol/terra), and Claude-CLI,
enforcing CREATE_NO_WINDOW, stdin=DEVNULL, isolated Git worktree / directory
sandboxes, and strict write_paths merge allowlists.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
from typing import Any
from uuid import uuid4

from policy_state import data_dir, record_event
from result_optimizer import optimize_result
from runtime_state import RuntimeError as TaskRuntimeError


CREATE_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000 if os.name == "nt" else 0)
MAX_SANDBOX_ENTRIES = 100_000
MAX_SANDBOX_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB
SKIP_SANDBOX_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".venv",
    "venv",
    ".packages",
}
IGNORED_MUTATION_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
BLOCKED_SECRET_NAMES = {".env", ".env.local", "credentials.json", "secrets.json", "id_rsa", "id_ed25519"}

ROLE_PRESETS: dict[str, dict[str, str]] = {
    "architect": {
        "cli": "agy",
        "model": "gemini-4-carbon-high",
        "effort": "high",
        "mode": "plan",
    },
    "coder": {
        "cli": "agy",
        "model": "gemini-4-hydrogen-high",
        "effort": "high",
        "mode": "auto",
    },
    "coder_dense": {
        "cli": "agy",
        "model": "gemini-4-hydrogen-xhigh",
        "effort": "xhigh",
        "mode": "auto",
    },
    "explorer": {
        "cli": "agy",
        "model": "gemini-4-hydrogen-medium",
        "effort": "medium",
        "mode": "plan",
    },
    "writer": {
        "cli": "agy",
        "model": "gemini-4-argon-high",
        "effort": "high",
        "mode": "auto",
    },
    "verifier": {
        "cli": "codex",
        "model": "gpt-6.1-sol",
        "effort": "high",
        "mode": "exec",
    },
    "sidecar_mechanical": {
        "cli": "agy",
        "model": "gemini-4-hydrogen-medium",
        "effort": "medium",
        "mode": "plan",
    },
}

DEFAULT_BINARIES: dict[str, list[str]] = {
    "agy": ["agy"],
    "codex": ["codex"],
    "claude": ["claude"],
}


def _validate_worker_id(worker_id: str) -> str:
    if not isinstance(worker_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", worker_id):
        raise TaskRuntimeError(f"Invalid worker_id: {worker_id!r}")
    return worker_id


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junc = getattr(path, "is_junction", None)
        if callable(is_junc) and is_junc():
            return True
        st = os.lstat(path)
        file_attrs = getattr(st, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if file_attrs & reparse_flag:
            return True
    except OSError:
        return False
    return False


def _ensure_no_reparse_in_chain(base_root: Path, rel_path: str) -> Path:
    resolved_root = base_root.resolve()
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p and p != "."]
    cur = base_root
    for part in parts:
        if part == "..":
            raise TaskRuntimeError(f"Traversal segment forbidden: {rel_path}")
        cur = cur / part
        if cur.exists() and _is_reparse_or_symlink(cur):
            raise TaskRuntimeError(f"Symlink or junction escape blocked at: {rel_path}")
    resolved_target = cur.resolve()
    if not resolved_target.is_relative_to(resolved_root):
        raise TaskRuntimeError(f"Target escapes workspace root: {rel_path}")
    return cur


def _norm_rel(path_str: str) -> str:
    if not isinstance(path_str, str):
        raise TaskRuntimeError("Path in write_paths must be a string")
    raw_parts = [p for p in path_str.replace("\\", "/").strip().split("/") if p and p != "."]
    if not raw_parts:
        raise TaskRuntimeError("Empty or root relative path is not allowed in write_paths")
    if any(p == ".." for p in raw_parts):
        raise TaskRuntimeError(f"Traversal segment not allowed in path: {path_str}")
    cleaned = "/".join(raw_parts)
    if Path(cleaned).name.casefold() in BLOCKED_SECRET_NAMES:
        raise TaskRuntimeError(f"Secret path blocked in write_paths: {path_str}")
    return cleaned


def _is_path_allowed(rel_path: str, allowed_prefixes: list[str]) -> bool:
    norm = rel_path.replace("\\", "/").strip("/")
    norm_cmp = norm.casefold() if os.name == "nt" else norm
    for prefix in allowed_prefixes:
        p = prefix.replace("\\", "/").strip("/")
        p_cmp = p.casefold() if os.name == "nt" else p
        if norm_cmp == p_cmp or norm_cmp.startswith(p_cmp + "/"):
            return True
    return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_tree(root: Path, scope_prefixes: list[str] | None = None, *, forbid_reparse: bool = False) -> dict[str, str]:
    manifest: dict[str, str] = {}
    targets: list[Path] = []
    if scope_prefixes:
        for prefix in scope_prefixes:
            candidate = root / prefix
            if candidate.exists() or _is_reparse_or_symlink(candidate):
                targets.append(candidate)
    else:
        targets = [root]

    seen: set[Path] = set()
    for target in targets:
        if target != root and _is_reparse_or_symlink(target):
            if forbid_reparse:
                raise TaskRuntimeError(f"Symlink or junction forbidden in sandbox: {target.relative_to(root).as_posix()}")
            continue
        if target.is_file():
            rel = target.relative_to(root).as_posix()
            manifest[rel] = _sha256_file(target)
            continue
        for dirpath, dirnames, filenames in os.walk(target, followlinks=False):
            cur_dir = Path(dirpath)
            safe_dirs: list[str] = []
            for d in dirnames:
                if d in SKIP_SANDBOX_DIRS:
                    continue
                dpath = cur_dir / d
                if _is_reparse_or_symlink(dpath):
                    if forbid_reparse:
                        raise TaskRuntimeError(
                            f"Symlink or junction directory forbidden in sandbox: {dpath.relative_to(root).as_posix()}"
                        )
                    continue
                safe_dirs.append(d)
            dirnames[:] = safe_dirs
            if any(part in IGNORED_MUTATION_PARTS for part in cur_dir.relative_to(root).parts):
                continue
            for fname in filenames:
                fpath = cur_dir / fname
                if fpath in seen:
                    continue
                if _is_reparse_or_symlink(fpath):
                    if forbid_reparse:
                        raise TaskRuntimeError(
                            f"Symlink or junction file forbidden in sandbox: {fpath.relative_to(root).as_posix()}"
                        )
                    continue
                if not fpath.is_file():
                    continue
                seen.add(fpath)
                rel = fpath.relative_to(root).as_posix()
                manifest[rel] = _sha256_file(fpath)
    return manifest


def _resolve_binary(cli_kind: str, override_bin: str | None = None) -> str:
    if override_bin:
        return override_bin
    env_key = f"AGY_WORKER_{cli_kind.upper()}_BIN"
    if os.environ.get(env_key):
        return os.environ[env_key]
    for candidate in DEFAULT_BINARIES.get(cli_kind, [cli_kind]):
        if os.path.isabs(candidate) and Path(candidate).is_file():
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    return DEFAULT_BINARIES.get(cli_kind, [cli_kind])[-1]


def _run_silent(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 30,
    input_bytes: bytes | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    kwargs: dict[str, Any] = {
        "cwd": str(cwd) if cwd else None,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": timeout,
        "creationflags": CREATE_NO_WINDOW,
        "env": env,
    }
    if input_bytes is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = input_bytes
    return subprocess.run(cmd, **kwargs)


def _kill_process_tree(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _detect_git_root(path: Path) -> Path | None:
    try:
        proc = _run_silent(["git", "rev-parse", "--show-toplevel"], cwd=path, timeout=8)
        if proc.returncode == 0:
            top = Path(proc.stdout.decode("utf-8", errors="replace").strip()).resolve()
            if top == path.resolve():
                return top
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def _meta_dir() -> Path:
    folder = data_dir() / "cli_workspaces"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _logs_dir() -> Path:
    folder = data_dir() / "cli_worker_logs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def prepare_workspace(
    source_root: str,
    write_paths: list[str],
    *,
    worker_id: str | None = None,
    copy_roots: list[str] | None = None,
) -> dict[str, Any]:
    """Create an isolated workspace via Git worktree or bounded directory sandbox."""
    src = Path(source_root).resolve()
    if not src.is_dir():
        raise TaskRuntimeError(f"Source workspace does not exist: {source_root}")
    allowed = [_norm_rel(p) for p in write_paths]
    if not allowed:
        raise TaskRuntimeError("Parallel write isolation requires at least one path in write_paths")

    wid = _validate_worker_id(worker_id or f"w_{uuid4().hex[:12]}")
    meta_root = _meta_dir().resolve()
    sandbox_dir = meta_root / wid
    meta_file = meta_root / f"{wid}.meta.json"
    if sandbox_dir.resolve().parent != meta_root or meta_file.resolve().parent != meta_root:
        raise TaskRuntimeError("Invalid worker_id path escape")
    if sandbox_dir.exists():
        raise TaskRuntimeError(f"Workspace sandbox already exists for {wid}")

    git_root = _detect_git_root(src)
    mode = "git_worktree" if git_root is not None else "directory_sandbox"

    if mode == "git_worktree":
        add_proc = _run_silent(["git", "worktree", "add", "--detach", str(sandbox_dir), "HEAD"], cwd=src, timeout=30)
        if add_proc.returncode != 0:
            err = add_proc.stderr.decode("utf-8", errors="replace").strip()
            raise TaskRuntimeError(f"git worktree add failed: {err}")
        diff_proc = _run_silent(["git", "diff", "HEAD", "--binary"], cwd=src, timeout=20)
        if diff_proc.returncode == 0 and diff_proc.stdout:
            _run_silent(["git", "apply", "--whitespace=nowarn", "-"], cwd=sandbox_dir, timeout=20, input_bytes=diff_proc.stdout)
        untracked = _run_silent(["git", "ls-files", "--others", "--exclude-standard"], cwd=src, timeout=15)
        if untracked.returncode == 0 and untracked.stdout:
            for line in untracked.stdout.decode("utf-8", errors="replace").splitlines():
                rel = line.strip()
                if not rel:
                    continue
                sfile = src / rel
                dfile = sandbox_dir / rel
                if sfile.is_file() and sfile.stat().st_size <= 50 * 1024 * 1024:
                    dfile.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(sfile, dfile)
        scope_prefixes = None
    else:
        # Derive minimal top-level copy scope if copy_roots not explicitly given, or full bounded copy
        if copy_roots is not None:
            prefixes = [_norm_rel(p) for p in copy_roots]
        else:
            prefixes = []
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        entry_count = 0
        total_bytes = 0

        if prefixes:
            walk_roots = [(p, src / p) for p in prefixes if (src / p).exists()]
        else:
            walk_roots = [("", src)]

        for rel_base, target in walk_roots:
            if target.is_file():
                entry_count += 1
                size = target.stat().st_size
                total_bytes += size
                if entry_count > MAX_SANDBOX_ENTRIES or total_bytes > MAX_SANDBOX_BYTES:
                    shutil.rmtree(sandbox_dir, ignore_errors=True)
                    raise TaskRuntimeError("Workspace exceeds 100,000 entries or 4 GiB sandbox cap")
                dest = sandbox_dir / rel_base
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, dest)
                continue
            for dirpath, dirnames, filenames in os.walk(target):
                dirnames[:] = [d for d in dirnames if d not in SKIP_SANDBOX_DIRS]
                cur = Path(dirpath)
                rel_dir = cur.relative_to(src)
                (sandbox_dir / rel_dir).mkdir(parents=True, exist_ok=True)
                for fname in filenames:
                    sfile = cur / fname
                    if not sfile.is_file():
                        continue
                    entry_count += 1
                    total_bytes += sfile.stat().st_size
                    if entry_count > MAX_SANDBOX_ENTRIES or total_bytes > MAX_SANDBOX_BYTES:
                        shutil.rmtree(sandbox_dir, ignore_errors=True)
                        raise TaskRuntimeError("Workspace exceeds 100,000 entries or 4 GiB sandbox cap")
                    shutil.copy2(sfile, sandbox_dir / rel_dir / fname)
        scope_prefixes = prefixes or None

    baseline = _snapshot_tree(sandbox_dir, scope_prefixes)
    meta = {
        "worker_id": wid,
        "mode": mode,
        "source_root": str(src),
        "sandbox_dir": str(sandbox_dir),
        "write_paths": allowed,
        "scope_prefixes": scope_prefixes,
        "baseline_hashes": baseline,
        "merged": False,
        "created_at": time.time(),
    }
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    record_event("cli_workspace_prepared", {"worker_id": wid, "mode": mode, "write_paths": allowed})
    return {
        "worker_id": wid,
        "mode": mode,
        "source_root": str(src),
        "sandbox_dir": str(sandbox_dir),
        "write_paths": allowed,
        "baseline_file_count": len(baseline),
    }


def merge_workspace(worker_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Verify mutations against write_paths allowlist and merge into source_root."""
    wid = _validate_worker_id(worker_id)
    meta_root = _meta_dir().resolve()
    meta_file = (meta_root / f"{wid}.meta.json").resolve()
    if meta_file.parent != meta_root or not meta_file.is_file():
        raise TaskRuntimeError(f"Unknown isolated workspace: {worker_id}")
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    sandbox_dir = Path(meta["sandbox_dir"])
    source_root = Path(meta["source_root"])
    allowed: list[str] = meta["write_paths"]
    baseline: dict[str, str] = meta["baseline_hashes"]
    if not sandbox_dir.is_dir():
        raise TaskRuntimeError(f"Sandbox directory missing for {wid}")

    current = _snapshot_tree(sandbox_dir, meta.get("scope_prefixes"), forbid_reparse=True)
    added_or_modified = [
        rel for rel, sha in sorted(current.items()) if baseline.get(rel) != sha
    ]
    deleted = [rel for rel in sorted(baseline) if rel not in current]
    all_mutations = sorted(set(added_or_modified) | set(deleted))

    unauthorized = [rel for rel in all_mutations if not _is_path_allowed(rel, allowed)]
    if unauthorized:
        raise TaskRuntimeError(
            "Unauthorized file mutation outside write_paths: " + ", ".join(unauthorized)
        )

    # Validate both sandbox source and destination chains contain zero symlinks/junctions
    for rel in all_mutations:
        _ensure_no_reparse_in_chain(sandbox_dir, rel)
        _ensure_no_reparse_in_chain(source_root, rel)

    if not dry_run:
        for rel in added_or_modified:
            src_file = _ensure_no_reparse_in_chain(sandbox_dir, rel)
            dst_file = _ensure_no_reparse_in_chain(source_root, rel)
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = dst_file.with_name(f".{dst_file.name}.{uuid4().hex[:8]}.tmp")
            shutil.copy2(src_file, tmp_file, follow_symlinks=False)
            os.replace(tmp_file, dst_file)
        for rel in deleted:
            dst_file = _ensure_no_reparse_in_chain(source_root, rel)
            if dst_file.is_file() and not _is_reparse_or_symlink(dst_file):
                dst_file.unlink()
        meta["merged"] = True
        meta["merged_at"] = time.time()
        meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        record_event("cli_workspace_merged", {"worker_id": wid, "changed_files": all_mutations})

    return {
        "worker_id": wid,
        "dry_run": dry_run,
        "added_or_modified": added_or_modified,
        "deleted": deleted,
        "changed_files": all_mutations,
    }


def cleanup_workspace(worker_id: str, *, force: bool = False) -> dict[str, Any]:
    """Remove an isolated workspace only after merge (unless force=True)."""
    wid = _validate_worker_id(worker_id)
    meta_root = _meta_dir().resolve()
    meta_file = (meta_root / f"{wid}.meta.json").resolve()
    if meta_file.parent != meta_root or not meta_file.is_file():
        raise TaskRuntimeError(f"Unknown isolated workspace: {worker_id}")
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    sandbox_dir = Path(meta["sandbox_dir"])
    source_root = Path(meta["source_root"])

    if not meta.get("merged") and not force and sandbox_dir.is_dir():
        current = _snapshot_tree(sandbox_dir, meta.get("scope_prefixes"))
        dirty = [rel for rel, sha in current.items() if meta["baseline_hashes"].get(rel) != sha]
        if dirty:
            raise TaskRuntimeError(
                f"Refusing to delete unmerged workspace {worker_id} with dirty files: {dirty[:5]}"
            )

    if meta.get("mode") == "git_worktree" and sandbox_dir.is_dir():
        _run_silent(["git", "worktree", "remove", "--force", str(sandbox_dir)], cwd=source_root, timeout=20)
    if sandbox_dir.exists():
        shutil.rmtree(sandbox_dir, ignore_errors=True)
    meta_file.unlink(missing_ok=True)
    record_event("cli_workspace_cleaned", {"worker_id": worker_id, "force": force})
    return {"worker_id": worker_id, "cleaned": True}


def build_cli_command(
    *,
    role_preset: str = "coder",
    prompt: str,
    cwd: str,
    raw_user_request: str | None = None,
    cli_kind: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    mode: str | None = None,
    conversation_id: str | None = None,
    output_format: str = "text",
    add_dirs: list[str] | None = None,
    skip_permissions: bool = True,
    settings_override: dict[str, Any] | None = None,
    binary_override: str | None = None,
    log_path: str | None = None,
) -> dict[str, Any]:
    """Construct validated non-interactive CLI command array and environment."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise TaskRuntimeError("CLI worker prompt must be a non-empty string")
    preset = ROLE_PRESETS.get(role_preset, ROLE_PRESETS["coder"])
    chosen_cli = (cli_kind or preset["cli"]).lower()
    chosen_model = model or preset["model"]
    chosen_effort = effort or preset["effort"]
    chosen_mode = mode or preset["mode"]

    if role_preset == "architect" and (not isinstance(raw_user_request, str) or not raw_user_request.strip()):
        raise TaskRuntimeError(
            "Architect (Carbon) dispatch requires raw_user_request verbatim to prevent router summarization loss"
        )

    final_prompt = prompt.strip()
    if raw_user_request and raw_user_request.strip() not in final_prompt:
        final_prompt = f"【用户原始需求全文】\n{raw_user_request.strip()}\n\n【执行边界与目标】\n{final_prompt}"

    exe = _resolve_binary(chosen_cli, binary_override)
    workdir = str(Path(cwd).resolve())

    if chosen_cli == "agy":
        cmd = [exe, "-p", final_prompt, "--model", chosen_model, "--effort", chosen_effort]
        if skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        elif chosen_mode in {"accept-edits", "auto", "plan"}:
            cmd.extend(["--mode", chosen_mode])
        for extra_dir in add_dirs or []:
            if isinstance(extra_dir, str) and extra_dir.strip():
                cmd.extend(["--add-dir", str(Path(extra_dir).resolve())])
        if output_format in {"text", "json", "stream-json"} and output_format != "text":
            cmd.extend(["--output-format", output_format])
        if conversation_id:
            cmd.extend(["--conversation", conversation_id])
        if settings_override:
            cmd.extend(["--settings", json.dumps(settings_override, ensure_ascii=False)])
        if log_path:
            cmd.extend(["--log-file", log_path])
    elif chosen_cli == "codex":
        cmd = [
            exe,
            "exec",
            "-m",
            chosen_model,
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            workdir,
            final_prompt,
        ]
    elif chosen_cli == "claude":
        cmd = [exe, "-p", final_prompt, "--model", chosen_model]
    else:
        raise TaskRuntimeError(f"Unsupported cli_kind: {chosen_cli}")

    return {
        "cli_kind": chosen_cli,
        "role_preset": role_preset,
        "model": chosen_model,
        "effort": chosen_effort,
        "cwd": workdir,
        "command": cmd,
        "stdin_policy": "DEVNULL_EOF",
        "creationflags": CREATE_NO_WINDOW,
    }


def run_cli_worker(
    *,
    role_preset: str = "coder",
    prompt: str,
    cwd: str,
    task_id: str | None = None,
    step_id: str | None = None,
    raw_user_request: str | None = None,
    cli_kind: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    mode: str | None = None,
    conversation_id: str | None = None,
    add_dirs: list[str] | None = None,
    skip_permissions: bool = True,
    settings_override: dict[str, Any] | None = None,
    binary_override: str | None = None,
    env_overrides: dict[str, str] | None = None,
    timeout_sec: int = 600,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute a headless CLI worker with silent process flags, log capture, and Evidence compression."""
    if timeout_sec < 1 or timeout_sec > 5400:
        raise TaskRuntimeError("timeout_sec must be between 1 and 5400 seconds")
    run_id = f"run_{uuid4().hex[:12]}"
    log_file = _logs_dir() / f"{run_id}.out.log"
    ls_diag_log = _logs_dir() / f"{run_id}.ls.log"
    spec = build_cli_command(
        role_preset=role_preset,
        prompt=prompt,
        cwd=cwd,
        raw_user_request=raw_user_request,
        cli_kind=cli_kind,
        model=model,
        effort=effort,
        mode=mode,
        conversation_id=conversation_id,
        add_dirs=add_dirs,
        skip_permissions=skip_permissions,
        settings_override=settings_override,
        binary_override=binary_override,
        log_path=str(ls_diag_log) if (cli_kind or ROLE_PRESETS.get(role_preset, {}).get("cli")) == "agy" else None,
    )
    if dry_run:
        return {**spec, "run_id": run_id, "log_path": str(log_file), "dry_run": True}

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if env_overrides:
        for k, v in env_overrides.items():
            env[str(k)] = str(v)

    started = time.time()
    with log_file.open("wb") as log_handle:
        proc = subprocess.Popen(
            spec["command"],
            cwd=spec["cwd"],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=CREATE_NO_WINDOW,
        )
        timed_out = False
        try:
            exit_code = proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_tree(proc.pid)
            exit_code = -9

    duration_ms = int((time.time() - started) * 1000)
    raw_output = log_file.read_text(encoding="utf-8", errors="replace") if log_file.is_file() else ""
    evidence_id = None
    compact_output = raw_output[:4000]
    if task_id and raw_output:
        try:
            opt = optimize_result(
                task_id,
                raw_output,
                step_id=step_id,
                tool=f"cli_worker:{spec['cli_kind']}",
                command=" ".join(spec["command"][:4]),
                exit_code=exit_code,
            )
            compact_output = opt.get("compact_result", compact_output)
            evidence_id = opt.get("evidence_id")
        except Exception:
            compact_output = raw_output[:4000]

    record_event(
        "cli_worker_completed",
        {
            "run_id": run_id,
            "cli_kind": spec["cli_kind"],
            "model": spec["model"],
            "exit_code": exit_code,
            "timed_out": timed_out,
            "duration_ms": duration_ms,
        },
    )
    return {
        "run_id": run_id,
        "pid": proc.pid,
        "cli_kind": spec["cli_kind"],
        "model": spec["model"],
        "effort": spec["effort"],
        "cwd": spec["cwd"],
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "log_path": str(log_file),
        "evidence_id": evidence_id,
        "compact_output": compact_output,
    }
