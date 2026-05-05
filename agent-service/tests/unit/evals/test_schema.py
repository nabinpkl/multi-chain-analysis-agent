"""Schema round-trip + validation tests for Layer 1 of Ship 2 (ADR 14).

Probe specs are a discriminated union over `kind`. Each probe kind
has its own typed `*Spec` class with args inlined as fields. Tests
exercise:

1. **Round-trip per probe kind** (one fixture per kind, 7 total):
   YAML on disk -> ruamel.yaml -> EvalCase.model_validate (which
   dispatches each probe to the right *Spec via the discriminator)
   -> model_dump -> EvalCase.model_validate again. Catches schema
   drift the moment a field becomes non-round-trippable.

2. **Validation negative cases**: every `Field` constraint and
   `field_validator` in schema.py has at least one test that
   exercises the rejection path. Includes the discriminator
   negative case (unknown `kind`).

3. **Cross-type sanity**: ProbeResult references probe_id/case_id/
   run_id strings that match what an EvalCase + RunMetadata would
   produce.

Tests are sync (no asyncio fixtures); the schema layer has zero IO.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError, TypeAdapter
from ruamel.yaml import YAML

from agent_service.evals.schema import (
    ClaimGroundedInSpec,
    EvalCase,
    GatePassedSpec,
    HasMatchingSpanSpec,
    LlmCallUsedModelSpec,
    NoSpanWithStatusSpec,
    ProbeKind,
    ProbeResult,
    ProbeSpec,
    RunMetadata,
    SpanLatencyP50UnderSpec,
    ToolCalledWithArgsSpec,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "cases"
_YAML = YAML(typ="safe")
_PROBE_SPEC_ADAPTER = TypeAdapter(ProbeSpec)


def _load_yaml_cases(name: str) -> list[dict]:
    """Load a fixture file. Each fixture is a YAML list of one or more
    case dicts."""
    raw = _YAML.load((_FIXTURES / name).read_text())
    assert isinstance(raw, list) and raw, f"fixture {name} must be a non-empty list"
    return raw


# ---------------------------------------------------------------------------
# Group 1: round-trip per probe kind
# ---------------------------------------------------------------------------

# Each entry pairs a fixture filename with the probe kind it exercises.
# Adding a new probe kind to schema.ProbeKind requires adding a row here
# (and the corresponding fixture file). The test that asserts "every
# ProbeKind has a fixture" enforces this.
_PROBE_KIND_FIXTURES = [
    ("has_matching_span.yaml", "has_matching_span"),
    ("tool_called_with_args.yaml", "tool_called_with_args"),
    ("claim_grounded_in.yaml", "claim_grounded_in"),
    ("gate_passed.yaml", "gate_passed"),
    ("span_latency_p50_under.yaml", "span_latency_p50_under"),
    ("no_span_with_status.yaml", "no_span_with_status"),
    ("llm_call_used_model.yaml", "llm_call_used_model"),
]


@pytest.mark.parametrize("fixture_name,expected_kind", _PROBE_KIND_FIXTURES)
def test_round_trip_yaml_per_probe_kind(fixture_name: str, expected_kind: str) -> None:
    """YAML -> EvalCase -> dict -> EvalCase preserves equality, and the
    fixture exercises the expected probe kind."""
    raw_cases = _load_yaml_cases(fixture_name)
    for raw in raw_cases:
        case = EvalCase.model_validate(raw)
        assert any(p.kind == expected_kind for p in case.probes), (
            f"fixture {fixture_name} should exercise probe kind {expected_kind!r}"
        )

        # Round-trip: dump back to dict, re-validate, equality.
        dumped = case.model_dump(mode="python")
        re_case = EvalCase.model_validate(dumped)
        assert re_case == case, f"round-trip mismatch for fixture {fixture_name}"


def test_every_probe_kind_has_a_fixture() -> None:
    """Adding a probe kind to the Literal without adding a fixture
    leaves Layer 1 untested. Forcing a fixture row keeps the schema
    and its examples in sync."""
    declared = set(get_args(ProbeKind))
    fixtured = {kind for _, kind in _PROBE_KIND_FIXTURES}
    missing = declared - fixtured
    assert not missing, f"probe kinds without a fixture: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Group 2: validation negative cases
# ---------------------------------------------------------------------------


def test_probe_id_must_be_non_empty() -> None:
    with pytest.raises(ValidationError, match="probe_id must be non-empty"):
        HasMatchingSpanSpec(probe_id="   ", span_name="mcae.snapshot.lease")


def test_invalid_probe_kind_rejected() -> None:
    """Discriminator with no matching union member fails at parse time."""
    with pytest.raises(ValidationError):
        _PROBE_SPEC_ADAPTER.validate_python(
            {"probe_id": "x", "kind": "not_a_real_kind", "span_name": "x"}
        )


def test_missing_required_arg_fails_at_load() -> None:
    """The whole point of the discriminated union refactor: a malformed
    case fails at YAML-load time pointing at the missing arg, not later
    at probe-call time."""
    with pytest.raises(ValidationError, match="source_kind"):
        _PROBE_SPEC_ADAPTER.validate_python(
            {"probe_id": "p", "kind": "claim_grounded_in"}  # missing source_kind
        )


def test_extra_field_on_probe_spec_rejected() -> None:
    """Each *Spec class has extra='forbid' (via _ProbeSpecBase). A
    YAML field that doesn't exist on the matched spec class fails
    rather than silently dropping."""
    with pytest.raises(ValidationError):
        _PROBE_SPEC_ADAPTER.validate_python(
            {
                "probe_id": "p",
                "kind": "has_matching_span",
                "span_name": "x",
                "wat": "this field does not exist on HasMatchingSpanSpec",
            }
        )


def test_case_id_must_be_non_empty() -> None:
    with pytest.raises(ValidationError, match="must be non-empty"):
        EvalCase(
            case_id="",
            suite="s",
            inputs={"userQuestion": "x"},
            probes=[HasMatchingSpanSpec(probe_id="p", span_name="x")],
        )


def test_suite_must_be_non_empty() -> None:
    with pytest.raises(ValidationError, match="must be non-empty"):
        EvalCase(
            case_id="c",
            suite="   ",
            inputs={"userQuestion": "x"},
            probes=[HasMatchingSpanSpec(probe_id="p", span_name="x")],
        )


def test_probes_list_must_be_non_empty() -> None:
    with pytest.raises(ValidationError, match="at least one probe"):
        EvalCase(case_id="c", suite="s", inputs={"userQuestion": "x"}, probes=[])


def test_probe_ids_must_be_unique_within_a_case() -> None:
    with pytest.raises(ValidationError, match="probe_id values must be unique"):
        EvalCase(
            case_id="c",
            suite="s",
            inputs={"userQuestion": "x"},
            probes=[
                HasMatchingSpanSpec(probe_id="dup", span_name="x"),
                ClaimGroundedInSpec(probe_id="dup", source_kind="primitive"),
            ],
        )


def test_span_latency_ms_must_be_positive() -> None:
    """Field(gt=0) rejects zero and negative values; catches typos
    like ms=0 that would pass any latency."""
    with pytest.raises(ValidationError):
        SpanLatencyP50UnderSpec(probe_id="p", span_name="x", ms=0)


def test_score_out_of_unit_interval_rejected() -> None:
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError, match=r"score must be in \[0, 1\]"):
        ProbeResult(
            run_id="r",
            case_id="c",
            probe_id="p",
            trace_id="t",
            passed=False,
            score=1.5,
            started_at=now,
            finished_at=now,
        )


def test_score_none_is_allowed() -> None:
    """Pure pass/fail probes leave score=None. Make sure the
    unit-interval check doesn't reject the absent case."""
    now = datetime.now(timezone.utc)
    r = ProbeResult(
        run_id="r",
        case_id="c",
        probe_id="p",
        trace_id="t",
        passed=True,
        started_at=now,
        finished_at=now,
    )
    assert r.score is None


