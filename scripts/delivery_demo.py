"""Create isolated demo tasks, or revise the demo after a human UI rejection.

This script never accepts a delivery. Review buttons must be used in the UI.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from delivery_state import detail, reopen, submit
from runtime_engine import set_plan, update_step, verify
from runtime_state import create_task, get_task
from runtime_tools import call
from skill_resolver import SkillService


def finish(task_id, artifact):
    task = get_task(task_id)
    revision = task['revision']
    set_plan(task_id, revision, [{'id': 'produce', 'goal': '准备交付内容并检查文件', 'role': 'coder', 'depends_on': [], 'expected_outputs': [artifact.name], 'write_paths': [artifact.name]}])
    update_step(task_id, revision + 1, 'produce', 'running')
    update_step(task_id, revision + 2, 'produce', 'done')
    verify(task_id, [{'kind': 'file_exists', 'path': artifact.name, 'criterion': '产物可打开，交付内容包含说明'}])
    return call('runtime_task', {'action': 'complete', 'task_id': task_id, 'expected_revision': revision + 4, 'final_result': '演示产物已生成并检查'}, SkillService())


def register(task_id, version, artifact):
    return submit(task_id, get_task(task_id)['revision'], version, f'演示交付 v{version + 1}：说明文件已生成，等待人工验收。',
                  [{'path': str(artifact)}], {'summary': '文件存在并可读取；此任务用于隔离环境中的流程演示。', 'checks': [{'name': '文件可读', 'passed': artifact.is_file()}]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['seed', 'revise'])
    parser.add_argument('--data-dir', required=True)
    args = parser.parse_args()
    base = Path(args.data_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    os.environ['CODEX_AGENT_POLICY_DATA'] = str(base)
    manifest = base / 'demo.json'
    if args.action == 'seed':
        if manifest.exists():
            print(manifest.read_text(encoding='utf-8')); return
        project = base / '交付演示'; project.mkdir(exist_ok=True)
        profile = {'root_goal': '流程演示：提交 → 退回 → 新版 → 验收', 'local_goal': '隔离演示任务', 'task_type': 'coding', 'constraints': ['仅演示数据'], 'expected_outputs': ['交付说明.md'], 'acceptance_criteria': ['产物可打开，交付内容包含说明'], 'required_capabilities': [], 'complexity_hint': 'moderate'}
        task_id = create_task(profile, str(project))['task_id']
        artifact = project / '交付说明.md'
        artifact.write_text('# 交付演示 v1\n\n初版内容。请在面板退回并提出补充使用说明。\n', encoding='utf-8')
        finish(task_id, artifact); register(task_id, 0, artifact)
        create_task(dict(profile, root_goal='演示：待整理验收清单', task_type='writing'), str(project))
        other = base / '文档演示'; other.mkdir(exist_ok=True)
        running = create_task(dict(profile, root_goal='演示：编写项目使用指南', task_type='writing'), str(other))
        set_plan(running['task_id'], 1, [{'id': 'draft', 'goal': '编写使用指南', 'role': 'writing_drafter', 'depends_on': [], 'expected_outputs': ['guide.md'], 'write_paths': ['guide.md']}])
        update_step(running['task_id'], 2, 'draft', 'running')
        missing = create_task(dict(profile, root_goal='演示：缺失文件会明确标记'), str(other))
        existing = other / 'check.md'; existing.write_text('用于检查演示执行', encoding='utf-8')
        finish(missing['task_id'], existing)
        register(missing['task_id'], 0, other / 'missing.pdf')
        result = {'task_id': task_id, 'artifact': str(artifact), 'data_dir': str(base)}
        manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    else:
        result = json.loads(manifest.read_text(encoding='utf-8'))
        task_id = result['task_id']
        latest = detail(task_id)['versions'][0]
        if latest['status'] != 'rejected':
            raise ValueError('先在面板填写意见并点击退回修改')
        feedback = latest['reviews'][-1]['comment']
        reopen(task_id, get_task(task_id)['revision'], latest['version'], '根据退回意见修改：' + feedback)
        artifact = Path(result['artifact'])
        artifact.write_text('# 交付演示 v2\n\n## 使用说明\n\n1. 打开任务卡片。\n2. 查看版本产物和检查报告。\n3. 填写意见退回或验收通过。\n\n已处理意见：' + feedback + '\n', encoding='utf-8')
        finish(task_id, artifact); register(task_id, latest['version'], artifact)
        result['version'] = latest['version'] + 1
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
