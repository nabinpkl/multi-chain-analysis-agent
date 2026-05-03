"""Phase A walking-skeleton agent. Two tools (`wallet_profile`,
`community_summary`), one output type (`Claim`), no gate, no thread
state, no diff. Proves the snapshot-lease end-to-end and that two
primitives can be composed in one turn against a consistent view.

Phase B.1 grows this into the real loop driver with structural +
constitution gates and proper UsageLimits.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai import Agent, RunContext

from .llm import primary_model
from .primitive_client import PrimitiveClient, PrimitiveError
from .wire import Claim
from .wire.shared import (
    CommunitySummaryInput,
    WalletProfileInput,
)


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
    "`community_summary` for context. Then return a single `Claim` "
    "summarising what you found. Ground the summary in the values returned "
    "by the tools. Always return via `final_result`."
)


def build_agent() -> Agent[AgentDeps, Claim]:
    agent: Agent[AgentDeps, Claim] = Agent(
        model=primary_model(),
        deps_type=AgentDeps,
        output_type=Claim,
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
            # `time_scope` accepts the bare string "live" via the
            # generated RootModel's coercion. Pass via dict so we
            # don't depend on the per-file duplicate `TimeScope`
            # class identity (datamodel-codegen emits a fresh
            # TimeScope class per consuming file).
            input_ = WalletProfileInput.model_validate(
                {"addr": addr, "time_scope": "live"}
            )
            out = await ctx.deps.primitive_client.wallet_profile(
                input_, ctx.deps.snapshot_id
            )
        except PrimitiveError as e:
            return {"error": e.kind, "message": e.message}
        return out.model_dump()

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
            input_ = CommunitySummaryInput.model_validate(
                {"community_id": community_id, "time_scope": "live"}
            )
            out = await ctx.deps.primitive_client.community_summary(
                input_, ctx.deps.snapshot_id
            )
        except PrimitiveError as e:
            return {"error": e.kind, "message": e.message}
        return out.model_dump()

    return agent