def test_run_metadata_pass_count_cannot_exceed_probe_count() -> None:
    with pytest.raises(ValidationError, match="pass_count.*> probe_count"):
        RunMetadata(
            run_id="r",
            started_at=datetime.now(timezone.utc),
            git_sha="abc",
            agent_version="0.1.0",
            framework_adapter="framework_free",
            case_count=1,
            probe_count=3,
            pass_count=4,
        )


def test_extra_fields_rejected_on_eval_case() -> None:
    """`extra="forbid"` keeps stale fields from sneaking in via YAML
    that pydantic would otherwise silently drop."""
    with pytest.raises(ValidationError):
        EvalCase.model_validate(
            {
                "case_id": "c",
                "suite": "s",
                "inputs": {"userQuestion": "x"},
                "probes": [
                    {"probe_id": "p", "kind": "has_matching_span", "span_name": "x"}
                ],
                "wat": "this field does not exist on the schema",
            }
        )


# ---------------------------------------------------------------------------
# Group 3: cross-type sanity
# ---------------------------------------------------------------------------


def test_probe_result_keys_compose_with_case_and_run() -> None:
    """Manufacture a real EvalCase + RunMetadata, then build a
    ProbeResult whose foreign-key strings come from them. Catches
    field-type drift across types (e.g. case_id became int)."""
    case = EvalCase.model_validate(_load_yaml_cases("has_matching_span.yaml")[0])
    now = datetime.now(timezone.utc)
    run = RunMetadata(
        run_id="01HXYZ",
        started_at=now,
        git_sha="deadbeef",
        agent_version="0.1.0",
        framework_adapter="framework_free",
        case_count=1,
        probe_count=len(case.probes),
        pass_count=len(case.probes),
    )
    result = ProbeResult(
        run_id=run.run_id,
        case_id=case.case_id,
        probe_id=case.probes[0].probe_id,
        trace_id="abc123",
        passed=True,
        started_at=now,
        finished_at=now,
    )
    assert result.run_id == run.run_id
    assert result.case_id == case.case_id
    assert result.probe_id == case.probes[0].probe_id


def test_typed_spec_consumed_directly_by_layer_2_signature() -> None:
    """Sanity check that typed *Spec classes are usable as direct
    function arguments without the `args` indirection that the
    discriminated-union refactor was meant to eliminate."""
    spec = ClaimGroundedInSpec(probe_id="p", source_kind="primitive")
    # A Layer 2 probe will declare its `run` like:
    #   async def run(spec: ClaimGroundedInSpec, ...) -> ProbeResult
    # so the consuming code reads spec.source_kind directly. No
    # _Args.model_validate(spec.args) step.
    assert spec.source_kind == "primitive"
    assert spec.kind == "claim_grounded_in"
