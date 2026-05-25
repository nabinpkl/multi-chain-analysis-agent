# PRD

What this project is, what it is not, and what is in scope right now.

For "how it is built", see [SPEC.md](SPEC.md). For repo rules, see [AGENTS.md](AGENTS.md).

## Framing

This is an **agent-design exercise**. The blockchain is the chosen substrate, not the subject. Solana mainnet was picked because it produces real public high-volume data: clean ingest, idempotent writes, rate-limit discipline, and grounded narrative all become non-negotiable when the firehose is real. A tutorial-shaped dataset would let bad agent shapes look fine. A live chain does not.

The agent is the load-bearing part. Everything else (the Rust ingester, ClickHouse, the in-memory graph) exists to give the agent something honest to ground on.

## Problem statement

Reading on-chain wallet behavior is hard. Block explorers stop at a single transaction. RPCs return raw JSON keyed by signature, not by relationship. Anyone trying to answer "what is this wallet actually doing, and who else is it doing it with" today reconstructs the graph by hand for every question. Existing LLM-powered analyst tools that try to answer such questions are closed-source, paid, and opaque about how their model features ground (or do not ground) their answers. The interesting question is not "can an LLM do this", it is "can an LLM do this with defenses that are inspectable, switchable, and verifiable end-to-end".

## Product vision

An LLM agent that answers questions about live Solana wallet behavior, grounded in a real-time on-chain graph, using a fixed set of typed primitives. The agent never authors SQL or hand-rolls graph traversals; it composes pre-defined primitives whose results are checked structurally before any prose reaches the user. The defenses around the agent are open and inspectable, including switches that let a visitor turn each defense off and watch what regresses.

## In scope today

Each item below has a one-line acceptance bar that an outside reader can verify by reading the cited source.

