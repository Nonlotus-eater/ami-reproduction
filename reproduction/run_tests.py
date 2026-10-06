"""Run upstream standalone tests in separate, network-denied processes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
GUARD = '''import socket

def blocked(*args, **kwargs):
    raise RuntimeError("Offline tests forbid outbound network")
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.create_connection = blocked
'''


def main():
    output = ROOT / 'artifacts' / 'unit-tests'
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix='ami-offline-') as temp:
        Path(temp, 'sitecustomize.py').write_text(GUARD)
        env = {k: v for k, v in os.environ.items() if not k.startswith(('AMI_', 'OPENAI_', 'DASHSCOPE_'))}
        env.update(PYTHONPATH=os.pathsep.join([temp, str(ROOT)]), HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                   AMI_DB_PATH=str(Path(temp) / 'unused.sqlite3'))
        for test in sorted((ROOT / 'tests').glob('test_*.py')):
            result = subprocess.run([sys.executable, str(test)], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=120)
            (output / (test.stem + '.txt')).write_text(result.stdout + result.stderr)
            results.append({'file': test.name, 'exit_code': result.returncode})
            print(('PASS' if result.returncode == 0 else 'FAIL'), test.name, flush=True)
    (output / 'summary.json').write_text(json.dumps(results, indent=2) + '\n')
    return int(any(row['exit_code'] for row in results))


if __name__ == '__main__':
    sys.exit(main())
