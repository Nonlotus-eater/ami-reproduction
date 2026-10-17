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


def export(output):
    output = Path(output).resolve()
    if output == ROOT or ROOT.is_relative_to(output):
        raise ValueError('Choose a new output directory, not the workspace or its ancestor')
    files = {Path(p) for p in ('.env.example', 'pyproject.toml', 'README.md',
        'configs/README.md', 'configs/runtime.json', 'configs/suites/smoke.json',
        'configs/datasets/public_sources.json', 'datasets/README.md',
        'docs/model_handoff_99.md', 'docs/evaluation_contract.md',
        'docs/capability_label_mapping.md', 'docs/publication_scope.md',
        'docs/configuration_validation_20261017.md',
        'docs/benchmark_guide.md', 'docs/sample.md', 'samples/README.md',
        'docs/references/README.md', 'docs/references/AMI_Benchmark_99_reference.docx',
        'patches/ami-system-role.patch', 'patches/invmem-native-interface.patch',
        'patches/refind-native-interface.patch', 'scripts/configure_release.py',
        'scripts/prepare_five_seeds.py', 'scripts/sample_five_seeds.py',
        'scripts/release_smoke.py',
        'scripts/download_public_datasets.py', 'scripts/export_publication.py')}
    for folder in ('src', 'sources', 'examples', 'samples', 'integrations', 'configs/reproduction', 'tests', 'patches/tests'):
        for path in (ROOT / folder).rglob('*'):
            if path.is_file() and path.suffix in {'.py', '.json', '.jsonl'} and '__pycache__' not in path.parts:
                if path.name not in EXCLUDED_SOURCE and path.name != 'test_retry_clbench_judges.py':
                    files.add(path.relative_to(ROOT))
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
    lines[node.lineno - 1:node.end_lineno] = ['MODULES = {"mem0": "mem0", "activememoryindex": "activememoryindex"}\n']
    registry.write_text(''.join(lines))
    test = output / 'tests/test_unbounded_reader.py'
    tree = ast.parse(test.read_text())
    tree.body = [n for n in tree.body if not (isinstance(n, ast.ImportFrom)
                and n.module == 'memory_benchmark.methods.lightmem')]
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            node.body = [n for n in node.body if not (isinstance(n, ast.FunctionDef) and n.name.startswith('test_lightmem_'))]
    test.write_text(ast.unparse(tree) + '\n')
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
        'InvMem/ReFind adapters and bridge are in `integrations/`; native implementations are fetched separately.\n')
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
              'sample_data': {'seed': 17, 'history_instances': 22, 'qa_pairs': 202},
              'raw_dataset_snapshots_included': False, 'results_included': False,
              'credential_value_scan': 'passed',
              'transformations': ['remove optional LightMem factory registrations and test',
                                  'portable AMI/Mem0 template paths', 'four-method baseline lock'],
              'files': {str(p.relative_to(output)): digest(p) for p in exported},
              'total_bytes': sum(p.stat().st_size for p in exported)}
    (output / 'PUBLICATION_MANIFEST.json').write_text(json.dumps(report, indent=2) + '\n')
    return {'output': str(output), 'files': len(exported) + 1, 'bytes': report['total_bytes'],
            'credential_value_scan': 'passed', 'git_history_included': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(export(args.output))
