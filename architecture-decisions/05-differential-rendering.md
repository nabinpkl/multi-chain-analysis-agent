# Slice 2  Differential Rendering + Rolling Window + Event-Sourced Delta Protocol

## What we have today

**Confirmed: zero windowing exists.** Backend graph grows forever from process start. Frontend graph grows forever from tab open. Commit `3969982` added 24h temporal ring → reverted in `fe3d647` along with the state machine. Clean slate.

So we build window + delta protocol from nothing. This is bigger than original "slice 2"  it subsumes what I called slice 7. Doing it now is correct: every later slice (roles, communities, layout) plugs into this protocol as additional `GraphDelta` variants. Build the bus once.

## Core architecture

**Event sourcing with rolling window.** Backend is the single source of truth. Every state mutation emits a typed `GraphDelta` with monotonic `seq: u64`. Clients are pure reducers  apply deltas in seq order, hold identical state.

Two mutation sources, both backed by the same delta channel:
1. **Live ingest** (Redpanda consumer) → emits `NodeAdded`, `EdgeAdded`, `ComponentAssigned`, AND `EdgeExpired` / `NodeExpired` / `ComponentAssigned` for any state aged out by the cutoff advance this ingest call caused. One ingest = one atomic batch of deltas.
2. **Cold-start replay** (per-connection synthetic) → reuses same delta types.

