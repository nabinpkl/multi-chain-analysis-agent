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