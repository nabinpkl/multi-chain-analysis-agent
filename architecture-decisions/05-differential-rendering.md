# Slice 2  Differential Rendering + Rolling Window + Event-Sourced Delta Protocol

## What we have today

**Confirmed: zero windowing exists.** Backend graph grows forever from process start. Frontend graph grows forever from tab open. Commit `3969982` added 24h temporal ring → reverted in `fe3d647` along with the state machine. Clean slate.

So we build window + delta protocol from nothing. This is bigger than original "slice 2"  it subsumes what I called slice 7. Doing it now is correct: every later slice (roles, communities, layout) plugs into this protocol as additional `GraphDelta` variants. Build the bus once.

## Core architecture

**Event sourcing with rolling window.** Backend is the single source of truth. Every state mutation emits a typed `GraphDelta` with monotonic `seq: u64`. Clients are pure reducers  apply deltas in seq order, hold identical state.

Three mutation sources, all backed by the same delta channel:
1. **Live ingest** (Redpanda consumer) → `EdgeAdded`, `NodeAdded`, `ComponentMerged`
2. **Window expiry** (background tick) → `EdgeExpired`, `NodeExpired`, `ComponentReset`
3. **Cold-start replay** (per-connection synthetic) → reuses same delta types

## Cold start without snapshot dump

The clean trick: **serialize current state as a stream of synthetic deltas, send over the same SSE channel.** Bytes are the same as a JSON blob, but:
- Wire chunks small (one event per chunk, ~80 bytes)
- Browser parses + applies incrementally as bytes arrive
- Same client code path as live (one reducer, no "snapshot" parser)
- TCP backpressure regulates the firehose
- User sees graph emerge, not blank-then-pop

```
client connects → backend (under graph read lock):
  for each interned node: emit NodeAdded { seq, idx, pubkey }
  for each edge in window: emit EdgeAdded { seq, idx, src_idx, dst_idx, ... }
  for each component: emit ComponentSet { seq, root_idx, members: [idx, ...] }
  emit CaughtUp { seq: latest_at_lock_release }
  release lock
  switch connection to live broadcast tail starting from latest_at_lock_release+1
```

Wire size for 1hr at 100 edges/sec ≈ 360k events. With u32 NodeIdx in EdgeAdded (not base58), each event ~30B → ~10MB. Gzip → ~3MB. Streamed, not blobbed. Acceptable.

## Reconnect resume

SSE has built-in `Last-Event-ID` header. Backend keeps a ring of recent deltas (last 60s, ~6k events at 100/s):

```
on connect with Last-Event-ID = N:
  if ring contains seq N+1: replay ring [N+1..latest], then live tail
  else: cold-start (state serialize)
```

Cheap reconnect after brief network blip → kilobytes. Long disconnect → cold-start.

## Window expiry  the hard part

Edge expiry needs to undo state. Specifically:
- Remove from `edges` Vec (or tombstone  see below)
- Remove from `out_adj[src]` and `in_adj[dst]`
- Decrement component meta
- **Possible component split**: UF cannot un-union. Two adjacent components held together only by an expired edge → split silently broken without intervention.

**Solution: periodic component rebuild after expiry.**

Every expiry tick (e.g. every 30s):
1. Compute set of edges to drop (block_time < now - 1h)
2. Compute set of nodes that become orphan (no remaining edges)
3. Drop them from `edges`, `out_adj`, `in_adj`, interner, UF
4. **Rebuild components from scratch via BFS over remaining edges.** O(V+E). For 50k nodes / 360k edges ≈ 50ms. Fine for 30s cadence.
5. Diff new component map vs old → emit minimal delta set:
   - `EdgeExpired { idx }` for each dropped edge
   - `NodeExpired { idx }` for each dropped node
   - `ComponentReset { mappings: [(node_idx, new_component_id)] }` for nodes whose component changed

**Component ID stability**: keep component_id space monotonic (separate counter, not root NodeIdx) so the IDs are stable identifiers that persist across rebuilds when membership doesn't change. This means we don't use UF root as component_id externally  only internally for fast live unions.

```rust
struct GraphState {
    // ... slice 1 fields ...
    component_id_for_node: Vec<ComponentId>,  // by NodeIdx, stable across rebuilds
    next_component_id: u32,
}
```

On UF union: bump component_id_for_node to surviving_root's component. On rebuild: assign new component_ids to truly-new partitions, reuse for unchanged.

## Tombstones vs hard delete

Two choices for `edges` Vec on expiry:

**A. Hard delete with reindexing**: shift all later EdgeIdx down. Breaks every reference. Reject.

**B. Tombstone (replace with `Option<GraphEdge>`)**: keep slot, mark None. Live consumers and queries skip None. EdgeIdx stable. Memory grows unbounded though  need periodic compaction.

**C. Generation-based**: each edge has `generation: u32`, expired = bumped to invalid generation. Cheaper than Option but same idea.

Pick **B** for clarity. Add background compaction every hour: rebuild Vec, remap idx, broadcast `Reindex { mappings }`. Defer compaction to when memory pressure shows  early version just leaks tombstones.

