"""Tests for the `/agent/turn` streaming-POST route contract. Lifespan
+ dependency wiring only; no LLM calls.

Proto canonical JSON (camelCase) inbound; SSE body outbound. The
`make_ask_payload` helper builds the camelCase request shape; tests
pass through that.

Every test that POSTs to /agent/turn drains the stream because the
loop driver fires synchronously on POST (no two-step handoff) and
will call `/turn/begin` against the mocked data plane immediately.
The `with_happy_path_primitives` fixture supplies canned primitives
so the loop driver runs cleanly through to AgentDone.
"""

from __future__ import annotations

from tests.fixtures import primitive_responses as canned


def _post_and_drain(test_app, payload: dict) -> tuple[int, str]:
    """POST /agent/turn, drain the SSE response body, return
    (status_code, server-minted thread_id from response header)."""
    with test_app.stream("POST", "/agent/turn", json=payload) as resp:
        thread_id = resp.headers.get("x-mca-thread-id", "")
        # Drain the body so the loop driver runs to completion and
        # the /turn/end mock gets satisfied. Without this, the SSE
        # generator gets cancelled mid-turn and pytest-httpx flags
        # the unmatched begin_turn call.
        for _ in resp.iter_lines():
            pass
        return resp.status_code, thread_id


def test_turn_mints_thread_id(test_app, with_happy_path_primitives):
    status, tid = _post_and_drain(test_app, canned.make_ask_payload())
    assert status == 200
    assert tid


def test_turn_echoes_thread_id_when_provided(test_app, with_happy_path_primitives):
    payload = canned.make_ask_payload()
    status, first_tid = _post_and_drain(test_app, payload)
    assert status == 200
    assert first_tid
    # Re-use the just-minted id explicitly; server should run as
    # the next turn of the same thread.
    payload2 = canned.make_ask_payload(thread_id=first_tid)
    status2, tid = _post_and_drain(test_app, payload2)
    assert status2 == 200
    assert tid == first_tid


def test_turn_mints_unique_thread_ids_when_omitted(
    test_app, with_happy_path_primitives
):
    _, a = _post_and_drain(test_app, canned.make_ask_payload())
    _, b = _post_and_drain(test_app, canned.make_ask_payload())
    assert a != b


def test_turn_unknown_thread_id_returns_404(test_app):
    """Stale localStorage path. Frontend recovery: clear the local
    thread_id, retry POST without it, server mints a fresh one."""
    payload = canned.make_ask_payload(thread_id="no-such-thread-id")
    resp = test_app.post("/agent/turn", json=payload)
    assert resp.status_code == 404


def test_turn_rejects_missing_context(test_app):
    """Phase II requires a `context` block. Without it the loop driver
    has no view-context to wrap into the user prompt; reject early so
    the error is synchronous, not a delayed SSE error frame."""
    resp = test_app.post("/agent/turn", json={"userQuestion": "q"})
    assert resp.status_code == 400


def test_turn_rejects_empty_question(test_app):
    """Empty `userQuestion` is a synchronous 400. Loop driver would have
    nothing to send to the model otherwise."""
    payload = canned.make_ask_payload()
    payload["userQuestion"] = "   "
    resp = test_app.post("/agent/turn", json=payload)
    assert resp.status_code == 400


def test_turn_accepts_non_wallet_focus(test_app, with_happy_path_primitives):
    """Phase II broadened the walking-skeleton restriction. The model
    sees focus via the `<context>` block; community/edge focuses flow
    through unchanged. The walking-skeleton hard-coded wallet
    requirement is gone."""
    payload = canned.make_ask_payload()
    payload["context"]["focus"] = {"community": {"id": 8}}
    status, _ = _post_and_drain(test_app, payload)
    assert status == 200
