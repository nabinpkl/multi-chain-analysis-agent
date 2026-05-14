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


def test_model_delta_surfaces_when_judge_swap_changes_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the eval-judge model in env differs from what the
    baseline recorded, the diff surfaces it as a model_delta. Not a
    regression event; explanatory signal that helps the operator
    attribute probe flips below to a model swap."""
    _write_probe(tmp_path, case_id="c1", probe_id="p1", passed=True)
    baseline = Baseline(
        suite="s",
        captured_at=datetime.now(timezone.utc),
        git_sha="sha",
        agent_version="0.1.0",
        agent_primary_model="nvidia/nemotron-3-super-120b-a12b:free",
        agent_policy_model="openai/gpt-oss-20b:free",
        eval_judge_model="openrouter/owl-alpha",
        results={"c1": {"p1": "pass"}},
    )

    # Operator swaps the judge in .env between mint and run.
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "qwen/qwen-2.5-72b-instruct:free")
    monkeypatch.setenv("AGENT_PRIMARY_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
    monkeypatch.setenv("AGENT_POLICY_MODEL", "openai/gpt-oss-20b:free")

    report = diff_against_baseline(baseline, tmp_path)
    assert report.is_clean is True  # probes match; no regression
    assert report.model_deltas == [
        ("eval_judge_model", "openrouter/owl-alpha", "qwen/qwen-2.5-72b-instruct:free")
    ]
    rendered = render_report(report)
    assert "model deltas" in rendered.lower()
    assert "owl-alpha" in rendered
    assert "qwen-2.5-72b" in rendered


def test_no_model_delta_when_envs_match_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanity: when env matches what the baseline recorded, no
    model_deltas. Otherwise every clean run would surface a noise
    delta."""
    _write_probe(tmp_path, case_id="c1", probe_id="p1", passed=True)
    baseline = Baseline(
        suite="s",
        captured_at=datetime.now(timezone.utc),
        git_sha="sha",
        agent_version="0.1.0",
        agent_primary_model="nvidia/nemotron-3-super-120b-a12b:free",
        agent_policy_model="openai/gpt-oss-20b:free",
        eval_judge_model="openrouter/owl-alpha",
        results={"c1": {"p1": "pass"}},
    )
    monkeypatch.setenv("AGENT_PRIMARY_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
    monkeypatch.setenv("AGENT_POLICY_MODEL", "openai/gpt-oss-20b:free")
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "openrouter/owl-alpha")

    report = diff_against_baseline(baseline, tmp_path)
    assert report.is_clean is True
    assert report.model_deltas == []
    assert render_report(report) == ""


def test_old_baseline_without_model_provenance_does_not_false_alarm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baselines minted before the model-provenance fields existed
    have empty strings for those fields. The diff must NOT report
    a model_delta in that case (it has no prior value to compare).
    Backward-compat for already-committed baselines."""
    _write_probe(tmp_path, case_id="c1", probe_id="p1", passed=True)
    baseline = Baseline(
        suite="s",
        captured_at=datetime.now(timezone.utc),
        git_sha="sha",
        agent_version="0.1.0",
        # Old baseline: model-provenance fields default to empty strings
        results={"c1": {"p1": "pass"}},
    )
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "qwen/qwen-2.5-72b-instruct:free")

    report = diff_against_baseline(baseline, tmp_path)
    assert report.model_deltas == []  # no comparison possible
    assert report.is_clean is True


def test_runtime_mismatch_short_circuits_probe_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline minted under codex, current process resolves to
    pydantic_ai (via env). The diff must refuse to compare probe
    outcomes (the comparison is meaningless across runtimes) and
    surface `runtime_mismatch`. Probe diff lists stay empty
    regardless of what `results` says."""
    # Probe would fail if compared; the diff should never get there.
    _write_probe(tmp_path, case_id="c1", probe_id="p1", passed=False)
    baseline = Baseline(
        suite="s",
        captured_at=datetime.now(timezone.utc),
        git_sha="sha",
        agent_version="0.1.0",
        runtime="codex",
        codex_primary_model="gpt-5.4",
        results={"c1": {"p1": "pass"}},
    )
    monkeypatch.setenv("AGENT_DEFAULT_RUNTIME", "pydantic_ai")

    report = diff_against_baseline(baseline, tmp_path)

    assert report.runtime_mismatch == ("codex", "pydantic_ai")
    assert report.new_failures == []
    assert report.newly_passing == []
    assert report.schema_deltas == []
    assert report.is_clean is False  # mismatch is severe
    rendered = render_report(report)
    assert "runtime mismatch" in rendered.lower()
    assert "re-mint" in rendered.lower()


