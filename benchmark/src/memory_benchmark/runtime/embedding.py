"""Shared v4 embedding transform, matching the existing AMI remote policy.

Keep source text intact. Split UTF-8 on code-point boundaries, normalize each
chunk vector, byte-weight pool, then normalize the result. No query prefix,
newline replacement, truncation, local fallback, or cross-method cache.
"""
import math

CHUNK_BYTES = 2048
DIMENSIONS = frozenset({64, 128, 256, 512, 768, 1024, 1536, 2048})
STRATEGY = "utf8-2048-bytes-byte-weighted-normalized-mean-v1"


def settings(profile):
    if profile.get("AMI_EMBED_MODEL", "text-embedding-v4") != "text-embedding-v4":
        raise ValueError("Shared embedding requires text-embedding-v4")
    if profile.get("AMI_EMBED_BACKEND", "openai") != "openai":
        raise ValueError("Shared embedding requires the openai-compatible API")
    if profile.get("AMI_REPRO_EMBED_PROTOCOL", "openai") != "openai":
        raise ValueError("Shared embedding requires the openai-compatible protocol")
    dimensions = int(profile.get("AMI_EMBED_DIMENSIONS", "1024"))
    batch = int(profile.get("AMI_EMBED_BATCH", "10"))
    if dimensions not in DIMENSIONS or not 1 <= batch <= 10:
        raise ValueError("Unsupported v4 embedding dimensions or batch size")
    return dimensions, batch


def chunks(text):
    if not isinstance(text, str) or not text:
        raise ValueError("Embedding inputs must be nonempty strings")
    parts, start, size = [], 0, 0
    for position, char in enumerate(text):
        length = len(char.encode("utf-8"))
        if size + length > CHUNK_BYTES:
            parts.append(text[start:position])
            start, size = position, 0
        size += length
    parts.append(text[start:])
    return parts


def normalized(vector, dimensions):
    if not isinstance(vector, (list, tuple)) or len(vector) != dimensions:
        raise ValueError("Embedding vector dimension mismatch")
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in vector):
        raise ValueError("Embedding vector must contain finite numbers")
    scale = max(abs(v) for v in vector)
    if scale == 0:
        raise ValueError("Embedding vector must be nonzero")
    values = [v / scale for v in vector]
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]


def encode(texts, request, dimensions, batch):
    if not isinstance(texts, list):
        raise ValueError("Embedding batch must be a list")
    parts = [chunks(text) for text in texts]  # Validate every input before sending.
    pooled = [[0.0] * dimensions for _ in texts]
    payload, owners, weights = [], [], []

    def flush():
        vectors = request(payload)
        if len(vectors) != len(payload):
            raise ValueError("Embedding batch response is incomplete")
        for owner, weight, vector in zip(owners, weights, vectors):
            vector = normalized(vector, dimensions)
            for index, value in enumerate(vector):
                pooled[owner][index] += weight * value
        payload.clear()
        owners.clear()
        weights.clear()

    for owner, pieces in enumerate(parts):
        total = sum(len(piece.encode("utf-8")) for piece in pieces)
        for piece in pieces:
            payload.append(piece)
            owners.append(owner)
            weights.append(len(piece.encode("utf-8")) / total)
            if len(payload) == batch:
                flush()
    if payload:
        flush()
    return [normalized(vector, dimensions) for vector in pooled]


def provider(gateway):
    """Bind native LightMem/Mem0 embedder interfaces to one stage-owned gateway."""
    class APIEmbedding:
        def __init__(self, config):
            self.config = config
            if config.model != "text-embedding-v4":
                raise ValueError("Native embedder model differs from shared API")
            self.config.embedding_dims = gateway.embedding_dimensions

        def embed(self, text, memory_action=None):
            if isinstance(text, list):
                return gateway.embed(text)
            return gateway.embed([text])[0]

        def embed_batch(self, texts, memory_action="add"):
            return gateway.embed(texts)

        def get_stats(self):
            rows = [r for r in gateway.budget.rows if r["kind"] == "embedding"]
            return {"total_calls": len(rows), "total_tokens": sum(r.get("input_tokens", 0) for r in rows)}

    return APIEmbedding
