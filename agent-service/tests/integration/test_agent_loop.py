"""Tests for the Pydantic AI agent's tool wiring + output validation,
using `TestModel` so no real LLM call is made.

Phase II: AgentDeps grew to carry session_id, session_started_at_ms,
and the thread's binding_store so emit_claim + structural verify can
read them. Tools record into binding_store via build_binding so a
follow-up structural pass can verify cited values trace.
"""

from __future__ import annotations

from pydantic_ai.models.test import TestModel

from agent_service.agent import AgentDeps, build_agent
from agent_service.policy.binding_store import PrimitiveBindingStore
from agent_service.primitive_client import PrimitiveClient

from tests.conftest import DATA_PLANE_BASE
from tests.fixtures import primitive_responses as canned


def _make_deps(client: PrimitiveClient, snapshot_id: str = canned.VALID_SNAPSHOT_ID) -> AgentDeps:
    """Build a fresh AgentDeps with an empty binding store. Tests want
    isolation between runs so accumulation doesn't bleed across cases."""
    return AgentDeps(
        primitive_client=client,
        snapshot_id=snapshot_id,
        session_id="test-session",
        session_started_at_ms=0,
        binding_store=PrimitiveBindingStore(),
    )


async def test_agent_dispatches_wallet_profile_tool(
    primitive_client: PrimitiveClient, with_happy_path_primitives
):
    """TestModel by default calls every registered tool once. We verify
    wallet_profile was dispatched, the canned mock response came back
    through PrimitiveClient, the binding store recorded the call, and
    the agent produced a string output (Phase II narrative channel)."""
    agent = build_agent()
    test_model = TestModel(call_tools=["wallet_profile"])
    deps = _make_deps(primitive_client)

    with agent.override(model=test_model):
        result = await agent.run(
            f"Profile {canned.WALLET_PROFILE_ADDR}",
            deps=deps,
        )

    assert isinstance(result.output, str)
    assert result.output  # non-empty

    primitive_calls = [
        r for r in with_happy_path_primitives.get_requests()
        if r.url.path == "/primitive/wallet_profile"
    ]
    assert len(primitive_calls) >= 1

    # Binding store recorded the call so the structural gate can verify.
    assert len(deps.binding_store) == 1
    # Per-turn replay record captured for ship 4.
    assert any(r.primitive_name == "wallet_profile" for r in deps.tool_call_records)

    from multichain.wire.shared.v1 import primitive_envelope_pb2 as env_pb

    decoded = env_pb.WalletProfileRequest()
    decoded.ParseFromString(primitive_calls[0].read())
    assert decoded.snapshot_id == canned.VALID_SNAPSHOT_ID
    assert decoded.input.addr  # non-empty
    assert decoded.input.time_scope.HasField("live")


async def test_agent_dispatches_community_summary_tool(
    primitive_client: PrimitiveClient, with_happy_path_primitives
):
    agent = build_agent()
    test_model = TestModel(call_tools=["community_summary"])
    deps = _make_deps(primitive_client)

    with agent.override(model=test_model):
        await agent.run("Summarise community 8", deps=deps)

    cs_calls = [
        r for r in with_happy_path_primitives.get_requests()
        if r.url.path == "/primitive/community_summary"
    ]
    assert len(cs_calls) >= 1
    assert len(deps.binding_store) == 1


async def test_agent_handles_primitive_error_gracefully(
    primitive_client: PrimitiveClient, mock_data_plane
):
    """When PrimitiveClient raises (Rust returned 404 / 410 / 5xx), the
    tool body catches it and returns a wrapped <external_data> string
    so the agent's run doesn't crash."""
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/primitive/wallet_profile",
        status_code=410,
        json=canned.SNAPSHOT_GONE_ERROR,
    )

    agent = build_agent()
    test_model = TestModel(call_tools=["wallet_profile"])
    deps = _make_deps(primitive_client, snapshot_id="stale")

    with agent.override(model=test_model):
        result = await agent.run("q", deps=deps)

    # Agent still produced a string narrative (TestModel default).
    assert isinstance(result.output, str)
    # Binding store stayed empty (the error path skips recording).
    assert len(deps.binding_store) == 0


