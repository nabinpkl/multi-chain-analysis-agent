"""In-memory thread state. Direct port of `backend/src/agent/mod.rs`'s
`AgentThread` + `TurnToolCallRecord` + the FIFO eviction.

Per-process registry indexed by `thread_id`. Per-thread `asyncio.Lock`
serializes turns within one thread (matters when the frontend opens
two SSE streams against the same thread; the lock holds the second one
until the first releases). Server restart wipes everything, matching
the Rust `thread.in_memory_only` stub semantics.

Three pieces of per-turn state survive across turns:

1. `messages`: Pydantic AI doesn't expose a raw rig-style message vec
   the way Rust's `client.complete_with_history` did. Instead we pass
   `message_history` between `agent.run()` calls. The store holds a
   list of pydantic-ai `ModelMessage` objects produced by the most
   recent turn.

2. `claims`: every approved Claim from prior turns. Bounded at
   `MAX_THREAD_CLAIMS`. Used by the constitution gate's `same_turn_claims`
   payload field for narrative judgement.

3. `bindings`: ship 3 PrimitiveBindingStore. Survives across turns so a
   follow-up turn can structurally verify against earlier primitive
   output without re-fetching.

Plus per-turn buffers:

4. `tool_calls_per_turn`: ship 4 replay buffer keyed by turn number.
   Stores `(primitive_name, args, output_dict, call_id)` so the diff
   walker can re-fetch and compare.

5. `user_questions_per_turn`: ship 4 history for the repeat detector.
   Keyed by turn number; FIFO with the tool call map.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai.messages import ModelMessage

from multichain.wire.agent.v1 import claim_pb2

from .policy.binding_store import PrimitiveBindingStore

# FIFO cap on `claims`. Matches Rust's `MAX_THREAD_CLAIMS = 20`. Covers
# ~5-10 turns of typical conversation; older Claims drop.
MAX_THREAD_CLAIMS: int = 20

# FIFO cap on the per-turn tool-call replay map AND the user-questions
# map. They share the cap so they stay roughly aligned. Matches Rust's
# `MAX_THREAD_TOOL_CALL_TURNS = 20`.
MAX_THREAD_TOOL_CALL_TURNS: int = 20


@dataclass(slots=True)
class TurnToolCallRecord:
    """Per-turn tool-call snapshot. Captured as each primitive dispatch
    returns; replayed on repeat detection. The diff walker reads fields
    out of `output_value` (already a Python dict from the envelope's
    Struct field) via the per-primitive diff_spec.

    Backend-only; never crosses the wire."""

    primitive_name: str
    args: dict[str, Any]
    output_value: dict[str, Any]
    call_id: str


@dataclass(slots=True)
class AgentThread:
    """One conversation's state. Lives in memory for the lifetime of
    the Python process; refresh / restart drops it (same semantics as
    the Rust `thread.in_memory_only` stub)."""

    thread_id: str
    started_at_ms: int
    turn_count: int = 0
    # Pydantic AI message history. Passed to `agent.run(message_history=...)`
    # on each follow-up turn.
    message_history: list[ModelMessage] = field(default_factory=list)
    # Approved Claims from prior turns.
    claims: list[claim_pb2.Claim] = field(default_factory=list)
    bindings: PrimitiveBindingStore = field(default_factory=PrimitiveBindingStore)
    tool_calls_per_turn: dict[int, list[TurnToolCallRecord]] = field(default_factory=dict)
    user_questions_per_turn: dict[int, str] = field(default_factory=dict)

    def record_claim(self, claim: claim_pb2.Claim) -> None:
        """Append an approved claim, dropping the oldest when the cap
        is exceeded."""
        self.claims.append(claim)
        while len(self.claims) > MAX_THREAD_CLAIMS:
            self.claims.pop(0)

    def record_turn_tool_call(self, turn: int, record: TurnToolCallRecord) -> None:
        """Insert a per-turn tool-call snapshot, dropping the oldest
        turn's entries when the cap is exceeded. emit_claim is filtered
        out by the loop driver before calling here."""
        self.tool_calls_per_turn.setdefault(turn, []).append(record)
        self._evict_oldest_turn_if_needed()

    def record_turn_user_question(self, turn: int, question: str) -> None:
        """Record the user's question for this turn (used by the repeat
        detector). One per turn; later writes overwrite (loop only
        writes once per turn anyway)."""
        self.user_questions_per_turn[turn] = question
        self._evict_oldest_turn_if_needed()

    def _evict_oldest_turn_if_needed(self) -> None:
        """Evict by smallest turn key when either map exceeds cap. Both
        maps share the cap so they stay roughly aligned."""
        while len(self.tool_calls_per_turn) > MAX_THREAD_TOOL_CALL_TURNS:
            oldest = min(self.tool_calls_per_turn.keys())
            self.tool_calls_per_turn.pop(oldest, None)
        while len(self.user_questions_per_turn) > MAX_THREAD_TOOL_CALL_TURNS:
            oldest = min(self.user_questions_per_turn.keys())
            self.user_questions_per_turn.pop(oldest, None)


class ThreadRegistry:
    """In-process thread store. One `AgentThread` per `thread_id` plus
    a per-thread `asyncio.Lock` so concurrent turns on the same thread
    serialize.

    Outer lock guards registry mutation (insert/lookup); inner per-
    thread locks guard turn execution. Standard double-locking pattern;
    fine here because thread creation is cheap and contention rare."""

    def __init__(self) -> None:
        self._threads: dict[str, AgentThread] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

    async def get_or_create(self, thread_id: str) -> tuple[AgentThread, asyncio.Lock]:
        """Look up the thread and its lock. Creates both lazily if
        first encounter. Returns the lock unacquired; caller is
        responsible for `async with` around the turn."""
        async with self._registry_lock:
            thread = self._threads.get(thread_id)
            if thread is None:
                thread = AgentThread(
                    thread_id=thread_id,
                    started_at_ms=int(time.time() * 1000),
                )
                self._threads[thread_id] = thread
                self._locks[thread_id] = asyncio.Lock()
            return thread, self._locks[thread_id]

    def get(self, thread_id: str) -> AgentThread | None:
        """Read-only lookup. Returns None if the thread doesn't exist."""
        return self._threads.get(thread_id)

    def __len__(self) -> int:
        return len(self._threads)
