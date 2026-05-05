"""Tests for the gate_passed probe."""

from __future__ import annotations

import pytest

from agent_service.evals.probes import gate_passed
from agent_service.evals.schema import GatePassedSpec
from tests.unit.evals.conftest import FakeChClient, fake_ch


@pytest.mark.asyncio
async def test_pass_when_gate_approved() -> None:
    spec = GatePassedSpec(probe_id="p", gate_kind="placeholder")
    result = await gate_passed.run(
        spec, "tid", fake_ch([{"n": 1}]),
        run_id="r", case_id="c",
    )
    assert result.passed is True
    assert result.observed["span_name"] == "mcae.gate.placeholder"
    assert result.observed["version_required"] is None


@pytest.mark.asyncio
async def test_fail_when_no_approval_recorded() -> None:
    spec = GatePassedSpec(probe_id="p", gate_kind="constitution")
    result = await gate_passed.run(
        spec, "tid", fake_ch([{"n": 0}]),
        run_id="r", case_id="c",
    )
    assert result.passed is False


@pytest.mark.asyncio
async def test_version_pin_added_to_query_when_set() -> None:
    """version='v4' adds an extra WHERE clause asserting
    SpanAttributes['mcae.gate.version']='v4'. The version value
    reaches the wrapper as a typed param, never f-string-interpolated."""
    spec = GatePassedSpec(
        probe_id="p", gate_kind="narrative_constitution", version="v4"
    )
    ch = FakeChClient(respond_with=lambda _sql, _p: [{"n": 1}])
    result = await gate_passed.run(
        spec, "tid", ch, run_id="r", case_id="c",
    )
    assert result.passed is True
    sql, params = ch.calls[0]
    assert "v4" not in sql  # not interpolated
    assert params.get("ver") == "v4"  # bound through placeholder
    assert result.observed["version_required"] == "v4"
