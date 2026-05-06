"""Tests for the slowest_call_under_ms probe.

Probe semantics:
- Scans matching spans (LLM hops or tool calls) by SpanName LIKE
  pattern, sorts by Duration DESC, takes the slowest one.
- Pass if its duration converted to ms is strictly less than `ms`.
- Surfaces the offender's identity (gen_ai.request.model for LLM,
  gen_ai.tool.name for tool) so a failure tells you *which* call
  stalled, not just that something stalled.
- Zero matching spans is a probe error (vacuous-pass guard).

Tests cover both call_kind values, the SQL parameterization, the
threshold compare, and the empty-result error path.
"""

from __future__ import annotations

import pytest

from agent_service.evals.probes import slowest_call_under_ms
from agent_service.evals.schema import SlowestCallUnderMsSpec
from tests.unit.evals.conftest import FakeChClient


def _spec(call_kind: str, ms: int) -> SlowestCallUnderMsSpec:
    return SlowestCallUnderMsSpec(
        probe_id="t",
        call_kind=call_kind,  # type: ignore[arg-type]
        ms=ms,
    )


@pytest.mark.asyncio
async def test_passes_when_slowest_llm_under_threshold() -> None:
    """A 12s LLM hop against a 60s ceiling passes; the slow model id
    is still surfaced in observed for diagnostic purposes."""
    spec = _spec("llm", 60000)
    rows = [
        {
            "SpanName": "chat openai/gpt-oss-20b:free",
            "identity": "openai/gpt-oss-20b:free",
            "duration_ns": 12_000_000_000,  # 12s in ns
        }
    ]
    ch = FakeChClient(respond_with=lambda _sql, _p: rows)
    r = await slowest_call_under_ms.run(
        spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
    )
    assert r.passed is True
    assert r.observed["slowest_ms"] == 12_000
    assert r.observed["slowest_identity"] == "openai/gpt-oss-20b:free"
    assert r.observed["call_kind"] == "llm"
    assert r.error is None


@pytest.mark.asyncio
async def test_fails_when_slowest_llm_over_threshold_and_names_offender() -> None:
    """A 75s LLM hop against a 60s ceiling fails; the failure surfaces
    the model id so the case author knows which provider stalled
    rather than guessing."""
    spec = _spec("llm", 60000)
    rows = [
        {
            "SpanName": "chat nvidia/nemotron-3-super-120b-a12b:free",
            "identity": "nvidia/nemotron-3-super-120b-a12b:free",
            "duration_ns": 75_000_000_000,
        }
    ]
    ch = FakeChClient(respond_with=lambda _sql, _p: rows)
    r = await slowest_call_under_ms.run(
        spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
    )
    assert r.passed is False
    assert r.observed["slowest_ms"] == 75_000
    assert (
        r.observed["slowest_identity"]
        == "nvidia/nemotron-3-super-120b-a12b:free"
    )
    assert r.error is None


@pytest.mark.asyncio
async def test_passes_for_tool_call_under_threshold_with_tool_name() -> None:
    """`call_kind=tool` reads gen_ai.tool.name. A 200ms tool call
    against a 10s ceiling passes; the tool name comes back."""
    spec = _spec("tool", 10000)
    rows = [
        {
            "SpanName": "running tool wallet_profile",
            "identity": "wallet_profile",
            "duration_ns": 200_000_000,
        }
    ]
    ch = FakeChClient(respond_with=lambda _sql, _p: rows)
    r = await slowest_call_under_ms.run(
        spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
    )
    assert r.passed is True
    assert r.observed["slowest_identity"] == "wallet_profile"
    assert r.observed["call_kind"] == "tool"


@pytest.mark.asyncio
async def test_zero_matching_spans_is_probe_error_not_vacuous_pass() -> None:
    """Empty result set is treated as a probe error so a typo'd
    call_kind or a refusal-case turn (no LLM/tool calls) doesn't
    silently pass. The error message names the call_kind."""
    spec = _spec("tool", 10000)
    ch = FakeChClient(respond_with=lambda _sql, _p: [])
    r = await slowest_call_under_ms.run(
        spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
    )
    assert r.passed is False
    assert r.error is not None
    assert "no matching tool spans" in r.error


@pytest.mark.asyncio
async def test_query_uses_parameterized_pattern_and_trace_id() -> None:
    """SpanName LIKE pattern, identity attr, and trace_id all bound
    as named CH params (no SQL interpolation). Regression guard."""
    spec = _spec("llm", 60000)
    rows = [
        {"SpanName": "chat x", "identity": "x", "duration_ns": 1}
    ]
    ch = FakeChClient(respond_with=lambda _sql, _p: rows)
    await slowest_call_under_ms.run(
        spec, "trace-INJECT-Y", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
    )
    assert len(ch.calls) == 1
    sql, params = ch.calls[0]
    assert "trace-INJECT-Y" not in sql
    assert "chat %" not in sql  # the LIKE pattern itself is bound
    assert params["tid"] == "trace-INJECT-Y"
    assert params["pat"] == "chat %"
    assert params["ident"] == "gen_ai.request.model"


@pytest.mark.asyncio
async def test_tool_kind_uses_running_tool_pattern_and_tool_name_attr() -> None:
    """`call_kind=tool` swaps both the LIKE pattern and the identity
    attribute. Pinned so a future refactor of the dispatch table
    doesn't accidentally read gen_ai.request.model from a tool span."""
    spec = _spec("tool", 5000)
    rows = [
        {
            "SpanName": "running tool community_summary",
            "identity": "community_summary",
            "duration_ns": 50_000_000,
        }
    ]
    ch = FakeChClient(respond_with=lambda _sql, _p: rows)
    await slowest_call_under_ms.run(
        spec, "trace1", ch, run_id="run", case_id="c"  # type: ignore[arg-type]
    )
    _sql, params = ch.calls[0]
    assert params["pat"] == "running tool%"
    assert params["ident"] == "gen_ai.tool.name"
