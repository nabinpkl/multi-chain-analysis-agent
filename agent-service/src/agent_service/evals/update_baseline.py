"""Mint or refresh a suite's regression baseline.

Reads the most recent run directory under `evals/runs/`, builds a
`Baseline` from it, writes it to `evals/baselines/<suite_stem>.json`.
The matching run is selected by walking case directories and
asserting the first ProbeResult's case_id maps to a case in the
target suite YAML; this lets you keep multiple suites' runs in the
same `runs/` directory without cross-contamination.

Refuses to overwrite when the source run has any failed probe,
unless `--force` is given. The escape hatch is for the philosophy-
2 case where we deliberately want to lock in a known-failing
contract (the regression net then catches "stopped failing" as
much as "started failing").
"""

from __future__ import annotations

import argparse
import sys
from importlib.metadata import version as pkg_version
from pathlib import Path

from agent_service.evals.baseline import (
    build_baseline_from_run,
    persist_baseline,
)
from agent_service.evals.runner import _git_sha, load_suite

_DEFAULT_RUNS_ROOT = Path("evals/runs")
_DEFAULT_BASELINES_ROOT = Path("evals/baselines")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m agent_service.evals.update_baseline",
        description=(
            "Refresh a suite's committed baseline from the latest run. "
            "Run `just eval <suite>` first; this CLI consumes the run "
            "artifacts."
        ),
    )
    parser.add_argument(
        "suite",
        type=Path,
        help="Path to the suite YAML whose baseline you want to refresh.",
    )
    parser.add_argument(
        "--runs-root",
        type=Path,
        default=_DEFAULT_RUNS_ROOT,
        help=(
            "Directory holding per-run output dirs "
            f"(default: {_DEFAULT_RUNS_ROOT})."
        ),
    )
    parser.add_argument(
        "--baselines-root",
        type=Path,
        default=_DEFAULT_BASELINES_ROOT,
        help=(
            "Directory holding committed baseline files "
            f"(default: {_DEFAULT_BASELINES_ROOT})."
        ),
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help=(
            "Source the baseline from a specific run id rather than "
            "the most recent matching run. Use when the latest run "
            "is unrepresentative (e.g. transient flake) and an "
            "earlier run captured the contract you want to lock in."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Refresh even when the source run has failing probes. "
            "Use for philosophy-2 cases where the contract is to "
            "lock in known-failing probes so future passes register "
            "as deltas requiring acknowledgement."
        ),
    )
    return parser.parse_args(argv)


def _latest_run_for_suite(runs_root: Path, expected_case_ids: set[str]) -> Path:
    """Pick the most recently modified run dir whose case dirs
    intersect the suite's case ids. Mtime ordering is enough; run
    ids are non-sortable random hex (per `_make_run_id`'s
    docstring), so we cannot lexicographic-sort to find the latest.

    Raises FileNotFoundError if no matching run is found.
    """
    candidates = sorted(
        (p for p in runs_root.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for run_dir in candidates:
        run_case_ids = {p.name for p in run_dir.iterdir() if p.is_dir()}
        if run_case_ids & expected_case_ids:
            return run_dir
    raise FileNotFoundError(
        f"no run under {runs_root} contains any of cases {sorted(expected_case_ids)}; "
        f"run `just eval {expected_case_ids}` first."
    )


def _has_failures(run_root: Path) -> bool:
    """Check `run.json` for the cheapest failure signal. Avoids re-
    walking every probe file."""
    from agent_service.evals.schema import RunMetadata

    meta_path = run_root / "run.json"
    if not meta_path.exists():
        return True  # treat missing summary as conservative fail
    meta = RunMetadata.model_validate_json(meta_path.read_text())
    return meta.pass_count < meta.probe_count


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    cases = load_suite(args.suite)
    expected_case_ids = {c.case_id for c in cases}
    suite_name = cases[0].suite

    if args.run_id:
        run_root = args.runs_root / args.run_id
        if not run_root.is_dir():
            print(f"run id {args.run_id} not found under {args.runs_root}", file=sys.stderr)
            return 1
    else:
        try:
            run_root = _latest_run_for_suite(args.runs_root, expected_case_ids)
        except FileNotFoundError as e:
            print(str(e), file=sys.stderr)
            return 1

    if _has_failures(run_root) and not args.force:
        print(
            f"refusing to lock in {run_root.name}: it has failing probes. "
            "Pass --force to lock anyway (philosophy-2 contracts).",
            file=sys.stderr,
        )
        return 1

    try:
        agent_version = pkg_version("agent-service")
    except Exception:
        agent_version = "unknown"

    baseline = build_baseline_from_run(
        suite=suite_name,
        run_root=run_root,
        git_sha=_git_sha(),
        agent_version=agent_version,
    )
    out_path = args.baselines_root / f"{args.suite.stem}.json"
    persist_baseline(baseline, out_path)
    print(f"wrote {out_path} from run {run_root.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
