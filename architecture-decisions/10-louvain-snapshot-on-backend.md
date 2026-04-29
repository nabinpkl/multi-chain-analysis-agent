# Backend Louvain migration with runtime switch

## Goal

Move Louvain off the frontend main thread to a per-window analytics task on
the backend. Kill the freeze at 50k+. Frontend's role classifier, hub stats,
MPC detection stay where they are; they consume backend's community labels as
input. Provide a runtime UI switch so the user can A/B between frontend and
backend Louvain without restarts.

## Why this layer specifically

Louvain on main is the only thing that scales nonlinearly with graph size and
crosses the 3s tick boundary at 50k. Role + hub at 50k are ~100-400ms 
bounded, not freeze-causing. Shipping just Louvain to backend is the smallest
change that fixes the freeze.

## Architectural framing (read this first)

Today, backend's role is passive: ingest builds the canonical graph in
`GraphState`, stats endpoints read it, that's the whole job. This change
**expands backend to be an analytics provider**, not just a store.

The pattern is **read models derived from a single write model**:

- **Write model**: `GraphState`. Single source of truth. Owned by ingest.
- **Read model (per window)**: an analytics task that periodically reads
  `GraphState` under brief read lock, snapshots a window-scoped slice,
  releases the lock, and computes a derived view (Louvain communities, stable
  labels). Persistent state on the task is just the derived labels and prev
  partitions  no shadow adjacency, no event subscription.

This is the industry-standard pattern for in-process analytics over an
OLTP-shaped state: think materialized views over a primary table, refreshed on
a schedule. Stream-and-replicate (each consumer mirrors the full graph from
events) is more common when the consumer is a *separate service in a different
process*; for in-process tasks it adds memory and cognitive overhead without
buying anything we need.

A future agent reading the code sees one graph (`GraphState`) and N derived
read views, not N+1 parallel graphs.

## Architecture

```
graph_consumer (existing single writer)  ──►  GraphState  (Arc<RwLock>)
                                                ▲     ▲
                            brief read lock     │     │     brief read lock
                            every 3s            │     │     every 3s
                                              ┌─┴───┐ │ ┌─────┴───┐
                                              │ana  │ │ │ analytics│ ... (NUM_WINDOWS tasks)
                                              │(60s)│ │ │  (3600s) │
                                              └──┬──┘ │ └──────┬───┘
                                                 │    │        │
                                                 │ snapshot_tx (watch)
                                                 │    │        │
                                                 │ analytics broadcast (NEW, per window)
                                                 ▼    ▼        ▼
                                       SSE handler  multiplexes edge + analytics
                                                       │
                                                       ▼
                                                  Frontend
                                       (mode-aware: applies analytics if
                                        louvainSource === 'backend',
                                        runs local Louvain otherwise)
```

Each analytics task is fully isolated in terms of *its derived state*, but
**reads from the shared `GraphState`** for raw data on each tick. No event
subscription, no adjacency mirror.

## Per-task internal state

```rust
struct AnalyticsTask {
    window_idx: usize,

    // Derived state, persistent across ticks. Small.
    community_label: FxHashMap<NodeIdx, u32>,
    next_community_id: u32,                    // monotonic per-window, never reused
    prev_partition_per_component:
        FxHashMap<ComponentId, FxHashMap<NodeIdx, u32>>,
    prev_components:
        FxHashMap<ComponentId, FxHashSet<NodeIdx>>,    // for dirty diffing

    epoch: u32,
    snapshot_tx: watch::Sender<Arc<AnalyticsSnapshot>>,
}
```

No `adj`, no `ComponentTracker`. Adjacency and components are rebuilt fresh
each tick from the write model.

## Snapshot helper

A new function on `GraphState` (or a free function in the analytics module)
that takes a brief read lock and produces a per-window snapshot:

```rust
pub fn snapshot_window(
    g: &GraphState,
    window_idx: usize,
) -> WindowSnapshot {
    let mut adj: FxHashMap<NodeIdx, FxHashMap<NodeIdx, f64>> = FxHashMap::default();
    for &edge_id in g.windows[window_idx].edges_by_time.iter() {
        let Some(e) = g.get_edge(edge_id) else { continue };
        let weight = e.amount as f64;
        *adj.entry(e.src).or_default().entry(e.dst).or_insert(0.0) += weight;
        *adj.entry(e.dst).or_default().entry(e.src).or_insert(0.0) += weight;
    }
    // Compute per-window UF from the snapshot's adjacency. GraphState's
    // `node_to_component` is global; we need per-window connectivity.
    let components = uf_partition(&adj);
    WindowSnapshot { adj, components }
}

pub struct WindowSnapshot {
    pub adj: FxHashMap<NodeIdx, FxHashMap<NodeIdx, f64>>,
    pub components: FxHashMap<ComponentId, FxHashSet<NodeIdx>>,
}
```

