"""Tests for the baseline diff, with focus on the inconclusive
state added in layer 2 of the provider-error / model-regression
disambiguation work.

The core invariants:
  - Inconclusive probes do NOT register as new_failures even if
    the baseline says they were passing.
  - Inconclusive probes do NOT register as schema deltas.
  - Inconclusive probes ARE surfaced in `report.inconclusive` so
    the operator sees them.
  - is_clean is True iff there are no new failures, no newly
    passing, and no schema deltas (inconclusive does NOT block
    cleanliness).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_service.evals.baseline import (
    Baseline,
    diff_against_baseline,
    persist_baseline,
    render_report,
)
from agent_service.evals.schema import ProbeResult


def _write_probe(
    run_root: Path,
    *,
    case_id: str,
    probe_id: str,
    passed: bool,
    inconclusive: bool = False,
    error: str | None = None,
) -> None:
    case_dir = run_root / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    r = ProbeResult(
        run_id="r",
        case_id=case_id,
        probe_id=probe_id,
        trace_id="t",
        passed=passed,
        inconclusive=inconclusive,
        error=error,
        started_at=now,
        finished_at=now,
    )
    (case_dir / f"{probe_id}.json").write_text(r.model_dump_json(indent=2))


def _baseline_with(suite: str, results: dict[str, dict[str, str]]) -> Baseline:
    return Baseline(
        suite=suite,
        captured_at=datetime.now(timezone.utc),
        git_sha="sha",
        agent_version="0.1.0",
        results=results,  # type: ignore[arg-type]
    )


def test_inconclusive_does_not_register_as_new_failure(tmp_path: Path) -> None:
    """The motivating case: baseline says probe should pass, run
    reports it as inconclusive (suppressed by infra-health). The
    diff must NOT call this a regression."""
    _write_probe(tmp_path, case_id="c1", probe_id="p1", passed=False, inconclusive=True)
    _write_probe(tmp_path, case_id="c1", probe_id="p2", passed=True)
    baseline = _baseline_with("s", {"c1": {"p1": "pass", "p2": "pass"}})

    report = diff_against_baseline(baseline, tmp_path)

    assert report.new_failures == []
    assert report.is_clean is True
    assert ("c1", "p1", "") in report.inconclusive


def test_inconclusive_does_not_register_as_schema_delta(tmp_path: Path) -> None:
    """An inconclusive probe IS in the YAML; it just had no decided
    outcome this run. It must NOT show as 'removed' in the schema-
    delta list (which would tell the operator the suite shape
    changed when in fact only the run state changed)."""
    _write_probe(tmp_path, case_id="c1", probe_id="p1", passed=False, inconclusive=True)
    baseline = _baseline_with("s", {"c1": {"p1": "pass"}})

    report = diff_against_baseline(baseline, tmp_path)

    assert report.schema_deltas == []
    assert report.is_clean is True


def test_real_failure_still_registers_when_some_probes_inconclusive(
    tmp_path: Path,
) -> None:
    """One probe is inconclusive (provider flake), another one
    legitimately failed (real regression). The real failure must
    still be reported; the inconclusive must not mask it."""
    _write_probe(tmp_path, case_id="c1", probe_id="p1", passed=False, inconclusive=True)
    _write_probe(tmp_path, case_id="c1", probe_id="p2", passed=False)  # real regression
    baseline = _baseline_with("s", {"c1": {"p1": "pass", "p2": "pass"}})

    report = diff_against_baseline(baseline, tmp_path)

    assert report.new_failures == [("c1", "p2")]
    assert report.is_clean is False
    assert len(report.inconclusive) == 1


def test_render_report_shows_inconclusive_section(tmp_path: Path) -> None:
    _write_probe(
        tmp_path,
        case_id="c1",
        probe_id="p1",
        passed=False,
        inconclusive=True,
        error="(suppressed by infra-health: UnexpectedModelBehavior on agent run)",
    )
    baseline = _baseline_with("s", {"c1": {"p1": "pass"}})

    report = diff_against_baseline(baseline, tmp_path)
    text = render_report(report)

    assert "inconclusive" in text.lower()
    assert "SKIP" in text
    assert "UnexpectedModelBehavior" in text


def test_clean_run_with_no_inconclusive_renders_nothing(tmp_path: Path) -> None:
    _write_probe(tmp_path, case_id="c1", probe_id="p1", passed=True)
    baseline = _baseline_with("s", {"c1": {"p1": "pass"}})

    report = diff_against_baseline(baseline, tmp_path)

    assert report.is_clean is True
    assert report.inconclusive == []
    assert render_report(report) == ""


def test_persist_baseline_round_trip(tmp_path: Path) -> None:
    """persist_baseline + load via Baseline.model_validate_json
    should round-trip cleanly. Catches regressions where the JSON
    shape stops matching the model (extra trailing newline, sort
    behavior, etc)."""
    b = _baseline_with(
        "s",
        {
            "case_z": {"probe_b": "pass", "probe_a": "fail"},
            "case_a": {"probe_x": "pass"},
        },
    )
    out = tmp_path / "b.json"
    persist_baseline(b, out)
    loaded = Baseline.model_validate_json(out.read_text())
    # Inner dicts and outer dict are sorted by persist_baseline for
    # stable git diffs. Verify by inspecting keys order in raw JSON.
    raw = json.loads(out.read_text())
    assert list(raw["results"].keys()) == ["case_a", "case_z"]
    assert list(raw["results"]["case_z"].keys()) == ["probe_a", "probe_b"]
    assert loaded.results == b.results
