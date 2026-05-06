"""Pass if the `mcae.turn` root span's named attribute equals
the expected string value.

Reads exactly one attribute from exactly one span (the turn root)
and compares against a string literal. Useful for asserting
turn-scope invariants the agent already records: tool-call count,
claims emitted, claims approved, narrative length, run.type. These
attrs are emitted on every turn (set in `loop_driver.py` when the
turn span is built), so the probe doesn't depend on which path
through the agent the case exercises.

ClickHouse stores SpanAttributes as `Map(LowCardinality(String),
String)`, so all values come back as strings. The probe takes
`expected` as a string and case authors write integer attrs as
their string form ('0', '3'). This is a contract decision: probes
that compare numbers (latency thresholds, percentile bounds) are
typed in the integer domain via dedicated probes
(`span_latency_p50_under`); a generic equality probe stays in the
string domain so its semantics are unambiguous.
"""

from __future__ import annotations

from datetime import datetime, timezone

from agent_service.evals.ch import ClickHouseClient
from agent_service.evals.schema import ProbeResult, TurnAttributeEqualsSpec


async def run(
    spec: TurnAttributeEqualsSpec,
    trace_id: str,
    ch: ClickHouseClient,
    *,
    run_id: str,
    case_id: str,
) -> ProbeResult:
    started = datetime.now(timezone.utc)
    try:
        rows = await ch.query(
            "SELECT SpanAttributes[{attr:String}] AS v "
            "FROM otel.otel_traces "
            "WHERE TraceId = {tid:String} AND SpanName = 'mcae.turn' "
            "ORDER BY Timestamp ASC "
            "LIMIT 1",
            tid=trace_id,
            attr=spec.attr,
        )
        if not rows:
            return ProbeResult(
                run_id=run_id,
                case_id=case_id,
                probe_id=spec.probe_id,
                trace_id=trace_id,
                passed=False,
                observed={"attr": spec.attr, "expected": spec.expected},
                error=(
                    "no mcae.turn span found for trace; either the "
                    "trace did not produce a turn root (case never "
                    "ran the loop) or it has not flushed to CH yet "
                    "(wait_for_trace_indexed should have caught this)"
                ),
                started_at=started,
                finished_at=datetime.now(timezone.utc),
            )
        actual = rows[0]["v"] or ""
        passed = actual == spec.expected
        return ProbeResult(
            run_id=run_id,
            case_id=case_id,
            probe_id=spec.probe_id,
            trace_id=trace_id,
            passed=passed,
            observed={
                "attr": spec.attr,
                "expected": spec.expected,
                "actual": actual,
            },
            started_at=started,
            finished_at=datetime.now(timezone.utc),
        )
    except Exception as e:  # noqa: BLE001
        return ProbeResult(
            run_id=run_id,
            case_id=case_id,
            probe_id=spec.probe_id,
            trace_id=trace_id,
            passed=False,
            error=f"probe error: {type(e).__name__}: {str(e)[:200]}",
            started_at=started,
            finished_at=datetime.now(timezone.utc),
        )
