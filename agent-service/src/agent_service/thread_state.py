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

from multichain.wire.agent.v1 import claim_pb2, session_pb2
from multichain.wire.shared.v1 import provenance_pb2

from agent_service.policy.binding_store import PrimitiveBindingStore

# Chunk 4 on-disk schema version stamp. Bumped only when state.json
# acquires a breaking field-shape change; additive fields stay on
# the same version and are read with `.get(key, default)`. See
# `from_state_dict` for the migration policy.
_STATE_SCHEMA_VERSION: int = 1


class RuntimeMismatchError(Exception):
    """Raised when a request resumes an existing thread with an
    `AgentRequest.runtime` field that disagrees with the value the
    server persisted at thread creation. Runtime is locked per
    thread; switching runtimes mid-conversation would silently
    swap which engine speaks for the user, which is exactly the
    drop-in-equivalence trap the chunk 3 plan refuses. The POST
    handler translates this to HTTP 400 with a clear "start a new
    chat to switch runtime" message."""

    def __init__(self, thread_id: str, stored: int, requested: int) -> None:
        self.thread_id = thread_id
        self.stored = stored
        self.requested = requested
        super().__init__(
            f"thread {thread_id} runtime is {session_pb2.AgentRuntime.Name(stored)}; "
            f"request asked for {session_pb2.AgentRuntime.Name(requested)}"
        )

log = structlog.get_logger(__name__)

# FIFO cap on `claims`. Matches Rust's `MAX_THREAD_CLAIMS = 20`. Covers
# ~5-10 turns of typical conversation; older Claims drop.
MAX_THREAD_CLAIMS: int = 20

# FIFO cap on the per-turn tool-call replay map AND the user-questions
# map. They share the cap so they stay roughly aligned. Matches Rust's
# `MAX_THREAD_TOOL_CALL_TURNS = 20`.
MAX_THREAD_TOOL_CALL_TURNS: int = 20


