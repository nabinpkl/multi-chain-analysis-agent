"""Pass if any LLM call in the trace used `model_name`.

Reads `gen_ai.request.model` from pydantic_ai-emitted `chat <model>`
spans (OTel GenAI semconv). Useful for asserting eval traffic used
the expected primary or policy model rather than silently falling
back when one is throttled.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..ch import ClickHouseClient
from ..schema import LlmCallUsedModelSpec, ProbeResult


async def run(
    spec: LlmCallUsedModelSpec,
    trace_id: str,
    ch: ClickHouseClient,
    *,
    run_id: str,
    case_id: str,
) -> ProbeResult:
    started = datetime.now(timezone.utc)
    try:
        rows = await ch.query(
            "SELECT count() AS n FROM otel.otel_traces "
            "WHERE TraceId = {tid:String} "
            "AND SpanAttributes['gen_ai.request.model'] = {model:String}",
            tid=trace_id,
            model=spec.model_name,
        )
        matched = int(rows[0]["n"]) if rows else 0
        passed = matched > 0
        return ProbeResult(
            run_id=run_id,
            case_id=case_id,
            probe_id=spec.probe_id,
            trace_id=trace_id,
            passed=passed,
            observed={"matched_call_count": matched, "expected_model": spec.model_name},
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
