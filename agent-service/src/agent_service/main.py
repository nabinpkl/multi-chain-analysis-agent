"""FastAPI entrypoint for the Phase II agent plane.

Routes:
- `GET  /health` -> `{"status": "ok"}`
- `POST /agent/turn` -> `text/event-stream` of the 9 SSE frame
  variants (Claim, NarrativeWithRefs, NarrativeRetracted, Progress,
  Error, Done, NoMovement, ChangedSince, GatePath). Returns 404 when
  the request carries a `thread_id` that no longer exists (stale
  localStorage on the client; frontend retries without).

Single streaming POST; the request body is parsed, the loop driver
runs, and its events stream back as the response body. Frontend
wiring is one env-var (`NEXT_PUBLIC_AGENT_URL=http://localhost:8003`).

Wire format per AGENTS.md "Wire format per hop": browser hops carry
proto canonical JSON. Inbound `AgentRequest` parses via
`json_format.Parse`; outbound SSE event `data:` payloads serialize via
`json_format.MessageToJson(preserving_proto_field_name=False)` for
camelCase field names.

Loop orchestration lives in `loop_driver.run_turn`; this module
handles HTTP wiring + lifespan setup of the long-lived clients
(Pydantic AI agents, primitive client, thread registry). The
prior bespoke `agent_ledger` table was deleted in Ship 1 of the
agent-observability foundation (ADR 13); OTel spans are now the
single source of truth, fanned out by the otel-collector to CH-A's
`otel.otel_traces` and to Langfuse.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from google.protobuf import json_format
from sse_starlette.sse import EventSourceResponse

from multichain.wire.agent.v1 import session_pb2 as sess_pb
from multichain.wire.agent.v1 import sse_pb2

from agent_service.agent import build_agent
from agent_service.codex_driver import run_turn_codex
from agent_service.codex_profile import build_codex_driver, build_codex_profile
from agent_service.loop_driver import LoopHandles, run_turn
from agent_service.otel import init_otel, instrument_fastapi
from agent_service.policy.constitution import build_constitution_agent
from agent_service.primitive_client import PrimitiveClient
from agent_service.repeat_detector import build_repeat_agent
from agent_service.thread_state import RuntimeMismatchError, ThreadRegistry

log = structlog.get_logger(__name__)


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
    # ThreadRegistry persists `<thread_root>/threads/<thread_id>/state.json`
    # on every turn end. `THREAD_ROOT` defaults to a local `.cache`
    # directory so the service runs fine outside docker; in docker the
    # compose file bind-mounts a real volume into `/var/threads`.
    thread_root = Path(os.environ.get("THREAD_ROOT", "./.cache/threads"))
    threads = ThreadRegistry(thread_root=thread_root)

    # Chunk 3 codex runtime. Build the static profile + driver once
    # so per-turn calls hit the cached session pool. The codex CLI
    # has to be on PATH (the docker image bakes it; local-dev paths
    # need a global `codex` install). When unavailable, we log and
    # leave `codex_driver=None`; the POST handler 503s codex
    # requests rather than silently falling back to pydantic-ai.
    codex_driver = None
    try:
        codex_profile = build_codex_profile(
            data_plane_url=base_url, cwd=Path.cwd()
        )
        codex_driver = build_codex_driver(
            profile=codex_profile,
            codex_home_root=thread_root / "codex_homes",
        )
        log.info("codex_runtime_ready")
    except Exception:  # noqa: BLE001
        log.exception("codex_runtime_init_failed")

    handles = LoopHandles(
        primary_agent=build_agent(),
        constitution_agent=build_constitution_agent(),
        repeat_agent=build_repeat_agent(),
        primitive_client=primitive_client,
        threads=threads,
        debug_public=debug_public,
        codex_driver=codex_driver,
    )
    app.state.handles = handles
    # Backwards-compat alias for tests still poking at app.state.primitive_client
    app.state.primitive_client = primitive_client

    try:
        yield
    finally:
        log.info("agent_service_stopping")
        await primitive_client.close()
        # CodexAppServerDriver owns a session pool of long-lived
        # codex subprocesses; close it on shutdown so the subprocess
        # exits cleanly and any per-thread sqlite is flushed.
        if codex_driver is not None:
            try:
                codex_driver.close()
            except Exception:  # noqa: BLE001
                log.exception("codex_driver_close_failed")


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


@app.post("/agent/turn")
async def agent_turn(request: Request) -> EventSourceResponse:
    """Single streaming POST. The request body is a proto canonical
    JSON `AgentRequest`; the response body is `text/event-stream`
    carrying every SSE frame variant the loop driver emits.

    Thread lifecycle:
      * `thread_id` absent  mint a fresh UUID, run as turn 0.
      * `thread_id` present + thread exists (memory or disk)  run as
        the next turn in that conversation.
      * `thread_id` present + thread does NOT exist  HTTP 404. The
        frontend recovers by clearing its localStorage entry and
        retrying without `thread_id`.

    No per-POST handoff token (the prior `session_id` / `_PENDING`
    pattern). The persistent identity is the conversation, which is
    `thread_id`; per-turn correlation is `f"{thread_id}:{turn}"` and
    lives inside the trace, not on the wire.
    """
    raw = await request.body()
    req = sess_pb.AgentRequest()
    try:
        json_format.Parse(raw, req, ignore_unknown_fields=True)
    except json_format.ParseError as e:
        raise HTTPException(
            status_code=400, detail=f"invalid AgentRequest: {e}"
        ) from None

    _validate_request(req)

    handles: LoopHandles = request.app.state.handles

    # Validate thread_id: present + unknown -> 404 so the client can
    # transparently retry without it (the frontend handles 404 by
    # clearing localStorage and retrying with thread_id absent).
    if req.thread_id and not handles.threads.exists(req.thread_id):
        raise HTTPException(status_code=404, detail="thread_id not found")

    # Resolve runtime. UNSPECIFIED on the wire falls back to
    # PYDANTIC_AI server-side (chunk 3 backward-compat). On resume,
    # the request's runtime must agree with the persisted lock from
    # thread creation; mismatch is a 400 so the client can show
    # "start a new chat to switch runtime" without a silent runtime
    # swap.
    requested_runtime = (
        req.runtime
        if req.runtime != sess_pb.AGENT_RUNTIME_UNSPECIFIED
        else sess_pb.AGENT_RUNTIME_PYDANTIC_AI
    )
    if req.thread_id:
        stored_runtime = handles.threads.runtime_for(req.thread_id)
        if stored_runtime is not None and stored_runtime != requested_runtime:
            raise HTTPException(
                status_code=400,
                detail=(
                    "thread runtime is "
                    f"{sess_pb.AgentRuntime.Name(stored_runtime)}; "
                    f"request asked for "
                    f"{sess_pb.AgentRuntime.Name(requested_runtime)}. "
                    "Start a new chat to switch runtime."
                ),
            )
        # Trust the stored value over the wire field on resume; the
        # client may have lost its localStorage selection mid-session.
        if stored_runtime is not None:
            requested_runtime = stored_runtime
    if requested_runtime == sess_pb.AGENT_RUNTIME_CODEX and handles.codex_driver is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "codex runtime is not available on this server "
                "(codex CLI missing or driver init failed)"
            ),
        )

    thread_id = req.thread_id or str(uuid.uuid4())
    turn_started_at_ms = int(time.time() * 1000)

    log.info(
        "agent_turn_start",
        thread_id=thread_id,
        focus_kind=req.context.focus.WhichOneof("entity")
        if req.context.HasField("focus")
        else None,
    )

    # Pick the per-runtime driver. Both have the same async generator
    # contract; the dispatch is one branch.
    driver_fn = (
        run_turn_codex
        if requested_runtime == sess_pb.AGENT_RUNTIME_CODEX
        else run_turn
    )

    async def event_iter():
        try:
            async for frame in driver_fn(
                handles=handles,
                request=req,
                thread_id=thread_id,
                turn_started_at_ms=turn_started_at_ms,
            ):
                yield frame
        except RuntimeMismatchError as e:
            # Race: between the runtime_for peek above and entering
            # get_or_create inside the driver, a concurrent turn
            # locked the thread to a different runtime. Surface a
            # clear error frame so the SSE consumer doesn't hang.
            log.warning("runtime_mismatch_during_dispatch", error=str(e))
            yield {
                "event": "Error",
                "data": json_format.MessageToJson(
                    sse_pb2.Error(message=str(e)),
                    preserving_proto_field_name=False,
                    indent=None,
                ),
            }
        except asyncio.CancelledError:
            log.info("agent_stream_cancelled", thread_id=thread_id)
            raise

    # Critical SSE headers: nginx / cloudflared / browsers won't buffer
    # if these are set. `X-Mca-Thread-Id` lets the frontend learn the
    # server-minted thread_id when the client posted with no id, so
    # it can persist to localStorage without parsing a synthetic
    # bootstrap event off the stream.
    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
        "X-Mca-Thread-Id": thread_id,
    }
    return EventSourceResponse(event_iter(), headers=headers)
