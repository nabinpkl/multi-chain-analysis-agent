"""Pass if the slowest LLM call or tool call in the trace is under
`ms` milliseconds. On failure, `observed` names the offender so the
case author sees *which* model or tool stalled.

pydantic_ai's GenAI semconv emits one span per LLM hop and per tool
call. We match by SpanName pattern and pull the identity attribute
appropriate to the family:

- LLM hops: SpanName starts with `chat `; identity is
  `SpanAttributes['gen_ai.request.model']`.
- Tool calls: SpanName starts with `running tool`; identity is
  `SpanAttributes['gen_ai.tool.name']`.

ClickHouse `Duration` is in nanoseconds; we convert to ms after the
aggregate so the comparison reads naturally against the spec field.

Edge cases:

- Zero matching spans: probe error (`no matching <kind> spans found`).
  A `tool` probe against a refusal case is the obvious authoring
  mistake; vacuous pass would hide it. Use `turn_attribute_equals`
  on `mcae.turn.tool_calls=0` for "no tools called" assertions.
- The slowest span has an empty identity attribute (CH map default).
  We still report the duration; `slowest_identity` comes back as the
  empty string. Diagnostic is intentionally lenient: the duration is
  the load-bearing fact, identity is the breadcrumb.
"""

from __future__ import annotations

from datetime import datetime, timezone

from agent_service.evals.ch import ClickHouseClient
from agent_service.evals.schema import ProbeResult, SlowestCallUnderMsSpec


_SPAN_PATTERNS: dict[str, tuple[str, str]] = {
    # call_kind -> (SpanName LIKE pattern, identity attribute key)
    "llm": ("chat %", "gen_ai.request.model"),
    "tool": ("running tool%", "gen_ai.tool.name"),
}


async def run(
    spec: SlowestCallUnderMsSpec,
    trace_id: str,
    ch: ClickHouseClient,
    *,
    run_id: str,
    case_id: str,
) -> ProbeResult:
    started = datetime.now(timezone.utc)
    pattern, identity_attr = _SPAN_PATTERNS[spec.call_kind]
    try:
        rows = await ch.query(
            "SELECT SpanName, "
            "       SpanAttributes[{ident:String}] AS identity, "
            "       toFloat64(Duration) AS duration_ns "
            "FROM otel.otel_traces "
            "WHERE TraceId = {tid:String} "
            "  AND SpanName LIKE {pat:String} "
            "ORDER BY Duration DESC "
            "LIMIT 1",
            tid=trace_id,
            pat=pattern,
            ident=identity_attr,
        )
        if not rows:
            return ProbeResult(
                run_id=run_id,
                case_id=case_id,
                probe_id=spec.probe_id,
                trace_id=trace_id,
                passed=False,
                error=(
                    f"no matching {spec.call_kind} spans found "
                    f"(pattern {pattern!r}); cannot compute slowest"
                ),
                observed={"call_kind": spec.call_kind, "pattern": pattern},
                started_at=started,
                finished_at=datetime.now(timezone.utc),
            )
        row = rows[0]
        slowest_ms = float(row["duration_ns"]) / 1_000_000
        passed = slowest_ms < spec.ms
        return ProbeResult(
            run_id=run_id,
            case_id=case_id,
            probe_id=spec.probe_id,
            trace_id=trace_id,
            passed=passed,
            observed={
                "call_kind": spec.call_kind,
                "slowest_span_name": row["SpanName"],
                "slowest_identity": row["identity"],
                "slowest_ms": slowest_ms,
                "threshold_ms": spec.ms,
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