No background tick. Expiry is a side effect of ingest, not its own event source. With block-time-anchored cutoff (locked decision #1), `latest_block_time` only advances when an edge ingests, so `cutoff = latest_block_time - 3600` only advances when an edge ingests  making any "expiry tick" purely redundant with the ingest call itself.

## Cold start without snapshot dump

The clean trick: **serialize current state as a stream of synthetic deltas, send over the same SSE channel.** Bytes are the same as a JSON blob, but:
- Wire chunks small (one event per chunk, ~80 bytes)
- Browser parses + applies incrementally as bytes arrive
- Same client code path as live (one reducer, no "snapshot" parser)
- TCP backpressure regulates the firehose
- User sees graph emerge, not blank-then-pop

```
client connects → backend (under graph read lock):
  for each interned (live) node:  emit NodeAdded { idx, pubkey }            (no seq)
  for each edge in window:         emit EdgeAdded { idx, src, dst, ... }      (no seq)
  for each node:                   emit ComponentAssigned { node, component_id } (no seq)
  emit CaughtUp { seq: live_seq_at_release }
  release lock
  forward live broadcast tail starting from live_seq_at_release + 1
```

Wire size for 1hr at 100 edges/sec ≈ 360k events. With u32 NodeIdx in EdgeAdded (not base58), each event ~30B → ~10MB. Gzip → ~3MB. Streamed, not blobbed. Acceptable.

## Reconnect  always cold-start

Browsers reconnect EventSource automatically (~3s retry). Two options:
- **Ring buffer + `Last-Event-ID` resume** (~60s of deltas in memory, replay on reconnect). Saves bandwidth on brief blips. Adds reducer ordering invariants, ring management, seq durability complexity.
- **Always cold-start on reconnect.** Reducer just resets, replays bootstrap, joins live. Cold-start cost ≈ 3MB gzipped over WiFi ≈ few hundred ms.

**Pick always-cold-start for slice 2.** Cheaper to implement, smaller surface, identical correctness. The "saves a few MB on a 5-second network blip" argument is a real future concern but a v0 over-engineering trap. Add ring buffer + Last-Event-ID later if monitoring shows reconnect frequency × cold-start cost is meaningful.

`seq: u64` stays on live deltas anyway  cheap (8 bytes), useful for client log replay debugging and deterministic test fixtures, and the ring buffer can drop in later without protocol change.

## Window expiry  inline in ingest, no tick

Edge expiry needs to undo state. Specifically:
- Tombstone the slot in `edges` Vec (set to `None`)
- Remove `EdgeIdx` from `out_adj[src]` and `in_adj[dst]`
- Decrement component meta
- **Possible component split**: UF cannot un-union. Two sub-components held together only by an expired edge → split silently broken without intervention.

**Algorithm  every state mutation flows through `ingest()`:**

```rust
fn ingest(&mut self, edge: &Edge) -> Vec<GraphDelta> {
    let mut deltas = vec![];

    // 1. Advance cutoff (block-time anchored, locked decision #1)
    self.latest_block_time = self.latest_block_time.max(edge.block_time);
    let cutoff = self.latest_block_time.saturating_sub(3600);

    // 2. Drain expired edges from time-sorted index
    let mut dirty_components: FxHashSet<ComponentId> = default();
    while let Some(&front_idx) = self.edges_by_time.front() {
        let e = self.edges[front_idx as usize].as_ref().unwrap();
        if e.block_time >= cutoff { break; }
        dirty_components.insert(self.component_id_for_node[e.src as usize]);
        deltas.extend(self.tombstone_edge(front_idx));  // emits EdgeExpired (+ NodeExpired if orphan)
        self.edges_by_time.pop_front();
    }

    // 3. Add new edge (existing slice 1 logic)  may emit NodeAdded × {0,1,2} + EdgeAdded + ComponentAssigned on union
    deltas.extend(self.add_edge(edge));

    // 4. Settle splits for dirty components (affected-component-only BFS, parallel via rayon)
    deltas.extend(self.settle_components(dirty_components));

    deltas
}
```

One ingest call → one atomic batch of deltas → one contiguous range of seq numbers → one broadcast send. No tick. No background task. Pure event-driven.

**Affected-component-only BFS, parallelized across dirty components.** DSU is fundamentally non-decremental (no `un-union`), so split detection requires re-traversal. Industry options considered:

- Full BFS rebuild every tick (O(V+E))  what most batch systems do (Flink, GraphX). Wasted work when most components untouched. Reject as default.
- HLT/HDT decremental connectivity (O(log² n) per delete)  research-grade, ~thousands of LOC, bug-prone. Reject.
- Link-cut / Euler-tour trees  same. Reject.
- **Affected-component-only BFS**: track which component each expired edge sat in. Only those components are dirty. Untouched components stay unchanged, no work. Components are disjoint by definition → BFS each via `rayon::par_iter`. Serial within a component (parallel BFS overhead beats gain for ~10k-node components), parallel across components. **Pick this.**
- Spanning-forest non-tree-edge skip (Holm-Thorup level-structure prefix)  refinement. Most expired edges in dense graphs are non-tree → skip BFS entirely for those. Adds spanning-tree maintenance per insert. Defer until profiling shows giant-component split-BFS dominates.

For each dirty component, BFS partitions its node set. If `partitions.len() == 1` → no split, keep the component_id, no deltas. Else → largest partition keeps the old id, each remaining partition gets a fresh id from `next_component_id`, emit `ComponentAssigned` delta per reassigned node.

Solana caveat: traffic collapses to one giant component holding 80-95% of nodes. Any expiry inside the giant marks it dirty → giant BFS ≈ full rebuild for that ingest call. Worst-case ingest stall ~50ms when this happens. Mitigation = the deferred spanning-forest refinement. Document trigger to revisit.

**Component ID stability**: keep component_id space monotonic (separate counter, not root NodeIdx) so the IDs are stable identifiers that persist across splits/merges when membership doesn't change. This means we don't use UF root as component_id externally  only internally for fast live unions.

```rust
type ComponentId = u64;  // monotonic, never reused  u64 to avoid wrap (~8 months at worst-case alloc rate with u32)

struct GraphState {
    // ... slice 1 fields (interner, edges, out_adj, in_adj, uf) ...

    // Membership: which component each node currently belongs to. Dense, indexed by NodeIdx.
    node_to_component: Vec<ComponentId>,

    // Allocator: hands out fresh ComponentId values, never reused.
    component_id_seq: ComponentId,
}

impl GraphState {
    fn alloc_component_id(&mut self) -> ComponentId {
        let id = self.component_id_seq;
        self.component_id_seq += 1;
        id
    }
}
```

On UF union: rewrite `node_to_component` for nodes on the smaller side to surviving component's id. On split detection: largest partition keeps its old id, each remaining partition calls `alloc_component_id()`.

**No `component_meta` map for slice 2.** Aggregate stats (total_components, largest_component_size) for `/graph/stats` are derived on demand via single pass over `node_to_component`. O(N), cheap at our scale. Add a memoized cache or per-component meta map only if profiling shows the stats endpoint is hot. Keeps slice 2 invariants minimal.

## Edge slab  `Vec<Option<GraphEdge>>` with free-list reuse

`edges` is a slab allocator: `Vec<Option<GraphEdge>>` paired with `free_edge_slots: Vec<EdgeIdx>`. New edges fill freed slots first, only grow the Vec when the free-list is empty. Equivalent to "HashMap with integer keys" but strictly better  dense keys we control mean direct array indexing wins on every axis (insert, delete, lookup, iteration, memory, cache locality) over a real `FxHashMap<EdgeIdx, GraphEdge>`. Slab = manual hashmap with hash function = identity.

```rust
struct GraphState {
    edges: Vec<Option<GraphEdge>>,
    free_edge_slots: Vec<EdgeIdx>,
    // ...
}

fn alloc_edge_slot(&mut self, edge: GraphEdge) -> EdgeIdx {
    if let Some(idx) = self.free_edge_slots.pop() {
        self.edges[idx as usize] = Some(edge);
        return idx;
    }
    let idx = self.edges.len() as EdgeIdx;
    self.edges.push(Some(edge));
    idx
}

fn free_edge_slot(&mut self, idx: EdgeIdx) {
    self.edges[idx as usize] = None;
    self.free_edge_slots.push(idx);
}
```

Properties:
- **Memory bounded** at peak concurrent edges in window (~360k for 1hr at 100/s). No leak, no compaction task, no `Reindex` broadcast event.
- **EdgeIdx stable while edge lives at that slot.** Reuse only after free, and SSE preserves event order so any client sees `EdgeExpired { idx: N }` before any later `EdgeAdded { idx: N }`. Reducer applies in order  drops then adds. No staleness.
- Same allocator pattern as `component_id_seq`. Standard slab.

Alternatives rejected:
- `Vec::remove` (shifts everything)  invalidates all `EdgeIdx ≥ deleted`. Walking every adjacency to renumber = O(E) per delete. **Reject.**
- `Vec::swap_remove`  only the moved edge's idx changes, but external holders (SSE clients) get out-of-sync without a `Reindex` event. Adds protocol complexity for no win over slab. **Reject.**
- `FxHashMap<EdgeIdx, GraphEdge>`  same big-O, worse constants, worse cache, worse memory, no insertion-idx control. **Reject.**
- Tombstone without free-list  leaks tombstones forever in a long-running process. **Reject.**
- Generation counter per slot  solves a different problem (ABA on stale handles). We already get freshness via SSE event ordering. **Reject as overkill.**

Apply same slab pattern to `interner` (NodeIdx free-list) when nodes orphan and get expired. Mirror structure.

## GraphDelta enum (final shape for slice 2)

```rust
#[derive(Serialize, TS)]
#[serde(tag = "type")]
pub enum GraphDelta {
    NodeAdded     { seq: u64, idx: u32, pubkey: String },
    EdgeAdded     { seq: u64, idx: u32, src: u32, dst: u32, mint: Option<String>,
                    amount: u64, slot: u64, kind: Option<EdgeKind> },
    ComponentAssigned  { seq: u64, node: u32, component_id: u64 },
    EdgeExpired   { seq: u64, idx: u32 },
    NodeExpired   { seq: u64, idx: u32 },
    CaughtUp      { seq: u64 },
}
```

`ComponentMerged` removed  replaced by per-node `ComponentAssigned`. Cleaner: client just updates one map entry per event. Backend emits one `ComponentAssigned` for each node whose component_id changed (including all members of absorbed component on union).

For union of small + large component: emit ComponentAssigned for nodes in smaller side (typically few). Cost = size of smaller component per merge.

## Event seq durability

`seq` is an in-memory `u64` counter on `GraphState`, increments on every emitted delta. **Resets to 0 on backend restart.** Frontend doesn't act on `seq` directly in slice 2 (always cold-starts on reconnect); the field is on the wire for client log replay debugging and to keep the door open for a ring buffer + Last-Event-ID resume in a later slice without a protocol break.

## Frontend changes

`use-graph-stream` = pure protocol reducer. Roles/communities/layout layers consume the graph via separate hooks. Slice 2 builds the protocol; existing detection logic (role-detect, mpc-detect, per-component-layout) keeps running on top of the new state source. Slices 3-6 each delete one detection module as backend takes over.

**New** `frontend/src/hooks/use-graph-stream.ts`:
- EventSource on `/graph/stream`. On error / close: full reconnect → fresh cold-start (no `Last-Event-ID` resume in slice 2).
- State maintained:
  - graphology `Graph` instance (the source of truth for renderers)
  - `idxToPubkey: Map<NodeIdx, string>` (resolves EdgeAdded src/dst integers to pubkeys for graphology)
  - `nodeToComponent: Map<NodeIdx, ComponentId>` (per-node component membership)
  - `caughtUp: boolean` flag for first-paint gating
- Explicit reducer cases:
  - `NodeAdded { idx, pubkey }` → `idxToPubkey.set(idx, pubkey)`; `graph.addNode(pubkey)`
  - `EdgeAdded { idx, src, dst, ... }` → resolve via `idxToPubkey`; `graph.addEdgeWithKey(idx, srcPubkey, dstPubkey, attrs)`
  - `ComponentAssigned { node, component_id }` → `nodeToComponent.set(node, component_id)`; mark node attribute dirty for re-color
  - `EdgeExpired { idx }` → `graph.dropEdge(idx)` (idx is the SSE-stable EdgeIdx used as graphology edge key)
  - `NodeExpired { idx }` → resolve via `idxToPubkey`; `graph.dropNode(pubkey)`; `idxToPubkey.delete(idx)`; `nodeToComponent.delete(idx)`
  - `CaughtUp { seq }` → `caughtUp = true`; render gate releases
- Replace use-raw-stream entirely.

**Deleted**:
- `frontend/src/lib/components.ts` (Union-Find  backend owns connectivity now)
- `frontend/src/hooks/use-raw-stream.ts` (770-line god hook)

## Backend module changes

**New**:
- `backend/src/graph/expiry.rs`  `tombstone_edge`, `settle_components` (rayon parallel BFS across dirty components), `edges_by_time` VecDeque maintenance, NodeIdx free-list on orphan
- `backend/src/graph/bootstrap.rs`  state-to-events serialization for cold-start
- `backend/src/api/graph_stream.rs`  SSE handler (no Last-Event-ID; cold-start on every connect)

**Modified**:
- `backend/src/graph/mod.rs`  `ingest()` extended: cutoff advance + drain expired + add edge + settle splits, all in one call. Returns Vec<GraphDelta>. Tag each delta with monotonic seq from a `seq_counter: u64` field on GraphState.
- `backend/src/graph/delta.rs`  replace slice 1 enum with the slice 2 variants.
- `backend/src/state.rs`  replace `raw_tx: broadcast::Sender<Arc<Edge>>` with `delta_tx: broadcast::Sender<GraphDelta>`. No backcompat shim per AGENTS.md.
- `backend/src/api/raw.rs`  **delete entirely**. `/graph/raw/stream` obsolete.
- `backend/src/api/mod.rs`  drop raw stream route, add `/graph/stream`. Drop `/graph/components` route. Keep `/graph/stats` for ops/health observability (cheap, derived on demand).
- `backend/src/api/components.rs`  **delete** (slice 1 polling endpoint, replaced by SSE).
- `backend/src/main.rs`  no new background task. Existing graph consumer task drives all expiry inline via ingest.
- `backend/Cargo.toml`  add `rayon` for parallel BFS across dirty components.

## Locked decisions

1. **Window cutoff = `block_time` (chain time), not system clock.** Cutoff is `latest_block_time - 1h`, where `latest_block_time` is the max `block_time` seen so far in the ingester. Reasons:
   - Chain backlog: if ingester falls behind tip by 10 minutes, wall-clock cutoff would expire edges that are still "fresh" relative to data we've actually processed. Block-time cutoff stays consistent with data flow.
   - Clock drift: Oracle VM clock vs validator clock vs client clock all differ. Anchoring to block_time means all clients see the same cutoff regardless of their local time.
   - Replay determinism: rebuilding from ClickHouse is reproducible (block_time is immutable per slot); wall-clock isn't.
   - Stalled ingest: if RPC drops out for 5min, no new edges arrive, `latest_block_time` doesn't advance, nothing expires. Window freezes. Correct behavior  we don't fabricate "now."
   - Implementation: track `latest_block_time: u64` on `GraphState`. `ingest()` advances it, computes cutoff, drains expired before adding new edge.

2. **Expiry is a side effect of `ingest()`. No background tick.** Because cutoff is anchored to `latest_block_time`, and `latest_block_time` only advances when an edge ingests, every expiry opportunity coincides with an ingest call. Periodic tick would be redundant. One ingest = atomic batch of (expiry deltas + add deltas + settle deltas). Single event source for the whole system.

3. **Component split detection = affected-component-only BFS, parallel via rayon across dirty components.** Track which components lost an edge this ingest, BFS each via `rayon::par_iter`. Untouched components do zero work. Serial within a component, parallel across. Spanning-forest non-tree-edge skip refinement deferred until profiling shows giant-component split-BFS dominates p99 ingest latency.

4. **Component IDs are stable identifiers from a monotonic `u64` counter.** Not UF root NodeIdx. UF root is internal; ComponentId is the external stable handle that survives splits/merges when membership doesn't change. Backed by `node_to_component: Vec<ComponentId>` indexed by NodeIdx, plus `component_id_seq: ComponentId` allocator. **Type is `u64` (not `u32`)** because ComponentId is the only ID in the system that grows monotonically without slab reuse  at worst-case alloc rate (~200/sec) `u32` wraps in ~8 months of continuous uptime, well inside the lifetime of a long-running portfolio service. `u64` makes wrap unreachable (~2.9B years). Memory cost: +4 bytes per node in `node_to_component` (~400KB at 100k nodes), trivial. Other IDs (NodeIdx, EdgeIdx, MintIdx) stay `u32` because slab allocators bound them at peak concurrent count.

5. **Edge slab = `Vec<Option<GraphEdge>>` + `free_edge_slots: Vec<EdgeIdx>` free-list reuse.** New edges fill freed slots first, only grow the Vec when free-list is empty. EdgeIdx stable while edge lives. Memory bounded at peak concurrent edges in window (~360k for 1hr at 100/s). No leak, no compaction task, no `Reindex` broadcast event. Same allocator pattern as `component_id_seq`. Same slab pattern applies to `interner` (NodeIdx free-list) on orphan-node expiry.

6. **Reconnect = always cold-start. No ring buffer, no `Last-Event-ID` resume in slice 2.** Cold-start cost ≈ 3MB gzipped ≈ few hundred ms over WiFi. Cheaper than the ring/seq durability complexity. `seq: u64` stays on live deltas (cheap, useful for log replay) so a ring can drop in later without protocol change.

7. **On UF union, identifying "smaller side" cheaply**: each UF root carries metadata `{ component_id, size }` colocated with parent/rank in the UF struct. On `union(a, b)`: compare `size[root_a]` vs `size[root_b]`, smaller absorbed into larger, smaller side's nodes adopt larger's `component_id`. Enumerating "all nodes in a component" requires either a reverse index (Vec per root, expensive maintenance) or O(N) scan over `node_to_component`. **Pick: O(N) scan**. Profile if hot. Reverse index added later only if union/split BFS dominates.

## Open decisions

(none  all blocking decisions locked above)

## Notes / deferred concerns

- **Multi-tab concurrent bootstraps**: each client connection does its own cold-start under read lock. For solo dev and a portfolio piece with maybe 1-3 concurrent viewers this is fine  read locks don't block other reads, only writes, and write rate is bounded by ingest (~5-100 RPC/sec). At scale, share one bootstrap stream across pending connections. Defer until concurrent SSE traffic is real.

## Risk register

1. **Cold-start under read lock briefly slows ingest**: bootstrap iterates state under read lock for the duration of the cold-stream send. RwLock allows concurrent reads, only writes wait. Write rate is bounded by RPC ingest (~5-100/sec). For solo VM with 1-3 viewers and bootstrap ~few hundred ms, the impact is at most a few delayed ingests per cold-start. Accept. Mitigation if it ever shows up: snapshot state into an `Arc<StateSnapshot>` (cheap clone of refs), release lock, iterate off-lock  defer until measured.

2. **Giant-component split-BFS stalls ingest**: when an edge expiring inside the giant component (80-95% of nodes on Solana) triggers split detection, that single ingest call BFS's the giant. ~50ms p99 stall on the consumer task. Mitigations in order: (a) accept it, ingest rate is bounded by RPC anyway; (b) move dirty-component BFS off the write lock by snapshotting node set + adjacency views, BFS off-lock, swap result back under lock; (c) ship the deferred spanning-forest non-tree-edge skip  most expiries are non-tree → skip BFS entirely → giant only triggers split-BFS on the rare tree-edge expiry.

3. **Multi-client component_id divergence on cold-start**: two clients connecting seconds apart see slightly different `latest_block_time` snapshots → potentially different ComponentIds for the same node if a split/merge happened between their bootstraps. Live deltas reconcile within seconds (each client receives the same ComponentAssigned events from then on). Document as eventually consistent.

4. **Frontend memory on long sessions**: graphology graph instance grows over the lifetime of the tab. Frontend reducer MUST honor `EdgeExpired` and `NodeExpired` to drop entries  otherwise memory leaks at the rate of the ingest. Test with > 1h tab uptime.

## Verification

1. Open one tab, let graph populate from cold-start. Confirm `caughtUp` flips after bootstrap, live deltas continue.
2. Open second tab  after its cold-start completes, both tabs render the same nodes, edges, and component partitions (ComponentIds may differ in absolute value if cold-starts straddled a split/merge, but partitions match within seconds).
3. Kill backend, restart. Tabs reconnect → fresh cold-start → identical state across tabs.
4. Wait > 1h. Oldest edges drop via `EdgeExpired`. Frontend graphology shrinks accordingly. Memory does not grow unbounded.
5. Network throttle (Chrome devtools → Slow 3G). Cold-start succeeds, just slower.
6. Disconnect network briefly (5s) and reconnect → fresh cold-start → state converges.
7. `docker compose stop api` mid-session → frontend SSE error → retries → cold-starts when backend back.
8. Force a component split: stop ingest, manually expire a tree edge of the giant component (test hook), confirm `ComponentAssigned` deltas emit for the smaller side and frontend re-colors.

## Files touched (concrete)

(Single source of truth  supersedes the "Backend module changes" / "Frontend changes" lists above. Those sections describe behavior; this list is the authoritative file inventory.)

**Backend new**:
- `graph/expiry.rs`  tombstone_edge, settle_components (rayon), edges_by_time VecDeque, NodeIdx free-list on orphan
- `graph/bootstrap.rs`  state-to-events serializer for cold-start
- `api/graph_stream.rs`  SSE handler, cold-start on every connect

**Backend modified**:
- `graph/mod.rs`  `ingest()` extended (cutoff advance + drain expired + add edge + settle splits + seq tag)
- `graph/delta.rs`  replace slice 1 enum with slice 2 variants
- `state.rs`  drop `raw_tx`; add `delta_tx: broadcast::Sender<GraphDelta>` (broadcast originates from `graph-engine` consumer task as it ingests, slice 1's plumbing extended)
- `api/mod.rs`  drop `/graph/raw/stream` and `/graph/components` routes; add `/graph/stream`; keep `/graph/stats`
- `main.rs`  wire `delta_tx` into AppState. Drop `state-sink` consumer task spawn (orphaned, see below). Existing `graph-engine` consumer task is the sole live ingest path.
- `config.rs`  drop `KAFKA_GROUP_LIVE_STATE` env var (state-sink consumer group gone)
- `Cargo.toml`  add `rayon`

**Backend deleted**:
- `api/raw.rs` (raw fire-hose obsolete; frontend no longer needs raw edges  it consumes the typed delta protocol)
- `api/components.rs` (slice 1 polling endpoint, replaced by SSE)
- `sinks/state_sink.rs`  sole consumer of `raw_tx`, dies with `/graph/raw/stream`. Slice 1's `graph-engine` consumer group already consumes the same `solana.raw-edges` Kafka topic and now broadcasts deltas via `delta_tx`. Two consumer groups for the same topic was redundant; was kept in slice 1 only because the SSE fire-hose still needed `Arc<Edge>` broadcasts. With slice 2, one consumer group handles ingest → graph mutation → delta broadcast in a single path.

**Frontend new**:
- `hooks/use-graph-stream.ts`  protocol reducer + state holders

**Frontend modified**:
- `lib/component-stats.ts`  read graphology + nodeToComponent
- `lib/per-component-layout.ts`  read graphology + nodeToComponent
- `lib/role-detect.ts`  input switched from use-raw-stream to graphology (still runs frontend-side until slice 3)
- `lib/mpc-detect.ts`  same input swap (still runs frontend-side until slice 5)
- `lib/api.ts`  drop `subscribeRawStream`, drop `RawEdge` re-export
- `app/page.tsx`  swap hook

**Frontend deleted**:
- `hooks/use-raw-stream.ts`
- `lib/components.ts` (Union-Find)
- `lib/generated/EdgeWire.ts`, `lib/generated/EdgeKind.ts` (replaced by slice 2 GraphDelta types)

**ts-rs auto-generated** (committed): `GraphDelta.ts`, `EdgeKind.ts` (still used by EdgeAdded), per-variant types

## Slice 2 ships as one PR

Earlier I considered a 2a/2b split (backend first, then frontend with a translation shim). **Reject the split.** A shim that translates new deltas back to the old `EdgeWire` shape is exactly the kind of backcompat layer AGENTS.md tells us not to write. The PR is large but every piece is load-bearing for the same architectural change. Two PRs would just create a throwaway shim and double the review cost.

Ship as one. Verify end-to-end with the verification list above.
