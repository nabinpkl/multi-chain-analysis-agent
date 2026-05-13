"""Shared helpers for the two-mode runtime substrate.

When the agent runs under the codex runtime, helper LLM calls
(constitution gate, eval judge, repeat detector) need a way to hand
codex a JSON Schema that codex/OpenAI accepts in strict structured-
output mode. Pydantic-generated schemas almost work but miss the
strict-mode tightening flags: `additionalProperties: false` on every
object, full `required` arrays, no `default` values. `to_strict_json_schema`
walks a pydantic-emitted schema and applies those rewrites in a deep
copy so the original model definition is untouched.

The pydantic-ai runtime path does NOT go through here; pydantic-ai
handles structured output internally via its own model adapters.
This module is the codex-runtime-only equivalent.

Future `runtime_call()` helper will live here too.
"""

from __future__ import annotations

import copy
from typing import Any


def to_strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of `schema` rewritten for OpenAI strict mode.

    OpenAI's structured-output strict mode (which codex forwards to)
    demands three things that pydantic-emitted schemas do not always
    provide:

    1. `additionalProperties: false` on every object subschema.
       Pydantic only emits this for models with `extra="forbid"`;
       models using the default `extra="ignore"` omit it.
    2. Every key in `properties` must appear in `required`. Pydantic
       only lists fields without defaults in `required`.
    3. `default` keys are not allowed anywhere. Pydantic emits them
       for fields with defaults.

    The walker enforces all three. Nullable fields keep their
    `anyOf: [..., {"type": "null"}]` shape (strict-mode-legal). The
    pydantic side of the round-trip continues to apply defaults when
    a value is missing from the model JSON output, so widening
    `required` here does not break model validation; it only changes
    what we ask the LLM to emit.
    """
    return _walk(copy.deepcopy(schema))


def _walk(node: Any) -> Any:
    if isinstance(node, dict):
        node.pop("default", None)
        is_object_schema = node.get("type") == "object" or "properties" in node
        if is_object_schema:
            props = node.get("properties") or {}
            node["additionalProperties"] = False
            node["required"] = list(props.keys())
        for key in list(node.keys()):
            node[key] = _walk(node[key])
        return node
    if isinstance(node, list):
        return [_walk(item) for item in node]
    return node
