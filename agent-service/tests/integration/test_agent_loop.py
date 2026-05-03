"""Tests for the Pydantic AI agent's tool wiring + output validation,
using `TestModel` so no real LLM call is made.

Verifies the entire agent stack (Phase 0/A walking-skeleton scope):
- Tools are registered on the agent (TestModel auto-calls every tool
  it can see)
- Tool deps inject correctly (PrimitiveClient + snapshot_id reach
  the tool body)
- Tool calls reach the mocked Rust data plane via the real
  PrimitiveClient
- Output is a string (Phase I dropped the stub Claim output_type;
  Phase II will reintroduce structured emission via the `emit_claim`
  tool, see issue #14)
"""

from __future__ import annotations

from pydantic_ai.models.test import TestModel

from agent_service.agent import AgentDeps, build_agent
from agent_service.primitive_client import PrimitiveClient

from tests.conftest import DATA_PLANE_BASE
from tests.fixtures import primitive_responses as canned


async def test_agent_dispatches_wallet_profile_tool(
    primitive_client: PrimitiveClient, with_happy_path_primitives
):
    """TestModel by default calls every registered tool once. We
    verify the wallet_profile tool was dispatched, the canned mock
    response came back through PrimitiveClient, and the agent
    produced a string output (walking-skeleton contract; Phase II
    grows this into structured Claim emission)."""
    agent = build_agent()
    test_model = TestModel(call_tools=["wallet_profile"])

    deps = AgentDeps(
        primitive_client=primitive_client,
        snapshot_id=canned.VALID_SNAPSHOT_ID,
        focus_addr=canned.WALLET_PROFILE_ADDR,
    )

    with agent.override(model=test_model):
        result = await agent.run(
            f"Profile {canned.WALLET_PROFILE_ADDR}",
            deps=deps,
        )

    # Output is a free-form narrative string in Phase 0/A.
    assert isinstance(result.output, str)
    assert result.output  # non-empty

    # The mocked Rust route was hit.
    primitive_calls = [
        r for r in with_happy_path_primitives.get_requests()
        if r.url.path == "/primitive/wallet_profile"
    ]
    assert len(primitive_calls) >= 1

    # And the binary-protobuf body contained the leased snapshot_id.
    from multichain.wire.shared.v1 import primitive_envelope_pb2 as env_pb

    decoded = env_pb.WalletProfileRequest()
    decoded.ParseFromString(primitive_calls[0].read())
    assert decoded.snapshot_id == canned.VALID_SNAPSHOT_ID
    # TestModel auto-generates a synthetic addr; we just verify the
    # field round-trips populated and the time_scope.live oneof is set.
    assert decoded.input.addr  # non-empty
    assert decoded.input.time_scope.HasField("live")


async def test_agent_dispatches_community_summary_tool(
    primitive_client: PrimitiveClient, with_happy_path_primitives
):
    agent = build_agent()
    test_model = TestModel(call_tools=["community_summary"])

    deps = AgentDeps(
        primitive_client=primitive_client,
        snapshot_id=canned.VALID_SNAPSHOT_ID,
        focus_addr="X",
    )

    with agent.override(model=test_model):
        await agent.run("Summarise community 8", deps=deps)

    cs_calls = [
        r for r in with_happy_path_primitives.get_requests()
        if r.url.path == "/primitive/community_summary"
    ]
    assert len(cs_calls) >= 1


async def test_agent_handles_primitive_error_gracefully(
    primitive_client: PrimitiveClient, mock_data_plane
):
    """When PrimitiveClient raises (Rust returned 404 / 410 / 5xx),
    the tool body catches it and returns a structured error dict
    instead of letting the agent crash. Verifies the
    `try/except PrimitiveError` block in `agent.py`."""
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/primitive/wallet_profile",
        status_code=410,
        json=canned.SNAPSHOT_GONE_ERROR,
    )

    agent = build_agent()
    test_model = TestModel(call_tools=["wallet_profile"])

    deps = AgentDeps(
        primitive_client=primitive_client,
        snapshot_id="stale",
        focus_addr="X",
    )

    # Should NOT raise; tool should swallow and return error dict.
    with agent.override(model=test_model):
        result = await agent.run("q", deps=deps)

    # The agent still produced a string narrative (TestModel default).
    assert isinstance(result.output, str)


async def test_agent_no_real_network_call(
    primitive_client: PrimitiveClient, with_happy_path_primitives
):
    """Hard belt-and-suspenders check: if a real OpenRouter call
    leaked into a TestModel-wrapped run, pytest-httpx would record
    it (because PrimitiveClient is the only legitimate httpx user
    in this test). Assert that only Rust data-plane URLs were
    contacted."""
    agent = build_agent()
    test_model = TestModel(call_tools=["wallet_profile"])
    deps = AgentDeps(
        primitive_client=primitive_client,
        snapshot_id=canned.VALID_SNAPSHOT_ID,
        focus_addr="X",
    )

    with agent.override(model=test_model):
        await agent.run("q", deps=deps)

    for req in with_happy_path_primitives.get_requests():
        assert str(req.url).startswith(DATA_PLANE_BASE), (
            f"unexpected outbound HTTP to {req.url}"
        )