1. **Solana mainnet ingestion.** Rust ingester pulls `getBlock`, normalizes every wallet-to-wallet movement (SOL + every SPL / Token-2022 mint), publishes to Redpanda. Idempotent on tx signature. Bar: re-fetching slot N produces the same ClickHouse rows as fetching it once. See [SPEC.md  Ingestion invariants](SPEC.md#ingestion-invariants).
2. **In-memory graph engine.** Rust process maintains the live graph in `FxHashMap`-backed structures, exposes `/graph/snapshot` and an SSE delta stream at `/graph/stream`. Bar: a frontend reconnect produces a consistent snapshot followed by a non-overlapping delta tail. See ADR [03-graph-engine](architecture-decisions/03-graph-engine.md), ADR [06-tombstone-handling](architecture-decisions/06-tombstone-handling.md).
3. **Typed primitive surface for the agent.** Seven read-only primitives (`wallet_profile`, `community_summary`, `path_between`, `top_by_metric`, `time_window_diff`, `tag_lookup`, `get_token_info`) reachable over HTTP `/primitive/*` and MCP `/mcp`. Bar: no primitive accepts free-form SQL; every call has a typed envelope from `proto/multichain/wire/shared/v1/`. See [docs/agent-design/02-typed-primitive-layer.md](docs/agent-design/02-typed-primitive-layer.md).
4. **Two-runtime agent with parity.** The same agent runs under `pydantic-ai` (HTTP primitives) and `codex` (MCP). Defenses must be bit-for-bit identical on both surfaces. Bar: hermetic eval cases pass on both runtimes; a runtime-parity case under `evals/cases-hermetic/` regresses if a defense lands on only one side. See [docs/securing-agents/05-per-defense-ablation-and-runtime-parity.md](docs/securing-agents/05-per-defense-ablation-and-runtime-parity.md).
5. **Layered output gate.** Every `Claim` and `Narrative` SSE frame passes a placeholder gate, a structural value-compare against the binding store, and an LLM judge before reaching the wire. Bar: a claim whose numeric value does not match its provenance reference is replaced or dropped before send. See [docs/securing-agents/03-output-verification-pipeline.md](docs/securing-agents/03-output-verification-pipeline.md).
6. **Per-defense ablation switches.** Defenses are toggleable from any client by design, so a visitor can flip one off and see which eval cases regress. Bar: the switch surface is in protobuf at `proto/multichain/wire/agent/v1/switches.proto`; both runtimes read the same enum. See ADR [11-agent-switches](architecture-decisions/11-agent-switches.md).
7. **Anonymous resource bounds.** A cookie-plus-truncated-IP principal carries multi-axis budgets (tokens, db_time_ms, tool_calls, sessions). Bar: a request that exceeds any axis returns a typed budget-exhausted result, not a 500. See [docs/agent-design/05-rate-limit-anonymous-principal-and-token-cost.md](docs/agent-design/05-rate-limit-anonymous-principal-and-token-cost.md).
8. **Observability backbone.** OpenTelemetry spans from both runtimes land in a contrib otel-collector, fan out to ClickHouse (`otel` DB, queryable alongside `multichain`) and Langfuse. Bar: every agent turn is a trace; every eval probe runs against the same span shape that production emits. See ADR [13-agent-observability](architecture-decisions/13-agent-observability.md).
9. **Hermetic eval substrate.** Cases under `evals/cases-hermetic/` run against a mock data plane (`eval-mock` at `:8005`) with deterministic fixtures, so a defense ablation produces a reproducible regression signal independent of mainnet drift. Bar: `just eval-hermetic <suite>` exits non-zero on any probe regression vs the committed baseline. See ADR [14-agent-eval-substrate](architecture-decisions/14-agent-eval-substrate.md).

## Out of scope today

Not yet built. May be built later if the project keeps going.

- Chains other than Solana. The wire types in `proto/` are generic enough to accept them, but no other adapter exists.
- Off-chain JSON metadata fetching from token `uri` fields. The on-chain triple (`name`, `symbol`, `uri`) is resolved; the URI is passed through as an opaque string.
- LSTs (JitoSOL, mSOL, bSOL) and non-stablecoin majors (JUP, BONK, PYTH, WIF) in the canonical-mint registry. Only USDC, USDT, wSOL today. See ADR [16-canonical-mint-registry](architecture-decisions/16-canonical-mint-registry.md).
- Mobile clients. The frontend assumes a desktop browser with WebGL.
- Pre-computed graph metrics beyond simple aggregates. PageRank, centrality, and community labels are computed on demand; materializing them is deferred until a query is measurably slow.
- Real-time WebSocket push of agent state. The frontend polls the SSE stream.

## Non-goals (permanent)

Will not be built. These are not "later" items; they are out of project scope by design.

- **A replacement for Helius, Solscan, or Solana Beach.** This is a graph-and-agent demo, not an explorer or an indexer-as-a-service.
- **A trading tool or signal generator.** No buy/sell recommendations, no MEV analysis, no on-chain writes.
- **A financial-advice surface.** Narrative claims describe observed graph state. They are not opinions about value or risk.
- **A multi-tenant SaaS.** No auth, no accounts, no tenancy isolation. The deployment is one VM, one process, anonymous visitors.
- **A production-grade ops target.** No horizontal scaling, no disaster recovery, no on-call rotation. The single-VM constraint is the point.

## Users

- **Forensic analysts** trying to understand wallet behavior without writing custom indexers.
- **Security researchers** studying how LLM agents over structured data can be defended (and attacked). The defense switches exist for them.
- **Engineers** putting LLMs over their own graphs, who want a worked example of layered output verification, runtime parity, and ablation discipline.

The project is not built for end-user retail traders.

## Success metrics

The project succeeds if these stay healthy across changes:

- **Hermetic eval pass rate.** Every committed defense has at least one positive and one negative case in `evals/cases-hermetic/`. Both must pass on both runtimes for the suite to be green.
- **Runtime-parity diff = 0.** Eval probes that compare pydantic-ai output to codex output for the same case must agree on every checked field.
- **Output-gate drop rate stays observable.** The percentage of claims dropped by the structural verifier is a span attribute, not a hidden counter; a regression in claim quality shows up in Langfuse before a user notices.
- **p95 primitive latency under load.** Single-VM, so the target is "the graph stays responsive while the agent thinks", not nines.

The project is not measured by user count, retention, or revenue. There are no users in the SaaS sense.

## Current scope progress

The ship-by-ship status table lives in [docs/agent-design/00-build-order.md](docs/agent-design/00-build-order.md). That document is the source of truth; this PRD does not mirror it because the mirror would rot. Read the build-order doc for which ships are done, which are in flight, and which have not started.

## Known limitations

The data-layer gaps that have not been closed are listed in the "Known Limitations" section of [AGENTS.md](AGENTS.md). That section is updated as gaps open or close. Per-defense residuals (what each chapter explicitly does not cover) are at the end of every chapter in [docs/securing-agents/](docs/securing-agents/).

The biggest open gap to know about before forming opinions:

- **Token symbols are attacker-controlled at the display layer.** The on-chain `name` and `symbol` strings are whatever the mint authority embedded at creation; anyone can mint a Token-2022 with `name="USD Coin"` at a non-canonical pubkey. The canonical-mint registry (USDC, USDT, wSOL) plus the `verified` flag on `get_token_info` payloads is the current partial defense. The data layer is unambiguous (mint pubkeys are forge-proof); the human-facing narrative is where the risk lives. See ADR [16-canonical-mint-registry](architecture-decisions/16-canonical-mint-registry.md) and the "Token metadata" section of [AGENTS.md](AGENTS.md).
