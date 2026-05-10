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

import time
import uuid
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
    EmitClaimInput,
    ToolCallRecord,
    build_agent,
)
from agent_service.boundary import (
    UnsafeUserInputError,
    build_context_block,
    reject_if_unsafe_user_question,
)
from agent_service.core.envelope import TurnEnvelope
from agent_service.core.sink import TurnSink
from agent_service.llm_retry import with_provider_retry
from agent_service.policy import constitution as constitution_module
from agent_service.policy import structural as structural_module
from agent_service.policy.binding_store import PrimitiveBindingStore
from agent_service.policy.constitution import judge_claim, judge_narrative
from agent_service.policy.placeholder import validate_refs
from agent_service.policy.structural import verify_chip_values
from agent_service.primitive_client import PrimitiveClient
from agent_service.prompts.composer import drops_from_switches
from multichain.wire.agent.v1 import (
    claim_pb2,
    narrative_pb2,
    sse_pb2,
)
from multichain.wire.shared.v1 import provenance_pb2

log = structlog.get_logger(__name__)
_tracer = trace.get_tracer(__name__)

# Per-turn agent runtime cap. Free-tier OpenRouter: keep tokens tight,
# tool calls bounded so a runaway tool-call loop can't exhaust quota.
_USAGE_LIMITS = UsageLimits(
    request_limit=10,
    tool_calls_limit=8,
)

# Gate version pinned at module load. The placeholder gate is purely
# deterministic ref-validation with no version notion of its own; v1
# is a stable signal for eval probes that something other than "the
# gate didn't run" happened.
_PLACEHOLDER_VERSION = "v1"

