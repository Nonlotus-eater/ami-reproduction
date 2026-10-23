"""Versioned implementation of AML's public Answer context-budget rules.

Source: https://agentmemories.ai/api-guide (Runtime Rules, checked 2026-10-23).
This aligns public limits; it does not claim identical private Adapter behavior.
"""
from ..runtime.profile import MODEL_CONTEXT_TOKEN_LIMITS

POLICY = "aml_ranked_prefix_v1"
TOP_K = 100
CONTEXT_TOKENS = 128000
INPUT_TOKENS = 117760
OUTPUT_TOKENS = 8192
SAFETY_TOKENS = 2048


def validate_settings(runtime):
    expected = {
        "top_k": TOP_K,
        "answer_input_token_limit": INPUT_TOKENS,
        "answer_max_tokens": OUTPUT_TOKENS,
        "answer_safety_margin_tokens": SAFETY_TOKENS,
    }
    for name, value in expected.items():
        if type(runtime.get(name)) is not int or runtime[name] != value:
            raise ValueError(f"{POLICY} requires {name}={value}")
    if "reader_token_budget" not in runtime or runtime["reader_token_budget"] is not None:
        raise ValueError(f"{POLICY} budgets the complete prompt; reader_token_budget must be null")
    if runtime.get("answer_output_policy") != "fixed_aml_budget":
        raise ValueError(f"{POLICY} requires answer_output_policy=fixed_aml_budget")
    if runtime.get("answer_part_truncation_policy") is not None:
        raise ValueError(f"{POLICY} uses ranked evidence prefixes instead of summary splitting")
    if (MODEL_CONTEXT_TOKEN_LIMITS.get(runtime.get("model")) != CONTEXT_TOKENS or
            INPUT_TOKENS + OUTPUT_TOKENS + SAFETY_TOKENS != CONTEXT_TOKENS):
        raise ValueError("AML Answer requires the published 128000-token context window")
