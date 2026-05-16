"""Per-turn tool-call budget constants.

Post-Phase-2 of the runtime-alignment work, the tool-call budget is
enforced entirely Rust-side at `backend/src/mcp.rs::try_consume_budget`.
Both runtimes consume the Rust MCP server now, so the in-process Python
counter that used to live here is dead. What remains is the sentinel
strings both runtimes pattern-match on the wire:

* `NO_MORE_LOOKUPS_ERROR_KIND` is the `error` field value Rust sets on
  the `<external_data>`-wrapped envelope when a primitive dispatch
  trips the cap. `mcp_hook.process_tool_call` checks for it on the
  pydantic-ai path; `codex_driver._pump_codex_events` checks for it
  on the codex path. Used to flip `AgentDeps.budget_exhausted_fired`
  / equivalent so `mcae.turn.budget_exhausted` gets stamped.

* `NO_MORE_LOOKUPS_GUIDANCE` is the model-visible guidance text Rust
  pairs with the error. Kept here so a future log query that wants to
  match the exact wording (e.g. "did we ever return the budget
  envelope this hour") has a single Python-side constant to import.

The cap value itself lives at `backend/src/mcp.rs:140` (read from the
`AGENT_TURN_TOOL_CALL_BUDGET` env on Rust startup); the Python side
no longer reads that env.
"""

from __future__ import annotations

from typing import Final


NO_MORE_LOOKUPS_ERROR_KIND: Final[str] = "no_more_lookups_this_turn"

NO_MORE_LOOKUPS_GUIDANCE: Final[str] = (
    "You have used this turn's tool budget. Do not call any "
    "more lookup tools. Finalize the answer using only the "
    "data already returned this turn. If the data so far is "
    "not enough, say so honestly in the narrative."
)
