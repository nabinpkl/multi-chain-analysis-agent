"""Tests for the snapshot-lease lifecycle end-to-end through the
FastAPI app. Uses Pydantic AI's `Agent.override(model=TestModel(...))`
to skip the real LLM call so we can focus on the begin/end wiring.

What we verify:
- `POST /turn/begin` is hit exactly once per turn
- `POST /turn/end` is hit exactly once per turn (including failure paths)
- Every primitive call carries the leased snapshot_id
"""

from __future__ import annotations

import json

from pydantic_ai.models.test import TestModel

from agent_service.main import app

from tests.conftest import DATA_PLANE_BASE
from tests.fixtures import primitive_responses as canned


def _consume_sse(test_app, session_id: str) -> list[dict]:
    """Drain the SSE stream and return the parsed events."""
    events: list[dict] = []
    with test_app.stream("GET", f"/agent/stream/{session_id}") as resp:
        assert resp.status_code == 200
        current_event: str | None = None
        for line in resp.iter_lines():
            if line.startswith("event:"):
                current_event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and current_event:
                payload = line.split(":", 1)[1].strip()
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    parsed = {"_raw": payload}
                events.append({"event": current_event, "data": parsed})
                current_event = None
    return events


def test_turn_begin_called_once_per_turn(test_app, with_happy_path_primitives):
    """Happy path: agent runs, begin hit once, end hit once."""
    test_model = TestModel(call_tools=[])  # no tool calls; agent just emits final
    with app.state.agent.override(model=test_model):
        ask = test_app.post(
            "/agent/ask",
            json=canned.make_ask_payload("Profile this wallet"),
        ).json()
        events = _consume_sse(test_app, ask["session_id"])

    # Final frame is the `Done` event carrying AgentDone.
    assert events[-1]["event"] == "Done"
    done_payload = events[-1]["data"]
    assert done_payload["session_id"] == ask["session_id"]
    assert isinstance(done_payload["elapsed_ms"], int)
    assert done_payload["elapsed_ms"] >= 0

    requests = with_happy_path_primitives.get_requests()
    begin_calls = [r for r in requests if r.url.path == "/turn/begin"]
    end_calls = [r for r in requests if r.url.path == "/turn/end"]
    assert len(begin_calls) == 1
    assert len(end_calls) == 1


def test_turn_end_carries_lease_id(test_app, with_happy_path_primitives):
    """The /turn/end body must include the snapshot_id from /turn/begin."""
    test_model = TestModel(call_tools=[])
    with app.state.agent.override(model=test_model):
        ask = test_app.post("/agent/ask", json=canned.make_ask_payload("q")).json()
        _consume_sse(test_app, ask["session_id"])

    requests = with_happy_path_primitives.get_requests()
    end_calls = [r for r in requests if r.url.path == "/turn/end"]
    assert len(end_calls) == 1
    end_body = json.loads(end_calls[0].read().decode())
    assert end_body == {"snapshot_id": canned.VALID_SNAPSHOT_ID}


def test_primitive_calls_carry_lease_id(test_app, with_happy_path_primitives):
    """Every primitive call in the turn must carry the same leased
    snapshot_id. If the agent dispatches a tool that drops it, every
    real-Rust call would 410 Gone."""
    test_model = TestModel(call_tools=["wallet_profile"])
    with app.state.agent.override(model=test_model):
        ask = test_app.post("/agent/ask", json=canned.make_ask_payload("q")).json()
        _consume_sse(test_app, ask["session_id"])

    requests = with_happy_path_primitives.get_requests()
    primitive_calls = [r for r in requests if r.url.path.startswith("/primitive/")]
    assert len(primitive_calls) >= 1
    for call in primitive_calls:
        body = json.loads(call.read().decode())
        assert body["snapshot_id"] == canned.VALID_SNAPSHOT_ID


def test_turn_end_fires_even_when_agent_raises(test_app, mock_data_plane, monkeypatch):
    """Critical safety property: `try/finally` in the SSE handler
    releases the lease even if the agent crashes mid-turn. Without
    this, GC would still reap within 5 minutes, but we'd leak under
    crash storms."""
    mock_data_plane.add_response(
        method="POST",
        url=f"{DATA_PLANE_BASE}/turn/begin",
        json=canned.SNAPSHOT_BEGIN_RESPONSE,
    )
    mock_data_plane.add_response(
        method="POST", url=f"{DATA_PLANE_BASE}/turn/end", status_code=204
    )

    # Patch agent.run to raise. We don't care which layer raises;
    # the contract under test is that the SSE handler's `finally`
    # block runs `client.end_turn(snapshot_id)` regardless.
    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated mid-turn failure")

    monkeypatch.setattr(app.state.agent, "run", _boom)

    ask = test_app.post("/agent/ask", json=canned.make_ask_payload("q")).json()
    events = _consume_sse(test_app, ask["session_id"])

    # Event stream got an Error frame followed by Done.
    error_events = [e for e in events if e["event"] == "Error"]
    assert len(error_events) == 1

    # Critically: /turn/end was still called.
    requests = mock_data_plane.get_requests()
    end_calls = [r for r in requests if r.url.path == "/turn/end"]
    assert len(end_calls) == 1
