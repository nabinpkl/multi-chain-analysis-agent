"""Unit tests for `agent_service.llm_runtime`.

Covers the deterministic pieces of the two-mode runtime substrate:

- `to_strict_json_schema` rewrites pydantic schemas to satisfy
  OpenAI strict mode (additionalProperties: false everywhere, full
  required, no `default` keys).
- `_parse_strict` extracts the first JSON object from a text response
  and validates it against a pydantic model, raising
  `RuntimeCallParseError` (carrying the raw text) on any failure.
- `resolve_helper_runtime` maps the `AGENT_DEFAULT_RUNTIME` env var
  onto the runtime backend.

The codex and pydantic-ai backends themselves are exercised via the
smoke script (`scripts/smoke_codex_output_schema.py`) and the
per-probe integration tests; here we keep to the deterministic
substrate so the suite stays CPU-bound and fast.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, Field

from agent_service.llm_runtime import (
    RuntimeCallParseError,
    _parse_strict,
    resolve_helper_runtime,
    to_strict_json_schema,
)


# ---------------------------------------------------------------------------
# to_strict_json_schema
# ---------------------------------------------------------------------------


class _Inner(BaseModel):
    a: int
    b: str = "default-b"


class _Outer(BaseModel):
    inner: _Inner
    note: str = ""
    maybe: _Inner | None = None


def test_strict_walker_stamps_additional_properties_false_on_every_object() -> None:
    """The walker must hit nested $defs entries AND the root object,
    leaving no object subschema without the flag."""
    out = to_strict_json_schema(_Outer.model_json_schema())
    assert out["additionalProperties"] is False
    for name, sub in out["$defs"].items():
        if sub.get("type") == "object" or "properties" in sub:
            assert sub["additionalProperties"] is False, name


def test_strict_walker_widens_required_to_all_properties() -> None:
    """Pydantic only lists fields without defaults in `required`.
    Strict mode demands every property be required; defaults are
    irrelevant on the wire (caller applies them in model_validate)."""
    out = to_strict_json_schema(_Outer.model_json_schema())
    assert set(out["required"]) == {"inner", "note", "maybe"}
    inner_def = out["$defs"]["_Inner"]
    assert set(inner_def["required"]) == {"a", "b"}


def test_strict_walker_strips_default_keys() -> None:
    """`default` is not allowed anywhere in a strict-mode schema."""
    out = to_strict_json_schema(_Outer.model_json_schema())
    inner_def = out["$defs"]["_Inner"]
    assert "default" not in inner_def["properties"]["b"]
    assert "default" not in out["properties"]["note"]


def test_strict_walker_does_not_mutate_input() -> None:
    """Walker must deep-copy so callers can keep using the original
    pydantic-emitted schema after the rewrite."""
    original = _Outer.model_json_schema()
    snapshot = original.copy()
    _ = to_strict_json_schema(original)
    assert original == snapshot


def test_strict_walker_preserves_nullable_via_anyof() -> None:
    """Nullable fields use `anyOf: [..., {"type": "null"}]`. The walker
    must NOT touch that shape since it is strict-mode-legal."""
    out = to_strict_json_schema(_Outer.model_json_schema())
    maybe_field = out["properties"]["maybe"]
    assert "anyOf" in maybe_field
    types = {alt.get("type") for alt in maybe_field["anyOf"] if "type" in alt}
    assert "null" in types


# ---------------------------------------------------------------------------
# _parse_strict
# ---------------------------------------------------------------------------


class _Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0.0, le=1.0)
    reason: str


def test_parse_strict_happy_path() -> None:
    instance = _parse_strict('{"score": 0.9, "reason": "ok"}', _Verdict)
    assert instance.score == 0.9
    assert instance.reason == "ok"


def test_parse_strict_extracts_json_from_surrounding_prose() -> None:
    """Many free-tier models prepend / append commentary even when
    asked not to. The parser finds the first `{` and reads exactly
    one JSON value from there."""
    text = (
        "Sure, here's my evaluation:\n\n"
        '{"score": 0.85, "reason": "on-topic"}\n\n'
        "Hope this helps!"
    )
    instance = _parse_strict(text, _Verdict)
    assert instance.score == 0.85


def test_parse_strict_handles_braces_inside_string_values() -> None:
    """A non-greedy regex stops at the inner `}` of `${ref:0}` in a
    string value; `json.JSONDecoder.raw_decode` handles balanced
    braces correctly. This pins that we never regress to a regex."""
    text = (
        '{"score": 1.0, "reason": "cites audit values via ${ref:0} and ${ref:1}"}'
    )
    instance = _parse_strict(text, _Verdict)
    assert instance.score == 1.0
    assert "${ref:0}" in instance.reason


def test_parse_strict_raises_with_raw_text_when_no_json() -> None:
    text = "I think this looks good but I'm not sure."
    with pytest.raises(RuntimeCallParseError) as excinfo:
        _parse_strict(text, _Verdict)
    assert excinfo.value.raw_text == text
    assert "no JSON object" in str(excinfo.value)


def test_parse_strict_raises_with_raw_text_on_invalid_json() -> None:
    text = '{"score": 0.9, "reason":'
    with pytest.raises(RuntimeCallParseError) as excinfo:
        _parse_strict(text, _Verdict)
    assert excinfo.value.raw_text == text
    assert "JSON parse failed" in str(excinfo.value)


def test_parse_strict_raises_with_raw_text_on_validation_failure() -> None:
    """JSON parses but field constraints fail (score out of range,
    unknown field due to extra=forbid)."""
    text = '{"score": 1.5, "reason": "ok"}'
    with pytest.raises(RuntimeCallParseError) as excinfo:
        _parse_strict(text, _Verdict)
    assert excinfo.value.raw_text == text
    assert "did not match" in str(excinfo.value)


# ---------------------------------------------------------------------------
# resolve_helper_runtime
# ---------------------------------------------------------------------------


def test_resolve_helper_runtime_defaults_to_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_DEFAULT_RUNTIME", raising=False)
    assert resolve_helper_runtime() == "codex"


def test_resolve_helper_runtime_pydantic_ai_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accept the bare-suffix form and the full proto-name form."""
    for raw in ("pydantic_ai", "pydantic-ai", "AGENT_RUNTIME_PYDANTIC_AI"):
        monkeypatch.setenv("AGENT_DEFAULT_RUNTIME", raw)
        assert resolve_helper_runtime() == "pydantic_ai"


def test_resolve_helper_runtime_unrecognized_falls_back_to_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typos fall back to codex (matches the primary-runtime default).
    Logged elsewhere; here we just pin the behavior."""
    monkeypatch.setenv("AGENT_DEFAULT_RUNTIME", "anthropic-claude")
    assert resolve_helper_runtime() == "codex"
