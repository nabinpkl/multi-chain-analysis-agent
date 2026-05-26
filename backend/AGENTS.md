# `backend/` stack and conventions

These are the picks for the Rust data plane (ingester, graph engine, HTTP + MCP server). Root [../AGENTS.md](../AGENTS.md) carries the cross-service rules; this file is what an agent working in `backend/` needs in front of them.

## Stack

- **Async runtime:** `tokio`. Single binary, single process. One runtime serves both batch ingest (`tokio::io` file streaming) and HTTP.
- **HTTP framework:** `axum` 0.8.x. The 2026 default for Rust HTTP. `axum::response` built-in for SSE.
- **Middleware:** `tower` + `tower-http`. Connection hygiene (per-IP limits, timeouts) lives here.
- **Serialization (non-wire):** `serde` + `serde_json` for internal config and dev-side HTTP JSON fallback. Cross-service types go through protobuf, not hand-typed serde structs.
- **Hashing:** `rustc-hash` (`FxHashSet` / `FxHashMap`). Faster than std `HashMap` for our key shapes.
- **Tracing:** `tracing` + `tracing-subscriber`. JSON output in prod (`LOG_FORMAT=json`), pretty in dev.
- **Rate limiting:** `governor` for token-bucket limiters. Wrap the RPC client once; every call goes through the wrapper.
- **Wire types:** import from `src/wire/generated/` (the `buffa`-generated mod tree from `proto/`). Never hand-author a struct that crosses a service boundary.
- **MCP:** `rmcp` for the `/mcp` route on the internal listener. Host-header allowlist via `MCP_ALLOWED_HOSTS`.

## Conventions

- **Single binary, single process.** No worker pools, no microservices, no message queues inside the Rust side.
- **Idempotency on every write.** Edges land via `ReplacingMergeTree(version)` keyed on tx signature; replaying a slot must produce the same DB state as fetching it once.
- **Bounded channels between fetcher and writer.** `tokio::sync::mpsc::channel(N)`, never unbounded. Backpressure is how the rate limit survives DB stalls.
- **Graceful shutdown.** `tokio::signal::ctrl_c()` drains in-flight RPC, flushes the write buffer, updates the checkpoint, then exits.
- **Error categorization in types.** `enum IngestError` with explicit handling in the loop. Skipped slot (`-32007`), block unavailable at tip (`-32004`), 429, 5xx, parse failure, DB write failure each have their own action.

## Internal listener trust boundary

`INTERNAL_PORT=8004` carries `/turn/*`, `/primitive/*`, `/mcp`. It is bound inside the container only, never published to the host, and must not be put behind any externally-facing reverse proxy or ingress. The agent-service container reaches it via `http://api:8004`. Anything that opens this listener to a wider audience must change the trust model in [../SPEC.md](../SPEC.md) and [../docs/securing-agents/](../docs/securing-agents/) first.

## What goes elsewhere

- The full ingestion-invariant text (14 invariants): [../SPEC.md  Ingestion invariants](../SPEC.md#ingestion-invariants).
- Cross-service stack + versions: [../README.md  Stack](../README.md#stack).
- System topology, ports, hop-by-hop wire format: [../SPEC.md](../SPEC.md).
- Graph-engine internals (Union-Find, tombstones, Louvain snapshot): the ADRs under [../architecture-decisions/](../architecture-decisions/).
