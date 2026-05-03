"""Ship 4 repeat detector. Pre-loop small LLM gate that judges whether
the new user message is a FULL REPEAT of any prior turn in the same
thread. Drives the loop's branch into the incremental-answer path
(replay turn N's primitives, diff against captured outputs, narrate
only what changed) vs the normal main loop.

Direct port of `backend/src/agent/repeat_detector.rs`. Same system
prompt, same JSON schema, same fall-through behavior on parse failure.

Runs only when `AgentSwitches.dont_repeat_yourself = true`. Failure
modes (timeout, parse failure, empty history) all return
`RepeatDetectorOutcome.no_repeat(...)` so detection is opportunistic
and never breaks the turn.

Uses the cheap policy model via Pydantic AI's structured-output path.
~100-150ms extra per turn when the switch is on; zero when off.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent

from .llm import policy_model

log = structlog.get_logger(__name__)


_REPEAT_SYSTEM = """You are a repeat-detection classifier. Given the user's prior questions in this conversation (as a list of turn_id: question) and a NEW user message, decide whether the new message is a FULL REPEAT of any prior turn.

A REPEAT means SAME FOCUS (same wallet, community, or entity) AND SAME INTENT (asking for the same kind of analysis, e.g. profile, summary).

NOT a repeat:
- Different focus (different wallet/community/entity)
- Partial overlap (asking about a sub-aspect of a prior answer, e.g. "what is its biggest counterparty?" after "tell me about wallet X")
- Different intent on the same focus (e.g. interpretation vs structured profile)

Special case: if the user explicitly asks for a refresh ("refresh", "again", "tell me again about X", "what's the latest on X"), set user_explicitly_wants_refresh=true REGARDLESS of repeat detection. The downstream loop uses this to bypass the incremental path even when a repeat would otherwise fire.

Reply with ONLY valid JSON, no prose, no code fences. Schema:
{
  "repeat_of_turn": <integer turn_id from the prior list, or null if not a repeat>,
  "user_explicitly_wants_refresh": <true | false>,
  "reason": "<one short sentence explaining your decision>"
}"""


class _RepeatJudgement(BaseModel):
    """Pydantic shape Pydantic AI extracts from the LLM's JSON output."""

    model_config = ConfigDict(extra="ignore")

    repeat_of_turn: int | None = None
    user_explicitly_wants_refresh: bool = False
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RepeatDetectorOutcome:
    """Outcome of a single detection pass. `repeat_of_turn` is the
    validated turn id (validated to exist in the prior questions map);
    callers can trust it as a key into thread state. `reason` is human-
    readable, surfaced in the path trace's note."""

    repeat_of_turn: int | None
    reason: str
    user_explicitly_wants_refresh: bool

    @classmethod
    def no_repeat(cls, reason: str) -> "RepeatDetectorOutcome":
        return cls(repeat_of_turn=None, reason=reason, user_explicitly_wants_refresh=False)


def build_repeat_agent() -> Agent[None, _RepeatJudgement]:
    """Construct the repeat detector agent. Cheap policy model, no
    tools, structured output."""
    return Agent(
        model=policy_model(),
        output_type=_RepeatJudgement,
        system_prompt=_REPEAT_SYSTEM,
    )


def _format_user_prompt(prior: dict[int, str], new: str) -> str:
    """Stable ordering by turn id so the model sees chronological
    order even though the dict is unordered."""
    sorted_items = sorted(prior.items(), key=lambda kv: kv[0])
    history = "\n".join(f"{t}: {q}" for t, q in sorted_items)
    # Use json.dumps to escape the new message so embedded quotes don't
    # break the prompt grammar. Mirror of Rust's `{:?}` debug formatting.
    new_json = json.dumps(new)
    return (
        f"Prior turns (turn_id: question):\n{history}\n\n"
        f"New message: {new_json}\n\nRespond with the JSON."
    )


async def detect_repeat(
    prior_questions: dict[int, str],
    new_user_msg: str,
    agent: Agent[None, _RepeatJudgement],
) -> RepeatDetectorOutcome:
    """Run the detector. Returns `no_repeat(...)` on any failure (LLM
    error, parse failure, empty history). The downstream loop uses
    `repeat_of_turn is not None` as the only branch signal; missed
    repeats fall through to the normal main loop with no behavioral
    cost."""
    if not prior_questions:
        return RepeatDetectorOutcome.no_repeat("no prior turns in thread")

    user_prompt = _format_user_prompt(prior_questions, new_user_msg)

    try:
        result = await agent.run(user_prompt)
    except Exception as e:  # noqa: BLE001
        log.warning("repeat_detector_call_failed", error=str(e))
        return RepeatDetectorOutcome.no_repeat("detector call failed")

    raw = result.output
    # Validate: the model may return a turn id that doesn't exist
    # (hallucinated). Only trust it if it indexes a real prior turn.
    validated = (
        raw.repeat_of_turn
        if raw.repeat_of_turn is not None and raw.repeat_of_turn in prior_questions
        else None
    )

    if raw.reason:
        reason = raw.reason
    elif validated is not None:
        reason = "repeat detected"
    else:
        reason = "no repeat"

    return RepeatDetectorOutcome(
        repeat_of_turn=validated,
        reason=reason,
        user_explicitly_wants_refresh=raw.user_explicitly_wants_refresh,
    )
