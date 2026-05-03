"""Thread state + registry semantics. Mirror of the eviction tests in
`backend/src/agent/mod.rs`."""

from __future__ import annotations

import asyncio

import pytest

from agent_service.thread_state import (
    MAX_THREAD_CLAIMS,
    MAX_THREAD_TOOL_CALL_TURNS,
    AgentThread,
    ThreadRegistry,
    TurnToolCallRecord,
)


def _make_thread(thread_id: str = "t1") -> AgentThread:
    return AgentThread(thread_id=thread_id, started_at_ms=0)


def test_record_claim_evicts_at_cap():
    t = _make_thread()
    for i in range(MAX_THREAD_CLAIMS + 5):
        t.record_claim({"id": f"c{i}"})
    assert len(t.claims) == MAX_THREAD_CLAIMS
    # Oldest 5 dropped; first survivor is index 5.
    assert t.claims[0]["id"] == "c5"


def test_tool_call_per_turn_evicts_oldest_turn():
    t = _make_thread()
    rec = TurnToolCallRecord(
        primitive_name="wallet_profile",
        args={"addr": "X"},
        output_value={},
        call_id="x",
    )
    for turn in range(MAX_THREAD_TOOL_CALL_TURNS + 3):
        t.record_turn_tool_call(turn, rec)
    assert len(t.tool_calls_per_turn) == MAX_THREAD_TOOL_CALL_TURNS
    # Oldest 3 turns dropped (turn keys 0, 1, 2). Smallest survivor = 3.
    assert min(t.tool_calls_per_turn.keys()) == 3


def test_user_questions_evict_in_lockstep():
    t = _make_thread()
    for turn in range(MAX_THREAD_TOOL_CALL_TURNS + 2):
        t.record_turn_user_question(turn, f"q{turn}")
    assert len(t.user_questions_per_turn) == MAX_THREAD_TOOL_CALL_TURNS
    assert min(t.user_questions_per_turn.keys()) == 2


async def test_registry_creates_and_returns_lock():
    reg = ThreadRegistry()
    thread, lock = await reg.get_or_create("t1")
    assert thread.thread_id == "t1"
    assert isinstance(lock, asyncio.Lock)
    # Same id returns same thread + same lock (identity).
    again_thread, again_lock = await reg.get_or_create("t1")
    assert again_thread is thread
    assert again_lock is lock


async def test_registry_serializes_concurrent_turns_on_same_thread():
    """The per-thread lock prevents two concurrent SSE GETs against the
    same thread from running their loop bodies in parallel. We exercise
    this by acquiring both halves of the lock and ensuring the second
    `async with` blocks until the first releases."""
    reg = ThreadRegistry()
    _, lock = await reg.get_or_create("t1")

    order: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def first():
        async with lock:
            order.append("first-acquired")
            started.set()
            await release.wait()
            order.append("first-released")

    async def second():
        await started.wait()
        # Try to acquire while first holds; should block.
        async with lock:
            order.append("second-acquired")

    task1 = asyncio.create_task(first())
    task2 = asyncio.create_task(second())
    # Yield so second has a chance to start and queue.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(task1, task2)

    assert order == ["first-acquired", "first-released", "second-acquired"]
