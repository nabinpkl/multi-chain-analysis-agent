"""Span name + attribute key constants for agent-service domain spans.

Single source of truth so producer (loop_driver, primitive_client) and
consumer (eval probes in Ship 2, Langfuse-side filters, ad-hoc SQL)
import from one place. A rename here is a one-grep migration; a rename
inline in two files is a silent eval regression.

All domain span names + attribute keys are namespaced under `mcae.*`.
That prefix is our private contract. Eval probes assert against it,
external readers can ignore everything outside it. When OTel GenAI
semconv stabilizes for concepts we currently model under `mcae.*`
(e.g. tool I/O, agent steps), the alias layer lives here, not in
call sites.

Two attrs intentionally stay outside `mcae.*`: `session.id` and
`thread.id`. Both are cross-cutting OTel/OpenInference conventions
that downstream tools (Langfuse session grouping, future Phoenix)
read directly. Renaming them under `mcae.*` would cost free
session-aware UI for nothing.

Step B of Ship 1 (ADR 13) added the domain spans below on top of
the GenAI semconv spans Pydantic AI emits for free (`agent run`,
`chat <model>`, `running tool`) and the FastAPI server span. One
synthetic root, `mcae.turn`, wraps the loop body so turn-scope
attrs (session/thread/turn-index/run-type) live somewhere queryable
without per-span propagation gymnastics.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Span names (alphabetical within group)
# ---------------------------------------------------------------------------

# Turn-scope wrapper (synthetic root for the loop body).
AGENT_TURN: Final = "mcae.turn"

# Gates.
GATE_PLACEHOLDER: Final = "mcae.gate.placeholder"
GATE_STRUCTURAL: Final = "mcae.gate.structural"
GATE_CONSTITUTION: Final = "mcae.gate.constitution"
GATE_NARRATIVE_CONSTITUTION: Final = "mcae.gate.narrative_constitution"

# Emission events (per-claim, per-narrative).
CLAIM_EMITTED: Final = "mcae.claim.emitted"
NARRATIVE_EMITTED: Final = "mcae.narrative.emitted"

# HTTP-shaped operations against the Rust data plane.
SNAPSHOT_LEASE: Final = "mcae.snapshot.lease"
PRIMITIVE_WALLET_PROFILE: Final = "mcae.primitive.wallet_profile"
PRIMITIVE_COMMUNITY_SUMMARY: Final = "mcae.primitive.community_summary"

# Repeat-path machinery.
REPEAT_DETECTION: Final = "mcae.repeat.detection"
TURN_DIFF: Final = "mcae.turn.diff"


# ---------------------------------------------------------------------------
# Attribute keys
# ---------------------------------------------------------------------------


class Attrs:
    """All custom attribute keys we emit. Pydantic AI's GenAI semconv
    keys (`gen_ai.system`, `gen_ai.usage.input_tokens`, etc) are
    handled by the framework; we only define our own here.

    Convention: domain attrs are prefixed `mcae.<namespace>.<field>`.
    Cross-cutting standard attrs (`session.id`, `thread.id`) are
    intentionally bare so downstream OTel-aware tools index them.
    """

    # Cross-cutting standards (NOT `mcae.*`-prefixed by design).
    SESSION_ID: Final = "session.id"
    THREAD_ID: Final = "thread.id"

    # Turn-scope (set on mcae.turn so SQL can `WHERE root.mcae.turn.* = X`).
    TURN_INDEX: Final = "mcae.turn.index"
    TURN_USER_QUESTION: Final = "mcae.turn.user_question"
    TURN_TOOL_CALLS: Final = "mcae.turn.tool_calls"
    TURN_CLAIMS_EMITTED: Final = "mcae.turn.claims_emitted"
    TURN_CLAIMS_APPROVED: Final = "mcae.turn.claims_approved"
    TURN_NARRATIVE_CHARS: Final = "mcae.turn.narrative_chars"
    RUN_TYPE: Final = "mcae.run.type"  # "production" | "eval" | "dev"

    # Gates (every mcae.gate.* span carries verdict + optional reason
    # and a version pin so eval probes can assert "constitution v4
    # passed", not just "the constitution gate passed today").
    GATE_VERDICT: Final = "mcae.gate.verdict"  # "approved" | "retracted" | "reject"
    GATE_REASON: Final = "mcae.gate.reason"
    GATE_VERSION: Final = "mcae.gate.version"
    GATE_BINDING_SIZE: Final = "mcae.gate.binding_size"  # structural only
    GATE_FAILED_CHIP: Final = "mcae.gate.failed_chip"  # structural only, if retract

    # Claim emission.
    CLAIM_ID: Final = "mcae.claim.id"
    CLAIM_KIND: Final = "mcae.claim.kind"
    CLAIM_HEADLINE: Final = "mcae.claim.headline"
    CLAIM_PROVENANCE_COUNT: Final = "mcae.claim.provenance_count"
    CLAIM_BODY_CHARS: Final = "mcae.claim.body_chars"
    CLAIM_VERDICT: Final = "mcae.claim.verdict"  # final verdict after all gates
    CLAIM_SOURCE_KIND: Final = "mcae.claim.source_kind"  # "primitive" | "exploratory"

    # Narrative emission.
    NARRATIVE_LENGTH_CHARS: Final = "mcae.narrative.length_chars"
    NARRATIVE_VERDICT: Final = "mcae.narrative.verdict"
    NARRATIVE_ASSEMBLED_PROVENANCE_COUNT: Final = "mcae.narrative.assembled_provenance_count"

    # Snapshot lease + primitives.
    SNAPSHOT_ID: Final = "mcae.snapshot.id"
    SNAPSHOT_DURATION_MS: Final = "mcae.snapshot.duration_ms"
    PRIMITIVE_DURATION_MS: Final = "mcae.primitive.duration_ms"
    PRIMITIVE_OUTPUT_DIGEST: Final = "mcae.primitive.output_digest"  # sha256-12 of body
    PRIMITIVE_INPUT_ADDR: Final = "mcae.primitive.input.addr"
    PRIMITIVE_INPUT_COMMUNITY_ID: Final = "mcae.primitive.input.community_id"
    # Full JSON payloads on primitive spans. Typed input attrs above
    # stay because they are cheap to query in SQL; these are the rich
    # debug surface (Langfuse renders them inline) and the future eval
    # probe target for `tool_returned_field(metric, value)`. Both are
    # capped to PRIMITIVE_PAYLOAD_MAX_BYTES; on overflow the value
    # ends with the literal " ...[truncated, total=N]".
    PRIMITIVE_INPUT: Final = "mcae.primitive.input"
    PRIMITIVE_OUTPUT: Final = "mcae.primitive.output"

    # Repeat detector + diff.
    REPEAT_IS_REPEAT: Final = "mcae.repeat.is_repeat"
    REPEAT_OF_TURN: Final = "mcae.repeat.of_turn"
    REPEAT_REASON: Final = "mcae.repeat.reason"
    REPEAT_USER_WANTS_REFRESH: Final = "mcae.repeat.user_explicitly_wants_refresh"
    DIFF_CHANGED_COUNT: Final = "mcae.diff.changed_count"
    DIFF_UNCHANGED_COUNT: Final = "mcae.diff.unchanged_count"
    DIFF_PRIMITIVES_REPLAYED: Final = "mcae.diff.primitives_replayed"


# Per-attribute byte cap on the JSON payloads attached to primitive
# spans. 8 KiB is large enough for the wallet_profile envelope (one
# wallet, top counterparties) without bloating trace storage. Probes
# that need full payloads can re-fetch via the snapshot id.
PRIMITIVE_PAYLOAD_MAX_BYTES: Final = 8192


# ---------------------------------------------------------------------------
# Verdict string conventions (so producer + consumer agree on enum values)
# ---------------------------------------------------------------------------

VERDICT_APPROVED: Final = "approved"
VERDICT_RETRACTED: Final = "retracted"
VERDICT_REJECT: Final = "reject"

# mcae.run.type values.
RUN_TYPE_PRODUCTION: Final = "production"
RUN_TYPE_EVAL: Final = "eval"
RUN_TYPE_DEV: Final = "dev"

# mcae.claim.source_kind values.
#
# Trust-model anchor for the structural value gate. Today every claim
# is "primitive" because the only evidence-gathering tools are typed
# primitives (wallet_profile, community_summary) whose envelopes feed
# the PrimitiveBindingStore. When the planned sql_explore tool ships,
# claims grounded in raw SQL rows will be marked "exploratory" and
# the constitution gate hedges their prose; the structural gate will
# refuse to anchor numbers from exploratory sources. Defining the
# enum now lets the eval probe `claim_grounded_in(source_kind=...)`
# exist before sql_explore does, avoiding a migration.
SOURCE_KIND_PRIMITIVE: Final = "primitive"
SOURCE_KIND_EXPLORATORY: Final = "exploratory"
