"""Regression checks for reader."""
import json
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from memory_benchmark.benchmarks.native_contract import ROOT
from memory_benchmark.methods.activememoryindex import Adapter as AMI
from memory_benchmark.methods.mem0 import Adapter as Mem0
from memory_benchmark.pipeline.context import FixtureTokenizer, GPTTokenizer, normalize, pack_ranked_prefix
from memory_benchmark.pipeline.aml_reader import POLICY as AML_POLICY
from memory_benchmark.pipeline.reader import answer
from memory_benchmark.pipeline.runner import validate_runtime
from memory_benchmark.pipeline.windowed_reader import collect_part, count_prompt, generate, split_text
from memory_benchmark.runtime.model_client import OutputTruncatedError, Reply
from memory_benchmark.schemas import Evidence, Query

class UnboundedReaderTests(unittest.TestCase):

    def test_reader_keeps_all_evidence_and_sends_large_prompt_without_extra_cap(self):
        runtime = json.loads((ROOT / 'configs/runtime.json').read_text())
        runtime.update(answer_context_policy=None, top_k=None, reader_token_budget=None, answer_input_token_limit=None, answer_max_tokens=None)
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

class AMLReaderTests(unittest.TestCase):

    def setUp(self):
        self.runtime = json.loads((ROOT / 'configs/runtime.json').read_text())

    def test_long_evidence_is_audited_but_only_prefix_reaches_one_answer_call(self):
        source = 'evidence ' * 20000
        evidence = [Evidence('first', source), Evidence('later', 'LATER_SENTINEL')]
        original = [item.to_wire() for item in evidence]
        client = Mock()
        client.complete.return_value = Reply('answer', 'gpt-4o-mini')
        result = answer(Query('longmemeval_s', 'e', 'q', 'QUESTION_SENTINEL'), evidence, ('user', 'assistant'), client, FixtureTokenizer(), self.runtime)
        self.assertEqual(result['answer_context_policy'], AML_POLICY)
        self.assertEqual(result['reader_mode'], 'ranked_prefix')
        self.assertTrue(result['context_truncated'])
        self.assertEqual(result['included_ids'], ['first'])
        self.assertEqual(result['partially_included_ids'], ['first'])
        self.assertEqual(result['omitted_ids'], ['later'])
        self.assertTrue(source.startswith(result['reader_context']))
        self.assertNotIn('LATER_SENTINEL', result['reader_prompt'])
        self.assertIn('QUESTION_SENTINEL', result['reader_prompt'])
        self.assertEqual(result['prompt_tokens_counted'], 117760)
        self.assertGreater(result['original_prompt_tokens_counted'], 117760)
        self.assertEqual([item.to_wire() for item in evidence], original)
        client.complete.assert_called_once_with(result['reader_prompt'], stage='answer', max_tokens=8192, temperature=0)
        self.assertEqual(result['prompt_tokens_counted'] + 8192 + result['context_safety_margin'], 128000)

    def test_questions_alone_over_budget_fail_without_shortening_or_model_call(self):
        client = Mock()
        with self.assertRaisesRegex(ValueError, 'without evidence'):
            answer(Query('longmemeval_s', 'e', 'q', 'Q' * 117761), [], ('user', 'assistant'), client, FixtureTokenizer(), self.runtime)
        client.complete.assert_not_called()

    def test_unicode_prefix_counts_chat_prompt_and_preserves_question_options_instructions(self):
        tokenizer = GPTTokenizer('gpt-4o-mini')
        evidence = [Evidence('a', '第一段🙂' * 30), Evidence('b', '第二段🙂' * 200), Evidence('c', '不应跳到此段')]

        def render(context):
            return [{'role': 'system', 'content': 'INSTRUCTIONS_SENTINEL'}, {'role': 'user', 'content': 'QUESTION_SENTINEL\nOPTIONS_SENTINEL\n' + context}]
        budget = count_prompt(render(evidence[0].text + '\n'), tokenizer) + 50
        (context, prompt, stats) = pack_ranked_prefix(evidence, tokenizer, render, budget)
        self.assertTrue('\n'.join((item.text for item in evidence)).startswith(context))
        self.assertLessEqual(count_prompt(prompt, tokenizer), budget)
        self.assertEqual(prompt, render(context))
        self.assertEqual(stats['included_ids'], ['a', 'b'])
        self.assertEqual(stats['partially_included_ids'], ['b'])
        self.assertEqual(stats['omitted_ids'], ['c'])
        self.assertNotIn('�', context)

    def test_exact_fit_and_empty_retrieval_need_no_truncation(self):
        tokenizer = FixtureTokenizer()
        render = lambda context: 'QUESTION\n' + context
        evidence = [Evidence('a', 'fact', timestamp='2026-10-23')]
        expected = '[2026-10-23] fact'
        budget = count_prompt(render(expected), tokenizer)
        (context, prompt, stats) = pack_ranked_prefix(evidence, tokenizer, render, budget)
        self.assertEqual(context, expected)
        self.assertFalse(stats['context_truncated'])
        self.assertEqual(stats['included_ids'], ['a'])
        self.assertEqual(stats['partially_included_ids'], [])
        (context, prompt, stats) = pack_ranked_prefix([], tokenizer, render, budget)
        self.assertEqual(context, '')
        self.assertEqual(stats['retrieved'], 0)
        self.assertFalse(stats['context_truncated'])

    def test_more_than_top_k_is_contract_error_instead_of_silent_slicing(self):
        client = Mock()
        with self.assertRaisesRegex(ValueError, 'Top K'):
            answer(Query('longmemeval_s', 'e', 'q', 'Q'), [Evidence(str(i), 'fact') for i in range(101)], ('user', 'assistant'), client, FixtureTokenizer(), self.runtime)
        client.complete.assert_not_called()

    def test_aml_policy_rejects_conflicting_budgets_before_calling_model(self):
        validate_runtime(self.runtime)
        for (field, value) in (('top_k', None), ('answer_input_token_limit', 119000), ('answer_max_tokens', None), ('answer_max_tokens', 16384), ('answer_safety_margin_tokens', 256), ('reader_token_budget', 4096), ('answer_output_policy', 'model_maximum'), ('answer_part_truncation_policy', 'split_retry_v2')):
            with self.subTest(field=field, value=value):
                runtime = {**self.runtime, field: value}
                with self.assertRaises(ValueError):
                    validate_runtime(runtime)
                client = Mock()
                with self.assertRaises(ValueError):
                    answer(Query('longmemeval_s', 'e', 'q', 'Q'), [], ('user', 'assistant'), client, FixtureTokenizer(), runtime)
                client.complete.assert_not_called()

