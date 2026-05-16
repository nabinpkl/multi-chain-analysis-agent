"""Pydantic AI agent wired to the Rust MCP server.

After the runtime-alignment work (Phase 2), pydantic-ai consumes its
entire tool surface from `http://${DATA_PLANE_URL}/mcp` via
`MCPServerStreamableHTTP`. The four tools (`wallet_profile`,
`community_summary`, `get_token_info`, `emit_claims`) are authored
once in `backend/src/mcp.rs`; pydantic-ai sees them via MCP
introspection at agent build, and per-tool side effects (binding
store population, `tool_call_records`, budget tracking, channel
suppression for `get_token_info`) live in
`agent_service.mcp_hook.process_tool_call`.

Output channel: `output_type=str` for the narrative leg. The agent's
final string is the Narrative. Claims flow out-of-band: the model
calls the MCP `emit_claims` tool (batched, one call per turn), Rust
writes the claims to a per-snapshot mpsc channel, and
`core.run.run_one_turn` drains the channel via SSE before invoking
`run_post_tools_phase` over the drained list. Same two-channel
contract the codex path uses.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStreamableHTTP

from agent_service import llm
from agent_service.policy.binding_store import PrimitiveBindingStore
from agent_service.prompts.composer import compose_system_prompt

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Tool input shapes (used by the post-tools gate stack to validate
# drained claims, NOT by pydantic-ai's own tool dispatch  the model
# calls Rust's `emit_claims` via MCP, the drained payloads come back
# via SSE and are validated against these classes before reaching the
# gate stack).
# ---------------------------------------------------------------------------


class _ProvenanceRefIn(BaseModel):
    """Validation shape for one provenance entry. Tagged union via
    `kind` plus per-kind fields. `core.post_tools._map_provenance`
    maps these into proto `ProvenanceRef` messages before structural
    verification."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(description='"wallet" | "community" | "edge" | "time_range" | "number"')
    # wallet
    addr: str | None = None
    idx: int | None = None
    # edge
    edge_id: str | None = None
    src: int | None = None
    dst: int | None = None
    # community
    id: int | None = None
    # time_range
    from_s: int | None = None
    to_s: int | None = None
    # number
    metric: str | None = None
    value: float | None = None
    support: list[str] = Field(default_factory=list)


class _NumberRefIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str
    value: float
    support: list[str] = Field(default_factory=list)


