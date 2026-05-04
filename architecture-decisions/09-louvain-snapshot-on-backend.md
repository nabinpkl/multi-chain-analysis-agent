# 10: Backend Louvain via per-window analytics tasks

Move Louvain community detection off the frontend main thread to a
per-window analytics task on the backend. Frontend keeps role
classification, hub stats, and MPC detection (each ~100-400ms at
50k nodes); they consume the backend's community labels as input.

## Status

Accepted, shipped. Implementation lives in `backend/src/analytics/`
(task, snapshot, louvain, stable_labels, delta) and
`frontend/src/store/analytics.ts` + `frontend/src/hooks/use-raw-stream.ts`.
A runtime UI toggle lets the user A/B between frontend and backend
Louvain.

## Problem

Frontend Louvain on the main thread freezes the page at 50k+ nodes.
Role classifier and hub stats are fine on frontend at that scale
(bounded ~100-400ms passes); only Louvain crosses the 3-second tick
boundary as graph size grows. Shipping just Louvain to backend is
the smallest change that fixes the freeze.

## Decision

Five coupled architectural choices.

### 1. Backend role expansion: from passive store to analytics provider

Today, backend's role is passive: ingest builds the canonical graph
in `GraphState`, stats endpoints read it. This change expands backend
to be an analytics provider, not just a store.

The pattern is **read models derived from a single write model**:

- **Write model:** `GraphState`, single source of truth, owned by
  ingest.
- **Read model (per window):** an analytics task that periodically
  reads `GraphState` under brief read lock, snapshots a window-scoped
  slice, releases the lock, computes a derived view (Louvain
  communities + stable labels). Persistent state on the task is just
  the derived labels and previous partitions. No shadow adjacency,
  no event subscription.

Industry-standard pattern for in-process analytics over an
OLTP-shaped state: materialized views over a primary table,
refreshed on a schedule. A future agent reading the code sees one
graph and N derived read views, not N+1 parallel graphs.

```
graph_consumer (single writer) ──► GraphState (Arc<RwLock>)
                                       ▲
                       brief read lock │
                       every 3s        │
                                  ┌────┴────┐
                                  │ analytics│ × NUM_WINDOWS
                                  │   task   │
                                  └────┬────┘
                                       │ analytics broadcast
                                       ▼
                              SSE handler (multiplexed)
                                       │
                                       ▼
                                   Frontend
```

### 2. Stream-and-replicate rejected

Each consumer mirroring the full graph from events is more common
when the consumer is a separate service in a different process. For
in-process tasks it adds memory and cognitive overhead without
buying anything we need. The brief-read-lock + snapshot approach is
the simpler shape.

### 3. Time-bound 3-second batch (not size-bound, not hybrid)

Louvain is a batch algorithm; computes a global modularity-optimal
partition end-to-end. Something has to define the batch boundary.

Time-bound at 3s, justified by load shape and cost shape:

1. Continuous load (~405 tx/s sustained) means every 3s tick has a
   non-trivial batch of changes. No "wasted empty tick" regime to
   optimize for.
2. Per-tick fixed cost is the snapshot walk: ~5-10ms at 50k edges.
   Bounded.
3. Per-tick variable cost is Louvain on dirty components only.
   Self-scales with batch size: small change set means small
   Louvain run.
4. UI latency cap is predictable: changes appear within 3s + Louvain
   runtime.
5. Simplest implementation: one timer, no event subscription, no
   counter poll, no hybrid wakeup logic.

Size-bound triggering ("run when N events accumulated") would earn
its keep under bursty load with idle gaps. Our load is roughly
constant, not bursty; size-bound buys nothing. Hybrid (min of time,
size) only matters if Louvain runs spike past the tick interval,
which the adaptive-throttle risk mitigation already covers.

The 3s number is a knob, not architecture.

### 4. Per-window monotonic community IDs with stable label matching

Each window assigns its own community IDs from a per-window monotonic
counter (never reused). On each tick, after Louvain produces a fresh
local partition for a dirty component, a max-overlap matching
algorithm assigns global IDs:

- Largest new groups get priority (most matching power for stable
  labels).
- One previous community ID claimed at most once per match cycle.
- New communities (no significant overlap with prior) get fresh IDs.

This keeps the colored regions in the UI from flickering as nodes
shift. Without stable matching, every tick would reshuffle community
IDs and the UI would flash.

