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

# For blockchain learner see a emerging pattern from data cex dex etc
# Emergence from data framing
Star with 100+ spokes = CEX hot wallet (Binance, OKX) — one wallet receiving deposits from and sending withdrawals to many users
Medium hub with fewer thicker edges = DEX pool / aggregator — fewer counterparties, higher per-edge volume
Dense multi-party cluster = MEV bots cycling, arbitrage rings, or a protocol's internal routing
Long filament between two hubs = bridge route (funds moving CEX → DEX or chain → chain)
Isolated pair in the periphery = two individual wallets
Sudden fanout = airdrop distribution or drainer event

# For builder why blockhcain is the beauty to play around with data
every large data is gated behind companies databse but blockchain being public is a gift for builders and we shouldn't tear it down just to have privacy rather we should make public as a feature not a bug so showing in public will give accountability and since with wallets you are already private just your movement can be linked thats a tradeoff but thats is also solveable via liquidity pools to new wallet not a big deal. but still how much flowed that must be tracked and should be public.

# This could be genuinely beautiful and works with the architecture we've built. Let me paint what it'd look like:

**The picture.** The existing graph stays — hubs as stars, spokes as orbital tracks, bridges as intercluster filaments. On top, a continuous flow of **tiny bright pulses travels along the edges** — one pulse per confirmed transaction. Busy hubs swarm with incoming/outgoing pulses; quiet hubs sit dim. An airdrop shows as a firework burst from one node. A drainer shows as inbound pulses converging on one wallet from many sources at once. Instead of the graph "bubbling" every second from relayout, you see **actual bloodstream flow** — nothing relaying, just events traveling along the structure that already exists. The structural layout stays calm while the particle layer pulses. Night sky metaphor nails it: constellations (hubs) are stable, shooting stars (txs) move.

**How it'd work technically.** Backend already has each tx crossing the state machine as it applies. Add a second SSE channel — *not* snapshots, *tx events* — that emits `{from, to, amount, t}` for every tx the state machine ingests, at up to the ingest rate (~500/s). Frontend receives them, looks up the edge in the existing sigma graph, animates a bright dot from source node to target node along that edge over ~1.5s with fade-out. Cull on animation complete. Ignore tx events whose edge isn't currently rendered (non-hub pairs) — keeps the subgraph honesty.

**Cost.** Backend sidecar channel: ~hundred lines. Frontend particle system: sigma.js has a renderer plugin API; a custom "pulse layer" is a week of careful work (animation lifecycle, batching, WebGL performance at 500 concurrent pulses). Payload: ~50KB/s SSE, trivial.

**Fit with the product.** Very high. This is the "live feed as night sky" version of what we've been building — ambient, alive without being bubbly, and every pulse is a real event not an algorithm re-solving. It makes the graph watchable for long stretches. Bloomberg-ticker-grade differentiator. And it's how the SOC dashboards you inspire from (ATC radar, trading floors) actually feel.

**Risk.** One: bugs in pulse lifecycle (leaks, memory growth) can degrade performance silently. Two: layered visual complexity can cross into "too busy" if not tuned. Three: tx events for non-hub edges are invisible, which is fine but means the "firehose" impression is capped by hub subgraph density.

Worth committing to as the next arc after we stabilize what we have.

# Ask ai agent to learn about relationship between exchnages and wallets and hubs
giant hubs bridges etc.

# Worth considering a fix later — e.g., full-strength attraction when both endpoints are hubs.

**What the frontend shows today is 100% geometric**

Everything visible is one of:
- The graph canvas (Sigma rendering nodes and edges)
- Role swatches with counts in the sidebar (9 numbers)
- Total wallets / edges / lagged in the status panel
- Edge color legend (3 line samples)
- A paragraph of marketing copy

That's the full surface. Nine count integers and a paragraph. No tables, no rankings, no detail panels, no search, no time series, nothing clickable.

