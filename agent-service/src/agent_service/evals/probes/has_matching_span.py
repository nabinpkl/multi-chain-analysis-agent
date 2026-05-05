"""Pass if at least one span in the trace matches `span_name` and
(if `attrs` is set) every listed attribute key/value pair.

Generic primitive used by other probes too via similar SQL shapes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from agent_service.evals.ch import ClickHouseClient
from agent_service.evals.schema import HasMatchingSpanSpec, ProbeResult


async def run(
    spec: HasMatchingSpanSpec,
    trace_id: str,
    ch: ClickHouseClient,
    *,
    run_id: str,
    case_id: str,
) -> ProbeResult:
    started = datetime.now(timezone.utc)
    try:
        # Bind every attr (k, v) as two String params each. The
        # SpanAttributes Map column is keyed by string; both the key
        # lookup and the value comparison go through the typed
        # placeholder mechanism so attacker-controlled YAML cannot
        # smuggle SQL through either position.
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
        passed = matched > 0
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