### 5. Louvain ported in-house, no library dependency

`petgraph-louvain` (the obvious Rust option) has uncertain
maintenance status. The algorithm is small (~250 LoC for Phase A
local moves + Phase B collapse + map-back). Per the project rule of
"don't depend on unmaintained libraries" (`AGENTS.md` library
maintenance bar), port from scratch.

### 6. Runtime UI toggle (frontend ↔ backend Louvain)

Zustand-backed segmented control persisted to localStorage,
defaulting to `'frontend'` (existing behavior, safest fallback).
Visible on every page so a user can A/B compare during demo or
troubleshooting. Mode transitions are graceful within one detect
interval (≤3s).

The toggle stays in the codebase as a safety net until the backend
path is settled in production for 2+ weeks; removing the frontend
Louvain code is a separate cleanup ticket.

## Consequences

### Accepted

- Backend gains a new module (`backend/src/analytics/`) and a new
  responsibility (analytics provider, not just store).
- `NUM_WINDOWS` analytics tasks run continuously, each reading
  `GraphState` under brief read lock every 3s.
- Brief read lock during snapshot can block the ingest writer for
  ~5-10ms per tick per task. Across staggered tasks at 405 tx/s,
  this is ~2-4 messages of queueing in any 3s window. Negligible.
- New SSE channel: `AnalyticsBatch` events, diff-only, epoch-ordered.
  Bootstrap snapshot via watch-channel read in the SSE bootstrap
  path.
- Frontend keeps role classifier + hub stats + MPC detection
  unchanged; only Louvain swaps source.
- Per-window UF gets recomputed every tick from the snapshot's
  adjacency rather than cached on `GraphState`. ~5ms at 50k edges;
  caching deferred until it shows up as hot.

### Rejected

- **Stream-and-replicate**: each analytics task mirroring the full
  graph from event subscriptions. Memory + cognitive overhead with
  no benefit at in-process scale.
- **Size-bound or hybrid batch trigger**: load shape doesn't justify
  it.
- **`petgraph-louvain`**: maintenance uncertain.
- **Caching per-window UF on `GraphState`**: only if the snapshot
  walk shows up as hot. Not yet.
- **Removing frontend Louvain code on first ship**: deferred behind
  the runtime toggle as safety net for 2+ weeks of production
  stability.
- **Role classifier + hub stats moved to backend**: separate phase.
  Cheap on frontend at 50k (~100ms), not freeze-causing.

## Risks (preserved as monitor list)

| Risk | Defense |
|---|---|
| Louvain port produces visibly different partitions vs frontend | Runtime UI toggle for A/B verification |
| Stable label flicker | Instrument label-change counts per tick; tune matching heuristic if observed |
| Bootstrap missing communities for first few seconds | Watch-channel snapshot read in SSE bootstrap path |
| Out-of-order analytics arrival | Epoch monotonic; frontend rejects stale |
| 1h Louvain crosses 3s tick at 50k | Adaptive throttle: if last run took 4s, push next tick to 12s. Self-bounded; analytics task isn't on main, no death spiral |

## Implementation reference

The shipped code is the source of truth:

- `backend/src/analytics/mod.rs` (task spawn, public surface)
- `backend/src/analytics/task.rs` (per-window task struct + run loop)
- `backend/src/analytics/snapshot.rs` (`snapshot_window` helper + UF
  partition)
- `backend/src/analytics/louvain.rs` (algorithm port)
- `backend/src/analytics/stable_labels.rs` (max-overlap matching)
- `backend/src/analytics/delta.rs` (`AnalyticsBatch` +
  `AnalyticsSnapshot` wire types)
- `backend/src/api/graph_stream.rs` (SSE multiplex + bootstrap)
- `frontend/src/store/analytics.ts` (Zustand slice for
  `louvainSource`)
- `frontend/src/hooks/use-raw-stream.ts` (handler + detect gate via
  ref)

## References

- ADR 8 (moving layout to a Web Worker), the prior step in
  unblocking the frontend main thread.
- Louvain method: Blondel, Guillaume, Lambiotte, Lefebvre (2008),
  "Fast unfolding of communities in large networks."
- Read-model-from-write-model pattern: Fowler's "CQRS" essay; the
  shape applies in-process here as it does across services.
- AGENTS.md "Library maintenance bar" (rationale for porting
  Louvain rather than depending on `petgraph-louvain`).
