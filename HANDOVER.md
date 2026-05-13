# Handover — two-mode runtime substrate landed

Last touched: 2026-05-13. Delete this file once the next session has picked up the threads.

## TL;DR

Three arcs landed in this session:

1. **Codex built-in tool lockdown** (driver 0.2.0 + actor-config disable map).
2. **Canonical-mint verification labeling** on `get_token_info` (USDC / USDT / wSOL allow-list + prompt rule + `verified` flag passing through redaction).
3. **Two-mode runtime substrate** — `agent_service/llm_runtime.py::runtime_call` is the single helper-call entry point. All four helpers (primary, constitution gate, eval judge, repeat detector) now route through it. Under `AGENT_DEFAULT_RUNTIME=codex` they all use subscription auth via `~/.codex/auth.json`; under `pydantic_ai` they all use the configured free-tier provider (Gemini today). No mixed-stack runs.

Net diff across the runtime arc: 5 commits, negative LOC, 342 unit/integration tests pass, three live codex smokes confirm end-to-end wiring.

## Commit log

MCAE (`multi-chain-analysis-engine`, branch `main`):

```
ad821bc fix(agent): align redaction test with canonical-mints contract
026cada feat(agent): migrate repeat detector to runtime_call
7b07998 feat(agent): migrate constitution gate to runtime_call
a4be095 feat(agent): runtime_call helper, mcae-helper codex profile, migrate eval judge
6bb1e4a feat(agent): add to_strict_json_schema for codex outputSchema callers
ff97932 chore(agent): add codex outputSchema smoke and bump driver to 0.3.0
8ab2362 feat(evals): add EVAL_ALLOW_SHARED_FAMILY escape hatch
e35b9dc feat(agent): tag get_token_info with canonical-mint verification
d9d1f8b feat: implement built-in tool lockdown checks and update spans for codex tool calls
8499a1a feat: lock agent to MCP tools and update codex-agent-driver version to 0.2.0
```

Sister repo (`/Users/nabin/projects/second-brain`, branch `main`):

```
8eee7e6a feat(codex-agent-driver): forward outputSchema on turn/start
```

The codex-agent-driver is an editable path install at `packages/codex-agent-driver`. MCAE's `agent-service/uv.lock` is pinned to 0.3.0.

## Where the new substrate lives

- [agent-service/src/agent_service/llm_runtime.py](agent-service/src/agent_service/llm_runtime.py)
  - `runtime_call(role, system_prompt, user_prompt, output_model, ...)` — single entry point. Returns `(instance, raw_text)`.
  - `to_strict_json_schema(schema)` — walks a pydantic-emitted JSON Schema and rewrites it for OpenAI strict mode (adds `additionalProperties: false`, widens `required`, strips `default`). Only the codex path uses it.
  - `RuntimeCallParseError(ValueError)` — carries `.raw_text` so callers can stash the un-parsed response in observed diagnostics.
  - `resolve_helper_runtime()` — reads `AGENT_DEFAULT_RUNTIME`, defaults to codex.
  - Module-level `_helper_driver` cache. First codex call pays subprocess spawn; subsequent calls reuse via session pool (actor_id=`"helper"`).

- [agent-service/src/agent_service/codex_profile.py](agent-service/src/agent_service/codex_profile.py)
  - `build_codex_helper_profile(cwd)` — `id="mcae-helper"`, no MCP servers, no built-in tools, `ephemeral_default=True`. Separate profile from `mcae` (the analyst profile) so the session-pool fingerprint stays stable.

- [agent-service/scripts/smoke_codex_output_schema.py](agent-service/scripts/smoke_codex_output_schema.py)
  - Investigation tool. Round-trips `JudgeVerdict` and `ConstitutionVerdict` through the strict-schema + outputSchema path. Run with `CODEX_HELPER_MODEL=gpt-5.4-mini uv --directory agent-service run python scripts/smoke_codex_output_schema.py`.

## Migration shape (recipe for future helpers)

The three migrations followed the same pattern. If a future helper agent needs the same treatment:

1. Drop the lifespan-built `Agent` from `LoopHandles` and `main.py`.
2. Drop the per-turn rebuild in `loop_driver.py` (and `codex_driver.py` if it has one).
3. Rewrite the call site as a stateless function that takes `llm_override` and any per-call config, builds the system prompt fresh, and calls `runtime_call(role=..., output_model=...)`.
4. On the codex path, `llm_override` is ignored (codex picks its model via `CODEX_HELPER_MODEL` env).
5. Catch `RuntimeCallParseError` separately if you want to expose `.raw_text` in operator-facing diagnostics (probe `observed`, gate span attrs). Catch broad `Exception` for the soft-approve / fall-through path.
6. Drop `with_provider_retry` wrapping — runtime_call handles its own retry on the pydantic-ai branch.

Reference commits: `7b07998` (constitution), `026cada` (repeat detector). The eval judge migration (`a4be095`) carries the same shape but the probe context adds its own ProbeResult wrapping.

## Runtime config knobs

In `.env` / `.env.example`:

