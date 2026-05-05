"""Pass if no span by `span_name` carries the named status.

`error` matches our convention from primitive_client.py: setting
`SpanAttributes['error']='true'` on 4xx/5xx responses. `ok` matches
the absence of that mark (no error attribute set).
"""

from __future__ import annotations

from datetime import datetime, timezone

from agent_service.evals.ch import ClickHouseClient
from agent_service.evals.schema import NoSpanWithStatusSpec, ProbeResult


async def run(
    spec: NoSpanWithStatusSpec,
    trace_id: str,
    ch: ClickHouseClient,
    *,
    run_id: str,
    case_id: str,
) -> ProbeResult:
    started = datetime.now(timezone.utc)
    try:
        # error: count spans with the error attribute set.
        # ok:    count spans without the error attribute set.
        # Both go through SpanAttributes Map lookup; same safe
        # binding pattern as has_matching_span.
        if spec.status == "error":
            where = (
                "TraceId = {tid:String} AND SpanName = {name:String} "
                "AND SpanAttributes['error'] = 'true'"
            )
        else:
            where = (
                "TraceId = {tid:String} AND SpanName = {name:String} "
                "AND SpanAttributes['error'] != 'true'"
            )
        sql = f"SELECT count() AS n FROM otel.otel_traces WHERE {where}"
        rows = await ch.query(sql, tid=trace_id, name=spec.span_name)
        offending = int(rows[0]["n"]) if rows else 0
        passed = offending == 0
        return ProbeResult(
            run_id=run_id,
            case_id=case_id,
            probe_id=spec.probe_id,
            trace_id=trace_id,
            passed=passed,
            observed={"matched_span_count": offending, "status_filter": spec.status},
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
