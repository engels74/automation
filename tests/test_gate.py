import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('gate', Path(__file__).parents[1] / 'actions/gate/gate.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class GateTests(unittest.TestCase):
    def test_all_required_jobs_must_succeed(self):
        self.assertEqual(gate.failures({'quality': {'result': 'success'}, 'tests': {'result': 'success'}}, ['quality', 'tests']), [])

    def test_every_non_success_result_blocks(self):
        for result in ['failure', 'cancelled', 'skipped', 'pending', 'neutral', 'timed_out', '', None]:
            with self.subTest(result=result):
                self.assertTrue(gate.failures({'tests': {'result': result}}, ['tests']))

    def test_missing_required_job_blocks(self):
        self.assertTrue(gate.failures({'quality': {'result': 'success'}}, ['quality', 'tests']))

    def test_unaccounted_job_blocks(self):
        self.assertTrue(gate.failures({'quality': {'result': 'success'}, 'tests': {'result': 'failure'}}, ['quality']))

    def test_empty_duplicate_and_malformed_input_blocks(self):
        for needs, required in [({}, []), ({}, ['a', 'a']), ([], ['a']), ({'a': {}}, ['a']), ({'a': None}, ['a'])]:
            with self.subTest(needs=needs, required=required):
                self.assertTrue(gate.failures(needs, required))
