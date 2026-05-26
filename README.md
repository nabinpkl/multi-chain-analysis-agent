# multi-chain-analysis-agent

An LLM analyst over a real-time Solana wallet graph. The agent answers questions about live on-chain wallet behavior using a fixed set of typed primitives; every claim is structurally verified before any prose reaches the user.

**This is an agent-design exercise.** The blockchain is the chosen substrate, not the subject. Solana mainnet was picked because it produces real public high-volume data, which forces clean ingest, idempotent writes, rate-limit discipline, and grounded narrative. A tutorial-shaped dataset lets bad agent shapes look fine. A live chain does not.

## Where to read first

- [PRD.md](PRD.md). What this is, what it is not, in/out of scope, success metrics.
- [SPEC.md](SPEC.md). How it is built. System topology, wire contracts, data model, ingestion invariants, agent runtime, output gate, observability, eval substrate, local dev contract.
- [AGENTS.md](AGENTS.md). The repo rules. Read before opening a PR.
- [docs/securing-agents/00-overview.md](docs/securing-agents/00-overview.md). Transferable lessons on securing LLM agents, with this codebase as the worked example. Seven chapters, threat-model-to-defense map.
- [docs/agent-design/00-build-order.md](docs/agent-design/00-build-order.md). The ship-by-ship narrative with retros. Current state lives in the status table near the bottom.
- [architecture-decisions/](architecture-decisions/). ADRs for the load-bearing technical choices.

## What is interesting here

1. **The agent's defense posture.** The agent reads attacker-controlled bytes from on-chain token metadata, memos, and user chat. Seven chapters in [docs/securing-agents/](docs/securing-agents/) work through the layered defense, with the unit tests and hermetic eval cases that pin each layer in real code. Includes the meta-defense problem: the LLM judge is itself an LLM and can be attacked.
2. **Two-runtime parity.** The same agent runs under `pydantic-ai` (HTTP primitives) and `codex` (MCP). Defenses must be bit-for-bit identical on both surfaces. Runtime-parity discipline: [docs/securing-agents/05-per-defense-ablation-and-runtime-parity.md](docs/securing-agents/05-per-defense-ablation-and-runtime-parity.md).
3. **Per-defense ablation switches.** Defenses are toggleable from any client by design, so a visitor can flip one off and see which eval cases regress. Switch surface: ADR [11-agent-switches](architecture-decisions/11-agent-switches.md).
4. **Build-order discipline.** [docs/agent-design/00-build-order.md](docs/agent-design/00-build-order.md) is the ship log. Seams are correct from ship 1; implementations thicken across ships, with retros appended after every ship so the predicted-vs-actual gap stays visible.

## Architecture in one paragraph

A Rust ingester reads Solana RPC, normalizes transactions into edges, publishes to Redpanda. Two consumers: a sink that lands edges in ClickHouse (`ReplacingMergeTree`, idempotent on tx signature), and a graph engine that maintains an in-memory Rust graph (`FxHashMap`-keyed). The graph engine exposes `/graph/snapshot` plus an SSE delta stream at `/graph/stream`. The frontend is a thin WebGL renderer. The agent-service (Python, FastAPI, pydantic-ai or codex) sits beside the Rust service, calls into it over HTTP `/primitive/*` or MCP `/mcp` for typed graph primitives (`wallet_profile`, `community_summary`, `top_by_metric`, ...), and streams typed `Claim` and `Narrative` SSE frames to the browser. Every claim and every narrative passes through a layered output gate before reaching the wire.

For the long version, see [SPEC.md](SPEC.md) and the per-subsystem docs in [docs/architecture/](docs/architecture/).

## Stack

| Layer | Choice | Notes |
|---|---|---|
| Ingest + graph + HTTP | Rust + axum 0.8 + tokio | Single binary, single process. Tower middleware for connection hygiene. |
| Stream bus | Redpanda | Kafka wire-compatible, single binary, no JVM, no ZooKeeper. |
| Warehouse | ClickHouse | `ReplacingMergeTree(version)` on edges so re-fetching a slot is idempotent. |
| Agent plane | Python 3.14 + pydantic-ai + FastAPI | Two runtimes (pydantic-ai over HTTP, codex over MCP), parity-checked. |
| Frontend | Next.js 16 + Tailwind v4 + shadcn/ui + Zustand | Talks to the agent-service over HTTP/SSE. Deployable anywhere static-Next.js + a public agent-service URL can coexist. |
| Wire types | Protobuf via `buf` | Single source of truth; codegen to Rust + Python + TypeScript. |
| Observability | OpenTelemetry + Langfuse v3 | OTel collector sidecar; Langfuse self-hosted for trace UX. |

