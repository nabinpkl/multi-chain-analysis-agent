"""Shared helpers for the two-mode runtime substrate.

When the agent runs under the codex runtime, helper LLM calls
(constitution gate, eval judge, repeat detector) need to route to the
same codex auth path so we don't mix subscription auth with
OpenRouter / Gemini API keys in one run. `runtime_call` is the single
entry point those helpers go through: it dispatches between codex
(via the `openai-codex` SDK on a dedicated helper app-server) and
pydantic-ai (the existing free-tier provider plumbing in
`agent_service.llm`) based on the `AGENT_DEFAULT_RUNTIME` env var.

The helper app-server is separate from the analyst app-server in
`main.py` (its own `AsyncCodex` + `CODEX_HOME`), mirroring the old
`mcae-helper` profile separation: helper threads mount no MCP server
and are ephemeral, so analyst MCP traffic never bleeds into a helper
call.

The codex path uses codex's `output_schema` so the final assistant
message is server-enforced JSON. The pydantic-ai path keeps today's
text-completion + manual-parse shape (we deliberately stay off
pydantic-ai's tool-calling output mode because many free-tier
OpenRouter models don't expose `tool_choice`).

`to_strict_json_schema` rewrites pydantic-emitted JSON Schemas to
satisfy OpenAI's strict structured-output mode (which codex forwards
to). Only used on the codex path; pydantic-ai handles its own
schema massaging.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
from typing import Any, Literal, TypeVar

import structlog
from openai_codex import AsyncCodex, Sandbox
from pydantic import BaseModel, ValidationError
from pydantic_ai import Agent

from agent_service import llm
from agent_service.codex_config import build_codex_config, helper_thread_config
from agent_service.llm_retry import with_provider_retry

log = structlog.get_logger(__name__)

Runtime = Literal["codex", "pydantic_ai"]

T = TypeVar("T", bound=BaseModel)


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


def resolve_helper_runtime() -> Runtime:
    """Map `AGENT_DEFAULT_RUNTIME` onto the helper runtime.

    Default is codex (matches the primary-runtime default in
    `main._resolve_default_runtime`). Explicit `pydantic_ai` opts the
    helpers out of codex so a developer iterating against the
    pydantic-ai primary doesn't accidentally burn codex subscription
    quota on the eval judge.
    """
    raw = os.environ.get("AGENT_DEFAULT_RUNTIME", "").strip().lower()
    if raw in ("pydantic_ai", "pydantic-ai", "agent_runtime_pydantic_ai"):
        return "pydantic_ai"
    return "codex"


# Module-level helper app-server cache. One `AsyncCodex` serves every
# helper call in the process; native ephemeral threads keep codex's
# sqlite session store empty. First call pays the app-server spawn
# cost; subsequent calls reuse the same process. The helper CODEX_HOME
# is distinct from the analyst app-server's so the two codex processes
# never contend on one sqlite.
_helper_codex: AsyncCodex | None = None
_helper_codex_lock = asyncio.Lock()


async def _get_helper_codex() -> AsyncCodex:
    global _helper_codex
    if _helper_codex is None:
        async with _helper_codex_lock:
            if _helper_codex is None:
                codex_home = Path(
                    os.environ.get(
                        "CODEX_HELPER_HOME", "./.cache/codex_helper_home"
                    )
                )
                codex = AsyncCodex(build_codex_config(codex_home=codex_home))
                await codex.__aenter__()
                _helper_codex = codex
    return _helper_codex


def reset_helper_driver_for_testing() -> None:
    """Drop the cached helper app-server. Tests that monkeypatch env
    between cases call this to force a fresh one on the next
    runtime_call. Tests stub `_codex_runtime_call`, so no real
    subprocess exists to close here; the reference is simply cleared."""
    global _helper_codex
    _helper_codex = None


_DECODER = json.JSONDecoder()


class RuntimeCallParseError(ValueError):
    """Raised when a runtime_call response could not be extracted or
    validated as the requested pydantic model. Carries the raw text
    on `.raw_text` so callers can surface it in operator-facing
    diagnostics (probe `observed.raw_response_first_500`, gate
    span attrs, etc.)."""

    def __init__(self, message: str, raw_text: str) -> None:
        super().__init__(message)
        self.raw_text = raw_text


def _parse_strict(raw_text: str, output_model: type[T]) -> T:
    """Parse the first JSON object from `raw_text` and validate against
    `output_model`. Raises `RuntimeCallParseError` with the raw text
    attached on either extraction or validation failure."""
    start = raw_text.find("{")
    if start == -1:
        raise RuntimeCallParseError(
            f"no JSON object in response (first 200 chars): {raw_text[:200]!r}",
            raw_text,
        )
    try:
        parsed, _ = _DECODER.raw_decode(raw_text, start)
    except json.JSONDecodeError as e:
        raise RuntimeCallParseError(
            f"JSON parse failed: {e}; "
            f"text starting at first {{: {raw_text[start:start + 200]!r}",
            raw_text,
        ) from e
    if not isinstance(parsed, dict):
        raise RuntimeCallParseError(
            f"response root is not a JSON object: {type(parsed).__name__}",
            raw_text,
        )
    try:
        return output_model.model_validate(parsed)
    except ValidationError as e:
        raise RuntimeCallParseError(
            f"response did not match {output_model.__name__}: {e}; "
            f"parsed: {parsed!r}",
            raw_text,
        ) from e


async def runtime_call(
    *,
    role: llm.Role,
    system_prompt: str,
    user_prompt: str,
    output_model: type[T],
    runtime: Runtime | None = None,
    model_id: str | None = None,
    llm_override: Any = None,
    per_attempt_timeout_s: float = 45.0,
) -> tuple[T, str]:
    """Run one LLM helper call and return `(instance, raw_text)`.

    Dispatches between codex and pydantic-ai based on `runtime`
    (defaults to `resolve_helper_runtime()`). Both paths return the
    same shape so callers don't branch.

    Codex path: spawns / reuses the cached `mcae-helper` codex
    subprocess, passes `to_strict_json_schema(output_model.model_json_schema())`
    as `outputSchema`, parses the final assistant message. Auth via
    `~/.codex/auth.json` (subscription, no per-call billing). The
    `llm_override` and `model_id` args are honored as a fallback model
    pick; codex's central `CODEX_HELPER_MODEL` env wins when neither
    is set.

    Pydantic-ai path: builds a pydantic-ai `Agent` with `output_type=str`,
    runs it through `with_provider_retry` for transient-failure
    handling, then parses the first JSON object out of the response.
    `llm_override` is forwarded to `llm.make_model` so the dev Models
    panel's per-turn provider + model id selection works the same as
    before; an explicit `model_id` arg wins if both are set.

    Raises `RuntimeCallParseError` (carrying `.raw_text`) on parse /
    validation failure. Caller surfaces it as a probe / gate error.
    """
    chosen = runtime or resolve_helper_runtime()
    if chosen == "codex":
        return await _codex_runtime_call(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            output_model=output_model,
            model_id=model_id,
        )
    return await _pydantic_ai_runtime_call(
        role=role,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        output_model=output_model,
        model_id=model_id,
        llm_override=llm_override,
        per_attempt_timeout_s=per_attempt_timeout_s,
    )


async def _codex_runtime_call(
    *,
    system_prompt: str,
    user_prompt: str,
    output_model: type[T],
    model_id: str | None,
) -> tuple[T, str]:
    codex = await _get_helper_codex()
    schema = to_strict_json_schema(output_model.model_json_schema())
    model = (
        model_id
        or (os.environ.get("CODEX_HELPER_MODEL", "").strip() or None)
    )
    thread = await codex.thread_start(
        sandbox=Sandbox.read_only,
        developer_instructions=system_prompt,
        config=helper_thread_config(),
        ephemeral=True,
        model=model,
    )
    result = await thread.run(user_prompt, output_schema=schema)
    raw_text = result.final_response or ""
    if not raw_text:
        raise RuntimeError("codex turn returned no final message")
    instance = _parse_strict(raw_text, output_model)
    return instance, raw_text


async def _pydantic_ai_runtime_call(
    *,
    role: llm.Role,
    system_prompt: str,
    user_prompt: str,
    output_model: type[T],
    model_id: str | None,
    llm_override: Any,
    per_attempt_timeout_s: float,
) -> tuple[T, str]:
    agent: Agent[None, str] = Agent(
        model=llm.make_model(role, override=llm_override, model_id=model_id),
        output_type=str,
        system_prompt=system_prompt,
    )
    result = await with_provider_retry(
        lambda: agent.run(user_prompt),
        label=f"runtime_call:{role}",
        per_attempt_timeout_s=per_attempt_timeout_s,
    )
    raw_text: str = result.output
    instance = _parse_strict(raw_text, output_model)
    return instance, raw_text
