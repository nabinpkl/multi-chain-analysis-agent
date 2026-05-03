"""Verify the agent-service prompts are byte-identical copies of the
Rust sources. If someone edits one side and forgets `just sync-prompts`,
this test fails.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from agent_service.prompts import PROMPTS_DIR, load_prompt


# Rust prompt source paths, computed relative to this test file
# (tests/unit/test_prompts_loaded.py -> agent-service -> repo root).
_REPO_ROOT = Path(__file__).resolve().parents[3]
RUST_SYSTEM_PROMPT = _REPO_ROOT / "backend" / "src" / "agent" / "prompt_v4.txt"
RUST_POLICY_PROMPT = _REPO_ROOT / "backend" / "src" / "agent" / "policy_prompt_v4.txt"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_prompts_dir_resolves():
    assert PROMPTS_DIR.is_dir()
    assert (PROMPTS_DIR / "system_v4.txt").is_file()
    assert (PROMPTS_DIR / "policy_v4.txt").is_file()


def test_load_prompt_returns_non_empty_string():
    """Loader smoke test: both prompts are loadable and non-empty.
    Doesn't assert content (that's covered by the byte-equal test)."""
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


def test_system_prompt_byte_equal_to_rust_source():
    """Drift detector. If Rust prompt_v4.txt changes and someone forgets
    to copy it over, this fails. Use sha256 (hex) so the failure
    message gives an actionable diff hint."""
    assert RUST_SYSTEM_PROMPT.is_file(), (
        f"Rust source missing: {RUST_SYSTEM_PROMPT}"
    )
    py_path = PROMPTS_DIR / "system_v4.txt"
    rust_hash = _sha256(RUST_SYSTEM_PROMPT)
    py_hash = _sha256(py_path)
    assert rust_hash == py_hash, (
        f"system_v4.txt byte mismatch:\n"
        f"  Rust ({RUST_SYSTEM_PROMPT}): {rust_hash}\n"
        f"  Py   ({py_path}): {py_hash}\n"
        f"Run `just sync-prompts` to refresh the Python copy."
    )


def test_policy_prompt_byte_equal_to_rust_source():
    """Same drift detector for the constitution-gate prompt."""
    assert RUST_POLICY_PROMPT.is_file()
    py_path = PROMPTS_DIR / "policy_v4.txt"
    rust_hash = _sha256(RUST_POLICY_PROMPT)
    py_hash = _sha256(py_path)
    assert rust_hash == py_hash, (
        f"policy_v4.txt byte mismatch:\n"
        f"  Rust ({RUST_POLICY_PROMPT}): {rust_hash}\n"
        f"  Py   ({py_path}): {py_hash}\n"
        f"Run `just sync-prompts` to refresh the Python copy."
    )


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
