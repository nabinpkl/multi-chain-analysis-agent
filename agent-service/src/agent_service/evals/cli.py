"""Eval CLI. Entry point for `python -m agent_service.evals <suite>`.

After the run completes, the CLI loads the matching baseline file
(default lookup: `<baselines_root>/<suite_stem>.json`) and diffs
the run's pass/fail map against it. Any non-empty delta (new
failures, newly passing, schema changes) prints a report and
exits non-zero. `--no-baseline` skips the diff for ad-hoc runs.

Exit codes:
  0  Run completed AND baseline diff is clean (or --no-baseline).
  1  Run completed but at least one probe failed AND no baseline
     to compare against (first-run case for a fresh suite, or
     --no-baseline). Exit non-zero so shells short-circuit.
  2  Run completed but baseline diff is non-empty (regression or
     unacknowledged drift). The probe-level pass/fail counts may
     even be unchanged (e.g. one new pass + one new fail), so
     having a distinct exit code from 1 lets shell automation
     differentiate "run failed" from "run drifted".
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import get_args

from agent_service.evals.baseline import (
    diff_against_baseline,
    load_baseline,
    render_report,
)
from agent_service.evals.runner import run_suite
from agent_service.evals.schema import FrameworkAdapter

_DEFAULT_BASE_URL = "http://localhost:8003"
_DEFAULT_RUNS_ROOT = Path("evals/runs")
_DEFAULT_BASELINES_ROOT = Path("evals/baselines")
_DEFAULT_MOCK_SETUP_URL = "http://localhost:8005"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m agent_service.evals",
        description="Run an eval suite against a running agent service.",
    )
    parser.add_argument(
        "suite",
        type=Path,
        help="Path to a YAML file containing a list of EvalCase dicts.",
    )
    parser.add_argument(
        "--base-url",
        default=_DEFAULT_BASE_URL,
        help=f"Agent service base URL (default: {_DEFAULT_BASE_URL}).",
    )
    parser.add_argument(
        "--runs-root",
        type=Path,
        default=_DEFAULT_RUNS_ROOT,
        help=(
            "Directory under which per-run output dirs are written "
            f"(default: {_DEFAULT_RUNS_ROOT})."
        ),
    )
    parser.add_argument(
        "--baselines-root",
        type=Path,
        default=_DEFAULT_BASELINES_ROOT,
        help=(
            "Directory holding committed baseline JSON files "
            f"(default: {_DEFAULT_BASELINES_ROOT}). The baseline for "
            "a suite is looked up at <baselines-root>/<suite-stem>.json."
        ),
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help=(
            "Override the default baseline lookup with an explicit "
            "path. Useful when comparing a run against a baseline "
            "from a different suite or a saved snapshot."
        ),
    )
    parser.add_argument(
        "--no-baseline",
        action="store_true",
        help=(
            "Skip the baseline diff entirely. Use for ad-hoc runs "
            "or while iterating on probes before locking the first "
            "baseline. Exit code reflects raw probe pass/fail only."
        ),
    )
    parser.add_argument(
        "--adapter",
        choices=list(get_args(FrameworkAdapter)),
        default="framework_free",
        help=(
            "Eval framework adapter. Only `framework_free` is wired "
            "today (dispatches probes directly). Reserved for a "
            "future adapter; see ADR 14 2026-05-05 addendum."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("live", "hermetic"),
        default="live",
        help=(
            "Eval substrate mode. `live` (default) runs against the "
            "production agent-service + Rust data plane, hitting real "
            "Solana mainnet / ClickHouse / Gemini APIs. `hermetic` "
            "runs against the `agent-service-eval` sibling container "
            "(port 8013) which routes its data-plane calls to the "
            "mock substrate at `--mock-setup-url`. Hermetic cases "
            "live under `evals/cases-hermetic/` and use `fixtures:` "
            "to make tool responses deterministic."
        ),
    )
    parser.add_argument(
        "--mock-setup-url",
        default=_DEFAULT_MOCK_SETUP_URL,
        help=(
            "Hermetic mode: URL of the mock substrate's eval-runner "
            "control surface (POST/DELETE `/eval/setup`). The runner "
            "POSTs each case's `fixtures:` field before invoking the "
            "agent and DELETEs after. Ignored in live mode."
        ),
    )
    return parser.parse_args(argv)


def _resolve_baseline_path(args: argparse.Namespace) -> Path:
    if args.baseline is not None:
        return args.baseline
    return args.baselines_root / f"{args.suite.stem}.json"


async def _run(args: argparse.Namespace) -> int:
    try:
        agent_version = pkg_version("agent-service")
    except Exception:
        agent_version = "unknown"

    meta = await run_suite(
        args.suite,
        runs_root=args.runs_root,
        base_url=args.base_url,
        framework_adapter=args.adapter,
        agent_version=agent_version,
        mock_setup_url=args.mock_setup_url if args.mode == "hermetic" else None,
    )

    inconclusive_suffix = (
        f", {meta.inconclusive_count} inconclusive"
        if meta.inconclusive_count
        else ""
    )
    decided = meta.probe_count - meta.inconclusive_count
    print(
        f"{meta.run_id}: {meta.pass_count}/{decided} decided probes pass "
        f"across {meta.case_count} cases{inconclusive_suffix}  "
        f"({args.runs_root}/{meta.run_id})"
    )

    if args.no_baseline:
        return 0 if meta.pass_count == decided else 1

    baseline_path = _resolve_baseline_path(args)
    baseline = load_baseline(baseline_path)
    if baseline is None:
        print(
            f"no baseline at {baseline_path}; mint one with "
            f"`just eval-baseline {args.suite}` once the run looks right."
        )
        return 0 if meta.pass_count == decided else 1

    run_root = args.runs_root / meta.run_id
    report = diff_against_baseline(baseline, run_root)
    rendered = render_report(report)
    if rendered:
        print(rendered)
    if report.is_clean:
        # Print the explicit "clean" line only when there are no
        # inconclusive entries to surface; otherwise the report
        # body has already covered the operator-facing detail.
        if not report.inconclusive:
            print(f"baseline diff clean against {baseline_path}.")
        return 0
    print(
        f"\nbaseline drift detected. If intended, refresh with "
        f"`just eval-baseline {args.suite}`."
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
