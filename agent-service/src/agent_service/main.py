"""FastAPI entrypoint for the Phase II agent plane.

Routes:
- `GET  /health` -> `{"status": "ok"}`
- `POST /agent/ask` -> `AgentSessionStarted` (proto canonical JSON;
  stashes pending session)
- `GET  /agent/stream/{session_id}` -> SSE stream of all 9 frame
  variants (Claim, NarrativeWithRefs, NarrativeRetracted, Progress,
  Error, Done, NoMovement, ChangedSince, GatePath)

Two-call handoff matches the Rust pattern. Frontend wiring is one
env-var (`NEXT_PUBLIC_AGENT_URL=http://localhost:8003`).

Wire format per AGENTS.md "Wire format per hop": browser hops carry
proto canonical JSON. Inbound `AgentRequest` parses via
`json_format.Parse`; outbound responses serialize via
`json_format.MessageToJson(preserving_proto_field_name=False)` for
camelCase field names.

Loop orchestration lives in `loop_driver.run_turn`; this module
handles HTTP wiring, session handoff, lifespan setup of the long-lived
clients (Pydantic AI agents, primitive client, thread registry,
ledger).
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
from fastapi import FastAPI, HTTPException, Request, Response
from google.protobuf import json_format
from sse_starlette.sse import EventSourceResponse

from multichain.wire.agent.v1 import session_pb2 as sess_pb

from .agent import build_agent
from .ledger.writer import Ledger
from .loop_driver import LoopHandles, run_turn
from .policy.constitution import build_constitution_agent
from .primitive_client import PrimitiveClient
from .repeat_detector import build_repeat_agent
from .thread_state import ThreadRegistry

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# In-memory pending-session map for /agent/ask -> /agent/stream handoff.
# Per-turn, single-consumer; the handler pops on read.
# ---------------------------------------------------------------------------


@dataclass
class PendingSession:
    request: sess_pb.AgentRequest
    thread_id: str
    session_started_at_ms: int


_PENDING: dict[str, PendingSession] = {}


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    base_url = os.environ.get("DATA_PLANE_URL", "http://api:8002")
    debug_public = os.environ.get("AGENT_DEBUG_PUBLIC", "0") == "1"
    log.info("agent_service_starting", data_plane=base_url, debug_public=debug_public)

    primitive_client = PrimitiveClient(base_url=base_url)
    threads = ThreadRegistry()
    ledger = await Ledger.connect()

    handles = LoopHandles(
        primary_agent=build_agent(),
        constitution_agent=build_constitution_agent(),
        repeat_agent=build_repeat_agent(),
        primitive_client=primitive_client,
        threads=threads,
        ledger=ledger,
        debug_public=debug_public,
    )
    app.state.handles = handles
    # Backwards-compat alias for tests still poking at app.state.primitive_client
    app.state.primitive_client = primitive_client

    try:
        yield
    finally:
        log.info("agent_service_stopping")
        await primitive_client.close()
        await ledger.close()


app = FastAPI(title="multichain agent-service", version="0.2.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _proto_to_camel_json(msg) -> str:
    """Serialize a proto message to canonical JSON with camelCase field
    names, no indentation, no whitespace."""
    return json_format.MessageToJson(
        msg, preserving_proto_field_name=False, indent=None
    )


def _validate_request(req: sess_pb.AgentRequest) -> None:
    """Synchronous validation of the request shape so misconfigured
    clients see a 400, not a delayed SSE error frame.

    Phase II requires `context.focus` to be set so the loop has a
    `focus_addr` for the system prompt. Selection-only requests fail
    early with a clear message."""
    if not req.user_question.strip():
        raise HTTPException(status_code=400, detail="user_question must not be empty")
    if not req.HasField("context"):
        raise HTTPException(status_code=400, detail="context is required")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/agent/ask")
async def ask(request: Request) -> Response:
    """Stash the request under a fresh session_id; the SSE handler
    picks it up and runs the loop driver. Inbound is proto canonical
    JSON `AgentRequest`; outbound is proto canonical JSON
    `AgentSessionStarted`."""
    raw = await request.body()
    req = sess_pb.AgentRequest()
    try:
        json_format.Parse(raw, req, ignore_unknown_fields=True)
    except json_format.ParseError as e:
        raise HTTPException(status_code=400, detail=f"invalid AgentRequest: {e}") from None

    _validate_request(req)

    session_id = secrets.token_urlsafe(16)
    thread_id = req.thread_id or str(uuid.uuid4())

    # Look up the prospective thread to surface the per-turn count back
    # in the AgentSessionStarted response. ThreadRegistry doesn't yet
    # have an entry; we reflect the caller's view (turn count is the
    # turn this request will run as).
    thread = request.app.state.handles.threads.get(thread_id)
    turn = thread.turn_count if thread is not None else 0

    _PENDING[session_id] = PendingSession(
        request=req,
        thread_id=thread_id,
        session_started_at_ms=int(time.time() * 1000),
    )
    log.info(
        "agent_ask",
        session_id=session_id,
        thread_id=thread_id,
        turn=turn,
        focus_kind=req.context.focus.WhichOneof("entity") if req.context.HasField("focus") else None,
    )
    started = sess_pb.AgentSessionStarted(
        session_id=session_id, thread_id=thread_id, turn=turn
    )
    return Response(
        content=_proto_to_camel_json(started),
        media_type="application/json",
    )


@app.get("/agent/stream/{session_id}")
async def stream(session_id: str, request: Request) -> EventSourceResponse:
    """SSE stream that runs one turn through the loop driver and emits
    every frame variant."""
    pending = _PENDING.pop(session_id, None)
    if pending is None:
        raise HTTPException(status_code=404, detail="session not found or already consumed")

    handles: LoopHandles = request.app.state.handles

    async def event_iter():
        try:
            async for frame in run_turn(
                handles=handles,
                request=pending.request,
                session_id=session_id,
                thread_id=pending.thread_id,
                session_started_at_ms=pending.session_started_at_ms,
            ):
                yield frame
        except asyncio.CancelledError:
            log.info("agent_stream_cancelled", session_id=session_id)
            raise

    # Critical SSE headers: nginx / cloudflared / browsers won't buffer
    # if these are set. Plan risk #1.
    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return EventSourceResponse(event_iter(), headers=headers)
