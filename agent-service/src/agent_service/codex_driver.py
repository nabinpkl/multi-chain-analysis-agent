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
from agent_service.core.run import (
    _build_claim,
    _claims_to_judgement_payload,
    _normalize_verdict,
    _set_retracted,
)
from agent_service.diff_replay import run_repeat_path
from agent_service.repeat_detector import detect_repeat
from agent_service.thread_state import TurnToolCallRecord
from agent_service.llm_retry import begin_role_timing_capture, with_provider_retry
from agent_service.policy import constitution as constitution_module
from agent_service.policy import structural as structural_module
from agent_service.policy.binding_store import build_binding
from agent_service.policy.constitution import judge_narrative
from agent_service.policy.placeholder import validate_refs
from agent_service.policy.structural import verify_chip_values
from multichain.wire.shared.v1 import provenance_pb2
from agent_service.thread_state import AgentThread, NarrativeSnapshot
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


def _provenance_refs_from_json(
    refs_json: list[dict[str, Any]],
) -> list[provenance_pb2.ProvenanceRef]:
    """Convert the kebab-case-tagged JSON shape that Rust serde
    emits for `Vec<ProvenanceRef>` (see
    `backend/src/primitives/types.rs:54`) into the proto
    `ProvenanceRef` messages the structural gate consumes.

    Rust's discriminator field is `kind`; values are kebab-case
    variant names (`wallet`, `community`, `edge`, `time-range`,
    `number`). Field names inside each variant are already
    snake_case, which matches the proto. Refs whose shape doesn't
    parse cleanly are skipped so a slight schema drift doesn't
    crash the whole binding population.
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
        except (TypeError, ValueError) as e:
            log.warning("provenance_ref_parse_failed", kind=kind, error=str(e))
            continue
    return out


def _extract_tool_call_signature(
    raw_event: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]] | None:
    """Pull `(tool_name, args)` out of a codex `item/started` raw
    event. Used to set up the per-tool-call record the repeat
    detector replays against the live snapshot on a follow-up turn.

    Codex serializes one MCP tool call as
    `params.item.type=="mcpToolCall"` with `tool`, `server`, and
    `arguments` fields. Shape is undocumented across codex-cli
    versions, so this stays defensive  any missing key returns
    None and the caller skips recording. The repeat detector then
    just doesn't see this tool call as priorturn evidence, which
    is the same fallback the pydantic-ai path takes when an
    `agent.tool` wrapper throws before recording.
    """
    if not raw_event:
        return None
    params = raw_event.get("params") if isinstance(raw_event, dict) else None
    if not isinstance(params, dict):
        return None
    item = params.get("item")
    if not isinstance(item, dict):
        return None
    if item.get("type") != "mcpToolCall":
        return None
    tool_name = item.get("tool")
    args = item.get("arguments")
    if not isinstance(tool_name, str) or not isinstance(args, dict):
        return None
    return tool_name, args


def _extract_mcp_envelope(
    output_json: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """Return `(value_dict, provenance_list)` from a
    `TOOL_COMPLETED.output` payload, navigating the codex-cli MCP
    wrapper. Codex serializes an `mcpToolCall` `result` as
    `{"content": [...], "structuredContent": {<our envelope>},
    "_meta": null}` and `_tool_output(item)` json-dumps the whole
    `result` object. The envelope chunk 3.5 widened
    (`backend/src/mcp.rs`) lands under `structuredContent`.

    None on shape mismatch (failed tool calls land here  `result`
    is null + `error` is non-null, _tool_output picks the error
    message instead). Callers no-op binding population and tool-
    call recording on None; the structural gate then has nothing
    to verify against for that tool, same fallback as the
    pydantic-ai path when a primitive errors.
    """
    if not output_json:
        return None
    try:
        payload = json.loads(output_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    structured = payload.get("structuredContent")
    if not isinstance(structured, dict):
        return None
    value = structured.get("value")
    provenance_json = structured.get("provenance") or []
    if not isinstance(value, dict) or not isinstance(provenance_json, list):
        return None
    return value, provenance_json


def _record_tool_output_binding(
    *,
    thread: AgentThread,
    tool_name: str,
    output_json: str | None,
) -> None:
    """Parse a `TOOL_COMPLETED.output` payload from codex and
    populate the per-thread `PrimitiveBindingStore`. The MCP tool
    surface returns `{value, provenance}` for the two analytical
    tools (chunk 3.5 widened `backend/src/mcp.rs`); `get_token_info`
    returns a bare value with no envelope and is skipped here.

    Failures (malformed JSON, missing envelope keys, no provenance)
    return without recording. The structural gate then no-ops on
    that tool's claims; defensive parsing keeps the codex turn
    intact when the data plane's schema drifts.
    """
    if tool_name not in ("wallet_profile", "community_summary"):
        return
    envelope = _extract_mcp_envelope(output_json)
    if envelope is None:
        # Tool errored (e.g. wallet not in live window) or schema
        # drift; structural gate no-ops on this turn's claims that
        # would have referenced this binding, same as the
        # pydantic-ai path when a primitive errors.
        return
    value, provenance_json = envelope
    provenance = _provenance_refs_from_json(provenance_json)
    binding = build_binding(
        primitive=tool_name,
        call_id=f"{tool_name}:codex:{time.time_ns():x}",
        captured_at_ms=int(time.time() * 1000),
        value_json=value,
        provenance=provenance,
    )
    thread.bindings.record(binding)


def _pump_codex_events(
    *,
    driver: CodexAppServerDriver,
    request: CodexRunRequest,
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue,
) -> None:
    """Run `CodexAppServerDriver.stream` on a worker thread and push
    each event back into the main asyncio loop's queue. Used by the
    async driver to interleave TEXT_DELTA / TOOL_STARTED frames with
    the claim drain in real time.

    Termination: a `None` sentinel is enqueued once the codex
    iterator returns (or raises). The async consumer reads until
    it sees the sentinel; any exception is re-raised on the
    consumer side by surfacing a `("error", exc)` tuple.
    """
    try:
        for evt in driver.stream(request):
            loop.call_soon_threadsafe(queue.put_nowait, ("codex", evt))
    except Exception as exc:  # noqa: BLE001
        loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
    finally:
        loop.call_soon_threadsafe(queue.put_nowait, ("codex_done", None))


def _emit_claims_from_drain(
    *,
    drained: list[dict[str, Any]],
    thread: AgentThread,
    thread_id: str,
    turn_started_at_ms: int,
    dont_fabricate: bool,
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

        # Chunk 3.5 item 6: structural value-compare gate. Runs on
        # codex claims now that `_record_tool_output_binding`
        # populates `thread.bindings` from the {value, provenance}
        # envelope. Only retracts when `dont_fabricate` is on; when
        # off the gate observes-without-acting so the ablation
        # suite can compare gated vs ungated codex behavior.
        with _tracer.start_as_current_span(spans.GATE_STRUCTURAL) as g:
            g.set_attribute(spans.Attrs.GATE_VERSION, structural_module.VERSION)
            g.set_attribute(
                spans.Attrs.GATE_BINDING_SIZE,
                len(thread.bindings.all_numbers()),
            )
            struct_err = verify_chip_values(
                list(claim.provenance), thread.bindings
            )
            if struct_err is not None and dont_fabricate:
                g.set_attribute(
                    spans.Attrs.GATE_VERDICT, spans.VERDICT_RETRACTED
                )
                g.set_attribute(
                    spans.Attrs.GATE_REASON, struct_err.to_human_string()
                )
                g.set_attribute(
                    spans.Attrs.GATE_FAILED_CHIP,
                    str(getattr(struct_err, "kind", "unknown")),
                )
                _set_retracted(claim, struct_err.to_human_string())
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
                    rejection_text = (
                        "Your message contained chat-template-style "
                        "tokens or other non-natural-language patterns "
                        "that aren't supported in this conversation. "
                        "Please rephrase in plain English."
                    )
                    # Chunk 4: persist the rejection so history replay
                    # shows the same shape the live UI saw  a bubble
                    # explaining why we shut the turn down.
                    thread.record_turn_narrative(
                        turn,
                        NarrativeSnapshot(text=rejection_text),
                    )
                    yield _frame(
                        "Narrative",
                        narrative_pb2.NarrativeWithRefs(
                            text=rejection_text,
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

                # Chunk 3.5 item 7: repeat detection on codex path.
                # Mirrors `loop_driver.run_turn:342-402`. The repeat
                # detector itself is an LLM call (model decides
                # whether the new question reasks a prior one); on
                # hit, we replay the prior turn's tool calls against
                # the fresh snapshot via the shared
                # `diff_replay.run_repeat_path`. No codex stream
                # fires on a repeat-hit  the diff is deterministic.
                if (
                    request.switches.dont_repeat_yourself
                    and turn >= 1
                    and thread.user_questions_per_turn
                ):
                    prior_qs = {
                        t: q
                        for t, q in thread.user_questions_per_turn.items()
                        if t != turn
                    }
                    if prior_qs:
                        with _tracer.start_as_current_span(
                            spans.REPEAT_DETECTION
                        ) as rd_span:
                            outcome = await detect_repeat(
                                prior_qs,
                                request.user_question,
                                handles.repeat_agent,
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
                                    spans.Attrs.REPEAT_OF_TURN,
                                    outcome.repeat_of_turn,
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
                                runtime="codex",
                            )
                            lease = await handles.primitive_client.begin_turn()
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

                # `actor_id=thread_id` is the chunk 3.6 isolation
                # key. `CodexAppServerSessionPool` indexes its
                # session entries on `(profile_id, actor_id, ...)`,
                # and `prepare_actor_codex_home` materializes the
                # codex_home subtree at
                # `<CODEX_HOME_ROOT>/local/<thread_id>/`. Each chat
                # thread now gets its own subprocess + sqlite +
                # config + prompt cache, so a "new chat" click
                # really starts cold and threads don't bleed
                # prompt-cache state into one another.
                codex_request = CodexRunRequest(
                    prompt=request.user_question,
                    actor_id=thread_id,
                    provider_thread_id=(
                        thread.codex_provider_thread_id or None
                    ),
                    developer_instructions=turn_dev_instructions,
                    context_items=context_items,
                )

                # Stamp the provider_thread_id we're handing codex
                # BEFORE the stream runs. Pairs with
                # `CODEX_PROVIDER_THREAD_ID_RECEIVED` below; mismatch
                # = silent cache split. Empty string on turn 0 (no
                # prior thread to resume), which is the expected
                # "this is a fresh codex thread" signal.
                turn_span.set_attribute(
                    spans.Attrs.CODEX_PROVIDER_THREAD_ID_SENT,
                    codex_request.provider_thread_id or "",
                )

                yield _frame(
                    "Progress",
                    sse_pb2.Progress(
                        phase="drafting", detail="codex (gpt-5-codex)"
                    ),
                )

                # Drive codex on a worker thread; pump events back
                # into the event loop via an asyncio.Queue so we
                # can yield NarrativeDelta frames as the underlying
                # model emits tokens, not in one blob at turn end.
                # The thread-bridge also gives us a single per-event
                # consumer point where TOOL_STARTED/TOOL_COMPLETED
                # handlers can populate the binding store + tool
                # call record (chunks 3.5 items 6 + 7).
                role_t0 = time.monotonic()
                codex_queue: asyncio.Queue = asyncio.Queue()
                codex_worker = asyncio.create_task(
                    asyncio.to_thread(
                        _pump_codex_events,
                        driver=handles.codex_driver,
                        request=codex_request,
                        loop=asyncio.get_running_loop(),
                        queue=codex_queue,
                    )
                )

                final_text: str = ""
                provider_thread_id_local: str = ""
                tool_events: list[str] = []
                streamed_chars = 0
                codex_error: Exception | None = None
                # Chunk 3.5 item 7: track per-tool args between
                # TOOL_STARTED and TOOL_COMPLETED so we can record a
                # full `TurnToolCallRecord` once the output lands.
                # Keyed by `tool_id` (codex's per-call id).
                pending_tool_calls: dict[str, tuple[str, dict[str, Any]]] = {}
                # Chunk 3.7 cost observability. Codex emits
                # TOKEN_USAGE_UPDATED multiple times during a turn
                # (after each model call). We keep the LATEST snapshot
                # rather than summing; the `.last` breakdown already
                # represents this turn's cost and the `.total` is
                # thread-cumulative through this turn. None on turns
                # codex doesn't bother emitting (e.g. an immediate
                # cancel).
                latest_token_usage: Any = None
                while True:
                    source, payload = await codex_queue.get()
                    if source == "codex_done":
                        break
                    if source == "error":
                        codex_error = payload  # type: ignore[assignment]
                        continue
                    if source != "codex":
                        continue
                    evt = payload
                    if evt.provider_thread_id:
                        provider_thread_id_local = evt.provider_thread_id
                    if evt.type == CodexRunEventType.TEXT_DELTA:
                        if evt.text:
                            streamed_chars += len(evt.text)
                            yield _frame(
                                "NarrativeDelta",
                                narrative_pb2.NarrativeDelta(text=evt.text),
                            )
                    elif evt.type == CodexRunEventType.TOOL_STARTED:
                        tool_events.append(
                            f"start:{evt.text or evt.tool_id or 'tool'}"
                        )
                        # Buffer args for the eventual TOOL_COMPLETED
                        # so we can record a TurnToolCallRecord with
                        # both args and output (chunk 3.5 item 7).
                        sig = _extract_tool_call_signature(evt.raw_event)
                        if sig is not None and evt.tool_id:
                            pending_tool_calls[evt.tool_id] = sig
                        yield _frame(
                            "Progress",
                            sse_pb2.Progress(
                                phase="primitive",
                                detail=evt.text or evt.tool_id or "tool",
                            ),
                        )
                    elif evt.type == CodexRunEventType.TOOL_COMPLETED:
                        tool_name = evt.text or evt.tool_id or ""
                        tool_events.append(f"done:{tool_name or 'tool'}")
                        # Chunk 3.5 item 6: populate the per-thread
                        # binding store from the {value, provenance}
                        # envelope. Lets the structural value-compare
                        # gate run over claims emitted later in this
                        # turn (or in follow-up turns).
                        _record_tool_output_binding(
                            thread=thread,
                            tool_name=tool_name,
                            output_json=evt.output,
                        )
                        # Chunk 3.5 item 7: record TurnToolCallRecord
                        # so a follow-up turn's repeat detector can
                        # replay this tool call. The args were
                        # buffered on TOOL_STARTED; output_value
                        # comes from the {value, provenance}
                        # envelope on this event. If either is
                        # missing (rare: schema drift on
                        # codex-cli) we just skip the record  the
                        # repeat path no-ops on un-recorded tools.
                        pending = (
                            pending_tool_calls.pop(evt.tool_id, None)
                            if evt.tool_id
                            else None
                        )
                        if pending is not None and pending[0] in (
                            "wallet_profile",
                            "community_summary",
                        ):
                            envelope = _extract_mcp_envelope(evt.output)
                            if envelope is not None:
                                value, _ = envelope
                                thread.record_turn_tool_call(
                                    turn,
                                    TurnToolCallRecord(
                                        primitive_name=pending[0],
                                        args=pending[1],
                                        output_value=value,
                                        call_id=evt.tool_id or "",
                                    ),
                                )
                    elif evt.type == CodexRunEventType.MESSAGE_COMPLETED:
                        final_text = evt.final_text or ""
                    elif evt.type == CodexRunEventType.TOKEN_USAGE_UPDATED:
                        if evt.token_usage is not None:
                            latest_token_usage = evt.token_usage

                # Ensure the worker task is fully done (the sentinel
                # was already delivered, but the future may still
                # hold a residual exception we want to observe).
                try:
                    await codex_worker
                except Exception as worker_exc:  # noqa: BLE001
                    if codex_error is None:
                        codex_error = worker_exc

                if codex_error is not None:
                    raise codex_error

                role_timings["primary"] = (
                    role_timings.get("primary", 0.0)
                    + (time.monotonic() - role_t0)
                )

                if provider_thread_id_local:
                    thread.codex_provider_thread_id = provider_thread_id_local

                # Chunk 3.7 cost observability stamps. Together with
                # the `CODEX_PROVIDER_THREAD_ID_SENT` attr above:
                # - `sent != received` (when sent != "") => silent
                #   cache split. The thread continues but codex
                #   re-minted its sqlite-side thread; prompt cache
                #   was NOT reused this turn.
                # - `cache_hit_rate`: cached_input/input from the
                #   `.last` breakdown (this turn). 1.0 = fully
                #   cached, 0.0 = cold; -1.0 sentinel when
                #   input_tokens=0 (metadata-only turn, division
                #   would be undefined).
                # - `.last.*`: this turn's cost.
                # - `.total.*`: thread-cumulative cost through this
                #   turn.
                turn_span.set_attribute(
                    spans.Attrs.CODEX_PROVIDER_THREAD_ID_RECEIVED,
                    provider_thread_id_local or "",
                )
                cache_hit_rate: float = -1.0
                if latest_token_usage is not None:
                    last = latest_token_usage.last
                    total = latest_token_usage.total
                    turn_span.set_attribute(
                        spans.Attrs.CODEX_TOKENS_LAST_TOTAL,
                        last.total_tokens,
                    )
                    turn_span.set_attribute(
                        spans.Attrs.CODEX_TOKENS_LAST_INPUT,
                        last.input_tokens,
                    )
                    turn_span.set_attribute(
                        spans.Attrs.CODEX_TOKENS_LAST_CACHED_INPUT,
                        last.cached_input_tokens,
                    )
                    turn_span.set_attribute(
                        spans.Attrs.CODEX_TOKENS_LAST_OUTPUT,
                        last.output_tokens,
                    )
                    turn_span.set_attribute(
                        spans.Attrs.CODEX_TOKENS_LAST_REASONING,
                        last.reasoning_output_tokens,
                    )
                    turn_span.set_attribute(
                        spans.Attrs.CODEX_TOKENS_TOTAL_TOTAL,
                        total.total_tokens,
                    )
                    turn_span.set_attribute(
                        spans.Attrs.CODEX_TOKENS_TOTAL_INPUT,
                        total.input_tokens,
                    )
                    turn_span.set_attribute(
                        spans.Attrs.CODEX_TOKENS_TOTAL_CACHED_INPUT,
                        total.cached_input_tokens,
                    )
                    turn_span.set_attribute(
                        spans.Attrs.CODEX_TOKENS_TOTAL_OUTPUT,
                        total.output_tokens,
                    )
                    turn_span.set_attribute(
                        spans.Attrs.CODEX_TOKENS_TOTAL_REASONING,
                        total.reasoning_output_tokens,
                    )
                    if last.input_tokens > 0:
                        cache_hit_rate = (
                            last.cached_input_tokens / last.input_tokens
                        )
                    if latest_token_usage.model_context_window is not None:
                        turn_span.set_attribute(
                            spans.Attrs.CODEX_MODEL_CONTEXT_WINDOW,
                            latest_token_usage.model_context_window,
                        )
                turn_span.set_attribute(
                    spans.Attrs.CODEX_CACHE_HIT_RATE, cache_hit_rate
                )

                log.info(
                    "codex_turn_complete",
                    thread_id=thread_id,
                    provider_thread_id_sent=codex_request.provider_thread_id
                    or "",
                    provider_thread_id_received=provider_thread_id_local,
                    cache_hit_rate=cache_hit_rate,
                    tokens_last_total=(
                        latest_token_usage.last.total_tokens
                        if latest_token_usage
                        else 0
                    ),
                    tool_events=tool_events,
                    final_chars=len(final_text),
                    streamed_chars=streamed_chars,
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
                    dont_fabricate=request.switches.dont_fabricate,
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

                # Chunk 3.5 item 5: run the constitution gate over
                # the codex final prose, same path the pydantic-ai
                # core uses (`core/run.py:483-523`). Gated by the
                # `defend_constitution_judge` switch so the
                # ablation suite can still pull raw codex output.
                # `same_turn_claims` carries this turn's approved
                # claims plus prior-turn approved claims for
                # narrative-coherence context. Approved or skipped
                # → `Narrative` SSE frame. Retracted / rejected →
                # `NarrativeRetracted` SSE frame; the frontend
                # renders it in the same struck-amber bubble as
                # the pydantic-ai retraction.
                approved_claim_list = [c for c, ok in results if ok]
                narrative_retracted = False
                if (
                    request.switches.stay_in_role.defend_constitution_judge
                    and final_text
                ):
                    role_t_const = time.monotonic()
                    with _tracer.start_as_current_span(
                        spans.GATE_NARRATIVE_CONSTITUTION
                    ) as g:
                        g.set_attribute(
                            spans.Attrs.GATE_VERSION,
                            constitution_module.VERSION,
                        )
                        verdict = await with_provider_retry(
                            lambda: judge_narrative(
                                handles.constitution_agent,
                                text=final_text,
                                same_turn_claims=(
                                    _claims_to_judgement_payload(
                                        approved_claim_list
                                    )
                                    + _claims_to_judgement_payload(
                                        list(thread.claims)
                                    )
                                ),
                            ),
                            label="constitution_narrative",
                        )
                        normalized = _normalize_verdict(verdict.verdict)
                        g.set_attribute(spans.Attrs.GATE_VERDICT, normalized)
                        if verdict.reason:
                            g.set_attribute(
                                spans.Attrs.GATE_REASON, verdict.reason
                            )
                        if verdict.verdict in ("retract", "reject"):
                            retraction_reason = (
                                verdict.reason
                                or f"constitution {verdict.verdict}"
                            )
                            ret = narrative_pb2.NarrativeRetracted(
                                text=final_text,
                                reason=retraction_reason,
                            )
                            if handles.debug_public:
                                ret.debug_reason = (
                                    f"constitution: {verdict.reason}"
                                )
                            # Chunk 4 history record: retracted snapshot
                            # carries the retraction reason so the
                            # replay path can render the same muted /
                            # amber bubble the live UI renders.
                            thread.record_turn_narrative(
                                turn,
                                NarrativeSnapshot(
                                    text=final_text,
                                    retracted_reason=retraction_reason,
                                ),
                            )
                            yield _frame("NarrativeRetracted", ret)
                            narrative_retracted = True
                    role_timings["policy"] = role_timings.get(
                        "policy", 0.0
                    ) + (time.monotonic() - role_t_const)

                if not narrative_retracted:
                    # Chunk 4 history record: approved snapshot. Provenance
                    # stays empty in this MVP (matches what we emit on
                    # the live frame); when assembled provenance gets
                    # wired into the codex path, populate here too.
                    thread.record_turn_narrative(
                        turn,
                        NarrativeSnapshot(text=final_text),
                    )
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
