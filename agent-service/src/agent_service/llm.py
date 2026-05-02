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
