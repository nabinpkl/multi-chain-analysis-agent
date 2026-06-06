# Dependency exceptions

Dependencies that do not fully clear the library-maintenance bar in
root [AGENTS.md](../AGENTS.md), recorded here with the specific risk
accepted and the trigger for revisiting. Adding an entry is the
documented escape hatch; silently shipping a sub-bar dependency is not.

## `openai-codex` (Beta)

- **Where:** `agent-service` (`pyproject.toml`), the codex agent runtime
  and the helper-call path in `agent_service/llm_runtime.py`.
- **Pinned:** `==0.1.0b3` (exact, not a range).
- **Bar check (2026-06-05):** Official OpenAI SDK in the high-activity
  `openai/codex` monorepo. Latest release `0.1.0b3` uploaded 2026-06-03
  (within the 1-month freshness window). `requires-python >=3.10`.
  Bundles the codex binary via its `openai-codex-cli-bin` dependency.
- **Why it does not fully clear the bar:** The release line is Beta
  (`0.1.0bN`). The maintenance signal is strong (active, official), so
  the risk is **API churn across minor/beta bumps**, not abandonment.
- **Risk accepted:** A beta minor bump may rename or restructure the
  thread / turn / notification surface this service depends on
  (`AsyncCodex`, `thread_start`/`thread_resume`, `TurnHandle.stream()`
  yielding `Notification{method, payload}`, the per-thread `config`
  overlay used to mount the data-plane MCP server and disable codex
  built-in tools).
- **Containment:** The notification wire shape is parsed through
  `agent_service/codex_events.py` (a small, self-owned parser over the
  raw camelCase payload dict), so typed-attribute churn in the SDK does
  not ripple through the driver. The exact version is pinned.
- **Revisit trigger:** Re-audit on every bump; run
  `scripts/smoke_codex_output_schema.py` after each. Re-evaluate the
  exception when a GA (`>=0.1.0` non-beta) release lands, and remove
  this entry once on a stable line. Drop the SDK if a bump breaks the
  thread/turn/notification surface without a clean migration.
