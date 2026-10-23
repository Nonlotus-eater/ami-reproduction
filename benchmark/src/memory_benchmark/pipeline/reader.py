from importlib import import_module
import time
from .context import count_prompt, normalize, pack, pack_ranked_prefix
from .aml_reader import POLICY as AML_POLICY, CONTEXT_TOKENS, validate_settings

MODULES = {"locomo_refined": "locomo_refined", "longmemeval_s": "longmemeval"}


def answer(query, evidence, speakers, client, tokenizer, runtime):
    started = time.perf_counter()
    evidence = normalize(evidence, runtime["top_k"])
    def render_context(value):
        if query.benchmark in MODULES:
            template = import_module(f"memory_benchmark.benchmarks.{MODULES[query.benchmark]}.prompts")
            return template.render(query, value, speakers)
        from ..benchmarks.public_contract import render
        return render(query, value, runtime["model"])
    processing = {}
    policy = runtime.get("answer_context_policy")
    if policy == AML_POLICY:
        validate_settings(runtime)
        context, prompt, stats = pack_ranked_prefix(
            evidence, tokenizer, render_context, runtime["answer_input_token_limit"])
        counted = stats.pop("prompt_tokens_counted")
        processing = {"answer_context_policy": policy, "reader_mode": "ranked_prefix",
                      "answer_max_tokens_requested": runtime["answer_max_tokens"],
                      "answer_input_token_limit": runtime["answer_input_token_limit"],
                      "model_context_tokens": CONTEXT_TOKENS,
                      "context_safety_margin": runtime["answer_safety_margin_tokens"]}
    else:
        context, stats = pack(evidence, tokenizer, runtime["reader_token_budget"])
        prompt = render_context(context)
        counted = tokenizer.count(prompt) if isinstance(prompt, str) else count_prompt(prompt, tokenizer)
        if runtime["answer_input_token_limit"] is not None and counted > runtime["answer_input_token_limit"]:
            raise ValueError("Rendered reader prompt exceeds the frozen input budget")
    if policy in {"model_window_v1", "model_window_v2"}:
        from .windowed_reader import generate
        reply, prompt, processing = generate(query, context, render_context, client, tokenizer, runtime)
        counted = processing.pop("prompt_tokens_counted")
    else:
        reply = client.complete(prompt, stage="answer", max_tokens=runtime["answer_max_tokens"],
                                temperature=runtime["answer_temperature"])
    return {"id": query.qa_id, "status": "answered", "generated_answer": reply.text,
            "returned_model": reply.model, "reader_prompt": prompt,
            "reader_context": context, "prompt_tokens_counted": counted,
            "seconds": time.perf_counter() - started, **stats, **processing}
