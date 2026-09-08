import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


repair = module('repair', 'actions/biome-repair/repair.py')
guard = module('guard', 'actions/dispatch-guard/guard.py')
HEAD = 'a' * 40
NEW = 'b' * 40
REPO = 'example/app'
PR = {'state': 'open', 'draft': False, 'user': {'login': 'renovate[bot]', 'id': 29139614, 'type': 'Bot'},
      'head': {'sha': HEAD, 'ref': 'renovate/biome', 'repo': {'full_name': REPO}},
      'base': {'repo': {'full_name': REPO}}}
PAYLOAD = {'head': HEAD, 'pr': 7, 'repository': REPO, 'files': [
    {'path': 'biome.json', 'content': base64.b64encode(b'{"formatter": {"enabled": true}}\n').decode()}]}


class FakeGitHub:
    dispatch = repair.GitHub.dispatch

    def __init__(self, race=False):
        self.calls = []
        self.race = race

    def call(self, path, data=None, method=None):
        self.calls.append((path, data, method))
        if path == '/pulls/7':
            return copy.deepcopy(PR)
        if path == f'/git/commits/{HEAD}':
            return {'tree': {'sha': 'tree'}}
        if path == '/git/trees/tree?recursive=1':
            return {'tree': [{'path': 'biome.json', 'type': 'blob', 'mode': '100644'}]}
        if path == '/git/blobs':
            return {'sha': 'blob'}
        if path == '/git/trees':
            return {'sha': 'new-tree'}
        if path == '/git/commits':
            assert data['parents'] == [HEAD]
            return {'sha': NEW}
        if path.startswith('/git/refs/'):
            assert data == {'sha': NEW, 'force': False}
            if self.race:
                raise RuntimeError('non-fast-forward')
            return {}
        if path == '/actions/workflows/ci.yml/dispatches':
            assert data == {'ref': 'renovate/biome', 'inputs': {'pr-number': '7', 'expected-head-sha': NEW}}
            return None
        raise AssertionError(path)


