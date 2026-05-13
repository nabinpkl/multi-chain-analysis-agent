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
import hashlib
import json
import sqlite3
import time
from collections.abc import AsyncIterator
from pathlib import Path
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
    resolve_narrative_text,
)
from agent_service.diff_replay import run_repeat_path
from agent_service.repeat_detector import detect_repeat
from agent_service.thread_state import TurnToolCallRecord
from agent_service.llm_retry import begin_role_timing_capture, with_provider_retry
from agent_service.policy import constitution as constitution_module
from agent_service.policy import structural as structural_module
from agent_service.policy.binding_store import build_binding
from agent_service.policy.constitution import (
    build_constitution_agent,
    judge_narrative,
)
from agent_service.policy.placeholder import validate_refs
from agent_service.policy.structural import verify_chip_values
from agent_service.prompts.composer import (
    compose_system_prompt,
    drops_from_switches,
)
from multichain.wire.shared.v1 import provenance_pb2
from agent_service.thread_state import AgentThread, NarrativeSnapshot
from multichain.wire.agent.v1 import (
    claim_pb2,
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

# Codex-specific tool-surface delta vs `system_v4.txt`. The system
# prompt is authored for the pydantic-ai surface which exposes
# `emit_claim` (singular, called once per claim); codex's MCP surface
# exposes `emit_claims` (batched plural, one call per turn). We
# substitute the tool name in the composed prompt below so codex sees
# a single, correct name everywhere, then add this footer to encode
# the batching rule the MCP schema can't express on its own ("one
# call per turn, all claims"). Anything else policy-shaped MUST live
# in `system_v4.txt` so both runtimes inherit it.
_CODEX_EMIT_TOOL_NAME = "emit_claims"
_PYDANTIC_EMIT_TOOL_NAME = "emit_claim"
_CODEX_TOOL_SURFACE_FOOTER = (
    "<codex_tool_surface>\n"
    "`emit_claims` is batched plural: pass ALL claims for this turn "
    "in ONE call. Do not split chips across multiple invocations. "
    "Every read-side tool that accepts `snapshot_id` MUST receive "
    "the value provided in the snapshot pin below.\n"
    "</codex_tool_surface>"
)


def _adapt_system_prompt_for_codex(text: str) -> str:
    """Rewrite the pydantic-ai-shaped tool name in the composed system
    prompt to the codex tool surface name. Three backtick-quoted
    mentions live in `prompts/system_v4.txt`; substituting them keeps
    the codex prompt consistent (the model never sees the wrong tool
    name) while preserving a single authored source for policy. If
    `system_v4.txt` gains new tool-name mentions, this substitution
    catches them automatically because the match is on the backtick-
    quoted form.
    """
    return text.replace(
        f"`{_PYDANTIC_EMIT_TOOL_NAME}`",
        f"`{_CODEX_EMIT_TOOL_NAME}`",
    )

# Tool-name to primitive-span-name mapping. The pydantic-ai path
# emits these spans via `primitive_client.py` (one per `await`
# against the data plane). The codex path doesn't go through
# `primitive_client`  codex's own MCP client talks straight to
# `backend/src/mcp.rs`  so the spans are missing today. We
# synthesize equivalents from TOOL_STARTED/TOOL_COMPLETED events
# so eval probes (`tool_called_with_args`,
# `span_latency_p50_under(mcae.primitive.wallet_profile)`) treat
# both runtimes uniformly. Only the three read-side primitives are
# wrapped; `emit_claims` is the write-side channel and gets its own
# `mcae.claim.emitted` span shape (chunk 3.5 future).
_PRIMITIVE_SPAN_NAMES: dict[str, str] = {
    "wallet_profile": spans.PRIMITIVE_WALLET_PROFILE,
    "community_summary": spans.PRIMITIVE_COMMUNITY_SUMMARY,
    "get_token_info": spans.PRIMITIVE_GET_TOKEN_INFO,
}


def _capped_json(value: Any, cap: int = spans.PRIMITIVE_PAYLOAD_MAX_BYTES) -> str:
    """Serialize `value` to JSON and cap it at `cap` bytes, matching
    the convention used by `primitive_client._proto_to_capped_json`.
    On overflow the returned string ends with the literal
    ` ...[truncated, total=N]` so SQL filters can identify caps.
    """
    if value is None:
        return ""
    try:
        s = json.dumps(value, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""
    if len(s.encode("utf-8")) <= cap:
        return s
    # Re-encode the prefix to a safe boundary so the truncation marker
    # appended below doesn't slice through a multi-byte char.
    encoded = s.encode("utf-8")
    prefix = encoded[:cap].decode("utf-8", errors="ignore")
    return f"{prefix} ...[truncated, total={len(encoded)}]"


def _digest12(payload: str | bytes | None) -> str:
    """sha256 → first 12 hex chars. Same shape `primitive_client`
    stamps on `mcae.primitive.output_digest` so cross-runtime
    digest comparison works (identical tool inputs produce
    identical digests regardless of runtime)."""
    if payload is None:
        return ""
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    return hashlib.sha256(raw).hexdigest()[:12]

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


def _read_codex_model(
    *,
    codex_home_root: Path | None,
    thread_id: str,
    provider_thread_id: str,
) -> str | None:
    """Read the model name codex actually used for this thread from
    its sqlite. Codex persists `(id, model, model_provider, ...)`
    rows in `state_5.sqlite::threads` keyed by the codex-side
    provider_thread_id; we recover that id from the
    `MESSAGE_COMPLETED` event chain and look it up here so the
    `gen_ai.request.model` attribute we stamp on the turn span
    matches the actual model codex routed against (e.g. `gpt-5.5`
    vs the developer-instruction text claiming `gpt-5-codex`).

    All errors collapse to `None`. The caller stamps tokens
    without a model when this returns None; Langfuse then shows
    usage but no auto-cost. This is the soft-fail path  one
    sqlite read out of band shouldn't be able to break a turn.

    Codex runs its sqlite in WAL mode, so this read does not
    block while codex holds the same db open from its subprocess
    side. Read-only `mode=ro` is belt-and-suspenders to make that
    contract explicit.
    """
    if codex_home_root is None or not provider_thread_id:
        return None
    db_path = (
        Path(codex_home_root)
        / "local"
        / thread_id
        / "sqlite"
        / "state_5.sqlite"
    )
    if not db_path.exists():
        return None
    try:
        uri = f"file:{db_path}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=0.5) as conn:
            row = conn.execute(
                "SELECT model FROM threads WHERE id = ?",
                (provider_thread_id,),
            ).fetchone()
        if row is None:
            return None
        model = row[0]
        return str(model) if model else None
    except sqlite3.Error as e:
        log.warning(
            "codex_model_sqlite_read_failed",
            thread_id=thread_id,
            error=str(e),
        )
        return None


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

        # Phase-2 eval observability. Wrap each claim in
        # `mcae.claim.emitted` so the gate spans nest underneath
        # (matching the pydantic-ai trace shape in
        # `core/run.py:311`). Stamping `mcae.claim.source_kind`
        # makes `claim_grounded_in(source_kind=primitive)` work
        # on codex; without this attribute the probe vacuously
        # passed because no spans matched. Final
        # `mcae.claim.verdict` is set right before each `out.append`
        # so a single span query gives the per-claim outcome
        # history regardless of which gate (or pydantic
        # validation) retracted it.
        with _tracer.start_as_current_span(spans.CLAIM_EMITTED) as claim_span:
            claim_span.set_attribute(spans.Attrs.CLAIM_ID, claim.id)
            claim_span.set_attribute(
                spans.Attrs.CLAIM_KIND, claim_pb2.ClaimKind.Name(claim.kind)
            )
            claim_span.set_attribute(
                spans.Attrs.CLAIM_HEADLINE, claim.headline
            )
            claim_span.set_attribute(
                spans.Attrs.CLAIM_PROVENANCE_COUNT, len(claim.provenance)
            )
            claim_span.set_attribute(
                spans.Attrs.CLAIM_BODY_CHARS, len(claim.body_markdown)
            )
            # `primitive` because the codex MCP surface today is
                # `wallet_profile` + `community_summary` +
                # `get_token_info`  all typed primitives whose
                # output envelopes feed the binding store. When
                # codex eventually gets `sql_explore`-style tools
                # the source_kind for those claims becomes
                # `exploratory`; this stays the only call-site
                # that needs the conditional.
            claim_span.set_attribute(
                spans.Attrs.CLAIM_SOURCE_KIND,
                spans.SOURCE_KIND_PRIMITIVE,
            )

            if not claim.provenance:
                _set_retracted(
                    claim,
                    "claim has empty provenance; cite at least one entity",
                )
                claim_span.set_attribute(
                    spans.Attrs.CLAIM_VERDICT, spans.VERDICT_RETRACTED
                )
                out.append((claim, False))
                continue
            with _tracer.start_as_current_span(spans.GATE_PLACEHOLDER) as g:
                g.set_attribute(
                    spans.Attrs.GATE_VERSION, _PLACEHOLDER_VERSION
                )
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
                    claim_span.set_attribute(
                        spans.Attrs.CLAIM_VERDICT, spans.VERDICT_RETRACTED
                    )
                    out.append((claim, False))
                    continue
                g.set_attribute(
                    spans.Attrs.GATE_VERDICT, spans.VERDICT_APPROVED
                )

            # Chunk 3.5 item 6: structural value-compare gate.
            # Runs on codex claims now that
            # `_record_tool_output_binding` populates
            # `thread.bindings` from the {value, provenance}
            # envelope. Only retracts when `dont_fabricate` is on;
            # when off the gate observes-without-acting so the
            # ablation suite can compare gated vs ungated codex
            # behavior.
            with _tracer.start_as_current_span(spans.GATE_STRUCTURAL) as g:
                g.set_attribute(
                    spans.Attrs.GATE_VERSION, structural_module.VERSION
                )
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
                        spans.Attrs.GATE_REASON,
                        struct_err.to_human_string(),
                    )
                    g.set_attribute(
                        spans.Attrs.GATE_FAILED_CHIP,
                        str(getattr(struct_err, "kind", "unknown")),
                    )
                    _set_retracted(claim, struct_err.to_human_string())
                    claim_span.set_attribute(
                        spans.Attrs.CLAIM_VERDICT, spans.VERDICT_RETRACTED
                    )
                    out.append((claim, False))
                    continue
                g.set_attribute(
                    spans.Attrs.GATE_VERDICT, spans.VERDICT_APPROVED
                )
            claim_span.set_attribute(
                spans.Attrs.CLAIM_VERDICT, spans.VERDICT_APPROVED
            )
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
    # Phase-2 eval spans hoisted to outer scope so the bottom-of-
    # function finally can close them if an exception unwinds the
    # turn before the happy-path `.end()` call fires. Each span is
    # manually managed (not via `with`) because the codex worker
    # loop is one linear sequence and wrapping it in a `with` block
    # would re-indent hundreds of lines.
    chat_span: Any = None
    pending_tool_spans: dict[str, tuple[Any, float]] = {}

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
                # Cockpit-pattern observables for the channel switches.
                # Pydantic-ai stamps these on the turn root in
                # `core/run.py:180-187`; mirroring them here keeps the
                # `channel_narrative_off` probe runtime-agnostic and
                # lets dashboards filter turns by channel state without
                # branching on `runtime`.
                turn_span.set_attribute(
                    spans.Attrs.TURN_CHANNEL_NARRATIVE_OUTPUT_ENABLED,
                    request.switches.channels.narrative_output_enabled,
                )
                turn_span.set_attribute(
                    spans.Attrs.TURN_CHANNEL_EXTERNAL_TEXT_INPUT_ENABLED,
                    request.switches.channels.external_text_input_enabled,
                )
                turn_span.set_attribute("runtime", "codex")

                # Resolve the live-window seconds for this turn. Proto
                # default 0 means "caller didn't pin a window; use the
                # data-plane default". Non-zero flows through to the
                # snapshot lease (so the snapshot covers the right
                # slice) AND the system prompt (so the agent's framing
                # matches what the snapshot will actually contain).
                # Mirror of the pydantic-ai resolution in
                # `loop_driver.py`.
                requested_window_secs: int | None = (
                    int(request.context.live_window_secs)
                    if request.HasField("context")
                    and request.context.live_window_secs
                    else None
                )
                effective_window_secs = requested_window_secs or 60

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
                    # Boundary-side observability parity with pydantic-ai
                    # (`core/run.py:213-235`). Stamp the same attrs the
                    # pydantic boundary stamps so eval probes work
                    # uniformly: the narrative span wraps the rejection
                    # emit, and turn-level zero counters keep the
                    # attribute set identical to the normal-completion
                    # shape (just zeroed). Without these, the refusal-
                    # suite probes (`zero-tool-calls-on-injection`,
                    # `narrative-emitted-approved`, etc) fail under
                    # codex because the attributes don't exist.
                    with _tracer.start_as_current_span(
                        spans.NARRATIVE_EMITTED
                    ) as nar_span:
                        nar_span.set_attribute(
                            spans.Attrs.NARRATIVE_VERDICT,
                            spans.VERDICT_APPROVED,
                        )
                        # Apply the narrative-output channel switch.
                        # Channel off => empty text, suppressed=true on
                        # the span. Channel on => unchanged text,
                        # length stamped. Shared helper with the
                        # pydantic-ai path (`core/run.py`).
                        sse_text = resolve_narrative_text(
                            rejection_text,
                            narrative_output_enabled=(
                                request.switches.channels.narrative_output_enabled
                            ),
                            nar_span=nar_span,
                        )
                        if sse_text:
                            nar_span.set_attribute(
                                spans.Attrs.NARRATIVE_TEXT, sse_text
                            )
                        nar_span.set_attribute(
                            spans.Attrs.NARRATIVE_ASSEMBLED_PROVENANCE_COUNT,
                            0,
                        )
                        turn_span.set_attribute(
                            spans.Attrs.TURN_UNSAFE_INPUT_REJECTED, "true"
                        )
                        turn_span.set_attribute(
                            spans.Attrs.TURN_UNSAFE_INPUT_PATTERN, e.pattern
                        )
                    # Chunk 4: persist the rejection so history replay
                    # shows the same shape the live UI saw  a bubble
                    # explaining why we shut the turn down. The
                    # snapshot stores the SSE-shaped text (post-
                    # suppression) so reopens render the same thing
                    # the live user saw.
                    thread.record_turn_narrative(
                        turn,
                        NarrativeSnapshot(text=sse_text),
                    )
                    yield _frame(
                        "Narrative",
                        narrative_pb2.NarrativeWithRefs(
                            text=sse_text,
                            provenance=[],
                        ),
                    )
                    # Stamp turn-level zero counters so the boundary-
                    # short-circuit produces the same attribute set as
                    # the normal-completion path. Eval probes assert
                    # against these (`mcae.turn.tool_calls=0`,
                    # `mcae.turn.claims_emitted=0`) to verify the
                    # rejection didn't silently fire any tools.
                    turn_span.set_attribute(spans.Attrs.TURN_CLAIMS_EMITTED, 0)
                    turn_span.set_attribute(spans.Attrs.TURN_CLAIMS_APPROVED, 0)
                    turn_span.set_attribute(spans.Attrs.TURN_TOOL_CALLS, 0)
                    turn_span.set_attribute(
                        spans.Attrs.TURN_NARRATIVE_CHARS, len(sse_text)
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

                # Snapshot lease. Same `PrimitiveClient.begin_turn` the
                # pydantic-ai path uses; one snapshot per turn. The
                # `requested_window_secs` was resolved earlier from
                # `request.context.live_window_secs`; `None` lets the
                # data plane apply its 60s default.
                lease = await handles.primitive_client.begin_turn(
                    window_secs=requested_window_secs,
                )
                snapshot_id = lease.snapshot_id
                # Stamp the resolved window on the turn root span so
                # OTel queries can attribute "this codex turn ran over
                # a 15-minute slice" without correlating against the
                # inbound request. Mirror of the loop_driver.py stamp;
                # `lease.window_secs` is the ground truth from the
                # data plane, not what we asked for.
                turn_span.set_attribute(
                    spans.Attrs.SNAPSHOT_WINDOW_SECS, lease.window_secs
                )
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

                # Per-turn developer instructions = the composed
                # system prompt (single source of truth in
                # `prompts/system_v4.txt`, dropping rules per the
                # switches passed in this turn) + the codex-specific
                # tool-surface footer + the snapshot pin. The codex
                # profile carries only a stub identity message so the
                # session-pool fingerprint stays stable across turns
                # while switch-driven rule drops flow per-turn the
                # same way the pydantic-ai path handles them.
                turn_drops = drops_from_switches(request.switches)
                composed_system = _adapt_system_prompt_for_codex(
                    compose_system_prompt(
                        drop_rule_ids=turn_drops,
                        live_window_secs=effective_window_secs,
                    )
                )
                turn_dev_instructions = (
                    f"{composed_system}\n\n"
                    f"{_CODEX_TOOL_SURFACE_FOOTER}\n\n"
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
                # Resolve codex primary model + reasoning effort with
                # the three-tier fallback the frontend's builder view
                # expects:
                #   1. per-turn `request.codex_override.{model_id,
                #      reasoning_effort}` from the Models panel,
                #   2. lifespan `handles.codex_primary_model` /
                #      `handles.codex_reasoning_effort` resolved from
                #      `CODEX_PRIMARY_MODEL` / `CODEX_REASONING_EFFORT`
                #      env at startup,
                #   3. codex-cli's own default (None on the wire).
                # Empty strings on the wire are treated as "no
                # override at this tier" so the panel can clear a
                # pin without re-emitting a value.
                override_model: str | None = None
                override_effort: str | None = None
                if request.HasField("codex_override"):
                    if request.codex_override.model_id:
                        override_model = request.codex_override.model_id
                    if request.codex_override.reasoning_effort:
                        override_effort = (
                            request.codex_override.reasoning_effort
                        )
                effective_model = (
                    override_model or handles.codex_primary_model
                )
                effective_effort = (
                    override_effort or handles.codex_reasoning_effort
                )
                # Stamp the effective values on the turn span so
                # eval probes + Langfuse can attribute model/effort
                # to the right turn without correlating against
                # env-snapshot files. `*_source` tells per-turn
                # readers whether this turn came from a UI pin vs
                # the env fallback.
                if effective_model is not None:
                    turn_span.set_attribute(
                        "mcae.codex.model.id", effective_model
                    )
                    turn_span.set_attribute(
                        "mcae.codex.model.source",
                        "override" if override_model else "env",
                    )
                if effective_effort is not None:
                    turn_span.set_attribute(
                        "mcae.codex.reasoning_effort", effective_effort
                    )
                    turn_span.set_attribute(
                        "mcae.codex.reasoning_effort.source",
                        "override" if override_effort else "env",
                    )

                codex_request = CodexRunRequest(
                    prompt=request.user_question,
                    actor_id=thread_id,
                    provider_thread_id=(
                        thread.codex_provider_thread_id or None
                    ),
                    developer_instructions=turn_dev_instructions,
                    context_items=context_items,
                    model=effective_model,
                    reasoning_effort=effective_effort,
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
                # Counts every TOOL_COMPLETED event. Stamped as
                # `mcae.turn.tool_calls` on the turn span at end-of-
                # turn so eval probes (e.g. refusal-suite's
                # `zero-tool-calls-on-injection`) work uniformly
                # across runtimes. Pydantic-ai counts
                # `deps.tool_call_records` for the same attr; this
                # is the codex-side analog.
                tool_completed_count = 0
                codex_error: Exception | None = None
                # Chunk 3.5 item 7: track per-tool args between
                # TOOL_STARTED and TOOL_COMPLETED so we can record a
                # full `TurnToolCallRecord` once the output lands.
                # Keyed by `tool_id` (codex's per-call id).
                pending_tool_calls: dict[str, tuple[str, dict[str, Any]]] = {}
                # Phase-2 eval observability. `pending_tool_spans`
                # holds the open `mcae.primitive.<tool>` span + its
                # start monotonic for each in-flight tool call. We
                # open on TOOL_STARTED and close on TOOL_COMPLETED
                # so eval probes that query
                # `mcae.primitive.wallet_profile` (today emitted
                # only by `primitive_client.py` on the pydantic-ai
                # path) see equivalent spans on the codex path.
                # Any spans still in the dict when this function
                # unwinds get closed defensively in the outer
                # `finally` block at the end of `run_turn_codex`.
                # Hoisted to function scope so the finally can see
                # them.
                pending_tool_spans.clear()
                # Synthetic `chat codex.<model>` generation span
                # spanning the codex worker block. Pydantic-ai's
                # `instrument_all` creates equivalent `chat <model>`
                # spans automatically; codex doesn't go through
                # that instrumentation, so we open one here and
                # stamp the GenAI semconv attrs on it (instead of
                # on the turn span, which would turn the trace
                # root into a generation observation in Langfuse
                # and conflate "turn" with "LLM inference").
                # Opened with the bare name `chat codex`; renamed
                # to `chat codex.<model>` after the model name
                # comes back from the sqlite read post-loop. Also
                # hoisted to function scope so the bottom finally
                # can close it on exception.
                chat_span = _tracer.start_span("chat codex")
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
                            # Track the pre-suppression char count
                            # regardless of channel state so the
                            # cockpit telemetry (`pre_suppression_chars`
                            # on the narrative span) is honest about
                            # what the model produced.
                            streamed_chars += len(evt.text)
                            # Drop the delta frame when the narrative-
                            # output channel is off. The model keeps
                            # streaming on codex's side (we can't tell
                            # codex-cli to stop mid-turn), but the
                            # SSE consumer sees no streaming prose.
                            # Matches pydantic-ai's behavior of
                            # suppressing at the final-emit boundary,
                            # extended here to the live delta channel
                            # codex adds on top.
                            if request.switches.channels.narrative_output_enabled:
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
                        # Phase-2 eval observability. Open the
                        # synthetic `mcae.primitive.<tool>` span so
                        # eval probes that scan for it see equivalent
                        # spans on the codex path. Skips tools we
                        # don't recognize (any future tool would
                        # need a `_PRIMITIVE_SPAN_NAMES` entry to
                        # show up). Both the span and its start
                        # monotonic land in `pending_tool_spans`;
                        # TOOL_COMPLETED closes the span and stamps
                        # duration + output digest.
                        if sig is not None and evt.tool_id:
                            tname = sig[0]
                            span_name = _PRIMITIVE_SPAN_NAMES.get(tname)
                            if span_name is not None:
                                ps = _tracer.start_span(span_name)
                                ps.set_attribute(
                                    spans.Attrs.SNAPSHOT_ID,
                                    snapshot_id or "",
                                )
                                ps.set_attribute(
                                    spans.Attrs.PRIMITIVE_INPUT,
                                    _capped_json(sig[1]),
                                )
                                # Stamp typed input attrs so eval
                                # probes that filter on `input.addr`
                                # / `input.community_id` / `input.mint`
                                # work without parsing JSON in CH.
                                args = sig[1]
                                if isinstance(args, dict):
                                    if isinstance(args.get("addr"), str):
                                        ps.set_attribute(
                                            spans.Attrs.PRIMITIVE_INPUT_ADDR,
                                            args["addr"],
                                        )
                                    if isinstance(args.get("community_id"), int):
                                        ps.set_attribute(
                                            spans.Attrs.PRIMITIVE_INPUT_COMMUNITY_ID,
                                            args["community_id"],
                                        )
                                    if isinstance(args.get("mint"), str):
                                        ps.set_attribute(
                                            spans.Attrs.PRIMITIVE_INPUT_MINT,
                                            args["mint"],
                                        )
                                pending_tool_spans[evt.tool_id] = (
                                    ps,
                                    time.monotonic(),
                                )
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
                        tool_completed_count += 1
                        # Phase-2 eval observability. Close the
                        # primitive span opened on TOOL_STARTED.
                        # Stamps duration_ms (so latency probes
                        # work), output payload (capped), and
                        # output digest. If the codex tool errored,
                        # `output` carries an error message instead
                        # of the envelope; we stamp it as the
                        # output, set `error=True`, and still close
                        # the span so the probe sees the failure.
                        ps_entry = (
                            pending_tool_spans.pop(evt.tool_id, None)
                            if evt.tool_id
                            else None
                        )
                        if ps_entry is not None:
                            ps, ps_t0 = ps_entry
                            ps.set_attribute(
                                spans.Attrs.PRIMITIVE_DURATION_MS,
                                int((time.monotonic() - ps_t0) * 1000),
                            )
                            ps.set_attribute(
                                spans.Attrs.PRIMITIVE_OUTPUT,
                                _capped_json(evt.output or "")
                                if not isinstance(evt.output, str)
                                else (
                                    evt.output[
                                        : spans.PRIMITIVE_PAYLOAD_MAX_BYTES
                                    ]
                                    if len(evt.output.encode("utf-8"))
                                    <= spans.PRIMITIVE_PAYLOAD_MAX_BYTES
                                    else (
                                        evt.output[
                                            : spans.PRIMITIVE_PAYLOAD_MAX_BYTES
                                        ]
                                        + f" ...[truncated, total={len(evt.output.encode('utf-8'))}]"
                                    )
                                ),
                            )
                            ps.set_attribute(
                                spans.Attrs.PRIMITIVE_OUTPUT_DIGEST,
                                _digest12(evt.output),
                            )
                            # Codex marks tool errors via the output
                            # payload shape (no `structuredContent`);
                            # `_extract_mcp_envelope` returning None
                            # is the canonical "tool errored"
                            # signal, same one the binding-store
                            # recorder uses.
                            if (
                                _extract_mcp_envelope(evt.output) is None
                                and tool_name in _PRIMITIVE_SPAN_NAMES
                            ):
                                ps.set_attribute("error", True)
                            ps.end()
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

                # Chunk 3.7 cost observability  GenAI semconv
                # bridge for Langfuse. Stamp the OTel `gen_ai.*`
                # keys that Langfuse converts into a GENERATION
                # observation with auto-computed cost against its
                # model-pricing table. Without these attrs, codex
                # turns show `totalCost: 0` and an empty token
                # column on the traces list (the pydantic-ai path
                # has these stamped by its own `instrument_all`
                # integration, see otel.py).
                #
                # Stamped on the synthetic `chat codex.<model>`
                # CHILD span (not the turn root) so the trace
                # topology matches pydantic-ai's: one GENERATION
                # observation hanging off the turn root, not the
                # turn root itself being a generation. This keeps
                # the turn span semantically a "session" / "run"
                # and the chat span semantically "one LLM call",
                # which is the shape every Langfuse dashboard +
                # eval probe already expects.
                #
                # We stamp from `.last` (per-turn breakdown). The
                # `.total` (thread-cumulative) breakdown stays on
                # the `codex.tokens.total.*` keys on the turn span
                # for SQL aggregation.
                #
                # The model name comes from a tiny sqlite read on
                # codex's per-thread state_5.sqlite (WAL-mode, so
                # we don't block codex's own writes). Soft-fail:
                # on any sqlite error we still stamp usage but
                # without a model, the chat span keeps its bare
                # `chat codex` name, and Langfuse shows tokens
                # with no auto-cost.
                if latest_token_usage is not None:
                    chat_span.set_attribute(
                        spans.Attrs.GEN_AI_SYSTEM, "openai"
                    )
                    chat_span.set_attribute(
                        spans.Attrs.GEN_AI_USAGE_INPUT_TOKENS,
                        latest_token_usage.last.input_tokens,
                    )
                    chat_span.set_attribute(
                        spans.Attrs.GEN_AI_USAGE_OUTPUT_TOKENS,
                        latest_token_usage.last.output_tokens,
                    )
                    chat_span.set_attribute(
                        spans.Attrs.GEN_AI_USAGE_TOTAL_TOKENS,
                        latest_token_usage.last.total_tokens,
                    )
                    if latest_token_usage.last.cached_input_tokens:
                        chat_span.set_attribute(
                            spans.Attrs.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS,
                            latest_token_usage.last.cached_input_tokens,
                        )
                    codex_model = _read_codex_model(
                        codex_home_root=handles.codex_home_root,
                        thread_id=thread_id,
                        provider_thread_id=provider_thread_id_local,
                    )
                    if codex_model:
                        chat_span.set_attribute(
                            spans.Attrs.GEN_AI_REQUEST_MODEL, codex_model
                        )
                        chat_span.set_attribute(
                            spans.Attrs.GEN_AI_RESPONSE_MODEL, codex_model
                        )
                        # Rename the chat span to embed the model
                        # for human readability (`chat codex.gpt-5.5`).
                        # The `llm_call_used_model` eval probe
                        # matches on `gen_ai.request.model` attr
                        # not the span name, so the rename is
                        # decorative for probes but useful in
                        # Langfuse / CH trace listings.
                        chat_span.update_name(
                            f"chat codex.{codex_model}"
                        )

                # Close the chat span now that all `gen_ai.*` and
                # name updates have landed. The span's duration
                # then reflects the worker-loop wall time, which
                # is "how long codex spent on this turn"  the
                # natural Langfuse generation duration. We set
                # `chat_span = None` so the outer finally doesn't
                # double-close on the happy path.
                chat_span.end()
                chat_span = None

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
                        thread.record_turn_claim(turn, claim)
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
                # Assemble narrative-side provenance from approved
                # claims in emission order, matching pydantic-ai's
                # contract (`core/run.py:421-423`). `${ref:N}` in the
                # narrative indexes ACROSS all claims' provenance, so
                # the index space is the concatenation, not per-claim.
                assembled_provenance: list[provenance_pb2.ProvenanceRef] = []
                for _c in approved_claim_list:
                    assembled_provenance.extend(_c.provenance)
                narrative_retracted = False
                # Narrative-level placeholder gate. Runs BEFORE the
                # constitution gate (mirroring `core/run.py:474`) so
                # an out-of-bounds `${ref:N}` retracts deterministically
                # and we don't pay the constitution LLM cost on prose
                # that's already going to be retracted. The span emits
                # even on vacuous passes (text has no `${ref:N}`) so
                # eval probes querying `mcae.gate.placeholder` see
                # parity with the pydantic-ai path.
                placeholder_narrative_reason: str | None = None
                if final_text:
                    with _tracer.start_as_current_span(
                        spans.GATE_PLACEHOLDER
                    ) as pg:
                        pg.set_attribute(
                            spans.Attrs.GATE_VERSION, _PLACEHOLDER_VERSION
                        )
                        ref_err = validate_refs(
                            final_text, len(assembled_provenance)
                        )
                        if ref_err is not None:
                            pg.set_attribute(
                                spans.Attrs.GATE_VERDICT,
                                spans.VERDICT_RETRACTED,
                            )
                            placeholder_narrative_reason = (
                                ref_err.to_human_string()
                            )
                            pg.set_attribute(
                                spans.Attrs.GATE_REASON,
                                placeholder_narrative_reason,
                            )
                        else:
                            pg.set_attribute(
                                spans.Attrs.GATE_VERDICT,
                                spans.VERDICT_APPROVED,
                            )
                if placeholder_narrative_reason is not None:
                    # Placeholder retract: route through the retracted
                    # emit branch with the placeholder reason so the
                    # constitution gate is skipped (same short-circuit
                    # pydantic-ai's `narrative_snapshot` path uses).
                    ret = narrative_pb2.NarrativeRetracted(
                        text=final_text,
                        reason=placeholder_narrative_reason,
                    )
                    if handles.debug_public:
                        ret.debug_reason = (
                            f"placeholder_validate: {placeholder_narrative_reason}"
                        )
                    with _tracer.start_as_current_span(
                        spans.NARRATIVE_EMITTED
                    ) as nar_span:
                        nar_span.set_attribute(
                            spans.Attrs.NARRATIVE_VERDICT,
                            spans.VERDICT_RETRACTED,
                        )
                        sse_text = resolve_narrative_text(
                            final_text,
                            narrative_output_enabled=(
                                request.switches.channels.narrative_output_enabled
                            ),
                            nar_span=nar_span,
                        )
                        if sse_text:
                            nar_span.set_attribute(
                                spans.Attrs.NARRATIVE_TEXT, sse_text
                            )
                        nar_span.set_attribute(
                            spans.Attrs.NARRATIVE_ASSEMBLED_PROVENANCE_COUNT,
                            len(assembled_provenance),
                        )
                        ret.text = sse_text
                        thread.record_turn_narrative(
                            turn,
                            NarrativeSnapshot(
                                text=sse_text,
                                retracted_reason=placeholder_narrative_reason,
                            ),
                        )
                        yield _frame("NarrativeRetracted", ret)
                    narrative_retracted = True
                if (
                    not narrative_retracted
                    and request.switches.stay_in_role.defend_constitution_judge
                    and final_text
                ):
                    role_t_const = time.monotonic()
                    # When the turn opts into a non-default live
                    # window, rebuild the constitution agent for this
                    # turn so its policy prompt's `${LIVE_WINDOW_HUMAN}`
                    # placeholder matches what the primary agent saw.
                    # Without this, the gate's "60-second live window"
                    # framing would retract a correct 15-minute
                    # narrative as window-mismatched. Sub-ms build
                    # cost, dwarfed by the LLM call that follows.
                    constitution_agent_for_turn = (
                        build_constitution_agent(
                            live_window_secs=effective_window_secs,
                        )
                        if effective_window_secs != 60
                        else handles.constitution_agent
                    )
                    with _tracer.start_as_current_span(
                        spans.GATE_NARRATIVE_CONSTITUTION
                    ) as g:
                        g.set_attribute(
                            spans.Attrs.GATE_VERSION,
                            constitution_module.VERSION,
                        )
                        verdict = await with_provider_retry(
                            lambda: judge_narrative(
                                constitution_agent_for_turn,
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
                            # Wrap retract emit in `mcae.narrative.emitted`
                            # span so eval probes querying narrative-leg
                            # outcomes see the same trace shape as the
                            # pydantic-ai path. Verdict is `retracted`;
                            # provenance count is 0 because retracted
                            # prose can't safely carry chips.
                            with _tracer.start_as_current_span(
                                spans.NARRATIVE_EMITTED
                            ) as nar_span:
                                nar_span.set_attribute(
                                    spans.Attrs.NARRATIVE_VERDICT,
                                    spans.VERDICT_RETRACTED,
                                )
                                # Apply the narrative-output channel
                                # switch to the retracted text. Channel
                                # off => empty text, suppressed=true.
                                # The retraction `reason` stays
                                # unmodified (it's structured metadata,
                                # not the model's prose); the user
                                # still sees WHY the bubble was
                                # retracted, just with no prose body.
                                sse_text = resolve_narrative_text(
                                    final_text,
                                    narrative_output_enabled=(
                                        request.switches.channels.narrative_output_enabled
                                    ),
                                    nar_span=nar_span,
                                )
                                if sse_text:
                                    nar_span.set_attribute(
                                        spans.Attrs.NARRATIVE_TEXT, sse_text
                                    )
                                nar_span.set_attribute(
                                    spans.Attrs.NARRATIVE_ASSEMBLED_PROVENANCE_COUNT,
                                    len(assembled_provenance),
                                )
                                # The retract frame carries the
                                # suppressed text; reason flows
                                # unchanged so the UI can still render
                                # the retraction badge.
                                ret.text = sse_text
                                # Chunk 4 history record: retracted snapshot
                                # carries the retraction reason so the
                                # replay path can render the same muted /
                                # amber bubble the live UI renders.
                                thread.record_turn_narrative(
                                    turn,
                                    NarrativeSnapshot(
                                        text=sse_text,
                                        retracted_reason=retraction_reason,
                                    ),
                                )
                                yield _frame("NarrativeRetracted", ret)
                            narrative_retracted = True
                    role_timings["policy"] = role_timings.get(
                        "policy", 0.0
                    ) + (time.monotonic() - role_t_const)

                if not narrative_retracted:
                    # Wrap approved narrative emit in
                    # `mcae.narrative.emitted` span so eval probes
                    # (`narrative-emitted-approved` etc) see the same
                    # trace shape as pydantic-ai. Verdict is
                    # `approved`; provenance count is the assembled
                    # provenance length (empty in this MVP until
                    # codex-side assembled-provenance ships).
                    with _tracer.start_as_current_span(
                        spans.NARRATIVE_EMITTED
                    ) as nar_span:
                        nar_span.set_attribute(
                            spans.Attrs.NARRATIVE_VERDICT,
                            spans.VERDICT_APPROVED,
                        )
                        # Apply the narrative-output channel switch
                        # to the approved prose. Channel off => empty
                        # text, suppressed=true on the span. Channel
                        # on => unchanged text, length stamped.
                        sse_text = resolve_narrative_text(
                            final_text,
                            narrative_output_enabled=(
                                request.switches.channels.narrative_output_enabled
                            ),
                            nar_span=nar_span,
                        )
                        if sse_text:
                            nar_span.set_attribute(
                                spans.Attrs.NARRATIVE_TEXT, sse_text
                            )
                        nar_span.set_attribute(
                            spans.Attrs.NARRATIVE_ASSEMBLED_PROVENANCE_COUNT,
                            len(assembled_provenance),
                        )
                        # Chunk 4 history record: approved snapshot.
                        # Snapshot stores the SSE-shaped (post-
                        # suppression) text so history reopens render
                        # what the live user actually saw; provenance
                        # is the assembled list from this turn's
                        # approved claims so chip-ref resolution in
                        # history matches the live frame.
                        thread.record_turn_narrative(
                            turn,
                            NarrativeSnapshot(
                                text=sse_text,
                                provenance=list(assembled_provenance),
                            ),
                        )
                        yield _frame(
                            "Narrative",
                            narrative_pb2.NarrativeWithRefs(
                                text=sse_text,
                                provenance=assembled_provenance,
                            ),
                        )
                turn_span.set_attribute(
                    spans.Attrs.TURN_NARRATIVE_CHARS, len(final_text)
                )
                # Mirror pydantic-ai's end-of-turn count stamp
                # (`core/run.py:572`) so the refusal-suite's
                # `turn_attribute_equals(mcae.turn.tool_calls, "0")`
                # probes resolve to zero on a refusal turn that
                # didn't dispatch any tools (and to the actual
                # count otherwise).
                turn_span.set_attribute(
                    spans.Attrs.TURN_TOOL_CALLS, tool_completed_count
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
        # Phase-2 eval span cleanup. Same shape as the drain-task
        # cancel above: an exception unwound the turn before the
        # happy-path `.end()` fired, so close defensively here so
        # spans don't leak (unclosed spans miss the BatchSpanProcessor
        # window and never reach the collector). Mark each
        # remaining primitive span `error=True` so eval probes can
        # tell "tool started, turn aborted" from a clean completion.
        for tool_id, (ps, _t0) in pending_tool_spans.items():
            try:
                ps.set_attribute("error", True)
                ps.end()
            except Exception:  # noqa: BLE001
                pass
        pending_tool_spans.clear()
        if chat_span is not None:
            try:
                chat_span.end()
            except Exception:  # noqa: BLE001
                pass
        # Persist thread state regardless of success or failure so
        # the codex_provider_thread_id assignment survives a turn
        # that errored mid-flight.
        if thread_for_persist is not None:
            try:
                handles.threads.persist(thread_for_persist)
            except Exception:  # noqa: BLE001
                log.exception("thread_persist_failed", thread_id=thread_id)
