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


Yes — and it's exactly the right frame. We backed into a solar system. Hubs are stars, spokes are planets/moons in orbit, hub-to-hub edges are binary star gravitational couplings, the canvas gravity is the galactic center.

What planetary physics gives us that Force-Atlas 2 doesn't:

- **Mass-weighted inertia**: in real solar systems, the sun barely moves when a comet swings by. Our hubs *should* feel immovable relative to their spokes. FA2 treats every node as equal mass → hubs drift. If we set `node.mass = degree` and bump repulsion scaling with mass, hubs become gravitational anchors.

- **Orbital stability instead of equilibrium-by-damping**: FA2 reaches equilibrium by dissipating energy (`slowDown`). Real orbits are stable because of angular momentum — they don't need damping to hold shape. That's why our graph "bubbles" — it has no angular memory, only damping. A custom orbital layout would remember each spoke's angle around its hub and preserve it across snapshots.

- **Attraction proportional to mass, not inverse**: my current `edgeWeight = 1 / log(hub_degree)` is *anti-planetary*. Real moons are strongly bound to heavy planets, not weakly. Reversing that (+ relying on mass-weighted repulsion to prevent spoke pileup) brings us closer.

- **Hierarchy**: stars host planets host moons. Our graph has hubs and their sub-hubs. A true hierarchical layout could place sub-hubs in orbital shells inside the primary hub's gravity well.

**What's the move?** Nothing urgent today. But when you come back to polish: replace FA2 with a custom orbital layout — sigma.js supports pluggable layouts. Roughly a week of work. Payoff is enormous: the visualization stops "bubbling" and starts "orbiting." Real stability, visually and structurally. You'd have built something no other blockchain viz does.