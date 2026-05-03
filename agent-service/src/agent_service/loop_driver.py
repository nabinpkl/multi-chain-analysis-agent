"""Phase II loop driver. Replaces the Rust `loop.rs` orchestration
end-to-end. Runs one turn of one session, emits SSE frames as a
generator that `main.py`'s SSE handler streams to the browser.

Per turn:

1. Look up or create the `AgentThread`. Acquire the per-thread lock
   so concurrent SSE GETs on the same thread serialize.
2. (ship 4) If `dont_repeat_yourself` is on AND `turn >= 2`, run the
   repeat detector. On hit: replay prior turn's tool calls, run diff,
   emit `NoMovement` or `ChangedSince`, write ledger events, return.
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
12. Release snapshot lease, write closing ledger events, emit `Done`.

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
from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits

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

from .agent import AgentDeps, EmitClaimInput, ToolCallRecord
from .boundary import build_context_block
from .diff import diff_outputs, spec_for
from .ledger.writer import Ledger, LedgerEventDraft, LedgerEventKind
from .policy.binding_store import PrimitiveBindingStore
from .policy.constitution import (
    ConstitutionVerdict,
    judge_claim,
    judge_narrative,
)
from .policy.placeholder import validate_refs
from .policy.structural import verify_chip_values
from .primitive_client import PrimitiveClient, PrimitiveError
from .repeat_detector import RepeatDetectorOutcome, detect_repeat
from .thread_state import (
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
    ledger: Ledger
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
    listing the changed fields."""
    prior_calls = thread.tool_calls_per_turn.get(repeat_of_turn, [])
    primitives_replayed: list[str] = []
    all_changed: list[diff_pb2.FieldDelta] = []
    total_unchanged = 0

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

    # Ledger: record the repeat decision + diff result.
    await handles.ledger.write(
        LedgerEventDraft(
            session_id=session_id,
            kind=LedgerEventKind.TURN_DIFF,
            payload={
                "repeat_of_turn": repeat_of_turn,
                "primitives_replayed": primitives_replayed,
                "changed_count": len(all_changed),
                "unchanged_count": total_unchanged,
            },
        )
    )


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
            turn = thread.turn_count
            thread.turn_count += 1
            thread.record_turn_user_question(turn, request.user_question)

            # ------ Session-started ledger event ------
            await handles.ledger.write(
                LedgerEventDraft(
                    session_id=session_id,
                    kind=LedgerEventKind.SESSION_STARTED,
                    payload={
                        "thread_id": thread_id,
                        "turn": turn,
                        "user_question": request.user_question,
                    },
                )
            )

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
                    outcome = await detect_repeat(
                        prior_qs, request.user_question, handles.repeat_agent
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
                result = await handles.primary_agent.run(user_msg, **run_kwargs)
            except Exception as e:  # noqa: BLE001
                log.exception("primary_agent_run_failed", session_id=session_id)
                yield _emit_error(e, debug_public=handles.debug_public)
                yield _terminal_done(session_id, session_started_at_ms)
                return

            narrative_text: str = result.output

            # ------ Process emitted claims ------
            approved_claims: list[claim_pb2.Claim] = []
            for ec in deps.emitted_claims:
                claim = _build_claim(
                    input_=ec,
                    session_id=session_id,
                    session_started_at_ms=session_started_at_ms,
                )

                # Rule 1: empty provenance always retracts.
                if not claim.provenance:
                    _set_retracted(claim, "claim has empty provenance; cite at least one entity")
                    yield _frame("Claim", claim)
                    continue

                # Deterministic placeholder validation.
                ref_err = validate_refs(claim.body_markdown, len(claim.provenance))
                if ref_err is not None:
                    _set_retracted(claim, ref_err.to_human_string())
                    yield _frame("Claim", claim)
                    continue

                # Structural value compare (ship 5a). Always run when
                # there's a binding store with anything in it.
                struct_err = verify_chip_values(list(claim.provenance), thread.bindings)
                if struct_err is not None and request.switches.dont_fabricate:
                    _set_retracted(claim, struct_err.to_human_string())
                    yield _frame("Claim", claim)
                    continue

                # Constitution gate (only when stay_in_role is on; the
                # constitution covers domain + identity + citation).
                if request.switches.stay_in_role:
                    verdict = await judge_claim(
                        handles.constitution_agent,
                        headline=claim.headline,
                        body_markdown=claim.body_markdown,
                        provenance_summary=_summarize_provenance(claim.provenance),
                    )
                    if verdict.verdict in ("retract", "reject"):
                        _set_retracted(
                            claim, verdict.reason or f"constitution {verdict.verdict}"
                        )
                        yield _frame("Claim", claim)
                        continue

                # Approved.
                yield _frame("Claim", claim)
                approved_claims.append(claim)
                thread.record_claim(claim)

            # ------ Narrative leg ------
            assembled_provenance: list[provenance_pb2.ProvenanceRef] = []
            for c in approved_claims:
                assembled_provenance.extend(c.provenance)

            # Placeholder validation against assembled provenance.
            ref_err = validate_refs(narrative_text, len(assembled_provenance))
            if ref_err is not None:
                ret = narrative_pb2.NarrativeRetracted(
                    text=narrative_text, reason=ref_err.to_human_string()
                )
                if handles.debug_public:
                    ret.debug_reason = f"placeholder_validate: {ref_err.kind}"
                yield _frame("NarrativeRetracted", ret)
            elif request.switches.stay_in_role:
                # Constitution gate on narrative (ship 5a's prose-judgement layer).
                verdict = await judge_narrative(
                    handles.constitution_agent,
                    text=narrative_text,
                    same_turn_claims=_claims_to_judgement_payload(approved_claims)
                    + _claims_to_judgement_payload(thread.claims[:-len(approved_claims) or None]),
                )
                if verdict.verdict in ("retract", "reject"):
                    ret = narrative_pb2.NarrativeRetracted(
                        text=narrative_text,
                        reason=verdict.reason or f"constitution {verdict.verdict}",
                    )
                    if handles.debug_public:
                        ret.debug_reason = f"constitution: {verdict.reason}"
                    yield _frame("NarrativeRetracted", ret)
                else:
                    nar = narrative_pb2.NarrativeWithRefs(
                        text=narrative_text, provenance=assembled_provenance
                    )
                    yield _frame("Narrative", nar)
            else:
                # raw-llm or agent-without-grounding: skip constitution.
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

            # Final ledger row for this turn.
            await handles.ledger.write(
                LedgerEventDraft(
                    session_id=session_id,
                    kind=LedgerEventKind.TURN_COMPLETED,
                    payload={
                        "turn": turn,
                        "claims_emitted": len(deps.emitted_claims),
                        "claims_approved": len(approved_claims),
                        "tool_calls": len(deps.tool_call_records),
                        "narrative_chars": len(narrative_text),
                    },
                )
            )

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
        # Ledger: drop the per-session sequence counter so memory stays bounded.
        try:
            handles.ledger.drop_session(session_id)
        except Exception:  # noqa: BLE001
            pass


def _terminal_done(session_id: str, session_started_at_ms: int) -> dict[str, str]:
    elapsed_ms = max(0, int(time.time() * 1000) - session_started_at_ms)
    return _frame(
        "Done",
        session_pb2.AgentDone(session_id=session_id, elapsed_ms=min(elapsed_ms, 0xFFFFFFFF)),
    )


def _emit_error(exc: Exception, *, debug_public: bool) -> dict[str, str]:
    err = sse_pb2.Error(message=_GENERIC_ERROR_MSG)
    if debug_public:
        err.debug_message = f"{type(exc).__name__}: {exc}"
    return _frame("Error", err)