Cost at 50k edges (1h window): ~5-10ms walk. Held under read lock. Other
windows are subsets, smaller.

## Trigger: time-bound batch (3s)

Louvain is a batch algorithm. It computes a global modularity-optimal
partition end to end; it isn't designed to be incrementalized per-edge. So
*something* has to define the batch boundary.

We pick **time-bound at 3s**. Justified by load shape and cost shape, not by
preference:

1. Continuous load (~405 tx/s sustained) means every 3s tick has a non-trivial
   batch of changes. There is no "wasted empty tick" regime to optimize for.
2. Per-tick fixed cost is the snapshot walk: ~5-10ms at 50k edges. Bounded.
3. Per-tick variable cost is Louvain on dirty components only. Self-scales
   with batch size: small change set means small Louvain run.
4. UI latency cap is predictable: changes appear within 3s + Louvain runtime.
5. Simplest implementation: one timer, no event subscription, no counter
   poll, no hybrid wakeup logic.

Size-bound triggering ("run when N events accumulated") would earn its keep
under bursty load with idle gaps. Our load isn't shaped that way; it's
roughly constant. Hybrid (min of time, size) only matters if Louvain runs
spike past the tick interval, which the existing adaptive-throttle entry in
the risks table already covers.

The 3s number is a knob, not architecture. Tune down for snappier UI, up if
snapshot cost shows up as hot. The architecture is "time-bound batch over a
snapshot."

Dirty tracking is *derived* from snapshot comparison against
`prev_components`, not from event subscription. Cheap (set comparison per
component).

## Tick loop per task

```rust
async fn run(window_idx: usize, state: AppState) {
    let mut t = AnalyticsTask::new(window_idx);
    loop {
        sleep(TICK_INTERVAL).await;

        // Brief read lock, snapshot, release.
        let snapshot = {
            let g = state.graph.read();
            snapshot_window(&g, window_idx)
        };

        // Dirty = components whose member set changed (or new components).
        let mut dirty = FxHashSet::default();
        for (cid, members) in &snapshot.components {
            if t.prev_components.get(cid) != Some(members) {
                dirty.insert(*cid);
            }
        }
        // Components that disappeared since last tick: their members'
        // labels need removal.
        let mut community_removals = Vec::new();
        for (cid, members) in &t.prev_components {
            if !snapshot.components.contains_key(cid) {
                for n in members {
                    if t.community_label.remove(n).is_some() {
                        community_removals.push(*n);
                    }
                }
                t.prev_partition_per_component.remove(cid);
            }
        }

        // Louvain on dirty components.
        let mut community_changes = Vec::new();
        for cid in dirty {
            let members = &snapshot.components[&cid];
            if members.len() < SUB_CLUSTER_THRESHOLD {
                for n in members {
                    if t.community_label.remove(n).is_some() {
                        community_removals.push(*n);
                    }
                }
                t.prev_partition_per_component.remove(&cid);
                continue;
            }
            let partition_local = louvain_per_component(members, &snapshot.adj);
            let partition_global = stable_match(
                partition_local,
                t.prev_partition_per_component.get(&cid),
                &mut t.next_community_id,
            );
            for (n, gid) in &partition_global {
                if t.community_label.get(n) != Some(gid) {
                    community_changes.push((*n, *gid));
                    t.community_label.insert(*n, *gid);
                }
            }
            t.prev_partition_per_component.insert(cid, partition_global);
        }

        if !community_changes.is_empty() || !community_removals.is_empty() {
            t.epoch += 1;
            let batch = Arc::new(AnalyticsBatch {
                epoch: t.epoch,
                community_changes,
                community_removals,
            });
            let _ = state.analytics.txs[window_idx].send(batch);
            let snap = Arc::new(AnalyticsSnapshot {
                epoch: t.epoch,
                labels: t.community_label.clone(),
            });
            let _ = t.snapshot_tx.send(snap);
        }
        t.prev_components = snapshot.components;
    }
}
```

## Louvain algorithm (port from scratch)

