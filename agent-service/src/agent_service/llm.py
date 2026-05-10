"""LLM provider wiring. Free-tier OpenRouter for the production
preset; LM Studio (or any OpenAI-compatible local server) as a dev
override.

`make_model` is the SINGLE gateway every agent and probe uses to
construct a Pydantic AI model. Every call route flows through this
one function: mocking it in tests blocks all upstream LLM traffic
across every agent the turn lifecycle drives, current and future,
with no per-agent override plumbing.

Provider config is data, not code. Adding a new provider (Anthropic
native, vLLM, whatever) is a new entry in `_PROVIDERS`, not a new
branch in this function. The two providers we ship today both speak
OpenAI-compatible REST, so both flow through `OpenAIProvider`
parameterized by `base_url` + `api_key`. We drop the OpenRouter-
specific provider class (and its attribution headers) since those
matter only for OpenRouter dashboard rankings, not for routing.

Per-request override: the agent loop reads `request.llm_override`
(a `LlmOverride` proto on `AgentRequest`) and threads the matching
`RoleOverride` to `make_model` for each agent it rebuilds that
turn. Empty / missing = production preset (env-driven OpenRouter,
identical to today). The dev builder view's Models section is the
only thing that populates the override; production frontend never
sets it, so prod traffic is unaffected.

Env-driven model ids stay required so production (and the eval
substrate) share one source of truth: `agent_service/evals/
schema.py` derives the eval-judge forbidden-family list from the
same env vars at YAML-load time (preference-leakage prevention,
ICLR 2026); silent defaults would desync those views.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

Role = Literal["primary", "policy", "judge"]
ProviderId = Literal["openrouter", "gemini", "local"]

_ROLE_TO_ENV: dict[Role, str] = {
    "primary": "AGENT_PRIMARY_MODEL",
    "policy": "AGENT_POLICY_MODEL",
    "judge": "EVAL_JUDGE_MODEL",
}


@dataclass(frozen=True)
class _ProviderCfg:
    """Resolved provider config: how to build an `OpenAIProvider` for
    this provider.

    `base_url`: literal URL the provider answers at.
    `api_key`: secret used to authenticate. For local servers (LM
        Studio) the value is a dummy because the openai client
        requires a non-empty string but the local server ignores it.
    """

    base_url: str
    api_key: str


def _api_key_required(env_var: str, *, purpose: str) -> str:
    value = os.environ.get(env_var, "")
    if not value:
        raise RuntimeError(
            f"{env_var} env var is required ({purpose}). "
            f"Set it in the project .env."
        )
    return value


def _required_model_id(env_var: str) -> str:
    """Read a model id from env, raise with a precise message if
    missing."""
    value = os.environ.get(env_var, "")
    if not value:
        raise RuntimeError(
            f"{env_var} env var is required. Set it in the project "
            ".env (see .env.example for current free-tier picks)."
        )
    return value


def _resolve_provider(provider_id: ProviderId) -> _ProviderCfg:
    """Resolve provider config at call time so env var changes take
    effect without a process restart (relevant for `LOCAL_LLM_BASE_URL`
    edits during dev iteration)."""
    if provider_id == "openrouter":
        return _ProviderCfg(
            base_url="https://openrouter.ai/api/v1",
            api_key=_api_key_required("AGENT_API_KEY", purpose="OpenRouter API key"),
        )
    if provider_id == "gemini":
        # Google's OpenAI-compatible shim sits at this base; the same
        # API key works for both Gemini (proprietary) and Gemma
        # (open weights served by Google) model ids. Verified
        # 2026-05 against gemma-4-31b-it. Tool-calling and JSON-
        # schema structured output both work through this shim,
        # though Gemma models embed `<thought>...</thought>` blocks
        # in `message.content` that pydantic_ai treats as part of
        # the assistant's reply; Gemini-2.5+ models put the same
        # reasoning in a separate `extra_content.google.thought`
        # field, so they're cleaner if `<thought>` leakage matters.
        return _ProviderCfg(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            api_key=_api_key_required(
                "GEMINI_API_KEY", purpose="Google Generative Language API key"
            ),
        )
    if provider_id == "local":
        return _ProviderCfg(
            base_url=os.environ.get(
                "LOCAL_LLM_BASE_URL", "http://host.docker.internal:1234/v1"
            ),
            # LM Studio ignores api_key; the openai client requires a
            # non-empty string. Any literal works.
            api_key="lm-studio",
        )
    raise RuntimeError(f"unknown provider id: {provider_id!r}")


def _normalize_provider(raw: str) -> ProviderId:
    """Permissive provider-id parsing. Empty / unrecognized values
    fall through to the production default (controlled by
    `AGENT_DEFAULT_PROVIDER`, fallback "openrouter" for backward
    compat). Matches the proto's wire contract: `RoleOverride.
    provider == ""` means "use the default for this role." Frontend
    extensions can introduce new provider names without breaking
    older backends; the worst case for a typo is "ignored, default
    applied."

    `AGENT_DEFAULT_PROVIDER` exists because the OpenRouter free tier
    became unusable on the primary role (queue-depth stalls hitting
    the 75s/attempt cap), and the natural reaction is to flip the
    operating default to a different provider (gemini today, perhaps
    something else tomorrow). Without this knob the only way to
    redirect every empty-override request is to lie in the per-role
    env model id (e.g. set `AGENT_PRIMARY_MODEL=gemma-4-31b-it` AND
    rewrite the hardcoded "openrouter" return below). The env var
    keeps the production default declarative.
    """
    if raw in ("local", "gemini", "openrouter"):
        return raw  # type: ignore[return-value]
    fallback = os.environ.get("AGENT_DEFAULT_PROVIDER", "").strip().lower()
    if fallback in ("local", "gemini", "openrouter"):
        return fallback  # type: ignore[return-value]
    return "openrouter"


def make_model(role: Role, *, override=None, model_id: str | None = None) -> Model:
    """Construct a Pydantic AI model for the given role.

    `role` selects the env var holding the production model id
    (AGENT_PRIMARY_MODEL / AGENT_POLICY_MODEL / EVAL_JUDGE_MODEL).

    `override` is an optional `multichain.wire.agent.v1.llm_pb2.
    RoleOverride`-shaped object (any object with `.provider: str`
    and `.model_id: str` attributes works; we duck-type to keep
    proto types out of the import surface here). Empty fields fall
    through to the env-driven default for that role.

    `model_id` is a direct model-id override used by the eval-judge
    probe to swap the judge per-case via case YAML; spec validator
    has already verified the family is not shared with any stage of
    the agent under test before the override reaches here. Mutually
    exclusive with `override` for the same field; the explicit
    `model_id` arg wins if both are set.
    """
    raw_provider = getattr(override, "provider", "") if override is not None else ""
    raw_model_id = getattr(override, "model_id", "") if override is not None else ""

    provider_id: ProviderId = _normalize_provider(raw_provider)

    resolved_model_id: str
    if model_id:
        resolved_model_id = model_id
    elif raw_model_id:
        # Override-supplied model id wins for any provider. Originally
        # we only honored this on the local path because the dev pain
        # was OpenRouter latency, not OpenRouter model picking. That
        # changed once we identified specific free-tier models (e.g.
        # `nvidia/nemotron-3-super-120b-a12b:free`) as the timeout
        # source: the builder view's Models section now lets a dev
        # pin a known-fast `:free` OpenRouter model per role, so the
        # path needs to honor `model_id` for openrouter too.
        resolved_model_id = raw_model_id
    else:
        resolved_model_id = _required_model_id(_ROLE_TO_ENV[role])

    cfg = _resolve_provider(provider_id)
    return OpenAIChatModel(
        resolved_model_id,
        provider=OpenAIProvider(base_url=cfg.base_url, api_key=cfg.api_key),
    )