def test_runtime_match_runs_full_probe_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanity inverse: when baseline runtime matches current runtime,
    the diff runs normally and produces ordinary probe deltas."""
    _write_probe(tmp_path, case_id="c1", probe_id="p1", passed=False)
    baseline = Baseline(
        suite="s",
        captured_at=datetime.now(timezone.utc),
        git_sha="sha",
        agent_version="0.1.0",
        runtime="codex",
        codex_primary_model="gpt-5.4",
        results={"c1": {"p1": "pass"}},
    )
    monkeypatch.setenv("AGENT_DEFAULT_RUNTIME", "codex")

    report = diff_against_baseline(baseline, tmp_path)

    assert report.runtime_mismatch is None
    assert report.new_failures == [("c1", "p1")]
    assert report.is_clean is False  # real regression


def test_codex_baseline_skips_agent_env_model_deltas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The original misleading-baseline-diff bug: a codex-runtime
    baseline emitted `model_deltas` for `AGENT_PRIMARY_MODEL` env
    changes even though codex ignores that env. Under codex, those
    env vars are dead weight and must NOT surface as deltas. Only
    `CODEX_PRIMARY_MODEL` is load-bearing."""
    _write_probe(tmp_path, case_id="c1", probe_id="p1", passed=True)
    baseline = Baseline(
        suite="s",
        captured_at=datetime.now(timezone.utc),
        git_sha="sha",
        agent_version="0.1.0",
        runtime="codex",
        agent_primary_model="openai/gpt-5.4-mini",
        codex_primary_model="gpt-5.4",
        results={"c1": {"p1": "pass"}},
    )
    monkeypatch.setenv("AGENT_DEFAULT_RUNTIME", "codex")
    # Operator rotates the pydantic-ai env (dead weight under codex)
    # AND keeps CODEX_PRIMARY_MODEL the same. No delta should fire.
    monkeypatch.setenv("AGENT_PRIMARY_MODEL", "gemini-3.1-flash-lite")
    monkeypatch.setenv("CODEX_PRIMARY_MODEL", "gpt-5.4")

    report = diff_against_baseline(baseline, tmp_path)

    assert report.model_deltas == []
    assert report.is_clean is True


def test_codex_baseline_surfaces_codex_primary_model_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive case: under codex, a `CODEX_PRIMARY_MODEL`
    swap is the load-bearing delta and DOES surface."""
    _write_probe(tmp_path, case_id="c1", probe_id="p1", passed=True)
    baseline = Baseline(
        suite="s",
        captured_at=datetime.now(timezone.utc),
        git_sha="sha",
        agent_version="0.1.0",
        runtime="codex",
        codex_primary_model="gpt-5.4",
        results={"c1": {"p1": "pass"}},
    )
    monkeypatch.setenv("AGENT_DEFAULT_RUNTIME", "codex")
    monkeypatch.setenv("CODEX_PRIMARY_MODEL", "gpt-5-mini")

    report = diff_against_baseline(baseline, tmp_path)

    assert report.model_deltas == [
        ("codex_primary_model", "gpt-5.4", "gpt-5-mini")
    ]


def test_legacy_baseline_without_runtime_falls_back_to_pydantic_ai_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backward-compat: baselines minted before the runtime field
    existed have `runtime=""`. The diff must skip the mismatch check
    AND fall through to the legacy pydantic-ai env model-delta
    shape (the only shape supported pre-this-patch). Otherwise every
    existing committed baseline would either spurious-fail or stop
    surfacing useful model deltas."""
    _write_probe(tmp_path, case_id="c1", probe_id="p1", passed=True)
    baseline = Baseline(
        suite="s",
        captured_at=datetime.now(timezone.utc),
        git_sha="sha",
        agent_version="0.1.0",
        # runtime defaults to "" (legacy)
        agent_primary_model="openrouter/old-model",
        results={"c1": {"p1": "pass"}},
    )
    monkeypatch.setenv("AGENT_DEFAULT_RUNTIME", "codex")  # current runtime
    monkeypatch.setenv("AGENT_PRIMARY_MODEL", "openrouter/new-model")

    report = diff_against_baseline(baseline, tmp_path)

    assert report.runtime_mismatch is None  # legacy: skip check
    assert report.model_deltas == [
        ("agent_primary_model", "openrouter/old-model", "openrouter/new-model")
    ]


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
