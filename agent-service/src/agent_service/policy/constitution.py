"""Constitution gate. Second Pydantic AI agent that judges the
agent's prose output against the policy constitution and emits a
structured `ConstitutionVerdict`.

The constitution prompt lives in `prompts/policy_v4.txt` (verbatim
copy of the Rust prompt). The model returns one of three verdict
strings ("approve" | "retract" | "reject") plus a reason and a
structured extraction sidecar of numbers seen in the prose.

The Pydantic output type is a plain pydantic model (not a proto) so
Pydantic AI can derive a JSON schema for the LLM. The loop driver
maps the result into `multichain.wire.agent.v1.ConstitutionVerdict`
proto for ledger / wire emission.

Lenient parsing: the LLM occasionally adds extra fields or tweaks the
extraction shape. We accept what we can, log the rest, and never let
a malformed sidecar fail the gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent

from ..llm import policy_model

log = structlog.get_logger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "policy_v4.txt"


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


@dataclass
class _Deps:
    """Empty deps slot; the constitution agent has no tools or runtime
    context beyond the prompt content itself."""


def _system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def build_constitution_agent() -> Agent[_Deps, ConstitutionVerdict]:
    """Construct the policy gate agent. One model call per invocation;
    no tools; structured output via Pydantic AI's native output_type.

    The cheap policy model is hardcoded here (gpt-oss-20b free tier).
    Free-tier rate limits force sequential calls; the loop driver
    pipelines this after the primary agent completes, never in parallel."""
    return Agent(
        model=policy_model(),
        deps_type=_Deps,
        output_type=ConstitutionVerdict,
        system_prompt=_system_prompt(),
    )


# ---------------------------------------------------------------------------
# Public surface: helpers the loop driver calls per channel.
# ---------------------------------------------------------------------------


async def judge_claim(
    agent: Agent[_Deps, ConstitutionVerdict],
    *,
    headline: str,
    body_markdown: str,
    provenance_summary: list[dict],
) -> ConstitutionVerdict:
    """Judge a single Claim against the constitution. The user-side
    payload mirrors the Rust shape: `channel="claim"`, the Claim's
    headline + body, and a summary of provenance entries (kind + key
    values) so Rule 1 (provenance presence) and Rule 5 (citation
    discipline) have what they need."""
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
        result = await agent.run(user_prompt, deps=_Deps())
        return result.output
    except Exception as e:  # noqa: BLE001
        log.warning("constitution_claim_call_failed", error=str(e))
        # Soft-approve on detector failure so a flaky LLM call doesn't
        # nuke the agent's user-facing output. The structural gate has
        # already run by this point and provides the load-bearing
        # factuality check; constitution is the prose-judgment layer.
        return ConstitutionVerdict(verdict="approve", reason="constitution call failed; soft-approve")


async def judge_narrative(
    agent: Agent[_Deps, ConstitutionVerdict],
    *,
    text: str,
    same_turn_claims: list[dict],
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
        result = await agent.run(user_prompt, deps=_Deps())
        return result.output
    except Exception as e:  # noqa: BLE001
        log.warning("constitution_narrative_call_failed", error=str(e))
        return ConstitutionVerdict(verdict="approve", reason="constitution call failed; soft-approve")
