"""Explicit credential file, normalized model settings, public identity only."""
from pathlib import Path
from urllib.parse import urlsplit
from .embedding import settings, STRATEGY
from .retry import RetryPolicy

ALLOWED_MODELS = {"gpt-4o-mini": {"gpt-4o-mini", "gpt-4o-mini-2024-07-18"}}
# Model-native ceiling, used when Answer has no additional benchmark output cap.
# https://developers.openai.com/api/docs/models/gpt-4o-mini
MODEL_OUTPUT_TOKEN_LIMITS = {"gpt-4o-mini": 16384}
MODEL_CONTEXT_TOKEN_LIMITS = {"gpt-4o-mini": 128000}


def read_profile(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            raise ValueError("Invalid env configuration line")
        key, value = line.split("=", 1)
        value = value.strip()
        if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def validate_profile(profile, model):
    if model not in ALLOWED_MODELS:
        raise ValueError("Unsupported model contract")
    for name in ("OPENAI_BASE_URL", "AMI_EMBED_BASE_URL"):
        value = profile.get(name, "")
        u = urlsplit(value)
        if (u.scheme != "https" or not u.hostname or u.username or u.password or u.query
                or u.fragment or any(c in value for c in "{}<>")):
            raise ValueError("Invalid configured model endpoint")
    if not profile.get("OPENAI_API_KEY") or not profile.get("AMI_EMBED_API_KEY"):
        raise ValueError("Missing configured model credential")
    if profile.get("AMI_LLM_MODEL", model) != model:
        raise ValueError("Private profile model differs from frozen runtime")
    settings(profile)
    return profile


def public_identity(profile, runtime):
    dimensions, batch = settings(profile)
    identity = {"model": runtime["model"], "llm_base_url": profile["OPENAI_BASE_URL"],
            "embedding_base_url": profile["AMI_EMBED_BASE_URL"],
            "embedding_model": profile.get("AMI_EMBED_MODEL", "text-embedding-v4"),
            "embedding_dimensions": dimensions, "embedding_batch": batch,
            "embedding_preprocess": STRATEGY, "embedding_query_preprocess": "identity",
            "response_model_allowlist": sorted(ALLOWED_MODELS[runtime["model"]])}
    # Old frozen runs record retry-policy changes in their execution overlay.
    # Preserve their provider/store identity so existing checkpoints still load.
    if "max_retries" in runtime:
        identity["request_retry_policy"] = RetryPolicy.from_profile(profile, runtime["max_retries"]).public()
    return identity
