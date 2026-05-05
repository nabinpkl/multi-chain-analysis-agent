"""Pass if every `mcae.claim.emitted` span in the trace has
`mcae.claim.source_kind == source_kind`.

Today every claim is `primitive` because evidence-gathering tools
are typed primitives whose envelopes feed the structural binding
store. The `exploratory` value lights up when the planned
sql_explore tool ships; an exploratory claim is one whose evidence
includes raw SQL rows that aren't structurally verifiable.

Edge case: zero claims emitted means vacuous truth. The probe
passes with `observed.note='zero claims to check'` so cases that
intentionally exercise the no-claim path (e.g. 'who are you' turns)
do not fail. Cases that REQUIRE at least one claim should pair
this probe with a `has_matching_span` check on `mcae.claim.emitted`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from agent_service.evals.ch import ClickHouseClient
from agent_service.evals.schema import ClaimGroundedInSpec, ProbeResult


async def run(
    spec: ClaimGroundedInSpec,
    trace_id: str,
    ch: ClickHouseClient,
    *,
    run_id: str,
    case_id: str,
) -> ProbeResult:
    started = datetime.now(timezone.utc)
    try:
        rows = await ch.query(
            "SELECT SpanAttributes['mcae.claim.source_kind'] AS sk "
            "FROM otel.otel_traces "
            "WHERE TraceId = {tid:String} AND SpanName = 'mcae.claim.emitted'",
            tid=trace_id,
        )
        if not rows:
            return ProbeResult(
                run_id=run_id,
                case_id=case_id,
                probe_id=spec.probe_id,
                trace_id=trace_id,
                passed=True,
                observed={
                    "claim_count": 0,
                    "note": "zero claims to check; pair with has_matching_span if a claim is required",
                },
                started_at=started,
                finished_at=datetime.now(timezone.utc),
            )
        wrong = [r for r in rows if r["sk"] != spec.source_kind]
        passed = len(wrong) == 0
        return ProbeResult(
            run_id=run_id,
            case_id=case_id,
            probe_id=spec.probe_id,
            trace_id=trace_id,
            passed=passed,
            observed={
                "claim_count": len(rows),
                "wrong_kind_count": len(wrong),
                "expected_source_kind": spec.source_kind,
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
