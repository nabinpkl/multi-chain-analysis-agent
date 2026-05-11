"""Codex runtime driver. Mirrors `loop_driver.run_turn`'s contract so
`main.py` dispatches between runtimes with one switch.

Architecture:

* Snapshot lease (existing `PrimitiveClient.begin_turn`).
* Background async task draining `GET /turn/{snapshot_id}/claims`.
  Each `data: {<claim>}` line is buffered for replay through the
  existing gate stack after the codex stream finishes.
* Codex runs in a worker thread via `asyncio.to_thread`; the
  `CodexAppServerDriver` exposes a SYNC iterator that would block
  the event loop otherwise. We collect TEXT_DELTA / TOOL_STARTED /
  MESSAGE_COMPLETED events synchronously inside the thread and
  return the aggregated result. Per-tool Progress frames in real
  time are chunk 3.5; this MVP emits one Progress at the start.
* When codex returns, we close the snapshot lease so the drain
  socket sees EOF and exits cleanly. Each drained claim is parsed
  to `EmitClaimInput`, built into a `claim_pb2.Claim`, run through
  the placeholder gate (`validate_refs`), and emitted via the
  same SSE Claim frame the pydantic-ai path uses.
* Final narrative arrives as the codex `MESSAGE_COMPLETED.final_text`
  and goes out as one `NarrativeWithRefs`. Constitution + structural
  gates on the codex path are explicitly deferred to chunk 3.5 per
  the plan; the placeholder gate is the only one that runs over
  codex-emitted claims today.
* `Done` carries elapsed wall time + OTel trace id + role timings.

What's intentionally NOT in this MVP (each tracked in the chunk 3
plan's "out of scope" section or marked for a 3.5 follow-up):

* Real-time TEXT_DELTA streaming to the frontend.
* Constitution gate over codex prose (`judge_narrative`).
* Structural value-compare gate over claims. `PrimitiveBindingStore`
  stays empty on codex turns because the MCP tool surface returns
  `.value` only, not the envelope `.provenance` block.
* Repeat detection (`dont_repeat_yourself`). Codex doesn't record
  tool calls into `thread.tool_calls_per_turn` and so the diff
  walker has nothing to replay.
* Snapshot id in MCP session state. We thread it through the
  developer prompt for now (30-token tax per turn).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import structlog
from codex_agent_driver import (
    CodexAppServerDriver,
    CodexRunContextItem,
    CodexRunEventType,
    CodexRunRequest,
)
from opentelemetry import trace
from pydantic import ValidationError

from agent_service import spans
from agent_service.agent import EmitClaimInput
from agent_service.boundary import (
    UnsafeUserInputError,
    build_context_block,
    reject_if_unsafe_user_question,
)
from agent_service.core.run import _build_claim, _set_retracted
from agent_service.llm_retry import begin_role_timing_capture
from agent_service.policy.placeholder import validate_refs
from agent_service.thread_state import AgentThread
from multichain.wire.agent.v1 import (
    narrative_pb2,
    session_pb2,
    sse_pb2,
)
from google.protobuf import json_format

log = structlog.get_logger(__name__)
_tracer = trace.get_tracer(__name__)

# Placeholder-gate version stamped on per-claim spans. Same string
# the pydantic-ai path uses; one value across runtimes so probes
# can group on `gate.placeholder.version` without runtime branching.
_PLACEHOLDER_VERSION = "v1"

# Generic error message; surfaces to user on failure. Raw exception
# crosses the wire only when `debug_public` is set.
_GENERIC_ERROR_MSG = (
    "Couldn't produce a valid response. Try rephrasing or try again."
)

# Bound on how long we wait for the SSE drain to deliver tail claims
# after the codex stream has returned. mpsc is unbounded so all
# emitted claims are already buffered; in practice the drain finishes
# in low-millisecond range once we close the snapshot. 5s ceiling
# covers slow IO without hanging a stuck turn forever.
_DRAIN_TAIL_TIMEOUT_S = 5.0


def _frame(event: str, msg) -> dict[str, str]:
    return {
        "event": event,
        "data": json_format.MessageToJson(
            msg, preserving_proto_field_name=False, indent=None
        ),
    }


async def _drain_claims(
    *,
    data_plane_url: str,
    snapshot_id: str,
    out: list[dict[str, Any]],
) -> None:
    """Background task that reads the per-snapshot claim SSE drain
    on the Rust side and appends each `event: claim` payload into
    `out`. Returns when the stream closes (which happens after the
    main flow calls `/turn/end` and the Rust side drops the mpsc
    sender), or when its task is cancelled.

    Single-consumer endpoint by contract; the chunk 3 driver owns
    it exclusively for the current turn.
    """
    url = data_plane_url.rstrip("/") + f"/turn/{snapshot_id}/claims"
    timeout = httpx.Timeout(60.0, read=60.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    log.warning(
                        "claim_drain_status",
                        snapshot_id=snapshot_id,
                        status=resp.status_code,
                    )
                    return
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    try:
                        out.append(json.loads(payload))
                    except json.JSONDecodeError as e:
                        log.warning(
                            "claim_drain_decode_failed",
                            snapshot_id=snapshot_id,
                            error=str(e),
                        )
    except asyncio.CancelledError:
        # Caller decided to abandon the drain (timeout / shutdown).
        # Re-raise so the task ends with the expected cancellation
        # state, not silently swallowed.
        raise
    except httpx.HTTPError as e:
        log.warning(
            "claim_drain_http_error", snapshot_id=snapshot_id, error=str(e)
        )


def _run_codex_sync(
    *,
    driver: CodexAppServerDriver,
    request: CodexRunRequest,
) -> dict[str, Any]:
    """Drive `CodexAppServerDriver.stream` to completion on a worker
    thread (sync iterator + blocking I/O). Returns the aggregated
    outcome the caller emits as SSE frames.

    We collect tool-name events for logging but don't surface them
    as Progress frames in this MVP; per-tool Progress with mid-turn
    UI updates is chunk 3.5 work that needs an async-bridge from
    the worker thread back into the event loop.
    """
    final_text: str | None = None
    provider_thread_id: str | None = None
    tool_events: list[str] = []
    for evt in driver.stream(request):
        if evt.provider_thread_id:
            provider_thread_id = evt.provider_thread_id
        if evt.type == CodexRunEventType.TOOL_STARTED:
            tool_events.append(f"start:{evt.text or evt.tool_id or 'tool'}")
        elif evt.type == CodexRunEventType.TOOL_COMPLETED:
            tool_events.append(f"done:{evt.text or evt.tool_id or 'tool'}")
        elif evt.type == CodexRunEventType.MESSAGE_COMPLETED:
            final_text = evt.final_text
    return {
        "final_text": final_text or "",
        "provider_thread_id": provider_thread_id or "",
        "tool_events": tool_events,
    }


def _emit_claims_from_drain(
    *,
    drained: list[dict[str, Any]],
    thread: AgentThread,
    thread_id: str,
    turn_started_at_ms: int,
) -> list[tuple[Any, bool]]:
    """Convert each drained claim dict into a `claim_pb2.Claim`,
    run the placeholder gate, and return the list paired with
    "approved?" flags.

    The frontier-vs-drained boundary uses the existing pydantic
    shape (`EmitClaimInput`) for validation so any divergence
    between the Rust schema and the Python gate side surfaces here
    as a single clear error instead of N silent field drops. The
    Rust `ClaimInput` was authored to be a field-for-field mirror;
    practically every drain payload should validate cleanly.

    Claims that fail Pydantic validation OR the placeholder gate
    arrive at the caller with `approved=False`; the caller emits
    the SSE frame either way (the UI renders retracted claims with
    the reason inline).
    """
    out: list[tuple[Any, bool]] = []
    for raw in drained:
        try:
            parsed = EmitClaimInput.model_validate(raw)
        except ValidationError as e:
            log.warning(
                "drained_claim_validation_failed",
                thread_id=thread_id,
                error=str(e),
                raw_keys=list(raw.keys()),
            )
            continue
        claim = _build_claim(
            input_=parsed,
            thread_id=thread_id,
            turn_started_at_ms=turn_started_at_ms,
        )
        if not claim.provenance:
            _set_retracted(
                claim, "claim has empty provenance; cite at least one entity"
            )
            out.append((claim, False))
            continue
        with _tracer.start_as_current_span(spans.GATE_PLACEHOLDER) as g:
            g.set_attribute(spans.Attrs.GATE_VERSION, _PLACEHOLDER_VERSION)
            ref_err = validate_refs(
                claim.body_markdown, len(claim.provenance)
            )
            if ref_err is not None:
                g.set_attribute(
                    spans.Attrs.GATE_VERDICT, spans.VERDICT_RETRACTED
                )
                g.set_attribute(
                    spans.Attrs.GATE_REASON, ref_err.to_human_string()
                )
                _set_retracted(claim, ref_err.to_human_string())
                out.append((claim, False))
                continue
            g.set_attribute(spans.Attrs.GATE_VERDICT, spans.VERDICT_APPROVED)
        out.append((claim, True))
    return out


def _terminal_done(
    turn_started_at_ms: int,
    role_timings: dict[str, float],
) -> dict[str, str]:
    """Build the `Done` SSE frame. Copy of the pydantic-ai loop's
    helper; lifted here so the codex path doesn't import private
    helpers from `loop_driver`. Both paths emit the same proto
    shape so the frontend stays runtime-agnostic."""
    elapsed_ms = max(0, int(time.time() * 1000) - turn_started_at_ms)
    span_ctx = trace.get_current_span().get_span_context()
    trace_id_hex = (
        format(span_ctx.trace_id, "032x") if span_ctx.is_valid else ""
    )
    timings_proto = session_pb2.RoleTimings(
        primary_ms=min(
            int(role_timings.get("primary", 0.0) * 1000), 0xFFFFFFFF
        ),
        policy_ms=min(
            int(role_timings.get("policy", 0.0) * 1000), 0xFFFFFFFF
        ),
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


def _emit_error_frame(exc: Exception, *, debug_public: bool) -> dict[str, str]:
    err = sse_pb2.Error(message=_GENERIC_ERROR_MSG)
    if debug_public:
        err.debug_message = f"{type(exc).__name__}: {exc}"
    return _frame("Error", err)


async def run_turn_codex(
    *,
    handles,  # LoopHandles; loose-typed to avoid an import cycle
    request: session_pb2.AgentRequest,
    thread_id: str,
    turn_started_at_ms: int,
) -> AsyncIterator[dict[str, str]]:
    """One turn through the codex runtime. Mirrors
    `loop_driver.run_turn`'s contract: async generator yielding
    `{event, data}` dicts that match `EventSourceResponse`'s shape.
    """
    role_timings = begin_role_timing_capture()
    snapshot_id: str | None = None
    drain_task: asyncio.Task | None = None
    drained: list[dict[str, Any]] = []
    thread_for_persist: AgentThread | None = None
    data_plane_url = handles.primitive_client.base_url  # set in PrimitiveClient

    try:
        thread, lock = await handles.threads.get_or_create(
            thread_id, runtime=session_pb2.AGENT_RUNTIME_CODEX
        )
        thread_for_persist = thread
        async with lock:
            with _tracer.start_as_current_span(spans.AGENT_TURN) as turn_span:
                turn = thread.turn_count
                thread.turn_count += 1
                thread.record_turn_user_question(turn, request.user_question)

                # Same span attrs the pydantic-ai path stamps so OTel
                # queries don't need a runtime branch. `runtime` is
                # stamped raw on the span for `WHERE runtime='codex'`
                # filters once the new attribute is added to spans.py;
                # for now we leave it as a free-form attr.
                turn_span.set_attribute(spans.Attrs.SESSION_ID, thread_id)
                turn_span.set_attribute(spans.Attrs.THREAD_ID, thread_id)
                turn_span.set_attribute(spans.Attrs.TURN_INDEX, turn)
                turn_span.set_attribute(spans.Attrs.RUN_TYPE, request.run_type or "production")
                turn_span.set_attribute(
                    spans.Attrs.TURN_USER_QUESTION, request.user_question
                )
                turn_span.set_attribute("runtime", "codex")

                # Boundary check: same rail as pydantic-ai. Chat-template
                # spoofing patterns get rejected before codex ever sees
                # the user question.
                try:
                    if request.switches.stay_in_role.defend_chat_template_spoofing:
                        reject_if_unsafe_user_question(request.user_question)
                except UnsafeUserInputError as e:
                    log.info(
                        "user_input_rejected_at_boundary",
                        pattern=e.pattern,
                        runtime="codex",
                    )
                    yield _frame(
                        "Narrative",
                        narrative_pb2.NarrativeWithRefs(
                            text=(
                                "Your message contained chat-template-style "
                                "tokens or other non-natural-language patterns "
                                "that aren't supported in this conversation. "
                                "Please rephrase in plain English."
                            ),
                            provenance=[],
                        ),
                    )
                    yield _terminal_done(turn_started_at_ms, role_timings)
                    return

                yield _frame(
                    "Progress",
                    sse_pb2.Progress(
                        phase="planning", detail="opening codex turn"
                    ),
                )

                # Snapshot lease. Same `PrimitiveClient.begin_turn` the
                # pydantic-ai path uses; one snapshot per turn.
                lease = await handles.primitive_client.begin_turn()
                snapshot_id = lease.snapshot_id
                log.info(
                    "turn_begin",
                    thread_id=thread_id,
                    snapshot_id=snapshot_id,
                    turn=turn,
                    runtime="codex",
                )

                # Start the claim drain BEFORE codex emits so the
                # mpsc receiver is bound and no claim races into the
                # buffer ahead of a consumer. (mpsc is unbounded so
                # technically the order doesn't matter, but explicit
                # ordering keeps the design simple.)
                drain_task = asyncio.create_task(
                    _drain_claims(
                        data_plane_url=data_plane_url,
                        snapshot_id=snapshot_id,
                        out=drained,
                    )
                )
                # Brief delay so the GET handshake completes before
                # codex's first emit_claims call; otherwise the SSE
                # drain may miss the trailing CRLF and reorder events.
                await asyncio.sleep(0.05)

                # Build the codex run request. Snapshot id threads via
                # developer instructions per the chunk 3 plan; view
                # context is appended as a context item so codex sees
                # focused-entity hints.
                context_items: list[CodexRunContextItem] = []
                if request.HasField("context"):
                    ctx_block = build_context_block(
                        request.context, ""
                    ).strip()
                    if ctx_block:
                        context_items.append(
                            CodexRunContextItem(text=ctx_block)
                        )

                turn_dev_instructions = (
                    f"Per-turn snapshot id: snapshot_id='{snapshot_id}'. "
                    "Pass this exact value to every tool call that "
                    "accepts a snapshot_id."
                )

                codex_request = CodexRunRequest(
                    prompt=request.user_question,
                    actor_id="codex_home",
                    provider_thread_id=(
                        thread.codex_provider_thread_id or None
                    ),
                    developer_instructions=turn_dev_instructions,
                    context_items=context_items,
                )

                yield _frame(
                    "Progress",
                    sse_pb2.Progress(
                        phase="drafting", detail="codex (gpt-5-codex)"
                    ),
                )

                # Drive codex to completion on a worker thread. The
                # sync iterator would block the event loop otherwise.
                role_t0 = time.monotonic()
                outcome = await asyncio.to_thread(
                    _run_codex_sync,
                    driver=handles.codex_driver,
                    request=codex_request,
                )
                role_timings["primary"] = (
                    role_timings.get("primary", 0.0)
                    + (time.monotonic() - role_t0)
                )

                final_text = outcome["final_text"]
                pti = outcome["provider_thread_id"]
                if pti:
                    thread.codex_provider_thread_id = pti

                log.info(
                    "codex_turn_complete",
                    thread_id=thread_id,
                    provider_thread_id=pti,
                    tool_events=outcome["tool_events"],
                    final_chars=len(final_text),
                )

                # Close the snapshot lease so the drain socket sees
                # EOF (the Rust side drops the mpsc sender on
                # /turn/end). Wait briefly for tail claims to flush
                # from kernel buffers into the asyncio task.
                await asyncio.sleep(0.3)
                await handles.primitive_client.end_turn(snapshot_id)
                snapshot_id = None  # avoid double-close in finally

                try:
                    await asyncio.wait_for(
                        drain_task, timeout=_DRAIN_TAIL_TIMEOUT_S
                    )
                except asyncio.TimeoutError:
                    drain_task.cancel()
                    log.warning(
                        "claim_drain_timeout", thread_id=thread_id
                    )
                drain_task = None  # avoid double-await in finally

                # Run the placeholder gate over each drained claim
                # and emit Claim frames. The structural value-compare
                # gate is a no-op on codex turns (binding store stays
                # empty) by design; chunk 3.5 widens the MCP tool
                # surface to populate it.
                results = _emit_claims_from_drain(
                    drained=drained,
                    thread=thread,
                    thread_id=thread_id,
                    turn_started_at_ms=turn_started_at_ms,
                )
                turn_span.set_attribute(
                    spans.Attrs.TURN_CLAIMS_EMITTED, len(results)
                )
                approved_count = 0
                for claim, approved in results:
                    yield _frame("Claim", claim)
                    if approved:
                        thread.record_claim(claim)
                        approved_count += 1
                turn_span.set_attribute(
                    spans.Attrs.TURN_CLAIMS_APPROVED, approved_count
                )

                # Final narrative. No constitution gate on this MVP
                # (chunk 3.5); codex prose passes through verbatim
                # with empty provenance since the existing chip-
                # resolution pipeline runs on Claim-emitted refs,
                # not narrative-side provenance.
                yield _frame(
                    "Narrative",
                    narrative_pb2.NarrativeWithRefs(
                        text=final_text,
                        provenance=[],
                    ),
                )
                turn_span.set_attribute(
                    spans.Attrs.TURN_NARRATIVE_CHARS, len(final_text)
                )

                yield _terminal_done(turn_started_at_ms, role_timings)

    except asyncio.CancelledError:
        log.info("agent_stream_cancelled", thread_id=thread_id, runtime="codex")
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("codex_driver_failed", thread_id=thread_id)
        yield _emit_error_frame(e, debug_public=handles.debug_public)
        yield _terminal_done(turn_started_at_ms, role_timings)
    finally:
        # Cancel the drain task if it's still running (early-exit
        # paths like boundary rejection, validation error, or the
        # codex stream blowing up mid-turn).
        if drain_task is not None and not drain_task.done():
            drain_task.cancel()
            try:
                await drain_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if snapshot_id is not None:
            await handles.primitive_client.end_turn(snapshot_id)
        # Persist thread state regardless of success or failure so
        # the codex_provider_thread_id assignment survives a turn
        # that errored mid-flight.
        if thread_for_persist is not None:
            try:
                handles.threads.persist(thread_for_persist)
            except Exception:  # noqa: BLE001
                log.exception("thread_persist_failed", thread_id=thread_id)
