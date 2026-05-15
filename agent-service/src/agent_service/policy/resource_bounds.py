"""Per-turn resource-bound policy. One source of truth for the
tool-call budget both runtimes obey.

When the per-turn tool-call budget is exhausted, the next data-lookup
primitive dispatch must NOT execute and must NOT raise. It returns a
structured `no_more_lookups_this_turn` tool result wrapped in the
existing `<external_data>` envelope so the model sees it as a normal
tool result, pivots to finalizing its narrative over the data it
already gathered this turn, and lets the existing gate stack
(constitution + binding) judge whatever it produces.

The cap value comes from the env var `AGENT_TURN_TOOL_CALL_BUDGET`
(default 8). Both pydantic-ai and the Rust MCP server read the same
env var so the two runtimes stay in lockstep.

Only the three read-side primitives count against the budget:
`wallet_profile`, `community_summary`, `get_token_info`. The reporting
tool `emit_claim` is exempt because it doesn't dispatch a lookup.
"""

from __future__ import annotations

import os
from typing import Final


TURN_TOOL_CALL_BUDGET: Final[int] = int(
    os.environ.get("AGENT_TURN_TOOL_CALL_BUDGET", "8")
)

NO_MORE_LOOKUPS_ERROR_KIND: Final[str] = "no_more_lookups_this_turn"

NO_MORE_LOOKUPS_GUIDANCE: Final[str] = (
    "You have used this turn's tool budget. Do not call any "
    "more lookup tools. Finalize the answer using only the "
    "data already returned this turn. If the data so far is "
    "not enough, say so honestly in the narrative."
)

NO_MORE_LOOKUPS_PAYLOAD: Final[dict[str, str]] = {
    "error": NO_MORE_LOOKUPS_ERROR_KIND,
    "guidance": NO_MORE_LOOKUPS_GUIDANCE,
}

BUDGETED_PRIMITIVES: Final[frozenset[str]] = frozenset(
    {"wallet_profile", "community_summary", "get_token_info"}
)


def is_budget_exhausted(used: int) -> bool:
    """True when the caller must short-circuit instead of dispatching
    the next lookup. `used` is the count of dispatches already made
    this turn; the cap is reached when used >= budget."""
    return used >= TURN_TOOL_CALL_BUDGET
