## How to handle 100k+ nodes and edges on graph

### The real bottlenecks in `per-component-layout.ts`

1. **O(N²) repulsion per component, every frame.** Cap is `MAX_N2_COMPONENT_SIZE = 400`. Once your giant component crosses 400, you skip it, good. But components in the 100-400 range still do N². At 11k nodes you almost certainly have several mid-size components doing 100k-160k pair calcs each, every animation frame. That's the dominant cost.

2. **`graph.forEachEdge(srcId, …)` inside the node loop.** This is graphology's hashmap-backed iteration, called `n` times per component, even when you only want intra-component edges. For a 400-node component you're hitting graphology's edge index 400 times per frame just for attraction.

3. **`pushComponentsApart` is O(C²)** over all components. Component count grows roughly with node count for a transactional graph. 11k nodes → easily 2-3k components → 4-9M centroid pair checks per frame, plus a centroid+radius recomputation that scans every node twice each frame.

4. **`forEachNode` + `setNodeAttribute` x2 per node per frame** to read/write x/y. graphology attribute access goes through hashmap lookups. 11k × ~6 attribute reads × 60fps = ~4M map lookups/sec just for I/O.

5. **MPC/role detect every 3s** does Louvain on the whole graph, builds many `Map`s, calls `graph.forEachNode` and `graph.forEachNeighbor`. At 11k it's a multi-hundred-ms stall every 3 seconds (you'll see the visible hitch).

6. **`hideEdgesOnMove: false`** means Sigma redraws all 19k edges on every pan/zoom frame. Setting it `true` is the single biggest "feels snappy" win for free.

### What industry does for 10k-100k live graphs

| Technique | What it solves |
|---|---|
| **Barnes-Hut quadtree repulsion** (FA2 default) | O(N log N) instead of O(N²). Required past ~1k nodes. |
| **WebWorker for layout** | Frees main thread so Sigma's render stays at 60fps even when layout stalls. |
| **WebGL force layout (Cosmograph, d3-force-3d on GPU)** | 100k+ nodes at 60fps; physics on the GPU. |
| **Edge bundling / aggregation** | Collapse many parallel edges into one weighted edge. |
| **Degree-based filtering / level-of-detail** | Hide leaves at low zoom, show them on zoom-in. |
| **"Cool" the layout** | Ramp damping to ~0 after N seconds; new nodes still settle but the steady state is frozen. Free huge CPU win. |
| **Frame budget / skip frames** | Run layout every 2nd or 3rd RAF, not every RAF. |
| **Drop low-signal edges** | One-tx singleton dust edges hidden, kept in data layer. Your "blink" idea. |

### What I'd actually do, in order of bang-for-buck

**Tier 1, takes hours, 5-10x gain:**

1. **`hideEdgesOnMove: true`** in the Sigma settings. Free.
2. **Cool the simulation.** After 10-15s of stable node count, stop running layout and only run it when an edge actually arrives (and even then, only for the affected component). Right now you run every frame regardless. Most of your CPU is recomputing forces for nodes that aren't moving.
3. **Replace your O(N²) repulsion with Barnes-Hut.** `graphology-layout-forceatlas2` already ships a worker version with Barnes-Hut: `graphology-layout-forceatlas2/worker`. You'd lose the per-component isolation but for a connected transaction graph that's probably fine, and you can keep your `pushComponentsApart` rigid pass on top of it.
4. **Batch read x/y once per frame into the typed arrays you already build, layout in arrays only, write back at the end.** You already do this inside the per-component loop. The fix is to do it for the *whole graph* once per frame, never call `getNodeAttribute('x')` inside an inner loop.

**Tier 2, half a day, makes 50k feasible:**

5. **Move layout to a WebWorker.** Pass it the delta queue + the graph structure (CSR-style typed arrays). Worker computes positions, transfers a `Float32Array` back via `postMessage` with `transferable`. Main thread just writes positions onto graphology and Sigma renders. This is the canonical pattern for sigma at scale. graphology already has worker examples.
6. **Run MPC/Louvain in a separate worker too.** Right now it freezes the main thread for ~hundreds of ms every 3s.
7. **Drop dust edges from the visible graph.** Your "blink" idea is good and matches what fraud-graph products do. Concretely:
   - Keep the data in graphology (or a shadow store) for stats.
   - Don't call `addEdgeWithKey` for an edge whose both endpoints have degree 1 *and* whose volume is below a threshold *and* whose age is below ~5s. Materialize it only if it survives or attaches to something interesting.
   - Or: render with `forceLabel`/`hidden` flag toggled by zoom. Sigma supports per-node/edge hidden attributes.

