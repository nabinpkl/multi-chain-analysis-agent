"""FastAPI entrypoint for the Phase 0 walking skeleton.

Routes:
- `GET  /health` -> `{"status": "ok"}`
- `POST /agent/ask` -> `AgentSessionStarted` (stashes pending session)
- `GET  /agent/stream/{session_id}` -> SSE stream of Claim, AgentDone, Done

Two-call handoff matches the existing Rust `/agent/ask` + `/agent/stream`
pattern so the frontend wiring in Phase C is a one-line env-var change,
not a protocol rewrite.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

import structlog
from fastapi import FastAPI, HTTPException
from sse_starlette.sse import EventSourceResponse

from .agent import AgentDeps, build_agent
from .primitive_client import PrimitiveClient
from .wire import AgentDone, AgentRequest, AgentSessionStarted, Claim, Done

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# In-memory pending-session map for /agent/ask -> /agent/stream handoff.
# Phase 0 only; Phase B.5 builds proper thread state with locks.
# ---------------------------------------------------------------------------


@dataclass
class PendingSession:
    request: AgentRequest
    thread_id: str


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


@app.post("/agent/ask", response_model=AgentSessionStarted)
async def ask(req: AgentRequest) -> AgentSessionStarted:
    """Stash the request under a fresh session_id; the SSE handler
    picks it up and runs the agent.

    Phase 0 returns immediately. Phase B.6 grows this into the real
    session lifecycle (thread lookup, switches, ledger session-start
    event)."""
    session_id = secrets.token_urlsafe(16)
    thread_id = req.thread_id or str(uuid.uuid4())
    _PENDING[session_id] = PendingSession(request=req, thread_id=thread_id)
    log.info(
        "agent_ask",
        session_id=session_id,
        thread_id=thread_id,
        focus_addr=req.focus_addr,
    )
    return AgentSessionStarted(session_id=session_id, thread_id=thread_id)


@app.get("/agent/stream/{session_id}")
async def stream(session_id: str) -> EventSourceResponse:
    """SSE stream that runs the agent and emits frames. Phase 0 emits:
    `Claim`, `AgentDone`, `Done`. Phase B.2 wires the full 9-frame set
    via codegen-aligned shapes."""
    pending = _PENDING.pop(session_id, None)
    if pending is None:
        raise HTTPException(status_code=404, detail="session not found or already consumed")

    async def event_iter():
        try:
            agent = app.state.agent
            deps = AgentDeps(
                primitive_client=app.state.primitive_client,
                focus_addr=pending.request.focus_addr,
            )
            user_prompt = (
                f"Focused wallet: {pending.request.focus_addr}\n\n"
                f"Question: {pending.request.question}"
            )
            log.info("agent_run_started", session_id=session_id)
            result = await agent.run(user_prompt, deps=deps)
            claim: Claim = result.output
            log.info(
                "agent_run_completed",
                session_id=session_id,
                addr=claim.addr,
                tokens=result.usage().total_tokens
                if hasattr(result.usage(), "total_tokens")
                else None,
            )
            yield {"event": "claim", "data": claim.model_dump_json()}
            yield {
                "event": "agent-done",
                "data": AgentDone(session_id=session_id).model_dump_json(),
            }
            yield {"event": "done", "data": Done().model_dump_json()}
        except asyncio.CancelledError:
            log.info("agent_stream_cancelled", session_id=session_id)
            raise
        except Exception as e:  # noqa: BLE001 - Phase 0 catch-all so the SSE doesn't hang
            log.exception("agent_stream_failed", session_id=session_id)
            yield {"event": "error", "data": f'{{"message": "{type(e).__name__}: {e}"}}'}
            yield {"event": "done", "data": Done(ok=False).model_dump_json()}

    # Critical SSE headers: nginx / cloudflared / browsers won't buffer
    # if these are set. The plan calls these out as risk #1.
    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return EventSourceResponse(event_iter(), headers=headers)
