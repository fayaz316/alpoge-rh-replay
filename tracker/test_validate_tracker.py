"""Regression tests for validate_tracker.py.

The cases that matter: a reviewed receipt whose task list was replaced
with copies of one unrelated (but valid) module id, with its object
digests emptied, used to pass validation; and a reviewed receipt with
one of its tasks (and the matching object digest) removed, leaving the
rest unchanged, also used to pass; and a reviewed receipt with its entire
task list deleted was skipped by every receipt check.  The validator must
reject all of these, while still accepting the repository as it stands.
"""
import gzip
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VALIDATOR = ROOT / 'tracker' / 'validate_tracker.py'
RECEIPT = Path('evidence/volunteers/P0012.json')


def run_validator(root):
    return subprocess.run([sys.executable, str(VALIDATOR), str(root)],
                          capture_output=True, text=True)


class ValidateTrackerTests(unittest.TestCase):
    def test_repository_as_it_stands_passes(self):
        result = run_validator(ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_corrupted_reviewed_receipt_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / 'repo'
            shutil.copytree(ROOT, copy,
                            ignore=shutil.ignore_patterns('.git', '__pycache__'))
            with gzip.open(copy / 'tracker/modules.jsonl.gz', 'rt') as f:
                rows = [json.loads(line) for line in f if line.strip()]
            unrelated = next(r['id'] for r in rows if r['packet'] != 'P0012')
            receipt_path = copy / RECEIPT
            receipt = json.loads(receipt_path.read_text())
            receipt['tasks'] = [unrelated] * 64
            receipt['objects'] = {}
            receipt_path.write_text(json.dumps(receipt))
            result = run_validator(copy)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            for check_name in ('evidence.reviewed_tasks_unique',
                               'evidence.reviewed_tasks_match_claim',
                               'evidence.reviewed_objects_match_tasks'):
                self.assertIn(f'FAIL {check_name}', result.stdout)

    def test_reviewed_receipt_missing_one_task_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / 'repo'
            shutil.copytree(ROOT, copy,
                            ignore=shutil.ignore_patterns('.git', '__pycache__'))
            receipt_path = copy / RECEIPT
            receipt = json.loads(receipt_path.read_text())
            removed = receipt['tasks'].pop()
            del receipt['objects'][removed]
            self.assertEqual(len(receipt['tasks']), 63)
            receipt_path.write_text(json.dumps(receipt))
            result = run_validator(copy)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('FAIL evidence.reviewed_tasks_complete_claim', result.stdout)

    def test_reviewed_receipt_without_tasks_field_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / 'repo'
            shutil.copytree(ROOT, copy,
                            ignore=shutil.ignore_patterns('.git', '__pycache__'))
            receipt_path = copy / RECEIPT
            receipt = json.loads(receipt_path.read_text())
            del receipt['tasks']
            receipt_path.write_text(json.dumps(receipt))
            result = run_validator(copy)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('FAIL evidence.volunteers_tasks_present', result.stdout)

    def test_unknown_packet_with_empty_tasks_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / 'repo'
            shutil.copytree(ROOT, copy,
                            ignore=shutil.ignore_patterns('.git', '__pycache__'))
            (copy / 'evidence/volunteers/FAKE.json').write_text(
                json.dumps({'packet': 'FAKE_PACKET', 'tasks': [], 'objects': {}})
            )
            result = run_validator(copy)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('FAIL evidence.reviewed_tasks_complete_claim', result.stdout)
            self.assertIn('FAIL evidence.volunteers_tasks_present', result.stdout)

    def test_bool_exit_code_and_non_sha256_object_digest_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / 'repo'
            shutil.copytree(ROOT, copy,
                            ignore=shutil.ignore_patterns('.git', '__pycache__'))
            receipt_path = copy / RECEIPT
            receipt = json.loads(receipt_path.read_text())
            receipt['exit_code'] = False
            first_task = receipt['tasks'][0]
            receipt['objects'][first_task] = 'not-a-sha256'
            receipt_path.write_text(json.dumps(receipt))
            result = run_validator(copy)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('FAIL evidence.receipt_exit_code', result.stdout)
            self.assertIn('FAIL evidence.reviewed_objects_match_tasks', result.stdout)

    def test_nested_volunteer_receipt_without_tasks_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / 'repo'
            shutil.copytree(ROOT, copy,
                            ignore=shutil.ignore_patterns('.git', '__pycache__'))
            nested = copy / 'evidence/volunteers/sub/bad.json'
            nested.parent.mkdir(parents=True, exist_ok=True)
            nested.write_text(json.dumps({'notes': 'missing tasks'}))
            result = run_validator(copy)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('FAIL evidence.volunteers_tasks_present', result.stdout)

    def test_unhashable_task_entry_reports_named_failure(self):
        for bad_entry in (['M000001'], {'id': 'M000001'}):
            with self.subTest(bad_entry=bad_entry), tempfile.TemporaryDirectory() as tmp:
                copy = Path(tmp) / 'repo'
                shutil.copytree(ROOT, copy,
                                ignore=shutil.ignore_patterns('.git', '__pycache__'))
                receipt_path = copy / RECEIPT
                receipt = json.loads(receipt_path.read_text())
                receipt['tasks'] = [bad_entry]
                receipt_path.write_text(json.dumps(receipt))
                result = run_validator(copy)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stderr, '')
                self.assertIn('FAIL evidence.volunteers_tasks_present', result.stdout)
                self.assertIn('FAIL evidence.receipt_tasks_exist', result.stdout)
                self.assertIn('FAILED:', result.stdout)

if __name__ == '__main__':
    unittest.main()
