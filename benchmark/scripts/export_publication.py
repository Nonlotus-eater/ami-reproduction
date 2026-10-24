"""Build allowlisted benchmark sources and seed-17 samples for the fork."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import shutil
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_SOURCE = {'lightmem.py', 'lightmem_api.py', 'lightmem_memma.py', 'pilot.py'}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export(output, profile=None):
    profile = profile or ('full' if (ROOT / 'src/aml_sdk').is_dir() else 'core')
    if profile not in {'core', 'full'}:
        raise ValueError('Unknown publication profile')
    output = Path(output).resolve()
    if output == ROOT or ROOT.is_relative_to(output):
        raise ValueError('Choose a new output directory, not the workspace or its ancestor')
    files = {Path(p) for p in ('.env.example', '.dockerignore', 'pyproject.toml', 'README.md',
        'configs/README.md', 'configs/runtime.json', 'configs/suites/smoke.json',
        'configs/datasets/public_sources.json', 'configs/small_benchmark_v1.json', 'datasets/README.md',
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
    for folder in ('src', 'sources', 'examples', 'samples', 'methods/my_method', 'configs/reproduction', 'tests', 'patches/tests'):
        for path in (ROOT / folder).rglob('*'):
            if path.is_file() and path.suffix in {'.py', '.json', '.jsonl'} and '__pycache__' not in path.parts:
                if path.name not in EXCLUDED_SOURCE:
                    files.add(path.relative_to(ROOT))
    if profile == 'core':
        files = {path for path in files if not str(path).startswith(
            ('src/aml_sdk/', 'examples/sdk/', 'methods/my_method/'))
            and str(path) not in {'.dockerignore', 'src/memory_benchmark/methods/aml_http.py', 'tests/test_sdk.py'}}
        for relative in ('docs/references/AMI_Benchmark_99_reference.docx', 'docs/references/README.md'):
            if (ROOT / relative).is_file():
                files.add(Path(relative))
    # Do not overwrite an existing candidate or the target fork.
    output.mkdir(parents=True, exist_ok=False)
    for relative in sorted(files):
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
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
    if profile == 'full':
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
    if profile == 'core':
        readme = output / 'README.md'
        text = readme.read_text().split('## SDK 首版：', 1)[0]
        text = '\n'.join(line for line in text.splitlines()
                         if not line.startswith(('src/aml_sdk/', 'examples/sdk/', 'methods/my_method/')))
        readme.write_text(text.rstrip() + '\n')
        for name in ('README.md', 'docs/model_handoff_99.md'):
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
                            'python3 scripts/export_publication.py --profile core --output')
        text = text.replace('本次保留 AMI、Mem0、InvMem、ReFind；',
                            '本次采用 core 范围，只发布四方法评测相关功能；SDK 和自定义方法研究入口暂不发布。保留 AMI、Mem0、InvMem、ReFind；')
        text = text.replace('| 参考 Word | 本地留档 |', '| 参考 Word | 保留此前已发布的参考资料 |')
        scope.write_text(text + '\n')
        project = output / 'pyproject.toml'
        text = re.sub(r'^sdk = .*\n', '', project.read_text(), flags=re.MULTILINE)
        text = re.sub(r'^aml-sdk = .*\n', '', text, flags=re.MULTILINE)
        project.write_text(text)
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
    # Compare against long credential values without ever printing their content.
    secrets = []
    env = ROOT / '.env'
    if env.is_file():
        for line in env.read_text().splitlines():
            if '=' not in line or line.lstrip().startswith('#'):
                continue
            key, value = line.split('=', 1)
            value = value.strip().strip('\"\'')
            if re.search(r'KEY|TOKEN|SECRET|PASSWORD', key) and len(value) >= 12:
                secrets.append(value.encode())
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
                      'research_starting_point': 'InvMem / vanilla-rag-memory 0.6.0', 'track': 'textual'} if profile == 'full' else None,
              'public_source_sampling': {'entrypoint': 'scripts/sample_small_benchmark.py',
                                         'config': 'configs/small_benchmark_v1.json',
                                         'strategy': 'v1-history30-leaf2',
                                         'historical_inputs_required': False},
              'sample_data': {'seed': 17, 'history_instances': 22, 'qa_pairs': 202},
              'raw_dataset_snapshots_included': False, 'results_included': False,
              'credential_value_scan': 'passed',
              'transformations': ['remove optional LightMem registrations/tests and local-only historical v2 regression test',
                                  'portable AMI/Mem0 template paths', 'four-method baseline lock'] + (
                                      ['exclude SDK and research backend; adapt docs and entrypoints'] if profile == 'core' else []),
              'files': {str(p.relative_to(output)): digest(p) for p in exported},
              'total_bytes': sum(p.stat().st_size for p in exported)}
    (output / 'PUBLICATION_MANIFEST.json').write_text(json.dumps(report, indent=2) + '\n')
    return {'output': str(output), 'files': len(exported) + 1, 'bytes': report['total_bytes'],
            'credential_value_scan': 'passed', 'git_history_included': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--profile', choices=('core', 'full'),
                        help='core: four-method evaluation; full: also SDK and research backend; default follows available sources')
    args = parser.parse_args()
    print(export(args.output, args.profile))