## GraphDelta enum (final shape for slice 2)

```rust
#[derive(Serialize, TS)]
#[serde(tag = "type")]
pub enum GraphDelta {
    NodeAdded     { seq: u64, idx: u32, pubkey: String },
    EdgeAdded     { seq: u64, idx: u32, src: u32, dst: u32, mint: Option<String>,
                    amount: u64, slot: u64, kind: Option<EdgeKind> },
    ComponentSet  { seq: u64, node: u32, component_id: u32 },
    EdgeExpired   { seq: u64, idx: u32 },
    NodeExpired   { seq: u64, idx: u32 },
    CaughtUp      { seq: u64 },
}
```

`ComponentMerged` removed  replaced by per-node `ComponentSet`. Cleaner: client just updates one map entry per event. Backend emits one `ComponentSet` for each node whose component_id changed (including all members of absorbed component on union).

For union of small + large component: emit ComponentSet for nodes in smaller side (typically few). Cost = size of smaller component per merge.

## Event seq durability

Seq is in-memory u64 counter, increments on every emitted delta. **Resets to 0 on backend restart.** Reconnects with stale seq from previous backend instance → ring lookup fails → cold-start. Correct behavior. Document explicitly so frontend doesn't try to be clever.

## Frontend changes

**New** `frontend/src/hooks/use-graph-stream.ts`:
- EventSource on `/graph/stream`, automatic Last-Event-ID resume (browser does this for free)
- Reducer maintains: graphology graph + `Map<NodeIdx, pubkey>` (for EdgeAdded → graph mutation by pubkey)
- Apply each delta type
- Expose graph instance + `caughtUp: boolean` flag for first-paint gating
- Replace use-raw-stream entirely

**Deleted**:
- `frontend/src/lib/components.ts` (Union-Find)
- `frontend/src/hooks/use-raw-stream.ts` (770-line god hook)
- Per-component layout, role detect, mpc detect  wait, those depend on slice 3+. Keep them for now, just rewire input source to graphology + componentMap.

Actually  hook split is the right move. `use-graph-stream` = pure protocol reducer. Roles/communities/layout layers consume the graph via separate hooks. Slice 2 builds the protocol, leaves existing detection logic running on top. Clean lift-and-shift, then slices 3-6 each delete one detection module as backend takes over.

## Backend module changes

**New**:
- `backend/src/graph/window.rs`  expiry tick, BFS component rebuild
- `backend/src/graph/event_log.rs`  seq counter + ring buffer of last 60s deltas
- `backend/src/api/graph_stream.rs`  SSE handler with Last-Event-ID

**Modified**:
- `backend/src/graph/mod.rs`  `ingest()` returns Vec<GraphDelta> already, add seq tagging via shared event_log
- `backend/src/state.rs`  replace `raw_tx: broadcast::Sender<Arc<Edge>>` with `delta_tx: broadcast::Sender<GraphDelta>`. Or keep both temporarily? Pick: replace (no backcompat shim per AGENTS.md).
- `backend/src/api/raw.rs`  **delete entirely**. `/graph/raw/stream` becomes obsolete.
- `backend/src/api/mod.rs`  drop raw stream route, add `/graph/stream`
- `backend/src/main.rs`  start window expiry background task

**Deleted**: `/graph/components` and `/graph/stats` HTTP routes from slice 1? Keep for now  useful for ops/debugging. Or delete since nothing consumes them. **Delete** per AGENTS.md no-dead-code (frontend will use SSE state, not poll).

Actually `/graph/stats` useful for /health-style observability. Keep it. Delete `/graph/components`.

## Locked decisions

1. **Window cutoff = `block_time` (chain time), not system clock.** Cutoff is `latest_block_time - 1h`, where `latest_block_time` is the max `block_time` seen so far in the ingester. Reasons:
   - Chain backlog: if ingester falls behind tip by 10 minutes, wall-clock cutoff would expire edges that are still "fresh" relative to data we've actually processed. Block-time cutoff stays consistent with data flow.
   - Clock drift: Oracle VM clock vs validator clock vs client clock all differ. Anchoring to block_time means all clients see the same cutoff regardless of their local time.
   - Replay determinism: rebuilding from ClickHouse is reproducible (block_time is immutable per slot); wall-clock isn't.
   - Stalled ingest: if RPC drops out for 5min, no new edges arrive, `latest_block_time` doesn't advance, nothing expires. Window freezes. Correct behavior  we don't fabricate "now."
   - Implementation: track `latest_block_time: AtomicU64` updated on every ingest. Expiry task reads it, computes `cutoff = latest_block_time - 3600`, drops edges with `block_time < cutoff`.

## Key open decisions to lock before coding

1. **Expiry tick interval**: every 30s? every 5min? Tradeoff = lag between true cutoff and observed cutoff vs CPU/broadcast volume. Pick 30s.

2. **Component rebuild trigger**: every expiry tick unconditionally, or only when at least one edge expired? Pick: only when something expired.

3. **Bootstrap event order**: nodes-first then edges (referential integrity), or interleaved by insertion order (matches live semantics)? → nodes-first then edges. Simpler reducer. Client tolerates it.

