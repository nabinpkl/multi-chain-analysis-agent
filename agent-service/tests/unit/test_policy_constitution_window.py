"""Unit coverage for `agent_service.policy.constitution`'s live-window
substitution.

The constitution gate's `policy_v4.txt` carries a `${LIVE_WINDOW_HUMAN}`
placeholder so its policy framing ("the agent analyzes a ${window}
live window") matches whatever window the primary agent was actually
built with. If the two prompts drift, the gate retracts correct
narratives at any non-default window because its framing would say
"60 seconds" while the agent narrated, say, "15 minutes".

These tests pin the formatter end-to-end on each enum value. The
gate itself is stateless function calls (`judge_claim`,
`judge_narrative`) routing through `runtime_call`; the
`live_window_secs` kwarg is forwarded to `_system_prompt(...)` per
call. The end-to-end "kwarg actually reaches the system prompt"
property is covered by the integration smoke (real codex turn) and
by `tests/unit/test_llm_runtime.py` exercising the runtime substrate.
"""

from __future__ import annotations

from agent_service.policy.constitution import _system_prompt


def test_system_prompt_default_renders_60_seconds() -> None:
    """Default window (60s) substitutes to the literal "60 seconds",
    matching the historical prompt content byte-for-byte at run
    time. Production preset stays unchanged."""
    out = _system_prompt()
    assert "60 seconds" in out
    assert "${LIVE_WINDOW_HUMAN}" not in out


def test_system_prompt_widened_renders_human_string() -> None:
    """Eval-mode widening to 900s substitutes "15 minutes" with no
    leftover "60 seconds" framing  the lockstep invariant the
    composer's own tests pin on the primary side."""
    out = _system_prompt(900)
    assert "15 minutes" in out
    assert "60 seconds" not in out
    assert "${LIVE_WINDOW_HUMAN}" not in out


def test_system_prompt_covers_enum_values() -> None:
    """Same coverage shape as the composer's window-substitution
    test, applied to the constitution prompt so a formatter drift
    between the two would fail at least one suite immediately."""
    assert "10 seconds" in _system_prompt(10)
    assert "5 minutes" in _system_prompt(300)
    assert "30 minutes" in _system_prompt(1800)
    assert "1 hour" in _system_prompt(3600)
