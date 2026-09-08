import base64
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('merge_helper', ROOT / 'actions/merge/merge.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
HEAD, BASE, MERGED = 'a' * 40, 'b' * 40, 'c' * 40
REPOSITORY = 'engels74/automation'
EARLY, NOW, LATER = '2026-09-01T10:00:00Z', '2026-09-01T10:01:00Z', '2026-09-01T10:02:00Z'
BOT = {'login': 'renovate[bot]', 'id': 29139614, 'type': 'Bot'}


class FakeAPI:
    def __init__(self):
        self.root = 'https://api.github.com/repos/' + REPOSITORY
        self.config = json.loads((ROOT / '.github/merge-policy.json').read_text())
        self.base = BASE
        self.repo = {'id': 10, 'full_name': REPOSITORY, 'default_branch': 'main', 'archived': False, 'allow_auto_merge': False}
        self.pr = {'state': 'open', 'draft': False, 'merged': False, 'mergeable': True, 'number': 1,
                   'base': {'ref': 'main', 'sha': BASE, 'repo': {'full_name': REPOSITORY}},
                   'head': {'ref': 'renovate/example', 'sha': HEAD, 'repo': {'full_name': REPOSITORY}},
                   'user': BOT, 'updated_at': NOW, 'requested_reviewers': [], 'requested_teams': [], 'labels': []}
        self.comment = {'id': 7, 'user': BOT, 'body': '/merge-when-green', 'created_at': NOW,
                        'updated_at': NOW, 'issue_url': self.root + '/issues/1'}
        self.renovate = {'extends': ['github>engels74/automation//automerge.json#v1.0.1']}
        self.runs = [{'id': 20, 'workflow_id': 30, 'head_sha': HEAD, 'head_branch': 'renovate/example',
                      'event': 'pull_request', 'pull_requests': [{'number': 1}], 'status': 'completed',
                      'conclusion': 'success', 'run_attempt': 1, 'check_suite_id': 40,
                      'created_at': EARLY, 'run_started_at': EARLY, 'updated_at': EARLY}]
        self.jobs, self.checks = [], []
        for index, name in enumerate(self.config['required_checks']):
            self.add_job(name, index + 50)
        self.reviews = []
        self.permission = 'admin'
        self.writes = []
        self.before_read = None
        self.snapshot_reads = 0
        self.fail_dispatch = False

    def add_job(self, name, check_id, conclusion='success'):
        self.jobs.append({'name': name, 'run_id': 20, 'run_attempt': 1, 'status': 'completed',
                          'conclusion': conclusion, 'check_run_url': self.root + f'/check-runs/{check_id}'})
        self.checks.append({'id': check_id, 'name': name, 'head_sha': HEAD, 'app': {'id': 15368},
                            'check_suite': {'id': 40}, 'status': 'completed', 'conclusion': conclusion})

    def request(self, path, method='GET', data=None):
        if method != 'GET':
            self.writes.append((path, method, data))
            if path == '/pulls/1/merge':
                self.base = MERGED
                return {'merged': True, 'sha': MERGED}
            if self.fail_dispatch and path.endswith('/dispatches'):
                raise helper.Blocked('dispatch failed')
            return None
        if path == '':
            self.snapshot_reads += 1
            if self.before_read:
                self.before_read(self)
            return deepcopy(self.repo)
        if path.startswith('/git/ref/heads/'):
            return {'object': {'sha': self.base}}
        if path.startswith('/contents/'):
            value = self.config if 'merge-policy.json' in path else self.renovate
            return {'encoding': 'base64', 'content': base64.b64encode(json.dumps(value).encode()).decode()}
        if path == '/pulls/1':
            return deepcopy(self.pr)
        if path.startswith('/compare/'):
            return {'ahead_by': 1, 'behind_by': 0, 'merge_base_commit': {'sha': self.base}}
        if path == '/actions/workflows/ci.yml':
            return {'id': 30, 'path': '.github/workflows/ci.yml', 'state': 'active'}
        if path == '/issues/comments/7':
            return deepcopy(self.comment)
        if path.endswith('/permission'):
            return {'permission': self.permission}
        raise AssertionError('unexpected test API read: ' + path)

    def pages(self, path, field=None):
        if path.endswith('/reviews'):
            return deepcopy(self.reviews)
        if path.startswith('/actions/workflows/'):
            return deepcopy(self.runs)
        if path.endswith('/jobs'):
            return deepcopy(self.jobs)
        if path.startswith('/check-suites/'):
            return deepcopy(self.checks)
        if path == '/issues/1/comments':
            return [deepcopy(self.comment)]
        raise AssertionError('unexpected pagination: ' + path)


class MergeTests(unittest.TestCase):
    def test_success_merges_expected_sha_and_dispatches_exact_default_commit(self):
        api = FakeAPI()
        helper.merge(api, REPOSITORY, 'main', 1, 7)
        self.assertEqual(api.writes, [('/pulls/1/merge', 'PUT', {'sha': HEAD, 'merge_method': 'squash'}),
            ('/actions/workflows/ci.yml/dispatches', 'POST', {'ref': 'main', 'inputs': {'expected-default-sha': MERGED}})])

    def reject(self, mutate):
        api = FakeAPI()
        mutate(api)
        with self.assertRaises(helper.Blocked):
            helper.merge(api, REPOSITORY, 'main', 1, 7)
        self.assertEqual(api.writes, [])

    def test_every_non_success_mandatory_conclusion_blocks(self):
        for conclusion in ['failure', 'cancelled', 'neutral', 'skipped', 'timed_out', None]:
            with self.subTest(conclusion=conclusion):
                self.reject(lambda a: a.jobs[0].update(conclusion=conclusion))

    def test_missing_pending_duplicate_unexpected_and_spoofed_checks_block(self):
        mutations = [lambda a: a.jobs.pop(), lambda a: a.jobs[0].update(status='queued'),
                     lambda a: a.jobs.append(deepcopy(a.jobs[0])), lambda a: a.add_job('undeclared', 99),
                     lambda a: a.checks[0].update(app={'id': 123}), lambda a: a.checks[0].update(head_sha=BASE),
                     lambda a: a.checks[0].update(check_suite={'id': 99}), lambda a: a.checks.clear(),
                     lambda a: a.jobs[0].update(run_attempt=2), lambda a: a.runs[0].update(workflow_id=99),
                     lambda a: a.runs[0].update(event='workflow_run'), lambda a: a.runs[0].update(pull_requests=[])]
        for i, mutate in enumerate(mutations):
            with self.subTest(case=i):
                self.reject(mutate)

    def test_newest_pending_or_failed_run_overrides_older_success(self):
        for status, conclusion in [('queued', None), ('completed', 'failure')]:
            self.reject(lambda a: a.runs.append({**a.runs[0], 'id': 21, 'created_at': LATER,
                'run_started_at': LATER, 'status': status, 'conclusion': conclusion}))

    def test_wrong_and_stale_requests_block(self):
        mutations = [lambda a: a.comment.update(user={**BOT, 'id': 1}),
                     lambda a: a.comment.update(updated_at=LATER), lambda a: a.comment.update(created_at=EARLY, updated_at=EARLY),
                     lambda a: a.pr.update(updated_at=LATER), lambda a: a.runs[0].update(updated_at=LATER),
                     lambda a: a.comment.update(body='/merge-anything'), lambda a: a.comment.update(issue_url=a.root + '/issues/2'),
                     lambda a: a.pr.update(labels=[{'name': 'manual-dependencies'}]),
                     lambda a: a.pr.update(user={'login': 'person', 'id': 2, 'type': 'User'}),
                     lambda a: a.renovate.update(automerge=False), lambda a: a.renovate.update(extends=[]),
                     lambda a: a.renovate.update(ignoreTests=True)]
        for i, mutate in enumerate(mutations):
            with self.subTest(case=i):
                self.reject(mutate)

    def test_head_base_repo_policy_and_ci_races_never_merge(self):
        mutations = [lambda a: a.pr['head'].update(sha='d' * 40), lambda a: setattr(a, 'base', 'e' * 40),
                     lambda a: a.repo.update(default_branch='develop'), lambda a: a.config.update(enabled=False),
                     lambda a: a.runs[0].update(run_attempt=2)]
        for i, mutate in enumerate(mutations):
            with self.subTest(case=i):
                def install(a):
                    a.before_read = lambda client: mutate(client) if client.snapshot_reads == 2 else None
                self.reject(install)

    def test_draft_fork_unknown_mergeability_and_native_automerge_block(self):
        for mutate in [lambda a: a.pr.update(draft=True), lambda a: a.pr.update(mergeable=None),
                       lambda a: a.pr['head'].update(repo={'full_name': 'contributor/example'}),
                       lambda a: a.pr['base'].update(ref='develop'), lambda a: a.repo.update(allow_auto_merge=True)]:
            self.reject(mutate)

    def test_stale_approval_and_outstanding_review_requests_block(self):
        for state, commit in [('APPROVED', BASE), ('CHANGES_REQUESTED', HEAD), ('DISMISSED', HEAD), ('PENDING', HEAD)]:
            def mutate(a):
                a.config['minimum_approvals'] = 1
                a.reviews = [{'id': 1, 'state': state, 'commit_id': commit, 'user': {'id': 80}}]
            self.reject(mutate)
        self.reject(lambda a: a.pr.update(requested_reviewers=[{'id': 80}]))
        self.reject(lambda a: a.pr.update(requested_teams=[{'id': 80}]))

    def test_current_approval_satisfies_policy(self):
        api = FakeAPI()
        api.config['minimum_approvals'] = 1
        api.reviews = [{'id': 1, 'state': 'APPROVED', 'commit_id': HEAD, 'user': {'id': 80}}]
        helper.merge(api, REPOSITORY, 'main', 1, 7)
        self.assertEqual(api.writes[0][1], 'PUT')

    def test_manual_request_requires_authorization_and_both_commits(self):
        api = FakeAPI()
        api.comment.update(user={'id': 80, 'login': 'maintainer', 'type': 'User'}, body=f'/merge {HEAD} {BASE}')
        helper.merge(api, REPOSITORY, 'main', 1, 7)
        for permission, body in [('read', f'/merge {HEAD} {BASE}'), ('admin', f'/merge {HEAD}'), ('admin', f'/merge {HEAD} {MERGED}')]:
            def mutate(a):
                a.permission = permission
                a.comment.update(user={'id': 80, 'login': 'maintainer', 'type': 'User'}, body=body)
            self.reject(mutate)

    def test_only_declared_optional_reporting_can_skip(self):
        api = FakeAPI()
        api.config['optional_checks'] = ['report']
        api.add_job('report', 99, 'skipped')
        helper.merge(api, REPOSITORY, 'main', 1, 7)

    def test_stale_bot_handoff_is_removed_for_renovate_to_reissue(self):
        api = FakeAPI()
        api.pr['updated_at'] = LATER
        helper.invalidate(api, 'main', 1)
        self.assertEqual(api.writes, [('/issues/comments/7', 'DELETE', None)])

    def test_default_ci_dispatch_failure_is_reported_after_merge(self):
        api = FakeAPI()
        api.fail_dispatch = True
        with patch.object(helper.time, 'sleep'), self.assertRaises(helper.Blocked):
            helper.merge(api, REPOSITORY, 'main', 1, 7)
        self.assertEqual(sum(method == 'PUT' for _, method, _ in api.writes), 1)
        self.assertEqual(sum(path.endswith('/dispatches') for path, _, _ in api.writes), 3)

    def test_deployment_requires_latest_successful_default_branch_ci(self):
        api = FakeAPI()
        api.base = HEAD
        api.config['deploy_workflows'] = ['deploy.yml']
        api.runs[0].update(head_branch='main', event='workflow_dispatch')
        helper.complete(api, 'main', deepcopy(api.runs[0]))
        self.assertEqual(api.writes, [('/actions/workflows/deploy.yml/dispatches', 'POST',
            {'ref': 'main', 'inputs': {'expected-default-sha': HEAD}})])
        api.writes.clear()
        api.runs[0]['conclusion'] = 'failure'
        with self.assertRaises(helper.Blocked):
            helper.complete(api, 'main', deepcopy(api.runs[0]))
        self.assertEqual(api.writes, [])


if __name__ == '__main__':
    unittest.main()