- `AGENT_DEFAULT_RUNTIME` — `codex` (default) or `pydantic_ai`. Single knob; flips all four helpers.
- `CODEX_HELPER_MODEL` — model id codex uses for helper calls. Defaults to codex-cli's default if unset (`gpt-5.4-mini` family today).
- `CODEX_PRIMARY_MODEL` — primary-agent model under codex runtime. Independent of the helper model.
- `EVAL_ALLOW_SHARED_FAMILY` — `true` opts out of the eval-judge family-leakage guard. Use locally when iterating on rubric wording; default `false`.
- `AGENT_PRIMARY_MODEL` / `AGENT_POLICY_MODEL` / `EVAL_JUDGE_MODEL` — pydantic-ai-runtime model picks. Ignored under codex.

## What's open

Three concrete next steps, in priority order:

1. **Run a real eval pass under codex runtime.** `AGENT_DEFAULT_RUNTIME=codex CODEX_HELPER_MODEL=gpt-5.4-mini just eval evals/cases/wallet_profile_smoke.yaml`. The unit tests cover structure; this surfaces cost / latency / rubric calibration against codex-shaped outputs and refreshes the baseline. The `judge-token-symbols-qualified` probe in particular is untested against codex-judge output.

2. **Adversarial-mint eval case.** The canonical-mints commit (`e35b9dc`) defends against the impersonation case but there's no eval fixture proving the prompt rule actually fires. Add a synthetic Token-2022 fixture in [agent-service/tests/fixtures/primitive_responses.py](agent-service/tests/fixtures/primitive_responses.py) with `name="USD Coin"` / `symbol="USDC"` at a non-canonical pubkey, plus a yaml case asserting the narrative qualifies the symbol as unverified. The `judge-token-symbols-qualified` probe (`evals/cases/wallet_profile_smoke.yaml`) was built for this — it's currently a placeholder.

3. **Per-turn `llm_override` provider switching on codex path.** Today `llm_override.provider` is honored only on the pydantic-ai branch (via `make_model`). Under codex, the dev Models panel's provider switch is ignored. This is acceptable per the two-mode design (codex is one auth path) but the Models panel UI should reflect that — either grey out the provider dropdown on codex turns, or document the override-ignored behavior in the panel tooltip.

## Watchlist / known gotchas

- **Pydantic-ai output mode flip on policy gate.** The constitution gate and repeat detector now use text-completion + manual parse on the pydantic-ai branch (was tool-calling structured output before). Gemini's clean-JSON emission quality determines whether parse failures uptick. Watch `repeat_detector_parse_failed` / `constitution_*_parse_failed` log warnings; if they exceed ~1%, revisit. Soft-approve on parse failure means quality regressions are silent until you check logs.

- **Codex helper subprocess lifetime.** `_helper_driver` is module-level cached. The first call in a process pays ~1-2s spawn; subsequent calls in the same process reuse via the session pool. In `just dev` this means cold-start of the first eval probe / first agent turn is slow; warm reuse is fast.

- **OpenAI strict mode quirks.** `to_strict_json_schema` handles the three known requirements (additionalProperties, required widening, default stripping). If you add a new helper model with patterns the walker doesn't cover (e.g. `oneOf` discriminators, `$ref` cycles), expect codex to reject it with a schema error. Test via the smoke script before wiring it into a hot path.

- **Test `test_get_token_info_redacts_text_when_switch_off`** asserts `canonical_*` fields pass through sanitization. That's the canonical-mints design intent ("verified flag is a tag, not a filter"). If a future commit changes that contract, this test needs updating too.

## Spawned chips (UI sidebar)

One chip remains on the user's session UI:

- **Fix canonical_name leak in redaction-off path** — already addressed in this session (commit `ad821bc`). The chip may still be visible; user can dismiss it. Verified `tests/integration/test_agent_loop.py` passes 7/7.

## How to verify the substrate end-to-end

```bash
# Unit tests (CPU-bound, fast)
cd agent-service && uv run pytest tests/unit/ tests/integration/test_agent_loop.py -q
# Expect: 342 passed (1 warning, 3 skipped is fine — pre-existing test_snapshot_lease.py + test_agent_routes.py have window-mock drift).

# Live codex smoke (requires ~/.codex/auth.json present + codex CLI on PATH)
cd agent-service && CODEX_HELPER_MODEL=gpt-5.4-mini uv run python scripts/smoke_codex_output_schema.py
# Expect: both JudgeVerdict and ConstitutionVerdict PASS.

# Live runtime_call wiring through codex
cd agent-service && CODEX_HELPER_MODEL=gpt-5.4-mini AGENT_DEFAULT_RUNTIME=codex uv run python -c "
import asyncio
from agent_service.policy.constitution import judge_narrative
async def main():
    v = await judge_narrative(text='Wallet routed 12 SOL to two neighbors.', same_turn_claims=[])
    print(v)
asyncio.run(main())
"
# Expect: verdict='retract' (uncited audit-class numbers) with populated extraction.
```

## Files NOT to touch in the next session

Nothing is permanently locked; these are just non-obvious dependencies:

- `agent-service/src/agent_service/codex_profile.py` has both `build_codex_profile` (analyst, id="mcae") and `build_codex_helper_profile` (helper, id="mcae-helper"). They must stay separate — the analyst profile has MCP servers, the helper profile has none. Merging them breaks the session-pool fingerprinting.
- `agent-service/src/agent_service/llm_runtime.py::_helper_driver` global is intentional. Don't refactor to a per-call driver; the session-pool reuse is what makes helper calls fast.
- `codex-agent-driver` (sister repo) 0.3.0 is the version pinned in `agent-service/uv.lock`. If you bump it, run the smoke script to confirm `outputSchema` still works the same way.