_CLAIM_KIND_MAP = {
    "PROFILE": claim_pb2.CLAIM_KIND_PROFILE,
    "PATTERN": claim_pb2.CLAIM_KIND_PATTERN,
    "COMPARISON": claim_pb2.CLAIM_KIND_COMPARISON,
    "SUMMARY": claim_pb2.CLAIM_KIND_SUMMARY,
    "PULSE": claim_pb2.CLAIM_KIND_PULSE,
}


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
    constitution_agent: Agent,
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
    ones. The driver passes the cached `primary_agent` /
    `constitution_agent` and the `primitive_client` directly (no
    handles bundle) so future drivers don't need a `LoopHandles`
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
        rejection_text = (
            "Your message contained chat-template-style tokens or "
            "other non-natural-language patterns that aren't supported "
            "in this conversation. Please rephrase in plain English. "
            "I'm a read-only analyst agent for the Solana transaction "
            "graph; I can profile wallets, summarize communities, and "
            "look at on-chain transfers."
        )
        with _tracer.start_as_current_span(spans.NARRATIVE_EMITTED) as nar_span:
            sse_text = _resolve_narrative_text(
                rejection_text,
                narrative_output_enabled=envelope.switches.channels.narrative_output_enabled,
                nar_span=nar_span,
            )
            nar_span.set_attribute(spans.Attrs.NARRATIVE_ASSEMBLED_PROVENANCE_COUNT, 0)
            if sse_text:
                nar_span.set_attribute(spans.Attrs.NARRATIVE_TEXT, sse_text)
            nar_span.set_attribute(spans.Attrs.NARRATIVE_VERDICT, spans.VERDICT_APPROVED)
            turn_span.set_attribute(spans.Attrs.TURN_UNSAFE_INPUT_REJECTED, "true")
            turn_span.set_attribute(spans.Attrs.TURN_UNSAFE_INPUT_PATTERN, e.pattern)
        await sink.emit(
            "Narrative",
            narrative_pb2.NarrativeWithRefs(text=sse_text, provenance=[]),
        )
        # Stamp end-of-turn aggregates on the rejection short-circuit
        # so downstream eval probes see the same attribute set as the
        # normal path (just zeroed).
        turn_span.set_attribute(spans.Attrs.TURN_CLAIMS_EMITTED, 0)
        turn_span.set_attribute(spans.Attrs.TURN_CLAIMS_APPROVED, 0)
        turn_span.set_attribute(spans.Attrs.TURN_TOOL_CALLS, 0)
        turn_span.set_attribute(spans.Attrs.TURN_NARRATIVE_CHARS, len(sse_text))
        log.info("user_input_rejected_at_boundary", pattern=e.pattern)
        return TurnOutcome()

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
    # Resolve which agent to run. Production preset (every defense on)
    # uses the cached startup-built primary_agent. When per-defense
    # switches drop one or more rules, build a fresh agent per turn
    # with the right drop set. Pydantic AI Agent setup is sub-
    # millisecond (no I/O), so the cost is negligible compared to
    # the LLM call about to follow.
    turn_drops = drops_from_switches(envelope.switches)
    turn_agent: Agent = (
        build_agent(
            drop_rule_ids=turn_drops, llm_override=envelope.primary_llm_override
        )
        if turn_drops
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

    # ------ Process emitted claims ------
    # One `claim.emitted` span per claim wrapping all gate calls so
    # the trace tree shows per-claim trees: claim.emitted →
    # (gate.placeholder, gate.structural, gate.constitution →
    # gen_ai.chat). The final verdict attribute is set right before
    # the SSE frame emit so a single span query gives the per-claim
    # outcome history.
    approved_claims: list[claim_pb2.Claim] = []
    for ec in deps.emitted_claims:
        claim = _build_claim(
            input_=ec,
            thread_id=envelope.correlation_id,
            turn_started_at_ms=started_at_ms,
        )

        with _tracer.start_as_current_span(spans.CLAIM_EMITTED) as claim_span:
            claim_span.set_attribute(spans.Attrs.CLAIM_ID, claim.id)
            claim_span.set_attribute(
                spans.Attrs.CLAIM_KIND, claim_pb2.ClaimKind.Name(claim.kind)
            )
            claim_span.set_attribute(spans.Attrs.CLAIM_HEADLINE, claim.headline)
            claim_span.set_attribute(
                spans.Attrs.CLAIM_PROVENANCE_COUNT, len(claim.provenance)
            )
            claim_span.set_attribute(
                spans.Attrs.CLAIM_BODY_CHARS, len(claim.body_markdown)
            )
            claim_span.set_attribute(
                spans.Attrs.CLAIM_SOURCE_KIND, spans.SOURCE_KIND_PRIMITIVE
            )

            # Rule 1: empty provenance always retracts. No gate span
            # for this case; the claim-level verdict carries the
            # reason.
            if not claim.provenance:
                _set_retracted(
                    claim, "claim has empty provenance; cite at least one entity"
                )
                claim_span.set_attribute(
                    spans.Attrs.CLAIM_VERDICT, spans.VERDICT_RETRACTED
                )
                await sink.emit("Claim", claim)
                continue

            # Deterministic placeholder validation.
            with _tracer.start_as_current_span(spans.GATE_PLACEHOLDER) as g:
                g.set_attribute(spans.Attrs.GATE_VERSION, _PLACEHOLDER_VERSION)
                ref_err = validate_refs(claim.body_markdown, len(claim.provenance))
                if ref_err is not None:
                    g.set_attribute(spans.Attrs.GATE_VERDICT, spans.VERDICT_RETRACTED)
                    g.set_attribute(spans.Attrs.GATE_REASON, ref_err.to_human_string())
                    _set_retracted(claim, ref_err.to_human_string())
                    claim_span.set_attribute(
                        spans.Attrs.CLAIM_VERDICT, spans.VERDICT_RETRACTED
                    )
                    await sink.emit("Claim", claim)
                    continue
                g.set_attribute(spans.Attrs.GATE_VERDICT, spans.VERDICT_APPROVED)

            # Structural value compare (ship 5a). Always runs; only
            # retracts when dont_fabricate is on.
            with _tracer.start_as_current_span(spans.GATE_STRUCTURAL) as g:
                g.set_attribute(spans.Attrs.GATE_VERSION, structural_module.VERSION)
                g.set_attribute(
                    spans.Attrs.GATE_BINDING_SIZE, len(bindings.all_numbers())
                )
                struct_err = verify_chip_values(list(claim.provenance), bindings)
                if struct_err is not None and envelope.switches.dont_fabricate:
                    g.set_attribute(spans.Attrs.GATE_VERDICT, spans.VERDICT_RETRACTED)
                    g.set_attribute(
                        spans.Attrs.GATE_REASON, struct_err.to_human_string()
                    )
                    g.set_attribute(
                        spans.Attrs.GATE_FAILED_CHIP,
                        str(getattr(struct_err, "kind", "unknown")),
                    )
                    _set_retracted(claim, struct_err.to_human_string())
                    claim_span.set_attribute(
                        spans.Attrs.CLAIM_VERDICT, spans.VERDICT_RETRACTED
                    )
                    await sink.emit("Claim", claim)
                    continue
                g.set_attribute(spans.Attrs.GATE_VERDICT, spans.VERDICT_APPROVED)

            # Constitution gate (only when defend_constitution_judge is
            # on; the constitution covers domain + identity + citation).
            # The constitution agent's gen_ai.chat span auto-nests under
            # this gate.constitution span via OTel context.
            if envelope.switches.stay_in_role.defend_constitution_judge:
                with _tracer.start_as_current_span(spans.GATE_CONSTITUTION) as g:
                    g.set_attribute(
                        spans.Attrs.GATE_VERSION, constitution_module.VERSION
                    )
                    verdict = await judge_claim(
                        constitution_agent,
                        headline=claim.headline,
                        body_markdown=claim.body_markdown,
                        provenance_summary=_summarize_provenance(claim.provenance),
                    )
                    g.set_attribute(
                        spans.Attrs.GATE_VERDICT, _normalize_verdict(verdict.verdict)
                    )
                    if verdict.reason:
                        g.set_attribute(spans.Attrs.GATE_REASON, verdict.reason)
                    if verdict.verdict in ("retract", "reject"):
                        _set_retracted(
                            claim,
                            verdict.reason or f"constitution {verdict.verdict}",
                        )
                        claim_span.set_attribute(
                            spans.Attrs.CLAIM_VERDICT,
                            _normalize_verdict(verdict.verdict),
                        )
                        await sink.emit("Claim", claim)
                        continue

            # Approved.
            claim_span.set_attribute(spans.Attrs.CLAIM_VERDICT, spans.VERDICT_APPROVED)
            await sink.emit("Claim", claim)
            approved_claims.append(claim)

    # ------ Narrative leg ------
    # Wrapped in narrative.emitted so the trace tree shows the
    # narrative path with its placeholder validation + optional
    # constitution gate as children. One span per turn.
    assembled_provenance: list[provenance_pb2.ProvenanceRef] = []
    for c in approved_claims:
        assembled_provenance.extend(c.provenance)

    # The constitution gate's policy LLM call is consistently the
    # longest stage of a turn (~11s p50). Without this Progress frame
    # the UI sits silent between the primary LLM finishing and the
    # gated narrative arriving.
    if envelope.switches.stay_in_role.defend_constitution_judge:
        await sink.emit(
            "Progress", sse_pb2.Progress(phase="judging", detail="constitution gate")
        )

    sse_text = ""
    with _tracer.start_as_current_span(spans.NARRATIVE_EMITTED) as nar_span:
        # Apply the narrative-output channel switch. When off,
        # `sse_text` is "" and the cockpit instruments
        # (narrative.suppressed=true, pre_suppression_chars) are
        # stamped on this span. When on, sse_text == narrative_text
        # and the usual length attribute is stamped. Placeholder
        # validation below still runs against the model's original
        # text so a "model wrote uncited audit numbers but we
        # suppressed" path is observable distinct from "model wrote
        # nothing".
        sse_text = _resolve_narrative_text(
            narrative_text,
            narrative_output_enabled=envelope.switches.channels.narrative_output_enabled,
            nar_span=nar_span,
        )
        nar_span.set_attribute(
            spans.Attrs.NARRATIVE_ASSEMBLED_PROVENANCE_COUNT, len(assembled_provenance)
        )
        # Full narrative text capped to NARRATIVE_TEXT_MAX_BYTES;
        # overflow marker matches the primitive-payload convention so
        # eval probes (and Langfuse) can detect truncation. When
        # suppressed sse_text is "", which we still stamp so
        # downstream queries see an empty field rather than a missing
        # one.
        if len(sse_text) <= spans.NARRATIVE_TEXT_MAX_BYTES:
            _capped_narrative = sse_text
        else:
            _capped_narrative = (
                sse_text[: spans.NARRATIVE_TEXT_MAX_BYTES]
                + f" ...[truncated, total={len(sse_text)}]"
            )
        nar_span.set_attribute(spans.Attrs.NARRATIVE_TEXT, _capped_narrative)

        # Placeholder validation against assembled provenance. Reuses
        # the gate.placeholder span name for symmetry with the
        # per-claim path; a downstream filter on
        # SpanName='gate.placeholder' AND parent_span name discriminates
        # which case it covered.
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
                    text=sse_text, reason=ref_err.to_human_string()
                )
                if debug_public:
                    ret.debug_reason = f"placeholder_validate: {ref_err.kind}"
                await sink.emit("NarrativeRetracted", ret)
            else:
                pg.set_attribute(spans.Attrs.GATE_VERDICT, spans.VERDICT_APPROVED)

        if ref_err is not None:
            pass  # Already emitted; fall through to aggregate-stamp + return
        elif envelope.switches.stay_in_role.defend_constitution_judge:
            # Constitution gate on narrative. The constitution agent's
            # gen_ai.chat span auto-nests under this.
            with _tracer.start_as_current_span(spans.GATE_NARRATIVE_CONSTITUTION) as g:
                g.set_attribute(spans.Attrs.GATE_VERSION, constitution_module.VERSION)
                # `same_turn_claims` carries this turn's approved claims
                # PLUS the driver's prior approved claims (chat: thread
                # history; monitor: empty). The split lets the
                # constitution judge see same-turn coherence and prior-
                # turn context without conflating them.
                verdict = await judge_narrative(
                    constitution_agent,
                    text=narrative_text,
                    same_turn_claims=_claims_to_judgement_payload(approved_claims)
                    + _claims_to_judgement_payload(list(prior_claims)),
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
                        text=sse_text,
                        reason=verdict.reason or f"constitution {verdict.verdict}",
                    )
                    if debug_public:
                        ret.debug_reason = f"constitution: {verdict.reason}"
                    await sink.emit("NarrativeRetracted", ret)
                else:
                    nar_span.set_attribute(
                        spans.Attrs.NARRATIVE_VERDICT, spans.VERDICT_APPROVED
                    )
                    nar = narrative_pb2.NarrativeWithRefs(
                        text=sse_text, provenance=assembled_provenance
                    )
                    await sink.emit("Narrative", nar)
        else:
            # raw-llm or agent-without-grounding: skip constitution.
            nar_span.set_attribute(
                spans.Attrs.NARRATIVE_VERDICT, spans.VERDICT_APPROVED
            )
            nar = narrative_pb2.NarrativeWithRefs(
                text=sse_text, provenance=assembled_provenance
            )
            await sink.emit("Narrative", nar)

    # ------ Per-turn aggregates ------
    # Stamped on the active mcae.turn span so SQL queries can answer
    # "how many claims got approved this turn" without scanning per-
    # claim spans. Replaces the prior TURN_COMPLETED ledger row.
    turn_span.set_attribute(spans.Attrs.TURN_CLAIMS_EMITTED, len(deps.emitted_claims))
    turn_span.set_attribute(spans.Attrs.TURN_CLAIMS_APPROVED, len(approved_claims))
    turn_span.set_attribute(spans.Attrs.TURN_TOOL_CALLS, len(deps.tool_call_records))
    # Aggregate uses the post-suppression length so the cockpit
    # instrument matches what actually left the agent. The pre-
    # suppression length lives on the narrative span as
    # `mcae.narrative.pre_suppression_chars` for probes that want
    # both numbers.
    turn_span.set_attribute(spans.Attrs.TURN_NARRATIVE_CHARS, len(sse_text))

    return TurnOutcome(
        new_message_history=list(result.all_messages()),
        tool_call_records=list(deps.tool_call_records),
        approved_claims=approved_claims,
    )


