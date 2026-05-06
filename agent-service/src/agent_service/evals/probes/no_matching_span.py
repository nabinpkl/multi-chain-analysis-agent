"""Pass if NO span by `span_name` (and, if `attrs` is set, with all
listed attribute key/value matches) exists in the trace.

Direct mirror of `has_matching_span` with the assertion inverted.
Used by switches-off cases to pin "this code path did NOT run":
e.g. `stayInRole=false` should leave `mcae.gate.constitution` and
`mcae.gate.narrative_constitution` unemitted; without a probe that
asserts absence, the case has no way to prove the switch did
anything beyond "narrative still appears".
"""

from __future__ import annotations

from datetime import datetime, timezone

from agent_service.evals.ch import ClickHouseClient
from agent_service.evals.schema import NoMatchingSpanSpec, ProbeResult


async def run(
    spec: NoMatchingSpanSpec,
    trace_id: str,
    ch: ClickHouseClient,
    *,
    run_id: str,
    case_id: str,
) -> ProbeResult:
    started = datetime.now(timezone.utc)
    try:
        where_clauses = ["TraceId = {tid:String}", "SpanName = {name:String}"]
        params: dict[str, str] = {"tid": trace_id, "name": spec.span_name}
        for i, (k, v) in enumerate(spec.attrs.items()):
            params[f"k{i}"] = k
            params[f"v{i}"] = v
            where_clauses.append(
                f"SpanAttributes[{{k{i}:String}}] = {{v{i}:String}}"
            )
        sql = (
            "SELECT count() AS n FROM otel.otel_traces WHERE "
            + " AND ".join(where_clauses)
        )
        rows = await ch.query(sql, **params)
        matched = int(rows[0]["n"]) if rows else 0
        passed = matched == 0
        return ProbeResult(
            run_id=run_id,
            case_id=case_id,
            probe_id=spec.probe_id,
            trace_id=trace_id,
            passed=passed,
            observed={"matched_span_count": matched},
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
