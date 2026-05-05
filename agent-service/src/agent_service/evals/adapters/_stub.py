"""Framework-free adapter. Dispatches each probe in a case directly
to its `run` function with no eval-framework involvement.

This is what running framework-free looks like at the call-site
shape; it is also what the runner uses while the pydantic_evals
adapter is being built. Intentionally small (~30 LOC) so it stays
honest about how little the framework layer actually has to do.
"""

from __future__ import annotations

from agent_service.evals import probes
from agent_service.evals.agent_runner import AgentRun
from agent_service.evals.ch import ClickHouseClient
from agent_service.evals.schema import EvalCase, ProbeResult


async def run_case(
    case: EvalCase,
    ch: ClickHouseClient,
    *,
    run_id: str,
    agent_run: AgentRun,
) -> list[ProbeResult]:
    """Run every probe in `case` against `agent_run.trace_id`.
    Returns one ProbeResult per probe in declaration order."""
    results: list[ProbeResult] = []
    for spec in case.probes:
        runner = probes.dispatch(spec.kind)
        result = await runner(
            spec,
            agent_run.trace_id,
            ch,
            run_id=run_id,
            case_id=case.case_id,
        )
        results.append(result)
    return results
