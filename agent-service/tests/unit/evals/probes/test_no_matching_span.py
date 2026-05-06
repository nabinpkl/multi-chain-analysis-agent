"""Tests for the no_matching_span probe.

Mirror of has_matching_span with the assertion inverted: pass iff
zero matching spans. The key cases pin both directions of the flip
(zero matches passes, nonzero fails) and the same parameterized SQL
shape (so SQL-injection guarantees are inherited from the shared
query template).
"""

from __future__ import annotations

import pytest

from agent_service.evals.probes import no_matching_span
from agent_service.evals.schema import NoMatchingSpanSpec
from tests.unit.evals.conftest import FakeChClient, fake_ch


@pytest.mark.asyncio
async def test_passes_when_zero_matches() -> None:
    spec = NoMatchingSpanSpec(probe_id="p", span_name="mcae.gate.constitution")
    result = await no_matching_span.run(
        spec, "abc123", fake_ch([{"n": 0}]),
        run_id="r", case_id="c",
    )
    assert result.passed is True
    assert result.observed["matched_span_count"] == 0
    assert result.error is None


@pytest.mark.asyncio
async def test_fails_when_any_match_found() -> None:
    """Even one matching span flips this to a fail. Switches-off cases
    rely on this strict equality (the gate either ran or it didn't)."""
    spec = NoMatchingSpanSpec(probe_id="p", span_name="mcae.gate.constitution")
    result = await no_matching_span.run(
        spec, "abc123", fake_ch([{"n": 1}]),
        run_id="r", case_id="c",
    )
    assert result.passed is False
    assert result.observed["matched_span_count"] == 1


@pytest.mark.asyncio
async def test_attrs_filter_binds_each_pair_as_typed_params() -> None:
    """attrs further constrain the absence assertion: passes only if
    no span has both the name AND the listed attrs. SQL-binding
    contract matches has_matching_span (regression guard against a
    future refactor that splits the two probes' query templates)."""
    spec = NoMatchingSpanSpec(
        probe_id="p",
        span_name="mcae.gate.constitution",
        attrs={"mcae.gate.verdict": "INJECT-APPROVED"},
    )
    ch = FakeChClient(respond_with=lambda _sql, _p: [{"n": 0}])
    result = await no_matching_span.run(
        spec, "INJECT-TID", ch, run_id="r", case_id="c",
    )
    assert result.passed is True
    sql, params = ch.calls[0]
    assert "INJECT-" not in sql, "value leaked into SQL body"
    assert "INJECT-TID" in params.values()
    assert "INJECT-APPROVED" in params.values()
    assert "{tid:String}" in sql
    assert "{name:String}" in sql


@pytest.mark.asyncio
async def test_ch_failure_returns_error_result_not_raise() -> None:
    class BoomCh:
        async def query(self, *_a, **_k):
            raise RuntimeError("connection refused")

        async def aclose(self) -> None:
            pass

    spec = NoMatchingSpanSpec(probe_id="p", span_name="mcae.gate.constitution")
    result = await no_matching_span.run(
        spec, "tid", BoomCh(),  # type: ignore[arg-type]
        run_id="r", case_id="c",
    )
    assert result.passed is False
    assert "connection refused" in (result.error or "")
