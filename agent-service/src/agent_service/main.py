"""FastAPI entrypoint for the Phase 0/A walking skeleton.

Routes:
- `GET  /health` -> `{"status": "ok"}`
- `POST /agent/ask` -> `AgentSessionStarted` (stashes pending session)
- `GET  /agent/stream/{session_id}` -> SSE stream of NarrativeWithRefs,
  AgentDone, Done

Two-call handoff matches the existing Rust `/agent/ask` + `/agent/stream`
pattern so the frontend wiring in Phase C is a one-line env-var change,
not a protocol rewrite.

Phase I locked the wire shapes (full `Claim`, `NarrativeWithRefs`, gate
types) but did NOT rewrite the loop. This handler still emits a
walking-skeleton narrative wrapping the agent's free-form string. Phase
II rewrites this to honor the two-channel contract (Claims via
`emit_claim` tool + Narrative as the agent's `output_type=str`).
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

import structlog
from fastapi import FastAPI, HTTPException
from sse_starlette.sse import EventSourceResponse

from .agent import AgentDeps, build_agent
from .primitive_client import PrimitiveClient
from .wire import (
    AgentDone,
    AgentRequest,
    AgentSessionStarted,
    NarrativeWithRefs,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# In-memory pending-session map for /agent/ask -> /agent/stream handoff.
# Phase 0/A only; Phase V (#7) builds proper thread state with locks.
# ---------------------------------------------------------------------------


@dataclass
class PendingSession:
    request: AgentRequest
    thread_id: str
    started_at_ms: int


_PENDING: dict[str, PendingSession] = {}


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    base_url = os.environ.get("DATA_PLANE_URL", "http://api:8002")
    log.info("agent_service_starting", data_plane=base_url)
    app.state.primitive_client = PrimitiveClient(base_url=base_url)
    app.state.agent = build_agent()
    try:
        yield
    finally:
        log.info("agent_service_stopping")
        await app.state.primitive_client.close()


app = FastAPI(title="multichain agent-service", version="0.1.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def _focus_addr_or_die(req: AgentRequest) -> str:
    """Phase 0/A walking-skeleton: the focused wallet still drives the
    single primitive call. Real loop reads `context.focus` + selection
    + the model's tool dispatch instead. This helper isolates the
    walking-skeleton coupling so Phase II can delete it cleanly."""
    focus = req.context.focus
    if focus is None or focus.kind != "wallet":
        raise HTTPException(
            status_code=400,
            detail="Phase 0/A walking skeleton requires context.focus to be a wallet ref",
        )
    return focus.id


@app.post("/agent/ask", response_model=AgentSessionStarted)
async def ask(req: AgentRequest) -> AgentSessionStarted:
    """Stash the request under a fresh session_id; the SSE handler
    picks it up and runs the agent. Phase 0/A only validates the
    request shape; Phase VI grows this into the real session
    lifecycle (thread lookup, switches honored, ledger session-start
    event)."""
    # Validate the focus-addr coupling early so a misconfigured
    # frontend sees a synchronous 400, not a delayed SSE error frame.
    _focus_addr_or_die(req)

    session_id = secrets.token_urlsafe(16)
    thread_id = req.thread_id or str(uuid.uuid4())
    _PENDING[session_id] = PendingSession(
        request=req,
        thread_id=thread_id,
        started_at_ms=int(time.time() * 1000),
    )
    log.info(
        "agent_ask",
        session_id=session_id,
        thread_id=thread_id,
        focus=req.context.focus.model_dump() if req.context.focus else None,
    )
    return AgentSessionStarted(session_id=session_id, thread_id=thread_id, turn=0)


@app.get("/agent/stream/{session_id}")
async def stream(session_id: str) -> EventSourceResponse:
    """SSE stream that runs the agent and emits frames. Phase 0/A
    emits: `Narrative`, `Done`. Phase VI wires the full 9-frame set
    (Claim via `emit_claim` tool, GatePath when `show_trace`,
    NoMovement / ChangedSince on the diff path)."""
    pending = _PENDING.pop(session_id, None)
    if pending is None:
        raise HTTPException(status_code=404, detail="session not found or already consumed")

    async def event_iter():
        snapshot_id: str | None = None
        try:
            agent = app.state.agent
            client = app.state.primitive_client
            focus_addr = _focus_addr_or_die(pending.request)

            # Phase A snapshot lease: pin a consistent view across
            # every primitive call this turn. Released in `finally`
            # below; if the request is cancelled mid-flight, GC
            # sweeps it within 5 minutes.
            lease = await client.begin_turn()
            snapshot_id = lease.snapshot_id
            log.info(
                "turn_begin",
                session_id=session_id,
                snapshot_id=snapshot_id,
                expires_at_ms=lease.expires_at_ms,
            )

            deps = AgentDeps(
                primitive_client=client,
                snapshot_id=snapshot_id,
                focus_addr=focus_addr,
            )
            user_prompt = (
                f"Focused wallet: {focus_addr}\n\n"
                f"Question: {pending.request.user_question}"
            )
            log.info("agent_run_started", session_id=session_id)
            result = await agent.run(user_prompt, deps=deps)
            narrative_text: str = result.output
            log.info(
                "agent_run_completed",
                session_id=session_id,
                tokens=result.usage().total_tokens
                if hasattr(result.usage(), "total_tokens")
                else None,
            )

            # Phase 0/A: emit the agent's free-form output as a
            # NarrativeWithRefs with empty provenance. Phase II will
            # replace this with proper Claim emission via the
            # `emit_claim` tool plus a NarrativeWithRefs assembled
            # from the turn's claim provenance.
            narrative = NarrativeWithRefs(text=narrative_text, provenance=[])
            yield {"event": "Narrative", "data": narrative.model_dump_json()}

            elapsed_ms = max(0, int(time.time() * 1000) - pending.started_at_ms)
            done_payload = AgentDone(session_id=session_id, elapsed_ms=elapsed_ms)
            yield {"event": "Done", "data": done_payload.model_dump_json()}
        except asyncio.CancelledError:
            log.info("agent_stream_cancelled", session_id=session_id)
            raise
        except Exception as e:  # noqa: BLE001 - Phase 0 catch-all so the SSE doesn't hang
            log.exception("agent_stream_failed", session_id=session_id)
            yield {"event": "Error", "data": f'{{"message": "{type(e).__name__}: {e}"}}'}
            elapsed_ms = max(0, int(time.time() * 1000) - pending.started_at_ms)
            done_payload = AgentDone(session_id=session_id, elapsed_ms=elapsed_ms)
            yield {"event": "Done", "data": done_payload.model_dump_json()}
        finally:
            if snapshot_id is not None:
                await app.state.primitive_client.end_turn(snapshot_id)

    # Critical SSE headers: nginx / cloudflared / browsers won't buffer
    # if these are set. The plan calls these out as risk #1.
    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return EventSourceResponse(event_iter(), headers=headers)
