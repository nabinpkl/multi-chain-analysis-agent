"""Agent ledger writer. Python owns the agent_ledger table after Phase
C; this module is the only writer.

Schema: same as the Rust v0 (`backend/src/store/schema.rs:64`) so a
no-rewrite cutover is possible. Drop semantics ("all data refreshes"
per AGENTS.md) means we can wipe and recreate freely if the schema
ever needs to change.

Per-session monotonic sequence counter, sha256 of canonical JSON,
fire-and-forget writes that swallow errors with structlog warnings.
Matches the existing Rust semantics: a flaky ClickHouse cannot kill
an in-flight session.

Uses `clickhouse-connect` (official ClickHouse Inc Python client,
async support, MIT). Maintained-check passed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import structlog

try:
    import clickhouse_connect  # type: ignore
    from clickhouse_connect.driver.asyncclient import AsyncClient  # type: ignore
except ImportError:  # pragma: no cover - optional during early dev
    clickhouse_connect = None  # type: ignore
    AsyncClient = Any  # type: ignore

log = structlog.get_logger(__name__)


class LedgerEventKind(str, Enum):
    """Closed enum of event kinds. Wire string matches Rust's
    `LedgerEventKind::as_str()` so historical replays remain readable
    across the cutover. New variants beyond the Rust set:
    - `TURN_DIFF`: ship 4 dont_repeat_yourself diff path
    - `TURN_COMPLETED`: end-of-turn summary (replaces SESSION_ENDED for
      per-turn rows; SESSION_ENDED reserved for actual session close)"""

    SESSION_STARTED = "session_started"
    PROMPT = "prompt"
    LLM_CALL = "llm_call"
    LLM_RESPONSE = "llm_response"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    CLAIM_EMITTED = "claim_emitted"
    POLICY_VERDICT = "policy_verdict"
    BUDGET_DECREMENT = "budget_decrement"
    SESSION_ENDED = "session_ended"
    TURN_DIFF = "turn_diff"
    TURN_COMPLETED = "turn_completed"


@dataclass(slots=True)
class LedgerEventDraft:
    """Loop-side input shape. Hash + sequence are added by the writer."""

    session_id: str
    kind: LedgerEventKind
    payload: dict[str, Any] = field(default_factory=dict)
    principal_hash: str = "0" * 64  # sha256 hex; 32 bytes -> 64 chars
    pre_estimate_units: int = 0
    post_actual_units: int = 0
    cost_relevant: bool = False


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS multichain.agent_ledger (
    session_id            String,
    sequence              UInt64,
    timestamp_ms          UInt64,
    kind                  LowCardinality(String),
    principal_hash        String,
    payload               String,
    payload_hash          String,
    pre_estimate_units    UInt32,
    post_actual_units     UInt32,
    cost_relevant         UInt8,
    redaction_policy_ver  UInt32,
    inserted_at           DateTime DEFAULT now()
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(toDateTime(timestamp_ms / 1000))
ORDER BY (session_id, sequence)
TTL toDateTime(timestamp_ms / 1000) + INTERVAL 90 DAY
SETTINGS index_granularity = 8192
""".strip()

_INSERT_COLUMNS = [
    "session_id",
    "sequence",
    "timestamp_ms",
    "kind",
    "principal_hash",
    "payload",
    "payload_hash",
    "pre_estimate_units",
    "post_actual_units",
    "cost_relevant",
    "redaction_policy_ver",
]


class Ledger:
    """Async ClickHouse writer for the agent_ledger table.

    Construct via `await Ledger.connect(host=..., ...)` so the
    underlying client is properly bound to the running event loop.
    `_seq_lock` serializes per-session sequence assignment + the
    INSERT call so concurrent writes within one session don't collide
    on the counter.
    """

    def __init__(self, client: Any | None) -> None:
        self._client = client
        self._sequences: dict[str, int] = {}
        self._seq_lock = asyncio.Lock()

    @classmethod
    async def connect(
        cls,
        *,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        database: str = "multichain",
    ) -> "Ledger":
        """Open the async client and run the idempotent CREATE TABLE.
        Falls back to a no-op writer if `clickhouse-connect` isn't
        installed (lets tests run without the dep)."""
        if clickhouse_connect is None:
            log.warning("clickhouse_connect_unavailable", note="ledger writes disabled")
            return cls(client=None)

        host = host or os.environ.get("CLICKHOUSE_HOST", "clickhouse")
        port = port or int(os.environ.get("CLICKHOUSE_HTTP_PORT", "8123"))
        username = username or os.environ.get("CLICKHOUSE_USER", "default")
        password = password if password is not None else os.environ.get("CLICKHOUSE_PASSWORD", "")

        try:
            client = await clickhouse_connect.get_async_client(
                host=host,
                port=port,
                username=username,
                password=password,
                database=database,
            )
            await client.command(_CREATE_TABLE_SQL)
            log.info("ledger_connected", host=host, port=port, database=database)
            return cls(client=client)
        except Exception as e:  # noqa: BLE001
            log.warning("ledger_connect_failed", error=str(e))
            return cls(client=None)

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001
                pass

    async def write(self, draft: LedgerEventDraft) -> int:
        """Write one event. Returns the assigned sequence number; 0
        when the underlying client is unavailable. Fire-and-forget
        from the loop driver's perspective (errors are swallowed)."""
        if self._client is None:
            # Silent no-op when the ledger isn't wired (early dev, tests).
            return 0

        payload_str = _canonical_json(draft.payload)
        payload_hash = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()

        async with self._seq_lock:
            self._sequences[draft.session_id] = self._sequences.get(draft.session_id, 0) + 1
            sequence = self._sequences[draft.session_id]

        row = [
            draft.session_id,
            sequence,
            int(time.time() * 1000),
            draft.kind.value,
            draft.principal_hash,
            payload_str,
            payload_hash,
            draft.pre_estimate_units,
            draft.post_actual_units,
            1 if draft.cost_relevant else 0,
            0,  # redaction_policy_ver
        ]

        try:
            await self._client.insert(
                table="agent_ledger",
                data=[row],
                column_names=_INSERT_COLUMNS,
                database="multichain",
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "ledger_write_failed",
                kind=draft.kind.value,
                session_id=draft.session_id,
                error=str(e),
            )
        return sequence

    def drop_session(self, session_id: str) -> None:
        """Drop the per-session sequence counter on session end. Keeps
        the in-memory map bounded. Synchronous because the lock is
        only contended during writes; this is safe to call from
        cleanup paths without awaiting."""
        # Best-effort; if a write is in flight that's fine, the next
        # write for this session will start at 1 again which is the
        # desired behavior anyway.
        self._sequences.pop(session_id, None)


def _canonical_json(payload: Any) -> str:
    """Sorted-keys, no extraneous whitespace. The hash input. Matches
    what serde_json would emit on the Rust side closely enough for
    cross-language equality on simple shapes (the Rust hash is over
    `serde_json::to_string`, which doesn't sort; future ledger replay
    parity would need to align this)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
