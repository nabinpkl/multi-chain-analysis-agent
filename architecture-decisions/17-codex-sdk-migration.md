# 17: Codex runtime on the official `openai-codex` SDK

This document records replacing the `codex-agent-driver` path-dependency
(a hand-written JSON-RPC stdio bridge in the sibling `second-brain`
repo) with OpenAI's official `openai-codex` Python SDK, and moving
per-chat isolation from per-thread `codex_home` trees to native codex
threads. It supersedes the *mechanism* of ADR 15; the rationale there
(codex as a harness, the built-in-tool lockdown, the two-mode
`runtime_call` substrate, server-enforced structured output, family
coherence) is unchanged.

## Status

Accepted, 2026-06-06.

## Problem

ADR 15 chose `codex-agent-driver` because, at the time, no official
codex SDK existed and a thin "model SDK" wrapper would have thrown away
the harness features (native threads, MCP host, sandbox, session pool).
That driver was a real maintenance surface: a hand-written app-server
JSON-RPC client, a session pool, per-actor `config.toml` writers, a
`codex_home` materializer, all pinned to a codex CLI SHA, living in a
sibling repo as an editable path-dep. The path-dep also forced an
out-of-band checkout in CI (the agent-service test job was disabled by
default) and a Node build stage plus a named build context in every
Dockerfile that built agent-service.

OpenAI has since shipped `openai-codex` (Beta). It *is* the harness, not
a thin wrapper: it spawns and drives `codex app-server`, exposes native
threads (`thread_start` / `thread_resume`), streams typed
`Notification{method, payload}` objects, supports `Sandbox.read_only`,
server-enforced `output_schema`, a per-thread `config` overlay (for
MCP-server and built-in-tool configuration), and a native `AsyncCodex`
with `async for event in turn.stream()`. The original reason the sister
package existed is gone.

## Decision

### 1. Drive codex through `openai-codex`, one shared app-server

The codex runtime is one `AsyncCodex` built at FastAPI lifespan
(`main.py`), closed at shutdown, stored on `LoopHandles.codex`. The SDK
bundles the codex binary (`openai-codex-cli-bin`), so no separate CLI
install is needed; it reads subscription auth from `auth.json` under a
writable `CODEX_HOME` seeded from the read-only `~/.codex` mount.

`codex_driver.run_turn_codex` keeps its public contract (`AgentRequest`
-> SSE frames) and its entire post-stream gate / claim-drain / span
machinery. Only the event *source* changed: instead of a sync driver
pumped over a worker thread + `asyncio.Queue`, it does
`thread.turn(...)` then `async for notification in handle.stream()`.
The SDK's typed payloads are dumped back to their camelCase wire dict
and parsed by `agent_service.codex_events` (a small self-owned parser
ported from the codex app-server protocol), so beta SDK
typed-attribute churn cannot ripple through the driver.

### 2. Native threads replace per-thread `codex_home`

Each chat thread maps to one native codex thread. `AgentThread` stores
the SDK `thread.id`; a new chat calls `thread_start`, a resume calls
`thread_resume(thread_id)`. This deletes the `CODEX_HOME_ROOT`
per-thread materialization, the `prepare_actor_codex_home` symlink
dance, and the `state_5.sqlite` model-name read hack (the effective
model is now the one we requested, stamped directly on the chat span).

Trade-off accepted: chats now share one app-server process and one
sqlite (native thread isolation) instead of filesystem-level isolation.
For a single-developer service this is strictly simpler with no
observable behavior loss.

### 3. Lockdown + MCP move to the per-thread `config` overlay

The built-in-tool lockdown (disabling shell, unified_exec, apply_patch,
web_search, view_image, image_generation, computer_use, browser_use,
apps, tool_search) and the analyst MCP-server mount with its four-tool
allow-list are now a per-thread `config` dict passed to `thread_start`
/ `thread_resume` (`codex_config.py`), plus `Sandbox.read_only` and
`approval_policy = "never"`. The disable map is ported verbatim from the
old driver and keeps the codex-SHA pin comment. The `no-builtin-tool-call`
eval probe remains the regression net.

### 4. Helper app-server stays separate

The gates / eval judge / repeat detector run through a *second*
module-level `AsyncCodex` (`llm_runtime.py`) with its own `CODEX_HOME`,
ephemeral threads, the lockdown overlay, and no MCP server. This
preserves ADR 15 §3's no-bleed property (analyst MCP traffic never
reaches a helper call) using two app-servers instead of two session
pools. `to_strict_json_schema` + `_parse_strict` are unchanged; the
helper now calls `thread.run(prompt, output_schema=...)` and parses
`result.final_response`.

## Consequences

### Accepted
- The SDK is Beta. API-churn risk is recorded in
  `docs/dependency-exceptions.md` with an exact version pin and a
  revisit trigger; the `codex_events` parser contains the blast radius.
- Two codex app-server processes per service (analyst + helper) instead
  of one driver with two session pools. Equivalent isolation, slightly
  more process memory.
- Per-chat filesystem isolation is gone (native thread isolation
  instead). A future multi-tenant deployment would revisit this.

### Removed
- `codex-agent-driver` dependency and `[tool.uv.sources]` entry.
- `agent_service/codex_profile.py`, `_read_codex_model`, the
  `_pump_codex_events` worker bridge, `CODEX_HOME_ROOT`, the
  `codex_home_root` `LoopHandles` field.
- The Dockerfile Node/codex-CLI build stage and the `codex-agent-driver`
  named build contexts in `docker-compose.yml` and the mock-service
  Dockerfile. The agent-service CI job is re-enabled (no sibling
  checkout needed).

## What this overrides

From ADR 15 "Implementation surface" and "Consequences": the
`codex-agent-driver` (sister package) section, the `CODEX_HOME_ROOT`
env, the per-thread `codex_home` isolation, and the editable path-dep
maintenance note. Everything in ADR 15's "Decision" and "Rationale"
about *why* codex is the harness and *what* the two-mode substrate
guarantees still stands.

## References

- ADR 15 (`15-codex-as-agent-harness.md`). The codex-as-harness
  rationale this ADR keeps and the mechanism it replaces.
- `docs/dependency-exceptions.md`. The Beta `openai-codex` pin.
- `openai/codex` `sdk/python`. The SDK source.