class RepairTests(unittest.TestCase):
    def test_dispatch_retries_transient_failure(self):
        api = FakeGitHub()
        original = api.call
        count = 0
        def call(path, data=None, method=None):
            nonlocal count
            if path.endswith('/dispatches'):
                count += 1
                if count < 3:
                    raise repair.urllib.error.URLError('temporary outage')
            return original(path, data, method)
        api.call = call
        with patch.object(repair.time, 'sleep'):
            self.assertEqual(repair.publish(PAYLOAD, ['biome.json'], ['src'], api, REPO, 'ci.yml', 7, HEAD), NEW)
        self.assertEqual(count, 3)

    def test_real_biome_migrates_and_formats_idempotently(self):
        version = json.loads((Path(__file__).parents[1] / 'package.json').read_text())['devDependencies']['@biomejs/biome']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'src').mkdir()
            (root / 'frontend/src').mkdir(parents=True)
            (root / 'package.json').write_text(json.dumps({'devDependencies': {'@biomejs/biome': version}}))
            for file, config in [('biome.json', {}), ('frontend/biome.json', {'root': False})]:
                (root / file).write_text(json.dumps({'$schema': 'https://biomejs.dev/schemas/2.4.0/schema.json', **config}))
            for file in ['src/example.ts', 'frontend/src/example.ts']:
                (root / file).write_text('export const value={name:"example"}\n')
            def run(*args):
                return subprocess.check_output(args, cwd=root, stderr=subprocess.STDOUT).decode().strip()
            run('bun', 'install', '--ignore-scripts')
            run('git', 'init', '-q')
            run('git', 'add', 'package.json', 'bun.lock', 'biome.json', 'frontend', 'src')
            run('git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'test: initialize fixture')
            head = run('git', 'rev-parse', 'HEAD')
            class API:
                def call(self, path):
                    if path == '/pulls/7':
                        return {**PR, 'head': {**PR['head'], 'sha': head}}
                    return [{'filename': 'package.json'}]
            previous = Path.cwd()
            try:
                os.chdir(root)
                with patch.dict(os.environ, {'GITHUB_REPOSITORY': REPO, 'GITHUB_OUTPUT': str(root / 'outputs')}):
                    repair.compute(['biome.json', 'frontend/biome.json'], ['src', 'frontend/src'], '.', API(), 7, head, root / 'repair.json')
                payload = json.loads((root / 'repair.json').read_text())
                self.assertEqual({f['path'] for f in payload['files']}, {'biome.json', 'frontend/biome.json', 'src/example.ts', 'frontend/src/example.ts'})
                for file in ['biome.json', 'frontend/biome.json']:
                    self.assertIn(f'/schemas/{version}/schema.json', (root / file).read_text())
            finally:
                os.chdir(previous)

    def test_only_current_same_repository_renovate_pr_is_eligible(self):
        self.assertTrue(repair.eligible(PR, REPO, HEAD))
        for change in [{'state': 'closed'}, {'draft': True}, {'user': {'login': 'someone'}},
                       {'user': {**PR['user'], 'id': 1}},
                       {'user': {**PR['user'], 'type': 'User'}},
                       {'user': {'login': 'renovate[bot]'}},
                       {'head': {'sha': HEAD, 'repo': {'full_name': 'someone/fork'}}}]:
            self.assertFalse(repair.eligible({**PR, **change}, REPO, HEAD))
        self.assertFalse(repair.eligible(PR, REPO, NEW))

    def test_protected_and_escaping_paths_are_rejected(self):
        for path in ['../biome.json', '/biome.json', '.github/workflows/ci.yml', 'src/../package.json',
                     'src/package.json', 'renovate.json', 'src/file.sh', '.git/config', 'src/.hidden.ts']:
            with self.subTest(path=path):
                self.assertFalse(repair.allowed(path, ['biome.json'], ['src']))
        self.assertTrue(repair.allowed('frontend/biome.json', ['frontend/biome.json'], []))
        self.assertTrue(repair.allowed('src/lib/file.ts', ['biome.json'], ['src']))

    def test_payload_rejects_duplicate_empty_and_binary_output(self):
        for files in [[], PAYLOAD['files'] * 2, [{'path': 'biome.json', 'content': 'AA=='}]]:
            with self.assertRaises(ValueError):
                repair.validate_payload({**PAYLOAD, 'files': files}, ['biome.json'], ['src'])

    def test_publish_dispatches_the_new_commit(self):
        api = FakeGitHub()
        self.assertEqual(repair.publish(PAYLOAD, ['biome.json'], ['src'], api, REPO, 'ci.yml', 7, HEAD), NEW)
        self.assertEqual(api.calls[-1][0], '/actions/workflows/ci.yml/dispatches')

    def test_concurrent_push_cannot_be_overwritten(self):
        api = FakeGitHub(race=True)
        with self.assertRaises(RuntimeError):
            repair.publish(PAYLOAD, ['biome.json'], ['src'], api, REPO, 'ci.yml', 7, HEAD)
        self.assertFalse(any('/dispatches' in path for path, _, _ in api.calls))

    def test_wrong_artifact_is_rejected_before_writes(self):
        api = FakeGitHub()
        with self.assertRaises(ValueError):
            repair.publish({**PAYLOAD, 'head': NEW}, ['biome.json'], ['src'], api, REPO, 'ci.yml', 7, HEAD)
        self.assertEqual(api.calls, [])

    def test_dispatch_must_match_run_sha_branch_and_current_pr(self):
        self.assertTrue(guard.validate(PR, REPO, HEAD, 'renovate/biome', HEAD))
        self.assertFalse(guard.validate(PR, REPO, NEW, 'renovate/biome', HEAD))
        self.assertFalse(guard.validate(PR, REPO, HEAD, 'main', HEAD))
        self.assertFalse(guard.validate({**PR, 'state': 'closed'}, REPO, HEAD, 'renovate/biome', HEAD))
