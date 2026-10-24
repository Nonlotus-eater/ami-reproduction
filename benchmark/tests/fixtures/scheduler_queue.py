"""A deterministic queue substitute: no memory algorithm or model evaluation."""
from collections import Counter
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import socket
import sys
import time


def denied(*args, **kwargs):
    raise RuntimeError('Network forbidden in synthetic queue')


socket.socket.connect = socket.socket.connect_ex = socket.create_connection = denied
settings_path = Path(sys.argv[1])
settings = json.loads(settings_path.read_text())
command = json.loads(sys.argv[2])
output = Path(command[command.index('--output') + 1])
config = Path(command[command.index('--methods-config') + 1])
method, = json.loads(config.read_text())
key = method + ':' + output.name
state_path = settings_path.parent / 'processes.json'


@contextmanager
def registry():
    with state_path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(state_path.read_text()) if state_path.exists() else {
            'active': {}, 'peak': 0, 'method_peaks': {}, 'launches': []}
        yield state
        state_path.write_text(json.dumps(state))


output.mkdir(parents=True, exist_ok=True)
with registry() as state:
    attempt = sum(r['key'] == key for r in state['launches']) + 1
    state['launches'].append({'key': key, 'attempt': attempt, 'pid': os.getpid(),
                             'resume': '--resume' in command, 'command': command})
    state['active'][str(os.getpid())] = method
    state['peak'] = max(state['peak'], len(state['active']))
    for name, count in Counter(state['active'].values()).items():
        state['method_peaks'][name] = max(state['method_peaks'].get(name, 0), count)
    if len(state['active']) >= settings.get('barrier', 1):
        settings_path.with_suffix('.barrier').touch()

try:
    deadline = time.monotonic() + 15
    while not settings_path.with_suffix('.barrier').exists() or (
            settings.get('hold') and not settings_path.with_suffix('.release').exists()):
        if time.monotonic() > deadline:
            raise RuntimeError('Synthetic queue barrier timed out')
        time.sleep(0.01)
    time.sleep(0.08)
    (output / 'manifest.json').write_text('{"synthetic_queue": true}\n')
    answer = output / 'answers.fixture.json'
    if not answer.exists():
        answer.write_text(json.dumps({'answer': 'saved once', 'writer_pid': os.getpid()}))
    failure = settings.get('failure', {})
    failed = key == failure.get('key') and (failure.get('always') or attempt == 1)
    summary = {'complete': not failed, 'expected': 2, 'scored': 0 if failed else 2,
               'local_overall_macro_benchmark': None if failed else 50.0,
               'capabilities': {c: {'score': 50.0} for c in 'ABCDEGH'}}
    # Match the real queue's atomic publication: the supervisor may read this
    # file while the fixture is still running.
    summary_path = output / (method + '.summary.json')
    temporary = summary_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(summary))
    temporary.replace(summary_path)
finally:
    with registry() as state:
        state['active'].pop(str(os.getpid()), None)

raise SystemExit(1 if failed else 0)
