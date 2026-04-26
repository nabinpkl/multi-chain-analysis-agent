# Slice 1  Backend Graph Engine (Union-Find + Adjacency)

## Context

Frontend currently owns all graph compute in `frontend/src/hooks/use-raw-stream.ts` (770 lines): graphology mutate, Union-Find components, Louvain, role classifier, layout. Each browser tab redoes the same work over the same edge fire-hose. Backend just streams raw edges and forgets them.

We are inverting ownership in vertical slices. Slice 0 (ts-rs type contract) shipped. Slice 1 stands up the backend graph engine  node interner, adjacency, Union-Find  running parallel to the frontend with zero visible behavior change. Frontend keeps its own Union-Find and Sigma render exactly as today; backend computes the same components and exposes them on a new HTTP route for verification.

This unlocks slices 2–8: once components are computed backend-side and proven equivalent, the frontend stops doing it; once proven, we add roles, communities, layout the same way. Slice 1 is the load-bearing piece  it establishes the data structure that every later slice builds on.

**On restart**: consumer starts from `latest` Redpanda offset. No backfill, no replay, no dedupe needed. Graph rebuilds from new edges as they arrive. ClickHouse holds historical truth if we ever need to rebuild.

## Design

### Data structures

```rust
// backend/src/graph/interner.rs
type NodeIdx = u32;

struct NodeInterner {
    forward: FxHashMap<String, NodeIdx>,  // base58 pubkey → idx
    reverse: Vec<String>,                  // idx → base58 pubkey
}

// backend/src/graph/union_find.rs
struct UnionFind {
    parent: Vec<NodeIdx>,  // self-loop = root
    rank: Vec<u8>,
}

// backend/src/graph/state.rs
type EdgeIdx = u32;
type MintIdx = u32;

struct GraphEdge {
    src: NodeIdx,
    dst: NodeIdx,
    amount: u64,           // lamports for SOL, base units for SPL
    mint: Option<MintIdx>,
    slot: u64,
    kind: Option<EdgeKind>, // mirrors api::raw::EdgeKind
}

struct ComponentMeta {
    size: u32,
    edge_count: u64,
    first_seen_slot: u64,
    last_seen_slot: u64,
}

pub struct GraphState {
    interner: NodeInterner,
    mint_interner: NodeInterner,  // same struct, separate instance

    edges: Vec<GraphEdge>,             // by EdgeIdx
    out_adj: Vec<Vec<EdgeIdx>>,        // by NodeIdx
    in_adj:  Vec<Vec<EdgeIdx>>,        // by NodeIdx

    uf: UnionFind,
    components: FxHashMap<NodeIdx, ComponentMeta>,  // keyed by current root
}
```

Choices locked:
- **Adjacency = `Vec<Vec<EdgeIdx>>` indexed by NodeIdx**. Direct index (no hash), append O(1), grows in lockstep with interner. SmallVec deferred until profile shows allocator pressure.
- **Both `out_adj` and `in_adj`**. Slice 3 (roles) needs in vs out degree distinction; build now to avoid refactor.
- **Union-Find = roll own**, path compression + union by rank (`Vec<NodeIdx>` parent + `Vec<u8>` rank). `petgraph::unionfind` rejected (fixed-size at construction, not append-friendly).
- **Concurrency = `Arc<RwLock<GraphState>>`**. Single writer (consumer task), multiple readers (HTTP handlers). Actor pattern revisited in slice 7 when delta broadcast lands.
- **Eviction**: none. Pure additive in-memory state. Restart = clean slate from `latest` offset. Document trigger to revisit (memory pressure on Oracle VM).
- **Dedupe**: none. At-least-once delivery dups within a session are rare (consumer rebalance). UF idempotent on dup edges  components stay correct. Edge count over-counts on dup, acceptable v0.

### Module layout

```
backend/src/graph/
  mod.rs              GraphState struct + ingest(EdgeWire) → Vec<GraphDelta>
  interner.rs         NodeInterner (used for nodes + mints)
  union_find.rs       UnionFind, find/union/component_count
  delta.rs            GraphDelta enum (ts-rs), unused by routes in slice 1 but emitted by ingest()
  consumer.rs         Redpanda consumer task on solana.raw-edges with group "graph-engine", auto.offset.reset=latest
```

`GraphDelta` enum defined now even though slice 1 doesn't broadcast  keeps `ingest()` signature stable for slices 2+:

```rust
#[derive(Serialize, TS)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
enum GraphDelta {
    NodeAdded { idx: u32, pubkey: String },
    EdgeAdded { idx: u32, src: u32, dst: u32 },
    ComponentMerged { absorbed_root: u32, surviving_root: u32, new_size: u32 },
}
```

### HTTP routes

```
GET /graph/components?limit=5000&cursor=<NodeIdx>
  → { nodes: [{ pubkey: String, component_id: u32 }], next_cursor: Option<u32>,
      total_nodes: u32, total_components: u32 }

GET /graph/stats
  → { total_nodes: u32, total_edges: u32, total_components: u32,
      largest_component_size: u32, last_ingested_slot: Option<u64> }
```

Both response types derive `TS` and export to `frontend/src/lib/generated/`. Hard cap `limit ≤ 50000` per AGENTS.md rule 13. Cursor = last NodeIdx returned; interner gives natural insertion order.