@dataclass(slots=True)
class NarrativeSnapshot:
    """Chunk 4 history-feature record. The final narrative the agent
    emitted on one turn, captured so a reopened thread can replay
    the full chat scroll instead of showing blank prose bubbles.

    Three fields cover the live narrative paths both runtimes drive:
    - `text`: the prose itself, either approved by the constitution
      gate or carrying the model's draft when retracted.
    - `provenance`: typed entity refs assembled from this turn's
      claims (1-indexed against `${ref:N}` placeholders in `text`).
      The renderer resolves chips against this list, same shape the
      live `NarrativeWithRefs` SSE frame carries.
    - `retracted_reason`: empty when the gate approved the
      narrative; otherwise the user-facing reason the gate gave.
      Drives the retracted-styling in the chat scroll on reopen.

    Backend-only; the wire shape is `TranscriptTurn` in
    `history.proto`."""

    text: str
    provenance: list[provenance_pb2.ProvenanceRef] = field(
        default_factory=list
    )
    retracted_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize for state.json. Provenance refs go through
        the same `MessageToJson` codec as `AgentThread.claims` so
        the JSON shape on disk is consistent across both lists."""
        return {
            "text": self.text,
            "retracted_reason": self.retracted_reason,
            "provenance": [
                json_format.MessageToJson(
                    p, preserving_proto_field_name=False
                )
                for p in self.provenance
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NarrativeSnapshot":
        refs: list[provenance_pb2.ProvenanceRef] = []
        for p_json in data.get("provenance", []):
            ref = provenance_pb2.ProvenanceRef()
            json_format.Parse(p_json, ref, ignore_unknown_fields=True)
            refs.append(ref)
        return cls(
            text=str(data.get("text", "")),
            provenance=refs,
            retracted_reason=str(data.get("retracted_reason", "")),
        )


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
    # Which runtime owns this thread. Locked at creation; the chunk
    # 3 dispatch switch in `main.py` reads it via
    # `ThreadRegistry.runtime_for` before invoking the matching
    # driver. Stored as the proto enum int (`session_pb2.AgentRuntime`
    # values) rather than a string so the wire shape on
    # `runtime.json` round-trips through the enum name without a
    # parallel "is this still the right string" check.
    runtime: int = session_pb2.AGENT_RUNTIME_PYDANTIC_AI
    # Codex-side thread handle, returned by codex on the first turn
    # (`thread/start`) and threaded back via
    # `CodexRunRequest.provider_thread_id` on every subsequent turn
    # so codex resumes the same conversation (preserves prompt
    # caching + codex-side memory). Empty on the pydantic-ai
    # runtime; populated only by the codex driver.
    codex_provider_thread_id: str = ""
    # Pydantic AI message history. Passed to `agent.run(message_history=...)`
    # on each follow-up turn.
    message_history: list[ModelMessage] = field(default_factory=list)
    # Approved Claims from prior turns.
    claims: list[claim_pb2.Claim] = field(default_factory=list)
    bindings: PrimitiveBindingStore = field(default_factory=PrimitiveBindingStore)
    tool_calls_per_turn: dict[int, list[TurnToolCallRecord]] = field(default_factory=dict)
    user_questions_per_turn: dict[int, str] = field(default_factory=dict)
    # Chunk 4. Per-turn final narrative for transcript replay when
    # the user reopens this thread. FIFO-capped with
    # `MAX_THREAD_TOOL_CALL_TURNS` so memory + disk usage stay
    # bounded the same way other per-turn maps do.
    narratives_per_turn: dict[int, NarrativeSnapshot] = field(default_factory=dict)
    # Chunk 4. Per-turn ownership map for approved claims. The flat
    # `claims` list above is FIFO-capped at MAX_THREAD_CLAIMS and
    # has no turn attribution (the constitution gate's
    # `same_turn_claims` payload doesn't need it). This map keeps
    # the same Claim protos keyed by their emit-turn so the
    # transcript replay can render each turn's chips on the right
    # bubble. Same FIFO posture as `tool_calls_per_turn`: cap by
    # turn-count, drop the oldest turn's bucket when over cap.
    claims_per_turn: dict[int, list[claim_pb2.Claim]] = field(
        default_factory=dict
    )
    # Chunk 4. Soft-archive flag. Threads with `archived=True` are
    # hidden from the default history list (`GET /agent/threads`)
    # but still resumable via `GET /agent/thread/{id}`. No GC; the
    # on-disk tree (state.json + summary.json + codex_homes) stays
    # until manually purged.
    archived: bool = False

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

    def record_turn_claim(self, turn: int, claim: claim_pb2.Claim) -> None:
        """Record an approved claim under its emitting turn. Both
        drivers call this alongside `record_claim` so the flat
        thread-level list (used by the constitution gate) and the
        per-turn map (used by transcript replay) stay in sync. FIFO
        eviction matches `tool_calls_per_turn`: drop the oldest
        turn's bucket once the turn-count cap is exceeded."""
        self.claims_per_turn.setdefault(turn, []).append(claim)
        while len(self.claims_per_turn) > MAX_THREAD_TOOL_CALL_TURNS:
            oldest = min(self.claims_per_turn.keys())
            self.claims_per_turn.pop(oldest, None)

    def record_turn_narrative(
        self, turn: int, snapshot: NarrativeSnapshot
    ) -> None:
        """Record the final narrative for this turn so the history
        reopen path can replay the full prose. Called by both
        drivers right before they emit the terminal `Narrative` or
        `NarrativeRetracted` SSE frame, so what gets persisted is
        exactly what the live UI saw.
        """
        self.narratives_per_turn[turn] = snapshot
        # Same FIFO cap as the tool-call / question maps so the
        # three per-turn maps stay in lockstep when the cap kicks
        # in.
        while len(self.narratives_per_turn) > MAX_THREAD_TOOL_CALL_TURNS:
            oldest = min(self.narratives_per_turn.keys())
            self.narratives_per_turn.pop(oldest, None)

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
            # Schema version is monotonic. New ADDITIVE fields stay
            # on the current version (read-side uses `.get`); a
            # version bump is only required when an existing field
            # changes shape or is renamed. So far chunk 4 = v1.
            "schema_version": _STATE_SCHEMA_VERSION,
            "thread_id": self.thread_id,
            "started_at_ms": self.started_at_ms,
            "turn_count": self.turn_count,
            "runtime": self.runtime,
            "codex_provider_thread_id": self.codex_provider_thread_id,
            "archived": self.archived,
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
            "narratives_per_turn": {
                str(turn): snap.to_dict()
                for turn, snap in self.narratives_per_turn.items()
            },
            "claims_per_turn": {
                str(turn): [
                    json_format.MessageToJson(
                        c, preserving_proto_field_name=False
                    )
                    for c in cs
                ]
                for turn, cs in self.claims_per_turn.items()
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
        # `narratives_per_turn` is chunk 4. Pre-chunk-4 state.json
        # files lack the key; `.get` returns {} and the empty map
        # is fine  reopened pre-chunk-4 threads render with blank
        # narrative bubbles (same shape as a retracted-no-text
        # turn, which already renders gracefully).
        narratives: dict[int, NarrativeSnapshot] = {
            int(turn_str): NarrativeSnapshot.from_dict(snap)
            for turn_str, snap in data.get("narratives_per_turn", {}).items()
        }
        # `claims_per_turn` is chunk 4. Pre-chunk-4 state.json files
        # lack the key; the empty map means the reopened transcript
        # falls back to the (already-bounded) flat `claims` list
        # rendered against the last turn  same shape the chunk-4
        # MVP shipped with for compatibility.
        claims_per_turn: dict[int, list[claim_pb2.Claim]] = {}
        for turn_str, cs in data.get("claims_per_turn", {}).items():
            bucket: list[claim_pb2.Claim] = []
            for c_json in cs:
                claim = claim_pb2.Claim()
                json_format.Parse(c_json, claim, ignore_unknown_fields=True)
                bucket.append(claim)
            claims_per_turn[int(turn_str)] = bucket
        return cls(
            thread_id=data["thread_id"],
            started_at_ms=int(data["started_at_ms"]),
            turn_count=int(data.get("turn_count", 0)),
            runtime=int(data.get("runtime", session_pb2.AGENT_RUNTIME_PYDANTIC_AI)),
            codex_provider_thread_id=str(data.get("codex_provider_thread_id", "")),
            archived=bool(data.get("archived", False)),
            message_history=message_history,
            claims=claims,
            bindings=PrimitiveBindingStore.from_dict(data.get("bindings", {})),
            tool_calls_per_turn=tool_calls,
            user_questions_per_turn=user_questions,
            narratives_per_turn=narratives,
            claims_per_turn=claims_per_turn,
        )


# Maximum length (in chars) of the auto-generated thread title.
# Long enough to give the row a useful first-question snippet
# without crowding the dropdown; matches second-brain's posture
# of "title from first prompt, no LLM call."
_TITLE_MAX_CHARS: int = 80


def _build_summary_row(thread: AgentThread) -> dict[str, Any]:
    """Project an `AgentThread` to the seven-field history row.
    Same shape `GET /agent/threads` serves; written to
    `summary.json` on every `persist()` so the list endpoint never
    needs to read the full `state.json` for any thread.

    `title` and `last_user_question` both derive from
    `user_questions_per_turn`. Title is the FIRST user_question
    truncated; last_user_question is the most recent. Both empty
    when the thread minted but never completed a turn (rare; only
    possible if persist runs before `record_turn_user_question`).
    """
    sorted_turns = sorted(thread.user_questions_per_turn.keys())
    first_q = (
        thread.user_questions_per_turn[sorted_turns[0]]
        if sorted_turns
        else ""
    )
    last_q = (
        thread.user_questions_per_turn[sorted_turns[-1]]
        if sorted_turns
        else ""
    )
    title = first_q[:_TITLE_MAX_CHARS]
    return {
        "schema_version": _STATE_SCHEMA_VERSION,
        "thread_id": thread.thread_id,
        "runtime": thread.runtime,
        "runtime_name": session_pb2.AgentRuntime.Name(thread.runtime),
        "started_at_ms": thread.started_at_ms,
        "turn_count": thread.turn_count,
        "title": title,
        "last_user_question": last_q,
        "archived": thread.archived,
    }


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

    def _summary_path(self, thread_id: str) -> Path | None:
        """Sibling of `state.json` carrying the full row payload for
        the history list (chunk 4 replaces the chunk-3 `runtime.json`
        with this wider sidecar). One sub-KB JSON per thread holding
        every field `GET /agent/threads` projects to a row: runtime,
        timestamps, turn count, title, last user question, archived.

        Why a sidecar rather than projecting from `state.json`: the
        list endpoint scans EVERY thread on disk to populate the
        dropdown. Reading the ~10KB state.json per row would be
        ~50x more IO than the sidecar (~200B). The sidecar IS the
        list-side source of truth for chunk 4; state.json remains
        the source of truth for the detail endpoint + the runtime
        itself (gates, hydration, etc).
        """
        if self._thread_root is None:
            return None
        return self._thread_root / "threads" / thread_id / "summary.json"

    def runtime_for(self, thread_id: str) -> int | None:
        """Peek at the persisted runtime without loading the full
        thread. Returns the `session_pb2.AgentRuntime` enum int, or
        None when the thread is unknown (memory miss + disk miss).
        Memory path: return the cached thread's runtime. Disk
        path: read `summary.json` only. Used by the POST handler
        to short-circuit a mismatched request with 400 before any
        per-turn work runs."""
        cached = self._threads.get(thread_id)
        if cached is not None:
            return cached.runtime
        path = self._summary_path(thread_id)
        if path is None or not path.exists():
            return None
        try:
            data = json.loads(path.read_text("utf-8"))
            return int(data.get("runtime", session_pb2.AGENT_RUNTIME_PYDANTIC_AI))
        except (OSError, json.JSONDecodeError, ValueError) as e:
            log.warning(
                "summary_file_load_failed", thread_id=thread_id, error=str(e)
            )
            return None

    def list_summaries(
        self, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        """Iterate every per-thread `summary.json` on disk and
        return the raw row dicts, newest first by `started_at_ms`.
        The chunk 4 `GET /agent/threads` projects these into the
        proto `ThreadSummary` shape; the registry stays
        wire-agnostic.

        Disk-scan cost is O(N) opens, but each file is ~200B so
        even thousands of threads stay under ~100ms total. When
        the scan starts dominating list latency, add an in-memory
        LRU keyed by thread_id and invalidate it on `persist()` /
        `archive()`.
        """
        if self._thread_root is None:
            return []
        threads_dir = self._thread_root / "threads"
        if not threads_dir.exists():
            return []
        summaries: list[dict[str, Any]] = []
        for child in threads_dir.iterdir():
            if not child.is_dir():
                continue
            summary_path = child / "summary.json"
            if not summary_path.exists():
                continue
            try:
                data = json.loads(summary_path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                log.warning(
                    "summary_file_skip", thread_id=child.name, error=str(e)
                )
                continue
            if not include_archived and bool(data.get("archived", False)):
                continue
            summaries.append(data)
        summaries.sort(
            key=lambda s: int(s.get("started_at_ms", 0)), reverse=True
        )
        return summaries

    def archive(self, thread_id: str) -> bool:
        """Soft-archive: set `archived=True` on state.json + summary
        sidecar. Idempotent. Returns True when a thread was flipped
        (memory or disk), False when the thread doesn't exist.

        Codex sqlite under `codex_homes/local/<thread_id>/` is
        intentionally left in place; chunk 4 archive only hides
        the thread from the default list. A future "delete forever"
        endpoint would close the codex session pool entry + sweep
        the tree."""
        if self._thread_root is None:
            return False
        # Memory path: flip the in-memory copy first, then persist.
        cached = self._threads.get(thread_id)
        if cached is not None:
            if cached.archived:
                return True
            cached.archived = True
            self.persist(cached)
            return True
        # Disk-only path: load via from_state_dict, flip, persist.
        loaded = self._load_from_disk(thread_id)
        if loaded is None:
            return False
        if loaded.archived:
            return True
        loaded.archived = True
        self._threads[thread_id] = loaded
        if thread_id not in self._locks:
            self._locks[thread_id] = asyncio.Lock()
        self.persist(loaded)
        return True

    def transcript_for(
        self, thread_id: str, include_archived: bool = False
    ) -> AgentThread | None:
        """Load the full `AgentThread` for replay. Memory path first,
        then disk. Used by `GET /agent/thread/{id}` to project the
        full transcript via `narratives_per_turn` +
        `user_questions_per_turn` + `claims`.

        `include_archived=False` returns None for archived threads
        so the default detail endpoint stays consistent with the
        default list view. The handler can pass True to bypass
        when the client explicitly asks for an archived row."""
        thread = self._threads.get(thread_id) or self._load_from_disk(
            thread_id
        )
        if thread is None:
            return None
        if thread.archived and not include_archived:
            return None
        return thread

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

    async def get_or_create(
        self,
        thread_id: str,
        runtime: int = session_pb2.AGENT_RUNTIME_PYDANTIC_AI,
    ) -> tuple[AgentThread, asyncio.Lock]:
        """Look up the thread and its lock. Creates both lazily if
        first encounter. On in-memory miss tries disk; on disk miss
        creates a fresh thread with the supplied runtime. Returns
        the lock unacquired; caller is responsible for `async with`
        around the turn.

        On a disk hit whose persisted `runtime` disagrees with the
        requested one, raises `RuntimeMismatchError`; the POST
        handler translates that to 400. Runtime is locked at
        creation per the chunk 3 plan.
        """
        async with self._registry_lock:
            thread = self._threads.get(thread_id)
            if thread is None:
                # Disk fallback before mint-fresh.
                loaded = self._load_from_disk(thread_id)
                if loaded is not None:
                    if loaded.runtime != runtime:
                        raise RuntimeMismatchError(
                            thread_id=thread_id,
                            stored=loaded.runtime,
                            requested=runtime,
                        )
                    thread = loaded
                else:
                    thread = AgentThread(
                        thread_id=thread_id,
                        started_at_ms=int(time.time() * 1000),
                        runtime=runtime,
                    )
                self._threads[thread_id] = thread
                self._locks[thread_id] = asyncio.Lock()
            else:
                if thread.runtime != runtime:
                    raise RuntimeMismatchError(
                        thread_id=thread_id,
                        stored=thread.runtime,
                        requested=runtime,
                    )
            return thread, self._locks[thread_id]

    def get(self, thread_id: str) -> AgentThread | None:
        """Read-only lookup. Memory hit only; does not touch disk.
        Used by the request handler to decide between use-existing,
        mint-fresh, and 404. Call `exists(thread_id)` for the
        memory-or-disk check."""
        return self._threads.get(thread_id)

    def is_busy(self, thread_id: str) -> bool:
        """Non-blocking probe for "is a turn currently running on
        this thread". Chunk 3.5 uses this in `POST /agent/turn` to
        return HTTP 409 immediately when a second turn arrives
        before the first finishes, rather than silently queuing on
        the per-thread `asyncio.Lock` until release.

        `asyncio.Lock.locked()` is sync, allocation-free, and
        reliable: a `True` reading means another coroutine is
        currently inside `async with lock:` for this thread, and
        `False` means the next `async with` would acquire
        immediately. Threads we've never seen also count as
        not-busy (no lock allocated yet)."""
        lock = self._locks.get(thread_id)
        return lock is not None and lock.locked()

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
        so concurrent writes can't race with the next turn's read.

        Writes two sibling files under
        `<thread_root>/threads/<thread_id>/`:

        - `state.json`: full thread state (message_history, claims,
          bindings, per-turn tool calls, narratives_per_turn,
          runtime, archived, ...).
        - `summary.json` (chunk 4, replaces chunk-3 `runtime.json`):
          full history-row payload  thread_id, runtime, started_at,
          turn_count, title, last_user_question, archived. Read by
          `GET /agent/threads` so the list endpoint never opens
          state.json. ~200 bytes per file; same atomic-write path.

        Both files use the same `tempfile + os.replace` atomic-write
        idiom so a crash mid-write never leaves a half-baked file
        on disk.
        """
        state_path = self._state_path(thread.thread_id)
        if state_path is None:
            return
        state_path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write_json(state_path, thread.to_state_dict())
        summary_path = self._summary_path(thread.thread_id)
        if summary_path is not None:
            self._atomic_write_json(
                summary_path, _build_summary_row(thread)
            )

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict) -> None:
        """Atomic JSON write via tempfile-in-same-dir + os.replace.
        Used for both `state.json` and `runtime.json`; same code
        path so the two files have identical crash semantics."""
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.stem}-",
            suffix=".tmp",
            delete=False,
        )
        try:
            json.dump(payload, tmp)
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
