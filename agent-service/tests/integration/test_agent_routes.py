"""Tests for `/agent/ask` and `/agent/stream/{session_id}` route
contracts. Lifespan + dependency wiring only; no LLM calls.
"""

from __future__ import annotations


def test_ask_returns_session_started(test_app):
    resp = test_app.post(
        "/agent/ask",
        json={
            "question": "anything",
            "focus_addr": "X",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["session_id"], str) and body["session_id"]
    assert isinstance(body["thread_id"], str) and body["thread_id"]


def test_ask_echoes_thread_id_when_provided(test_app):
    given = "thread-12345"
    resp = test_app.post(
        "/agent/ask",
        json={"question": "q", "focus_addr": "X", "thread_id": given},
    )
    assert resp.status_code == 200
    assert resp.json()["thread_id"] == given


def test_ask_mints_unique_session_ids(test_app):
    a = test_app.post("/agent/ask", json={"question": "q", "focus_addr": "X"}).json()
    b = test_app.post("/agent/ask", json={"question": "q", "focus_addr": "X"}).json()
    assert a["session_id"] != b["session_id"]


def test_ask_mints_unique_thread_ids_when_omitted(test_app):
    a = test_app.post("/agent/ask", json={"question": "q", "focus_addr": "X"}).json()
    b = test_app.post("/agent/ask", json={"question": "q", "focus_addr": "X"}).json()
    assert a["thread_id"] != b["thread_id"]


def test_stream_unknown_session_returns_404(test_app):
    resp = test_app.get("/agent/stream/no-such-session-id")
    assert resp.status_code == 404


def test_ask_validates_required_fields(test_app):
    resp = test_app.post("/agent/ask", json={"question": "q"})  # missing focus_addr
    assert resp.status_code == 422


def test_ask_rejects_extra_fields(test_app):
    resp = test_app.post(
        "/agent/ask",
        json={"question": "q", "focus_addr": "X", "uninvited": "field"},
    )
    assert resp.status_code == 422
