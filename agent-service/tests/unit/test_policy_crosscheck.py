"""Parity tests for the crosscheck taxonomy + tolerance + LLM-extractor
compare. Mirror of `policy_crosscheck.rs`'s test set."""

from __future__ import annotations

from agent_service.policy.crosscheck import (
    CrosscheckConfig,
    ExtractedNumber,
    LlmExtractedNumber,
    UnitClass,
    classify_metric,
    cross_check_extracted_pair,
    within_tolerance,
)


def _n(value: float, unit_class: UnitClass) -> ExtractedNumber:
    return ExtractedNumber(value=value, unit_class=unit_class, hedged=False)


# --- within_tolerance ------------------------------------------------------


def test_within_tolerance_exact_match():
    assert within_tolerance(12.4, 12.4, 0.10)


def test_within_tolerance_small_drift_ok():
    assert within_tolerance(12.4, 12.5, 0.10)  # ~0.8% off


def test_within_tolerance_large_drift_fails():
    assert not within_tolerance(12.4, 50.0, 0.10)


def test_within_tolerance_zero_only_matches_zero():
    assert within_tolerance(0.0, 0.0, 0.10)
    assert not within_tolerance(0.001, 0.0, 0.10)


# --- classify_metric -------------------------------------------------------


def test_classify_sol_synonyms():
    for k in (
        "volume",
        "total_volume",
        "inbound_volume",
        "lamports",
        "sol_inflow",
        "in_volume_lamports",
        "out_volume_lamports",
        "bidir_volume_lamports",
        "total_volume_lamports",
    ):
        assert classify_metric(k) is UnitClass.SOL, k


def test_classify_count_synonyms():
    for k in ("degree", "edge_count", "connections", "tx_count"):
        assert classify_metric(k) is UnitClass.COUNT, k


def test_classify_community_id():
    assert classify_metric("community_id") is UnitClass.COMMUNITY_ID
    assert classify_metric("community") is UnitClass.COMMUNITY_ID


def test_classify_unknown_falls_to_raw():
    assert classify_metric("score") is UnitClass.RAW
    assert classify_metric("frobnicated_factor") is UnitClass.RAW


# --- cross_check_extracted_pair --------------------------------------------


def test_pair_empty_narrative_approves():
    assert cross_check_extracted_pair([], [], [], CrosscheckConfig()) is None


def test_pair_match_in_claims_approves():
    narr = [_n(12.4, UnitClass.SOL)]
    claims = [_n(12.5, UnitClass.SOL)]
    assert cross_check_extracted_pair(narr, claims, []) is None


def test_pair_match_in_extra_source_approves():
    narr = [_n(33.0, UnitClass.COUNT)]
    extra = [_n(33.0, UnitClass.COUNT)]
    assert cross_check_extracted_pair(narr, [], extra) is None


def test_pair_unsourced_retracts():
    narr = [_n(50000.0, UnitClass.SOL)]
    claims = [_n(12.4, UnitClass.SOL)]
    err = cross_check_extracted_pair(narr, claims, [])
    assert err is not None
    assert err.kind == "unsourced"
    assert err.value == 50000.0
    assert err.unit_class is UnitClass.SOL


def test_pair_unit_class_mismatch_retracts():
    narr = [_n(33.0, UnitClass.SOL)]
    claims = [_n(33.0, UnitClass.COUNT)]
    assert cross_check_extracted_pair(narr, claims, []) is not None


# --- LlmExtractedNumber ----------------------------------------------------


def test_llm_extracted_known_classes():
    cases = [
        ("sol", UnitClass.SOL),
        ("count", UnitClass.COUNT),
        ("community_id", UnitClass.COMMUNITY_ID),
        ("community", UnitClass.COMMUNITY_ID),
    ]
    for s, expected in cases:
        llm = LlmExtractedNumber(value=1.0, unit_class=s)
        assert llm.into_extracted().unit_class is expected, s


def test_llm_extracted_unknown_falls_to_raw():
    llm = LlmExtractedNumber(value=1.0, unit_class="weight")
    assert llm.into_extracted().unit_class is UnitClass.RAW
