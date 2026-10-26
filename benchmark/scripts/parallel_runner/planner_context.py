"""Extractive planner-view compaction. Never alters stored or returned evidence."""
import hashlib
import tiktoken

LIMIT = 117760
SUFFIX = '\nRespond with the next action only.'
MARKER = '\n[... omitted from planner view for context budget; full evidence remains stored ...]\n'


class PlannerContext:
    def __init__(self, limit=LIMIT):
        self.encoding = tiktoken.get_encoding('o200k_base')
        self.limit = limit
        self.observations = {}
        self.events = []

    def tokens(self, text):
        return self.encoding.encode(text, disallowed_special=())

    def count(self, messages):
        return 2 + sum(4 + len(self.tokens(m['content'])) for m in messages)

    def format_observation(self, hits):
        if not hits:
            return 'No matching memory found.'
        seen, blocks, original = {}, [], []
        duplicate_count = 0
        for rank, hit in enumerate(hits, 1):
            header = f'Result {rank} | score={hit.score:.4f} | session={hit.anchor.session_id}'
            lines, full = [], []
            for chunk in hit.context:
                marker = '[MATCH] ' if chunk.id == hit.anchor.id else ''
                full.append(marker + chunk.content)
                digest = hashlib.sha256(chunk.content.encode()).hexdigest()
                if digest in seen:
                    lines.append(marker + f'[Exact text already shown at {seen[digest]}; chunk={chunk.id}]')
                    duplicate_count += 1
                else:
                    seen[digest] = f'Result {rank}, chunk={chunk.id}'
                    lines.append(marker + f'[chunk={chunk.id}] ' + chunk.content)
            blocks.append((header, '\n'.join(lines)))
            original.append(header + '\n' + '\n'.join(full))
        rendered = '\n\n'.join(h + '\n' + body for h, body in blocks)
        content = 'Observation: ' + rendered + SUFFIX
        self.observations[content] = blocks
        self.events.append({'stage': 'observation', 'results': len(hits),
                            'original_tokens': len(self.tokens('\n\n'.join(original))),
                            'deduplicated_tokens': len(self.tokens(rendered)),
                            'duplicate_chunk_occurrences_removed': duplicate_count})
        return rendered

    def fit(self, messages):
        copied = [dict(m) for m in messages]
        before = self.count(copied)
        if before <= self.limit:
            self.events.append({'stage': 'planner_prompt', 'before_tokens': before,
                                'after_tokens': before, 'truncated': False, 'limit': self.limit})
            return copied
        eligible = {i: [(h, self.tokens(body)) for h, body in self.observations[m['content']]]
                    for i, m in enumerate(messages)
                    if m.get('role') == 'user' and m['content'] in self.observations}
        if not eligible:
            raise ValueError('Oversize non-observation content; refusing to truncate the question or instructions')

        def excerpt(tokens, cap):
            if len(tokens) <= cap:
                return self.encoding.decode(tokens)
            head = (cap * 3) // 4
            tail = cap - head
            decode = lambda values: self.encoding.decode_bytes(values).decode('utf-8', errors='ignore')
            return decode(tokens[:head]) + MARKER + (decode(tokens[-tail:]) if tail else '')

        def render(cap):
            result = [dict(m) for m in messages]
            for i, blocks in eligible.items():
                result[i]['content'] = ('Observation: ' + '\n\n'.join(
                    h + '\n' + excerpt(tokens, cap) for h, tokens in blocks) + SUFFIX)
            return result

        result = render(0)
        if self.count(result) > self.limit:
            raise ValueError('Question/actions/headers exceed budget; refusing to truncate them')
        lo, hi = 0, max(len(t) for blocks in eligible.values() for _, t in blocks)
        while lo <= hi:
            cap = (lo + hi) // 2
            candidate = render(cap)
            if self.count(candidate) <= self.limit:
                result, lo = candidate, cap + 1
            else:
                hi = cap - 1
        after = self.count(result)
        assert after <= self.limit
        assert all(result[i] == m for i, m in enumerate(messages) if i not in eligible)
        self.events.append({'stage': 'planner_prompt', 'before_tokens': before,
                            'after_tokens': after, 'truncated': True, 'limit': self.limit,
                            'observation_messages_compacted': len(eligible)})
        return result
