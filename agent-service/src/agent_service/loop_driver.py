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
from dataclasses import dataclass, field
from typing import Any

import structlog
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
from agent_service.diff_replay import _frame as _shared_frame, run_repeat_path
from agent_service.llm_retry import begin_role_timing_capture
from agent_service.policy.constitution import build_constitution_agent
from agent_service.primitive_client import PrimitiveClient
from agent_service.repeat_detector import build_repeat_agent, detect_repeat
from agent_service.thread_state import (
    AgentThread,
    ThreadRegistry,
    TurnToolCallRecord,
)
from multichain.wire.agent.v1 import (
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
    # Chunk 3. Long-lived codex driver built in lifespan (one
    # `CodexAppServerDriver` per service process, with an internal
    # session pool that persists subprocess connections across
    # turns). `None` when the codex CLI is unavailable in the
    # environment (tests, local dev without `codex` on PATH); the
    # `POST /agent/turn` handler 503s codex-runtime requests in
    # that case rather than silently falling back to pydantic-ai.
    codex_driver: Any = None
    # Chunk 3.7 cost observability. Root of the per-thread codex_home
    # tree (set to `CODEX_HOME_ROOT` env, default `./codex_homes`).
    # After a codex turn completes we read
    # `<root>/local/<thread_id>/sqlite/state_5.sqlite` to recover the
    # model name codex actually used and stamp it as
    # `gen_ai.request.model` on the trace so Langfuse can match it
    # against its model-pricing table. None when the codex runtime
    # isn't usable on this host; in that case tokens still ship to
    # Langfuse but without a model name, so the generation
    # observation has usage data but `totalCost: 0`.
    codex_home_root: Any = None
    # Codex primary model + reasoning effort, env-driven. Mirrors
    # `AGENT_PRIMARY_MODEL` / `AGENT_POLICY_MODEL` on the pydantic-ai
    # side: the operator sets `CODEX_PRIMARY_MODEL=gpt-5-mini` to swap
    # the model codex routes against, with no code change. None
    # falls through to codex-cli's own default
    # (today: gpt-5.5; varies across cli versions). Pinned at
    # lifespan startup; mid-run swaps require a service restart so
    # in-flight thread caches (codex's sqlite per-thread thread row
    # records the model that minted the thread) stay consistent
    # across turns within one chat.
    codex_primary_model: str | None = None
    codex_reasoning_effort: str | None = None
    # Chunk 3.5. Active-turn registry keyed by thread_id. The POST
    # handler registers the per-turn asyncio.Task here on entry and
    # clears it on exit; `DELETE /agent/turn/{thread_id}` looks up
    # the task and calls `task.cancel()`. Cancellation propagates
    # into the generator's `except asyncio.CancelledError` branch
    # which closes the codex session / cancels the drain / releases
    # the snapshot lease cleanly. Mutated by `main.py`; the
    # dataclass default factory keeps each LoopHandles owning its
    # own dict.
    active_turns: dict[str, asyncio.Task] = field(default_factory=dict)


# Frame helpers + repeat-path body now live in `diff_replay` so
# the codex driver shares them without copy-paste. The local
# `_frame` alias keeps the rest of this file unchanged.
_frame = _shared_frame


# ---------------------------------------------------------------------------
# Main turn entry point (chat driver)
# ---------------------------------------------------------------------------


async def run_turn(
    *,
    handles: LoopHandles,
    request,  # session_pb2.AgentRequest
    thread_id: str,
    turn_started_at_ms: int,
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
    # Thread reference for the post-turn `finally` persist. Stays
    # None when an exception fires before `get_or_create` returns
    # (rare; only thread-registry init / lookup errors). On every
    # other path we end up writing `state.json` so disk and memory
    # stay in sync regardless of success or failure.
    thread_for_persist: AgentThread | None = None
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
    # Resolve the live-window seconds for this turn. Proto default 0
    # means "caller didn't pin a window; use the data plane default".
    # Any non-zero value flows through to (a) the snapshot lease via
    # `begin_turn(window_secs=...)`, (b) the per-turn primary agent
    # build so the system prompt's `${LIVE_WINDOW_HUMAN}` placeholder
    # matches what the snapshot will actually cover, and (c) the per-
    # turn constitution agent build so the gate's policy framing stays
    # in lockstep with the primary prompt (otherwise the gate would
    # retract correct narratives on any non-default window).
    requested_window_secs: int | None = (
        int(request.context.live_window_secs)
        if request.HasField("context") and request.context.live_window_secs
        else None
    )
    # The value we feed the per-turn prompt is the requested window
    # when set, the 60s default otherwise. We separately stamp the
    # ACTUAL window the lease resolves to after begin_turn returns
    # (defense in depth: if Rust resolved to a different window we
    # record what was materialized, not what we asked for).
    effective_window_secs = requested_window_secs or 60
    primary_agent_for_turn = (
        build_agent(
            llm_override=primary_override,
            live_window_secs=effective_window_secs,
        )
        if primary_override is not None or effective_window_secs != 60
        else handles.primary_agent
    )
    constitution_agent_for_turn = (
        build_constitution_agent(
            llm_override=policy_override,
            live_window_secs=effective_window_secs,
        )
        if policy_override is not None or effective_window_secs != 60
        else handles.constitution_agent
    )
    repeat_agent_for_turn = (
        build_repeat_agent(llm_override=policy_override)
        if policy_override is not None
        else handles.repeat_agent
    )
    try:
        thread, lock = await handles.threads.get_or_create(
            thread_id, runtime=session_pb2.AGENT_RUNTIME_PYDANTIC_AI
        )
        thread_for_persist = thread
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
                # OTel `session.id` is the standard Langfuse session-
                # grouping key. We stamp it with `thread_id` so all
                # turns of one conversation land under one Langfuse
                # session. (Pre-cleanup, this stamped the per-POST
                # session token which made every turn a separate
                # session in Langfuse, which was wrong.)
                turn_span.set_attribute(spans.Attrs.SESSION_ID, thread_id)
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
                                thread_id=thread_id,
                                repeat_of_turn=outcome.repeat_of_turn,
                                reason=outcome.reason,
                            )
                            lease = await handles.primitive_client.begin_turn(
                                window_secs=requested_window_secs,
                            )
                            snapshot_id = lease.snapshot_id
                            async for frame in run_repeat_path(
                                handles=handles,
                                thread=thread,
                                repeat_of_turn=outcome.repeat_of_turn,
                                snapshot_id=snapshot_id,
                            ):
                                yield frame
                            yield _terminal_done(
                                turn_started_at_ms, role_timings
                            )
                            return

                # ------ Snapshot lease ------
                lease = await handles.primitive_client.begin_turn(
                    window_secs=requested_window_secs,
                )
                snapshot_id = lease.snapshot_id
                # Stamp the resolved window on the turn root span so
                # OTel queries can filter by what the lease actually
                # materialized (which may differ from what we asked
                # for if a future Rust release silently snaps to a
                # neighboring enum value; we always record the
                # ground truth from the response, not the request).
                turn_span = trace.get_current_span()
                turn_span.set_attribute(
                    spans.Attrs.SNAPSHOT_WINDOW_SECS, lease.window_secs
                )
                log.info(
                    "turn_begin",
                    thread_id=thread_id,
                    snapshot_id=snapshot_id,
                    turn=turn,
                    window_secs=lease.window_secs,
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
                    # turn_id names this specific turn (unique across
                    # the system because thread_id is unique and turn
                    # is monotone within a thread).
                    turn_id=f"{thread_id}:{turn}",
                    # correlation_id groups related turns; for chat
                    # that is the conversation handle = thread_id.
                    correlation_id=thread_id,
                    switches=request.switches,
                    run_type=request.run_type,
                    intent=request.user_question,
                    view_context=request.context,
                    history=list(thread.message_history)
                    if thread.message_history
                    else [],
                    primary_llm_override=primary_override,
                    # Carry the lease-resolved window into the core so
                    # the per-defense agent rebuild path (which fires
                    # when `drop_rule_ids` is non-empty) uses the same
                    # window in its prompt as we did when building
                    # `primary_agent_for_turn` above.
                    live_window_secs=int(lease.window_secs),
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
                            started_at_ms=turn_started_at_ms,
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
                    thread.record_turn_claim(turn, claim)
                # Chunk 4 history record. Core captures the final
                # narrative snapshot (approved or retracted) and
                # returns it on the outcome; we persist it onto
                # the thread so the history-reopen path can replay
                # the prose. None on early-exit paths that never
                # reached a narrative emission point.
                if outcome.narrative_snapshot is not None:
                    thread.record_turn_narrative(
                        turn, outcome.narrative_snapshot
                    )

                yield _terminal_done(turn_started_at_ms, role_timings)

    except asyncio.CancelledError:
        log.info("agent_stream_cancelled", thread_id=thread_id)
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("loop_driver_failed", thread_id=thread_id)
        yield _emit_error(e, debug_public=handles.debug_public)
        yield _terminal_done(turn_started_at_ms, role_timings)
    finally:
        # Persist thread state regardless of success or failure so
        # disk and memory match. On the error path the thread has
        # the incremented turn_count and the recorded user_question
        # but no claims / message_history update; persisting that
        # partial-but-accurate state is better than leaving disk
        # stale (next turn would otherwise re-run as the same turn
        # index after a restart, breaking repeat detection and OTel
        # turn correlation). Wrapped in try/except so a write
        # failure can't mask the original error.
        if thread_for_persist is not None:
            try:
                handles.threads.persist(thread_for_persist)
            except Exception:  # noqa: BLE001
                log.exception(
                    "thread_persist_failed", thread_id=thread_id
                )
        if snapshot_id is not None:
            await handles.primitive_client.end_turn(snapshot_id)


def _terminal_done(
    turn_started_at_ms: int,
    role_timings: dict[str, float],
) -> dict[str, str]:
    elapsed_ms = max(0, int(time.time() * 1000) - turn_started_at_ms)
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
