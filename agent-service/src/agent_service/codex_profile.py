"""Codex runtime profile builder.

Chunk 3 wires codex as a second agent runtime behind the same
`POST /agent/turn` surface. The driver is the
`codex-agent-driver.CodexAppServerDriver` from the sister
`second-brain` package; this module builds the static
`CodexAgentProfile` once at lifespan startup and caches the driver
on `LoopHandles` so per-turn calls don't pay the profile-config
cost.

What's static (lifespan-cached):
- The agent profile: id, sandbox mode, approval policy, MCP server
  list. None of these change per turn.
- The driver instance: holds the session pool + transport factory.

What's per-turn (driver consumer's responsibility):
- The `CodexRunRequest` shape: prompt, developer instructions,
  per-thread `provider_thread_id`, per-actor `actor_id`. Built by
  `codex_driver.run_turn_codex` from the current request +
  `AgentThread` state. **All policy guidance** (role, identity,
  citation discipline, defense rules) ships in the per-turn
  `developer_instructions` so the codex path honors per-turn
  switches the same way pydantic-ai does, and so the single
  source of truth is `prompts/system_v4.txt` composed via
  `prompts/composer.compose_system_prompt`.

Profile shape rationale (per the chunk 3 plan, section 6):

- `id="mcae"`: stable id; codex uses it as a config-fingerprint
  key in the session pool.
- `sandbox="read-only"`: agent never writes the filesystem. The
  data plane is read-only over MCP; emit_claims writes go through
  an mpsc, not the FS.
- `approval_policy="never"`: no human-in-the-loop approvals during
  agent turns. Mismatched with codex's interactive CLI mode but
  correct for service-side use.
- One `CodexHttpMcpServer` pointed at `${DATA_PLANE_URL}/mcp`:
  every codex tool call (`wallet_profile`, `community_summary`,
  `get_token_info`, `emit_claims`) routes through the Rust data
  plane's streamable-HTTP MCP server at
  `backend/src/mcp.rs::McaeMcp`. Snapshot id is threaded into the
  developer prompt; future iterations move it into MCP session
  state.
"""

from __future__ import annotations

from pathlib import Path

from codex_agent_driver import (
    CodexAgentProfile,
    CodexAppServerDriver,
    CodexHttpMcpServer,
)

# Stable profile-level stub. Just enough text for codex to know a
# per-turn developer message is coming; the actual policy + tools +
# snapshot pin all arrive per-turn via `CodexRunRequest.developer_instructions`
# built by `codex_driver.run_turn_codex`. Keeping this static and
# minimal means the session-pool fingerprint never churns and the
# only source of truth for policy is `prompts/system_v4.txt`.
_PROFILE_STUB_INSTRUCTIONS = (
    "You are an analyst agent. Every turn you receive a developer "
    "message containing the full policy prompt, tool-surface notes, "
    "and a per-turn snapshot id. Follow that per-turn message exactly."
)


def build_codex_profile(
    *,
    data_plane_url: str,
    cwd: Path,
) -> CodexAgentProfile:
    """Build the lifespan-cached `CodexAgentProfile`.

    Parameters
    ----------
    data_plane_url:
        Base URL of the Rust data plane, e.g. `http://api:8004`.
        The MCP server is mounted at `${data_plane_url}/mcp`.
    cwd:
        Working directory codex sees. The agent never reads files
        in practice (read-only sandbox, no FS tools enabled) so
        this is mostly decorative; we pass the agent-service
        working directory so codex's project-trust check has a
        path to look at.
    """
    mcp_url = data_plane_url.rstrip("/") + "/mcp"
    return CodexAgentProfile(
        id="mcae",
        cwd=cwd,
        developer_instructions=_PROFILE_STUB_INSTRUCTIONS,
        sandbox="read-only",
        approval_policy="never",
        # Lock the agent to MCP tools only. Codex still has built-in
        # shell, unified_exec, apply_patch, web_search, view_image,
        # image_generation, computer_use, browser_use, tool_search, and
        # the apps subsystem on by default; the driver disables each
        # one in the actor config because none belong to the analyst
        # tool surface defined below.
        builtin_tools=frozenset(),
        project_root_markers=(),
        trusted_projects=(cwd,),
        mcp_servers=(
            CodexHttpMcpServer(
                id="mcae_data_plane",
                url=mcp_url,
                required=True,
                # Pin to the four tools the analyst path uses. Any
                # future tool surface change in `backend/src/mcp.rs`
                # has to also update this list to be visible.
                enabled_tools=(
                    "wallet_profile",
                    "community_summary",
                    "get_token_info",
                    "emit_claims",
                ),
            ),
        ),
    )


def build_codex_driver(
    *,
    profile: CodexAgentProfile,
    codex_home_root: Path,
) -> CodexAppServerDriver:
    """Build the lifespan-cached `CodexAppServerDriver`.

    `codex_home_root` is the directory under which the driver
    materializes per-actor codex_home trees via
    `prepare_actor_codex_home`. We pass `<THREAD_ROOT>/codex_homes`
    here; the per-thread driver invocation uses `actor_id =
    "codex_home"` so each thread's writable codex state lands at
    `<THREAD_ROOT>/codex_homes/local/codex_home/`. Auth + base
    `config.toml` are symlinked from `~/.codex` (mounted read-only
    via the docker-compose `${HOME}/.codex:/root/.codex:ro` bind
    mount).

    The session pool persists subprocess connections across turns
    so resuming an existing codex thread doesn't re-spawn the
    codex binary on every message.
    """
    return CodexAppServerDriver(
        profile=profile,
        codex_home_root=codex_home_root,
    )
