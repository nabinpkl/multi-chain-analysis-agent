"""Shared helpers for the two-mode runtime substrate.

When the agent runs under the codex runtime, helper LLM calls
(constitution gate, eval judge, repeat detector) need to route to the
same codex auth path so we don't mix subscription auth with
OpenRouter / Gemini API keys in one run. `runtime_call` is the single
entry point those helpers go through: it dispatches between codex
(via `codex-agent-driver` against a dedicated `mcae-helper` profile)
and pydantic-ai (the existing free-tier provider plumbing in
`agent_service.llm`) based on the `AGENT_DEFAULT_RUNTIME` env var.

The codex path uses codex's `outputSchema` so the final assistant
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
from codex_agent_driver import (
    CodexAppServerDriver,
    CodexRunEventType,
    CodexRunRequest,
)
from pydantic import BaseModel, ValidationError
from pydantic_ai import Agent

from agent_service import llm
from agent_service.codex_profile import build_codex_helper_profile
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


# Module-level driver cache. The helper profile is identical across
# every helper call so one driver instance serves them all; codex's
# session pool reuses the underlying subprocess across requests with
# matching actor_id + cwd + codex_home. First call pays the spawn
# cost; subsequent calls within the process amortize it.
_helper_driver: CodexAppServerDriver | None = None


def _get_helper_driver() -> CodexAppServerDriver:
    global _helper_driver
    if _helper_driver is None:
        cwd = Path.cwd()
        codex_home_root = Path(
            os.environ.get("CODEX_HOME_ROOT", "./codex_homes")
        )
        codex_home_root.mkdir(parents=True, exist_ok=True)
        profile = build_codex_helper_profile(cwd=cwd)
        _helper_driver = CodexAppServerDriver(
            profile=profile,
            codex_home_root=codex_home_root,
        )
    return _helper_driver


def reset_helper_driver_for_testing() -> None:
    """Drop the cached driver. Tests that monkeypatch env between
    cases call this to force a fresh driver on the next runtime_call."""
    global _helper_driver
    if _helper_driver is not None:
        _helper_driver.close()
    _helper_driver = None


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
    per_attempt_timeout_s: float = 45.0,
) -> tuple[T, str]:
    """Run one LLM helper call and return `(instance, raw_text)`.

    Dispatches between codex and pydantic-ai based on `runtime`
    (defaults to `resolve_helper_runtime()`). Both paths return the
    same shape so callers don't branch.

    Codex path: spawns / reuses the cached `mcae-helper` codex
    subprocess, passes `to_strict_json_schema(output_model.model_json_schema())`
    as `outputSchema`, parses the final assistant message. Auth via
    `~/.codex/auth.json` (subscription, no per-call billing).

    Pydantic-ai path: builds a pydantic-ai `Agent` with `output_type=str`,
    runs it through `with_provider_retry` for transient-failure
    handling, then parses the first JSON object out of the response.

    Raises `ValueError` on parse / validation failure with the raw
    text in the message. Caller surfaces it as a probe / gate error.
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
        per_attempt_timeout_s=per_attempt_timeout_s,
    )


async def _codex_runtime_call(
    *,
    system_prompt: str,
    user_prompt: str,
    output_model: type[T],
    model_id: str | None,
) -> tuple[T, str]:
    driver = _get_helper_driver()
    schema = to_strict_json_schema(output_model.model_json_schema())
    model = (
        model_id
        or (os.environ.get("CODEX_HELPER_MODEL", "").strip() or None)
    )
    request = CodexRunRequest(
        prompt=user_prompt,
        actor_id="helper",
        developer_instructions=system_prompt,
        ephemeral=True,
        output_schema=schema,
        model=model,
    )

    def _drain() -> str:
        final_text: str | None = None
        for event in driver.stream(request):
            if event.type is CodexRunEventType.MESSAGE_COMPLETED:
                final_text = event.final_text or ""
                break
        if final_text is None:
            raise RuntimeError("codex stream ended without MESSAGE_COMPLETED")
        return final_text

    raw_text = await asyncio.to_thread(_drain)
    instance = _parse_strict(raw_text, output_model)
    return instance, raw_text


async def _pydantic_ai_runtime_call(
    *,
    role: llm.Role,
    system_prompt: str,
    user_prompt: str,
    output_model: type[T],
    model_id: str | None,
    per_attempt_timeout_s: float,
) -> tuple[T, str]:
    agent: Agent[None, str] = Agent(
        model=llm.make_model(role, model_id=model_id),
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
