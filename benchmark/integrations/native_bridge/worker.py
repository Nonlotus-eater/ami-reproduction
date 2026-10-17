"""Method-owned memory worker using unchanged benchmark History/Query/Evidence."""
from importlib import metadata
from pathlib import Path
import sys
import time
import traceback

from common import adapter_class, pace_method_embeddings, read, write
from memory_benchmark.pipeline.context import normalize
from memory_benchmark.runtime.artifacts import append_row, file_hash, rows
from memory_benchmark.runtime.model_client import Gateway
from memory_benchmark.schemas import History, Query


def main(path):
    job = read(path)
    assert not any('target' in k or 'gold' in k for k in job)
    output = Path(job['output'])
    output.mkdir(parents=True, exist_ok=True)
    (output / 'attempt.lock').open('x').close()
    report = {'status': 'running', 'method': job['method'], 'scope': job['scope'],
        'python': sys.executable, 'only_memory_and_retrieval': True,
        'packages': {n: metadata.version(n) for n in ['openai', 'httpx', 'tiktoken', 'fastapi', 'pydantic']}}
    write(output / 'report.json', report)
    gateway = adapter = None
    start = time.monotonic()
    try:
        for field in ('history', 'queries'):
            assert file_hash(job[field]) == job['input_hashes'][field]
        history = History.from_wire(read(job['history']))
        queries = [Query(**row) for row in rows(job['queries'])]
        gateway = Gateway(job['runtime'], output / 'usage.json', expected_identity=job['provider_identity'])
        pace_method_embeddings(gateway, job.get('method_embedding_pacing'))
        adapter = adapter_class(job['method_config']['adapter_file'], job['method'])(
            job['method_config'], history, output, job['scope'], gateway)
        for session in history.sessions:
            gateway.budget.phase = 'memory_add'
            adapter.add(session)
            gateway.budget.check()
            append_row(output / 'events.jsonl', {'stage': 'add', 'session_id': session.session_id,
                'messages': len(session.messages), 'timestamp': session.timestamp})
        gateway.budget.phase = 'memory_finalize'
        adapter.finalize()
        append_row(output / 'events.jsonl', {'stage': 'finalize'})
        for query in queries:
            assert query.benchmark == history.benchmark and query.episode_id == history.episode_id
            gateway.budget.phase = 'memory_search'
            evidence = normalize(adapter.search(query, job['runtime']['top_k']), job['runtime']['top_k'])
            gateway.budget.check()
            append_row(output / 'retrievals.jsonl', {'id': query.qa_id, 'evidence': [e.to_wire() for e in evidence]})
        report.update(status='complete', questions=len(queries), sessions=len(history.sessions))
    except Exception as error:
        report.update(status='failed', error_type=type(error).__name__,
            stack=[{'file': f.filename, 'line': f.lineno, 'function': f.name} for f in traceback.extract_tb(error.__traceback__)])
    finally:
        if adapter:
            adapter.close()
        if gateway:
            gateway.close()
            if gateway.budget.failed:
                report.update(status='failed', provider_failure=gateway.budget.failed)
        report['seconds'] = time.monotonic() - start
        write(output / 'report.json', report)
    return 0 if report['status'] == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1]))
