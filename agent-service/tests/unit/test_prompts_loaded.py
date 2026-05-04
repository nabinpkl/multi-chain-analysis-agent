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
