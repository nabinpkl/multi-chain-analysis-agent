"""Pass if a tool by `tool_name` was invoked with arguments
matching every key/value pair in `arg_predicates`.

Reads two attributes:
- `gen_ai.tool.name` on pydantic_ai-emitted `running tool` spans
  identifies the tool. Used for the WHERE filter.
- `mcae.primitive.input` on the corresponding `mcae.primitive.<name>`
  span carries the canonical-JSON-encoded proto request. We parse
  and check predicates against parsed fields.

Predicates compare equality only. A predicate `addr: "ABC"` passes
if the parsed JSON has `addr == "ABC"` (camelCase per our wire
format). Nested or operator-style predicates are intentionally not
supported; if cases need them, model them as a new ProbeKind rather
than overloading this one.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from agent_service.evals.ch import ClickHouseClient
from agent_service.evals.schema import ProbeResult, ToolCalledWithArgsSpec


def _all_predicates_match(parsed_input: dict[str, Any], predicates: dict[str, Any]) -> bool:
    """Top-level equality check; the proto canonical-JSON encoding
    keeps wire-format camelCase field names."""
    return all(parsed_input.get(k) == v for k, v in predicates.items())


async def run(
    spec: ToolCalledWithArgsSpec,
    trace_id: str,
    ch: ClickHouseClient,
    *,
    run_id: str,
    case_id: str,
) -> ProbeResult:
    started = datetime.now(timezone.utc)
    try:
        # Read primitive spans because they carry mcae.primitive.input
        # (the full JSON), not the pydantic_ai-emitted `running tool`
        # span which only has the tool name + call id.
        sql = (
            "SELECT SpanAttributes['mcae.primitive.input'] AS input_json "
            "FROM otel.otel_traces "
            "WHERE TraceId = {tid:String} AND SpanName = {span_name:String}"
        )
        primitive_span = f"mcae.primitive.{spec.tool_name}"
        rows = await ch.query(sql, tid=trace_id, span_name=primitive_span)

        matching_calls: list[dict[str, Any]] = []
        for row in rows:
            raw = row["input_json"]
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                # Truncated payloads (mcae.primitive.input is capped
                # at 8 KiB by primitive_client._proto_to_capped_json)
                # may not parse cleanly; treat as non-matching rather
                # than failing the whole probe.
                continue
            if _all_predicates_match(parsed, spec.arg_predicates):
                matching_calls.append(parsed)

        passed = len(matching_calls) > 0
        return ProbeResult(
            run_id=run_id,
            case_id=case_id,
            probe_id=spec.probe_id,
            trace_id=trace_id,
            passed=passed,
            observed={
                "primitive_span": primitive_span,
                "total_calls": len(rows),
                "matching_calls": len(matching_calls),
                "predicates": spec.arg_predicates,
            },
            started_at=started,
            finished_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        return ProbeResult(
            run_id=run_id,
            case_id=case_id,
            probe_id=spec.probe_id,
            trace_id=trace_id,
            passed=False,
            error=f"probe error: {e}",
            started_at=started,
            finished_at=datetime.now(timezone.utc),
        )
