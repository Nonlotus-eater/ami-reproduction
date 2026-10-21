"""Exercise the real supervisor with real processes and synthetic queue work."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

from scripts import run_five_seed_experiments as supervisor


def denied(*args, **kwargs):
    raise RuntimeError('Network forbidden in scheduler fixture')


socket.socket.connect = socket.socket.connect_ex = socket.create_connection = denied
settings = Path(sys.argv[1]).resolve()
actual_popen = subprocess.Popen
actual_time = time.time
actual_sleep = time.sleep
offset = 0


def accelerated_sleep(seconds):
    global offset
    offset += seconds
    actual_sleep(0.02)


def queue_process(command, **kwargs):
    config = Path(command[command.index('--methods-config') + 1])
    method, = json.loads(config.read_text())
    expected = ('memory_benchmark.runtime.native_bridge.runner'
                if method in {'invmem', 'refind'} else 'memory_benchmark.runtime.queue')
    assert command[2:4] == ['-m', expected], command
    return actual_popen([sys.executable, str(Path(__file__).with_name('scheduler_queue.py')),
                        str(settings), json.dumps(command)], **kwargs)


supervisor.subprocess = SimpleNamespace(Popen=queue_process, STDOUT=subprocess.STDOUT,
                                       DEVNULL=subprocess.DEVNULL)
supervisor.time = SimpleNamespace(time=lambda: actual_time() + offset,
                                time_ns=time.time_ns, sleep=accelerated_sleep)
sys.argv = [str(supervisor.__file__), *sys.argv[2:]]
raise SystemExit(supervisor.main())
