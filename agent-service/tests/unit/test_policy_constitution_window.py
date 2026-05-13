"""Unit coverage for `agent_service.policy.constitution`'s live-window
substitution.

The constitution gate's `policy_v4.txt` carries a `${LIVE_WINDOW_HUMAN}`
placeholder so its policy framing ("the agent analyzes a ${window}
live window") matches whatever window the primary agent was actually
built with. If the two prompts drift, the gate retracts correct
narratives at any non-default window because its framing would say
"60 seconds" while the agent narrated, say, "15 minutes".

These tests pin the substitution end-to-end: `_system_prompt(N)`
substitutes correctly, AND `build_constitution_agent(live_window_secs=N)`
threads the value through so the constructed agent carries the
substituted prompt. Both are checked because the gate only stays in
lockstep with the primary agent if the rebuild path is wired
correctly  asserting the formatter alone wouldn't catch a
constructor that ignored its new kwarg.
"""

from __future__ import annotations

from agent_service.policy.constitution import (
    _system_prompt,
    build_constitution_agent,
)


def test_system_prompt_default_renders_60_seconds():
    """Default window (60s) substitutes to the literal "60 seconds",
    matching the historical prompt content byte-for-byte at run
    time. Production preset stays unchanged."""
    out = _system_prompt()
    assert "60 seconds" in out
    assert "${LIVE_WINDOW_HUMAN}" not in out


def test_system_prompt_widened_renders_human_string():
    """Eval-mode widening to 900s substitutes "15 minutes" with no
    leftover "60 seconds" framing  the lockstep invariant the
    composer's own tests pin on the primary side."""
    out = _system_prompt(900)
    assert "15 minutes" in out
    assert "60 seconds" not in out
    assert "${LIVE_WINDOW_HUMAN}" not in out


def test_system_prompt_covers_enum_values():
    """Same coverage shape as the composer's window-substitution
    test, applied to the constitution prompt so a formatter drift
    between the two would fail at least one suite immediately."""
    assert "10 seconds" in _system_prompt(10)
    assert "5 minutes" in _system_prompt(300)
    assert "30 minutes" in _system_prompt(1800)
    assert "1 hour" in _system_prompt(3600)


def test_build_constitution_agent_threads_window_into_prompt():
    """The constructor reads its `live_window_secs` kwarg and feeds
    the substituted prompt into the pydantic-ai Agent's
    `system_prompt`. Without this, the rebuild path in the loop
    driver would silently swallow the window choice and the gate
    would diverge from the primary agent's prompt."""
    agent = build_constitution_agent(live_window_secs=900)
    # pydantic-ai exposes the system prompt(s) on `_system_prompts`
    # (a tuple of static strings + dynamic functions). For static
    # input we get the substituted string in the tuple's static slot.
    # If the internal attribute name shifts on a pydantic-ai bump,
    # fall back to reading the agent's underlying message_history
    # builder; for now this attribute access is the cheapest probe.
    rendered = "\n".join(
        str(p) for p in getattr(agent, "_system_prompts", ())
    )
    assert "15 minutes" in rendered
    assert "60 seconds" not in rendered


def test_build_constitution_agent_default_window_unchanged():
    """No-kwarg construction continues to produce the 60s prompt
    the cached lifespan-built agent the loop driver hands out by
    default stays observably equivalent to today's flat behavior."""
    agent = build_constitution_agent()
    rendered = "\n".join(
        str(p) for p in getattr(agent, "_system_prompts", ())
    )
    assert "60 seconds" in rendered
    assert "${LIVE_WINDOW_HUMAN}" not in rendered
