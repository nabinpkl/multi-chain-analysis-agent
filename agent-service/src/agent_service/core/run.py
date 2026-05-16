"""Role-agnostic agent loop body.

Extracted from `loop_driver.run_turn` so future drivers (monitor #38,
scheduled pulse, peer-consult) can reuse the loop without copy-paste.

The core does NOT know which driver invoked it. It reads the
normalized fields on `TurnEnvelope`, emits frames through the
abstract `TurnSink`, and returns a `TurnOutcome` the driver writes
back into its own state (chat: thread state; monitor: alert log;
etc).

Caller responsibilities, NOT done here:

* Open the OTel `mcae.turn` root span and stamp role-specific attrs
  (chat: session/thread/turn-index/user-question; monitor: alert id /
  rule id / matched event ref; etc). The core stamps the
  role-agnostic attrs on the active span (run_type, channel switches,
  per-turn aggregates) but never opens or closes the span itself.
* Open the snapshot lease, pass `snapshot_id`. Release in a `finally`.
* Provide a `PrimitiveBindingStore` (chat: per-thread, persistent
  across turns; monitor: per-alert, fresh).
* Provide message history via `envelope.history` if the surface is
  multi-turn (chat). Single-shot drivers leave it empty.

What the core does:

* Stamp role-agnostic span attrs (run_type, channel switches).
* Boundary-check `envelope.intent` (channel-aware suppression on the
  rejection narrative).
* Build the user-side prompt with `<context>` block and run the
  primary agent.
* Process emitted claims through the gate stack (placeholder,
  structural, optional constitution).
* Run the narrative leg with channel-output suppression.
* Stamp per-turn aggregates on the active span.
* Return everything the driver needs to update its own state.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import structlog
from opentelemetry import trace
from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits

from agent_service import spans
from agent_service.agent import (
    AgentDeps,
    ToolCallRecord,
    build_agent,
)
from agent_service.boundary import (
    UnsafeUserInputError,
    build_context_block,
    reject_if_unsafe_user_question,
)
from agent_service.core.envelope import TurnEnvelope
from agent_service.core.post_tools import (
    resolve_narrative_text,
    run_post_tools_phase,
)
from agent_service.core.sink import TurnSink
from agent_service.thread_state import NarrativeSnapshot
from agent_service.llm_retry import with_provider_retry
from agent_service.policy.binding_store import PrimitiveBindingStore
from agent_service.primitive_client import PrimitiveClient
from agent_service.prompts.composer import drops_from_switches
from multichain.wire.agent.v1 import (
    claim_pb2,
    narrative_pb2,
    sse_pb2,
)

log = structlog.get_logger(__name__)
_tracer = trace.get_tracer(__name__)

# Per-turn pydantic-ai usage cap. We deliberately do NOT set
# tool_calls_limit here: tool-call budgeting is enforced at the
# per-tool-body interceptor (see agent.py's wallet_profile /
# community_summary / get_token_info bodies plus
# policy/resource_bounds.py). That path returns a structured
# no_more_lookups tool result so the model can finalize its
# narrative gracefully, instead of pydantic-ai raising
# UsageLimitExceeded which would die as a generic SSE Error.
#
# request_limit stays because it defends against a stuck-without-
# tools model-request loop (model burns model requests without
# making progress). For that pathological case there is no
# graceful pivot, so exception-on-hit is the correct behavior.
_USAGE_LIMITS = UsageLimits(request_limit=10)


# ---------------------------------------------------------------------------
# Outcome the core hands back to the driver for write-back
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TurnOutcome:
    """What the driver writes back into its own state after the core
    finishes. Drivers map these onto whatever persistence shape they
    keep (chat: thread state; monitor: alert audit; etc).

    Empty defaults make the rejection / no-narrative path return a
    well-formed but trivial outcome the driver can write back as a
    no-op.
    """

    new_message_history: list[Any] = field(default_factory=list)
    tool_call_records: list[ToolCallRecord] = field(default_factory=list)
    approved_claims: list[claim_pb2.Claim] = field(default_factory=list)
    # Chunk 4 history replay. The final narrative this turn produced
    # (approved, retracted, or boundary-rejected). None when the
    # turn never reached a narrative emission point  e.g. an early
    # error before the model ran. The chat driver writes this onto
    # `thread.record_turn_narrative` so the reopen path can replay
    # the prose.
    narrative_snapshot: "NarrativeSnapshot | None" = None


# ---------------------------------------------------------------------------
# Core entry point
# ---------------------------------------------------------------------------


def resolve_run_type(raw: str) -> str:
    """Map a raw run_type string to the value stamped on the
    `mcae.turn` span. Empty string (proto3 default) means production.
    Other values pass through verbatim so callers can extend with new
    types ('dev', custom suite labels) without a proto bump.
    """
    return raw or spans.RUN_TYPE_PRODUCTION


async def run_one_turn(
    *,
    primary_agent: Agent,
    primitive_client: PrimitiveClient,
    envelope: TurnEnvelope,
    bindings: PrimitiveBindingStore,
    snapshot_id: str,
    started_at_ms: int,
    sink: TurnSink,
    debug_public: bool,
    prior_claims: Iterable[claim_pb2.Claim] = (),
) -> TurnOutcome:
    """Run one role-agnostic turn through the agent core.

    The active OTel span (opened by the driver) carries the role-
    specific attrs already; this function adds the role-agnostic
    ones. The driver passes the cached `primary_agent` and the
    `primitive_client` directly (no handles bundle) so future drivers
    don't need a `LoopHandles`
    shape they only half-fill.

    `prior_claims` is the chat thread's history of approved claims
    EXCLUDING this turn's, used by the narrative constitution gate
    for `same_turn_claims` context. Single-shot drivers pass `()`.
    """
    turn_span = trace.get_current_span()

    # Role-agnostic span attrs.
    turn_span.set_attribute(spans.Attrs.RUN_TYPE, resolve_run_type(envelope.run_type))
    turn_span.set_attribute(
        spans.Attrs.TURN_CHANNEL_NARRATIVE_OUTPUT_ENABLED,
        envelope.switches.channels.narrative_output_enabled,
    )
    turn_span.set_attribute(
        spans.Attrs.TURN_CHANNEL_EXTERNAL_TEXT_INPUT_ENABLED,
        envelope.switches.channels.external_text_input_enabled,
    )

    # ------ Topical rail: reject unsafe user input ------
    # The `intent` field is one of two untrusted-input slots (the
    # other is `<external_data>` blocks from primitive outputs).
    # Chat-template control tokens, closing pseudo-tags, and HTML
    # script tags have zero legitimate use, so we hard-reject at the
    # boundary before agent.run() is ever invoked. Tool dispatch is
    # impossible by construction on a rejected turn; the model never
    # sees the malicious tokens. See boundary.py and #33.
    #
    # Gated by `defend_chat_template_spoofing` (#35) so the
    # article-side ablation surface can disable the rail and observe
    # raw model behavior under chat-template-shape injection.
    try:
        if envelope.switches.stay_in_role.defend_chat_template_spoofing:
            reject_if_unsafe_user_question(envelope.intent)
    except UnsafeUserInputError as e:
        sse_text = emit_unsafe_input_rejection_observability(
            tracer=_tracer,
            turn_span=turn_span,
            pattern=e.pattern,
            narrative_output_enabled=envelope.switches.channels.narrative_output_enabled,
        )
        await sink.emit(
            "Narrative",
            narrative_pb2.NarrativeWithRefs(text=sse_text, provenance=[]),
        )
        log.info("user_input_rejected_at_boundary", pattern=e.pattern)
        return TurnOutcome(
            narrative_snapshot=NarrativeSnapshot(text=sse_text),
        )

    # ------ Planning progress ------
    await sink.emit(
        "Progress",
        sse_pb2.Progress(
            phase="planning", detail="reading context, choosing primitive"
        ),
    )

    # ------ Build user prompt with <context> block ------
    user_msg = build_context_block(envelope.view_context, envelope.intent)

    # ------ Run primary agent ------
    deps = AgentDeps(
        primitive_client=primitive_client,
        snapshot_id=snapshot_id,
        turn_started_at_ms=started_at_ms,
        binding_store=bindings,
        external_text_input_enabled=envelope.switches.channels.external_text_input_enabled,
    )
    run_kwargs: dict = {"deps": deps, "usage_limits": _USAGE_LIMITS}
    if envelope.history:
        run_kwargs["message_history"] = envelope.history
    # Heads-up to the UI that we are about to spend the bulk of the
    # turn waiting on the primary LLM. Without this the spinner sits
    # on "planning" through the entire generation (5-15s on free-tier
    # OpenRouter).
    await sink.emit(
        "Progress", sse_pb2.Progress(phase="drafting", detail="primary model")
    )
    # Resolve which agent to run. Production preset (every defense on
    # AND default live window) uses the cached startup-built
    # primary_agent. When per-defense switches drop one or more rules
    # OR the turn opts into a non-default live window, build a fresh
    # agent per turn with the right drops + window. Pydantic AI Agent
    # setup is sub-millisecond (no I/O), so the cost is negligible
    # compared to the LLM call about to follow.
    turn_drops = drops_from_switches(envelope.switches)
    needs_rebuild = bool(turn_drops) or envelope.live_window_secs != 60
    turn_agent: Agent = (
        build_agent(
            drop_rule_ids=turn_drops,
            llm_override=envelope.primary_llm_override,
            live_window_secs=envelope.live_window_secs,
        )
        if needs_rebuild
        else primary_agent
    )

    # 75s per attempt covers a normal multi-tool turn (~25s today)
    # with headroom for slow free-tier hops, while still failing
    # fast enough that one stuck call gets a retry within the 180s
    # SSE stream cap. Total worst-case budget: 75 + 1s backoff + 75
    # = 151s.
    result = await with_provider_retry(
        lambda: turn_agent.run(user_msg, **run_kwargs),
        label="primary_agent",
        per_attempt_timeout_s=75.0,
    )
    narrative_text: str = result.output

    # ------ Gate stack + narrative leg + per-turn aggregates ------
    # Shared with codex_driver.run_turn_codex; the function owns
    # claim-by-claim gates (placeholder → structural → constitution),
    # the narrative leg (placeholder → constitution), SSE frame
    # emission for Claim / Narrative / NarrativeRetracted, and the
    # per-turn aggregate stamping on `turn_span`.
    outcome = await run_post_tools_phase(
        emitted_claims=deps.emitted_claims,
        narrative_text=narrative_text,
        bindings=bindings,
        envelope=envelope,
        thread_id=envelope.correlation_id,
        turn_started_at_ms=started_at_ms,
        prior_claims=prior_claims,
        sink=sink,
        debug_public=debug_public,
        turn_span=turn_span,
        tool_calls_count=len(deps.tool_call_records),
        budget_exhausted=deps.budget_exhausted_fired,
    )

    return TurnOutcome(
        new_message_history=list(result.all_messages()),
        tool_call_records=list(deps.tool_call_records),
        approved_claims=outcome.approved_claims,
        narrative_snapshot=outcome.narrative_snapshot,
    )




# ---------------------------------------------------------------------------
# Loop-body helpers (role-agnostic; private to the core)
# ---------------------------------------------------------------------------


# The narrative text used to refuse a turn whose user-question hit
# the chat-template / role-pseudo-tag / HTML-script-tag rail in
# `boundary.reject_if_unsafe_user_question`. Single source so the
# pydantic-ai and codex paths emit byte-identical wording; drift in
# this string is a frontend display drift that's annoying to bisect.
UNSAFE_USER_INPUT_REJECTION_NARRATIVE = (
    "Your message contained chat-template-style tokens or "
    "other non-natural-language patterns that aren't supported "
    "in this conversation. Please rephrase in plain English. "
    "I'm a read-only analyst agent for the Solana transaction "
    "graph; I can profile wallets, summarize communities, and "
    "look at on-chain transfers."
)


def emit_unsafe_input_rejection_observability(
    *,
    tracer: trace.Tracer,
    turn_span: trace.Span,
    pattern: str,
    narrative_output_enabled: bool,
) -> str:
    """Stamp the narrative-and-turn span attributes that mark a turn as
    rejected at the user-input topical rail, and return the post-
    channel-switch SSE text the caller should ship to the user.

    Shared shape across pydantic-ai (`core.run.run_one_turn`) and
    codex (`codex_driver._agent_turn_codex`). The caller still owns
    wire-frame emission (`sink.emit` vs yielding SSE frames) and
    thread-state persistence; those shapes differ enough between the
    two drivers that pulling them in here would entangle this helper
    with both. Everything that IS shared  the rejection wording, the
    span attribute set, the narrative-channel resolution, the zeroed
    turn aggregates  lives here so a regression in either path can
    only happen by editing this function.
    """
    with tracer.start_as_current_span(spans.NARRATIVE_EMITTED) as nar_span:
        nar_span.set_attribute(
            spans.Attrs.NARRATIVE_VERDICT, spans.VERDICT_APPROVED
        )
        sse_text = resolve_narrative_text(
            UNSAFE_USER_INPUT_REJECTION_NARRATIVE,
            narrative_output_enabled=narrative_output_enabled,
            nar_span=nar_span,
        )
        if sse_text:
            nar_span.set_attribute(spans.Attrs.NARRATIVE_TEXT, sse_text)
        nar_span.set_attribute(
            spans.Attrs.NARRATIVE_ASSEMBLED_PROVENANCE_COUNT, 0
        )
        turn_span.set_attribute(spans.Attrs.TURN_UNSAFE_INPUT_REJECTED, "true")
        turn_span.set_attribute(spans.Attrs.TURN_UNSAFE_INPUT_PATTERN, pattern)
    # Turn-level zero aggregates so the boundary short-circuit produces
    # the same attribute set as a normal-completion turn (just zeroed).
    # Eval probes assert against these (`mcae.turn.tool_calls=0`,
    # `mcae.turn.claims_emitted=0`) to verify the rejection didn't
    # silently fire any tools.
    turn_span.set_attribute(spans.Attrs.TURN_CLAIMS_EMITTED, 0)
    turn_span.set_attribute(spans.Attrs.TURN_CLAIMS_APPROVED, 0)
    turn_span.set_attribute(spans.Attrs.TURN_TOOL_CALLS, 0)
    turn_span.set_attribute(spans.Attrs.TURN_NARRATIVE_CHARS, len(sse_text))
    return sse_text

