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
PRIMITIVE_GET_TOKEN_INFO: Final = "mcae.primitive.get_token_info"

# Synthesized for any codex tool call that is NOT one of the four
# MCAE MCP tools (`wallet_profile`, `community_summary`,
# `get_token_info`, `emit_claims`). Built-in tool surfaces (shell,
# unified_exec, apply_patch, web_search, view_image, image_generation,
# computer_use, browser_use, tool_search, apps) are disabled in the
# per-actor config.toml via `CodexAgentProfile.builtin_tools=frozenset()`;
# this span exists so an eval probe can assert the lockdown stayed
# in place. A passing turn never emits this span; a regression that
# unlocks builtins makes it visible.
CODEX_TOOL_BUILTIN: Final = "mcae.codex.tool.builtin"

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
    # True when the per-turn tool-call budget interceptor fired at
    # least once this turn (i.e. the model tried to dispatch a lookup
    # past the cap and received a no_more_lookups_this_turn tool
    # result instead of the primitive output). Probes assert this on
    # the runaway_tool_call_loop case to verify the interceptor
    # engaged and the model recovered gracefully.
    TURN_BUDGET_EXHAUSTED: Final = "mcae.turn.budget_exhausted"
    TURN_CLAIMS_EMITTED: Final = "mcae.turn.claims_emitted"
    TURN_CLAIMS_APPROVED: Final = "mcae.turn.claims_approved"
    TURN_NARRATIVE_CHARS: Final = "mcae.turn.narrative_chars"
    RUN_TYPE: Final = "mcae.run.type"  # "production" | "eval" | "dev"

    # Topical-rail rejection (set when a turn short-circuits before
    # agent.run() because the user question hit `reject_if_unsafe_user
    # _question`). Probes can assert `unsafe_input_rejected = "true"`
    # to gate-test the boundary defense without needing to query
    # narrative content. See boundary.py and #33 for context.
    TURN_UNSAFE_INPUT_REJECTED: Final = "mcae.turn.unsafe_input_rejected"
    TURN_UNSAFE_INPUT_PATTERN: Final = "mcae.turn.unsafe_input_pattern"

    # Channel cockpit instruments. One attribute per ChannelSwitches
    # field, stamped on the mcae.turn span at turn start. Probes
    # assert `mcae.turn.channels.narrative_output_enabled = "false"`
    # to gate-test the off-state path without needing to inspect
    # SSE bytes. Keys mirror the proto field names so a future
    # field add maps 1-to-1.
    TURN_CHANNEL_NARRATIVE_OUTPUT_ENABLED: Final = (
        "mcae.turn.channels.narrative_output_enabled"
    )
    TURN_CHANNEL_EXTERNAL_TEXT_INPUT_ENABLED: Final = (
        "mcae.turn.channels.external_text_input_enabled"
    )

    # Narrative suppression (set on `mcae.narrative.emitted` when
    # the narrative-output channel is off). Pairs with
    # NARRATIVE_LENGTH_CHARS=0 so a probe can assert "model wrote
    # text but we suppressed it" vs "model wrote nothing" without
    # ambiguity.
    NARRATIVE_SUPPRESSED: Final = "mcae.narrative.suppressed"
    NARRATIVE_PRE_SUPPRESSION_CHARS: Final = (
        "mcae.narrative.pre_suppression_chars"
    )

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
    # Full narrative text. Capped to NARRATIVE_TEXT_MAX_BYTES so
    # OTel attribute storage stays bounded; on overflow the value
    # ends with " ...[truncated, total=N]" matching the convention
    # used by the primitive payload caps. Lets eval-judge probes
    # (and Langfuse) read the actual prose without an extra fetch.
    NARRATIVE_TEXT: Final = "mcae.narrative.text"

    # Snapshot lease + primitives.
    SNAPSHOT_ID: Final = "mcae.snapshot.id"
    SNAPSHOT_DURATION_MS: Final = "mcae.snapshot.duration_ms"
    # Live-window seconds the snapshot was materialized against, as
    # resolved by the data plane (`SnapshotBeginResponse.window_secs`).
    # Stamped on the lease span + the turn root so OTel queries can
    # filter / group by window without correlating against the inbound
    # request body.
    SNAPSHOT_WINDOW_SECS: Final = "mcae.snapshot.window_secs"
    PRIMITIVE_DURATION_MS: Final = "mcae.primitive.duration_ms"
    PRIMITIVE_OUTPUT_DIGEST: Final = "mcae.primitive.output_digest"  # sha256-12 of body
    PRIMITIVE_INPUT_ADDR: Final = "mcae.primitive.input.addr"
    PRIMITIVE_INPUT_COMMUNITY_ID: Final = "mcae.primitive.input.community_id"
    PRIMITIVE_INPUT_MINT: Final = "mcae.primitive.input.mint"
    PRIMITIVE_GET_TOKEN_INFO_SOURCE: Final = "mcae.primitive.get_token_info.source_program"
    # Set true on the current tool span when the
    # `external_text_input_enabled` channel switch is off and
    # `get_token_info`'s name/symbol/uri were replaced with the
    # redaction placeholder before being wrapped in <external_data>.
    # Eval probes assert this attribute to verify the gate held.
    PRIMITIVE_GET_TOKEN_INFO_SANITIZED: Final = (
        "mcae.primitive.get_token_info.sanitized"
    )
    # Full JSON payloads on primitive spans. Typed input attrs above
    # stay because they are cheap to query in SQL; these are the rich
    # debug surface (Langfuse renders them inline) and the future eval
    # probe target for `tool_returned_field(metric, value)`. Both are
    # capped to PRIMITIVE_PAYLOAD_MAX_BYTES; on overflow the value
    # ends with the literal " ...[truncated, total=N]".
    PRIMITIVE_INPUT: Final = "mcae.primitive.input"
    PRIMITIVE_OUTPUT: Final = "mcae.primitive.output"

    # Built-in tool spans (`mcae.codex.tool.builtin`). Namespaced
    # separately from `mcae.primitive.*` so probes that filter by
    # span name keep clean semantics. `name` is the tool identifier
    # the model invoked (e.g. "shell", "web_search"); the other
    # attrs mirror the primitive span shape so debuggers see the
    # same fields.
    CODEX_TOOL_NAME: Final = "mcae.codex.tool.name"
    CODEX_TOOL_INPUT: Final = "mcae.codex.tool.input"
    CODEX_TOOL_OUTPUT: Final = "mcae.codex.tool.output"
    CODEX_TOOL_DURATION_MS: Final = "mcae.codex.tool.duration_ms"

    # Repeat detector + diff.
    REPEAT_IS_REPEAT: Final = "mcae.repeat.is_repeat"
    REPEAT_OF_TURN: Final = "mcae.repeat.of_turn"
    REPEAT_REASON: Final = "mcae.repeat.reason"
    REPEAT_USER_WANTS_REFRESH: Final = "mcae.repeat.user_explicitly_wants_refresh"
    DIFF_CHANGED_COUNT: Final = "mcae.diff.changed_count"
    DIFF_UNCHANGED_COUNT: Final = "mcae.diff.unchanged_count"
    DIFF_PRIMITIVES_REPLAYED: Final = "mcae.diff.primitives_replayed"

    # Codex runtime observability. Stamped on the `mcae.turn` root
    # span when `runtime=codex`. Together they answer two questions
    # one trace at a time:
    #
    #   1. Did the prompt cache stay continuous across this thread?
    #      Compare `sent` (id we resumed) vs `received` (id codex
    #      emitted back). On a successful resume the two match;
    #      mismatch (or `sent=""` past turn 0) means codex silently
    #      forked a fresh thread and the cache split.
    #
    #   2. What did this turn actually cost? Codex's
    #      `CodexTokenUsage` carries a `.last` breakdown (this turn)
    #      and a `.total` breakdown (thread-cumulative). We stamp
    #      `last.*` for per-turn cost analysis and `total.*` for
    #      thread-cumulative budgeting. `cache_hit_rate` is the
    #      ratio `cached_input / input` from the `.last` breakdown;
    #      a healthy resumed turn should sit near 1.0.
    #
    # All keys are bare scalars (no JSON), so SQL queries on
    # otel_traces / Langfuse can aggregate directly.
    CODEX_PROVIDER_THREAD_ID_SENT: Final = (
        "codex.provider_thread_id.sent"
    )
    CODEX_PROVIDER_THREAD_ID_RECEIVED: Final = (
        "codex.provider_thread_id.received"
    )
    CODEX_TOKENS_LAST_TOTAL: Final = "codex.tokens.last.total"
    CODEX_TOKENS_LAST_INPUT: Final = "codex.tokens.last.input"
    CODEX_TOKENS_LAST_CACHED_INPUT: Final = (
        "codex.tokens.last.cached_input"
    )
    CODEX_TOKENS_LAST_OUTPUT: Final = "codex.tokens.last.output"
    CODEX_TOKENS_LAST_REASONING: Final = "codex.tokens.last.reasoning"
    CODEX_TOKENS_TOTAL_TOTAL: Final = "codex.tokens.total.total"
    CODEX_TOKENS_TOTAL_INPUT: Final = "codex.tokens.total.input"
    CODEX_TOKENS_TOTAL_CACHED_INPUT: Final = (
        "codex.tokens.total.cached_input"
    )
    CODEX_TOKENS_TOTAL_OUTPUT: Final = "codex.tokens.total.output"
    CODEX_TOKENS_TOTAL_REASONING: Final = "codex.tokens.total.reasoning"
    # Float in [0.0, 1.0]; 0.0 = no cache hit, 1.0 = fully cached.
    # NaN-safe sentinel  -1.0  for turns whose input_tokens==0
    # (rare, but happens on metadata-only turns).
    CODEX_CACHE_HIT_RATE: Final = "codex.cache_hit_rate"
    # Model context window codex reports for this turn. Useful for
    # eval-side "how close are we to the cap" questions; stamped
    # only when the codex event carries it.
    CODEX_MODEL_CONTEXT_WINDOW: Final = "codex.model_context_window"

    # ----- OpenTelemetry GenAI semantic-convention bridge -----
    #
    # Langfuse auto-converts any OTel span carrying `gen_ai.*` keys
    # into a "GENERATION" observation. That observation type is what
    # Langfuse uses to compute `totalCost` from its model-pricing
    # table AND to render the token-count column on the traces list.
    # Without these attrs, codex turns show up as plain spans with
    # `totalCost: 0` and no token column populated.
    #
    # Pydantic AI's `instrument_all(use_aggregated_usage_attribute
    # _names=True)` already stamps the same keys on its own LLM call
    # spans (`otel.py:107`). We mirror those exact key names on the
    # codex turn-root span so Langfuse treats both runtimes
    # uniformly  same trace shape, same usage column, same cost
    # calculation.
    #
    # Stamped from `CodexTokenUsage.last` (per-turn breakdown) so
    # one OTel span = one turn = one generation observation. The
    # `.total` (thread-cumulative) breakdown stays on the
    # `codex.tokens.total.*` keys above for our own SQL/eval
    # aggregations; it's not propagated into the gen_ai keys
    # because Langfuse expects them to be turn-scoped.
    GEN_AI_SYSTEM: Final = "gen_ai.system"
    GEN_AI_REQUEST_MODEL: Final = "gen_ai.request.model"
    GEN_AI_RESPONSE_MODEL: Final = "gen_ai.response.model"
    GEN_AI_USAGE_INPUT_TOKENS: Final = "gen_ai.usage.input_tokens"
    GEN_AI_USAGE_OUTPUT_TOKENS: Final = "gen_ai.usage.output_tokens"
    GEN_AI_USAGE_TOTAL_TOKENS: Final = "gen_ai.usage.total_tokens"
    # Langfuse extension key for prompt-cache hits. Maps to
    # `usage_details.cache_read_input_tokens` on the Langfuse
    # generation observation, which is how the dashboard renders
    # the "cached" segment of the input-token bar. Codex reports
    # this on every TOKEN_USAGE_UPDATED event as
    # `last.cached_input_tokens` (subset of `last.input_tokens`).
    GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS: Final = (
        "gen_ai.usage.cache_read_input_tokens"
    )


# Per-attribute byte cap on the JSON payloads attached to primitive
# spans. 8 KiB is large enough for the wallet_profile envelope (one
# wallet, top counterparties) without bloating trace storage. Probes
# that need full payloads can re-fetch via the snapshot id.
PRIMITIVE_PAYLOAD_MAX_BYTES: Final = 8192

# Per-attribute byte cap on the narrative text attached to
# `mcae.narrative.emitted`. Same 8 KiB shape as the primitive cap;
# narratives under our agent's USAGE_LIMITS rarely exceed 4-5 KiB.
NARRATIVE_TEXT_MAX_BYTES: Final = 8192


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
