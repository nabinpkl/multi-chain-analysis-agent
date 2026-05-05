"""On-disk persistence for eval run outputs.

Layout under `evals/runs/<run_id>/`:

    run.json                          # RunMetadata for the whole run
    <case_id>/<probe_id>.json         # one ProbeResult per probe per case

This shape gives us:
- One file per probe so a glob like `evals/runs/<id>/*/gate-passed-*.json`
  is enough to rebuild aggregates without parsing one giant file.
- Trivially diffable across runs (Session 6 baseline diffing).
- Self-contained: zip up `evals/runs/<run_id>/` and you have everything
  needed to reproduce an analysis.

Result writes are synchronous I/O (Python's `Path.write_text`),
intentional since runs are small (dozens of cases, hundreds of probes
at most) and synchronous keeps the call sites trivial.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from agent_service.evals.schema import (
    FrameworkAdapter,
    ProbeResult,
    RunMetadata,
)


def persist_result(r: ProbeResult, run_root: Path) -> None:
    """Write one probe result as `<run_root>/<case_id>/<probe_id>.json`.
    Creates the case directory on first write."""
    case_dir = run_root / r.case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / f"{r.probe_id}.json").write_text(
        r.model_dump_json(indent=2)
    )


def persist_run_metadata(meta: RunMetadata, run_root: Path) -> None:
    """Write the run summary at `<run_root>/run.json`."""
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "run.json").write_text(meta.model_dump_json(indent=2))


def summarize_run(
    *,
    run_id: str,
    run_root: Path,
    started_at: datetime,
    finished_at: datetime,
    git_sha: str,
    agent_version: str,
    framework_adapter: FrameworkAdapter,
) -> RunMetadata:
    """Walk `<run_root>/<case>/<probe>.json` files, count cases /
    probes / passes, return a built RunMetadata. Skips
    `<run_root>/run.json` itself if already written."""
    case_dirs = [p for p in run_root.iterdir() if p.is_dir()]
    case_count = len(case_dirs)
    probe_count = 0
    pass_count = 0
    for case_dir in case_dirs:
        for probe_file in case_dir.glob("*.json"):
            probe_count += 1
            r = ProbeResult.model_validate_json(probe_file.read_text())
            if r.passed:
                pass_count += 1
    return RunMetadata(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        git_sha=git_sha,
        agent_version=agent_version,
        framework_adapter=framework_adapter,
        case_count=case_count,
        probe_count=probe_count,
        pass_count=pass_count,
    )
