"""Only in-memory MockTransport; never contacts a provider or uses real keys."""
import json
import socket
import unittest
from unittest.mock import patch

import httpx
import numpy as np

from reproduction.native_embedding import NativeEmbeddingClient
from app import config, embed


class NativeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch.object(socket.socket, 'connect', side_effect=AssertionError('No network allowed'))
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.calls = []

    def client(self, reply):
        def handle(request):
            self.calls.append(request)
            return reply(request) if callable(reply) else reply
        client = NativeEmbeddingClient('https://synthetic.invalid/api/v1/services/embeddings/text-embedding/text-embedding',
                                       'synthetic-fixture-not-real', 1, transport=httpx.MockTransport(handle))
        self.addCleanup(client.close)
        return client

    def invoke(self, client):
        return client.create(model='text-embedding-v4', input=['synthetic one', 'synthetic two'], dimensions=64, encoding_format='float')

    def test_native_wire_format_and_upstream_vector_checks(self):
        rows = [{'text_index': 1, 'embedding': [0, 3] + [0] * 62},
                {'text_index': 0, 'embedding': [4, 0] + [0] * 62}]
        client = self.client(httpx.Response(200, json={'output': {'embeddings': rows}}))
        import threading
        with patch.multiple(config, EMBED_DIMENSIONS=64, EMBED_MODEL='text-embedding-v4'):
            vectors = embed._remote_batch(client, threading.Semaphore(1), ['synthetic one', 'synthetic two'])
        body = json.loads(self.calls[0].content)
        self.assertEqual(body, {'model':'text-embedding-v4', 'input':{'texts':['synthetic one','synthetic two']},
                                'parameters':{'dimension':64,'output_type':'dense'}})
        self.assertEqual(self.calls[0].headers['authorization'], 'Bearer synthetic-fixture-not-real')
        np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), [1,1])
        self.assertEqual(np.argmax(vectors, axis=1).tolist(), [0,1])

    def test_error_and_redirect_never_retry_or_expose_body(self):
        for status in [301, 401, 403, 429, 500, 503]:
            with self.subTest(status=status):
                client = self.client(httpx.Response(status, text='sensitive-echo', headers={'location':'https://elsewhere.invalid'}))
                before = len(self.calls)
                with self.assertRaises(RuntimeError) as error:
                    self.invoke(client)
                self.assertEqual(len(self.calls)-before, 1)
                self.assertNotIn('sensitive-echo', str(error.exception))

    def test_network_error_never_retry(self):
        def fail(request):
            raise httpx.ConnectError('sensitive-detail', request=request)
        with self.assertRaisesRegex(RuntimeError, '^Native embedding network failure; no retry performed$'):
            self.invoke(self.client(fail))
        self.assertEqual(len(self.calls),1)

    def test_response_errors_and_model_mismatch(self):
        for body in [{'code':'InvalidApiKey'}, {'model':'wrong-model','output':{'embeddings':[]}},
                     {'output':{'embeddings':'bad'}}, {'status_code':500}, {}]:
            with self.subTest(body=body):
                with self.assertRaises(RuntimeError):
                    self.invoke(self.client(httpx.Response(200,json=body)))

    def test_wrong_requested_model_makes_zero_requests(self):
        client = self.client(httpx.Response(200,json={}))
        with self.assertRaises(RuntimeError):
            client.create(model='wrong',input=['synthetic'],dimensions=64,encoding_format='float')
        self.assertEqual(self.calls,[])

    def test_original_vector_dimension_validation_retained(self):
        client=self.client(httpx.Response(200,json={'output':{'embeddings':[{'text_index':0,'embedding':[1]}]}}))
        import threading
        with patch.multiple(config, EMBED_DIMENSIONS=64, EMBED_MODEL='text-embedding-v4'), self.assertRaises(ValueError):
            embed._remote_batch(client,threading.Semaphore(1),['synthetic'])


if __name__ == '__main__':
    unittest.main()
