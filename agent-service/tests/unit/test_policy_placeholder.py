"""Parity tests for the placeholder gate. Mirror of the Rust
`policy_placeholder.rs` test set so the port is verifiable case-by-case."""

from __future__ import annotations

from agent_service.policy.placeholder import RefError, count_refs, validate_refs


def test_validates_in_bounds():
    text = "Wallet ${ref:0} has ${ref:2} connections."
    assert validate_refs(text, 3) is None


def test_rejects_out_of_bounds():
    text = "Wallet ${ref:5} has connections."
    err = validate_refs(text, 3)
    assert err is not None
    assert err.kind == "out_of_bounds"
    assert err.index == 5
    assert err.provenance_len == 3


def test_rejects_first_out_of_bounds_when_multiple_refs():
    """First-error semantics matches Rust gate: surface first failure
    in document order, model self-corrects on retry."""
    text = "${ref:0} then ${ref:99}"
    err = validate_refs(text, 1)
    assert err is not None
    assert err.kind == "out_of_bounds"
    assert err.index == 99


def test_empty_provenance_with_any_ref_retracts():
    text = "Wallet ${ref:0}."
    err = validate_refs(text, 0)
    assert err is not None
    assert err.kind == "out_of_bounds"
    assert err.index == 0
    assert err.provenance_len == 0


def test_no_refs_at_all_approves():
    text = "The wallet has 3 distinguishing properties."
    assert validate_refs(text, 0) is None
    assert validate_refs(text, 5) is None


def test_unicode_safe_apostrophe():
    """Curly apostrophe (U+2019, 3 bytes UTF-8). Python regex on str is
    char-boundary safe, mirroring Rust's behavior. Must not panic."""
    text = "I’m looking at wallet ${ref:0}it has ${ref:1} connections."
    assert validate_refs(text, 2) is None


def test_unicode_safe_em_dash():
    text = "Wallet${ref:0}has activity."
    assert validate_refs(text, 1) is None


def test_count_refs_with_repeats():
    text = "${ref:0} and ${ref:0} again, plus ${ref:1}."
    assert count_refs(text) == 3


def test_count_refs_zero_when_no_refs():
    assert count_refs("plain prose") == 0
    assert count_refs("") == 0


def test_malformed_token_skipped():
    """Malformed grammar variants don't match; gate doesn't accept lenient
    forms. Constitution gate catches the bare audit number that would
    result. Dual safety net."""
    text = "Wallet $(ref:0) has activity, ${REF:1} somewhere."
    assert validate_refs(text, 0) is None
    assert count_refs(text) == 0


def test_human_string_singular_vs_plural():
    s = RefError(kind="out_of_bounds", index=3, provenance_len=1).to_human_string()
    assert "1 entry" in s, s
    s = RefError(kind="out_of_bounds", index=3, provenance_len=4).to_human_string()
    assert "4 entries" in s, s
