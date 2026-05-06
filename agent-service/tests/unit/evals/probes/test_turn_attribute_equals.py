"""Tests for the turn_attribute_equals probe.

Probe semantics:
- Reads SpanAttributes[attr] off the mcae.turn root span.
- Compares against `expected` (a string, since CH stores all attrs
  as strings).
- passed=True iff actual == expected.
- Empty/missing attr returns "" from CH map access; passes only if
  expected is also "".
- No mcae.turn span at all is a probe error (with diagnostic).

Tests cover the matrix of (attr present, attr absent, span absent,
expected matches, expected doesn't match).
"""

from __future__ import annotations

import pytest

from agent_service.evals.probes import turn_attribute_equals
from agent_service.evals.schema import TurnAttributeEqualsSpec
from tests.unit.evals.conftest import FakeChClient


def _spec(attr: str, expected: str) -> TurnAttributeEqualsSpec:
    return TurnAttributeEqualsSpec(
        probe_id="t",
        attr=attr,
        expected=expected,
    )


@pytest.mark.asyncio
async def test_passes_when_attr_value_equals_expected() -> None:
    spec = _spec("mcae.turn.tool_calls", "0")
    ch = FakeChClient(respond_with=lambda _sql, _p: [{"v": "0"}])
    r = await turn_attribute_equals.run(
        spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
    )
    assert r.passed is True
    assert r.observed["actual"] == "0"
    assert r.observed["expected"] == "0"
    assert r.observed["attr"] == "mcae.turn.tool_calls"
    assert r.error is None


@pytest.mark.asyncio
async def test_fails_when_attr_value_differs() -> None:
    spec = _spec("mcae.turn.tool_calls", "0")
    ch = FakeChClient(respond_with=lambda _sql, _p: [{"v": "3"}])
    r = await turn_attribute_equals.run(
        spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
    )
    assert r.passed is False
    assert r.observed["actual"] == "3"
    assert r.observed["expected"] == "0"
    assert r.error is None  # legitimate fail, not a probe error


@pytest.mark.asyncio
async def test_passes_when_expected_empty_and_attr_missing() -> None:
    """CH map access for a missing key returns empty string. If the
    case author expects empty (rare but valid), the probe should
    pass. Pinned so a future refactor doesn't break this edge."""
    spec = _spec("mcae.turn.never_set", "")
    ch = FakeChClient(respond_with=lambda _sql, _p: [{"v": ""}])
    r = await turn_attribute_equals.run(
        spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
    )
    assert r.passed is True


@pytest.mark.asyncio
async def test_fails_with_error_when_no_turn_span_in_trace() -> None:
    """Trace doesn't have mcae.turn at all (turn never built). The
    probe surfaces this as a probe error rather than calling the
    attribute mismatch a clean fail; the trace shape is wrong."""
    spec = _spec("mcae.turn.tool_calls", "0")
    ch = FakeChClient(respond_with=lambda _sql, _p: [])
    r = await turn_attribute_equals.run(
        spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
    )
    assert r.passed is False
    assert r.error is not None
    assert "no mcae.turn span" in r.error


@pytest.mark.asyncio
async def test_query_uses_parameterized_attr_and_trace_id() -> None:
    """Probe must bind both attr and trace_id as named params, not
    interpolate. SQL injection regression test."""
    spec = _spec("mcae.turn.claims_emitted", "1")
    ch = FakeChClient(respond_with=lambda _sql, _p: [{"v": "1"}])
    await turn_attribute_equals.run(
        spec, "trace-INJECT-X", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
    )
    assert len(ch.calls) == 1
    sql, params = ch.calls[0]
    # Neither value embedded in SQL
    assert "trace-INJECT-X" not in sql
    assert "mcae.turn.claims_emitted" not in sql
    assert params["attr"] == "mcae.turn.claims_emitted"
    assert params["tid"] == "trace-INJECT-X"
