"""Unit pins on the resource_bounds module.

Post-Phase-2 of the runtime-alignment work the module is just two
sentinel strings; both runtimes pattern-match these on the wire to
detect budget exhaustion. The actual budget counter lives in Rust
(`backend/src/mcp.rs::try_consume_budget`). A drift in either
string breaks the codex / pydantic-ai parity contract; the tests
are the early warning.
"""

from __future__ import annotations

from agent_service.policy import resource_bounds


def test_sentinel_is_structural_not_natural_language():
    """The error-kind sentinel is what `mcp_hook.process_tool_call`
    and `codex_driver._pump_codex_events` grep the structured
    response for. It must be a structured error-kind token
    (snake_case, distinct from anything a primitive would
    legitimately return), not a natural-language phrase that could
    appear in another tool's prose output."""
    assert resource_bounds.NO_MORE_LOOKUPS_ERROR_KIND == "no_more_lookups_this_turn"
    assert " " not in resource_bounds.NO_MORE_LOOKUPS_ERROR_KIND


def test_guidance_carries_model_directive():
    """Both runtimes return Rust's guidance string verbatim; pinning
    the load-bearing phrases here makes a silent reword on the Rust
    side surface as a Python test failure before it ships."""
    g = resource_bounds.NO_MORE_LOOKUPS_GUIDANCE
    assert "tool budget" in g
    assert "Finalize" in g
