"""Tests for the has_matching_span probe."""

from __future__ import annotations

import pytest

from agent_service.evals.probes import has_matching_span
from agent_service.evals.schema import HasMatchingSpanSpec
from tests.unit.evals.conftest import FakeChClient, fake_ch


@pytest.mark.asyncio
async def test_pass_when_count_positive() -> None:
    spec = HasMatchingSpanSpec(probe_id="p", span_name="mcae.snapshot.lease")
    result = await has_matching_span.run(
        spec, "abc123", fake_ch([{"n": 1}]),
        run_id="r", case_id="c",
    )
    assert result.passed is True
    assert result.observed["matched_span_count"] == 1
    assert result.error is None


@pytest.mark.asyncio
async def test_fail_when_count_zero() -> None:
    spec = HasMatchingSpanSpec(probe_id="p", span_name="mcae.snapshot.lease")
    result = await has_matching_span.run(
        spec, "abc123", fake_ch([{"n": 0}]),
        run_id="r", case_id="c",
    )
    assert result.passed is False
    assert result.observed["matched_span_count"] == 0


@pytest.mark.asyncio
async def test_attrs_filter_binds_each_pair_as_typed_params() -> None:
    """Each k/v pair in attrs adds a SpanAttributes[{k:String}] =
    {v:String} clause, with both sides bound through the safe
    placeholder mechanism."""
    spec = HasMatchingSpanSpec(
        probe_id="p",
        span_name="mcae.gate.placeholder",
        attrs={
            "mcae.gate.verdict": "INJECT-APPROVED",
            "mcae.gate.version": "INJECT-V1",
        },
    )
    ch = FakeChClient(respond_with=lambda _sql, _p: [{"n": 2}])
    result = await has_matching_span.run(
        spec, "INJECT-TID", ch, run_id="r", case_id="c",
    )
    assert result.passed is True
    sql, params = ch.calls[0]
    # Distinct sentinel values let us assert "values never appear in
    # the SQL body" without colliding with placeholder syntax names
    # like {v0:String}.
    assert "INJECT-" not in sql, "value leaked into SQL body"
    # Every dynamic value reaches the wrapper as a typed param.
    assert "INJECT-TID" in params.values()
    assert "INJECT-APPROVED" in params.values()
    assert "INJECT-V1" in params.values()
    # And the SQL uses the placeholder syntax for each.
    assert "{tid:String}" in sql
    assert "{name:String}" in sql
    assert sql.count(":String}") >= 4  # tid, name, k0/v0, k1/v1


@pytest.mark.asyncio
async def test_ch_failure_returns_error_result_not_raise() -> None:
    """Probes never raise; CH errors land on ProbeResult.error so
    the runner can persist them as structured failures."""

    class BoomCh:
        async def query(self, *_a, **_k):
            raise RuntimeError("connection refused")

        async def aclose(self) -> None:
            pass

    spec = HasMatchingSpanSpec(probe_id="p", span_name="mcae.snapshot.lease")
    result = await has_matching_span.run(
        spec, "tid", BoomCh(),  # type: ignore[arg-type]
        run_id="r", case_id="c",
    )
    assert result.passed is False
    assert "connection refused" in (result.error or "")
