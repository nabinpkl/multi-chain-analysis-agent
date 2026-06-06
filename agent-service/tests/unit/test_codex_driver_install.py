"""Smoke check for the `openai-codex` SDK install + codex config glue.

The codex runtime is driven by the official `openai-codex` SDK (see
`docs/dependency-exceptions.md` for the Beta pin). This test does NOT
spawn a codex app-server. It verifies the SDK public surface we wire
against is importable and that our config builders produce the lockdown
+ MCP overlay shapes the analyst and helper threads pass to
`thread_start`. Failure here means the dependency regressed in
`pyproject.toml` (or the image was rebuilt without the SDK) and the
codex runtime won't even start.

Keep this test fast and side-effect free; it runs on every pytest
invocation as part of the no-LLM baseline.
"""

from __future__ import annotations

from agent_service.codex_config import (
    ANALYST_MCP_TOOLS,
    analyst_thread_config,
    helper_thread_config,
)


def test_openai_codex_importable() -> None:
    """The SDK symbols the codex runtime wires against import cleanly."""
    from openai_codex import AsyncCodex, Sandbox, TextInput
    from openai_codex.types import Notification, ReasoningEffort

    assert Sandbox.read_only.value == "read-only"
    # The async client + notification stream are the load-bearing
    # surface; keep the symbols referenced so the import is meaningful.
    _ = (AsyncCodex, TextInput, Notification, ReasoningEffort)


def test_analyst_config_mounts_mcp_and_locks_down_builtins() -> None:
    """The analyst overlay mounts the data-plane MCP server with the
    four-tool allow-list and disables every codex built-in tool."""
    config = analyst_thread_config(data_plane_url="http://api:8004")

    server = config["mcp_servers"]["mcae_data_plane"]
    assert server["url"] == "http://api:8004/mcp"
    assert server["required"] is True
    assert tuple(server["enabled_tools"]) == ANALYST_MCP_TOOLS

    # Lockdown: a representative built-in across each table is off.
    assert config["approval_policy"] == "never"
    assert config["features"]["shell_tool"] is False
    assert config["features"]["tool_search"] is False
    assert config["web_search"] == "disabled"
    assert config["tools"]["view_image"] is False
    assert config["apps"]["_default"]["enabled"] is False
    assert config["experimental_use_unified_exec_tool"] is False


def test_helper_config_locks_down_builtins_without_mcp() -> None:
    """The helper overlay carries the same lockdown but mounts no MCP
    server  helpers are pure text-in / JSON-out."""
    config = helper_thread_config()
    assert "mcp_servers" not in config
    assert config["approval_policy"] == "never"
    assert config["features"]["shell_tool"] is False
    assert config["features"]["apply_patch_freeform"] is False
