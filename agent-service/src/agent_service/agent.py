"""Phase 0 walking-skeleton agent. One tool (`wallet_profile`), one
output type (`Claim`), no gate, no thread state, no diff. Proves the
end-to-end shape of `Pydantic AI -> Rust primitive -> SSE Claim`.

Phase B.1 grows this into the real loop driver with structural +
constitution gates and proper UsageLimits.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai import Agent, RunContext

from .llm import primary_model
from .primitive_client import PrimitiveClient, PrimitiveError
from .wire import Claim


@dataclass
class AgentDeps:
    """Per-turn dependencies handed to the Pydantic AI agent."""

    primitive_client: PrimitiveClient
    focus_addr: str


SYSTEM_PROMPT = (
    "You analyse Solana wallets observed in a live 60-second graph window. "
    "Call the `wallet_profile` tool with the wallet address you are asked "
    "about, then return a one-line summary suitable for a `Claim`. "
    "Use the `top_counterparties`, `stats.total_volume_lamports`, and "
    "`role` fields to ground the summary. "
    "Always return a single Claim object via `final_result`."
)


def build_agent() -> Agent[AgentDeps, Claim]:
    """Construct the Phase 0 agent. Built fresh per process; Pydantic
    AI agents are cheap to instantiate."""

    agent: Agent[AgentDeps, Claim] = Agent(
        model=primary_model(),
        deps_type=AgentDeps,
        output_type=Claim,
        system_prompt=SYSTEM_PROMPT,
    )

    @agent.tool
    async def wallet_profile(ctx: RunContext[AgentDeps], addr: str) -> dict:
        """Profile a Solana wallet observed in the live 60-second
        window. Returns role, community membership, total volume, and
        top counterparties.

        Args:
            addr: Solana wallet address (base58 pubkey). If unsure,
                use the focused wallet from the system prompt.
        """
        try:
            out = await ctx.deps.primitive_client.wallet_profile(addr)
        except PrimitiveError as e:
            # Surface the structured error back to the model; it can
            # decide whether to abort or summarize gracefully.
            return {"error": e.kind, "message": e.message}
        return out.model_dump()

    return agent