async def test_agent_emit_claim_tool_buffers_into_deps(
    primitive_client: PrimitiveClient, with_happy_path_primitives
):
    """emit_claim is the structured-output channel. The tool buffers
    drafts into deps.emitted_claims; the loop driver stamps + gates
    them after agent.run() returns. Verify the buffer accumulates."""
    agent = build_agent()
    # Use TestModel's custom_output_args to feed structured tool args.
    # call_tools=["emit_claim"] makes TestModel synthesize an emit_claim
    # call with a default-constructed EmitClaimInput shape.
    test_model = TestModel(call_tools=["emit_claim"])
    deps = _make_deps(primitive_client)

    with agent.override(model=test_model):
        await agent.run("emit a profile claim", deps=deps)

    # TestModel's auto-call should have queued at least one draft.
    assert len(deps.emitted_claims) >= 1


async def test_get_token_info_passes_through_text_when_switch_on(
    primitive_client: PrimitiveClient, mock_data_plane
):
    """Production preset (`external_text_input_enabled=True`) leaves the
    name/symbol/uri intact in the <external_data> wrapper the model sees."""
    proto_ct = {"Content-Type": "application/x-protobuf"}
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/primitive/get_token_info",
        content=canned.encode_get_token_info_response(),
        headers=proto_ct,
        is_reusable=True,
    )

    agent = build_agent()
    test_model = TestModel(call_tools=["get_token_info"])
    deps = _make_deps(primitive_client)  # default external_text_input_enabled=True

    with agent.override(model=test_model):
        await agent.run("token info", deps=deps)

    record = next(
        r for r in deps.tool_call_records if r.primitive_name == "get_token_info"
    )
    assert record.output_value["name"] == "USD Coin"
    assert record.output_value["symbol"] == "USDC"


async def test_get_token_info_redacts_text_when_switch_off(
    primitive_client: PrimitiveClient, mock_data_plane
):
    """When `external_text_input_enabled` is False the tool returns a
    wrapped block whose name/symbol fields are the redaction
    placeholder, while the replay record keeps the unredacted payload
    so ship 4 diff stays correct."""
    from agent_service.boundary import EXTERNAL_TEXT_REDACTED_PLACEHOLDER

    proto_ct = {"Content-Type": "application/x-protobuf"}
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/primitive/get_token_info",
        content=canned.encode_get_token_info_response(),
        headers=proto_ct,
        is_reusable=True,
    )

    agent = build_agent()
    test_model = TestModel(call_tools=["get_token_info"])
    deps = _make_deps(primitive_client)
    deps.external_text_input_enabled = False

    with agent.override(model=test_model):
        result = await agent.run("token info", deps=deps)

    # Replay record keeps the truth.
    record = next(
        r for r in deps.tool_call_records if r.primitive_name == "get_token_info"
    )
    assert record.output_value["name"] == "USD Coin"
    assert record.output_value["symbol"] == "USDC"

    # The model's view, captured via TestModel's recorded messages, must
    # contain the redacted placeholder rather than the issuer text.
    history = result.all_messages()
    serialized = repr(history)
    assert EXTERNAL_TEXT_REDACTED_PLACEHOLDER in serialized
    assert "USD Coin" not in serialized
    assert "USDC" not in serialized
    # Constrained-format fields still pass through so the model can
    # talk about which mint the answer was for.
    assert "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v" in serialized


async def test_agent_no_real_network_call(
    primitive_client: PrimitiveClient, with_happy_path_primitives
):
    """Hard belt-and-suspenders: only Rust data-plane URLs should appear
    in the recorded outbound HTTP."""
    agent = build_agent()
    test_model = TestModel(call_tools=["wallet_profile"])
    deps = _make_deps(primitive_client)

    with agent.override(model=test_model):
        await agent.run("q", deps=deps)

    for req in with_happy_path_primitives.get_requests():
        assert str(req.url).startswith(DATA_PLANE_BASE), (
            f"unexpected outbound HTTP to {req.url}"
        )
