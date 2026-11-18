"""Versioned deliveries and human reviews, separate from execution state.

File bytes are retained in SQLite so overwriting a source never destroys a
reviewed version. Only explicitly registered, non-private project files are read.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from runtime_state import RuntimeError, _db as runtime_db, _row


class DeliveryError(RuntimeError):
    pass


class DeliveryConflict(DeliveryError):
    pass


MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_DELIVERY_BYTES = 128 * 1024 * 1024


def _integer(value, minimum=1):
    if type(value) is not int or value < minimum:
        raise DeliveryError('任务修订号和交付版本必须是有效整数')


@contextmanager
def _db(write=False):
    with runtime_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS delivery_versions (
              id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
              version INTEGER NOT NULL, task_revision INTEGER NOT NULL,
              summary TEXT NOT NULL, checks TEXT NOT NULL, requirements TEXT NOT NULL,
              status TEXT NOT NULL, invalid_reason TEXT NOT NULL DEFAULT '',
              reopened_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
              UNIQUE(task_id, version)
            );
            CREATE TABLE IF NOT EXISTS delivery_files (
              id TEXT PRIMARY KEY, delivery_id TEXT NOT NULL REFERENCES delivery_versions(id),
              kind TEXT NOT NULL, path TEXT NOT NULL, name TEXT NOT NULL,
              digest TEXT, content BLOB, UNIQUE(delivery_id, path)
            );
            CREATE TABLE IF NOT EXISTS delivery_reviews (
              id TEXT PRIMARY KEY, delivery_id TEXT NOT NULL REFERENCES delivery_versions(id),
              decision TEXT NOT NULL, comment TEXT NOT NULL, reviewer TEXT NOT NULL,
              created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS delivery_reopens (
              id TEXT PRIMARY KEY, delivery_id TEXT NOT NULL REFERENCES delivery_versions(id),
              reason TEXT NOT NULL, previous_state TEXT NOT NULL, created_at REAL NOT NULL
            );
        """)
        if write:
            db.execute("BEGIN IMMEDIATE")
        yield db


def _task(db, task_id):
    row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise DeliveryError("Unknown task_id")
    return _row(row)


def _latest(db, task_id):
    return db.execute("SELECT * FROM delivery_versions WHERE task_id = ? ORDER BY version DESC LIMIT 1", (task_id,)).fetchone()


def _source(task, raw):
    if not isinstance(raw, str) or not raw.strip():
        raise DeliveryError("文件路径不能为空")
    root = Path(task["project_root"]).resolve()
    path = Path(raw)
    path = (path if path.is_absolute() else root / path).resolve()
    if not path.is_relative_to(root):
        raise DeliveryError("仅允许登记任务项目目录内的文件")
    parts = path.relative_to(root).parts
    if (any(p.startswith('.') for p in parts)
            or any(word in p.casefold() for p in parts for word in ('credential', 'password', 'secret', 'token'))
            or path.name.casefold() in {'config.toml', 'id_rsa', 'id_ed25519'}
            or path.suffix.casefold() in {'.pem', '.key', '.p12'}):
        raise DeliveryError("不能登记私密配置或凭据文件")
    return path


def _read(path):
    if not path.exists():
        return None
    if not path.is_file():
        raise DeliveryError("产物路径必须是文件")
    with path.open('rb') as stream:
        value = stream.read(MAX_FILE_BYTES + 1)
    if len(value) > MAX_FILE_BYTES:
        raise DeliveryError("单个产物不能超过 64 MiB")
    return value


def _integrity(task, item):
    try:
        content = _read(_source(task, item['path']))
    except (OSError, ValueError):
        return 'unavailable'
    if content is None:
        return 'missing'
    if item['digest'] is None or sha256(content).hexdigest() != item['digest']:
        return 'changed'
    return 'ok'


def _refresh(db, task, latest):
    if latest is None or latest['reopened_at'] is not None or latest['status'] not in {'pending_review', 'accepted'}:
        return latest
    files = db.execute("SELECT id, path, digest FROM delivery_files WHERE delivery_id = ?", (latest['id'],)).fetchall()
    if not files or any(_integrity(task, f) != 'ok' for f in files):
        db.execute("UPDATE delivery_versions SET status = 'invalidated', invalid_reason = ?, updated_at = ? WHERE id = ?",
                   ('登记文件已修改、缺失或无法读取；请重开任务并提交新版后重新验收。', time.time(), latest['id']))
        return _latest(db, task['task_id'])
    return latest


