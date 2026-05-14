"""Eval suite runner. Orchestrates the four moving pieces:

1. Load a YAML suite into typed `EvalCase` objects.
2. For each case, invoke the agent over HTTP with `runType=eval`
   and capture the OTel trace id from the AgentDone frame.
3. Dispatch every probe in the case against that trace id via the
   `framework_free` adapter (the only one wired; ADR 14 2026-05-05
   addendum explains why we didn't end up with a framework on top).
4. Persist every ProbeResult and a RunMetadata summary under
   `evals/runs/<run_id>/`.

Concurrency: cases run sequentially. Probes within a case run
sequentially too. Both are intentional given our scale (handful to
dozens of cases per suite). Adding concurrency requires a
"rate-limit per provider" answer first; deferred.
"""

from __future__ import annotations

import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

import httpx
from ruamel.yaml import YAML

from agent_service.evals.adapters import _stub
from agent_service.evals.agent_runner import (
    AgentRun,
    invoke_agent_get_trace_id,
    wait_for_trace_indexed,
)
from agent_service.evals.ch import ClickHouseClient
from agent_service.evals.infra_health import has_terminal_provider_failure
from agent_service.evals.persist import (
    persist_result,
    persist_run_metadata,
    summarize_run,
)
from agent_service.evals.schema import (
    EvalCase,
    FrameworkAdapter,
    ProbeResult,
    RunMetadata,
)

_YAML = YAML(typ="safe")

# Adapter callable shape. Each adapter module exposes a `run_case`
# coroutine matching this signature; the runner picks one and calls
# it the same way regardless of which framework (or none) backs it.
AdapterRunCase = Callable[..., Awaitable[list[ProbeResult]]]


def load_suite(suite_path: Path) -> list[EvalCase]:
    """Read a YAML file containing a list of case dicts, validate
    each as EvalCase. Bad cases raise pydantic ValidationError with
    a precise location; we let it propagate to the CLI so a
    malformed suite fails fast before the agent is invoked."""
    raw = _YAML.load(suite_path.read_text())
    if not isinstance(raw, list):
        raise ValueError(
            f"suite {suite_path} must be a YAML list of case dicts; "
            f"got {type(raw).__name__}"
        )
    return [EvalCase.model_validate(c) for c in raw]


def _select_adapter(name: FrameworkAdapter) -> AdapterRunCase:
    # Single-arm dispatch today. Stays as a function (not inlined)
    # so adding a future adapter is a one-line `elif` against the
    # `name` Literal, matched by the type checker.
    if name == "framework_free":
        return _stub.run_case
    raise AssertionError(f"unreachable adapter value: {name!r}")


def _make_run_id() -> str:
    """Random hex, URL-safe. UUID4 truncated to 16 chars (64 bits of
    entropy) is enough to make collisions vanishingly unlikely
    across the lifetime of a personal-scale eval workflow.

    NOT sortable. Listing `evals/runs/` in name order does NOT
    yield chronological order. Use file mtime (Path.stat().st_mtime)
    or RunMetadata.started_at when ordering matters. Switching to
    a sortable id (ULID) is fine if it ever does, but the library
    must pass the AGENTS.md maintenance bar at the time of adoption.
    """
    return uuid.uuid4().hex[:16]


