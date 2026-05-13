"""Constitution gate. Policy LLM call that judges the agent's prose
output against the constitution and emits a structured
`ConstitutionVerdict` ("approve" | "retract" | "reject" plus reason
and a numbers-extracted sidecar).

The constitution prompt lives in `prompts/policy_v4.txt` (verbatim
copy of the Rust prompt). The loop driver reads the verdict + reason
inline; the `gate.constitution` and `gate.narrative_constitution`
OTel spans carry the verdict as attributes (per ADR 13).

The gate is **stateless function calls** through
`agent_service.llm_runtime.runtime_call`, which dispatches between
codex (subscription auth) and pydantic-ai (free-tier Gemini) based on
`AGENT_DEFAULT_RUNTIME`. Same model family for primary + policy
under codex; same Gemini stack under pydantic-ai. No per-process
agent instance kept around: building a stateless judge call is sub-
millisecond and avoids the lifespan-build / per-turn-rebuild
gymnastics the previous shape needed for window + override
plumbing.

Lenient parsing: the strict-schema walker in `llm_runtime` ensures
codex's `outputSchema` accepts the pydantic shape; on pydantic-ai we
parse the first JSON object out of plain text. Either path,
`RuntimeCallParseError` surfaces as a soft-approve so a flaky LLM
call doesn't nuke the agent's user-facing output (the structural
gate has already run by this point and provides the load-bearing
factuality check).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from agent_service.llm_runtime import (
    RuntimeCallParseError,
    runtime_call,
)
from agent_service.prompts.composer import (
    DEFAULT_LIVE_WINDOW_SECS,
    substitute_live_window,
)

log = structlog.get_logger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "policy_v4.txt"

# Version pin for the gate.constitution span attribute. Derived from
# the prompt filename so a prompt swap (policy_v4.txt → policy_v5.txt)
# bumps this automatically; eval probes can pin to specific versions.
VERSION: str = _PROMPT_PATH.stem.removeprefix("policy_")


class _ExtractedNumberLLM(BaseModel):
    """Pydantic shape for one extracted number. Lenient on extra fields."""

    model_config = ConfigDict(extra="ignore")

    value: float
    unit_class: str = Field(description='"sol" | "count" | "community_id" | "raw"')
    phrase: str = ""


class _ExtractionLLM(BaseModel):
    """Sidecar structure the constitution prompt asks the LLM to emit."""

    model_config = ConfigDict(extra="ignore")

    narrative_numbers: list[_ExtractedNumberLLM] = Field(default_factory=list)
    claim_numbers: list[_ExtractedNumberLLM] = Field(default_factory=list)


class ConstitutionVerdict(BaseModel):
    """Structured response from the constitution agent. Mirror of the
    proto `ConstitutionVerdict`. Returned by `judge_*` functions; loop
    driver maps it onto the wire proto."""

    model_config = ConfigDict(extra="ignore")

    verdict: Literal["approve", "retract", "reject"]
    reason: str = ""
    extraction: _ExtractionLLM | None = None


def _system_prompt(live_window_secs: int = DEFAULT_LIVE_WINDOW_SECS) -> str:
    """Load `policy_v4.txt` and substitute the `${LIVE_WINDOW_HUMAN}`
    placeholder with the human string for `live_window_secs`.

    Shares the formatter with `prompts/composer.substitute_live_window`
    so the constitution gate's framing of "the agent analyzes a
    ${window} live window" stays in lockstep with the primary agent's
    "user is viewing the last ${window} of transfers." If the two
    drift, the gate retracts correct narratives on any non-default
    window.
    """
    return substitute_live_window(
        _PROMPT_PATH.read_text(encoding="utf-8"), live_window_secs
    )


def _soft_approve(reason: str) -> ConstitutionVerdict:
    """Soft-approve fallback when the LLM call or parse fails. The
    structural gate has already run; constitution is the prose-judgment
    layer, so a flaky LLM call should not nuke user-facing output."""
    return ConstitutionVerdict(verdict="approve", reason=reason)


async def judge_claim(
    *,
    headline: str,
    body_markdown: str,
    provenance_summary: list[dict],
    live_window_secs: int = DEFAULT_LIVE_WINDOW_SECS,
    llm_override: Any = None,
) -> ConstitutionVerdict:
    """Judge a single Claim against the constitution. The user-side
    payload mirrors the Rust shape: `channel="claim"`, the Claim's
    headline + body, and a summary of provenance entries (kind + key
    values) so Rule 1 (provenance presence) and Rule 5 (citation
    discipline) have what they need.

    `llm_override` is forwarded to the pydantic-ai runtime path's
    `make_model` call; on codex it is ignored (codex picks its model
    centrally via `CODEX_HELPER_MODEL`).
    """
    payload = {
        "channel": "claim",
        "claim": {
            "headline": headline,
            "body_markdown": body_markdown,
            "provenance_summary": provenance_summary,
        },
    }
    user_prompt = json.dumps(payload, separators=(",", ":"))
    try:
        verdict, _raw = await runtime_call(
            role="policy",
            system_prompt=_system_prompt(live_window_secs),
            user_prompt=user_prompt,
            output_model=ConstitutionVerdict,
            llm_override=llm_override,
            per_attempt_timeout_s=30.0,
        )
        return verdict
    except RuntimeCallParseError as e:
        log.warning(
            "constitution_claim_parse_failed",
            error=str(e)[:200],
            raw_first_200=e.raw_text[:200],
        )
        return _soft_approve("constitution parse failed; soft-approve")
    except Exception as e:  # noqa: BLE001
        log.warning("constitution_claim_call_failed", error=str(e))
        return _soft_approve("constitution call failed; soft-approve")


async def judge_narrative(
    *,
    text: str,
    same_turn_claims: list[dict],
    live_window_secs: int = DEFAULT_LIVE_WINDOW_SECS,
    llm_override: Any = None,
) -> ConstitutionVerdict:
    """Judge a Narrative against the constitution. Same call shape as
    `judge_claim` but `channel="narrative"` and the payload includes
    `same_turn_claims` so Rule 5 can validate citations against the
    Claims emitted earlier in the same turn."""
    payload = {
        "channel": "narrative",
        "payload": {"text": text},
        "same_turn_claims": same_turn_claims,
    }
    user_prompt = json.dumps(payload, separators=(",", ":"))
    try:
        verdict, _raw = await runtime_call(
            role="policy",
            system_prompt=_system_prompt(live_window_secs),
            user_prompt=user_prompt,
            output_model=ConstitutionVerdict,
            llm_override=llm_override,
            per_attempt_timeout_s=30.0,
        )
        return verdict
    except RuntimeCallParseError as e:
        log.warning(
            "constitution_narrative_parse_failed",
            error=str(e)[:200],
            raw_first_200=e.raw_text[:200],
        )
        return _soft_approve("constitution parse failed; soft-approve")
    except Exception as e:  # noqa: BLE001
        log.warning("constitution_narrative_call_failed", error=str(e))
        return _soft_approve("constitution call failed; soft-approve")
