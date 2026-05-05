"""Tests for the span_latency_p50_under probe."""

from __future__ import annotations

import pytest

from agent_service.evals.probes import span_latency_p50_under
from agent_service.evals.schema import SpanLatencyP50UnderSpec
from tests.unit.evals.conftest import fake_ch


@pytest.mark.asyncio
async def test_pass_when_p50_under_threshold() -> None:
    spec = SpanLatencyP50UnderSpec(
        probe_id="p", span_name="mcae.gate.narrative_constitution", ms=15000
    )
    # 5 billion nanos = 5000 ms < 15000 ms
    result = await span_latency_p50_under.run(
        spec, "tid", fake_ch([{"n": 3, "p50_ns": 5_000_000_000}]),
        run_id="r", case_id="c",
    )
    assert result.passed is True
    assert result.observed["p50_ms"] == 5000
    assert result.observed["threshold_ms"] == 15000


@pytest.mark.asyncio
async def test_fail_when_p50_at_or_above_threshold() -> None:
    spec = SpanLatencyP50UnderSpec(
        probe_id="p", span_name="mcae.gate.narrative_constitution", ms=5000
    )
    result = await span_latency_p50_under.run(
        spec, "tid", fake_ch([{"n": 1, "p50_ns": 11_000_000_000}]),
        run_id="r", case_id="c",
    )
    assert result.passed is False
    assert result.observed["p50_ms"] == 11000


@pytest.mark.asyncio
async def test_zero_matching_spans_fails_with_error() -> None:
    """No spans means no p50; rather than vacuously pass, surface
    the case-authoring mistake as a probe error so it stands out
    in the run summary."""
    spec = SpanLatencyP50UnderSpec(
        probe_id="p", span_name="mcae.gate.does_not_exist", ms=5000
    )
    result = await span_latency_p50_under.run(
        spec, "tid", fake_ch([{"n": 0, "p50_ns": 0.0}]),
        run_id="r", case_id="c",
    )
    assert result.passed is False
    assert result.error is not None
    assert "no matching spans" in result.error