def _git_sha() -> str:
    """Current HEAD sha, or 'unknown' if not in a git checkout."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"


async def run_suite(
    suite_path: Path,
    *,
    runs_root: Path,
    base_url: str,
    framework_adapter: FrameworkAdapter,
    agent_version: str,
    mock_setup_url: str | None = None,
) -> RunMetadata:
    """Run every case in the suite, persist results, return a
    RunMetadata summary. The summary is also persisted as
    `<runs_root>/<run_id>/run.json`.

    The summary + run.json ALWAYS land, even if the case loop
    raises mid-suite (agent service down, trace timeout, malformed
    case, etc.). Partial runs report the cases that completed; the
    error is re-raised after persistence so the caller still sees
    it. This way `evals/runs/<run_id>/` is never silently
    incomplete; a regression diff against a partial run shows
    "got through 3 of 5" rather than "no run.json, mystery."

    `mock_setup_url` triggers hermetic mode: before each case the
    runner POSTs `case.fixtures` to `{mock_setup_url}/eval/setup`,
    after each case it DELETEs. When None (default), the runner is
    in live mode and the fixtures field is unused.
    """
    cases = load_suite(suite_path)
    run_id = _make_run_id()
    run_root = runs_root / run_id
    run_root.mkdir(parents=True, exist_ok=True)

    started = datetime.now(timezone.utc)
    adapter = _select_adapter(framework_adapter)

    case_loop_error: BaseException | None = None
    ch = await ClickHouseClient.connect()
    try:
        async with httpx.AsyncClient(timeout=120) as http:
            for case in cases:
                if mock_setup_url is not None:
                    fixtures_body = (
                        case.fixtures.model_dump_json()
                        if case.fixtures is not None
                        else "{}"
                    )
                    setup_resp = await http.post(
                        f"{mock_setup_url}/eval/setup",
                        content=fixtures_body,
                        headers={"Content-Type": "application/json"},
                    )
                    if setup_resp.status_code >= 400:
                        raise RuntimeError(
                            f"mock /eval/setup rejected fixtures for "
                            f"case {case.case_id}: HTTP "
                            f"{setup_resp.status_code} {setup_resp.text}"
                        )
                try:
                    agent_run: AgentRun = await invoke_agent_get_trace_id(
                        case.inputs,
                        base_url=base_url,
                        http=http,
                    )
                finally:
                    if mock_setup_url is not None:
                        try:
                            await http.delete(f"{mock_setup_url}/eval/setup")
                        except Exception as e:  # noqa: BLE001
                            # Don't let a teardown blip break the case
                            # loop; next case's setup will reseed.
                            import structlog
                            structlog.get_logger(__name__).warning(
                                "mock_eval_clear_failed",
                                case_id=case.case_id,
                                error=str(e),
                            )
                # Wait for the OTel pipeline to flush the turn's
                # spans into ClickHouse before probes query them.
                # Without this, probes that fired right after
                # AgentDone returned see zero matching spans.
                await wait_for_trace_indexed(agent_run.trace_id, ch)
                results = await adapter(
                    case, ch, run_id=run_id, agent_run=agent_run
                )
                # If the trace had a terminal provider failure (the
                # agent loop itself errored mid-turn, not a normal
                # completion the probes evaluate), flip any failing
                # probe to inconclusive=True. Probes that passed
                # despite the failure stay as pass: their assertion
                # held against whatever spans did emit, so the signal
                # is real. Only the failures need disambiguation,
                # because we cannot tell whether they would have
                # passed had the agent completed normally.
                terminal, summary = await has_terminal_provider_failure(
                    agent_run.trace_id, ch
                )
                if terminal:
                    suffix = (
                        f" (suppressed by infra-health: {summary})"
                        if summary
                        else " (suppressed by infra-health)"
                    )
                    results = [
                        r.model_copy(
                            update={
                                "inconclusive": True,
                                "error": (r.error or "") + suffix,
                            }
                        )
                        if not r.passed
                        else r
                        for r in results
                    ]
                for r in results:
                    persist_result(r, run_root)
    except BaseException as e:
        # Capture and continue to summarization so the partial run
        # state is on disk; re-raise after persistence below.
        case_loop_error = e
    finally:
        await ch.aclose()

    finished = datetime.now(timezone.utc)
    meta = summarize_run(
        run_id=run_id,
        run_root=run_root,
        started_at=started,
        finished_at=finished,
        git_sha=_git_sha(),
        agent_version=agent_version,
        framework_adapter=framework_adapter,
    )
    persist_run_metadata(meta, run_root)
    if case_loop_error is not None:
        raise case_loop_error
    return meta
