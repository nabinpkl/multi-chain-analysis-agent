"""Thread state + registry semantics. Mirror of the eviction tests in
`backend/src/agent/mod.rs`."""

from __future__ import annotations

import asyncio

import pytest

from pathlib import Path

from agent_service.thread_state import (
    MAX_THREAD_CLAIMS,
    MAX_THREAD_TOOL_CALL_TURNS,
    AgentThread,
    NarrativeSnapshot,
    ThreadRegistry,
    TurnToolCallRecord,
)
from multichain.wire.agent.v1 import claim_pb2, session_pb2
from multichain.wire.shared.v1 import provenance_pb2


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


# ---------------------------------------------------------------------------
# Chunk 4: narrative + archive round-trips, summary sidecar
# ---------------------------------------------------------------------------


def _wallet_ref(addr: str) -> provenance_pb2.ProvenanceRef:
    return provenance_pb2.ProvenanceRef(
        wallet=provenance_pb2.WalletRef(addr=addr, idx=0)
    )


def _number_ref(metric: str, value: float) -> provenance_pb2.ProvenanceRef:
    return provenance_pb2.ProvenanceRef(
        number=provenance_pb2.NumberRef(metric=metric, value=value)
    )


def test_narrative_snapshot_round_trip_via_state_dict():
    """NarrativeSnapshot (with two different provenance kinds) survives
    to_state_dict -> from_state_dict cleanly. Guards the chunk 4
    serialization shape against silent breakage in `MessageToJson` /
    `Parse` on the ProvenanceRef oneof."""
    t = _make_thread()
    t.record_turn_user_question(0, "profile this wallet")
    t.record_turn_narrative(
        0,
        NarrativeSnapshot(
            text="Wallet ${ref:1} is a whale.",
            provenance=[
                _wallet_ref("DLZSeiq2xjikgwcniQB6B89uodkbQHrTcco6mJu9UNuK"),
                _number_ref("volume_lamports", 37_917_586_924.0),
            ],
            retracted_reason="",
        ),
    )
    dumped = t.to_state_dict()
    loaded = AgentThread.from_state_dict(dumped)

    snap = loaded.narratives_per_turn[0]
    assert snap.text == "Wallet ${ref:1} is a whale."
    assert snap.retracted_reason == ""
    assert len(snap.provenance) == 2
    assert snap.provenance[0].WhichOneof("ref") == "wallet"
    assert snap.provenance[0].wallet.addr == (
        "DLZSeiq2xjikgwcniQB6B89uodkbQHrTcco6mJu9UNuK"
    )
    assert snap.provenance[1].WhichOneof("ref") == "number"
    assert snap.provenance[1].number.metric == "volume_lamports"
    assert snap.provenance[1].number.value == 37_917_586_924.0


def test_retracted_narrative_round_trip():
    t = _make_thread()
    t.record_turn_user_question(0, "q")
    t.record_turn_narrative(
        0,
        NarrativeSnapshot(
            text="this was retracted",
            provenance=[],
            retracted_reason="constitution retract: off-topic",
        ),
    )
    loaded = AgentThread.from_state_dict(t.to_state_dict())
    assert (
        loaded.narratives_per_turn[0].retracted_reason
        == "constitution retract: off-topic"
    )


def test_archived_field_round_trip():
    t = _make_thread()
    t.archived = True
    loaded = AgentThread.from_state_dict(t.to_state_dict())
    assert loaded.archived is True


def test_pre_chunk4_state_dict_loads_with_defaults():
    """A state.json without `narratives_per_turn` or `archived` (pre-
    chunk-4 era) loads cleanly with empty defaults. Guards the
    additive-only schema rule."""
    legacy = {
        "thread_id": "old",
        "started_at_ms": 1,
        "turn_count": 0,
        "runtime": session_pb2.AGENT_RUNTIME_PYDANTIC_AI,
        "message_history": "[]",
        "claims": [],
        "bindings": {},
        "tool_calls_per_turn": {},
        "user_questions_per_turn": {},
        # NO narratives_per_turn, NO archived, NO schema_version,
        # NO claims_per_turn.
    }
    loaded = AgentThread.from_state_dict(legacy)
    assert loaded.narratives_per_turn == {}
    assert loaded.archived is False
    assert loaded.claims_per_turn == {}


def _make_claim(thread_id: str, headline: str) -> claim_pb2.Claim:
    claim = claim_pb2.Claim(
        id=f"claim-{headline}",
        thread_id=thread_id,
        kind=claim_pb2.CLAIM_KIND_PROFILE,
        headline=headline,
        body_markdown=f"body for {headline}",
    )
    claim.policy_verdict.approved.SetInParent()
    return claim


