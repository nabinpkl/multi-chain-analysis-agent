"""Span name + attribute key constants for agent-service domain spans.

Single source of truth so producer (loop_driver, primitive_client) and
consumer (eval probes in Ship 2, Langfuse-side filters, ad-hoc SQL)
import from one place. A rename here is a one-grep migration; a rename
inline in two files is a silent eval regression. Risk #6 in the
agent-observability plan calls this out explicitly.

Naming follows OTel convention: lowercase, dot-separated namespace,
verb-noun where natural. Attribute keys mirror their span's namespace
when it adds clarity (`gate.verdict`, not bare `verdict`).

Step B of Ship 1 (ADR 13) adds the 11 domain spans below on top of
the 3 GenAI semconv spans Pydantic AI emits for free
(`agent.run`, `gen_ai.chat`, `execute_tool`) and the FastAPI server
span. One synthetic root, `agent.turn`, wraps the loop body so the
4 turn-scoped attrs (session/thread/turn/run-type) live somewhere
queryable without per-span propagation gymnastics.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Span names (alphabetical within group)
# ---------------------------------------------------------------------------

# Turn-scope wrapper (synthetic root for the loop body).
AGENT_TURN: Final = "agent.turn"

# Gates.
GATE_PLACEHOLDER: Final = "gate.placeholder"
GATE_STRUCTURAL: Final = "gate.structural"
GATE_CONSTITUTION: Final = "gate.constitution"
GATE_NARRATIVE_CONSTITUTION: Final = "gate.narrative_constitution"

# Emission events (per-claim, per-narrative).
CLAIM_EMITTED: Final = "claim.emitted"
NARRATIVE_EMITTED: Final = "narrative.emitted"

# HTTP-shaped operations against the Rust data plane.
SNAPSHOT_LEASE: Final = "snapshot.lease"
PRIMITIVE_WALLET_PROFILE: Final = "primitive.wallet_profile"
PRIMITIVE_COMMUNITY_SUMMARY: Final = "primitive.community_summary"

# Repeat-path machinery.
REPEAT_DETECTION: Final = "repeat.detection"
TURN_DIFF: Final = "turn.diff"


# ---------------------------------------------------------------------------
# Attribute keys
# ---------------------------------------------------------------------------


class Attrs:
    """All custom attribute keys we emit. Pydantic AI's GenAI semconv
    keys (`gen_ai.system`, `gen_ai.usage.input_tokens`, etc) are
    handled by the framework; we only define our own here."""

    # Turn-scope (set on agent.turn so SQL can `WHERE root.session_id = X`).
    SESSION_ID: Final = "session.id"
    THREAD_ID: Final = "thread.id"
    TURN_INDEX: Final = "turn.index"
    RUN_TYPE: Final = "run.type"  # "production" | "eval" | "dev"

    # Gates (every gate.* span carries verdict + optional reason).
    GATE_VERDICT: Final = "gate.verdict"  # "approved" | "retracted" | "reject"
    GATE_REASON: Final = "gate.reason"
    GATE_BINDING_SIZE: Final = "gate.binding_size"  # structural only
    GATE_FAILED_CHIP: Final = "gate.failed_chip"  # structural only, if retract

    # Claim emission.
    CLAIM_ID: Final = "claim.id"
    CLAIM_KIND: Final = "claim.kind"
    CLAIM_HEADLINE: Final = "claim.headline"
    CLAIM_PROVENANCE_COUNT: Final = "claim.provenance_count"
    CLAIM_BODY_CHARS: Final = "claim.body_chars"
    CLAIM_VERDICT: Final = "claim.verdict"  # final verdict after all gates

    # Narrative emission.
    NARRATIVE_LENGTH_CHARS: Final = "narrative.length_chars"
    NARRATIVE_VERDICT: Final = "narrative.verdict"
    NARRATIVE_ASSEMBLED_PROVENANCE_COUNT: Final = "narrative.assembled_provenance_count"

    # Snapshot lease + primitives.
    SNAPSHOT_ID: Final = "snapshot.id"
    SNAPSHOT_DURATION_MS: Final = "snapshot.duration_ms"
    PRIMITIVE_DURATION_MS: Final = "primitive.duration_ms"
    PRIMITIVE_OUTPUT_DIGEST: Final = "primitive.output_digest"  # sha256-12 of body
    PRIMITIVE_INPUT_ADDR: Final = "primitive.input.addr"
    PRIMITIVE_INPUT_COMMUNITY_ID: Final = "primitive.input.community_id"

    # Repeat detector + diff.
    REPEAT_IS_REPEAT: Final = "repeat.is_repeat"
    REPEAT_OF_TURN: Final = "repeat.of_turn"
    REPEAT_REASON: Final = "repeat.reason"
    REPEAT_USER_WANTS_REFRESH: Final = "repeat.user_explicitly_wants_refresh"
    DIFF_CHANGED_COUNT: Final = "diff.changed_count"
    DIFF_UNCHANGED_COUNT: Final = "diff.unchanged_count"
    DIFF_PRIMITIVES_REPLAYED: Final = "diff.primitives_replayed"


# ---------------------------------------------------------------------------
# Verdict string conventions (so producer + consumer agree on enum values)
# ---------------------------------------------------------------------------

VERDICT_APPROVED: Final = "approved"
VERDICT_RETRACTED: Final = "retracted"
VERDICT_REJECT: Final = "reject"

# run.type values.
RUN_TYPE_PRODUCTION: Final = "production"
RUN_TYPE_EVAL: Final = "eval"
RUN_TYPE_DEV: Final = "dev"
