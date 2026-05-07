"""Verify the agent-service prompts load and carry the contracts the
rest of the agent code depends on.

Python is the source of truth for prompts after the Phase II migration
(ADR 12). The byte-equality drift detectors that previously chained
this directory back to `backend/src/agent/prompt_v4.txt` were deleted
along with the Rust prompt files in Phase C; what remains is the
loader smoke + the contract assertions that catch silent edits to
the prompt body.
"""

from __future__ import annotations

from agent_service.prompts import PROMPTS_DIR, load_prompt


def test_prompts_dir_resolves():
    assert PROMPTS_DIR.is_dir()
    assert (PROMPTS_DIR / "system_v4.txt").is_file()
    assert (PROMPTS_DIR / "policy_v4.txt").is_file()


def test_load_prompt_returns_non_empty_string():
    """Loader smoke test: both prompts are loadable and non-empty."""
    sys = load_prompt("system_v4")
    pol = load_prompt("policy_v4")
    assert isinstance(sys, str) and len(sys) > 100
    assert isinstance(pol, str) and len(pol) > 100


def test_load_prompt_missing_raises():
    """Typo detection: load_prompt('system_v3') (or any non-existent
    file) raises FileNotFoundError so callers know fast."""
    import pytest

    with pytest.raises(FileNotFoundError):
        load_prompt("does_not_exist_v999")


def test_system_prompt_documents_external_data_boundary():
    """Phase I.4 contract: the prompt teaches the model to treat
    `<external_data>` blocks as data, not instructions. If that
    rule disappears, the wrap_external_data helper loses its
    defense-in-depth claim and we want to know."""
    sys = load_prompt("system_v4")
    assert "<external_data>" in sys, (
        "Prompt no longer mentions <external_data> blocks; "
        "wrap_external_data's defense-in-depth claim is moot. "
        "Either restore the rule or document the change."
    )


def test_system_prompt_documents_context_boundary():
    """Phase I.4 contract: the prompt teaches the model to read the
    `<context>` block first. If that disappears, build_context_block's
    invariant ('treated as ground truth') no longer holds."""
    sys = load_prompt("system_v4")
    assert "<context>" in sys


def test_system_prompt_documents_emit_claim_tool():
    """Phase II contract: the agent's emit_claim tool is named in the
    prompt. Catches accidental rename / removal."""
    sys = load_prompt("system_v4")
    assert "emit_claim" in sys


def test_system_prompt_uses_tagged_rule_structure():
    """#36 contract: the system prompt is structured as XML-tagged
    rules with stable ids, not free-form markdown. The composer in
    `prompts/composer.py` parses these tags to build per-switch
    prompt variants. If the tagged structure is removed (or rule
    ids drift), the composer's drop-by-id surface stops working
    and the article-side ablation switches silently no-op. Lock
    the major id namespace here."""
    sys = load_prompt("system_v4")
    # Top-level scaffolding tags.
    assert "<role>" in sys and "</role>" in sys
    assert "<rules>" in sys and "</rules>" in sys
    assert "<output_format>" in sys and "</output_format>" in sys
    # Defense rules in the per-defense ablation namespace. Each
    # `defense:*` id is wired to a per-defense switch in the loop
    # driver; renaming or removing one needs an aligned switch
    # update.
    for rid in (
        "defense:memo_injection",
        "defense:user_question_untrusted",
        "defense:chat_template_rejection",
        "defense:off_domain",
        "defense:identity",
    ):
        assert f'<rule id="{rid}">' in sys, (
            f"Defense rule id {rid!r} missing from system_v4.txt; "
            "the per-defense ablation surface in `composer.py` "
            "depends on it."
        )


def test_system_prompt_documents_user_question_topical_rail():
    """#33 contract: the prompt teaches the model that the user's
    free-text question is itself untrusted, that persona-swap and
    fictional-game framings are out-of-domain, and that chat-
    template tokens in user input are rejected at the boundary
    before the model sees them. If any of these substrings
    disappear, the boundary defense in `boundary.py`
    (`reject_if_unsafe_user_question`) loses its paired model-side
    guidance and the layered defense degrades to single-layer."""
    sys = load_prompt("system_v4")
    # Persona-swap rule is present.
    assert "persona-swap" in sys or "persona swap" in sys, (
        "Prompt no longer covers persona-swap framings; the "
        "Layer-2 model-side defense for #33 is missing."
    )
    # Boundary-rejection rule is present (paired with the
    # boundary.py `reject_if_unsafe_user_question` helper).
    assert "rejected at the boundary" in sys, (
        "Prompt no longer documents the boundary-rejection contract; "
        "the model has no explicit rule to refuse the turn if the "
        "boundary check ever has a hole."
    )
