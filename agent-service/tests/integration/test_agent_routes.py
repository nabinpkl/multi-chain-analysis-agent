"""Tests for `/agent/ask` and `/agent/stream/{session_id}` route
contracts. Lifespan + dependency wiring only; no LLM calls.

Phase I locked the request shape: `user_question` + `context.focus`
(EntityRef) + optional `switches`/`show_trace`/`thread_id`. The
canned `make_ask_payload` helper builds it; tests pass through that.
"""

from __future__ import annotations

from tests.fixtures import primitive_responses as canned


def test_ask_returns_session_started(test_app):
    resp = test_app.post("/agent/ask", json=canned.make_ask_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["session_id"], str) and body["session_id"]
    assert isinstance(body["thread_id"], str) and body["thread_id"]
    assert body["turn"] == 0


def test_ask_echoes_thread_id_when_provided(test_app):
    given = "thread-12345"
    resp = test_app.post(
        "/agent/ask",
        json=canned.make_ask_payload(thread_id=given),
    )
    assert resp.status_code == 200
    assert resp.json()["thread_id"] == given


def test_ask_mints_unique_session_ids(test_app):
    a = test_app.post("/agent/ask", json=canned.make_ask_payload()).json()
    b = test_app.post("/agent/ask", json=canned.make_ask_payload()).json()
    assert a["session_id"] != b["session_id"]


def test_ask_mints_unique_thread_ids_when_omitted(test_app):
    a = test_app.post("/agent/ask", json=canned.make_ask_payload()).json()
    b = test_app.post("/agent/ask", json=canned.make_ask_payload()).json()
    assert a["thread_id"] != b["thread_id"]


def test_stream_unknown_session_returns_404(test_app):
    resp = test_app.get("/agent/stream/no-such-session-id")
    assert resp.status_code == 404


def test_ask_validates_required_fields(test_app):
    """Missing `context` is a 422; pydantic catches it before our
    own focus-addr 400 check fires."""
    resp = test_app.post("/agent/ask", json={"user_question": "q"})
    assert resp.status_code == 422


def test_ask_rejects_extra_fields(test_app):
    """`extra='forbid'` on AgentRequest catches typos like
    `focus_addr` (the old Phase 0 field name)."""
    bad = canned.make_ask_payload()
    bad["focus_addr"] = "X"  # field that doesn't exist on AgentRequest
    resp = test_app.post("/agent/ask", json=bad)
    assert resp.status_code == 422


def test_ask_rejects_non_wallet_focus(test_app):
    """Phase 0/A walking-skeleton requires a wallet focus. Edge or
    community focus is a synchronous 400 (not a delayed SSE error
    frame). Phase II will broaden this."""
    payload = canned.make_ask_payload()
    payload["context"]["focus"] = {"kind": "community", "id": 8}
    resp = test_app.post("/agent/ask", json=payload)
    assert resp.status_code == 400
