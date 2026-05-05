"""Eval CLI. Entry point for `python -m agent_service.evals <suite>`.

Prints a one-line summary on completion and exits non-zero if any
probe failed, so `just eval` integrates cleanly with shell short-
circuit operators.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import get_args

from agent_service.evals.runner import run_suite
from agent_service.evals.schema import FrameworkAdapter

_DEFAULT_BASE_URL = "http://localhost:8003"
_DEFAULT_RUNS_ROOT = Path("evals/runs")


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
        "--adapter",
        choices=list(get_args(FrameworkAdapter)),
        default="framework_free",
        help=(
            "Eval framework adapter. Only `framework_free` is wired "
            "today (dispatches probes directly). Reserved for a "
            "future adapter; see ADR 14 2026-05-05 addendum."
        ),
    )
    return parser.parse_args(argv)


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
    )

    print(
        f"{meta.run_id}: {meta.pass_count}/{meta.probe_count} probes pass "
        f"across {meta.case_count} cases  ({args.runs_root}/{meta.run_id})"
    )
    return 0 if meta.pass_count == meta.probe_count else 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
