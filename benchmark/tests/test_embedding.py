import math
import unittest

from memory_benchmark.runtime.embedding import chunks, encode, normalized, settings
from memory_benchmark.runtime.profile import public_identity


class EmbeddingTests(unittest.TestCase):
    def test_multilingual_chunks_preserve_every_byte_and_whitespace(self):
        text = ("中文🙂\n  abc\t" * 600)
        parts = chunks(text)
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(part.encode("utf-8")) <= 2048 for part in parts))

    def test_batching_crosses_text_boundaries_and_preserves_order(self):
        texts = ["a" * 4096, "b", "c"]
        calls = []
        def request(payload):
            calls.append(list(payload))
            return [[1, 0] if part[0] == "a" else [0, 1] for part in payload]
        vectors = encode(texts, request, 2, 3)
        self.assertEqual([len(c) for c in calls], [3, 1])
        self.assertEqual(vectors, [[1, 0], [0, 1], [0, 1]])

    def test_long_text_pooling_uses_byte_weights_and_normalizes_chunks(self):
        def request(payload):
            return [[100, 0] if part[0] == "a" else [0, 2] for part in payload]
        vector, = encode(["a" * 2048 + "b" * 1024], request, 2, 10)
        self.assertAlmostEqual(vector[0], 2 / math.sqrt(5))
        self.assertAlmostEqual(vector[1], 1 / math.sqrt(5))

    def test_invalid_texts_are_rejected_before_any_request(self):
        calls = []
        with self.assertRaises(ValueError):
            encode(["valid", ""], lambda p: calls.append(p), 2, 10)
        self.assertEqual(calls, [])
        self.assertEqual(encode([], lambda p: calls.append(p), 2, 10), [])

    def test_bad_vectors_cannot_enter_index(self):
        for vector in ([0, 0], [1], [float("nan"), 1], [float("inf"), 1], [True, 1]):
            with self.subTest(vector=vector), self.assertRaises(ValueError):
                normalized(vector, 2)
        self.assertEqual(normalized([1e300, 0], 2), [1, 0])

    def test_profile_dimensions_batch_and_preprocessing_are_frozen_identity(self):
        profile = {"OPENAI_BASE_URL": "https://llm.example/v1", "AMI_EMBED_BASE_URL": "https://embed.example/v1"}
        first = public_identity(profile, {"model": "gpt-4o-mini"})
        second = public_identity({**profile, "AMI_EMBED_DIMENSIONS": "512"}, {"model": "gpt-4o-mini"})
        self.assertNotEqual(first, second)
        self.assertEqual(first["embedding_dimensions"], 1024)
        self.assertIn("embedding_preprocess", first)
        for changes in ({"AMI_EMBED_DIMENSIONS": "384"}, {"AMI_EMBED_BATCH": "11"},
                        {"AMI_EMBED_MODEL": "other"}, {"AMI_EMBED_BACKEND": "bge"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                settings({**profile, **changes})
