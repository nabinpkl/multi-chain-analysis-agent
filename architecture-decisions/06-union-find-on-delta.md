# Windowed DSU — Industry Standards

## TL;DR

| Approach | Cost | Complexity | Verdict |
|---|---|---|---|
| Full BFS rebuild | O(V+E) every tick | trivial | what most batch systems use |
| **Affected-component-only BFS** | O(V_dirty + E_dirty), parallel across disjoint components | low | **pick this** |
| Spanning-forest replacement search | amortized cheap, big constant | medium | optional refinement |
| HLT/HDT decremental connectivity | O(log² n) per delete | very high | research, not production |
| Link-cut / Euler-tour trees | O(log n) | very high | research |

## Industry reality

- **Graph DBs** (Neo4j, JanusGraph, TigerGraph, Memgraph): don't materialize connected components at all. Compute on query.
- **Streaming graph engines** (Flink/Spark GraphX): batch full recompute per micro-batch. Same as Approach A1.
- **Differential Dataflow / Naiad / Aspen**: incremental view maintenance. Localized rebuild. Closest to what we want, but framework-level.
- **Real-time graph systems** (high-freq fraud, AML): typically tombstone + periodic full rebuild. Favor simplicity over algorithmic cleverness.

Industry rarely implements HLT in prod. Implementation bugs eat any theoretical win. Aspen (CMU) is the notable exception.

## DSU is fundamentally non-decremental

UF / DSU has no `un-union` operation. Two roads:
1. Don't try. Track components separately (e.g. `component_id_for_node: Vec<u32>` updated by BFS rebuild).
2. Rebuild UF from scratch on each window slide.

Either way, **the BFS is the work.** UF is a side cache.

## Approach A — affected-component-only BFS (recommended)

Yes, you can localize. Trick = track which component each expired edge sits in. Only those components are dirty. Rest untouched.

```rust
fn expire_tick(state: &mut GraphState, cutoff_block_time: u64) -> Vec<GraphDelta> {
    // 1. Collect expired edges, tag dirty components
    let mut dirty: FxHashSet<ComponentId> = default();
    let mut expired_idxs = vec![];
    for (eidx, slot) in state.edges.iter().enumerate() {
        if let Some(e) = slot {
            if e.block_time < cutoff_block_time {
                dirty.insert(state.component_id_for_node[e.src as usize]);
                expired_idxs.push(eidx as u32);
            }
        }
    }

    // 2. Drop expired from edges + adj
    for eidx in &expired_idxs {
        let e = state.edges[*eidx as usize].take().unwrap();
        state.out_adj[e.src as usize].retain(|x| x != eidx);
        state.in_adj[e.dst as usize].retain(|x| x != eidx);
    }

    // 3. Per dirty component: BFS partition (parallelizable)
    let partitions: Vec<_> = dirty.par_iter()  // rayon
        .map(|&old_id| (old_id, partition_via_bfs(state, old_id)))
        .collect();

    // 4. Reassign component_ids for splits
    let mut deltas = vec![];
    for (old_id, parts) in partitions {
        if parts.len() == 1 { continue; }  // no split, keep id
        // largest partition keeps old_id; rest get fresh ids
        let mut sorted = parts;
        sorted.sort_by_key(|p| std::cmp::Reverse(p.len()));
        for partition in &sorted[1..] {
            let new_id = state.alloc_component_id();
            for &node in partition {
                state.component_id_for_node[node as usize] = new_id;
                deltas.push(GraphDelta::ComponentSet { node, component_id: new_id });
            }
        }
    }

    // 5. NodeExpired for now-orphan nodes (no remaining edges)
    // 6. EdgeExpired deltas appended for each expired_idxs

    deltas
}
```

Per-component BFS is **fully disjoint** (components are by definition disconnected). Throw at rayon, get linear speedup. 4-core Oracle VM = 4× faster.

## Why parallel BFS within a component is hard

Within one component, BFS is serial (frontier expansion has data deps). Parallel BFS exists (Beamer's direction-optimized, etc.) but for our sizes (~10k nodes per component) overhead beats wins. **Parallelize across components, serial within.**

## Solana-specific reality check

Solana txs collapse into one giant component fast. DEX hubs + exchanges = ~80-95% of nodes in one component. Expiring any edge in the giant marks giant dirty → BFS over 80% of graph → no savings vs full rebuild.

Mitigation = approach C below. Otherwise accept that expiry tick on Solana ≈ full BFS in practice.

## Approach C — spanning-forest non-tree skip (refinement)

Maintain a spanning tree per component. On edge expiry:
- **Non-tree edge**: just remove. Connectivity unchanged. Skip BFS entirely.
- **Tree edge**: search remaining non-tree edges of that component for one crossing the cut. If found, swap in (no split). If not, BFS to confirm split.

For dense components (giant on Solana), most edges are non-tree. Skipping non-tree expiries saves 90%+ of work even on the giant.

Cost = maintain spanning tree on every union. ~constant per insert.

Implementation: store `is_tree_edge: bool` per edge, plus a tree-parent pointer per node. Union-Find already has parent pointer; reuse it as the spanning tree. Standard technique — Holm-Thorup's "level" structure starts here.

This is ~100 lines of code. Pays for itself if expiry tick is the hot path.

## Recommendation

**Build A first.** Ship. Profile. If expiry tick on giant component dominates wall time, add C (spanning-forest non-tree skip). Don't go further unless real numbers force it.

For 50k-360k edges total at 30s tick interval, A is almost certainly enough. Solo VM, single user. Engineering time better spent on slices 3-8.

## What to add to the plan file

- Locked decision: **affected-component-only BFS, rayon parallelism across dirty components**. Refinement (spanning-tree non-tree skip) deferred until profiling shows need.

Want me to update `05-differential-rendering.md` with this?