### Wire-up to existing infra

- Reuse `stream::consumer::build_consumer(brokers, "graph-engine", topic, "latest")` from `backend/src/stream/consumer.rs:4`.
- New consumer task spawned in `main.rs` alongside `state-sink` and `ch-sink` (mirror pattern at `backend/src/main.rs:55-71`).
- Subscribe to `shutdown_rx: watch::Receiver<bool>` (pattern at `backend/src/main.rs:128-136`); on signal, drop locks, exit.
- Add `graph: Arc<RwLock<GraphState>>` field to `AppState` in `backend/src/state.rs:16`. Both new routes consume via `State(state): State<AppState>` (pattern at `backend/src/api/raw.rs:25`).
- Register routes in `backend/src/api/mod.rs:9-15` before `.with_state(state)`.

### Parallel-run verification

Frontend keeps its existing Union-Find. Verification = manual diff in dev:

1. Curl `GET /graph/components?limit=50000` after backend warms up.
2. In browser dev console, dump frontend's component map (`useRawStream` exposes via window for debug).
3. Diff: every node in both should map to the same equivalence class (ids may differ  match on partition, not label). Quick script: group-by component_id on each side, sort each group's nodes, compare set-of-sets.

If equivalence holds for several minutes of live data → slice 1 done, slice 2 unlocked.

## Files

**New**
- `backend/src/graph/mod.rs`
- `backend/src/graph/interner.rs`
- `backend/src/graph/union_find.rs`
- `backend/src/graph/delta.rs`
- `backend/src/graph/consumer.rs`
- `backend/src/api/components.rs`
- `backend/src/api/graph_stats.rs`

**Modified**
- `backend/src/main.rs`  declare `mod graph;`, build initial `GraphState`, spawn graph consumer task, add handle to bg_handles
- `backend/src/state.rs:16`  add `pub graph: Arc<RwLock<GraphState>>` to `AppState`
- `backend/src/api/mod.rs:1-15`  declare `pub mod components; pub mod graph_stats;` and add `.route("/graph/components", get(components::query))` + `.route("/graph/stats", get(graph_stats::stats))`
- `backend/src/config.rs:42`  add `KAFKA_GROUP_GRAPH` env (default `"graph-engine"`)

**Auto-generated (committed)**
- `frontend/src/lib/generated/GraphDelta.ts`
- `frontend/src/lib/generated/ComponentsResponse.ts`
- `frontend/src/lib/generated/GraphStatsResponse.ts`
- (any nested types ts-rs emits)

**Frontend**: zero changes. New routes exist but nothing consumes them yet. That is the whole point of slice 1.

## Reuse from existing code

- `backend/src/stream/consumer.rs:4`  `build_consumer(brokers, group_id, topic, auto_offset_reset)`. Pass `"latest"` for the new group.
- `backend/src/stream/topics.rs:24`  `Envelope` struct + JSON deserialization. Same payload shape as `state-sink` and `ch-sink` consume.
- `backend/src/api/raw.rs:43-49`  `EdgeKind` already derives `TS`. Reuse the type in `GraphEdge`/`GraphDelta` rather than redefining.
- `backend/src/sinks/state_sink.rs`  full template for `consumer.rs`: `tokio::select!` between `consumer.stream()`, periodic commit timer, and `shutdown_rx.changed()`. Manual offset commit on shutdown.
- `rustc-hash::FxHashMap` already in `Cargo.toml`  use for interner + components map.
- `parking_lot::RwLock` already in `Cargo.toml`  preferred over `std::sync::RwLock` (no poisoning, faster).

## Verification

1. **Compile**: `cargo check` from `backend/` clean.
2. **Type generation**: `cargo test export_bindings` from `backend/`. Confirm `frontend/src/lib/generated/GraphDelta.ts`, `ComponentsResponse.ts`, `GraphStatsResponse.ts` materialize.
3. **Frontend typecheck**: `pnpm exec tsc --noEmit` from `frontend/`. No errors (frontend doesn't import the new types yet, but they should still parse).
4. **Build**: `docker compose up -d --build` from repo root. All containers healthy per AGENTS.md rule.
5. **Smoke**: `curl http://localhost:8002/health` ok. `curl http://localhost:8002/graph/stats` returns shape with non-zero `total_nodes` after ~30s of live ingest.
6. **Pagination**: `curl 'http://localhost:8002/graph/components?limit=100'` returns 100 nodes + `next_cursor`. Next request with cursor returns next 100, no overlap.
7. **Equivalence (manual)**: load frontend, let it accumulate state ~2 min, dump frontend component map, fetch backend `/graph/components?limit=50000`, diff partitions. Same equivalence classes.
8. **Restart cleanliness**: `docker compose restart api`. New consumer joins with `latest` offset, `/graph/stats` resets to small numbers, grows again as new edges arrive. No replay flood.
9. **Shutdown**: `docker compose stop api`. Logs show graph consumer received shutdown signal, exited cleanly. No "task panicked" entries.

## Out of scope for slice 1 (explicit)

- Frontend code changes
- Delta broadcast (slice 2)
- Roles, communities, layout (slices 3, 5, 8)
- Eviction / windowing of in-memory state
- Restart-replay from ClickHouse
- Memory profiling / SmallVec
- Actor refactor of GraphState ownership
