"""LLM provider wiring. Free-tier OpenRouter for both primary
(narrative) and policy (constitution) models.

Per `agent_ship_state.md`, free-tier rate limits force sequential
calls. Phase 0 only constructs the primary model; the policy model
arrives in Phase B.4.
"""

from __future__ import annotations

import os

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider


def _api_key() -> str:
    key = os.environ.get("AGENT_API_KEY")
    if not key:
        raise RuntimeError(
            "AGENT_API_KEY env var is required (OpenRouter API key). "
            "Set it in the project .env."
        )
    return key


def primary_model() -> OpenAIChatModel:
    """Default agent model. Free tier on OpenRouter; sequential calls
    only (do not fan out)."""
    return OpenAIChatModel(
        "nvidia/nemotron-3-super-120b-a12b:free",
        provider=OpenRouterProvider(api_key=_api_key()),
    )


def policy_model() -> OpenAIChatModel:
    """Constitution / repeat-detector model. Wired in Phase B.4. Phase
    0 does not call this."""
    return OpenAIChatModel(
        "openai/gpt-oss-20b:free",
        provider=OpenRouterProvider(api_key=_api_key()),
    )


def judge_model(model_id: str) -> OpenAIChatModel:
    """Eval-judge model. The model id comes from the case YAML
    (`LlmJudgeSpec.model`) so the operator can pick per-case. The
    spec validator rejects any id whose family matches a stage of
    the agent under test (preference leakage avoidance); we trust
    the validator and don't re-check here.

    Same OpenRouter provider as the agent's own calls, so the
    free-tier rate limit is shared. With per-call retry from
    `with_provider_retry` and infrequent llm_judge probe usage
    (per the ADR rule 'use sparingly, only where deterministic
    probes can't reach'), the shared limit has not been observed
    to throttle in practice. If it does, the right move is to
    rotate the judge model id to a less-loaded provider, not to
    add separate rate-limit infra."""
    return OpenAIChatModel(
        model_id,
        provider=OpenRouterProvider(api_key=_api_key()),
    )
