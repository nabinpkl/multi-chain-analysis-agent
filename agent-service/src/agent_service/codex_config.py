"""Codex SDK configuration builders.

The codex runtime is one shared `AsyncCodex` app-server (built at
lifespan in `main.py`) whose isolation is per native codex thread
(`thread_start` / `thread_resume`), not per-thread `codex_home`.
Everything that used to be a `CodexAgentProfile` field now lands as a
per-thread `config` overlay passed to `thread_start` / `thread_resume`:

- The built-in-tool **lockdown** (`_builtin_lockdown`) disables every
  codex built-in tool. This is the load-bearing security property: the
  analyst agent must operate against exactly the four data-plane MCP
  tools and nothing else (shell, web_search, apply_patch, ... stay off).
  Applied to BOTH analyst and helper threads.
- The analyst thread additionally mounts the data-plane **HTTP MCP
  server** with a four-tool allow-list (`analyst_thread_config`).
- Helper threads (constitution gate / eval judge / repeat detector)
  mount no MCP server (`helper_thread_config`); they are pure
  text-in / JSON-out via `output_schema`.

`approval_policy = "never"` is set in the overlay so codex never blocks
a turn on an approval prompt; combined with `Sandbox.read_only` (passed
separately on `thread_start`) and the empty built-in surface, the agent
can only read via MCP and emit text.

The disable map is ported from the codex config.toml feature/tool
schema. Pinned to openai/codex commit
392e94e9ea756cffd89f35941e881d29b2a81a6e (verified against
codex-rs/features/src/lib.rs and codex-rs/config/src/config_toml.rs).
When codex changes its feature schema, update this map and the pinned
SHA in the same commit. The eval probe `no-builtin-tool-call`
(`evals/cases/model_assertions_codex.yaml`) asserts the
`mcae.codex.tool.builtin` span never fires and is the safety net for a
missed update.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from openai_codex import CodexConfig

# The four MCAE MCP tools the analyst path is allowed to call. Any
# future tool surface change in `backend/src/mcp.rs` must also update
# this allow-list to be visible to the agent.
ANALYST_MCP_TOOLS: tuple[str, ...] = (
    "wallet_profile",
    "community_summary",
    "get_token_info",
    "emit_claims",
)

_MCP_SERVER_ID = "mcae_data_plane"


def prepare_codex_home(codex_home: Path) -> Path:
    """Materialize a writable CODEX_HOME and seed `auth.json` from the
    read-only base codex home (`CODEX_BASE_HOME`, default `~/.codex`,
    which the container bind-mounts read-only). The app-server writes
    its sqlite / cache / logs under `codex_home`; subscription auth is
    read from the seeded file. Idempotent."""
    codex_home.mkdir(parents=True, exist_ok=True)
    base = Path(os.environ.get("CODEX_BASE_HOME", "~/.codex")).expanduser()
    src = base / "auth.json"
    dst = codex_home / "auth.json"
    if src.exists() and not dst.exists() and not dst.is_symlink():
        try:
            dst.symlink_to(src)
        except OSError:
            shutil.copy2(src, dst)
    return codex_home


def build_codex_config(*, codex_home: Path) -> CodexConfig:
    """Build the process-level `CodexConfig` for an app-server.

    `CODEX_HOME` points at a writable directory (seeded via
    `prepare_codex_home`) the app-server uses for its sqlite / cache /
    logs and from which it reads `auth.json`. The SDK bundles the codex
    binary, so no `codex_bin` is set.
    """
    prepare_codex_home(codex_home)
    return CodexConfig(env={"CODEX_HOME": str(codex_home)})


def analyst_thread_config(*, data_plane_url: str) -> dict[str, Any]:
    """Per-thread `config` overlay for an analyst chat thread: the
    built-in lockdown plus the data-plane MCP server with its four-tool
    allow-list. Passed to `thread_start` / `thread_resume`."""
    mcp_url = data_plane_url.rstrip("/") + "/mcp"
    config = _base_thread_config()
    config["mcp_servers"] = {
        _MCP_SERVER_ID: {
            "url": mcp_url,
            "enabled": True,
            "required": True,
            "enabled_tools": list(ANALYST_MCP_TOOLS),
        }
    }
    return config


def helper_thread_config() -> dict[str, Any]:
    """Per-thread `config` overlay for a helper thread (constitution
    gate / eval judge / repeat detector): the built-in lockdown, no MCP
    server. Helper threads are pure text-in / JSON-out."""
    return _base_thread_config()


def _base_thread_config() -> dict[str, Any]:
    """The lockdown + approval policy shared by every codex thread."""
    config: dict[str, Any] = {"approval_policy": "never"}
    _apply_builtin_lockdown(config)
    return config


# Concrete config writes that disable each codex built-in tool, as a
# nested config overlay. Each entry: (location, key, value) where
# location is "top" for a top-level key or a dotted table path
# (`features`, `tools`, `apps._default`).
_BUILTIN_DISABLE_WRITES: tuple[tuple[str, str, Any], ...] = (
    ("features", "shell_tool", False),
    ("features", "unified_exec", False),
    ("top", "experimental_use_unified_exec_tool", False),
    ("features", "apply_patch_freeform", False),
    ("top", "web_search", "disabled"),
    ("tools", "view_image", False),
    ("features", "image_generation", False),
    ("features", "computer_use", False),
    ("features", "browser_use", False),
    ("features", "apps", False),
    ("apps._default", "enabled", False),
    ("features", "tool_search", False),
)


def _apply_builtin_lockdown(config: dict[str, Any]) -> None:
    for location, key, value in _BUILTIN_DISABLE_WRITES:
        if location == "top":
            config[key] = value
            continue
        table = config
        for part in location.split("."):
            table = table.setdefault(part, {})
        table[key] = value