4. **Cold-start delta seq**: synthetic events get the same seq value (snapshot of latest at lock acquire), or each gets unique seq? → all share `seq = bootstrap_seq`, with `CaughtUp { seq: bootstrap_seq }` after. Reconnects with id `bootstrap_seq` resume cleanly. Saves 360k seq increments per connection.

   Wait: doesn't work. If all bootstrap events share one seq, Last-Event-ID can't distinguish "got first 1000 bootstrap events" from "got all". Browser sets Last-Event-ID = id of LAST event received. If all bootstrap events have id=N, partial bootstrap → reconnect with Last-Event-ID=N → backend thinks fully caught up.

   Fix: bootstrap events DON'T set `id:` field in SSE format. Only live events have id. Browser's Last-Event-ID stays at 0 during bootstrap, gets set to first live event id after CaughtUp. Reconnect during bootstrap → id=0 → re-bootstrap. Reconnect after CaughtUp → id=N → ring resume.

5. **Multi-tab** (same client multiple SSE connections): each does its own cold-start. Backend handles N concurrent bootstraps under read lock  bootstrap iteration holds read lock for ~hundreds of ms per connection. Fine for solo dev. At scale, snapshot once + replay. Defer.

## Risk register

1. **Cold-start under read lock blocks ingest**: bootstrap iterates state under read lock. Live writers need write lock. If bootstrap is slow (10MB stream over slow client connection), writer starves. Fix: snapshot state into a `Arc<StateSnapshot>` cheaply (clone refs), release read lock, iterate snapshot off-lock. Adds ~10MB transient memory per cold-start. Acceptable.

2. **Component rebuild on expiry stalls live ingest**: rebuild needs write lock. 50ms of stall every 30s. Acceptable. If becomes problem, do BFS off-lock then swap.

3. **Multi-client component_id divergence**: if expiry happens between two clients' cold-starts, they see different snapshots. Live deltas reconcile within seconds. Document as eventually consistent.

4. **Browser SSE reconnect timing**: EventSource auto-reconnects after ~3s. Ring buffer at 60s easily covers brief blips. Long disconnects fall to cold-start.

5. **Frontend memory on long sessions**: graphology + delta history. Without window enforcement on frontend, memory grows. Frontend must ALSO honor `EdgeExpired` / `NodeExpired` and drop nodes/edges. Verify in implementation.

## Verification

1. Open one tab, let graph populate from cold-start.
2. Open second tab  should see identical graph after its cold-start completes (same nodes, edges, components  modulo IDs which are deterministic).
3. Kill backend, restart. Tabs reconnect → Last-Event-ID stale → fresh cold-start. Identical state across tabs.
4. Wait > 1h. Confirm oldest edges drop, components recompute, frontend reflects.
5. Network throttle (Chrome devtools → Slow 3G). Cold-start should still succeed, just slow.
6. Disconnect network briefly (5s). Reconnect via ring buffer, no state divergence.
7. Disconnect 2min. Falls to cold-start. State converges.
8. `docker compose stop api` mid-session → frontend SSE error, retries, eventually cold-starts when backend back.

## Files touched (concrete)

**Backend new**: `graph/window.rs`, `graph/event_log.rs`, `graph/bootstrap.rs`, `api/graph_stream.rs`

**Backend modified**: `graph/mod.rs` (seq tagging on ingest), `graph/delta.rs` (new variants), `state.rs` (delta_tx replaces raw_tx), `main.rs` (start window task), `api/mod.rs` (route swap)

**Backend deleted**: `api/raw.rs`, `api/components.rs` (slice 1 polling endpoint, replaced)

**Frontend new**: `hooks/use-graph-stream.ts`

**Frontend deleted**: `hooks/use-raw-stream.ts`, `lib/components.ts`, `lib/api.ts`'s `subscribeRawStream`

**Frontend modified**: `lib/component-stats.ts` (read graphology), `lib/per-component-layout.ts` (read graphology), `lib/role-detect.ts` (still runs frontend-side until slice 3), `lib/mpc-detect.ts` (still runs frontend-side until slice 5), `app/page.tsx` (swap hook)

**ts-rs auto-generated**: full GraphDelta enum exports

## Scope reality check

This is roughly **2-3× the work of slice 1**. Distinct subsystems:
- Window expiry + component rebuild (backend)
- Event log + seq + ring (backend)
- SSE bootstrap-then-tail handler (backend)
- Reducer hook (frontend)
- Expiry application on frontend
- Wholesale replacement of existing fire-hose path

If we want to ship in iterations:

**Slice 2a**: backend rolling window + expiry + delta channel. Replace `raw_tx` with `delta_tx`. Frontend adapter shim translates new deltas to old EdgeWire shape so use-raw-stream keeps working unchanged. Ship + verify backend correctness.

**Slice 2b**: frontend reducer hook, replaces use-raw-stream. Delete shim. Delete frontend UF + components.ts.

Two PRs, each verifiable. Recommended.

**Or** do it as one slice if you want the architecture pure first time. Up to you. I'd recommend 2a/2b split.

Want me to dispatch this as 2a first, or as one combined slice?