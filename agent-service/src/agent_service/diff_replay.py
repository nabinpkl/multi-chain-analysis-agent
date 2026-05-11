"""Shared repeat-detection / diff-replay path for both agent runtimes.

Chunk 3.5 lifts what was `loop_driver._run_repeat_path` (and its
`_format_changed_prose` helper) into a runtime-agnostic module so
the codex driver can drive the same path without duplicating the
diff-walker plumbing. Both drivers populate
`thread.tool_calls_per_turn` from their respective tool-call sites
(pydantic-ai: `agent.py` tool wrappers; codex: TOOL_COMPLETED
events from the JSON-RPC stream), then call this path on a turn
whose `dont_repeat_yourself` switch fires.

What lives here:

* `_frame(event, msg)`: SSE-frame builder. Same proto canonical
  JSON shape both drivers emit; lifted so the next driver doesn't
  re-implement it.
* `run_repeat_path(handles, thread, ...)`: replays the prior
  turn's tool calls against the live snapshot, diffs outputs,
  yields `NoMovement` or `ChangedSince` SSE frames.
* `format_changed_prose(changes)`: deterministic single-paragraph
  summary of what fields moved. Used both inside this module and
  re-exported for tests.

What does NOT live here:

* The repeat *detection* step (`detect_repeat`). That stays in
  `agent_service/repeat_detector.py` because it's a model call,
  not a diff walk. Each driver invokes detect first, decides to
  enter the repeat path, then calls into this module.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import structlog
from google.protobuf import json_format
from opentelemetry import trace

from agent_service import spans
from agent_service.diff import diff_outputs, spec_for
from agent_service.primitive_client import PrimitiveError
from agent_service.thread_state import AgentThread
from multichain.wire.agent.v1 import diff_pb2

if TYPE_CHECKING:
    from agent_service.loop_driver import LoopHandles

log = structlog.get_logger(__name__)
_tracer = trace.get_tracer(__name__)


def _frame(event: str, msg) -> dict[str, str]:
    """Build one SSE frame dict in the shape `EventSourceResponse`
    consumes (`{event, data}` with `data` as canonical proto JSON,
    camelCase per AGENTS.md 'wire format per hop')."""
    return {
        "event": event,
        "data": json_format.MessageToJson(
            msg, preserving_proto_field_name=False, indent=None
        ),
    }


def format_changed_prose(changes: list[diff_pb2.FieldDelta]) -> str:
    """Deterministic single-paragraph summary of what diff fields
    moved. Plain prose; no chips, no audit numbers. Public for
    unit-test access without importing private symbols."""
    if not changes:
        return "No movement since the prior turn."
    parts: list[str] = []
    for c in changes:
        case = c.change.WhichOneof("change")
        if case == "number_moved":
            n = c.change.number_moved
            parts.append(
                f"{c.field_path} moved from {n.prior:.2f} to {n.current:.2f}"
            )
        elif case == "count_changed":
            n = c.change.count_changed
            parts.append(
                f"{c.field_path} changed from {int(n.prior)} to {int(n.current)}"
            )
        elif case == "set_changed":
            s = c.change.set_changed
            added_n = len(s.added)
            removed_n = len(s.removed)
            parts.append(f"{c.field_path}: {added_n} added, {removed_n} removed")
    return "Since the prior turn: " + "; ".join(parts) + "."


async def run_repeat_path(
    *,
    handles: "LoopHandles",
    thread: AgentThread,
    repeat_of_turn: int,
    snapshot_id: str,
) -> AsyncIterator[dict[str, str]]:
    """Replay the prior turn's tool calls against the fresh
    snapshot, diff outputs, emit NoMovement or ChangedSince. No
    LLM narrative call on the empty path; ChangedSince carries
    deterministic prose listing the changed fields.

    Wrapped in a `turn.diff` span so the SQL query
    `SELECT changed_count, primitives_replayed FROM otel_traces
    WHERE SpanName='turn.diff'` answers "what shifted between
    this turn and the prior one" without replaying the loop. Per-
    primitive replays nest as `primitive.*` spans automatically.
    """
    prior_calls = thread.tool_calls_per_turn.get(repeat_of_turn, [])
    primitives_replayed: list[str] = []
    all_changed: list[diff_pb2.FieldDelta] = []
    total_unchanged = 0

    with _tracer.start_as_current_span(spans.TURN_DIFF) as diff_span:
        diff_span.set_attribute(spans.Attrs.REPEAT_OF_TURN, repeat_of_turn)
        for record in prior_calls:
            try:
                if record.primitive_name == "wallet_profile":
                    fresh = await handles.primitive_client.wallet_profile(
                        addr=record.args["addr"], snapshot_id=snapshot_id
                    )
                elif record.primitive_name == "community_summary":
                    fresh = await handles.primitive_client.community_summary(
                        community_id=record.args["community_id"],
                        snapshot_id=snapshot_id,
                    )
                else:
                    continue
            except PrimitiveError as e:
                log.warning(
                    "repeat_replay_failed",
                    primitive=record.primitive_name,
                    error=e.kind,
                )
                continue

            primitives_replayed.append(record.primitive_name)
            spec = spec_for(record.primitive_name)
            delta = diff_outputs(
                record.primitive_name, spec, record.output_value, fresh.value
            )
            all_changed.extend(delta.changed)
            total_unchanged += delta.unchanged_field_count

        diff_span.set_attribute(spans.Attrs.DIFF_CHANGED_COUNT, len(all_changed))
        diff_span.set_attribute(
            spans.Attrs.DIFF_UNCHANGED_COUNT, total_unchanged
        )
        diff_span.set_attribute(
            spans.Attrs.DIFF_PRIMITIVES_REPLAYED, primitives_replayed
        )

        if not all_changed:
            nm = diff_pb2.NoMovement(
                prior_turn=repeat_of_turn,
                primitives_replayed=primitives_replayed,
            )
            yield _frame("NoMovement", nm)
        else:
            delta = diff_pb2.Delta(
                changed=all_changed, unchanged_field_count=total_unchanged
            )
            prose = format_changed_prose(all_changed)
            cs = diff_pb2.ChangedSince(
                prior_turn=repeat_of_turn, delta=delta, prose=prose
            )
            yield _frame("ChangedSince", cs)
