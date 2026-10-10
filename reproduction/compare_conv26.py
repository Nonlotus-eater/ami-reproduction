"""Offline AMI / LightMem / LightMem+MemMA comparison on aligned Qwen8B QA.

Re-scores saved predictions using the same dataset scorer. Retrieval comparison
uses observed evidence-ID coverage; incomplete provenance is kept explicit.
No model calls, no changes to historical experiment files.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import re
import statistics
import sys

from .locomo_conversation import DATASET, load_records, save
from .serve import ROOT

BASELINE = DATASET.parents[1] / 'memma/results/qwen_api_conv26_3seeds_20260928_151025'
METRICS = ['f1', 'bleu1', 'observed_recall_at30', 'observed_success_at30', 'observed_all_at30']


def ids(value):
    if isinstance(value, str):
        return set(re.findall(r'D\d+:\d+', value))
    return set().union(*(ids(v) for v in value or []))


def question_signature(q):
    answer = q['answer'] if isinstance(q['answer'], list) else [q['answer']]
    return (q['question'], tuple(sorted(str(a) for a in answer)), str(q['category']),
            tuple(sorted(ids(q.get('evidence')))), bool(q.get('is_multi_modality')))


def coverage(trace, gold):
    selected = trace[:30]
    gold = ids(gold)
    found = set().union(*(ids(row.get('source_dia_ids')) for row in selected))
    hit = gold & found
    complete = all(row.get('provenance_complete') is True for row in selected)
    return {'observed_recall_at30': len(hit) / len(gold) if gold else None,
            'observed_success_at30': float(bool(hit)) if gold else None,
            'observed_all_at30': float(gold <= found) if gold else None,
            'fully_traceable': complete,
            'traceable_fraction': sum(row.get('provenance_complete') is True for row in selected) / len(selected) if selected else 0,
            'returned': len(selected)}


def averages(rows):
    result = {'questions': len(rows), 'questions_with_evidence': sum(r['observed_recall_at30'] is not None for r in rows)}
    for name in METRICS:
        values = [r[name] for r in rows if r[name] is not None]
        result[name] = statistics.mean(values) if values else None
    labelled = [r for r in rows if r['observed_recall_at30'] is not None]
    result['fully_traceable_questions'] = sum(r['fully_traceable'] for r in labelled)
    result['fully_traceable_query_rate'] = result['fully_traceable_questions'] / len(labelled) if labelled else None
    result['mean_traceable_memory_fraction'] = statistics.mean(r['traceable_fraction'] for r in rows) if rows else None
    return result


def write_csv(path, rows):
    keys = list(dict.fromkeys(key for row in rows for key in row))
    with path.open('w', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


async def analyze(args):
    sys.path.insert(0, str(args.dataset / 'src'))
    import evaluate
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source_files = []
    ami_report = json.loads((args.ami / 'report.json').read_text())
    baseline_manifest = json.loads((args.baseline / 'experiment_manifest.json').read_text())
    expected = {q['qa_id']: q for q in load_records(args.ami / 'questions.jsonl')}
    assert len(expected) == 138 and all(q['sample_id'] == 'conv-26' for q in expected.values())
    runs, records, audit = [], [], []
    ami_id_map = json.loads((args.ami / 'evidence_id_map.json').read_text())
    raw_source = {item_id: dia_id for dia_id, item_id in ami_id_map.items()}
    arms = [('AMI', None, args.ami)] + [
        (method, seed, args.baseline / 'full' / f'seed_{seed}' / 'qwen_8b' / method)
        for method in ['lightmem', 'memma'] for seed in [42, 43, 44]]
    for method, seed, directory in arms:
        label = method if seed is None else f'{method}_{seed}'
        questions_path, predictions_path = directory / 'questions.jsonl', directory / 'predictions.jsonl'
        questions = {q['qa_id']: q for q in load_records(questions_path)}
        predictions = load_records(predictions_path)
        assert set(questions) == set(expected), label
        assert len(predictions) == len({q['qa_id'] for q in predictions}) == 138, label
        assert {q['qa_id'] for q in predictions} == set(expected), label
        for qid in expected:
            assert question_signature(questions[qid]) == question_signature(expected[qid]), (label, qid)
        scored = await evaluate.evaluate_public_predictions(questions_path=questions_path,
            predictions_path=predictions_path, output_path=output / f'{label}_rescored.jsonl',
            metrics=('f1', 'bleu'), concurrency=1)
        saved_scores = {row['qa_id']: row for row in load_records(directory / 'predictions_scored.jsonl')}
        for row in scored:
            for key in ['f1_score', 'bleu_score']:
                assert abs(row[key] - saved_scores[row['qa_id']][key]) < 1e-12, (label, row['qa_id'], key)
        source_files += [questions_path, predictions_path, directory / 'predictions_scored.jsonl']
        if method == 'AMI':
            traces = {}
            for qid in expected:
                path = directory / 'retrieval' / (qid.replace('#', '_') + '.json')
                memories = json.loads(path.read_text())['data']
                traces[qid] = [
                    {'source_dia_ids': [raw_source[m['id']]] if m['id'] in raw_source else [],
                     'provenance_complete': m['id'] in raw_source} for m in memories]
                source_files.append(path)
            audit.append({'method': 'AMI', 'model': ami_report['llm_model'],
                'embedding': ami_report['embedding_model'], 'memory_count': ami_report['stored_raw'] + ami_report['stored_facts'],
                'temperature': 0, 'answer_max_tokens': 512, 'retrieve_k': 30,
                'extract_max_tokens': 1200, 'source_policy': 'Verified raw IDs only; fact lineage unknown'})
        else:
            trace_path = directory / 'retrieval_trajectory.jsonl'
            trajectories = {r['qa_id']: r for r in load_records(trace_path)}
            assert set(trajectories) == set(expected)
            traces = {qid: r['trace']['final'] for qid, r in trajectories.items()}
            for qid, r in trajectories.items():
                assert ids(r['gold_evidence']) == ids(expected[qid]['evidence'])
            result_path = next(directory.glob('results_*.json'))
            result = json.loads(result_path.read_text())
            memory_audit = json.loads((directory / 'memory_audit.json').read_text())
            eval_run = json.loads((directory / 'eval_run.json').read_text())
            command = eval_run['command']
            embedding = command[command.index('--embedding_model') + 1]
            audit.append({'method': method, 'seed': seed, 'model': result['model'],
                'embedding': embedding, 'memory_count': memory_audit['memory_count'],
                'temperature': result['answer_temperature'], 'answer_max_tokens': result['answer_max_tokens'],
                'retrieve_k': result['retrieve_k'], 'extract_max_tokens': baseline_manifest['run_settings']['extraction_max_tokens'],
                'source_policy': 'Saved memory provenance; partial update lineage stays unknown'})
            assert result['model'] == 'qwen3-8b' and result['prediction_errors'] == 0
            assert all(len(trace) == 30 for trace in traces.values())
            source_files += [trace_path, result_path, directory / 'memory_audit.json', directory / 'eval_run.json']
        arm_rows = []
        for row in scored:
            qid = row['qa_id']
            diagnostic = {'method': method, 'seed': seed, 'qa_id': qid, 'category': str(expected[qid]['category']),
                'question': expected[qid]['question'], 'prediction': row['predicted_answer'],
                'f1': row['f1_score'], 'bleu1': row['bleu_score'], **coverage(traces[qid], expected[qid]['evidence'])}
            arm_rows.append(diagnostic)
        records += arm_rows
        runs.append({'method': method, 'seed': seed, 'overall': averages(arm_rows),
                     'by_category': {category: averages([r for r in arm_rows if r['category'] == category])
                                     for category in ['1', '2', '3', '4']}})
    aggregated = {}
    for method in ['AMI', 'lightmem', 'memma']:
        selected = [run for run in runs if run['method'] == method]
        aggregate = {'runs': len(selected), 'by_category': {}}
        for group in ['overall', '1', '2', '3', '4']:
            groups = [run['overall'] if group == 'overall' else run['by_category'][group] for run in selected]
            summary = {'questions_per_run': groups[0]['questions'],
                       'evidence_questions_per_run': groups[0]['questions_with_evidence']}
            for name in METRICS + ['fully_traceable_query_rate', 'mean_traceable_memory_fraction']:
                values = [g[name] for g in groups]
                summary[name] = {'mean': statistics.mean(values),
                    'sample_sd': statistics.stdev(values) if len(values) > 1 else None, 'per_run': values}
            if group == 'overall':
                aggregate['overall'] = summary
            else:
                aggregate['by_category'][group] = summary
        aggregated[method] = aggregate
    paired = []
    for qid, question in expected.items():
        row = {'qa_id': qid, 'category': question['category'], 'question': question['question']}
        for method in ['AMI', 'lightmem', 'memma']:
            subset = [r for r in records if r['method'] == method and r['qa_id'] == qid]
            for metric in METRICS:
                values = [r[metric] for r in subset if r[metric] is not None]
                row[f'{method}_{metric}'] = statistics.mean(values) if values else None
        for method in ['lightmem', 'memma']:
            for metric in ['f1', 'bleu1']:
                row[f'AMI_minus_{method}_{metric}'] = row[f'AMI_{metric}'] - row[f'{method}_{metric}']
        paired.append(row)
    complete_subsets = []
    for seed in [42, 43, 44]:
        maps = {method: {r['qa_id']: r for r in records if r['method'] == method and
                        (method == 'AMI' or r['seed'] == seed)} for method in ['AMI', 'lightmem', 'memma']}
        subset = [qid for qid in expected if ids(expected[qid]['evidence']) and
                  all(maps[method][qid]['fully_traceable'] for method in maps)]
        complete_subsets.append({'seed': seed, 'questions': len(subset), 'question_ids': subset,
            'metrics': {method: averages([maps[method][qid] for qid in subset]) for method in maps} if subset else {}})
    summary = {'conversation': 'conv-26', 'questions': 138, 'gold_evidence_questions': 137,
        'model': 'qwen3-8b', 'metrics': aggregated, 'runs': runs, 'configuration_audit': audit,
        'fully_traceable_shared_subsets': complete_subsets,
        'notes': ['AMI has one run; baselines have three seeds.',
                  'Retrieval measures observed provenance coverage, not semantic correctness.',
                  'AMI facts have no verified turn lineage and are excluded from observed hits.',
                  'Embeddings, temperature and token budgets differ; this is not a controlled causal comparison.']}
    save(output / 'summary.json', summary)
    write_csv(output / 'per_question.csv', paired)
    write_csv(output / 'per_run.csv', [{'method': run['method'], 'seed': run['seed'], **run['overall']} for run in runs])
    write_csv(output / 'retrieval_details.csv', records)
    save(output / 'source_manifest.json', {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(source_files))})
    save(output / 'configuration_audit.json', audit)

    def fmt(method, metric, category=None):
        group = aggregated[method]['overall'] if category is None else aggregated[method]['by_category'][category]
        value = group[metric]
        return f"{value['mean'] * 100:.2f}" + (f" ± {value['sample_sd'] * 100:.2f}" if value['sample_sd'] is not None else '')

    lines = ['# conv-26：AMI / LightMem / LightMem + MemMA', '',
        '日期：2026-10-10。比较相同 Qwen3-8B、相同 138 道问题。AMI 为本次单轮；历史基线为 seed 42/43/44 三轮。',
        '所有保存答案均用当前同一 LoCoMo-Refined 官方词法函数重算，与保存分数逐题一致；未调用任何模型。', '',
        '## 回答质量', '', '分数 ×100；± 为三轮样本标准差，AMI 单轮没有标准差。', '',
        '| 方法 | F1 | BLEU-1 | 轮数 |', '|---|---:|---:|---:|']
    labels = {'AMI': 'AMI', 'lightmem': 'LightMem', 'memma': 'LightMem + MemMA'}
    for method in labels:
        lines.append(f"| {labels[method]} | {fmt(method, 'f1')} | {fmt(method, 'bleu1')} | {aggregated[method]['runs']} |")
    lines += ['', '## 检索：同分母的已观察来源覆盖 @30', '',
        '138 题中 137 题有标注证据；下列三个指标统一在这 137 题上计算，使用实际最终返回顺序的前 30 条。',
        'Success=已观察来源至少覆盖一条证据；All=已观察来源覆盖全部证据；Recall=覆盖比例。',
        '来源不完整时，这些值是下界，不能把未观察到来源当成语义检索失败。', '',
        '| 方法 | Success | All | Recall | 完整可追踪问题比例 |', '|---|---:|---:|---:|---:|']
    for method in labels:
        lines.append(f"| {labels[method]} | {fmt(method, 'observed_success_at30')} | {fmt(method, 'observed_all_at30')} | {fmt(method, 'observed_recall_at30')} | {fmt(method, 'fully_traceable_query_rate')} |")
    lines += ['', 'AMI 的 raw 按存储 ID 精确映射回 Dialogue ID；fact 没有经验证的句级来源，因此不补推来源。',
        'LightMem / MemMA 使用保存的 source_dia_ids；MemMA 的更新记忆有不完整 lineage。',
        '不能把 MemMA 仅限完整来源子集的旧 evidence_recall 均值与其他方法全 137 题的均值混用。',
        '共同完整来源子集题数（seed 42/43/44）：' + ', '.join(str(s['questions']) for s in complete_subsets) + '。完整子集结果保存在 summary.json。', '',
        '## 分题型回答质量', '', '| 题型 | 题数 | AMI F1 | LightMem F1 | LightMem+MemMA F1 | AMI BLEU-1 | LightMem BLEU-1 | LightMem+MemMA BLEU-1 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    categories = {'1': '多跳', '2': '时间', '3': '开放域', '4': '单跳'}
    for category, title in categories.items():
        count = aggregated['AMI']['by_category'][category]['questions_per_run']
        values = [fmt(method, metric, category) for metric in ['f1', 'bleu1'] for method in labels]
        lines.append(f"| {title} | {count} | " + ' | '.join(values) + ' |')
    lines += ['', '## 配置与解释边界', '',
        '| 项目 | AMI | LightMem / LightMem+MemMA |', '|---|---|---|',
        '| 数据 / 模型 / 最终记忆数上限 | conv-26 / Qwen3-8B / 30 | 相同 |',
        '| Embedding | text-embedding-v4，1024 维 | all-MiniLM-L6-v2，384 维 |',
        '| 回答 temperature | 0 | 0.7 |',
        '| 回答输出上限 | 512 tokens | 8192 tokens |',
        '| 抽取输出上限 | 1200 tokens；每 Add 最多 24 facts | 8192 tokens；原生 LightMem 缓冲提取 |',
        '| 记忆表示 | 419 raw + 348 facts | 抽取、压缩和更新后的记忆 |',
        '| 检索增强 | recall 融合、邻居扩展、raw-first | LightMem 基础检索；MemMA 额外指导/迭代/自检 |',
        '| 来源记录 | raw 可精确溯源；fact 句级来源缺失 | 保存原生来源；部分 MemMA 更新来源不完整 |', '',
        'AMI 使用现行 LightMem 的 concise reader 模板，人物分组适配 AMI 的 raw/fact；reader 输入组织仍有差异。',
        '上述是现有运行配置的结果对照，不应将差值单独归因为某个框架或宣称统计显著。', '',
        '## 文件与来源', '',
        f'- AMI：`{args.ami.resolve()}`', f'- 历史 Qwen8B 基线：`{args.baseline.resolve()}`',
        '- summary.json：各轮、类别、配置和共同可追踪子集。',
        '- per_question.csv：138 题配对结果、三 seed 均值及 AMI 差值。',
        '- per_run.csv：各轮总体指标。',
        '- retrieval_details.csv：每题每轮的来源覆盖与完整性。',
        '- source_manifest.json：原始文件路径与 SHA256；原历史产物未修改。']
    (output / 'report.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({'answer': {m: aggregated[m]['overall'] for m in aggregated},
                      'shared_complete_counts': [r['questions'] for r in complete_subsets]}, ensure_ascii=False, indent=2))
    print('Aligned and verified: 138 QA × 7 runs; 137 evidence-labelled QA per run. Output:', output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=DATASET)
    parser.add_argument('--baseline', type=Path, default=BASELINE)
    parser.add_argument('--ami', type=Path, default=ROOT / 'artifacts/locomo-conv26-qwen-once')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/comparison-conv26-qwen8b')
    asyncio.run(analyze(parser.parse_args()))


if __name__ == '__main__':
    main()
