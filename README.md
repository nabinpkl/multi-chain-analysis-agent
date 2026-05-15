# multi-chain-analysis-engine

A real-time graph of Solana on-chain state with an LLM agent on top. Listen to transactions, normalize them into typed edges, build the wallet graph in memory, serve it to a browser, let an agent answer questions about it. Built solo as a portfolio piece, deployed end-to-end on a single Oracle Free Tier VM behind Cloudflare.

The interesting parts of this repo are not the data pipeline (Kafka into ClickHouse into a Rust in-memory graph is a well-trodden shape). They are:

1. The agent's defense posture. The agent reads attacker-controlled bytes from on-chain token metadata, memos, and user chat. Seven chapters in [docs/securing-agents/](docs/securing-agents/) work through the layered defense, with the unit tests and hermetic eval cases that pin each layer in real code. Includes the meta-defense problem: the constitution gate is itself an LLM and can be attacked.
2. The build-order discipline. [docs/agent-design/00-build-order.md](docs/agent-design/00-build-order.md) is the ship log. Seams correct from ship 1, implementations thicken across ships, with retros appended after every ship so the predicted-vs-actual gap stays visible.
3. The two-runtime architecture. The same agent runs under `pydantic-ai` (HTTP primitives) and `codex` (MCP). Defenses must be bit-for-bit identical on both surfaces; the parity discipline is in [docs/securing-agents/05-per-defense-ablation-and-runtime-parity.md](docs/securing-agents/05-per-defense-ablation-and-runtime-parity.md).

## Architecture in one paragraph

Rust ingester reads Solana RPC, normalizes transactions into edges, publishes to Redpanda. Two consumers: a sink that lands edges in ClickHouse (`ReplacingMergeTree`, idempotent on tx signature), and a graph engine that maintains an in-memory Rust graph (FxHash-keyed). The graph engine exposes `/graph/snapshot` plus an SSE delta stream `/graph/stream`. The frontend is a thin WebGL renderer. The agent-service (Python, FastAPI, pydantic-ai) sits beside the Rust service, calls into it over HTTP `/primitive/*` or MCP `/mcp` for typed graph primitives (`wallet_profile`, `community_summary`, `top_by_metric`, ...), and streams typed `Claim` and `Narrative` SSE frames to the browser. Every claim and every narrative passes through a layered output gate before reaching the wire.

For the long version, see [ARCHITECTURE.md](ARCHITECTURE.md) and the per-subsystem docs in [docs/architecture/](docs/architecture/).

## Stack

| Layer | Choice | Notes |
|---|---|---|
| Ingest + graph + HTTP | Rust + axum 0.8 + tokio | Single binary, single process. Tower middleware for connection hygiene. |
| Stream bus | Redpanda | Kafka wire-compatible, lighter to run on a 24GB VM. |
| Warehouse | ClickHouse | `ReplacingMergeTree(version)` on edges so re-fetching a slot is idempotent. |
| Agent plane | Python 3.14 + pydantic-ai + FastAPI | Two runtimes, pydantic-ai over HTTP and codex over MCP, parity-checked. |
| Frontend | Next.js 16 + Tailwind v4 + shadcn/ui + Zustand | Pages on Vercel, talks to the VM over a Cloudflare tunnel. |
| Wire types | Protobuf via `buf` | Single source of truth; codegen to Rust + Python + TypeScript. |
| Observability | OpenTelemetry + Langfuse | OTel collector sidecar; Langfuse for trace UX. |

[AGENTS.md](AGENTS.md) is the authoritative ruleset for adding dependencies, defining wire types, and naming things in each language. Anything in this README that disagrees with AGENTS.md is wrong and AGENTS.md wins.

## Repo layout

```
backend/             Rust crate. Ingester, graph engine, HTTP + MCP server.
agent-service/       Python agent. Pydantic-ai loop, codex MCP loop, policy gate,
                     eval runner, OTel domain spans.
frontend/            Next.js app. Graph renderer + agent sidebar.
proto/               .proto files. The wire types every service speaks.
evals/cases/         Live eval cases.
evals/cases-hermetic/ Hermetic eval cases. Each pins a specific defense.
evals/baselines/     Frozen probe-result baselines. Regressions diff against these.
docs/                Long-form. See docs/README.md for the map.
architecture-decisions/ ADR-style decision records.
docker-compose.yml   Full local stack: data plane, agent plane, langfuse, eval mock.
justfile             Top-level recipes (test, eval-hermetic, eval-baseline, regen-wire-types).
```

## Run it locally

Prereqs: Docker, Rust toolchain, Python 3.14 (`uv` recommended), pnpm, the `buf` CLI for proto regen.

```bash
# Full stack including eval profile (mock-service + agent-service-eval).
docker compose --profile eval up -d --build

# Frontend dev server on 3008.
just dev

# Unit tests (backend + agent-service).
just test

# Hermetic eval pinned to a single suite.
just eval-hermetic evals/cases-hermetic/wallet_profile_smoke.yaml

# Regen wire types after editing anything under proto/.
just regen-wire-types
```

Backend API is on `:8002`. Agent service is on `:8013` in the eval profile (`:8003` in normal dev). Langfuse UI is on `:3000` once the stack is healthy. Frontend is on `:3008`.

## Where to start reading

If you are reviewing this as a portfolio piece, the order I recommend:

1. [AGENTS.md](AGENTS.md). Repo rules and project intent. Sets context for everything else.
2. [docs/agent-design/00-build-order.md](docs/agent-design/00-build-order.md). The ship-by-ship narrative with retros. Current state lives in the status table near the bottom.
3. [docs/securing-agents/00-overview.md](docs/securing-agents/00-overview.md). The threat-model-to-defense map; pick a chapter from there.
4. The codebase entry points the security overview names: [agent-service/src/agent_service/boundary.py](agent-service/src/agent_service/boundary.py), [agent-service/src/agent_service/policy/](agent-service/src/agent_service/policy/), [agent-service/src/agent_service/prompts/system_v4.txt](agent-service/src/agent_service/prompts/system_v4.txt), [backend/src/mcp.rs](backend/src/mcp.rs).

If you are reviewing this as a data-engineering piece, swap step 2 for [docs/architecture/](docs/architecture/) and the ingest path under [backend/src/ingest/](backend/src/ingest/).

## What this project is not

Not a product. There is no auth, no multi-tenancy, no SLA. The agent's runtime switches are reachable from any client by design, so a visitor can flip a defense off and see what regresses. The constitution gate, the binding store, the canonical-mint registry, all of it is built to be inspected.

Not a complete defense story. Each `securing-agents/` chapter ends with a residuals section naming what is not covered. The "Known Limitations" section of [AGENTS.md](AGENTS.md) lists the data-layer gaps that have not been closed.

Not production-grade ops. Single VM, no horizontal scaling, no real disaster recovery. The free-tier deploy is intentional: the constraint forces a clean small system instead of an over-engineered small system.
