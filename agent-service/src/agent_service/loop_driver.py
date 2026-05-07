"""Phase II loop driver. Replaces the Rust `loop.rs` orchestration
end-to-end. Runs one turn of one session, emits SSE frames as a
generator that `main.py`'s SSE handler streams to the browser.

Per turn:

1. Look up or create the `AgentThread`. Acquire the per-thread lock
   so concurrent SSE GETs on the same thread serialize.
2. (ship 4) If `dont_repeat_yourself` is on AND `turn >= 2`, run the
   repeat detector. On hit: replay prior turn's tool calls, run diff,
   emit `NoMovement` or `ChangedSince`, return.
3. Open a snapshot lease so every primitive call this turn reads from
   the same materialized window.
4. Build user message with `<context>` block.
5. Run the primary Pydantic AI agent. Tool calls accumulate into
   `deps.binding_store`, `deps.tool_call_records`, `deps.emitted_claims`.
6. For each emitted claim:
   - Map to proto `Claim`
   - Validate `${ref:N}` placeholders (deterministic)
   - Run structural value compare (deterministic, ship 5a)
   - If `dont_fabricate` is on, also run constitution gate (LLM)
   - Stamp verdict, emit `Claim` SSE frame
   - Record in thread state if approved
7. Build assembled provenance from approved claims (concatenated arrays
   in emission order, per the prompt's "Citation discipline" section).
8. Validate narrative `${ref:N}` placeholders against assembled provenance.
9. If `stay_in_role` or `dont_fabricate` is on, run constitution gate on
   narrative.
10. Emit `Narrative` or `NarrativeRetracted`.
11. Update thread state (turn count, message history, claims, bindings,
    tool calls, user question).
12. Release snapshot lease, emit `Done`.

Ship 1 of agent-observability (ADR 13) made OTel spans the single
source of truth; the prior bespoke `agent_ledger` table + writer were
deleted. Every event the ledger used to record now lives as a span
attribute on a properly-parented trace, fanned out by the otel-
collector to CH-A `otel.otel_traces` (SQL + cross-store joins) and
to Langfuse (visual flame graph).

Optional `GatePath` SSE frame on approved claim emissions when
`request.show_trace=true`. Same toggle as the Rust loop's
`agent_show_trace` map.

Single async generator entry point: `run_turn(...)`. Yields
`{"event": <name>, "data": <proto-canonical-json-str>}` dicts that
match `EventSourceResponse`'s expected shape.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator

import structlog
from google.protobuf import json_format
from opentelemetry import trace
from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits

from agent_service import spans

# Module-level tracer. init_otel() registered the global TracerProvider
# at app startup; this resolves through to it. Tests with
# OTEL_SDK_DISABLED=true get a no-op tracer so `with start_as_current_span`
# stays cheap.
_tracer = trace.get_tracer(__name__)


def _resolve_run_type(raw: str) -> str:
    """Map an `AgentRequest.run_type` string to the value stamped on
    the mcae.turn span. Empty string (the proto3 default) means
    'production'. Other values pass through verbatim so callers can
    extend with new types (e.g. 'dev', custom suite labels) without
    a proto bump."""
    return raw or spans.RUN_TYPE_PRODUCTION

from multichain.wire.agent.v1 import (
    claim_pb2,
    diff_pb2,
    narrative_pb2,
    policy_pb2,
    session_pb2,
    sse_pb2,
    switches_pb2,
)
from multichain.wire.shared.v1 import provenance_pb2

from agent_service.agent import AgentDeps, EmitClaimInput, ToolCallRecord
from agent_service.boundary import (
    UnsafeUserInputError,
    build_context_block,
    reject_if_unsafe_user_question,
)
from agent_service.diff import diff_outputs, spec_for
from agent_service.llm_retry import with_provider_retry
from agent_service.policy.binding_store import PrimitiveBindingStore
from agent_service.policy import constitution as constitution_module
from agent_service.policy import structural as structural_module
from agent_service.policy.constitution import (
    ConstitutionVerdict,
    judge_claim,
    judge_narrative,
)
from agent_service.policy.placeholder import validate_refs
from agent_service.policy.structural import verify_chip_values

# Gate version pinned at module load. The placeholder gate is purely
# deterministic ref-validation with no version notion of its own;
# v1 is a stable signal for eval probes that something other than
# "the gate didn't run" happened. Constitution and structural read
# their own VERSION constants so a prompt swap or algorithm bump
# propagates without touching the loop driver.
_PLACEHOLDER_VERSION = "v1"
from agent_service.primitive_client import PrimitiveClient, PrimitiveError
from agent_service.repeat_detector import RepeatDetectorOutcome, detect_repeat
from agent_service.thread_state import (
    AgentThread,
    ThreadRegistry,
    TurnToolCallRecord,
)

log = structlog.get_logger(__name__)

# Per-turn agent runtime cap. Free-tier OpenRouter: keep tokens tight,
# tool calls bounded so a runaway tool-call loop can't exhaust quota.
_USAGE_LIMITS = UsageLimits(
    request_limit=10,
    tool_calls_limit=8,
)

# Generic user-facing error message; raw exception only crosses the wire
# when AGENT_DEBUG_PUBLIC=1.
_GENERIC_ERROR_MSG = (
    "Couldn't produce a valid response. Try rephrasing or try again."
)


@dataclass
class LoopHandles:
    """Bundle of long-lived dependencies the loop reads on each turn.
    Built once in `main.py`'s lifespan handler and passed into
    `run_turn` via app state."""

    primary_agent: Agent
    constitution_agent: Agent
    repeat_agent: Agent
    primitive_client: PrimitiveClient
    threads: ThreadRegistry
    debug_public: bool


# ---------------------------------------------------------------------------
# Frame helpers (proto -> {"event", "data": json_str} dicts for SSE)
# ---------------------------------------------------------------------------


def _frame(event: str, msg) -> dict[str, str]:
    return {
        "event": event,
        "data": json_format.MessageToJson(
            msg, preserving_proto_field_name=False, indent=None
        ),
    }


# ---------------------------------------------------------------------------
# Provenance mapping (tool-arg ProvenanceRefIn -> proto ProvenanceRef)
# ---------------------------------------------------------------------------


def _map_provenance(refs: list) -> list[provenance_pb2.ProvenanceRef]:
    """Convert tool-arg `_ProvenanceRefIn` shapes to proto messages.
    Skips entries with the wrong fields for their kind (model errors)
    rather than crashing the gate."""
    out: list[provenance_pb2.ProvenanceRef] = []
    for r in refs:
        kind = (r.kind or "").lower()
        if kind == "wallet" and r.addr is not None:
            wallet = provenance_pb2.WalletRef(addr=r.addr)
            if r.idx is not None:
                wallet.idx = r.idx
            out.append(provenance_pb2.ProvenanceRef(wallet=wallet))
        elif kind == "community" and r.id is not None:
            out.append(
                provenance_pb2.ProvenanceRef(
                    community=provenance_pb2.CommunityRef(id=r.id)
                )
            )
        elif kind == "edge" and r.edge_id is not None and r.src is not None and r.dst is not None:
            out.append(
                provenance_pb2.ProvenanceRef(
                    edge=provenance_pb2.EdgeRef(id=r.edge_id, src=r.src, dst=r.dst)
                )
            )
        elif kind == "time_range" and r.from_s is not None and r.to_s is not None:
            out.append(
                provenance_pb2.ProvenanceRef(
                    time_range=provenance_pb2.TimeRangeRef(from_s=r.from_s, to_s=r.to_s)
                )
            )
        elif kind == "number" and r.metric is not None and r.value is not None:
            out.append(
                provenance_pb2.ProvenanceRef(
                    number=provenance_pb2.NumberRef(
                        metric=r.metric,
                        value=r.value,
                        support=list(r.support or []),
                    )
                )
            )
        # Skip silently: malformed refs surface as missing chips at render time.
    return out


# ---------------------------------------------------------------------------
# Claim builder (EmitClaimInput -> proto Claim with stamps)
# ---------------------------------------------------------------------------


_CLAIM_KIND_MAP = {
    "PROFILE": claim_pb2.CLAIM_KIND_PROFILE,
    "PATTERN": claim_pb2.CLAIM_KIND_PATTERN,
    "COMPARISON": claim_pb2.CLAIM_KIND_COMPARISON,
    "SUMMARY": claim_pb2.CLAIM_KIND_SUMMARY,
    "PULSE": claim_pb2.CLAIM_KIND_PULSE,
}


def _build_claim(
    *,
    input_: EmitClaimInput,
    session_id: str,
    session_started_at_ms: int,
) -> claim_pb2.Claim:
    """Stamp the runtime-controlled fields onto a Claim drafted by the
    model. Verdict starts Approved; gates may downgrade to Retracted."""
    kind_enum = _CLAIM_KIND_MAP.get(
        input_.kind.upper(), claim_pb2.CLAIM_KIND_UNSPECIFIED
    )
    elapsed = max(0, int(time.time() * 1000) - session_started_at_ms)
    claim = claim_pb2.Claim(
        id=str(uuid.uuid4()),
        session_id=session_id,
        kind=kind_enum,
        headline=input_.headline,
        body_markdown=input_.body_markdown,
        provenance=_map_provenance(input_.provenance),
        emitted_at_ms=min(elapsed, 0xFFFFFFFF),
    )
    for n in input_.support_numbers:
        claim.support_numbers.add(metric=n.metric, value=n.value, support=list(n.support or []))
    # Default Approved; gates downgrade.
    claim.policy_verdict.approved.SetInParent()
    return claim


def _set_retracted(claim: claim_pb2.Claim, reason: str) -> None:
    claim.policy_verdict.retracted.reason = reason


def _normalize_verdict(v: str) -> str:
    """Map constitution-gate verdicts ("approve" | "retract" | "reject")
    onto the span verdict vocabulary so all gate.* + claim.emitted +
    narrative.emitted spans share one set of strings (`approved` /
    `retracted` / `reject`). Eval probes filter on these; consistency
    here saves a CASE expression in every consumer query."""
    return {
        "approve": spans.VERDICT_APPROVED,
        "retract": spans.VERDICT_RETRACTED,
        "reject": spans.VERDICT_REJECT,
    }.get(v, v)


# ---------------------------------------------------------------------------
# Provenance summary helper for constitution gate payloads
# ---------------------------------------------------------------------------


def _summarize_provenance(refs) -> list[dict]:
    """Compact form of provenance for the constitution gate's user
    payload. Mirrors the JSON the Rust gate sends. Keeps payload small."""
    out = []
    for r in refs:
        case = r.WhichOneof("ref")
        if case == "wallet":
            out.append({"kind": "wallet", "addr": r.wallet.addr})
        elif case == "community":
            out.append({"kind": "community", "id": r.community.id})
        elif case == "edge":
            out.append({"kind": "edge", "id": r.edge.id})
        elif case == "time_range":
            out.append({"kind": "time_range", "from_s": r.time_range.from_s, "to_s": r.time_range.to_s})
        elif case == "number":
            out.append(
                {
                    "kind": "number",
                    "metric": r.number.metric,
                    "value": r.number.value,
                }
            )
    return out


def _claims_to_judgement_payload(claims: list[claim_pb2.Claim]) -> list[dict]:
    """Trim Claims down to the shape the constitution gate's narrative
    payload uses for `same_turn_claims`: headline + body + provenance
    summary. Keeps the payload size sane for free-tier rate limits."""
    return [
        {
            "headline": c.headline,
            "body_markdown": c.body_markdown,
            "provenance_summary": _summarize_provenance(c.provenance),
        }
        for c in claims
    ]


# ---------------------------------------------------------------------------
# Repeat path (ship 4 dont_repeat_yourself)
# ---------------------------------------------------------------------------


async def _run_repeat_path(
    *,
    handles: LoopHandles,
    thread: AgentThread,
    repeat_of_turn: int,
    snapshot_id: str,
    session_id: str,
    session_started_at_ms: int,
) -> AsyncIterator[dict[str, str]]:
    """Replay the prior turn's tool calls against the fresh snapshot,
    diff outputs, emit NoMovement or ChangedSince. No LLM narrative
    call on the empty path; ChangedSince carries deterministic prose
    listing the changed fields.

    Wrapped in a `turn.diff` span so the SQL query
    `SELECT changed_count, primitives_replayed FROM otel_traces WHERE
    SpanName='turn.diff'` answers "what shifted between this turn and
    the prior one" without replaying the loop. Per-primitive replays
    nest as primitive.* spans automatically (the wallet_profile /
    community_summary calls open their own spans inside this context).
    """
    prior_calls = thread.tool_calls_per_turn.get(repeat_of_turn, [])
    primitives_replayed: list[str] = []
    all_changed: list[diff_pb2.FieldDelta] = []
    total_unchanged = 0

    with _tracer.start_as_current_span(spans.TURN_DIFF) as diff_span:
        diff_span.set_attribute(spans.Attrs.REPEAT_OF_TURN, repeat_of_turn)
        for record in prior_calls:
            try:
                if record.primitive_name == "wallet_profile":
                    fresh = await handles.primitive_client.wallet_profile(
                        addr=record.args["addr"], snapshot_id=snapshot_id
                    )
                elif record.primitive_name == "community_summary":
                    fresh = await handles.primitive_client.community_summary(
                        community_id=record.args["community_id"],
                        snapshot_id=snapshot_id,
                    )
                else:
                    continue
            except PrimitiveError as e:
                log.warning(
                    "repeat_replay_failed",
                    primitive=record.primitive_name,
                    error=e.kind,
                )
                continue

            primitives_replayed.append(record.primitive_name)
            spec = spec_for(record.primitive_name)
            delta = diff_outputs(record.primitive_name, spec, record.output_value, fresh.value)
            all_changed.extend(delta.changed)
            total_unchanged += delta.unchanged_field_count

        diff_span.set_attribute(spans.Attrs.DIFF_CHANGED_COUNT, len(all_changed))
        diff_span.set_attribute(spans.Attrs.DIFF_UNCHANGED_COUNT, total_unchanged)
        diff_span.set_attribute(
            spans.Attrs.DIFF_PRIMITIVES_REPLAYED, primitives_replayed
        )

        if not all_changed:
            # No-movement bubble. Deterministic; no LLM call.
            nm = diff_pb2.NoMovement(
                prior_turn=repeat_of_turn,
                primitives_replayed=primitives_replayed,
            )
            yield _frame("NoMovement", nm)
        else:
            delta = diff_pb2.Delta(
                changed=all_changed, unchanged_field_count=total_unchanged
            )
            # Deterministic prose summary; cheap and avoids a second LLM
            # round-trip on the repeat path. Loop driver ports the Rust
            # behavior of "small narrative call describing what shifted"
            # in a future revision; v0 is just a structured summary.
            prose = _format_changed_prose(all_changed)
            cs = diff_pb2.ChangedSince(prior_turn=repeat_of_turn, delta=delta, prose=prose)
            yield _frame("ChangedSince", cs)


def _format_changed_prose(changes: list[diff_pb2.FieldDelta]) -> str:
    """Deterministic single-paragraph summary of what diff fields
    moved. Plain prose; no chips, no audit numbers (the user reads the
    structured Delta for the values)."""
    if not changes:
        return "No movement since the prior turn."
    parts: list[str] = []
    for c in changes:
        case = c.change.WhichOneof("change")
        if case == "number_moved":
            n = c.change.number_moved
            parts.append(f"{c.field_path} moved from {n.prior:.2f} to {n.current:.2f}")
        elif case == "count_changed":
            n = c.change.count_changed
            parts.append(f"{c.field_path} changed from {int(n.prior)} to {int(n.current)}")
        elif case == "set_changed":
            s = c.change.set_changed
            added_n = len(s.added)
            removed_n = len(s.removed)
            parts.append(
                f"{c.field_path}: {added_n} added, {removed_n} removed"
            )
    return "Since the prior turn: " + "; ".join(parts) + "."


# ---------------------------------------------------------------------------
# Main turn entry point
# ---------------------------------------------------------------------------


async def run_turn(
    *,
    handles: LoopHandles,
    request,  # session_pb2.AgentRequest
    session_id: str,
    thread_id: str,
    session_started_at_ms: int,
) -> AsyncIterator[dict[str, str]]:
    """One turn of one session. Yields SSE frame dicts. The SSE
    handler streams them as `event:`/`data:` pairs."""
    snapshot_id: str | None = None
    try:
        thread, lock = await handles.threads.get_or_create(thread_id)
        async with lock:
          # Root span for this turn. Carries the four turn-scoped attrs
          # (session/thread/turn/run-type) so SQL filters can
          # `WHERE SpanName='agent.turn' AND SpanAttributes['session.id']='...'`
          # then join children via TraceId. Everything below opens under
          # this context, including Pydantic AI's auto agent.run /
          # gen_ai.chat / execute_tool spans. OTel's span context
          # manager is sync (no __aexit__), so it must nest inside the
          # async-with rather than combine. `yield` inside is fine:
          # OTel uses contextvars so the active-span stack is preserved
          # across async suspension points.
          with _tracer.start_as_current_span(spans.AGENT_TURN) as turn_span:
            turn = thread.turn_count
            thread.turn_count += 1
            thread.record_turn_user_question(turn, request.user_question)

            turn_span.set_attribute(spans.Attrs.SESSION_ID, session_id)
            turn_span.set_attribute(spans.Attrs.THREAD_ID, thread_id)
            turn_span.set_attribute(spans.Attrs.TURN_INDEX, turn)
            turn_span.set_attribute(
                spans.Attrs.RUN_TYPE, _resolve_run_type(request.run_type)
            )
            # The user question lives on the mcae.turn span so traces
            # are searchable by what was asked. Previously this also
            # went into the SESSION_STARTED ledger row; ledger deletion
            # consolidated it into the span attribute set.
            turn_span.set_attribute(spans.Attrs.TURN_USER_QUESTION, request.user_question)

            # ------ Topical rail: reject unsafe user input ------
            # The user question is one of two untrusted-input slots
            # (the other is `<external_data>` blocks from primitive
            # outputs). Chat-template control tokens, closing
            # pseudo-tags, and HTML script tags have zero legitimate
            # use in the analyst-tool chat field, so we hard-reject
            # at the boundary before agent.run() is ever invoked.
            # Tool dispatch is impossible by construction on a
            # rejected turn; the model never sees the malicious
            # tokens. See boundary.py and #33 for the threat model
            # this defends.
            try:
                reject_if_unsafe_user_question(request.user_question)
            except UnsafeUserInputError as e:
                rejection_text = (
                    "Your message contained chat-template-style tokens "
                    "or other non-natural-language patterns that aren't "
                    "supported in this conversation. Please rephrase in "
                    "plain English. I'm a read-only analyst agent for "
                    "the Solana transaction graph; I can profile wallets, "
                    "summarize communities, and look at on-chain transfers."
                )
                with _tracer.start_as_current_span(spans.NARRATIVE_EMITTED) as nar_span:
                    nar_span.set_attribute(
                        spans.Attrs.NARRATIVE_LENGTH_CHARS, len(rejection_text)
                    )
                    nar_span.set_attribute(
                        spans.Attrs.NARRATIVE_ASSEMBLED_PROVENANCE_COUNT, 0
                    )
                    nar_span.set_attribute(spans.Attrs.NARRATIVE_TEXT, rejection_text)
                    nar_span.set_attribute(
                        spans.Attrs.NARRATIVE_VERDICT, spans.VERDICT_APPROVED
                    )
                    # Stamp the rejection reason on the turn span so
                    # traces and probes can attribute the short-circuit
                    # to a specific token shape rather than guessing
                    # from the narrative text.
                    turn_span.set_attribute(
                        spans.Attrs.TURN_UNSAFE_INPUT_REJECTED, "true"
                    )
                    turn_span.set_attribute(
                        spans.Attrs.TURN_UNSAFE_INPUT_PATTERN, e.pattern
                    )
                yield _frame(
                    "Narrative",
                    narrative_pb2.NarrativeWithRefs(
                        text=rejection_text, provenance=[]
                    ),
                )
                # Mirror the end-of-turn aggregate stamping at the
                # bottom of the normal path so downstream eval probes
                # see consistent turn-root attributes regardless of
                # whether the turn was rejected or completed.
                turn_span.set_attribute(spans.Attrs.TURN_CLAIMS_EMITTED, 0)
                turn_span.set_attribute(spans.Attrs.TURN_CLAIMS_APPROVED, 0)
                turn_span.set_attribute(spans.Attrs.TURN_TOOL_CALLS, 0)
                turn_span.set_attribute(
                    spans.Attrs.TURN_NARRATIVE_CHARS, len(rejection_text)
                )
                log.info(
                    "user_input_rejected_at_boundary",
                    session_id=session_id,
                    pattern=e.pattern,
                )
                yield _terminal_done(session_id, session_started_at_ms)
                return

            # ------ Repeat path (ship 4) ------
            if (
                request.switches.dont_repeat_yourself
                and turn >= 1
                and thread.user_questions_per_turn
            ):
                prior_qs = {
                    t: q
                    for t, q in thread.user_questions_per_turn.items()
                    if t != turn  # exclude the just-recorded current question
                }
                if prior_qs:
                    with _tracer.start_as_current_span(spans.REPEAT_DETECTION) as rd_span:
                        outcome = await detect_repeat(
                            prior_qs, request.user_question, handles.repeat_agent
                        )
                        is_repeat = outcome.repeat_of_turn is not None
                        rd_span.set_attribute(spans.Attrs.REPEAT_IS_REPEAT, is_repeat)
                        rd_span.set_attribute(
                            spans.Attrs.REPEAT_USER_WANTS_REFRESH,
                            outcome.user_explicitly_wants_refresh,
                        )
                        if is_repeat:
                            rd_span.set_attribute(
                                spans.Attrs.REPEAT_OF_TURN, outcome.repeat_of_turn
                            )
                        if outcome.reason:
                            rd_span.set_attribute(
                                spans.Attrs.REPEAT_REASON, outcome.reason
                            )
                    if (
                        outcome.repeat_of_turn is not None
                        and not outcome.user_explicitly_wants_refresh
                    ):
                        log.info(
                            "repeat_detected",
                            session_id=session_id,
                            repeat_of_turn=outcome.repeat_of_turn,
                            reason=outcome.reason,
                        )
                        # Open snapshot for replay primitives.
                        lease = await handles.primitive_client.begin_turn()
                        snapshot_id = lease.snapshot_id
                        async for frame in _run_repeat_path(
                            handles=handles,
                            thread=thread,
                            repeat_of_turn=outcome.repeat_of_turn,
                            snapshot_id=snapshot_id,
                            session_id=session_id,
                            session_started_at_ms=session_started_at_ms,
                        ):
                            yield frame
                        # Done frame and snapshot release happen in finally.
                        yield _terminal_done(session_id, session_started_at_ms)
                        return

            # ------ Snapshot lease ------
            lease = await handles.primitive_client.begin_turn()
            snapshot_id = lease.snapshot_id
            log.info(
                "turn_begin",
                session_id=session_id,
                snapshot_id=snapshot_id,
                turn=turn,
            )

            # ------ Planning progress ------
            yield _frame(
                "Progress",
                sse_pb2.Progress(
                    phase="planning", detail="reading context, choosing primitive"
                ),
            )

            # ------ Build user prompt with <context> block ------
            user_msg = build_context_block(request.context, request.user_question)

            # ------ Run primary agent ------
            deps = AgentDeps(
                primitive_client=handles.primitive_client,
                snapshot_id=snapshot_id,
                session_id=session_id,
                session_started_at_ms=session_started_at_ms,
                binding_store=thread.bindings,
            )
            try:
                run_kwargs: dict = {"deps": deps, "usage_limits": _USAGE_LIMITS}
                if thread.message_history:
                    run_kwargs["message_history"] = thread.message_history
                # Heads-up to the UI that we are about to spend the
                # bulk of the turn waiting on the primary LLM. Without
                # this the spinner sits on "planning" through the
                # entire generation (5-15s on free-tier OpenRouter).
                yield _frame(
                    "Progress",
                    sse_pb2.Progress(phase="drafting", detail="primary model"),
                )
                # 75s per attempt covers a normal multi-tool turn
                # (~25s today) with headroom for slow free-tier hops,
                # while still failing fast enough that one stuck call
                # gets a retry within the 180s SSE stream cap. Total
                # worst-case budget: 75 + 1s backoff + 75 = 151s.
                result = await with_provider_retry(
                    lambda: handles.primary_agent.run(user_msg, **run_kwargs),
                    label="primary_agent",
                    per_attempt_timeout_s=75.0,
                )
            except Exception as e:  # noqa: BLE001
                log.exception("primary_agent_run_failed", session_id=session_id)
                yield _emit_error(e, debug_public=handles.debug_public)
                yield _terminal_done(session_id, session_started_at_ms)
                return

            narrative_text: str = result.output

            # ------ Process emitted claims ------
            # One `claim.emitted` span per claim wrapping all gate calls
            # so the trace tree shows per-claim trees: claim.emitted →
            # (gate.placeholder, gate.structural, gate.constitution →
            # gen_ai.chat). The final verdict attribute is set right
            # before the SSE frame yield so a single span query gives
            # the per-claim outcome history.
            approved_claims: list[claim_pb2.Claim] = []
            for ec in deps.emitted_claims:
                claim = _build_claim(
                    input_=ec,
                    session_id=session_id,
                    session_started_at_ms=session_started_at_ms,
                )

                with _tracer.start_as_current_span(spans.CLAIM_EMITTED) as claim_span:
                    claim_span.set_attribute(spans.Attrs.CLAIM_ID, claim.id)
                    claim_span.set_attribute(
                        spans.Attrs.CLAIM_KIND,
                        claim_pb2.ClaimKind.Name(claim.kind),
                    )
                    claim_span.set_attribute(spans.Attrs.CLAIM_HEADLINE, claim.headline)
                    claim_span.set_attribute(
                        spans.Attrs.CLAIM_PROVENANCE_COUNT, len(claim.provenance)
                    )
                    claim_span.set_attribute(
                        spans.Attrs.CLAIM_BODY_CHARS, len(claim.body_markdown)
                    )
                    # Today the only evidence-gathering tools are typed
                    # primitives, so every claim is "primitive". When
                    # sql_explore ships, the loop will inspect which
                    # tools contributed evidence to this claim and pick
                    # SOURCE_KIND_EXPLORATORY when any sql_explore row
                    # was used. Defining the attr now means the eval
                    # probe `claim_grounded_in(source_kind=...)` can be
                    # written against today's traces.
                    claim_span.set_attribute(
                        spans.Attrs.CLAIM_SOURCE_KIND, spans.SOURCE_KIND_PRIMITIVE
                    )

                    # Rule 1: empty provenance always retracts. No gate
                    # span emitted for this case; the claim-level verdict
                    # carries the reason.
                    if not claim.provenance:
                        _set_retracted(claim, "claim has empty provenance; cite at least one entity")
                        claim_span.set_attribute(spans.Attrs.CLAIM_VERDICT, spans.VERDICT_RETRACTED)
                        yield _frame("Claim", claim)
                        continue

                    # Deterministic placeholder validation.
                    with _tracer.start_as_current_span(spans.GATE_PLACEHOLDER) as g:
                        g.set_attribute(spans.Attrs.GATE_VERSION, _PLACEHOLDER_VERSION)
                        ref_err = validate_refs(claim.body_markdown, len(claim.provenance))
                        if ref_err is not None:
                            g.set_attribute(spans.Attrs.GATE_VERDICT, spans.VERDICT_RETRACTED)
                            g.set_attribute(spans.Attrs.GATE_REASON, ref_err.to_human_string())
                            _set_retracted(claim, ref_err.to_human_string())
                            claim_span.set_attribute(spans.Attrs.CLAIM_VERDICT, spans.VERDICT_RETRACTED)
                            yield _frame("Claim", claim)
                            continue
                        g.set_attribute(spans.Attrs.GATE_VERDICT, spans.VERDICT_APPROVED)

                    # Structural value compare (ship 5a). Always runs;
                    # only retracts when dont_fabricate is on.
                    with _tracer.start_as_current_span(spans.GATE_STRUCTURAL) as g:
                        g.set_attribute(spans.Attrs.GATE_VERSION, structural_module.VERSION)
                        g.set_attribute(spans.Attrs.GATE_BINDING_SIZE, len(thread.bindings.all_numbers()))
                        struct_err = verify_chip_values(list(claim.provenance), thread.bindings)
                        if struct_err is not None and request.switches.dont_fabricate:
                            g.set_attribute(spans.Attrs.GATE_VERDICT, spans.VERDICT_RETRACTED)
                            g.set_attribute(spans.Attrs.GATE_REASON, struct_err.to_human_string())
                            g.set_attribute(spans.Attrs.GATE_FAILED_CHIP, str(getattr(struct_err, "kind", "unknown")))
                            _set_retracted(claim, struct_err.to_human_string())
                            claim_span.set_attribute(spans.Attrs.CLAIM_VERDICT, spans.VERDICT_RETRACTED)
                            yield _frame("Claim", claim)
                            continue
                        g.set_attribute(spans.Attrs.GATE_VERDICT, spans.VERDICT_APPROVED)

                    # Constitution gate (only when stay_in_role is on; the
                    # constitution covers domain + identity + citation).
                    # The constitution agent's gen_ai.chat span auto-nests
                    # under this gate.constitution span via OTel context.
                    if request.switches.stay_in_role:
                        with _tracer.start_as_current_span(spans.GATE_CONSTITUTION) as g:
                            g.set_attribute(spans.Attrs.GATE_VERSION, constitution_module.VERSION)
                            verdict = await judge_claim(
                                handles.constitution_agent,
                                headline=claim.headline,
                                body_markdown=claim.body_markdown,
                                provenance_summary=_summarize_provenance(claim.provenance),
                            )
                            g.set_attribute(
                                spans.Attrs.GATE_VERDICT,
                                _normalize_verdict(verdict.verdict),
                            )
                            if verdict.reason:
                                g.set_attribute(spans.Attrs.GATE_REASON, verdict.reason)
                            if verdict.verdict in ("retract", "reject"):
                                _set_retracted(
                                    claim, verdict.reason or f"constitution {verdict.verdict}"
                                )
                                claim_span.set_attribute(
                                    spans.Attrs.CLAIM_VERDICT,
                                    _normalize_verdict(verdict.verdict),
                                )
                                yield _frame("Claim", claim)
                                continue

                    # Approved.
                    claim_span.set_attribute(spans.Attrs.CLAIM_VERDICT, spans.VERDICT_APPROVED)
                    yield _frame("Claim", claim)
                    approved_claims.append(claim)
                    thread.record_claim(claim)

            # ------ Narrative leg ------
            # Wrapped in narrative.emitted so the trace tree shows the
            # narrative path with its placeholder validation + optional
            # constitution gate as children. One span per turn.
            assembled_provenance: list[provenance_pb2.ProvenanceRef] = []
            for c in approved_claims:
                assembled_provenance.extend(c.provenance)

            # The constitution gate's policy LLM call (gpt-oss-20b on
            # free-tier OpenRouter) is consistently the longest stage
            # of a turn (~11s p50 per issue #16). Without this Progress
            # frame the UI sits silent between the primary LLM finishing
            # and the gated narrative arriving.
            if request.switches.stay_in_role:
                yield _frame(
                    "Progress",
                    sse_pb2.Progress(phase="judging", detail="constitution gate"),
                )

            with _tracer.start_as_current_span(spans.NARRATIVE_EMITTED) as nar_span:
                nar_span.set_attribute(
                    spans.Attrs.NARRATIVE_LENGTH_CHARS, len(narrative_text)
                )
                nar_span.set_attribute(
                    spans.Attrs.NARRATIVE_ASSEMBLED_PROVENANCE_COUNT,
                    len(assembled_provenance),
                )
                # Full narrative text capped to NARRATIVE_TEXT_MAX_BYTES;
                # overflow marker matches the primitive-payload convention
                # so eval probes (and Langfuse) can detect truncation.
                if len(narrative_text) <= spans.NARRATIVE_TEXT_MAX_BYTES:
                    _capped_narrative = narrative_text
                else:
                    _capped_narrative = (
                        narrative_text[: spans.NARRATIVE_TEXT_MAX_BYTES]
                        + f" ...[truncated, total={len(narrative_text)}]"
                    )
                nar_span.set_attribute(
                    spans.Attrs.NARRATIVE_TEXT, _capped_narrative
                )

                # Placeholder validation against assembled provenance.
                # Reuses the gate.placeholder span name for symmetry with
                # the per-claim path; a downstream filter on
                # SpanName='gate.placeholder' AND parent_span name
                # discriminates which case it covered.
                with _tracer.start_as_current_span(spans.GATE_PLACEHOLDER) as pg:
                    pg.set_attribute(spans.Attrs.GATE_VERSION, _PLACEHOLDER_VERSION)
                    ref_err = validate_refs(narrative_text, len(assembled_provenance))
                    if ref_err is not None:
                        pg.set_attribute(spans.Attrs.GATE_VERDICT, spans.VERDICT_RETRACTED)
                        pg.set_attribute(spans.Attrs.GATE_REASON, ref_err.to_human_string())
                        nar_span.set_attribute(
                            spans.Attrs.NARRATIVE_VERDICT, spans.VERDICT_RETRACTED
                        )
                        ret = narrative_pb2.NarrativeRetracted(
                            text=narrative_text, reason=ref_err.to_human_string()
                        )
                        if handles.debug_public:
                            ret.debug_reason = f"placeholder_validate: {ref_err.kind}"
                        yield _frame("NarrativeRetracted", ret)
                    else:
                        pg.set_attribute(spans.Attrs.GATE_VERDICT, spans.VERDICT_APPROVED)

                if ref_err is not None:
                    pass  # Already yielded; fall through to thread-state
                elif request.switches.stay_in_role:
                    # Constitution gate on narrative. The constitution
                    # agent's gen_ai.chat span auto-nests under this.
                    with _tracer.start_as_current_span(spans.GATE_NARRATIVE_CONSTITUTION) as g:
                        g.set_attribute(spans.Attrs.GATE_VERSION, constitution_module.VERSION)
                        verdict = await judge_narrative(
                            handles.constitution_agent,
                            text=narrative_text,
                            same_turn_claims=_claims_to_judgement_payload(approved_claims)
                            + _claims_to_judgement_payload(thread.claims[:-len(approved_claims) or None]),
                        )
                        g.set_attribute(
                            spans.Attrs.GATE_VERDICT, _normalize_verdict(verdict.verdict)
                        )
                        if verdict.reason:
                            g.set_attribute(spans.Attrs.GATE_REASON, verdict.reason)
                        if verdict.verdict in ("retract", "reject"):
                            nar_span.set_attribute(
                                spans.Attrs.NARRATIVE_VERDICT,
                                _normalize_verdict(verdict.verdict),
                            )
                            ret = narrative_pb2.NarrativeRetracted(
                                text=narrative_text,
                                reason=verdict.reason or f"constitution {verdict.verdict}",
                            )
                            if handles.debug_public:
                                ret.debug_reason = f"constitution: {verdict.reason}"
                            yield _frame("NarrativeRetracted", ret)
                        else:
                            nar_span.set_attribute(
                                spans.Attrs.NARRATIVE_VERDICT, spans.VERDICT_APPROVED
                            )
                            nar = narrative_pb2.NarrativeWithRefs(
                                text=narrative_text, provenance=assembled_provenance
                            )
                            yield _frame("Narrative", nar)
                else:
                    # raw-llm or agent-without-grounding: skip constitution.
                    nar_span.set_attribute(
                        spans.Attrs.NARRATIVE_VERDICT, spans.VERDICT_APPROVED
                    )
                    nar = narrative_pb2.NarrativeWithRefs(
                        text=narrative_text, provenance=assembled_provenance
                    )
                    yield _frame("Narrative", nar)

            # ------ Update thread state ------
            thread.message_history = list(result.all_messages())
            for record in deps.tool_call_records:
                if record.primitive_name in ("wallet_profile", "community_summary"):
                    thread.record_turn_tool_call(
                        turn,
                        TurnToolCallRecord(
                            primitive_name=record.primitive_name,
                            args=record.args,
                            output_value=record.output_value,
                            call_id=record.call_id,
                        ),
                    )

            # Final per-turn aggregates stamped on the mcae.turn span
            # so SQL queries can answer "how many claims got approved
            # this turn" without scanning per-claim spans. Replaces the
            # prior TURN_COMPLETED ledger row.
            turn_span.set_attribute(spans.Attrs.TURN_CLAIMS_EMITTED, len(deps.emitted_claims))
            turn_span.set_attribute(spans.Attrs.TURN_CLAIMS_APPROVED, len(approved_claims))
            turn_span.set_attribute(spans.Attrs.TURN_TOOL_CALLS, len(deps.tool_call_records))
            turn_span.set_attribute(spans.Attrs.TURN_NARRATIVE_CHARS, len(narrative_text))

            yield _terminal_done(session_id, session_started_at_ms)

    except asyncio.CancelledError:
        log.info("agent_stream_cancelled", session_id=session_id)
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("loop_driver_failed", session_id=session_id)
        yield _emit_error(e, debug_public=handles.debug_public)
        yield _terminal_done(session_id, session_started_at_ms)
    finally:
        if snapshot_id is not None:
            await handles.primitive_client.end_turn(snapshot_id)


def _terminal_done(session_id: str, session_started_at_ms: int) -> dict[str, str]:
    elapsed_ms = max(0, int(time.time() * 1000) - session_started_at_ms)
    # Stamp the active OTel trace id onto the Done frame so the
    # frontend can deep-link into Langfuse / SQL the trace by id.
    # Empty string when the SDK is disabled (tests) or no active span.
    span_ctx = trace.get_current_span().get_span_context()
    trace_id_hex = format(span_ctx.trace_id, "032x") if span_ctx.is_valid else ""
    return _frame(
        "Done",
        session_pb2.AgentDone(
            session_id=session_id,
            elapsed_ms=min(elapsed_ms, 0xFFFFFFFF),
            trace_id=trace_id_hex,
        ),
    )


def _emit_error(exc: Exception, *, debug_public: bool) -> dict[str, str]:
    err = sse_pb2.Error(message=_GENERIC_ERROR_MSG)
    if debug_public:
        err.debug_message = f"{type(exc).__name__}: {exc}"
    return _frame("Error", err)
