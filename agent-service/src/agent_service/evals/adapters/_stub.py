"""Framework-free adapter. Dispatches each probe in a case directly
to its `run` function with no eval-framework involvement.

This is the only adapter wired today. ADR 14's 2026-05-05 addendum
records why a pydantic_evals adapter, originally planned as Layer
4, was dropped (its `SpanQuery` primitive requires in-process span
capture, incompatible with our cross-process OTel → CH pipeline).
Intentionally small (~30 LOC) so it stays honest about how little
the framework layer actually has to do.
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
