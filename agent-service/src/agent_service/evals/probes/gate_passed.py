"""Pass if the named gate (`mcae.gate.<gate_kind>`) reached
verdict='approved' and (if `version` is set) was running the
expected version.

Gate version pins were added to make this assertion possible: a
prompt swap that shifts what 'approved' means without bumping the
version is a real-world drift the spec catches.
"""

from __future__ import annotations

from datetime import datetime, timezone

from agent_service.evals.ch import ClickHouseClient
from agent_service.evals.schema import GatePassedSpec, ProbeResult


async def run(
    spec: GatePassedSpec,
    trace_id: str,
    ch: ClickHouseClient,
    *,
    run_id: str,
    case_id: str,
) -> ProbeResult:
    started = datetime.now(timezone.utc)
    try:
        span_name = f"mcae.gate.{spec.gate_kind}"
        where = (
            "TraceId = {tid:String} AND SpanName = {name:String} "
            "AND SpanAttributes['mcae.gate.verdict'] = 'approved'"
        )
        params: dict[str, str] = {"tid": trace_id, "name": span_name}
        if spec.version is not None:
            where += " AND SpanAttributes['mcae.gate.version'] = {ver:String}"
            params["ver"] = spec.version
        sql = f"SELECT count() AS n FROM otel.otel_traces WHERE {where}"
        rows = await ch.query(sql, **params)
        approved = int(rows[0]["n"]) if rows else 0
        passed = approved > 0
        return ProbeResult(
            run_id=run_id,
            case_id=case_id,
            probe_id=spec.probe_id,
            trace_id=trace_id,
            passed=passed,
            observed={
                "approved_span_count": approved,
                "span_name": span_name,
                "version_required": spec.version,
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
