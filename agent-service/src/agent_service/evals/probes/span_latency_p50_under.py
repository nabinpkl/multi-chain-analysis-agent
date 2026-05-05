"""Pass if the median (p50) duration across all matching spans is
under `ms` milliseconds.

OTel's `Duration` column is in nanoseconds; we convert to ms after
the aggregate. Edge case: zero matching spans means there is no p50
to compute. The probe fails with `error='no matching spans found'`
because asserting a latency target on a span that did not execute
is almost certainly a case-authoring mistake; vacuous truth here
hides bugs.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..ch import ClickHouseClient
from ..schema import ProbeResult, SpanLatencyP50UnderSpec


async def run(
    spec: SpanLatencyP50UnderSpec,
    trace_id: str,
    ch: ClickHouseClient,
    *,
    run_id: str,
    case_id: str,
) -> ProbeResult:
    started = datetime.now(timezone.utc)
    try:
        rows = await ch.query(
            "SELECT count() AS n, "
            "       quantile(0.5)(toFloat64(Duration)) AS p50_ns "
            "FROM otel.otel_traces "
            "WHERE TraceId = {tid:String} AND SpanName = {name:String}",
            tid=trace_id,
            name=spec.span_name,
        )
        n = int(rows[0]["n"]) if rows else 0
        if n == 0:
            return ProbeResult(
                run_id=run_id,
                case_id=case_id,
                probe_id=spec.probe_id,
                trace_id=trace_id,
                passed=False,
                error=(
                    f"no matching spans found for {spec.span_name!r}; "
                    "cannot compute p50"
                ),
                observed={"matched_span_count": 0},
                started_at=started,
                finished_at=datetime.now(timezone.utc),
            )
        p50_ns = float(rows[0]["p50_ns"])
        p50_ms = p50_ns / 1_000_000
        passed = p50_ms < spec.ms
        return ProbeResult(
            run_id=run_id,
            case_id=case_id,
            probe_id=spec.probe_id,
            trace_id=trace_id,
            passed=passed,
            observed={
                "matched_span_count": n,
                "p50_ms": p50_ms,
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
