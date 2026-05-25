# SPEC

How this project is built. Contracts, invariants, and the source-of-truth pointers.

For "what it is and is not", see [PRD.md](PRD.md). For repo rules, see [AGENTS.md](AGENTS.md). This document cites the per-subsystem docs in [docs/](docs/) and the decision records in [architecture-decisions/](architecture-decisions/) rather than duplicating them.

## Table of contents

1. [System topology](#system-topology)
2. [Wire contracts](#wire-contracts)
3. [Data model](#data-model)
4. [Ingestion invariants](#ingestion-invariants)
5. [Graph engine](#graph-engine)
6. [Agent runtime](#agent-runtime)
7. [Output gate](#output-gate)
8. [Token metadata and canonical mint registry](#token-metadata-and-canonical-mint-registry)
9. [Observability](#observability)
10. [Eval substrate](#eval-substrate)
11. [Local dev contract](#local-dev-contract)

## System topology

```
                       Solana mainnet RPC
                              |
                              v
                    +--------------------+
                    |  Rust ingester     |  (rate-limited, token-bucket)
                    |  backend/src/ingest|
                    +---------+----------+
                              |
                              v
                    Redpanda (solana.raw-edges)
                              |
                  +-----------+-----------+
                  |                       |
                  v                       v
       +---------------------+   +----------------------+
       | ClickHouse sink     |   | Graph engine         |
       | (ReplacingMergeTree)|   | in-memory FxHashMap  |
       +---------+-----------+   +----------+-----------+
                 |                          |
                 |       HTTP / SSE         |
                 |   +----------------------+
                 |   |
                 v   v
       +-----------------------+     /primitive/* (binary proto)        +-------------------+
       |  Rust API (backend)   | <----------------------------------+   | agent-service     |
       |  axum, port 8002      |                                    |   | Python, FastAPI   |
       |  internal port 8004   | <----- /mcp (rmcp, JSON-RPC) ------+-- | port 8003         |
       +-----------+-----------+                                        +---------+---------+
                   |                                                              |
                   | /graph/snapshot, /graph/stream (SSE)                         |
                   v                                                              | /agent/ask (SSE)
       +-----------------------+                                                  v
       |  Frontend (Next.js)   |  <----- Claim, Narrative SSE frames -------------+
       |  WebGL renderer       |
       |  port 3008            |
       +-----------------------+

       OTel spans from both Rust and Python flow through:
           otel-collector :4318 -> ClickHouse (otel DB) + Langfuse (host :3001)
```

Two services, two processes, one VM. The Rust process owns ingest, the graph, the wire surface to the browser, and the typed primitives the agent calls. The Python service owns the agent loop (pydantic-ai or codex), the output gate, and the SSE stream to the browser.

Service split rationale: ADR [12-python-agent-migration](architecture-decisions/12-python-agent-migration.md).

### Ports

| Port | Owner | Bound on | Purpose |
|------|-------|----------|---------|
| 8002 | backend (api) | host | Public surface: `/health`, `/graph/snapshot`, `/graph/stream` |
| 8004 | backend (api internal) | container only | `/turn/*`, `/primitive/*`, `/mcp`; NOT exposed to host or tunnel |
| 8003 | agent-service | host | `/agent/ask`, `/agent/stream/{id}` |
| 8013 | agent-service-eval | host | Same image as 8003 but pointed at the mock data plane |
| 8005 | eval-mock | host | Hermetic substrate (fixture-backed primitive responses) |
| 3008 | frontend (next dev) | host | Pinned by `just dev` so URLs stay stable |
| 3001 | langfuse-web | host | Langfuse v3 UI (host :3001 -> container :3000 to avoid Next collision) |
| 4318 / 4317 | otel-collector | host | OTLP HTTP / gRPC |
| 8123 / 9000 | clickhouse | host | CH-A: HTTP / native; CH-B (langfuse) is intra-network only |
| 9092 | redpanda | host | Kafka wire-compatible broker |

The `INTERNAL_PORT=8004` listener is the trust boundary. It carries the agent's data-plane calls and is reachable only from the docker compose network (and an `MCP_ALLOWED_HOSTS` allowlist for the `/mcp` route). It is never published to the cloudflared tunnel. See `.env.example` and `backend/src/api/mod.rs::internal_router`.

## Wire contracts

**Single source of truth: `proto/multichain/wire/{shared,agent}/v1/*.proto`.** Everything that crosses a service boundary is defined there. Anything authored as a Rust struct, Python pydantic model, or TS interface that crosses a service boundary is a bug.

Today's proto inventory:

| Package | Files |
|---|---|
| `multichain.wire.shared.v1` | `community_summary.proto`, `get_token_info.proto`, `primitive_envelope.proto`, `provenance.proto`, `role.proto`, `scope.proto`, `snapshot.proto`, `subgraph.proto`, `wallet_profile.proto` |
| `multichain.wire.agent.v1` | `claim.proto`, `constitution.proto`, `diff.proto`, `entity.proto`, `history.proto`, `llm.proto`, `narrative.proto`, `policy.proto`, `session.proto`, `sse.proto`, `switches.proto` |

Codegen flow (`just regen-wire-types`):

- **Rust:** `buffa` + `protoc-gen-buffa` (Anthropic), pure Rust, JSON serialization, zero-copy views. Output: `backend/src/wire/generated/`.
- **Python:** Google `protobuf` runtime + `protoc --python_out`. Output: `agent-service/src/multichain/`.
- **TypeScript:** `@bufbuild/protobuf` + `@bufbuild/protoc-gen-es`, ESM-native. Output: `frontend/src/lib/wire/`.

Generated artifacts are checked in. CI fails if regenerated output differs from checked-in.

### Wire format per hop

Protobuf supports two wire encodings: binary (compact, ~3x smaller, ~5x faster) and canonical JSON (well-specified, browser-friendly). The hop, not the project, picks.

| Hop | Format | Content-Type | Why |
|-----|--------|--------------|-----|
| Browser to Python `/agent/*` | proto canonical JSON | `application/json` | Browser fetch, camelCase TS-friendly |
| Python to Browser `/agent/stream/{id}` (SSE) | proto canonical JSON in SSE `data:` | `text/event-stream` | EventSource is text-only |
| Python to Rust `/primitive/*` | binary protobuf | `application/x-protobuf` | Service-to-service, both speak proto natively |
| Python to Rust `/turn/{begin,end}` | binary protobuf | `application/x-protobuf` | Same |
| Browser to Rust `/health`, `/graph/*` | proto canonical JSON | `application/json` | Browser + curl-debuggable |

Rust HTTP routes MAY accept JSON as a fallback for `curl` debugging, sniffed via `Content-Type`. Production traffic from Python is always binary.

Canonical proto JSON encoding rules (applied automatically by all three runtimes):

- Field names: `snake_case` in `.proto` becomes `camelCase` on wire.
- Oneofs: `{"<active_case_name>": {<sub-message>}}`.
- Enums: full proto name as string (e.g. `"CLAIM_KIND_PROFILE"`).
- 64-bit ints: encoded as JSON strings (JS Number is 53-bit). Use `int32`/`uint32` in `.proto` for fields known to fit.
- Empty messages: `{}` (presence is the signal in oneofs).
- `optional` fields: omitted when not set.
- Bytes: base64. Timestamps: RFC 3339.

## Data model

### ClickHouse tables (database `multichain`)

| Table | Engine | Purpose |
|-------|--------|---------|
| `edges` | `ReplacingMergeTree(version)` keyed on tx signature + ordinal | Every wallet-to-wallet movement. Replaying a slot collapses duplicates on merge. |
| `ingestion_state` | `ReplacingMergeTree(updated_at)` | `(component, last_slot, updated_at)`. Single-row checkpoint per component. |
| `token_metadata` | `ReplacingMergeTree(fetched_at_slot)` | Lazy cache of `(mint, name, symbol, uri, fetched_at_slot)`. TTL via `METADATA_CACHE_TTL_SLOTS`. |
| `memos` | `ReplacingMergeTree` keyed on signature | Memo program payloads, attacker-controlled outer text. |

A separate ClickHouse instance (`langfuse-clickhouse`, CH-B) owns Langfuse's storage. Workload + schema isolation is total. See ADR [13-agent-observability](architecture-decisions/13-agent-observability.md).

### Edge shape

Edges are normalized in `backend/src/ingest/parser.rs::parse_edges` by diffing pre/post balances across SOL and every SPL / Token-2022 mint. The `mint` column is empty for SOL; the mint pubkey for every other token. Mint-issuance and burn residuals are emitted as `kind="mint"` and `kind="burn"` edges using the mint pubkey as the synthetic peer.

The idempotency key is the tuple `(signature, ordinal_within_tx)`. `version` is the slot number; ReplacingMergeTree picks the highest-version row per key on merge. This is the contract that makes re-fetching slot N safe.

### Key proto messages

- `multichain.wire.agent.v1.Claim`: the unit of model output. Carries `kind`, `subject`, `body_markdown` with `{{ref_*}}` placeholders, `provenance` (list of `ProvenanceRef`), `bindings` (list of `NumberRef`). See ADR [docs/agent-design/03-agent-loop-and-injection-defense.md](docs/agent-design/03-agent-loop-and-injection-defense.md).
- `multichain.wire.shared.v1.ProvenanceRef`: opaque pointer back to the primitive call + field that grounded a claim. Verifier reads this when checking values.
- `multichain.wire.agent.v1.Switches`: per-defense ablation surface. Three switches today (`stay_in_role`, `dont_fabricate`, `cross_check`); cross_check has three sub-modes. Both runtimes read the same enum. See ADR [11-agent-switches](architecture-decisions/11-agent-switches.md).
- `multichain.wire.agent.v1.Sse*`: every frame the agent streams to the browser. `Claim`, `Narrative`, `BudgetUpdate`, `AgentDone`, etc.

## Ingestion invariants

These 14 invariants are the contract the ingester must satisfy. AGENTS.md previously embedded the full text; it now points here.

**1. Idempotency on ingestion, non-negotiable.** Every write is idempotent. `signature` (tx hash) is the primary uniqueness key. `ReplacingMergeTree(version)` collapses duplicates on merge. Re-fetching slot N produces the same DB state as fetching it once. Without idempotency, restart = phantom edges in the graph.

**2. Durable checkpoint of last-processed slot.** Single-row `ingestion_state` storing `(component, last_slot, updated_at)`. Update *after* the batch commits, not before. On startup, read to know where to resume. Without this, restart = re-ingest from genesis or skip slots silently. Both bad.

**3. Rate-limited RPC client wrapper.** Hard cap, no per-call drift. The `RpcClient` carries two independent rate-limit lanes (ingester + primitive) using `governor` token buckets. Defaults from `.env.example`: 1 req/s on ingester, 1 req/2s on primitive, combined ~1.5 req/s, comfortably under Solana mainnet's per-IP cap (100 req/10s global). Every call goes through the wrapper. No exceptions.

**4. Batched writes, not single-row inserts.** ClickHouse hates 1-row inserts. Accumulate edges in `Vec<Edge>` per ingestion worker. Flush on: `INGEST_BATCH_SIZE` rows (10k default) OR `INGEST_FLUSH_SECS` (5s default) elapsed, whichever first. `tokio::sync::mpsc` channel between fetcher and writer.

**5. Graceful shutdown.** `tokio::signal::ctrl_c()` triggers shutdown: drain in-flight RPC calls, flush the write buffer, update the checkpoint, exit. Ungraceful shutdown = re-ingest partial slots next start, wasted RPC budget.

**6. Error categorization.** Encoded once in `enum IngestError` with explicit handling in the loop:

| Error | Action |
|---|---|
| `-32007` skipped slot | Increment, continue (not an error) |
| `-32004` block unavailable | Wait, retry same slot (you are at tip) |
| 429 rate limit | Exponential backoff |
| 5xx / network | Retry with backoff, up to N attempts |
| Parse failure | Log + alert + skip slot (do not crash) |
| DB write failure | Retry batch, then crash if persistent |

**7. Structured logging + tracing from start.** `tracing` + `tracing-subscriber`. Log slot fetched, batch flushed, errors with context, rate-limit hits. JSON output in prod (`LOG_FORMAT=json`), pretty in dev. Adding logs after = painful.

**8. Frontend-to-backend type contract.** No hand-typed wire types between Rust and TS. Single proto source, codegen via `just regen-wire-types`. See [Wire contracts](#wire-contracts). Anything hand-typed is a bug.

**9. Config via env vars, not files.** All secrets (RPC key, ClickHouse password, OTel basic auth) from env. `.env` is gitignored; `.env.example` is committed. Oracle VM + Vercel = config in two places; env vars are the lingua franca.

**10. Backpressure boundary between fetch and write.** Bounded `tokio::sync::mpsc::channel(100)` between RPC fetcher and DB writer. If the writer slows, the fetcher blocks on send, ingestion slows, the rate-limit envelope is respected. Unbounded channels under DB stall = OOM.

**11. Health endpoint for the API.** `GET /health` returns `{ last_slot, lag_seconds, db_ok }`. When the frontend looks weird, the first question is "is the ingester alive and current?" The endpoint answers that.

**12. Do not precompute graph metrics yet.** v0 stores raw edges plus simple aggregates. PageRank, centrality, communities materialize only when a query is measurably slow. Building heuristics on imagined queries is over-engineering.

**13. Frontend rendering boundary.** WebGL only above ~10k nodes (Sigma.js / Cosmograph / react-force-graph). Server-side caps: no API returns more than 50k edges per response. One bad query (`top wallets ever`) must not crash the user's tab.

**14. Backpressure on the SSE delta stream.** The graph-engine SSE stream uses bounded broadcast channels per client; a slow consumer is dropped rather than allowed to back up the broadcaster. See ADR [04-differential-rendering](architecture-decisions/04-differential-rendering.md).

What is explicitly NOT a v0 concern: auth, multi-tenancy, horizontal scaling, distributed tracing, real-time WebSocket push to the frontend (SSE is fine), a caching layer (ClickHouse is fast enough), microservices, Kubernetes. Each is a real tool; adding any of them now signals over-engineered small project, not executed cleanly on focused one.

## Graph engine

In-memory, single-process, FxHash-keyed. The graph state is held under an `RwLock`; reads (snapshot, traversal) hold the read lock briefly, writes (apply edge) hold the write lock for one node.

- Adjacency + Union-Find: ADR [03-graph-engine](architecture-decisions/03-graph-engine.md).
- Tombstone handling for deleted edges (slab allocator + free-list, EdgeIdx stable across reuse): ADR [06-tombstone-handling](architecture-decisions/06-tombstone-handling.md).
- Union-Find recompute on delta: ADR [05-union-find-on-delta](architecture-decisions/05-union-find-on-delta.md).
- Louvain snapshot computed every 3s by a background analytics task that reads under a brief lock and broadcasts stable labels: ADR [09-louvain-snapshot-on-backend](architecture-decisions/09-louvain-snapshot-on-backend.md).
- 100k-node+ rendering strategy: ADR [07-handling-100kplus-nodes](architecture-decisions/07-handling-100kplus-nodes.md) and ADR [08-moving-layout-to-worker](architecture-decisions/08-moving-layout-to-worker.md).

Snapshot vs delta protocol:

- `GET /graph/snapshot` returns the full live graph at a coherent point in time.
- `GET /graph/stream` is an SSE stream of delta frames since a client-supplied cursor.
- Reconnect produces snapshot-at-T followed by a non-overlapping delta tail.

## Agent runtime

Two runtimes, one set of defenses. Both call the same typed-primitive surface; both emit the same `Claim` wire format; both pass through the same output gate before any byte reaches the browser.

- **pydantic-ai over HTTP `/primitive/*`** (binary protobuf). Per-role provider configurable; default `gemini-3.1-flash-lite` via the OpenAI-compat endpoint, free tier. See [docs/agent-design/01-agent-overview.md](docs/agent-design/01-agent-overview.md).
- **codex over `/mcp`** (JSON-RPC). Subscription auth via `~/.codex/auth.json`. Default runtime today (`AGENT_DEFAULT_RUNTIME=codex`). Spawned as a per-thread subprocess pool. See ADR [15-codex-as-agent-harness](architecture-decisions/15-codex-as-agent-harness.md).

The runtime selector reads `AgentRequest.runtime`; unspecified falls through to `AGENT_DEFAULT_RUNTIME`. Hermetic eval cases pin runtime per case.

### Loop and injection defense

ReAct loop with three layers of injection defense (structural separation, tool-result-as-data, output policy). See [docs/agent-design/03-agent-loop-and-injection-defense.md](docs/agent-design/03-agent-loop-and-injection-defense.md) and the five chapters under [docs/securing-agents/](docs/securing-agents/).

### Per-defense ablation

Defenses are toggleable from any client via `multichain.wire.agent.v1.Switches`. The contract: every switch maps to a prompt rule, a code path, and at least one eval case (positive + negative). See ADR [11-agent-switches](architecture-decisions/11-agent-switches.md) and [docs/securing-agents/05-per-defense-ablation-and-runtime-parity.md](docs/securing-agents/05-per-defense-ablation-and-runtime-parity.md).

### Resource bounds

A `Principal` carries multi-axis budgets (tokens, db_time_ms, tool_calls, sessions). Construction: cookie + truncated IP (no fingerprinting). Hard per-turn cap on data-lookup primitive dispatches via `AGENT_TURN_TOOL_CALL_BUDGET` (default 8); reporting tools (`emit_claim`) do not count. Both runtimes read this value; keeping them pointed at the same value preserves parity. See [docs/agent-design/05-rate-limit-anonymous-principal-and-token-cost.md](docs/agent-design/05-rate-limit-anonymous-principal-and-token-cost.md).

## Output gate

Three stages, in order, every claim:

1. **Placeholder resolution.** `body_markdown` contains `{{ref_*}}` placeholders that map into the claim's `provenance` and `bindings` lists. Unresolved placeholders fail the gate.
2. **Structural value-compare.** Each `NumberRef` is checked against the underlying primitive result via the binding store. A claim whose numeric value does not match its provenance is dropped or replaced.
3. **LLM judge ("constitution gate").** A separate model reads the claim plus the primitive results and judges whether the prose is grounded. Family-leakage guard: the judge model cannot share a family prefix with the agent's primary model unless `EVAL_ALLOW_SHARED_FAMILY=true` (ICLR 2026: same-family judge biases toward agreeing with itself).

Full pipeline: [docs/securing-agents/03-output-verification-pipeline.md](docs/securing-agents/03-output-verification-pipeline.md). Threats to the judge itself: [docs/securing-agents/07-meta-defense-trust-boundary.md](docs/securing-agents/07-meta-defense-trust-boundary.md).

## Token metadata and canonical mint registry

Metadata is resolved lazily via `backend/src/metadata/fetch.rs::fetch_token_metadata`: Metaplex Token Metadata PDA first, Token-2022 inline metadata extension as fallback. Cached in `multichain.token_metadata` with TTL `METADATA_CACHE_TTL_SLOTS` (default 9000 slots, ~1h). Served through the `get_token_info` primitive.

Display-layer trust: the on-chain `name`/`symbol`/`uri` strings are attacker-controlled. The canonical-mint registry (USDC, USDT, wSOL today) plus a `verified` flag on the `get_token_info` payload is the partial defense. The prompt's `token_verification` rule teaches the model to use canonical labels when verified and qualify the symbol as unverified otherwise.

Full design and the gap list: [docs/architecture/token-metadata-ingestion.md](docs/architecture/token-metadata-ingestion.md), ADR [16-canonical-mint-registry](architecture-decisions/16-canonical-mint-registry.md), and the "Known Limitations" section of [AGENTS.md](AGENTS.md).

## Observability

One OTel ingest, multiple exporters. Both Rust and Python emit spans to the contrib `otel-collector` sidecar at `:4318`. The collector fans out to:

- **CH-A `otel` database.** Queryable alongside the production `multichain` database. Used by eval probes (the eval substrate joins span attributes to case ids).
- **Langfuse v3.** UI at host `:3001` (container `:3000`). Six-service self-hosted stack: postgres, redis, minio, dedicated clickhouse (CH-B), worker, web.

Spans are the single source of truth. The earlier hand-rolled action ledger is superseded; see ADR [13-agent-observability](architecture-decisions/13-agent-observability.md) for the migration. Semantic conventions for domain spans are in the same ADR.

## Eval substrate

Four layers:

- **Schema.** Case YAML describes inputs, fixtures, runtime, probes. Validation rejects family-leakage configurations.
- **Probes.** Each probe is a span-attribute or value assertion against the trace produced by the case. Probe types: `assertion`, `llm_judge`, `runtime_parity`.
- **Runner.** Live mode posts to `/agent/ask` and captures the trace id from the `AgentDone` SSE frame, then runs probes against `otel.otel_traces`. Hermetic mode points the agent at the mock data plane (`eval-mock` at `:8005`) with deterministic fixtures keyed by the case's `fixtures:` field.
- **Baseline.** `just eval-baseline <suite>` consumes the latest run and writes `evals/baselines/<suite>.json`. Subsequent runs diff against it. Refuses to lock in failing probes without `--force`.

Layered design and substrate decisions: ADR [14-agent-eval-substrate](architecture-decisions/14-agent-eval-substrate.md). Frontier checklist of what is/is not covered (May 2026 snapshot): [docs/evals.md](docs/evals.md).

Runner CLIs:

```bash
just eval-hermetic evals/cases-hermetic/wallet_profile_smoke.yaml
just eval-live evals/cases-live/wallet_profile_smoke.yaml
just eval-baseline <suite> [--force]
```

## Local dev contract

### Prereqs

- Docker (for the compose stack).
- Rust toolchain (for backend tests + the `dump-mcp-schemas` cargo bin).
- Python 3.14 with `uv` (for agent-service tests + eval runner).
- `pnpm` (for the frontend dev server).
- `buf` CLI (for proto regen).

### Env vars

Copy `.env.example` to `.env` and fill in. The keys break into groups:

- **Server:** `PORT` (default 8002), `INTERNAL_PORT` (default 8004), `CORS_ORIGIN`, `RUST_LOG`, `LOG_FORMAT`.
- **ClickHouse:** `CLICKHOUSE_URL`, `CLICKHOUSE_DB`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD`.
- **Solana RPC:** `SOLANA_RPC_URL`, `RPC_INGESTER_MIN_INTERVAL_MS`, `RPC_PRIMITIVE_MIN_INTERVAL_MS`, `INGEST_BATCH_SIZE`, `INGEST_FLUSH_SECS`, `METADATA_CACHE_TTL_SLOTS`.
- **MCP:** `MCP_ALLOWED_HOSTS` (Host-header allowlist for the internal `/mcp` route, default `localhost,127.0.0.1,::1,api`).
- **Cloudflare tunnel:** `CLOUDFLARE_TUNNEL_TOKEN` (production only, profile `prod`).
- **Agent:** `AGENT_DEFAULT_PROVIDER`, `AGENT_DEFAULT_RUNTIME`, `AGENT_PRIMARY_MODEL`, `AGENT_POLICY_MODEL`, `EVAL_JUDGE_MODEL`, `EVAL_ALLOW_SHARED_FAMILY`, `AGENT_TURN_TOOL_CALL_BUDGET`, `CODEX_PRIMARY_MODEL`, `CODEX_REASONING_EFFORT`, `AGENT_API_KEY` (OpenRouter), `GEMINI_API_KEY`, `LOCAL_LLM_BASE_URL`.
- **Langfuse:** `LANGFUSE_VERSION`, `NEXTAUTH_SECRET`, `NEXTAUTH_URL`, `SALT`, `ENCRYPTION_KEY`, `LANGFUSE_CLICKHOUSE_PASSWORD`, `LANGFUSE_POSTGRES_PASSWORD`, `LANGFUSE_REDIS_AUTH`, `LANGFUSE_MINIO_ROOT_USER`, `LANGFUSE_MINIO_ROOT_PASSWORD`, `LANGFUSE_INIT_*`, `LANGFUSE_OTEL_AUTH_BASIC`.

`.env.example` has secret-generation one-liners in comments. The compose file aborts startup with a clear error if a required secret is unset.

### Compose profiles

- **Default:** `redpanda`, `clickhouse`, `state-reset`, `api`, `agent-service`, `otel-collector`, `langfuse-*`.
- **`eval` profile:** adds `eval-mock` (host :8005) and `agent-service-eval` (host :8013, same image as `agent-service` but pointed at the mock data plane).
- **`prod` profile:** adds `cloudflared` tunnel.

### Startup order

`docker compose up -d --build` brings the stack up in dependency order:

1. `redpanda`, `clickhouse` (healthchecked).
2. `state-reset` runs once, creates the `otel` database that the collector exporter needs.
3. `api`, `otel-collector`, `agent-service`, langfuse stack.

The Rust ingester resumes from the `ingestion_state` checkpoint on every start.

### Common recipes

```bash
just dev                # frontend dev server on :3008
just test               # full agent-service pytest (wiring-only; budget <5s)
just test-unit          # unit suite only
just test-integration   # integration suite only
just regen-wire-types   # protobuf -> Rust + Python + TS
just dump-mcp-schemas   # refresh the MCP tools/list snapshot for hermetic mock
just docker             # nuke volumes + rebuild from scratch
just eval-hermetic <suite>
just eval-live <suite>
just eval-baseline <suite> [--force]
just eval-pick-wallet   # pick a wallet observable in the current live window
```

After any backend feature change, per AGENTS.md, run `docker compose up -d --build` at the end of the change.
