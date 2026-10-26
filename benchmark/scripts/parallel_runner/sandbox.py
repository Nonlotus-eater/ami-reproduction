"""Mount only one history's inputs/state; targets and other histories stay hidden."""
from pathlib import Path
import os
import shutil
from .common import PACKAGE, read, write, sha


def worker_command(job, jobpath, executable, run, config):
    from memory_benchmark.runtime.worker_launch import sandbox_command, python_prefixes
    from memory_benchmark.runtime.profile import read_profile
    run = Path(run)
    output = Path(job['output'])
    output.mkdir(parents=True, exist_ok=True)
    inputs = output / 'worker_inputs'
    inputs.mkdir()
    restricted = {**job, 'runtime': dict(job['runtime']),
                  'launch_config': {**job['launch_config'], 'tokenizer_cache': '/tmp/tokenizer-cache'}}
    for key, name in [('history', 'history.json'), ('queries', 'queries.jsonl')]:
        path = inputs / name
        shutil.copyfile(job[key], path)
        if sha(path) != job['input_hashes'][key]:
            raise ValueError('Worker input changed')
        restricted[key] = str(path)
    private = None
    if not job.get('offline'):
        profile = read_profile(job['runtime']['env_file'])
        private = inputs / 'provider.env'
        with open(private, 'x', opener=lambda p, flags: os.open(p, flags, 0o600)) as f:
            f.write('\n'.join(f'{k}={v}' for k, v in profile.items() if k.startswith(('AMI_', 'OPENAI_'))) + '\n')
        restricted['runtime']['env_file'] = '/run/aml-provider.env'
    path = output / 'worker.json'
    write(path, restricted)
    readonly = [(run / 'framework/src', run / 'framework/src'), (PACKAGE, PACKAGE)]
    readonly += [(p, p) for p in python_prefixes(executable) if not p.is_relative_to('/usr')]
    if private:
        readonly += [(private, Path('/run/aml-provider.env'))]
    if not job.get('offline'):
        for key in ['source_root', 'adapter_file', 'fastembed_cache']:
            if key in job['method_config']:
                p = Path(job['method_config'][key])
                if p.exists():
                    readonly.append((p, p))
    cache = Path(config.get('tokenizer_cache') or os.environ.get('TIKTOKEN_CACHE_DIR', '/tmp/data-gym-cache'))
    if cache.is_dir():
        readonly.append((cache, Path('/tmp/tokenizer-cache')))
    env = {'PATH': f'{Path(executable).parent}:/usr/bin:/bin', 'HOME': '/tmp/home',
           'PYTHONPATH': os.pathsep.join([str(PACKAGE), str(run / 'framework/src')]),
           'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1', 'OMP_NUM_THREADS': '1',
           'OPENBLAS_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1', 'TOKENIZERS_PARALLELISM': 'false',
           'TIKTOKEN_CACHE_DIR': '/tmp/tokenizer-cache', 'HF_HOME': '/tmp/hf', 'XDG_CACHE_HOME': '/tmp/cache'}
    # A per-history directory contains checkpoint/cache/job files, never scoring targets.
    writable = [output.parent, Path(config['ledger_dir'])]
    command = [executable, '-B', '-m', 'parallel_runner.native_worker', str(path)]
    try:
        args = sandbox_command(command, readonly=readonly, writable=writable, cwd=output, env=env)
    except BaseException:
        if private:
            private.unlink(missing_ok=True)
        raise
    write(output / 'isolation.json', {'policy': 'bwrap_parallel_history_v1',
          'readonly': [str(b) for _, b in readonly], 'writable': list(map(str, writable)),
          'targets_mounted': False, 'host_root_mounted': False})
    return args, {'PATH': '/usr/bin:/bin'}, private
