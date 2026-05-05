"""Tests for the llm_call_used_model probe."""

from __future__ import annotations

import pytest

from agent_service.evals.probes import llm_call_used_model
from agent_service.evals.schema import LlmCallUsedModelSpec
from tests.unit.evals.conftest import FakeChClient, fake_ch


@pytest.mark.asyncio
async def test_pass_when_model_seen() -> None:
    spec = LlmCallUsedModelSpec(
        probe_id="p", model_name="nvidia/nemotron-3-super-120b-a12b:free"
    )
    result = await llm_call_used_model.run(
        spec, "tid", fake_ch([{"n": 2}]),
        run_id="r", case_id="c",
    )
    assert result.passed is True
    assert result.observed["matched_call_count"] == 2


@pytest.mark.asyncio
async def test_fail_when_model_absent() -> None:
    spec = LlmCallUsedModelSpec(probe_id="p", model_name="x/y:z")
    result = await llm_call_used_model.run(
        spec, "tid", fake_ch([{"n": 0}]),
        run_id="r", case_id="c",
    )
    assert result.passed is False


@pytest.mark.asyncio
async def test_model_name_bound_via_param_not_interpolated() -> None:
    spec = LlmCallUsedModelSpec(probe_id="p", model_name="x/y:z")
    ch = FakeChClient(respond_with=lambda _sql, _p: [{"n": 1}])
    await llm_call_used_model.run(
        spec, "tid", ch, run_id="r", case_id="c",
    )
    sql, params = ch.calls[0]
    assert "x/y:z" not in sql
    assert params["model"] == "x/y:z"
