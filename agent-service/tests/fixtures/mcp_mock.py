"""Hermetic mock of the Rust data-plane MCP server (`/mcp`) for the
pydantic-ai agent's `MCPServerStreamableHTTP` client.

The pydantic-ai runtime's entire tool surface is the MCP server
(`backend/src/mcp.rs`), reached over streamable HTTP. On every agent
run the client performs the JSON-RPC handshake (initialize ->
tools/list) and dispatches tool calls (tools/call). This mock answers
that protocol with plain `application/json` responses (the mcp 1.27
client accepts JSON as well as SSE) and refuses the optional
server-push GET stream with 405 so the client proceeds without it.

Tool calls are recorded on the returned `McpRecorder` so tests can
assert on the arguments the agent sent (e.g. that every call carries
the leased snapshot_id) without depending on the now-removed direct
`/primitive/*` HTTP path.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from tests.conftest import DATA_PLANE_BASE

MCP_URL = re.compile(rf"^{re.escape(DATA_PLANE_BASE)}/mcp(\?.*)?$")

# Tool descriptors advertised on tools/list. Schemas mirror the four
# tools in backend/src/mcp.rs closely enough for the client to dispatch;
# the agent only needs name + an object input schema.
_TOOLS: list[dict[str, Any]] = [
    {
        "name": "wallet_profile",
        "description": "Profile a wallet over the snapshot window.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "snapshot_id": {"type": "string"},
                "addr": {"type": "string"},
            },
            "required": ["snapshot_id", "addr"],
        },
    },
    {
        "name": "community_summary",
        "description": "Summarize a community over the snapshot window.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "snapshot_id": {"type": "string"},
                "community_id": {"type": "integer"},
            },
            "required": ["snapshot_id", "community_id"],
        },
    },
    {
        "name": "get_token_info",
        "description": "Resolve SPL token metadata.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "snapshot_id": {"type": "string"},
                "mint": {"type": "string"},
            },
            "required": ["snapshot_id", "mint"],
        },
    },
    {
        "name": "emit_claims",
        "description": "Emit batched claim chips for the turn.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "snapshot_id": {"type": "string"},
                "claims": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["snapshot_id", "claims"],
        },
    },
]


@dataclass
class McpToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class McpRecorder:
    """Records the tools/call requests the agent made."""

    calls: list[McpToolCall] = field(default_factory=list)

    def calls_for(self, name: str) -> list[McpToolCall]:
        return [c for c in self.calls if c.name == name]


def _result(rid: Any, result: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "id": rid, "result": result},
        headers={"content-type": "application/json"},
    )


def _default_tool_result(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Minimal MCP CallToolResult. Echoes the snapshot_id so a turn that
    routes the value into a narrative still has something concrete."""
    text = json.dumps({"tool": name, "snapshot_id": args.get("snapshot_id")})
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": {"value": {"ok": True}, "provenance": []},
        "isError": False,
    }


def register_mcp_mock(
    httpx_mock,
    *,
    tool_results: dict[str, dict[str, Any]] | None = None,
) -> McpRecorder:
    """Register the `/mcp` streamable-HTTP handshake + tool dispatch on
    `httpx_mock`. Returns an `McpRecorder` capturing tools/call args.

    `tool_results` overrides the canned CallToolResult per tool name.
    """
    overrides = tool_results or {}
    recorder = McpRecorder()

    def _post(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method")
        rid = body.get("id")
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "mock-mcae", "version": "0"},
                    },
                },
                headers={
                    "content-type": "application/json",
                    "mcp-session-id": "test-session",
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return _result(rid, {"tools": _TOOLS})
        if method == "tools/call":
            params = body.get("params") or {}
            name = params.get("name", "")
            args = params.get("arguments") or {}
            recorder.calls.append(McpToolCall(name=name, arguments=args))
            result = overrides.get(name) or _default_tool_result(name, args)
            return _result(rid, result)
        # Unknown method: JSON-RPC method-not-found.
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            },
            headers={"content-type": "application/json"},
        )

    httpx_mock.add_callback(
        _post, url=MCP_URL, method="POST", is_reusable=True, is_optional=True
    )
    # Refuse the optional server-push GET stream; the client proceeds
    # without it. Same for the session-close DELETE on teardown.
    httpx_mock.add_response(
        url=MCP_URL, method="GET", status_code=405, is_reusable=True, is_optional=True
    )
    httpx_mock.add_response(
        url=MCP_URL, method="DELETE", status_code=200, is_reusable=True, is_optional=True
    )
    return recorder
