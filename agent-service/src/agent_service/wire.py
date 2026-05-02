"""Phase 0 SSE wire types. Five frames stubbed; the remaining four
(`Progress`, `Narrative`, `NarrativeRetracted`, `Error`, `GatePath`,
`NoMovement`, `ChangedSince`) land in Phase B.2 alongside the codegen
pipeline that auto-derives the matching frontend TS.

Hand-written here on purpose: Phase 0 is a walking skeleton, not the
real wire contract. Once Phase B.2 wires `dump_agent_schemas.py +
json-schema-to-typescript`, this file becomes the source of truth.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    """Forbid extra fields by default. Catches schema drift fast."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Inbound: client -> /agent/ask
# ---------------------------------------------------------------------------


class AgentRequest(_StrictModel):
    """Question the user asked plus the focused wallet (Phase 0 only
    reads `focus_addr`; the full `ViewContext` lands in Phase B.2)."""

    question: str
    focus_addr: str
    thread_id: str | None = None


# ---------------------------------------------------------------------------
# Outbound: /agent/ask response
# ---------------------------------------------------------------------------


class AgentSessionStarted(_StrictModel):
    """Returned synchronously from POST /agent/ask. The frontend opens
    GET /agent/stream/{session_id} to pick up the SSE stream."""

    session_id: str
    thread_id: str


# ---------------------------------------------------------------------------
# SSE frames
# ---------------------------------------------------------------------------


class Claim(_StrictModel):
    """Phase 0 minimal Claim. Real shape in Phase B.2 includes
    `provenance: list[ProvenanceRef]`, `kind`, `policy_verdict`,
    `stub_markers`, `emitted_at_ms`. For now: enough to prove the
    end-to-end SSE round-trip."""

    kind: Literal["wallet-profile"] = "wallet-profile"
    addr: str
    summary: str = Field(
        description="One-line agent-generated summary of the wallet.",
    )


class AgentDone(_StrictModel):
    """Per-turn closer. Phase B.2 adds `cost`, `gate_verdicts`, etc."""

    session_id: str
    turn_count: int = 1


class Done(_StrictModel):
    """Final SSE event. Frontend uses this to close the EventSource."""

    ok: bool = True
