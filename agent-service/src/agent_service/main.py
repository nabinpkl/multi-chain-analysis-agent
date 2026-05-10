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
clients (Pydantic AI agents, primitive client, thread registry). The
prior bespoke `agent_ledger` table was deleted in Ship 1 of the
agent-observability foundation (ADR 13); OTel spans are now the
single source of truth, fanned out by the otel-collector to CH-A's
`otel.otel_traces` and to Langfuse.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from google.protobuf import json_format
from sse_starlette.sse import EventSourceResponse

from multichain.wire.agent.v1 import session_pb2 as sess_pb

from agent_service.agent import build_agent
from agent_service.loop_driver import LoopHandles, run_turn
from agent_service.otel import init_otel, instrument_fastapi
from agent_service.policy.constitution import build_constitution_agent
from agent_service.primitive_client import PrimitiveClient
from agent_service.repeat_detector import build_repeat_agent
from agent_service.thread_state import ThreadRegistry

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
    base_url = os.environ.get("DATA_PLANE_URL", "http://api:8004")
    debug_public = os.environ.get("AGENT_DEBUG_PUBLIC", "0") == "1"
    log.info("agent_service_starting", data_plane=base_url, debug_public=debug_public)

    # Ship 1 of agent-observability foundation (ADR 13). Bring up OTel
    # before agents are built so Agent.instrument_all() is in place
    # when build_agent / build_constitution_agent / build_repeat_agent
    # construct their Pydantic AI Agent instances.
    init_otel("multichain-agent")

    primitive_client = PrimitiveClient(base_url=base_url)
    threads = ThreadRegistry()

    handles = LoopHandles(
        primary_agent=build_agent(),
        constitution_agent=build_constitution_agent(),
        repeat_agent=build_repeat_agent(),
        primitive_client=primitive_client,
        threads=threads,
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


app = FastAPI(title="multichain agent-service", version="0.2.0", lifespan=lifespan)

# Wrap every HTTP request in an OTel server span. The agent-stream
# handler then nests Pydantic AI agent.run + our domain spans under
# it, giving one trace per browser request. /health is excluded so
# liveness probes don't flood the collector.
instrument_fastapi(app)

# CORS for browser hops. Frontend (Next dev or Vercel) is a different
# origin from this service. Mirrors the Rust `CORS_ORIGIN` env-var
# convention (see backend/src/config.rs + backend/src/main.rs):
# `*` (default) -> permissive; otherwise an exact origin string. The
# preflight OPTIONS requests the browser sends before POST /agent/ask
# (Content-Type: application/json triggers preflight) hit this layer
# and respond 200 with the right Access-Control-* headers, so the
# browser allows the actual fetch through.
_cors_origin = os.environ.get("CORS_ORIGIN", "*")
if _cors_origin == "*":
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=".*",
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[_cors_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


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


@app.get("/agent/local/models")
async def local_models() -> dict:
    """Proxy `GET ${LOCAL_LLM_BASE_URL}/models` so the frontend
    builder view can populate its local-model dropdown without
    hitting the user's host directly (CORS would block the browser
    even if the URL were reachable).

    Defaults LOCAL_LLM_BASE_URL to `http://host.docker.internal:1234/v1`
    (LM Studio's default port). On Mac/Windows Docker Desktop this
    name resolves automatically; on Linux the docker-compose
    `extra_hosts: ["host.docker.internal:host-gateway"]` line on
    this service supplies the missing entry.

    Returns one canonical shape regardless of failure mode:
      `{"reachable": bool, "baseUrl": "...", "models": [{id, object}, ...]}`
    so the frontend renders one error state for "LM Studio not
    running, no permitted models, and any other connection failure."
    """
    base_url = os.environ.get(
        "LOCAL_LLM_BASE_URL", "http://host.docker.internal:1234/v1"
    )
    url = base_url.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(url)
        if r.status_code != 200:
            return {"reachable": False, "baseUrl": base_url, "models": []}
        body = r.json()
        # OpenAI-compatible /models response: {"object": "list", "data": [...]}.
        # LM Studio + most others follow this. We forward the `data` array
        # under `models` and drop everything else.
        models = body.get("data", []) if isinstance(body, dict) else []
        return {"reachable": True, "baseUrl": base_url, "models": models}
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError):
        return {"reachable": False, "baseUrl": base_url, "models": []}


@app.get("/agent/config/role-defaults")
async def role_defaults() -> dict[str, str]:
    """Expose the per-role env-driven model ids (`AGENT_PRIMARY_MODEL`,
    `AGENT_POLICY_MODEL`, `EVAL_JUDGE_MODEL`) so the builder view can
    render the actual id in its "use the default" dropdown option
    instead of the abstract phrase "env default." Builders need to
    see *which* model the production preset would pick (currently
    e.g. `nvidia/nemotron-3-super-120b-a12b:free` for primary, the
    timeout offender we identified) without ssh'ing into the
    container or grep'ing `.env`. Empty string when an env var is
    unset, mirroring the wire contract for `RoleOverride`.
    """
    return {
        "primary": os.environ.get("AGENT_PRIMARY_MODEL", ""),
        "policy": os.environ.get("AGENT_POLICY_MODEL", ""),
        "judge": os.environ.get("EVAL_JUDGE_MODEL", ""),
    }


@app.get("/agent/gemini/models")
async def gemini_models() -> dict:
    """Proxy `GET https://generativelanguage.googleapis.com/v1beta/openai/models`
    so the builder view can populate its Gemini/Gemma dropdown.

    Why this exists: OpenRouter free tier became unusable on the
    primary role (queue-depth stalls hitting the 75s/attempt cap on
    `nvidia/nemotron-3-super-120b-a12b:free`). Google's Generative
    Language API exposes Gemma open-weights models directly with no
    queue-depth issue and tool-calling support. The proxy keeps
    every "list models" path on one canonical shape.

    Auth required: `GEMINI_API_KEY` env var. We DO forward this key
    server-side (unlike `/agent/openrouter/models` where the
    listing is public). When the key is missing we return one
    canonical "unreachable" shape rather than 500'ing so the
    builder view can render a clear empty state.

    The returned model ids are normalized: Google's `/models`
    response uses `models/<id>` while the chat-completions endpoint
    accepts the bare `<id>`. We strip the prefix here so the
    dropdown value matches what `make_model` expects to send.

    Returns: `{"reachable": bool, "models": [{id, name}]}`.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return {"reachable": False, "models": []}
    url = "https://generativelanguage.googleapis.com/v1beta/openai/models"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
        if r.status_code != 200:
            return {"reachable": False, "models": []}
        body = r.json()
        raw = body.get("data", []) if isinstance(body, dict) else []
        out = []
        for m in raw:
            if not isinstance(m, dict):
                continue
            mid = m.get("id", "")
            if not isinstance(mid, str) or not mid:
                continue
            # Google returns "models/gemma-4-31b-it"; the OpenAI-compat
            # chat completions endpoint accepts the bare id, so strip
            # to match what `make_model` will send on the wire.
            bare = mid[len("models/") :] if mid.startswith("models/") else mid
            out.append({"id": bare, "name": m.get("name", bare)})
        out.sort(key=lambda m: m["id"])
        return {"reachable": True, "models": out}
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError):
        return {"reachable": False, "models": []}


@app.get("/agent/openrouter/models")
async def openrouter_models() -> dict:
    """Proxy `GET https://openrouter.ai/api/v1/models`, filtered to
    free-tier ids (suffix `:free`), so the builder view can populate
    its OpenRouter dropdown.

    Why server-side instead of fetching from the browser: keeps every
    "list models" path on one canonical shape (matches
    `/agent/local/models`), and OpenRouter occasionally trickles
    request budgets across origins; routing through the agent service
    means one server-side IP not many browser ones.

    `:free` filter is the user-asked semantic. The pricing object
    (`pricing.prompt == "0"` etc.) would also work, but suffix is
    what OpenRouter publishes as the durable identifier and is what
    we want pinned in the user's localStorage.

    Auth: OpenRouter `/models` is publicly readable; no API key
    required. We intentionally do NOT forward `AGENT_API_KEY` here:
    the listing should work even before the env is fully configured
    (e.g. on first project clone).

    Returns one canonical shape regardless of failure:
      `{"reachable": bool, "models": [{id, name, contextLength, ...}]}`
    """
    url = "https://openrouter.ai/api/v1/models"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
        if r.status_code != 200:
            return {"reachable": False, "models": []}
        body = r.json()
        raw = body.get("data", []) if isinstance(body, dict) else []
        free = [
            {
                "id": m.get("id", ""),
                "name": m.get("name", ""),
                "contextLength": m.get("context_length"),
            }
            for m in raw
            if isinstance(m, dict) and isinstance(m.get("id"), str)
            and m["id"].endswith(":free")
        ]
        # Stable sort by id so the dropdown order is deterministic.
        free.sort(key=lambda m: m["id"])
        return {"reachable": True, "models": free}
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError):
        return {"reachable": False, "models": []}


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