**What the data layer ALREADY computes but never surfaces**

Every detect tick (every 3 seconds) we compute:

- **Tip-style accounts**: top 8 by degree+dust signature, IDs, full bucket distribution by tips-touched, total unique searchers
- **Heavy MEV searchers**: full per-bot record (tips touched, in/out/bidir SOL, non-tip degree)
- **MPC communities**: flagged community IDs, looper share, intra-volume share, size, total volume per community
- **Top hubs**: by degree (10), by volume (10), with their full IDs and metrics
- **Cluster centrality diagnostics**: per-component biggest/runner-up degrees, top-vs-second ratios, distance from centroid
- **Per-component stats**: size, totalVolume, edgeCount, top members by degree and volume, role counts
- **Per-node roles**: classified label for every wallet
- **Per-mint activity**: count of edges per mint, kind distribution

All of this lives in `componentStatsRef`, `rolesRef`, `mintAddrsRef`, plus the console logs that dump it every 3 seconds. The user can see it only by opening DevTools and reading raw JSON output. That's not a surface — that's a diagnostic dump.

**The missing analytical surfaces**

In rough priority order based on what the data already supports:

1. **Top MPC communities table** — ranked by total volume, with member count, looper share, intra-volume share. Clickable to show members. We have all of this in `componentStatsRef` and the `[mpc]` log.

2. **Heavy MEV searchers leaderboard** — ranked by tips touched + non-tip degree. Already computed in the `[mev] heavy searchers` log. Just needs a table.

3. **Top mints leaderboard** — ranked by edges, broken down by transfer/mint/burn. Surfaces the pump.fun lifecycle and the rent-recovery patterns we discovered. Computable from the edge stream directly.

4. **Hub leaderboards** — top sol-hub, spl-hub, multi-hub with their degrees and connection patterns. Useful for finding DEX vaults, exchange wallets, cross-asset routers.

5. **Wallet profile (click-through)** — click any node, see its role, full address, in/out volume, neighbor list, component membership. Most table-stakes for any chain explorer; we have everything needed.

6. **Per-component summary** — list of the largest 20 components with their stats. Lets the user navigate the topology by zooming to specific components.

7. **Live tx-rate / activity timeline** — count of transactions, edges, mints, burns per minute over the last hour. Useful baseline for "is something unusual happening right now?"

**Performance considerations you mentioned**

After SPL capture, edge throughput jumped ~3-5x. Frame rate is still acceptable (haven't seen it tank), but we haven't stress-tested the layout at 50k+ wallets. The per-component layout is `O(N²)` for components ≤400 nodes, attraction `O(E)` for edges. Those scale linearly-ish for now.

Specific perf hotspots to look at:
- Detect tick walks the entire graph every 3s (forEachNode + classifier) — `O(N)` but the constant factor matters
- Louvain runs synchronously on the main thread (graphology-communities-louvain) — could be moved to a worker
- Component-stats walks every component's members — `O(N)` per detect
- Sigma rendering scales with visible nodes/edges

**Suggested starting point**

The "MPC explorer" surface (your original Pass 2 from the session summary) is probably the highest-leverage analytical addition because:

- The data is unique on the chain (most explorers don't surface MPC closed-loop detection)
- We already compute it every 3 seconds and just throw it on the floor
- The first-pass UI is just a sortable table — small lift
- It builds on the existing classifier, no new heuristics needed

Alternatively the "wallet profile page" (table stakes) is the user-facing one that completes the chain-explorer feel.

Or we could start with a unified "live ticker" panel that shows: top mints right now, top MPC communities right now, top heavy searchers right now — all in one sidebar, ranked, refreshed every 3s. Lowest-effort way to expose the data we're already computing.

Which direction feels right? My read: the live ticker first (cheap, exposes the data we've been talking about all session), then a wallet profile click-through, then the dedicated MPC explorer. But you might want to go straight to one of the more focused surfaces.