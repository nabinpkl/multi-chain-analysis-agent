"""Tests for the snapshot-lease lifecycle end-to-end through the
FastAPI app. The autouse `_no_live_llm` fixture (in `conftest.py`)
stubs `agent_service.llm.make_model` so every agent the lifespan
builds gets a `TestModel` instead of a real OpenRouter-backed model.
No per-test override plumbing.

What we verify:
- `POST /turn/begin` is hit exactly once per turn
- `POST /turn/end` is hit exactly once per turn (including failure paths)
- Every primitive call carries the leased snapshot_id
"""

from __future__ import annotations

import json

from agent_service.main import app

from tests.conftest import DATA_PLANE_BASE
from tests.fixtures import primitive_responses as canned


def _post_turn_and_consume(test_app, payload: dict) -> list[dict]:
    """POST /agent/turn and drain the SSE response body. Returns the
    parsed events list."""
    events: list[dict] = []
    with test_app.stream("POST", "/agent/turn", json=payload) as resp:
        assert resp.status_code == 200
        current_event: str | None = None
        for line in resp.iter_lines():
            if line.startswith("event:"):
                current_event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and current_event:
                payload_str = line.split(":", 1)[1].strip()
                try:
                    parsed = json.loads(payload_str)
                except json.JSONDecodeError:
                    parsed = {"_raw": payload_str}
                events.append({"event": current_event, "data": parsed})
                current_event = None
    return events


def test_turn_begin_called_once_per_turn(test_app, with_happy_path_primitives):
    """Happy path: agent runs, begin hit once, end hit once."""
    events = _post_turn_and_consume(
        test_app, canned.make_ask_payload("Profile this wallet")
    )

    # Final frame is the `Done` event carrying AgentDone.
    assert events[-1]["event"] == "Done"
    done_payload = events[-1]["data"]
    assert isinstance(done_payload.get("elapsedMs", 0), int)
    assert done_payload.get("elapsedMs", 0) >= 0

    requests = with_happy_path_primitives.get_requests()
    begin_calls = [r for r in requests if r.url.path == "/turn/begin"]
    end_calls = [r for r in requests if r.url.path == "/turn/end"]
    assert len(begin_calls) == 1
    assert len(end_calls) == 1


def test_turn_end_carries_lease_id(test_app, with_happy_path_primitives):
    """The /turn/end body must include the snapshot_id from /turn/begin
    (binary protobuf-encoded SnapshotEndRequest)."""
    from multichain.wire.shared.v1 import snapshot_pb2 as snap_pb

    _post_turn_and_consume(test_app, canned.make_ask_payload("q"))

    requests = with_happy_path_primitives.get_requests()
    end_calls = [r for r in requests if r.url.path == "/turn/end"]
    assert len(end_calls) == 1
    sent = end_calls[0]
    assert sent.headers.get("content-type") == "application/x-protobuf"
    decoded = snap_pb.SnapshotEndRequest()
    decoded.ParseFromString(sent.read())
    assert decoded.snapshot_id == canned.VALID_SNAPSHOT_ID


def test_primitive_calls_carry_lease_id(
    test_app, with_happy_path_primitives, monkeypatch
):
    """Every primitive call in the turn must carry the same leased
    snapshot_id. If the agent dispatches a tool that drops it, every
    real-Rust call would 410 Gone. Bodies are binary-protobuf encoded
    `WalletProfileRequest` / `CommunitySummaryRequest`."""
    from pydantic_ai.models.test import TestModel

    from multichain.wire.shared.v1 import primitive_envelope_pb2 as env_pb

    # The autouse `_no_live_llm` fixture hands every agent a
    # `TestModel(call_tools=[])`; this test needs the primary agent
    # to actually invoke `wallet_profile`, so swap its model in. Direct
    # `agent.model = ...` (instance attribute) survives the
    # TestClient -> SSE handler thread hop, unlike `Agent.override(...)`
    # which is contextvar-based and invisible across threads.
    monkeypatch.setattr(
        app.state.handles.primary_agent,
        "model",
        TestModel(call_tools=["wallet_profile"]),
    )

    _post_turn_and_consume(test_app, canned.make_ask_payload("q"))

    requests = with_happy_path_primitives.get_requests()
    primitive_calls = [r for r in requests if r.url.path.startswith("/primitive/")]
    assert len(primitive_calls) >= 1
    for call in primitive_calls:
        assert call.headers.get("content-type") == "application/x-protobuf"
        if call.url.path == "/primitive/wallet_profile":
            decoded = env_pb.WalletProfileRequest()
        else:
            decoded = env_pb.CommunitySummaryRequest()
        decoded.ParseFromString(call.read())
        assert decoded.snapshot_id == canned.VALID_SNAPSHOT_ID


def test_turn_end_fires_even_when_agent_raises(test_app, mock_data_plane, monkeypatch):
    """Critical safety property: `try/finally` in the SSE handler
    releases the lease even if the agent crashes mid-turn. Without
    this, GC would still reap within 5 minutes, but we'd leak under
    crash storms."""
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/turn/begin",
        content=canned.encode_snapshot_begin_response(),
        headers={"Content-Type": "application/x-protobuf"},
    )
    mock_data_plane.add_response(
        method="POST", url=f"{DATA_PLANE_BASE}/turn/end", status_code=204
    )

    # Patch agent.run to raise. We don't care which layer raises;
    # the contract under test is that the SSE handler's `finally`
    # block runs `client.end_turn(snapshot_id)` regardless.
    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated mid-turn failure")

    monkeypatch.setattr(app.state.handles.primary_agent, "run", _boom)

    events = _post_turn_and_consume(test_app, canned.make_ask_payload("q"))

    # Event stream got an Error frame followed by Done.
    error_events = [e for e in events if e["event"] == "Error"]
    assert len(error_events) == 1

    # Critically: /turn/end was still called.
    requests = mock_data_plane.get_requests()
    end_calls = [r for r in requests if r.url.path == "/turn/end"]
    assert len(end_calls) == 1