Modularity gain:
```
ΔQ_move(n, c) = k_in(n, c)/m  −  k(n) · Σ_tot(c) / (2m²)
```
where `k_in(n, c)` is sum of weights of edges from n into community c, `k(n)`
is n's total weighted degree, `Σ_tot(c)` is total weighted degree of community
c, `m` is total weight in the (sub)graph.

**Phase A (local moves)**: for each node, evaluate moving to each neighbor's
community, take the best ΔQ if positive. Repeat until no moves in a full pass.

**Phase B (collapse)**: collapse each community into a super-node. Edge
weights between super-nodes = sum of original cross-community edge weights.
Re-run Phase A on the collapsed graph.

**Iterate A/B** until total modularity stops improving.

**Map back**: each level keeps a parent pointer; the final super-community
projects down to original nodes.

Per-component scope: Louvain runs on a single component's adjacency
restricted to that component's members. Mathematically equivalent to global
Louvain because cross-component pairs have zero edge weight; the algorithm
never moves a node to a non-neighbor community.

Implementation: ~250 lines. Library option (`petgraph-louvain`) skipped
because maintenance status is uncertain and the algorithm is small enough to
own (project rule: don't depend on unmaintained libraries).

## Stable label matching

Per dirty component, after Louvain produces local partition `P_new`
(algorithm-internal IDs 0..k):

```rust
let mut matched = FxHashSet::default();
let mut result = FxHashMap::default();
let groups = group_by_partition(&P_new);
let sorted = groups.into_iter()
    .sorted_by_key(|(_, m)| -(m.len() as i32));     // largest groups first

for (local_id, members) in sorted {
    let mut counts: FxHashMap<u32, u32> = FxHashMap::default();
    if let Some(prev) = prev_partition.as_ref() {
        for n in &members {
            if let Some(prev_id) = prev.get(n) {
                if !matched.contains(prev_id) {
                    *counts.entry(*prev_id).or_insert(0) += 1;
                }
            }
        }
    }
    let global_id = match counts.into_iter().max_by_key(|(_, c)| *c) {
        Some((id, _)) => { matched.insert(id); id }
        None => { let id = *next_community_id; *next_community_id += 1; id }
    };
    for n in members { result.insert(n, global_id); }
}
```

Invariants:
- One old community ID claimed at most once.
- Largest new groups get priority (most matching power).
- New communities get fresh IDs from per-window monotonic counter; never
  reused.

## Wire format

```rust
// backend/src/analytics/delta.rs
#[derive(Serialize, TS, Clone, Debug)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct AnalyticsBatch {
    pub epoch: u32,
    pub community_changes: Vec<(u32, u32)>,    // (NodeIdx, community_id)
    pub community_removals: Vec<u32>,           // NodeIdx
}
```

Diff-only. Bootstrap snapshot is a single `AnalyticsBatch` with
`community_changes` containing all currently-labeled nodes.

## State.rs additions

```rust
#[derive(Clone)]
pub struct AnalyticsChannels {
    pub txs: [broadcast::Sender<Arc<AnalyticsBatch>>; NUM_WINDOWS],
    pub snapshots: [watch::Receiver<Arc<AnalyticsSnapshot>>; NUM_WINDOWS],
}

pub struct AnalyticsSnapshot {
    pub epoch: u32,
    pub labels: FxHashMap<u32, u32>,
}

// AppState gains: pub analytics: AnalyticsChannels,
```

The snapshot watch channel lets the SSE bootstrap path read the latest
analytics state without holding any lock; the analytics task pushes a new
snapshot after each tick.

## SSE handler change (graph_stream.rs)

Subscribe to two channels for the chosen window. Multiplex with
`futures::stream::select`.

Bootstrap order:
1. Existing edge bootstrap (NodeAdded + EdgeAdded events).
2. NEW: read `state.analytics.snapshots[w].borrow()`, emit a single
   `AnalyticsBatch` with all current labels.
3. CaughtUp.
4. Live tail (multiplexed edge + analytics).

New SSE clients see communities immediately, not after one tick latency.

## Frontend: runtime switch via Zustand (NO env var)

```ts
// frontend/src/store/analytics.ts (NEW)
export type LouvainSource = 'frontend' | 'backend';

export interface AnalyticsSlice {
  louvainSource: LouvainSource;
  setLouvainSource: (s: LouvainSource) => void;
}

export const createAnalyticsSlice = (set, get): AnalyticsSlice => ({
  louvainSource:
    typeof window !== 'undefined' &&
    localStorage.getItem('mca:louvainSource') === 'backend'
      ? 'backend'
      : 'frontend',
  setLouvainSource: (s) => {
    set({ louvainSource: s });
    if (typeof window !== 'undefined') {
      localStorage.setItem('mca:louvainSource', s);
    }
  },
});
```

Initialized from localStorage so the choice survives reloads. Default is
`'frontend'` (existing behavior, safest fallback).

## Hook integration with reactive switch

The SSE listener is set up once in a `useEffect` per window-change. To
consume the always-current mode value without re-creating the listener (which
would re-bootstrap), use a ref synced from the Zustand value:

```ts
const louvainSource = useAnalyticsStore(s => s.louvainSource);
const louvainSourceRef = useRef(louvainSource);
useEffect(() => { louvainSourceRef.current = louvainSource; }, [louvainSource]);

es.addEventListener("AnalyticsBatch", (ev) => {
  if (louvainSourceRef.current !== 'backend') return;       // ignore in frontend mode
  const batch = JSON.parse(ev.data);
  if (batch.epoch <= lastEpochRef.current) return;
  lastEpochRef.current = batch.epoch;
  for (const [idx, communityId] of batch.community_changes) {
    const pubkey = idxToPubkeyRef.current.get(idx);
    if (!pubkey) continue;
    nodeToCommunityRef.current.set(pubkey, communityId);
    const slot = slotByPubkeyRef.current.get(pubkey);
    if (slot !== undefined) {
      layoutClientRef.current?.setCommunity(slot, communityId);
    }
  }
  for (const idx of batch.community_removals) {
    const pubkey = idxToPubkeyRef.current.get(idx);
    if (!pubkey) continue;
    nodeToCommunityRef.current.delete(pubkey);
  }
});
```

Detect interval gate (same ref):

```ts
function runDetect() {
  const nodeToCommunity = louvainSourceRef.current === 'backend'
    ? nodeToCommunityRef.current        // populated by AnalyticsBatch handler
    : runFrontendLouvain(graph);         // existing path

  // ... mpcMembers, searcherProfile, classifyNodes, hubStats unchanged ...
}
```

## Mode transitions at runtime

**Frontend → Backend**:
- Frontend stops running Louvain in next detect tick.
- `nodeToCommunityRef` contains last frontend-computed labels (stale wrt
  backend's space).
- On next backend tick (≤ 3s) an AnalyticsBatch arrives and overwrites.
- 0-3s window where layout uses stale frontend labels for cross-community
  repulsion. Acceptable transition.

**Backend → Frontend**:
- Frontend resumes running Louvain in next detect tick.
- `nodeToCommunityRef` gets recomputed entirely from local graphology.
- AnalyticsBatch events keep arriving (backend always emits) but are ignored.

Either direction is graceful within one detect interval.

## UI control

Toggle row in the sidebar status panel, between WINDOW and the live
indicator. Same visual style as the window buttons:

```
LOUVAIN
[FRONTEND] [BACKEND]
```

Two-state segmented control. State driven by `useAnalyticsStore`.
localStorage persists the choice across reloads. Visible on every page so a
user can A/B compare during demo or troubleshooting.

## Shipping as one change

This ships end to end in a single change. No log-only intermediate phases.
User verifies visually at the end by toggling the UI switch and comparing
frontend vs backend Louvain.

Build order within the change (mechanical, not gated):

1. **Backend types and channels** — `AnalyticsBatch`, `AnalyticsSnapshot`,
   `AnalyticsChannels` on `AppState` (broadcast per window + watch per
   window).
2. **Snapshot helper** — `snapshot_window` builds per-window adjacency + UF
   from `GraphState` under brief read lock.
3. **Louvain + stable labels** — port from scratch into
   `analytics/louvain.rs` and `analytics/stable_labels.rs`.
4. **Analytics task** — `analytics/task.rs` with the tick loop: sleep,
   snapshot, diff `prev_components` for dirty, run Louvain on dirty, stable
   match, emit `AnalyticsBatch`, push `AnalyticsSnapshot` to watch.
5. **Spawn six tasks** in `main.rs` (one per window, NUM_WINDOWS = 6).
6. **SSE multiplex** — `graph_stream.rs` reads the analytics watch snapshot
   in bootstrap, then multiplexes edge broadcast + analytics broadcast in the
   live tail.
7. **Auto-generate** `frontend/src/lib/generated/AnalyticsBatch.ts` via
   ts-rs.
8. **Frontend Zustand slice** — `store/analytics.ts` with `louvainSource`
   persisted to localStorage. Default `'frontend'`.
9. **UI toggle** — segmented control in the sidebar status panel.
10. **Hook integration** — `use-raw-stream.ts` adds `AnalyticsBatch` listener
    gated by ref; detect tick gate switches between local Louvain and backend
    labels by ref.
11. **Build** — `docker compose up -d --build` per AGENTS.md.

After build succeeds, user toggles the UI switch and verifies visually that
both modes render coherent communities and that the freeze at 50k+ disappears
in backend mode.

## Risks and mitigations

| Risk | Defense |
|------|---------|
| Snapshot read lock blocks ingest | Bounded: ~5-10ms every 3s per task. Across NUM_WINDOWS staggered tasks, ingest blocked at most ~5-10ms in any 3s window  ~2-4 messages worth of queueing at 405 tx/s. Negligible. |
| Louvain port produces visibly different partitions | User verifies visually post-build via the UI toggle (frontend vs backend side-by-side). Tune if partitions diverge meaningfully. |
| Stable label flicker | Visible as community color flapping in the UI. If observed, instrument label-change counts per tick and tune the matching heuristic. |
| Bootstrap missing communities for first few seconds | Watch-channel snapshot read in bootstrap path. |
| Out-of-order analytics arrival | Epoch monotonic, frontend rejects stale. |
| 1h Louvain crosses 3s tick at 50k | Adaptive throttle: if last run took 4s, push next tick to 12s. Self-bounded; analytics task isn't on main, so no death spiral. |
| User toggles mid-session, layout jitters during transition | Acceptable 0-3s transition. Documented behavior. |
| `snapshot_window` recomputes UF every tick | Cost ~5ms at 50k. If it shows up as hot, optionally cache per-window UF on `GraphState` later. Out of scope for this phase. |

## Files

**Backend new**:
- `backend/src/analytics/mod.rs`  task spawn, public surface
- `backend/src/analytics/task.rs`  per-window task struct + run loop
- `backend/src/analytics/snapshot.rs`  `snapshot_window` helper + UF partition
- `backend/src/analytics/louvain.rs`  algorithm port
- `backend/src/analytics/stable_labels.rs`  max-overlap matching
- `backend/src/analytics/delta.rs`  `AnalyticsBatch` + `AnalyticsSnapshot`

**Backend modified**:
- `backend/src/state.rs`  add `AnalyticsChannels`
- `backend/src/main.rs`  spawn NUM_WINDOWS analytics tasks
- `backend/src/api/graph_stream.rs`  multiplex SSE, bootstrap snapshot

**Frontend new**:
- `frontend/src/store/analytics.ts`  Zustand slice for `louvainSource`
- `frontend/src/components/flow/louvain-source-toggle.tsx`  UI control

**Frontend modified**:
- `frontend/src/hooks/use-raw-stream.ts`  handler + detect gate via ref
- `frontend/src/components/flow/graph-page.tsx` (or wherever sidebar lives) 
  embed toggle

**Auto-generated**:
- `frontend/src/lib/generated/AnalyticsBatch.ts`

**Project rule**:
After backend changes, run `docker compose up -d --build` per AGENTS.md.

## Effort estimate

End-to-end in a single change:

- Backend types, channels, snapshot helper: 0.5 day
- Louvain port + stable label matching: 1.5 days
- Analytics task + spawn + SSE multiplex + bootstrap snapshot: 0.5 day
- Frontend Zustand slice + UI toggle + hook integration: 0.5 day
- Build + visual verification: 0.5 day

Total: ~3 days, shipped as one change. User verifies visually at the end via
the UI toggle.

## What's intentionally NOT in this plan

- **Role classifier on backend**: separate phase. Cheap on frontend at 50k
  (~100ms), not freeze-causing.
- **Hub stats on backend**: same.
- **Token-mint / tip-account immediate coloring at applyEdge time**:
  independent small win.
- **Removing frontend Louvain code**: deferred until backend stable in prod
  for 2+ weeks. Toggle stays as the safety net; turning it off becomes a
  separate cleanup ticket.
- **Caching per-window UF on `GraphState`**: only if `snapshot_window` UF
  cost shows up as hot. Otherwise the simpler "rebuild each tick" path is
  fine.