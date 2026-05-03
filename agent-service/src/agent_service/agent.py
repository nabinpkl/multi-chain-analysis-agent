"""Phase 0/A walking-skeleton agent. Two tools (`wallet_profile`,
`community_summary`), `output_type=str` (free-form narrative), no
gate, no thread state, no diff. Proves the snapshot-lease end-to-end
and that two primitives can be composed in one turn against a
consistent view.

Phase I locked the wire shapes (full `Claim`, `NarrativeWithRefs`,
the gate types). Phase II rewrites this file to wire `emit_claim`
as a tool, drop `output_type=str`, load the verbatim system prompt,
and run the actual two-channel contract. Don't enrich this file
further; it goes away when Phase II lands.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai import Agent, RunContext

from .llm import primary_model
from .primitive_client import PrimitiveClient, PrimitiveError


@dataclass
class AgentDeps:
    """Per-turn dependencies handed to the Pydantic AI agent. The
    `snapshot_id` is opened by the caller (FastAPI handler) before
    `agent.run()` and released after; tool functions here just read
    it from deps."""

    primitive_client: PrimitiveClient
    snapshot_id: str
    focus_addr: str


SYSTEM_PROMPT = (
    "You analyse Solana wallets observed in a live 60-second graph window. "
    "You have two tools:\n"
    "  - `wallet_profile`: profile a single wallet (role, community, "
    "volumes, top counterparties).\n"
    "  - `community_summary`: summarise a community by id (size, internal "
    "vs external volume, top members).\n\n"
    "Workflow: call `wallet_profile` for the focused wallet first. If the "
    "result includes a `community_id`, optionally follow up with "
    "`community_summary` for context. Then return a single short narrative "
    "summarising what you found, grounded in the values returned by the "
    "tools."
)


def build_agent() -> Agent[AgentDeps, str]:
    """Phase I: `output_type=str` keeps the walking-skeleton runnable
    (TestModel can auto-generate any string) without bringing along
    the stub Claim shape we just deleted. Phase II swaps to the real
    contract: `output_type=str` for the narrative channel and an
    `emit_claim` tool for typed Claim emission.
    """
    agent: Agent[AgentDeps, str] = Agent(
        model=primary_model(),
        deps_type=AgentDeps,
        output_type=str,
        system_prompt=SYSTEM_PROMPT,
    )

    @agent.tool
    async def wallet_profile(ctx: RunContext[AgentDeps], addr: str) -> dict:
        """Profile a Solana wallet observed in the live 60-second window.

        Args:
            addr: Solana wallet address (base58 pubkey). Use the focused
                wallet from the system prompt if unsure.
        """
        try:
            # primitive_client constructs the proto request internally
            # (TimeScope=Live by default). Returns PrimitiveResult with
            # `value` already a Python dict materialized from the
            # google.protobuf.Struct envelope field.
            result = await ctx.deps.primitive_client.wallet_profile(
                addr=addr,
                snapshot_id=ctx.deps.snapshot_id,
            )
        except PrimitiveError as e:
            return {"error": e.kind, "message": e.message}
        return result.value

    @agent.tool
    async def community_summary(
        ctx: RunContext[AgentDeps], community_id: int
    ) -> dict:
        """Summarise a community by its numeric id (typically obtained
        from a prior `wallet_profile.community_id` field).

        Args:
            community_id: Stable community label (u32) reported by the
                analytics layer.
        """
        try:
            result = await ctx.deps.primitive_client.community_summary(
                community_id=community_id,
                snapshot_id=ctx.deps.snapshot_id,
            )
        except PrimitiveError as e:
            return {"error": e.kind, "message": e.message}
        return result.value

    return agent
