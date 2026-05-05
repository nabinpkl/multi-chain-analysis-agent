"""Tests for the tool_called_with_args probe."""

from __future__ import annotations

import json

import pytest

from agent_service.evals.probes import tool_called_with_args
from agent_service.evals.schema import ToolCalledWithArgsSpec
from tests.unit.evals.conftest import fake_ch


@pytest.mark.asyncio
async def test_pass_when_call_matches_predicates() -> None:
    spec = ToolCalledWithArgsSpec(
        probe_id="p",
        tool_name="wallet_profile",
        arg_predicates={"addr": "ABC"},
    )
    result = await tool_called_with_args.run(
        spec, "tid",
        fake_ch([{"input_json": json.dumps({"input": {"addr": "ABC"}, "addr": "ABC"})}]),
        run_id="r", case_id="c",
    )
    assert result.passed is True
    assert result.observed["matching_calls"] == 1


@pytest.mark.asyncio
async def test_fail_when_no_call_matches_predicate() -> None:
    spec = ToolCalledWithArgsSpec(
        probe_id="p",
        tool_name="wallet_profile",
        arg_predicates={"addr": "EXPECTED"},
    )
    result = await tool_called_with_args.run(
        spec, "tid",
        fake_ch([{"input_json": json.dumps({"addr": "OTHER"})}]),
        run_id="r", case_id="c",
    )
    assert result.passed is False
    assert result.observed["matching_calls"] == 0
    assert result.observed["total_calls"] == 1


@pytest.mark.asyncio
async def test_no_predicates_passes_on_any_call() -> None:
    """Empty predicates: any successful tool call counts."""
    spec = ToolCalledWithArgsSpec(probe_id="p", tool_name="wallet_profile")
    result = await tool_called_with_args.run(
        spec, "tid",
        fake_ch([{"input_json": json.dumps({"addr": "X"})}]),
        run_id="r", case_id="c",
    )
    assert result.passed is True


@pytest.mark.asyncio
async def test_truncated_or_invalid_json_skipped_not_failed() -> None:
    """Primitive payloads are capped at 8 KiB; truncated JSON is
    treated as a non-matching call rather than a probe error."""
    spec = ToolCalledWithArgsSpec(
        probe_id="p", tool_name="wallet_profile", arg_predicates={"addr": "X"}
    )
    result = await tool_called_with_args.run(
        spec, "tid",
        fake_ch([
            {"input_json": '{"addr": "X" ...[truncated, total=12000]'},  # invalid JSON
            {"input_json": json.dumps({"addr": "X"})},  # valid, matches
        ]),
        run_id="r", case_id="c",
    )
    assert result.passed is True
    assert result.observed["matching_calls"] == 1
    assert result.observed["total_calls"] == 2
