"""Codex app-server notification parsing.

The `openai-codex` SDK streams `Notification{method, payload}` objects
where `payload` is a generated pydantic model. We do not bind to those
beta-version-specific typed attribute paths. Instead we dump each
payload back to its camelCase wire dict and parse it here, with the
same defensive logic the codex app-server protocol has used across CLI
versions. `model_dump(by_alias=True, mode="json")` reproduces exactly
the `params` shape the codex JSON-RPC notification carried on the wire
(`itemId`, `delta`, `item`, `tokenUsage`, `turn`, ...), so this parser
is insulated from SDK typed-API churn (see
`docs/dependency-exceptions.md`).

`run_turn_codex` consumes the `CodexRunEvent` shape this module emits;
the field set is the subset the driver loop reads.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from openai_codex.types import Notification


class CodexRunEventType(StrEnum):
    TEXT_DELTA = "text_delta"
    REASONING_RAW_DELTA = "reasoning_raw_delta"
    REASONING_SUMMARY_DELTA = "reasoning_summary_delta"
    TOKEN_USAGE_UPDATED = "token_usage_updated"
    MESSAGE_COMPLETED = "message_completed"
    TOOL_STARTED = "tool_started"
    TOOL_OUTPUT_DELTA = "tool_output_delta"
    TOOL_COMPLETED = "tool_completed"


@dataclass(slots=True, frozen=True)
class CodexTokenUsageBreakdown:
    total_tokens: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int


@dataclass(slots=True, frozen=True)
class CodexTokenUsage:
    total: CodexTokenUsageBreakdown
    last: CodexTokenUsageBreakdown
    model_context_window: int | None = None


@dataclass(slots=True, frozen=True)
class CodexRunEvent:
    type: CodexRunEventType
    provider_thread_id: str | None = None
    message_id: str | None = None
    tool_id: str | None = None
    text: str | None = None
    output: str | None = None
    final_text: str | None = None
    token_usage: CodexTokenUsage | None = None
    status: str | None = None
    raw_event: dict[str, Any] = field(default_factory=dict)


def notification_to_raw(notification: Notification) -> dict[str, Any]:
    """Reconstruct the `{method, params}` JSON-RPC dict from an SDK
    `Notification`. `params` is the payload dumped to its camelCase wire
    shape, which is what every helper below reads from."""
    params = notification.payload.model_dump(by_alias=True, mode="json")
    return {"method": notification.method, "params": params}


def event_from_notification(message: dict[str, Any]) -> CodexRunEvent | None:
    method = message.get("method")
    params = message.get("params")
    if not isinstance(method, str) or not isinstance(params, dict):
        return None

    if method == "item/agentMessage/delta":
        return CodexRunEvent(
            type=CodexRunEventType.TEXT_DELTA,
            message_id=_string(params.get("itemId")),
            text=_string(params.get("delta")) or "",
            raw_event=message,
        )
    if method in ("item/reasoning/textDelta", "item/reasoning/rawContentDelta"):
        return CodexRunEvent(
            type=CodexRunEventType.REASONING_RAW_DELTA,
            message_id=_string(params.get("itemId")),
            text=_reasoning_text(params),
            raw_event=message,
        )
    if method == "item/reasoning/summaryTextDelta":
        return CodexRunEvent(
            type=CodexRunEventType.REASONING_SUMMARY_DELTA,
            message_id=_string(params.get("itemId")),
            text=_string(params.get("delta")) or "",
            raw_event=message,
        )
    if method == "item/reasoning/summaryPartAdded":
        return CodexRunEvent(
            type=CodexRunEventType.REASONING_SUMMARY_DELTA,
            message_id=_string(params.get("itemId")),
            text=_reasoning_summary_text(params),
            raw_event=message,
        )
    if method == "thread/tokenUsage/updated":
        token_usage = _token_usage(params)
        if token_usage is None:
            return None
        return CodexRunEvent(
            type=CodexRunEventType.TOKEN_USAGE_UPDATED,
            message_id=_string(params.get("turnId")),
            token_usage=token_usage,
            raw_event=message,
        )
    if method in (
        "item/commandExecution/outputDelta",
        "item/fileChange/outputDelta",
    ):
        return CodexRunEvent(
            type=CodexRunEventType.TOOL_OUTPUT_DELTA,
            tool_id=_string(params.get("itemId")),
            output=_string(params.get("delta")) or "",
            raw_event=message,
        )
    if method == "item/started":
        item = params.get("item")
        if isinstance(item, dict) and _is_tool_item(item):
            return CodexRunEvent(
                type=CodexRunEventType.TOOL_STARTED,
                tool_id=_string(item.get("id")),
                text=_tool_label(item),
                status=_string(_status_str(item.get("status"))),
                raw_event=message,
            )
    if method == "item/completed":
        item = params.get("item")
        if not isinstance(item, dict):
            return None
        item_type = item.get("type")
        if item_type == "agentMessage":
            return CodexRunEvent(
                type=CodexRunEventType.MESSAGE_COMPLETED,
                message_id=_string(item.get("id")),
                final_text=_string(item.get("text")) or "",
                raw_event=message,
            )
        if _is_tool_item(item):
            return CodexRunEvent(
                type=CodexRunEventType.TOOL_COMPLETED,
                tool_id=_string(item.get("id")),
                text=_tool_label(item),
                output=_tool_output(item),
                status=_string(_status_str(item.get("status"))),
                raw_event=message,
            )
    return None


def is_final_answer_completed(message: dict[str, Any]) -> bool:
    if message.get("method") != "item/completed":
        return False
    params = message.get("params")
    if not isinstance(params, dict):
        return False
    item = params.get("item")
    return (
        isinstance(item, dict)
        and item.get("type") == "agentMessage"
        and item.get("phase") == "final_answer"
    )


def is_thread_idle(message: dict[str, Any]) -> bool:
    if message.get("method") != "thread/status/changed":
        return False
    params = message.get("params")
    if not isinstance(params, dict):
        return False
    status = params.get("status")
    return isinstance(status, dict) and status.get("type") == "idle"


def turn_status(message: dict[str, Any]) -> str | None:
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    turn = params.get("turn")
    if not isinstance(turn, dict):
        return None
    return _status_str(turn.get("status"))


def turn_error(message: dict[str, Any]) -> str | None:
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    turn = params.get("turn")
    if not isinstance(turn, dict):
        return None
    error = turn.get("error")
    if isinstance(error, dict):
        message_value = error.get("message")
        if isinstance(message_value, str):
            return message_value
    return None


def _is_tool_item(item: dict[str, Any]) -> bool:
    return item.get("type") in {
        "commandExecution",
        "mcpToolCall",
        "dynamicToolCall",
        "webSearch",
        "fileChange",
    }


def _tool_label(item: dict[str, Any]) -> str | None:
    for key in ("command", "query", "tool", "server"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value
    action = item.get("action")
    if isinstance(action, dict):
        query = action.get("query")
        if isinstance(query, str) and query.strip():
            return query
    return _string(item.get("type"))


def _tool_output(item: dict[str, Any]) -> str | None:
    for key in ("aggregatedOutput", "result", "error"):
        value = item.get(key)
        if isinstance(value, str):
            return value
        if value is not None:
            return json.dumps(value)
    return None


def _decoded_chunk(params: dict[str, Any]) -> str:
    for key in ("chunk", "data", "delta"):
        value = params.get(key)
        if not isinstance(value, str):
            continue
        try:
            return base64.b64decode(value).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return value
    return ""


def _reasoning_summary_text(params: dict[str, Any]) -> str:
    for key in ("text", "summary", "delta"):
        value = params.get(key)
        if isinstance(value, str):
            return value
    part = params.get("part")
    if isinstance(part, dict):
        for key in ("text", "summary"):
            value = part.get(key)
            if isinstance(value, str):
                return value
    return ""


def _reasoning_text(params: dict[str, Any]) -> str:
    for key in ("delta", "text", "raw_content", "content"):
        value = params.get(key)
        if isinstance(value, str):
            return value
    return ""


def _token_usage(params: dict[str, Any]) -> CodexTokenUsage | None:
    value = params.get("tokenUsage")
    if not isinstance(value, dict):
        return None
    total = _token_usage_breakdown(value.get("total"))
    last = _token_usage_breakdown(value.get("last"))
    if total is None or last is None:
        return None
    return CodexTokenUsage(
        total=total,
        last=last,
        model_context_window=_int(value.get("modelContextWindow")),
    )


def _token_usage_breakdown(value: object) -> CodexTokenUsageBreakdown | None:
    if not isinstance(value, dict):
        return None
    return CodexTokenUsageBreakdown(
        total_tokens=_int(value.get("totalTokens")) or 0,
        input_tokens=_int(value.get("inputTokens")) or 0,
        cached_input_tokens=_int(value.get("cachedInputTokens")) or 0,
        output_tokens=_int(value.get("outputTokens")) or 0,
        reasoning_output_tokens=_int(value.get("reasoningOutputTokens")) or 0,
    )


def _status_str(value: object) -> str | None:
    """Codex statuses dump to plain strings (`inProgress`, `completed`,
    `failed`); enums already serialize to their value via mode='json'."""
    return value if isinstance(value, str) else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None
