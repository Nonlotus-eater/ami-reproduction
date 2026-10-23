"""Shared evidence normalization and exact-model context counting."""
from ..schemas import Evidence


class GPTTokenizer:
    def __init__(self, model):
        import tiktoken
        self.encoding = tiktoken.encoding_for_model(model)
        self.name = self.encoding.name

    def count(self, text):
        return len(self.encoding.encode(text, disallowed_special=()))


class FixtureTokenizer:
    name = "offline-utf8-byte-count-NOT-model-tokens"

    def count(self, text):
        return len(text.encode("utf-8"))


def count_prompt(prompt, tokenizer):
    messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else prompt
    return sum(tokenizer.count(message["content"]) + 4 for message in messages) + 2


def normalize(values, top_k):
    if top_k is not None and (type(top_k) is not int or top_k <= 0):
        raise ValueError("Top K must be a positive integer or null")
    evidence = [v if isinstance(v, Evidence) else Evidence.from_wire(v) for v in values]
    if ((top_k is not None and len(evidence) > top_k) or
            len({v.id for v in evidence}) != len(evidence)):
        raise ValueError("Search response exceeds Top K or duplicates evidence IDs")
    return evidence


def pack(values, tokenizer, budget):
    if budget is not None and (type(budget) is not int or budget <= 0):
        raise ValueError("Context budget must be a positive integer or null")
    if budget is None:
        result = "\n".join(f"[{v.timestamp}] {v.text}" if v.timestamp else v.text for v in values)
        return result, {"retrieved": len(values), "included_ids": [v.id for v in values],
                        "omitted_ids": [], "context_tokens": tokenizer.count(result),
                        "tokenizer": tokenizer.name}
    selected, omitted = [], []
    texts = []
    for item in values:
        text = f"[{item.timestamp}] {item.text}" if item.timestamp else item.text
        candidate = "\n".join([*texts, text])
        if tokenizer.count(candidate) <= budget:
            selected.append(item.id)
            texts.append(text)
        else:
            omitted.append(item.id)
    result = "\n".join(texts)
    return result, {"retrieved": len(values), "included_ids": selected, "omitted_ids": omitted,
                    "context_tokens": tokenizer.count(result), "tokenizer": tokenizer.name}


def pack_ranked_prefix(values, tokenizer, render, input_limit):
    """Fit a Unicode-safe evidence prefix against the complete rendered prompt.

    Only evidence is shortened. Query/options/instructions belong to render and
    are never sliced, and the original Search candidates are never modified.
    """
    texts = [f"[{v.timestamp}] {v.text}" if v.timestamp else v.text for v in values]
    original = "\n".join(texts)
    prompt = render(original)
    original_count = count_prompt(prompt, tokenizer)
    end = len(original)
    if original_count > input_limit:
        empty_prompt = render("")
        if count_prompt(empty_prompt, tokenizer) > input_limit:
            raise ValueError("Question/options/instructions exceed the Answer input budget without evidence")
        # Count the rendered prompt at every candidate boundary. Searching Unicode
        # character offsets avoids corrupting a multi-byte character at a BPE cut.
        low, high = 0, len(original)
        while high - low > 1:
            middle = (low + high) // 2
            if count_prompt(render(original[:middle]), tokenizer) <= input_limit:
                low = middle
            else:
                high = middle
        end = low
        prompt = render(original[:end])
    context = original[:end]
    included, omitted, partial = [], [], []
    start = 0
    for item, text in zip(values, texts):
        if start < end:
            included.append(item.id)
            if start + len(text) > end:
                partial.append(item.id)
        else:
            omitted.append(item.id)
        start += len(text) + 1
    counted = count_prompt(prompt, tokenizer)
    if counted > input_limit:
        raise ValueError("Rendered reader prompt exceeds the Answer input budget")
    return context, prompt, {
        "retrieved": len(values), "included_ids": included, "omitted_ids": omitted,
        "partially_included_ids": partial, "context_tokens": tokenizer.count(context),
        "tokenizer": tokenizer.name, "context_truncated": end < len(original),
        "original_context_tokens": tokenizer.count(original),
        "original_prompt_tokens_counted": original_count, "prompt_tokens_counted": counted,
    }
