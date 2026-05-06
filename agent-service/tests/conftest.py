"""Shared pytest fixtures for the agent-service test suite.

Hard rule: NO LIVE LLM CALLS in any test that imports from here. The
total runtime budget for the baseline test set is <5s; a real
OpenRouter call would blow past that and fail loudly.

Two boundaries we mock:

1. Rust data plane (`http://api:8002`): mocked via `pytest-httpx`.
   Tests register canned responses for `/turn/begin`, `/turn/end`,
   `/primitive/wallet_profile`, `/primitive/community_summary`.

2. LLM provider (OpenRouter via Pydantic AI): mocked via Pydantic
   AI's built-in `TestModel`, which replays canned tool calls and
   final outputs without ever opening a network socket.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from pydantic_ai import Agent

# Set a dummy AGENT_API_KEY + the model-id env vars that
# `llm.py` reads strict-mode. `setdefault` so a real .env (loaded
# via `set dotenv-load := true` in the justfile for integration
# scenarios) wins. Tests that need to assert on specific env
# values can still monkeypatch.setenv(...) inside the test.
os.environ.setdefault("AGENT_API_KEY", "test-dummy-key")
os.environ.setdefault("AGENT_PRIMARY_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
os.environ.setdefault("AGENT_POLICY_MODEL", "openai/gpt-oss-20b:free")
os.environ.setdefault("EVAL_JUDGE_MODEL", "openrouter/owl-alpha")

# Disable OTel SDK before any agent_service module imports. The lifespan
# handler calls init_otel(), which would otherwise spin up a real
# TracerProvider + BatchSpanProcessor pointing at the in-compose
# `otel-collector` hostname; in unit tests that's an unresolvable name,
# producing 5+ seconds of retry-and-backoff log noise per test that
# uses the FastAPI TestClient. The disabled-mode short-circuits to a
# no-op tracer so wiring stays exercised but no network is touched.
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

from agent_service.main import app  # noqa: E402
from agent_service.primitive_client import PrimitiveClient  # noqa: E402

from tests.fixtures import primitive_responses as canned  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DATA_PLANE_BASE = "http://api:8002"


# ---------------------------------------------------------------------------
# pytest-httpx mock surface
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_data_plane(httpx_mock):
    """Convenience wrapper around the `httpx_mock` fixture from
    pytest-httpx. Pre-registers no responses; tests opt into specific
    routes via the `add_response` helpers below.

    Use directly when you need full control over the mock behavior;
    use `with_happy_path_primitives` for the default canned set.
    """
    return httpx_mock


@pytest.fixture
def with_happy_path_primitives(mock_data_plane):
    """Pre-register the full happy-path response set: a snapshot
    lease, one wallet_profile, one community_summary, and a turn end.
    Tests that only need the success flow grab this fixture and
    forget about HTTP plumbing.

    Mark each route as reusable so multiple identical calls in one
    turn don't blow up the mock.
    """
    # `is_optional=True` so tests that only exercise begin/end (and
    # don't touch the primitive routes) don't fail the
    # "mocked-but-not-requested" assertion.
    proto_ct = {"Content-Type": "application/x-protobuf"}
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/turn/begin",
        content=canned.encode_snapshot_begin_response(),
        headers=proto_ct,
        is_reusable=True,
        is_optional=True,
    )
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/turn/end",
        status_code=204,
        is_reusable=True,
        is_optional=True,
    )
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/primitive/wallet_profile",
        content=canned.encode_wallet_profile_response(),
        headers=proto_ct,
        is_reusable=True,
        is_optional=True,
    )
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/primitive/community_summary",
        content=canned.encode_community_summary_response(),
        headers=proto_ct,
        is_reusable=True,
        is_optional=True,
    )
    return mock_data_plane


# ---------------------------------------------------------------------------
# PrimitiveClient (real client + mocked transport)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def primitive_client(mock_data_plane) -> AsyncIterator[PrimitiveClient]:
    """Real `PrimitiveClient` instance pointed at the mock data plane.
    Tests can call `await client.wallet_profile(...)` without mocking
    the client class itself, so the client's own logic (envelope
    parsing, error mapping) is under test."""
    client = PrimitiveClient(base_url=DATA_PLANE_BASE)
    try:
        yield client
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# FastAPI TestClient
# ---------------------------------------------------------------------------


@pytest.fixture
def test_app(mock_data_plane) -> Iterator[TestClient]:
    """FastAPI TestClient against the real `agent_service.main:app`.

    The app's lifespan handler creates a `PrimitiveClient` pointing
    at `DATA_PLANE_URL` (default `http://api:8002`). We override
    that env var to match what `mock_data_plane` intercepts.

    The lifespan also constructs the Pydantic AI agent. We do NOT
    override the agent here at the app level; tests that need to
    intercept LLM calls use the `agent_with_test_model` fixture and
    monkey-patch `app.state.agent` for the test's duration.
    """
    os.environ["DATA_PLANE_URL"] = DATA_PLANE_BASE
    with TestClient(app) as client:
        yield client


# ---------------------------------------------------------------------------
# Pydantic AI TestModel (LLM substitute)
# ---------------------------------------------------------------------------


@pytest.fixture
def test_model_factory():
    """Factory for `pydantic_ai.models.test.TestModel` instances.
    Returns a callable so each test can configure its own canned
    response set without worrying about state leak between tests.

    Returned model never opens a network socket. If the agent under
    test attempts to reach OpenRouter, that's a wiring bug the test
    surfaces immediately.
    """
    from pydantic_ai.models.test import TestModel

    def make(**kwargs):
        # Allow override of any TestModel constructor kwarg per call
        # site; common ones: `call_tools`, `custom_output_args`,
        # `seed`.
        return TestModel(**kwargs)

    return make


@pytest.fixture
def agent_with_test_model(test_model_factory):
    """Build the project agent with a TestModel substitute. Tests can
    configure tool-call behavior by passing `**kwargs` to the
    factory before this fixture is invoked, but the simplest pattern
    is to use `Agent.override(model=...)` inside the test body so
    each test sees a fresh model.
    """
    from agent_service.agent import build_agent

    return build_agent()


# ---------------------------------------------------------------------------
# Bare httpx fixture (for tests that build their own client)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def httpx_async_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client