def submit(task_id, expected_revision, expected_version, summary, artifacts, check_report):
    """Call after runtime_task(complete); this never grants human acceptance."""
    _integer(expected_revision)
    _integer(expected_version, 0)
    if not isinstance(summary, str) or not summary.strip():
        raise DeliveryError("交付摘要不能为空")
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 30:
        raise DeliveryError("请登记 1 至 30 个实际产物")
    if not isinstance(check_report, dict) or not isinstance(check_report.get('summary'), str) or not check_report['summary'].strip():
        raise DeliveryError("需要包含 summary 的检查报告")
    encoded_checks = json.dumps(check_report, ensure_ascii=False)
    if len(summary) > 10000 or len(encoded_checks) > 100000:
        raise DeliveryError("交付说明或报告过长")
    with _db(True) as db:
        task = _task(db, task_id)
        latest = _latest(db, task_id)
        if task['revision'] != expected_revision or (latest['version'] if latest else 0) != expected_version:
            raise DeliveryConflict("任务或交付版本已变化，请刷新后重试")
        if task['status'] != 'complete':
            raise DeliveryConflict("执行完成后才能提交交付，请先完成计划与检查")
        if latest and latest['reopened_at'] is None:
            raise DeliveryConflict("修改产物前必须显式重开任务，旧交付将保留")
        delivery_id, now = uuid4().hex, time.time()
        files, paths, size = [], set(), 0
        entries = [(a, 'artifact') for a in artifacts]
        if check_report.get('path'):
            entries.append(({'path': check_report['path']}, 'report'))
        for entry, kind in entries:
            if not isinstance(entry, dict):
                raise DeliveryError("产物必须是包含 path 的对象")
            path = _source(task, entry.get('path'))
            if str(path).casefold() in paths:
                raise DeliveryError("产物路径重复")
            paths.add(str(path).casefold())
            content = _read(path)
            if content is None and not entry.get("allow_missing"):
                raise DeliveryError(f"提交的产物文件不存在: {path}")
            size += len(content) if content is not None else 0
            if size > MAX_DELIVERY_BYTES:
                raise DeliveryError("单次交付不能超过 128 MiB")
            files.append((uuid4().hex, delivery_id, kind, str(path), path.name,
                          sha256(content).hexdigest() if content is not None else None, content))
        db.execute("INSERT INTO delivery_versions (id, task_id, version, task_revision, summary, checks, requirements, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_review', ?, ?)",
                   (delivery_id, task_id, expected_version + 1, task['revision'], summary.strip(), encoded_checks,
                    json.dumps(task['state']['profile']['acceptance_criteria'], ensure_ascii=False), now, now))
        db.executemany("INSERT INTO delivery_files VALUES (?, ?, ?, ?, ?, ?, ?)", files)
    return detail(task_id)


def _version(db, task, row):
    value = dict(row)
    value['check_report'] = json.loads(value.pop('checks'))
    value['acceptance_criteria'] = json.loads(value.pop('requirements'))
    value['files'] = []
    for f in db.execute("SELECT id, kind, path, name, digest, content IS NOT NULL AS snapshot_available FROM delivery_files WHERE delivery_id = ? ORDER BY rowid", (row['id'],)):
        item = dict(f)
        item['source_status'] = _integrity(task, item)
        value['files'].append(item)
    value['reviews'] = [dict(r) for r in db.execute("SELECT * FROM delivery_reviews WHERE delivery_id = ? ORDER BY created_at", (row['id'],))]
    value['reopens'] = [dict(r) for r in db.execute("SELECT id, reason, created_at FROM delivery_reopens WHERE delivery_id = ? ORDER BY created_at", (row['id'],))]
    return value


def _card(task, latest):
    state = task['state']
    plan = state['plan']
    current = next((s for s in plan if s['status'] == 'running'), None)
    current = current or next((s for s in plan if s['status'] in {'pending', 'failed'}), None)
    started = any(s['status'] != 'pending' for s in plan)
    if latest and latest['reopened_at'] is None:
        status = {'pending_review': 'pending_review', 'accepted': 'accepted', 'rejected': 'needs_revision', 'invalidated': 'needs_revision'}[latest['status']]
    elif (latest and latest['reopened_at']) or started or task['status'] == 'complete':
        status = 'in_progress'
    else:
        status = 'pending'
    profile = state['profile']
    return {'task_id': task['task_id'], 'project_id': task['project_id'], 'project_root': task['project_root'],
            'project_name': Path(task['project_root']).name, 'task_type': profile['task_type'],
            'goal': profile['root_goal'], 'role': current['role'] if current else (plan[-1]['role'] if plan else profile.get('responsible_role', '未分配')),
            'stage': current['goal'] if current else ('执行完成 · 尚未提交产物' if task['status'] == 'complete' and not latest else '执行完成' if task['status'] == 'complete' else '等待计划'),
            'done_steps': sum(s['status'] == 'done' for s in plan), 'total_steps': len(plan),
            'execution_status': task['status'], 'revision': task['revision'], 'delivery_status': status,
            'delivery_version': latest['version'] if latest else None,
            'summary': latest['summary'] if latest else '尚未提交产物',
            'updated_at': max(task['updated_at'], latest['updated_at'] if latest else 0)}


