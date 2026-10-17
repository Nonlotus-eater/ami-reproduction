import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
from memory_benchmark.benchmarks.native_contract import ROOT
from memory_benchmark.methods.activememoryindex import Adapter as AMI
from memory_benchmark.methods.mem0 import Adapter as Mem0
from memory_benchmark.pipeline.context import FixtureTokenizer, normalize
from memory_benchmark.pipeline.reader import answer
from memory_benchmark.runtime.model_client import Reply
from memory_benchmark.schemas import Evidence, Query

class UnboundedReaderTests(unittest.TestCase):

    def test_reader_keeps_all_evidence_and_sends_large_prompt_without_extra_cap(self):
        runtime = json.loads((ROOT / 'configs/runtime.json').read_text())
        evidence = [Evidence(str(i), 'evidence ' * 16) for i in range(125)]
        query = Query('longmemeval_s', 'e', 'q', 'Q' * 120001)
        client = Mock()
        client.complete.return_value = Reply('answer', 'gpt-4o-mini')
        result = answer(query, evidence, ('user', 'assistant'), client, FixtureTokenizer(), runtime)
        self.assertEqual(result['included_ids'], [v.id for v in evidence])
        self.assertEqual(result['omitted_ids'], [])
        self.assertGreater(result['context_tokens'], 4096)
        self.assertGreater(result['prompt_tokens_counted'], 119000)
        self.assertIsNone(client.complete.call_args.kwargs['max_tokens'])
        with self.assertRaises(ValueError):
            normalize([Evidence('same', 'a'), Evidence('same', 'b')], None)

    def test_mem0_unbounded_search_uses_actual_scoped_collection_count(self):
        adapter = object.__new__(Mem0)
        adapter.scope = 'scope'
        adapter.memory = Mock()
        adapter.memory.vector_store.collection_name = 'collection'
        adapter.memory.vector_store.client.count.return_value = SimpleNamespace(count=145)
        adapter.memory.search.return_value = {'results': []}
        adapter.search(Query('clbench', 'e', 'q', 'question'), None)
        adapter.memory.vector_store._create_filter.assert_called_once_with({'user_id': 'scope'})
        adapter.memory.search.assert_called_once_with('question', filters={'user_id': 'scope'}, top_k=145)
        adapter.memory.search.reset_mock()
        adapter.memory.vector_store.client.count.return_value = SimpleNamespace(count=0)
        self.assertEqual(adapter.search(Query('clbench', 'e', 'q', 'question'), None), [])
        adapter.memory.search.assert_not_called()

    def test_ami_unbounded_search_lifts_native_count_cap_only_for_scoped_request(self):
        native_config = SimpleNamespace(RETURN_LIMIT=100, RETURN_CHAR_BUDGET=400000)
        native_store = SimpleNamespace(get=Mock(return_value=SimpleNamespace(items=list(range(151)))))
        adapter = object.__new__(AMI)
        (adapter.scope, adapter.history, adapter.gateway) = ('scope', SimpleNamespace(sessions=[]), Mock())

        def respond(path, json):
            self.assertEqual(native_config.RETURN_LIMIT, 151)
            self.assertEqual(native_config.RETURN_CHAR_BUDGET, float('inf'))
            self.assertEqual(json['top_k'], 151)
            return SimpleNamespace(status_code=200, json=lambda : {'data': []})
        adapter.service = SimpleNamespace(post=Mock(side_effect=respond))
        with patch.dict('sys.modules', {'app': SimpleNamespace(config=native_config, store=native_store)}):
            self.assertEqual(adapter.search(Query('clbench', 'e', 'q', 'question'), None), [])
        self.assertEqual(native_config.RETURN_LIMIT, 100)
        self.assertEqual(native_config.RETURN_CHAR_BUDGET, 400000)
        native_store.get.assert_called_once_with('scope')
if __name__ == '__main__':
    unittest.main()
