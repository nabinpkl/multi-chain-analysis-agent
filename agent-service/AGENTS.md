# `agent-service/` stack and conventions

These are the picks for the Python agent plane: the loop, the output gate, the eval substrate. This is the load-bearing part of the project (per the framing in root [../AGENTS.md](../AGENTS.md) and [../PRD.md](../PRD.md)); the Rust data plane exists to give this side something honest to ground on.

Root [../AGENTS.md](../AGENTS.md) carries the cross-service rules. This file is what an agent working in `agent-service/` needs in front of them.

## Stack

- **Python:** 3.14. Kept in lockstep with the dev venv; the `openai-codex` SDK only needs `>=3.10`, so this is a parity pin, not a hard floor.
- **Env + packaging:** `uv`. `pyproject.toml` + `uv.lock` are authoritative; no `requirements.txt`.
- **Agent runtimes (two, parity-checked):**
  - `pydantic-ai-slim[openai,mcp]` for the pydantic-ai runtime. Consumes the Rust MCP server at `http://api:8004/mcp` via `MCPServerStreamableHTTP`.
  - `openai-codex` (official OpenAI SDK, Beta-pinned, see [../docs/dependency-exceptions.md](../docs/dependency-exceptions.md)) for the codex runtime. Primary runtime today; drives one shared `AsyncCodex` app-server with native threads, MCP tools, subscription auth via `~/.codex/auth.json`. SDK config + lockdown overlay live in `codex_config.py`; notification parsing in `codex_events.py`; the turn driver in `codex_driver.py`.
- **HTTP + SSE:** `fastapi` + `uvicorn[standard]` + `sse-starlette`.
- **Logging:** `structlog`. Structured from the first log line; JSON in prod.
- **Wire types:** `protobuf` runtime (pinned `>=7.34.1` via `override-dependencies`). Generated package lives at `src/multichain/` and ships in the wheel; never hand-author a wire type.
- **Observability:** `opentelemetry-sdk` + `opentelemetry-exporter-otlp-proto-http` + `opentelemetry-instrumentation-fastapi`. Spans go to `http://otel-collector:4318`. Pydantic-AI emits GenAI semconv spans for free; we add domain spans on top.
- **Eval substrate:**
  - `ruamel.yaml` for case-suite YAML (PyYAML is borderline-stale per the maintenance bar).
  - `clickhouse-connect` (pinned `<1`) for probe queries against `otel.otel_traces`. Server-side parameterized via `parameters=` keyword; never interpolate values client-side.
- **HTTP client (outbound):** `httpx`. No `requests`.
- **Tests:** `pytest` + `pytest-asyncio` + `pytest-httpx` + `dirty-equals`. Async mode is `auto` (no per-function decorator).

## Conventions

- **Absolute imports only.** Never `from ..something import X` or `from .X import Y`. Use `from agent_service.policy.binding_store import X`. Generated `multichain.*` imports are naturally absolute and stay that way. (Root [../AGENTS.md](../AGENTS.md) makes this a global rule; restating because the pain is loudest here.)
- **No live LLM calls in tests.** The `pytest` baseline budget is `<5s`; longer means a real OpenRouter/Gemini call snuck past the `TestModel` boundary. Conftest has an autouse fixture stubbing `agent_service.llm.make_model`; tests that need the real function opt in via `@pytest.mark.real_llm` and must not make network calls.
- **Two-runtime parity is the contract.** Any defense, switch, prompt rule, or budget enforced on one runtime must be enforced identically on the other. Both runtimes read the same env values (`AGENT_TURN_TOOL_CALL_BUDGET`, model ids, switch defaults). Keeping them pointed at the same value preserves parity; diverging values silently breaks evals.
- **DeprecationWarning is an error.** `pytest` `filterwarnings = ["error::DeprecationWarning:agent_service.*"]` so codegen / pydantic / pydantic-ai drift bubbles up as a test failure, not silent rot.
- **Eval-judge family-leakage guard.** The judge model cannot share a family prefix with the agent's primary model unless `EVAL_ALLOW_SHARED_FAMILY=true`. Schema validation enforces this at YAML load time. ICLR 2026: same-family judge biases toward agreeing with itself.
- **ClickHouse queries are parameterized.** Always pass values through `clickhouse_connect`'s `parameters=` keyword. Never f-string a value into the SQL. The wrapper in `agent_service/evals/ch.py` enforces this.
- **Codex thread + home hygiene.** Isolation is per native codex thread (`thread_start` / `thread_resume`), not per-thread `codex_home`. One shared `AsyncCodex` app-server serves all analyst chats; a separate helper app-server (own `CODEX_HOME`) serves the gates / judge / repeat detector so analyst MCP traffic never bleeds in. The built-in-tool lockdown + MCP mount ride on the per-thread `config` overlay (`codex_config.py`). Host `~/.codex` is mounted read-only (auth source); the app-servers write only under their writable `CODEX_HOME`. Do not write to the host base from inside the container.

## Output-gate discipline

Every `Claim` and `Narrative` passes three stages before reaching the SSE wire:

1. Placeholder resolution (`{{ref_*}}` in `body_markdown` must resolve against `provenance` + `bindings`).
2. Structural value-compare against the binding store.
3. LLM judge.

Any new model output channel must opt into the same pipeline. Bypassing the gate, even "temporarily for debugging", violates the "no parallel paths" rule in root [../AGENTS.md](../AGENTS.md). Add a switch and an eval case instead.

## What goes elsewhere

- Full output-gate design: [../docs/securing-agents/03-output-verification-pipeline.md](../docs/securing-agents/03-output-verification-pipeline.md).
- Two-runtime parity discipline: [../docs/securing-agents/05-per-defense-ablation-and-runtime-parity.md](../docs/securing-agents/05-per-defense-ablation-and-runtime-parity.md).
- Switch contract per defense: ADR [../architecture-decisions/11-agent-switches.md](../architecture-decisions/11-agent-switches.md).
- Observability spans + Langfuse setup: ADR [../architecture-decisions/13-agent-observability.md](../architecture-decisions/13-agent-observability.md).
- Eval substrate four-layer design: ADR [../architecture-decisions/14-agent-eval-substrate.md](../architecture-decisions/14-agent-eval-substrate.md).
- Codex-as-harness rationale: ADR [../architecture-decisions/15-codex-as-agent-harness.md](../architecture-decisions/15-codex-as-agent-harness.md).
- Per-runtime cost-budget framework: [../docs/agent-design/05-rate-limit-anonymous-principal-and-token-cost.md](../docs/agent-design/05-rate-limit-anonymous-principal-and-token-cost.md).
- Cross-service stack + versions: [../README.md  Stack](../README.md#stack).
