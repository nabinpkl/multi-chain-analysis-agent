"""Tests for `/agent/ask` and `/agent/stream/{session_id}` route
contracts. Lifespan + dependency wiring only; no LLM calls.

Stage 3 of the proto migration: requests + responses are proto
canonical JSON (camelCase). The `make_ask_payload` helper builds
the camelCase shape; tests pass through that.
"""

from __future__ import annotations

from tests.fixtures import primitive_responses as canned


def test_ask_returns_session_started(test_app):
    resp = test_app.post("/agent/ask", json=canned.make_ask_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["sessionId"], str) and body["sessionId"]
    assert isinstance(body["threadId"], str) and body["threadId"]
    # `turn` defaults to 0 and proto3 canonical JSON omits zero scalars,
    # so the field is absent on a fresh session.
    assert "turn" not in body or body["turn"] == 0


def test_ask_echoes_thread_id_when_provided(test_app):
    given = "thread-12345"
    resp = test_app.post(
        "/agent/ask",
        json=canned.make_ask_payload(thread_id=given),
    )
    assert resp.status_code == 200
    assert resp.json()["threadId"] == given


def test_ask_mints_unique_session_ids(test_app):
    a = test_app.post("/agent/ask", json=canned.make_ask_payload()).json()
    b = test_app.post("/agent/ask", json=canned.make_ask_payload()).json()
    assert a["sessionId"] != b["sessionId"]


def test_ask_mints_unique_thread_ids_when_omitted(test_app):
    a = test_app.post("/agent/ask", json=canned.make_ask_payload()).json()
    b = test_app.post("/agent/ask", json=canned.make_ask_payload()).json()
    assert a["threadId"] != b["threadId"]


def test_stream_unknown_session_returns_404(test_app):
    resp = test_app.get("/agent/stream/no-such-session-id")
    assert resp.status_code == 404


def test_ask_rejects_missing_focus(test_app):
    """A request that parses cleanly but has no focus must hit the
    Phase 0/A walking-skeleton 400 from `_focus_addr_or_die`. Sending
    an empty body parses to a default AgentRequest with no focus set."""
    resp = test_app.post("/agent/ask", json={"userQuestion": "q"})
    assert resp.status_code == 400


def test_ask_rejects_non_wallet_focus(test_app):
    """Phase 0/A walking-skeleton requires a wallet focus. Edge or
    community focus is a synchronous 400 (not a delayed SSE error
    frame). Phase II will broaden this."""
    payload = canned.make_ask_payload()
    payload["context"]["focus"] = {"community": {"id": 8}}
    resp = test_app.post("/agent/ask", json=payload)
    assert resp.status_code == 400
