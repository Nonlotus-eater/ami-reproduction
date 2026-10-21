"""Add boundaries, source preservation, and regressions for real input formats."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from memory_benchmark.methods.activememoryindex import Adapter
from memory_benchmark.methods.compatibility import validate_ami_roles
from memory_benchmark.pipeline.ingestion import POLICY, plan, word_count
from memory_benchmark.runtime.artifacts import rows
from memory_benchmark.schemas import History, Message, Session


def history(messages):
    return History("clbench", "episode", ("User", "Assistant"),
                   (Session("source-session", "2026-10-01T00:00:00+00:00", "source date", tuple(messages)),))


class IngestionTests(unittest.TestCase):
    def test_boundary_matrix_reconstructs_sources_and_keeps_each_limit(self):
        for messages in (0, 1, 19, 20, 21, 40, 41):
            for words in (0, 1, 1999, 2000, 2001, 4001):
                with self.subTest(messages=messages, words=words):
                    texts = ['  中文🙂\t' * words + '\n'] + ['follow-up' for _ in range(max(0, messages - 1))]
                    texts = texts if messages else []
                    source = history(Message(str(i), 'system' if i == 0 else 'assistant',
                        'Alice', text, None) for i, text in enumerate(texts))
                    chunks, audit = plan(source, POLICY)
                    rebuilt = {str(i): '' for i in range(messages)}
                    for chunk in chunks:
                        self.assertLessEqual(len(chunk.messages), 20)
                        self.assertLessEqual(sum(word_count(m.text) for m in chunk.messages), 2000)
                        for message in chunk.messages:
                            rebuilt[message.source_id] += message.text
                    self.assertEqual([rebuilt[str(i)] for i in range(messages)], texts)
                    self.assertEqual(len({c.chunk_id for c in chunks}), len(chunks))
                    self.assertEqual(plan(source, POLICY)[1]['plan_sha256'], audit['plan_sha256'])

    def test_message_limit_keeps_roles_dates_session_and_order(self):
        source = history(Message(str(i), "system" if i == 0 else "user", "User", str(i), None)
                         for i in range(41))
        chunks, audit = plan(source, POLICY)
        self.assertEqual([len(c.messages) for c in chunks], [20, 20, 1])
        self.assertEqual([m for c in chunks for m in c.messages], list(source.sessions[0].messages))
        self.assertEqual({c.session_id for c in chunks}, {"source-session"})
        self.assertEqual({c.timestamp for c in chunks}, {source.sessions[0].timestamp})
        self.assertEqual(len({c.chunk_id for c in chunks}), 3)
        self.assertEqual(plan(source, POLICY), (chunks, audit))

    def test_words_and_a_long_single_message_are_partitioned_without_loss(self):
        text = "  " + "中文🙂\u2028hello\t" * 2500 + "শেষ  "
        source = history([Message("original", "system", "System", text, None)])
        chunks, audit = plan(source, POLICY)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(m.text for c in chunks for m in c.messages), text)
        self.assertTrue(all(sum(word_count(m.text) for m in c.messages) <= 2000 for c in chunks))
        self.assertEqual({m.source_id for c in chunks for m in c.messages}, {"original"})
        self.assertTrue(all(row["source_spans"] for row in audit["chunks"]))

    def test_word_boundary_flushes_before_next_message(self):
        source = history([Message("a", "user", "User", "a " * 1900, None),
                          Message("b", "assistant", "Assistant", "b " * 101, None)])
        chunks, _ = plan(source, POLICY)
        self.assertEqual([sum(word_count(m.text) for m in c.messages) for c in chunks], [1900, 101])

    def test_legacy_sessions_are_unchanged_and_unknown_policy_is_rejected(self):
        source = history([Message("m", "user", "User", "hello", None)])
        self.assertIs(plan(source)[0], source.sessions)
        with self.assertRaises(ValueError):
            plan(source, "unknown")

    def test_ami_uses_distinct_write_ids_but_preserves_source_session(self):
        source = history(Message(str(i), "user", "User", "hello", None) for i in range(21))
        chunks, _ = plan(source, POLICY)
        adapter = object.__new__(Adapter)
        adapter.scope = "isolated"
        adapter.gateway = Mock()
        adapter.service = SimpleNamespace(post=Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {"success": True})))
        for chunk in chunks:
            adapter.add(chunk)
        calls = [c.kwargs["json"] for c in adapter.service.post.call_args_list]
        self.assertEqual({c["session_id"] for c in calls}, {"source-session"})
        self.assertEqual(len({c["request_id"] for c in calls}), 2)

    def test_ami_schema_fails_locally_before_rejecting_a_system_write(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "app/main.py"
            p.parent.mkdir()
            p.write_text('class Message:\n    role: Literal["user", "assistant"]\n')
            with self.assertRaisesRegex(ValueError, "rejects roles"):
                validate_ami_roles({"source_root": folder}, {"system"})
            p.write_text('class Message:\n    role: Literal["user", "assistant", "system"]\n')
            self.assertIn("system", validate_ami_roles({"source_root": folder}, {"system"}))

    def test_jsonl_preserves_unicode_line_separators_and_rejects_a_partial_tail(self):
        values = [{"answer": "first\u2028second\u2029third\u0085last"}, {"answer": "normal"}]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "answers.jsonl"
            text = "\n".join(json.dumps(v, ensure_ascii=False) for v in values) + "\n"
            path.write_text(text, encoding="utf-8")
            self.assertGreater(len(text.splitlines()), 2)
            self.assertEqual(list(rows(path)), values)
            path.write_text(text + '{"answer":', encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                list(rows(path))
