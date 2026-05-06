"""LLM provider wiring. Free-tier OpenRouter for primary (narrative)
agent, policy (constitution + repeat detector) agent, and eval-judge.

Per `agent_ship_state.md`, free-tier rate limits force sequential
calls.

Model ids are env-driven (AGENT_PRIMARY_MODEL, AGENT_POLICY_MODEL,
EVAL_JUDGE_MODEL) so production and the eval substrate share one
source of truth. `agent_service/evals/schema.py` derives the eval-
judge forbidden-family list from these vars at YAML load time
(preference-leakage prevention, ICLR 2026); swap a model in `.env`,
the validator picks it up on the next process start. No manual
sync between the agent's stage-model definitions and the eval
substrate's bias-prevention check.

All three env vars are required at first call; missing values
raise RuntimeError so misconfiguration fails fast instead of
producing confusing pydantic-ai errors deeper in the stack.
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


def _required_model_id(env_var: str) -> str:
    """Read a model id from env, raise with a precise message if
    missing. We require these explicitly rather than falling back
    to hardcoded defaults: the eval-judge forbidden-family list is
    derived from the same env vars, so silent defaults would
    desync production behavior from the validator's view of which
    families to ban."""
    value = os.environ.get(env_var, "")
    if not value:
        raise RuntimeError(
            f"{env_var} env var is required. Set it in the project "
            ".env (see .env.example for current free-tier picks)."
        )
    return value


def primary_model() -> OpenAIChatModel:
    """Default agent model. Free tier on OpenRouter; sequential calls
    only (do not fan out). Model id from AGENT_PRIMARY_MODEL."""
    return OpenAIChatModel(
        _required_model_id("AGENT_PRIMARY_MODEL"),
        provider=OpenRouterProvider(api_key=_api_key()),
    )


def policy_model() -> OpenAIChatModel:
    """Constitution / repeat-detector model. Model id from
    AGENT_POLICY_MODEL."""
    return OpenAIChatModel(
        _required_model_id("AGENT_POLICY_MODEL"),
        provider=OpenRouterProvider(api_key=_api_key()),
    )


def judge_model(model_id: str | None = None) -> OpenAIChatModel:
    """Eval-judge model. If `model_id` is None, falls back to the
    EVAL_JUDGE_MODEL env var; either way the spec validator
    (`LlmJudgeSpec._resolve_and_check_model`) has already verified
    the family is not shared with any stage of the agent under
    test, so we don't re-check here.

    Same OpenRouter provider as the agent's own calls, so the
    free-tier rate limit is shared. With per-call retry from
    `with_provider_retry` and infrequent llm_judge probe usage
    (per the ADR rule 'use sparingly, only where deterministic
    probes can't reach'), the shared limit has not been observed
    to throttle in practice. If it does, the right move is to
    rotate the judge model id to a less-loaded provider, not to
    add separate rate-limit infra."""
    resolved = model_id or _required_model_id("EVAL_JUDGE_MODEL")
    return OpenAIChatModel(
        resolved,
        provider=OpenRouterProvider(api_key=_api_key()),
    )
