"""Tests for the no_span_with_status probe."""

from __future__ import annotations

import pytest

from agent_service.evals.probes import no_span_with_status
from agent_service.evals.schema import NoSpanWithStatusSpec
from tests.unit.evals.conftest import FakeChClient, fake_ch


@pytest.mark.asyncio
async def test_pass_when_no_offending_span() -> None:
    spec = NoSpanWithStatusSpec(
        probe_id="p", span_name="mcae.primitive.wallet_profile", status="error"
    )
    result = await no_span_with_status.run(
        spec, "tid", fake_ch([{"n": 0}]),
        run_id="r", case_id="c",
    )
    assert result.passed is True


@pytest.mark.asyncio
async def test_fail_when_offending_span_present() -> None:
    spec = NoSpanWithStatusSpec(
        probe_id="p", span_name="mcae.primitive.wallet_profile", status="error"
    )
    result = await no_span_with_status.run(
        spec, "tid", fake_ch([{"n": 1}]),
        run_id="r", case_id="c",
    )
    assert result.passed is False
    assert result.observed["matched_span_count"] == 1
    assert result.observed["status_filter"] == "error"


@pytest.mark.asyncio
async def test_status_ok_uses_negated_filter() -> None:
    """The `ok` branch counts spans WITHOUT the error attribute set."""
    spec = NoSpanWithStatusSpec(
        probe_id="p", span_name="mcae.primitive.wallet_profile", status="ok"
    )
    ch = FakeChClient(respond_with=lambda _sql, _p: [{"n": 0}])
    result = await no_span_with_status.run(
        spec, "tid", ch, run_id="r", case_id="c",
    )
    assert result.passed is True
    sql, _ = ch.calls[0]
    assert "!= 'true'" in sql
