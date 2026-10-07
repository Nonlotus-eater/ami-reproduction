"""DashScope native HTTP transport bridge, outside the unchanged upstream embedder.

Converts wire format only. Upstream retains batching, splitting, dimensions,
index validation, normalization, pooling and retrieval. No SDK dependency added.
"""
from types import SimpleNamespace
import threading

import httpx


class NativeEmbeddingClient:
    def __init__(self, endpoint, api_key, timeout, *, transport=None):
        self.endpoint = endpoint
        self._key = api_key
        # No automatic redirects/retries; an HTTP failure is a failed operation.
        self._http = httpx.Client(timeout=timeout, follow_redirects=False,
                                  transport=transport)
        self.embeddings = self

    def close(self):
        self._http.close()

    def create(self, *, model, input, dimensions, encoding_format):
        if model != 'text-embedding-v4' or encoding_format != 'float':
            raise RuntimeError('Unexpected native embedding model or format')
        try:
            response = self._http.post(
                self.endpoint,
                headers={'Authorization': 'Bearer ' + self._key},
                json={'model': model, 'input': {'texts': input},
                      'parameters': {'dimension': dimensions, 'output_type': 'dense'}})
        except httpx.RequestError:
            raise RuntimeError('Native embedding network failure; no retry performed') from None
        if response.status_code != 200:
            # Never propagate response bodies/headers which could echo credentials.
            raise RuntimeError(f'Native embedding HTTP {response.status_code}; no retry performed')
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError()
            if body.get('code') or str(body.get('status_code', 200)) != '200':
                raise ValueError()
            if body.get('model', model) != model:
                raise ValueError()
            rows = body['output']['embeddings']
            if not isinstance(rows, list):
                raise ValueError()
            data = [SimpleNamespace(index=row['text_index'], embedding=row['embedding']) for row in rows]
        except (ValueError, TypeError, KeyError):
            raise RuntimeError('Native embedding invalid/error response or model mismatch') from None
        return SimpleNamespace(data=data)


def install():
    """Explicit launcher opt-in only; installing makes no network calls."""
    from app import config, embed
    if config.EMBED_BACKEND != 'openai' or config.EMBED_RETRIES != 0:
        raise ValueError('Native bridge requires remote backend and AMI_EMBED_RETRIES=0')
    endpoint = embed._remote_config()
    if not endpoint.startswith('https://') or not endpoint.endswith('/services/embeddings/text-embedding/text-embedding'):
        raise ValueError('Native bridge requires the complete HTTPS text-embedding endpoint')
    if not config.EMBED_API_KEY:
        raise ValueError('Native embedding credential is missing')
    client = NativeEmbeddingClient(endpoint, config.EMBED_API_KEY, config.EMBED_TIMEOUT)
    gate = threading.BoundedSemaphore(config.EMBED_CONCURRENCY)
    embed._get_remote_client = lambda: (client, gate)
    original_identity = embed.embedding_identity
    embed.embedding_identity = lambda: dict(original_identity(), transport='dashscope-native-http-v1')
    return client
