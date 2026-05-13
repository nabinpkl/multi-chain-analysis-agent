# 15: Codex as agent harness, two-mode runtime substrate

This document records the decision to treat codex (the OpenAI Codex
CLI shipped at https://github.com/openai/codex) as an agent harness
that runs alongside the pydantic-ai harness shipped in ADR 12, and to
route every helper LLM call through a single `runtime_call` dispatch
so the choice of harness is one knob with all-or-nothing semantics.

## Status

Accepted, 2026-05-13. Shipped across commits `8499a1a` (lock agent
to MCP tools, codex-agent-driver 0.2.0 with builtin disable map),
`d9d1f8b` (built-in tool lockdown span synthesis), `ff97932` (driver
0.3.0 with `outputSchema` forwarding + smoke), `6bb1e4a`
(`to_strict_json_schema` strict-mode walker), `a4be095`
(`runtime_call` helper, `mcae-helper` codex profile, eval judge
migration), `7b07998` (constitution gate migration), `026cada`
(repeat detector migration). Sister-repo commit `8eee7e6a` on
`/Users/nabin/projects/second-brain/packages/codex-agent-driver`.

## Problem

ADR 12 made Pydantic AI the agent runtime. Three pains compounded
once the agent passed the walking-skeleton phase:

1. **Free-tier provider stalls.** OpenRouter free-tier models
   periodically sit on requests for minutes (queue-depth on shared
   pools). Per-attempt timeouts plus single retry catch the common
   case; the long tail still bleeds turn budget. Gemini's
   OpenAI-compat shim is the more reliable free option for short
   helper calls but it has its own ceiling (503 overload bursts) and
   reasoning quality is uneven on harder tasks.
2. **Mixed-stack runs.** Once codex was introduced for the primary
   agent (no ADR previously, see "Pre-existing gap" below), the
   in-turn policy gate and the eval judge still hit Gemini. One run
   produced traces with two different auth paths, two different
   provider stacks, and two different cost stories. The
   preference-leakage literature (ICLR 2026) on judge-of-agent
   family-coherence cuts both ways: the same family is a bias
   problem only if the family is actually the same.
3. **Output mode drift across helpers.** Pydantic AI's tool-calling
   structured output mode (the `output_type=ModelCls` shape) was
   used by the constitution gate and repeat detector; the eval
   judge had to be on text-completion mode because some free-tier
   OpenRouter models do not expose `tool_choice`. Two parse paths
   for what is structurally the same operation.

Codex offers a path through all three: subscription auth via
`~/.codex/auth.json` carries no per-call billing, the codex CLI
internally hits gpt-5.4-mini family models that are faster than
free-tier OpenRouter on similar workloads, codex's app-server
protocol exposes a server-enforced `outputSchema` parameter that
makes structured output a hard guarantee, and the codex MCP host
already wires our four data-plane tools through `backend/src/mcp.rs`.

What codex is **not** is a model SDK. It is a CLI tool that ships
its own subprocess, manages a sqlite session store, owns its own
prompt cache, runs an MCP client, applies a sandbox, and answers an
app-server JSON-RPC protocol. Wrapping it like an SDK (one HTTP
call returning a model output) loses the harness features. Treating
it as a harness, with its own session lifecycle and configuration
surface, keeps them.

## Decision

Three coordinated commitments.

### 1. Codex is a parallel runtime, not a provider behind Pydantic AI

Codex runs as a subprocess via the `codex-agent-driver` package
(`/Users/nabin/projects/second-brain/packages/codex-agent-driver`,
pinned to 0.3.0 in `agent-service/uv.lock`). The driver speaks
codex's app-server JSON-RPC over stdio and maintains a session pool
that reuses subprocesses across thread resumes. Per-thread state
lives in a writable `codex_home` materialized under
`$CODEX_HOME_ROOT/<actor_id>/`; the read-only base config and
`auth.json` are symlinked from `~/.codex` (mounted into the
container as `:ro`).

Codex coexists with pydantic-ai. Both runtimes implement the same
`AgentRequest -> SSE` contract on `POST /agent/turn`. The selection
is per-request via the `runtime` field on `AgentRequest`, defaulting
to `AGENT_DEFAULT_RUNTIME` (default `codex`).

### 2. Two-mode runtime substrate via `runtime_call`

Every helper LLM call (eval judge, constitution gate, repeat
detector) goes through `agent_service.llm_runtime.runtime_call`.
The function takes `role`, `system_prompt`, `user_prompt`,
`output_model` (a pydantic class), optional `llm_override` and
`model_id`, and dispatches based on `AGENT_DEFAULT_RUNTIME`.

```
+------------------------+
|   runtime_call         |
|   (one entry point)    |
+-----------+------------+
            |
   resolve_helper_runtime()
            |
   +--------+---------+
   |                  |
   v                  v
+------+         +----+--------+
|codex |         | pydantic-ai |
+--+---+         +-----+-------+
   |                   |
   v                   v
mcae-helper       Agent(output_type=str)
profile (no MCP, +  with_provider_retry
no built-ins,
ephemeral)
   |                   |
   v                   v
outputSchema      text completion
strict-wrapped    + first-JSON parse
   |                   |
   +---------+---------+
             v
   (instance, raw_text)
```

`AGENT_DEFAULT_RUNTIME=codex` (the default) routes everything
through codex; `pydantic_ai` routes everything through the
existing free-tier provider stack. No per-helper override. The
no-mixed-stack property is the point.

### 3. Helper profile separation

`build_codex_helper_profile(cwd)` returns an `id="mcae-helper"`
codex profile distinct from the analyst profile
`id="mcae"`. The helper profile has no MCP servers, no built-in
tools, and `ephemeral_default=True` so codex's sqlite session
store stays empty. The session-pool fingerprint key includes the
profile id, so the two profiles get separate subprocess pools.
Merging them would let analyst-side MCP traffic bleed into
helper calls.

Both profiles set `builtin_tools=frozenset()`, which the driver
translates into a `[features]`/`[tools]` disable block in each
actor's `config.toml`. This is the same lockdown shape on both
profiles, applied via the same writer.

## What this overrides

From ADR 12 ("Migrating the agent plane to Python"):

| Original | Now |
|---|---|
| **D-2 amendment** Pydantic AI is the agent runtime, OpenRouter via `OpenAIProvider`. | Pydantic AI is one of two runtimes. Codex is the other and is the default. The OpenRouter / Gemini path remains for the pydantic-ai branch unchanged. |
| **Single-process model** for in-turn helper calls (constitution gate, repeat detector). | Helper calls dispatch through `runtime_call`. Under codex, helper turns spawn against the `mcae-helper` profile's session-pooled subprocess. |
| **Tool-calling structured output via pydantic-ai's `output_type=ModelCls`** (used by constitution gate, repeat detector). | Text completion plus first-JSON parse on the pydantic-ai branch. Codex branch uses server-enforced `outputSchema`. Parse logic is shared (`_parse_strict` in `llm_runtime.py`). |

D-1, D-3, D-4, D-5, D-6, D-7 from `docs/agent-design/01-agent-overview.md`
and the six locked invariants in 01 all survive. The data plane
(Rust :8002) is unchanged. The wire-types contract is unchanged.
The structural / constitution / placeholder gates run in the same
order with the same thresholds; only their LLM-call substrate
moved.

## Pre-existing gap closed by this ADR

Codex was introduced as a primary-agent runtime in commits prior to
this session (around `8f8871c` "mature codex runtime parity",
`bac45a8` "add runtime field to AgentRequest") without an ADR.
ADR 12 still read as "Pydantic AI is THE agent runtime." This ADR
retroactively documents codex's foundation as a harness alongside
the two-mode substrate added on top.

## Rationale

Five drivers, decreasing weight.

### 1. Subscription auth eliminates the per-call cost story for helpers

The eval judge and the constitution gate each fire on every turn
when their switches are on. At free-tier Gemini's reasonable
quality bar, the latency is fine but the rate ceiling is real (503
bursts), and OpenRouter free-tier latency is unpredictable.
Codex's auth is the ChatGPT subscription; there is no per-call
billing and no separate rate limit to manage. The same auth path
serves primary + policy + judge + repeat.

### 2. Server-enforced structured output removes parse-failure tail

Codex forwards an `outputSchema` parameter to OpenAI's strict
structured-output mode. When set, the assistant message is
guaranteed to conform to the schema; the previous text-parse-retry
loop is gone by construction. The strict mode requires
`additionalProperties: false` on every object subschema, full
`required` arrays, and no `default` keys. Pydantic-emitted schemas
do not satisfy these by default. The walker `to_strict_json_schema`
rewrites them in a deep-copy without touching the original model.

The pydantic-ai branch keeps text-parse because many free-tier
OpenRouter models do not expose `tool_choice` (verified
2026-05-06: gemma, baidu/cobuddy, owl-alpha all 404'd on it).
Pydantic-ai's tool-calling output mode is unusable on those
providers. Text completion + manual parse works on every
text-generation model and matches what the eval judge has used
since ship 2.

### 3. One harness for tool surface bounding

Codex ships ten built-in tools enabled by default: shell,
unified_exec, apply_patch (freeform), web_search, view_image,
image_generation, computer_use, browser_use, apps, tool_search.
Without bounds, the analyst agent could call any of them; the
`sandbox = "read-only"` setting blocks file writes but does not
hide the tools from the model. The agent must operate against a
known surface of four MCP tools (`wallet_profile`,
`community_summary`, `get_token_info`, `emit_claims`) and nothing
else.

The driver writes a per-actor `config.toml` with the matching
disable expressions when `builtin_tools=frozenset()` is set on the
profile. The disable map lives in
`codex-agent-driver/src/codex_agent_driver/actor_home.py::_BUILTIN_DISABLE_MAP`
and pins to codex source SHA `392e94e9ea756cffd89f35941e881d29b2a81a6e`.

Three enforcement paths were surveyed:

| Path | Reads from | Pros | Cons |
|---|---|---|---|
| `$CODEX_HOME/config.toml` (per-actor) | always | Per-consumer flexibility, configured by the driver, one file per actor. | One source of truth must be the driver. |
| `/etc/codex/requirements.toml` (host MDM) | only if present | Tamper-resistant at the host level. | Requires baking into the container image; parallel enforcement path; identical defaults across consumers. |
| Cloud requirements (codex.com policies) | only with cloud auth | Centrally managed by an admin. | Out of scope for a self-hosted agent service. |

Per AGENTS.md "no parallel paths to the same outcome," only the
config.toml path is wired. A future container-level hardening pass
could layer requirements.toml on top, but doing so today is
duplication with no demonstrated risk it addresses.

Disable map shape (kept in the driver, repeated here for ADR
context):

| Logical tool | Disable expression | Codex source ref |
|---|---|---|
| shell | `[features] shell_tool = false` | `features/src/lib.rs:165` |
| unified_exec | `[features] unified_exec = false` + top-level `experimental_use_unified_exec_tool = false` | `features/src/lib.rs:156` |
| apply_patch (freeform) | `[features] apply_patch_freeform = false` | `features/src/lib.rs:260` |
| web_search | top-level `web_search = "disabled"` | `config_toml.rs` `WebSearchMode` |
| view_image | `[tools] view_image = false` | `config_toml.rs` `ToolsToml.view_image` |
| image_generation | `[features] image_generation = false` | `features/src/lib.rs:503` |
| computer_use | `[features] computer_use = false` | `features/src/lib.rs:489` |
| browser_use | `[features] browser_use = false` | `features/src/lib.rs:483` |
| apps | `[features] apps = false` + `[apps._default] enabled = false` | `features/src/lib.rs:401` |
| tool_search | `[features] tool_search = false` | `features/src/lib.rs:409` |

Updating to a new codex CLI version is a three-step procedure:
read the new `features/src/lib.rs` and `config/src/config_toml.rs`,
update the map if any tool moved or new tools landed, bump the SHA
pin in the source comment. The eval probe `no-builtin-tool-call`
in `evals/cases/model_assertions_codex.yaml` asserts the
`mcae.codex.tool.builtin` span is never emitted in a turn, which
fires when any non-MCP tool gets called and provides the safety
net for a missed map update.

### 4. Family coherence across helpers when the runtime is codex

The eval judge family-leakage guard
(`agent_service.evals.schema._judge_forbidden_families`) defends
against same-family preference bias when the judge model shares a
prefix with the agent stage models. Under codex runtime, primary +
policy + judge + repeat all run as gpt-5.4-mini family. That is the
same family, and the bias risk applies.

Two structural defenses sit alongside the env knob:

- `EVAL_ALLOW_SHARED_FAMILY=true` is an explicit opt-out the
  developer sets when iterating on rubric wording or running an
  experiment where the bias is documented. Default false.
- The judge of a judge architecture (eval judge after-turn rubric
  grader, constitution gate in-turn policy judge) means the judge
  the agent sees and the judge a probe applies are different
  surfaces. Same family does not mean same prompt or same role.

Codex's central model pick is `CODEX_HELPER_MODEL` (defaults to
codex CLI's default if unset). The pydantic-ai side keeps
`AGENT_PRIMARY_MODEL` / `AGENT_POLICY_MODEL` / `EVAL_JUDGE_MODEL`
as separate envs so each role can pick a different free-tier
model.

### 5. Iteration speed for helpers stays acceptable

Codex helper-call latency depends on subprocess state. First call
in a process pays ~1 to 2 seconds for spawn; subsequent calls in
the same process reuse the subprocess via the session pool keyed
on `(profile_id, actor_id, cwd, codex_home)`. The helper profile
uses a fixed `actor_id="helper"`, so all helper calls within a
process share one subprocess after the first. In `just dev` the
first eval probe or first agent turn pays the spawn cost; warm
reuse is fast.

Compared to a free-tier OpenRouter cold start (frequently 30+
seconds on shared pools), the warm-reuse path is faster. Compared
to Gemini (which has no spawn step), codex is slightly slower for
the first call and equivalent thereafter.

## Consequences

### Accepted

- Two harnesses to maintain. The codex-agent-driver lives in a
  sister repo and is editable-installed; bumping it requires
  running the smoke at `agent-service/scripts/smoke_codex_output_schema.py`
  to confirm `outputSchema` still round-trips.
- Per-turn `LlmOverride.provider` field is ignored on the codex
  branch. The dev Models panel UI should reflect that or document
  it; users currently see the dropdown work on pydantic-ai turns
  and silently no-op on codex turns. Tracked as a follow-on.
- Codex's ChatGPT subscription auth is a tenancy concern. The
  service runs against one developer's `~/.codex/auth.json`; a
  multi-tenant deployment would need a different auth model
  (per-tenant codex profile, per-tenant codex_home, no shared
  auth.json bind mount).
- Output mode flip on the policy gate and the repeat detector
  changes Gemini's quality bar from "tool-call enforced" to "emit
  parseable JSON." If Gemini regresses on JSON cleanliness, parse
  failures will spike. Watch `repeat_detector_parse_failed` /
  `constitution_*_parse_failed` log warnings; soft-approve on
  parse failure means quality drift is silent until logs are
  checked.
- `_helper_driver` is a module-level cache. Tests that monkeypatch
  env between cases call `reset_helper_driver_for_testing()` to
  force a fresh driver. The cache is not thread-safe across event
  loops; the single-process FastAPI deployment makes this fine
  today.

### Rejected

- **Codex as a Pydantic AI provider.** Wrapping codex behind
  `OpenAIChatModel` was considered. It would let the policy gate
  keep its tool-calling structured output via `output_type`. It
  also throws away codex's session pool, MCP tool surface, and
  sandbox by reducing codex to a single HTTP-shaped call. Rejected.
- **Per-helper runtime override.** Letting the constitution gate
  use codex while the repeat detector stays on Gemini was
  considered for migration sequencing. Rejected after the eval
  judge migration landed: per-helper picks recreate the mixed-stack
  problem the substrate is designed to eliminate, and the
  migration sequence (judge then constitution then repeat detector)
  worked without it.
- **Codex requirements.toml as the lockdown enforcement path.**
  Considered for tamper resistance. Rejected because it is a
  parallel enforcement path with identical defaults and no per-consumer
  flexibility. Per AGENTS.md.
- **Skip `to_strict_json_schema` and rely on codex's
  `sanitize_json_schema`.** Codex applies its own schema sanitation
  before forwarding to OpenAI. It does not add
  `additionalProperties: false` for objects that lack the flag, so
  pydantic models with `extra="ignore"` still get rejected.
  Confirmed via the smoke script (`ConstitutionVerdict` failed
  before the wrapper, passes after).
- **Per-turn codex driver instance for helpers.** Considered to
  avoid module-level state. Rejected because subprocess spawn cost
  is 1-2 seconds; amortizing it across calls is the whole point of
  the session pool.

## Implementation surface

### Python (`agent-service/src/agent_service/`)

- `llm_runtime.py`. New. `runtime_call` entry point,
  `to_strict_json_schema` walker, `_parse_strict` shared parser,
  `RuntimeCallParseError` with `.raw_text`, `_helper_driver`
  cache, `resolve_helper_runtime`.
- `codex_profile.py`. Extended. `build_codex_helper_profile`
  alongside `build_codex_profile`; the analyst profile gains
  `builtin_tools=frozenset()` to lock the analyst surface to MCP
  only.
- `policy/constitution.py`. Stateless `judge_claim` /
  `judge_narrative` routing through `runtime_call`.
  `build_constitution_agent` removed.
- `repeat_detector.py`. Stateless `detect_repeat` routing through
  `runtime_call`. `build_repeat_agent` removed.
- `evals/probes/llm_judge.py`. Routes through `runtime_call`;
  catches `RuntimeCallParseError` separately to preserve
  `observed.raw_response_first_500`.
- `loop_driver.py`, `main.py`, `core/run.py`, `core/envelope.py`,
  `codex_driver.py`. Updated to drop the helper-agent fields from
  `LoopHandles`, drop the per-turn rebuilds, and forward
  `policy_llm_override` to the gate calls.
- `evals/schema.py`. New `EVAL_ALLOW_SHARED_FAMILY` env knob.
- `spans.py`. New `mcae.codex.tool.builtin` span name and
  attributes. `codex_driver.py` synthesizes one span per non-MCP
  tool call codex makes; the `no-builtin-tool-call` probe asserts
  absence.

### codex-agent-driver (sister package)

- `profile.py`. `CodexBuiltinTool` enum + `builtin_tools` field on
  `CodexAgentProfile`.
- `actor_home.py`. `_BUILTIN_DISABLE_MAP`, `_TomlBuilder`,
  `_write_actor_config` refactor.
- `models.py`. `output_schema: dict | None` on `CodexRunRequest`.
- `provider.py`. Forward `outputSchema` on `turn/start`.

### Env

- `AGENT_DEFAULT_RUNTIME`. `codex` (default) or `pydantic_ai`.
- `CODEX_HELPER_MODEL`. Codex helper-call model id; falls through
  to codex CLI's default.
- `CODEX_PRIMARY_MODEL`. Codex primary-agent model id; falls
  through to codex CLI's default.
- `CODEX_HOME_ROOT`. Directory under which the driver materializes
  per-actor codex homes. Defaults to `./codex_homes`.
- `EVAL_ALLOW_SHARED_FAMILY`. Opts out of the family-leakage guard
  in the eval schema validator. Default false.

### Tests

- `tests/unit/test_llm_runtime.py`. Strict-schema walker,
  `_parse_strict` happy path + four failure modes (no-JSON,
  invalid-JSON, validation, prose surround), runtime resolver.
- `tests/unit/evals/probes/test_llm_judge.py`. Rewritten to mock
  `runtime_call` instead of pydantic-ai `Agent`. Covers both
  runtime branches.
- `tests/integration/test_agent_loop.py::test_get_token_info_redacts_text_when_switch_off`.
  Pins the canonical-mints pass-through contract (see ADR 16).
- `scripts/smoke_codex_output_schema.py`. Live codex turn through
  the strict-schema path. Run with codex CLI on PATH and
  `~/.codex/auth.json` present.

## Verification

End-to-end verification ran during the migration:

- Unit tests: 342 passed across `tests/unit/` and the safe
  integration files.
- Codex outputSchema smoke (`smoke_codex_output_schema.py`):
  `JudgeVerdict` and `ConstitutionVerdict` both PASS, including
  the nested `extraction: null` branch.
- Live `runtime_call` through codex for each migrated helper.
  `judge_narrative` correctly applied Rule 5 (citation discipline)
  and retracted an uncited narrative with populated extraction.
  `detect_repeat` correctly identified an explicit refresh, a
  different-wallet not-repeat, and the empty-prior fast path.
- `mcae.codex.tool.builtin` span: probe `no-builtin-tool-call` in
  `evals/cases/model_assertions_codex.yaml` runs against any
  codex-runtime turn and fails if codex emits a non-MCP tool call.

## References

- ADR 12 (`12-python-agent-migration.md`). The original
  pydantic-ai-only stance; this ADR widens it.
- ADR 13 (`13-agent-observability.md`). Span catalog; this ADR
  adds `mcae.codex.tool.builtin`.
- ADR 14 (`14-agent-eval-substrate.md`). Eval framework; this ADR
  adds `EVAL_ALLOW_SHARED_FAMILY` and the codex runtime for the
  judge.
- ADR 16 (`16-canonical-mint-registry.md`). The display-layer
  hardening for `get_token_info`; orthogonal to runtime choice
  but lands in the same arc.
- Codex source SHA `392e94e9ea756cffd89f35941e881d29b2a81a6e`
  (`openai/codex` repo). The disable map's verification anchor.
- OpenAI structured outputs strict-mode documentation. Requires
  `additionalProperties: false`, full `required`, no `default`.
- `codex-agent-driver` 0.3.0 (sister repo path install).
- Ship commits: `8499a1a`, `d9d1f8b`, `ff97932`, `6bb1e4a`,
  `a4be095`, `7b07998`, `026cada`.
