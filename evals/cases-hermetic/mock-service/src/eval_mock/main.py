"""Mock-substrate entry point. Single FastAPI app on port 8005
serving:

  - `/turn/{begin,end}`, `/turn/{snapshot_id}/claims`,
    `/primitive/*` (HTTP shim in `http_shim.py`).
  - `/eval/setup` POST/DELETE (runner control surface in
    `setup_routes.py`).
  - `/mcp` (FastMCP-backed Streamable HTTP transport in
    `mcp_proxy.py`; tool schemas loaded from `schemas.json`).
  - `/health` liveness.

Pydantic-ai's `PrimitiveClient` and codex's `CodexHttpMcpServer`
both point at this URL in hermetic mode via the
`agent-service-eval` docker service's `DATA_PLANE_URL=http://eval-mock:8005`
env var.

Lifespan: the MCP transport needs `session_manager.run()` active for
the duration of the app or every `/mcp` POST returns 503. We thread
that into FastAPI's lifespan via an AsyncExitStack so adding more
context-managed substrate pieces later (e.g. a background fixture
watcher) is one `stack.enter_async_context(...)` line.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI

from eval_mock.http_shim import router as http_router
from eval_mock.mcp_proxy import get_app as get_mcp_app
from eval_mock.mcp_proxy import get_session_manager
from eval_mock.setup_routes import router as setup_router


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(get_session_manager().run())
        yield


app = FastAPI(
    title="eval-mock",
    version="0.1.0",
    description=(
        "Hermetic-eval mock substrate. Serves the Rust data plane's "
        "HTTP surface and `/mcp` MCP transport, backed by a fixture "
        "store the eval runner controls."
    ),
    lifespan=lifespan,
)

app.include_router(http_router)
app.include_router(setup_router)
app.mount("/mcp", get_mcp_app())


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
