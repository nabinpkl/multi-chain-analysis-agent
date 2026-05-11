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
  `AgentThread` state.

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

# Static developer prompt the codex profile ships with. The chunk 3
# driver appends per-turn details (snapshot id, view context, the
# user question) before passing it through `CodexRunRequest`.
_DEVELOPER_INSTRUCTIONS = """
You are the multi-chain-analysis-engine analyst agent. You analyze
the live Solana transaction graph by calling tools on the
`mcae_data_plane` MCP server.

Tools you have:

- `wallet_profile(snapshot_id, addr)`: profile a Solana wallet.
  Returns role, community_id, transfer counts, top counterparties.
- `community_summary(snapshot_id, community_id)`: summarize a
  cluster. Returns size, volume split, top wallets.
- `get_token_info(mint)`: resolve an SPL / Token-2022 mint to its
  name, symbol, and metadata URI. No snapshot id needed.
- `emit_claims(snapshot_id, claims)`: batched. Emit ALL chips for
  the turn in ONE call after gathering enough evidence. Each claim
  carries:
  * `kind`: one of `PROFILE | PATTERN | COMPARISON | SUMMARY | PULSE`
  * `headline`: one sentence under 100 chars
  * `body_markdown`: structured paragraph; cite provenance with
    `${ref:N}` (1-indexed against the `provenance` array)
  * `provenance`: non-empty list of typed entity refs. Each ref
    has a discriminator `kind` field:
    - `kind=wallet`: requires `addr` (base58 pubkey) + `idx`.
    - `kind=community`: requires `id` (int) + `idx`.
    - `kind=edge`: requires `edge_id` + `src` + `dst` + `idx`.
    - `kind=time_range`: requires `from_s` + `to_s` + `idx`.
    - `kind=number`: requires `metric` + `value` + `idx`.

Rules:

1. Always pass the `snapshot_id` you receive in the per-turn
   developer message to every tool call that takes one. Snapshots
   expire shortly after the turn starts.
2. Cite. Every claim with an unresolved `${ref:N}` placeholder or
   empty provenance gets retracted by the gate stack downstream.
3. One emit_claims call per turn carrying all chips. Don't
   stream chips across multiple calls.
4. After tools return, write a single final narrative summarizing
   what you found. Reference claims you emitted; the UI renders
   chips alongside your prose.
""".strip()


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
        developer_instructions=_DEVELOPER_INSTRUCTIONS,
        sandbox="read-only",
        approval_policy="never",
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
