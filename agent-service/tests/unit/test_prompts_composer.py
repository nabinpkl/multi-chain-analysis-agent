"""Tests for the system-prompt composer in `agent_service.prompts.composer`.

The composer parses `<rule id="...">` blocks out of `system_v4.txt`
and drops by id; the production preset uses an empty drop set and
must stay byte-identical to the source file. These tests pin:

- empty drop set returns the source verbatim (the byte-identity
  guarantee that protects every committed eval baseline);
- dropping a real rule removes it and only it;
- dropping multiple rules works as a set union;
- unknown drop ids raise loudly so a typo'd switch wiring fails fast;
- duplicate rule ids in the source raise loudly so paste accidents
  cannot silently shadow a defense rule;
- `known_rule_ids` enumerates the declared ids for callers that want
  to pre-validate switch maps at startup.
"""

from __future__ import annotations

import pytest

from agent_service.prompts import load_prompt
from agent_service.prompts.composer import (
    CompositionError,
    compose_system_prompt,
    known_rule_ids,
)


# ---------------------------------------------------------------------------
# Tiny fixture for the dropping/duplicate/unknown-id behaviors.
#
# Using a fixture instead of the live `system_v4.txt` keeps these tests
# focused on the parser shape: changes to the real prompt's wording
# can't accidentally break parser-contract tests.
# ---------------------------------------------------------------------------


_FIXTURE = """<role>
You are a test agent.
</role>

<rules>

<rule id="alpha">
# Alpha
First rule body.
</rule>

<rule id="beta">
# Beta
Second rule body.
</rule>

<rule id="defense:gamma">
# Gamma
Third rule body. Mentions `<|im_start|>` literally inside its body.
</rule>

</rules>

<output_format>
# Style
Trailing.
</output_format>
"""


# ---------------------------------------------------------------------------
# Identity / round-trip
# ---------------------------------------------------------------------------


def test_compose_empty_drop_set_returns_source_verbatim_fixture():
    """The byte-identity guarantee on a controlled fixture."""
    assert compose_system_prompt(source_text=_FIXTURE) == _FIXTURE


def test_compose_empty_drop_set_returns_real_prompt_verbatim():
    """The byte-identity guarantee on the real prompt. This is the
    regression guard for production preset == today's flat behavior:
    if anyone refactors the composer or the prompt in a way that
    breaks identity for the empty drop set, every committed eval
    baseline could silently shift."""
    raw = load_prompt("system_v4")
    assert compose_system_prompt() == raw


# ---------------------------------------------------------------------------
# Drops
# ---------------------------------------------------------------------------


def test_compose_drops_single_rule():
    out = compose_system_prompt(
        source_text=_FIXTURE, drop_rule_ids=["beta"]
    )
    # The dropped rule's body is gone.
    assert "Second rule body." not in out
    # The other rules survive.
    assert "First rule body." in out
    assert "Third rule body." in out
    # The surrounding scaffolding survives.
    assert "<role>" in out
    assert "<rules>" in out
    assert "<output_format>" in out


def test_compose_drops_multiple_rules():
    out = compose_system_prompt(
        source_text=_FIXTURE, drop_rule_ids=["alpha", "defense:gamma"]
    )
    assert "First rule body." not in out
    assert "Third rule body." not in out
    # The middle rule survives.
    assert "Second rule body." in out


def test_compose_preserves_rule_order_after_drop():
    """Dropping a middle rule does not reorder the surrounding ones.
    Catches any "collect, mutate, re-emit" refactor that might
    accidentally shuffle output."""
    out = compose_system_prompt(
        source_text=_FIXTURE, drop_rule_ids=["beta"]
    )
    alpha_pos = out.index("First rule body.")
    gamma_pos = out.index("Third rule body.")
    assert alpha_pos < gamma_pos


def test_compose_drops_real_defense_rule():
    """Smoke-test against the live prompt: dropping a real defense
    rule actually removes its prose. Pins the rule-id namespace
    against silent renames in `system_v4.txt`."""
    full = compose_system_prompt()
    dropped = compose_system_prompt(drop_rule_ids=["defense:identity"])
    # Identity-rule prose should be gone; benign-rule prose survives.
    assert "do NOT name the underlying LLM" in full
    assert "do NOT name the underlying LLM" not in dropped
    # The role block at the top still exists.
    assert "<role>" in dropped


# ---------------------------------------------------------------------------
# Loud failures
# ---------------------------------------------------------------------------


def test_compose_unknown_drop_id_raises():
    """A typo in a switch-to-rule mapping (`defense:offdomain` instead
    of `defense:off_domain`) would silently no-op without this guard
    and the agent would falsely report defense success."""
    with pytest.raises(CompositionError) as ei:
        compose_system_prompt(
            source_text=_FIXTURE, drop_rule_ids=["defense:does_not_exist"]
        )
    assert "defense:does_not_exist" in str(ei.value)


def test_compose_duplicate_rule_id_raises():
    """An editor pasting a rule twice should fail loudly on load."""
    duplicate_fixture = _FIXTURE.replace(
        '<rule id="beta">',
        '<rule id="alpha">',  # second `alpha` instead of `beta`
        1,
    )
    with pytest.raises(CompositionError) as ei:
        compose_system_prompt(source_text=duplicate_fixture)
    assert "alpha" in str(ei.value)


def test_compose_unknown_drop_id_lists_known_ids_for_diagnostics():
    """Error messages should help the caller fix the typo. Locking
    this so future refactors keep the help text useful."""
    with pytest.raises(CompositionError) as ei:
        compose_system_prompt(
            source_text=_FIXTURE, drop_rule_ids=["typo"]
        )
    msg = str(ei.value)
    # The message names the unknown id and at least one known id so
    # the caller sees both 'what was wrong' and 'what's valid'.
    assert "typo" in msg
    assert "alpha" in msg or "beta" in msg or "defense:gamma" in msg


# ---------------------------------------------------------------------------
# known_rule_ids enumeration
# ---------------------------------------------------------------------------


def test_known_rule_ids_lists_fixture_ids():
    ids = known_rule_ids(source_text=_FIXTURE)
    assert ids == frozenset({"alpha", "beta", "defense:gamma"})


def test_known_rule_ids_lists_real_defense_namespace():
    """Smoke against the live prompt: the per-defense ablation
    surface (`defense:*` rules the article will toggle) is present.
    Pins the rule-id namespace contract."""
    ids = known_rule_ids()
    expected_defense_ids = {
        "defense:memo_injection",
        "defense:user_question_untrusted",
        "defense:chat_template_rejection",
        "defense:off_domain",
        "defense:identity",
    }
    missing = expected_defense_ids - ids
    assert not missing, (
        f"Expected defense rule ids missing from system_v4.txt: "
        f"{sorted(missing)}. The composer's switch map relies on "
        f"these ids. Either restore them or update the article-side "
        f"switch wiring."
    )


def test_known_rule_ids_raises_on_duplicate():
    """Same loud-failure contract as compose_system_prompt; pinned
    here so callers can pre-validate at startup without composing."""
    duplicate_fixture = _FIXTURE.replace(
        '<rule id="beta">',
        '<rule id="alpha">',
        1,
    )
    with pytest.raises(CompositionError):
        known_rule_ids(source_text=duplicate_fixture)
