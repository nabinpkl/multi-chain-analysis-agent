"""LLM provider wiring. Free-tier OpenRouter for primary (narrative)
agent, policy (constitution + repeat detector) agent, and eval-judge.

Per `agent_ship_state.md`, free-tier rate limits force sequential
calls.

`make_model` is the SINGLE gateway every agent and probe uses to
construct a Pydantic AI model. All OpenRouter traffic flows through
this one function. Tests monkeypatch `agent_service.llm.make_model`
with a `TestModel`-returning stub so future agents added to the
turn lifecycle inherit the no-live-LLM guarantee for free; no
per-agent override plumbing needed.

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
from typing import Literal

from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

Role = Literal["primary", "policy", "judge"]

_ROLE_TO_ENV: dict[Role, str] = {
    "primary": "AGENT_PRIMARY_MODEL",
    "policy": "AGENT_POLICY_MODEL",
    "judge": "EVAL_JUDGE_MODEL",
}


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


def make_model(role: Role, *, model_id: str | None = None) -> Model:
    """Construct a Pydantic AI model for the given role. The single
    gate every agent and probe must enter; mocking this one function
    in tests blocks all OpenRouter traffic across every agent the
    turn lifecycle drives, current and future.

    `role` selects the env var holding the production model id
    (AGENT_PRIMARY_MODEL / AGENT_POLICY_MODEL / EVAL_JUDGE_MODEL).
    `model_id` overrides the env-var lookup; used by the eval-judge
    probe to swap the judge per-case via case YAML. The spec
    validator (`LlmJudgeSpec._resolve_and_check_model`) already
    verified the family is not shared with any stage of the agent
    under test before the override reaches here.
    """
    resolved = model_id or _required_model_id(_ROLE_TO_ENV[role])
    return OpenAIChatModel(
        resolved,
        provider=OpenRouterProvider(api_key=_api_key()),
    )
