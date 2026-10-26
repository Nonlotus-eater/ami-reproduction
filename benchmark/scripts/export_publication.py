"""Export the current proportional benchmark; legacy releases remain in Git history."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import shutil
from zipfile import ZipFile
try:
    from .check_credentials import known_credentials
except ImportError:
    from check_credentials import known_credentials

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_SOURCE = {'lightmem.py', 'lightmem_api.py', 'lightmem_memma.py', 'pilot.py'}
LEGACY_COMMIT = '6ab98adeacd50dc934772a3c64f630b3709f9a24'
LEGACY_FILES = {
    'scripts/run_five_seed_experiments.py', 'scripts/sample_small_benchmark.py',
    'scripts/enable_memory_checkpoints.py', 'scripts/resume_with_memory_checkpoints.py',
    'scripts/enable_request_retries.py', 'scripts/disable_model_proxy.py',
    'scripts/release_smoke.py', 'scripts/merge_experiment_repeats.py',
    'configs/small_benchmark_v1.json', 'tests/test_sampling.py',
    'tests/test_repeat_merge.py', 'tests/test_checkpoint_rollout.py', 'tests/test_retry_rollout.py',
    'tests/fixtures/scheduler_bootstrap.py', 'tests/fixtures/scheduler_queue.py',
}
CURRENT_DOCS = {
    'docs/deployment.md', 'docs/legacy_versions.md', 'docs/evaluation_contract.md',
    'docs/capability_label_mapping.md', 'docs/publication_scope.md',
    'docs/benchmark_guide.md', 'docs/proportional_parallel_release.md',
}


def trim_legacy_tests(output):
    """Keep shared-core tests while removing tests of unpublished legacy CLIs."""
    rules = {
        'test_reusability.py': ({'SchedulerProcessTests'}, {'test_sampling_replays_and_freezing_rejects_modified_source_or_selected_list'}, set()),
        'test_queue.py': ({'FiveSeedExperimentTests'}, set(), {'scripts.run_five_seed_experiments'}),
        'test_direct_network.py': (set(), {'test_resume_environment_removes_all_proxy_variants', 'test_handoff_requires_every_persisted_output_to_match_its_checkpoint'}, {'scripts.disable_model_proxy'}),
    }
    for name, (classes, functions, imports) in rules.items():
        path = output / 'tests' / name
        tree = ast.parse(path.read_text())
        tree.body = [n for n in tree.body if not (
            isinstance(n, ast.ClassDef) and n.name in classes or
            isinstance(n, ast.ImportFrom) and n.module in imports)]
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                node.body = [n for n in node.body if not isinstance(n, ast.FunctionDef) or n.name not in functions]
        if name == 'test_direct_network.py' and not any(isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'PROXY_KEYS' for t in n.targets) for n in tree.body):
            tree.body.insert(0, ast.parse("PROXY_KEYS = ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy')").body[0])
        path.write_text(ast.unparse(tree) + '\n')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export(output, profile=None):
    profile = profile or 'benchmark'
    if profile not in {'core', 'benchmark', 'full'}:
        raise ValueError('Unknown publication profile')
    output = Path(output).resolve()
    if output == ROOT or ROOT.is_relative_to(output):
        raise ValueError('Choose a new output directory, not the workspace or its ancestor')
    files = {Path(p) for p in ('.env.example', '.dockerignore', '.githooks/pre-commit', 'scripts/check_credentials.py',
        'docs/aml_security_fixes_20261023.md', 'pyproject.toml', 'README.md',
        'configs/README.md', 'configs/runtime.json', 'configs/suites/smoke.json',
        'configs/datasets/public_sources.json', 'configs/datasets/text_suite.json', 'configs/small_benchmark_v1.json', 'datasets/README.md',
        'docs/model_handoff_99.md', 'docs/evaluation_contract.md',
        'docs/capability_label_mapping.md', 'docs/publication_scope.md',
        'docs/memory_checkpoint_recovery.md', 'docs/request_retry_deployment.md', 'docs/reader_aml_update.md',
        'docs/configuration_validation_20261017.md',
        'docs/benchmark_guide.md', 'docs/sample.md', 'docs/sync_20261021.md', 'samples/README.md',
        'methods/my_method/README.md', 'methods/my_method/ATTRIBUTION.md',
        'methods/my_method/Dockerfile', 'methods/my_method/compose.yaml',
        'patches/ami-system-role.patch', 'patches/invmem-native-interface.patch',
        'patches/refind-native-interface.patch', 'scripts/configure_release.py',
        'scripts/prepare_five_seeds.py', 'scripts/sample_small_benchmark.py',
        'scripts/prepare_sampled_seeds.py', 'scripts/run_five_seed_experiments.py',
        'scripts/enable_memory_checkpoints.py', 'scripts/resume_with_memory_checkpoints.py',
        'scripts/enable_request_retries.py', 'scripts/disable_model_proxy.py',
        'scripts/release_smoke.py',
        'scripts/download_public_datasets.py', 'scripts/export_publication.py')}
    for relative in ['scripts/run_parallel.py', 'scripts/sample_proportional.py', 'docs/proportional_parallel_release.md']:
        files.add(Path(relative))
    files.update(Path(p) for p in CURRENT_DOCS)
    for folder in ['scripts/parallel_runner', 'configs/parallel', 'configs/proportional-200-v1', 'samples/proportional-200-v1', 'samples/tokenizer-cache']:
        for path in (ROOT / folder).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix not in {'.pyc', '.log'}:
                files.add(path.relative_to(ROOT))
    for folder in ('src', 'sources', 'examples', 'samples', 'methods/my_method', 'configs/reproduction', 'tests', 'patches/tests'):
        for path in (ROOT / folder).rglob('*'):
            if path.is_file() and path.suffix in {'.py', '.json', '.jsonl'} and '__pycache__' not in path.parts:
                if path.name not in EXCLUDED_SOURCE:
                    files.add(path.relative_to(ROOT))
    if profile in {'core', 'benchmark'}:
        client_files = {'__init__.py', 'backend.py', 'client.py', 'contracts.py'}
        files = {path for path in files if not str(path).startswith(('examples/sdk/', 'methods/my_method/'))
            and str(path) not in {'.dockerignore', 'tests/test_sdk.py'}
            and not (str(path).startswith('src/aml_sdk/') and (profile == 'core' or path.name not in client_files))
            and not (profile == 'core' and str(path) == 'src/memory_benchmark/methods/aml_http.py')}
        for relative in ('docs/references/AMI_Benchmark_99_reference.docx', 'docs/references/README.md'):
            if (ROOT / relative).is_file():
                files.add(Path(relative))
    if profile in {'core', 'benchmark'}:
        files = {path for path in files if not (
            path.name.startswith(('test_derived', 'test_probe', 'test_invmem_derived'))
            or any('derived' in part for part in path.parts))}
        files = {path for path in files if str(path) not in LEGACY_FILES
            and not str(path).startswith(('configs/reproduction/', 'samples/seed-17/'))
            and (not str(path).startswith('samples/') or str(path) == 'samples/README.md'
                 or str(path).startswith(('samples/proportional-200-v1/', 'samples/tokenizer-cache/')))
            and (not str(path).startswith('docs/') or str(path) in CURRENT_DOCS)}
    # Do not overwrite an existing candidate or the target fork.
    output.mkdir(parents=True, exist_ok=False)
    for relative in sorted(files):
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
        if relative == Path('.githooks/pre-commit'):
            target.chmod(0o755)
    methods = json.loads((ROOT / 'configs/methods.json').read_text())
    methods = {name: methods[name] for name in ('mem0', 'activememoryindex')}
    for name, config in methods.items():
        folder = 'mem0' if name == 'mem0' else 'ami-reproduction'
        config['source_root'] = f'baselines/{folder}'
        config['python'] = f'baselines/{folder}/.venv/bin/python'
        if name == 'mem0':
            config['fastembed_cache'] = 'models/fastembed'
    (output / 'configs/methods.json').write_text(json.dumps(methods, indent=2) + '\n')
    lock = (ROOT / 'sources/publication-baselines.lock.json').read_text()
    (output / 'sources/baselines.lock.json').write_text(lock)
    # Remove registrations for optional adapters excluded from this distribution.
    registry = output / 'src/memory_benchmark/methods/__init__.py'
    text = registry.read_text()
    node = next(n for n in ast.parse(text).body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'MODULES' for t in n.targets))
    lines = text.splitlines(keepends=True)
    modules = {name: name for name in ('mem0', 'activememoryindex', 'invmem', 'refind')}
    if profile in {'benchmark', 'full'}:
        modules['aml_http'] = 'aml_http'
    lines[node.lineno - 1:node.end_lineno] = ['MODULES = ' + repr(modules) + '\n']
    registry.write_text(''.join(lines))
    test = output / 'tests/test_reader.py'
    tree = ast.parse(test.read_text())
    tree.body = [n for n in tree.body if not (isinstance(n, ast.ImportFrom)
                and n.module == 'memory_benchmark.methods.lightmem')]
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            node.body = [n for n in node.body if not (isinstance(n, ast.FunctionDef) and
                (n.name.startswith('test_lightmem_') or
                 n.name == 'test_v2_regression_keeps_summary_reader_when_default_templates_follow_aml'))]
    test.write_text(ast.unparse(tree) + '\n')
    if profile in {'core', 'benchmark'}:
        readme = output / 'README.md'
        text = readme.read_text().split('## SDK 首版：', 1)[0].split('## AML HTTP 记忆接口', 1)[0]
        text = '\n'.join(line for line in text.splitlines()
                         if not line.startswith(('src/aml_sdk/', 'examples/sdk/', 'methods/my_method/')))
        readme.write_text(text.rstrip() + '\n')
        for name in ('README.md', 'docs/deployment.md'):
            path = output / name
            path.write_text(path.read_text().replace('[api,data,sdk]', '[api,data]').replace('[api,sdk]', '[api]'))
        guide = output / 'docs/benchmark_guide.md'
        guide.write_text('\n'.join(line for line in guide.read_text().splitlines()
                                   if 'src/aml_sdk/' not in line and 'sdk/' not in line) + '\n')
        scope = output / 'docs/publication_scope.md'
        text = '\n'.join(line for line in scope.read_text().splitlines()
                         if not line.startswith('| `src/aml_sdk/`'))
        text = text.replace('包含并发接管、续跑及 SDK 检查', '包含并发接管、续跑及四方法复用检查')
        text = text.replace('python3 scripts/export_publication.py --output',
                            f'python3 scripts/export_publication.py --profile {profile} --output')
        text = text.replace('本次保留 AMI、Mem0、InvMem、ReFind；',
                            ('本次采用 core 范围，只发布四方法评测相关功能；SDK 和自定义方法研究入口暂不发布。' if profile == 'core' else
                             '本次采用 benchmark 范围，包含四方法评测及 AML HTTP 客户端；服务端和自定义方法研究入口单独提供。') +
                            '保留 AMI、Mem0、InvMem、ReFind；')
        text = text.replace('| 参考 Word | 本地留档 |', '| 参考 Word | 保留此前已发布的参考资料 |')
        scope.write_text(text + '\n')
        project = output / 'pyproject.toml'
        text = re.sub(r'^sdk = .*\n', '', project.read_text(), flags=re.MULTILINE)
        text = re.sub(r'^aml-sdk = .*\n', '', text, flags=re.MULTILINE)
        if profile == 'core':
            text = re.sub(r'^http_memory = .*\n', '', text, flags=re.MULTILINE)
        project.write_text(text)
        if profile == 'benchmark':
            readme = output / 'README.md'
            readme.write_text(readme.read_text().rstrip() + '\n\n## AML HTTP 记忆接口\n\n'
                '安装 `.[api,data,http_memory]` 后，可使用 `aml_sdk.client.MemoryClient` 或 '
                '`aml_http` adapter 连接兼容 Add/Search 的记忆服务。客户端提供有界重试及响应校验；'
                '实验请求及恢复规则见 [运行说明](docs/proportional_parallel_release.md)。\n')
        trim_legacy_tests(output)
    (output / '.gitignore').write_text(
        '.env\n.env.*\n!.env.example\n.venv/\n**/.venv/\n**/.benchmark-env/\n'
        '__pycache__/\n*.pyc\n*.egg-info/\nbaselines/\nmodels/\nruns/\n'
        'datasets/*\n!datasets/README.md\nconfigs/local/\nartifacts/\nbuild/\ndist/\n')
    (output / 'patches/README.md').write_text(
        '# Native compatibility patches\n\n'
        'Pinned commits, SHA-256 and adapter sources: [baseline lock](../sources/baselines.lock.json).\n\n'
        '* AMI: accept system messages without changing their roles.\n'
        '* InvMem: preserve unknown recording dates.\n'
        '* ReFind: accept system messages.\n\n'
        'Apply each patch only to its pinned upstream checkout. Keep upstream attribution and licenses.\n'
        'InvMem/ReFind adapters are in `src/memory_benchmark/methods/`; the frozen-worker bridge is in `src/memory_benchmark/runtime/native_bridge/`. Native implementations are fetched separately.\n')
    # Compare current and historical values without ever printing secrets.
    secrets, _ = known_credentials(ROOT)
    exported = sorted(p for p in output.rglob('*') if p.is_file())
    for path in exported:
        contents = [path.read_bytes()]
        if path.suffix == '.docx':
            with ZipFile(path) as archive:
                contents.extend(archive.read(name) for name in archive.namelist() if not name.endswith('/'))
        if any(value in content for content in contents for value in secrets):
            raise ValueError(f'Credential match in candidate file: {path.relative_to(output)}')
        if path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError('Unexpected large file in publication bundle')
    report = {'target_subdirectory': 'benchmark/', 'git_history_included': False,
              'methods': ['activememoryindex', 'mem0', 'invmem', 'refind'],
              'publication_profile': profile,
              'sdk': {'entrypoint': 'aml-sdk', 'extra': 'sdk', 'storage': 'method_owned',
                      'reference_backend': 'examples/sdk/invmem_backend.py',
                      'research_backend': 'methods/my_method/backend.py',
                      'research_starting_point': 'InvMem / vanilla-rag-memory 0.6.0', 'track': 'textual'} if profile == 'full' else
                     {'mode': 'http_client', 'extra': 'http_memory', 'client': 'aml_sdk.client.MemoryClient',
                      'adapter': 'aml_http', 'storage': 'method_owned'} if profile == 'benchmark' else None,
              'public_source_sampling': {'entrypoint': 'scripts/sample_proportional.py',
                                         'config': 'configs/proportional-200-v1/sampling.json',
                                         'strategy': 'proportional-cluster-200-v1',
                                         'historical_inputs_required': False},
              'sample_data': {'seeds': [17,18,19,20,21], 'history_instances': 286, 'qa_pairs': 1000, 'bundle': 'samples/proportional-200-v1', 'legacy_seed17_sample_retained': profile == 'full'},
              'legacy_version': {'commit': LEGACY_COMMIT,
                  'archive': 'https://github.com/Nonlotus-eater/ami-reproduction/archive/' + LEGACY_COMMIT + '.zip'},
              'parallel_defaults': {'budget_cny': None, 'history_concurrency': None,
                  'provider_limits': None, 'user_configurable': True,
                  'refind_context_policy': 'deduplicate_observations_v1'},
              'raw_dataset_snapshots_included': False, 'results_included': False,
              'parallel_entrypoint': 'scripts/run_parallel.py', 'derived_included': profile == 'full',
              'credential_value_scan': 'passed',
              'transformations': ['remove optional LightMem registrations/tests and local-only historical v2 regression test',
                                  'portable AMI/Mem0 template paths', 'four-method baseline lock'] + (
                                      ['exclude SDK and research backend; adapt docs and entrypoints'] if profile == 'core' else
                                      ['include AML HTTP client retries; exclude SDK server and research backend'] if profile == 'benchmark' else []),
              'files': {str(p.relative_to(output)): digest(p) for p in exported},
              'total_bytes': sum(p.stat().st_size for p in exported)}
    (output / 'PUBLICATION_MANIFEST.json').write_text(json.dumps(report, indent=2) + '\n')
    return {'output': str(output), 'files': len(exported) + 1, 'bytes': report['total_bytes'],
            'credential_value_scan': 'passed', 'git_history_included': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--profile', choices=('core', 'benchmark', 'full'),
                        help='core: four methods; benchmark: also AML HTTP client; full: also SDK server and research backend')
    args = parser.parse_args()
    print(export(args.output, args.profile))