**Tier 3, days, but this is what 100k+ products do:**

8. **Switch to Cosmograph or sigma's WebGL with custom programs.** Cosmograph does layout on GPU, scales to millions, designed exactly for this. The cost is losing your custom per-component physics tuning. You'd express your visual intent through its parameters instead.
9. **Server-side overlay computation** (the previous answer). Once layout isn't the bottleneck, the next bottleneck is your detect-interval; move it to the backend.

### Concrete suggestion for the next 30 minutes

If you want one change that will probably unblock you tonight:

```ts
// raw-graph-canvas.tsx settings
hideEdgesOnMove: true,
```

```ts
// per-component-layout.ts add at top of stepPerComponentLayout
const totalForceMagnitude = /* track sum of |v| over last frame */;
if (graph.order > 200 && totalForceMagnitude < EPSILON) return;
```

And a "cool" timer: after 20 seconds of no new edges, set a `frozen` flag and bail at the top of `stepPerComponentLayout`. Unfreeze when an edge arrives, run for a few seconds, refreeze.

That alone usually takes a 11k/19k graph from "hangs" to "smooth" without touching the algorithm.

### TL;DR

Backend will not save you. The layout is doing O(N²) per component every frame plus O(C²) component-pair checks plus full graphology attribute I/O on every node every frame, and Sigma is redrawing 19k edges during pan. Standard playbook: (1) freeze when stable, (2) Barnes-Hut, (3) WebWorker, (4) hide edges on move, (5) drop dust edges from the visible layer (your blink idea, yes do it). Real "100k smooth" means GPU layout (Cosmograph) or sigma + worker + Barnes-Hut, which is what the products you're benchmarking against are using.



## Points

Right, you're never idle. The "cool when stable" trick only works for graphs that reach equilibrium, and yours never will. So forget freezing the global simulation. The leverage is elsewhere.

### The actual frame in your case

Every 4-5 seconds you get ~1000 edges. That's ~200 edges/sec. Most of those touch wallets that already exist or wallets that appear once and never again. Layout work scales with the **structure that matters**, not with the firehose. So the fix is to stop letting the firehose drive layout work directly.

### What does help when you're never idle

**1. Layout only the components that changed this frame.**
Right now `stepPerComponentLayout` iterates *every* component every frame. With your traffic, almost every frame only ~5-20 components actually got touched by a new edge. Maintain a `dirtyComponents: Set<rootId>` that `applyEdge` writes into when it adds an edge or merges components. Each frame, layout iterates only that set, then clears it. Untouched components are already at equilibrium relative to their internals, leave them alone. This alone usually drops layout cost 50-90x for streaming graphs.

**2. Layout a fixed budget per frame, not "everything."**
Pick a budget like "≤8ms of layout per frame." Iterate dirty components, each one ticked once. If the budget runs out, stop. Remaining dirty components carry over to the next frame. The visual is identical because humans can't see a one-frame delay on a force settling.

**3. Decimate the firehose.**
Even at 200 edges/sec, you don't need to apply 200 layout updates per second. You're animating at 60fps, so:
- Apply edge data and graph mutation as deltas arrive (cheap).
- But only call layout once per frame regardless of how many edges arrived in that frame (you already do this via `requestAnimationFrame`, good).
- For pure visual purposes, if a component got hit by 50 edges this frame, you still only step its layout once. You're doing that. Good.

**4. Coalesce visually-identical edges.**
Most of your firehose is repeat tx between the same pair. You already increment `txCount` and `weight` on existing edges instead of creating new ones (good). But you're calling `refreshEdgeSizes`, `commitEdge`, `nodeSize`, and the whole UF dance for *every* repeat tx. For a hub like a Jito tip, that's hundreds of pointless updates per frame. Throttle this to once-per-component-per-frame.

**5. Drop dust at the data layer.**
The "blink" idea you mentioned earlier. Singleton transfers, two unknown wallets, one tx, never seen again, account for the bulk of your noise. Hold them in a "probationary" buffer for ~5s. If they don't accumulate weight or attach to a known component, never add them to the visible graph. They're in your stats and the backend has them; they just don't enter layout. This is what fraud-graph products call "interesting subgraph filtering" and it's the single biggest scaling lever for transactional graphs.

