from importlib import import_module
import time
from .context import normalize, pack

MODULES = {"locomo_refined": "locomo_refined", "longmemeval_s": "longmemeval"}


def answer(query, evidence, speakers, client, tokenizer, runtime):
    started = time.perf_counter()
    evidence = normalize(evidence, runtime["top_k"])
    context, stats = pack(evidence, tokenizer, runtime["reader_token_budget"])
    if query.benchmark in MODULES:
        template = import_module(f"memory_benchmark.benchmarks.{MODULES[query.benchmark]}.prompts")
        prompt = template.render(query, context, speakers)
    else:
        from ..benchmarks.public_contract import render
        prompt = render(query, context, runtime["model"])
    counted = tokenizer.count(prompt) if isinstance(prompt, str) else sum(tokenizer.count(m["content"]) + 4 for m in prompt) + 2
    if runtime["answer_input_token_limit"] is not None and counted > runtime["answer_input_token_limit"]:
        raise ValueError("Rendered reader prompt exceeds the frozen input budget")
    reply = client.complete(prompt, stage="answer", max_tokens=runtime["answer_max_tokens"],
                            temperature=runtime["answer_temperature"])
    return {"id": query.qa_id, "status": "answered", "generated_answer": reply.text,
            "returned_model": reply.model, "reader_prompt": prompt,
            "reader_context": context, "prompt_tokens_counted": counted,
            "seconds": time.perf_counter() - started, **stats}
