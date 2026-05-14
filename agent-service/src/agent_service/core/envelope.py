"""Role-agnostic input shape for one turn through the agent core.

A driver (chat, monitor, scheduled pulse, peer-consult, …) is
responsible for normalizing its world into one of these. The core
reads from the envelope without knowing which driver wrote it.

What's in the envelope:

* `turn_id` / `correlation_id`  identity. `turn_id` names this single
  turn (chat: per-message; monitor: per-alert). `correlation_id`
  groups related turns (chat: session id; monitor: rule run id).
* `switches`  the full `AgentSwitches` proto, governs every defense,
  cross-check, and channel.
* `run_type`  production / eval / article / etc. Stamped on the OTel
  turn span so probes can filter.
* `intent`  what the LLM sees as the user message. The chat driver
  passes the user question verbatim. A future monitor driver passes
  a formatted brief ("an alert matching <rule X> just landed at slot N
  from <signer>  analyze and surface anything notable"). The core
  treats it as opaque text.
* `view_context`  focused entities (wallet, edge, community) and
  the live window. May be `None` for drivers without a focus concept.
* `history`  prior turns for multi-turn surfaces. Chat populates
  this from thread state; single-shot drivers leave it empty.

What's NOT in the envelope (deliberately):

* Anything Trigger-shaped per-driver. The "this is a chat / monitor /
  pulse" distinction is a driver concern. The core sees normalized
  fields, not a discriminated union.
* Thread / session bookkeeping. That's chat-specific state and lives
  in the chat driver, not in the foundation type.
* Sinks. Sinks are passed alongside the envelope, not inside it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from multichain.wire.agent.v1 import entity_pb2 as ent_pb
from multichain.wire.agent.v1 import switches_pb2


@dataclass(slots=True, kw_only=True)
class TurnEnvelope:
    """Role-agnostic input for one turn through `core.run_one_turn`."""

    # Identity --------------------------------------------------------
    turn_id: str
    correlation_id: str

    # Control ---------------------------------------------------------
    switches: switches_pb2.AgentSwitches
    run_type: str

    # What the LLM sees as the user message. Drivers normalize their
    # trigger into this string. Core treats it as opaque text and
    # passes it through `boundary.build_context_block` /
    # `boundary.reject_if_unsafe_user_question` like any other untrusted
    # input.
    intent: str

    # Focused entities + live window. May be None for drivers without
    # a focus concept (e.g. a future monitor agent reacting to a global
    # rule rather than to a focused wallet).
    view_context: ent_pb.ViewContext | None = None

    # Prior agent turns the model should see for context. Empty for
    # single-shot drivers. Type stays `Any` here to avoid pulling
    # pydantic-ai types into the foundation; the chat driver populates
    # this from `thread.message_history` and pydantic-ai validates it
    # at agent.run() time.
    history: list[Any] = field(default_factory=list)

    # Optional per-role LLM override for this turn (a `RoleOverride`).
    # Empty / None = production preset (env-driven provider stack).
    # `primary_llm_override` is threaded into the per-turn rebuild of
    # the primary agent on the per-defense drop path. `policy_llm_override`
    # is forwarded per-call to `judge_claim` (which routes through
    # `runtime_call`) so the dev Models panel's policy-role pick still
    # takes effect even though the gate itself is now stateless.
    primary_llm_override: Any | None = None
    policy_llm_override: Any | None = None

    # Resolved live window the snapshot was materialized against, in
    # seconds. Set by the driver from
    # `request.context.live_window_secs` (after resolving 0/missing
    # to the 60s default) so the core can rebuild the primary agent
    # with the matching prompt when either drops OR a non-default
    # window is in effect for this turn. Mirrors the data plane's
    # `SnapshotBeginResponse.window_secs`. Default 60 keeps the
    # constructor backward compatible with callers that pre-date
    # window parameterization.
    live_window_secs: int = 60