[AGENTS.md](AGENTS.md) is the authoritative ruleset for adding dependencies, defining wire types, and naming things in each language. Anything in this README that disagrees with AGENTS.md is wrong and AGENTS.md wins.

## Repo layout

```
backend/                Rust crate. Ingester, graph engine, HTTP + MCP server.
agent-service/          Python agent. Pydantic-ai loop, codex MCP loop, policy gate,
                        eval runner, OTel domain spans.
frontend/               Next.js app. Graph renderer + agent sidebar.
proto/                  .proto files. The wire types every service speaks.
evals/cases-live/       Live eval cases (real mainnet, real models).
evals/cases-hermetic/   Hermetic eval cases (mock data plane, deterministic fixtures).
evals/baselines/        Frozen probe-result baselines. Regressions diff against these.
docs/                   Long-form. See docs/README.md for the map.
architecture-decisions/ ADR-style decision records.
docker-compose.yml      Full local stack: data plane, agent plane, langfuse, eval profile.
justfile                Top-level recipes (test, eval-hermetic, eval-baseline, regen-wire-types).
PRD.md                  Product: what it is and is not, scope, success metrics.
SPEC.md                 Technical: contracts, invariants, source-of-truth pointers.
AGENTS.md               Repo rules.
```

## Run it locally

Prereqs: Docker, Rust toolchain, Python 3.14 (`uv` recommended), `pnpm`, the `buf` CLI for proto regen.

```bash
# 1. Copy env template and fill required secrets. The .env.example
#    comments include openssl one-liners for the Langfuse secrets.
cp .env.example .env
$EDITOR .env

# 2. Full stack including the eval profile (eval-mock + agent-service-eval).
docker compose --profile eval up -d --build

# 3. Frontend dev server on :3008.
just dev

# 4. Unit tests (agent-service wiring; budget <5s, no real LLM calls).
just test

# 5. Hermetic eval pinned to a single suite.
just eval-hermetic evals/cases-hermetic/wallet_profile_smoke.yaml

# 6. Regen wire types after editing anything under proto/.
just regen-wire-types
```

Default ports after the stack is healthy:

| Port | Service |
|------|---------|
| 8002 | Rust API (public: `/health`, `/graph/snapshot`, `/graph/stream`) |
| 8003 | agent-service (`/agent/ask`) |
| 8013 | agent-service-eval (eval profile only, points at the mock data plane) |
| 8005 | eval-mock (eval profile only) |
| 3008 | Frontend dev server |
| 3001 | Langfuse v3 UI (host :3001 -> container :3000 to avoid Next collision) |
| 4318 / 4317 | OTel collector (HTTP / gRPC) |
| 8123 / 9000 | ClickHouse (HTTP / native) |

Full env-var inventory and what each one does: see the [Local dev contract](SPEC.md#local-dev-contract) section of SPEC.md and the comments in `.env.example`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Before opening a PR, read [AGENTS.md](AGENTS.md): it is the authoritative ruleset on dependencies, wire types, idiomatic conventions, and what not to do.

## Reporting security issues

See [SECURITY.md](SECURITY.md). The agent defense layers are explicitly in scope; the public demo VM is not.

## What this project is not

Not a product. There is no auth, no multi-tenancy, no SLA. The agent's runtime switches are reachable from any client by design, so a visitor can flip a defense off and see what regresses. The LLM judge, the binding store, the canonical-mint registry, all of it is built to be inspected.

Not a complete defense story. Each `securing-agents/` chapter ends with a residuals section naming what is not covered. The "Known Limitations" section of [AGENTS.md](AGENTS.md) lists the data-layer gaps that have not been closed.

Not production-grade ops. The stack is `docker compose up` on one host; there is no horizontal scaling, no leader election, no real disaster recovery story. The single-host constraint is intentional: it keeps the system small and clean instead of forcing premature distributed-systems complexity.

See [PRD.md](PRD.md) for the full in-scope / out-of-scope / non-goals breakdown.

## License

Apache License 2.0. See [LICENSE](LICENSE).
