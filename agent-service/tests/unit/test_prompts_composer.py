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

from multichain.wire.agent.v1 import switches_pb2 as sw_pb

from agent_service.prompts import load_prompt
from agent_service.prompts.composer import (
    CompositionError,
    compose_system_prompt,
    drops_from_switches,
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
    """The byte-identity guarantee on the real prompt, modulo the
    `${LIVE_WINDOW_HUMAN}` placeholder substitution that's the only
    intentional transformation on the default-window path. With the
    default 60s window the placeholder renders as "60 seconds"  the
    exact literal text the prompt carried before parameterization
    so the production preset is observably equivalent to today's
    flat behavior. If anyone refactors the composer in a way that
    breaks this invariant for the empty drop set, every committed
    eval baseline could silently shift."""
    raw = load_prompt("system_v4")
    expected = raw.replace("${LIVE_WINDOW_HUMAN}", "60 seconds")
    assert compose_system_prompt() == expected
    # And: an unwidened compose still contains the literal "60
    # seconds" (the placeholder was replaced).
    assert "60 seconds" in compose_system_prompt()
    assert "${LIVE_WINDOW_HUMAN}" not in compose_system_prompt()


def test_compose_widens_live_window_substitutes_human_string():
    """Widening to 900s renders "15 minutes" everywhere the
    placeholder appears, with no leftover "60 seconds" string. Same
    coverage shape for the other enum values guards against
    formatter drift."""
    out_900 = compose_system_prompt(live_window_secs=900)
    assert "15 minutes" in out_900
    assert "60 seconds" not in out_900
    assert "${LIVE_WINDOW_HUMAN}" not in out_900

    out_300 = compose_system_prompt(live_window_secs=300)
    assert "5 minutes" in out_300
    assert "60 seconds" not in out_300

    out_3600 = compose_system_prompt(live_window_secs=3600)
    assert "1 hour" in out_3600
    assert "60 seconds" not in out_3600


def test_compose_unknown_window_falls_back_to_seconds_suffix():
    """An off-enum value (which Rust would 400 on, but the composer
    is permissive so future enum extensions don't crash the prompt
    path) renders as a bare "N seconds" string. Substitution still
    happens; the placeholder never leaks into the rendered prompt."""
    out = compose_system_prompt(live_window_secs=7200)
    assert "7200 seconds" in out
    assert "${LIVE_WINDOW_HUMAN}" not in out


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
        "defense:external_data",
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


# ---------------------------------------------------------------------------
# drops_from_switches: switches sub-message -> rule-id drop set
# ---------------------------------------------------------------------------


def _all_on_switches() -> sw_pb.AgentSwitches:
    """Production preset: every defense in StayInRoleSwitches is on."""
    return sw_pb.AgentSwitches(
        stay_in_role=sw_pb.StayInRoleSwitches(
            defend_chat_template_spoofing=True,
            defend_constitution_judge=True,
            defend_persona_swap=True,
            defend_decode_and_execute=True,
            defend_identity_reveal=True,
            defend_off_domain=True,
        ),
    )


def test_drops_from_switches_production_preset_drops_nothing():
    """Production: all sub-defenses on -> empty drop set -> prompt
    is the unmodified file. The byte-identity guarantee depends on
    this returning frozenset()."""
    assert drops_from_switches(_all_on_switches()) == frozenset()


def test_drops_from_switches_chat_template_off_drops_rule():
    s = _all_on_switches()
    s.stay_in_role.defend_chat_template_spoofing = False
    assert drops_from_switches(s) == frozenset({"defense:chat_template_rejection"})


def test_drops_from_switches_persona_swap_alone_off_keeps_rule():
    """The user_question_untrusted rule covers BOTH persona-swap
    and decode-and-execute. Either alone off -> rule stays."""
    s = _all_on_switches()
    s.stay_in_role.defend_persona_swap = False
    assert drops_from_switches(s) == frozenset()


def test_drops_from_switches_decode_alone_off_keeps_rule():
    """Mirror of the persona-swap test."""
    s = _all_on_switches()
    s.stay_in_role.defend_decode_and_execute = False
    assert drops_from_switches(s) == frozenset()


def test_drops_from_switches_persona_and_decode_both_off_drops_rule():
    """Both off -> drop the shared rule."""
    s = _all_on_switches()
    s.stay_in_role.defend_persona_swap = False
    s.stay_in_role.defend_decode_and_execute = False
    assert drops_from_switches(s) == frozenset(
        {"defense:user_question_untrusted"}
    )


def test_drops_from_switches_identity_off_drops_rule():
    s = _all_on_switches()
    s.stay_in_role.defend_identity_reveal = False
    assert drops_from_switches(s) == frozenset({"defense:identity"})


def test_drops_from_switches_off_domain_off_drops_rule():
    s = _all_on_switches()
    s.stay_in_role.defend_off_domain = False
    assert drops_from_switches(s) == frozenset({"defense:off_domain"})


def test_drops_from_switches_constitution_judge_does_not_drop_any_rule():
    """`defend_constitution_judge` gates the gate spans, not the
    prompt content. Pinned so a future refactor doesn't accidentally
    couple it to a rule drop and double-disable on switch off."""
    s = _all_on_switches()
    s.stay_in_role.defend_constitution_judge = False
    assert drops_from_switches(s) == frozenset()


def test_drops_from_switches_all_off_drops_every_defense_rule():
    """End-to-end ablation: every defense in StayInRoleSwitches off
    -> the full set of `defense:*` rules covered by the mapping is
    dropped. Acts as a regression test for the rule-namespace
    contract: if a new defense rule is added without wiring its
    drop here, this test surfaces it."""
    s = sw_pb.AgentSwitches(
        stay_in_role=sw_pb.StayInRoleSwitches(
            defend_chat_template_spoofing=False,
            defend_constitution_judge=False,
            defend_persona_swap=False,
            defend_decode_and_execute=False,
            defend_identity_reveal=False,
            defend_off_domain=False,
        ),
    )
    assert drops_from_switches(s) == frozenset(
        {
            "defense:chat_template_rejection",
            "defense:user_question_untrusted",
            "defense:identity",
            "defense:off_domain",
        }
    )


def test_drops_from_switches_drops_match_known_rule_ids():
    """Sanity: every rule id we drop actually exists in the source
    file. Without this, a typo in `drops_from_switches` would
    silently no-op until someone toggled the switch."""
    ids = known_rule_ids()
    s = sw_pb.AgentSwitches(
        stay_in_role=sw_pb.StayInRoleSwitches(),  # all defaults false
    )
    drops = drops_from_switches(s)
    missing = drops - ids
    assert not missing, (
        f"drops_from_switches references rule ids not present in "
        f"system_v4.txt: {sorted(missing)}. Either rename the rule "
        f"or fix the mapping."
    )


# ---------------------------------------------------------------------------
# end drops_from_switches
# ---------------------------------------------------------------------------


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
