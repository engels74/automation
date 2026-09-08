import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('dispatch_guard', Path(__file__).parents[1] / 'actions/dispatch-guard/guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
SHA = 'a' * 40


class DispatchTests(unittest.TestCase):
    def env(self, **changes):
        return {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_SHA': SHA, 'GITHUB_REF_NAME': 'main',
                'GITHUB_REPOSITORY': 'engels74/automation', 'GH_TOKEN': 'test', 'EXPECTED_DEFAULT': SHA, **changes}

    def responses(self, head=SHA, branch='main'):
        return [io.BytesIO(json.dumps(value).encode()) for value in [
            {'default_branch': branch}, {'object': {'sha': head}}]]

    def test_current_default_dispatch_passes(self):
        with patch.dict(os.environ, self.env(), clear=True), patch.object(guard.urllib.request, 'urlopen', side_effect=self.responses()):
            guard.main()

    def test_stale_default_dispatch_and_wrong_branch_block(self):
        for head, branch in [('b' * 40, 'main'), (SHA, 'develop')]:
            with self.subTest(head=head, branch=branch), patch.dict(os.environ, self.env(), clear=True), patch.object(guard.urllib.request, 'urlopen', side_effect=self.responses(head, branch)), self.assertRaises(ValueError):
                guard.main()

    def test_mixed_malformed_and_wrong_event_inputs_fail_before_network(self):
        for values in [dict(PR_NUMBER='1'), dict(EXPECTED_HEAD=SHA), dict(EXPECTED_DEFAULT='main'), dict(GITHUB_EVENT_NAME='push')]:
            with self.subTest(values=values), patch.dict(os.environ, self.env(**values), clear=True), patch.object(guard.urllib.request, 'urlopen') as network, self.assertRaises(ValueError):
                guard.main()
            network.assert_not_called()

    def test_ordinary_ci_requires_no_network_or_dispatch_inputs(self):
        with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'push'}, clear=True), patch.object(guard.urllib.request, 'urlopen') as network:
            guard.main()
        network.assert_not_called()
