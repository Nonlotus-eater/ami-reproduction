"""Local-only request/crash workload for the real bubblewrap regression."""
import json
import os
from pathlib import Path
import sys
import threading
import time

import httpx
from parallel_runner.shared_control import SharedControl
from parallel_runner.budget import CallBudget, make_transport

mode, folder, role, endpoint = sys.argv[1:]
folder = Path(folder)
control = SharedControl(folder / 'ledger.sqlite3')


def write(name, value):
    target = folder / (role + '-' + name + '.json')
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(value))
    temporary.replace(target)


if mode == 'request':
    budget = CallBudget({'AMI_TEST_INPUT_CNY_PER_MILLION':'1',
        'AMI_TEST_OUTPUT_CNY_PER_MILLION':'1','AMI_TEST_EMBED_CNY_PER_MILLION':'1'})
    budget.phase = role
    transport = make_transport(budget, 'llm', endpoint, 'gpt-4o-mini', disable_thinking_required=False)
    with httpx.Client(transport=transport, trust_env=False) as client:
        response = client.post(endpoint, json={'model':'gpt-4o-mini',
            'messages':[{'role':'user','content':role}],'max_tokens':8})
        write('result', {'status':response.status_code, 'body':response.json(),
            'pid':os.getpid(),'namespace':os.readlink('/proc/self/ns/pid'), 'failed':budget.failed})
else:
    def crash():
        while not (folder / (role + '-crash')).exists():
            time.sleep(.01)
        os._exit(7)  # Deliberately bypass context-manager cleanup.
    threading.Thread(target=crash, daemon=True).start()
    ident = control.reserve('llm', role, role, 100, 10)
    write('ready', {'id':ident, 'pid':os.getpid(), 'namespace':os.readlink('/proc/self/ns/pid')})
    with control.admit(ident, 'llm', 110, timeout=15):
        write('active', {'id':ident})
        while True:
            time.sleep(.05)