class WindowedReaderTests(unittest.TestCase):

    def test_part_cache_separates_query_provider_seed_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as folder:

            class Client:
                output = Path(folder) / 'usage.json'
                identity = {'provider': 'one'}
                calls = 0

                def complete(inner, *args, **kwargs):
                    inner.calls += 1
                    return Reply('fixture notes', 'gpt-4o-mini')
            client = Client()
            from memory_benchmark.pipeline.windowed_reader import complete_part, part_prompt
            prompt = part_prompt('Question one', 'evidence')
            (_, detail) = complete_part(prompt, client, self.tokenizer, self.runtime)
            self.assertFalse(detail['reused'])
            (_, detail) = complete_part(prompt, client, self.tokenizer, self.runtime)
            self.assertTrue(detail['reused'])
            self.assertEqual(client.calls, 1)
            complete_part(part_prompt('Question two', 'evidence'), client, self.tokenizer, self.runtime)
            complete_part(prompt, client, self.tokenizer, {**self.runtime, 'seed': 18})
            client.identity = {'provider': 'two'}
            (_, detail) = complete_part(prompt, client, self.tokenizer, self.runtime)
            self.assertEqual(client.calls, 4)
            path = Path(detail['artifact']) / 'reply.json'
            value = json.loads(path.read_text())
            value['text'] = 'changed cached notes'
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, 'response changed'):
                complete_part(prompt, client, self.tokenizer, self.runtime)
            self.assertEqual(client.calls, 4)

    def setUp(self):
        self.tokenizer = GPTTokenizer('gpt-4o-mini')
        self.runtime = {'model': 'gpt-4o-mini', 'seed': 17, 'answer_max_tokens': None, 'answer_temperature': 0}

    def test_truncated_summary_v2_uses_smaller_children_without_dropping_source(self):
        source = 'fact ' * 800
        pieces = []

        class Client:

            def complete(inner, prompt, **options):
                part = prompt[1]['content'].split('Evidence part:\n', 1)[1]
                if part == source:
                    raise OutputTruncatedError(stage='answer_map', response_path='original-length-response.json')
                pieces.append(part)
                return Reply('notes', 'gpt-4o-mini')
        (notes, details, events) = collect_part('Q', source, Client(), self.tokenizer, {**self.runtime, 'answer_part_truncation_policy': 'split_retry_v2'})
        self.assertGreaterEqual(len(pieces), 4)
        self.assertEqual(''.join(pieces), source)
        self.assertEqual(events[0]['split_factor'], 4)
        self.assertEqual(len(notes), len(pieces))

    def test_near_window_input_is_reduced_before_output_becomes_too_small(self):
        source = 'fact ' * 3400
        calls = []

        class Client:

            def complete(inner, prompt, **kwargs):
                calls.append((prompt, kwargs))
                return Reply('short evidence notes' if kwargs['stage'] == 'answer_map' else 'answer', 'gpt-4o-mini')
        runtime = {**self.runtime, 'answer_context_policy': 'model_window_v2', 'answer_min_output_tokens': 512}
        with patch.dict('memory_benchmark.pipeline.windowed_reader.MODEL_CONTEXT_TOKEN_LIMITS', {'gpt-4o-mini': 4096}), patch.dict('memory_benchmark.pipeline.windowed_reader.MODEL_OUTPUT_TOKEN_LIMITS', {'gpt-4o-mini': 512}):
            (_, _, meta) = generate(Query('beam', 'e', 'q', 'Q'), source, lambda s: s, Client(), self.tokenizer, runtime)
        self.assertEqual(meta['reader_mode'], 'hierarchical_evidence')
        self.assertGreaterEqual(calls[-1][1]['max_tokens'], 512)
        for (prompt, options) in calls:
            self.assertLessEqual(count_prompt(prompt, self.tokenizer) + options['max_tokens'] + 256, 4096)

    def test_remaining_output_space_preserves_the_entire_prompt(self):
        prompt = 'word ' * 3000
        calls = []

        class Client:

            def complete(inner, sent, **kwargs):
                calls.append((sent, kwargs))
                return Reply('answer', 'gpt-4o-mini')
        with patch.dict('memory_benchmark.pipeline.windowed_reader.MODEL_CONTEXT_TOKEN_LIMITS', {'gpt-4o-mini': 4096}):
            (reply, sent, meta) = generate(Query('beam', 'e', 'q', 'Q'), prompt, lambda s: s, Client(), self.tokenizer, self.runtime)
        self.assertEqual(sent, prompt)
        self.assertEqual(len(calls), 1)
        self.assertEqual(meta['reader_mode'], 'single_call')
        self.assertLess(calls[0][1]['max_tokens'], 16384)
        self.assertEqual(count_prompt(sent, self.tokenizer) + calls[0][1]['max_tokens'] + 256, 4096)

    def test_unicode_partition_is_lossless_and_every_part_fits(self):
        source = '中文🙂 hello\n' * 1000 + 'শেষ'
        parts = split_text(source, self.tokenizer, 100)
        self.assertEqual(''.join(parts), source)
        self.assertTrue(all((self.tokenizer.count(part) <= 100 for part in parts)))

    def test_oversized_evidence_is_all_read_then_uses_native_final_prompt(self):
        source = 'fact and date\n' * 3000
        calls = []

        class Client:

            def complete(inner, prompt, **kwargs):
                calls.append((prompt, kwargs))
                return Reply('evidence notes' if kwargs['stage'] == 'answer_map' else 'answer', 'gpt-4o-mini')
        render = lambda s: 'NATIVE QUESTION\n' + s
        with patch.dict('memory_benchmark.pipeline.windowed_reader.MODEL_CONTEXT_TOKEN_LIMITS', {'gpt-4o-mini': 4096}), patch.dict('memory_benchmark.pipeline.windowed_reader.MODEL_OUTPUT_TOKEN_LIMITS', {'gpt-4o-mini': 512}):
            (reply, final, meta) = generate(Query('beam', 'e', 'q', 'Q'), source, render, Client(), self.tokenizer, self.runtime)
        maps = [prompt for (prompt, kwargs) in calls if kwargs['stage'] == 'answer_map']
        restored = ''.join((prompt[1]['content'].split('Evidence part:\n', 1)[1] for prompt in maps))
        self.assertEqual(restored, source)
        self.assertTrue(final.startswith('NATIVE QUESTION\n'))
        self.assertEqual(meta['reader_mode'], 'hierarchical_evidence')
        self.assertTrue(all((count_prompt(prompt, self.tokenizer) + kw['max_tokens'] + 256 <= 4096 for (prompt, kw) in calls)))

    def test_saved_parts_are_reused_after_a_final_answer_failure(self):
        with tempfile.TemporaryDirectory() as folder:

            class Client:
                output = Path(folder) / 'usage.json'
                identity = {'model': 'gpt-4o-mini'}
                failed = False
                maps = 0

                def complete(inner, prompt, **kwargs):
                    if kwargs['stage'] == 'answer_map':
                        inner.maps += 1
                        return Reply('notes', 'gpt-4o-mini')
                    if not inner.failed:
                        inner.failed = True
                        raise RuntimeError('transient final-answer failure')
                    return Reply('answer', 'gpt-4o-mini')
            client = Client()
            with patch.dict('memory_benchmark.pipeline.windowed_reader.MODEL_CONTEXT_TOKEN_LIMITS', {'gpt-4o-mini': 4096}), patch.dict('memory_benchmark.pipeline.windowed_reader.MODEL_OUTPUT_TOKEN_LIMITS', {'gpt-4o-mini': 512}):
                with self.assertRaises(RuntimeError):
                    generate(Query('beam', 'e', 'q', 'Q'), 'fact ' * 6000, lambda s: s, client, self.tokenizer, self.runtime)
                maps = client.maps
                generate(Query('beam', 'e', 'q', 'Q'), 'fact ' * 6000, lambda s: s, client, self.tokenizer, self.runtime)
            self.assertEqual(client.maps, maps)

    def test_truncated_summaries_split_without_losing_any_source_characters(self):
        source = 'a fact with a date\n' * 1600
        (completed, truncated) = ([], [])
        tokenizer = self.tokenizer

        class Client:

            def complete(inner, prompt, **kwargs):
                if kwargs['stage'] == 'answer':
                    return Reply('final', 'gpt-4o-mini')
                part = prompt[1]['content'].split('Evidence part:\n', 1)[1]
                if tokenizer.count(part) > 1200:
                    truncated.append(part)
                    raise OutputTruncatedError(stage='answer_map')
                completed.append(part)
                return Reply('notes', 'gpt-4o-mini')
        runtime = {**self.runtime, 'answer_part_truncation_policy': 'split_retry_v1'}
        with patch.dict('memory_benchmark.pipeline.windowed_reader.MODEL_CONTEXT_TOKEN_LIMITS', {'gpt-4o-mini': 4096}), patch.dict('memory_benchmark.pipeline.windowed_reader.MODEL_OUTPUT_TOKEN_LIMITS', {'gpt-4o-mini': 512}):
            (_, _, meta) = generate(Query('beam', 'e', 'q', 'Q'), source, lambda s: s, Client(), tokenizer, runtime)
        self.assertTrue(truncated)
        self.assertEqual(''.join(completed), source)
        self.assertTrue(meta['reader_parts'][0]['truncation_splits'])
        self.assertEqual(sum((d['source_characters'] for d in meta['reader_parts'][0]['parts'])), len(source))

    def test_known_truncated_parts_are_not_requested_again_and_valid_children_are_cached(self):
        with tempfile.TemporaryDirectory() as folder:

            class Client:
                output = Path(folder) / 'usage.json'
                identity = {'model': 'gpt-4o-mini'}
                calls = 0

                def complete(inner, prompt, **kwargs):
                    inner.calls += 1
                    part = prompt[1]['content'].split('Evidence part:\n', 1)[1]
                    if len(part) > 1000:
                        raise OutputTruncatedError(stage='answer_map', response_path='recorded-response.json')
                    return Reply('notes', 'gpt-4o-mini')
            source = 'fact ' * 350
            client = Client()
            with self.assertRaises(OutputTruncatedError):
                collect_part('Q', source, client, self.tokenizer, self.runtime)
            self.assertEqual(client.calls, 1)
            runtime = {**self.runtime, 'answer_part_truncation_policy': 'split_retry_v1'}
            (notes, details, events) = collect_part('Q', source, client, self.tokenizer, runtime)
            self.assertEqual(client.calls, 1 + sum((not d['reused'] for d in details)))
            calls = client.calls
            (cached, _, _) = collect_part('Q', source, client, self.tokenizer, runtime)
            self.assertEqual(cached, notes)
            self.assertEqual(client.calls, calls)
            self.assertEqual(events[0]['response_path'], 'recorded-response.json')

    def test_other_validation_errors_are_not_treated_as_output_truncation(self):

        class Client:
            calls = 0

            def complete(inner, *args, **kwargs):
                inner.calls += 1
                raise ValueError('provider validation failed')
        client = Client()
        with self.assertRaisesRegex(ValueError, 'provider validation failed'):
            collect_part('Q', 'source ' * 500, client, self.tokenizer, {**self.runtime, 'answer_part_truncation_policy': 'split_retry_v1'})
        self.assertEqual(client.calls, 1)
if __name__ == '__main__':
    unittest.main()