class EmitClaimInput(BaseModel):
    """One emitted claim's wire shape. Mirrors Rust's `ClaimInput` in
    `backend/src/mcp.rs:329` byte-for-byte. The MCP tool's
    `inputSchema` is generated from Rust's schemars derive, so the
    model sees the same per-field guidance whichever runtime is
    routing the call; this Python class exists only so the drain
    consumer can `model_validate` each entry into a typed shape the
    gate stack consumes."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(
        description='Claim kind: "PROFILE" | "PATTERN" | "COMPARISON" | "SUMMARY" | "PULSE"'
    )
    headline: str = Field(description="One sentence under 100 chars")
    body_markdown: str = Field(
        description="Structured paragraph; use ${ref:N} placeholders for chip references"
    )
    provenance: list[_ProvenanceRefIn] = Field(
        description="Non-empty list of typed entity references; ${ref:N} resolves against this"
    )
    support_numbers: list[_NumberRefIn] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Per-turn replay record
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolCallRecord:
    """One read-primitive call captured for ship-4 repeat replay.
    Populated by `mcp_hook.process_tool_call` for the three read
    primitives; the loop driver lifts the wallet_profile /
    community_summary entries onto thread state after the turn."""

    primitive_name: str
    args: dict
    output_value: dict
    call_id: str


# ---------------------------------------------------------------------------
# Per-turn deps handed to the MCP hook
# ---------------------------------------------------------------------------


@dataclass
class AgentDeps:
    """Per-turn dependencies the MCP hook reads from `ctx.deps`. Built
    in `core.run.run_one_turn` before `agent.run()` and torn down
    after."""

    # Snapshot id from `primitive_client.begin_turn`; the hook injects
    # it into every snapshot-pinned tool call so the model's prompt
    # context stays clean of the value.
    snapshot_id: str
    # Wall-clock at turn start; used by `core.post_tools._build_claim`
    # to compute `emitted_at_ms` on drained claims.
    turn_started_at_ms: int
    # Live binding store (thread-scoped, persists across turns; loop
    # driver passes the thread's binding store here so this turn's
    # primitive outputs land in the same store the structural gate
    # later reads).
    binding_store: PrimitiveBindingStore
    # Channel switch: when False, the hook redacts `get_token_info`'s
    # attacker-controlled `name`/`symbol`/`uri` fields before the
    # model sees the payload. Default True matches the production
    # preset; tests and the loop driver pass the value from the
    # per-turn switch envelope.
    external_text_input_enabled: bool = True
    # Per-turn replay tape. Populated by the MCP hook for the three
    # read primitives; loop driver pulls these out after `agent.run()`
    # to record into thread state for ship-4 replay.
    tool_call_records: list["ToolCallRecord"] = field(default_factory=list)
    # Flipped true by the MCP hook when Rust's
    # `try_consume_budget` short-circuits a dispatch with the
    # `no_more_lookups_this_turn` sentinel. Read by
    # `core.post_tools.run_post_tools_phase` to stamp
    # `mcae.turn.budget_exhausted` on the turn span.
    budget_exhausted_fired: bool = False


# ---------------------------------------------------------------------------
# Agent constructor
# ---------------------------------------------------------------------------


def _mcp_server_url() -> str:
    """Resolve the MCP endpoint URL from env. Matches the codex
    profile convention at `codex_profile.py:79`; default works for
    the docker compose internal network where the Rust container is
    addressable as `api`."""
    base = os.environ.get("DATA_PLANE_URL", "http://api:8004").rstrip("/")
    return f"{base}/mcp"


def build_agent(
    *,
    drop_rule_ids: Iterable[str] = (),
    llm_override=None,
    live_window_secs: int = 60,
) -> Agent[AgentDeps, str]:
    """Construct the production agent.

    Tool surface: discovered live from the Rust MCP server. Pydantic-ai
    introspects `tools/list` at agent build, generates input models from
    Rust's schemars schemas, and routes every call through
    `agent_service.mcp_hook.process_tool_call`. Per-tool side effects
    (binding store, replay tape, budget tracking, token-info
    sanitization, `<external_data>` re-wrapping) live in the hook.

    Output channel: free-form string (the narrative). Claims flow out
    of band via the per-snapshot SSE drain.

    Args:
        drop_rule_ids: optional set of rule ids to elide from the
            system prompt before agent construction. Production
            preset is the empty set; the loop driver populates this
            from per-defense switches so the article-side ablation
            surface can disable individual defenses without touching
            the .txt file. See `prompts/composer.py` for the
            rule-id namespace.
        llm_override: optional `RoleOverride`-shaped object pinning
            the primary agent to a specific provider + model id for
            this turn. Empty / None = production preset (env-driven
            OpenRouter). Set by the dev builder view's Models
            section; production frontend never populates it. See
            `agent_service.llm.make_model` for resolution rules.
        live_window_secs: live-window seconds the snapshot will be
            materialized against. Substituted into the system
            prompt's `${LIVE_WINDOW_HUMAN}` placeholder so the agent
            sees the right framing for the window it'll actually
            analyze. Default 60 matches the data plane's default
            window and produces "last 60 seconds" prose; eval cases
            that widen the window pass the value through to keep
            the prompt aligned with the snapshot.
    """
    # Import inside the function to avoid circular dependency: mcp_hook
    # imports AgentDeps / ToolCallRecord from this module, and we
    # import the hook here for the toolset wiring.
    from agent_service.mcp_hook import process_tool_call

    mcp_toolset = MCPServerStreamableHTTP(
        url=_mcp_server_url(),
        process_tool_call=process_tool_call,
    )

    return Agent[AgentDeps, str](
        model=llm.make_model("primary", override=llm_override),
        deps_type=AgentDeps,
        output_type=str,
        toolsets=[mcp_toolset],
        system_prompt=compose_system_prompt(
            drop_rule_ids=drop_rule_ids,
            live_window_secs=live_window_secs,
        ),
    )
