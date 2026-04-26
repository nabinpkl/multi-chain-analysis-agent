

# Plan: Move Graph from Frontend → Backend (Vertical Slices)

## Why this is a redesign

Frontend currently does heavy lifting in [use-raw-stream.ts](frontend/src/hooks/use-raw-stream.ts) (770 lines): graphology mutate, Union-Find components, Louvain communities, role classifier, per-component layout. Backend just streams raw edges. Need to invert: backend owns graph state, frontend renders snapshots/deltas.

**Constraint**: zero visible behavior change per slice. Every slice ships green, frontend looks identical.

## Target architecture

```
ingester → redpanda → ch-sink (persist)
                    → graph-engine (in-memory graphology-equivalent in Rust)
                                    ↓
                            snapshot + delta broadcast
                                    ↓
                    /graph/snapshot (initial) + /graph/stream (deltas SSE)
                                    ↓
                            frontend = thin renderer
```

Backend graph engine = single tokio task, owns `petgraph` or custom adjacency map, runs Union-Find + Louvain + role classifier on tick, broadcasts deltas.

## Vertical slices

**Slice 0 — type contract**
Wire `ts-rs` on existing `EdgeWire`. Replace hand-rolled `RawEdge` in [api.ts:7](frontend/src/lib/api.ts:7) with generated type. No behavior change. Unblocks all later slices.

**Slice 1 — backend Union-Find + components**
Add `graph` module in Rust. Consume same `solana.raw-edges` topic as third consumer group. Maintain `HashMap<NodeId, ComponentId>` via Union-Find. Expose `/graph/components` returning `{node → component_id}`. Frontend keeps doing its own Union-Find (parallel run, ignore backend output). Verify backend output matches frontend computation in dev console. **No frontend change yet.**

**Slice 2 — frontend consumes backend components**
Switch frontend to read `component_id` from edge payload (backend annotates each edge before broadcast). Delete frontend Union-Find code in `lib/components.ts`. Sigma still renders identically.

**Slice 3 — backend roles**
Port `role-detect.ts` heuristics to Rust (token-mint, tip-account, mev-searcher, multi-hub, sol-hub, spl-hub, whale, mpc-member, normal). Backend computes per node, includes `role` in node payload. Frontend keeps own classifier running parallel for verify.

**Slice 4 — frontend consumes backend roles**
Drop `role-detect.ts`, `role-colors.ts` keeps mapping role → color only. Visual identical.

**Slice 5 — backend Louvain + MPC**
Port `mpc-detect.ts` using `graph-rs` or port Louvain algorithm. Emit `community_id` + `mpc_member` flag per node. Run parallel.

**Slice 6 — frontend consumes communities**
Delete `mpc-detect.ts` + `graphology-communities-louvain` dep.

**Slice 7 — snapshot + delta protocol**
Replace edge-by-edge SSE with: `GET /graph/snapshot` (full state on connect) + `/graph/stream` (`NodeAdded | NodeUpdated | EdgeAdded | EdgeRemoved | RoleChanged | ComponentMerged`). Frontend graphology becomes pure mirror of backend state. Delete component-stats.ts logic running clientside.

**Slice 8 — backend layout (optional)**
Port `per-component-layout.ts` force layout to Rust. Backend emits `(x, y)` per node. Frontend skips layout step, Sigma uses provided coords. Biggest CPU win.

**Slice 9 — strip diagnostic console.logs**
[use-raw-stream.ts:172-406](frontend/src/hooks/use-raw-stream.ts:172) gated behind `NEXT_PUBLIC_DEBUG_GRAPH` env. Hook shrinks from 770 → ~150 lines (pure render bridge).

## Key decisions to lock before slice 1

1. **Graph crate**: `petgraph` (mature, generic) vs roll own `FxHashMap<Pubkey, NodeData>` + adjacency `Vec<EdgeIdx>`. Recommend roll-own — Louvain + Union-Find don't need petgraph's algorithms, custom struct fits SSE delta emit better.
2. **Delta wire format**: tagged enum via serde + ts-rs. Avoid raw JSON.
3. **Snapshot size cap**: per AGENTS.md rule 13, never return >50k edges. Decide windowing (last 24h? last N edges?) before slice 7.
4. **Parallel-run verification**: slice 1/3/5 each need a debug toggle to log diffs between backend output + frontend compute. Build once in slice 1, reuse.

## Risk

Slice 7 is the cliff. Before it, frontend still mutates own graphology; after, frontend is dumb mirror. If snapshot+delta protocol bugs, whole UI breaks. Recommend feature flag (`NEXT_PUBLIC_GRAPH_SOURCE=backend|frontend`) for slice 7 rollback.

## Order of value

Slice 0 → 1 → 2 unlocks fastest payoff (delete frontend Union-Find, prove protocol). Slice 8 biggest perf win. Slice 7 biggest architecture win. Slice 3-6 are mechanical ports once 1-2 prove the pattern.