**6. Pause physics during user interaction (still worth doing).**
Even though you're never idle, the user *is* sometimes idle and sometimes interacting. While they drag, freeze layout for the 200ms of the gesture. Doesn't reduce total CPU much, but kills the "everything wiggles when I touch it" symptom you just observed.

### Order to implement

If I had to pick the single highest-impact change for "1000 edges every 5s, never idle":

> **Dirty-component layout.** Make the layout loop iterate only components touched by edges this frame.

That single change matches industry practice for streaming graph viz (Cosmograph, Graphistry, sigma-at-scale demos all do this). Combined with your existing per-component isolation, you go from "layout cost grows with graph size" to "layout cost grows with churn rate," and churn rate is bounded by attention span (a hub touched 200 times this frame still only ticks once).

After that, dust filtering is what makes the 11k → 50k jump possible. Past that, WebWorker for layout.


## After per component layout change fix


### Path from 30k to 50k+ edges, ranked by leverage

You just got dirty-component scope, which fixed the per-frame cost of layout. The next ceilings are different. Here's what hits next, in order.

---

### 1. Fix the `addEdgeWithKey` slot-reuse bug (correctness, not perf, but blocks everything)

Right now your dev log is spammed with `Graph.addEdgeWithKey: the "X" edge already exists`. Backend reuses `EdgeIdx` from `free_edge_slots` when an edge expires. If `EdgeAdded` for the recycled idx arrives at the frontend before (or coalesced with) the matching `EdgeExpired`, you call `addEdgeWithKey` on a key that's still live.

Symptom looks cosmetic but it's actually corrupting your graph state: the *new* edge silently fails to render, but `idxToPubkey` and component UF still got mutated. Over hours, drift accumulates. Fix this before chasing more frames.

**Two clean options:**
- Backend: don't reuse `EdgeIdx` slots. Make EdgeIdx monotonic, accept slab fragmentation. At 200 edges/sec for hours, slab grows by ~2.5M entries/day, fine for memory.
- Frontend: in `applyEdge`, if `graph.hasEdge(e.signature)` already, treat it as the increment-existing branch instead of throwing.

Backend fix is simpler and correct at the source. Frontend fix is defensive. Do the backend one.

---

### 2. Move layout to a Web Worker

**This is the biggest single jump.** Right now layout, MPC detection, role classification, and Sigma's render *all share the main thread*. Once your dirty-component layout costs >5ms per frame (you'll hit this around 30-40k edges with bigger components), Sigma's 60fps render starts dropping frames even when nothing else is wrong.

Move:
- `stepPerComponentLayout` → worker.
- `detectMpcClusters` → worker (or its own worker).
- `classifyNodes` and stats → same worker.

Main thread becomes thin: it owns Sigma + graphology, receives edge deltas from SSE, forwards them to the worker, and applies position updates the worker sends back via `Float32Array` transferable.

This is the single architectural change that takes you from "smooth at 30k" to "smooth at 100k" because the GPU can already render 100k; the bottleneck is everything else fighting it for the main thread.

Cost: ~1 day. Payoff: 3-5x headroom.

---

### 3. Replace your O(N²) repulsion with Barnes-Hut

Your `MAX_N2_COMPONENT_SIZE = 400` cap means components above 400 nodes get **no pairwise repulsion**. They survive on edge attraction + collision pass alone, which is why dense components compress into knots.

`graphology-layout-forceatlas2` ships a Barnes-Hut implementation. It's O(N log N) and well-tested. Use it inside your dirty-component loop instead of your custom O(N²) inner block. Keep your other custom forces (cross-community boost, megahub rest length, tip-vs-tip) as a *post-pass* on top of FA2's output.

What this unlocks: components in the 400-5000 range start having proper repulsion, which means they spread out instead of clumping, which means edges become readable, which means 50k edges *look like a graph* instead of a smear.

Cost: half a day. Payoff: the 1-5k component visual that's unreadable today becomes readable.

---

### 4. Dust filtering at the visible-graph layer

Your "blink" idea, properly framed.

