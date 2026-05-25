"""Smoke check for the `codex-agent-driver` install.

Chunk 3 wires `codex-agent-driver` (sibling repo
`second-brain/packages/codex-agent-driver`) into agent-service as a
path dep. The package is the JSON-RPC stdio bridge to the codex
subprocess; the chunk-3 driver layered on top of it will sit in
`agent_service/codex_driver.py`.

This test does NOT spawn a codex subprocess. It only verifies the
package is importable from the agent-service venv and that the
public surface we plan to wire against is reachable. Failure here
means the path dep regressed in `pyproject.toml` (or the docker
image was rebuilt without the sibling package available) and the
chunk-3 driver won't even start.

Keep this test fast and side-effect free; it runs on every
pytest invocation as part of the no-LLM baseline.
"""

from __future__ import annotations

from pathlib import Path


def test_codex_agent_driver_importable() -> None:
    """The public symbols chunk 3 wires against import cleanly."""
    from codex_agent_driver import (
        CodexAgentProfile,
        CodexAppServerDriver,
        CodexHttpMcpServer,
        CodexRunEventType,
        CodexRunRequest,
        prepare_actor_codex_home,
    )

    # The package's stable id; we'll stamp it on traces as the
    # runtime provider so probes can distinguish codex turns from
    # pydantic-ai turns.
    assert CodexAppServerDriver.id == "codex-app-server"

    # Sanity: the run-event enum carries the variants the codex
    # driver will translate into our SSE frames.
    expected = {
        "TEXT_DELTA",
        "TOOL_STARTED",
        "TOOL_COMPLETED",
        "MESSAGE_COMPLETED",
        "TOKEN_USAGE_UPDATED",
    }
    actual = {e.name for e in CodexRunEventType}
    missing = expected - actual
    assert not missing, f"codex-agent-driver missing event variants: {missing}"

    # Re-export check: prepare_actor_codex_home is the helper the
    # chunk-3 driver will call to materialize
    # `<thread_root>/threads/<thread_id>/local/codex_home/` per turn.
    assert callable(prepare_actor_codex_home)

    # Keep the symbols referenced so linters don't elide the import.
    _ = (CodexAgentProfile, CodexHttpMcpServer, CodexRunRequest)


def test_codex_profile_and_driver_construct() -> None:
    """Build the profile + driver shape the chunk-3 bridge will use.

    No subprocess. No I/O. Just confirms the constructors accept the
    args we plan to pass and that the data-plane MCP-server URL is
    wired through to the profile.
    """
    from codex_agent_driver import (
        CodexAgentProfile,
        CodexAppServerDriver,
        CodexHttpMcpServer,
        CodexRunRequest,
    )

    profile = CodexAgentProfile(
        id="mcae",
        cwd=Path("/tmp"),
        developer_instructions="multi-chain-analysis-agent codex bridge",
        sandbox="read-only",
        approval_policy="never",
        mcp_servers=(
            CodexHttpMcpServer(
                id="mcae_data_plane",
                url="http://api:8004/mcp",
                required=True,
            ),
        ),
    )
    assert profile.id == "mcae"
    assert profile.sandbox == "read-only"
    assert profile.approval_policy == "never"
    assert len(profile.mcp_servers) == 1
    assert profile.mcp_servers[0].id == "mcae_data_plane"
    assert profile.mcp_servers[0].url == "http://api:8004/mcp"

    req = CodexRunRequest(
        prompt="hello from the smoke test",
        actor_id="codex_home",
    )
    assert req.prompt == "hello from the smoke test"
    assert req.actor_id == "codex_home"
    assert req.provider_thread_id is None

    # Driver construction is pure config; .stream() is what spawns
    # the codex subprocess. We never call .stream() here.
    driver = CodexAppServerDriver(profile=profile)
    assert type(driver).__name__ == "CodexAppServerDriver"
