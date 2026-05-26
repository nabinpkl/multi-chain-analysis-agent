# Why ClickHouse for the warehouse

ClickHouse owns three distinct workloads in this project: the canonical `edges` table that captures every wallet-to-wallet movement, the lazy `token_metadata` cache that backs `/primitive/get_token_info`, and a separate instance (CH-B) that holds Langfuse's observability data. This doc explains why ClickHouse is the right shape for all three, and why the data plane uses a columnar OLAP store rather than the more obvious choices (Postgres, SQLite, an embedded KV store).

For the data model contract see [SPEC.md  Data model](../../SPEC.md#data-model). For the observability split between CH-A and CH-B see ADR [13-agent-observability](../../architecture-decisions/13-agent-observability.md).

## The decision

One ClickHouse instance (CH-A) holds the project's canonical data: `multichain.edges`, `multichain.ingestion_state`, `multichain.token_metadata`, `multichain.memos`. The schema is bootstrapped on every API start by `backend/src/store/schema.rs::bootstrap`; it is safe to call repeatedly.

The edges table uses `ReplacingMergeTree(version)` keyed on `(signature, instruction_idx)` with `version = slot`. That single choice is what makes the entire ingestion contract work.

```
ENGINE = ReplacingMergeTree(version)
ORDER BY (signature, instruction_idx)
```

A second ClickHouse instance (CH-B) is dedicated to Langfuse and ships in the docker-compose stack alongside the rest of the langfuse stack (postgres, redis, minio, worker, web). It speaks the same protocol but is workload-isolated from CH-A.

## What this design buys

**Idempotency at the storage layer, not the application layer.** The ingester WILL re-fetch slot N on restart, on retry, on race with the tip tracker. With `ReplacingMergeTree(version)`, re-inserting an `(signature, instruction_idx)` row that already exists is harmless: the duplicate collapses on the next merge, and reads always see the highest-`version` row per key. The application never needs to ask "is this row already there?" The 14 ingestion invariants in [SPEC.md](../../SPEC.md#ingestion-invariants) include "every write is idempotent" as invariant #1; ClickHouse makes it free.

**Columnar storage matches the query shape.** Every primitive the agent calls (`wallet_profile`, `community_summary`, `top_by_metric`, `time_window_diff`) is a scan over an axis of the edges table: "all edges where `from_wallet = X` in the last 60s", "top 100 wallets by edge count in a window". These are columnar queries. Row-store would force a table scan or an index per query shape; columnar reads only the columns that matter, decompresses block-at-a-time, and finishes in milliseconds. The actual single-node benchmark on the live window stays well under 100ms for the heavy primitives.

**Batched inserts at the engine's preferred unit.** ClickHouse is known to dislike single-row inserts (it produces too many small parts, and merges fall behind). The ingester buffers edges in `Vec<Edge>` and flushes on `INGEST_BATCH_SIZE` rows or `INGEST_FLUSH_SECS` seconds, whichever comes first (defaults 10000 / 5s). This is invariant #4 in [SPEC.md](../../SPEC.md#ingestion-invariants); the buffer size is sized to put each insert in ClickHouse's happy zone.

**Lazy metadata cache is a one-line schema.** `multichain.token_metadata` is also `ReplacingMergeTree`, keyed on `mint`, versioned by `fetched_at_slot`. First read for a mint hits RPC, decodes the on-chain metadata (Metaplex Token Metadata PDA first, Token-2022 inline metadata extension as fallback), and writes a row. Subsequent reads serve from the table until `fetched_at_slot` falls outside the TTL window (`METADATA_CACHE_TTL_SLOTS`, default ~1 hour). When CDC-from-instructions lands (issue #48), the same table accepts ingest-time writes and the lazy path becomes dead code. The schema does not change; only the writer does. That is the property a `ReplacingMergeTree` versioned by `fetched_at_slot` was chosen to give.

**Two ClickHouse instances are cheap.** CH-A (the canonical project DB) and CH-B (Langfuse's storage) run as two separate compose services with independent volumes, passwords, and resource budgets. Workload isolation is total: a runaway agent query against `otel.otel_traces` cannot starve the live `edges` reads. The cost is one additional volume and one additional service definition; the benefit is the kind of isolation a single shared instance would lose under load. See ADR [13-agent-observability](../../architecture-decisions/13-agent-observability.md) for the full rationale.

**Otel-collector ships traces in natively.** The OpenTelemetry collector (`otel/opentelemetry-collector-contrib`) has a first-class `clickhouseexporter` that writes `otel_traces`, `otel_logs`, and `otel_metrics` tables with schema management built in. Both runtimes (pydantic-ai, codex) emit spans through the same collector; the eval substrate queries the resulting traces by `trace_id` for probe assertions. No bespoke ingest path, no glue code. ClickHouse is the storage layer the observability ecosystem already expects.

## What this design costs

**Operational footprint.** ClickHouse is heavier than SQLite or a small Postgres. CH-A and CH-B together carry two volumes, two memory budgets, two healthchecks. For the single-host deploy that the project targets, this is still well within budget; for someone forking the repo to run on a very small instance, ClickHouse is the largest individual service in the compose stack. Mitigation: ClickHouse Server's default memory budget is tunable via `z_clickhouse-overrides.xml` (committed at `infra/`), and we do tune it down.

**Schema migrations require care.** ClickHouse's `ALTER TABLE` semantics are non-trivial under load (some alters are O(table-size), some are metadata-only). The current strategy is a one-line `DROP TABLE IF EXISTS multichain.edges` + recreate at startup (see `bootstrap()`), accepted because edges are re-derivable from the Solana RPC checkpoint. For tables that cannot be wiped (e.g. `token_metadata` once CDC lands and becomes the source of truth), we will need a real migration tool. We do not have one yet; this is acknowledged debt.

**Distributed semantics are not used.** ClickHouse is famous for its distributed-cluster story (Distributed engine, ReplicatedMergeTree, sharding). We use none of it. CH-A is a single node, single replica. The choice is consistent with the project's "single-host, no horizontal scaling" stance (per [PRD.md](../../PRD.md)), but it does mean we are using a small subset of what ClickHouse can do. If the project ever grew to a multi-node deploy, that complexity would land all at once; for now we are paying ClickHouse-shaped operational cost without yet getting ClickHouse-shaped scale benefits.

**The Rust client lags Python.** `clickhouse-rs` (used by the Rust data plane) has fewer ergonomic features than Python's `clickhouse-connect` (used by the agent-service's eval probes). Specifically, server-side parameter binding via `{name:Type}` placeholders is cleaner in Python. The Rust side compensates by keeping query shapes simple and constructing them with `format!`-with-numeric-literals; the agent-service uses `parameters=` for anything that takes a runtime value. The split is acceptable today but worth knowing.

## The alternatives we rejected

**PostgreSQL with TimescaleDB.** Mature, well-known, has hypertables for time-series, has a real migration story. Rejected because the workload is fundamentally OLAP scans, not OLTP transactions: every primitive is "scan a window of edges and aggregate", not "fetch row by primary key". Postgres can do this, but it requires explicit denormalization, materialized views, and aggressive index management. ClickHouse does it as the default behavior. The point-read story (e.g. "fetch by `signature`") is the one place Postgres would have been ergonomically nicer; the analyst surface is the rest of the application, and analyst queries are what determine the warehouse choice.

**SQLite with FTS.** Trivially small operational footprint. Rejected because the live `edges` workload would saturate SQLite's writer contention model under the ingester's batched-insert cadence, and because the eval-probe query shape (joining trace spans across thousands of rows) is not what SQLite is built for. SQLite is the right choice when concurrent writers are not a thing; here, the ingester and the agent's `/primitive/get_token_info` cache-fill path are both writers.

**Apache Druid or Apache Pinot.** Real columnar OLAP, real distributed story, real ingestion connector ecosystem. Rejected because both require operating a cluster (coordinator, broker, historical, etc.) even at single-node scale, and the single-host constraint makes the operational complexity not worth the marginal capability gain over ClickHouse-single-node. If the project ever scaled to "multiple analyst nodes serving thousands of concurrent agent turns", Druid would be worth revisiting; today, no.

**A purpose-built graph database (Neo4j, Memgraph, KuzuDB).** The actual data IS graph-shaped. Rejected because the live graph already lives in memory in the Rust process (see [WhyRust.md](WhyRust.md)) and ClickHouse is the warehouse beneath it, not the operational graph. The two-layer split (in-memory graph for read paths, columnar warehouse for analytical paths and history) lets each layer be excellent at one thing. Adding a graph DB as a third store would mean three sources of truth for "what edges exist", which is two more than this project can afford.

## The contract with future ships

ClickHouse stays the warehouse. The shape of "what tables live in CH-A" can grow (new aggregations, new materialized views, the eventual CDC-from-instructions write-through path), and a real migration tool will land when the first table that cannot be wiped-and-rebuilt comes online. CH-B stays as a workload-isolated Langfuse store unless and until the observability surface changes.
