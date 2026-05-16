"""Shared post-tools phase for both runtimes.

Owns everything that happens AFTER the primary agent's tool-dispatch
loop completes and BEFORE the driver writes the turn outcome back to
its own state. This is the second half of the role-agnostic core:
turn the model's emitted claims and narrative_text into gated SSE
frames and a NarrativeSnapshot.

The function is single-source so the per-claim constitution gate,
narrative leg, channel suppression, and turn-aggregate stamping
behave identically on pydantic-ai and codex. Before this module
existed each runtime inlined its own copy of the gate stack, and
codex's copy silently skipped `judge_claim` for several months.

Caller responsibilities, NOT done here:

* Open / close the OTel `mcae.turn` root span (this module reads the
  active span and stamps attributes on it).
* Run the primary agent loop and drain claims.
* Maintain `tool_call_records` and ship them onto the turn outcome.
* Release the snapshot lease and persist driver-specific state.

What this module does:

* Per-claim gate stack: empty-provenance retract → placeholder →
  structural (only retracts when `dont_fabricate` is on) →
  constitution (only when `defend_constitution_judge` is on). Emits
  one `mcae.claim.emitted` span per claim wrapping the gate spans.
* Narrative leg: channel-output suppression → placeholder gate →
  optional constitution gate → SSE Narrative or NarrativeRetracted
  emit. One `mcae.narrative.emitted` span wraps the whole leg.
* Stamps per-turn aggregates on the active turn span:
  `claims_emitted`, `claims_approved`, `tool_calls`,
  `budget_exhausted`, `narrative_chars` (post-suppression).
* Builds and returns the `NarrativeSnapshot` for the driver to
  persist.

The post-suppression `narrative_chars` stamp matches the pydantic-ai
historical shape (`len(sse_text)` after channel suppression); codex
previously stamped the pre-suppression length, which broke probes
asserting `mcae.turn.narrative_chars=0` when the narrative channel
was off.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace

from agent_service import spans
from agent_service.agent import EmitClaimInput
from agent_service.core.envelope import TurnEnvelope
from agent_service.core.sink import TurnSink
from agent_service.policy import constitution as constitution_module
from agent_service.policy import structural as structural_module
from agent_service.policy.binding_store import PrimitiveBindingStore
from agent_service.policy.constitution import judge_claim, judge_narrative
from agent_service.policy.placeholder import validate_refs
from agent_service.policy.structural import verify_chip_values
from agent_service.thread_state import NarrativeSnapshot
from multichain.wire.agent.v1 import claim_pb2, narrative_pb2, sse_pb2
from multichain.wire.shared.v1 import provenance_pb2

_tracer = trace.get_tracer(__name__)


# The placeholder gate is deterministic and has no version notion of
# its own; v1 is a stable signal for eval probes that something other
# than "the gate didn't run" happened.
_PLACEHOLDER_VERSION = "v1"


_CLAIM_KIND_MAP = {
    "PROFILE": claim_pb2.CLAIM_KIND_PROFILE,
    "PATTERN": claim_pb2.CLAIM_KIND_PATTERN,
    "COMPARISON": claim_pb2.CLAIM_KIND_COMPARISON,
    "SUMMARY": claim_pb2.CLAIM_KIND_SUMMARY,
    "PULSE": claim_pb2.CLAIM_KIND_PULSE,
}


@dataclass(slots=True)
class PostToolsOutcome:
    """What `run_post_tools_phase` hands back to the driver.

    `approved_claims` is the subset of `emitted_claims` that survived
    every gate. `narrative_snapshot` is None only on the early-error
    paths inside the function (today there are none; field stays
    Optional for the boundary-rail caller pattern).
    """

    approved_claims: list[claim_pb2.Claim]
    narrative_snapshot: NarrativeSnapshot | None


def resolve_narrative_text(
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

    Also used by `core.run.emit_unsafe_input_rejection_observability`
    for the boundary-rejection narrative; that's why this lives at
    module scope rather than nested in `run_post_tools_phase`.
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


def provenance_refs_from_json(
    refs_json: list,
) -> list[provenance_pb2.ProvenanceRef]:
    """Parse MCP-envelope provenance JSON into proto `ProvenanceRef`s.

    Rust's `primitives::types::ProvenanceRef` enum serializes via serde
    with `tag = "kind", rename_all = "kebab-case"`, so each entry is
    `{"kind": "wallet", "addr": "..."}` /
    `{"kind": "community", "id": 8}` /
    `{"kind": "time-range", "from_s": ..., "to_s": ...}` / etc. This
    function maps that shape to the protobuf-message form the
    `PrimitiveBindingStore.record(...)` API expects.

    Used by both runtimes after they receive a primitive output from
    Rust over MCP: codex's `_record_tool_output_binding` consumes
    structured content from the codex CLI envelope; pydantic-ai's
    `mcp_hook.process_tool_call` consumes the same shape directly from
    pydantic-ai's `direct_call_tool` return value.

    Malformed entries are dropped with a structlog warning rather than
    aborting the whole binding population; this matches the existing
    codex behavior and keeps a single bad chip from breaking the gate
    stack for the rest of the turn's claims.
    """
    out: list[provenance_pb2.ProvenanceRef] = []
    for r in refs_json:
        if not isinstance(r, dict):
            continue
        kind = r.get("kind", "")
        try:
            if kind == "wallet" and "addr" in r:
                wallet = provenance_pb2.WalletRef(addr=r["addr"])
                if r.get("idx") is not None:
                    wallet.idx = int(r["idx"])
                out.append(provenance_pb2.ProvenanceRef(wallet=wallet))
            elif kind == "community" and "id" in r:
                out.append(
                    provenance_pb2.ProvenanceRef(
                        community=provenance_pb2.CommunityRef(id=int(r["id"]))
                    )
                )
            elif kind == "edge" and {"id", "src", "dst"} <= r.keys():
                out.append(
                    provenance_pb2.ProvenanceRef(
                        edge=provenance_pb2.EdgeRef(
                            id=r["id"], src=int(r["src"]), dst=int(r["dst"])
                        )
                    )
                )
            elif kind == "time-range" and {"from_s", "to_s"} <= r.keys():
                out.append(
                    provenance_pb2.ProvenanceRef(
                        time_range=provenance_pb2.TimeRangeRef(
                            from_s=int(r["from_s"]),
                            to_s=int(r["to_s"]),
                        )
                    )
                )
            elif kind == "number" and {"metric", "value"} <= r.keys():
                out.append(
                    provenance_pb2.ProvenanceRef(
                        number=provenance_pb2.NumberRef(
                            metric=r["metric"],
                            value=float(r["value"]),
                            support=list(r.get("support") or []),
                        )
                    )
                )
        except (TypeError, ValueError):
            # Drop the malformed entry; downstream consumers see a missing
            # chip rather than a thrown exception. Caller-side logging is
            # sufficient since the entry never reaches the gate stack.
            continue
    return out


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


async def run_post_tools_phase(
    *,
    emitted_claims: Sequence[EmitClaimInput],
    narrative_text: str,
    bindings: PrimitiveBindingStore,
    envelope: TurnEnvelope,
    thread_id: str,
    turn_started_at_ms: int,
    prior_claims: Iterable[claim_pb2.Claim],
    sink: TurnSink,
    debug_public: bool,
    turn_span: trace.Span,
    tool_calls_count: int,
    budget_exhausted: bool,
) -> PostToolsOutcome:
    """Run the gate stack and narrative leg, emit SSE frames, stamp
    turn aggregates, return the snapshot.

    See module docstring for the responsibilities split. The function
    NEVER raises; gate failures retract the relevant claim or narrative
    in place. Caller wraps in its own try/finally for snapshot release
    and span close.
    """
    approved_claims: list[claim_pb2.Claim] = []
    for ec in emitted_claims:
        claim = _build_claim(
            input_=ec,
            thread_id=thread_id,
            turn_started_at_ms=turn_started_at_ms,
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

            # Structural value compare. Always runs; only retracts
            # when dont_fabricate is on.
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
                        headline=claim.headline,
                        body_markdown=claim.body_markdown,
                        provenance_summary=_summarize_provenance(claim.provenance),
                        live_window_secs=envelope.live_window_secs,
                        llm_override=envelope.policy_llm_override,
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
            "Progress",
            sse_pb2.Progress(phase="judging", detail="constitution gate"),
        )

    sse_text = ""
    narrative_snapshot: NarrativeSnapshot | None = None
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
        sse_text = resolve_narrative_text(
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
                placeholder_reason = ref_err.to_human_string()
                ret = narrative_pb2.NarrativeRetracted(
                    text=sse_text, reason=placeholder_reason
                )
                if debug_public:
                    ret.debug_reason = f"placeholder_validate: {ref_err.kind}"
                await sink.emit("NarrativeRetracted", ret)
                narrative_snapshot = NarrativeSnapshot(
                    text=sse_text,
                    provenance=list(assembled_provenance),
                    retracted_reason=placeholder_reason,
                )
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
                    text=narrative_text,
                    same_turn_claims=_claims_to_judgement_payload(approved_claims)
                    + _claims_to_judgement_payload(list(prior_claims)),
                    live_window_secs=envelope.live_window_secs,
                    llm_override=envelope.policy_llm_override,
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
                    constitution_reason = (
                        verdict.reason or f"constitution {verdict.verdict}"
                    )
                    ret = narrative_pb2.NarrativeRetracted(
                        text=sse_text, reason=constitution_reason,
                    )
                    if debug_public:
                        ret.debug_reason = f"constitution: {verdict.reason}"
                    await sink.emit("NarrativeRetracted", ret)
                    narrative_snapshot = NarrativeSnapshot(
                        text=sse_text,
                        provenance=list(assembled_provenance),
                        retracted_reason=constitution_reason,
                    )
                else:
                    nar_span.set_attribute(
                        spans.Attrs.NARRATIVE_VERDICT, spans.VERDICT_APPROVED
                    )
                    nar = narrative_pb2.NarrativeWithRefs(
                        text=sse_text, provenance=assembled_provenance
                    )
                    await sink.emit("Narrative", nar)
                    narrative_snapshot = NarrativeSnapshot(
                        text=sse_text,
                        provenance=list(assembled_provenance),
                    )
        else:
            # raw-llm or agent-without-grounding: skip constitution.
            nar_span.set_attribute(
                spans.Attrs.NARRATIVE_VERDICT, spans.VERDICT_APPROVED
            )
            nar = narrative_pb2.NarrativeWithRefs(
                text=sse_text, provenance=assembled_provenance
            )
            await sink.emit("Narrative", nar)
            narrative_snapshot = NarrativeSnapshot(
                text=sse_text,
                provenance=list(assembled_provenance),
            )

    # ------ Per-turn aggregates ------
    # Stamped on the active mcae.turn span so SQL queries can answer
    # "how many claims got approved this turn" without scanning per-
    # claim spans. Replaces the prior TURN_COMPLETED ledger row.
    turn_span.set_attribute(spans.Attrs.TURN_CLAIMS_EMITTED, len(emitted_claims))
    turn_span.set_attribute(spans.Attrs.TURN_CLAIMS_APPROVED, len(approved_claims))
    turn_span.set_attribute(spans.Attrs.TURN_TOOL_CALLS, tool_calls_count)
    # True when the per-tool budget interceptor short-circuited at
    # least one dispatch this turn. Pairs with TURN_TOOL_CALLS:
    # when budget_exhausted is true, tool_calls is clamped at the
    # cap (8 by default).
    turn_span.set_attribute(spans.Attrs.TURN_BUDGET_EXHAUSTED, budget_exhausted)
    # Aggregate uses the post-suppression length so the cockpit
    # instrument matches what actually left the agent. The pre-
    # suppression length lives on the narrative span as
    # `mcae.narrative.pre_suppression_chars` for probes that want
    # both numbers.
    turn_span.set_attribute(spans.Attrs.TURN_NARRATIVE_CHARS, len(sse_text))

    return PostToolsOutcome(
        approved_claims=approved_claims,
        narrative_snapshot=narrative_snapshot,
    )