# ---------------------------------------------------------------------------
# Loop-body helpers (role-agnostic; private to the core)
# ---------------------------------------------------------------------------


def _resolve_narrative_text(
    raw_text: str,
    *,
    narrative_output_enabled: bool,
    nar_span: trace.Span,
) -> str:
    """Apply the narrative-output channel switch to the model's prose
    and stamp the matching cockpit instruments on `nar_span`.

    When the channel is on, returns `raw_text` unchanged and stamps
    the usual length attribute. When the channel is off, returns ""
    and stamps `narrative.suppressed=true` plus
    `narrative.pre_suppression_chars=len(raw_text)` so probes can
    assert "model wrote N chars but the cockpit suppressed them"
    without inspecting SSE bytes.
    """
    if narrative_output_enabled:
        nar_span.set_attribute(spans.Attrs.NARRATIVE_LENGTH_CHARS, len(raw_text))
        return raw_text
    nar_span.set_attribute(spans.Attrs.NARRATIVE_SUPPRESSED, True)
    nar_span.set_attribute(spans.Attrs.NARRATIVE_LENGTH_CHARS, 0)
    nar_span.set_attribute(
        spans.Attrs.NARRATIVE_PRE_SUPPRESSION_CHARS, len(raw_text)
    )
    return ""


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
        elif (
            kind == "edge"
            and r.edge_id is not None
            and r.src is not None
            and r.dst is not None
        ):
            out.append(
                provenance_pb2.ProvenanceRef(
                    edge=provenance_pb2.EdgeRef(id=r.edge_id, src=r.src, dst=r.dst)
                )
            )
        elif kind == "time_range" and r.from_s is not None and r.to_s is not None:
            out.append(
                provenance_pb2.ProvenanceRef(
                    time_range=provenance_pb2.TimeRangeRef(
                        from_s=r.from_s, to_s=r.to_s
                    )
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


def _build_claim(
    *,
    input_: EmitClaimInput,
    thread_id: str,
    turn_started_at_ms: int,
) -> claim_pb2.Claim:
    """Stamp the runtime-controlled fields onto a Claim drafted by
    the model. Verdict starts Approved; gates may downgrade to
    Retracted."""
    kind_enum = _CLAIM_KIND_MAP.get(
        input_.kind.upper(), claim_pb2.CLAIM_KIND_UNSPECIFIED
    )
    elapsed = max(0, int(time.time() * 1000) - turn_started_at_ms)
    claim = claim_pb2.Claim(
        id=str(uuid.uuid4()),
        thread_id=thread_id,
        kind=kind_enum,
        headline=input_.headline,
        body_markdown=input_.body_markdown,
        provenance=_map_provenance(input_.provenance),
        emitted_at_ms=min(elapsed, 0xFFFFFFFF),
    )
    for n in input_.support_numbers:
        claim.support_numbers.add(
            metric=n.metric, value=n.value, support=list(n.support or [])
        )
    # Default Approved; gates downgrade.
    claim.policy_verdict.approved.SetInParent()
    return claim


def _set_retracted(claim: claim_pb2.Claim, reason: str) -> None:
    claim.policy_verdict.retracted.reason = reason


def _normalize_verdict(v: str) -> str:
    """Map constitution-gate verdicts onto the span verdict vocabulary
    so all gate.* + claim.emitted + narrative.emitted spans share one
    set of strings (`approved` / `retracted` / `reject`)."""
    return {
        "approve": spans.VERDICT_APPROVED,
        "retract": spans.VERDICT_RETRACTED,
        "reject": spans.VERDICT_REJECT,
    }.get(v, v)


def _summarize_provenance(refs) -> list[dict]:
    """Compact form of provenance for the constitution gate's user
    payload. Mirrors the JSON the Rust gate sends."""
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
            out.append(
                {
                    "kind": "time_range",
                    "from_s": r.time_range.from_s,
                    "to_s": r.time_range.to_s,
                }
            )
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
    summary."""
    return [
        {
            "headline": c.headline,
            "body_markdown": c.body_markdown,
            "provenance_summary": _summarize_provenance(c.provenance),
        }
        for c in claims
    ]
