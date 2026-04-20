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
