"""Seed-wise dataset/capability means and sample standard deviations."""
import csv
import statistics
from .common import read, write, Path


def export_tables(run, config, aggregate=None):
    run = Path(run)
    data = {(seed, m['name']): read(run/'summaries'/f'seed-{seed}'/(m['name']+'.json'))
            for seed in config['seeds'] for m in config['methods']}
    rows = []
    for method in config['methods']:
        name = method['name']
        for group in ['benchmarks', 'capabilities', 'capability_leaves']:
            keys = sorted({k for seed in config['seeds'] for k in data[seed, name][group]})
            for key in keys:
                values = [data[seed, name][group].get(key, {}) for seed in config['seeds']]
                valid = [v['score'] for v in values if v.get('score') is not None]
                missing = any(v.get('expected', 0) != v.get('scored', 0) for v in values)
                rows.append({'method': name, 'group': group, 'category': key,
                    'mean': statistics.mean(valid) if valid and not missing and not config.get('offline') else None,
                    'sample_std': statistics.stdev(valid) if len(valid)>1 and not missing and not config.get('offline') else None,
                    'valid_seed_n': len(valid), 'complete': not missing,
                    **{f'seed_{seed}_score': v.get('score') if not config.get('offline') else None
                       for seed, v in zip(config['seeds'], values)},
                    **{f'seed_{seed}_questions': v.get('expected', 0) for seed, v in zip(config['seeds'], values)}})
    for group in ['benchmarks', 'capabilities', 'capability_leaves']:
        selected = [v for v in rows if v['group'] == group]
        with (run/'summaries'/(group+'.csv')).open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(selected[0]))
            writer.writeheader()
            writer.writerows(selected)
    write(run/'summaries/breakdowns.json', rows)

    def formatted(mean, sd, n):
        if mean is None:return 'NA'
        return f'{mean:.2f} ± {sd:.2f}' + (f' (n={n})' if n != len(config['seeds']) else '') if sd is not None else f'{mean:.2f} ± NA (n={n})'
    methods=[m['name'] for m in config['methods']]
    lines=['# 五 seed 实验汇总','',
           '离线模拟：不能作为方法成绩。' if config.get('offline') else '分数为 0–100，跨 seed 等权；标准差使用样本标准差（ddof=1）。',
           '',f"ReFind 规划上下文策略：`{config.get('refind_context_policy')}`。缺题不当零分；未覆盖小类记 NA。",'',
           '| 方法 | 能力均分 | 数据集均分 |','|---|---:|---:|']
    for name in methods:
        values=(aggregate or {}).get(name,{})
        fields=[]
        for metric in ['capability_overall','dataset_overall']:
            v=values.get(metric,{})
            fields.append(formatted(v.get('mean'),v.get('sample_std'),len(config['seeds'])))
        lines.append('| '+name+' | '+' | '.join(fields)+' |')
    for group,label in [('benchmarks','数据集得分'),('capabilities','能力大类得分'),('capability_leaves','能力小类得分')]:
        selected=[r for r in rows if r['group']==group]
        keys=sorted({r['category'] for r in selected})
        bykey={(r['category'],r['method']):r for r in selected}
        lines.extend(['','## '+label,'','| 类别 | '+' | '.join(methods)+' |','|---|'+'---:|'*len(methods)])
        for key in keys:
            fields=[]
            for name in methods:
                v=bykey[key,name];fields.append(formatted(v['mean'],v['sample_std'],v['valid_seed_n']))
            lines.append('| '+key+' | '+' | '.join(fields)+' |')
    lines.extend(['','每个 seed 的分数与题目数见 summaries/ 的三个 CSV；完整性见 PROGRESS.json。',''])
    temporary=run/'report.md.tmp';temporary.write_text('\n'.join(lines));temporary.replace(run/'report.md')
