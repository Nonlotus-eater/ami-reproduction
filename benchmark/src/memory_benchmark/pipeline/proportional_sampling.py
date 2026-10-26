"""Versioned 200-question proportional sampler with complete-history reuse."""
from collections import Counter, defaultdict, deque
import hashlib
import itertools
import json
SEEDS = [17, 18, 19, 20, 21]
BENCHMARKS = ['locomo_refined', 'longmemeval_s', 'clbench', 'personamem_v1', 'personamem_v2', 'beam']
QUESTIONS = 200
POLICY = 'proportional-cluster-200-v1'

def priority(namespace, value):
    return hashlib.sha256(json.dumps([namespace, value], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def qkey(q):
    return q['benchmark'], q['qa_id']


def hkey(q):
    return q['benchmark'], q['source_unit_id']


def hamilton(counts, n, namespace):
    denominator = sum(counts.values())
    base = {t: n * count // denominator for t, count in counts.items()}
    order = sorted(counts, key=lambda t: (-(n * counts[t] % denominator), priority(namespace, t)))
    for t in order[:n - sum(base.values())]:
        base[t] += 1
    return base


def balanced(counts, totals, namespace):
    """Integer row totals + Hamilton column totals, each cell floor/ceil ideal.

    A small integral max-flow coordinates rounding across all five seeds.
    """
    kinds = sorted(counts)
    denominator = sum(counts.values())
    overall = hamilton(counts, sum(totals.values()), namespace + ':columns')
    base = {s: {t: totals[s] * counts[t] // denominator for t in kinds} for s in SEEDS}
    row_need = {s: totals[s] - sum(base[s].values()) for s in SEEDS}
    column_need = {t: overall[t] - sum(base[s][t] for s in SEEDS) for t in kinds}
    graph = defaultdict(list)

    def edge(a, b, capacity):
        forward = [b, capacity, None]
        backward = [a, 0, forward]
        forward[2] = backward
        graph[a].append(forward)
        graph[b].append(backward)
        return forward

    source, sink = ('source',), ('sink',)
    handles = {}
    for s in sorted(SEEDS, key=lambda s: priority(namespace + ':row', s)):
        edge(source, ('seed', s), row_need[s])
        for t in sorted(kinds, key=lambda t: priority(namespace + f':seed:{s}', t)):
            if totals[s] * counts[t] % denominator:
                handles[s, t] = edge(('seed', s), ('type', t), 1)
    for t in kinds:
        assert column_need[t] >= 0
        edge(('type', t), sink, column_need[t])
    flow = 0
    while True:
        parents = {source: None}
        queue = deque([source])
        while queue and sink not in parents:
            node = queue.popleft()
            for item in graph[node]:
                if item[1] > 0 and item[0] not in parents:
                    parents[item[0]] = node, item
                    queue.append(item[0])
        if sink not in parents:
            break
        amount, node = 10**9, sink
        while node != source:
            parent, item = parents[node]
            amount = min(amount, item[1])
            node = parent
        node = sink
        while node != source:
            parent, item = parents[node]
            item[1] -= amount
            item[2][1] += amount
            node = parent
        flow += amount
    assert flow == sum(row_need.values()), 'Joint rounding infeasible'
    result = {s: {t: base[s][t] + (1 - handles[s, t][1] if (s, t) in handles else 0) for t in kinds} for s in SEEDS}
    assert all(sum(result[s].values()) == totals[s] for s in SEEDS)
    assert all(sum(result[s][t] for s in SEEDS) == overall[t] for t in kinds)
    assert all(abs(result[s][t] - totals[s] * counts[t] / denominator) < 1 + 1e-10 for s in SEEDS for t in kinds)
    return result


def capacity(records):
    return Counter(q['native_type'] for q in records)


def covers(groups, chosen, quotas):
    cap = Counter()
    for h in chosen:
        cap.update(capacity(groups[h]))
    return all(cap[t] >= n for t, n in quotas.items())


def select_histories(pool, quotas, seed, used_clusters, used_histories):
    benchmark = pool[0]['benchmark']
    groups = defaultdict(list)
    for q in pool:
        if benchmark == 'beam' and q['length_split'] != '100K':
            continue
        groups[q['source_unit_id']].append(q)
    cluster = {h: groups[h][0]['cluster_id'] for h in groups}
    namespace = f'history:{seed}:{benchmark}'
    if benchmark in ['beam', 'locomo_refined']:
        feasible = [pair for pair in itertools.combinations(sorted(groups), 2) if covers(groups, pair, quotas)]
        assert feasible
        chosen = list(min(feasible, key=lambda pair: (
            sum(used_clusters[benchmark, cluster[h]] for h in pair),
            sum(used_histories[benchmark, h] for h in pair), priority(namespace, pair))))
        return groups, chosen
    chosen, remaining = [], dict(quotas)
    while any(remaining.values()):
        gains = {h: sum(min(n, capacity(qs)[t]) for t, n in remaining.items()) for h, qs in groups.items() if h not in chosen}
        candidates = [h for h, gain in gains.items() if gain >= 2]
        if not candidates:
            candidates = [h for h, gain in gains.items() if gain > 0]
        assert candidates, 'Historical pool cannot cover native type quota'
        selected_clusters = {cluster[h] for h in chosen}
        history = min(candidates, key=lambda h: (
            used_clusters[benchmark, cluster[h]], cluster[h] in selected_clusters,
            used_histories[benchmark, h], priority(namespace, h)))
        chosen.append(history)
        remaining = {t: max(0, quotas[t] - sum(capacity(groups[h])[t] for h in chosen)) for t in quotas}
    # Remove redundant writes without changing any question-type quota.
    for h in sorted(chosen, key=lambda h: priority(namespace + ':prune', h)):
        other = [x for x in chosen if x != h]
        if covers(groups, other, quotas):
            chosen = other
    assert covers(groups, chosen, quotas)
    return groups, chosen


def sample_questions(pool, quotas, seed, used_questions, used_clusters, used_histories):
    benchmark = pool[0]['benchmark']
    namespace = f'question:{seed}:{benchmark}'
    selected, load = [], Counter()
    if benchmark in ['longmemeval_s', 'clbench']:
        for kind in sorted(quotas, key=lambda t: priority(namespace + ':type', t)):
            candidates = [q for q in pool if q['native_type'] == kind]
            for _ in range(quotas[kind]):
                chosen = min(candidates, key=lambda q: (
                    used_questions[qkey(q)], used_clusters[benchmark, q['cluster_id']],
                    load[q['cluster_id']], priority(namespace, q['qa_id'])))
                selected.append(chosen)
                candidates.remove(chosen)
                load[chosen['cluster_id']] += 1
    else:
        groups, histories = select_histories(pool, quotas, seed, used_clusters, used_histories)
        for kind in sorted(quotas, key=lambda t: priority(namespace + ':type', t)):
            candidates = [q for h in histories for q in groups[h] if q['native_type'] == kind]
            for _ in range(quotas[kind]):
                chosen = min(candidates, key=lambda q: (
                    load[q['source_unit_id']], used_questions[qkey(q)], priority(namespace, q['qa_id'])))
                selected.append(chosen)
                candidates.remove(chosen)
                load[chosen['source_unit_id']] += 1
        assert {q['source_unit_id'] for q in selected} == set(histories)
    assert capacity(selected) == Counter({t: n for t, n in quotas.items() if n})
    assert len(selected) == len({qkey(q) for q in selected})
    return selected


def design(pool):
    dataset_population = Counter(q['benchmark'] for q in pool)
    dataset_quotas = balanced(dataset_population, {s: QUESTIONS for s in SEEDS}, 'dataset-quota')
    type_quotas = {b: balanced(capacity([q for q in pool if q['benchmark'] == b]),
        {s: dataset_quotas[s][b] for s in SEEDS}, 'native-type-quota:' + b) for b in BENCHMARKS}
    selected = {}
    used_questions, used_clusters, used_histories = Counter(), Counter(), Counter()
    for seed in SEEDS:
        chosen = []
        for benchmark in BENCHMARKS:
            part = sample_questions([q for q in pool if q['benchmark'] == benchmark], type_quotas[benchmark][seed],
                seed, used_questions, used_clusters, used_histories)
            chosen.extend(part)
        assert len(chosen) == QUESTIONS
        selected[seed] = sorted(chosen, key=qkey)
        used_questions.update(qkey(q) for q in chosen)
        used_clusters.update({(q['benchmark'], q['cluster_id']) for q in chosen})
        used_histories.update({hkey(q) for q in chosen})
    return selected, dataset_quotas, type_quotas


