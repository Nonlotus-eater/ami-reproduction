"""Offline proofs of worker isolation, configuration binding and secret guards."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from memory_benchmark.benchmarks.native_contract import ROOT
from memory_benchmark.runtime.artifacts import file_hash, read_json, write_json, write_rows
from memory_benchmark.runtime.native_freeze import freeze_methods, native_identity, snapshot_bindings, verify_method
from memory_benchmark.runtime.worker_launch import run_memory_worker, sandbox_command
from memory_benchmark.schemas import History, Message, Query, Session
from scripts.check_credentials import inspect


class NativeConfigurationTests(unittest.TestCase):
    def test_templates_resources_and_effective_profile_are_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "native"
            source.mkdir()
            (source / "app.py").write_text("VALUE = 1\n")
            template = source / ".env.example"
            template.write_text("AMI_LLM_MAX_TOKENS_EXTRACT=1200\n")
            resource = source / "config.json"
            resource.write_text('{"retrieval": 1}')
            # Secrets and raw data are not part of the copied native source.
            (source / ".env").write_text("OPENAI_API_KEY=synthetic-secret-never-copy\n")
            (source / "datasets").mkdir()
            (source / "datasets/targets.json").write_text('{"gold": "synthetic"}')
            profile = root / "provider.env"
            profile.write_text("OPENAI_API_KEY=synthetic-key-first\nAMI_LLM_MAX_TOKENS_EXTRACT=1200\n")
            config = {"python": sys.executable, "source_root": str(source)}
            runtime = {"env_file": str(profile)}
            record = freeze_methods({"activememoryindex": config}, root / "run", runtime)["activememoryindex"]
            frozen = root / "run/frozen_native/activememoryindex/source_root"
            self.assertFalse((frozen / ".env").exists())
            self.assertFalse((frozen / "datasets").exists())
            verify_method(record, runtime)
            template.write_text("AMI_LLM_MAX_TOKENS_EXTRACT=4000\n")
            self.assertNotEqual(native_identity(config), record["native_code"])
            with self.assertRaisesRegex(ValueError, "configuration changed"):
                verify_method(record, runtime)
            self.assertEqual((frozen / ".env.example").read_text(), "AMI_LLM_MAX_TOKENS_EXTRACT=1200\n")
            template.write_text("AMI_LLM_MAX_TOKENS_EXTRACT=1200\n")
            resource.write_text('{"retrieval": 2}')
            with self.assertRaises(ValueError):
                verify_method(record, runtime)
            resource.write_text('{"retrieval": 1}')
            profile.write_text("OPENAI_API_KEY=synthetic-key-rotated\nAMI_LLM_MAX_TOKENS_EXTRACT=1200\n")
            verify_method(record, runtime)
            profile.write_text("OPENAI_API_KEY=synthetic-key-rotated\nAMI_LLM_MAX_TOKENS_EXTRACT=4000\n")
            with self.assertRaisesRegex(ValueError, "effective profile"):
                verify_method(record, runtime)
            (frozen / "config.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "Frozen native"):
                snapshot_bindings(record, root / "run", "activememoryindex")


class WorkerSandboxTests(unittest.TestCase):
    def test_private_provider_file_is_removed_even_when_sandbox_setup_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "attempt"
            output.mkdir()
            history, queries, profile = root / "history.json", root / "queries.jsonl", root / ".env"
            history.write_text("{}")
            queries.write_text("{}")
            profile.write_text("OPENAI_API_KEY=synthetic-credential\n")
            job = {"output": str(output), "history": str(history), "queries": str(queries),
                   "runtime": {"env_file": str(profile)}, "method_config": {}, "method": "probe",
                   "offline": False, "input_hashes": {"history": file_hash(history), "queries": file_hash(queries)}}
            with patch("memory_benchmark.runtime.worker_launch.verify_method"), \
                 patch("memory_benchmark.runtime.worker_launch.snapshot_bindings", side_effect=RuntimeError("setup failed")):
                with self.assertRaisesRegex(RuntimeError, "setup failed"):
                    run_memory_worker(job, executable=sys.executable, framework=root / "framework",
                        queue_output=root, record={"native_code": {}}, timeout=1, log=None)
            self.assertFalse((output / "worker_inputs/provider.env").exists())

    def test_missing_sandbox_fails_closed(self):
        with patch("memory_benchmark.runtime.worker_launch.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "requires bubblewrap"):
                sandbox_command(["true"], readonly=[], writable=[], cwd="/tmp", env={})

    def test_actual_worker_cannot_read_gold_via_absolute_symlink_proc_or_subprocess(self):
        # Do NOT skip when namespaces are unavailable: the deployment must fail
        # its tests rather than silently claim that workers are isolated.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "native"
            source.mkdir()
            (source / "app.py").write_text("VALUE=1\n")
            (source / ".env.example").write_text("AMI_LLM_MAX_TOKENS_EXTRACT=1200\n")
            gold = root / "prepared/targets.eval.jsonl"
            write_rows(gold, [{"gold": "SYNTHETIC_GOLD_SENTINEL"}])
            original = root / "original_dataset.json"
            original.write_text("SYNTHETIC_GOLD_SENTINEL")
            profile = root / ".env"
            profile.write_text("HOST_SECRET=SYNTHETIC_SECRET_SENTINEL")
            other = root / "other-seed/answers.jsonl"
            write_rows(other, [{"answer": "SYNTHETIC_GOLD_SENTINEL"}])
            output = root / "queue/method/dataset/history/memory_attempt_001"
            output.mkdir(parents=True)
            framework = root / "queue/frozen_framework"
            shutil.copytree(ROOT / "src", framework / "src", ignore=shutil.ignore_patterns("__pycache__"))
            history = History("locomo_refined", "h", ("Ada", "Bob"),
                (Session("s", None, "", (Message("m", "user", "Ada", "Ada likes tea", None),)),))
            hp, qp = root / "prepared/history.json", root / "prepared/queries.jsonl"
            write_json(hp, history.to_wire())
            write_rows(qp, [Query("locomo_refined", "h", "q", "What does Ada like?").to_wire()])
            (output / "escape").symlink_to(gold)
            config = {"source_root": str(source), "python": sys.executable}
            # Build a real snapshot, then use offline gateway-free execution.
            envfile = root / "test.env"
            envfile.write_text("AMI_ALLOW_RETRIES=0\n")
            record = freeze_methods({"attack": config}, root / "queue", {"env_file": str(envfile)})["attack"]
            attack = root / "attack.py"
            attack.write_text('''import json,sys,subprocess,sqlite3
from pathlib import Path
from memory_benchmark.runtime import worker
from memory_benchmark.schemas import Evidence
job=json.loads(Path(sys.argv[1]).read_text())
class Attack:
 def __init__(self,*args,**kw): pass
 def add(self,*args): pass
 def finalize(self): pass
 def close(self): pass
 def search(self,*args):
  blocked=[]
  for value in job['forbidden_paths']:
   try:
    Path(value).read_bytes(); blocked.append(False)
   except OSError: blocked.append(True)
  child=subprocess.run(['/bin/cat', job['forbidden_paths'][0]],capture_output=True)
  blocked.append(child.returncode!=0)
  try:
   Path(job['native_file']).write_text('tampered'); blocked.append(False)
  except OSError: blocked.append(True)
  with sqlite3.connect(Path(job['output'])/'memory.sqlite3') as db:
   db.execute('CREATE TABLE memories(value TEXT)')
   db.execute("INSERT INTO memories VALUES ('tea')")
   count=db.execute('SELECT COUNT(*) FROM memories').fetchone()[0]
  return [Evidence('probe',json.dumps({'blocked':blocked,'writes':count,
    'template':Path(job['template']).read_text()}))]
worker.create=lambda *a,**k:Attack()
raise SystemExit(0 if worker.execute(job)['status']=='complete' else 1)
''')
            paths = [gold, original, profile, other, output / "escape",
                     Path(f"/proc/{os.getpid()}/root") / str(gold).lstrip("/"),
                     Path("/proc/1/root") / str(gold).lstrip("/")]
            job = {"method": "attack", "method_config": config, "runtime": {"top_k": 100},
                   "scope": "test", "offline": True, "history": str(hp), "queries": str(qp),
                   "output": str(output), "input_hashes": {"history": file_hash(hp), "queries": file_hash(qp)},
                   "forbidden_paths": list(map(str, paths)), "native_file": str(source / "app.py"),
                   "template": str(source / ".env.example")}
            with (output / "worker.log").open("w") as log:
                child = run_memory_worker(job, executable=sys.executable, framework=framework,
                    queue_output=root / "queue", record=record, timeout=20, log=log,
                    script=attack, extra_readonly=[(attack, attack)])
            self.assertEqual(child.returncode, 0, (output / "worker.log").read_text())
            result = json.loads(json.loads((output / "retrievals.jsonl").read_text())["evidence"][0]["text"])
            self.assertTrue(all(result["blocked"]), result)
            self.assertEqual(result["writes"], 1)
            self.assertIn("1200", result["template"])
            self.assertNotIn("SYNTHETIC_GOLD_SENTINEL", (output / "retrievals.jsonl").read_text())
            saved = read_json(output / "job.json")
            self.assertNotEqual(saved["history"], str(hp))
            self.assertFalse(read_json(output / "isolation.json")["host_root_mounted"])


class CredentialGuardTests(unittest.TestCase):
    def test_real_precommit_hook_blocks_a_private_env_without_echoing_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
            self.assertEqual(git("init", "-q").returncode, 0)
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            git("config", "core.hooksPath", str(ROOT / ".githooks"))
            secret = "synthetic-hook-credential-12345"
            (root / ".env").write_text("OPENAI_API_KEY=" + secret + "\n")
            git("add", ".env")
            result = git("commit", "-qm", "must be blocked")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("private_env_file", result.stdout + result.stderr)
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_index_scan_rejects_historical_values_and_private_env_but_allows_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL)
            git("init", "-q")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            secret = "synthetic-historical-credential-12345"
            (root / ".env").write_text("OPENAI_API_KEY=" + secret + "\n")
            git("add", ".env")
            self.assertEqual(inspect(root)["staged_findings"][0]["reason"], "private_env_file")
            git("commit", "-qm", "synthetic legacy env")
            git("rm", "--cached", ".env")
            self.assertEqual(inspect(root)["status"], "passed")
            self.assertEqual(inspect(root, history=True)["historical_credential_env_blobs"], 1)
            (root / "code.py").write_text('TOKEN="' + secret + '"\n')
            git("add", "code.py")
            # Modify worktree to prove the scanner inspects the STAGED blob.
            (root / "code.py").write_text("# safe worktree\n")
            result = inspect(root)
            self.assertEqual(result["staged_findings"], [{"path": "code.py", "reason": "credential_value"}])
            self.assertNotIn(secret, json.dumps(result))


if __name__ == "__main__":
    unittest.main()
