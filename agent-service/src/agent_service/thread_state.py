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
import dataclasses
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from google.protobuf import json_format
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from multichain.wire.agent.v1 import claim_pb2

from agent_service.policy.binding_store import PrimitiveBindingStore

log = structlog.get_logger(__name__)

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

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TurnToolCallRecord":
        return cls(
            primitive_name=data["primitive_name"],
            args=data["args"],
            output_value=data["output_value"],
            call_id=data["call_id"],
        )


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

    # ------------------------------------------------------------------
    # Disk serialization
    # ------------------------------------------------------------------
    def to_state_dict(self) -> dict[str, Any]:
        """Serialize the thread to a JSON-shaped dict for on-disk
        persistence. Round-trips via `from_state_dict`. Used by
        `ThreadRegistry` to write `state.json` at end of turn so a
        container restart can reload the conversation."""
        return {
            "thread_id": self.thread_id,
            "started_at_ms": self.started_at_ms,
            "turn_count": self.turn_count,
            # pydantic-ai ships an official TypeAdapter for the whole
            # `list[ModelMessage]` union. Round-trips every variant
            # (UserPromptPart, ToolReturnPart, ToolCallPart, etc) with
            # the framework's own version-pinned codec; no custom
            # walking needed.
            "message_history": ModelMessagesTypeAdapter.dump_json(
                self.message_history
            ).decode("utf-8"),
            # Proto canonical JSON per Claim; one element per claim so
            # the file is grep-able by id.
            "claims": [
                json_format.MessageToJson(c, preserving_proto_field_name=False)
                for c in self.claims
            ],
            "bindings": self.bindings.to_dict(),
            # `tool_calls_per_turn` and `user_questions_per_turn` are
            # keyed by int turn; JSON requires string keys, so encode
            # with `str(turn)` and decode back to int on load.
            "tool_calls_per_turn": {
                str(turn): [r.to_dict() for r in records]
                for turn, records in self.tool_calls_per_turn.items()
            },
            "user_questions_per_turn": {
                str(turn): q for turn, q in self.user_questions_per_turn.items()
            },
        }

    @classmethod
    def from_state_dict(cls, data: dict[str, Any]) -> "AgentThread":
        message_history: list[ModelMessage] = list(
            ModelMessagesTypeAdapter.validate_json(data["message_history"])
        )
        claims: list[claim_pb2.Claim] = []
        for c_json in data.get("claims", []):
            claim = claim_pb2.Claim()
            json_format.Parse(c_json, claim, ignore_unknown_fields=True)
            claims.append(claim)
        tool_calls: dict[int, list[TurnToolCallRecord]] = {}
        for turn_str, records in data.get("tool_calls_per_turn", {}).items():
            tool_calls[int(turn_str)] = [
                TurnToolCallRecord.from_dict(r) for r in records
            ]
        user_questions: dict[int, str] = {
            int(turn_str): q
            for turn_str, q in data.get("user_questions_per_turn", {}).items()
        }
        return cls(
            thread_id=data["thread_id"],
            started_at_ms=int(data["started_at_ms"]),
            turn_count=int(data.get("turn_count", 0)),
            message_history=message_history,
            claims=claims,
            bindings=PrimitiveBindingStore.from_dict(data.get("bindings", {})),
            tool_calls_per_turn=tool_calls,
            user_questions_per_turn=user_questions,
        )


class ThreadRegistry:
    """In-process thread store. One `AgentThread` per `thread_id` plus
    a per-thread `asyncio.Lock` so concurrent turns on the same thread
    serialize.

    Outer lock guards registry mutation (insert/lookup); inner per-
    thread locks guard turn execution. Standard double-locking pattern;
    fine here because thread creation is cheap and contention rare.

    Threads are backed by `<thread_root>/threads/<thread_id>/state.json`
    on disk when `thread_root` is provided. On lookup, a memory miss
    falls back to disk read; if `state.json` exists we hydrate the
    `AgentThread` and cache it. `persist(thread)` writes atomically
    via `tempfile + os.replace` and is called by the loop driver at
    end of turn under the per-thread lock. When `thread_root` is None
    (unit tests, ephemeral dev), persistence is a no-op."""

    def __init__(self, thread_root: Path | None = None) -> None:
        self._threads: dict[str, AgentThread] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()
        self._thread_root = thread_root
        if self._thread_root is not None:
            (self._thread_root / "threads").mkdir(parents=True, exist_ok=True)

    def _state_path(self, thread_id: str) -> Path | None:
        if self._thread_root is None:
            return None
        return self._thread_root / "threads" / thread_id / "state.json"

    def _load_from_disk(self, thread_id: str) -> AgentThread | None:
        path = self._state_path(thread_id)
        if path is None or not path.exists():
            return None
        try:
            data = json.loads(path.read_text("utf-8"))
            return AgentThread.from_state_dict(data)
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as e:
            # Corrupt state file: log + treat as missing so the caller
            # decides whether to mint fresh or 404. Don't crash the
            # process over one bad file.
            log.warning(
                "thread_state_load_failed",
                thread_id=thread_id,
                error=str(e),
            )
            return None

    async def get_or_create(self, thread_id: str) -> tuple[AgentThread, asyncio.Lock]:
        """Look up the thread and its lock. Creates both lazily if
        first encounter. On in-memory miss tries disk; on disk miss
        creates a fresh thread. Returns the lock unacquired; caller
        is responsible for `async with` around the turn."""
        async with self._registry_lock:
            thread = self._threads.get(thread_id)
            if thread is None:
                # Disk fallback before mint-fresh.
                thread = self._load_from_disk(thread_id) or AgentThread(
                    thread_id=thread_id,
                    started_at_ms=int(time.time() * 1000),
                )
                self._threads[thread_id] = thread
                self._locks[thread_id] = asyncio.Lock()
            return thread, self._locks[thread_id]

    def get(self, thread_id: str) -> AgentThread | None:
        """Read-only lookup. Memory hit only; does not touch disk.
        Used by the request handler to decide between use-existing,
        mint-fresh, and 404. Call `exists(thread_id)` for the
        memory-or-disk check."""
        return self._threads.get(thread_id)

    def exists(self, thread_id: str) -> bool:
        """True iff the thread is in memory OR has a `state.json` on
        disk. Used by the POST /agent/turn handler to validate a
        client-supplied `thread_id` and return 404 when stale."""
        if thread_id in self._threads:
            return True
        path = self._state_path(thread_id)
        return path is not None and path.exists()

    def persist(self, thread: AgentThread) -> None:
        """Atomically write the thread's state to disk. No-op when
        `thread_root` is None. Caller must hold the per-thread lock
        so concurrent writes can't race with the next turn's read."""
        path = self._state_path(thread.thread_id)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # NamedTemporaryFile in the same directory so os.replace is
        # atomic (rename within one filesystem). delete=False because
        # we hand the file off to os.replace; on success the temp
        # file becomes the target; on failure the cleanup catches it.
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".state-",
            suffix=".tmp",
            delete=False,
        )
        try:
            json.dump(thread.to_state_dict(), tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, path)
        except Exception:
            tmp.close()
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise

    def __len__(self) -> int:
        return len(self._threads)
