"""Tests for the claim_grounded_in probe."""

from __future__ import annotations

import pytest

from agent_service.evals.probes import claim_grounded_in
from agent_service.evals.schema import ClaimGroundedInSpec
from tests.unit.evals.conftest import fake_ch


@pytest.mark.asyncio
async def test_pass_when_every_claim_matches_source_kind() -> None:
    spec = ClaimGroundedInSpec(probe_id="p", source_kind="primitive")
    result = await claim_grounded_in.run(
        spec, "tid",
        fake_ch([{"sk": "primitive"}, {"sk": "primitive"}]),
        run_id="r", case_id="c",
    )
    assert result.passed is True
    assert result.observed["claim_count"] == 2
    assert result.observed["wrong_kind_count"] == 0


@pytest.mark.asyncio
async def test_fail_when_any_claim_has_wrong_source_kind() -> None:
    spec = ClaimGroundedInSpec(probe_id="p", source_kind="primitive")
    result = await claim_grounded_in.run(
        spec, "tid",
        fake_ch([{"sk": "primitive"}, {"sk": "exploratory"}]),
        run_id="r", case_id="c",
    )
    assert result.passed is False
    assert result.observed["wrong_kind_count"] == 1


@pytest.mark.asyncio
async def test_zero_claims_passes_with_note() -> None:
    """Vacuous truth: cases that intentionally exercise the no-claim
    path (e.g. 'who are you' turns) must not fail this probe."""
    spec = ClaimGroundedInSpec(probe_id="p", source_kind="primitive")
    result = await claim_grounded_in.run(
        spec, "tid", fake_ch([]),
        run_id="r", case_id="c",
    )
    assert result.passed is True
    assert result.observed["claim_count"] == 0
    assert "note" in result.observed
