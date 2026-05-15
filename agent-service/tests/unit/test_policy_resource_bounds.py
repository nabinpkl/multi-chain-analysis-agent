"""Unit pins on the resource_bounds module.

These tests freeze the shape both runtimes depend on: the constant
value, the envelope payload, the sentinel string, and the
is_budget_exhausted predicate. A change to any of these breaks the
codex / pydantic-ai parity contract; the tests are the early
warning.
"""

from __future__ import annotations

import importlib
import os

import pytest

from agent_service.policy import resource_bounds


def test_default_budget_is_8():
    """No env override -> 8. Matches the historical
    tool_calls_limit value the pydantic-ai loop carried before the
    interceptor design landed."""
    assert resource_bounds.TURN_TOOL_CALL_BUDGET == 8


def test_is_budget_exhausted_boundary():
    """The predicate is `used >= budget`. At budget-1 we still
    allow; at budget we short-circuit."""
    assert not resource_bounds.is_budget_exhausted(0)
    assert not resource_bounds.is_budget_exhausted(
        resource_bounds.TURN_TOOL_CALL_BUDGET - 1
    )
    assert resource_bounds.is_budget_exhausted(
        resource_bounds.TURN_TOOL_CALL_BUDGET
    )
    assert resource_bounds.is_budget_exhausted(
        resource_bounds.TURN_TOOL_CALL_BUDGET + 1
    )


def test_no_more_lookups_payload_shape():
    """The payload is the single source of truth for what the
    model sees on every budget-exhausted tool result. Both
    runtimes return this exact dict. Drift breaks the codex-side
    sentinel scan in codex_driver.py and the eval-case
    llm_judge rubric."""
    payload = resource_bounds.NO_MORE_LOOKUPS_PAYLOAD
    assert payload["error"] == "no_more_lookups_this_turn"
    assert payload["error"] == resource_bounds.NO_MORE_LOOKUPS_ERROR_KIND
    assert "tool budget" in payload["guidance"]
    assert "Finalize" in payload["guidance"]


def test_sentinel_is_structural_not_natural_language():
    """The error-kind sentinel is what codex_driver.py greps the
    TOOL_COMPLETED event payload for. It must be a structured
    error-kind token (snake_case, distinct from anything a
    primitive would legitimately return), not a natural-language
    phrase that could appear in another tool's prose output."""
    assert resource_bounds.NO_MORE_LOOKUPS_ERROR_KIND == "no_more_lookups_this_turn"
    assert " " not in resource_bounds.NO_MORE_LOOKUPS_ERROR_KIND


def test_budgeted_primitives_set():
    """Only the three read-side primitives count against the
    budget. emit_claim is reporting, not lookup."""
    assert resource_bounds.BUDGETED_PRIMITIVES == frozenset(
        {"wallet_profile", "community_summary", "get_token_info"}
    )
    assert "emit_claim" not in resource_bounds.BUDGETED_PRIMITIVES
    assert "emit_claims" not in resource_bounds.BUDGETED_PRIMITIVES


def test_env_override(monkeypatch: pytest.MonkeyPatch):
    """AGENT_TURN_TOOL_CALL_BUDGET overrides the default at module
    load. Operator can tune the cap without recompiling. Both
    runtimes must read the same env var so they stay in lockstep;
    this test pins the Python side's contract."""
    monkeypatch.setenv("AGENT_TURN_TOOL_CALL_BUDGET", "3")
    reloaded = importlib.reload(resource_bounds)
    try:
        assert reloaded.TURN_TOOL_CALL_BUDGET == 3
        assert reloaded.is_budget_exhausted(2) is False
        assert reloaded.is_budget_exhausted(3) is True
    finally:
        # Reload again without the override so other tests see
        # the default value. monkeypatch removes the env var on
        # teardown; we must re-import to refresh the module
        # constant.
        monkeypatch.delenv("AGENT_TURN_TOOL_CALL_BUDGET", raising=False)
        importlib.reload(resource_bounds)
