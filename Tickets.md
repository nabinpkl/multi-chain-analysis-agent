# Tickets — running backlog

Items deferred from prior iterations. Pick from here when starting new work.

## Backlog

- [ ] **SPL token transfers (v1)** — add Token Program + Token-2022 transfer parsing alongside SOL.
- [ ] **Backfill mode / configurable `START_SLOT`** — let the ingester catch up a historical window instead of only tailing tip.
- [ ] **Wallet clustering, materialized views, top-N aggregates** — pre-aggregated `cluster_edges`, `top_wallets_per_day`, etc. See AGENTS.md item 13 for when to do this (after a real query is slow, not before).
- [ ] **Frontend wiring / API endpoints beyond health** — subgraph queries (`/graph/wallet/:pk/neighbors`, `/graph/overview`), tied to LOD-tier rendering plan.
- [ ] **Migrations framework** — replace `CREATE TABLE IF NOT EXISTS` bootstrap with `refinery` once schema starts evolving.
- [ ] **Prometheus metrics + Sentry** — `/metrics` endpoint and panic / error reporting for prod visibility (AGENTS.md items 8 + 15).
- [ ] **Tests** — unit tests for parser (golden block fixtures), integration test for ingester loop with fake RPC + ClickHouse. Add once interfaces stabilize after first end-to-end run.
- [ ] **SOL transfers via PDA lamport mutation** — parser doesn't see these. See "Known Limitations" in `AGENTS.md`. Likely solved by complementing instruction-scan with `preBalances`/`postBalances` diff.
- [ ] **Frontend type contract** — generate TS types from Rust structs via `ts-rs` so the Next.js client consumes typed responses (AGENTS.md item 9).
- [ ] **SSE `/events/stream` for live transfer ticker** — tap ingester's flush path via `tokio::sync::broadcast`, filter for big transfers (e.g. `amount > 1000 SOL`), expose as `axum::response::sse::Sse`. Drives sidebar ticker + counter animations. Poll-based `/graph/overview` covers the aggregate view; SSE is the complement for event-level "feels alive" UX. Day-2 after overview endpoint ships.
- [ ] **ClickHouse materialized views (conditional — not scheduled)** — build `edges_hourly` (MV aggregating `edges` by `hour × from × to`) when *any* of these triggers fire, not before:
  1. A second endpoint lands that reads `edges` (e.g. `/wallet/:pk/neighbors`, `/cluster/:id/flow`). One compute, many consumers is where MVs earn weight vs per-endpoint in-memory caches.
  2. Any aggregate query on `edges` crosses ~300ms in practice (user-perceptible paint lag).
  3. We want to prune raw `edges` retention (90d) while keeping longer history on rollups.
  Schema sketch: `MATERIALIZED VIEW edges_hourly ENGINE = SummingMergeTree ORDER BY (hour, from_wallet, to_wallet) AS SELECT toStartOfHour(toDateTime(block_time)) AS hour, from_wallet, to_wallet, sum(amount) volume, count() tx_count FROM edges GROUP BY ...`. Refreshable-MV variant (`REFRESH EVERY 10 SECOND`) is the alternative to Axum in-memory cache *only* once we run multiple API replicas — single-binary wins with in-memory.


# Ask AI to compute read only query in the graph so we have to be careful to not query delete.
# AI usages

# SOC2 Dashboard for solona

Alternative (bigger change, not now): replace the header with something the panel can't do — a SOC-style event callout ("new whale entered top-500", "cluster X gained 12 edges in 30s"). But that's real work, not a tighten pass. Park it.

