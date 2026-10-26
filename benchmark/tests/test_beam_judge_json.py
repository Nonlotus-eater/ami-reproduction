"""Judge syntax recovery must preserve score validation and saved raw output."""
import json
import unittest

from memory_benchmark.benchmarks.native_contract import contract
from memory_benchmark.benchmarks.public_contract import evaluate, parse_beam_rubric_scores
from memory_benchmark.runtime.model_client import Reply
from memory_benchmark.schemas import Query, Target


def payload(reason, score=.5):
    return json.dumps({'scores': [{'index': 0, 'score': score, 'reason': reason}]})


def malformed(reason, score=.5):
    return payload('REASON_PLACEHOLDER', score).replace('REASON_PLACEHOLDER', reason)


class BeamJudgeJsonTests(unittest.TestCase):
    def test_valid_json_is_identical_to_native_parse(self):
        raw = payload('A valid explanation with a quoted "name" and a newline\n.')
        parsed, changes = parse_beam_rubric_scores(raw, 1)
        self.assertEqual(parsed, contract('beam').parse_rubric_scores(raw, 1))
        self.assertEqual(changes, 0)

    def test_unescaped_latex_preserves_literal_reason_and_score(self):
        reason = r'The response omits \(2^n \geq n+1\).'
        raw = malformed(reason)
        with self.assertRaises(json.JSONDecodeError):
            contract('beam').parse_rubric_scores(raw, 1)
        parsed, changes = parse_beam_rubric_scores(raw, 1)
        self.assertEqual(parsed, [{'index': 0, 'score': .5, 'reason': reason}])
        self.assertEqual(changes, 3)

    def test_already_escaped_latex_windows_paths_and_unicode_are_unchanged(self):
        for reason in [r'\(x \geq 1\)', r'C:\data\file', 'Unicode ≥', 'tab\tnewline\n']:
            with self.subTest(reason=reason):
                raw = payload(reason)
                parsed, changes = parse_beam_rubric_scores(raw, 1)
                self.assertEqual(parsed[0]['reason'], reason.strip())
                self.assertEqual(changes, 0)

    def test_mixed_valid_and_invalid_escapes_are_not_double_escaped(self):
        raw = malformed(r'first\nthen \(x\); path C:\\data')
        parsed, changes = parse_beam_rubric_scores(raw, 1)
        self.assertEqual(parsed[0]['reason'], 'first\nthen \\(x\\); path C:\\data')
        self.assertEqual(changes, 2)

    def test_valid_unicode_escape_is_preserved_during_recovery(self):
        raw = malformed(r'\u2265 and \(x\)')
        parsed, changes = parse_beam_rubric_scores(raw, 1)
        self.assertEqual(parsed[0]['reason'], '≥ and \\(x\\)')
        self.assertEqual(changes, 2)

    def test_fenced_and_prefixed_json_uses_native_object_boundaries(self):
        raw = malformed(r'\(x\)')
        for text in ['```json\n' + raw + '\n```', 'Here is the "evaluation":\n' + raw]:
            with self.subTest(text=text):
                parsed, changes = parse_beam_rubric_scores(text, 1)
                self.assertEqual(parsed[0]['reason'], r'\(x\)')
                self.assertEqual(changes, 2)

    def test_malformed_unicode_is_still_rejected(self):
        with self.assertRaises(json.JSONDecodeError):
            parse_beam_rubric_scores(malformed(r'bad \u12zz'), 1)

    def test_broken_structure_is_still_rejected(self):
        for text in [payload('reason')[:-2], payload('reason').replace('0.5', '0.5,'),
                     payload('reason').replace('"scores"', r'\scores')]:
            with self.subTest(text=text):
                with self.assertRaises((json.JSONDecodeError, ValueError)):
                    parse_beam_rubric_scores(text, 1)

    def test_invalid_numeric_score_is_not_repaired(self):
        with self.assertRaises(ValueError):
            parse_beam_rubric_scores(malformed(r'\(x\)', .7), 1)

    def test_missing_duplicate_or_extra_rubrics_are_still_rejected(self):
        reason = r'\(x\)'
        for text, count in [(malformed(reason), 2),
                            (malformed(reason).replace(']}', ', {"index": 0, "score": 1, "reason": "duplicate"}]}'), 1),
                            (malformed(reason).replace('"index": 0', '"index": 2'), 1)]:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_beam_rubric_scores(text, count)

    def test_evaluate_keeps_original_prompt_response_and_numeric_mean(self):
        raw = malformed(r'Example \(2^n \geq n+1\) is omitted.')
        class Client:
            def __init__(self):
                self.calls = []
            def complete(self, prompt, **kwargs):
                self.calls.append((prompt, kwargs))
                return Reply(raw, 'gpt-4o-mini')
        client = Client()
        query = Query('beam', 'episode', 'q', 'QUESTION')
        target = Target('q', '', (), question_type='summarization',
                        scoring_data={'rubric_nuggets': ['CRITERION']})
        runtime = {'judge_max_tokens': 16384, 'judge_temperature': 0}
        result = evaluate(query, target, 'SAVED ANSWER', client, runtime)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0], (contract('beam').render_batch_judge_prompt(
            'QUESTION', 'SAVED ANSWER', ['CRITERION']),
            {'stage': 'judge', 'max_tokens': 16384, 'temperature': 0}))
        self.assertEqual(result['judge_response'], raw)
        self.assertEqual(result['llm_judge_score'], .5)
        self.assertEqual(result['judge_json_normalization']['escaped_backslashes'], 3)


if __name__ == '__main__':
    unittest.main()