def detail(task_id):
    with _db(True) as db:
        task = _task(db, task_id)
        latest = _refresh(db, task, _latest(db, task_id))
        versions = [_version(db, task, r) for r in db.execute("SELECT * FROM delivery_versions WHERE task_id = ? ORDER BY version DESC", (task_id,)).fetchall()]
        return {'task': _card(task, latest), 'acceptance_criteria': task['state']['profile']['acceptance_criteria'],
                'execution_checks': task['state'].get('verification', []), 'versions': versions,
                'feedback': versions[0]['reviews'] if versions else [],
                'reopen_required': bool(latest and latest['reopened_at'] is None)}


def board(project_id=None, task_type=None):
    with _db(True) as db:
        tasks = [_row(r) for r in db.execute("SELECT * FROM tasks ORDER BY updated_at DESC").fetchall()]
        projects = {t['project_id']: {'id': t['project_id'], 'name': Path(t['project_root']).name, 'path': t['project_root']} for t in tasks}
        cards = []
        for task in tasks:
            if project_id and task['project_id'] != project_id:
                continue
            if task_type and task['state']['profile']['task_type'] != task_type:
                continue
            latest = _refresh(db, task, _latest(db, task['task_id']))
            cards.append(_card(task, latest))
        return {'tasks': sorted(cards, key=lambda t: t['updated_at'], reverse=True), 'projects': list(projects.values()),
                'task_types': sorted({t['state']['profile']['task_type'] for t in tasks})}


def review(task_id, version, decision, comment=''):
    _integer(version)
    if decision not in {'accepted', 'rejected'}:
        raise DeliveryError("无效的验收操作")
    if not isinstance(comment, str) or len(comment) > 10000 or (decision == 'rejected' and not comment.strip()):
        raise DeliveryError("退回修改必须填写意见（最多 10000 字）")
    conflict = None
    with _db(True) as db:
        task = _task(db, task_id)
        latest = _refresh(db, task, _latest(db, task_id))
        if not latest or latest['version'] != version or latest['reopened_at'] is not None:
            conflict = '交付版本已变化或任务已重开，请刷新后重试'
        elif latest['status'] not in {'pending_review', 'invalidated'}:
            conflict = '该版本已有验收结论，请刷新后重试'
        elif decision == 'accepted' and (latest['status'] != 'pending_review' or task['status'] != 'complete' or task['revision'] != latest['task_revision']):
            conflict = '文件已变化、缺失或执行状态已变化，必须提交新版后重新验收'
        else:
            now = time.time()
            db.execute("INSERT INTO delivery_reviews VALUES (?, ?, ?, ?, 'human', ?)",
                       (uuid4().hex, latest['id'], decision, comment.strip(), now))
            db.execute("UPDATE delivery_versions SET status = ?, updated_at = ? WHERE id = ?", (decision, now, latest['id']))
    if conflict:
        raise DeliveryConflict(conflict)
    return detail(task_id)


def reopen(task_id, expected_revision, version, reason):
    _integer(expected_revision)
    _integer(version)
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 10000:
        raise DeliveryError("重开任务需要明确的修改原因")
    with _db(True) as db:
        task = _task(db, task_id)
        latest = _latest(db, task_id)
        if task['revision'] != expected_revision or not latest or latest['version'] != version or latest['reopened_at'] is not None:
            raise DeliveryConflict("任务或交付已变化，请刷新后重试")
        if task['status'] != 'complete':
            raise DeliveryConflict("仅可重开已完成的执行任务")
        now = time.time()
        db.execute("INSERT INTO delivery_reopens VALUES (?, ?, ?, ?, ?)",
                   (uuid4().hex, latest['id'], reason.strip(), json.dumps(task['state'], ensure_ascii=False), now))
        state = task['state']
        state['plan_history'].append(state['plan'])
        state.update(plan=[], verification=[], context_refs=[], selected_skills={}, compact_pending=False)
        state.pop('final_result', None)
        db.execute("UPDATE tasks SET status = 'active', revision = revision + 1, state = ?, updated_at = ? WHERE id = ?",
                   (json.dumps(state, ensure_ascii=False), now, task_id))
        db.execute("UPDATE delivery_versions SET reopened_at = ?, updated_at = ? WHERE id = ?", (now, now, latest['id']))
    return detail(task_id)


def artifact(file_id):
    """Return immutable registered bytes, never accept an arbitrary path."""
    with _db() as db:
        row = db.execute("SELECT name, content FROM delivery_files WHERE id = ?", (file_id,)).fetchone()
        if row is None or row['content'] is None:
            raise DeliveryError("文件不存在：该交付没有可用的文件快照")
        return row['name'], bytes(row['content'])
