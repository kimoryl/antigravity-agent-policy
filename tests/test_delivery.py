from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import dashboard
import delivery_state as delivery
from runtime_engine import set_plan, update_step, verify
from runtime_state import create_task, get_task, RuntimeError
from runtime_tools import call, TOOLS
try:
    from skill_resolver import SkillService
except ImportError:
    SkillService = lambda: None


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'CODEX_AGENT_POLICY_DATA': self.temp.name})
        self.env.start()
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        self.file = self.root / '交付.txt'
        self.file.write_text('version one', encoding='utf-8')
        self.report = self.root / 'checks.json'
        self.report.write_text('{"passed":true}', encoding='utf-8')
        self.skills = SkillService()
        self.profile = {'root_goal': '交付测试', 'local_goal': '交付测试', 'task_type': 'coding', 'constraints': [], 'expected_outputs': ['交付.txt'], 'acceptance_criteria': ['文件可读'], 'required_capabilities': [], 'complexity_hint': 'moderate'}
        self.task_id = create_task(self.profile, str(self.root))['task_id']

    def tearDown(self):
        if dashboard._server:
            dashboard._server.shutdown()
            dashboard._server.server_close()
            dashboard._server = None
            dashboard._url = None
        self.env.stop()
        self.temp.cleanup()

    def complete(self):
        revision = get_task(self.task_id)['revision']
        set_plan(self.task_id, revision, [{'id': 'build', 'goal': '实现与检查', 'role': 'coder', 'depends_on': [], 'write_paths': [], 'expected_outputs': ['交付.txt']}])
        update_step(self.task_id, revision + 1, 'build', 'running')
        update_step(self.task_id, revision + 2, 'build', 'done')
        verify(self.task_id, [{'kind': 'file_exists', 'path': '交付.txt', 'criterion': '文件可读'}])
        return call('runtime_task', {'action': 'complete', 'task_id': self.task_id, 'expected_revision': revision + 4, 'final_result': '已完成'}, self.skills)

    def submit(self, version=0, path=None):
        return call('runtime_delivery', {'action': 'submit', 'task_id': self.task_id,
                    'expected_revision': get_task(self.task_id)['revision'], 'expected_version': version,
                    'summary': '已提交产物', 'artifacts': [{'path': path or str(self.file)}],
                    'check_report': {'summary': '文件可读检查通过', 'path': str(self.report)}}, self.skills)

    def reopen(self, version=1):
        return call('runtime_delivery', {'action': 'reopen', 'task_id': self.task_id,
                    'expected_revision': get_task(self.task_id)['revision'], 'version': version,
                    'reason': '根据人工意见补充内容'}, self.skills)

    def test_full_cycle_and_preserved_versions(self):
        self.complete()
        first = self.submit()
        self.assertEqual(first['task']['delivery_status'], 'pending_review')
        task_before = get_task(self.task_id)
        rejected = delivery.review(self.task_id, 1, 'rejected', '补充使用说明')
        self.assertEqual(rejected['task']['delivery_status'], 'needs_revision')
        self.assertEqual(get_task(self.task_id), task_before)
        read = call('runtime_task', {'action': 'get', 'task_id': self.task_id}, self.skills)
        self.assertEqual(read['delivery']['feedback'][0]['comment'], '补充使用说明')
        with self.assertRaises(delivery.DeliveryConflict):
            self.submit(1)
        reopened = self.reopen()
        self.assertEqual(reopened['task']['delivery_status'], 'in_progress')
        self.assertEqual(get_task(self.task_id)['state']['verification'], [])
        self.file.write_text('version two: usage instructions', encoding='utf-8')
        self.complete()
        second = self.submit(1)
        self.assertEqual(second['task']['delivery_status'], 'pending_review')
        with self.assertRaises(delivery.DeliveryConflict):
            delivery.review(self.task_id, 1, 'accepted')
        accepted = delivery.review(self.task_id, 2, 'accepted')
        self.assertEqual(accepted['task']['delivery_status'], 'accepted')
        self.assertEqual([v['version'] for v in accepted['versions']], [2, 1])
        old = accepted['versions'][1]
        self.assertEqual(old['status'], 'rejected')
        self.assertEqual(old['reviews'][0]['comment'], '补充使用说明')
        self.assertEqual(delivery.artifact(old['files'][0]['id'])[1], b'version one')

    def test_modified_accepted_version_requires_new_acceptance(self):
        self.complete(); self.submit(); delivery.review(self.task_id, 1, 'accepted')
        self.file.write_text('modified', encoding='utf-8')
        changed = delivery.detail(self.task_id)
        self.assertEqual(changed['task']['delivery_status'], 'needs_revision')
        self.assertEqual(changed['versions'][0]['status'], 'invalidated')
        self.assertEqual(changed['versions'][0]['reviews'][0]['decision'], 'accepted')
        # Restoring bytes cannot silently reinstate an invalidated acceptance.
        self.file.write_text('version one', encoding='utf-8')
        with self.assertRaises(delivery.DeliveryConflict):
            delivery.review(self.task_id, 1, 'accepted')
        self.reopen(); self.complete(); self.submit(1)
        self.assertEqual(delivery.detail(self.task_id)['task']['delivery_status'], 'pending_review')

    def test_review_rechecks_files_without_prior_poll(self):
        self.complete(); self.submit()
        self.file.write_text('changed between viewing and accepting', encoding='utf-8')
        with self.assertRaises(delivery.DeliveryConflict):
            delivery.review(self.task_id, 1, 'accepted')
        self.assertEqual(delivery.detail(self.task_id)['versions'][0]['status'], 'invalidated')

    def test_report_change_also_invalidates_acceptance(self):
        self.complete(); self.submit(); delivery.review(self.task_id, 1, 'accepted')
        self.report.write_text('changed report', encoding='utf-8')
        self.assertEqual(delivery.board()['tasks'][0]['delivery_status'], 'needs_revision')

    def test_missing_files_are_explicit_and_snapshots_survive_deletion(self):
        self.complete()
        first = self.submit()
        file_id = first['versions'][0]['files'][0]['id']
        self.file.unlink()
        self.assertEqual(delivery.detail(self.task_id)['versions'][0]['files'][0]['source_status'], 'missing')
        self.assertEqual(delivery.artifact(file_id)[1], b'version one')
        with self.assertRaises(delivery.DeliveryConflict):
            delivery.review(self.task_id, 1, 'accepted')

    def test_missing_at_submission_has_no_download(self):
        self.complete()
        with self.assertRaises(delivery.DeliveryError):
            self.submit(path=str(self.root / 'missing.pdf'))
        result = call(
            'runtime_delivery',
            {
                'action': 'submit',
                'task_id': self.task_id,
                'expected_revision': get_task(self.task_id)['revision'],
                'expected_version': 0,
                'summary': '已提交产物',
                'artifacts': [{'path': str(self.root / 'missing.pdf'), 'allow_missing': True}],
                'check_report': {'summary': '文件可读检查通过', 'path': str(self.report)},
            },
            self.skills,
        )
        item = result['versions'][0]['files'][0]
        self.assertEqual(item['source_status'], 'missing')
        self.assertFalse(item['snapshot_available'])
        with self.assertRaises(delivery.DeliveryError):
            delivery.artifact(item['id'])

    def test_validation_and_private_path_boundaries(self):
        with self.assertRaises(delivery.DeliveryConflict): self.submit()
        self.complete()
        for path in ('../outside.txt', '.env', 'credentials.json', 'private.key'):
            with self.subTest(path=path), self.assertRaises(delivery.DeliveryError): self.submit(path=path)
        self.submit()
        with self.assertRaises(delivery.DeliveryError): delivery.review(self.task_id, 1, 'rejected', '  ')
        with self.assertRaises(delivery.DeliveryConflict): delivery.reopen(self.task_id, 999, 1, '修改')
        with self.assertRaises(delivery.DeliveryError): delivery.review(self.task_id, True, 'accepted')
        with self.assertRaises(RuntimeError): set_plan(self.task_id, get_task(self.task_id)['revision'], [{'id': 'bypass', 'goal': 'edit', 'role': 'coder'}])
        with self.assertRaises(RuntimeError): call('runtime_task', {'action': 'status', 'task_id': self.task_id, 'expected_revision': get_task(self.task_id)['revision'], 'status': 'active'}, self.skills)
        with self.assertRaises(RuntimeError): call('runtime_delivery', {'action': 'accept', 'task_id': self.task_id}, self.skills)

    def test_simultaneous_submissions_and_reviews(self):
        self.complete()
        def submit():
            try: self.submit(); return True
            except delivery.DeliveryConflict: return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(lambda _: submit(), range(2))), [False, True])
        def review():
            try: delivery.review(self.task_id, 1, 'accepted'); return True
            except delivery.DeliveryConflict: return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(lambda _: review(), range(2))), [False, True])
        self.assertEqual(len(delivery.detail(self.task_id)['versions'][0]['reviews']), 1)

    def test_board_filters_and_no_twenty_task_cutoff(self):
        for i in range(24): create_task(dict(self.profile, task_type='writing', root_goal=f'任务{i}'), str(self.root))
        other = self.root / 'other'; other.mkdir()
        other_task = create_task(self.profile, str(other))
        all_tasks = delivery.board()
        self.assertEqual(len(all_tasks['tasks']), 26)
        self.assertEqual(len(delivery.board(task_type='writing')['tasks']), 24)
        self.assertEqual([t['task_id'] for t in delivery.board(project_id=other_task['project_id'])['tasks']], [other_task['task_id']])

    def test_restart_preserves_reviews_and_snapshots(self):
        self.complete(); self.submit(); delivery.review(self.task_id, 1, 'rejected', '调整格式')
        # Every call uses a newly opened SQLite connection.
        result = call('runtime_delivery', {'action': 'get', 'task_id': self.task_id}, SkillService())
        self.assertEqual(result['feedback'][0]['comment'], '调整格式')
        self.assertEqual(delivery.artifact(result['versions'][0]['files'][0]['id'])[1], b'version one')

    def test_http_endpoints_and_version_conflict(self):
        self.complete(); self.submit()
        url = dashboard.start_dashboard(self.skills)['url']
        def get(path):
            with urlopen(url + path) as response: return response.read(), response.headers
        self.assertIn('交付面板', get('')[0].decode())
        self.assertIn('delivery-card', get('delivery.js')[0].decode())
        data = json.loads(get('api/delivery?task_id=' + self.task_id)[0])
        file_id = data['versions'][0]['files'][0]['id']
        content, headers = get('api/artifact?file_id=' + file_id)
        self.assertEqual(content, b'version one')
        self.assertIn('sandbox', headers['Content-Security-Policy'])
        self.assertIn('attachment', get('api/artifact?file_id=' + file_id + '&download=1')[1]['Content-Disposition'])
        for endpoint in ('api/artifact?file_id=unknown', 'api/artifact?path=../outside', '../api/deliveries'):
            with self.assertRaises(HTTPError) as error: get(endpoint)
            self.assertEqual(error.exception.code, 404)
        def review(version, decision='accepted', comment='', origin=None):
            headers = {'Content-Type': 'application/json'}
            if origin: headers['Origin'] = origin
            body = json.dumps({'task_id': self.task_id, 'version': version, 'decision': decision, 'comment': comment}).encode()
            with urlopen(Request(url + 'api/delivery/review', data=body, headers=headers)) as response: return json.load(response)
        with self.assertRaises(HTTPError) as error: review(1, origin='https://untrusted.example')
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(HTTPError) as error: review(999)
        self.assertEqual(error.exception.code, 409)
        self.assertEqual(review(1, 'rejected', '补充说明')['task']['delivery_status'], 'needs_revision')
        self.reopen(); self.file.write_text('v2', encoding='utf-8'); self.complete(); self.submit(1)
        self.assertEqual(review(2)['task']['delivery_status'], 'accepted')
        self.assertIn('runtime_delivery', {t['name'] for t in TOOLS})


if __name__ == '__main__':
    unittest.main()
