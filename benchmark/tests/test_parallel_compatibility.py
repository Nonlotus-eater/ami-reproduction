from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections import deque

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from parallel_runner.common import rows, validate_config, read, load_config, pool_size
from parallel_runner.supervisor import take_next_job


class LocalCompatibility(unittest.TestCase):
    def test_parallel_entry_does_not_preload_an_installed_framework(self):
        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder) / 'memory_benchmark'
            package.mkdir()
            (package / '__init__.py').write_text(
                "raise RuntimeError('wrong editable checkout was imported')\n")
            result = subprocess.run(
                [sys.executable, str(ROOT / 'scripts/run_parallel.py'),
                 'prepare', '--help'],
                env={**os.environ, 'PYTHONPATH': folder}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--foreground', result.stdout)

    def test_defaults_are_unlimited_and_user_limits_have_no_fixed_ceiling(self):
        config = read(ROOT / 'configs/parallel/four-methods.json')
        validate_config(config)
        for key in ['memory_workers','reader_history_workers','query_workers',
                    'total_history_workers','per_method_history_workers','budget_cny']:
            self.assertIsNone(config[key])
        for spec in config['providers'].values():
            self.assertEqual(spec, {'inflight':None,'rpm':None,'tpm':None})
        validate_config({**config, 'total_history_workers':100000})
        self.assertEqual(pool_size(None, 1144), 1144)
        self.assertEqual(pool_size(70, 1144), 70)
        self.assertEqual(pool_size(None, 0), 1)

    def test_venv_executable_symlink_is_not_dereferenced(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            executable = folder / 'venv/bin/python'
            executable.parent.mkdir(parents=True)
            executable.symlink_to(sys.executable)
            config = read(ROOT / 'configs/parallel/four-methods.json')
            config['methods'][0]['config']['python'] = 'venv/bin/python'
            path = folder / 'settings.json'
            path.write_text(json.dumps(config))
            effective = load_config(path)['methods'][0]['config']['python']
            self.assertEqual(effective, str(executable))
            self.assertNotEqual(effective, str(executable.resolve()))

    def test_method_cap_counts_both_pools_and_skips_full_method(self):
        memory = {i: {'method': 'AMI'} for i in range(3)}
        reader = {i: {'method': 'AMI'} for i in range(2)}
        blocked = {'method': 'AMI', 'id': 'blocked'}
        eligible = {'method': 'Mem0', 'id': 'eligible'}
        queue = deque([blocked, eligible])
        config = {'per_method_history_workers': 5}
        self.assertEqual(take_next_job(queue, memory, reader, config), eligible)
        self.assertEqual(list(queue), [blocked])
        self.assertIsNone(take_next_job(queue, memory, reader, config))
        reader.pop(0)
        self.assertEqual(take_next_job(queue, memory, reader, config), blocked)

    def test_unicode_line_separator_remains_inside_json_string(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'targets.jsonl'
            values = [{'gold': 'first\u2028second\u2029third'}, {'gold': 'next'}]
            path.write_text(''.join(json.dumps(v, ensure_ascii=False) + '\n' for v in values))
            self.assertEqual(rows(path), values)

    def test_invalid_total_history_concurrency_rejected(self):
        config = read(ROOT / 'configs/parallel/four-methods.json')
        for value in [0, -1, True, 2.5]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_config({**config, 'total_history_workers': value})

    def test_new_inputs_all_parse_and_have_exact_counts(self):
        source = ROOT / 'samples/proportional-200-v1/prepared'
        for seed in range(17, 22):
            prepared = source / f'seed-{seed}'
            cases = rows(prepared / 'cases.jsonl')
            questions = [q for case in cases for q in rows(prepared / case['queries'])]
            targets = [q for case in cases for q in rows(prepared / case['targets'])]
            self.assertEqual(len(questions), 200)
            self.assertEqual(len(targets), 200)
            self.assertEqual({q['qa_id'] for q in questions}, {q['qa_id'] for q in targets})
