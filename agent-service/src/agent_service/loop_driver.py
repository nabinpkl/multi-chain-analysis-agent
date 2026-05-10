"""Chat driver for the agent core. One async generator per turn,
yields SSE frame dicts that `main.py` streams to the browser.

This module is the chat surface only: thread / session / turn-index
bookkeeping, repeat detection, snapshot lease management, OTel turn
span lifecycle, terminal frame emission. The role-agnostic loop body
lives in `agent_service.core.run`. Per AGENTS.md "no god component"
this file used to be one ~1000-line ball of intake + loop + output;
the loop body moved out so monitor / pulse / peer-consult drivers
can reuse it without copy-paste.

Per turn:

1. Look up or create the `AgentThread`. Acquire the per-thread lock
   so concurrent SSE GETs on the same thread serialize.
2. Open `mcae.turn` root span. Stamp chat-specific attrs
   (session/thread/turn-index/user-question).
3. (ship 4) If `dont_repeat_yourself` is on AND `turn >= 2`, run the
   repeat detector. On hit: replay prior turn's tool calls, run
   diff, emit `NoMovement` or `ChangedSince`, return.
4. Open snapshot lease.
5. Build a `TurnEnvelope` from the request, instantiate `SseSink`,
   call `core.run_one_turn(envelope, sink)`. Drain the sink while the
   core runs.
6. On core return, write the outcome (message history, tool calls,
   approved claims) back into thread state.
7. Emit `Done`. Release snapshot lease in finally.

Single async generator entry point: `run_turn(...)`. Yields
`{"event": <name>, "data": <proto-canonical-json-str>}` dicts that
match `EventSourceResponse`'s expected shape.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import structlog
from google.protobuf import json_format
from opentelemetry import trace
from pydantic_ai import Agent

from agent_service import spans
from agent_service.agent import build_agent
from agent_service.core import (
    SseSink,
    TurnEnvelope,
    resolve_run_type,
    run_one_turn,
)
from agent_service.diff import diff_outputs, spec_for
from agent_service.llm_retry import begin_role_timing_capture
from agent_service.policy.constitution import build_constitution_agent
from agent_service.primitive_client import PrimitiveClient, PrimitiveError
from agent_service.repeat_detector import build_repeat_agent, detect_repeat
from agent_service.thread_state import (
    AgentThread,
    ThreadRegistry,
    TurnToolCallRecord,
)
from multichain.wire.agent.v1 import (
    diff_pb2,
    session_pb2,
    sse_pb2,
)

log = structlog.get_logger(__name__)

# Module-level tracer. init_otel() registered the global TracerProvider
# at app startup; this resolves through to it.
_tracer = trace.get_tracer(__name__)


def _override_or_none(role_override):
    """Treat empty `RoleOverride` (default-constructed proto with empty
    provider AND empty model_id) as None so the per-turn rebuild path
    only fires when an actual override is set. Production traffic
    sends `llm_override` unset; the dev frontend sends a populated
    field only when a developer flips the toggle.
    """
    if role_override is None:
        return None
    if not role_override.provider and not role_override.model_id:
        return None
    return role_override


# Generic user-facing error message; raw exception only crosses the
# wire when AGENT_DEBUG_PUBLIC=1.
_GENERIC_ERROR_MSG = (
    "Couldn't produce a valid response. Try rephrasing or try again."
)


@dataclass
class LoopHandles:
    """Bundle of long-lived dependencies the chat driver reads on
    each turn. Built once in `main.py`'s lifespan handler and passed
    into `run_turn` via app state.

    The core uses `primary_agent`, `constitution_agent`, and
    `primitive_client` directly (no handles bundle) so future drivers
    don't inherit chat-specific fields they don't need. The chat-only
    fields here (`threads`, `repeat_agent`) stay on this bundle.
    """

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
    nest as primitive.* spans automatically.
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
            prose = _format_changed_prose(all_changed)
            cs = diff_pb2.ChangedSince(prior_turn=repeat_of_turn, delta=delta, prose=prose)
            yield _frame("ChangedSince", cs)


def _format_changed_prose(changes: list[diff_pb2.FieldDelta]) -> str:
    """Deterministic single-paragraph summary of what diff fields
    moved. Plain prose; no chips, no audit numbers."""
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
# Main turn entry point (chat driver)
# ---------------------------------------------------------------------------


async def run_turn(
    *,
    handles: LoopHandles,
    request,  # session_pb2.AgentRequest
    session_id: str,
    thread_id: str,
    session_started_at_ms: int,
) -> AsyncIterator[dict[str, str]]:
    """One turn of one chat session. Yields SSE frame dicts.

    Builds a `TurnEnvelope` + `SseSink`, runs `core.run_one_turn` as
    a concurrent task while draining the sink as SSE frames, then
    writes the core's outcome back into thread state.
    """
    # Install a per-turn role-timing accumulator before any LLM call
    # could fire. Every successful call inside the turn (primary
    # agent, constitution gates, repeat detector) attributes its
    # wall-time into this bucket via `with_provider_retry`. We read
    # it back when stamping `AgentDone.role_timings` so the builder
    # view's Models panel can show "primary 73.8s last call" under
    # each role row.
    role_timings = begin_role_timing_capture()
    snapshot_id: str | None = None
    # Resolve per-turn agents up front. When the request carries an
    # `llm_override`, build fresh agents pinned to the requested
    # provider for this turn; otherwise reuse the lifespan-cached
    # ones. Sub-millisecond per build, no I/O. Constitution + repeat
    # share the "policy" override field (both are cheap policy-tier
    # gates); primary stands alone.
    primary_override = _override_or_none(
        getattr(request.llm_override, "primary", None)
        if request.HasField("llm_override")
        else None
    )
    policy_override = _override_or_none(
        getattr(request.llm_override, "policy", None)
        if request.HasField("llm_override")
        else None
    )
    primary_agent_for_turn = (
        build_agent(llm_override=primary_override)
        if primary_override is not None
        else handles.primary_agent
    )
    constitution_agent_for_turn = (
        build_constitution_agent(llm_override=policy_override)
        if policy_override is not None
        else handles.constitution_agent
    )
    repeat_agent_for_turn = (
        build_repeat_agent(llm_override=policy_override)
        if policy_override is not None
        else handles.repeat_agent
    )
    try:
        thread, lock = await handles.threads.get_or_create(thread_id)
        async with lock:
            # Root span for this turn. Carries the four turn-scoped
            # attrs (session/thread/turn-index/run-type) so SQL filters
            # can `WHERE SpanName='agent.turn' AND
            # SpanAttributes['session.id']='...'` then join children
            # via TraceId. Everything below opens under this context,
            # including Pydantic AI's auto agent.run / gen_ai.chat /
            # execute_tool spans. OTel's span context manager is sync
            # (no __aexit__), so it must nest inside the async-with
            # rather than combine. `yield` inside is fine: OTel uses
            # contextvars so the active-span stack is preserved across
            # async suspension points.
            with _tracer.start_as_current_span(spans.AGENT_TURN) as turn_span:
                turn = thread.turn_count
                thread.turn_count += 1
                thread.record_turn_user_question(turn, request.user_question)

                # Chat-specific span attrs. Core stamps the role-
                # agnostic ones (run_type, channel switches,
                # per-turn aggregates). RUN_TYPE is also stamped here
                # in case the core never gets called (repeat path
                # short-circuit) and downstream queries expect it.
                turn_span.set_attribute(spans.Attrs.SESSION_ID, session_id)
                turn_span.set_attribute(spans.Attrs.THREAD_ID, thread_id)
                turn_span.set_attribute(spans.Attrs.TURN_INDEX, turn)
                turn_span.set_attribute(
                    spans.Attrs.RUN_TYPE, resolve_run_type(request.run_type)
                )
                turn_span.set_attribute(
                    spans.Attrs.TURN_USER_QUESTION, request.user_question
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
                        with _tracer.start_as_current_span(
                            spans.REPEAT_DETECTION
                        ) as rd_span:
                            outcome = await detect_repeat(
                                prior_qs,
                                request.user_question,
                                repeat_agent_for_turn,
                            )
                            is_repeat = outcome.repeat_of_turn is not None
                            rd_span.set_attribute(
                                spans.Attrs.REPEAT_IS_REPEAT, is_repeat
                            )
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
                            yield _terminal_done(
                                session_id, session_started_at_ms, role_timings
                            )
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

                # ------ Build envelope + run core ------
                # `prior_claims` is the chat thread's history of
                # approved claims, used by the narrative constitution
                # gate for `same_turn_claims` context. Snapshot a
                # shallow copy now so the core's `approved_claims`
                # accumulator (which writes back into `thread.claims`
                # below) doesn't double-count this turn's output as
                # prior context.
                prior_claims_snapshot = list(thread.claims)
                envelope = TurnEnvelope(
                    turn_id=f"{session_id}:{turn}",
                    correlation_id=session_id,
                    switches=request.switches,
                    run_type=request.run_type,
                    intent=request.user_question,
                    view_context=request.context,
                    history=list(thread.message_history)
                    if thread.message_history
                    else [],
                    primary_llm_override=primary_override,
                )
                sink = SseSink()

                async def _run_core():
                    try:
                        return await run_one_turn(
                            primary_agent=primary_agent_for_turn,
                            constitution_agent=constitution_agent_for_turn,
                            primitive_client=handles.primitive_client,
                            envelope=envelope,
                            bindings=thread.bindings,
                            snapshot_id=snapshot_id,
                            started_at_ms=session_started_at_ms,
                            sink=sink,
                            debug_public=handles.debug_public,
                            prior_claims=prior_claims_snapshot,
                        )
                    finally:
                        # Close the sink no matter what; the driver's
                        # frame loop is awaiting it. If the core
                        # raised, we still need the loop to exit.
                        await sink.close()

                core_task = asyncio.create_task(_run_core())
                # Drain the sink concurrent with the core running.
                # The sink's queue keeps the core unblocked while the
                # driver yields each frame to FastAPI's
                # StreamingResponse.
                async for frame in sink.frames():
                    yield frame
                # Surface any core exception to the outer try/except.
                outcome = await core_task

                # ------ Write outcome back into thread state ------
                if outcome.new_message_history:
                    thread.message_history = outcome.new_message_history
                for record in outcome.tool_call_records:
                    if record.primitive_name in (
                        "wallet_profile",
                        "community_summary",
                    ):
                        thread.record_turn_tool_call(
                            turn,
                            TurnToolCallRecord(
                                primitive_name=record.primitive_name,
                                args=record.args,
                                output_value=record.output_value,
                                call_id=record.call_id,
                            ),
                        )
                for claim in outcome.approved_claims:
                    thread.record_claim(claim)

                yield _terminal_done(
                    session_id, session_started_at_ms, role_timings
                )

    except asyncio.CancelledError:
        log.info("agent_stream_cancelled", session_id=session_id)
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("loop_driver_failed", session_id=session_id)
        yield _emit_error(e, debug_public=handles.debug_public)
        yield _terminal_done(session_id, session_started_at_ms, role_timings)
    finally:
        if snapshot_id is not None:
            await handles.primitive_client.end_turn(snapshot_id)


def _terminal_done(
    session_id: str,
    session_started_at_ms: int,
    role_timings: dict[str, float],
) -> dict[str, str]:
    elapsed_ms = max(0, int(time.time() * 1000) - session_started_at_ms)
    # Stamp the active OTel trace id onto the Done frame so the
    # frontend can deep-link into Langfuse / SQL the trace by id.
    # Empty string when the SDK is disabled (tests) or no active span.
    span_ctx = trace.get_current_span().get_span_context()
    trace_id_hex = format(span_ctx.trace_id, "032x") if span_ctx.is_valid else ""
    # Per-role wall-time tally. Roles missing from the bucket (e.g.
    # judge today, since it isn't on the chat path) stay 0. Cap at
    # uint32 max for the same reason `elapsed_ms` does.
    timings_proto = session_pb2.RoleTimings(
        primary_ms=min(int(role_timings.get("primary", 0.0) * 1000), 0xFFFFFFFF),
        policy_ms=min(int(role_timings.get("policy", 0.0) * 1000), 0xFFFFFFFF),
        judge_ms=min(int(role_timings.get("judge", 0.0) * 1000), 0xFFFFFFFF),
    )
    return _frame(
        "Done",
        session_pb2.AgentDone(
            session_id=session_id,
            elapsed_ms=min(elapsed_ms, 0xFFFFFFFF),
            trace_id=trace_id_hex,
            role_timings=timings_proto,
        ),
    )


def _emit_error(exc: Exception, *, debug_public: bool) -> dict[str, str]:
    err = sse_pb2.Error(message=_GENERIC_ERROR_MSG)
    if debug_public:
        err.debug_message = f"{type(exc).__name__}: {exc}"
    return _frame("Error", err)