def test_claims_per_turn_round_trip_preserves_attribution():
    """Two turns each emit their own claim; after a state.json
    round-trip the per-turn ownership map keys back onto the same
    turns so the transcript replay path can render each turn's chips
    on its own bubble (chunk 4 follow-up; prior to this the flat
    claims list was attached to the last turn only)."""
    t = _make_thread()
    t.record_turn_user_question(0, "profile X")
    t.record_turn_user_question(1, "profile Y")
    c0 = _make_claim("t1", "X is a whale")
    c1 = _make_claim("t1", "Y is a router")
    t.record_claim(c0)
    t.record_turn_claim(0, c0)
    t.record_claim(c1)
    t.record_turn_claim(1, c1)

    loaded = AgentThread.from_state_dict(t.to_state_dict())

    assert set(loaded.claims_per_turn.keys()) == {0, 1}
    assert len(loaded.claims_per_turn[0]) == 1
    assert loaded.claims_per_turn[0][0].headline == "X is a whale"
    assert loaded.claims_per_turn[1][0].headline == "Y is a router"


def test_claims_per_turn_evicts_oldest_turn():
    """FIFO eviction by turn-count, matching the tool-call /
    user-question maps. Once the cap is hit the smallest turn's
    bucket falls off; surviving buckets keep their claim contents
    intact."""
    t = _make_thread()
    for turn in range(MAX_THREAD_TOOL_CALL_TURNS + 4):
        t.record_turn_claim(turn, _make_claim("t1", f"claim-{turn}"))
    assert len(t.claims_per_turn) == MAX_THREAD_TOOL_CALL_TURNS
    # First four turns dropped (0, 1, 2, 3). Smallest survivor = 4.
    assert min(t.claims_per_turn.keys()) == 4


def test_persist_writes_summary_sidecar(tmp_path: Path):
    """`persist()` writes both state.json AND summary.json with the
    row payload `GET /agent/threads` returns."""
    import json

    reg = ThreadRegistry(thread_root=tmp_path)
    thread = AgentThread(
        thread_id="t-summary",
        started_at_ms=1778500000000,
        turn_count=2,
        runtime=session_pb2.AGENT_RUNTIME_CODEX,
    )
    thread.record_turn_user_question(0, "first question")
    thread.record_turn_user_question(1, "second question")
    reg._threads["t-summary"] = thread
    reg.persist(thread)

    summary_path = tmp_path / "threads" / "t-summary" / "summary.json"
    assert summary_path.exists()
    summary = json.loads(summary_path.read_text())
    assert summary["thread_id"] == "t-summary"
    assert summary["runtime"] == session_pb2.AGENT_RUNTIME_CODEX
    assert summary["runtime_name"] == "AGENT_RUNTIME_CODEX"
    assert summary["title"] == "first question"
    assert summary["last_user_question"] == "second question"
    assert summary["turn_count"] == 2
    assert summary["archived"] is False
    assert summary["schema_version"] == 1


def test_list_summaries_orders_newest_first_and_hides_archived(
    tmp_path: Path,
):
    reg = ThreadRegistry(thread_root=tmp_path)
    for tid, started in [("old", 1), ("new", 100), ("mid", 50)]:
        thread = AgentThread(thread_id=tid, started_at_ms=started)
        reg._threads[tid] = thread
        reg.persist(thread)
    # Archive the newest.
    assert reg.archive("new") is True

    visible = reg.list_summaries(include_archived=False)
    assert [r["thread_id"] for r in visible] == ["mid", "old"]

    all_rows = reg.list_summaries(include_archived=True)
    assert [r["thread_id"] for r in all_rows] == ["new", "mid", "old"]


def test_transcript_for_returns_archived_only_when_asked(
    tmp_path: Path,
):
    reg = ThreadRegistry(thread_root=tmp_path)
    thread = AgentThread(thread_id="t", started_at_ms=1, archived=True)
    reg._threads["t"] = thread
    reg.persist(thread)

    assert reg.transcript_for("t", include_archived=False) is None
    got = reg.transcript_for("t", include_archived=True)
    assert got is not None
    assert got.thread_id == "t"


def test_archive_idempotent(tmp_path: Path):
    reg = ThreadRegistry(thread_root=tmp_path)
    thread = AgentThread(thread_id="t", started_at_ms=1)
    reg._threads["t"] = thread
    reg.persist(thread)

    assert reg.archive("t") is True
    assert reg.archive("t") is True
    assert reg.archive("nope") is False
