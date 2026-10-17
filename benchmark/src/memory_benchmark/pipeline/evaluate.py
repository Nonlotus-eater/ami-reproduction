from importlib import import_module
import time
from .reader import MODULES


def evaluate(query, target, answer, client, runtime):
    started = time.perf_counter()
    if query.qa_id != target.qa_id or query.qa_id != answer["id"]:
        raise ValueError("Query, answer and scoring target IDs differ")
    if query.benchmark not in MODULES:
        from ..benchmarks.public_contract import evaluate as native_evaluate
        result = native_evaluate(query, target, answer["generated_answer"], client, runtime)
        return {**result, "seconds": time.perf_counter() - started}
    scorer = import_module(f"memory_benchmark.benchmarks.{MODULES[query.benchmark]}.scorer")
    prompt = scorer.prompt(query, target, answer["generated_answer"])
    reply = client.complete(prompt, stage="judge", max_tokens=runtime["judge_max_tokens"],
                            temperature=runtime["judge_temperature"])
    label = scorer.parse(reply.text)
    return {"id": query.qa_id, "label": label, "is_correct": label == "CORRECT",
            "judge_response": reply.text, "judge_model": reply.model,
            "seconds": time.perf_counter() - started}
