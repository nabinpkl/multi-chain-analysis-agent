"""Process-tool-call hook for pydantic-ai's MCP toolset.

Bridges between the pydantic-ai Agent and the Rust MCP server at
`http://api:8004/mcp`. The hook fires on every MCP tool call AFTER
the Rust handler runs and BEFORE the model sees the response, and
owns the per-tool side effects that used to live in the four
`@agent.tool` bodies in `agent.py`:

- Injects `snapshot_id` into `tool_args` for the three read primitives.
  Rust looks up the per-snapshot budget counter and the live-window
  graph slice from this argument; pydantic-ai has the snapshot in
  `ctx.deps.snapshot_id` from the per-turn lease.
- Populates the binding store from the structured MCP response so the
  structural value-compare gate (`run_post_tools_phase`) has its
  ammunition.
- Records the tool call for ship-4 replay (`AgentDeps.tool_call_records`).
- Sets `AgentDeps.budget_exhausted_fired` when Rust returns the
  `no_more_lookups_this_turn` sentinel so `mcae.turn.budget_exhausted`
  gets stamped correctly downstream.
- Sanitizes `get_token_info`'s name / symbol / uri when the
  external-text-input channel switch is off.
- Re-wraps the response in `<external_data>` so the model sees the
  same envelope shape every test expects. Rust ALSO produces the
  wrapped text on the `content` field, but pydantic-ai's
  `direct_call_tool` prefers `structuredContent` when both are set,
  so the wrapped text doesn't reach the hook automatically. The
  Python `boundary.wrap_external_data` is byte-identical to Rust's
  (verified in Phase 1 prep at [backend/src/mcp.rs:76-80] vs
  [agent_service/boundary.py:193-208]), so the model behavior is
  unchanged.

Why the hook owns these instead of `@agent.tool` bodies: post-Phase-2
of the runtime-alignment work, the canonical tool surface is the Rust
MCP server and pydantic-ai is a thin client. Tool descriptions,
schemas, and arg validation all come from Rust; Python only carries
the side effects that depend on per-turn state (binding store, replay
records, channel switches).
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import structlog
from opentelemetry import trace
from pydantic_ai.mcp import CallToolFunc, ToolResult
from pydantic_ai.tools import RunContext

from agent_service import spans
from agent_service.agent import AgentDeps, ToolCallRecord
from agent_service.boundary import sanitize_token_info_payload, wrap_external_data
from agent_service.core.post_tools import provenance_refs_from_json
from agent_service.policy.binding_store import build_binding
from agent_service.policy.resource_bounds import NO_MORE_LOOKUPS_ERROR_KIND

log = structlog.get_logger(__name__)
_tracer = trace.get_tracer(__name__)


# Tools that take `snapshot_id` as a required arg. The hook injects
# it from `ctx.deps.snapshot_id` so the model's prompt context never
# carries the value verbatim (saves prompt tokens, avoids leaking the
# id into chat history, eliminates any model-trust requirement around
# snapshot pinning). All four MCP tools take `snapshot_id`:
# `wallet_profile`, `community_summary`, `get_token_info` route by it,
# and `emit_claims` writes its batch to the per-snapshot mpsc channel
# keyed by it.
_SNAPSHOT_PINNED_TOOLS: frozenset[str] = frozenset(
    {"wallet_profile", "community_summary", "get_token_info", "emit_claims"}
)


# Primitives whose output contributes to the binding store. The
# structural value-compare gate later cross-references claims' numeric
# refs against numbers seen in these primitives' values.
# `get_token_info` is excluded because its fields are all strings.
# `emit_claims` is excluded because it doesn't return graph data.
_BINDING_TOOLS: frozenset[str] = frozenset(
    {"wallet_profile", "community_summary"}
)


# Tools that contribute to the ship-4 replay tape. `emit_claims`
# is excluded because its drained claims are recorded separately.
_REPLAY_TAPE_TOOLS: frozenset[str] = frozenset(
    {"wallet_profile", "community_summary", "get_token_info"}
)


async def process_tool_call(
    ctx: RunContext[AgentDeps],
    call_tool: CallToolFunc,
    name: str,
    tool_args: dict[str, Any],
) -> ToolResult:
    """Intercept each MCP tool call. Inject snapshot_id, run per-tool
    side effects against the structured MCP response, return the
    `<external_data>` envelope the model has been calibrated against.
    """
    deps: AgentDeps = ctx.deps

    if name in _SNAPSHOT_PINNED_TOOLS:
        tool_args["snapshot_id"] = deps.snapshot_id

    with _tracer.start_as_current_span(f"mcae.primitive.{name}") as span:
        span.set_attribute(spans.Attrs.PRIMITIVE_INPUT, _capped_json(tool_args))

        result = await call_tool(name, tool_args, None)

        # Rust sets `structured_content` on every read tool's return,
        # so pydantic-ai's `direct_call_tool` returns a dict here for
        # the three read primitives plus `emit_claims`. Defensive
        # branch in case Rust ever emits text-only.
        if not isinstance(result, dict):
            return result

        # Budget exhaustion: Rust's `try_consume_budget` routes through
        # `tool_result_external_data` with a payload carrying
        # `error: NO_MORE_LOOKUPS_ERROR_KIND`. Surface to deps so the
        # turn-aggregate stamping downstream marks the turn
        # budget-exhausted; re-wrap and return so the model sees the
        # canonical envelope.
        if result.get("error") == NO_MORE_LOOKUPS_ERROR_KIND:
            deps.budget_exhausted_fired = True
            return wrap_external_data(name, result)

        # Three structured shapes from Rust:
        #
        #  wallet_profile     -> {"value": <WalletProfileOutput>, "provenance": [...]}
        #  community_summary  -> {"value": <CommunitySummaryOutput>, "provenance": [...]}
        #  get_token_info     -> {"mint": ..., "name": ..., ...}  (no envelope wrapper)
        #  emit_claims        -> {"accepted": <int>, ...}  (ack dict)
        if "value" in result and "provenance" in result:
            value = result["value"]
            provenance_json = result["provenance"]
        else:
            value = result
            provenance_json = []

        call_id = f"{name}:{uuid.uuid4().hex[:12]}"

        if name in _BINDING_TOOLS:
            captured_at_ms = int(time.time() * 1000)
            binding = build_binding(
                primitive=name,
                call_id=call_id,
                captured_at_ms=captured_at_ms,
                value_json=value,
                provenance=provenance_refs_from_json(provenance_json),
            )
            deps.binding_store.record(binding)

        if name in _REPLAY_TAPE_TOOLS:
            deps.tool_call_records.append(
                ToolCallRecord(
                    primitive_name=name,
                    args=dict(tool_args),
                    output_value=value,
                    call_id=call_id,
                )
            )

        # `get_token_info`'s name / symbol / uri are attacker-controlled
        # on-chain strings. When the external_text_input channel is off,
        # redact them before the model sees the payload. Mirrors the
        # pre-Phase-2 `@agent.tool` body at agent.py:366-372.
        model_payload: Any = value
        if name == "get_token_info" and not deps.external_text_input_enabled:
            model_payload = sanitize_token_info_payload(model_payload)
            span.set_attribute(
                spans.Attrs.PRIMITIVE_GET_TOKEN_INFO_SANITIZED, True
            )

        # Re-wrap. The Python wrap_external_data and Rust's
        # `wrap_external_data` produce byte-identical envelopes; this
        # call gives the model the same string the pre-Phase-2 path
        # produced.
        return wrap_external_data(name, model_payload)


def _capped_json(value: Any, cap: int = spans.PRIMITIVE_PAYLOAD_MAX_BYTES) -> str:
    """Truncated JSON dump for span attributes. Same overflow marker
    convention the codex driver uses so eval probes can detect
    truncation consistently across runtimes."""
    try:
        s = json.dumps(value, separators=(",", ":"))
    except (TypeError, ValueError):
        s = str(value)
    if len(s) > cap:
        return s[:cap] + f" ...[truncated, total={len(s)}]"
    return s