For a Solana stream, ~60-70% of edges are one-shot dust: two never-before-seen wallets, one tx, never seen again. They contribute zero analytical signal but each one adds:
- A graphology edge (memory + render cost).
- Two graphology nodes (memory + layout cost).
- A new component (UF cost, `pushComponentsApart` cost — this is your O(C²) ceiling).

Right now `pushComponentsApart` is your second-largest cost behind per-component physics. At 31k nodes you probably have 8-12k components. That's 30-70M centroid pair checks per frame.

**Concrete filter:**
- Probationary buffer: an edge between two unknown wallets goes to a holding store, not the graph.
- Promote to graph if: an endpoint accumulates ≥2 edges, OR connects to an existing graph wallet, OR carries volume above a threshold.
- After 5s without promotion, drop entirely (still counted in stats).

Implement as a wrapper around `applyEdge`. The visible graph stays in the 5-15k node range even when the firehose is at 200 edges/sec, because dust filters out by definition.

Cost: half a day. Payoff: drops `pushComponentsApart` cost by ~10x because component count collapses to "real" components.

---

### 5. Spatial hashing for `pushComponentsApart`

Even with dust filtering, this is O(C²). With 1k components it's 500k checks per frame. Replace with a uniform-grid spatial hash: bucket centroids by world-coordinate cells, only check pairs in the same/adjacent cells. Drops to ~O(C) for typical layouts where components don't pile on top of each other.

Cost: 2-3 hours. Payoff: removes a hidden ceiling that hits hard around 2k+ components.

---

### 6. Sigma render config tuning

Cheap wins, do these together:
- `defaultDrawNodeHover`: write a custom one that skips when `hideOnMove` is active.
- `nodeProgramClasses`: switch from default to `NodeCircleProgram` (already default in newest sigma; verify your version).
- `edgeProgramClasses`: ensure you're using the line program, not arrow (arrows cost 2-3x).
- `labelDensity`: you have 0.4. Drop to 0.2 at high node counts.
- `labelRenderedSizeThreshold`: you have 8. Bump to 12 — only render labels for visible-size hubs.
- `zIndex: true` (you have it). Confirm hubs get higher zIndex so they paint on top.

Cost: 1 hour. Payoff: 10-20% render frame budget.

---

### 7. Decimate node-attribute writes

`incAttr` is called ~6-8 times per `applyEdge`. Each call is a graphology hashmap lookup + write. At 200 edges/sec that's ~1500 hashmap ops/sec just for stats. None of these need to be live; the panel polls every 3s.

Move `volume`, `inVol`, `outVol`, `bidirVol`, `selfLoops` off graphology and into a separate `Map<string, NodeStats>` keyed by pubkey. Update during `applyEdge`, read during the 3s detect interval. graphology only stores what Sigma needs: `x, y, size, color, label`.

Cost: 2-3 hours. Payoff: ~2x faster `applyEdge`. Compounding because applyEdge runs hot.

---

### 8. Backend pre-aggregation of edge bumps

Right now every transaction between two existing wallets is a separate SSE `EdgeAdded` event, even though it just increments a counter on an existing edge. For a Jito tip you might get 50 events/sec for the same pair.

Backend can coalesce: if an `EdgeAdded` for `(src, dst)` was emitted in the last 100ms, replace its amount/txCount delta in-place in the broadcast queue. Frontend gets one event per 100ms per pair instead of 50.

Cost: 4-5 hours backend, 0 frontend. Payoff: 5-10x reduction in main-thread `applyEdge` calls during Jito-heavy periods.

---

### 9. (When you eventually need 100k+) GPU layout

If after all the above you want to push past 50-70k, switch to **Cosmograph**. It's WebGL+GPU layout, designed for exactly this. You lose your custom physics tuning but you gain "millions of nodes at 60fps."

Don't do this yet. Steps 1-8 will get you to 50-70k, and the work invested transfers if you ever migrate.

---

## What I'd actually do this week

In order, stopping when it's smooth enough:

1. Slot-reuse bug (prevents data corruption, must do).
2. Stats off graphology (quick, big payoff per edge).
3. Sigma config tuning (1 hour, free wins).
4. Dust filtering (half day, drops O(C²) ceiling).
5. Web Worker for layout + detection (1 day, biggest jump).

That sequence alone gets you smoothly past 50k edges on your current hardware. Barnes-Hut and spatial hash are improvements on top, not requirements for 50k.

