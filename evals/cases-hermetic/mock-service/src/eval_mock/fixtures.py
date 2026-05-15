"""Per-process fixture store. Single global instance feeds both the
FastAPI HTTP shim (pydantic-ai's `PrimitiveClient` path) and the
FastMCP proxy (codex's MCP path). The eval runner loads fixtures
via `POST /eval/setup` before each case and clears via
`DELETE /eval/setup` after. Cases run sequentially in the hermetic
runner, so single-shared-store semantics are correct without per-case
keying.

Mirrors the `EvalFixtures` pydantic model in
`agent_service.evals.schema`; the runner POSTs that model's
`model_dump_json()` here.

Claim plumbing (codex side): `emit_claims` MCP calls land here per
snapshot, and the `GET /turn/{snapshot_id}/claims` SSE handler drains
them. Plain `asyncio.Queue` per snapshot is enough; the hermetic
runner never opens more than one drain consumer per snapshot.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class _GetTokenInfoFixture:
    mint: str
    name: str | None = None
    symbol: str | None = None
    uri: str | None = None
    update_authority: str | None = None
    source_program: str = "token2022"


@dataclass(slots=True)
class _WalletProfileFixture:
    addr: str
    payload: dict[str, Any]


@dataclass(slots=True)
class _CommunitySummaryFixture:
    community_id: int
    payload: dict[str, Any]


@dataclass(slots=True)
class FixtureStore:
    """In-memory, per-process. `setup` replaces the whole shape;
    `clear` resets to empty defaults. Concurrent eval runs against
    one mock process would race on this state; the hermetic runner's
    sequential case loop is the only supported caller.
    """

    get_token_info: dict[str, _GetTokenInfoFixture] = field(default_factory=dict)
    wallet_profile: dict[str, _WalletProfileFixture] = field(default_factory=dict)
    community_summary: dict[int, _CommunitySummaryFixture] = field(default_factory=dict)
    # Per-snapshot claim queues. `emit_claims` puts onto them; the SSE
    # drain consumes. `snapshot_id` strings are the keys (matching the
    # ULID-style ids the mock's `/turn/begin` mints).
    claim_queues: dict[str, asyncio.Queue[dict[str, Any] | None]] = field(
        default_factory=dict
    )
    # Active snapshot ids. `/turn/begin` adds, `/turn/end` removes
    # and signals end-of-stream on the matching claim queue (None
    # sentinel) so the drain loop exits.
    active_snapshots: set[str] = field(default_factory=set)
    # Per-snapshot tool-call counters. Mirrors Rust's
    # `AppState::tool_call_budgets` so the codex hermetic path
    # exercises the same budget contract as production. `/turn/begin`
    # initializes the counter to 0; the MCP `call_tool` dispatcher
    # increments before each budgeted read tool and short-circuits
    # with the `no_more_lookups_this_turn` envelope when the counter
    # reaches the cap. `/turn/end` removes the entry.
    tool_call_counts: dict[str, int] = field(default_factory=dict)

    def setup(
        self,
        *,
        get_token_info: list[dict[str, Any]] | None = None,
        wallet_profile: list[dict[str, Any]] | None = None,
        community_summary: list[dict[str, Any]] | None = None,
    ) -> None:
        self.clear()
        for entry in get_token_info or []:
            self.get_token_info[entry["mint"]] = _GetTokenInfoFixture(
                mint=entry["mint"],
                name=entry.get("name"),
                symbol=entry.get("symbol"),
                uri=entry.get("uri"),
                update_authority=entry.get("update_authority"),
                source_program=entry.get("source_program", "token2022"),
            )
        for entry in wallet_profile or []:
            self.wallet_profile[entry["addr"]] = _WalletProfileFixture(
                addr=entry["addr"], payload=entry["payload"]
            )
        for entry in community_summary or []:
            cid = int(entry["community_id"])
            self.community_summary[cid] = _CommunitySummaryFixture(
                community_id=cid, payload=entry["payload"]
            )

    def clear(self) -> None:
        self.get_token_info.clear()
        self.wallet_profile.clear()
        self.community_summary.clear()
        # Drain any active queues to unblock parked consumers; the
        # next eval's `/turn/begin` will mint fresh queues.
        for snap_id, q in list(self.claim_queues.items()):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:  # pragma: no cover  unbounded queues
                pass
            self.claim_queues.pop(snap_id, None)
        self.active_snapshots.clear()
        self.tool_call_counts.clear()

    def register_snapshot(self, snapshot_id: str) -> None:
        self.active_snapshots.add(snapshot_id)
        self.claim_queues.setdefault(snapshot_id, asyncio.Queue())
        self.tool_call_counts.setdefault(snapshot_id, 0)

    def end_snapshot(self, snapshot_id: str) -> None:
        if snapshot_id not in self.active_snapshots:
            return
        self.active_snapshots.discard(snapshot_id)
        q = self.claim_queues.get(snapshot_id)
        if q is not None:
            q.put_nowait(None)
        self.tool_call_counts.pop(snapshot_id, None)

    def try_consume_budget(self, snapshot_id: str, cap: int) -> bool:
        """Increment the per-snapshot dispatch counter. Returns True
        when the caller may proceed; False when the caller MUST
        short-circuit with the no_more_lookups envelope because the
        cap has been reached. Mirrors Rust's `try_consume_budget` in
        `backend/src/mcp.rs` byte-for-byte (atomic increment, roll
        back on exhaustion so `mcae.turn.tool_calls` reads the exact
        dispatch count, not cap+1).

        Missing snapshot_id (the caller bypassed `/turn/begin`) ->
        skip the cap entirely. This matches Rust's behavior, where
        a missing `tool_call_budgets` entry causes
        `try_consume_budget` to return None and let the dispatch
        proceed.
        """
        if snapshot_id not in self.tool_call_counts:
            return True
        used = self.tool_call_counts[snapshot_id]
        if used >= cap:
            return False
        self.tool_call_counts[snapshot_id] = used + 1
        return True

    def push_claim(self, snapshot_id: str, claim: dict[str, Any]) -> None:
        q = self.claim_queues.get(snapshot_id)
        if q is None:
            # MCP `emit_claims` arrived before `/turn/begin` minted the
            # queue. Mint a queue on the fly so the claim isn't lost.
            q = asyncio.Queue()
            self.claim_queues[snapshot_id] = q
            self.active_snapshots.add(snapshot_id)
        q.put_nowait(claim)


# Module-level singleton. `eval_mock.main` imports this and threads it
# through every route handler via `Depends` so unit-testable handlers
# can override with a fresh `FixtureStore` per test.
STORE = FixtureStore()
