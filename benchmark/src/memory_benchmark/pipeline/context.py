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
